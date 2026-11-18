"""S02-I6: real owner answers at active and idle worker boundaries."""
import copy
import json
import threading
import uuid

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime.prompt import INPUT, INTERNAL_SPECS
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore

ASSUMPTION = "No answer was given: continue with your best stated assumption; do not ask the same question again."


def question(*, blocking=False, kind="information", required=False):
    return {"goal_revision": 1, "kind": kind, "prompt": "Which format?", "blocking": blocking,
            "fields": [{"id": "format", "label": "Format", "type": "credential_ref" if kind == "credential_ref" else "text", "required": required}]}


def setup(tmp_path, *, blocking=False, kind="information", required=False, with_read=True):
    first = assistant(name="ryn_request_input", args=question(blocking=blocking, kind=kind, required=required))
    if with_read:
        first["message"]["tool_calls"].append(assistant(name="inspect_service", args={"service_id": "A"}, call_id="independent_read")["message"]["tool_calls"][0])
    if with_read == "two":
        first["message"]["tool_calls"].append(assistant(name="inspect_service", args={"service_id": "B"}, call_id="second_read")["message"]["tool_calls"][0])
    runtime, model, tools, key = make_runtime(tmp_path, first, assistant("Independent work finished."), assistant("Used the answer."))
    task = runtime.create({"message": "Inspect A; use a sensible format while asking."})
    advance(runtime, 2)
    return runtime, model, tools, key, task["id"]


def get_question(runtime, task_id):
    row = runtime.get(task_id)
    return row["pending_input"] or row["open_questions"][0]


def answer(runtime, task_id, answers, q=None, **extra):
    row = runtime.get(task_id)
    q = q or get_question(runtime, task_id)
    return runtime.action(task_id, {"action": "answer", "pending_input_id": q["id"],
        "expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"],
        "answers": answers, "client_request_id": str(uuid.uuid4()), **extra})


def test_T1_nonblocking_keeps_sibling_read_and_public_question(tmp_path):
    runtime, _, tools, _, task_id = setup(tmp_path)
    row = runtime.get(task_id)
    assert row["status"] == "queued" and row["pending_input"] is None
    assert len(row["open_questions"]) == 1 and row["open_questions"][0]["blocking"] is False
    assert row["allowed_actions"]["answer"]["enabled"] is True
    event = next(e for e in row["events"] if e["type"] == "input_requested")
    assert event["blocking"] is False
    result = json.loads(row["messages"][-1]["content"])
    assert result["accepted"] is True and result["waiting_for_owner"] is False
    advance(runtime)
    assert [call[0] for call in tools.calls] == ["inspect_service"]
    assert not any(e.get("result", {}).get("error") == "agent_waiting_for_input" for e in runtime.get(task_id)["events"])


def test_T2_answer_during_model_preserves_work_then_reaches_next_request(tmp_path):
    runtime, model, _, _, task_id = setup(tmp_path, with_read=False)
    q = get_question(runtime, task_id)
    entered, release = threading.Event(), threading.Event()
    calls, cancelled_states, errors = [], [], []

    def complete(model_id, messages, specs, cancelled, on_event):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            entered.set()
            assert release.wait(30)
            cancelled_states.append(cancelled())
            return assistant("Work finished before the answer arrived.")
        return assistant("Used the answer.")

    model.complete = complete
    def run():
        try:
            runtime.run_once()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(30)
        before = runtime.get(task_id)
        row = answer(runtime, task_id, {"format": "markdown"}, q)
        assert row["control_revision"] == before["control_revision"]
        assert row["status"] == "running" and row["open_questions"] == []
        assert row["run_state"] == "running"
        assert row["pending_user_messages"][-1]["ui"]["kind"] == "input_answer"
        saved = runtime.store.read()["tasks"][task_id]
        assert saved["pending_control"] == {"kind": "information"}
    finally:
        release.set()
        worker.join(30)
    assert not worker.is_alive() and errors == [] and cancelled_states == [False]
    row = runtime.get(task_id)
    assert row["status"] == "queued"
    assert any(m.get("content") == "Work finished before the answer arrived." for m in row["messages"])
    advance(runtime)
    assert "markdown" in json.dumps(calls[1])


def test_T3_idle_answer_starts_another_round(tmp_path):
    runtime, model, _, _, task_id = setup(tmp_path)
    advance(runtime, 2)
    assert runtime.get(task_id)["status"] == "completed"
    row = answer(runtime, task_id, {"format": "plain text"})
    assert row["status"] == "queued" and row["steps"] == 0 and row["open_questions"] == []
    assert row["messages"][-1]["ui"]["kind"] == "input_answer"
    advance(runtime)
    assert "plain text" in json.dumps(model.calls[-1][1])
    assert runtime.get(task_id)["result"] == "Used the answer."


@pytest.mark.parametrize("blocking", [False, True])
def test_T4_empty_answer_uses_assumption_sentence(tmp_path, blocking):
    runtime, _, _, _, task_id = setup(tmp_path, blocking=blocking, with_read=False)
    row = answer(runtime, task_id, {})
    assert row["messages"][-1]["content"] == ASSUMPTION
    assert row["messages"][-1]["ui"]["kind"] == "input_answer"


