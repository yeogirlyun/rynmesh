"""Batch size, host scheduling and nested-tool accounting regressions.

Model replies are scripted; code cells execute in the actual Node worker.
"""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import code_mode, prompt
from rynmesh.agent_runtime.model_events import ModelEventError, ModelEventStream
from rynmesh.agent_runtime.output_policy import MAX_RAW_RESPONSE_BYTES
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.streamed_reads import NestedReadDispatch, recover_stream_reads
from rynmesh.agent_runtime.tool_dispatch import ToolDispatchGate, supports_parallel
from rynmesh.llm_package.codex_tools import _response_schema


def raw(index, name="ryn_file_read"):
    return assistant(name=name, args={"path": str(index)}, call_id=f"call_{index}")["message"]["tool_calls"][0]


@pytest.mark.parametrize("wire", ["chat", "responses", "completed"])
def test_forty_streamed_calls_survive_all_event_protocols(wire):
    calls = [raw(i) for i in range(40)]
    events = []
    stream = ModelEventStream(events.append)
    for i, call in enumerate(calls):
        if wire == "chat":
            stream.feed({"choices": [{"delta": {"tool_calls": [{"index": i, **call}]}}]})
        elif wire == "responses":
            stream.feed({"type": "response.output_item.done", "output_index": i * 2,
                         "item": {"type": "reasoning"}})
            stream.feed({"type": "response.output_item.done", "output_index": i * 2 + 1,
                         "item": {"type": "function_call", "call_id": call["id"], **call["function"]}})
        else:
            stream.feed({"type": "tool_call_complete", "index": i, "call": call})
    stream.finish({"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"})
    assert [e["call"]["id"] for e in events if e["type"] == "tool_call_complete"] == [c["id"] for c in calls]
    assert events[-1]["type"] == "response_complete"


