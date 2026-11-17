"""S13-I53: bound durable history while preserving accounting and delivery."""
import copy
import hashlib
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from test_agent_runtime import make_runtime, assistant, advance
from test_agent_context_compaction_policy import WindowCatalog, fill_until_near_limit
from test_agent_runtime_budget import task
from test_agent_runtime_model_switch import setup, body
from test_agent_js_integration import _seed_task_row, HOST_METHOD_MAPPING
from rynmesh.agent_runtime import budget, projects, results, tool_delivery
from rynmesh.agent_runtime.js_integration import plan_child_call, select_delivery
from rynmesh.agent_runtime.js_runtime.protocol import PendingHostCallEnvelope, canonical_arguments_sha256
from rynmesh.agent_runtime.protocol import event_result
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded


@pytest.mark.parametrize("unknown", [False, True])
def test_T1_s13_i53_hundred_rounds_preserve_exact_lifetime(unknown):
    row = task()
    for n in range(100):
        model = budget.reserve(row, "model", "main", input_bound=100, output_bound=100)
        usage = None if unknown and n == 0 else {"input_tokens": 11, "output_tokens": 7, "cached_tokens": 3}
        budget.settle(row, model["id"], usage=usage, elapsed_seconds=1)
        tool = budget.reserve(row, "tool", "read")
        budget.settle(row, tool["id"], elapsed_seconds=0.5)
        if n < 99:
            budget.new_round(row)
    # Compare to the same raw history with compaction disabled by reconstruction.
    expected = task()
    for n in range(100):
        expected.setdefault("budget", copy.deepcopy(budget.initialize(expected)))
        expected["budget"]["round"] = n + 1
        model = budget.reserve(expected, "model", "main", input_bound=100, output_bound=100)
        budget.settle(expected, model["id"], usage=None if unknown and n == 0 else {"input_tokens": 11, "output_tokens": 7, "cached_tokens": 3}, elapsed_seconds=1)
        tool = budget.reserve(expected, "tool", "read")
        budget.settle(expected, tool["id"], elapsed_seconds=0.5)
    assert budget.public(row)["lifetime"] == budget.public(expected)["lifetime"]
    assert {record["round"] for record in row["budget"]["records"]} == {99, 100}
    assert len(row["budget"]["round_totals"]) == 98
    assert budget.public(row)["current_round"] == budget.public(expected)["current_round"]


def test_T1_s13_i53_reserved_record_and_hard_time_limit_survive_compaction():
    row = task(max_total_seconds=10)
    reserved = budget.reserve(row, "tool", "pending", seconds_bound=1)
    spent = budget.reserve(row, "tool", "done", seconds_bound=9)
    budget.settle(row, spent["id"], elapsed_seconds=9)
    budget.new_round(row)
    budget.new_round(row)
    assert any(record["id"] == reserved["id"] for record in row["budget"]["records"])
    assert budget.time_remaining(row) == 1
    budget.settle(row, reserved["id"], elapsed_seconds=1)
    assert budget.time_remaining(row) == 0
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        budget.reserve(row, "tool", "too_late", seconds_bound=2)


def js_completion(tmp_path, record_receipt=True):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    _, catalog = _seed_task_row(store)
    arguments = {"window": {"app": "Notepad", "id": 1}, "x": 10, "y": 20, "click_count": 1, "mouse_button": "left"}
    envelope = PendingHostCallEnvelope(task_id="task_js_phase_a", session_id="sess_001", generation=1,
        execution_id="exec_001", callback_id="cb_write_1", ordinal=0, method="click", effect="write",
        target_tool="computer_act_window", arguments=arguments, arguments_sha256=canonical_arguments_sha256(arguments))
    call = store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], envelope, catalog, HOST_METHOD_MAPPING))
    payload = {"ok": True, "text": "verified "*1000}
    token = store.transaction(lambda data: tool_delivery.stage_child_completion(data["tasks"]["task_js_phase_a"], call,
        receipt_status="confirmed" if record_receipt else "not_executed", result=payload, record_receipt=record_receipt))
    return store, token, payload


def test_T2_s13_i53_outbox_reads_receipt_and_compacts_event(tmp_path):
    store, token, payload = js_completion(tmp_path)
    row = store.read()["tasks"][token.task_id]
    outbox = row["js_execution"]["delivery_outbox"][0]
    assert "payload" not in outbox and outbox["receipt_id"]
    live = {"task_id": row["id"], "session_id": "sess_001", "generation": 1, "execution_id": "exec_001", "alive": True}
    assert select_delivery(row, token, live).result == payload
    recorded = next(item for item in row["events"] if item["type"] == "js_child_result")
    assert "text" not in recorded["result"]
    assert event_result(row, recorded) == payload
    row["receipts"][0]["result"]["text"] = "tampered"
    with pytest.raises(AgentError):
        select_delivery(row, token, live)


