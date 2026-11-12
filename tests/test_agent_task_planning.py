"""Shared planning lifecycle: actual encrypted runtime and workstation tools.

Model decisions here are scripted; real-model replay is separate acceptance.
"""
import copy
import json

import pytest
from jsonschema import validate
from test_agent_codex_loop import workstation
from test_agent_runtime import advance, assistant, make_runtime
from test_codex_object_protocol import assert_strict_subset

from rynmesh.agent_runtime import planning, verification
from rynmesh.agent_runtime.prompt import PLAN, authority
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.llm_package.codex_tools import _response_schema, _validated


def step(sid="read", **extra):
    return {"id": sid, "text": "Read source inputs" if sid == "read" else "Deliver and check the result",
            "criterion_ids": ["goal-coverage"], "depends_on": [], "status": "pending",
            "expected_result": "Source values with their dates and coverage" if sid == "read" else "A saved, readable result file",
            "verification": "Compare with the actual tool output and read back the file", **extra}


def proposal(steps=None, revision=0, **extra):
    return {"goal_revision": 1, "expected_plan_revision": revision, "track_execution": True,
            "deliverables": ["A useful answer or file with source coverage"],
            "constraints": ["Missing values must not be guessed"], "steps": steps if steps is not None else [step()], **extra}


def update(row, args):
    validate(args, PLAN)
    return planning.update(row, args)


@pytest.fixture
def row(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Read inputs and deliver a checked result"})
    return runtime.store.read()["tasks"][task["id"]]


def receipt(row, rid="receipt_read", **extra):
    return {"id": rid, "tool": "ryn_file_read", "status": "confirmed", "result": {"text": "source"},
            "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"],
            "context_epoch": row.get("context_epoch", 0), **extra}


def until(runtime, tid, statuses=("completed", "failed", "waiting_input", "interrupted"), limit=24):
    for _ in range(limit):
        current = runtime.get(tid)
        if current["status"] in statuses:
            return current
        runtime.run_once()
    pytest.fail(f"Task did not settle: {runtime.get(tid)['status']}")


def test_schema_keeps_strict_object_protocol_and_restores_nested_optional_fields():
    tools = {"ryn_update_plan": {"name": "ryn_update_plan", "parameters": PLAN}}
    wire = _response_schema(tools, strict=True)
    assert_strict_subset(wire)
    args = proposal()
    optional = set(PLAN["properties"]) - set(PLAN["required"])
    sent = {**dict.fromkeys(optional), **args}
    nested_optional = set(PLAN["properties"]["steps"]["items"]["properties"]) - set(PLAN["properties"]["steps"]["items"]["required"])
    sent["steps"] = [{**dict.fromkeys(nested_optional), **item} for item in args["steps"]]
    value = {"content": "", "tool_calls": [{"name": "ryn_update_plan", "arguments": sent}]}
    result = _validated({"text": json.dumps(value)}, tools, False, restore_omitted=True)
    actual = json.loads(result["tool_calls"][0]["function"]["arguments"])
    assert actual == args


@pytest.mark.parametrize("steps", [
    [step(depends_on=["read"])], [step(), step()],
    [step(depends_on=["later"]), step("later", depends_on=["read"])],
    [step(criterion_ids=["invented-goal"])],
    [step(), step("deliver", depends_on=["read"], status="running")],
    [step(), step("deliver", depends_on=["read"], status="done", result_summary="Claimed")],
])
def test_invalid_graph_or_unmet_dependency_cannot_advance(row, steps):
    before = copy.deepcopy(row)
    with pytest.raises(AgentToolRejected):
        update(row, proposal(steps))
    assert row == before


def test_reopening_prerequisite_requires_reconciling_dependent_progress(row):
    update(row, proposal([step(status="done", result_summary="Identified the source"), step("deliver", depends_on=["read"])]))
    before = copy.deepcopy(row)
    with pytest.raises(AgentToolRejected):
        update(row, proposal([step(), step("deliver", depends_on=["read"], status="running")], revision=1))
    assert row == before


def test_omitted_progress_is_retained_and_structural_replan_is_recorded(row):
    first = step(status="done", result_summary="Source interpreted; no external fact claimed")
    update(row, proposal([first, step("deliver", depends_on=["read"])]))
    carried = {key: value for key, value in first.items() if key not in {"status", "result_summary"}}
    update(row, proposal([carried, step("deliver", depends_on=["read"], status="running")], revision=1))
    assert row["plan"]["steps"][0]["status"] == "done"
    assert planning.projection(row)["unfinished_step_ids"] == ["deliver"]
    before = copy.deepcopy(row)
    with pytest.raises(AgentToolRejected, match="agent_plan_revision_conflict"):
        update(row, proposal(revision=0))
    assert row == before
    changed = proposal([carried, step("deliver-alt", depends_on=["read"], text="Use another available file format")], revision=2)
    with pytest.raises(AgentToolRejected):
        update(row, changed)
    changed["replan_reason"] = "The requested format is unavailable; preserve the source and explain the alternative"
    update(row, changed)
    assert row["plan"]["replan_reason"] == changed["replan_reason"]
    assert row["plan_history"][-1]["revision"] == 2


