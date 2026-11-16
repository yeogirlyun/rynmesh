"""S02-I2 regression tests: content_filter one-shot guidance retry and provider-side cancelled retry."""
from __future__ import annotations

import copy
import inspect

from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import loop as loop_module
from rynmesh.agent_runtime import retry_policy, turn_state
from rynmesh.agent_runtime.turn_state import decide_turn


def test_turn_state_keeps_content_filter_interrupted_fallback_and_guidance_within_byte_limit():
    """T1 (Changes 2, 3): turn_state keeps content_filter -> interrupted fallback with loop comment; CONTENT_FILTER_GUIDANCE <= 512 bytes."""
    decision = decide_turn(finish_reason="content_filter")
    assert decision.action == "interrupted"
    assert decision.reason == "provider_stopped"
    assert not decision.end_turn

    source = inspect.getsource(turn_state.decide_turn)
    assert "loop" in source

    guidance = loop_module.CONTENT_FILTER_GUIDANCE
    assert isinstance(guidance, str) and guidance.strip()
    assert len(guidance.encode("utf-8")) <= 512
    lower = guidance.lower()
    assert "rephrase" in lower or "绕过" in guidance
    assert "encode" in lower or "split" in lower or "编码" in guidance
    assert "alternative" in lower or "替代" in guidance


def test_first_content_filter_appends_hidden_guidance_and_completes_on_retry(tmp_path):
    """T2 (Changes 1, 2): First content_filter appends hidden runtime_note guidance, emits content_filter_guidance, and second call completes."""
    runtime, model, _, _ = make_runtime(tmp_path)
    sent_requests: list[list[dict]] = []

    def complete(model_id, messages, specs, cancelled, on_event):
        sent_requests.append(copy.deepcopy(messages))
        if len(sent_requests) == 1:
            return {"message": {"role": "assistant", "content": ""}, "finish_reason": "content_filter"}
        return assistant("Here is a safe summary and the completed authorized work.")

    model.complete = complete
    created = runtime.create({"message": "Summarize the document and finish the report."})
    task_id = created["id"]

    # Step 1: content_filter triggers guidance injection and requeues without failing
    advance(runtime, 1)
    mid_saved = runtime.store.read()["tasks"][task_id]
    assert mid_saved["status"] == "queued"
    assert mid_saved["error"] is None
    assert mid_saved.get("content_filter_guidance_step") == [1, 0]
    assert any(ev["type"] == "content_filter_guidance" for ev in mid_saved["events"])
    guidance_msg = mid_saved["messages"][-1]
    assert guidance_msg["role"] == "user"
    assert guidance_msg["content"] == loop_module.CONTENT_FILTER_GUIDANCE
    assert guidance_msg["ui"] == {"kind": "runtime_note", "hidden": True}

    # Step 2: retry includes the guidance note once and completes
    advance(runtime, 1)
    done = runtime.get(task_id)
    assert done["status"] == "completed"
    assert done["result"] == "Here is a safe summary and the completed authorized work."
    assert len(sent_requests) == 2
    matching = [
        m for m in sent_requests[1]
        if m.get("role") == "user" and m.get("content") == loop_module.CONTENT_FILTER_GUIDANCE
    ]
    assert len(matching) == 1
    assert "ui" not in matching[0]


def test_two_consecutive_content_filters_in_same_step_stop_without_second_guidance(tmp_path):
    """T3 (Changes 1, 3): Two consecutive content_filter responses in the same step stop the task without looping."""
    assert decide_turn(finish_reason="content_filter").action == "interrupted"

    runtime, model, _, _ = make_runtime(tmp_path)
    calls = 0

    def complete(model_id, messages, specs, cancelled, on_event):
        nonlocal calls
        calls += 1
        return {"message": {"role": "assistant", "content": "filtered"}, "finish_reason": "content_filter"}

    model.complete = complete
    created = runtime.create({"message": "Check repeated content filter."})
    task_id = created["id"]

    advance(runtime, 1)
    assert runtime.get(task_id)["status"] == "queued"

    advance(runtime, 1)
    stopped = runtime.get(task_id)
    assert stopped["status"] == "failed"
    assert stopped["error"] == "agent_model_content_filtered"
    assert calls == 2
    guidance_events = [ev for ev in stopped["events"] if ev["type"] == "content_filter_guidance"]
    assert len(guidance_events) == 1


def test_provider_cancelled_retries_when_owner_did_not_cancel_and_stops_when_owner_cancelled(
    tmp_path, monkeypatch
):
    """T4 (Change 1): finish_reason=='cancelled' schedules upstream retry when owner did not cancel, and stays cancelled when owner cancelled."""
    runtime, model, _, _ = make_runtime(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(retry_policy, "now", lambda: clock[0])

    attempt = 0

    def gateway_cancel_then_ok(model_id, messages, specs, cancelled, on_event):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            assert not cancelled()
            return {"message": {"role": "assistant", "content": "partial"}, "finish_reason": "cancelled"}
        return assistant("Completed after transient gateway cancel.")

    model.complete = gateway_cancel_then_ok
    first_task = runtime.create({"message": "Run through transient gateway cancel."})
    first_id = first_task["id"]

    advance(runtime, 1)
    retrying = runtime.get(first_id)
    assert retrying["status"] == "queued"
    assert retrying["connection_recovery"]["state"] == "retrying"
    assert retrying["connection_recovery"]["error_code"] == "agent_model_upstream_failed"
    assert any(
        ev["type"] == "model_retry_scheduled" and ev.get("error") == "agent_model_upstream_failed"
        for ev in retrying["events"]
    )

    clock[0] += 60.0
    advance(runtime, 1)
    finished = runtime.get(first_id)
    assert finished["status"] == "completed"
    assert finished["result"] == "Completed after transient gateway cancel."

    # Subcase B: owner explicitly cancels while model request is in flight
    owner_task = runtime.create({"message": "Owner will cancel this turn."})
    owner_id = owner_task["id"]
    owner_calls = 0

    def owner_cancels_in_flight(model_id, messages, specs, cancelled, on_event):
        nonlocal owner_calls
        owner_calls += 1
        current = runtime.get(owner_id)
        runtime.action(
            owner_id,
            {"action": "cancel", "expected_control_revision": current["control_revision"]},
        )
        assert cancelled()
        return {"message": {"role": "assistant", "content": ""}, "finish_reason": "cancelled"}

    model.complete = owner_cancels_in_flight
    advance(runtime, 1)
    cancelled_row = runtime.get(owner_id)
    assert cancelled_row["status"] == "cancelled"
    assert owner_calls == 1
    assert not any(ev["type"] == "model_retry_scheduled" for ev in cancelled_row["events"])
