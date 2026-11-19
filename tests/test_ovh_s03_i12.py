"""S03-I12 history recovery through actual model menus and internal dispatch."""
import copy
import json

import pytest
from test_agent_runtime import advance, assistant, fit_window, make_runtime
from test_agent_runtime_model_switch import body, setup

from rynmesh.agent_runtime import context_compaction, tool_menu


NAMES = {"ryn_history_search", "ryn_history_read"}


def stored(runtime, task_id):
    return runtime.store.read()["tasks"][task_id]


def seed(runtime, task_id, messages, *, through=None, epoch=0):
    def write(data):
        row = data["tasks"][task_id]
        row["messages"].extend(copy.deepcopy(messages))
        row["context_epoch"] = epoch
        row["context_checkpoint"] = {"through": len(runtime._history(row)) if through is None else through,
                                     "content": "Earlier evidence summarized; continue inspection."}
    runtime.store.transaction(write)


def invoke(runtime, model, task_id, name, args):
    model.responses.append(assistant(name=name, args=args))
    advance(runtime, 2)
    events = stored(runtime, task_id)["events"]
    return next(e["result"] for e in reversed(events) if e["type"] == "tool_result")


def test_t1_search_only_checkpoint_prefix_and_current_epoch(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Find exact evidence"})["id"]
    messages = [dict(role="assistant", content="needle OLD", context_epoch=0),
                dict(role="user", content="needle OLD OWNER", context_epoch=0),
                dict(role="assistant", content="needle CURRENT", context_epoch=1),
                dict(role="assistant", content="needle AFTER THROUGH", context_epoch=1)]
    seed(runtime, task_id, messages, through=3, epoch=1)
    result = invoke(runtime, model, task_id, "ryn_history_search", {"query": "needle"})
    assert result["matches"] == [dict(position=3, role="assistant", tool_name="", snippet="needle CURRENT")]
    assert NAMES <= {s["function"]["name"] for s in model.calls[0][2]}
    assert not tools.calls


@pytest.mark.parametrize("change", ["model", "scope"])
def test_t2_real_epoch_change_cannot_recover_old_evidence(tmp_path, change):
    runtime, model, tools, _, task_id, _ = setup(tmp_path)
    complete = model.complete
    def bound_complete(model_id, messages, tool_specs, cancelled, on_event, *, expected_destination=None):
        if change == "model":
            assert expected_destination == stored(runtime, task_id)["model_destination"]
        return complete(model_id, messages, tool_specs, cancelled, on_event)
    model.complete = bound_complete
    seed(runtime, task_id, [dict(role="assistant", content="old-private-evidence", context_epoch=0)])
    if change == "model":
        runtime.switch_model(task_id, body(runtime, task_id))
    else:
        tools.prepare_scope = lambda selection, **kw: {"resource_grants": [], "egress_grants": []}
        row = runtime.get(task_id)
        runtime.update_scope(task_id, dict(scope={"resources": [], "allow_model_data": False},
            expected_control_revision=row["control_revision"], expected_scope_revision=row["scope_revision"]))
    row = stored(runtime, task_id)
    assert row["context_epoch"] == 1 and not row.get("context_checkpoint")
    assert NAMES.isdisjoint(s["function"]["name"] for s in runtime._specs(row))
    seed(runtime, task_id, [dict(role="assistant", content="new evidence", context_epoch=1)], epoch=1)
    runtime.action(task_id, dict(action="resume", expected_control_revision=runtime.get(task_id)["control_revision"]))
    result = invoke(runtime, model, task_id, "ryn_history_search", {"query": "old-private-evidence"})
    assert result["matches"] == []
    assert invoke(runtime, model, task_id, "ryn_history_read", {"position": 1})["error"] == "agent_history_position_invalid"
    assert "old-private-evidence" in json.dumps(stored(runtime, task_id)["messages"])


def test_t3_one_based_paging_recovers_original_json_and_resource_ref(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Recover original"})["id"]
    original = dict(role="tool", content=json.dumps({"text": "细节" * 500, "resource_ref": "resource://exact/reference"}, ensure_ascii=False),
                    tool_call_id="old-call", context_epoch=0)
    seed(runtime, task_id, [original])
    runtime.store.transaction(lambda data: data["tasks"][task_id]["limits"].update(max_output_chars=1024))
    offset, pages = 0, []
    while True:
        result = invoke(runtime, model, task_id, "ryn_history_read", {"position": 2, "offset_chars": offset})
        assert result["position"] == 2 and result["offset_chars"] == offset
        assert len(result["content"]) <= max(64, min(4096, (1024 - 512) // 6))
        pages.append(result["content"])
        if result["next_offset_chars"] is None:
            break
        assert result["next_offset_chars"] > offset
        offset = result["next_offset_chars"]
    assert json.loads("".join(pages)) == original
    assert stored(runtime, task_id)["messages"][1] == original
    invalid = invoke(runtime, model, task_id, "ryn_history_read", {"position": 3})
    assert invalid["error"] == "agent_history_position_invalid"
    invalid = invoke(runtime, model, task_id, "ryn_history_read", {"position": 2, "offset_chars": result["total_chars"] + 1})
    assert invalid["error"] == "agent_invalid_cursor"


def test_t4_literal_case_sensitive_search_filters_and_utf8_snippets(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Find originals"})["id"]
    seed(runtime, task_id, [dict(role="assistant", content="Needle.* exact", context_epoch=0,
        tool_calls=[dict(id="read-old", type="function", function=dict(name="ryn_file_read", arguments='{"path":"a.txt"}'))]),
        dict(role="tool", tool_call_id="read-old", content="Needle.* " + "测" * 100, context_epoch=0),
        dict(role="assistant", content="NeedleX wrong regex match", context_epoch=0)])
    assert invoke(runtime, model, task_id, "ryn_history_search", {"query": "needle"})["matches"] == []
    result = invoke(runtime, model, task_id, "ryn_history_search", dict(query="Needle.*", role="tool", tool_name="ryn_file_read", limit=1))
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert set(match) == {"position", "role", "tool_name", "snippet"}
    assert match["position"] == 3 and match["tool_name"] == "ryn_file_read"
    assert len(match["snippet"].encode()) <= 160
    assert match["snippet"].startswith("Needle.* ")


def test_t5_no_checkpoint_hides_tools_even_from_lazy_discovery(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Inspect"})["id"]
    fit_window(runtime, task_id, 12000, small_window=True)
    row = stored(runtime, task_id)
    assert NAMES.isdisjoint(s["function"]["name"] for s in runtime._specs(row))
    result = invoke(runtime, model, task_id, tool_menu.NAME, {"query": "ryn_history_read"})
    assert NAMES.isdisjoint(result.get("matches", []))


def test_t6_read_only_real_dispatch_and_task_isolation(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "read_only"})
    task_id = runtime.create({"message": "Inspect only"})["id"]
    seed(runtime, task_id, [dict(role="assistant", content="this-task-only", context_epoch=0)])
    other = runtime.create({"message": "other-task-private"})["id"]
    runtime.action(other, {"action": "pause"})
    result = invoke(runtime, model, task_id, "ryn_history_read", {"position": 2})
    assert json.loads(result["content"])["content"] == "this-task-only"
    assert runtime._metadata("ryn_history_read")["effect"] == "read"
    assert not tools.calls
    assert "other-task-private" not in json.dumps(result)


def test_checkpoint_history_tools_load_through_small_window_directory(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_context_chars": 12000})
    task_id = runtime.create({"message": "Recover missing detail"})["id"]
    fit_window(runtime, task_id, 12000, small_window=True)
    seed(runtime, task_id, [dict(role="assistant", content="archived-detail", context_epoch=0)])
    loaded = invoke(runtime, model, task_id, tool_menu.NAME, {"load": ["ryn_history_search"]})
    assert "ryn_history_search" in loaded.get("loaded_names", []), loaded
    found = invoke(runtime, model, task_id, "ryn_history_search", {"query": "archived-detail"})
    assert found["matches"][0]["position"] == 2


def test_search_default_limit_is_twenty_and_parameter_limit_is_enforced(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Inspect"})["id"]
    seed(runtime, task_id, [dict(role="assistant", content=f"match {i}", context_epoch=0) for i in range(24)])
    found = invoke(runtime, model, task_id, "ryn_history_search", {"query": "match"})
    assert len(found["matches"]) == 20
    assert [item["position"] for item in found["matches"]] == list(range(2, 22))
    invalid = invoke(runtime, model, task_id, "ryn_history_search", {"query": "match", "limit": 21})
    assert invalid["error"] == "agent_invalid_control_proposal"


def test_continuation_and_summary_explain_recovery_and_current_state(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    text = context_compaction.CHECKPOINT_CONTINUATION + context_compaction.CHECKPOINT_RECOVERY
    assert "Read current file contents directly when needed for an edit." in text
    assert "Use ryn_history_search/ryn_history_read for missing evidence of past work." in text
    assert "Refresh potentially stale file or process state before acting on it." in text
    instructions = runtime._summary_messages({"messages": []}, 4000)[0]["content"]
    assert "exact earlier messages stay readable with ryn_history_read; do not copy long excerpts" in instructions
    assert "exact earlier messages stay readable with ryn_history_read; do not copy long excerpts" in runtime._prefix_compaction_message(4000)["content"]
    assert "exact earlier messages stay readable with ryn_history_read; do not copy long excerpts" in runtime._summary_messages({}, 4000, compact_prompt="Keep facts concise")[0]["content"]