def test_T3_s13_i53_twenty_model_switches_keep_eight_summaries(tmp_path):
    runtime, _, _, _, tid, _ = setup(tmp_path)
    for i in range(20):
        def add(data):
            row = data["tasks"][tid]
            row["plan"]["steps"] = [{"title": f"Step {i}", "status": "done", "private_body": "huge"*1000}]
            row["verification_evidence"] = [{"proof": "large"*1000}]
        runtime.store.transaction(add)
        runtime.switch_model(tid, body(runtime, tid, "local:other" if i%2 == 0 else "local:test"))
    row = runtime.store.read()["tasks"][tid]
    assert all(len(row[key]) == 8 for key in ("model_history", "plan_history", "verification_history"))
    assert row["plan_history"][-1]["steps"] == [{"title": "Step 19", "status": "done"}]
    history = row["verification_history"][-1]
    assert "evidence" not in history and history["evidence_count"] == 1
    assert history["evidence_sha256"] == hashlib.sha256(encoded([{"proof": "large"*1000}])).hexdigest()


def test_T4_s13_i53_prune_old_results_preserves_checkpoint_and_expires_reads(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    tid = runtime.create({"message": "Bound disk usage"})["id"]
    row = runtime.store.read()["tasks"][tid]
    row["result_refs"] = []
    for i in range(36):
        ref = results.seal(runtime.store, row, {"name": "read", "operation_id": f"read{i}"}, {"text": "x"*2_000_000})
        row["result_refs"].append(ref)
    protected = row["result_refs"][0]
    row["context_epoch"] = 1
    row["result_refs"][-1]["context_epoch"] = 1
    row["context_checkpoint"] = {"through": 1, "content": "Keep", "references": {"result_ref": protected["id"]}}
    orphan = results.seal(runtime.store, row, {"name": "read", "operation_id": "orphan"}, {"text": "orphan"})
    results.prune(runtime.store, row)
    expired = [ref for ref in row["result_refs"] if ref.get("expired")]
    assert expired and not protected.get("expired")
    assert results.path(runtime.store, row["id"], protected["id"]).exists()
    assert not results.path(runtime.store, row["id"], orphan["id"]).exists()
    for ref in expired:
        assert not results.path(runtime.store, row["id"], ref["id"]).exists()
        with pytest.raises(AgentError, match="agent_result_expired"):
            results.read(runtime.store, row, ref["id"])


def test_T4_s13_i53_real_model_switch_invokes_prune(tmp_path, monkeypatch):
    runtime, _, _, _, tid, _ = setup(tmp_path)
    calls = []
    monkeypatch.setattr(results, "prune", lambda store, row: calls.append((row["id"], row["context_epoch"])), raising=False)
    runtime.switch_model(tid, body(runtime, tid))
    assert calls == [(tid, 1)]


def test_T4_s13_i53_real_compaction_cleans_unregistered_ciphertext(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant("Goal: inspect every service.\nFindings: all observed healthy.\nNext: report."),
        assistant("All inspected; nothing changed."))
    runtime.catalog = WindowCatalog(32_000, 4096)
    tid = runtime.create({"message": "Inspect every service; never change anything."})["id"]
    row = runtime.store.read()["tasks"][tid]
    ref = results.seal(runtime.store, row, {"name": "read", "operation_id": "unregistered"}, {"text": "orphan"})
    path = results.path(runtime.store, tid, ref["id"])
    fill_until_near_limit(runtime, tid, "x"*5000, limit=runtime._context_limit(row))
    assert path.exists()
    advance(runtime)
    assert len(model.calls) == 2 and runtime.store.read()["tasks"][tid]["context_checkpoint"]
    assert not path.exists()


def test_T3_s13_i53_existing_history_is_compacted_on_next_switch(tmp_path):
    runtime, _, _, _, tid, _ = setup(tmp_path)
    old_plan = {"revision": 1, "steps": [{"text": "Old step", "status": "done", "body": "private"*1000}]}
    evidence = [{"proof": "private"*1000}]
    def seed(data):
        row = data["tasks"][tid]
        row["plan_history"] = [copy.deepcopy(old_plan) for _ in range(20)]
        row["verification_history"] = [{"outcome": {"status": "unverified"}, "evidence": copy.deepcopy(evidence)} for _ in range(20)]
    runtime.store.transaction(seed)
    runtime.switch_model(tid, body(runtime, tid))
    row = runtime.store.read()["tasks"][tid]
    assert len(row["plan_history"]) == len(row["verification_history"]) == 8
    assert row["plan_history"][0] == {"steps": [{"title": "Old step", "status": "done"}]}
    assert row["verification_history"][0] == {"outcome": {"status": "unverified"}, "evidence_count": 1,
        "evidence_sha256": hashlib.sha256(encoded(evidence)).hexdigest()}


def test_T5_s13_i53_removed_projects_do_not_consume_active_slots(tmp_path):
    data = {"tasks": {}, "projects": {}}
    for i in range(140):
        root = tmp_path / str(i)
        root.mkdir()
        item = projects.register(data, {"path": str(root)})
        item["enabled"] = False
        if i == 0:
            data["tasks"]["keep"] = {"local_execution": {"project_id": item["id"]}}
    assert len(data["projects"]) <= 128
    assert data["tasks"]["keep"]["local_execution"]["project_id"] in data["projects"]


def test_T5_s13_i53_project_age_does_not_depend_on_json_key_order(tmp_path):
    data = {"tasks": {}, "projects": {f"project_{i}": {"id": f"project_{i}", "enabled": False, "created_at": i}
                                         for i in reversed(range(128))}}
    projects.register(data, {"path": str(tmp_path)})
    assert len(data["projects"]) == 128
    assert "project_0" not in data["projects"] and "project_127" in data["projects"]
