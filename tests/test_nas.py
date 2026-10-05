import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rynmesh.nas import NasError, NasPlugin, relative_path
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


class MemoryConnector:
    buckets = {}
    opened = 0

    def __init__(self, config):
        type(self).opened += 1
        if config["password"] == "wrong":
            raise PermissionError("secret exception with credentials")
        self.files = self.buckets.setdefault(config["share"], {})

    def list(self, path):
        return [
            {"name": k, "path": k, "directory": False, "size": len(v), "modified": 0}
            for k, v in self.files.items()
        ]

    def read(self, path, limit):
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if len(data) > limit:
            raise NasError("file_too_large", 413)
        return data

    def write(self, path, data):
        if path in self.files:
            raise FileExistsError(path)
        self.files[path] = data

    def mkdir(self, path):
        pass

    def close(self):
        pass


@pytest.fixture
def plugin(tmp_path):
    MemoryConnector.buckets = {}
    MemoryConnector.opened = 0
    p = NasPlugin(tmp_path)
    p.connectors["smb"] = MemoryConnector
    return p


def config(**kwargs):
    return dict(
        name="Home NAS",
        system="fnos",
        protocol="smb",
        host="nas.local",
        share="documents",
        username="tester",
        password="private-secret",
        **kwargs,
    )


def add(plugin, **kwargs):
    plugin.enable(True)
    return plugin.configure(config(**kwargs))["id"]


def test_disabled_never_opens_connections_and_persists(plugin, tmp_path):
    assert not plugin.status()["enabled"]
    with pytest.raises(NasError, match="disabled"):
        plugin.configure(config())
    assert MemoryConnector.opened == 0
    source = add(plugin, writable=True)
    plugin.operate(source, "write", "acceptance.txt", b"hello")
    plugin.enable(False)
    opened = MemoryConnector.opened
    for action in ["list", "read", "write", "ai", "mkdir"]:
        with pytest.raises(NasError, match="disabled"):
            plugin.operate(source, action, "acceptance.txt")
    assert opened == MemoryConnector.opened
    restored = NasPlugin(tmp_path)
    assert not restored.status()["enabled"]
    assert len(restored.status()["sources"]) == 1
    assert "private-secret" not in json.dumps(plugin.status())
    assert b"private-secret" not in (tmp_path / "plugins/nas/state.enc").read_bytes()
    plugin.enable(True)
    assert plugin.operate(source, "read", "acceptance.txt") == b"hello"


def test_multi_source_isolation_readonly_ai_and_exclusive_write(plugin):
    a = add(plugin, writable=True, allow_ai=True)
    cfg = config()
    cfg.update(share="other", system="synology", name="Office")
    b = plugin.configure(cfg)["id"]
    plugin.operate(a, "write", "报告.txt", "synthetic report".encode())
    assert plugin.operate(a, "ai", "报告.txt")["text"] == "synthetic report"
    assert plugin.operate(b, "list") == []
    with pytest.raises(NasError, match="read_only"):
        plugin.operate(b, "write", "x", b"data")
    with pytest.raises(NasError, match="ai_not_allowed"):
        plugin.operate(b, "ai", "x")
    with pytest.raises(NasError, match="file_exists"):
        plugin.operate(a, "write", "报告.txt", b"replacement")
    plugin.remove(a)
    with pytest.raises(NasError, match="not_found"):
        plugin.operate(a, "read", "报告.txt")
    assert MemoryConnector.buckets["documents"]["报告.txt"] == b"synthetic report"


@pytest.mark.parametrize(
    "path", ["../secret", "a/../secret", "/etc/passwd", "C:/secret", "a\\b", "a\x00b", "a/./b"]
)
def test_paths_reject_escape(path):
    with pytest.raises(NasError, match="invalid_path"):
        relative_path(path)


def test_failed_connection_not_saved_and_errors_redacted(plugin):
    plugin.enable(True)
    cfg = config()
    cfg["password"] = "wrong"
    with pytest.raises(NasError, match="nas_access_denied") as caught:
        plugin.configure(cfg)
    assert "credentials" not in str(caught.value)
    assert plugin.status()["sources"] == []


