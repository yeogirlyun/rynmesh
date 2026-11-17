"""S12-I24 partial patch evidence follows actual file and model paths."""
import json
from pathlib import Path

import pytest
from test_agent_apply_patch import patch
from test_agent_local_tools import local, execute
from test_agent_runtime import assistant, advance, approve
from test_agent_default_model import setup as runtime_setup
from agent_tool_fixtures import expose_on_create
from rynmesh.agent_runtime import file_review
from rynmesh.agent_runtime.file_review import FileReview
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore

NOTE = "Files listed were written; re-read them before retrying."


def edit_patch(names):
    return patch(*(line for name in names for line in [f"*** Update File: {name}", "@@", "-old", "+new"]))


def fail_replace(monkeypatch, name, error_type=PermissionError):
    replace = FileReview.replace
    def failing(self, step, *args, **kwargs):
        if Path(step["path"]).name == name:
            raise error_type("PRIVATE_REPLACE_DIAGNOSTIC")
        return replace(self, step, *args, **kwargs)
    monkeypatch.setattr(FileReview, "replace", failing)


@pytest.mark.parametrize("overflow", [False, True])
def test_t1_second_file_failure_reports_only_this_call_including_overflow_history(local, monkeypatch, overflow):
    tools, context, workspace, _ = local
    execute(tools, context, "ryn_file_write", {"path": "prior.txt", "content": "prior\n"}, "prior")
    if overflow:
        monkeypatch.setattr(file_review, "MAX_OPERATIONS", 1)
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text("old\n", encoding="utf-8")
    fail_replace(monkeypatch, "b.txt")
    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed") as error:
        execute(tools, context, "ryn_apply_patch", {"patch": edit_patch(["a.txt", "b.txt", "c.txt"])}, "partial")
    assert error.value.details == {"applied_paths": ["a.txt"], "failed_path": "b.txt", "note": NOTE}
    assert not isinstance(error.value, AgentToolRejected)
    assert (workspace / "a.txt").read_text() == "new\n"
    assert (workspace / "b.txt").read_text() == (workspace / "c.txt").read_text() == "old\n"
    assert "prior.txt" not in json.dumps(error.value.details) and "PRIVATE_REPLACE" not in json.dumps(error.value.details)
    assert tools.ledger_factory().get("partial")["status"] == "dispatched"


@pytest.mark.parametrize("error_type", [PermissionError, AgentToolRejected])
def test_t2_single_file_failure_has_empty_applied_list_and_uncertain_classification(local, monkeypatch, error_type):
    tools, context, workspace, _ = local
    (workspace / "a.txt").write_text("old\n", encoding="utf-8")
    fail_replace(monkeypatch, "a.txt", error_type)
    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed") as error:
        execute(tools, context, "ryn_apply_patch", {"patch": edit_patch(["a.txt"])}, "single")
    assert error.value.details == {"applied_paths": [], "failed_path": "a.txt", "note": NOTE}
    assert not isinstance(error.value, AgentToolRejected) and (workspace / "a.txt").read_text() == "old\n"


def test_t3_hunk_mismatch_remains_rejected_without_partial_write_evidence(local):
    tools, context, workspace, _ = local
    (workspace / "a.txt").write_text("old\n", encoding="utf-8")
    args = {"patch": patch("*** Add File: new.txt", "+new", "*** Update File: a.txt", "@@", "-missing", "+changed")}
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found") as error:
        execute(tools, context, "ryn_apply_patch", args, "mismatch")
    assert "applied_paths" not in (error.value.details or {}) and not hasattr(error.value, "applied_paths")
    assert not (workspace / "new.txt").exists() and (workspace / "a.txt").read_text() == "old\n"


@pytest.mark.parametrize("mode", ["automatic", "confirm_changes"])
def test_t1_model_receives_structured_partial_paths_and_no_automatic_rollback(tmp_path, monkeypatch, mode):
    runtime, model, tools, key, _ = runtime_setup(tmp_path)
    tools.local.store = runtime.store
    runtime.update_settings({"permission_mode": mode})
    expose_on_create(runtime, ["ryn_apply_patch"])
    model.responses.extend([assistant(name="ryn_apply_patch", args={"patch": edit_patch(["a.txt", "b.txt", "c.txt"])}),
        assistant("The first file changed. Re-read before retrying.")])
    created = runtime.create({"message": "Update these three files."})
    workspace = Path(created["local_execution"]["workspace_root"])
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text("old\n", encoding="utf-8")
    fail_replace(monkeypatch, "b.txt")
    try:
        if mode == "confirm_changes":
            advance(runtime, 2)
            approve(runtime, created["id"])
            advance(runtime)
            assert runtime.get(created["id"])["status"] == "interrupted"
            runtime = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
            tools.app.state.agent_runtime = runtime
            tools.local.store = runtime.store
            runtime.reconcile()
            advance(runtime)  # Run the scheduled read-only reconciliation before owner continuation.
            current = runtime.get(created["id"])
            runtime.add_message(created["id"], {"message": "Continue without retrying the patch.", "intent": "information",
                "expected_control_revision": current["control_revision"], "expected_goal_revision": current["goal"]["revision"]})
            advance(runtime)
        else:
            advance(runtime, 3)
        assert len(model.calls) == 2
        result = next(json.loads(m["content"]) for m in model.calls[1][1] if m["role"] == "tool")
        assert result["details"] == {"applied_paths": ["a.txt"], "failed_path": "b.txt", "note": NOTE}
        assert result["not_executed"] is False and result["outcome"] == "unknown"
        assert (workspace / "a.txt").read_text() == "new\n" and (workspace / "b.txt").read_text() == "old\n"
    finally:
        tools.local.close()


def test_applied_paths_are_limited_to_first_twenty_without_rolling_back_extra_writes(local, monkeypatch):
    tools, context, workspace, _ = local
    names = [f"file-{index:02d}.txt" for index in range(23)]
    for name in names:
        (workspace / name).write_text("old\n", encoding="utf-8")
    fail_replace(monkeypatch, names[-1])
    with pytest.raises(AgentError) as error:
        execute(tools, context, "ryn_apply_patch", {"patch": edit_patch(names)}, "many")
    assert error.value.details == {"applied_paths": names[:20], "failed_path": names[-1], "note": NOTE}
    assert all((workspace / name).read_text() == "new\n" for name in names[:-1])
