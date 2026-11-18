"""S05-I41 task directory grants and automatic legacy workspace fallback."""
import copy
import json
import uuid
from pathlib import Path

import pytest

from test_agent_local_tools import local, execute
from test_agent_runtime import make_runtime
from test_agent_projects import project_runtime
from test_agent_integrations import node, add_source, fake_upstream, call
from rynmesh.agent_runtime import automations, automation_worker
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box
from test_agent_resource_writes import writable, execute as resource_execute
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def legacy(store, task_id, mode="automatic"):
    def change(data):
        data["settings"]["permission_mode"] = mode
        data["settings"]["workspace_root"] = ""
        row = data["tasks"][task_id]
        row["permission_mode"] = mode
        row["local_execution"]["workspace_root"] = ""
    store.transaction(change)


def test_t1_automatic_unbound_file_write_uses_default_without_mutating_binding(local):
    tools, context, _, store = local
    legacy(store, "task")
    context = {**context, "local_execution": {**context["local_execution"], "workspace_root": ""}}
    before = copy.deepcopy(store.read()["tasks"]["task"]["local_execution"])
    root = Path(store.default_workspace())
    result = execute(tools, context, "ryn_file_write", {"path": "output.txt", "content": "automatic output"})
    assert result["written"] and (root / "output.txt").read_text() == "automatic output"
    assert tools.file_review("task", str(root)).load()["root"] == str(root)
    assert store.read()["tasks"]["task"]["local_execution"] == before


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes", "read_only"])
def test_t2_manual_modes_keep_the_unbound_workspace_error(local, mode):
    tools, context, _, store = local
    legacy(store, "task", mode)
    context["local_execution"]["workspace_root"] = ""
    with pytest.raises(AgentToolRejected, match="agent_workspace_changed_new_task_required"):
        tools.policy(context)


def test_t3_new_tasks_use_distinct_explicit_directories(local, tmp_path):
    tools, _, global_root, store = local
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    runtime.store = store
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    for i, root in enumerate(roots):
        task = runtime.create({"message": "Write in this folder", "workspace_root": str(root)})
        assert task["local_execution"]["workspace_source"] == "task"
        context = {"task_id": task["id"], "local_execution": task["local_execution"]}
        assert tools.policy(context)["workspace_root"] == str(root.resolve())
        execute(tools, context, "ryn_file_write", {"path": "result.txt", "content": str(i)}, f"write-{i}")
        assert (root / "result.txt").read_text() == str(i)
        assert tools.file_review(task["id"], str(root)).load()["root"] == str(root)
    assert store.read()["settings"]["workspace_root"] == str(global_root)


