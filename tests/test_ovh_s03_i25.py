"""S03-I25: checkpoint-bounded display log compaction and live owner messages."""
import copy

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_context_compaction_policy import exchange, seed

from rynmesh.agent_runtime import budget, context_compaction, event_compaction, projects
from rynmesh.agent_runtime.protocol import event


def append(row, kind, **fields):
    event(row, kind, **fields)


def log(tmp_path, *, checkpoint=True):
    row = {"id": "fixture", "events": [], "messages": [{"role": "user", "content": "One long task"}],
           "event_cursor": 0, "revision": 0, "control_revision": 0, "context_epoch": 0,
           "receipts": [{"id": "receipt"}], "operation_ledger": {"fixture": "unchanged"},
           "project": {"path": str(tmp_path)}, "context_checkpoint": {"through": 1, "content": "Summary"}}
    append(row, "created")
    for i in range(800):
        append(row, "provider_selected", provider="local", index=i)
        if i == 50:
            append(row, "tool_started", tool="ryn_file_write", call_id="write", operation_id="write_op", effect="write")
            # Production tool_result often has no effect; its started event supplies it.
            append(row, "tool_result", tool="ryn_file_write", call_id="write", operation_id="write_op", result={"status": "completed", "path": str(tmp_path / "a.py")})
            append(row, "tool_result", tool="ryn_file_write", call_id="write_only", effect="write", result={"status": "completed"})
            append(row, "tool_result", tool="ryn_file_read", call_id="read", result={"path": str(tmp_path / "a.py"), "start_line": 1, "end_line": 10, "total_lines": 20, "sha256": "a" * 64})
            append(row, "failed", error="test_failure")
            append(row, "approved", approval_id="owner_decision")
            append(row, "future_event_type", value="preserve unknown events")
            append(row, "context_compaction_started", through=1)
    if checkpoint:
        append(row, "context_compacted", through=1)
    for i in range(150):
        append(row, "provider_selected", index=800+i)
    return row


def test_t1_one_owner_turn_compacts_process_events_and_retains_evidence(tmp_path):
    row = log(tmp_path)
    original = copy.deepcopy(row)
    protected = [e for e in row["events"] if e["type"] not in {"provider_selected"}]
    removed = event_compaction.compact_in_turn(row)
    assert removed > 700 and len(row["events"]) < 200
    assert all(e in row["events"] for e in protected)
    assert any(e["type"] == "turn_compacted" and e["events"] > 1 for e in row["events"])
    assert row["messages"] == original["messages"]
    assert row["receipts"] == original["receipts"] and row["operation_ledger"] == original["operation_ledger"]
    assert row["event_cursor"] == original["event_cursor"]


def test_t2_without_checkpoint_does_not_touch_events(tmp_path):
    row = log(tmp_path, checkpoint=False)
    before = copy.deepcopy(row)
    assert event_compaction.compact_in_turn(row) == 0
    assert row == before


def test_t3_recent_150_events_are_unchanged_and_recompaction_is_idempotent(tmp_path):
    row = log(tmp_path)
    tail = copy.deepcopy(row["events"][-150:])
    event_compaction.compact_in_turn(row)
    assert row["events"][-150:] == tail
    before = copy.deepcopy(row)
    assert event_compaction.compact_in_turn(row) == 0
    assert row == before


def test_t4_read_coverage_and_write_receipt_projection_are_identical(tmp_path):
    row = log(tmp_path)
    coverage = projects.read_progress(row)
    receipts = context_compaction.receipt_projection(row, set(), 4000)
    assert coverage and receipts
    event_compaction.compact_in_turn(row)
    assert projects.read_progress(row) == coverage
    assert context_compaction.receipt_projection(row, set(), 4000) == receipts


def test_old_turn_folding_uses_the_same_evidence_preservation(tmp_path):
    row = log(tmp_path)
    coverage = projects.read_progress(row)
    receipts = context_compaction.receipt_projection(row, set(), 4000)
    for _ in range(12):
        append(row, "message_added")
        append(row, "provider_selected", provider="local")
    assert event_compaction.compact_old_turns(row, keep_turns=1) > 700
    assert projects.read_progress(row) == coverage
    assert context_compaction.receipt_projection(row, set(), 4000) == receipts


def test_process_compaction_preserves_progress_audit_counts_for_any_boundary(tmp_path):
    row = log(tmp_path, checkpoint=False)
    append(row, "tool_result", tool="inspect_service", call_id="one", result={"ok": True})
    first = row["event_cursor"]
    append(row, "tool_result", tool="inspect_service", call_id="two", result={"ok": True})
    second = row["event_cursor"]
    append(row, "tool_result", tool="inspect_service", result={"error": "failed"})
    append(row, "tool_result", tool="ryn_notes_read", result={"text": "internal"}, internal=True)
    append(row, "context_compacted", through=1)
    for _ in range(150):
        append(row, "provider_selected", provider="local")
    boundaries = [0, first, second, row["event_cursor"]]
    before = [budget._segment_successes(row, seq) for seq in boundaries]
    assert before[1:] == [1, 0, 0]
    assert event_compaction.compact_in_turn(row) > 700
    assert [budget._segment_successes(row, seq) for seq in boundaries] == before


def test_events_after_checkpoint_are_kept_even_outside_recent_suffix(tmp_path):
    row = log(tmp_path)
    for i in range(500):
        append(row, "provider_selected", index=1000+i)
    boundary = next(e["seq"] for e in row["events"] if e["type"] == "context_compacted")
    suffix = copy.deepcopy([e for e in row["events"] if e["seq"] >= boundary])
    assert event_compaction.compact_in_turn(row) > 700
    assert [e for e in row["events"] if e["seq"] >= boundary] == suffix