def test_smb_library_errno_types_for_missing_and_existing_files(plugin, monkeypatch):
    import smbclient
    from smbprotocol.exceptions import SMBOSError
    from smbprotocol.header import NtStatus

    from rynmesh.nas import SMBConnector

    def missing(*args, **kwargs):
        raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, "synthetic")

    monkeypatch.setattr(smbclient, "register_session", lambda *a, **kw: None)
    monkeypatch.setattr(smbclient, "stat", missing)
    connector = SMBConnector({**config(), "root": "", "port": 445})
    assert connector.path("new-folder") == r"\\nas.local\documents\new-folder"

    class Existing(MemoryConnector):
        def write(self, path, data):
            raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_COLLISION, path)

        def read(self, path, limit):
            return missing()

    source = add(plugin, writable=True)
    plugin.connectors["smb"] = Existing
    with pytest.raises(NasError, match="file_exists") as caught:
        plugin.operate(source, "write", "existing.txt", b"keep original")
    assert caught.value.status == 409
    with pytest.raises(NasError, match="file_not_found") as caught:
        plugin.operate(source, "read", "missing.txt")
    assert caught.value.status == 404


def test_smb_listing_keeps_nonstandard_port_and_private_session(monkeypatch):
    from types import SimpleNamespace

    import smbclient

    from rynmesh.nas import SMBConnector

    calls = []
    monkeypatch.setattr(smbclient, "register_session", lambda *a, **kw: None)
    monkeypatch.setattr(
        smbclient,
        "scandir",
        lambda *a, **kw: [SimpleNamespace(name="报告.txt", path=r"\\nas.local\documents\报告.txt")],
    )

    def stat(path, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(st_file_attributes=0x20, st_size=8, st_mtime=0)

    monkeypatch.setattr(smbclient, "stat", stat)
    connector = SMBConnector({**config(), "root": "", "port": 1445})
    assert connector.list("")[0]["name"] == "报告.txt"
    assert calls[0]["port"] == 1445
    assert calls[0]["connection_cache"] is connector.cache


def test_disable_waits_for_operation_and_prevents_next(plugin):
    source = add(plugin, writable=True)
    entered, finish = threading.Event(), threading.Event()

    class Blocking(MemoryConnector):
        def read(self, path, limit):
            entered.set()
            assert finish.wait(5)
            return b"complete"

    plugin.connectors["smb"] = Blocking
    with ThreadPoolExecutor() as pool:
        reading = pool.submit(plugin.operate, source, "read", "file")
        assert entered.wait(5)
        disabling = pool.submit(plugin.enable, False)
        assert not disabling.done()
        finish.set()
        assert reading.result() == b"complete"
        assert disabling.result()["enabled"] is False
    with pytest.raises(NasError, match="disabled"):
        plugin.operate(source, "read", "file")


def test_http_auth_and_file_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    store = RynmeshStore(home=tmp_path, network_dir=tmp_path / "network")
    app = create_app(store)
    plugin = app.state.nas_plugin
    MemoryConnector.buckets = {}
    plugin.connectors["smb"] = MemoryConnector
    with TestClient(app) as client:
        base = "/api/local/plugins/nas"
        assert client.get(base).json()["enabled"] is False
        assert client.put(base, json={"enabled": "true"}).status_code == 400
        assert (
            client.put(
                base, json={"enabled": True}, headers={"x-forwarded-for": "203.0.113.2"}
            ).status_code
            == 401
        )
        client.put(base, json={"enabled": True})
        response = client.post(base + "/sources", json=config(writable=True, allow_ai=True))
        assert response.status_code == 200, response.text
        source = response.json()["id"]
        url = f"{base}/sources/{source}/content"
        assert client.put(url, params={"path": "验收.txt"}, content=b"nas test").status_code == 200
        assert client.get(url, params={"path": "验收.txt"}).content == b"nas test"
        assert (
            client.get(url, params={"path": "验收.txt", "mode": "preview"}).json()["text"]
            == "nas test"
        )
        assert (
            client.get(url, params={"path": "验收.txt", "mode": "ai"}).json()["text"] == "nas test"
        )
        assert client.put(url, params={"path": "验收.txt"}, content=b"overwrite").status_code == 409
        assert client.get(url, params={"path": "../secret"}).status_code == 400
        picture = io.BytesIO()
        Image.new("RGB", (16, 8)).save(picture, format="PNG")
        assert (
            client.put(url, params={"path": "photo.png"}, content=picture.getvalue()).status_code
            == 200
        )
        response = client.get(
            url,
            params={"path": "photo.png", "mode": "image"},
            headers={"Origin": "http://tauri.localhost"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["x-image-width"] == "16"
        assert response.headers["cache-control"] == "no-store"
        assert "X-Image-Width" in response.headers["access-control-expose-headers"]
        assert client.get(url, params={"path": "验收.txt", "mode": "thumbnail"}).status_code == 415
        client.put(base, json={"enabled": False})
        assert client.get(url, params={"path": "验收.txt"}).status_code == 409


def test_webdav_paths_and_untrusted_xml():
    from rynmesh.nas import WebDAVConnector

    connector = WebDAVConnector(
        {"url": "http://localhost:9999/dav", "root": "work", "username": "x", "password": "y"}
    )
    try:
        assert connector.url("报告.txt").endswith("/work/%E6%8A%A5%E5%91%8A.txt")
        with pytest.raises(NasError):
            connector.url("%2e%2e/secret")
        connector.request = lambda *a, **k: b'<!DOCTYPE test [<!ENTITY a "bad">]><x />'
        with pytest.raises(NasError, match="invalid_directory"):
            connector.list("")
    finally:
        connector.close()


def test_webdav_protocol_listing_auth_and_no_redirects():
    import httpx

    from rynmesh.nas import WebDAVConnector

    seen = []

    def respond(request):
        seen.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=b"""<d:multistatus xmlns:d="DAV:">
              <d:response><d:href>/dav/work/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
              <d:response><d:href>/dav/work/report.txt</d:href><d:propstat><d:prop><d:getcontentlength>4</d:getcontentlength></d:prop></d:propstat></d:response>
              <d:response><d:href>/outside/secret.txt</d:href><d:propstat><d:prop/></d:propstat></d:response>
            </d:multistatus>""",
            )
        if request.method == "PUT":
            return httpx.Response(412)
        return httpx.Response(302, headers={"location": "http://other-host/secret"})

    connector = WebDAVConnector(
        {"url": "http://nas/dav", "root": "work", "username": "a", "password": "b"}
    )
    connector.client.close()
    connector.client = httpx.Client(
        transport=httpx.MockTransport(respond), auth=("a", "b"), follow_redirects=False
    )
    try:
        entries = connector.list("")
        assert [e["name"] for e in entries] == ["report.txt"]
        assert seen[0].headers["depth"] == "1"
        assert seen[0].headers["authorization"].startswith("Basic ")
        with pytest.raises(NasError, match="exists"):
            connector.write("report.txt", b"data")
        assert seen[-1].headers["if-none-match"] == "*"
        with pytest.raises(NasError, match="request_failed"):
            connector.read("report.txt", 100)
        assert len(seen) == 3
    finally:
        connector.close()


def test_image_preview_sanitizes_and_bounds_dimensions(plugin, monkeypatch):
    import rynmesh.nas as nas

    source = add(plugin, writable=True)
    output = io.BytesIO()
    Image.new("RGB", (640, 400), "#abcdef").save(output, format="PNG")
    original = output.getvalue()
    plugin.operate(source, "write", "photo.png", original)
    for mode, expected in [("image", (640, 400)), ("thumbnail", (160, 100))]:
        raw, width, height = plugin.operate(source, mode, "photo.png")
        assert (width, height) == (640, 400)
        with Image.open(io.BytesIO(raw)) as image:
            assert image.format == "WEBP"
            assert image.size == expected
            assert not image.getexif()
    assert plugin.operate(source, "read", "photo.png") == original
    monkeypatch.setattr(nas, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(NasError, match="40_megapixels") as exc:
        plugin.operate(source, "image", "photo.png")
    assert exc.value.status == 413
    monkeypatch.setattr(nas, "MAX_IMAGE", 10)
    with pytest.raises(NasError, match="too_large"):
        plugin.operate(source, "thumbnail", "photo.png")


@pytest.mark.parametrize(
    "raw", [b'<svg onload="alert(1)"></svg>', b"<html>not an image</html>", b"\x89PNG\r\n\x1a\n"]
)
def test_image_preview_rejects_active_or_broken_content(plugin, raw):
    source = add(plugin, writable=True)
    plugin.operate(source, "write", "fake.png", raw)
    with pytest.raises(NasError) as exc:
        plugin.operate(source, "image", "fake.png")
    assert exc.value.status == 415
    plugin.enable(False)
    with pytest.raises(NasError, match="disabled"):
        plugin.operate(source, "thumbnail", "fake.png")
