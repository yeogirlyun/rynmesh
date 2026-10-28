"""Collection-time bindings; no external models and no invented acceptance verdicts."""
import copy
import hashlib
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def capture(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "docs/acceptance/ryn-agent-delivery/real-model"))
    module = importlib.import_module("role_query_world")
    record = {"case_id": "Q01", "cohort": "ryn", "run_id": "a" * 32, "run_nonce": "b" * 32,
              "dataset_version": module.composer.DATASET_VERSION, "collector_profile": "unit-capture-only"}
    calls = []
    tools = SimpleNamespace(execute=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})
    runtime = SimpleNamespace(tools=tools, store=SimpleNamespace(read=lambda: {"tasks": {"task-1": {"id": "task-1"}}}))
    controller = SimpleNamespace(effective_input=module.composer.load_effective_case("Q01"), case={"id": "Q01"})
    collector = module.DraftDispatchCollector(tmp_path, record)
    return collector, controller, runtime, calls, module


def register(capture):
    collector, controller, runtime, _, _ = capture
    collector.register(controller, runtime, {"id": "task-1"})
    return collector


def test_no_dispatch_before_durable_registration(capture):
    collector, _, _, calls, _ = capture
    with pytest.raises(RuntimeError, match="registration_missing"):
        collector.model_request()
    assert not calls and not collector.path.read_text()


