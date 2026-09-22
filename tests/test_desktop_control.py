from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.peer_http import create_app
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_desktop_control_requires_local_auth_even_with_network_key(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "test-network-key")
    monkeypatch.setenv("RYNMESH_DESKTOP_MODE", "1")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "test-desktop-token")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    with TestClient(create_app(store=store)) as client:
        assert client.get("/api/local/desktop/status").status_code == 403
        assert client.post("/api/local/desktop/sharing", json={"enabled": True}).status_code == 403
        response = client.get("/api/local/desktop/status", headers={"x-ryn-local-token": "test-desktop-token"})
        assert response.status_code == 200
        assert response.json() == {"desktop_managed": True, "configured": False, "sharing": False, "active_tasks": 0, "setup_active": False}


def test_desktop_status_does_not_probe_or_restart_external_runtime(tmp_path, monkeypatch):
    from rynmesh.llm_package import routes
    from rynmesh.llm_package.manifest import LLMPackageManifest
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    manifest_path = home / "service.json"
    manifest = LLMPackageManifest(package_id="desktop-test", mode="openai_compatible", public_model_alias="test", model="test", base_url="http://127.0.0.1:9999/v1")
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    monkeypatch.setenv("RYNMESH_LLM_SERVICE_MANIFEST", str(manifest_path))
    class Adapter:
        def health(self): raise AssertionError("status must not probe model")
        def shutdown(self): raise AssertionError("status must not restart model")
    monkeypatch.setattr(routes, "adapter_from_manifest", lambda _: Adapter())
    app = FastAPI()
    install_llm_routes(app, store=store, home=home, messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"), resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    provider = app.state.llm_provider
    provider._running = 2
    with TestClient(app) as client:
        assert client.get("/api/local/desktop/status").json()["active_tasks"] == 2
        paused = client.post("/api/local/desktop/sharing", json={"enabled": False})
        assert paused.status_code == 200
        assert paused.json()["sharing"] is False
        assert provider._running == 2
        settings = json.loads((home / "llm/provider-settings.json").read_text())
        assert settings["publication_enabled"] is False
        assert client.post("/api/local/desktop/sharing", json={"enabled": "yes"}).status_code == 422
        assert client.post("/api/local/desktop/sharing", json={"enabled": True}).json()["sharing"] is True
        assert provider._running == 2


def test_unconfigured_desktop_cannot_enable_ai(tmp_path):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home, messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"), resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    with TestClient(app) as client:
        assert client.post("/api/local/desktop/sharing", json={"enabled": True}).status_code == 409
    assert not (home / "llm/provider-settings.json").exists()
