"""Actual run_once/control/restart paths around production workstation reads.

The inference boundary is scripted; file IO, encrypted journals, authorization,
receipts, owner controls and runtime reconciliation are the production paths.
"""
from __future__ import annotations

import copy
import json
import shutil
import threading
from types import SimpleNamespace

import pytest
from test_agent_codex_loop import WorkstationTools, workstation
from test_agent_runtime import Catalog, Model, assistant
from test_agent_runtime_control import control, steer

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore

EVIDENCE = "AUTHORED_STREAM_READ_EVIDENCE_20261018"
LATE_TEXT = "AUTHORED_LATE_MODEL_RESPONSE_MUST_BE_DISCARDED"


def _read_call():
    return assistant(name="ryn_file_read", args={"path": "source.txt"},
                     call_id="stream-read-control")["message"]["tool_calls"][0]


def _streaming_runtime(tmp_path, monkeypatch, phase):
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime, model, workspace = workstation(tmp_path)
    (workspace / "source.txt").write_text(EVIDENCE, encoding="utf-8")
    io_done, journal_done = threading.Event(), threading.Event()
    model_entered, model_release, tool_release = threading.Event(), threading.Event(), threading.Event()
    executions, requests, failures = [], [], []
    execute, transaction = runtime.tools.execute, runtime.store.transaction

    def tracked_execute(name, *args, **options):
        result = execute(name, *args, **options)
        if name == "ryn_file_read":
            executions.append(options["operation_id"])
            io_done.set()
            if phase == "intent":
                assert tool_release.wait(8), "Test must reclaim the admitted read worker"
        return result

    def tracked_transaction(mutate):
        result = transaction(mutate)
        complete = runtime.store.inspect(lambda data: any(
            entry.get("state") == "complete" for row in data["tasks"].values()
            for entry in row.get("stream_read_journal", [])))
        if complete:
            journal_done.set()
        return result

    def streaming(model_id, messages, specs, cancelled, on_event):
        requests.append(copy.deepcopy(messages))
        if len(requests) > 1:
            return assistant("Follow the latest owner instruction.")
        on_event({"type": "tool_call_complete", "index": 0, "call": _read_call()})
        model_entered.set()
        assert model_release.wait(8), "Test must reclaim the scripted inference thread"
        return {"message": {"role": "assistant", "content": LATE_TEXT,
                            "tool_calls": [_read_call()]}, "finish_reason": "tool_calls"}

    monkeypatch.setattr(runtime.tools, "execute", tracked_execute)
    monkeypatch.setattr(runtime.store, "transaction", tracked_transaction)
    monkeypatch.setattr(model, "complete", streaming)
    row = runtime.create({"message": "Read source.txt and report its contents."})

    def run():
        try:
            runtime.run_once()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run, name="ryn-control-test")
    worker.start()
    return SimpleNamespace(runtime=runtime, model=model, workspace=workspace, row=row, worker=worker,
        io_done=io_done, journal_done=journal_done, model_entered=model_entered,
        model_release=model_release, tool_release=tool_release,
        executions=executions, requests=requests, failures=failures)


def _wait_for_read(fixture, phase):
    assert fixture.model_entered.wait(5), "run_once must enter scripted streaming inference"
    assert fixture.io_done.wait(5), "Production file read must finish while inference is still open"
    if phase == "complete":
        assert fixture.journal_done.wait(5), "Execution result must reach the durable encrypted journal"
    saved = fixture.runtime.store.read()["tasks"][fixture.row["id"]]
    assert saved["in_flight"]["kind"] == "model" and saved["in_flight"]["dispatched"]
    assert len(saved["stream_read_journal"]) == 1
    assert saved["stream_read_journal"][0]["state"] == phase
    assert not any(message.get("tool_calls") or message["role"] == "tool" for message in saved["messages"])
    assert not any(event["type"] == "model_response_completed" for event in saved["events"])
    return saved


def _release_and_join(fixture):
    fixture.tool_release.set()
    fixture.model_release.set()
    fixture.worker.join(8)
    assert not fixture.worker.is_alive(), "run_once must join the admitted worker on control/error"
    assert fixture.failures == []


