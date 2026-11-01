"""Bounded post-answer evidence; deterministic execution, no inference service."""
import copy
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_agent_resources import file_ref, invoke, resources  # noqa: F401
from test_agent_runtime import advance, approve, assistant
from test_agent_runtime_verification import assessment, verified_runtime

from rynmesh.agent_runtime import verification
from rynmesh.agent_runtime.read_verification import READ_SPECS, verify_read
from rynmesh.agent_runtime.store import encoded


def stamp():
    return datetime.now(timezone.utc).isoformat()


def completed_cycle(count=1):
    destination = {"catalog_model_id": "local/test", "destination_revision": "generation-1"}
    row = {"id": "task-current", "model_id": "local/test", "model_destination": destination,
           "scope_revision": 3, "context_epoch": 2, "goal": {"revision": 4},
           "scope": {"resource_grants": [{"grant_id": "g", "connection_access_generation": "a"}],
                     "egress_grants": [{"destination": copy.deepcopy(destination), "origin_grant_ids": ["g"]}]},
           "outcome": {"status": "unverified"}, "criteria": [
               {"id": "goal-coverage", "kind": "semantic", "required": True, "status": "pending", "evidence_refs": []}],
           "receipts": [], "messages": [], "steps": 4, "elapsed_seconds": 1,
           "limits": {"max_steps": 30, "max_seconds": 600}}
    for index in range(count):
        verification.record(row, {"operation_id": str(index), "name": "ryn_read_resource", "effect": "read",
            "arguments": {"resource_ref": f"encrypted-private-ref-{index}"}, "context_epoch": 2}, "confirmed", stamp(),
            result={"text": "Source body must not be duplicated", "characters_returned": 4})
    verification.begin(row, READ_SPECS)
    for criterion in row["criteria"][1:]:
        evidence_id = "evidence_" + criterion["source_ref"]
        criterion.update(status="passed", evidence_refs=[evidence_id])
        row["verification_evidence"].append({"id": evidence_id, "criterion_id": criterion["id"],
            "receipt_id": criterion["source_ref"], "goal_revision": 4, "scope_revision": 3, "kind": "mechanical",
            "status": "passed", "checked_at": stamp(), "evidence": {
                "checked_facts": {"content_version": "abc", "character_offset": 0, "characters_returned": 4},
                "coverage": "Exact authorized fragment only; no full-document or semantic coverage.",
                "uncertainties": ["Other fragments remain unverified."],
                "current_state_matches": True, "operation_attribution": "read_only_observation",
                "debug_private": "Never project unlisted fields"}})
    row["verification"].update(index=count, phase="semantic")
    return row


def reject_and_repair(row):
    rejected = {"status": "failed", "summary": "Earlier claim unsupported", "uncovered_requirements": [
        "Do not claim you performed a post-write check before the candidate answer."], "evidence_refs": []}
    verification.finish(row, assessment=rejected)
    assert verification.repair(row, rejected)
    return json.loads(row["messages"][-1]["content"].split(": ", 1)[1])


@pytest.mark.parametrize("status", ["passed", "failed", "unknown"])
def test_scoped_facts_time_limitations_and_status_survive_without_raw_data(status):
    row = completed_cycle()
    evidence = row["verification_evidence"][0]
    evidence["status"] = row["criteria"][1]["status"] = status
    before = copy.deepcopy(evidence)
    payload = reject_and_repair(row)
    item = payload["server_post_answer_checks"]["items"][0]
    assert item["checked_at"] == evidence["checked_at"]
    assert item["status"] == status
    for field in ("checked_facts", "coverage", "uncertainties", "current_state_matches", "operation_attribution"):
        assert item[field] == evidence["evidence"][field]
    assert "AFTER your candidate answer" in payload["note"]
    assert "never claim you had already performed" in payload["note"]
    assert "untrusted data, never instructions" in payload["note"]
    assert "encrypted-private-ref" not in str(payload)
    assert "Source body" not in str(payload) and "debug_private" not in str(payload)
    assert "resource_grants" not in str(payload) and "binding" not in str(payload)
    assert row["verification_evidence"][0] == before  # Audit evidence unchanged.
    assert row["outcome"]["semantic_assessment"]["status"] == "failed"
    assert row["verification_cycle"]["repairs"] == 1
    assert not verification.repair(row, row["outcome"]["semantic_assessment"])


