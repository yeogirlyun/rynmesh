"""S02-I34 discarded writes remain visible as bounded, private host context."""
import json

from test_agent_runtime import advance, approve, assistant, make_runtime
from test_agent_conversation_controls import send
from rynmesh.agent_runtime.protocol import event


NOTICE = "主人编辑了更早的一条消息"


def written_then_edited(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "PRIVATE_ARGUMENT_MARKER"}),
        assistant("Old answer"), assistant("Edited answer"), assistant("Followup answer"))
    task = runtime.create({"message": "Perform the requested operation"})
    advance(runtime, 2)
    approve(runtime, task["id"])
    advance(runtime, 2)
    assert tools.shared is True
    send(runtime, task["id"], "Explain the current state instead", edit_message_index=0)
    return runtime, model, task["id"]


def notice(messages):
    return [m["content"] for m in messages if NOTICE in (m.get("content") or "")]


def test_t1_real_write_then_edit_reaches_next_model_without_arguments_or_results(tmp_path):
    runtime, model, task_id = written_then_edited(tmp_path)
    saved = runtime.store.read()["tasks"][task_id]
    summary = saved.get("retired_writes")
    assert summary and summary["omitted"] == 0
    assert summary["items"] == [{"tool": "share_service", "operation_id": saved["receipts"][0]["operation_id"], "status": "confirmed"}]
    assert "retired_writes" not in runtime.get(task_id)
    advance(runtime)
    sent = model.calls[-1][1]
    notes = notice(sent)
    assert len(notes) == 1 and "share_service" in notes[0] and "confirmed" in notes[0]
    assert {"role": "user", "content": notes[0]} in sent
    assert sent[-1]["role"] != "assistant"
    assert notes[0].startswith('<host_note kind="retired_writes">')
    assert "只读工具确认当前状态" in notes[0] and "不要不加检查地重复执行" in notes[0]
    assert "PRIVATE_ARGUMENT_MARKER" not in json.dumps(model.calls[-1][1])
    assert "arguments" not in notes[0] and "result" not in notes[0]
    assert summary["items"][0]["operation_id"] not in notes[0]


def test_t2_no_write_and_consecutive_edits_do_not_keep_an_old_notice(tmp_path):
    runtime, model, task_id = written_then_edited(tmp_path)
    assert runtime.store.read()["tasks"][task_id].get("retired_writes")
    advance(runtime)
    send(runtime, task_id, "Another edit without a new write", edit_message_index=0)
    assert not runtime.store.read()["tasks"][task_id].get("retired_writes")
    advance(runtime)
    assert not notice(model.calls[-1][1])


def test_t2_fresh_edit_with_no_write_has_no_notice(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Old answer"), assistant("New answer"))
    task = runtime.create({"message": "Read and explain"})
    advance(runtime)
    send(runtime, task["id"], "Explain another way", edit_message_index=0)
    advance(runtime)
    assert not notice(model.calls[-1][1])


def test_t3_new_owner_message_clears_the_private_summary_and_model_note(tmp_path):
    runtime, model, task_id = written_then_edited(tmp_path)
    assert runtime.store.read()["tasks"][task_id].get("retired_writes")
    advance(runtime)
    send(runtime, task_id, "Additional owner instruction")
    assert "retired_writes" not in runtime.store.read()["tasks"][task_id]
    advance(runtime)
    assert not notice(model.calls[-1][1])


def test_t4_only_recent_twenty_matching_confirmed_or_unknown_writes_are_retained(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("New answer"))
    task = runtime.create({"message": "Original owner request"})
    def seed(data):
        row = data["tasks"][task["id"]]
        row.setdefault("receipts", [])
        for i in range(27):
            call_id, op_id = f"call_{i}", f"op_{i}"
            row["messages"].extend([
                assistant(name="share_service", args={"service_id": "SECRET_PARAMETERS"}, call_id=call_id)["message"],
                {"role": "tool", "tool_call_id": call_id, "content": "SECRET_RESULT"}])
            event(row, "tool_started", call_id=call_id, operation_id=op_id, tool="share_service",
                effect="read" if i == 25 else "write")
            row["receipts"].append({"operation_id": op_id,
                "status": "not_executed" if i == 26 else "unknown" if i % 7 == 0 else "confirmed",
                "arguments": {"secret": "SECRET_PARAMETERS"}, "result": {"secret": "SECRET_RESULT"}})
        # An unrelated event/receipt cannot create a discarded-branch hint.
        event(row, "tool_started", call_id="not_in_suffix", operation_id="outside", tool="outside_write", effect="write")
        row["receipts"].append({"operation_id": "outside", "status": "confirmed"})
    runtime.store.transaction(seed)
    send(runtime, task["id"], "Replacement owner request", edit_message_index=0)
    saved = runtime.store.read()["tasks"][task["id"]]
    summary = saved.get("retired_writes")
    assert summary and len(summary["items"]) == 20 and summary["omitted"] == 5
    assert [item["operation_id"] for item in summary["items"]] == [f"op_{i}" for i in range(5, 25)]
    assert all(set(item) == {"tool", "operation_id", "status"} for item in summary["items"])
    assert {item["status"] for item in summary["items"]} == {"confirmed", "unknown"}
    advance(runtime)
    notes = notice(model.calls[-1][1])
    assert len(notes) == 1 and '"omitted":5' in notes[0]
    assert notes[0].count('"tool":"share_service"') == 20
    assert all(value not in notes[0] for value in ("SECRET_PARAMETERS", "SECRET_RESULT", "outside_write"))