@pytest.mark.parametrize("blocking", [False, True])
def test_T5_subagent_still_cannot_ask_owner(tmp_path, blocking):
    runtime, model, _, _ = make_runtime(tmp_path, assistant(name="ryn_request_input", args=question(blocking=blocking)), assistant("Reported to parent."))
    row = runtime.create({"message": "Child work"})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(parent_task_id="parent"))
    advance(runtime, 2)
    result = runtime.get(row["id"])
    assert any(e.get("result", {}).get("error") == "agent_subagent_input_blocked" for e in result["events"])
    assert result.get("open_questions", []) == [] and result["pending_input"] is None


def test_credentials_stay_blocking_even_when_false_is_requested(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, kind="credential_ref")
    row = runtime.get(task_id)
    assert row["status"] == "waiting_input" and row["pending_input"]["kind"] == "credential_ref"
    assert row["open_questions"] == [] and row["allowed_actions"]["answer"]["enabled"] is False


def test_default_schema_is_blocking_and_explains_early_questions():
    assert INPUT["properties"]["blocking"] == {"type": "boolean", "default": True}
    description = next(s["function"]["description"] for s in INTERNAL_SPECS if s["function"]["name"] == "ryn_request_input")
    assert "blocking=false" in description and "credential_ref" in description


def test_four_question_cap_and_answer_frees_a_slot(tmp_path):
    responses = [assistant(name="ryn_request_input", args=question(), call_id=f"q{i}") for i in range(6)]
    runtime, model, _, _ = make_runtime(tmp_path, *responses)
    task_id = runtime.create({"message": "Ask useful questions while continuing."})["id"]
    advance(runtime, 10)
    row = runtime.get(task_id)
    assert len(row["open_questions"]) == 4
    assert any(e.get("result", {}).get("error") == "agent_open_question_limit" for e in row["events"])
    answer(runtime, task_id, {"format": "markdown"})
    advance(runtime, 2)
    assert len(runtime.get(task_id)["open_questions"]) == 4


def test_old_tasks_default_empty_and_questions_survive_restart(tmp_path):
    runtime, model, tools, key, task_id = setup(tmp_path)
    original = runtime.get(task_id)["open_questions"]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    assert restarted.get(task_id)["open_questions"] == original
    runtime.store.transaction(lambda data: data["tasks"][task_id].pop("open_questions"))
    assert runtime.get(task_id)["open_questions"] == []


def test_answer_checks_id_goal_and_replay(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path)
    q = get_question(runtime, task_id)
    with pytest.raises(AgentError, match="agent_input_mismatch"):
        answer(runtime, task_id, {}, {**q, "id": "unknown"})
    with pytest.raises(AgentError, match="agent_goal_revision_conflict"):
        answer(runtime, task_id, {}, q, expected_goal_revision=2)
    row = runtime.get(task_id)
    body = {"action": "answer", "pending_input_id": q["id"], "answers": {}, "expected_goal_revision": 1,
            "expected_control_revision": row["control_revision"], "client_request_id": str(uuid.uuid4())}
    first = runtime.action(task_id, body)
    assert runtime.action(task_id, body) == first


def test_running_tool_answer_keeps_siblings_and_pairs_results_before_user_message(tmp_path):
    runtime, model, tools, _, task_id = setup(tmp_path, with_read="two")
    entered, release = threading.Event(), threading.Event()
    original = tools.execute
    cancellation = []
    def execute(name, args, *, cancelled, **kwargs):
        entered.set()
        assert release.wait(30)
        cancellation.append(cancelled())
        return original(name, args, cancelled=cancelled, **kwargs)
    tools.execute = execute
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    try:
        assert entered.wait(30)
        answer(runtime, task_id, {"format": "markdown"})
    finally:
        release.set()
        worker.join(30)
    assert not worker.is_alive() and cancellation and not any(cancellation)
    advance(runtime, 3)
    row = runtime.get(task_id)
    assert {call[1]["service_id"] for call in tools.calls} == {"A", "B"}
    answer_index = next(i for i,m in enumerate(row["messages"]) if m.get("ui", {}).get("kind") == "input_answer")
    last_tool = max(i for i,m in enumerate(row["messages"]) if m["role"] == "tool")
    assert answer_index > last_tool
    assert "markdown" in json.dumps(model.calls[-1][1])


def test_queued_answer_waits_until_sibling_results_are_paired(tmp_path):
    runtime, model, _, _, task_id = setup(tmp_path)
    answer(runtime, task_id, {"format": "markdown"})
    advance(runtime, 2)
    row = runtime.get(task_id)
    answer_index = next(i for i,m in enumerate(row["messages"]) if m.get("ui", {}).get("kind") == "input_answer")
    result_index = next(i for i,m in enumerate(row["messages"]) if m.get("tool_call_id") == "independent_read")
    assert answer_index > result_index


def test_new_goal_retires_old_open_questions(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path)
    row = runtime.get(task_id)
    changed = runtime.add_message(task_id, {"message": "Use another goal", "intent": "steer",
        "expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"]})
    assert changed["open_questions"] == []