@pytest.mark.parametrize("phase", ["intent", "complete"])
@pytest.mark.parametrize("action", ["cancel", "steer"])
def test_run_once_read_control_preserves_receipt_but_discards_stale_publication(tmp_path, monkeypatch, action, phase):
    fixture = _streaming_runtime(tmp_path, monkeypatch, phase)
    try:
        before = _wait_for_read(fixture, phase)
        if action == "cancel":
            control(fixture.runtime, fixture.row["id"], "cancel")
        else:
            steer(fixture.runtime, fixture.row["id"], "Ignore the old read; answer only CURRENT_OWNER_TURN.")
        _release_and_join(fixture)
        saved = fixture.runtime.store.read()["tasks"][fixture.row["id"]]
        assert len(fixture.executions) == 1
        assert len(saved["receipts"]) == 1
        receipt = saved["receipts"][0]
        assert receipt["status"] == "confirmed" and EVIDENCE in json.dumps(receipt["result"])
        assert receipt["goal_revision"] == before["goal"]["revision"]
        assert receipt["context_epoch"] == before.get("context_epoch", 0)
        assert saved["control_revision"] != before["control_revision"]
        assert saved["in_flight"] is None and saved["queue"] == []
        assert not any(message.get("tool_calls") or message["role"] == "tool" for message in saved["messages"])
        assert EVIDENCE not in json.dumps(saved["messages"]) and LATE_TEXT not in json.dumps(saved["messages"])
        assert not saved.get("observed_files"), "Old-authority file observations cannot become current preconditions"
        if action == "cancel":
            assert saved["status"] == "cancelled"
            fixture.runtime.run_once()
            assert len(fixture.requests) == 1
        else:
            assert saved["goal"]["revision"] == before["goal"]["revision"] + 1 and saved["status"] == "queued"
            fixture.runtime.run_once()
            assert fixture.runtime.get(fixture.row["id"])["status"] == "completed"
            assert len(fixture.requests) == 2
            assert "CURRENT_OWNER_TURN" in json.dumps(fixture.requests[1])
            assert EVIDENCE not in json.dumps(fixture.requests[1]) and LATE_TEXT not in json.dumps(fixture.requests[1])
        assert len(fixture.executions) == 1
    finally:
        _release_and_join(fixture)
        fixture.runtime.tools.local.processes.close()


@pytest.mark.parametrize("phase", ["intent", "complete"])
def test_service_restart_recovers_actual_unfinished_model_journal_without_reexecuting_read(tmp_path, monkeypatch, phase):
    fixture = _streaming_runtime(tmp_path / "original", monkeypatch, phase)
    restarted = None
    try:
        before = _wait_for_read(fixture, phase)
        snapshot_root = tmp_path / "restarted-encrypted-store"
        # Capture the committed store while inference has no final response.
        # No fabricated row/receipt is injected into this restart checkpoint.
        shutil.copytree(fixture.runtime.store.root, snapshot_root)
        control(fixture.runtime, fixture.row["id"], "cancel")
        _release_and_join(fixture)
        store = AgentStore(snapshot_root, fixture.runtime.store.key)
        tools = WorkstationTools(SimpleNamespace(store=store), snapshot_root)
        replayed = []
        execute = tools.execute
        def tracked(name, *args, **options):
            replayed.append(name)
            return execute(name, *args, **options)
        tools.execute = tracked
        model = Model(assistant("Recovered reads accounted for; no new read is needed."))
        restarted = AgentRuntime(store, model, tools, Catalog())
        restarted.reconcile()
        recovered = store.read()["tasks"][fixture.row["id"]]
        assert recovered["status"] == "interrupted" and recovered["in_flight"] is None
        assert recovered["queue"] == [] and replayed == []
        calls = [call for message in recovered["messages"] for call in message.get("tool_calls", [])]
        results = [message for message in recovered["messages"] if message["role"] == "tool"]
        assert [call["id"] for call in calls] == [message["tool_call_id"] for message in results] == ["stream-read-control"]
        assert "partial model response" in recovered["messages"][-2]["content"]
        assert LATE_TEXT not in json.dumps(recovered["messages"])
        assert len(recovered["receipts"]) == 1
        assert recovered["receipts"][0]["operation_id"] == before["stream_read_journal"][0]["call"]["operation_id"]
        result = json.loads(results[0]["content"])
        if phase == "complete":
            assert EVIDENCE in json.dumps(result) and recovered["receipts"][0]["status"] == "confirmed"
        else:
            assert result == {"error": "agent_stream_read_interrupted", "no_mutation": True}
            assert "not_executed" not in result and recovered["receipts"][0]["status"] == "failed"
        restarted.reconcile()
        assert len([message for message in store.read()["tasks"][fixture.row["id"]]["messages"]
                    if message["role"] == "tool"]) == 1
        control(restarted, fixture.row["id"], "resume")
        restarted.run_once()
        assert restarted.get(fixture.row["id"])["status"] == "completed"
        assert len(model.calls) == 1 and replayed == [] and len(fixture.executions) == 1
        supplied = model.calls[0][1]
        assert len([message for message in supplied if message.get("tool_call_id") == "stream-read-control"]) == 1
    finally:
        _release_and_join(fixture)
        fixture.runtime.tools.local.processes.close()
        if restarted is not None:
            restarted.tools.local.processes.close()


