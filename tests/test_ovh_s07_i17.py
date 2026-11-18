"""S07-I17: stop intent must not erase a dispatched operation's history."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from test_agent_runtime import assistant
from test_agent_subagents import _build_env
from rynmesh.agent_runtime import subagents
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore


@pytest.fixture
def writing_child(tmp_path):
    _, runtime, model, _ = _build_env(tmp_path)
    ledger = OperationLedger(tmp_path / "node")
    tools = LocalTools(SimpleNamespace(home=tmp_path / "node"), lambda: ledger)
    tools.store = runtime.store
    def specs():
        return [{"type": "function", "function": {"name": name, "description": SPECS[name][0], "parameters": copy.deepcopy(SPECS[name][3])}}
                for name in ("ryn_file_read", "ryn_file_write")]
    def metadata(name):
        description, effect, risk, _ = SPECS[name]
        return {"effect": effect, "title": description, "risk": risk, "scope_context_supported": True,
                "supports_preview": effect == "write", "preview_precondition": effect == "write"}
    # LocalTools is the real executor; the test supplies its small catalog facade.
    runtime.tools = SimpleNamespace(specs=specs, metadata=metadata, preview=tools.preview,
                                    execute=tools.execute, verification_specs=tools.verification_specs,
                                    verify=tools.verify, reconcile=tools.reconcile)
    parent = runtime.create({"message": "coordinate writes", "model_id": "local/gpt-4o"})["id"]
    def spawn(data):
        root = data["tasks"][parent]
        root.update(status="paused", queue=[], in_flight=None)
        return subagents.spawn_agent_in_tx(data, root, {
            "task_name": "writer", "prompt": "write out.txt", "role": "worker",
            "write_scope": ["out.txt"]})["agent_id"]
    child = runtime.store.transaction(spawn)
    model.child_responses.append(assistant(name="ryn_file_write", args={"path": "out.txt", "content": "real committed data"}, call_id="write_call"))
    runtime.run_once()
    inferred = runtime.store.read()["tasks"][child]
    assert inferred["queue"], json.dumps(inferred["events"], ensure_ascii=False)
    entered, release = threading.Event(), threading.Event()
    original = tools.execute
    def delayed_return(name, args, **options):
        result = original(name, args, **options)
        entered.set()
        assert release.wait(20), "test did not release the real file-write response"
        return result
    runtime.tools.execute = delayed_return
    worker = threading.Thread(target=runtime.run_once, name="s07-i17-real-write")
    worker.start()
    try:
        assert entered.wait(10), runtime.store.read()["tasks"][child]
        assert (tmp_path / "out.txt").read_text() == "real committed data"
        flight = runtime.store.read()["tasks"][child]["in_flight"]
        assert flight["dispatched"] is True and flight["name"] == "ryn_file_write"
        yield runtime, parent, child, release, worker, flight
    finally:
        release.set()
        worker.join(10)
        tools.processes.close()
        assert not worker.is_alive()


def stop(runtime, parent, child, mode):
    if mode in {"interrupt", "close"}:
        # Use the real parent model -> internal tool queue -> subagent stop path.
        name = "ryn_interrupt_agent" if mode == "interrupt" else "ryn_close_agent"
        runtime.model.parent_responses.append(assistant(name=name, args={"target": child}))
        runtime.store.transaction(lambda data: data["tasks"][parent].update(status="queued"))
        runtime.run_once()
        runtime.run_once()
    else:
        runtime.store.transaction(lambda data: data["tasks"][parent].update(status="queued"))
        runtime.action(parent, {"action": mode})


def paired(row):
    return [json.loads(message["content"]) for message in row["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "write_call"]


@pytest.mark.parametrize("mode", ["interrupt", "close", "cancel", "pause", "stop"])
def test_t2_t3_t6_stop_preserves_dispatched_write_and_never_claims_not_executed(writing_child, mode):
    runtime, parent, child, _, _, flight = writing_child
    runtime.store.transaction(lambda data: data["tasks"][child]["queue"].append({
        "call_id": "queued_other", "name": "ryn_file_read", "operation_id": "queued_other", "internal": False}))
    stop(runtime, parent, child, mode)
    row = runtime.store.read()["tasks"][child]
    assert row["in_flight"] == flight
    assert row["pending_control"]["kind"] == ("cancel" if mode in {"close", "cancel"} else "pause")
    assert row["status"] == ("cancelled" if mode in {"close", "cancel"} else "paused")
    assert not paired(row)
    assert [call["call_id"] for call in row["queue"]] == ["write_call"]
    unsent = [json.loads(message["content"]) for message in row["messages"] if message.get("tool_call_id") == "queued_other"]
    assert len(unsent) == 1 and unsent[0]["not_executed"] and unsent[0]["no_mutation"]
    assert any(event["type"] in {"paused", "cancelled"} and event.get("operation_in_flight") for event in row["events"])
    if mode in {"close", "cancel"}:
        assert row["subagent_meta"]["closed"]


@pytest.mark.parametrize("mode", ["interrupt", "close", "cancel"])
def test_t4_t7_tool_return_has_one_real_receipt_and_second_parent_notification(writing_child, mode):
    runtime, parent, child, release, worker, flight = writing_child
    stop(runtime, parent, child, mode)
    before = runtime.store.read()
    notices = before["tasks"][parent]["subagent_notifications"]
    assert len(notices) == 1
    assert notices[0]["operation_in_flight"] is True
    assert "最后一个操作仍在完成，结果稍后补报" in notices[0]["summary"]
    wire = subagents.format_notification_tag(notices[0])
    assert '"operation_in_flight": true' in wire
    release.set()
    worker.join(10)
    data = runtime.store.read()
    row = data["tasks"][child]
    assert row["in_flight"] is None and row["status"] == ("paused" if mode == "interrupt" else "cancelled")
    assert "pending_control" not in row
    replies = paired(row)
    assert len(replies) == 1 and replies[0].get("sha256") and not replies[0].get("not_executed")
    receipts = [r for r in row["receipts"] if r["operation_id"] == flight["operation_id"]]
    assert len(receipts) == 1 and receipts[0]["status"] == "confirmed"
    assert receipts[0]["result"]["sha256"] == replies[0]["sha256"]
    notices = data["tasks"][parent]["subagent_notifications"]
    assert len(notices) == 2 and not notices[-1].get("operation_in_flight")
    assert replies[0]["sha256"] in notices[-1]["summary"]
    runtime.store.transaction(lambda current: subagents.on_subagent_settled_in_tx(current, current["tasks"][child]))
    assert len(runtime.store.read()["tasks"][parent]["subagent_notifications"]) == 2


def test_t5_restart_keeps_unknown_write_and_schedules_reconciliation(writing_child):
    runtime, parent, child, _, _, flight = writing_child
    stop(runtime, parent, child, "interrupt")
    # Simulate restart with the durable state while the old response is absent.
    runtime.tools.reconcile = lambda *args, **kwargs: {"state": "unknown"}
    restarted = AgentRuntime(AgentStore(runtime.store.root, runtime.store.key), runtime.model, runtime.tools, runtime.catalog)
    restarted.reconcile()
    row = restarted.store.read()["tasks"][child]
    assert row["status"] == "interrupted" and row["error"] == "agent_write_outcome_unknown"
    assert row["in_flight"]["operation_id"] == flight["operation_id"]
    assert row["reconciliation"]["state"] == "pending"
    assert next(r for r in row["receipts"] if r["operation_id"] == flight["operation_id"])["status"] == "unknown"
    assert not paired(row)


def test_t8_idle_child_stops_and_rejects_only_undispatched_calls(tmp_path):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = runtime.create({"message": "coordinate", "model_id": "local/gpt-4o"})["id"]
    def exercise(data):
        root = data["tasks"][parent]
        child_id = subagents.spawn_agent_in_tx(data, root, {"task_name": "idle", "prompt": "inspect", "role": "explorer"})["agent_id"]
        child = data["tasks"][child_id]
        child["pending_approval"] = {"id": "stale"}
        child["pending_input"] = {"id": "stale"}
        child["queue"] = [{"call_id": "not_dispatched", "name": "ryn_file_read", "operation_id": "not_dispatched", "internal": False}]
        subagents.interrupt_agent_in_tx(data, root, {"target": child_id})
        return copy.deepcopy(child)
    row = runtime.store.transaction(exercise)
    assert row["status"] == "paused" and row["in_flight"] is None
    assert row["pending_approval"] is None and row["pending_input"] is None
    assert not row["queue"] and not row.get("pending_control")
    assert json.loads(next(m["content"] for m in row["messages"] if m.get("tool_call_id") == "not_dispatched"))["not_executed"]


@pytest.mark.parametrize("close", [False, True])
def test_ambiguous_child_preserves_status_error_and_flight(tmp_path, close):
    _, runtime, _, _ = _build_env(tmp_path)
    parent = runtime.create({"message": "coordinate", "model_id": "local/gpt-4o"})["id"]
    def exercise(data):
        root = data["tasks"][parent]
        child_id = subagents.spawn_agent_in_tx(data, root, {"task_name": "uncertain", "prompt": "inspect", "role": "explorer"})["agent_id"]
        child = data["tasks"][child_id]
        child.update(status="interrupted", error="agent_write_outcome_unknown", in_flight={"kind": "tool", "effect": "write", "dispatched": True})
        subagents.cascade_stop_subagents_in_tx(data, root, close=close)
        return child
    row = runtime.store.transaction(exercise)
    assert row["pending_control"] == {"kind": "cancel" if close else "pause"}
    assert row["status"] == "interrupted" and row["error"] == "agent_write_outcome_unknown"
    assert row["in_flight"]["dispatched"]


@pytest.mark.parametrize("queue_state", ["empty", "other_call_first"])
def test_finish_removes_matching_call_without_popping_another_or_crashing(writing_child, queue_state):
    runtime, parent, child, release, worker, _ = writing_child
    stop(runtime, parent, child, "interrupt")
    def rearrange(data):
        row = data["tasks"][child]
        row["queue"] = [] if queue_state == "empty" else [{"call_id": "other", "name": "ryn_file_read", "operation_id": "other", "internal": False}, *row["queue"]]
    runtime.store.transaction(rearrange)
    release.set()
    worker.join(10)
    row = runtime.store.read()["tasks"][child]
    assert len(paired(row)) == 1 and paired(row)[0].get("sha256")
    assert [call["call_id"] for call in row["queue"]] == ([] if queue_state == "empty" else ["other"])
