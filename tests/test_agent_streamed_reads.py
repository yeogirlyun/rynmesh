"""Encrypted host journals for completed-item read overlap; no real providers."""
from __future__ import annotations

import copy
import json
import threading

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.protocol import model_retry_authority
from rynmesh.agent_runtime.results import read
from rynmesh.agent_runtime.store import AgentError
from rynmesh.agent_runtime.stream_tools import StreamToolMismatch
from rynmesh.agent_runtime.streamed_reads import StreamedReads, recover_stream_reads


class Reads:
    def __init__(self, invoke=None):
        self.calls, self.invoke, self.explicit = [], invoke, False
        self.parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}

    def specs(self):
        return [{"type": "function", "function": {"name": name, "parameters": self.parameters}}
                for name in ("ryn_file_read", "write")]

    def metadata(self, name):
        return {"effect": "write" if name == "write" else "read", "scope_context_supported": True,
                "requires_explicit_approval": self.explicit}

    def execute(self, name, arguments, *, context, **kwargs):
        self.calls.append((name, arguments))
        if self.invoke:
            return self.invoke(name, arguments, context, kwargs)
        context["_deferred_observed_files"][arguments["path"]] = "test-sha"
        return {"path": arguments["path"], "api_key": "redact-me"}


def raw(index=0, name="ryn_file_read", path=None):
    return {"id": f"call_{index}", "type": "function", "function": {"name": name,
            "arguments": json.dumps({"path": path or f"file{index}.txt"})}}


def event(index=0, **kwargs):
    return {"type": "tool_call_complete", "index": index, "call": raw(index, **kwargs)}


def setup(tmp_path, invoke=None):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.tools = Reads(invoke)
    task_id = runtime.create({"message": "Read authored test files"})["id"]
    def claim(data):
        row = data["tasks"][task_id]
        row.update(status="running", in_flight={"kind": "model", "dispatched": True, "request_id": "request_fixture"})
        row.setdefault("context_epoch", 0)
        row["steps"] += 1
        return copy.deepcopy(row)
    selected = runtime.store.transaction(claim)
    selected.update(_run_control_revision=selected["control_revision"], _request_id="request_fixture",
                    _model_retry_authority=model_retry_authority(runtime.store.read()["settings"]))
    stopped = threading.Event()
    adapter = StreamedReads(runtime, selected, runtime.tools.specs(), stopped.is_set, {"ryn_file_read"})
    return runtime, adapter, selected, stopped


def test_early_read_durable_before_dispatch_then_publish_only_after_accepted_assistant(tmp_path):
    started, release = threading.Event(), threading.Event()
    def invoke(name, arguments, context, options):
        row = runtime.store.read()["tasks"][selected["id"]]
        assert row["stream_read_journal"][0]["state"] == "intent"
        assert row["receipts"][0]["status"] == "intent" and row["in_flight"]["kind"] == "model"
        assert row["messages"][-1]["role"] == "user"
        context["_deferred_observed_files"]["file0.txt"] = "test-sha"
        started.set()
        assert release.wait(2)
        return {"api_key": "redact-me", "ok": True}
    runtime, adapter, selected, _ = setup(tmp_path, invoke)
    adapter.on_event(event())
    assert started.wait(1)
    release.set()
    records = adapter.drain([raw()])
    row = runtime.store.read()["tasks"][selected["id"]]
    assert row["in_flight"]["kind"] == "model" and row["receipts"][0]["status"] == "confirmed"
    assert records[0]["result"]["api_key"] == "[redacted]"
    assert not adapter.publish(row, records) and not row.get("observed_files")
    row["messages"].append({"role": "assistant", "content": "", "tool_calls": [raw()]})
    assert adapter.publish(row, records)
    assert row["messages"][-1]["tool_call_id"] == "call_0"
    assert row["observed_files"]["file0.txt"]["sha256"] == "test-sha"
    assert row["stream_read_journal"] == [] and len(runtime.tools.calls) == 1
    assert adapter.publish(row, records) and len([m for m in row["messages"] if m["role"] == "tool"]) == 1


def test_reverse_completion_keeps_request_order_and_budget_settles(tmp_path):
    done = threading.Event()
    completed = []
    def invoke(name, arguments, context, options):
        if arguments["path"] == "file0.txt":
            assert done.wait(2)
        completed.append(arguments["path"])
        if arguments["path"] == "file1.txt":
            done.set()
        return {"path": arguments["path"]}
    runtime, adapter, selected, _ = setup(tmp_path, invoke)
    adapter.on_event(event(0))
    adapter.on_event(event(1))
    records = adapter.drain([raw(0), raw(1)])
    assert completed == ["file1.txt", "file0.txt"]
    assert [record["call"]["call_id"] for record in records] == ["call_0", "call_1"]
    row = runtime.store.read()["tasks"][selected["id"]]
    assert all(record["status"] == "settled" for record in row["budget"]["records"])


@pytest.mark.parametrize("change", ["explicit", "schema", "authority", "epoch", "cancel"])
def test_new_admission_rechecks_permissions_schema_and_control(tmp_path, change):
    runtime, adapter, selected, stopped = setup(tmp_path)
    if change == "explicit":
        runtime.tools.explicit = True
    elif change == "schema":
        runtime.tools.parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    elif change == "cancel":
        stopped.set()
    else:
        def change_data(data):
            if change == "authority":
                data["settings"]["permission_mode"] = "read_only"
            else:
                data["tasks"][selected["id"]]["context_epoch"] += 1
        runtime.store.transaction(change_data)
    adapter.on_event(event())
    assert adapter.abort() == [] and runtime.tools.calls == []


