"""S13-I25: task-owned permission changes through the real runtime and tools."""
from types import SimpleNamespace
import threading

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_default_model import setup as default_model_runtime
from agent_tool_fixtures import expose

from rynmesh.agent_runtime.default_model import NAME
from rynmesh.agent_runtime.execution_policy import decide, is_full_automatic
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.native_computer import NativeComputerFacade
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def change(runtime, task_id, mode):
    current = runtime.get(task_id)
    return runtime.action(task_id, {"action": "set_permission", "permission_mode": mode,
                                   "expected_control_revision": current["control_revision"]})


def test_t1_paused_read_only_can_switch_to_workspace_and_write_real_file(tmp_path):
    runtime, model, tools, _, _ = default_model_runtime(tmp_path / "state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime.update_settings({"permission_mode": "read_only", "workspace_root": str(workspace)})
    local = tools.local
    local.store = runtime.store
    model.responses.append(assistant(name="ryn_file_write", args={"path": "result.txt", "content": "changed task permission"}))
    try:
        task = runtime.create({"message": "Write result.txt"})
        expose(runtime, task["id"], ["ryn_file_write"])
        runtime.action(task["id"], {"action": "pause"})
        changed = change(runtime, task["id"], "workspace")
        assert changed["permission_mode"] == "workspace"
        assert runtime.settings()["permission_mode"] == "read_only"
        runtime.action(task["id"], {"action": "resume", "expected_control_revision": changed["control_revision"]})
        advance(runtime, 2)
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "changed task permission"
    finally:
        local.processes.close()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_t2_active_permission_change_is_rejected(tmp_path, status):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Continue"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(status=status))
    assert runtime.get(task["id"])["allowed_actions"].get("set_permission", {}).get("enabled") is False
    with pytest.raises(AgentError, match="agent_invalid_transition"):
        change(runtime, task["id"], "read_only")


@pytest.mark.parametrize("status", ["paused", "waiting_input", "waiting_approval", "completed", "failed", "cancelled", "interrupted"])
def test_t3_permission_event_revisions_and_old_authority_are_cleared(tmp_path, status):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Continue"})
    def prepare(data):
        row = data["tasks"][task["id"]]
        row.update(status=status, approved={"id": "old"}, pending_approval={"id": "old"},
                   task_grants=[{"operation_id": "old"}], permission_mode="read_only")
        row["operation_permissions"] = [{"basis": "read_only", "operation_id": "historical"}]
    runtime.store.transaction(prepare)
    before = runtime.get(task["id"])
    changed = change(runtime, task["id"], "workspace")
    raw = runtime.store.read()["tasks"][task["id"]]
    assert changed["control_revision"] == before["control_revision"] + 1
    event = next(item for item in changed["events"] if item["type"] == "permission_changed")
    assert (event["old_mode"], event["new_mode"]) == ("read_only", "workspace")
    assert raw["approved"] is None and raw["pending_approval"] is None
    assert not raw.get("task_grants")
    assert raw["operation_permissions"] == [{"basis": "read_only", "operation_id": "historical"}]
    assert raw["status"] == ("paused" if status == "waiting_approval" else status)


def test_t3_global_confirmation_default_does_not_interrupt_automatic_task(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.update_settings({"permission_mode": "automatic"})
    task = runtime.create({"message": "Share service"})
    started, release = threading.Event(), threading.Event()
    complete = model.complete
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return complete(*args, **kwargs)
    model.complete = blocked
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    try:
        assert started.wait(10)
        assert runtime.get(task["id"])["status"] == "running"
        runtime.update_settings({"permission_mode": "confirm_changes"})
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert runtime.get(task["id"])["control_revision"] == task["control_revision"]
    advance(runtime)
    assert len(tools.calls) == 1
    assert not runtime.get(task["id"])["pending_approval"]
    assert runtime.create({"message": "New task"})["permission_mode"] == "confirm_changes"


def test_t4_global_automatic_default_preserves_task_confirmation_and_approval(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.update_settings({"permission_mode": "confirm_changes"})
    task = runtime.create({"message": "Share service"})
    advance(runtime, 2)
    pending = runtime.get(task["id"])
    runtime.update_settings({"permission_mode": "automatic"})
    current = runtime.get(task["id"])
    assert current["status"] == "waiting_approval"
    assert current["pending_approval"] == pending["pending_approval"]
    assert tools.calls == []


def test_unchanged_desktop_flag_in_settings_form_preserves_existing_approval(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.update_settings({"permission_mode": "confirm_changes", "desktop_access": False})
    task = runtime.create({"message": "Share service"})
    advance(runtime, 2)
    pending = runtime.get(task["id"])
    # The settings dialog submits the stored desktop flag alongside the new default.
    runtime.update_settings({"permission_mode": "automatic", "desktop_access": False})
    current = runtime.get(task["id"])
    assert current["pending_approval"] == pending["pending_approval"]
    assert current["control_revision"] == pending["control_revision"]
    assert current["status"] == "waiting_approval"


def test_t5_explicit_read_only_sync_pauses_then_changes_each_live_task(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "automatic"})
    tasks = [runtime.create({"message": f"Task {index}"}) for index in range(2)]
    runtime.store.transaction(lambda data: data["tasks"][tasks[0]["id"]].update(status="running"))
    runtime.update_settings({"permission_mode": "read_only"})
    assert all(runtime.get(task["id"])["permission_mode"] == "automatic" for task in tasks)
    for task in tasks:
        runtime.action(task["id"], {"action": "pause"})
        changed = change(runtime, task["id"], "read_only")
        assert changed["permission_mode"] == "read_only"
        assert any(event["type"] == "permission_changed" and event["new_mode"] == "read_only" for event in changed["events"])


def test_t6_global_desktop_revocation_updates_live_rows_and_rejects_stale_context(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime.update_settings({"permission_mode": "workspace", "desktop_access": True, "workspace_root": str(workspace)})
    task = runtime.create({"message": "Desktop work"})
    context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
    local = LocalTools(SimpleNamespace(home=tmp_path), lambda: OperationLedger(tmp_path))
    local.store = runtime.store
    try:
        runtime.update_settings({"desktop_access": False})
        assert runtime.get(task["id"])["local_execution"]["desktop_access"] is False
        assert local.policy(context)["desktop_access"] is False
        with pytest.raises(AgentToolRejected, match="agent_desktop_access_required"):
            local.browser(context)
    finally:
        local.processes.close()


@pytest.mark.parametrize("mode,global_mode,expected", [
    ("automatic", "read_only", ("execute", "automatic")),
    ("workspace", "automatic", ("ask", "explicit_approval")),
    ("read_only", "automatic", ("deny", "read_only")),
])
def test_explicit_approval_and_desktop_decisions_use_task_mode_only(mode, global_mode, expected):
    row = {"permission_mode": mode, "local_execution": {"desktop_access": True}}
    settings = {"permission_mode": global_mode, "desktop_access": False}
    result = decide({"name": "external", "arguments": {}}, {"effect": "write", "requires_explicit_approval": True}, row, settings)
    assert (result.action, result.basis) == expected
    assert is_full_automatic(row, settings) is (mode == "automatic")
    desktop = decide({"name": "ryn_computer_action", "arguments": {}},
                     {"effect": "write", "computer_access": True, "workstation_permission_tool": "ryn_computer_action"}, row, settings)
    if mode != "read_only":
        assert desktop.action == "execute"


def test_default_model_change_uses_task_authority_despite_global_read_only(tmp_path):
    runtime, model, _, _, _ = default_model_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "automatic"})
    task = runtime.create({"message": "Set default model"})
    runtime.update_settings({"permission_mode": "read_only"})
    args = {"model_id": "local:other", "expected_settings_revision": runtime.configuration_snapshot()["settings_revision"]}
    model.responses.append(assistant(name=NAME, args=args))
    advance(runtime, 2)
    assert runtime.settings()["model_id"] == "local:other"
    assert runtime.get(task["id"])["permission_mode"] == "automatic"


def test_manual_permission_change_rejects_stale_revision_and_invalid_mode(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Work"})
    paused = runtime.action(task["id"], {"action": "pause"})
    with pytest.raises(AgentError, match="agent_control_revision_conflict"):
        runtime.action(task["id"], {"action": "set_permission", "permission_mode": "workspace", "expected_control_revision": task["control_revision"]})
    with pytest.raises(AgentError, match="agent_invalid_permission_mode"):
        change(runtime, task["id"], "unknown")
    assert runtime.get(task["id"])["control_revision"] == paused["control_revision"]


def test_native_computer_prepare_and_ready_keep_task_permission_after_default_change(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "automatic", "desktop_access": True})
    task = runtime.create({"message": "Desktop work"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(status="running"))
    context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
    native = NativeComputerFacade(runtime.store)
    # Window acquisition is the external boundary; both authority checks stay real.
    monkeypatch.setattr(native, "_require_observed_window_state", lambda *args, **kwargs:
                        SimpleNamespace(state_token="state", window_ref={"hwnd": "123"}))
    preview = {"window_ref": {"hwnd": "123"}, "expected_state_token": "state", "action_fingerprint": "action"}
    def capture():
        return native.capture_trusted_write_authority(task_id=task["id"], operation_id="op",
            tool_name="ryn_computer_action", arguments={"action": "focus"}, context=context,
            approved_preview=preview)
    stamp = capture()
    runtime.update_settings({"permission_mode": "read_only"})
    assert capture().permission_mode == "automatic"
    native.verify_ready_write_authority(stamp, validated_ready={"status": "ready", "mutation": "none", "inputLockHeld": True, "taskSessionId": task["id"],
        "operationId": "op", "expectedStateToken": "state", "actionFingerprint": "action",
        "window": {"hwnd": "123"}}, context=context, cancelled=lambda: False)


def test_cli_write_authority_uses_task_permission_after_default_change(tmp_path):
    runtime, _, tools, _, _ = default_model_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "automatic"})
    task = runtime.create({"message": "Configure CLI"})
    runtime.update_settings({"permission_mode": "read_only"})
    raw = runtime.store.read()["tasks"][task["id"]]
    context = {**runtime._execution_context(raw), "_cli_write": True}
    assert tools.cli_sources.check(context, lambda: False)["permission_mode"] == "automatic"


def test_linked_continuation_keeps_explicit_task_mode(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "read_only"})
    task = runtime.create({"message": "Existing work"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(status="completed"))
    change(runtime, task["id"], "workspace")
    continued = runtime._continue_in_new_task(task["id"], {}, "Continue the same work")
    assert continued["permission_mode"] == "workspace"
    assert runtime.settings()["permission_mode"] == "read_only"