def test_stream_read_falls_back_after_full_model_seconds_reservation_then_executes_once(tmp_path, monkeypatch):
    runtime, model, workspace = workstation(tmp_path)
    runtime.update_settings({"max_seconds": 10})
    (workspace / "source.txt").write_text(EVIDENCE, encoding="utf-8")
    calls, executions, during_stream = [], [], []
    execute = runtime.tools.execute
    def tracked(name, *args, **options):
        executions.append(options["operation_id"])
        return execute(name, *args, **options)
    def streaming(model_id, messages, specs, cancelled, on_event):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            on_event({"type": "tool_call_complete", "index": 0, "call": _read_call()})
            saved = runtime.store.read()["tasks"][row["id"]]
            model_usage = [record for record in saved["budget"]["records"] if record["kind"] == "model"]
            assert len(model_usage) == 1 and model_usage[0]["status"] == "reserved"
            assert model_usage[0]["charged_seconds"] == 10
            during_stream.append(copy.deepcopy(saved))
            assert executions == [] and not saved.get("stream_read_journal")
            return {"message": {"role": "assistant", "content": "", "tool_calls": [_read_call()]},
                    "finish_reason": "tool_calls", "reported_usage": {"input_tokens": 17, "output_tokens": 5}}
        assert EVIDENCE in messages[-1]["content"]
        return {**assistant("Read completed after model budget settlement."),
                "reported_usage": {"input_tokens": 23, "output_tokens": 7}}
    monkeypatch.setattr(runtime.tools, "execute", tracked)
    monkeypatch.setattr(model, "complete", streaming)
    row = runtime.create({"message": "Read source.txt once.", "budget_limits": {"max_total_seconds": 10}})
    try:
        runtime.run_once()
        pending = runtime.store.read()["tasks"][row["id"]]
        assert pending["status"] == "queued" and pending["error"] is None
        assert len(pending["queue"]) == 1 and executions == [] and len(during_stream) == 1
        assert pending["budget"]["records"][0]["status"] == "settled"
        runtime.run_once()
        runtime.run_once()
        saved = runtime.store.read()["tasks"][row["id"]]
        assert saved["status"] == "completed" and saved["error"] is None
        assert len(executions) == 1 and len(saved["receipts"]) == 1
        assert saved["receipts"][0]["status"] == "confirmed"
        assert not any(event["type"] == "model_retry_scheduled" for event in saved["events"])
        records = saved["budget"]["records"]
        assert [record["kind"] for record in records] == ["model", "tool", "model"]
        assert [(record["input_tokens"], record["output_tokens"]) for record in records if record["kind"] == "model"] == [(17, 5), (23, 7)]
        assert all(record["status"] == "settled" for record in records)
    finally:
        runtime.tools.local.processes.close()