def test_t5_byte_checks_happen_only_every_twenty_steps_and_warning_is_once(tmp_path, monkeypatch):
    row = log(tmp_path, checkpoint=False)
    row["messages"][0]["content"] = "x" * (event_compaction.TASK_BYTES_HARD_LIMIT + 1)
    calls = []
    from rynmesh.agent_runtime.output_policy import encoded
    def counted(value):
        calls.append(value["steps"])
        return encoded(value)
    monkeypatch.setattr(event_compaction, "encoded", counted, raising=False)
    for step in range(1, 40):
        row["steps"] = step
        event_compaction.check_in_turn_limits(row)
    assert calls == [20]
    assert len([e for e in row["events"] if e["type"] == "event_log_near_limit"]) == 1
    assert row["messages"][0]["content"].endswith("x")


@pytest.mark.parametrize("intent", ["steer", "information"])
@pytest.mark.parametrize("checkpoint", [False, True])
def test_t6_t7_live_oversized_log_accepts_owner_messages(tmp_path, intent, checkpoint):
    rt, _, _, _ = make_runtime(tmp_path)
    task_id = rt.create({"message": "Long task"})["id"]
    def live(data):
        row = data["tasks"][task_id]
        for _ in range(1100):
            append(row, "provider_selected", provider="local")
        if checkpoint:
            append(row, "context_compacted", through=0)
        row.update(status="running", in_flight={"kind": "model", "dispatched": True})
    rt.store.transaction(live)
    for text in ("New owner instruction", "Another owner instruction"):
        current = rt.get(task_id)
        result = rt.add_message(task_id, {"message": text, "intent": intent,
            "expected_control_revision": current["control_revision"], "expected_goal_revision": current["goal"]["revision"]})
        assert result["id"] == task_id
    row = rt.store.read()["tasks"][task_id]
    assert [m["content"] for m in row["pending_user_messages"]] == ["New owner instruction", "Another owner instruction"]
    assert not row.get("continued_to")
    notices = [e for e in row["events"] if e["type"] == "event_log_near_limit"]
    if checkpoint:
        assert len(row["events"]) < 200 and not notices
    else:
        assert len(notices) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "paused"])
def test_t8_stopped_oversized_log_still_continues_in_a_new_task(tmp_path, status):
    rt, _, _, _ = make_runtime(tmp_path)
    task_id = rt.create({"message": "Long task"})["id"]
    def stop(data):
        row = data["tasks"][task_id]
        for _ in range(1100):
            append(row, "provider_selected", provider="local")
        row.update(status=status, queue=[], in_flight=None)
    rt.store.transaction(stop)
    result = rt.add_message(task_id, {"message": "Continue this task", "expected_control_revision": rt.get(task_id)["control_revision"]})
    assert result["id"] != task_id
    assert rt.store.read()["tasks"][task_id]["continued_to"] == result["id"]


def test_t9_real_model_mark_compacts_without_serializing_full_row(tmp_path, monkeypatch):
    rt, _, _, _ = make_runtime(tmp_path, assistant("Done"))
    task_id = rt.create({"message": "Long task"})["id"]
    def fill(data):
        row = data["tasks"][task_id]
        for _ in range(800):
            append(row, "provider_selected", provider="local")
        append(row, "context_compacted", through=0)
        for _ in range(150):
            append(row, "provider_selected", provider="local")
    rt.store.transaction(fill)
    def no_row_serialization(_):
        pytest.fail("model mark serialized a whole row before step 20")
    monkeypatch.setattr(event_compaction, "encoded", no_row_serialization, raising=False)
    advance(rt)
    row = rt.store.read()["tasks"][task_id]
    assert row["status"] == "completed" and len(row["events"]) < 200


def test_checkpoint_commit_compacts_events_in_the_same_transaction(tmp_path, monkeypatch):
    rt, model, _, _ = make_runtime(tmp_path, assistant("Goal: inspect A. Findings: A observed. Next: answer."), assistant("A inspected."))
    rt.update_settings({"max_context_chars": 12000})
    task_id = rt.create({"message": "Inspect A, never change anything."})["id"]
    seed(rt, task_id, exchange("a", "观测" * 1400))
    def fill(data):
        for _ in range(800):
            append(data["tasks"][task_id], "provider_selected", provider="local")
    rt.store.transaction(fill)
    real = getattr(event_compaction, "compact_in_turn", None)
    calls = []
    def compact(row, **kwargs):
        assert row["context_checkpoint"]["source_sha256"] == row["events"][-1]["source_sha256"]
        assert row["events"][-1]["type"] == "context_compacted"
        calls.append(row["id"])
        return real(row, **kwargs)
    monkeypatch.setattr(event_compaction, "compact_in_turn", compact, raising=False)
    advance(rt)
    row = rt.store.read()["tasks"][task_id]
    assert calls == [task_id]
    assert len(model.calls) == 2 and len(row["events"]) < 200


def test_preserved_events_over_hard_limit_only_warn_and_do_not_fail_model(tmp_path):
    rt, _, _, _ = make_runtime(tmp_path, assistant("Done"))
    task_id = rt.create({"message": "Keep receipts"})["id"]
    def fill(data):
        for _ in range(1100):
            append(data["tasks"][task_id], "future_evidence_type", data="keep")
        append(data["tasks"][task_id], "context_compacted", through=0)
    rt.store.transaction(fill)
    advance(rt)
    row = rt.store.read()["tasks"][task_id]
    assert row["status"] == "completed"
    assert len([e for e in row["events"] if e["type"] == "future_evidence_type"]) == 1100
    assert len([e for e in row["events"] if e["type"] == "event_log_near_limit"]) == 1
