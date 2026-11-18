"""S06-I13: deterministic request-bound call identity, including streamed reads."""
import copy
import hashlib

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_streamed_reads import Reads, raw, setup
from test_agent_tool_router_core import call, spec

from rynmesh.agent_runtime.streamed_reads import StreamedReads
from rynmesh.agent_runtime.tool_router import ToolCallProtocolError, normalize_tool_calls


def rewritten(seed, index, original):
    return "call_ryn_" + hashlib.sha256(f"{seed}:{index}:{original}".encode()).hexdigest()[:24]


def test_t1_history_collision_rewrites_and_keeps_provider_identity():
    proposed = call("call_0")
    before = copy.deepcopy(proposed)
    result = normalize_tool_calls([proposed], [spec()], existing_call_ids={"call_0"}, rewrite_ids_seed="request")[0]
    assert result["call_id"] == rewritten("request", 0, "call_0")
    assert result["provider_call_id"] == "call_0"
    assert proposed == before


@pytest.mark.parametrize("identifier", [None, "", "contains spaces", "x" * 129, 7])
def test_t2_missing_or_invalid_id_is_rewritten(identifier):
    result = normalize_tool_calls([call(identifier)], [spec()], rewrite_ids_seed="request")[0]
    assert result["call_id"] == rewritten("request", 0, identifier)
    assert result["provider_call_id"] == identifier
    assert result["error_result"] is None


def test_t2_responses_missing_call_id_is_rewritten():
    result = normalize_tool_calls([{"type": "function_call", "name": "read", "arguments": '{"path":"a"}'}],
                                  [spec()], rewrite_ids_seed="request")[0]
    assert result["call_id"] == rewritten("request", 0, None)
    assert result["provider_call_id"] is None


def test_t3_same_batch_collisions_have_distinct_deterministic_ids():
    proposed = [call("call_0"), call("call_0")]
    result = normalize_tool_calls(proposed, [spec()], existing_call_ids={"call_0"}, rewrite_ids_seed="request")
    assert [c["call_id"] for c in result] == [rewritten("request", i, "call_0") for i in range(2)]
    assert all(c["provider_call_id"] == "call_0" for c in result)
    without_history = normalize_tool_calls(proposed, [spec()], rewrite_ids_seed="request")
    assert [c["call_id"] for c in without_history] == ["call_0", rewritten("request", 1, "call_0")]
    assert "provider_call_id" not in without_history[0]


def test_t4_without_seed_preserves_strict_duplicate_error():
    with pytest.raises(ToolCallProtocolError) as caught:
        normalize_tool_calls([call("call_0"), call("call_0")], [spec()])
    assert caught.value.reason == "duplicate_call_id"


@pytest.mark.parametrize("existing", [set(), {"call_0"}])
@pytest.mark.parametrize("identifier", ["call_0", None, "bad id"])
def test_t5_early_dispatch_matches_final_ids_with_response_indices(tmp_path, existing, identifier):
    runtime, adapter, selected, stopped = setup(tmp_path)
    if existing:
        old = raw(0)
        selected["messages"] += [{"role": "assistant", "content": "", "tool_calls": [old]},
                                  {"role": "tool", "tool_call_id": "call_0", "content": "{}"}]
        adapter = StreamedReads(runtime, selected, runtime.tools.specs(), stopped.is_set, {"ryn_file_read"})
    proposed = [raw(0), raw(1)]
    for item in proposed:
        item["id"] = identifier
    # Completed events can arrive out of order; response indices remain stable.
    adapter.on_event({"type": "tool_call_complete", "index": 1, "call": proposed[1]})
    adapter.on_event({"type": "tool_call_complete", "index": 0, "call": proposed[0]})
    records = adapter.drain(proposed)
    final = normalize_tool_calls(proposed, runtime.tools.specs(), existing_call_ids=existing,
                                 rewrite_ids_seed=selected["_request_id"], metadata=runtime._metadata)
    assert len(runtime.tools.calls) == 2
    assert [r["call"]["call_id"] for r in records] == [c["call_id"] for c in final]
    assert all(r["raw_call"]["id"] == r["call"]["call_id"] for r in records)


def test_partial_stream_recovery_preserves_rewritten_pair(tmp_path):
    runtime, adapter, selected, _ = setup(tmp_path)
    proposed = raw(0)
    proposed.pop("id")
    adapter.on_event({"type": "tool_call_complete", "index": 0, "call": proposed})
    records = adapter.drain([proposed])
    row = runtime.store.read()["tasks"][selected["id"]]
    assert adapter.record_recovered(row, records)
    assert row["messages"][-2]["tool_calls"][0]["id"] == rewritten(selected["_request_id"], 0, None)
    assert row["messages"][-1]["tool_call_id"] == row["messages"][-2]["tool_calls"][0]["id"]
    diagnostic = [e for e in row["events"] if e["type"] == "tool_call_id_rewritten"]
    assert len(diagnostic) == 1
    assert diagnostic[0]["hidden"] is True


