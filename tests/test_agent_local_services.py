import json
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions.local_services import LocalServiceCredentials
from rynmesh.agent_runtime.store import AgentError
from rynmesh.services.peer_box import load_or_create_messaging_key


def fixture(tmp_path):
    store = ExtensionStore(tmp_path / "agent", load_or_create_messaging_key(tmp_path / "key"))
    store.commit_imported_items(plugins=[{"id": "p", "mcp_server_ids": ["s"], "skill_ids": []}], skills=[],
        mcp_servers=[{"id": "s", "plugin_id": "p", "transport": "streamable_http", "url": "http://127.0.0.1:19293/mcp"}])
    store.set_plugin_enabled("p", True)
    invalidated = []
    clock = [1.0]
    service = LocalServiceCredentials(store, SimpleNamespace(invalidate_server=invalidated.append), clock=lambda: clock[0])
    return store, service, invalidated, clock


def prepare(service):
    return service.prepare(service_id="owned", plugin_ids=["p"], server_ids=["s"], port=19293)


def test_rotate_only_after_health_activation_and_no_secret_in_public_store(tmp_path):
    store, service, invalidated, _ = fixture(tmp_path)
    before = store.get_mcp_server_internal("s")
    pending = prepare(service)
    assert store.get_mcp_server_internal("s")["config_generation"] == before["config_generation"]
    service.activate(service_id="owned", generation=pending["generation"])
    current = store.get_mcp_server_internal("s")
    assert current["secret_headers"]["X-Ryn-Service-Token"] == pending["credential"]
    assert current["instance_id"] == before["instance_id"]
    assert current["config_generation"] != before["config_generation"]
    assert pending["credential"] not in json.dumps(store.overview())
    assert pending["credential"].encode() not in store.path.read_bytes()
    assert invalidated == ["s"]
    second = prepare(service)
    assert second["credential"] != pending["credential"]
    service.activate(service_id="owned", generation=second["generation"])
    assert store.get_mcp_server_internal("s")["secret_headers"]["X-Ryn-Service-Token"] == second["credential"]


def test_disabled_changed_or_expired_bindings_cannot_activate(tmp_path):
    store, service, invalidated, clock = fixture(tmp_path)
    first = prepare(service)
    store.set_plugin_enabled("p", False)
    with pytest.raises(AgentError): service.activate(service_id="owned", generation=first["generation"])
    assert invalidated == []
    store.set_plugin_enabled("p", True)
    second = prepare(service)
    clock[0] = 100
    with pytest.raises(AgentError): service.activate(service_id="owned", generation=second["generation"])
    assert invalidated == []


def test_foreign_port_or_plugin_does_not_get_service_credential(tmp_path):
    _, service, invalidated, _ = fixture(tmp_path)
    for plugin, port in [(["other"], 19293), (["p"], 19294)]:
        with pytest.raises(AgentError):
            service.prepare(service_id="owned", plugin_ids=plugin, server_ids=["s"], port=port)
    assert invalidated == []


def test_disabled_mapping_can_rotate_without_enabling_plugin(tmp_path):
    store, service, invalidated, _ = fixture(tmp_path)
    store.set_plugin_enabled("p", False)
    pending = prepare(service)
    service.activate(service_id="owned", generation=pending["generation"])
    current = store.get_mcp_server_internal("s")
    assert current["effective_enabled"] is False
    assert current["secret_headers"]["X-Ryn-Service-Token"] == pending["credential"]
    with pytest.raises(AgentError):
        store.get_mcp_server_internal("s", require_enabled=True)
    assert invalidated == ["s"]


