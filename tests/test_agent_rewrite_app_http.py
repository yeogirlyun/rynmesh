"""Scripted model boundary; real task HTTP, Desk state, approvals and receipts."""
import copy
import json
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_agent_integrations import node as node
from test_agent_runtime import Catalog, assistant

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.node_apps.agent import ACTION
from rynmesh.node_apps.plugin_agent import ALL_INFO, ALL_LIST
from rynmesh.node_apps.routes import install_node_apps
from rynmesh.services import peer_box


def tool_result(messages, call_id):
    return json.loads(next(message["content"] for message in messages
                           if message.get("tool_call_id") == call_id and message["role"] == "tool"))


class DeskModel:
    """No inference service: proposals consume the actual returned app contracts."""

    def __init__(self, title):
        self.title = title
        self.calls = []

    def complete(self, model_id, messages, specs, cancelled, on_event):
        self.calls.append((model_id, copy.deepcopy(messages), copy.deepcopy(specs)))
        step = len(self.calls) - 1
        if step == 0:
            return assistant(name=ALL_LIST, call_id="discover-plugins")
        if step == 1:
            plugin = next(row for row in tool_result(messages, "discover-plugins")["plugins"] if row["id"] == "desk")
            assert plugin["discovery_tool"] == ALL_INFO and plugin["execution_tool"] == ACTION
            return assistant(name=tool_menu.NAME, args={"load": [plugin["discovery_tool"]]}, call_id="load-info")
        if step == 2:
            return assistant(name=ALL_INFO, args={"app": "desk"}, call_id="desk-info")
        if step == 3:
            self.info = tool_result(messages, "desk-info")
            assert "create_ticket" in self.info["operations"]
            return assistant(name=tool_menu.NAME, args={"load": [self.info["execution_tool"]]}, call_id="load-action")
        if step == 4:
            self.arguments = {"app": "desk", "action": "create_ticket", "arguments": {"name": self.title},
                              "reviewed_context": self.info["workspace_context"]}
            return assistant(name=self.info["execution_tool"], args=self.arguments, call_id="create-desk-ticket")
        assert step == 5
        self.receipt = tool_result(messages, "create-desk-ticket")
        record = self.receipt["result"]["record"]
        assert record["name"] == self.title
        return assistant(f"Created Desk ticket {record['id']}: {record['name']}")


def test_task_http_discovers_writes_real_desk_and_retries_without_duplicate(node, tmp_path, monkeypatch):
    app, store = node
    # install_node_apps normally selects a user cache; this fixture owns every path.
    monkeypatch.setattr("rynmesh.node_apps.plugins.package_root", lambda _peer: tmp_path / "plugins")
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")

    def guard(request):
        if request.headers.get("x-fixture-owner") != "yes":
            raise HTTPException(401, detail="fixture_owner_required")

    install_node_apps(app, home=store.home, messaging_key=key, peer_id=store.peer_id,
                      friends=lambda: None, local_control=guard)
    app.state.craft_plugins.install("desk")
    service = app.state.node_apps
    _, tools, _ = build_agent_integrations(app, store, store.home)
    model = DeskModel("Isolated HTTP Desk fixture " + uuid.uuid4().hex)
    runtime = install_agent(app, store=store, home=store.home, workers=BackgroundWorkerRegistry(),
                            local_control=guard, messaging_key=key, integrations=(model, tools, Catalog()))
    headers = {"x-fixture-owner": "yes"}
    grants_before = copy.deepcopy(service.store.read()["grants"])

    with TestClient(app) as client:
        assert client.post("/api/local/agent/tasks", json={"message": "not authorized"}).status_code == 401
        response = client.put("/api/local/agent/settings", headers=headers, json={
            "enabled": True, "model_id": "local:test", "permission_mode": "automatic",
            "allowed_write_tools": [ACTION], "desktop_access": False})
        assert response.status_code == 200
        settings_before = client.get("/api/local/agent/settings", headers=headers).json()
        empty = client.get("/api/local/node-apps/desk/workspace", headers=headers)
        assert empty.status_code == 200 and empty.json()["records"] == {}
        body = {"message": "Create one Desk ticket named " + model.title,
                "client_request_id": str(uuid.uuid4())}
        created = client.post("/api/local/agent/tasks", headers=headers, json=body)
        assert created.status_code == 200
        task_id = created.json()["id"]
        assert client.post("/api/local/agent/tasks", headers=headers, json=body).json()["id"] == task_id
        task_path = "/api/local/agent/tasks/" + task_id
        for _ in range(20):
            runtime.run_once()
            waiting = client.get(task_path, headers=headers).json()
            if waiting["status"] == "waiting_approval":
                break
        assert waiting["status"] == "waiting_approval", waiting.get("error")
        assert client.get("/api/local/node-apps/desk/workspace", headers=headers).json()["records"] == {}
        pending = waiting["pending_approval"]
        assert pending["preview"]["app_preview"]["app"] == "desk"
        assert pending["preview"]["model_data_recipient"]["catalog_model_id"] == "local:test"
        approved = client.post(task_path + "/actions", headers=headers, json={
            "action": "approve", "approval_id": pending["id"],
            "expected_control_revision": waiting["control_revision"], "client_request_id": str(uuid.uuid4())})
        assert approved.status_code == 200, approved.text
        for _ in range(6):
            runtime.run_once()
            done = client.get(task_path, headers=headers).json()
            if done["status"] == "completed":
                break
        assert done["status"] == "completed", done.get("error")
        assert model.title in done["result"] and not done["local_execution"]["desktop_access"]
        workspace = client.get("/api/local/node-apps/desk/workspace", headers=headers).json()
        assert len(workspace["records"]["tickets"]) == 1
        assert workspace["records"]["tickets"][0]["name"] == model.title
        assert workspace["revision"] == model.info["workspace_context"]["revision"] + 1
        assert len(model.calls) == 6

        saved = runtime.store.read()["tasks"][task_id]
        operation = next(receipt for receipt in saved["receipts"] if receipt["tool"] == ACTION)
        assert model.receipt["operation_id"] == operation["operation_id"]
        retry = {"provider_peer": "", "app": "desk", "action": "create_ticket",
                 "arguments": {"name": model.title}, "operation_id": operation["operation_id"],
                 "preview_hash": pending["preview"]["app_preview"]["preview_hash"]}
        repeated = client.post("/api/local/node-apps/invoke", headers=headers, json=retry)
        assert repeated.status_code == 200 and repeated.json() == model.receipt
        assert client.get("/api/local/node-apps/desk/workspace", headers=headers).json() == workspace
        events = client.get(task_path + "/events", headers=headers).json()["events"]
        assert len([item for item in events if item["type"] == "approved"]) == 1
        assert any(item["type"] == "tool_result" and item.get("tool") == ACTION for item in events)
        assert client.get("/api/local/agent/settings", headers=headers).json() == settings_before
        assert service.store.read()["grants"] == grants_before
