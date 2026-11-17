"""S13-I1 task-local approval keys, real dispatch receipts and recovery."""
import copy
import hashlib
import uuid
from pathlib import Path

import pytest
from agent_tool_fixtures import expose_on_create
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_default_model import setup as default_setup, waiting

from rynmesh.agent_runtime import execution_policy as policy, operation_permissions, state, subagents
from rynmesh.agent_runtime.default_model import NAME
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore


def row(tmp_path):
    return {"id": "task-one", "permission_mode": "confirm_changes", "goal": {"revision": 1}, "scope_revision": 1,
            "local_execution": {"workspace_root": str(tmp_path)}, "approved": None}


def grant(target, call, metadata, approval_id="approved-original"):
    keys = policy.approval_keys(call, {**metadata, "workspace_root": target["local_execution"]["workspace_root"]})
    target.setdefault("task_grants", {}).update({key: {"approval_id": approval_id, "task_id": target["id"],
        "goal_revision": target["goal"]["revision"], "scope_revision": target["scope_revision"]} for key in keys})
    return keys


def test_T1_shell_keys_normalize_literal_tokens_and_bind_cwd_shell_tty(tmp_path):
    call = {"name": "ryn_shell_execute", "arguments": {"command": 'echo "hello world"', "cwd": str(tmp_path), "shell": "sh"}}
    meta = {"effect": "write"}
    target = row(tmp_path)
    keys = grant(target, call, meta)
    same = {**call, "arguments": {**call["arguments"], "command": "echo   'hello world'"}}
    assert policy.approval_keys(same, meta) == keys
    assert policy.decide(same, meta, target, {}) == policy.Decision("execute", "task_grant")
    for change in ({"cwd": str(tmp_path / "other")}, {"shell": "powershell"}, {"tty": True}, {"command": "echo other"}):
        other = {**call, "arguments": {**call["arguments"], **change}}
        assert policy.decide(other, meta, target, {}).action == "ask"
    compound = {**call, "arguments": {**call["arguments"], "command": "echo first; echo second"}}
    assert policy.approval_keys(compound, meta) != keys


def test_T2_patch_requires_every_source_and_move_target_key(tmp_path):
    meta = {"effect": "write", "workspace_root": str(tmp_path)}
    def patch(*names):
        return {"name": "ryn_apply_patch", "arguments": {"patch": "*** Begin Patch\n" + "".join(
            f"*** Add File: {name}\n+hello\n" for name in names) + "*** End Patch"}}
    target = row(tmp_path)
    keys = grant(target, patch("a.txt"), meta)
    assert policy.decide(patch("a.txt", "b.txt"), meta, target, {}).action == "ask"
    keys += grant(target, patch("b.txt"), meta, "approved-other")
    assert len(set(keys)) == 2
    assert policy.decide(patch("a.txt", "b.txt"), meta, target, {}).basis == "task_grant"
    move = {"name": "ryn_apply_patch", "arguments": {"patch": "*** Begin Patch\n*** Update File: a.txt\n*** Move to: c.txt\n@@\n-hello\n+new\n*** End Patch"}}
    assert len(policy.approval_keys(move, meta)) == 2
    assert policy.decide(move, meta, target, {}).action == "ask"


def test_T2_file_paths_are_absolute_normalized_and_mcp_keys_bind_original_identity(tmp_path):
    meta = {"effect": "write", "workspace_root": str(tmp_path)}
    call = {"name": "ryn_file_write", "arguments": {"path": "folder/../a.txt", "content": "one"}}
    same = {**call, "arguments": {"path": str(tmp_path / "a.txt"), "content": "different content"}}
    assert policy.approval_keys(call, meta) == policy.approval_keys(same, meta)
    binding = {"server_id": "server-a", "plugin_id": "plugin-a", "original_tool_name": "write"}
    mcp = {"name": "mcp__server__write", "arguments": {}, "extension_binding": binding}
    keys = policy.approval_keys(mcp, meta)
    for field in binding:
        changed = {**mcp, "extension_binding": {**binding, field: "other"}}
        assert policy.approval_keys(changed, meta) != keys


