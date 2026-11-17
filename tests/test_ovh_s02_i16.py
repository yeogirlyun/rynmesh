"""S02-I16 owner rollback through real encrypted state and running workers."""
import copy
import json
import threading
import uuid
import shutil

import pytest
from test_agent_conversation_controls import send, _run
from test_agent_runtime import advance, approve, assistant, make_runtime
from rynmesh.agent_runtime.store import AgentError
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.runtime import AgentRuntime


def rollback(runtime, task_id, index, **extra):
    return runtime.action(task_id, {"action": "rollback", "message_index": index,
        "expected_control_revision": runtime.get(task_id)["control_revision"], **extra})


@pytest.mark.parametrize("owner_turn", [1, 2])
def test_t1_rollback_keeps_only_prefix_rebuilds_goal_and_allows_followup(tmp_path, owner_turn):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("First answer"), assistant("Second answer"),
        assistant("Third answer"), assistant("Replacement answer"))
    task = runtime.create({"message": "Original goal"})
    advance(runtime)
    for text in ["Keep this constraint", "Discard this constraint"]:
        send(runtime, task["id"], text, intent="steer",
             expected_goal_revision=runtime.get(task["id"])["goal"]["revision"])
        advance(runtime)
    before = runtime.get(task["id"])
    indices = [i for i, m in enumerate(before["messages"]) if m["role"] == "user"]
    index = indices[owner_turn]
    retained_ids = {m.get("message_id") for m in before["messages"][:index] if m["role"] == "user"}
    expected_constraints = [c for c in before["goal"]["constraints"] if c["message_id"] in retained_ids]
    artifact = tmp_path / "already-written.txt"
    artifact.write_text("Do not undo this file")
    body = {"client_request_id": str(uuid.uuid4()), "expected_control_revision": before["control_revision"]}
    after = rollback(runtime, task["id"], index, **body)
    assert after["status"] == "completed" and after["result"] == "" and after["error"] is None
    assert [m["content"] for m in after["messages"]] == [m["content"] for m in before["messages"][:index]]
    assert after["goal"]["text"] == "Original goal" and after["goal"]["constraints"] == expected_constraints
    assert after["goal"]["revision"] == before["goal"]["revision"] + 1
    assert after["conversation_revision"] == 1 and after["control_revision"] > before["control_revision"]
    assert any(e["type"] == "message_rolled_back" and e["message_index"] == index for e in after["events"])
    assert after["allowed_actions"]["rollback"]["enabled"] is True
    assert artifact.read_text() == "Do not undo this file"
    stored = runtime.store.read()["tasks"][task["id"]]
    assert stored["conversation_branches"][-1]["retired_count"] == len(before["messages"]) - index
    assert "messages" not in stored["conversation_branches"][-1]
    assert runtime.action(task["id"], {"action": "rollback", "message_index": index, **body})["conversation_revision"] == 1
    send(runtime, task["id"], "New direction")
    advance(runtime)
    assert runtime.get(task["id"])["result"] == "Replacement answer"
    sent = json.dumps(model.calls[-1][1])
    assert "Discard this constraint" not in sent and "Third answer" not in sent


def test_t1_rollback_waits_for_inflight_model_and_discards_late_response(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("First answer"))
    task = runtime.create({"message": "First request"})
    advance(runtime)
    send(runtime, task["id"], "Rolled back request")
    index = len(runtime.get(task["id"])["messages"]) - 1
    entered, release, failures = threading.Event(), threading.Event(), []
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return assistant("Late discarded answer")
    model.complete = slow
    worker = threading.Thread(target=_run, args=(runtime, failures))
    worker.start()
    try:
        assert entered.wait(10)
        pending = rollback(runtime, task["id"], index)
        assert pending["run_state"] == "pausing"
        assert runtime.store.read()["tasks"][task["id"]]["pending_control"] == {"kind": "rollback", "message_index": index}
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    after = runtime.get(task["id"])
    assert after["status"] == "completed" and len(after["messages"]) == index
    assert all(m["content"] != "Late discarded answer" for m in after["messages"])


