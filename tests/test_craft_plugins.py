"""Real package/lifecycle/HTTP boundaries; never substitute catalog reads for edits."""

import hashlib
import io
import json
import socket
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_integrations import node as node

from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.node_apps import craft_host
from rynmesh.node_apps.craft_catalog import CRAFT
from rynmesh.node_apps.plugin_agent import INFO, LIST, PluginTools
from rynmesh.node_apps.plugins import (
    DEFAULT_PLUGINS,
    PluginManager,
    install_plugin_routes,
    installer_selection,
    package_root,
)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("wordcraft/index.html", "<title>Actual fixture</title><canvas></canvas>")
        archive.writestr("wordcraft/LICENSE", "Retained test license")
    raw = output.getvalue()
    monkeypatch.setitem(CRAFT, "wordcraft", {**CRAFT["wordcraft"], "web_sha256": hashlib.sha256(raw).hexdigest()})
    def download(url, path):
        assert url.endswith("/v0.6.1/wordcraft-web-0.6.1.zip")
        path.write_bytes(raw)
    value = PluginManager(tmp_path / "node", "owner-node", cache=tmp_path / "plugins", download=download)
    yield value
    value.close()


def test_defaults_external_storage_no_startup_work(manager, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(craft_host, "download_to", lambda *_: pytest.fail("startup download"))
    assert set(DEFAULT_PLUGINS) == {"pdfcraft", "wordcraft", "gridcraft", "deckcraft"}
    value = manager.catalog()
    assert not value["setup_completed"] and not any(p["installed"] for p in value["plugins"])
    assert not manager.cache.exists() and manager.servers == {}
    assert package_root("one") != package_root("two")
    assert "rynmesh/node_apps" not in str(package_root("one")).replace("\\", "/")


def test_install_enable_agent_disable_uninstall_and_file_retention(manager, tmp_path):
    manager.install("wordcraft")
    manager.finish_setup()
    package = manager.cache / "wordcraft/v0.6.1"
    assert "license" in (package / "payload/wordcraft/LICENSE").read_text()
    assert manager.servers == {}  # Installation does not launch a listener.
    tools = PluginTools(SimpleNamespace(state=SimpleNamespace(craft_plugins=manager)))
    assert any(s["function"]["name"] == INFO for s in tools.specs())
    info = tools.execute(INFO, {"app": "wordcraft"}, lambda: False)
    assert info["document_editing"] == "not_integrated" and not info["editor_opened"]
    assert info["warnings"] == ["cjk_font_coverage"]
    assert tools.execute(LIST, {}, lambda: False)["plugins"][0]["id"] == "wordcraft"
    manager.set_enabled("wordcraft", False)
    assert INFO not in {item["function"]["name"] for item in tools.specs()}
    with pytest.raises(AgentToolRejected, match="plugin_disabled"):
        tools.execute(INFO, {"app": "wordcraft"}, lambda: False)  # Stale schema is not authority.
    manager.set_enabled("wordcraft", True)
    export = tmp_path / "my-report.docx"
    export.write_bytes(b"user-export")
    with manager.lock:
        state = manager._read()
        state["preferences"] = {"wordcraft": {"zoom": 110}}
        manager._write(state)
    manager.uninstall("wordcraft")
    assert not package.exists() and export.read_bytes() == b"user-export"
    assert manager._read()["preferences"]["wordcraft"] == {"zoom": 110}
    manager.install("wordcraft")
    manager.uninstall("wordcraft", True)
    assert "wordcraft" not in manager._read()["preferences"]
    assert tools.execute(LIST, {}, lambda: False) == {"plugins": []}


def test_active_lease_blocks_lifecycle_and_rechecks_tamper(manager):
    manager.install("wordcraft")
    with manager.lease("wordcraft"):
        for operation in [lambda: manager.uninstall("wordcraft"), lambda: manager.set_enabled("wordcraft", False)]:
            with pytest.raises(craft_host.CraftError, match="plugin_busy"):
                operation()
    (manager.cache / "wordcraft/v0.6.1/payload/wordcraft/index.html").write_text("tampered")
    with pytest.raises(craft_host.CraftError, match="craft_build_not_verified"):
        manager.preview_launch("wordcraft")


def test_launch_real_separate_http_origin_disable_releases_port(manager, monkeypatch):
    manager.install("wordcraft")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(manager, "port", lambda slug: port)
    preview = manager.preview_launch("wordcraft")
    assert manager.servers == {}  # Preview is not a launch.
    with pytest.raises(craft_host.CraftError, match="plugin_launch_expired"):
        manager.launch("wordcraft", "model-invented-ticket")
    result = manager.launch("wordcraft", preview["ticket"])
    assert result["state"] == "ready"
    with httpx.Client(trust_env=False) as client:
        response = client.get(result["url"])
        assert response.status_code == 200 and "Actual fixture" in response.text
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert client.post(result["url"] + "api/local/craft-plugins").status_code == 405
        assert "set-cookie" not in response.headers
    manager.set_enabled("wordcraft", False)
    assert not manager.servers
    with socket.socket() as probe:
        # Windows may retain TIME_WAIT after an HTTP request; connecting must
        # fail, regardless of whether this OS permits immediate rebinding.
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_port_collision_never_opens_existing_service(manager, monkeypatch):
    manager.install("wordcraft")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setattr(manager, "port", lambda slug: occupied.getsockname()[1])
        ticket = manager.preview_launch("wordcraft")["ticket"]
        with pytest.raises(craft_host.CraftError, match="plugin_port_busy"):
            manager.launch("wordcraft", ticket)
        assert not manager.servers


def test_failed_download_never_publishes_installation(manager):
    manager.download = lambda url, path: path.write_bytes(b"wrong archive")
    with pytest.raises(craft_host.CraftError, match="craft_checksum_mismatch"):
        manager.install("wordcraft")
    assert not any(p["installed"] for p in manager.catalog()["plugins"])
    assert not (manager.cache / "wordcraft/v0.6.1").exists()
    assert not list(manager.cache.rglob(".install-*"))


def test_route_owner_gate_limits_and_closed_lifespan(manager, monkeypatch):
    monkeypatch.setattr("rynmesh.node_apps.plugins.PluginManager", lambda *_, **__: manager)
    app = FastAPI()
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(403)
    install_plugin_routes(app, home=manager.state_path.parent, peer_id="owner-node", local_control=guard)
    with TestClient(app) as client:
        assert client.get("/api/local/craft-plugins").status_code == 403
        headers = {"x-owner": "yes"}
        listed = client.get("/api/local/craft-plugins", headers=headers)
        assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
        assert client.post("/api/local/craft-plugins/wordcraft/install", headers=headers, json={"url": "https://evil.test"}).status_code == 400
        assert client.post("/api/local/craft-plugins/wordcraft/install", headers=headers, json={"padding": "a" * 2100}).status_code == 413
        result = client.post("/api/local/craft-plugins/wordcraft/install", headers=headers, json={})
        assert result.status_code == 200
        assert client.post("/api/local/craft-plugins/wordcraft/enable", headers=headers, json={"enabled": "yes"}).status_code == 409
        assert client.post("/api/local/craft-plugins/wordcraft/uninstall", headers=headers, json={"clear_preferences": "yes"}).status_code == 400
        assert client.post("/api/local/craft-plugins/setup", headers=headers, json={}).json()["setup_completed"]
    assert manager.closed


def test_native_unknown_and_expired_ticket(manager):
    with pytest.raises(craft_host.CraftError, match="craft_native_only"):
        manager.install("filmcraft")
    with pytest.raises(craft_host.CraftError, match="craft_unknown_app"):
        manager.install("../../user-documents")
    manager.install("wordcraft")
    ticket = manager.preview_launch("wordcraft")["ticket"]
    manager.tickets[ticket] = ("wordcraft", time.monotonic() - 1)
    with pytest.raises(craft_host.CraftError, match="plugin_launch_expired"):
        manager.launch("wordcraft", ticket)


def test_state_persists_without_restarting_listeners(manager):
    manager.install("wordcraft")
    manager.finish_setup()
    other = PluginManager(manager.state_path.parent, manager.peer_id, cache=manager.cache)
    value = other.catalog()
    assert value["setup_completed"]
    assert next(p for p in value["plugins"] if p["id"] == "wordcraft")["enabled"]
    assert not other.servers
    other.close()


def test_no_editor_payloads_in_distribution_configuration():
    root = Path(__file__).resolve().parents[1]
    for filename in ["tauri.conf.json", "tauri.windows.conf.json"]:
        config = json.loads((root / "webapp/src-tauri" / filename).read_text())
        assert "craft" not in json.dumps(config.get("bundle", {}).get("resources", {})).lower()
    assert "web_sha256" in CRAFT["wordcraft"]  # Only small pinned manifests ship.


def test_agent_registry_defaults_and_stale_tool_rejection(node, manager):
    from rynmesh.agent_runtime.bridge import build_agent_integrations

    app, store = node
    app.state.craft_plugins = manager
    _, tools, _ = build_agent_integrations(app, store, store.home)
    assert INFO not in {s["function"]["name"] for s in tools.specs()}
    manager.install("wordcraft")
    spec = next(s for s in tools.specs() if s["function"]["name"] == INFO)
    assert spec["function"]["parameters"]["properties"]["app"]["enum"] == ["wordcraft"]
    assert tools.metadata(INFO)["effect"] == "read"
    assert not tools.metadata(INFO).get("requires_explicit_approval")
    result = tools.execute(INFO, {"app": "wordcraft"}, operation_id="info-read", cancelled=lambda: False)
    assert result["editor_opened"] is False
    manager.set_enabled("wordcraft", False)
    with pytest.raises(AgentToolRejected, match="plugin_disabled"):
        tools.execute(INFO, {"app": "wordcraft"}, operation_id="stale-call", cancelled=lambda: False)


def test_installer_choices_are_bounded_preferences_not_download_authority(monkeypatch, manager):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    value = ["wordcraft;pdfcraft", 1]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER=0, REG_SZ=1, OpenKey=lambda *_: Key(), QueryValueEx=lambda *_: value))
    assert installer_selection() == ["wordcraft", "pdfcraft"]
    assert manager.catalog()["selection"] == ["wordcraft", "pdfcraft"]
    assert manager.catalog()["setup_requested"] and manager.servers == {}
    assert not manager.cache.exists()
    value[0] = ""
    assert installer_selection() == []  # Explicitly choosing none is preserved.
    value[0] = "wordcraft;../../commands"
    assert installer_selection() is None
    value[0] = "filmcraft"
    assert installer_selection() is None
