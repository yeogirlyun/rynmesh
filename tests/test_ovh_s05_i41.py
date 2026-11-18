"""S05-I41 task directory grants and automatic legacy workspace fallback."""
import copy
import uuid
from pathlib import Path

import pytest

from test_agent_local_tools import local, execute
from test_agent_runtime import make_runtime
from test_agent_projects import project_runtime
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
