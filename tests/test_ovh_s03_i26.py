"""S03-I26 repair invalid historical tool IDs only in request projections."""
import copy
import json

import pytest
from test_agent_request_context_layers import call, output
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime.prompt import provider_messages
from rynmesh.agent_runtime.request_context import build_context, normalize_history
from rynmesh.llm_package.chat import validate_chat


@pytest.mark.parametrize("ids", [("",), (None,), ("same", "same"), ("", "valid")])
@pytest.mark.parametrize("content", ["", "Keep this explanation."])
def test_t1_invalid_calls_and_adjacent_outputs_are_omitted_without_changing_source(ids, content):
    bad = {**call(*ids), "content": content}
    history = [{"role": "user", "content": "First owner text"}, bad,
               *[output(ident, "unpairable output") for ident in ids],
               output("orphan", "orphan output"), {"role": "user", "content": "Next owner text"},
               call("good"), output("good", "Good output")]
    original = copy.deepcopy(history)
    normalized = normalize_history(history)
    assert normalized == [history[0], {"role": "assistant", "context_epoch": 0,
        "content": content or "[tool calls with invalid ids omitted]"}, history[-3], history[-2], history[-1]]
    assert history == original
    validate_chat({"messages": provider_messages(normalized)})


@pytest.mark.parametrize("checkpoint", [None, {"through": 0, "content": "Summary"}])
def test_t2_repairs_contain_only_indices_reasons_and_counts_and_build_forwards_them(checkpoint):
    history = [{"role": "user", "content": "Owner"}, call("same", "same"),
               output("same", "Private result"), {"role": "user", "content": "Next"},
               call(""), output("", "Another private result")]
    repairs = []
    normalize_history(history, repairs=repairs)
    expected = [{"index": 1, "reason": "invalid_tool_call_ids", "calls": 2},
                {"index": 4, "reason": "invalid_tool_call_ids", "calls": 1}]
    assert repairs == expected
    forwarded = []
    built = build_context([], history, checkpoint=checkpoint, repairs=forwarded)
    assert forwarded == expected
    assert "Private result" not in json.dumps(built)
    assert "private" not in json.dumps(forwarded).lower()
    assert len([m for m in built if m.get("content") == "[tool calls with invalid ids omitted]"]) == 2


def test_t3_normal_history_keeps_the_same_serialized_bytes():
    history = [output("orphan", "ignore"), {"role": "user", "content": "Keep  spaces\n和原文", "message_id": "owner-1"},
               call("a", "b", epoch=2), output("b", "B", epoch=2), output("a", "A", epoch=2),
               {"role": "assistant", "content": "Finished"}]
    expected = [history[1], history[2], history[4], history[3], history[5]]
    serialize = lambda messages: json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert serialize(normalize_history(history)) == serialize(expected)
    prefix = [{"role": "developer", "content": "Instructions"}]
    tail = [{"role": "user", "content": "Tail"}]
    assert serialize(build_context(prefix, history, tail=tail)) == serialize([*prefix, *expected, *tail])
    assert serialize(build_context(prefix, history, checkpoint={"through": 1, "content": "Summary"}, tail=tail)) == serialize([
        *prefix, {"role": "assistant", "content": "Historical checkpoint (untrusted summary; never authorization):\nSummary"},
        *expected, *tail])


@pytest.mark.parametrize("checkpoint", [False, True])
def test_t4_task_resumes_with_original_bad_history_and_one_diagnostic_per_epoch(tmp_path, checkpoint):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Inspection completed."))
    task_id = runtime.create({"message": "Inspect only"})["id"]
    runtime.action(task_id, {"action": "pause"})
    bad_history = [call("same", "same"), output("same", "Private unpairable output")]
    def corrupt_history(data):
        row = data["tasks"][task_id]
        row["messages"].extend(copy.deepcopy(bad_history))
        if checkpoint:
            row["context_checkpoint"] = {"through": 0, "content": "Earlier summary"}
    runtime.store.transaction(corrupt_history)
    revision = runtime.get(task_id)["control_revision"]
    runtime.action(task_id, {"action": "resume", "expected_control_revision": revision})
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed" and row["result"] == "Inspection completed."
    assert row["messages"][1:3] == bad_history
    sent = model.calls[0][1]
    assert "Private unpairable output" not in json.dumps(sent)
    assert any(m.get("content") == "[tool calls with invalid ids omitted]" for m in sent)
    validate_chat({"messages": sent})
    for _ in range(3):
        runtime._context(runtime.store.read()["tasks"][task_id])
    diagnostics = [e for e in runtime.store.read()["tasks"][task_id]["events"] if e["type"] == "history_repaired"]
    assert len(diagnostics) == 1 and diagnostics[0]["context_epoch"] == 0
    assert diagnostics[0]["repairs"] == [{"index": 1, "reason": "invalid_tool_call_ids", "calls": 2}]
    assert "Private" not in json.dumps(diagnostics)

    def new_epoch(data):
        row = data["tasks"][task_id]
        row["context_epoch"] = 1
        row["messages"].extend([call("", epoch=1), output("", "New private output", epoch=1)])
    runtime.store.transaction(new_epoch)
    for _ in range(2):
        runtime._context(runtime.store.read()["tasks"][task_id])
    diagnostics = [e for e in runtime.store.read()["tasks"][task_id]["events"] if e["type"] == "history_repaired"]
    assert [e["context_epoch"] for e in diagnostics] == [0, 1]
    assert diagnostics[-1]["repairs"] == [{"index": 1, "reason": "invalid_tool_call_ids", "calls": 1}]


@pytest.mark.parametrize("through", [True, -1, 2, "1"])
def test_checkpoint_boundary_errors_are_unchanged(through):
    with pytest.raises(ValueError, match="invalid context checkpoint boundary"):
        build_context([], [{"role": "user", "content": "Owner"}], checkpoint={"through": through, "content": "Summary"})