def test_partial_read_seconds_reservation_has_own_deadline_and_keeps_actual_elapsed(tmp_path, monkeypatch):
    from rynmesh.agent_runtime import loop as loop_module
    from rynmesh.agent_runtime import runtime as runtime_module
    from rynmesh.agent_runtime import streamed_reads as reads_module

    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    for module in (loop_module, runtime_module, reads_module):
        monkeypatch.setattr(module, "time", fake_time)
    runtime, model, _ = workstation(tmp_path)
    runtime.update_settings({"max_seconds": 10})
    started = [threading.Event(), threading.Event()]
    release = threading.Event()
    local_checks, reservations, global_checks = {}, [], []
    proposals = [assistant(name="ryn_file_read", args={"path": f"file{index}.txt"},
                          call_id=f"partial-budget-{index}")["message"]["tool_calls"][0] for index in range(2)]
    model_requests = []

    def cooperative(name, args, *, cancelled, **options):
        index = int(args["path"][4])
        started[index].set()
        assert release.wait(5), "The test must release both admitted reads"
        stopped = cancelled()
        local_checks[index] = (stopped, getattr(cancelled, "reason", None))
        if stopped:
            raise AgentError(cancelled.reason)
        return {"path": args["path"], "read_complete": True}

    def streaming(model_id, messages, specs, cancelled, on_event):
        model_requests.append(copy.deepcopy(messages))
        if len(model_requests) > 1:
            return {**assistant("One read completed; the second hit its own budget deadline."),
                    "reported_usage": {"input_tokens": 31, "output_tokens": 9}}
        for index, proposal in enumerate(proposals):
            on_event({"type": "tool_call_complete", "index": index, "call": proposal})
        assert all(event.wait(5) for event in started)
        saved = runtime.store.read()["tasks"][row["id"]]
        reservations.extend(entry["reserved_seconds"] for entry in saved["stream_read_journal"])
        assert reservations == [10, 2]
        assert [record["charged_seconds"] for record in saved["budget"]["records"]] == [10, 10, 2]
        clock[0] = 3.0  # Both still fit the enclosing 10-second model tick.
        global_checks.append(cancelled())
        release.set()
        return {"message": {"role": "assistant", "content": "", "tool_calls": proposals},
                "finish_reason": "tool_calls", "reported_usage": {"input_tokens": 19, "output_tokens": 6}}

    monkeypatch.setattr(runtime.tools, "execute", cooperative)
    monkeypatch.setattr(model, "complete", streaming)
    row = runtime.create({"message": "Inspect two independent files.", "budget_limits": {"max_total_seconds": 22}})
    try:
        runtime.run_once()
        assert global_checks == [False]
        assert local_checks[0][0] is False
        assert local_checks[1] == (True, "agent_time_budget_exhausted")
        saved = runtime.store.read()["tasks"][row["id"]]
        tools = [record for record in saved["budget"]["records"] if record["kind"] == "tool"]
        assert len(tools) == 2 and all(record["status"] == "settled" for record in tools)
        assert [record["elapsed_seconds"] for record in tools] == [3.0, 3.0]
        assert [record["charged_seconds"] for record in tools] == [3.0, 3.0]
        # Cooperative polling overshot the second reservation by one second;
        # accounting must report that actual duration rather than truncate it.
        assert tools[1]["charged_seconds"] > reservations[1]
        assert saved["receipts"][1]["status"] == "failed"
        assert saved["receipts"][1]["result"]["error"] == "agent_time_budget_exhausted"
        runtime.run_once()
        saved = runtime.store.read()["tasks"][row["id"]]
        assert saved["status"] == "completed" and len(model_requests) == 2
        models = [record for record in saved["budget"]["records"] if record["kind"] == "model"]
        assert [(record["input_tokens"], record["output_tokens"]) for record in models] == [(19, 6), (31, 9)]
    finally:
        release.set()
        runtime.tools.local.processes.close()