def test_service_callback_requires_exact_durable_grant_and_live_authority(tmp_path):
    import copy
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from rynmesh.agent_runtime.extensions.routes import install_extension_routes
    from rynmesh.agent_runtime.operation_ledger import OperationLedger

    store, service, _, _ = fixture(tmp_path)
    pending = prepare(service)
    service.activate(service_id="owned", generation=pending["generation"])
    server = store.get_mcp_server_internal("s")
    authority = {"goal_revision": 1, "control_revision": 1, "scope_revision": 1, "settings_revision": 1}
    operation = {"operation_id": "op-1234567890123456", "task_id": "task1", "server_id": "s",
                 "arguments_sha256": "a" * 64, "expires_at": time.time() + 60,
                 "authority_revision": authority, "preview_digest": "b" * 64}
    ledger = OperationLedger(tmp_path / "runtime")
    binding = {"operation_id": operation["operation_id"], "task_id": "task1",
               "arguments_sha256": operation["arguments_sha256"], "extension_binding": {"server_id": "s"},
               "authority_revision": authority}
    ledger.begin(binding, {})
    ledger.update(operation["operation_id"], "dispatched", trusted_operation=operation)
    task = {"status": "running", "goal": {"revision": 1}, "control_revision": 1, "scope_revision": 1}
    data = {"tasks": {"task1": task}, "settings": {"enabled": True, "permission_mode": "confirm_changes"},
            "settings_revision": 1}
    def verify(_):
        store.get_mcp_server_internal("s", require_enabled=True,
                                      expected_config_generation=server["config_generation"])
    extensions = SimpleNamespace(store=store, manager=None, ledger_factory=lambda: ledger, verify_binding=verify)
    app = FastAPI()
    install_extension_routes(app, runtime=SimpleNamespace(store=SimpleNamespace(inspect=lambda fn: fn(data))),
                             extensions=extensions, local_control=lambda request: None)
    headers = {"X-Ryn-Service-Token": pending["credential"], "X-Ryn-Service-Generation": pending["generation"]}
    path = "/api/local/agent/extensions/local-service/authorize-operation"
    with TestClient(app, client=("127.0.0.1", 40001)) as client:
        def allowed(payload=operation, request_headers=headers):
            return client.post(path, json=payload, headers=request_headers).json() == {"allowed": True}
        assert allowed()
        for key, value in [("expires_at", time.time()+3600), ("preview_digest", "c"*64),
                           ("task_id", "other"), ("arguments_sha256", "d"*64)]:
            changed = copy.deepcopy(operation)
            changed[key] = value
            assert not allowed(changed)
        assert not allowed(request_headers={**headers, "X-Ryn-Service-Token": "wrong"})
        assert client.post(path, json=operation, headers={**headers, "Origin": "http://evil.test"}).status_code == 403
        for state in ["cancelled", "paused", "interrupted", "failed"]:
            task["status"] = state
            assert not allowed()
        task["status"] = "running"
        data["settings_revision"] = 2
        assert not allowed()
        data["settings_revision"] = 1
        assert allowed()
        ledger.update(operation["operation_id"], "intent")
        assert not allowed()
        ledger.update(operation["operation_id"], "confirmed")
        assert allowed()
        store.set_plugin_enabled("p", False)
        assert not allowed()
        store.set_plugin_enabled("p", True)
        rotated = prepare(service)
        service.activate(service_id="owned", generation=rotated["generation"])
        assert not allowed()


def test_managed_oauth_issuer_requires_enabled_instance_and_declared_https_origin(tmp_path):
    from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager
    store = ExtensionStore(tmp_path / "agent", load_or_create_messaging_key(tmp_path / "key"))
    resource = "http://127.0.0.1:19293/mcp"
    store.commit_imported_items(plugins=[{"id": "p", "mcp_server_ids": ["s"], "skill_ids": [],
        "identity_group": {"id": "test", "origin": "https://identity.example.test"}}], skills=[],
        mcp_servers=[{"id": "s", "plugin_id": "p", "transport": "streamable_http", "url": resource}])
    store.set_plugin_enabled("p", True)
    manager = McpSessionManager(store)
    try:
        assert not manager._managed_oauth_origin(resource, "https://identity.example.test/token")
        service = LocalServiceCredentials(store, manager)
        pending = prepare(service)
        service.activate(service_id="owned", generation=pending["generation"])
        assert manager._managed_oauth_origin(resource, "https://identity.example.test/token")
        for target in ["https://identity.example.test.evil.test/token", "http://identity.example.test/token",
                       "https://identity.example.test:444/token", "https://evil.test/token"]:
            assert not manager._managed_oauth_origin(resource, target)
        assert not manager._managed_oauth_origin(resource.replace("19293", "19294"), "https://identity.example.test")
        store.set_plugin_enabled("p", False)
        assert not manager._managed_oauth_origin(resource, "https://identity.example.test")
    finally:
        manager.close()