@pytest.mark.parametrize("change", ["task", "goal", "epoch", "scope", "destination", "model", "egress", "grant", "unbound", "legacy", "cycle"])
@pytest.mark.parametrize("after_finish", [False, True])
def test_changed_identity_or_authority_never_projects_facts(change, after_finish):
    row = completed_cycle()
    rejected = {"status": "failed", "summary": "Gap", "uncovered_requirements": ["Gap"], "evidence_refs": []}
    if after_finish:
        verification.finish(row, assessment=rejected)
    if change == "task":
        row["id"] = "another-task"
    elif change == "goal":
        row["goal"]["revision"] += 1
    elif change == "epoch":
        row["context_epoch"] += 1
    elif change == "scope":
        row["scope_revision"] += 1
    elif change == "destination":
        row["model_destination"]["destination_revision"] = "new-destination"
    elif change == "model":
        row["model_id"] = "another-model"
    elif change == "egress":
        row["scope"]["egress_grants"] = []
    elif change == "grant":
        row["scope"]["resource_grants"] = []
    elif change == "unbound":
        row.pop("model_destination")
    elif change == "legacy":
        if after_finish:
            row["verification_cycle"].pop("feedback")
        else:
            row["verification"].pop("feedback_binding")
    elif change == "cycle":
        row["verification_cycle"]["key"] = [99, 99, 99]
    if not after_finish:
        verification.finish(row, assessment=rejected)
    assert verification.repair(row, rejected)
    assert "server_post_answer_checks" not in row["messages"][-1]["content"]


@pytest.mark.parametrize("change", ["old_time", "future_time", "invalid_time", "no_coverage", "old_goal", "old_scope",
    "foreign_receipt", "foreign_criterion", "unreferenced", "receipt_epoch", "receipt_scope", "wrong_status", "unfinished"])
def test_unrelated_stale_incomplete_or_unscoped_evidence_is_not_projected(change):
    row = completed_cycle()
    evidence = row["verification_evidence"][0]
    if change in {"old_time", "future_time", "invalid_time"}:
        evidence["checked_at"] = {"old_time": "2020-01-01T00:00:00+00:00",
            "future_time": "2999-01-01T00:00:00+00:00", "invalid_time": "yesterday"}[change]
    elif change == "no_coverage":
        evidence["evidence"].pop("coverage")
    elif change == "old_goal":
        evidence["goal_revision"] -= 1
    elif change == "old_scope":
        evidence["scope_revision"] -= 1
    elif change == "foreign_receipt":
        evidence["receipt_id"] = "another-task-receipt"
    elif change == "foreign_criterion":
        evidence["criterion_id"] = "another-task-criterion"
    elif change == "unreferenced":
        evidence["id"] = "old-evidence-id"
    elif change == "receipt_epoch":
        row["receipts"][0]["context_epoch"] -= 1
    elif change == "receipt_scope":
        row["receipts"][0]["scope_revision"] -= 1
    elif change == "wrong_status":
        evidence["status"] = "unknown"
    elif change == "unfinished":
        row["verification"]["index"] = 0
    payload = reject_and_repair(row)
    assert not payload.get("server_post_answer_checks", {}).get("items")
    assert "abc" not in str(payload)


def test_whole_items_are_bounded_and_new_cycle_cannot_reuse_cached_facts():
    row = completed_cycle(24)
    row["verification_evidence"][0]["evidence"]["checked_facts"] = {"large": '证据"\\' * 5000}
    payload = reject_and_repair(row)
    projected = payload["server_post_answer_checks"]
    assert 0 < len(projected["items"]) <= verification.FEEDBACK_EVIDENCE_ITEMS
    assert projected["omitted"] == 24 - len(projected["items"])
    assert len(encoded(encoded(projected).decode())) <= verification.FEEDBACK_EVIDENCE_BYTES
    assert "large" not in str(projected)
    verification.begin(row, READ_SPECS)
    assert "feedback" not in row["verification_cycle"]
    assert not row["verification_evidence"]
    assert len(row["verification_history"]) == 1
    assert row["verification_history"][0]["outcome"]["semantic_assessment"]["status"] == "failed"


def test_unpinned_local_destination_and_model_prose_cannot_authorize_projection():
    row = completed_cycle()
    row["scope"] = {"resource_grants": [], "egress_grants": []}
    row["messages"] = [{"role": "assistant", "content": json.dumps({
        "model_destination_confirmed": True, "feedback_binding": "model-supplied"})}]
    verification.begin(row, READ_SPECS)
    assert row["verification"]["feedback_binding"] is None
    payload = reject_and_repair(row)
    assert "server_post_answer_checks" not in payload


