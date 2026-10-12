"""T04 mechanical evidence cannot be replaced by either model's assertion."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from test_agent_runtime import Tools, advance, approve, assistant, make_runtime
from test_agent_runtime_control import control, steer
from test_agent_runtime_retry import failing_model

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.verification import finish, parse_assessment


def assessment(status="passed", missing=None, refs=None):
    return assistant(json.dumps({"status": status, "summary": "Independent semantic assessment", "uncovered_requirements": missing or [], "evidence_refs": ["$supplied_mechanical_evidence"] if refs is None else refs}))


class VerifyingTools(Tools):
    def __init__(self):
        super().__init__()
        self.receipts = {}
        self.queries = []

    def execute(self, name, arguments, *, operation_id, cancelled):
        result = super().execute(name, arguments, operation_id=operation_id, cancelled=cancelled)
        self.receipts[operation_id] = result
        return result

    def reconcile(self, operation_id, *, context, cancelled):
        self.queries.append(operation_id)
        if operation_id not in self.receipts:
            return {"outcome": "unknown"}
        operation = context["operation"]
        return {"outcome": "executed", "receipt": {**{key: operation[key] for key in ("operation_id", "tool", "arguments_sha256")}, "result": self.receipts[operation_id]}}

    def verification_specs(self):
        return [{"id": "sharing_state", "tool_name": "share_service", "description": "Requested service is shared", "target_fields": ["service_id"], "kind": "mechanical"}]

    def verify(self, receipt, criteria, *, context, cancelled):
        assert criteria[0]["verifier"] == "sharing_state"
        assert receipt["operation_id"] == context["operation_id"]
        assert not cancelled()
        return {"status": "passed" if self.shared else "failed", "evidence": {"shared": self.shared,
                "current_state_matches": self.shared, "operation_attribution": receipt["status"] == "confirmed"}, "checked_at": "fixed"}


def verified_runtime(tmp_path, *responses):
    runtime, model, _, key = make_runtime(tmp_path, *responses)
    complete = model.complete
    def citing_model(model_id, messages, specs, cancelled, on_event):
        response = complete(model_id, messages, specs, cancelled, on_event)
        if not specs:
            content = response.get("message", {}).get("content", "")
            try:
                value = json.loads(content)
                if value.get("evidence_refs") == ["$supplied_mechanical_evidence"]:
                    payload = json.loads(messages[-1]["content"])
                    value["evidence_refs"] = [item["id"] for item in payload["evidence"]]
                    response = assistant(json.dumps(value))
            except (ValueError, KeyError, AttributeError):
                pass
        return response
    model.complete = citing_model
    tools = VerifyingTools()
    runtime.tools = tools
    return runtime, model, tools, key


def test_actual_write_readback_and_separate_goal_check_are_required(tmp_path):
    runtime, model, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"), assessment())
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 2)
    provisional = runtime.get(row["id"])
    assert provisional["status"] == "queued" and provisional["outcome"]["status"] == "unverified"
    advance(runtime, 2)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "verified"
    assert len(tools.calls) == 1 and len(model.calls) == 3
    assert model.calls[-1][2] == []
    assert len(model.calls[-1][1]) == 2  # Independent prompt, no execution conversation.
    assert done["steps"] == 3
    assert {c["kind"] for c in done["outcome"]["criteria_results"]} == {"semantic", "mechanical"}
    evidence = done["verification_evidence"][0]["evidence"]
    assert evidence["shared"] is True


def test_contradictory_checker_downgrades_then_repairs_after_reopen_without_repeating_write(tmp_path):
    runtime, model, tools, key = verified_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "A"}), assistant("A shared; all services healthy"),
        assessment("passed", ["Health of other services is not established."]),
        assistant("A is shared. Other service health was not checked."), assessment())
    row = runtime.create({"message": "Share A and report actual observations"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 4)
    pending = runtime.get(row["id"])
    assert pending["status"] == "queued" and pending["outcome"]["status"] == "unverified"
    semantic = pending["outcome"]["semantic_assessment"]
    assert semantic["status"] == "unknown" and semantic["reported_status"] == "passed"
    assert semantic["uncovered_requirements"] == ["Health of other services is not established."]
    assert any(event["type"] == "verification_assessment_normalized" for event in pending["events"])
    assert not any(event["type"] == "verification_format_retry_scheduled" for event in pending["events"])
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    reopened.reconcile()
    advance(reopened, 3)
    done = reopened.get(row["id"])
    assert done["outcome"]["status"] == "verified"
    assert done["result"] == "A is shared. Other service health was not checked."
    assert len(tools.calls) == 1 and len(model.calls) == 5
    assert reopened.store.read()["tasks"][row["id"]]["verification_cycle"]["repairs"] == 1


@pytest.mark.parametrize("refs", [[], ["forged"]])
def test_contradictory_checker_still_rejects_missing_or_forged_required_evidence(refs):
    row = {"goal": {"revision": 1}, "criteria": [{"required": True, "kind": "mechanical", "evidence_refs": ["current-proof"]}],
           "verification_evidence": [{"id": "current-proof"}], "receipts": []}
    with pytest.raises(AgentError, match="agent_invalid_verification_response"):
        parse_assessment(assessment("passed", ["Unestablished claim"], refs), row)


def test_denied_write_and_false_final_prose_never_pass_even_if_checker_agrees(tmp_path):
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}),
        assistant(name="inspect_service", args={"service_id": "A"}), assistant("Verified sharing succeeded"), assessment())
    row = runtime.create({"message": "Share A and confirm"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    control(runtime, row["id"], "deny", approval_id=waiting["pending_approval"]["id"])
    advance(runtime, 5)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "not_achieved"
    assert not tools.shared and len(tools.calls) == 1
    assert done["result"] == "Verified sharing succeeded"  # Original statement retained for audit.
    assert done["verification_evidence"][0]["evidence"]["shared"] is False
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["receipts"][0]["status"] == "not_executed"
    assert "owner_denied_operation" in json.dumps(stored["receipts"])


def test_incomplete_owner_goal_cannot_pass_by_checking_only_easy_target(tmp_path):
    runtime, model, _, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"),
        assessment("failed", ["B was also required and was not handled."]))
    row = runtime.create({"message": "Share A and B; explain both results"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 4)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "partial"
    assert "A and B" in json.dumps(model.calls[-1][1])
    assert done["outcome"]["uncertainties"] == ["B was also required and was not handled."]


@pytest.mark.parametrize("receipt_changes,criterion_changes,expected", [
    ({}, {}, "partial"),
    ({"effect": "read"}, {}, "not_achieved"),
    ({"status": "not_executed"}, {}, "not_achieved"),
    ({"status": "unknown"}, {}, "not_achieved"),
    ({"status": "failed"}, {}, "not_achieved"),
    ({"goal_revision": 1}, {}, "not_achieved"),
    ({"context_epoch": 2}, {}, "not_achieved"),
    ({"context_epoch": None}, {}, "not_achieved"),
    ({}, {"source_ref": "unrelated-receipt"}, "not_achieved"),
    ({}, {"required": False}, "not_achieved"),
    ({}, {"goal_revision": 1}, "not_achieved"),
    ({}, {"source": "model"}, "not_achieved"),
    ({}, {"kind": "semantic"}, "not_achieved"),
    ({}, {"status": "failed"}, "not_achieved"),
])
def test_partial_requires_bound_current_confirmed_write(receipt_changes, criterion_changes, expected):
    receipt = {"id": "receipt-save", "effect": "write", "status": "confirmed", "goal_revision": 2,
               "context_epoch": 3, "result": {"source_revision": "actually-saved"}, **receipt_changes}
    criterion = {"id": "save", "source": "tool", "source_ref": receipt["id"], "goal_revision": 2,
                 "required": True, "kind": "mechanical", "status": "passed", "description": "Saved source",
                 "evidence_refs": ["save-proof"], **criterion_changes}
    row = {"goal": {"revision": 2}, "context_epoch": 3, "receipts": [receipt], "criteria": [
        {"id": "goal-coverage", "required": True, "kind": "semantic", "status": "unknown", "evidence_refs": []},
        {"id": "prepare", "required": True, "kind": "mechanical", "status": "passed", "description": "Draft exists",
         "source": "tool", "source_ref": "receipt-prepare", "goal_revision": 2, "evidence_refs": ["prepare-proof"]},
        criterion,
        {"id": "probe", "required": True, "kind": "mechanical", "status": "failed", "description": "Probe failed",
         "evidence_refs": ["probe-proof"]},
    ]}
    finish(row)
    assert row["outcome"]["status"] == expected
    assert row["receipts"] == [receipt] and receipt["result"] == {"source_revision": "actually-saved"}


def test_complete_read_only_goal_still_verifies_without_write_progress():
    row = {"goal": {"revision": 1}, "receipts": [], "criteria": [
        {"id": "goal-coverage", "required": True, "kind": "semantic", "status": "passed", "evidence_refs": ["read-proof"]},
        {"id": "read", "required": True, "kind": "mechanical", "status": "passed", "description": "Observed current facts",
         "source": "tool", "source_ref": "receipt-read", "goal_revision": 1, "evidence_refs": ["read-proof"]},
    ]}
    finish(row)
    assert row["outcome"]["status"] == "verified"


@pytest.mark.parametrize("response", [assistant("Trust me, verified"), assistant(name="share_service", args={"service_id": "B"}),
    assistant(json.dumps({"status": "passed", "summary": "Invented proof", "uncovered_requirements": [], "evidence_refs": ["fake-proof"]}))])
def test_malformed_or_tool_requesting_checker_stays_unverified(tmp_path, response):
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant("An answer"), response, response)
    row = runtime.create({"message": "Explain a concept"})
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["outcome"]["status"] == "unverified"
    assert not tools.calls and not done["plan"]["steps"]
    assert "agent_invalid_verification_response" in done["outcome"]["uncertainties"]


def test_verification_budget_exhaustion_keeps_answer_without_fabricating_verified(tmp_path):
    runtime, model, _, _ = verified_runtime(tmp_path, assistant("Answer"))
    runtime.update_settings({"max_steps": 1})
    row = runtime.create({"message": "Explain"})
    advance(runtime, 2)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["outcome"]["status"] == "unverified"
    assert done["result"] == "Answer" and len(model.calls) == 1
    assert "agent_step_limit" in done["outcome"]["uncertainties"]


def test_steer_while_goal_checker_running_invalidates_old_assessment(tmp_path):
    runtime, model, _, _ = verified_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def old_check():
        entered.set()
        assert release.wait(3)
        return assessment(refs=[])
    failing_model(model, assistant("Old answer"), old_check, assistant("New answer"), assessment(refs=[]))
    row = runtime.create({"message": "Explain old topic"})
    advance(runtime)
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    changed = steer(runtime, row["id"], "Explain new topic instead")
    assert changed["outcome"]["status"] == "unverified"
    release.set()
    worker.join(3)
    assert runtime.get(row["id"])["outcome"]["status"] == "unverified"
    advance(runtime, 2)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "unverified" and done["goal"]["revision"] == 2
    assert done["criteria"][0]["status"] == "passed"  # Semantic assessment remains explicitly separate.
    assert done["result"] == "New answer"


def test_unknown_verifier_error_never_reexecutes_mutation(tmp_path):
    runtime, model, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"))
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 2)
    def broken(*args, **kwargs):
        raise AgentError("agent_model_connection_failed")
    tools.verify = broken
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "unverified"
    assert len(tools.calls) == 1 and len(model.calls) == 2


def unknown_write(runtime, tools):
    execute = tools.execute
    def lost_response(*args, **kwargs):
        execute(*args, **kwargs)
        raise RuntimeError("response lost after commit")
    tools.execute = lost_response
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime)
    assert runtime.get(row["id"])["outcome"]["status"] == "unknown"
    return row


def test_exact_original_operation_query_resolves_without_replaying_write(tmp_path):
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    row = unknown_write(runtime, tools)
    advance(runtime)
    recovered = runtime.get(row["id"])
    assert recovered["status"] == "queued" and recovered["error"] is None
    assert recovered["reconciliation"]["state"] == "executed"
    assert recovered["outcome"]["status"] == "unverified"
    assert len(tools.calls) == len(tools.queries) == 1
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["receipts"][0]["status"] == "confirmed"
    assert stored["in_flight"] is None and stored["queue"] == []
    assert json.loads(stored["messages"][-1]["content"])["shared"] is True


@pytest.mark.parametrize("intervention", ["pause", "cancel", "permission", "disable", "legacy"])
def test_exact_reconciliation_does_not_override_owner_control_or_legacy_authority(tmp_path, intervention):
    runtime, model, tools, key = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    row = unknown_write(runtime, tools)
    if intervention in {"pause", "cancel"}:
        control(runtime, row["id"], intervention)
    elif intervention == "permission":
        runtime.update_settings({"permission_mode": "read_only"})
    elif intervention == "disable":
        runtime.update_settings({"enabled": False})
        runtime.update_settings({"enabled": True})
    else:
        runtime.store.transaction(lambda data: data["tasks"][row["id"]]["in_flight"].pop("authority"))
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    advance(restarted)
    recovered = restarted.get(row["id"])
    assert recovered["status"] == ("cancelled" if intervention == "cancel" else "paused")
    assert recovered["reconciliation"]["state"] == "executed"
    assert len(tools.calls) == 1
    assert recovered["events"][-1]["continued"] is False


def test_restart_matching_current_state_cannot_prove_unknown_operation(tmp_path):
    runtime, model, tools, key = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    row = unknown_write(runtime, tools)
    fresh_tools = VerifyingTools()
    fresh_tools.shared = True  # Present state matches, original-op receipt lost.
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, fresh_tools, runtime.catalog)
    restarted.reconcile()
    advance(restarted, 3)
    unknown = restarted.get(row["id"])
    assert unknown["status"] == "interrupted" and unknown["outcome"]["status"] == "unknown"
    assert unknown["reconciliation"]["state"] == "unknown"
    assert len(fresh_tools.queries) == 1 and not fresh_tools.calls
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        control(restarted, row["id"], "resume")
    control(restarted, row["id"], "reconcile")
    advance(restarted)
    assert len(fresh_tools.queries) == 2 and not fresh_tools.calls


def test_mismatched_reconciliation_identity_cannot_clear_unknown_marker(tmp_path):
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    row = unknown_write(runtime, tools)
    tools.reconcile = lambda *a, **k: {"outcome": "executed", "receipt": {"operation_id": "different", "tool": "share_service", "arguments_sha256": "wrong", "result": {"shared": True}}}
    advance(runtime)
    unresolved = runtime.get(row["id"])
    assert unresolved["error"] == "agent_write_outcome_unknown"
    assert unresolved["outcome"]["status"] == "unknown" and len(tools.calls) == 1


def test_two_models_asserting_success_without_business_evidence_cannot_verify(tmp_path):
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant("A is now shared"), assessment())
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "unverified" and not tools.calls
    assert done["criteria"][0]["status"] == "passed"


def test_pause_and_restart_during_verification_keep_original_receipt(tmp_path):
    runtime, model, tools, key = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"), assessment())
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 2)
    control(runtime, row["id"], "pause")
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    assert restarted.get(row["id"])["status"] == "paused"
    control(restarted, row["id"], "resume")
    advance(restarted, 2)
    done = restarted.get(row["id"])
    assert done["outcome"]["status"] == "verified"
    assert len(tools.calls) == 1


def test_checker_without_current_mechanical_references_cannot_verify(tmp_path):
    runtime, _, _, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"), assessment(refs=[]), assessment(refs=[]))
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 5)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "unverified"
    assert "agent_invalid_verification_response" in done["outcome"]["uncertainties"]


@pytest.mark.parametrize("old_field", ["goal_revision", "context_epoch"])
def test_checker_cannot_cite_receipt_excluded_from_current_payload(tmp_path, old_field):
    runtime, _, _, _ = verified_runtime(tmp_path, assistant("Answer"), assessment(refs=["old-receipt"]), assessment(refs=["old-receipt"]))
    row = runtime.create({"message": "Explain"})
    def old_receipt(data):
        task = data["tasks"][row["id"]]
        receipt = {"id": "old-receipt", "goal_revision": 1, "context_epoch": 0, "effect": "read"}
        receipt[old_field] = -1
        task["receipts"] = [receipt]
    runtime.store.transaction(old_receipt)
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "unverified"
    assert "agent_invalid_verification_response" in done["outcome"]["uncertainties"]


@pytest.mark.parametrize("wrapper", ["```json\n{}\n```", "```\n{}\n```", " \n```JSON\n{}\n```\n "])
def test_single_outer_json_fence_preserves_strict_evidence_validation(wrapper):
    row = {"goal": {"revision": 1}, "criteria": [{"required": True, "kind": "mechanical", "evidence_refs": ["current-proof"]}],
           "verification_evidence": [{"id": "current-proof"}], "receipts": []}
    value = {"status": "passed", "summary": "Goal covered", "uncovered_requirements": [], "evidence_refs": ["current-proof"]}
    assert parse_assessment(assistant(wrapper.format(json.dumps(value))), row) == value
    value["evidence_refs"] = ["forged"]
    with pytest.raises(AgentError, match="agent_invalid_verification_response"):
        parse_assessment(assistant(wrapper.format(json.dumps(value))), row)


@pytest.mark.parametrize("wrapper", ["Here is the result:\n```json\n{}\n```", "```json\n{}\n```\nTrust me", "```json\n{}\n```\n```json\n{{}}\n```", "{}\n{{}}"])
def test_checker_fence_with_prose_or_multiple_json_values_is_rejected(wrapper):
    row = {"goal": {"revision": 1}, "criteria": [], "verification_evidence": [], "receipts": []}
    value = {"status": "passed", "summary": "Goal covered", "uncovered_requirements": [], "evidence_refs": []}
    with pytest.raises(AgentError, match="agent_invalid_verification_response"):
        parse_assessment(assistant(wrapper.format(json.dumps(value))), row)


def test_real_truncated_f02_response_is_recorded_then_recovers_once(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures/agent_runtime/truncated-verification.json").read_text(encoding="utf-8"))
    runtime, model, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"),
                                               fixture["response"], assessment())
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 4)
    retrying = runtime.store.read()["tasks"][row["id"]]
    assert retrying["status"] == "queued" and retrying["verification"]["format_retries"] == 1
    assert retrying["verification_rejected_responses"][0]["content"] == fixture["response"]["message"]["content"]
    assert retrying["verification_rejected_responses"][0]["usage"]["output_tokens"] == 4096
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "verified" and done["steps"] == 4
    assert len(tools.calls) == 1 and len(model.calls) == 4
    assert "previous response was invalid" in model.calls[-1][1][0]["content"]
    assert "verification_rejected_responses" not in done  # Private diagnostic body is not public event prose.


def test_missing_explanation_gets_one_continuation_without_replaying_confirmed_write(tmp_path):
    runtime, model, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"),
        assessment("failed", ["Explain the resulting state."]), assistant(name="inspect_service", args={"service_id": "A"}),
        assistant("A is shared; existing state read back."), assessment())
    row = runtime.create({"message": "Share A and explain the final state"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 9)
    done = runtime.get(row["id"])
    assert done["outcome"]["status"] == "verified"
    assert [call[0] for call in tools.calls] == ["share_service", "inspect_service"]
    assert len([e for e in done["events"] if e["type"] == "verification_retry_scheduled"]) == 1
    assert "Server verification feedback" in json.dumps(model.calls[3][1])
    assert runtime.store.read()["tasks"][row["id"]]["verification_history"]


def test_failed_correction_stops_after_one_continuation(tmp_path):
    runtime, model, _, _ = verified_runtime(tmp_path, assistant("Insufficient"), assessment("failed", ["Missing information"]),
        assistant("Still insufficient"), assessment("failed", ["Still missing information"]))
    row = runtime.create({"message": "Explain using necessary facts"})
    advance(runtime, 8)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["outcome"]["status"] == "not_achieved"
    assert len(model.calls) == 4
    assert len([e for e in done["events"] if e["type"] == "verification_retry_scheduled"]) == 1


def test_verification_continuation_can_wait_for_necessary_owner_input(tmp_path):
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant("Need location"), assessment("unknown", ["Target location is missing"]),
        assistant(name="ryn_request_input", args={"goal_revision": 1, "kind": "information", "prompt": "Which location?",
            "fields": [{"id": "location", "label": "Location", "type": "text", "required": True}]}))
    row = runtime.create({"message": "Inspect the selected location"})
    advance(runtime, 4)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_input" and waiting["pending_input"]["prompt"] == "Which location?"
    assert not tools.calls
