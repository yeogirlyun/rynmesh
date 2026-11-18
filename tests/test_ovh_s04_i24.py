"""S04-I24: automatic permissions through policy, menus and real tool dispatch."""
import copy

import pytest
from test_agent_integrations import node
from test_agent_runtime import advance, assistant
from test_craft_plugins import manager
from test_craft_agent import craft

from rynmesh.agent_runtime import subagents, tool_menu
from rynmesh.agent_runtime.execution_policy import classify_operation, decide
from rynmesh.agent_runtime.runtime import write_permission_revoked
from rynmesh.agent_runtime.store import DEFAULT_SETTINGS
from rynmesh.node_apps import craft_automation_catalog
from rynmesh.node_apps.craft_agent import INSTALL, TOOLS


def policy(mode, desktop=False):
    return {"permission_mode": mode, "desktop_access": desktop,
            "local_execution": {"desktop_access": desktop}, "allowed_write_tools": []}


@pytest.mark.parametrize("task,host,expected", [
    ("automatic", "automatic", ("execute", "automatic")),
    ("automatic", "workspace", ("ask", "explicit_approval")),
    ("workspace", "automatic", ("ask", "explicit_approval")),
    ("automatic", "confirm_changes", ("ask", "explicit_approval")),
])
def test_t1_explicit_approval_modes(task, host, expected):
    result = decide({"name": INSTALL, "arguments": {"app": "wordcraft"}},
                    {"effect": "write", "requires_explicit_approval": True}, policy(task), policy(host))
    assert (result.action, result.basis) == expected


def test_t2_read_only_and_owner_denial_still_win():
    call = {"name": INSTALL, "arguments": {"app": "wordcraft"}}
    meta = {"effect": "write", "requires_explicit_approval": True}
    assert decide(call, meta, policy("read_only"), policy("automatic")).action == "deny"
    host = {**policy("automatic"), "denied_tools": [INSTALL]}
    assert decide(call, meta, policy("automatic"), host).basis == "owner_rule"
    assert classify_operation(INSTALL, call["arguments"], meta)["basis"] == "explicit_approval"
    assert classify_operation(INSTALL, call["arguments"], meta)["effect_class"] == "write"


def install_runtime(craft):
    craft.backend.ready = False
    runtime, model = craft.runtime(
        assistant(name=tool_menu.NAME, args={"load": [TOOLS, INSTALL]}),
        assistant(name=INSTALL, args={"app": "wordcraft"}, call_id="install"),
        assistant("Installed."), mode="automatic", allowed=[INSTALL])
    return runtime, model


def test_t3_craft_installs_on_real_runtime_without_approval(craft):
    runtime, _ = install_runtime(craft)
    tid = runtime.create({"message": "Install document component"})["id"]
    advance(runtime, 8)
    row = runtime.get(tid)
    assert row["status"] == "completed", row.get("error")
    assert row["pending_approval"] is None
    assert craft.backend.installations == ["wordcraft"]
    auto = next(e for e in row["events"] if e["type"] == "automatically_authorized")
    assert auto["explicit_approval_bypassed"] is True
    assert auto["preview"]["sha256"] == craft_automation_catalog.AUTOMATION["wordcraft"]["windows-x64"]["sha256"]


def test_t4_automatic_subagent_install_is_not_blocked(craft):
    runtime, _ = install_runtime(craft)
    parent = runtime.create({"message": "Coordinate installation"})["id"]
    def spawn(data):
        data["tasks"][parent]["status"] = "paused"
        return subagents.spawn_agent_in_tx(data, data["tasks"][parent], {
            "task_name": "install", "prompt": "Install the document component", "role": "worker",
            "write_scope": ["."]})["agent_id"]
    child = runtime.store.transaction(spawn)
    advance(runtime, 8)
    row = runtime.get(child)
    assert row["status"] == "completed", row.get("error")
    assert craft.backend.installations == ["wordcraft"]
    assert not any(e.get("result", {}).get("error") == "agent_subagent_approval_blocked" for e in row["events"])


@pytest.mark.parametrize("name", ["ryn_browser_open", "ryn_computer_action", "mcp__computer_use__computer_click"])
def test_t5_automatic_desktop_ignores_false_flags(name):
    d = decide({"name": name, "arguments": {}}, {"effect": "write", "computer_access": True},
               policy("automatic"), policy("automatic"))
    assert (d.action, d.basis) == ("execute", "automatic")