def test_real_loop_reuses_provider_ids_but_pairs_host_ids_and_logs_once(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "one"}, call_id="call_0"),
        assistant(name="inspect_service", args={"service_id": "two"}, call_id="call_0"), assistant("Done"))
    task_id = runtime.create({"message": "Inspect both services"})["id"]
    for _ in range(8):
        advance(runtime)
        if runtime.get(task_id)["status"] in {"completed", "failed"}:
            break
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed"
    assert len(tools.calls) == 2
    history = model.calls[-1][1]
    ids = [c["id"] for m in history for c in m.get("tool_calls", [])]
    assert len(ids) == len(set(ids)) == 2
    assert ids[0] == "call_0" and ids[1].startswith("call_ryn_")
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == ids
    diagnostic = [e for e in row["events"] if e["type"] == "tool_call_id_rewritten"]
    assert len(diagnostic) == 1 and diagnostic[0]["call_id"] == ids[1]
    assert diagnostic[0]["hidden"] is True
    assert not any(m.get("ui", {}).get("kind") == "tool_call_id_rewritten" for m in row["messages"])


def test_real_model_stream_and_final_history_share_ids_and_one_diagnostic(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.tools = Reads()
    proposed = [raw(0), raw(1)]
    for item in proposed:
        item.pop("id")
    captured = []
    def complete(model_id, messages, tool_specs, cancelled, on_event):
        captured.append(copy.deepcopy(messages))
        if len(captured) == 1:
            for index, item in enumerate(proposed):
                on_event({"type": "tool_call_complete", "index": index, "call": item})
            return {"message": {"role": "assistant", "content": "", "tool_calls": proposed}}
        return assistant("Done.")
    runtime.model.complete = complete
    task_id = runtime.create({"message": "Read two test files"})["id"]
    for _ in range(8):
        advance(runtime)
        if runtime.get(task_id)["status"] == "completed":
            break
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed"
    assert len(runtime.tools.calls) == 2
    offered = [c["id"] for m in captured[-1] for c in m.get("tool_calls", [])]
    replies = [m["tool_call_id"] for m in captured[-1] if m["role"] == "tool"]
    assert len(set(offered)) == 2 and replies == offered
    assert all(i.startswith("call_ryn_") for i in offered)
    events = [e for e in row["events"] if e["type"] == "tool_call_id_rewritten"]
    assert [e["call_id"] for e in events] == offered
    assert all(e["hidden"] for e in events)


def test_interrupted_stream_recovery_pairs_host_id_without_redispatch(tmp_path):
    runtime, adapter, selected, _ = setup(tmp_path)
    proposed = raw(0)
    proposed.pop("id")
    adapter.on_event({"type": "tool_call_complete", "index": 0, "call": proposed})
    records = adapter.abort()
    row = runtime.store.read()["tasks"][selected["id"]]
    assert adapter.record_recovered(row, records)
    offered = row["messages"][-2]["tool_calls"][0]["id"]
    assert offered == rewritten("request_fixture", 0, None)
    assert row["messages"][-1]["tool_call_id"] == offered
    assert len(runtime.tools.calls) == 1
    assert len([e for e in row["events"] if e["type"] == "tool_call_id_rewritten"]) == 1


def test_responses_replay_uses_same_host_id_for_call_and_output():
    from rynmesh.llm_package.protocol_codecs import RESPONSES, make_request
    normalized = normalize_tool_calls([call("call_0")], [spec()], existing_call_ids={"call_0"},
                                      rewrite_ids_seed="request")[0]
    identity = normalized["call_id"]
    history = [{"role": "user", "content": "Read fixture"},
               {"role": "assistant", "content": "", "tool_calls": [call(identity)]},
               {"role": "tool", "tool_call_id": identity, "content": "fixture contents"}]
    wire = make_request({"messages": history, "tools": [spec()]}, RESPONSES, "native-model", "a" * 64)
    calls = [item["call_id"] for item in wire["input"] if item.get("type") == "function_call"]
    outputs = [item["call_id"] for item in wire["input"] if item.get("type") == "function_call_output"]
    assert calls == outputs == [identity]
    assert wire["store"] is False and "previous_response_id" not in wire
