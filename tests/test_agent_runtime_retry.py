"""Persistent inference retries never extend to uncertain business mutations."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_agent_runtime import Catalog, advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import retry_policy
from rynmesh.agent_runtime import runtime as runtime_module
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


@pytest.fixture(autouse=True)
def immediate_retry(monkeypatch):
    # This module tests retry safety; durable timing has dedicated clock-driven tests.
    monkeypatch.setattr(retry_policy, "delay", lambda *_: 0)


def failing_model(model, *outcomes):
    pending = list(outcomes)

    def complete(model_id, messages, specs, cancelled, on_event, **options):
        if "expected_destination" in options:
            assert options["expected_destination"] == model.destination
        model.calls.append((model_id, messages, specs))
        if not specs and getattr(model, "verification_auto", False):
            import json
            payload = json.loads(messages[-1]["content"])
            return assistant(json.dumps({"status": "passed", "summary": "Fixture readback facts covered", "uncovered_requirements": [],
                                         "evidence_refs": [item["id"] for item in payload["evidence"]]}))
        value = pending.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value
    model.complete = complete


@pytest.mark.parametrize("code", ["agent_inference_timeout", "agent_model_connection_failed", "agent_model_busy", "agent_model_upstream_failed",
                                 "agent_invalid_model_response", "agent_compaction_size_limit"])
def test_one_model_retry_is_persisted_and_succeeds(tmp_path, code):
    runtime, model, tools, key = make_runtime(tmp_path)
    failing_model(model, AgentError(code), assistant("Recovered."))
    row = runtime.create({"message": "Inspect availability"})
    advance(runtime)
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["status"] == "queued" and stored["model_retry_count"] == 1
    assert stored["steps"] == 1 and stored["in_flight"] is None
    assert stored["events"][-1]["type"] == "model_retry_scheduled"
    assert stored["events"][-1]["error"] == code
    assert stored["elapsed_seconds"] >= 0
    # Restart neither clears the durable retry counter nor duplicates an operation.
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    advance(restarted)
    done = restarted.store.read()["tasks"][row["id"]]
    assert done["status"] == "completed" and done["steps"] == 2
    assert done["model_retry_count"] == 0
    assert done["elapsed_seconds"] >= stored["elapsed_seconds"]
    assert len(model.calls) == 2 and tools.calls == []


def test_second_timeout_pauses_connection_even_after_restart(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path)
    failing_model(model, AgentError("agent_model_busy"), AgentError("agent_inference_timeout"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    advance(restarted, 3)
    done = restarted.get(row["id"])
    assert done["status"] == "paused" and done["error"] is None
    assert done["connection_recovery"]["state"] == "disconnected"
    assert done["steps"] == len(model.calls) == 2
    assert len([e for e in done["events"] if e["type"] == "model_retry_scheduled"]) == 1


@pytest.mark.parametrize("code", ["agent_inference_failed", "agent_model_unavailable", "agent_operation_failed"])
def test_unknown_or_nontransient_model_error_never_retries(tmp_path, code):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, AgentError(code))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    assert runtime.get(row["id"])["status"] == "failed"
    assert len(model.calls) == 1


def test_retry_cannot_exceed_step_budget(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_steps": 1})
    failing_model(model, AgentError("agent_model_busy"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    assert runtime.get(row["id"])["error"] == "agent_step_limit"
    assert len(model.calls) == 1


def test_retry_accumulates_active_time_and_stops_at_limit(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    runtime.update_settings({"max_seconds": 10})

    def first():
        clock[0] += 6
        raise AgentError("agent_inference_timeout")

    def second():
        clock[0] += 5
        raise AgentError("agent_model_busy")
    failing_model(model, first, second)
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["status"] == "failed" and stored["error"] == "agent_time_limit"
    assert stored["elapsed_seconds"] == pytest.approx(11, abs=0.001)
    assert stored["steps"] == 2


@pytest.mark.parametrize("control", ["pause", "cancel", "disable", "read_only", "revoke_allowlist"])
def test_late_model_failure_does_not_revive_owner_control(tmp_path, control):
    runtime, model, _, _ = make_runtime(tmp_path)
    if control == "revoke_allowlist":
        runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    row = runtime.create({"message": "Inspect"})

    def interrupted():
        current = runtime.get(row["id"])
        if control in {"pause", "cancel"}:
            runtime.action(row["id"], {"action": control, "expected_revision": current["revision"]})
        elif control == "disable":
            runtime.update_settings({"enabled": False})
        elif control == "read_only":
            runtime.update_settings({"permission_mode": "read_only"})
        else:
            runtime.update_settings({"allowed_write_tools": []})
        raise AgentError("agent_model_busy")
    failing_model(model, interrupted)
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["status"] == ("paused" if control in {"pause", "disable"} else "cancelled" if control == "cancel" else "failed")
    assert not any(e["type"] == "model_retry_scheduled" for e in done["events"])
    assert len(model.calls) == 1


@pytest.mark.parametrize("control", ["cancel", "read_only", "disable"])
def test_control_between_failure_and_retry_prevents_dispatch(tmp_path, control):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, AgentError("agent_model_busy"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    if control == "cancel":
        runtime.action(row["id"], {"action": "cancel", "expected_revision": runtime.get(row["id"])["revision"]})
    else:
        runtime.update_settings({"permission_mode": "read_only"} if control == "read_only" else {"enabled": False})
    advance(runtime, 2)
    assert len(model.calls) == 1
    assert runtime.get(row["id"])["status"] in {"cancelled", "paused", "failed"}


def test_success_resets_consecutive_retry_counter(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, AgentError("agent_model_busy"), assistant(name="inspect_service", args={"service_id": "svc"}),
                  AgentError("agent_inference_timeout"), assistant("Done"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 5)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["steps"] == 4
    assert len([e for e in done["events"] if e["type"] == "model_retry_scheduled"]) == 2


def test_cancelled_scheduled_retry_stays_cancelled_after_restart(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path)
    failing_model(model, AgentError("agent_model_busy"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    runtime.action(row["id"], {"action": "cancel", "expected_revision": runtime.get(row["id"])["revision"]})
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    advance(restarted, 2)
    assert restarted.get(row["id"])["status"] == "cancelled"
    assert len(model.calls) == 1


def test_write_error_with_transient_model_code_is_never_replayed(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc"}))
    row = runtime.create({"message": "Share"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    execute = tools.execute

    def ambiguous(*args, **kwargs):
        execute(*args, **kwargs)
        raise AgentError("agent_inference_timeout")
    tools.execute = ambiguous
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["status"] == "interrupted" and done["error"] == "agent_write_outcome_unknown"
    assert len(tools.calls) == 1 and len(model.calls) == 1
    assert not any(e["type"] == "model_retry_scheduled" for e in done["events"])