@pytest.mark.parametrize("mode,enabled", [("automatic", True), ("workspace", False)])
def test_t6_t7_t8_menu_policy_browser_and_old_tasks(craft, mode, enabled):
    runtime, _ = craft.runtime(mode=mode)
    tid = runtime.create({"message": "Use browser"})["id"]
    # Existing persisted tasks must work without a data migration.
    runtime.store.transaction(lambda d: d["tasks"][tid]["local_execution"].update(desktop_access=False))
    row = runtime.store.read()["tasks"][tid]
    (specs, _), _ = runtime._menu(row)
    names = {s["function"]["name"] for s in specs}
    assert set(tool_menu.BROWSER_TOOLS).issubset(names) is enabled
    context = {"task_id": tid, "permission_mode": mode, "local_execution": row["local_execution"]}
    local = craft.tools.local
    assert local.policy(context)["desktop_access"] is enabled
    if enabled:
        browser = local.browser(context)
        assert browser is local.browser(context)
    else:
        from rynmesh.agent_runtime.store import AgentToolRejected
        with pytest.raises(AgentToolRejected, match="agent_desktop_access_required"):
            local.browser(context)
        d = decide({"name": "ryn_browser_open", "arguments": {}}, {"effect": "write", "computer_access": True},
                   row, runtime.store.read()["settings"])
        assert (d.action, d.basis) == ("ask", "desktop_access")


@pytest.mark.parametrize("task,host", [("automatic", "workspace"), ("workspace", "automatic")])
@pytest.mark.parametrize("task_flag,host_flag", [(False, False), (False, True), (True, False), (True, True)])
def test_t9_mixed_desktop_requires_both_flags(task, host, task_flag, host_flag):
    d = decide({"name": "ryn_browser_open", "arguments": {}}, {"effect": "write", "computer_access": True},
               policy(task, task_flag), policy(host, host_flag))
    assert (d.action, d.basis) == (("execute", "automatic") if task_flag and host_flag else ("ask", "desktop_access"))


def test_t9_restart_automatic_browser_not_revoked():
    row = {**policy("automatic"), "in_flight": {"name": "ryn_browser_open", "effect": "write",
        "arguments": {"url": "https://example.test"}, "computer_access": True, "authorization": "automatic"}}
    assert write_permission_revoked(row, policy("automatic")) is False
    assert write_permission_revoked(row, policy("workspace")) is True


def test_t11_changed_install_preview_still_rejected_in_automatic(craft, monkeypatch):
    runtime, model = install_runtime(craft)
    original = craft.tools.craft.preview
    calls = []
    def changed(name, args, context, **kwargs):
        preview = original(name, args, context, **kwargs)
        if name == INSTALL:
            calls.append(copy.deepcopy(preview))
            if len(calls) > 1:
                preview["sha256"] = "0" * 64
        return preview
    monkeypatch.setattr(craft.tools.craft, "preview", changed)
    tid = runtime.create({"message": "Install document component"})["id"]
    advance(runtime, 5)
    row = runtime.get(tid)
    assert row["status"] != "waiting_approval"
    assert not craft.backend.installations
    assert any(e.get("result", {}).get("error") == "agent_approval_preview_changed" for e in row["events"])
    assert len(calls) >= 2


def test_t12_fixed_hashes_and_default_storage_unchanged():
    assert DEFAULT_SETTINGS["desktop_access"] is False
    assert craft_automation_catalog.AUTOMATION["wordcraft"]["windows-x64"]["sha256"] == "4576d76363b0513242e0c1e5038fabaee4b3369909de60821900a01fc6277261"


def test_mode_switch_restores_stored_desktop_flag(craft):
    runtime, _ = craft.runtime(mode="automatic")
    row = runtime.create({"message": "Switch mode"})
    assert row["local_execution"]["desktop_access"] is True
    runtime.store.transaction(lambda d: d["tasks"][row["id"]].update(status="paused"))
    row = runtime.action(row["id"], {"action": "set_permission", "permission_mode": "workspace", "expected_revision": row["revision"]})
    assert row["local_execution"]["desktop_access"] is False
    assert runtime.store.read()["settings"]["desktop_access"] is False


def test_tool_descriptions_explain_automatic(craft):
    from rynmesh.node_apps.agent import ACTION, SPECS
    specs = craft.tools.craft.specs() + SPECS
    for name in (INSTALL, ACTION):
        description = next(s["function"]["description"] for s in specs if s["function"]["name"] == name)
        assert "unless the task runs in automatic mode" in description