def test_t2_rollback_cannot_hide_unknown_dispatched_write(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant("First answer"),
        assistant(name="share_service", args={"service_id": "svc"}))
    task = runtime.create({"message": "First request"})
    advance(runtime)
    send(runtime, task["id"], "Share the service")
    index = len(runtime.get(task["id"])["messages"]) - 1
    advance(runtime, 2)
    approve(runtime, task["id"])
    def uncertain(*args, **kwargs):
        tools.shared = True
        raise RuntimeError("connection lost after mutation")
    tools.execute = uncertain
    advance(runtime)
    before = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    assert runtime.get(task["id"])["allowed_actions"].get("rollback", {}).get("enabled") is False
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        rollback(runtime, task["id"], index)
    assert runtime.store.read()["tasks"][task["id"]] == before


def test_t1_restart_finishes_pending_rollback_without_resuming_discarded_turn(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path / "original", assistant("First answer"))
    task = runtime.create({"message": "First request"})
    advance(runtime)
    send(runtime, task["id"], "Discarded turn")
    index = len(runtime.get(task["id"])["messages"]) - 1
    entered, release, failures = threading.Event(), threading.Event(), []
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return assistant("Late answer")
    model.complete = slow
    worker = threading.Thread(target=_run, args=(runtime, failures))
    worker.start()
    try:
        assert entered.wait(10)
        rollback(runtime, task["id"], index)
        shutil.copytree(runtime.store.root, tmp_path / "restarted")
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    restarted = AgentRuntime(AgentStore(tmp_path / "restarted", key), model, tools, runtime.catalog)
    restarted.reconcile()
    after = restarted.get(task["id"])
    assert after["status"] == "completed" and len(after["messages"]) == index
    assert after["error"] is None and after["conversation_revision"] == 1


def test_t3_first_owner_message_is_rejected_with_edit_hint(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "First request"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]]["messages"].insert(0,
        {"role": "system", "content": "Synthetic prior system record"}))
    with pytest.raises(AgentError, match="agent_rollback_first_message_use_edit"):
        rollback(runtime, task["id"], 1)
    assert len(runtime.get(task["id"])["messages"]) == 2


@pytest.mark.parametrize("index", [True, "2", -1, 99, 1])
def test_t3_invalid_or_non_user_index_is_rejected(tmp_path, index):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First answer"))
    task = runtime.create({"message": "First request"})
    advance(runtime)
    with pytest.raises(AgentError, match="agent_invalid_message_index"):
        rollback(runtime, task["id"], index)


@pytest.mark.parametrize("phase", ["admit", "deliver"])
def test_t1_pending_rollback_rejects_js_callbacks_before_prefix_is_rewritten(tmp_path, phase):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from test_agent_js_integration import _seed_task_row, HOST_METHOD_MAPPING, FakeCoordinatorPort
    from rynmesh.agent_runtime.js_integration import plan_child_call, deliver_committed_token
    from rynmesh.agent_runtime.js_runtime.protocol import PendingHostCallEnvelope, canonical_arguments_sha256
    from rynmesh.agent_runtime.tool_delivery import stage_tool_completion
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    _, catalog = _seed_task_row(store)
    env = PendingHostCallEnvelope(task_id="task_js_phase_a", session_id="sess_001", generation=1,
        execution_id="exec_001", callback_id="cb_read_1", ordinal=0, method="list_windows",
        effect="read", target_tool="computer_observe_windows", arguments={},
        arguments_sha256=canonical_arguments_sha256({}))
    if phase == "deliver":
        child = store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING))
        token = store.transaction(lambda data: stage_tool_completion(data["tasks"]["task_js_phase_a"], child,
            receipt_status="confirmed", result={"windows": []}))
    store.transaction(lambda data: data["tasks"]["task_js_phase_a"].update(status="paused",
        pending_control={"kind": "rollback", "message_index": 2}))
    if phase == "admit":
        with pytest.raises(AgentError, match="agent_js_authority_changed"):
            store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING))
    else:
        port = FakeCoordinatorPort()
        outcome = deliver_committed_token(store.read()["tasks"]["task_js_phase_a"], token, port,
            live_identity={"task_id": "task_js_phase_a", "session_id": "sess_001", "generation": 1,
                           "execution_id": "exec_001", "alive": True})
        assert outcome == "continuation_lost" and not port.resolved and not port.rejected
