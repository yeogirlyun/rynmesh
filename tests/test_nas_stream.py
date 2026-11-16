"""S05-I43 tests: chunked streaming NAS download/upload, configurable max_file_bytes, disconnect cleanup, and cloud fallback."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI

import rynmesh.nas as nas_mod
from rynmesh.nas import MAX_FILE, NasError, NasPlugin, WebDAVConnector, mount_nas

ONE_MIB = 1024 * 1024
HUNDRED_MIB = 100 * ONE_MIB


class _StreamingMockTransport(httpx.BaseTransport):
    """Sync httpx transport that preserves unbuffered request.stream without calling request.read()."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


async def _asgi_stream_get_sha_and_peak(target_app: FastAPI, path: str, query: dict[str, str]) -> tuple[int, str, int, int, str | None]:
    """Drive FastAPI/Starlette directly via ASGI so test client does not buffer 100 MiB in RAM."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": urlencode(query).encode("ascii"),
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    sent_request = False
    status_code = 0
    content_length: str | None = None
    digest = hashlib.sha256()
    total = 0

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status_code, content_length, total
        if message["type"] == "http.response.start":
            status_code = message["status"]
            for k, v in message.get("headers", []):
                if k.lower() == b"content-length":
                    content_length = v.decode("latin1")
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                digest.update(body)
                total += len(body)

    tracemalloc.start()
    tracemalloc.reset_peak()
    base_cur, _ = tracemalloc.get_traced_memory()
    try:
        await target_app(scope, receive, send)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return status_code, digest.hexdigest(), total, max(0, peak - base_cur), content_length


def _deterministic_chunk(index: int, size: int = ONE_MIB) -> bytes:
    seed = hashlib.sha256(f"nas-stream-block-{index}".encode("ascii")).digest()
    repeats = (size + len(seed) - 1) // len(seed)
    return (seed * repeats)[:size]


def _expected_100mib_sha256() -> str:
    h = hashlib.sha256()
    for idx in range(100):
        h.update(_deterministic_chunk(idx))
    return h.hexdigest()


class _VirtualLargeSMBFile:
    """100 MiB deterministic read-only SMB file that generates data on demand without storing 100 MiB in RAM."""

    def __init__(self, total_size: int = HUNDRED_MIB) -> None:
        self.total_size = total_size
        self.pos = 0
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if self.pos >= self.total_size:
            return b""
        if n is None or n < 0:
            n = self.total_size - self.pos
        take = min(n, self.total_size - self.pos)
        out = bytearray()
        while len(out) < take:
            block_idx = self.pos // ONE_MIB
            block_off = self.pos % ONE_MIB
            blk = _deterministic_chunk(block_idx)
            piece = blk[block_off : min(ONE_MIB, block_off + (take - len(out)))]
            out.extend(piece)
            self.pos += len(piece)
        return bytes(out)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


class _HashingSinkSMBFile:
    """SMB write handle that records chunk sizes and incremental SHA-256 without retaining 100 MiB in test memory."""

    def __init__(self, store: dict, path: str) -> None:
        self._store = store
        self._path = path
        self.hasher = hashlib.sha256()
        self.size = 0
        self.chunk_sizes: list[int] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.chunk_sizes.append(len(data))
        self.hasher.update(data)
        self.size += len(data)
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._store[self._path] = {
                "sha256": self.hasher.hexdigest(),
                "size": self.size,
                "chunk_sizes": list(self.chunk_sizes),
            }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def _make_fake_smb_api():
    state = {
        "virtual_files": {},
        "written": {},
        "open_handles": [],
        "reset_calls": 0,
    }

    class FakeSMBAPI:
        @staticmethod
        def register_session(*_a, **_kw):
            return None

        @staticmethod
        def stat(unc_path: str, **_kw):
            rel = unc_path.split("\\share\\", 1)[1].replace("\\", "/") if "\\share\\" in unc_path else ""
            if rel == "":
                return SimpleNamespace(st_file_attributes=0x10, st_size=0, st_mtime=1.0)
            if rel in state["virtual_files"]:
                return SimpleNamespace(st_file_attributes=0x20, st_size=state["virtual_files"][rel], st_mtime=1.0)
            if unc_path in state["written"]:
                return SimpleNamespace(st_file_attributes=0x20, st_size=state["written"][unc_path]["size"], st_mtime=1.0)
            raise OSError(errno.ENOENT, "not found")

        @staticmethod
        def scandir(_unc_path: str, **_kw):
            return []

        @staticmethod
        def open_file(unc_path: str, mode: str = "rb", **_kw):
            if mode == "rb":
                rel = unc_path.split("\\share\\", 1)[1].replace("\\", "/") if "\\share\\" in unc_path else ""
                if rel not in state["virtual_files"]:
                    raise OSError(errno.ENOENT, "not found")
                handle = _VirtualLargeSMBFile(state["virtual_files"][rel])
                state["open_handles"].append(handle)
                return handle
            if mode == "xb":
                if unc_path in state["written"]:
                    raise OSError(errno.EEXIST, "exists")
                return _HashingSinkSMBFile(state["written"], unc_path)
            raise ValueError(f"unexpected mode {mode}")

        @staticmethod
        def rename(src: str, dst: str, **_kw):
            if dst in state["written"]:
                raise OSError(errno.EEXIST, "destination exists")
            state["written"][dst] = state["written"].pop(src)

        @staticmethod
        def remove(target: str, **_kw):
            if target not in state["written"]:
                raise OSError(errno.ENOENT, "already gone")
            state["written"].pop(target, None)

        @staticmethod
        def reset_connection_cache(**_kw):
            state["reset_calls"] += 1

    return FakeSMBAPI, state


def _make_nas_app(tmp_path: Path) -> tuple[FastAPI, NasPlugin]:
    app = FastAPI()
    mount_nas(app, tmp_path, local_control=lambda: None)
    return app, app.state.nas_plugin


def test_t1_100mib_streaming_download_smb_and_webdav_peak_memory_under_20mib(tmp_path, monkeypatch):
    """T1 (Changes 1, 2, 3, 4): 100 MiB download over both SMBConnector and WebDAVConnector keeps peak memory delta < 20 MiB, sets Content-Length, and matches SHA-256."""
    expected_sha = _expected_100mib_sha256()

    # --- Part A: SMBConnector 100 MiB download via FastAPI route ---
    fake_smb_cls, smb_state = _make_fake_smb_api()
    smb_state["virtual_files"]["video.mp4"] = HUNDRED_MIB
    monkeypatch.setitem(os.sys.modules, "smbclient", fake_smb_cls)

    app_smb, plugin_smb = _make_nas_app(tmp_path / "smb-home")
    plugin_smb.enable(True)
    smb_src = plugin_smb.configure(
        {
            "name": "SMB",
            "system": "fnos",
            "protocol": "smb",
            "host": "nas.local",
            "share": "share",
            "username": "u",
            "password": "p",
            "writable": True,
        }
    )["id"]

    smb_status, smb_sha, smb_total, smb_peak_delta, smb_cl = asyncio.run(
        _asgi_stream_get_sha_and_peak(
            app_smb,
            f"/api/local/plugins/nas/sources/{smb_src}/content",
            {"path": "video.mp4"},
        )
    )
    assert smb_status == 200
    assert smb_total == HUNDRED_MIB
    assert smb_sha == expected_sha
    assert smb_cl == str(HUNDRED_MIB)
    assert smb_peak_delta < 20 * ONE_MIB, f"SMB peak memory delta too high: {smb_peak_delta}"

    # --- Part B: WebDAVConnector 100 MiB download via FastAPI route ---
    class _WebDAV100MStream(httpx.SyncByteStream):
        def __iter__(self):
            for idx in range(100):
                yield _deterministic_chunk(idx)

    def _dav_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            depth = request.headers.get("depth", "1")
            if depth == "0" and request.url.path.endswith("/installer.iso"):
                xml = (
                    '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/installer.iso</d:href>'
                    f"<d:propstat><d:prop><d:getcontentlength>{HUNDRED_MIB}</d:getcontentlength></d:prop></d:propstat>"
                    "</d:response></d:multistatus>"
                ).encode("utf-8")
                return httpx.Response(207, content=xml)
            xml = (
                b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/</d:href>'
                b"<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>"
                b"</d:response></d:multistatus>"
            )
            return httpx.Response(207, content=xml)
        if request.method == "GET" and request.url.path.endswith("/installer.iso"):
            return httpx.Response(
                200,
                headers={"Content-Length": str(HUNDRED_MIB)},
                stream=_WebDAV100MStream(),
            )
        return httpx.Response(404)

    def _make_mock_dav(cfg):
        conn = WebDAVConnector(cfg)
        conn.client.close()
        conn.client = httpx.Client(transport=_StreamingMockTransport(_dav_handler), auth=("u", "p"), follow_redirects=False)
        return conn

    app_dav, plugin_dav = _make_nas_app(tmp_path / "dav-home")
    plugin_dav.connectors["webdav"] = _make_mock_dav
    plugin_dav.enable(True)
    dav_src = plugin_dav.configure(
        {
            "name": "DAV",
            "system": "synology",
            "protocol": "webdav",
            "url": "http://nas.local/dav",
            "username": "u",
            "password": "p",
            "writable": True,
        }
    )["id"]

    dav_status, dav_sha, dav_total, dav_peak_delta, dav_cl = asyncio.run(
        _asgi_stream_get_sha_and_peak(
            app_dav,
            f"/api/local/plugins/nas/sources/{dav_src}/content",
            {"path": "installer.iso"},
        )
    )
    assert dav_status == 200
    assert dav_total == HUNDRED_MIB
    assert dav_sha == expected_sha
    assert dav_cl == str(HUNDRED_MIB)
    assert dav_peak_delta < 20 * ONE_MIB, f"WebDAV peak memory delta too high: {dav_peak_delta}"


def test_t2_client_midstream_disconnect_closes_oneshot_connector_and_stream(tmp_path):
    """T2 (Changes 3, 4): Closing the download stream midway closes both the active read generator and the one-shot connector."""
    closed_connectors: list[bool] = []
    closed_streams: list[bool] = []

    class _DisconnectConnector:
        def __init__(self, _cfg):
            self.closed = False

        def list(self, _path):
            return []

        def stat_size(self, _path):
            return HUNDRED_MIB

        def iter_read(self, _path, chunk=ONE_MIB):
            def _gen():
                try:
                    for idx in range(100):
                        yield _deterministic_chunk(idx, chunk)
                finally:
                    closed_streams.append(True)

            return _gen()

        def close(self):
            self.closed = True
            closed_connectors.append(True)

    app, plugin = _make_nas_app(tmp_path / "disc-home")
    plugin.connectors["smb"] = _DisconnectConnector
    plugin.enable(True)
    src_id = plugin.configure(
        {
            "name": "Disc",
            "system": "fnos",
            "protocol": "smb",
            "host": "nas.local",
            "share": "s",
            "username": "u",
            "password": "p",
        }
    )["id"]
    closed_connectors.clear()
    closed_streams.clear()

    # 1) Direct operate_stream early close
    conn_obj, iterator = plugin.operate_stream(src_id, "read", "big.bin")
    first = next(iterator)
    assert len(first) == ONE_MIB
    iterator.close()
    assert conn_obj.closed is True
    assert closed_streams == [True]
    assert closed_connectors == [True]
    assert src_id not in plugin.sessions

    # 2) HTTP client disconnects mid-stream after receiving 2 MiB
    closed_connectors.clear()
    closed_streams.clear()
    yielded_chunks = 0
    orig_iter_read = _DisconnectConnector.iter_read

    def _counting_iter_read(self, path_arg, chunk=ONE_MIB):
        gen = orig_iter_read(self, path_arg, chunk)

        def _wrap():
            nonlocal yielded_chunks
            try:
                for blk in gen:
                    yielded_chunks += 1
                    yield blk
            finally:
                gen.close()

        return _wrap()

    _DisconnectConnector.iter_read = _counting_iter_read

    async def _abort_after_2mib():
        path = f"/api/local/plugins/nas/sources/{src_id}/content"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"path=big.bin",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 50001),
            "server": ("127.0.0.1", 80),
        }
        sent_req = False
        seen = 0

        async def receive():
            nonlocal sent_req
            if not sent_req:
                sent_req = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal seen
            if message["type"] == "http.response.body":
                seen += len(message.get("body", b""))
                if seen >= 2 * ONE_MIB:
                    raise OSError("client disconnected mid-download")

        try:
            await app(scope, receive, send)
        except Exception:
            pass

    asyncio.run(_abort_after_2mib())
    assert yielded_chunks <= 5, f"Stream kept reading after disconnect: {yielded_chunks}"
    assert closed_streams == [True]
    assert closed_connectors == [True]


def test_t3_upload_100mib_streams_chunks_to_smb_and_webdav_with_matching_sha256(tmp_path, monkeypatch):
    """T3 (Changes 2, 5): Uploading 100 MiB spools to disk and invokes write_stream in 1 MiB chunks on both SMB and WebDAV connectors with exact SHA-256."""
    expected_sha = _expected_100mib_sha256()

    # --- Part A: SMBConnector 100 MiB upload ---
    fake_smb_cls, smb_state = _make_fake_smb_api()
    monkeypatch.setitem(os.sys.modules, "smbclient", fake_smb_cls)
    app_smb, plugin_smb = _make_nas_app(tmp_path / "up-smb")
    plugin_smb.enable(True)
    smb_src = plugin_smb.configure(
        {
            "name": "UpSMB",
            "system": "fnos",
            "protocol": "smb",
            "host": "nas.local",
            "share": "share",
            "username": "u",
            "password": "p",
            "writable": True,
        }
    )["id"]

    async def _upload_100mib(target_app: FastAPI, source_id: str, remote_name: str) -> httpx.Response:
        async def _body_gen():
            for idx in range(100):
                yield _deterministic_chunk(idx)

        transport = httpx.ASGITransport(app=target_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.put(
                f"/api/local/plugins/nas/sources/{source_id}/content",
                params={"path": remote_name},
                content=_body_gen(),
            )

    resp_smb = asyncio.run(_upload_100mib(app_smb, smb_src, "archive.tar"))
    assert resp_smb.status_code == 200, resp_smb.text
    assert resp_smb.json()["entry"]["size"] == HUNDRED_MIB
    dest_unc = r"\\nas.local\share\archive.tar"
    assert dest_unc in smb_state["written"]
    written_meta = smb_state["written"][dest_unc]
    assert written_meta["size"] == HUNDRED_MIB
    assert written_meta["sha256"] == expected_sha
    assert len(written_meta["chunk_sizes"]) == 100
    assert all(sz == ONE_MIB for sz in written_meta["chunk_sizes"])
    assert not any(".ryn-upload-" in k for k in smb_state["written"])

    # --- Part B: WebDAVConnector 100 MiB upload ---
    dav_put_records: list[dict] = []

    def _dav_put_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=(
                    b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/</d:href>'
                    b"<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>"
                    b"</d:response></d:multistatus>"
                ),
            )
        if request.method == "PUT":
            h = hashlib.sha256()
            sizes = []
            for part in request.stream:
                sizes.append(len(part))
                h.update(part)
            dav_put_records.append(
                {
                    "sha256": h.hexdigest(),
                    "chunk_count": len(sizes),
                    "total": sum(sizes),
                    "content_length": request.headers.get("content-length"),
                    "if_none_match": request.headers.get("if-none-match"),
                }
            )
            return httpx.Response(201)
        return httpx.Response(404)

    def _make_dav(cfg):
        conn = WebDAVConnector(cfg)
        conn.client.close()
        conn.client = httpx.Client(transport=_StreamingMockTransport(_dav_put_handler), auth=("u", "p"), follow_redirects=False)
        return conn

    app_dav, plugin_dav = _make_nas_app(tmp_path / "up-dav")
    plugin_dav.connectors["webdav"] = _make_dav
    plugin_dav.enable(True)
    dav_src = plugin_dav.configure(
        {
            "name": "UpDAV",
            "system": "synology",
            "protocol": "webdav",
            "url": "http://nas.local/dav",
            "username": "u",
            "password": "p",
            "writable": True,
        }
    )["id"]

    resp_dav = asyncio.run(_upload_100mib(app_dav, dav_src, "backup.img"))
    assert resp_dav.status_code == 200, resp_dav.text
    assert len(dav_put_records) == 1
    assert dav_put_records[0]["total"] == HUNDRED_MIB
    assert dav_put_records[0]["sha256"] == expected_sha
    assert dav_put_records[0]["chunk_count"] == 100
    assert dav_put_records[0]["content_length"] == str(HUNDRED_MIB)
    assert dav_put_records[0]["if_none_match"] == "*"


def test_t4_upload_exceeding_max_file_bytes_returns_413_and_leaves_no_residual_file(tmp_path, monkeypatch):
    """T4 (Changes 1, 5): Uploading past configured max_file_bytes returns 413 without leaving target or temp files on NAS or local tmp."""
    fake_smb_cls, smb_state = _make_fake_smb_api()
    monkeypatch.setitem(os.sys.modules, "smbclient", fake_smb_cls)
    app, plugin = _make_nas_app(tmp_path / "limit-home")
    plugin.enable(True)
    plugin.state["max_file_bytes"] = 4 * ONE_MIB
    plugin.save()
    src_id = plugin.configure(
        {
            "name": "LimSMB",
            "system": "fnos",
            "protocol": "smb",
            "host": "nas.local",
            "share": "share",
            "username": "u",
            "password": "p",
            "writable": True,
        }
    )["id"]

    async def _upload_over_limit():
        async def _six_mib():
            for idx in range(6):
                yield _deterministic_chunk(idx)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.put(
                f"/api/local/plugins/nas/sources/{src_id}/content",
                params={"path": "too-big.bin"},
                content=_six_mib(),
            )

    resp = asyncio.run(_upload_over_limit())
    assert resp.status_code == 413
    assert smb_state["written"] == {}
    tmp_dir = tmp_path / "limit-home" / "plugins" / "nas" / "tmp"
    if tmp_dir.exists():
        assert list(tmp_dir.iterdir()) == []


def test_t5_cloud_connector_falls_back_to_whole_buffer_and_enforces_64mib_limit(tmp_path):
    """T5 (Changes 1, 2, 5): Cloud connectors without iter_read/write_stream use existing read/write path and remain capped at 64 MiB."""
    cloud_calls: list[tuple[str, str, int]] = []
    cloud_files: dict[str, bytes] = {"small.txt": b"cloud-hello"}

    class _FakeCloudConnector:
        def __init__(self, cfg):
            self.config = cfg

        def list(self, _path):
            return []

        def read(self, path, limit):
            cloud_calls.append(("read", path, limit))
            data = cloud_files[path]
            if len(data) > limit:
                raise NasError("file_too_large", 413)
            return data

        def write(self, path, data):
            cloud_calls.append(("write", path, len(data)))
            cloud_files[path] = data
            return {"id": "cloud-file-1"}

        def close(self):
            return None

    app, plugin = _make_nas_app(tmp_path / "cloud-home")
    plugin.connectors["google"] = _FakeCloudConnector
    plugin.enable(True)
    with plugin.lock:
        plugin.state["sources"].append(
            {
                "id": "gdrive-1",
                "name": "GDrive",
                "system": "google",
                "protocol": "google",
                "root": "",
                "writable": True,
                "write_authorized": True,
                "allow_ai": False,
                "access_generation": "gen1",
            }
        )
        plugin.save()

    async def _exercise_cloud():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r_get = await client.get(
                "/api/local/plugins/nas/sources/gdrive-1/content",
                params={"path": "small.txt"},
            )
            assert r_get.status_code == 200
            assert r_get.content == b"cloud-hello"

            r_put_ok = await client.put(
                "/api/local/plugins/nas/sources/gdrive-1/content",
                params={"path": "uploaded.txt"},
                content=b"under-limit",
            )
            assert r_put_ok.status_code == 200

            async def _over_64mib():
                for _ in range(65):
                    yield b"a" * ONE_MIB

            r_put_big = await client.put(
                "/api/local/plugins/nas/sources/gdrive-1/content",
                params={"path": "over64m.bin"},
                content=_over_64mib(),
            )
            assert r_put_big.status_code == 413

    asyncio.run(_exercise_cloud())
    assert ("read", "small.txt", MAX_FILE) in cloud_calls
    assert ("write", "uploaded.txt", len(b"under-limit")) in cloud_calls
    assert "over64m.bin" not in cloud_files

    st = plugin.status()
    cloud_src_view = next(s for s in st["sources"] if s["id"] == "gdrive-1")
    assert cloud_src_view["max_file_bytes"] == MAX_FILE


def test_t6_status_max_file_bytes_reflects_default_configuration_and_tmp_startup_cleanup(tmp_path):
    """T6 (Change 1): status()['max_file_bytes'] defaults to STREAM_MAX_FILE (4 GiB), reflects configured overrides across restart, distinguishes protocols, and cleans tmp on startup."""
    home = tmp_path / "cfg-home"
    stale_tmp = home / "plugins" / "nas" / "tmp"
    stale_tmp.mkdir(parents=True, exist_ok=True)
    (stale_tmp / "leftover.part").write_bytes(b"stale")

    app, plugin = _make_nas_app(home)
    assert nas_mod.STREAM_MAX_FILE == 4 * 1024 * 1024 * 1024
    assert not (stale_tmp / "leftover.part").exists()
    assert plugin.status()["max_file_bytes"] == nas_mod.STREAM_MAX_FILE

    custom_limit = 512 * ONE_MIB

    async def _configure_limit():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.put(
                "/api/local/plugins/nas",
                json={"enabled": True, "max_file_bytes": custom_limit},
            )
            assert r.status_code == 200
            return r.json()

    updated_status = asyncio.run(_configure_limit())
    assert updated_status["max_file_bytes"] == custom_limit
    assert updated_status["max_file_bytes_by_protocol"]["smb"] == custom_limit
    assert updated_status["max_file_bytes_by_protocol"]["webdav"] == custom_limit
    assert updated_status["max_file_bytes_by_protocol"]["google"] == MAX_FILE

    reloaded = NasPlugin(home)
    assert reloaded.status()["max_file_bytes"] == custom_limit
