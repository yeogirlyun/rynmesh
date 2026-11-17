"""S02-I4: explicit auto intent selects start or steer inside the transaction."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from rynmesh.agent_runtime.store import AgentError
from test_agent_runtime import advance, assistant, make_runtime


def _auto(runtime, task_id, **extra):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": "Use the updated requirement", "intent": "auto",
                                        "expected_revision": row["revision"], **extra})


def test_t1_idle_auto_starts_new_turn_without_goal_revision(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First complete"), assistant("Second complete"))
    parent = runtime.create({"message": "Initial requirement"})
    advance(runtime)
    first = runtime.get(parent["id"])
    assert first["status"] == "completed"
    changed = _auto(runtime, parent["id"])
    assert changed["status"] == "queued"
    assert changed["goal"]["revision"] == first["goal"]["revision"] + 1
    assert changed["messages"][-1]["ui"]["kind"] == "message"
    assert any(e["type"] == "message_added" for e in changed["events"])
    advance(runtime)
    assert runtime.get(parent["id"])["result"] == "Second complete"


def test_t2_running_auto_steers_and_reaches_next_real_model_request(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def complete(model_id, messages, specs, cancelled, on_event):
        calls.append(messages)
        if len(calls) == 1:
            entered.set()
            assert release.wait(30)
            return assistant("Earlier turn result", name="share_service", args={"service_id": "A"})
        return assistant("Updated requirement handled")

    model.complete = complete
    parent = runtime.create({"message": "Initial requirement"})
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(runtime.run_once)
        try:
            assert entered.wait(30)
            assert runtime.get(parent["id"])["status"] == "running"
            changed = _auto(runtime, parent["id"])
            assert changed["goal"]["revision"] == 2
            assert changed["pending_user_messages"][-1]["ui"]["kind"] == "steer"
            assert any(e["type"] == "goal_changed" for e in changed["events"])
        finally:
            release.set()
        future.result(timeout=30)
    advance(runtime)
    done = runtime.get(parent["id"])
    assert done["result"] == "Updated requirement handled"
    assert "Use the updated requirement" in json.dumps(calls[-1])
    assert tools.calls == []


def test_t3_waiting_approval_auto_uses_steer_and_closes_old_authority(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "A"}), assistant("New direction handled"))
    parent = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    waiting = runtime.get(parent["id"])
    assert waiting["status"] == "waiting_approval"
    assert waiting["allowed_actions"]["steer"]["enabled"] is True
    changed = _auto(runtime, parent["id"])
    assert changed["status"] == "queued" and changed["pending_approval"] is None
    assert changed["goal"]["revision"] == waiting["goal"]["revision"] + 1
    assert changed["messages"][-1]["ui"]["kind"] == "steer"
    receipt = next(json.loads(m["content"]) for m in changed["messages"] if m["role"] == "tool")
    assert receipt["error"] == "agent_owner_changed" and receipt["not_executed"] is True
    with pytest.raises(AgentError, match="agent_approval_mismatch"):
        runtime.action(parent["id"], {"action": "approve", "expected_revision": changed["revision"],
                                     "approval_id": waiting["pending_approval"]["id"]})
    advance(runtime)
    assert runtime.get(parent["id"])["result"] == "New direction handled"
    assert tools.calls == [] and not tools.shared


@pytest.mark.parametrize("status", ["queued", "waiting_input"])
def test_other_active_states_auto_becomes_steer(tmp_path, status):
    responses = ([assistant(name="ryn_request_input", args={"goal_revision": 1,
        "kind": "information", "prompt": "Which service?", "fields": [
            {"id": "service", "label": "Service", "type": "text", "required": True}]})]
        if status == "waiting_input" else [])
    runtime, _, _, _ = make_runtime(tmp_path, *responses)
    parent = runtime.create({"message": "Initial requirement"})
    if status == "waiting_input":
        advance(runtime, 2)
    assert runtime.get(parent["id"])["status"] == status
    changed = _auto(runtime, parent["id"])
    assert changed["status"] == "queued" and changed["pending_input"] is None
    assert changed["messages"][-1]["ui"]["kind"] == "steer"


def test_auto_still_requires_current_revision_and_node_authorization(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    parent = runtime.create({"message": "Initial requirement"})
    row = runtime.get(parent["id"])
    with pytest.raises(AgentError, match="agent_revision_conflict"):
        _auto(runtime, parent["id"], expected_revision=row["revision"] - 1)
    assert runtime.get(parent["id"])["goal"]["revision"] == 1
    runtime.update_settings({"enabled": False})
    with pytest.raises(AgentError, match="agent_disabled"):
        _auto(runtime, parent["id"])


def test_auto_request_replay_does_not_create_second_owner_turn(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    parent = runtime.create({"message": "Initial requirement"})
    request_id = str(uuid.uuid4())
    changed = _auto(runtime, parent["id"], client_request_id=request_id)
    replay = runtime.add_message(parent["id"], {"message": "Use the updated requirement", "intent": "auto",
        "expected_revision": parent["revision"], "client_request_id": request_id})
    assert replay["goal"]["revision"] == changed["goal"]["revision"] == 2
    assert sum(m.get("content") == "Use the updated requirement" for m in replay["messages"]) == 1


def test_omitted_intent_and_explicit_steer_keep_old_revision_contract(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    parent = runtime.create({"message": "Initial requirement"})
    value = {"message": "A new requirement", "expected_revision": parent["revision"]}
    with pytest.raises(AgentError, match="agent_task_busy"):
        runtime.add_message(parent["id"], value)
    with pytest.raises(AgentError, match="agent_goal_revision_conflict"):
        runtime.add_message(parent["id"], {**value, "intent": "steer"})
