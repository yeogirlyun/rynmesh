"""Only admitted read prefixes can overlap a model stream."""
from __future__ import annotations

import threading

import pytest

from rynmesh.agent_runtime.stream_tools import StreamToolCoordinator, StreamToolMismatch


def raw(index, name="read"):
    return {"id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": "{}"}}


def event(index, name="read"):
    return {"type": "tool_call_complete", "index": index, "call": raw(index, name)}


def coordinator(run, *, admit=None, normalize=None, **options):
    return StreamToolCoordinator(normalize or (lambda item: {"name": item["function"]["name"], "effect": "read", "raw": item}),
                                 admit or (lambda index, call: {**call, "index": index, "durably_admitted": True}),
                                 run, parallel_reads={"read"}, **options)


def test_complete_item_really_starts_before_model_stream_ends():
    running, release = threading.Event(), threading.Event()
    def run(prepared):
        assert prepared["durably_admitted"]
        running.set()
        assert release.wait(2)
        return "read result"
    stream = coordinator(run)
    stream.on_event(event(0))
    assert running.wait(1) and stream.results is None
    release.set()
    records = stream.drain([raw(0)])
    assert records[0]["result"] == "read result" and records[0]["error"] is None


def test_reverse_completion_is_drained_in_request_index_order():
    first_running, second_done = threading.Event(), threading.Event()
    completed = []
    def run(prepared):
        if prepared["index"] == 0:
            first_running.set()
            assert second_done.wait(2)
        completed.append(prepared["index"])
        if prepared["index"] == 1:
            second_done.set()
        return prepared["index"]
    stream = coordinator(run)
    stream.on_event(event(0))
    assert first_running.wait(1)
    stream.on_event(event(1))
    records = stream.drain([raw(0), raw(1)])
    assert completed == [1, 0]
    assert [record["result"] for record in records] == [0, 1]


@pytest.mark.parametrize("blocked", [{"name": "write", "effect": "write"},
                                    {"name": "read", "effect": "read", "internal": True},
                                    {"name": "read", "effect": "read", "error_result": {"error": "invalid"}}])
def test_first_noneligible_item_blocks_later_reads_and_index_gaps(blocked):
    calls = []
    stream = coordinator(lambda prepared: calls.append(prepared),
                         normalize=lambda item: blocked if item["id"] == "call_0" else {"name": "read", "effect": "read"})
    stream.on_event(event(1))
    assert calls == [] and stream.started == {}
    stream.on_event(event(0))
    assert stream.blocked and stream.drain([raw(0), raw(1)]) == [] and calls == []


def test_failed_durable_admission_submits_nothing():
    calls = []
    def admit(index, call):
        raise RuntimeError("admission failed")
    stream = coordinator(lambda prepared: calls.append(prepared), admit=admit)
    with pytest.raises(RuntimeError, match="admission failed") as caught:
        stream.on_event(event(0))
    assert stream.closed and caught.value.stream_results == [] and calls == []


def test_cancel_waits_and_retains_the_started_read_receipt():
    running, release = threading.Event(), threading.Event()
    def run(prepared):
        running.set()
        assert release.wait(2)
        return "receipt after cancellation"
    stream = coordinator(run)
    stream.on_event(event(0))
    assert running.wait(1)
    timer = threading.Timer(0.02, release.set)
    timer.start()
    records = stream.cancel()
    timer.join()
    stream.on_event(event(1))
    assert stream.closed and len(records) == 1 and records[0]["result"] == "receipt after cancellation"
    assert list(stream.started) == [0]


def test_raw_final_mismatch_waits_and_exposes_receipts_without_rerun():
    calls = []
    stream = coordinator(lambda prepared: calls.append(prepared["index"]) or "read receipt")
    stream.on_event(event(0))
    different = raw(0)
    different["function"]["arguments"] = "{ }"
    with pytest.raises(StreamToolMismatch) as caught:
        stream.drain([different])
    assert stream.closed and calls == [0]
    assert caught.value.stream_results[0]["result"] == "read receipt"
    assert stream.close() == caught.value.stream_results


def test_worker_errors_are_returned_to_host_instead_of_dropped():
    def run(prepared):
        raise ValueError("bounded fixture failure")
    stream = coordinator(run)
    stream.on_event(event(0))
    records = stream.drain([raw(0)])
    assert isinstance(records[0]["error"], ValueError) and records[0]["result"] is None


def test_worker_creation_failure_retains_the_durably_admitted_intent(monkeypatch):
    def unavailable(**options):
        raise RuntimeError("worker unavailable")
    monkeypatch.setattr("rynmesh.agent_runtime.stream_tools.ThreadPoolExecutor", unavailable)
    stream = coordinator(lambda prepared: pytest.fail("must not run"))
    with pytest.raises(RuntimeError, match="worker unavailable") as caught:
        stream.on_event(event(0))
    assert caught.value.stream_results[0]["admission"]["durably_admitted"]
    assert isinstance(caught.value.stream_results[0]["error"], RuntimeError)
