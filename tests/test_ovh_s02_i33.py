"""S02-I33 regression tests: automatic-mode read-verified write retries and scoped cross-epoch write confirmation guards."""
from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import execution_policy
from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.protocol import digest


class PluginAndShellTools:
    """Real LocalTools shell execution combined with an extension-bound plugin write/read pair."""

    def __init__(self, store, home_dir):
        ledger = OperationLedger(home_dir)
        self.local = LocalTools(SimpleNamespace(home=home_dir), lambda: ledger)
        self.local.store = store
        self.plugin_write_calls: list[tuple[dict, str]] = []
        self.plugin_read_calls: list[tuple[dict, str]] = []
        self.fail_plugin_writes = False

    def specs(self):
        desc, _effect, _risk, params = LOCAL_SPECS["ryn_shell_execute"]
        shell_spec = {
            "type": "function",
            "function": {"name": "ryn_shell_execute", "description": desc, "parameters": copy.deepcopy(params)},
        }
        return [
            copy.deepcopy(shell_spec),
            {
                "type": "function",
                "function": {
                    "name": "ext_crm_read",
                    "description": "Read CRM record status",
                    "parameters": {
                        "type": "object",
                        "properties": {"record_id": {"type": "string"}},
                        "required": ["record_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ext_crm_write",
                    "description": "Update CRM record",
                    "parameters": {
                        "type": "object",
                        "properties": {"record_id": {"type": "string"}},
                        "required": ["record_id"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def metadata(self, name: str) -> dict:
        if name == "ryn_shell_execute":
            return {"effect": "write", "title": "ryn_shell_execute", "scope_context_supported": True}
        if name == "ext_crm_read":
            return {
                "effect": "read",
                "title": "Read CRM",
                "extension_binding": {"kind": "mcp", "server_id": "crm", "tool_name": "read"},
            }
        if name == "ext_crm_write":
            return {
                "effect": "write",
                "title": "Write CRM",
                "extension_binding": {"kind": "mcp", "server_id": "crm", "tool_name": "write"},
            }
        raise ValueError("unavailable")

    def verify_extension_binding(self, call: dict) -> None:
        binding = call.get("extension_binding")
        if not isinstance(binding, dict) or binding.get("server_id") != "crm":
            raise ValueError("invalid binding")

    def preview(self, name: str, arguments: dict, context=None, **_kwargs) -> dict:
        if name == "ryn_shell_execute" and context is not None:
            return self.local.preview(name, arguments, context)
        return {"tool": name, "arguments": copy.deepcopy(arguments)}

    def execute(self, name: str, arguments: dict, *, operation_id: str, cancelled, **kwargs) -> dict:
        if name == "ryn_shell_execute":
            ctx = kwargs["context"]
            return self.local.execute(
                name,
                arguments,
                operation_id=operation_id,
                cancelled=cancelled,
                expected_preview=self.local.preview(name, arguments, ctx),
                **kwargs,
            )
        if name == "ext_crm_read":
            self.plugin_read_calls.append((copy.deepcopy(arguments), operation_id))
            return {"record_id": arguments["record_id"], "status": "pending"}
        if name == "ext_crm_write":
            self.plugin_write_calls.append((copy.deepcopy(arguments), operation_id))
            if self.fail_plugin_writes:
                return {"is_error": True, "error": "agent_mcp_tool_returned_error", "side_effects": "unknown"}
            return {"record_id": arguments["record_id"], "updated": True}
        raise ValueError("unavailable")


def _make_switchable_runtime(tmp_path, *responses, permission_mode: str = "automatic"):
    runtime, model, _, key = make_runtime(tmp_path / "store", *responses)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    node_home = tmp_path / "node_home"
    node_home.mkdir(parents=True, exist_ok=True)
    tools = PluginAndShellTools(runtime.store, node_home)
    runtime.tools = tools
    rows = [
        {
            "id": identifier,
            "name": f"Model {identifier}",
            "source": "local_api",
            "source_name": identifier,
            "source_id": identifier,
            "peer_id": "self",
            "destination_revision": identifier + "-v1",
            "available": True,
            "agent_supported": True,
        }
        for identifier in ("local:test", "local:other")
    ]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_destination_binding = True
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "permission_mode": permission_mode,
            "workspace_root": str(workspace),
        }
    )
    return runtime, model, tools, workspace, key


def _switch_to_other_model(runtime, task_id: str):
    preview = runtime.preview_model_switch(task_id, "local:other")
    return runtime.switch_model(
        task_id,
        {
            "model_id": "local:other",
            "expected_control_revision": preview["control_revision"],
            "expected_scope_revision": preview["scope_revision"],
            "expected_destination": preview["destination"],
            "confirm_data_destination": True,
            "transfer_resource_scope": False,
            "client_request_id": str(uuid.uuid4()),
        },
    )


def _discover_all():
    return assistant(
        name="ryn_discover_tools",
        args={"load": ["ryn_shell_execute", "ext_crm_read", "ext_crm_write"]},
    )


def test_automatic_mode_allows_two_read_verified_retries_then_refuses_third(tmp_path):
    """T1 (Change 1): In full automatic mode, a failed write with side_effects=unknown may retry after a verified read up to 2 times, and is refused from the 3rd retry onward."""
    args = {"record_id": "rec-100"}
    runtime, model, tools, _, _ = _make_switchable_runtime(
        tmp_path,
        _discover_all(),
        # Initial failed write
        assistant(name="ext_crm_write", args=args),
        # Blind retry without read -> must still be rejected
        assistant(name="ext_crm_write", args=args),
        # Read #1 -> Retry #1 (allowed, fails again)
        assistant(name="ext_crm_read", args=args),
        assistant(name="ext_crm_write", args=args),
        # Read #2 -> Retry #2 (allowed, fails again)
        assistant(name="ext_crm_read", args=args),
        assistant(name="ext_crm_write", args=args),
        # Read #3 -> Retry #3 (3rd retry for same key -> refused!)
        assistant(name="ext_crm_read", args=args),
        assistant(name="ext_crm_write", args=args),
        assistant("Stopped after retry cap; asking owner."),
        permission_mode="automatic",
    )
    tools.fail_plugin_writes = True
    orig_complete = model.complete
    model.complete = lambda *a, **_kw: orig_complete(*a)

    created = runtime.create({"message": "Sync CRM record rec-100."})
    task_id = created["id"]
    for _ in range(30):
        if runtime.get(task_id)["status"] in {"completed", "failed", "interrupted"}:
            break
        runtime.run_once()

    done = runtime.get(task_id)
    assert done["status"] == "completed"
    # Initial attempt + 2 read-verified retries = 3 actual dispatches (blind retry & 3rd retry were both rejected)
    assert len(tools.plugin_write_calls) == 3
    assert len(tools.plugin_read_calls) == 3

    stored = runtime.store.read()["tasks"][task_id]
    expected_key = "ext_crm_write" + digest(args)
    assert stored.get("failed_write_retries", {}).get(expected_key) == 2

    tool_msgs = [json.loads(m["content"]) for m in stored["messages"] if m.get("role") == "tool"]
    refusals = [m for m in tool_msgs if m.get("error") == "agent_repeated_failed_write"]
    assert len(refusals) == 2
    assert "change the arguments" in refusals[-1]["correction"].lower() or "owner" in refusals[-1]["correction"].lower()


def test_non_automatic_mode_still_refuses_identical_failed_write_even_after_read(tmp_path):
    """T2 (Change 1): Non-automatic mode (workspace / confirm_changes) still refuses identical failed write even after a verified read."""
    args = {"record_id": "rec-200"}
    runtime, model, tools, _, _ = _make_switchable_runtime(
        tmp_path,
        _discover_all(),
        assistant(name="ext_crm_write", args=args),
        assistant(name="ext_crm_read", args=args),
        assistant(name="ext_crm_write", args=args),
        assistant("Reporting error to owner."),
        permission_mode="workspace",
    )
    tools.fail_plugin_writes = True
    orig_complete = model.complete
    model.complete = lambda *a, **_kw: orig_complete(*a)

    created = runtime.create({"message": "Update CRM record rec-200."})
    assert execution_policy.is_full_automatic(created, runtime.store.read()["settings"]) is False
    task_id = created["id"]
    for _ in range(20):
        cur = runtime.get(task_id)
        if cur["status"] == "waiting_approval":
            approve(runtime, task_id)
        elif cur["status"] in {"completed", "failed", "interrupted"}:
            break
        else:
            runtime.run_once()

    done = runtime.get(task_id)
    assert done["status"] == "completed"
    assert len(tools.plugin_write_calls) == 1
    assert len(tools.plugin_read_calls) == 1
    stored = runtime.store.read()["tasks"][task_id]
    assert "failed_write_retries" not in stored
    assert any(
        m.get("role") == "tool" and "agent_repeated_failed_write" in m.get("content", "")
        for m in stored["messages"]
    )


def test_model_history_task_allows_repeating_local_shell_command_after_model_switch(tmp_path):
    """T3 (Change 2): After switching models (model_history populated), repeating an identical local shell write command is allowed."""
    cmd_args = {"command": "Write-Output 'build-pass'"}
    runtime, model, tools, _, _ = _make_switchable_runtime(
        tmp_path,
        _discover_all(),
        assistant(name="ryn_shell_execute", args=cmd_args),
        assistant("First build finished."),
        permission_mode="automatic",
    )
    try:
        orig_complete = model.complete
        model.complete = lambda *a, **_kw: orig_complete(*a)

        created = runtime.create({"message": "Run the build check."})
        task_id = created["id"]
        advance(runtime, 6)
        assert runtime.get(task_id)["status"] == "completed"

        # Pause/switch model while keeping same goal_revision: simulate paused task switch
        runtime.store.transaction(lambda data: data["tasks"][task_id].update(status="paused"))
        _switch_to_other_model(runtime, task_id)
        model.responses.extend([
            _discover_all(),
            assistant(name="ryn_shell_execute", args=cmd_args),
            assistant("Second build after model switch finished."),
        ])
        runtime.action(
            task_id,
            {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]},
        )
        advance(runtime, 6)

        done = runtime.get(task_id)
        assert done["status"] == "completed"
        assert done["result"] == "Second build after model switch finished."
        stored = runtime.store.read()["tasks"][task_id]
        shell_receipts = [r for r in stored["receipts"] if r["tool"] == "ryn_shell_execute" and r["status"] == "confirmed"]
        assert len(shell_receipts) == 2
        assert not any("agent_operation_already_confirmed" in m.get("content", "") for m in stored["messages"])
    finally:
        tools.local.processes.close()


def test_model_history_blocks_repeated_plugin_write_until_owner_speaks_again(tmp_path):
    """T4 (Change 2): Repeated plugin write across context_epoch is blocked with new correction when owner has not spoken, and allowed once owner speaks again."""
    args = {"record_id": "rec-400"}
    runtime, model, tools, _, _ = _make_switchable_runtime(
        tmp_path,
        _discover_all(),
        assistant(name="ext_crm_write", args=args),
        assistant("Initial CRM update complete."),
        permission_mode="automatic",
    )
    orig_complete = model.complete
    model.complete = lambda *a, **_kw: orig_complete(*a)

    created = runtime.create({"message": "Update CRM rec-400."})
    task_id = created["id"]
    advance(runtime, 6)
    assert len(tools.plugin_write_calls) == 1

    # Switch model without new owner message -> identical plugin write must be blocked
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(status="paused"))
    _switch_to_other_model(runtime, task_id)
    model.responses.extend([
        _discover_all(),
        assistant(name="ext_crm_write", args=args),
        assistant("Blocked duplicate CRM write; asking owner to confirm."),
    ])
    runtime.action(
        task_id,
        {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]},
    )
    advance(runtime, 6)
    after_switch = runtime.get(task_id)
    assert after_switch["status"] == "completed"
    assert len(tools.plugin_write_calls) == 1

    stored_mid = runtime.store.read()["tasks"][task_id]
    refused = next(
        json.loads(m["content"])
        for m in stored_mid["messages"]
        if m.get("role") == "tool" and "agent_operation_already_confirmed" in m.get("content", "")
    )
    assert refused["error"] == "agent_operation_already_confirmed"
    assert "这个调用在更早的上下文里已确认执行过" in refused["correction"]
    assert "请先向主人确认" in refused["correction"]

    # Owner speaks again (even with intent="information", keeping same goal_revision!) -> identical plugin write is now allowed
    model.responses.extend([
        assistant(name="ext_crm_write", args=args),
        assistant("Executed second CRM update per owner confirmation."),
    ])
    runtime.add_message(
        task_id,
        {
            "message": "是的，请再执行一次同一个更新。",
            "intent": "information",
            "expected_control_revision": after_switch["control_revision"],
            "expected_goal_revision": after_switch["goal"]["revision"],
        },
    )
    advance(runtime, 6)
    final_row = runtime.get(task_id)
    assert final_row["status"] == "completed"
    assert len(tools.plugin_write_calls) == 2
