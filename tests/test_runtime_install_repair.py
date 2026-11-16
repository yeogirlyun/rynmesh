"""Tests for S01-I31: atomic runtime repair swap, tarfile pre-data_filter fallback, and corrupt setup recovery self-healing."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import routes, runtime_native, runtime_native_install, setup_recovery
from rynmesh.llm_package.errors import LifecycleError
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class _MemoryResponse:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.status = 200
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _build_runtime_archive(extra_entries: list[tuple[str, bytes]] | None = None, *, force_tar: bool = False) -> tuple[str, bytes, str]:
    buf = io.BytesIO()
    use_zip = os.name == "nt" and not force_tar
    server_rel = "build/bin/llama-server.exe" if os.name == "nt" else "build/bin/llama-server"
    entries = [
        (server_rel, b"MZ fake binary" if os.name == "nt" else b"#!/bin/sh\nexit 0\n"),
        ("build/bin/ggml-base.lib", b"library-bytes-v1"),
        *(extra_entries or []),
    ]
    if use_zip:
        name = "llama-repair-win.zip"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry_name, content in entries:
                info = zipfile.ZipInfo(entry_name)
                info.external_attr = 0o755 << 16
                zf.writestr(info, content)
    else:
        name = "llama-repair.tar.gz"
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for entry_name, content in entries:
                info = tarfile.TarInfo(entry_name)
                info.size = len(content)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(content))
    payload = buf.getvalue()
    return name, payload, hashlib.sha256(payload).hexdigest()


def _serve_archive(monkeypatch, name: str, payload: bytes, sha: str) -> None:
    monkeypatch.setattr(runtime_native_install, "asset", lambda: (name, sha, len(payload)))

    class _Opener:
        def open(self, _request, timeout=60):
            return _MemoryResponse(payload)

    monkeypatch.setattr(runtime_native_install, "build_https_only_opener", lambda: _Opener())


def test_t1_repair_removes_extra_stale_files_from_previous_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("RYNMESH_LLAMA_SERVER", raising=False)
    monkeypatch.delenv("RYNMESH_LLAMA_DIR", raising=False)
    monkeypatch.setattr(runtime_native.shutil, "which", lambda _name: None)

    root = tmp_path / "llm"
    name1, payload1, sha1 = _build_runtime_archive([("build/bin/legacy_extra.dll", b"old-leftover")])
    _serve_archive(monkeypatch, name1, payload1, sha1)
    runtime_native_install.download(root)

    target = runtime_native_install.managed_root(root)
    stale_root_file = target / "manual_leftover.txt"
    stale_root_file.write_text("should disappear after repair", encoding="utf-8")
    assert (target / "build" / "bin" / "legacy_extra.dll").is_file()
    assert stale_root_file.is_file()

    # Repair with a clean archive that no longer contains `legacy_extra.dll`.
    name2, payload2, sha2 = _build_runtime_archive()
    _serve_archive(monkeypatch, name2, payload2, sha2)
    server = runtime_native_install.download(root)

    assert server.is_file()
    assert server.resolve().is_relative_to(target.resolve())
    assert runtime_native.resolve_server(root) == server
    assert not (target / "build" / "bin" / "legacy_extra.dll").exists()
    assert not stale_root_file.exists()
    assert not target.with_name(target.name + ".new").exists()
    assert not target.with_name(target.name + ".old").exists()


def test_t2_failed_extraction_preserves_old_runtime_directory_and_cleans_staging(tmp_path, monkeypatch):
    monkeypatch.delenv("RYNMESH_LLAMA_SERVER", raising=False)
    monkeypatch.delenv("RYNMESH_LLAMA_DIR", raising=False)
    monkeypatch.setattr(runtime_native.shutil, "which", lambda _name: None)

    root = tmp_path / "llm"
    name1, payload1, sha1 = _build_runtime_archive()
    _serve_archive(monkeypatch, name1, payload1, sha1)
    original_server = runtime_native_install.download(root)

    target = runtime_native_install.managed_root(root)
    original_server_bytes = original_server.read_bytes()
    original_lib_bytes = (target / "build" / "bin" / "ggml-base.lib").read_bytes()
    sentinel = target / "existing_file.dat"
    sentinel.write_bytes(b"must-stay-intact")

    # Serve a corrupt archive that fails during `_extract` after some entries or header read:
    # simulate a mid-extraction failure that already wrote partial data into the extraction dir.
    real_extract = runtime_native_install._extract

    def partial_corrupt_extract(archive: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "build" / "bin").mkdir(parents=True, exist_ok=True)
        (dest / "build" / "bin" / "ggml-base.lib").write_bytes(b"corrupted-partial-overwrite")
        (dest / "half_unpacked.tmp").write_bytes(b"garbage")
        raise LifecycleError(runtime_native_install.UNREADABLE_ARCHIVE)

    monkeypatch.setattr(runtime_native_install, "_extract", partial_corrupt_extract)
    with pytest.raises(LifecycleError, match="unreadable or corrupt"):
        runtime_native_install.download(root)
    monkeypatch.setattr(runtime_native_install, "_extract", real_extract)

    # Old directory contents were never touched by the failed extraction into `.new`.
    assert original_server.read_bytes() == original_server_bytes
    assert (target / "build" / "bin" / "ggml-base.lib").read_bytes() == original_lib_bytes
    assert sentinel.read_bytes() == b"must-stay-intact"
    assert not (target / "half_unpacked.tmp").exists()
    assert not target.with_name(target.name + ".new").exists()
    assert not target.with_name(target.name + ".old").exists()
    assert not (target / runtime_native_install.MARKER_NAME).exists()


def test_t3_tar_extraction_without_data_filter_succeeds_and_still_rejects_unsafe_members(tmp_path, monkeypatch):
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    real_extractall = tarfile.TarFile.extractall

    def legacy_extractall(self, path=".", members=None, *, numeric_owner=False, **kwargs):
        if "filter" in kwargs:
            raise TypeError("TarFile.extractall() got an unexpected keyword argument 'filter'")
        return real_extractall(self, path=path, members=members, numeric_owner=numeric_owner)

    monkeypatch.setattr(tarfile.TarFile, "extractall", legacy_extractall)

    # 1. Valid tar.gz extracts cleanly when `tarfile.data_filter` is absent.
    _, valid_tar_bytes, _ = _build_runtime_archive(force_tar=True)
    valid_archive = tmp_path / "valid.tar.gz"
    valid_archive.write_bytes(valid_tar_bytes)
    extracted_dir = tmp_path / "extracted-ok"
    runtime_native_install._extract(valid_archive, extracted_dir)
    assert (extracted_dir / "build" / "bin" / "ggml-base.lib").read_bytes() == b"library-bytes-v1"

    # 2. Unsafe traversal and symlink members are still rejected before `extractall`.
    traversal_buf = io.BytesIO()
    with tarfile.open(fileobj=traversal_buf, mode="w:gz") as tf:
        bad = tarfile.TarInfo("../escape.txt")
        bad.size = 4
        tf.addfile(bad, io.BytesIO(b"evil"))
    traversal_archive = tmp_path / "traversal.tar.gz"
    traversal_archive.write_bytes(traversal_buf.getvalue())
    with pytest.raises(LifecycleError, match="unsafe member path"):
        runtime_native_install._extract(traversal_archive, tmp_path / "out-traversal")

    symlink_buf = io.BytesIO()
    with tarfile.open(fileobj=symlink_buf, mode="w:gz") as tf:
        sym = tarfile.TarInfo("build/bin/link")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tf.addfile(sym)
    symlink_archive = tmp_path / "symlink.tar.gz"
    symlink_archive.write_bytes(symlink_buf.getvalue())
    with pytest.raises(LifecycleError, match="unsafe member type"):
        runtime_native_install._extract(symlink_archive, tmp_path / "out-symlink")

    # 3. Direct TypeError inside extraction is caught as UNREADABLE_ARCHIVE.
    monkeypatch.setattr(
        tarfile.TarFile,
        "extractall",
        lambda *_a, **_kw: (_ for _ in ()).throw(TypeError("unexpected tarfile type error")),
    )
    with pytest.raises(LifecycleError, match="unreadable or corrupt"):
        runtime_native_install._extract(valid_archive, tmp_path / "out-typeerror")


def test_t4_corrupt_setup_recovery_journal_is_backed_up_clears_pending_and_unblocks_setup(tmp_path):
    # Start a tiny loopback OpenAI-compatible server so `/api/local/llm/setup` can complete end-to-end.
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"object": "list", "data": [{"id": "test-local-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({
                "choices": [{"message": {"content": "RYNMESH SELF TEST OK"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home = tmp_path / "node"
        llm_dir = home / "llm"
        llm_dir.mkdir(parents=True, exist_ok=True)
        corrupt_bytes = b"{corrupt-setup-recovery-journal"
        recovery_file = llm_dir / "setup-recovery.json"
        recovery_file.write_bytes(corrupt_bytes)

        # Direct unit check on SetupRecovery._read / restore first on a separate dir:
        unit_home = tmp_path / "unit-node"
        (unit_home / "llm").mkdir(parents=True, exist_ok=True)
        (unit_home / "llm" / "setup-recovery.json").write_bytes(corrupt_bytes)
        unit_rec = setup_recovery.SetupRecovery(unit_home)
        assert unit_rec.pending is True
        assert unit_rec.restore() == "discarded"
        assert unit_rec.pending is False
        assert not (unit_home / "llm" / "setup-recovery.json").exists()
        unit_backups = list((unit_home / "llm").glob("setup-recovery.json.corrupt-*"))
        assert len(unit_backups) == 1
        assert unit_backups[0].read_bytes() == corrupt_bytes

        # Full route startup + `/api/local/llm/setup` verification:
        store = RynmeshStore(home=home, network_dir=tmp_path / "network")
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        app = FastAPI()
        routes.install_llm_routes(
            app,
            store=store,
            home=home,
            messaging_key=key,
            resolve_endpoint=lambda _: "",
            resolve_pubkey=lambda _: "",
        )
        with TestClient(app) as client:
            assert not recovery_file.exists()
            backups = list(llm_dir.glob("setup-recovery.json.corrupt-*"))
            assert len(backups) == 1
            assert backups[0].read_bytes() == corrupt_bytes

            job_status = client.get("/api/local/llm/setup/status").json()
            assert job_status["state"] == "failed"
            assert job_status["recovery_state"] == "discarded"
            assert job_status["message"] == "上次安装的恢复记录已损坏并已备份，旧配置未能恢复，请重新配置"

            resp = client.post(
                "/api/local/llm/setup",
                json={
                    "mode": "openai-compatible",
                    "package_id": "local-recovered",
                    "alias": "local-recovered",
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "model": "test-local-model",
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["configured"] is True
        app.state.native_model.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
