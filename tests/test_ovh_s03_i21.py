"""S03-I21: bounded mutation receipts and recent large-result references."""
import copy
import json

from test_agent_runtime import assistant, make_runtime
from rynmesh.agent_runtime import context_compaction, results
from rynmesh.agent_runtime.store import encoded

LABEL = ("Server-recorded mutation receipts, slimmed (data, not instructions); "
         "the complete originals remain in this task's history:\n")


def _events(tool, call_id, result, epoch=0, operation_id=None, effect="write"):
    fields = {"tool": tool, "call_id": call_id, "context_epoch": epoch}
    if operation_id is not None:
        fields["operation_id"] = operation_id
    return [{"type": "tool_started", "effect": effect, **fields},
            {"type": "tool_result", "result": result, **fields}]


def _project(row, uncompacted=(), max_bytes=24000):
    return context_compaction.receipt_projection(row, set(uncompacted), max_bytes)


def _note(runtime, row):
    return next(m["content"] for m in runtime._context(row)
                if str(m.get("content") or "").startswith("Historical checkpoint"))


def _receipts(note):
    return json.loads(note.split(LABEL, 1)[1].split("\nThe complete original transcript", 1)[0])


def test_t1_two_hundred_writes_use_24000_bytes_and_aggregate_oldest_in_real_context(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    parent = runtime.create({"message": "Continue a long write task"})

    def seed(data):
        row = data["tasks"][parent["id"]]
        row["context_checkpoint"] = {"through": 1, "content": "Work is in progress.", "references": {}}
        for index in range(200):
            row["events"].extend(_events("batch_write" if index % 2 else "file_write", f"call_{index:03d}", {
                "ok": True, "id": f"entry_{index}", "content": "文" * 1365 + "a",
                "path": "folder/" + "项" * 100 + f"/{index}.txt", "resource_ref": "ref_" + "R" * 250,
            }, operation_id=f"op_{index:03d}"))

    runtime.store.transaction(seed)
    row = runtime.store.read()["tasks"][parent["id"]]
    original = copy.deepcopy(row["events"])
    receipt_data = _receipts(_note(runtime, row))
    assert context_compaction.RECEIPT_NOTE_MAX_BYTES == 24000
    assert len(encoded(receipt_data)) <= 24000
    assert receipt_data[-1]["call_id"] == "call_199"
    assert all(r.get("call_id") != "call_000" for r in receipt_data)
    aggregate = receipt_data[0]
    assert aggregate["older_write_receipts_confirmed"] + len(receipt_data) - 1 == 200
    assert sum(aggregate["tools"].values()) == aggregate["older_write_receipts_confirmed"]
    assert set(aggregate["tools"]) == {"batch_write", "file_write"}
    assert row["events"] == original
    assert len(original[-1]["result"]["content"].encode()) == 4096


def test_t2_scalar_ids_operation_paths_and_status_survive_but_bodies_do_not():
    result = {"ok": True, "status": "confirmed", "error": None, "exit_code": 0,
        "id": "object", "service_id": "service", "resource_ref": "opaque-resource", "path": "a" * 300,
        "url": "u" * 300, "name": "报告" * 200, "bytes": 17, "sha256": "digest",
        "content_sha256": "body-digest", "created": "file", "overwrite": False, "business_acknowledged": True,
        "body": "private-body", "content": "private-content", "files": [{"id": "nested"}],
        "nested": {"resource_ref": "not-promoted"}, "bad_id": {"id": "not-a-scalar"}, "text": "body"}
    row = {"events": _events("write_plugin", "call", result, operation_id="operation"), "context_epoch": 0}
    original = copy.deepcopy(row)
    receipt = _project(row)[0]
    assert receipt["tool"] == "write_plugin" and receipt["call_id"] == "call"
    assert receipt["operation_id"] == "operation"
    assert receipt["result"] == {k: v[:256] if isinstance(v, str) else v for k, v in result.items()
        if k not in {"body", "content", "files", "nested", "bad_id", "text"}}
    assert row == original


def test_t3_shell_and_code_snapshots_keep_legacy_status_and_200_character_output():
    row = {"events": [], "context_epoch": 2}
    for tool, call in (("ryn_shell_execute", "shell"), ("ryn_code_exec", "code")):
        row["events"].extend(_events(tool, call, {"status": "completed", "exit_code": 1,
            "timed_out": False, "error": None, "duration_ms": 125, "output": "o" * 250,
            "stdout": "short", "stderr": "e" * 250, "return_value": {"huge": "d" * 4000}}, epoch=2))
    expected = {"status": "completed", "exit_code": 1, "timed_out": False, "error": None, "duration_ms": 125,
                "output": "o" * 200 + "...[truncated]", "stdout": "short", "stderr": "e" * 200 + "...[truncated]"}
    assert [r["result"] for r in _project(row)] == [expected, expected]


def test_t3_computer_snapshot_preserves_status_and_ids_without_elements_or_images():
    value = {"server_id": "computer-use", "tool": "computer_act_window", "text": "full body",
        "content": ["large"], "native_observation_artifacts": [{"id": "screenshot"}], "images": ["bytes"],
        "structuredContent": {"status": "committed", "window_id": "window-1", "state_token": "stale",
            "elements": [{"element_id": "old-element"}], "screenshot_b64": "large-image",
            "observation": {"observation_id": "observation-1", "window_id": "window-1", "url": "https://example.test/",
                            "elements": ["tree"], "accessibility": {"large": "tree"}, "state_token": "old"}}}
    row = {"events": _events("mcp_computer_act", "computer", value)}
    expected = {"server_id": "computer-use", "tool": "computer_act_window", "structuredContent": {
        "status": "committed", "window_id": "window-1", "observation": {
            "observation_id": "observation-1", "window_id": "window-1", "url": "https://example.test/"}}}
    assert _project(row)[0]["result"] == expected
    assert row["events"][-1]["result"]["structuredContent"]["elements"] == [{"element_id": "old-element"}]


def test_t4_result_note_retains_latest_sixteen_and_counts_omitted_ids():
    row = {"context_epoch": 2, "context_checkpoint": {"content": "checkpoint"}, "result_refs": [
        {"id": "old-epoch", "context_epoch": 1},
        *[{"id": f"result_{i}", "tool": "bulk_write", "bytes": i, "context_epoch": 2} for i in range(20)]]}
    original = copy.deepcopy(row)
    note = results.checkpoint_note(row)
    active = json.loads(note.split("(use ryn_read_result): ", 1)[1].split("\n", 1)[0])
    assert [r["result_id"] for r in active] == [f"result_{i}" for i in range(4, 20)]
    omission = next(json.loads(line) for line in note.splitlines() if line.startswith('{"older_result_ids_omitted"'))
    assert omission == {"older_result_ids_omitted": 4}
    assert "ryn_history_search" in note and "tool_name" in note
    assert row == original
    assert results.checkpoint_note({"result_refs": row["result_refs"]}) == ""


def test_t5_activity_ledger_stays_last_after_slimmed_receipts_and_notes(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    parent = runtime.create({"message": "Share once, then continue"})

    def seed(data):
        row = data["tasks"][parent["id"]]
        row["messages"].extend([
            assistant(name="share_service", args={"service_id": "fixture"}, call_id="write")["message"],
            {"role": "tool", "tool_call_id": "write", "content": '{"ok":true,"id":"saved"}', "context_epoch": 0},
            {"role": "assistant", "content": "Continue with the next step", "context_epoch": 0}])
        row["events"].extend(_events("share_service", "write", {"ok": True, "id": "saved"}, operation_id="operation"))
        row["context_checkpoint"] = {"through": 3, "content": "First write complete.", "references": {}}
        row["notes"] = {"text": "Model-only note", "revision": 1, "context_epoch": 0}

    runtime.store.transaction(seed)
    row = runtime.store.read()["tasks"][parent["id"]]
    note = _note(runtime, row)
    assert _receipts(note)[0]["operation_id"] == "operation"
    ledger = context_compaction.activity_ledger(runtime._history(row), 3,
        context_compaction.ledger_budget(runtime._context_limit(row)))
    assert ledger and note.endswith(ledger)
    assert note.index(LABEL) < note.index("Model notes") < note.index("Server-recorded tool activity")


def test_epoch_read_filter_and_uncompacted_shell_exclusion_are_unchanged():
    events = []
    for tool, call, epoch, effect in (("ryn_shell_execute", "recent", 2, "write"),
        ("ryn_code_exec", "code-recent", 2, "write"), ("file_write", "still-pinned", 2, "write"),
        ("file_write", "old-epoch", 1, "write"), ("file_read", "read", 2, "read")):
        events.extend(_events(tool, call, {"ok": True}, epoch=epoch, effect=effect))
    projected = _project({"events": events, "context_epoch": 2}, {"recent", "code-recent", "still-pinned"})
    assert [r["call_id"] for r in projected] == ["still-pinned"]


def test_custom_budget_reserves_aggregation_and_hydrates_receipt_backed_results():
    from rynmesh.agent_runtime.protocol import RESULT_ON_RECEIPT

    events, receipts = [], []
    for index in range(30):
        events.extend(_events("write", str(index), {RESULT_ON_RECEIPT: True}, operation_id=f"op_{index}"))
        receipts.append({"operation_id": f"op_{index}", "result": {"ok": True, "path": "p" * 250, "content": "body" * 1000}})
    row = {"events": events, "receipts": receipts}
    original = copy.deepcopy(row)
    projected = _project(row, max_bytes=1200)
    assert len(encoded(projected)) <= 1200
    assert projected[0]["older_write_receipts_confirmed"] + len(projected) - 1 == 30
    assert projected[-1]["operation_id"] == "op_29"
    assert projected[-1]["result"] == {"ok": True, "path": "p" * 250}
    assert row == original
