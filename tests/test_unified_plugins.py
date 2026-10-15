"""Unified lifecycle against real encrypted app stores and resource boundaries."""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_integrations import node as node
from test_agent_resources import file_ref
from test_agent_resources import resources as resources
from test_nas import MemoryConnector, config
from test_node_apps import allow, invoke
from test_node_apps import mesh as mesh

from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.nas import NasError, NasPlugin
from rynmesh.node_apps import builtin_plugins as builtin
from rynmesh.node_apps.agent import ACTION, AppTools
from rynmesh.node_apps.craft_host import CraftError
from rynmesh.node_apps.engine import AppError
from rynmesh.node_apps.plugin_agent import ALL_INFO, ALL_LIST, INFO
from rynmesh.node_apps.plugins import DEFAULT_PLUGINS, PluginManager, install_plugin_routes


@pytest.fixture
def unified(mesh, tmp_path):
    nas = NasPlugin(tmp_path / "nas-home")
    nas.connectors["smb"] = MemoryConnector
    manager = PluginManager(mesh.friends[0].home, mesh.provider.peer_id,
                            cache=tmp_path / "packages", nas=nas,
                            download=lambda *_: pytest.fail("Built-in registration must not download"))
    nas.plugin_manager = manager
    mesh.provider.plugins = manager
    manager.adopt_legacy(mesh.provider)
    yield SimpleNamespace(manager=manager, nas=nas, service=mesh.provider, mesh=mesh)
    manager.close()


def test_fresh_catalog_seventeen_default_four_and_no_implicit_install(unified):
    value = unified.manager.catalog()
    assert len(value["plugins"]) == 17
    assert set(value["defaults"]) == set(DEFAULT_PLUGINS)
    assert sum(p["web"] for p in value["plugins"]) == 12
    assert sum(p["installable"] for p in value["plugins"]) == 16
    assert not any(p["installed"] or p["enabled"] for p in value["plugins"])
    assert not unified.manager.cache.exists()
    assert unified.service.call("", "catalog", {})["apps"] == []


@pytest.mark.parametrize("slug,action,collection", [
    ("resume", "create_resume", "resumes"),
    ("recruit", "create_job", "jobs"),
    ("desk", "create_ticket", "tickets"),
])
def test_real_business_adapter_lifecycle_preserves_data_and_historical_receipts(unified, slug, action, collection):
    manager, service = unified.manager, unified.service
    with pytest.raises(AppError, match="plugin_disabled"):
        service.call("", "view", {"app": slug})
    manager.install(slug)
    root = builtin.verify(manager.cache, slug)
    assert sum(p.stat().st_size for p in root.iterdir()) < 65536
    receipt = invoke(service, "", slug, action, {"name": "PRIVATE_RETAINED_DATA"})
    ciphertext = service.store.path.read_bytes()
    assert b"PRIVATE_RETAINED_DATA" not in ciphertext
    pending = service.preview("", slug, action, {"name": "Old approval"})
    manager.set_enabled(slug, False)
    assert service.call("", "catalog", {})["apps"] == []
    assert service.call("", "receipt", {"app": slug, "operation_id": receipt["operation_id"]}) == receipt
    manager.set_enabled(slug, True)
    with pytest.raises(AppError, match="review_changed"):
        service.invoke("", {"app": slug, "action": action, "arguments": {"name": "Old approval"},
                            "operation_id": "stale-after-enable", "preview_hash": pending["preview_hash"]})
    pending = service.preview("", slug, action, {"name": "Before uninstall"})
    ciphertext = service.store.path.read_bytes()
    manager.uninstall(slug)
    assert not root.exists() and service.store.path.read_bytes() == ciphertext
    manager.adopt_legacy(service)  # No automatic reinstallation of removed plugins.
    assert not root.exists()
    manager.install(slug)
    with pytest.raises(AppError, match="review_changed"):
        service.invoke("", {"app": slug, "action": action, "arguments": {"name": "Before uninstall"},
                            "operation_id": "stale-after-reinstall", "preview_hash": pending["preview_hash"]})
    assert service.call("", "view", {"app": slug})["records"][collection][0]["name"] == "PRIVATE_RETAINED_DATA"


