"""Actual Ryn loop wiring, with a deterministic inference boundary and real store."""
import copy
import json
import threading

import pytest
from test_agent_codex_loop import workstation
from test_agent_runtime import advance, assistant, compaction_history, make_runtime


def test_router_rejections_return_to_model_then_valid_tool_executes_once(tmp_path):
    invalid = assistant(name="inspect_service", args={"service_id": "svc1"}, call_id="bad-json")
    invalid["message"]["tool_calls"][0]["function"]["arguments"] = '{broken'
    runtime, model, tools, _ = make_runtime(tmp_path, invalid,
        assistant(name="inspect_service", args={"service_id": "svc1"}, call_id="repaired"), assistant("Read back."))
    row = runtime.create({"message": "Inspect svc1"})
    advance(runtime, 5)
    done = runtime.get(row["id"])
    assert done["status"] == "completed"
    feedback = json.loads(model.calls[1][1][-1]["content"])
    assert feedback["error"] == "agent_tool_arguments_invalid" and feedback["not_executed"]
    assert model.calls[1][1][-2]["tool_calls"][0]["function"]["arguments"] == '{broken'
    assert len(tools.calls) == 1
    assert len(runtime.store.read()["tasks"][row["id"]]["receipts"]) == 1


def test_unknown_tool_in_batch_does_not_repeat_valid_sibling(tmp_path):
    response = assistant(name="inspect_service", args={"service_id": "svc1"}, call_id="known")
    response["message"]["tool_calls"].extend(assistant(name="unknown", call_id="unknown")["message"]["tool_calls"])
    runtime, model, tools, _ = make_runtime(tmp_path, response, assistant("Done."))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 4)
    assert runtime.get(row["id"])["status"] == "completed"
    assert len(tools.calls) == 1
    outputs = [m for m in model.calls[-1][1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in outputs] == ["known", "unknown"]
    assert json.loads(outputs[1]["content"])["error"] == "agent_unknown_tool"


def test_bound_schema_change_returns_error_without_dispatch(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "svc1"}), assistant("Schema changed."))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    original = tools.specs
    def changed():
        specs = copy.deepcopy(original())
        specs[0]["function"]["parameters"]["properties"]["service_id"]["minLength"] = 2
        return specs
    tools.specs = changed
    advance(runtime, 2)
    assert tools.calls == []
    assert json.loads(model.calls[-1][1][-1]["content"])["error"] == "agent_tool_definition_changed"
    assert runtime.get(row["id"])["status"] == "completed"


@pytest.mark.parametrize("control", [{"end_turn": False}, {"needs_follow_up": True}, {"finish_reason": "length"}])
def test_provider_continuation_reenters_same_conversation(tmp_path, control):
    runtime, model, _, _ = make_runtime(tmp_path, {**assistant("First. "), **control}, assistant("Finished."))
    row = runtime.create({"message": "Finish the work"})
    advance(runtime, 2)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and len(model.calls) == 2
    assert done["result"] == "First. Finished."
    assert any(e["type"] == "turn_decision" and e["action"] == "continue" for e in done["events"])


