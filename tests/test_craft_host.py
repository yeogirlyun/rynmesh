"""Pinned editor packaging, hostile archives and isolation from owner APIs."""

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rynmesh.node_apps import craft_host as host
from rynmesh.node_apps.craft_catalog import CRAFT


def bundle(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in {
            "wordcraft/index.html": "<!doctype html><title>WordCraft</title><canvas></canvas>",
            "wordcraft/editor.js": "export {}",
            "wordcraft/editor.wasm": b"\0asm\1\0\0\0",
            "wordcraft/LICENSE-APACHE": "Fixture license retained",
            **(extra or {}),
        }.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.fixture
def fake_release(monkeypatch):
    data = bundle()
    monkeypatch.setitem(
        CRAFT, "wordcraft", {**CRAFT["wordcraft"], "web_sha256": hashlib.sha256(data).hexdigest()}
    )

    def download(url, path):
        assert (
            url
            == "https://github.com/storytold/wordcraft/releases/download/v0.6.1/wordcraft-web-0.6.1.zip"
        )
        Path(path).write_bytes(data)

    return download


def test_install_immutable_pin_restart_and_no_network_during_serve(tmp_path, fake_release):
    assert host.install(tmp_path, "wordcraft", download=fake_release)["state"] == "ready"
    web = host.installed(tmp_path, "wordcraft")
    assert "license retained" in (web / "LICENSE-APACHE").read_text()
    assert (
        host.install(tmp_path, "wordcraft", download=lambda *_: pytest.fail("re-download"))["state"]
        == "ready"
    )
    app = host.create_editor(tmp_path, "wordcraft", public_host="testserver")
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200 and "WordCraft" in response.text
        assert client.get("/editor.wasm").headers["content-type"] == "application/wasm"
        csp = response.headers["content-security-policy"]
        assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "Set-Cookie" not in response.headers
        for path in (
            "/api/local/node-apps",
            "/api/local/friends",
            "/receipt.json",
            "/../receipt.json",
            "/missing.js",
        ):
            assert client.get(path).status_code == 404
        assert client.post("/index.html", json={"upload": "secret"}).status_code == 405
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 400
    assert not (tmp_path / "identity.json").exists()
    (web / "editor.js").write_text("tampered")
    with pytest.raises(host.CraftError, match="not_verified"):
        host.create_editor(tmp_path, "wordcraft")


def test_checksum_failure_never_publishes_and_cleans_stage(tmp_path):
    with pytest.raises(host.CraftError, match="checksum"):
        host.install(tmp_path, "wordcraft", download=lambda u, p: p.write_bytes(b"wrong"))
    assert not (tmp_path / "wordcraft" / CRAFT["wordcraft"]["tag"]).exists()
    assert not list((tmp_path / "wordcraft").glob(".install-*"))


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "C:/absolute", "x\\escape", "wordcraft/../../escape"]
)
def test_hostile_archive_rejected_before_writing(tmp_path, name):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(bundle({name: "bad"}))
    with pytest.raises(host.CraftError, match="unsafe"):
        host.extract(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_duplicate_symlink_and_expansion_limits(tmp_path, monkeypatch):
    archive = tmp_path / "bad.zip"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        z.writestr(link, "../escape")
    archive.write_bytes(output.getvalue())
    with pytest.raises(host.CraftError, match="unsafe"):
        host.extract(archive, tmp_path / "out")
    archive.write_bytes(bundle({"wordcraft/EDITOR.js": "collision"}))
    with pytest.raises(host.CraftError, match="unsafe"):
        host.extract(archive, tmp_path / "out")
    monkeypatch.setattr(host, "MAX_EXPANDED", 4)
    archive.write_bytes(bundle())
    with pytest.raises(host.CraftError, match="limit"):
        host.extract(archive, tmp_path / "out")


def test_native_only_and_unknown_never_download(tmp_path):
    for slug, error in (("filmcraft", "native_only"), ("../../private", "unknown_app")):
        with pytest.raises(host.CraftError, match=error):
            host.install(tmp_path, slug, download=lambda *_: pytest.fail("download"))