def test_friend_authority_cannot_bypass_plugin_lifecycle(unified):
    manager, mesh = unified.manager, unified.mesh
    manager.install("desk")
    allow(mesh, mesh.buyer, "desk")
    peer = mesh.provider.peer_id
    assert [p["id"] for p in mesh.buyer.call(peer, "catalog", {})["apps"]] == ["desk"]
    old = mesh.buyer.preview(peer, "desk", "create_ticket", {"name": "Shared draft"})
    grants = mesh.provider.store.read()["grants"]
    manager.uninstall("desk")
    assert mesh.buyer.call(peer, "catalog", {})["apps"] == []
    with pytest.raises(AppError, match="plugin_disabled"):
        mesh.buyer.call(peer, "view", {"app": "desk"})
    manager.install("desk")
    assert mesh.provider.store.read()["grants"] == grants
    with pytest.raises(AppError, match="review_changed"):
        mesh.buyer.invoke(peer, {"app": "desk", "action": "create_ticket", "arguments": old["arguments"],
                                "operation_id": "friend-stale", "preview_hash": old["preview_hash"]})
    assert mesh.other.call(peer, "catalog", {})["apps"] == []  # Installing never grants another friend access.


def test_legacy_migration_uses_prior_activity_and_grants_once(mesh, tmp_path):
    invoke(mesh.provider, "", "resume", "create_resume", {"name": "Existing resume"})
    allow(mesh, mesh.buyer, "recruit")
    before = mesh.provider.store.path.read_bytes()
    manager = PluginManager(mesh.friends[0].home, mesh.provider.peer_id, cache=tmp_path / "packages")
    manager.adopt_legacy(mesh.provider)
    assert {p["id"] for p in manager.catalog()["plugins"] if p["installed"]} == {"resume", "recruit"}
    assert mesh.provider.store.path.read_bytes() == before
    manager.uninstall("resume")
    restarted = PluginManager(mesh.friends[0].home, mesh.provider.peer_id, cache=manager.cache)
    restarted.adopt_legacy(mesh.provider)
    assert not next(p for p in restarted.catalog()["plugins"] if p["id"] == "resume")["installed"]
    manager.close()
    restarted.close()


def test_builtin_tampering_and_active_execution_fail_closed(unified):
    manager = unified.manager
    manager.install("resume")
    with manager.lease("resume"):
        with pytest.raises(CraftError, match="plugin_busy"):
            manager.uninstall("resume")
    (builtin.location(manager.cache, "resume") / "manifest.json").write_text('{"command":"evil"}')
    with pytest.raises(AppError, match="plugin_unavailable"):
        unified.service.preview("", "resume", "create_resume", {"name": "Tampered"})
    assert not next(p for p in manager.catalog()["plugins"] if p["id"] == "resume")["enabled"]
    assert unified.service.call("", "catalog", {})["apps"] == []


def test_migration_cache_failure_keeps_node_startable_and_legacy_data_intact(mesh, tmp_path):
    invoke(mesh.provider, "", "resume", "create_resume", {"name": "Keep existing"})
    before = mesh.provider.store.path.read_bytes()
    manager = PluginManager(mesh.friends[0].home, mesh.provider.peer_id, cache=tmp_path / "packages")
    root = builtin.install(manager.cache, "resume")
    (root / "manifest.json").write_text("damaged")
    manager.adopt_legacy(mesh.provider)
    assert manager.catalog()["migration_error"] == "plugin_migration_failed"
    assert mesh.provider.store.path.read_bytes() == before
    assert not manager._read().get("unified_migrated")
    manager.uninstall("resume")
    manager.adopt_legacy(mesh.provider)
    assert manager.catalog()["migration_error"] is None
    assert next(p for p in manager.catalog()["plugins"] if p["id"] == "resume")["enabled"]
    manager.close()


def test_nas_old_toggle_sync_retains_credentials_files_and_ai_permissions(unified):
    manager, nas = unified.manager, unified.nas
    nas.enable(True)  # Existing settings endpoint uses the unified lifecycle.
    source = nas.configure(config(writable=True))["id"]
    nas.operate(source, "write", "retained.txt", b"Retained on NAS")
    original = nas.state["sources"].copy()
    assert not original[0]["allow_ai"]
    manager.uninstall("nas")
    assert not nas.status()["enabled"] and nas.state["sources"] == original
    with pytest.raises(NasError):
        nas.operate(source, "read", "retained.txt")
    assert b"private-secret" not in (nas.directory / "state.enc").read_bytes()
    nas.enable(True)
    assert next(p for p in manager.catalog()["plugins"] if p["id"] == "nas")["enabled"]
    assert nas.operate(source, "read", "retained.txt") == b"Retained on NAS"
    with pytest.raises(NasError, match="ai_not_allowed"):
        nas.operate(source, "ai", "retained.txt")
    with nas.access():
        with pytest.raises(CraftError, match="plugin_busy"):
            manager.set_enabled("nas", False)
    nas.enable(False)
    assert not next(p for p in manager.catalog()["plugins"] if p["id"] == "nas")["enabled"]