def test_context_layers_are_real_runtime_projection_not_durable_rewrite(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Done."))
    row = runtime.create({"message": "Owner exact constraint."})
    before = copy.deepcopy(runtime.store.read()["tasks"][row["id"]]["messages"])
    advance(runtime)
    request = model.calls[0][1]
    assert [m["role"] for m in request[:2]] == ["developer", "developer"]
    assert request[-1] == {"role": "user", "content": "Owner exact constraint."}
    assert runtime.store.read()["tasks"][row["id"]]["messages"][:len(before)] == before


def test_truncated_tool_item_is_not_dispatched_even_if_arguments_are_json(tmp_path):
    proposal = {**assistant(name="inspect_service", args={"service_id": "svc1"}), "finish_reason": "length"}
    runtime, model, tools, _ = make_runtime(tmp_path, proposal, assistant("Truncation handled."))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    assert tools.calls == []
    feedback = json.loads(model.calls[-1][1][-1]["content"])
    assert feedback["error"] == "agent_tool_call_incomplete" and feedback["not_executed"]
    assert runtime.get(row["id"])["status"] == "completed"


@pytest.mark.parametrize("incomplete", [{"finish_reason": "length"}, {"end_turn": False}, {"needs_follow_up": True}])
def test_incomplete_checkpoint_never_replaces_original_evidence(tmp_path, incomplete):
    runtime, _, tools, _ = make_runtime(tmp_path, {**assistant("Only partial summary"), **incomplete})
    runtime.update_settings({"max_context_chars": 12000})
    row = runtime.create({"message": "Inspect A. Never change B."})
    original = compaction_history(runtime, row["id"])
    advance(runtime)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["messages"] == original
    assert saved.get("context_checkpoint") is None
    assert saved["status"] == "queued" and saved["model_retry_count"] == 1
    assert tools.calls == []
    assert any(e["type"] == "context_compaction_rejected" and e["error"] == "agent_compaction_incomplete"
               for e in saved["events"])


def test_real_read_starts_during_stream_but_write_waits_for_accepted_response(tmp_path):
    runtime, model, workspace = workstation(tmp_path)
    (workspace / "source.txt").write_text("source evidence", encoding="utf-8")
    read_finished = threading.Event()
    execute = runtime.tools.execute
    def tracked(name, *args, **kwargs):
        result = execute(name, *args, **kwargs)
        if name == "ryn_file_read":
            read_finished.set()
        return result
    runtime.tools.execute = tracked
    read = assistant(name="ryn_file_read", args={"path": "source.txt"}, call_id="early-read")["message"]["tool_calls"][0]
    write = assistant(name="ryn_file_write", args={"path": "output.txt", "content": "created after stream"}, call_id="late-write")["message"]["tool_calls"][0]
    calls = []
    def streaming(model_id, messages, specs, cancelled, on_event):
        calls.append(messages)
        if len(calls) > 1:
            assert json.loads(messages[-1]["content"])["path"].endswith("output.txt")
            return assistant("Written and verified by tool receipt.")
        on_event({"type": "tool_call_complete", "index": 0, "call": read})
        on_event({"type": "tool_call_complete", "index": 1, "call": write})
        assert read_finished.wait(3), "Read must run before inference returns"
        assert not (workspace / "output.txt").exists()
        return {"message": {"role": "assistant", "content": "", "tool_calls": [read, write]}}
    model.complete = streaming
    row = runtime.create({"message": "Read source, then create output"})
    advance(runtime, 3)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["status"] == "completed", saved.get("error")
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "created after stream"
    assert [m["tool_call_id"] for m in saved["messages"] if m["role"] == "tool"] == ["early-read", "late-write"]
    assert len(saved["receipts"]) == 2 and all(r["status"] == "confirmed" for r in saved["receipts"])
    types = [event["type"] for event in saved["events"]]
    assert types.index("tool_execution_completed") < types.index("model_response_completed")


def test_stream_item_mismatch_recovers_actual_read_without_replay(tmp_path):
    runtime, model, workspace = workstation(tmp_path)
    (workspace / "source.txt").write_text("source evidence", encoding="utf-8")
    read = assistant(name="ryn_file_read", args={"path": "source.txt"}, call_id="partial-read")["message"]["tool_calls"][0]
    changed = copy.deepcopy(read)
    changed["function"]["arguments"] = '{"path":"other.txt"}'
    calls = []
    def streaming(model_id, messages, specs, cancelled, on_event):
        calls.append(copy.deepcopy(messages))
        if len(calls) > 1:
            assert any(m.get("tool_call_id") == "partial-read" and "source evidence" in m["content"] for m in messages)
            return assistant("Recovered read evidence.")
        on_event({"type": "tool_call_complete", "index": 0, "call": read})
        return {"message": {"role": "assistant", "content": "", "tool_calls": [changed]}}
    model.complete = streaming
    row = runtime.create({"message": "Read source"})
    advance(runtime, 2)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["status"] == "completed", saved.get("error")
    assert len(saved["receipts"]) == 1 and saved["receipts"][0]["status"] == "confirmed"
    assert len([m for m in saved["messages"] if m["role"] == "tool"]) == 1
    assert len(calls) == 2


def test_early_read_preserves_typed_correction_for_model_repair(tmp_path):
    runtime, model, workspace = workstation(tmp_path)
    (workspace / "source.txt").write_text("line one\nline two\n", encoding="utf-8")
    invalid = assistant(name="ryn_file_read", args={"path": "source.txt", "start_line": 999}, call_id="invalid-page")
    calls = []
    def streaming(model_id, messages, specs, cancelled, on_event):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            on_event({"type": "tool_call_complete", "index": 0, "call": invalid["message"]["tool_calls"][0]})
            return invalid
        if len(calls) == 2:
            feedback = json.loads(messages[-1]["content"])
            assert feedback["error"] == "agent_local_line_invalid" and feedback["not_executed"]
            assert feedback["details"]["total_lines"] == 2 and feedback["details"]["field_path"] == "start_line"
            assert "999" in feedback["correction"] and "2 lines" in feedback["correction"]
            return assistant(name="ryn_file_read", args={"path": "source.txt", "start_line": 1}, call_id="correct-page")
        assert "line one" in messages[-1]["content"]
        return assistant("Read corrected page.")
    model.complete = streaming
    row = runtime.create({"message": "Read source"})
    advance(runtime, 4)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["status"] == "completed", saved.get("error")
    assert [r["status"] for r in saved["receipts"]] == ["failed", "confirmed"]
