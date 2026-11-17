"""S13-I2 owner write rules and permanent approval on real runtime paths."""
import copy
import json
import uuid

import pytest
from test_agent_runtime import make_runtime, assistant, advance
from test_ovh_s13_i1 import setup_remember
from test_agent_default_model import setup as default_setup, waiting
from rynmesh.agent_runtime import execution_policy as policy, operation_permissions
from rynmesh.agent_runtime.runtime import AgentRuntime, write_permission_revoked
from rynmesh.agent_runtime.store import AgentError, AgentStore
from test_agent_local_tools import local


def target(mode="confirm_changes"):
    return {"permission_mode": mode, "allowed_write_tools": ["share_service"], "approved": None}


@pytest.mark.parametrize("mode", ["automatic", "confirm_changes", "workspace"])
def test_t1_denied_precedes_allow_and_existing_approval(mode):
    row = {**target(mode), "approved": {"id": "already-approved"}}
    assert policy.decide({"name": "share_service", "arguments": {}}, {"effect": "write"}, row,
        {"denied_tools": ["share_service"]}) == policy.Decision("deny", "owner_rule")


def test_t2_allow_cannot_bypass_explicit_approval_and_t3_read_only():
    call, meta = {"name": "share_service", "arguments": {}}, {"effect": "write"}
    assert policy.decide(call, meta, target(), {}) == policy.Decision("execute", "owner_rule")
    assert policy.decide(call, {**meta, "requires_explicit_approval": True}, target(), {}).basis == "explicit_approval"
    assert policy.decide(call, meta, target("read_only"), {}).action == "deny"
    assert policy.decide(call, {"effect": "read"}, target("read_only"), {"denied_tools": ["share_service"]}).basis == "read"


def test_t1_mcp_rules_bind_server_and_original_name_not_model_alias():
    binding = {"server_id": "server-a", "plugin_id": "plugin-a", "original_tool_name": "publish"}
    key = 'mcp:["server-a","publish"]'
    call = {"name": "mcp_alias_one", "arguments": {}, "extension_binding": binding}
    row = {**target(), "allowed_write_tools": [key]}
    assert policy.decide(call, {"effect": "write"}, row, {}).basis == "owner_rule"
    assert policy.decide({**call, "name": "mcp_alias_changed"}, {"effect": "write"}, row, {}).basis == "owner_rule"
    assert policy.decide({**call, "extension_binding": {**binding, "server_id": "server-b"}}, {"effect": "write"}, row, {}).action == "ask"
    assert policy.decide(call, {"effect": "write"}, row, {"denied_tools": [key]}).action == "deny"
    assert policy.decide(call, {"effect": "write"}, {**row, "allowed_write_tools": [call["name"]]}, {}).action == "ask"


def test_t4_intersection_is_rejected_atomically_for_full_and_partial_updates(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"allowed_write_tools": ["share_service"], "denied_tools": ["share_service"]})
    runtime.update_settings({"allowed_write_tools": ["share_service"]})
    before = runtime.configuration_snapshot()
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"denied_tools": ["share_service"]})
    assert runtime.configuration_snapshot() == before
    for invalid in ("share_service", ["unknown"], ["share_service", "share_service"], [23]):
        with pytest.raises(AgentError, match="agent_invalid_settings"):
            runtime.update_settings({"denied_tools": invalid})


def approve_always(runtime, pending):
    return runtime.action(pending["id"], {"action": "approve", "remember": "always",
        "approval_id": pending["pending_approval"]["id"], "expected_control_revision": pending["control_revision"],
        "client_request_id": str(uuid.uuid4())})


def test_t5_remember_always_persists_snapshot_and_actual_dispatch_after_restart(tmp_path):
    runtime, model, tools, key, pending = setup_remember(tmp_path)
    approve_always(runtime, pending)
    assert runtime.settings()["allowed_write_tools"] == ["share_service"]
    assert runtime.get(pending["id"])["allowed_write_tools"] == ["share_service"]
    advance(runtime)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    advance(restarted, 2)
    raw = restarted.store.read()["tasks"][pending["id"]]
    assert [args["service_id"] for _, args, _ in tools.calls] == ["A", "B"]
    assert raw["receipts"][-1]["authorization"] == "owner_rule"
    assert operation_permissions.projection(raw)[-1]["permission_basis"] == "owner_rule"
    future = restarted.create({"message": "New task"})
    assert future["allowed_write_tools"] == ["share_service"]
    restarted.update_settings({"allowed_write_tools": []})
    assert restarted.get(pending["id"])["allowed_write_tools"] == []