def test_T3_explicit_approval_and_T6_read_only_cannot_be_bypassed(tmp_path):
    target = row(tmp_path)
    call, meta = {"name": "share_service", "arguments": {"service_id": "A"}}, {"effect": "write"}
    grant(target, call, meta)
    assert policy.decide(call, {**meta, "requires_explicit_approval": True}, target, {}).basis == "explicit_approval"
    target["permission_mode"] = "read_only"
    assert policy.decide(call, meta, target, {}) == policy.Decision("deny", "read_only")
    target["permission_mode"] = "automatic"
    assert policy.decide(call, meta, target, {}) == policy.Decision("execute", "automatic")
    target["permission_mode"] = "workspace"
    assert policy.decide(call, meta, {**target, "id": "different-task"}, {}).action == "ask"


def setup_remember(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "A"}),
        assistant(name="share_service", args={"service_id": "B"}))
    runtime.update_settings({"permission_mode": "confirm_changes"})
    task = runtime.create({"message": "Share A and B"})
    advance(runtime, 2)
    pending = runtime.get(task["id"])
    return runtime, model, tools, key, pending


def remember(runtime, pending, **extra):
    return runtime.action(pending["id"], {"action": "approve", "approval_id": pending["pending_approval"]["id"],
        "expected_control_revision": pending["control_revision"], "client_request_id": str(uuid.uuid4()), "remember": "task", **extra})


def test_T4_remember_reaches_second_dispatch_after_restart_and_T7_original_approval_proof(tmp_path):
    runtime, model, tools, key, pending = setup_remember(tmp_path)
    remember(runtime, pending)
    advance(runtime)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    advance(restarted, 2)
    current = restarted.store.read()["tasks"][pending["id"]]
    assert [args["service_id"] for _, args, _ in tools.calls] == ["A", "B"]
    assert len([e for e in current["events"] if e["type"] == "approval_requested"]) == 1
    accepted = next(e for e in current["events"] if e["type"] == "approved")
    assert accepted["remember"] == "task"
    assert current["receipts"][-1]["authorization"] == "task_grant"
    assert current["receipts"][-1]["permission_record"]["basis"] == "task_grant"
    authorized = next(e for e in current["events"] if e["type"] == "task_grant_authorized")
    assert authorized["approval_keys"] and all(len(key) == 64 for key in authorized["approval_keys"])
    fact = operation_permissions.projection(current)[-1]
    assert fact["permission_basis"] == "owner_task_grant" and fact["approval_id"] == pending["pending_approval"]["id"]
    tampered = copy.deepcopy(current)
    tampered["receipts"][-1]["permission_record"]["approval_id"] = "forged"
    assert operation_permissions.projection(tampered)[-1]["permission_basis"] == "unknown"


@pytest.mark.parametrize("change", ["task_mode", "global_mode", "write_tools", "workspace", "goal", "edit"])
def test_T5_permission_and_goal_changes_clear_task_grants(tmp_path, change):
    runtime, _, _, _, pending = setup_remember(tmp_path)
    remember(runtime, pending)
    advance(runtime)
    current = runtime.get(pending["id"])
    assert runtime.store.read()["tasks"][current["id"]]["task_grants"]
    if change == "task_mode":
        runtime.action(current["id"], {"action": "pause"})
        current = runtime.get(current["id"])
        runtime.action(current["id"], {"action": "set_permission", "permission_mode": "workspace", "expected_control_revision": current["control_revision"]})
    elif change == "global_mode":
        runtime.update_settings({"permission_mode": "read_only"})
    elif change == "write_tools":
        runtime.update_settings({"allowed_write_tools": ["share_service"]})
    elif change == "workspace":
        new_root = tmp_path / "new-workspace"
        new_root.mkdir()
        runtime.update_settings({"workspace_root": str(new_root)})
    else:
        body = {"message": "Changed owner goal", "intent": "steer", "expected_control_revision": current["control_revision"],
                "expected_goal_revision": current["goal"]["revision"], "client_request_id": str(uuid.uuid4())}
        if change == "edit":
            body["edit_message_index"] = 0
        runtime.add_message(current["id"], body)
    assert not runtime.store.read()["tasks"][current["id"]].get("task_grants")