def test_short_progress_updates_preserve_contracts_and_validate_dependencies(row):
    update(row, proposal([step(), step("deliver", depends_on=["read"])]))
    first_contract = copy.deepcopy(row["plan"]["steps"][0])
    args = {"goal_revision": 1, "expected_plan_revision": 1, "step_updates": [
        {"id": "read", "status": "done", "result_summary": "Interpreted known inputs"},
        {"id": "deliver", "status": "running"}]}
    update(row, args)
    assert row["plan"]["steps"][0]["expected_result"] == first_contract["expected_result"]
    assert [s["status"] for s in row["plan"]["steps"]] == ["done", "running"]
    for updates in [[{"id": "unknown", "status": "done"}], [{"id": "read", "status": "pending"}],
                    [{"id": "read", "status": "done"}, {"id": "read", "status": "done"}]]:
        with pytest.raises(AgentToolRejected):
            update(row, {"goal_revision": 1, "expected_plan_revision": 2, "step_updates": updates})


@pytest.mark.parametrize("extra", [
    {"status": "failed"}, {"status": "unknown"}, {"status": "not_executed"},
    {"result": {"error": "failed"}}, {"result": {"is_error": True}},
    {"result": {"structuredContent": {"success": False}}},
    {"goal_revision": 99}, {"scope_revision": 99}, {"context_epoch": 99},
])
def test_failed_or_stale_receipts_cannot_support_finished_work(row, extra):
    row["receipts"] = [receipt(row, **extra)]
    with pytest.raises(AgentToolRejected):
        update(row, proposal([step(status="done", evidence_refs=["receipt_read"])]))
    assert row["plan"]["revision"] == 0
    if any(key in extra for key in ("goal_revision", "scope_revision", "context_epoch")):
        assert not planning._current_receipts(row)


def test_receipts_and_finished_steps_do_not_grant_permissions_or_verify_goal(row):
    row["receipts"] = [receipt(row)]
    before = copy.deepcopy({key: row[key] for key in ("goal", "criteria", "permission_mode", "scope", "outcome")})
    update(row, proposal([step(status="done", evidence_refs=["receipt_read"])]))
    assert planning.unfinished(row) == []
    assert row["plan"]["steps"][0]["evidence_status"] == "linked_receipts"
    assert {key: row[key] for key in before} == before
    assert "not business correctness" in planning.projection(row)["verification"]


def test_unknown_receipts_and_unexplained_blockers_are_rejected(row):
    for item in [step(status="done", evidence_refs=["forged"]), step(status="blocked"), step(status="done")]:
        with pytest.raises(AgentToolRejected):
            update(row, proposal([item]))
    update(row, proposal([step(status="blocked", blocker="The owner has not supplied the loan rate")]))
    assert planning.completion_reminder(row) is None


def test_tracked_plan_cannot_be_silently_disabled_or_cleared_and_history_is_bounded(row):
    update(row, proposal())
    for args in [proposal([], revision=1), proposal(revision=1, track_execution=False)]:
        with pytest.raises(AgentToolRejected):
            update(row, args)
    for rev in range(1, 10):
        update(row, proposal(revision=rev))
    assert len(row["plan_history"]) == 3
    assert len(planning.projection(row)["recent_receipts"]) <= 8


def test_incomplete_turn_gets_one_bounded_reconciliation_not_an_infinite_loop(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant(name="ryn_update_plan", args=proposal()), assistant("Premature done"), assistant("Still no result"))
    task = runtime.create({"message": "Produce the requested result"})
    done = until(runtime, task["id"])
    assert done["status"] == "completed" and done["outcome"]["status"] == "partial"
    assert done["outcome"]["unfinished_plan_steps"] == ["read"]
    assert len(model.calls) == 3
    assert sum(e["type"] == "turn_decision" and e["reason"] == "plan_reconciliation" for e in done["events"]) == 1
    assert "unfinished steps: read" in json.dumps(model.calls[-1][1])


def test_plan_reconciliation_cannot_exceed_owner_model_call_budget(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant(name="ryn_update_plan", args=proposal()), assistant("No result yet"))
    task = runtime.create({"message": "Produce a result", "budget_limits": {"max_total_model_calls": 2}})
    done = until(runtime, task["id"])
    assert len(model.calls) == 2
    assert done["status"] == "failed" and done["error"] == "agent_model_call_budget_exhausted"


def test_missing_business_inputs_wait_without_dispatching_a_dependent_write(tmp_path):
    blocked = proposal([step(status="blocked", blocker="Need actual rates and due dates"), step("deliver", depends_on=["read"])])
    runtime, _, tools, _ = make_runtime(tmp_path,
        assistant(name="ryn_update_plan", args=blocked),
        assistant(name="ryn_request_input", args={"goal_revision": 1, "prompt": "Provide rates and due dates", "kind": "information",
            "fields": [{"id": "terms", "label": "Rates and due dates", "type": "text", "required": True}]}))
    task = runtime.create({"message": "Calculate a repayment schedule without assuming bank terms"})
    done = until(runtime, task["id"])
    assert done["status"] == "waiting_input" and tools.calls == []
    assert done["plan"]["steps"][0]["status"] == "blocked"


