"""S06-I26 bounded aborts, nested deadlines and single-snapshot collection."""
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_agent_streamed_reads import setup, event, raw
from rynmesh.agent_runtime import output_policy, streamed_reads
from rynmesh.agent_runtime.protocol import model_retry_authority
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.streamed_reads import NestedReadDispatch, StreamedReads
from rynmesh.agent_runtime.stream_tools import StreamToolCoordinator


def test_t1_stopped_uncooperative_read_is_abandoned_after_ten_seconds(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def invoke(*args):
        entered.set()
        release.wait(30)  # Deliberately ignores the supplied cancellation callback.
        return {"late": "MUST_NOT_PUBLISH"}
    runtime, adapter, selected, stopped = setup(tmp_path, invoke)
    adapter.on_event(event())
    assert entered.wait(3)
    stopped.set()
    runtime.action(selected["id"], {"action": "stop"})
    started = time.monotonic()
    try:
        records = adapter.abort()
        elapsed = time.monotonic() - started
        assert 9 <= elapsed < 12
        assert type(adapter.coordinator.results[0]["error"]).__name__ == "StreamReadAbandoned"
        assert records[0]["result"]["error"] == "agent_stream_read_interrupted"
        before = runtime.store.read()["tasks"][selected["id"]]
        assert before["receipts"][0]["status"] == "failed"
        assert not adapter.publish(before, records)
    finally:
        release.set()
        future = adapter.coordinator.started[0]["future"]
        future.result(timeout=5)
        finished.set()
    after = runtime.store.read()["tasks"][selected["id"]]
    assert finished.is_set() and after["messages"] == before["messages"]
    assert after["receipts"][0]["result"] == before["receipts"][0]["result"]


def test_t2_normal_drain_waits_for_real_result(tmp_path):
    entered, release, drained = threading.Event(), threading.Event(), threading.Event()
    def invoke(*args):
        entered.set()
        assert release.wait(3)
        return {"normal": True}
    _, adapter, _, _ = setup(tmp_path, invoke)
    adapter.on_event(event())
    assert entered.wait(3)
    result = []
    def collect():
        result.extend(adapter.drain([raw()]))
        drained.set()
    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert not drained.wait(0.1)
    finally:
        release.set()
        thread.join(5)
    assert drained.is_set() and result[0]["result"] == {"normal": True}


@pytest.mark.parametrize("method", ["close", "cancel"])
def test_t1_close_and_cancel_bound_wait_even_without_external_cancel(monkeypatch, method):
    monkeypatch.setattr(output_policy, "STREAM_READ_CANCEL_TIMEOUT_S", 0.02, raising=False)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    def run(prepared):
        entered.set()
        release.wait(3)
        return "late"
    stream = StreamToolCoordinator(lambda value: {"name": "read", "effect": "read"},
        lambda *args: {}, run, parallel_reads={"read"})
    stream.on_event({"type": "tool_call_complete", "index": 0, "call": {"id": "call"}})
    assert entered.wait(1)
    results = []
    def close():
        results.extend(getattr(stream, method)())
        done.set()
    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert done.wait(0.5)
        assert type(results[0]["error"]).__name__ == "StreamReadAbandoned"
    finally:
        release.set()
        thread.join(3)


@pytest.mark.parametrize("maximum, elapsed", [(1200, 601), (60, 61)])
def test_t3_nested_reads_observe_task_or_six_hundred_second_deadline(tmp_path, monkeypatch, maximum, elapsed):
    clock = [1000.0]
    monkeypatch.setattr(streamed_reads, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    runtime, _, selected, _ = setup(tmp_path)
    def claim(data):
        row = data["tasks"][selected["id"]]
        row["limits"]["max_seconds"] = maximum
        row["in_flight"] = {"name": "ryn_code_exec", "kind": "tool", "dispatched": True,
                            "operation_id": "outer", "authority": model_retry_authority(data["settings"])}
        return copy.deepcopy(row)
    selected = runtime.store.transaction(claim)
    dispatch = NestedReadDispatch(runtime, selected, runtime.tools.specs(), lambda: False, "outer")
    def invoke(name, arguments, context, options):
        clock[0] += elapsed
        if options["cancelled"]():
            raise AgentToolRejected(options["cancelled"].reason)
        return {"late": True}
    runtime.tools.invoke = invoke
    with pytest.raises(AgentToolRejected, match="agent_time_budget_exhausted"):
        dispatch("ryn_file_read", {"path": "a"}, operation_id="nested", cancelled=lambda: False)
    receipt = runtime.store.read()["tasks"][selected["id"]]["receipts"][-1]
    assert receipt["status"] == "failed" and receipt["result"]["error"] == "agent_time_budget_exhausted"


def test_t4_ten_completed_records_use_one_store_snapshot():
    journal = [{"state": "complete", "call": {"operation_id": str(i)}, "result": {"index": i}} for i in range(10)]
    read = Mock(return_value={"tasks": {"task": {"stream_read_journal": journal}}})
    adapter = StreamedReads.__new__(StreamedReads)
    adapter.runtime = SimpleNamespace(store=SimpleNamespace(read=read))
    adapter.selected, adapter.records = {"id": "task"}, None
    records = [{"admission": {"call": {"operation_id": str(i)}}, "error": None} for i in range(10)]
    assert [row["result"]["index"] for row in adapter._collect(records)] == list(range(10))
    assert read.call_count == 1


def test_t4_empty_drain_does_not_add_a_store_read():
    adapter = StreamedReads.__new__(StreamedReads)
    read = Mock(return_value={"tasks": {}})
    adapter.runtime = SimpleNamespace(store=SimpleNamespace(read=read))
    adapter.selected, adapter.records = {"id": "task"}, None
    assert adapter._collect([]) == []
    assert read.call_count == 0


def test_t4_snapshot_refreshes_once_after_completing_an_error_record():
    data = {"tasks": {"task": {"stream_read_journal": [{"call": {"operation_id": "op"}, "state": "intent"}]}}}
    read = Mock(side_effect=lambda: copy.deepcopy(data))
    adapter = StreamedReads.__new__(StreamedReads)
    adapter.runtime = SimpleNamespace(store=SimpleNamespace(read=read))
    adapter.selected, adapter.records = {"id": "task"}, None
    def complete(prepared, outcome, elapsed):
        data["tasks"]["task"]["stream_read_journal"][0].update(state="complete", result={"fresh": True})
    adapter._complete_record = complete
    records = [{"admission": {"call": {"operation_id": "op"}}, "error": RuntimeError("interrupted")}]
    assert adapter._collect(records)[0]["result"] == {"fresh": True}
    assert read.call_count == 2