def test_T1_empty_quoted_argument_is_not_the_same_command(tmp_path):
    meta = {"effect": "write", "workspace_root": str(tmp_path)}
    def call(command):
        return {"name": "ryn_shell_execute", "arguments": {"command": command, "shell": "sh"}}
    assert policy.approval_keys(call('echo ""'), meta) != policy.approval_keys(call("echo"), meta)


def test_T4_grant_limit_falls_back_to_once(tmp_path):
    runtime, _, _, _, pending = setup_remember(tmp_path)
    runtime.store.transaction(lambda d: d["tasks"][pending["id"]].update(task_grants={f"existing-{n}": {} for n in range(256)}))
    remember(runtime, pending)
    advance(runtime, 3)
    current = runtime.get(pending["id"])
    assert current["status"] == "waiting_approval"
    assert len(runtime.store.read()["tasks"][pending["id"]]["task_grants"]) == 256


def test_T4_real_file_write_reuses_path_grant_but_another_path_asks(tmp_path):
    runtime, model, tools, _, _ = default_setup(tmp_path)
    tools.local.store = runtime.store
    expose_on_create(runtime, ["ryn_file_write"])
    runtime.update_settings({"permission_mode": "confirm_changes"})
    model.responses.extend([assistant(name="ryn_file_write", args={"path": "a.txt", "content": "one"}),
        assistant(name="ryn_file_write", args={"path": "./a.txt", "content": "two", "expected_sha256": hashlib.sha256(b"one").hexdigest()}),
        assistant(name="ryn_file_write", args={"path": "b.txt", "content": "new path"})])
    task = runtime.create({"message": "Write then update a.txt; ask for b.txt"})
    advance(runtime, 2)
    remember(runtime, runtime.get(task["id"]))
    advance(runtime, 5)
    current = runtime.store.read()["tasks"][task["id"]]
    root = Path(current["local_execution"]["workspace_root"])
    assert (root / "a.txt").read_text() == "two" and not (root / "b.txt").exists()
    assert current["status"] == "waiting_approval" and current["pending_approval"]["arguments"]["path"] == "b.txt"
    tools.local.close()


def test_T4_default_model_and_reconciliation_accept_task_grant_without_replaying_write(tmp_path):
    runtime, model, tools, key, _ = default_setup(tmp_path)
    task, _ = waiting(runtime, model)
    remember(runtime, task)
    advance(runtime)
    args = {"model_id": "local:test", "expected_settings_revision": runtime.configuration_snapshot()["settings_revision"]}
    model.responses.append(assistant(name=NAME, args=args))
    advance(runtime)
    execute = tools.execute
    def lost_ack(name, arguments, **options):
        execute(name, arguments, **options)
        raise RuntimeError("lost acknowledgement after atomic commit")
    tools.execute = lost_ack
    tools.reconcile = lambda *args, **kwargs: {"outcome": "unknown"}
    advance(runtime)
    failed = runtime.store.read()["tasks"][task["id"]]
    assert failed["in_flight"]["authorization"] == "task_grant"
    revision = runtime.configuration_snapshot()["settings_revision"]
    assert runtime.settings()["model_id"] == "local:test"
    tools.execute = execute
    del tools.reconcile
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    tools.app.state.agent_runtime = restarted
    restarted.reconcile()
    advance(restarted)
    recovered = restarted.get(task["id"])
    assert recovered["reconciliation"]["state"] == "executed" and recovered["status"] == "queued"
    assert restarted.configuration_snapshot()["settings_revision"] == revision
    tools.local.close()
