"""Real encrypted state/worker paths for stopping and editing chat turns."""
from __future__ import annotations

import copy
import json
import shutil
import threading
import uuid

import pytest
from test_agent_runtime import Catalog, Model, advance, approve, assistant, make_runtime
from test_agent_runtime_model_switch import body as model_switch_body
from test_agent_runtime_model_switch import scope_fixture
from test_agent_runtime_model_switch import setup as model_switch_setup

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def send(runtime, task_id, message, **extra):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": message, "expected_control_revision": row["control_revision"], **extra})


def test_long_messages_survive_create_continue_edit_and_restart(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path,
        assistant("Created."), assistant("Continued."), assistant("Edited."))
    runtime.catalog.list_models = lambda: [{**Catalog().list_models()[0],
        "context_window": 262144, "context_window_known": True, "max_output_tokens": 4096}]
    original = "第一季度销售说明\n" * 2000 + "CREATE_END"
    continued = "第二季度销售说明 😀\n" * 2000 + "CONTINUE_END"
    replacement = "第三季度销售说明\n" * 2000 + "EDIT_END"
    row = runtime.create({"message": original})
    assert row["messages"][0]["content"] == original
    advance(runtime)
    assert any(m["content"] == original for m in model.calls[-1][1] if m["role"] == "user")
    row = send(runtime, row["id"], continued)
    assert row["messages"][-1]["content"] == continued
    advance(runtime)
    assert any(m["content"] == continued for m in model.calls[-1][1] if m["role"] == "user")
    row = send(runtime, row["id"], replacement, edit_message_index=2)
    assert [m["content"] for m in row["messages"]] == [original, "Created.", replacement]
    advance(runtime)
    assert any(m["content"] == replacement for m in model.calls[-1][1] if m["role"] == "user")
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    row = restarted.get(row["id"])
    assert row["messages"][0]["content"] == original
    assert row["messages"][2]["content"] == replacement
    assert row["result"] == "Edited." and tools.calls == []


@pytest.mark.parametrize("value", [None, "", "   \n", 123, {}, []])
def test_message_text_still_rejects_empty_and_non_string_values(value):
    from rynmesh.agent_runtime.runtime import message_text

    with pytest.raises(AgentError, match="agent_invalid_message"):
        message_text(value)


def test_stop_closes_pending_proposals_and_accepts_the_next_message(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "old"}), assistant("New turn completed."))
    row = runtime.create({"message": "Change the old service."})
    advance(runtime, 2)
    assert runtime.get(row["id"])["status"] == "waiting_approval"
    stopped = runtime.action(row["id"], {"action": "stop"})
    assert stopped["status"] == "paused" and stopped["pending_approval"] is None
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["queue"] == [] and saved["in_flight"] is None
    assert tools.calls == []
    next_turn = send(runtime, row["id"], "Leave that service alone and answer this turn.",
                     intent="information", expected_goal_revision=stopped["goal"]["revision"])
    assert next_turn["status"] == "queued" and next_turn["steps"] == 0
    assert next_turn["goal"]["revision"] == stopped["goal"]["revision"] + 1
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "New turn completed."
    assert tools.calls == [] and len(model.calls) == 2