def test_abort_waits_and_retains_receipts_without_stale_epoch_publication(tmp_path):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    def invoke(*args):
        started.set()
        assert release.wait(2)
        return {"ok": True}
    runtime, adapter, selected, stopped = setup(tmp_path, invoke)
    adapter.on_event(event())
    assert started.wait(1)
    stopped.set()
    def reset(data):
        row = data["tasks"][selected["id"]]
        row["context_epoch"] += 1
        row["control_revision"] += 1
        row["status"] = "paused"
    runtime.store.transaction(reset)
    collected = []
    def abort():
        collected.extend(adapter.abort())
        closed.set()
    thread = threading.Thread(target=abort)
    thread.start()
    assert not closed.wait(0.05)
    release.set()
    thread.join(2)
    assert closed.is_set() and len(collected) == 1
    row = runtime.store.read()["tasks"][selected["id"]]
    assert row["receipts"][0]["status"] == "confirmed"
    assert row["receipts"][0]["context_epoch"] == selected["context_epoch"]
    assert not adapter.publish(row, collected) and not adapter.record_recovered(row, collected)
    assert row["messages"][-1]["role"] == "user"


def test_mismatch_joins_and_returns_receipts_recoverable_as_closed_partial_exchange(tmp_path):
    runtime, adapter, selected, _ = setup(tmp_path)
    adapter.on_event(event())
    with pytest.raises(StreamToolMismatch) as info:
        adapter.drain([raw(path="different.txt")])
    records = info.value.stream_results
    row = runtime.store.read()["tasks"][selected["id"]]
    assert len(records) == 1 and len(runtime.tools.calls) == 1
    assert adapter.record_recovered(row, records)
    assert "partial model response" in row["messages"][-2]["content"]
    assert row["messages"][-1]["tool_call_id"] == "call_0"


def test_large_output_is_encrypted_ref_and_worker_errors_are_safe(tmp_path):
    runtime, adapter, selected, _ = setup(tmp_path, lambda *args: {"text": "a" * 60000})
    adapter.on_event(event())
    record = adapter.drain([raw()])[0]
    row = runtime.store.read()["tasks"][selected["id"]]
    assert record["reference"]["id"] == record["result"]["result_ref"]
    value, _ = read(runtime.store, row, record["reference"]["id"])
    assert len(value["text"]) == 60000
    runtime2, adapter2, selected2, _ = setup(tmp_path / "error", lambda *args: (_ for _ in ()).throw(AgentError("private token must not leak")))
    adapter2.on_event(event())
    failed = adapter2.abort()[0]
    assert failed["result"] == {"error": "agent_operation_failed", "not_executed": False, "no_mutation": True}
    assert runtime2.store.read()["tasks"][selected2["id"]]["receipts"][0]["status"] == "failed"


def test_restart_unfinished_read_is_interrupted_no_mutation_without_redispatch(tmp_path):
    runtime, adapter, selected, _ = setup(tmp_path)
    adapter.raw[0] = raw()
    prepared = adapter._admit(0, adapter._normalize(raw()))
    assert prepared is not None and runtime.tools.calls == []
    row = runtime.store.read()["tasks"][selected["id"]]
    records = recover_stream_reads(row, allow_publish=True)
    assert records[0]["result"] == {"error": "agent_stream_read_interrupted", "no_mutation": True}
    assert runtime.tools.calls == [] and row["receipts"][0]["status"] == "failed"
    assert row["messages"][-1]["tool_call_id"] == "call_0"
    assert recover_stream_reads(row) == []


def test_abort_preserves_joined_execution_outcome_when_persistence_fails(tmp_path, monkeypatch):
    runtime, adapter, selected, _ = setup(tmp_path)
    def unavailable(*args):
        raise AgentError("agent_storage_limit")
    monkeypatch.setattr(adapter, "_complete_record", unavailable)
    adapter.on_event(event())
    with pytest.raises(AgentError, match="agent_stream_read_persistence_failed") as info:
        adapter.drain([raw()])
    records = info.value.stream_results
    assert len(records) == 1 and records[0]["result"]["path"] == "file0.txt"
    assert records[0]["persistence_error"] == "agent_storage_limit"
    assert len(runtime.tools.calls) == 1 and adapter.abort() == records
    row = runtime.store.read()["tasks"][selected["id"]]
    row["messages"].append({"role": "assistant", "content": "", "tool_calls": [raw()]})
    assert not adapter.publish(row, records)


def test_recovery_defaults_to_receipts_only_without_authority_grant(tmp_path):
    runtime, adapter, selected, _ = setup(tmp_path)
    adapter.raw[0] = raw()
    adapter._admit(0, adapter._normalize(raw()))
    row = runtime.store.read()["tasks"][selected["id"]]
    records = recover_stream_reads(row)
    assert records[0]["state"] == "complete"
    assert row["messages"][-1]["role"] == "user" and len(row["stream_read_journal"]) == 1
    assert runtime.tools.calls == []