def test_legacy_nas_configuration_migrates_without_enabling_it(mesh, tmp_path):
    nas = NasPlugin(tmp_path / "old-nas")
    nas.connectors["smb"] = MemoryConnector
    nas.enable(True)
    nas.configure(config())
    nas.enable(False)
    before = (nas.directory / "state.enc").read_bytes()
    manager = PluginManager(mesh.friends[0].home, mesh.provider.peer_id, cache=tmp_path / "packages", nas=nas)
    manager.adopt_legacy(mesh.provider)
    row = next(p for p in manager.catalog()["plugins"] if p["id"] == "nas")
    assert row["installed"] and not row["enabled"]
    assert (nas.directory / "state.enc").read_bytes() == before
    manager.close()


def test_agent_capabilities_default_available_keep_exact_business_approval(unified, node):
    from rynmesh.agent_runtime.bridge import build_agent_integrations

    app, store = node
    app.state.node_apps = unified.service
    app.state.craft_plugins = unified.manager
    app.state.nas_plugin = unified.nas
    _, tools, _ = build_agent_integrations(app, store, store.home)
    unified.manager.install("resume")
    unified.manager.install("nas")
    assert tools.metadata(ACTION)["requires_explicit_approval"]
    assert tools.execute(ALL_INFO, {"app": "resume"}, operation_id="info", cancelled=lambda: False)["execution_tool"] == ACTION
    enabled = tools.execute(ALL_LIST, {}, operation_id="list", cancelled=lambda: False)["plugins"]
    assert {p["id"] for p in enabled} == {"resume", "nas"}
    assert INFO not in {s["function"]["name"] for s in tools.specs()}
    args = {"app": "resume", "action": "create_resume", "arguments": {"name": "Agent approved"},
            "reviewed_context": unified.service.call("", "context", {"app": "resume"})}
    context = {"consumer_node_id": unified.service.peer_id, "model_destination": {"catalog_model_id": "local/test"}}
    adapter = AppTools(app)
    preview = adapter.preview(args, context)
    with pytest.raises(AgentToolRejected, match="approval_preview_changed"):
        adapter.execute(ACTION, args, "no-approval", lambda: False, None, context)
    result = adapter.execute(ACTION, args, "approved", lambda: False, preview, context)
    assert result["result"]["record"]["name"] == "Agent approved"
    unified.manager.set_enabled("resume", False)
    assert "resume" not in next(s for s in tools.specs() if s["function"]["name"] == ALL_INFO)["function"]["parameters"]["properties"]["app"]["enum"]
    with pytest.raises(AgentToolRejected, match="plugin_disabled"):
        tools.execute(ALL_INFO, {"app": "resume"}, operation_id="stale", cancelled=lambda: False)


def test_agent_nas_grants_do_not_survive_disable_reenable(resources, tmp_path):
    tools, nas, context, _ = resources
    manager = PluginManager(tmp_path / "owner", "owner", cache=tmp_path / "packages", nas=nas)
    nas.plugin_manager = manager
    manager.install("nas")
    reference = file_ref(tools, context)
    manager.set_enabled("nas", False)
    with pytest.raises(AgentToolRejected, match="permission_changed"):
        tools.execute("ryn_read_resource", {"resource_ref": reference}, context=context, cancelled=lambda: False)
    manager.set_enabled("nas", True)
    with pytest.raises(AgentToolRejected, match="permission_changed"):
        tools.execute("ryn_read_resource", {"resource_ref": reference}, context=context, cancelled=lambda: False)
    manager.close()


def test_unified_http_alias_and_fixed_builtin_entries(unified, monkeypatch):
    monkeypatch.setattr("rynmesh.node_apps.plugins.PluginManager", lambda *_, **__: unified.manager)
    app = FastAPI()
    install_plugin_routes(app, home=unified.manager.state_path.parent, peer_id=unified.service.peer_id, local_control=lambda _: None)
    with TestClient(app) as client:
        assert len(client.get("/api/local/plugins").json()["plugins"]) == 17
        assert client.get("/api/local/craft-plugins").json() == client.get("/api/local/plugins").json()
        assert client.post("/api/local/plugins/resume/install", json={}).status_code == 200
        assert client.post("/api/local/plugins/resume/launch", json={"ticket": "model"}).json()["detail"] == "plugin_internal_entry"
        row = next(p for p in client.get("/api/local/plugins").json()["plugins"] if p["id"] == "resume")
        assert row["launch_path"] == "/apps/workspaces?app=resume"
        assert json.loads((builtin.location(unified.manager.cache, "resume") / "manifest.json").read_bytes())["runtime"] == "trusted_builtin_adapter"