def test_t1_global_deny_reaches_real_model_result_and_revokes_approved_flight(tmp_path):
    runtime, model, tools, _, pending = setup_remember(tmp_path)
    approve_always(runtime, pending)
    runtime.update_settings({"allowed_write_tools": [], "denied_tools": ["share_service"]})
    advance(runtime, 2)
    raw = runtime.store.read()["tasks"][pending["id"]]
    assert not tools.calls
    assert any(e.get("result", {}).get("error") == "agent_permission_denied" for e in raw["events"])
    from rynmesh.agent_runtime.execution_policy import PERMISSION_DENIED_CORRECTION
    assert any(PERMISSION_DENIED_CORRECTION in m.get("content", "") for m in raw["messages"] if m["role"] == "tool")
    flight_row = {**target("automatic"), "in_flight": {"name": "share_service", "arguments": {},
        "effect": "write", "authorization": "approved"}}
    assert write_permission_revoked(flight_row, {"denied_tools": ["share_service"]})


def test_t5_mcp_remember_uses_stable_pair_and_update_settings_uses_same_validation(tmp_path):
    runtime, _, tools, _, pending = setup_remember(tmp_path)
    binding = {"server_id": "server-a", "plugin_id": "plugin-a", "original_tool_name": "publish"}
    original = tools.metadata
    tools.metadata = lambda name: {**original(name), "extension_binding": binding} if name == "share_service" else original(name)
    runtime.store.transaction(lambda data: data["tasks"][pending["id"]]["queue"][0].update(extension_binding=binding))
    approve_always(runtime, pending)
    assert runtime.settings()["allowed_write_tools"] == ['mcp:["server-a","publish"]']
    assert runtime.update_settings({"allowed_write_tools": ["share_service"]})["allowed_write_tools"] == ['mcp:["server-a","publish"]']
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"denied_tools": ['mcp:["server-a","publish"]']})


def test_t5_default_model_owner_rule_survives_lost_ack_reconciliation(tmp_path):
    from rynmesh.agent_runtime.default_model import NAME
    runtime, model, tools, key, _ = default_setup(tmp_path)
    pending, _ = waiting(runtime, model)
    approve_always(runtime, pending)
    advance(runtime)
    model.responses.append(assistant(name=NAME, args={"model_id": "local:test",
        "expected_settings_revision": runtime.configuration_snapshot()["settings_revision"]}))
    advance(runtime)
    execute = tools.execute
    def lost_ack(name, arguments, **options):
        execute(name, arguments, **options)
        raise RuntimeError("lost ack")
    tools.execute = lost_ack
    tools.reconcile = lambda *args, **kwargs: {"outcome": "unknown"}
    advance(runtime)
    raw = runtime.store.read()["tasks"][pending["id"]]
    assert raw["in_flight"]["authorization"] == "owner_rule" and runtime.settings()["model_id"] == "local:test"
    revision = runtime.configuration_snapshot()["settings_revision"]
    tools.execute = execute
    del tools.reconcile
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    tools.app.state.agent_runtime = restarted
    restarted.reconcile()
    advance(restarted)
    assert restarted.get(pending["id"])["reconciliation"]["state"] == "executed"
    assert restarted.configuration_snapshot()["settings_revision"] == revision
    tools.local.close()


def test_six_imported_permission_hooks_remain_ignored():
    from rynmesh.agent_runtime.extensions.imports import KNOWN_UNSUPPORTED_DIRS, KNOWN_UNSUPPORTED_MANIFEST_KEYS
    assert "not executed" in KNOWN_UNSUPPORTED_MANIFEST_KEYS["hooks"]
    assert "does not run" in KNOWN_UNSUPPORTED_DIRS["hooks"][1]


def test_live_local_owner_rule_and_denial_are_rechecked(local):
    tools, context, _, store = local
    def prepare(data):
        data["tasks"][context["task_id"]].update(allowed_write_tools=["ryn_file_write"],
            in_flight={"operation_id": "owner-write", "name": "ryn_file_write", "effect": "write",
                       "arguments": {"path": "a.txt"}, "authorization": "owner_rule"})
    store.transaction(prepare)
    allowed = tools.allowed({**context, "operation_id": "owner-write"})
    assert allowed()
    store.transaction(lambda data: data["settings"].update(denied_tools=["ryn_file_write"]))
    assert not allowed()
    store.transaction(lambda data: (data["settings"].update(denied_tools=[]),
        data["tasks"][context["task_id"]].update(allowed_write_tools=[])))
    assert not allowed()


def test_denied_rule_is_part_of_model_retry_authority():
    from rynmesh.agent_runtime.protocol import model_retry_authority
    settings = {"permission_mode": "automatic", "allowed_write_tools": []}
    assert model_retry_authority(settings) != model_retry_authority({**settings, "denied_tools": ["share_service"]})


def test_owner_rule_keeps_automatic_unknown_outcome_continuation_without_replay(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    calls = []
    def unknown(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("Acknowledgement lost")
    tools.execute = unknown
    created = runtime.create({"message": "Share A."})
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][created["id"]]
    assert row["status"] == "queued" and row["in_flight"] is None
    assert calls == [1] and row["receipts"][-1]["status"] == "unknown"
    assert row["receipts"][-1]["authorization"] == "owner_rule"
