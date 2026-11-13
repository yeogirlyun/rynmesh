"""Automatic continuation retains the same work's authority and accounting."""
import uuid

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.store import AgentError


def overflow(runtime, tid):
    runtime.store.transaction(lambda data: data["tasks"][tid]["messages"].extend(
        [{"role": "assistant", "content": "x" * 200_000, "context_epoch": 0} for _ in range(11)]))


def follow(runtime, tid, message, **extra):
    current = runtime.get(tid)
    return runtime.add_message(tid, {"message": message, "intent": "information",
        "expected_control_revision": current["control_revision"],
        "expected_goal_revision": current["goal"]["revision"], **extra})


def test_three_continuations_keep_hard_budget_settings_and_restart(tmp_path):
    runtime, _, _, key = make_runtime(tmp_path, *[assistant("Done.") for _ in range(4)])
    runtime.update_settings({"permission_mode": "read_only"})
    row = runtime.create({"message": "Work on the same report.", "response_language": "zh-CN",
                          "budget_limits": {"max_total_model_calls": 4}, "skill_ids": ["spreadsheet-work"]})
    for index in range(3):
        advance(runtime)
        before = runtime.store.read()["tasks"][row["id"]]
        runtime.update_settings({"permission_mode": "automatic"})
        overflow(runtime, row["id"])
        child = follow(runtime, row["id"], "Continue the report.")
        raw = runtime.store.read()["tasks"][child["id"]]
        assert raw["permission_mode"] == "read_only" and raw["response_language"] == "zh-CN"
        assert raw["local_execution"] == before["local_execution"]
        assert raw["skills"] == before["skills"]
        assert raw["budget"]["limits"]["max_total_model_calls"] == 4
        assert budget.public(raw)["lifetime"]["charged_model_calls"] == index + 1
        assert raw["budget"]["limit_sources"]["max_total_model_calls"] == "owner"
        row = child
    advance(runtime)
    overflow(runtime, row["id"])
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        follow(runtime, row["id"], "More work.")
    assert len(runtime.store.read()["tasks"]) == 4
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    restarted = AgentRuntime(AgentStore(tmp_path, key), runtime.model, runtime.tools, runtime.catalog)
    assert restarted.get(row["id"])["budget"]["lifetime"]["charged_model_calls"] == 4


def test_continuation_creation_and_links_are_one_transaction_and_retry_is_idempotent(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    row = runtime.create({"message": "Start."})
    advance(runtime)
    overflow(runtime, row["id"])
    import rynmesh.agent_runtime.runtime as module
    stamp = module.memory_natural.stamp_task_owner_messages_in_tx
    def fail(*args):
        raise OSError("synthetic commit failure")
    monkeypatch.setattr(module.memory_natural, "stamp_task_owner_messages_in_tx", fail)
    request = {"message": "Continue.", "client_request_id": str(uuid.uuid4())}
    with pytest.raises(OSError, match="synthetic commit failure"):
        follow(runtime, row["id"], **request)
    data = runtime.store.read()
    assert len(data["tasks"]) == 1 and not data["tasks"][row["id"]].get("continued_to")
    monkeypatch.setattr(module.memory_natural, "stamp_task_owner_messages_in_tx", stamp)
    child = follow(runtime, row["id"], **request)
    assert follow(runtime, row["id"], **request)["id"] == child["id"]
    assert len(runtime.store.read()["tasks"]) == 2
    assert runtime.get(row["id"])["continued_to"] == child["id"]


def test_uncertain_write_cannot_be_escaped_by_continuing(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    row = runtime.create({"message": "Start."})
    advance(runtime)
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(
        status="interrupted", error="agent_write_outcome_unknown", in_flight={"effect": "write", "dispatched": True}))
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        runtime._continue_in_new_task(row["id"], {"message": "Continue."}, "Continue.")
    assert len(runtime.store.read()["tasks"]) == 1


def test_superseded_source_cannot_run_edit_or_reset_its_copied_budget(tmp_path):
    from rynmesh.agent_runtime import model_switch
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    row = runtime.create({"message": "Start."})
    advance(runtime)
    overflow(runtime, row["id"])
    follow(runtime, row["id"], "Continue.")
    current = runtime.get(row["id"])
    assert not model_switch.available(runtime.store.read()["tasks"][row["id"]])
    assert not any(current["allowed_actions"][key]["enabled"] for key in ("resume", "budget", "model", "edit_message"))
    with pytest.raises(AgentError, match="agent_task_continued"):
        runtime.action(row["id"], {"action": "resume", "expected_control_revision": current["control_revision"]})
    with pytest.raises(AgentError, match="agent_invalid_transition"):
        runtime.update_budget(row["id"], {"limits": {}, "expected_control_revision": current["control_revision"]})


def test_continuation_retains_origin_checks_and_failed_validation_is_atomic(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    row = runtime.create({"message": "Start."})
    advance(runtime)
    origin = {"grant_id": "source-grant", "scope_revision": 0, "resource_ref": "task-bound-reference"}
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["messages"].append(
        {"role": "tool", "content": "source data", "context_epoch": 0, "resource_origin": origin}))
    overflow(runtime, row["id"])
    seen = []
    def validate(candidate, **kwargs):
        context = runtime._execution_context(candidate, cheap_only=True)
        seen.append(context)
        raise AgentError("agent_resource_origin_invalid")
    monkeypatch.setattr(runtime, "_validate_context", validate)
    with pytest.raises(AgentError, match="agent_resource_origin_invalid"):
        runtime._continue_in_new_task(row["id"], {"message": "Continue."}, "Continue.")
    assert seen[-1]["resource_origins"] == [origin]
    state = runtime.store.read()
    assert len(state["tasks"]) == 1 and not state["tasks"][row["id"]].get("continued_to")


def test_summary_does_not_resurrect_assistant_data_from_a_previous_scope():
    from rynmesh.agent_runtime.event_compaction import continuation_summary
    summary = continuation_summary({"context_epoch": 1, "messages": [
        {"role": "user", "content": "Owner request"},
        {"role": "assistant", "content": "OLD_SCOPE_DATA", "context_epoch": 0},
        {"role": "assistant", "content": "Current answer", "context_epoch": 1}]})
    assert "OLD_SCOPE_DATA" not in summary and "Current answer" in summary