def test_t4_binding_a_different_task_directory_keeps_global_setting(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    global_root, task_root = tmp_path / "global", tmp_path / "task"
    global_root.mkdir(); task_root.mkdir()
    runtime.update_settings({"workspace_root": str(global_root)})
    created = runtime.create({"message": "Continue legacy task"})
    def unbind(data):
        row = data["tasks"][created["id"]]
        row.update(status="failed", error="agent_workspace_changed_new_task_required")
        row["local_execution"]["workspace_root"] = ""
    runtime.store.transaction(unbind)
    result = runtime.bind_workspace(created["id"], {"workspace_root": str(task_root),
        "expected_control_revision": runtime.get(created["id"])["control_revision"], "client_request_id": str(uuid.uuid4())})
    assert result["local_execution"]["workspace_root"] == str(task_root.resolve())
    assert result["local_execution"]["workspace_source"] == "task" and result["status"] == "queued"
    assert runtime.store.read()["settings"]["workspace_root"] == str(global_root)


def test_t5_task_bound_root_survives_global_changes_but_legacy_binding_does_not(local, tmp_path):
    tools, context, workspace, store = local
    other = tmp_path / "other"; other.mkdir()
    store.transaction(lambda data: data["tasks"]["task"]["local_execution"].update(workspace_source="task"))
    context["local_execution"]["workspace_source"] = "task"
    store.transaction(lambda data: data["settings"].update(workspace_root=str(other)))
    assert tools.policy(context)["workspace_root"] == str(workspace)
    execute(tools, context, "ryn_file_write", {"path": "stable.txt", "content": "stable"})
    assert (workspace / "stable.txt").read_text() == "stable" and not (other / "stable.txt").exists()
    context["local_execution"].pop("workspace_source")
    with pytest.raises(AgentToolRejected, match="agent_workspace_changed_new_task_required"):
        tools.policy(context)


def test_t3_project_root_takes_precedence_and_default_creation_keeps_legacy_source(tmp_path):
    runtime, _, _, _ = project_runtime(tmp_path / "runtime")
    project_root, requested = tmp_path / "project", tmp_path / "requested"
    project_root.mkdir(); requested.mkdir()
    project = runtime.load_project({"path": str(project_root)})
    task = runtime.create({"message": "Project task", "project_id": project["id"], "workspace_root": str(requested)})
    assert task["local_execution"]["workspace_root"] == str(project_root.resolve())
    assert task["local_execution"]["project_id"] == project["id"]
    assert task["local_execution"].get("workspace_source") != "task"
    plain = runtime.create({"message": "Default task"})
    assert "workspace_source" not in plain["local_execution"]


@pytest.mark.parametrize("value", ["", None, 12, "missing-directory"])
def test_t3_explicit_invalid_workspace_is_rejected(tmp_path, value):
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    if value == "missing-directory":
        value = str(tmp_path / value)
    with pytest.raises(AgentError, match="agent_invalid_workspace|agent_workspace_not_directory"):
        runtime.create({"message": "Invalid folder", "workspace_root": value})


def test_t1_resource_upload_uses_automatic_runtime_workspace_fallback(writable, tmp_path):
    tools, grants, args, _, _, _, dav_root, _, _ = writable
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    runtime.update_settings({"permission_mode": "automatic"})
    task = runtime.create({"message": "Upload generated report"})
    legacy(runtime.store, task["id"])
    root = Path(runtime.store.default_workspace())
    (root / "source.md").write_text("generated report", encoding="utf-8")
    row = runtime.store.read()["tasks"][task["id"]]
    context = {**runtime._execution_context(row), **grants}
    result = resource_execute(tools, context, {"directory_ref": args["directory_ref"], "name": args["name"],
        "workspace_path": "source.md"}, name="ryn_upload_resource_file")
    assert result and (dav_root / "allowed/report.md").read_text() == "generated report"
    assert runtime.store.read()["tasks"][task["id"]]["local_execution"]["workspace_root"] == ""


def test_t1_native_computer_already_uses_the_same_policy_fallback(local):
    tools, context, _, store = local
    legacy(store, "task")
    context["local_execution"]["workspace_root"] = ""
    assert tools.native_computer._resolve_workspace_root(context) == Path(store.default_workspace())


def test_t1_automatic_fallback_passes_live_authority_checks(local):
    tools, context, original_root, store = local
    legacy(store, "task")
    context["local_execution"]["workspace_root"] = ""
    check = tools.allowed(context)
    assert check()
    store.transaction(lambda data: data["settings"].update(workspace_root=str(original_root)))
    assert not check()


def test_t5_task_directory_can_continue_after_restart_with_different_global_root(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    task = runtime.create({"message": "Continue after restart", "workspace_root": str(first)})
    runtime.update_settings({"workspace_root": str(second)})
    data = runtime.store.read()
    assert runtime._may_continue_after_restart(data["tasks"][task["id"]], data)


@pytest.mark.parametrize("tool", ["ryn_file_write", "ryn_shell_execute"])
def test_rework_automatic_legacy_write_runs_through_actual_loop(node, monkeypatch, tool):
    app, owner = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner, owner.home)
    runtime = AgentRuntime(AgentStore(owner.home / "legacy-agent", peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "permission_mode": "automatic", "verify_text_responses": False})
    args = ({"path": "loop.txt", "content": "actual loop output"} if tool == "ryn_file_write" else
            {"command": "Set-Content -LiteralPath loop.txt -Value 'actual loop output'", "shell": "powershell", "timeout_seconds": 30})
    fake_upstream(monkeypatch, [call("ryn_discover_tools", json.dumps({"load": [tool]}), "discover"),
        call(tool, json.dumps(args), "write"), {"role": "assistant", "content": "Output created."}])
    created = runtime.create({"message": "Write loop.txt"})
    legacy(runtime.store, created["id"])
    for _ in range(12):
        runtime.run_once()
        current = runtime.get(created["id"])
        if current["status"] in {"completed", "failed", "waiting_approval"}:
            break
    try:
        target = Path(runtime.store.default_workspace()) / "loop.txt"
        assert target.is_file(), current
        assert target.read_text(encoding="utf-8-sig").strip() == "actual loop output"
        assert current["status"] == "completed", current.get("error")
        assert not any("agent_invalid_operation_classification" in m.get("content", "") for m in current["messages"])
    finally:
        tools.local.close()


def test_rework_change_review_uses_effective_legacy_root(local, tmp_path):
    tools, context, _, store = local
    legacy(store, "task")
    context["local_execution"]["workspace_root"] = ""
    execute(tools, context, "ryn_file_write", {"path": "code.py", "content": "print('review me')"})
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    runtime.store, runtime.tools = store, tools
    row = store.read()["tasks"]["task"]
    row["receipts"] = [{"effect": "write", "status": "confirmed", "goal_revision": row["goal"]["revision"], "tool": "ryn_file_write"}]
    assert runtime._change_review(row) is not None


def test_rework_rebinding_after_fallback_write_isolates_undo_records(local, tmp_path):
    tools, context, _, store = local
    legacy(store, "task")
    context["local_execution"]["workspace_root"] = ""
    old_root = Path(store.default_workspace())
    execute(tools, context, "ryn_file_write", {"path": "old.txt", "content": "old"}, "old-write")
    old_review = tools.file_review("task", str(old_root))
    old_plan = old_review.preview()
    runtime, _, _, _ = make_runtime(tmp_path / "runtime")
    runtime.store, runtime.tools = store, tools
    store.transaction(lambda data: data["tasks"]["task"].update(status="failed"))
    new_root = tmp_path / "new-root"; new_root.mkdir()
    bound = runtime.bind_workspace("task", {"workspace_root": str(new_root), "expected_control_revision": runtime.get("task")["control_revision"], "client_request_id": str(uuid.uuid4())})
    new_context = {"task_id": "task", "local_execution": bound["local_execution"]}
    execute(tools, new_context, "ryn_file_write", {"path": "new.txt", "content": "new"}, "new-write")
    new_review = tools.file_review("task", str(new_root))
    assert not any(r["id"] == "old-write" for r in new_review.load()["records"])
    assert old_review.load()["records"]
    with pytest.raises(AgentError):
        new_review.undo(None, old_plan["token"], "old-token")
    plan = new_review.preview()
    new_review.undo(None, plan["token"], "new-token")
    assert not (new_root / "new.txt").exists() and (old_root / "old.txt").read_text() == "old"


@pytest.mark.parametrize("explicit", [False, True])
def test_rework_automation_only_inherits_owner_task_workspace_authority(local, tmp_path, explicit):
    tools, _, _, store = local
    runtime, _, _, _ = project_runtime(tmp_path / "runtime")
    runtime.store = store
    root = tmp_path / "owner-root"; root.mkdir()
    task = runtime.create({"message": "Schedule work", "workspace_root": str(root)})
    args = {"action": "create", "name": "Scheduled task", "prompt": "Write report", "kind": "cron", "repeat": "daily", "time": "22:00"}
    if explicit:
        args["workspace_root"] = str(root)
    def create(data):
        return automations.execute_automation_tool_call_in_tx(store, data, data["tasks"][task["id"]], args)
    saved = store.transaction(create)
    automation_id = saved["automation"]["id"]
    store.transaction(lambda data: automations.sync_automations_from_disk_in_tx(store, data))
    triggered = store.transaction(lambda data: automation_worker.trigger_automation_in_tx(runtime, data, automation_id, force=True))
    spawned = store.read()["tasks"][triggered["task_id"]]
    context = {"task_id": spawned["id"], "local_execution": spawned["local_execution"]}
    if explicit:
        assert spawned["local_execution"].get("workspace_source") != "task"
        with pytest.raises(AgentToolRejected, match="agent_workspace_changed_new_task_required"):
            tools.policy(context)
    else:
        assert spawned["local_execution"]["workspace_source"] == "task"
        assert tools.policy(context)["workspace_root"] == str(root)
    # Updating with an explicit path must revoke any previously inherited flag.
    if not explicit:
        store.transaction(lambda data: automations.execute_automation_tool_call_in_tx(store, data, data["tasks"][task["id"]],
            {"action": "update", "id": automation_id, "workspace_root": str(root)}))
        assert store.read()["automations"][automation_id].get("workspace_source") != "task"