def test_registration_uses_actual_bytes_and_unbound_effective_case_hash(capture):
    collector = register(capture)
    module = capture[-1]
    digest = hashlib.sha256((collector.output / "registration.json").read_bytes()).hexdigest()
    assert collector.registration_hash == collector.record["task_binding"]["registration_sha256"] == digest
    assert collector.binding["effective_case_sha256"] == module.composer.load_effective_case("Q01")["effective_sha256"]
    event = collector.model_request()
    assert event["registration_sha256"] == digest and event["task_id"] == "task-1"
    assert json.loads(collector.path.read_text()) == event


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_changed_registration_blocks_underlying_dispatch(capture, kind):
    collector = register(capture)
    (collector.output / "registration.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="registration_missing_or_changed"):
        if kind == "model":
            collector.model_request()
        else:
            capture[2].tools.execute("read", {}, context={"task_id": "task-1"})
    assert not collector.events and not capture[3]


def test_tool_task_binding_and_original_call_are_preserved(capture):
    collector = register(capture)
    tools, calls = capture[2].tools, capture[3]
    with pytest.raises(RuntimeError, match="tool_task_binding_mismatch"):
        tools.execute("read", {}, context={"task_id": "other"})
    assert not calls
    args, context = {"value": "unchanged"}, {"task_id": "task-1", "resource_grants": []}
    assert tools.execute("read", args, context=context, operation_id="op-1") == {"ok": True}
    assert calls == [(("read", args), {"context": context, "operation_id": "op-1"})]
    assert collector.events[0]["operation_id"] == "op-1"
    assert "value" not in collector.path.read_text()  # no arguments or bodies in audit


@pytest.mark.parametrize("mismatch", [None, "operation", "tool", "not_dispatched"])
def test_contextless_read_uses_durable_intent_without_adding_context(capture, mismatch):
    collector = register(capture)
    runtime, calls = capture[2], capture[3]
    flight = {"kind": "tool", "dispatched": True, "name": "read", "operation_id": "op-1"}
    if mismatch == "operation":
        flight["operation_id"] = "op-other"
    elif mismatch == "tool":
        flight["name"] = "another-tool"
    elif mismatch == "not_dispatched":
        flight["dispatched"] = False
    runtime.store.read = lambda: {"tasks": {"task-1": {"in_flight": flight}}}
    if mismatch:
        with pytest.raises(RuntimeError, match="durable_intent_mismatch"):
            runtime.tools.execute("read", {}, operation_id="op-1")
        assert not calls and not collector.events
    else:
        assert runtime.tools.execute("read", {}, operation_id="op-1") == {"ok": True}
        assert calls == [(("read", {}), {"operation_id": "op-1"})]


def test_durable_audit_precedes_underlying_tool_even_on_failure(capture):
    collector, controller, runtime, _, _ = capture
    def fail(*args, **kwargs):
        assert json.loads(collector.path.read_text())["tool_name"] == "read"
        raise ValueError("synthetic_failure")
    runtime.tools.execute = fail
    collector.register(controller, runtime, {"id": "task-1"})
    with pytest.raises(ValueError, match="synthetic_failure"):
        runtime.tools.execute("read", {}, context={"task_id": "task-1"})
    collector.record["error_type"] = "ValueError"
    collector.finalize()
    assert collector.record["dispatch_audit"]["coverage"] == "partial_dispatch_collection"
    assert not collector.record["dispatch_audit"]["completed_collection"]
    assert len(collector.record["dispatch_audit"]["events"]) == 1


def test_swallowed_capture_error_cannot_claim_complete_dispatch_coverage(capture):
    collector = register(capture)
    collector.model_request()
    with pytest.raises(RuntimeError, match="tool_task_binding_mismatch"):
        capture[2].tools.execute("read", {}, context={"task_id": "another-task"})
    # Runtime can serialize an observer exception into a tool error and continue.
    assert "error_type" not in collector.record
    collector.finalize()
    audit = collector.record["dispatch_audit"]
    assert audit["coverage"] == "partial_dispatch_collection" and not audit["completed_collection"]
    assert audit["capture_errors"] == ["draft_tool_task_binding_mismatch"]
    assert len(audit["events"]) == 1 and not capture[3]


def test_cannot_reopen_or_reregister_old_capture(capture):
    collector = register(capture)
    with pytest.raises(RuntimeError, match="already_started"):
        collector.register(capture[1], capture[2], {"id": "task-1"})
    with pytest.raises(FileExistsError):
        capture[-1].DraftDispatchCollector(collector.output, collector.record)


def test_reference_original_request_binding_and_exception_cleanup(capture):
    collector = register(capture)
    collector.record["cohort"] = "reference"
    native = {"id": "native-session:rpc:1", "rpc_id": 1, "session_id": "native-session", "method": "model",
              "sampling_binding": copy.deepcopy(collector.binding), "task_id": collector.task_id,
              "registration_sha256": collector.registration_hash, "emitted_at": "2026-10-10T08:00:00Z"}
    with pytest.raises(RuntimeError, match="context_missing"):
        collector.model_request()
    wrong = {**native, "task_id": "other-task"}
    with pytest.raises(RuntimeError, match="sampling_binding_mismatch"):
        with collector.reference_dispatch("model", {}, wrong):
            pytest.fail("unbound request reached dispatch")
    with pytest.raises(ValueError, match="synthetic"):
        with collector.reference_dispatch("model", {}, native):
            assert collector.model_request()["id"] == native["id"]
            raise ValueError("synthetic")
    with pytest.raises(RuntimeError, match="context_missing"):
        collector.model_request()
    assert len(collector.events) == len(collector.model_requests) == 1
    assert collector.events[0]["native_rpc"] == native


def test_missing_native_capture_remains_pending(capture):
    collector = register(capture)
    collector.finalize(collector.output / "missing.sqlite")
    native = collector.record["reference_session_readback"]
    assert native["status"] == "pending" and not native["observed_request_ids"]
    assert native["error_code"] == "draft_native_readback_incomplete"


def test_collected_dispatch_document_matches_frozen_grader_contract(capture):
    collector = register(capture)
    collector.model_request()
    capture[2].tools.execute("read", {}, context={"task_id": "task-1"})
    collector.finalize()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("capture_grader_contract", root / "tests/fixtures/agent_acceptance/role_grade_v2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    grader = module.DraftRoleGrader()
    envelope = grader.template("Q01")
    envelope.update(run_id=collector.record["run_id"], run_nonce=collector.record["run_nonce"], task_id=collector.task_id)
    envelope["run"].update(started_at=collector.record["registration"]["registered_at"], ended_at="2099-01-01T00:00:00Z")
    envelope["role_evidence"]["registration"] = "reg"
    assert grader.proof_binding(envelope) == collector.binding
    entries = {"reg": {"kind": "raw_trace", "sha256": collector.registration_hash},
               "models": {"kind": "model_exchange"}, "audit": {"kind": "raw_trace", "producer": "collector"}}
    documents = {"models": json.loads((collector.output / "model-requests.json").read_text(encoding="utf-8")),
                 "audit": json.loads((collector.output / "dispatch-audit.json").read_text(encoding="utf-8"))}
    failures, pending = [], []
    ids, _ = grader._dispatch_checks(envelope, entries, documents,
        {"dispatch_audit_source_ids": ["audit"], "first_dispatch_at": collector.record["first_dispatch_at"]},
        collector.record["task_binding"], failures, pending)
    assert not failures and not pending
    assert ids == {collector.model_requests[0]["id"]}
    # This checks one collector contract only, never a fabricated full-run grade.


@pytest.mark.parametrize("mismatch", ["audit", "sqlite_session"])
def test_native_sidecar_does_not_substitute_for_actual_session(capture, mismatch):
    collector = register(capture)
    native = {"id": "session:rpc:1", "rpc_id": 1, "session_id": "session", "method": "model",
              "sampling_binding": copy.deepcopy(collector.binding), "task_id": collector.task_id,
              "registration_sha256": collector.registration_hash, "emitted_at": "2026-10-10T08:00:00Z"}
    with collector.reference_dispatch("model", {}, native):
        collector.model_request()
    rows = [] if mismatch == "audit" else [native]
    (collector.output / "native-rpc-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    path = collector.output / "native.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table session(id text)")
        db.execute("create table message(session_id text)")
        db.execute("insert into session values (?)", ("other-session",))
        db.execute("insert into message values (?)", ("other-session",))
    collector.finalize(path)
    assert collector.record["reference_session_readback"]["status"] == "failed"
    assert not collector.record["reference_session_readback"]["observed_request_ids"]