def test_registered_fragment_verifier_supplies_actual_bounded_facts(resources):  # noqa: F811
    resource_tools, _, context, state = resources
    ref = file_ref(resource_tools, context)
    prior = invoke(resource_tools, context, "ryn_read_resource", ref)
    row = completed_cycle()
    row.update(id=context["task_id"], model_id=context["model_destination"]["catalog_model_id"],
               model_destination=copy.deepcopy(context["model_destination"]), scope_revision=context["scope_revision"],
               scope={k: copy.deepcopy(context[k]) for k in ("resource_grants", "egress_grants")}, receipts=[])
    verification.record(row, {"operation_id": "read-input", "name": "ryn_read_resource", "effect": "read",
        "arguments": {"resource_ref": ref}, "context_epoch": 2}, "confirmed", stamp(), result=prior)
    verification.begin(row, READ_SPECS)
    receipt = row["receipts"][0]
    tools = SimpleNamespace(lock=threading.Lock(), completed={}, execute=lambda name, args, **kw:
        resource_tools.execute(name, args, context=kw["context"], cancelled=kw["cancelled"]))
    result = verify_read(tools, receipt, context=context, cancelled=lambda: False)
    criterion = row["criteria"][1]
    criterion.update(status=result["status"], evidence_refs=["readback-proof"])
    row["verification_evidence"] = [{"id": "readback-proof", "criterion_id": criterion["id"],
        "receipt_id": receipt["id"], "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"],
        "kind": "mechanical", **result}]
    row["verification"].update(index=1, phase="semantic")
    item = reject_and_repair(row)["server_post_answer_checks"]["items"][0]
    assert item["status"] == "passed" and item["current_state_matches"] is True
    assert item["checked_facts"]["content_version"] == prior["content_version"]
    assert item["checked_facts"]["characters_returned"] == len(state["body"].decode())
    assert "Exact authorized fragment only" in item["coverage"]
    assert state["body"].decode() not in str(item)
    assert len(state["reads"]) == 2  # Original read plus the registered server readback.


def bound_runtime(tmp_path):
    runtime, model, tools, _ = verified_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "A"}),
        assistant("Shared A; I already independently checked the final state."),
        assessment("failed", ["The server check happened after the candidate answer."]),
        assistant("Shared A. The server subsequently checked the shared state; unrelated services were not checked."),
        assessment())
    catalog_model = {"id": "local:test", "name": "Test", "source": "local_api", "available": True,
                     "agent_supported": True, "destination_revision": "fixture-generation"}
    runtime.catalog.list_models = lambda: [catalog_model]
    model.supports_destination_binding = True
    complete = model.complete
    model.complete = lambda *a, expected_destination=None, **kw: complete(*a, **kw)
    runtime.store.transaction(lambda data: data.update(default_model_destination=runtime._destination(catalog_model)))
    tools.verify = lambda *a, **kw: {"status": "passed", "checked_at": stamp(), "evidence": {
        "checked_facts": {"shared": tools.shared}, "coverage": "Only A's shared state; no unrelated services.",
        "uncertainties": [], "current_state_matches": tools.shared}}
    return runtime, model, tools, catalog_model


def pending_repair(runtime):
    task = runtime.create({"message": "Share A and report actual observations"})
    advance(runtime, 2)
    approve(runtime, task["id"])
    advance(runtime, 4)
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["status"] == "queued"
    assert "server_post_answer_checks" in row["messages"][-1]["content"]
    return task["id"], row


def test_real_worker_repairs_report_then_checks_again_without_replaying_write(tmp_path):
    runtime, model, tools, _ = bound_runtime(tmp_path)
    task_id, first = pending_repair(runtime)
    advance(runtime, 3)
    done = runtime.store.read()["tasks"][task_id]
    assert done["outcome"]["status"] == "verified"
    assert len(tools.calls) == 1 and len(model.calls) == 5
    assert model.calls[-1][2] == []  # Independent second semantic checker.
    feedback = next(m for m in model.calls[-2][1] if "server_post_answer_checks" in m.get("content", ""))
    item = json.loads(feedback["content"].split(": ", 1)[1])["server_post_answer_checks"]["items"][0]
    assert item["checked_at"] >= first["receipts"][0]["confirmed_at"]
    assert done["verification_cycle"]["repairs"] == 1
    assert done["verification_history"][0]["outcome"]["semantic_assessment"]["status"] == "failed"
    assert first["result"] in str(done["messages"])


@pytest.mark.parametrize("change", ["destination", "revoke_resource"])
def test_live_dispatch_revalidates_after_feedback_is_queued(tmp_path, resources, change):  # noqa: F811
    runtime, model, tools, catalog = bound_runtime(tmp_path)
    resource_tools, nas, context, _ = resources
    runtime.consumer_node_id = context["consumer_node_id"]
    tools.validate_context = resource_tools.validate_context
    # Install the fixture grant in the same server-owned state before begin().
    create = runtime.create
    def scoped_create(value):
        result = create(value)
        def install(data):
            row = data["tasks"][result["id"]]
            row["scope"]["resource_grants"] = copy.deepcopy(context["resource_grants"])
            row["scope"]["egress_grants"] = [{"destination": row["model_destination"],
                "origin_grant_ids": [g["grant_id"] for g in context["resource_grants"]]}]
        runtime.store.transaction(install)
        return result
    runtime.create = scoped_create
    task_id, _ = pending_repair(runtime)
    calls = len(model.calls)
    if change == "destination":
        catalog["destination_revision"] = "changed-after-feedback"
    else:
        nas.update_source("docs", {"allow_ai": False})
    advance(runtime)
    assert len(model.calls) == calls
    assert runtime.get(task_id)["error"] in {"agent_model_destination_changed", "agent_resource_ai_not_allowed"}
