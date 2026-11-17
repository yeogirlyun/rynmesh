"""S07-I4: persistent, nonblocking subagent waits and descendant controls."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from rynmesh.agent_runtime import subagents
from rynmesh.agent_runtime.store import AgentToolRejected
from test_agent_subagents import _build_env


def _parent(runtime):
    return runtime.create({"message": "Coordinate workers", "model_id": "local/gpt-4o",
                           "client_request_id": str(uuid.uuid4())})["id"]


def _spawn(runtime, parent_id, name, status="running"):
    def apply(data):
        result = subagents.spawn_agent_in_tx(data, data["tasks"][parent_id],
                                            {"task_name": name, "prompt": "Report findings", "role": "explorer"})
        data["tasks"][result["agent_id"]]["status"] = status
        return result["agent_id"]
    return runtime.store.transaction(apply)


def _queue(runtime, parent_id, timeout=10, targets=None):
    call_id = "wait_" + uuid.uuid4().hex
    arguments = {"timeout_seconds": timeout}
    if targets is not None:
        arguments["targets"] = targets
    def apply(data):
        row = data["tasks"][parent_id]
        row.update(status="queued", updated_at="2000-01-01T00:00:00+00:00")
        row["messages"].append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {
                "name": "ryn_wait_agent", "arguments": json.dumps(arguments)}}]})
        row["queue"] = [{"call_id": call_id, "name": "ryn_wait_agent", "arguments": arguments,
                         "operation_id": "op_" + uuid.uuid4().hex, "effect": "read", "internal": True}]
    runtime.store.transaction(apply)
    return call_id


def _tick(runtime):
    # Recursion is precisely the old mechanism this task must remove. This also
    # keeps the red run from spending minutes inside the old blocking wait.
    assert not hasattr(runtime, "_wait_for_subagents")
    return runtime.run_once()


def _row(runtime, parent_id):
    return runtime.store.read()["tasks"][parent_id]


def _receipt(runtime, parent_id, call_id):
    return next(json.loads(m["content"]) for m in _row(runtime, parent_id)["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def _settle(runtime, child_id):
    def apply(data):
        child = data["tasks"][child_id]
        child.update(status="completed", result="Child complete")
        subagents.on_subagent_settled_in_tx(data, child)
    runtime.store.transaction(apply)


@pytest.mark.parametrize("initial_unread", [False, True])
def test_t1_first_unread_result_returns_with_other_target_running(tmp_path, initial_unread):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    done = _spawn(runtime, parent, "done")
    active = _spawn(runtime, parent, "active")
    if initial_unread:
        _settle(runtime, done)
    call = _queue(runtime, parent)
    _tick(runtime)
    if not initial_unread:
        _settle(runtime, done)
        _tick(runtime)
    result = _receipt(runtime, parent, call)
    assert result["timed_out"] is False
    assert result["all_settled"] is False
    assert result["still_running"] == [{"nickname": "Curie", "agent_path": "/root/active"}]
    assert len(result["notifications"]) == 1
    assert active in _row(runtime, parent)["subagent_ids"]
    assert "subagent_wait" not in _row(runtime, parent)


def test_t2_600_seconds_schema_and_real_model_dispatch(tmp_path):
    spec = next(s["function"] for s in subagents.SUBAGENT_TOOL_SPECS
                if s["function"]["name"] == "ryn_wait_agent")
    assert spec["parameters"]["properties"]["timeout_seconds"] == {
        "type": "integer", "minimum": 1, "maximum": 3600}
    assert "30" in spec["description"] and "3600" in spec["description"]
    _, runtime, model, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    child = _spawn(runtime, parent, "model_target")
    model.parent_responses.append({"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "wait_model", "type": "function", "function": {
            "name": "ryn_wait_agent", "arguments": '{"timeout_seconds":600}'}}]},
        "finish_reason": "tool_calls", "usage": {"input_tokens": 10, "output_tokens": 5}})
    runtime.run_once()
    _tick(runtime)
    waiting = _row(runtime, parent)["subagent_wait"]
    assert (datetime.fromisoformat(waiting["deadline"]) -
            datetime.fromisoformat(waiting["started_at"])).total_seconds() == 600
    _settle(runtime, child)
    _tick(runtime)
    assert _receipt(runtime, parent, "wait_model")["ok"] is True
    assert subagents.validate_subagent_tool("ryn_wait_agent", {"timeout_seconds": 600})["timeout_seconds"] == 600


def test_t3_small_timeout_normalized_and_noted_in_result(tmp_path):
    args = subagents.validate_subagent_tool("ryn_wait_agent", {"timeout_seconds": 5})
    assert args["timeout_seconds"] == 10 and "10" in args["timeout_note"]
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    call = _queue(runtime, parent, timeout=5)
    _tick(runtime)
    assert _receipt(runtime, parent, call)["timeout_note"] == args["timeout_note"]


def test_t4_leaf_all_is_empty_and_explained(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    leaf = _spawn(runtime, parent, "leaf")
    _spawn(runtime, parent, "sibling")
    runtime.store.transaction(lambda d: d["tasks"][parent].update(status="paused"))
    call = _queue(runtime, leaf)
    _tick(runtime)
    result = _receipt(runtime, leaf, call)
    assert result["agents"] == result["still_running"] == []
    assert result["message"] == "no subagents to wait for"
    assert result["timed_out"] is False


def test_t5_delivered_notification_does_not_wake_wait(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    child = _spawn(runtime, parent, "done")
    _spawn(runtime, parent, "active")
    _settle(runtime, child)
    runtime.store.transaction(lambda d: subagents.drain_subagent_notifications_in_tx(d, d["tasks"][parent]))
    call = _queue(runtime, parent)
    _tick(runtime)
    row = _row(runtime, parent)
    assert row["subagent_wait"]["call_id"] == call
    assert not runtime.store.inspect(lambda d: subagents.wait_ready(d, d["tasks"][parent]))
    revision = row["revision"]
    assert not _tick(runtime).activity
    assert _row(runtime, parent)["revision"] == revision
    runtime.store.transaction(lambda d: d["tasks"][parent]["subagent_wait"].update(
        deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
    _tick(runtime)
    result = _receipt(runtime, parent, call)
    assert result["timed_out"] is True and result["notifications"] == []


def test_t6_suspend_skips_claim_and_child_completion_resumes_same_call(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    child = _spawn(runtime, parent, "child")
    call = _queue(runtime, parent, timeout=600)
    _tick(runtime)
    before = _row(runtime, parent)
    assert before["status"] == "queued" and before["in_flight"] is None
    assert before["queue"][0]["call_id"] == call
    assert runtime.get(parent)["waiting_for_subagents"] is True
    assert not _tick(runtime).activity
    _settle(runtime, child)
    _tick(runtime)
    after = _row(runtime, parent)
    assert _receipt(runtime, parent, call)["all_settled"] is True
    assert after["queue"] == [] and "subagent_wait" not in after
    assert runtime.get(parent)["waiting_for_subagents"] is False


def test_t7_six_waiting_parents_release_workers_without_recursion(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parents = []
    for i in range(6):
        parent = _parent(runtime)
        child = _spawn(runtime, parent, f"child{i}")
        call = _queue(runtime, parent, timeout=600)
        _tick(runtime)
        parents.append((parent, child, call))
    assert all(_row(runtime, p).get("subagent_wait") for p, _, _ in parents)
    def queue_children(data):
        for _, child, _ in parents:
            data["tasks"][child]["status"] = "queued"
    runtime.store.transaction(queue_children)
    with ThreadPoolExecutor(max_workers=6) as workers:
        for _ in range(30):
            list(workers.map(lambda _: _tick(runtime), range(6)))
            if all(not _row(runtime, p)["queue"] for p, _, _ in parents):
                break
    for parent, child, call in parents:
        assert _row(runtime, child)["status"] == "completed"
        assert _receipt(runtime, parent, call)["all_settled"] is True


def test_t8_real_owner_message_wakes_wait_with_steered_receipt(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    _spawn(runtime, parent, "active")
    call = _queue(runtime, parent)
    _tick(runtime)
    row = _row(runtime, parent)
    runtime.add_message(parent, {"message": "Use the latest requirement", "intent": "steer",
                                 "expected_revision": row["revision"],
                                 "expected_control_revision": row["control_revision"],
                                 "expected_goal_revision": row["goal"]["revision"]})
    assert runtime.store.inspect(lambda d: subagents.wait_ready(d, d["tasks"][parent]))
    _tick(runtime)
    assert _receipt(runtime, parent, call)["steered"] is True
    assert _receipt(runtime, parent, call)["timed_out"] is False
    assert any(m.get("content") == "Use the latest requirement" for m in _row(runtime, parent)["messages"])
    assert not _row(runtime, parent).get("pending_control")


def test_t9_restart_persists_wall_clock_deadline_and_timeout_excludes_idle_time(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    _spawn(runtime, parent, "active", status="queued")
    call = _queue(runtime, parent)
    _tick(runtime)
    waiting = copy.deepcopy(_row(runtime, parent)["subagent_wait"])
    start = datetime.fromisoformat(waiting["started_at"])
    deadline = datetime.fromisoformat(waiting["deadline"])
    assert (deadline - start).total_seconds() == 10
    _, restarted, _, _ = _build_env(tmp_path)
    assert _row(restarted, parent)["subagent_wait"] == waiting
    assert not restarted.store.inspect(lambda d: subagents.wait_ready(d, d["tasks"][parent]))
    def expire(data):
        row = data["tasks"][parent]
        row["updated_at"] = "2000-01-01T00:00:00+00:00"
        row["subagent_wait"].update(started_at=(start - timedelta(hours=1)).isoformat(),
                                    deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    restarted.store.transaction(expire)
    elapsed = _row(restarted, parent)["elapsed_seconds"]
    _tick(restarted)
    assert _receipt(restarted, parent, call)["timed_out"] is True
    assert _row(restarted, parent)["elapsed_seconds"] - elapsed < 5


@pytest.mark.parametrize("operation,key", [(subagents.close_agent_in_tx, "closed"),
                                            (subagents.interrupt_agent_in_tx, "interrupted")])
def test_t10_leaf_control_all_does_not_change_siblings(tmp_path, operation, key):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    leaf = _spawn(runtime, parent, "leaf")
    sibling = _spawn(runtime, parent, "sibling")
    result = runtime.store.transaction(lambda d: operation(d, d["tasks"][leaf], {"target": "all"}))
    assert result[key] == []
    assert _row(runtime, sibling)["status"] == "running"


@pytest.mark.parametrize("operation", [subagents.close_agent_in_tx, subagents.interrupt_agent_in_tx])
@pytest.mark.parametrize("target", ["branch", "/root/branch", "sibling"])
def test_t11_descendant_controls_reject_parent_and_sibling(tmp_path, operation, target):
    _, runtime, _, _ = _build_env(tmp_path)
    root = _parent(runtime)
    parent = _spawn(runtime, root, "branch")
    leaf = _spawn(runtime, parent, "leaf")
    _spawn(runtime, root, "sibling")
    with pytest.raises(AgentToolRejected, match="agent_subagent_not_found") as error:
        runtime.store.transaction(lambda d: operation(d, d["tasks"][leaf], {"target": target}))
    assert "只能关闭/中断自己的子代理" in error.value.correction
    assert _row(runtime, parent)["status"] == "running"


def test_t12_root_can_close_grandchild_by_path(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    root = _parent(runtime)
    branch = _spawn(runtime, root, "branch")
    leaf = _spawn(runtime, branch, "leaf")
    result = runtime.store.transaction(lambda d: subagents.close_agent_in_tx(
        d, d["tasks"][root], {"target": "/root/branch/leaf"}))
    assert result["closed"][0]["agent_id"] == leaf
    assert _row(runtime, leaf)["subagent_meta"]["closed"] is True


def test_t13_sibling_message_and_wait_paths_remain_in_tree_scope(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    root = _parent(runtime)
    sender = _spawn(runtime, root, "sender")
    sibling = _spawn(runtime, root, "sibling")
    def apply(data):
        row = data["tasks"][sender]
        assert subagents.resolve_subagent_targets(data, row, ["/root/sibling"])[0]["id"] == sibling
        return subagents.send_message_in_tx(data, row, {"target": "/root/sibling", "message": "Shared finding"})
    assert runtime.store.transaction(apply)["agent_id"] == sibling
    assert any(m.get("content") == "Shared finding" for m in _row(runtime, sibling)["messages"])


@pytest.mark.parametrize("timeout", [True, "600", 3.5, None, 3601, -1])
def test_timeout_rejections_are_actionable_before_schema(tmp_path, timeout):
    with pytest.raises(AgentToolRejected, match="agent_invalid_tool_arguments") as error:
        subagents.validate_subagent_tool("ryn_wait_agent", {"timeout_seconds": timeout})
    assert "timeout_seconds" in error.value.correction
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    call = _queue(runtime, parent, timeout=timeout)
    _tick(runtime)
    result = _receipt(runtime, parent, call)
    assert result["error"] == "agent_invalid_tool_arguments"
    assert "timeout_seconds" in result["correction"]


def test_wait_ready_pending_control_and_no_state(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    assert runtime.store.inspect(lambda d: subagents.wait_ready(d, d["tasks"][parent]))
    _spawn(runtime, parent, "active")
    _queue(runtime, parent)
    _tick(runtime)
    runtime.store.transaction(lambda d: d["tasks"][parent].update(pending_control={"kind": "steer"}))
    assert runtime.store.inspect(lambda d: subagents.wait_ready(d, d["tasks"][parent]))


def test_owner_can_continue_after_pausing_suspended_wait(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = _parent(runtime)
    _spawn(runtime, parent, "active")
    _queue(runtime, parent)
    _tick(runtime)
    row = _row(runtime, parent)
    runtime.action(parent, {"action": "pause", "expected_revision": row["revision"]})
    row = _row(runtime, parent)
    runtime.add_message(parent, {"message": "Continue with new instructions", "intent": "steer",
                                 "expected_revision": row["revision"],
                                 "expected_goal_revision": row["goal"]["revision"]})
    assert _row(runtime, parent)["status"] == "queued"
    assert "subagent_wait" not in _row(runtime, parent)