def test_settings_sync_and_continuation_keep_automatic_desktop(craft):
    runtime, _ = craft.runtime(mode="automatic")
    tid = runtime.create({"message": "Continue this work"})["id"]
    runtime.store.transaction(lambda d: d["tasks"][tid].update(status="completed", local_execution={
        **d["tasks"][tid]["local_execution"], "desktop_access": False}))
    continued = runtime._continue_in_new_task(tid, {}, "Continue")
    assert continued["permission_mode"] == "automatic"
    assert continued["local_execution"]["desktop_access"] is True
    runtime.update_settings({"permission_mode": "workspace"})
    old = runtime.store.read()["tasks"][tid]
    assert old["permission_mode"] == "automatic"
    assert old["local_execution"]["desktop_access"] is True
    assert runtime.settings()["desktop_access"] is False


def test_t7_automatic_live_guard_and_browser_dispatch(craft, monkeypatch):
    runtime, _ = craft.runtime(mode="automatic")
    tid = runtime.create({"message": "Use the browser"})["id"]
    operation = {"name": "ryn_browser_open", "arguments": {"url": "https://example.test"}, "effect": "write",
                 "computer_access": True, "operation_id": "browser", "authorization": "automatic"}
    runtime.store.transaction(lambda d: d["tasks"][tid].update(status="running", in_flight=operation))
    row = runtime.store.read()["tasks"][tid]
    context = {"task_id": tid, "operation_id": "browser", "local_execution": row["local_execution"]}
    local = craft.tools.local
    assert local.allowed(context)() is True
    browser = local.browser(context)
    def run(name, args, task_id, resolve, allowed, cancelled):
        assert name == operation["name"] and task_id == tid
        assert allowed() is True
        assert not cancelled()
        return {"opened": True}
    monkeypatch.setattr(browser, "execute", run)
    assert local.perform(operation["name"], operation["arguments"], context, lambda: False) == {"opened": True}
    runtime.store.transaction(lambda d: d["tasks"][tid].update(in_flight={
        **operation, "name": INSTALL, "computer_access": False,
        "operation_classification": classify_operation(INSTALL, operation["arguments"],
                                                       {"requires_explicit_approval": True})}))
    assert local.allowed(context)() is True
    assert write_permission_revoked(runtime.store.read()["tasks"][tid], policy("workspace")) is True
    runtime.update_settings({"permission_mode": "workspace"})
    assert local.allowed(context)() is False


def test_t10_node_app_real_mutation_and_automatic_event(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from fastapi import FastAPI
    from test_agent_runtime import Catalog, Model
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.node_apps.agent import ACTION, SPECS, AppTools
    from rynmesh.node_apps.service import NodeApps
    from rynmesh.node_apps.store import AppStore
    key = X25519PrivateKey.generate()
    service = NodeApps(AppStore(tmp_path / "business", key), "consumer-node", lambda: None)
    app = FastAPI()
    app.state.node_apps = service
    impl = AppTools(app)
    class Tools:
        def specs(self):
            return SPECS
        def metadata(self, name):
            return {"effect": "write", "requires_explicit_approval": True, "scope_context_supported": True,
                    "supports_preview": True, "preview_precondition": True}
        def preview(self, name, args, context=None):
            return impl.preview(args, context)
        def execute(self, name, args, *, operation_id, cancelled, context, expected_preview=None):
            assert expected_preview is not None
            return impl.execute(name, args, operation_id, cancelled, expected_preview, context)
    model = Model(assistant(name=tool_menu.NAME, args={"load": [ACTION]}), assistant(name=ACTION, args={"app": "desk", "action": "create_ticket", "arguments": {"name": "S04-I24 ticket"}}), assistant("Done."))
    runtime = AgentRuntime(AgentStore(tmp_path / "agent", key), model, Tools(), Catalog(), consumer_node_id="consumer-node")
    runtime.update_settings({"enabled": True, "model_id": "local:test", "permission_mode": "automatic", "allowed_write_tools": [ACTION]})
    tid = runtime.create({"message": "Create a ticket"})["id"]
    advance(runtime, 7)
    row = runtime.get(tid)
    assert row["status"] == "completed", row.get("error")
    assert service.call("", "view", {"app": "desk"})["records"]["tickets"][0]["name"] == "S04-I24 ticket"
    event = next(e for e in row["events"] if e["type"] == "automatically_authorized")
    assert event["explicit_approval_bypassed"] is True
    assert event["preview"]["model_data_recipient"]["catalog_model_id"] == "local:test"