def test_stop_cancels_live_inference_preserves_partial_and_can_continue(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    entered, stopped = threading.Event(), threading.Event()
    failures, requests = [], []

    def inference(model_id, messages, specs, cancelled, on_event):
        requests.append(copy.deepcopy(messages))
        if len(requests) > 1:
            return assistant("Continued after stop.")
        on_event({"type": "text_delta", "text": "A partial response."})
        entered.set()
        assert stopped.wait(5)
        assert cancelled(), "The inference cancellation callback must observe Stop"
        return assistant("LATE_DISCARDED_RESPONSE")

    model.complete = inference
    row = runtime.create({"message": "First turn."})
    worker = threading.Thread(target=lambda: _run(runtime, failures))
    worker.start()
    try:
        assert entered.wait(5)
        paused = runtime.action(row["id"], {"action": "stop"})
        assert paused["status"] == "paused" and paused["run_state"] == "pausing"
        assert paused["messages"][-1]["content"] == "A partial response."
        assert paused["messages"][-1]["interrupted"] is True
    finally:
        stopped.set()
        worker.join(5)
    assert not worker.is_alive() and failures == []
    assert "LATE_DISCARDED_RESPONSE" not in json.dumps(runtime.get(row["id"]))
    send(runtime, row["id"], "Continue with my new request.")
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["result"] == "Continued after stop."
    assert len(requests) == 2 and tools.calls == []


def test_edit_replaces_suffix_preserves_prefix_audit_and_rotates_provider_conversation(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path)
    requests = []

    def inference(model_id, messages, specs, cancelled, on_event, *, conversation):
        requests.append((conversation, copy.deepcopy(messages)))
        return assistant(f"Response {len(requests)}")

    model.complete = inference
    model.supports_conversation_key = True
    row = runtime.create({"message": "Retained first question."})
    advance(runtime)
    send(runtime, row["id"], "DISCARDED_SECOND_QUESTION")
    advance(runtime)
    send(runtime, row["id"], "DISCARDED_FUTURE_QUESTION")
    advance(runtime)
    before = runtime.get(row["id"])
    usage = before["budget"]["lifetime"]["charged_model_calls"]
    edited = send(runtime, row["id"], "Replacement second question.", edit_message_index=2,
                  intent="information", expected_goal_revision=before["goal"]["revision"])
    assert edited["status"] == "queued" and edited["conversation_revision"] == 1
    assert [message["content"] for message in edited["messages"]] == [
        "Retained first question.", "Response 1", "Replacement second question."]
    assert edited["messages"][0]["message_id"] == before["messages"][0]["message_id"]
    assert edited["messages"][2]["message_id"] != before["messages"][2]["message_id"]
    assert edited["budget"]["lifetime"]["charged_model_calls"] == usage
    assert edited["goal"]["revision"] == before["goal"]["revision"] + 1
    assert [item["text"] for item in edited["goal"]["constraints"]] == [
        "Retained first question.", "Replacement second question."]
    assert edited["events"][-1]["type"] == "message_edited"
    assert edited["conversation_event_cursor"] == edited["events"][-1]["seq"]
    assert "conversation_branches" not in edited
    saved = runtime.store.read()["tasks"][row["id"]]
    assert "DISCARDED_FUTURE_QUESTION" in json.dumps(saved["conversation_branches"])
    assert "DISCARDED_FUTURE_QUESTION" not in json.dumps(runtime._context(saved))
    advance(runtime)
    assert requests[-1][0] != requests[-2][0]
    assert requests[-1][0].endswith(":edit:1")
    assert "DISCARDED_SECOND_QUESTION" not in json.dumps(requests[-1][1])
    assert "DISCARDED_FUTURE_QUESTION" not in json.dumps(requests[-1][1])
    assert "Response 1" in json.dumps(requests[-1][1])
    assert tools.calls == []
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    assert restarted.get(row["id"])["result"] == "Response 4"


def test_active_edit_defers_until_old_inference_stops_then_only_sends_replacement(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    failures, requests = [], []

    def inference(model_id, messages, specs, cancelled, on_event):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
            assert cancelled()
            return assistant("OLD_LATE_ANSWER")
        return assistant("Replacement answer.")

    model.complete = inference
    row = runtime.create({"message": "OLD_QUESTION"})
    worker = threading.Thread(target=lambda: _run(runtime, failures))
    worker.start()
    try:
        assert entered.wait(5)
        editing = send(runtime, row["id"], "NEW_QUESTION", edit_message_index=0)
        assert editing["run_state"] == "pausing"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and failures == []
    queued = runtime.get(row["id"])
    assert queued["status"] == "queued" and len(queued["messages"]) == 1
    assert queued["messages"][0]["content"] == "NEW_QUESTION"
    assert queued["goal"]["text"] == "NEW_QUESTION"
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "Replacement answer."
    assert "OLD_QUESTION" not in json.dumps(requests[1]) and "OLD_LATE_ANSWER" not in json.dumps(requests[1])


def test_edit_replay_is_idempotent_and_rejects_assistant_or_stale_index(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Original answer."))
    row = runtime.create({"message": "Original question."})
    advance(runtime)
    for index in (1, -1, 50, True):
        with pytest.raises(AgentError, match="agent_invalid_"):
            send(runtime, row["id"], "Edited", edit_message_index=index)
    before = runtime.get(row["id"])
    value = {"message": "Edited", "edit_message_index": 0, "expected_control_revision": before["control_revision"],
             "client_request_id": str(uuid.uuid4())}
    first = runtime.add_message(row["id"], value)
    replay = runtime.add_message(row["id"], value)
    assert first == replay and replay["conversation_revision"] == 1
    with pytest.raises(AgentError, match="agent_control_revision_conflict"):
        runtime.add_message(row["id"], {**value, "client_request_id": str(uuid.uuid4())})


def test_restart_finishes_committed_edit_before_sending_replacement(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path / "original")
    entered, release = threading.Event(), threading.Event()
    failures = []

    def inference(model_id, messages, specs, cancelled, on_event):
        entered.set()
        assert release.wait(5)
        return assistant("Late old answer.")

    model.complete = inference
    row = runtime.create({"message": "Old question."})
    worker = threading.Thread(target=lambda: _run(runtime, failures))
    worker.start()
    try:
        assert entered.wait(5)
        send(runtime, row["id"], "Committed replacement.", edit_message_index=0)
        shutil.copytree(runtime.store.root, tmp_path / "restarted")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and failures == []
    replacement_model = Model(assistant("Recovered replacement answer."))
    restarted = AgentRuntime(AgentStore(tmp_path / "restarted", key), replacement_model, tools, Catalog())
    restarted.reconcile()
    current = restarted.get(row["id"])
    assert current["status"] == "queued" and current["messages"][0]["content"] == "Committed replacement."
    assert len(current["messages"]) == 1 and current["conversation_revision"] == 1
    advance(restarted)
    assert restarted.get(row["id"])["result"] == "Recovered replacement answer."
    assert "Old question." not in json.dumps(replacement_model.calls[0][1])


def test_edit_cannot_hide_a_dispatched_write_with_unknown_outcome(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc"}))
    row = runtime.create({"message": "Share the service."})
    advance(runtime, 2)
    approve(runtime, row["id"])

    def uncertain(*args, **kwargs):
        tools.shared = True
        raise RuntimeError("connection lost after mutation")

    tools.execute = uncertain
    advance(runtime)
    unknown = runtime.get(row["id"])
    assert unknown["error"] == "agent_write_outcome_unknown"
    assert not unknown["allowed_actions"]["edit_message"]["enabled"]
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        send(runtime, row["id"], "Replace that question.", edit_message_index=0)
    assert runtime.get(row["id"])["messages"][0]["content"] == "Share the service."


def test_edit_preserves_response_provenance_without_reviving_excluded_model_context(tmp_path):
    runtime, model, _, _, task_id, _ = model_switch_setup(tmp_path)
    responses = iter(("Old model answer.", "Retained current model answer.", "Discarded future answer."))
    model.complete = lambda *args, **kwargs: assistant(next(responses))
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime)
    scope_fixture(runtime, task_id)
    runtime.switch_model(task_id, model_switch_body(runtime, task_id))
    send(runtime, task_id, "Question for the selected model.")
    advance(runtime)
    send(runtime, task_id, "Question to replace.")
    advance(runtime)
    before = runtime.get(task_id)
    index = next(index for index, message in enumerate(before["messages"]) if message["content"] == "Question to replace.")
    edited = send(runtime, task_id, "Replacement question.", edit_message_index=index)
    old_reply = next(message for message in edited["messages"] if message["content"] == "Old model answer.")
    current_reply = next(message for message in edited["messages"] if message["content"] == "Retained current model answer.")
    assert old_reply["context_epoch"] == 0  # The model-switch exclusion still applies.
    assert current_reply["context_epoch"] == 2 and current_reply["original_context_epoch"] == 1
    stored = runtime.store.read()["tasks"][task_id]
    context = json.dumps(runtime._context(stored))
    assert "Retained current model answer." in context
    assert all(text not in context for text in ("Old model answer.", "SOURCE-PRIVATE", "DERIVED-PRIVATE", "Discarded future answer."))
    assert runtime._execution_context(stored)["resource_origins"] == []
    # A second edit rebases the same prefix again without changing authorship.
    edited_again = send(runtime, task_id, "Another replacement.", edit_message_index=index)
    reply_again = next(message for message in edited_again["messages"] if message["content"] == "Retained current model answer.")
    assert reply_again["context_epoch"] == 3 and reply_again["original_context_epoch"] == 1


def test_interrupted_restart_task_continues_with_followup_or_resume_and_exposes_pending_steer(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("Resumed answer."), assistant("Continued after follow-up."))
    row = runtime.create({"message": "Inspect the current node."})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(
        status="running", steps=16, elapsed_seconds=180.0,
        in_flight={"kind": "model", "dispatched": True},
    ))
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    interrupted = restarted.get(row["id"])
    assert interrupted["status"] == "interrupted" and interrupted["error"] == "agent_node_restarted"
    assert interrupted["allowed_actions"]["resume"]["enabled"] is True
    assert interrupted["allowed_actions"]["steer"]["enabled"] is True
    assert interrupted["allowed_actions"]["information"]["enabled"] is True

    # Sending a follow-up message directly on an interrupted task continues it.
    continued = restarted.add_message(row["id"], {
        "message": "Continue from where it stopped and also check services.",
        "intent": "information",
        "expected_control_revision": interrupted["control_revision"],
        "expected_goal_revision": interrupted["goal"]["revision"],
    })
    assert continued["status"] == "queued" and continued["error"] is None
    assert continued["steps"] == 0 and continued["run_timing"]["elapsed_seconds"] == 0
    advance(restarted)
    done = restarted.get(row["id"])
    assert done["status"] == "completed" and done["result"] == "Resumed answer."

    # Mid-turn steer exposes pending_user_messages while in flight and settles on step completion.
    entered, release = threading.Event(), threading.Event()

    def slow_complete(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return assistant("Steered response.")

    model.complete = slow_complete
    restarted.add_message(row["id"], {
        "message": "Start second turn.",
        "intent": "information",
        "expected_control_revision": done["control_revision"],
        "expected_goal_revision": done["goal"]["revision"],
    })
    failures = []
    worker = threading.Thread(target=_run, args=(restarted, failures))
    worker.start()
    try:
        assert entered.wait(5)
        running = restarted.get(row["id"])
        steered = restarted.add_message(row["id"], {
            "message": "Adjust mid-turn: focus on local API.",
            "intent": "steer",
            "expected_control_revision": running["control_revision"],
            "expected_goal_revision": running["goal"]["revision"],
        })
        assert steered["pending_user_messages"][-1]["content"] == "Adjust mid-turn: focus on local API."
    finally:
        release.set()
        worker.join(5)
    assert not failures
    settled = restarted.get(row["id"])
    assert "pending_user_messages" not in settled
    assert any(m.get("content") == "Adjust mid-turn: focus on local API." for m in settled["messages"])


def _run(runtime, failures):
    try:
        runtime.run_once()
    except BaseException as exc:
        failures.append(exc)