def test_incomplete_chat_fragments_are_bounded_by_total_bytes():
    stream = ModelEventStream(lambda _event: None)
    with pytest.raises(ModelEventError, match="batch limit"):
        for i in range(10):
            call = raw(i)
            call["function"]["arguments"] = "x" * (MAX_RAW_RESPONSE_BYTES // 8)
            stream.feed({"choices": [{"delta": {"tool_calls": [{"index": i, **call}]}}]})


class BatchTools:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.runner = code_mode.CodeModeRunner(self)
        self.active = self.peak = 0

    def specs(self):
        return [copy.deepcopy(code_mode.SPEC), *[
            {"type": "function", "function": {"name": name, "description": name, "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False}}}
            for name in ("ryn_file_read", "ryn_file_write", "restricted_read")]]

    def metadata(self, name):
        return {"effect": "write" if name == "ryn_file_write" else "read", "scope_context_supported": True,
                "requires_explicit_approval": name == "restricted_read"}

    def preview(self, name, arguments, **_options):
        return arguments

    def execute(self, name, arguments, *, operation_id, cancelled, context=None, **_options):
        if name == "ryn_code_exec":
            return self.runner.execute(arguments, operation_id=operation_id, cancelled=cancelled, context=context)
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((name, arguments["path"], operation_id))
        try:
            return {"text": arguments["path"]}
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def host(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.tools = BatchTools()
    try:
        yield runtime, model, runtime.tools
    finally:
        runtime.tools.runner.close()


def run_until(runtime, task_id, target):
    for _ in range(100):
        row = runtime.get(task_id)
        if row["status"] in target:
            return row
        runtime.run_once()
    pytest.fail(f"task did not reach {target}: {runtime.get(task_id)['status']}")


@pytest.mark.parametrize("streaming", [False, True])
def test_large_direct_batch_preserves_write_approval_and_does_not_replay_reads(host, streaming):
    runtime, model, tools = host
    runtime.update_settings({"permission_mode": "confirm_changes"})
    calls = [raw(i, "ryn_file_write" if i == 20 else "ryn_file_read") for i in range(40)]
    replies = iter([{"message": {"content": "", "tool_calls": calls}}, assistant("Done.")])
    def complete(_model, _messages, _specs, cancelled, on_event):
        response = next(replies)
        if streaming:
            for i, call in enumerate(response["message"].get("tool_calls", [])):
                on_event({"type": "tool_call_complete", "index": i, "call": call})
        return response
    model.complete = complete
    row = runtime.create({"message": "Read the files and make the requested change."})
    waiting = run_until(runtime, row["id"], {"waiting_approval", "failed"})
    assert waiting["status"] == "waiting_approval", waiting.get("error")
    assert sorted(int(c[1]) for c in tools.calls) == list(range(20))
    approve(runtime, row["id"])
    done = run_until(runtime, row["id"], {"completed", "failed"})
    assert done["status"] == "completed", done.get("error")
    assert sorted(int(c[1]) for c in tools.calls) == list(range(40))
    assert len({c[2] for c in tools.calls}) == 40
    assert tools.calls[20][:2] == ("ryn_file_write", "20")
    replies = [m for m in done["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in replies] == [c["id"] for c in calls]
    assert tools.peak <= 8


def cell(count):
    return {"code": f"const values = await Promise.allSettled(Array.from({{length: {count}}}, (_, i) => tools.ryn_file_read({{path: String(i)}}))); text(values.map(v => v.status));",
            "yield_time_ms": 60000}


def test_code_mode_forty_children_have_individual_durable_receipts_and_budget(host):
    runtime, model, tools = host
    model.responses = [assistant(name="ryn_code_exec", args=cell(40)), assistant("Done.")]
    row = runtime.create({"message": "Read all forty files."})
    done = run_until(runtime, row["id"], {"completed", "failed"})
    assert done["status"] == "completed", done.get("error")
    assert sorted(int(c[1]) for c in tools.calls) == list(range(40))
    saved = runtime.store.read()["tasks"][row["id"]]
    children = [r for r in saved["receipts"] if r["tool"] == "ryn_file_read"]
    assert len(children) == 40 and all(r["status"] == "confirmed" for r in children)
    assert all(r["source"] == "code_mode" and r["parent_operation_id"] for r in children)
    assert len([r for r in saved["budget"]["records"] if r["kind"] == "tool"]) == 41
    assert saved["stream_read_journal"] == []
    assert len([m for m in saved["messages"] if m["role"] == "tool"]) == 1


def test_nested_calls_cannot_bypass_explicit_total_tool_budget(host):
    runtime, model, tools = host
    model.responses = [assistant(name="ryn_code_exec", args=cell(40)), assistant("Budget reached.")]
    row = runtime.create({"message": "Read files.", "budget_limits": {"max_total_tool_calls": 6}})
    advance(runtime, 2)
    assert len(tools.calls) == 5  # The composition envelope also consumes one call.
    saved = runtime.store.read()["tasks"][row["id"]]
    assert len([r for r in saved["budget"]["records"] if r["kind"] == "tool"]) == 6
    assert all(r["status"] == "settled" for r in saved["budget"]["records"])
    assert "agent_tool_budget_exhausted" in json.dumps(saved["messages"])


def test_code_mode_catalog_excludes_approval_required_reads(host):
    _, _, tools = host
    specs, _, _ = tools.runner._read_only_catalog({})
    assert "ryn_file_read" in specs
    assert "restricted_read" not in specs and "ryn_file_write" not in specs


def test_cancelling_code_cell_stops_waiting_children_and_discards_worker(host):
    _, _, tools = host
    started, stop = threading.Event(), threading.Event()
    calls = []
    def slow_read(name, arguments, *, cancelled, **_options):
        calls.append(arguments["path"])
        started.set()
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancelled(), "cell cancellation must reach child handlers"
        raise AgentToolRejected("agent_cancelled")
    tools.execute = slow_read
    with ThreadPoolExecutor(1) as pool:
        execution = pool.submit(tools.runner.execute, cell(40), operation_id="outer",
                                cancelled=stop.is_set, context={"task_id": "cancelled-cell"})
        assert started.wait(3)
        stop.set()
        with pytest.raises(AgentToolRejected, match="agent_cancelled"):
            execution.result(timeout=5)
    assert 1 <= len(calls) <= 8
    assert "cancelled-cell" not in tools.runner._task_workers


def test_parallel_gate_limits_running_work_without_rejecting_later_calls():
    gate = ToolDispatchGate(capacity=3)
    all_slots_busy, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = completed = 0
    def read():
        nonlocal active, peak, completed
        with gate.enter("batch", parallel=True, cancelled=lambda: False):
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 3:
                    all_slots_busy.set()
            assert release.wait(3)
            with lock:
                active -= 1
                completed += 1
    with ThreadPoolExecutor(12) as pool:
        pending = [pool.submit(read) for _ in range(40)]
        assert all_slots_busy.wait(2)
        release.set()
        for future in pending:
            future.result(timeout=3)
    assert peak == 3 and completed == 40 and gate.tasks == {}


def test_parallel_capability_still_requires_read_without_approval_or_desktop_access():
    read = {"effect": "read", "supports_parallel": True}
    assert supports_parallel("extension_read", read)
    assert not supports_parallel("unknown", {"effect": "read"})
    for override in ({"effect": "write"}, {"requires_explicit_approval": True}, {"computer_access": True}):
        assert not supports_parallel("ryn_file_read", {**read, **override})


def test_restart_closes_nested_intent_without_replaying_or_fabricating_chat_calls(host):
    runtime, _, tools = host
    row = runtime.create({"message": "Read files."})
    from rynmesh.agent_runtime.protocol import model_retry_authority
    def claim(data):
        saved = data["tasks"][row["id"]]
        saved.update(status="running", in_flight={"name": "ryn_code_exec", "kind": "tool", "dispatched": True,
                     "operation_id": "outer", "authority": model_retry_authority(data["settings"])})
        return copy.deepcopy(saved)
    selected = runtime.store.transaction(claim)
    selected["_run_control_revision"] = selected["control_revision"]
    dispatch = NestedReadDispatch(runtime, selected, tools.specs(), lambda: False, "outer")
    call = dispatch._normalize(raw(0))
    dispatch.raw[0] = raw(0)
    dispatch._admit(0, call)
    before = runtime.store.read()["tasks"][row["id"]]["messages"]
    runtime.store.transaction(lambda data: recover_stream_reads(data["tasks"][row["id"]], allow_publish=True))
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["messages"] == before and tools.calls == []
    assert saved["stream_read_journal"] == []
    assert saved["receipts"][0]["status"] == "failed"


def test_shipped_pinned_prompt_loses_obsolete_limit_but_custom_prompt_survives(host):
    runtime, _, _ = host
    row = runtime.create({"message": "Inspect."})
    layers = prompt.instruction_layers(row, persisted_base=prompt._LEGACY_BATCH_SYSTEM)
    assert layers[0]["content"] == prompt.SYSTEM
    custom = prompt._LEGACY_BATCH_SYSTEM + " Custom owner rule."
    assert prompt.instruction_layers(row, persisted_base=custom)[0]["content"] == custom


def test_codex_response_schema_has_no_batch_count_limit_but_disables_absent_tools():
    schema = _response_schema({"read": {"parameters": {"type": "object", "properties": {}, "additionalProperties": False}}})
    assert "maxItems" not in schema["properties"]["tool_calls"]
    assert _response_schema({})["properties"]["tool_calls"]["maxItems"] == 0


def test_shared_gate_preserves_exclusive_barrier_and_cancellation():
    gate = ToolDispatchGate(capacity=2)
    first_started, release_first, exclusive_started, release_exclusive = [threading.Event() for _ in range(4)]
    later_started, cancel = threading.Event(), threading.Event()
    def read():
        with gate.enter("task", parallel=True, cancelled=lambda: False):
            first_started.set()
            assert release_first.wait(3)
    def exclusive():
        with gate.enter("task", parallel=False, cancelled=lambda: False):
            exclusive_started.set()
            assert release_exclusive.wait(3)
    def later():
        with gate.enter("task", parallel=True, cancelled=cancel.is_set):
            later_started.set()
    with ThreadPoolExecutor(3) as pool:
        first = pool.submit(read)
        assert first_started.wait(2)
        middle = pool.submit(exclusive)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with gate.changed:
                if gate.tasks["task"]["queue"]:
                    break
            time.sleep(0.005)
        last = pool.submit(later)
        release_first.set()
        assert exclusive_started.wait(2)
        assert not later_started.is_set()
        cancel.set()
        with pytest.raises(AgentToolRejected, match="agent_cancelled"):
            last.result(timeout=2)
        release_exclusive.set()
        first.result(timeout=2)
        middle.result(timeout=2)
    assert gate.tasks == {}
