"""Discover real business contracts, approve, and persist through the Agent runtime."""

import copy
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_integrations import node as node
from test_agent_runtime import Catalog, advance, approve, assistant
from test_node_apps import invoke

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.nas import NasPlugin
from rynmesh.node_apps.agent import ACTION
from rynmesh.node_apps.builtin_plugins import NAS_TOOLS
from rynmesh.node_apps.catalog_data import CATALOG
from rynmesh.node_apps.plugin_agent import ALL_INFO, ALL_LIST
from rynmesh.node_apps.plugins import PluginManager
from rynmesh.node_apps.service import NodeApps
from rynmesh.node_apps.store import AppStore


@pytest.fixture
def apps(tmp_path, node):  # noqa: F811 - imported pytest fixture parameter
    app, store = node
    key = X25519PrivateKey.generate()
    nas = NasPlugin(tmp_path / "nas")
    manager = PluginManager(store.home, store.peer_id, cache=tmp_path / "plugins", nas=nas)
    nas.plugin_manager = manager
    service = NodeApps(AppStore(tmp_path / "business", key), store.peer_id, lambda: None, plugins=manager)
    app.state.node_apps, app.state.craft_plugins, app.state.nas_plugin = service, manager, nas
    _, tools, _ = build_agent_integrations(app, store, store.home)
    yield SimpleNamespace(manager=manager, service=service, tools=tools, key=key, peer_id=store.peer_id)
    manager.close()


def result(messages, call_id):
    return json.loads(next(message["content"] for message in messages
                           if message.get("tool_call_id") == call_id and message["role"] == "tool"))


class BusinessModel:
    """Only the model boundary is scripted; context is read from actual tool output."""

    def __init__(self, slug, action):
        self.slug, self.action = slug, action
        self.calls = []

    def complete(self, model_id, messages, specs, cancelled, on_event):
        self.calls.append((model_id, copy.deepcopy(messages), copy.deepcopy(specs)))
        step = len(self.calls) - 1
        if step == 0:
            return assistant(name=ALL_LIST, call_id="plugins")
        if step == 1:
            plugin = next(row for row in result(messages, "plugins")["plugins"] if row["id"] == self.slug)
            assert plugin["discovery_tool"] == ALL_INFO and plugin["execution_tool"] == ACTION
            return assistant(name=tool_menu.NAME, args={"load": [plugin["discovery_tool"]]}, call_id="load-info")
        if step == 2:
            return assistant(name=ALL_INFO, args={"app": self.slug}, call_id="info")
        if step == 3:
            self.info = result(messages, "info")
            assert self.action in self.info["operations"]
            return assistant(name=tool_menu.NAME, args={"load": [self.info["execution_tool"]]}, call_id="load-action")
        if step == 4:
            return assistant(name=self.info["execution_tool"], args={
                "app": self.info["app"], "action": self.action,
                "arguments": {"name": "Created through discovered Ryn tools"},
                "reviewed_context": self.info["workspace_context"],
            }, call_id="business-action")
        assert step == 5
        self.receipt = result(messages, "business-action")
        assert self.receipt["app"] == self.slug
        return assistant("已创建并保存。")


@pytest.mark.parametrize("slug,action,collection", [
    ("resume", "create_resume", "resumes"),
    ("recruit", "create_job", "jobs"),
    ("desk", "create_ticket", "tickets"),
])
def test_discovery_to_real_business_operation_keeps_exact_approval(apps, tmp_path, slug, action, collection):
    apps.manager.install(slug)
    model = BusinessModel(slug, action)
    runtime = AgentRuntime(AgentStore(tmp_path / "agent", apps.key), model, apps.tools, Catalog(),
                           consumer_node_id=apps.peer_id)
    runtime.update_settings({"enabled": True, "model_id": "local:test", "permission_mode": "automatic",
                             "allowed_write_tools": [ACTION], "desktop_access": False})
    task = runtime.create({"message": f"请在 Ryn {slug} 创建一条记录"})
    advance(runtime, 20)
    waiting = runtime.get(task["id"])
    assert waiting["status"] == "waiting_approval"
    assert apps.service.call("", "view", {"app": slug})["records"] == {}
    pending = waiting["pending_approval"]
    assert pending["preview"]["app_preview"]["app"] == slug
    assert pending["preview"]["model_data_recipient"]["catalog_model_id"] == "local:test"

    approve(runtime, task["id"])
    advance(runtime, 5)
    done = runtime.get(task["id"])
    assert done["status"] == "completed" and not done["local_execution"]["desktop_access"]
    stored = apps.service.call("", "view", {"app": slug})
    assert stored["records"][collection][0]["name"] == "Created through discovered Ryn tools"
    assert stored["revision"] == model.info["workspace_context"]["revision"] + 1
    operation = next(row for row in runtime.store.read()["tasks"][task["id"]]["receipts"] if row["tool"] == ACTION)
    assert apps.service.call("", "receipt", {"app": slug, "operation_id": operation["operation_id"]}) == model.receipt
    verified = apps.tools.verify(operation, [{"verifier": "node_app_receipt_state"}], context={}, cancelled=lambda: False)
    assert verified["status"] == "passed"


def test_business_info_context_is_fresh_without_private_records_and_stays_revocable(apps):
    apps.manager.install("desk")
    first = apps.tools.execute(ALL_INFO, {"app": "desk"}, operation_id="info-first", cancelled=lambda: False)
    assert first["operations"] == CATALOG["desk"]["operations"]
    invoke(apps.service, "", "desk", "create_ticket", {"name": "PRIVATE_WORKSPACE_TITLE"})
    fresh = apps.tools.execute(ALL_INFO, {"app": "desk"}, operation_id="info-fresh", cancelled=lambda: False)
    assert fresh["workspace_context"]["revision"] == first["workspace_context"]["revision"] + 1
    assert "PRIVATE_WORKSPACE_TITLE" not in json.dumps(fresh)
    args = {"app": "desk", "action": "list_tickets", "arguments": {}, "reviewed_context": first["workspace_context"]}
    context = {"consumer_node_id": apps.peer_id, "model_destination": {"catalog_model_id": "local:test"}}
    with pytest.raises(AgentToolRejected, match="apps_review_changed"):
        apps.tools.preview(ACTION, args, context=context)
    apps.manager.set_enabled("desk", False)
    with pytest.raises(AgentToolRejected, match="plugin_disabled"):
        apps.tools.execute(ALL_INFO, {"app": "desk"}, operation_id="disabled-info", cancelled=lambda: False)


def test_nas_discovery_names_exact_tools_without_granting_resources(apps):
    apps.manager.install("nas")
    info = apps.tools.execute(ALL_INFO, {"app": "nas"}, operation_id="nas-info", cancelled=lambda: False)
    assert info["execution_tool"] == "ryn_list_resource_roots"
    assert info["execution_tools"] == list(NAS_TOOLS)
    assert info["resource_permissions_required"] and "Agent resource scope" in info["scope_guidance"]
    assert "workspace_context" not in info
    context = {"owner_id": "local_owner", "consumer_node_id": apps.peer_id,
               "resource_grants": [], "egress_grants": []}
    roots = apps.tools.execute(info["execution_tool"], {}, operation_id="nas-roots", context=context, cancelled=lambda: False)
    assert roots["roots"] == []
    assert context["resource_grants"] == [] and apps.service.store.read()["grants"] == {}