def test_personal_work_plan_survives_restart_and_delivers_actual_file(tmp_path):
    initial = proposal([step(status="running"), step("deliver", depends_on=["read"])])
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_update_plan", args=initial), assistant(name="ryn_file_read", args={"path": "source.txt"}))
    (workspace / "source.txt").write_text("Personal project notes", encoding="utf-8")
    task = runtime.create({"message": "Read my notes and save a checked summary"})
    advance(runtime, 4)
    raw = runtime.store.read()["tasks"][task["id"]]
    rid = raw["receipts"][-1]["id"]
    header = next(m["content"] for m in runtime._context(raw) if m["role"] == "developer" and "Server control state:" in m["content"])
    state_line = next(line for line in header.splitlines() if line.startswith("Server control state: "))
    state = json.loads(state_line.removeprefix("Server control state: "))
    assert state["plan_revision"] == state["planning_state"]["plan_revision"] == 1
    assert state["planning_state"]["unfinished_step_ids"] == ["read", "deliver"]
    assert state["planning_state"]["recent_receipts"][0]["id"] == rid
    read = step(status="done", evidence_refs=[rid])
    model.responses.extend([
        assistant(name="ryn_update_plan", args=proposal([read, step("deliver", depends_on=["read"], status="running")], revision=1)),
        assistant(name="ryn_file_write", args={"path": "summary.txt", "content": "Summary of personal project notes"}),
        assistant(name="ryn_file_read", args={"path": "summary.txt"}),
    ])
    restarted = AgentRuntime(AgentStore(runtime.store.root, runtime.store.key), model, runtime.tools, runtime.catalog)
    assert restarted.get(task["id"])["plan"] == runtime.get(task["id"])["plan"]
    advance(restarted, 6)
    raw = restarted.store.read()["tasks"][task["id"]]
    assert raw["receipts"][-1]["tool"] == "ryn_file_read"
    deliver = step("deliver", depends_on=["read"], status="done", evidence_refs=[r["id"] for r in raw["receipts"][-2:]])
    model.responses.extend([assistant(name="ryn_update_plan", args=proposal([read, deliver], revision=2)), assistant("Saved and read back summary.txt")])
    done = until(restarted, task["id"])
    assert done["status"] == "completed" and planning.unfinished(done) == []
    assert done["outcome"]["status"] == "unverified"  # Readback is not independent goal assessment.
    assert (workspace / "summary.txt").read_text() == "Summary of personal project notes"
    assert restarted.artifacts(task["id"])["files"][0]["status"] == "current"
    assert rid in authority(raw) and "recent_receipts" in authority(raw)
    runtime.tools.local.processes.close()


def test_verifier_cannot_promote_unfinished_plan_to_verified(row):
    update(row, proposal())
    verification.finish(row, assessment={"status": "passed", "evidence_refs": [], "summary": "Claimed", "uncovered_requirements": []})
    assert row["outcome"]["status"] == "partial"
    assert row["outcome"]["unfinished_plan_steps"] == ["read"]


def test_failed_read_can_be_recorded_blocked_then_replanned_to_available_input(tmp_path):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_update_plan", args=proposal([step(status="running")])),
        assistant(name="ryn_file_read", args={"path": "missing.txt"}))
    (workspace / "available.txt").write_text("Actual available input", encoding="utf-8")
    task = runtime.create({"message": "Use the available input; report any missing sources"})
    advance(runtime, 4)
    raw = runtime.store.read()["tasks"][task["id"]]
    failed = raw["receipts"][-1]
    assert planning._failed(failed)
    changed = step(text="Read available.txt and disclose the missing source", status="running")
    model.responses.extend([
        assistant(name="ryn_update_plan", args=proposal([step(status="blocked", blocker="missing.txt does not exist", evidence_refs=[failed["id"]])], revision=1)),
        assistant(name="ryn_update_plan", args=proposal([changed], revision=2, replan_reason="Use the authorized alternative and disclose that the original is missing")),
        assistant(name="ryn_file_read", args={"path": "available.txt"}),
    ])
    advance(runtime, 6)
    raw = runtime.store.read()["tasks"][task["id"]]
    passed = raw["receipts"][-1]
    assert passed["tool"] == "ryn_file_read" and passed["result"]["text"] == "Actual available input"
    model.responses.extend([
        assistant(name="ryn_update_plan", args=proposal([{**changed, "status": "done", "evidence_refs": [passed["id"]]}], revision=3)),
        assistant("Read the alternative input; missing.txt was not available."),
    ])
    done = until(runtime, task["id"])
    assert done["status"] == "completed" and not planning.unfinished(done)
    raw = runtime.store.read()["tasks"][task["id"]]
    assert any(plan.get("replan_reason") for plan in raw["plan_history"])
    assert len(raw["receipts"]) == 2  # No retry loop or repeated write.
    runtime.tools.local.processes.close()
