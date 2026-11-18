"""Executable public releases: trust, lifecycle, migration and private-channel separation."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rynmesh.crypto import sign_payload
from rynmesh.node_sites.client import SiteClient
from rynmesh.node_sites.store import SiteStore
from rynmesh.public_plugins.manager import PublicPlugins
from rynmesh.public_plugins.packages import PluginError, verify_catalog
from scripts.build_public_plugin import build

PACKAGE = Path(__file__).resolve().parents[1] / "plugins/image-studio"


@pytest.fixture
def setup(tmp_path):
    key = Ed25519PrivateKey.generate().private_bytes_raw()
    _, archive, signed = build(PACKAGE, revision="a" * 40, key=key)
    store = SiteStore(tmp_path)
    sites = SimpleNamespace(store=store, engine=SimpleNamespace(me="me"),
                            host=SimpleNamespace(builtins={}, summaries={}, revoke_cache=lambda: None),
                            client=SimpleNamespace(), gateway=SimpleNamespace(url=lambda k: f"http://{k}.site.localhost:18150/"))
    data = {"catalog": signed, "archive": archive}
    requests = []

    def fetch(url, limit):
        requests.append(url)
        return json.dumps(data["catalog"]).encode() if url == "https://catalog.test" else data["archive"]

    manager = PublicPlugins(tmp_path, sites, source={"url": "https://catalog.test", "public_key": signed["public_key"]}, fetch=fetch)
    yield manager, sites, data, requests, key
    manager.close()


def test_install_start_local_use_without_company_and_private_sources(setup):
    manager, sites, _, requests, _ = setup
    assert requests == [] and manager.running == {}
    manager.refresh()
    assert manager.overview()["plugins"][0]["installed_version"] is None
    manager.install("image-studio")
    item = manager.overview()["plugins"][0]
    assert item["running"] and item["installed_version"] == "1.1.0"
    assert not sites.store.site("image-studio")["enabled"]
    assert len(requests) == 2 and all("company" not in u and "gshopper" not in u for u in requests)
    client = SiteClient(sites.engine, sites.store)
    client.local_fetch = manager.local_fetch
    key = manager.open("image-studio")["url"].split("//")[1].split(".")[0]
    response = client.fetch(key, "GET", "/api/me", [["X-Ryn-Caller-Member", "someone-else"]], b"")
    assert response[0] == 200 and json.loads(response[2])["name"] == "我"
    assert client.fetch(key, "GET", "/", [], b"")[0] == 200
    assert manager.owner("image-studio", "GET", "/status", b"")[0] == 200
    manager.action("image-studio", "disable")
    assert "image-studio" not in sites.host.builtins
    manager.action("image-studio", "enable")
    manager.action("image-studio", "uninstall")
    assert manager.running == {} and (manager.home / "image-studio/studio.db").exists()


def test_catalog_wrong_key_and_tampering_fail_closed(setup):
    manager, _, data, _, key = setup
    data["catalog"]["payload"]["plugins"][0]["name"] = "changed"
    with pytest.raises(PluginError, match="signature"):
        manager.refresh()
    assert manager.catalog == []
    signed = sign_payload(data["catalog"]["payload"], private_key_bytes=Ed25519PrivateKey.generate().private_bytes_raw()).to_dict()
    with pytest.raises(PluginError, match="signature"):
        verify_catalog(signed, manager.source["public_key"])


def test_modified_archive_or_installed_code_never_executes(setup):
    manager, _, data, _, _ = setup
    manager.refresh()
    original = data["archive"]
    data["archive"] += b"changed"
    with pytest.raises(PluginError, match="integrity"):
        manager.install("image-studio")
    assert not manager.running
    data["archive"] = original
    manager.install("image-studio")
    manager.action("image-studio", "disable")
    row = manager.catalog[0]
    (manager.package_path(row) / row["entry"]).write_text("raise Exception('tampered')")
    with pytest.raises(PluginError, match="integrity"):
        manager.action("image-studio", "enable")
    assert not manager.running


def test_update_rollback_preserves_data_and_share_scope(setup, tmp_path):
    manager, sites, data, _, key = setup
    manager.refresh()
    manager.install("image-studio")
    sites.store.update("image-studio", {"enabled": True, "audience": {"mode": "members", "members": ["alice"]}})
    import shutil
    package = tmp_path / "v2"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((package / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = "1.1.1"
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    _, data["archive"], data["catalog"] = build(package, revision="b" * 40, key=key)
    manager.refresh()
    assert manager.overview()["plugins"][0]["update_available"]
    manager.install("image-studio")
    assert manager.overview()["plugins"][0]["installed_version"] == "1.1.1"
    assert sites.store.site("image-studio")["enabled"]
    assert sites.store.site("image-studio")["audience"]["members"] == ["alice"]
    manager.action("image-studio", "rollback")
    assert manager.overview()["plugins"][0]["installed_version"] == "1.1.0"
    assert (manager.home / "image-studio/studio.db").exists()


def test_in_use_plugin_cannot_be_uninstalled(setup):
    manager, sites, _, _, _ = setup
    manager.refresh()
    manager.install("image-studio")
    runtime = manager.running["image-studio"][0]
    original = runtime.busy
    runtime.busy = lambda: True
    with pytest.raises(PluginError, match="busy"):
        manager.action("image-studio", "uninstall")
    assert "image-studio" in manager.running and "image-studio" in sites.host.builtins
    runtime.busy = original


def test_legacy_data_is_not_deleted_or_automatically_shared(tmp_path):
    store = SiteStore(tmp_path)
    store.register(sid="image-studio", title="做图", builtin=True)
    store.update("image-studio", {"enabled": True})
    marker = tmp_path / "image-studio/files/keep.png"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"existing")
    sites = SimpleNamespace(store=store, client=SimpleNamespace())
    manager = PublicPlugins(tmp_path, sites, source={"url": "unused", "public_key": "unused"})
    assert marker.read_bytes() == b"existing"
    assert not store.site("image-studio")["enabled"] and manager.running == {}


def test_restart_reuses_verified_package_without_network(setup):
    manager, sites, _, requests, _ = setup
    manager.refresh()
    manager.install("image-studio")
    manager.close()
    count = len(requests)
    restarted = PublicPlugins(manager.home, sites, source=manager.source, fetch=manager.fetch)
    try:
        assert restarted.overview()["plugins"][0]["running"]
        assert len(requests) == count
        assert restarted.local_fetch("image-studio", "GET", "/api/jobs", b"")[0] == 200
    finally:
        restarted.close()


def test_failed_new_runtime_restores_previous_version(setup, tmp_path):
    import shutil
    manager, sites, data, _, key = setup
    manager.refresh()
    manager.install("image-studio")
    package = tmp_path / "broken"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((package / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = "1.1.1"
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / manifest["entry"]).write_text("raise RuntimeError('broken release')", encoding="utf-8")
    _, data["archive"], data["catalog"] = build(package, revision="b" * 40, key=key)
    manager.refresh()
    with pytest.raises(PluginError, match="start_failed"):
        manager.install("image-studio")
    assert manager.overview()["plugins"][0]["installed_version"] == "1.1.0"
    assert manager.local_fetch("image-studio", "GET", "/", b"")[0] == 200


def test_public_control_requires_owner_authorization(setup, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from rynmesh.public_plugins import install_public_plugins
    manager, sites, _, _, _ = setup
    monkeypatch.setattr("rynmesh.public_plugins.PublicPlugins", lambda *a, **kw: manager)
    app = FastAPI()

    def guard(request):
        if request.headers.get("x-test-owner") != "yes":
            raise HTTPException(403)

    install_public_plugins(app, home=manager.home, sites=sites, local_control=guard)
    with TestClient(app) as client:
        assert client.get("/api/local/public-plugins").status_code == 403
        assert client.post("/api/local/public-plugins/refresh").status_code == 403
        assert client.post("/api/local/public-plugins/image-studio/install").status_code == 403
        assert client.get("/api/local/image-studio/status").status_code == 403
        headers = {"x-test-owner": "yes"}
        assert client.post("/api/local/public-plugins/refresh", headers=headers).status_code == 200
        assert client.post("/api/local/public-plugins/image-studio/install", headers=headers).status_code == 200
        assert client.get("/api/local/image-studio/status", headers=headers).status_code == 200
