"""Bounded execution plans inside the existing Agent, not another executor.

The model owns decomposition and business interpretation. The host owns graph,
revision and receipt validation. Plan progress never verifies the owner's goal.
"""
from __future__ import annotations

import copy

from .store import AgentToolRejected


def text(limit):
    return {"type": "string", "minLength": 1, "maxLength": limit}


STEP_PROPERTIES = {
    "status": {"enum": ["pending", "running", "blocked", "done"]},
    "expected_result": text(400),
    "verification": text(400),
    "result_summary": text(400),
    "blocker": text(400),
    "evidence_refs": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": text(160)},
}
PLAN_PROPERTIES = {
    "track_execution": {"type": "boolean"},
    "expected_plan_revision": {"type": "integer", "minimum": 0},
    "deliverables": {"type": "array", "maxItems": 8, "items": text(240)},
    "constraints": {"type": "array", "maxItems": 8, "items": text(240)},
    "replan_reason": text(400),
    "step_updates": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"id": text(64), **{k: copy.deepcopy(STEP_PROPERTIES[k]) for k in
                       ("status", "result_summary", "blocker", "evidence_refs")}}, "required": ["id", "status"]}},
}
GUIDANCE = (
    "For nontrivial multi-step work, use ryn_update_plan(track_execution=true, expected_plan_revision): "
    "define deliverables, constraints, dependencies and each step's expected_result/verification. "
    "Then use short step_updates, current receipt IDs and actual results to track or revise the work. "
    "Discover available tools/data; ask only for essential missing facts and never invent business rules. "
    "Check source coverage and real deliverables before finishing. Keep simple questions direct. "
    "For passed table calculations, export with calculation_receipt_id and path instead of repeating the recipe."
)
EXECUTION_GUIDANCE = (
    "For nontrivial multi-step work, use ryn_update_plan with track_execution=true and the current expected_plan_revision. "
    "Translate the user's request into concrete deliverables and constraints, discover the required tools/data, "
    "then give each step an expected_result and verification method with dependencies. "
    "Keep simple questions direct; use the existing model loop without a separate planner service or plan approval. "
    "Resolve discoverable facts yourself; use ryn_request_input only for essential missing business facts. "
    "Clarify ambiguous outcomes before solving a different problem; preserve the user's named scope and time window. "
    "For sequential balances or constrained dates, use ryn_schedule_validate to jointly check liquidity before inflows, "
    "scoped quantities, deadlines, working-day rules and daily capacity. State unconfirmed rules as scenario assumptions, "
    "not approved policy. A fixed-count calendar bound does not prove all alternative schedules infeasible. "
    "Never invent amounts, rates, dates, rules or source coverage to fill gaps. "
    "Create steps once, then use short step_updates with expected_plan_revision instead of repeating the full plan. "
    "Batch updates with related tool calls, linking current receipt IDs from planning_state when available. "
    "On errors inspect the result, repair a recoverable cause or revise the plan with replan_reason; "
    "never repeat an uncertain write or run a dependent step before its prerequisites are met. "
    "Before finishing, check source coverage and actual deliverables, reconcile the plan, and report remaining blockers. "
    "Use deterministic calculation and readback tools for tables. Export a passed calculation with "
    "ryn_table_export(calculation_receipt_id, path), reusing its exact host-stored recipe rather than recalculating unchanged inputs. "
    "A model plan or fluent answer is not verification."
)


def _reject(row, message):
    raise AgentToolRejected("agent_invalid_control_proposal", correction=message,
                            details={"expected_plan_revision": row["plan"]["revision"]})


def _current_receipts(row):
    binding = (row["goal"]["revision"], row["scope_revision"], row.get("context_epoch", 0))
    return {r["id"]: r for r in row.get("receipts", []) if
            tuple(r.get(key) for key in ("goal_revision", "scope_revision", "context_epoch")) == binding}


def _failed(receipt):
    result = receipt.get("result") or {}
    envelopes = [result] if isinstance(result, dict) else []
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        envelopes.append(result["structuredContent"])
    return receipt.get("status") != "confirmed" or any(
        item.get("error") or item.get("is_error") is True or item.get("isError") is True
        or item.get("success") is False for item in envelopes)


def _structure(plan):
    return {"steps": [{key: step.get(key) for key in
                       ("id", "depends_on", "criterion_ids")}
                      for step in plan.get("steps", [])],
            **{key: plan.get(key, []) for key in ("deliverables", "constraints")}}


def update(row, args):
    """Apply a schema-validated proposal atomically in the task transaction."""
    prior = row["plan"]
    if "expected_plan_revision" in args and args["expected_plan_revision"] != prior["revision"]:
        raise AgentToolRejected("agent_plan_revision_conflict", correction="Use the current plan revision and reconcile progress before retrying.",
                                details={"expected_plan_revision": prior["revision"]})
    tracked = args.get("track_execution", prior.get("track_execution", False))
    if prior.get("track_execution") and not tracked:
        _reject(row, "Keep execution tracking; revise obsolete steps with a replan_reason instead of disabling progress tracking.")
    if "step_updates" in args:
        updates = args["step_updates"]
        known = {s["id"] for s in prior["steps"]}
        if ("steps" in args or not prior.get("track_execution") or len({s["id"] for s in updates}) != len(updates)
                or not {s["id"] for s in updates} <= known):
            _reject(row, "Use step_updates only for unique existing IDs in a tracked plan; use steps plus a reason to change its graph.")
        indexed = {s["id"]: s for s in updates}
        args = {**args, "steps": [{**s, **indexed.get(s["id"], {})} for s in prior["steps"]]}
    if "steps" not in args:
        _reject(row, "Supply initial steps or short step_updates for an existing tracked plan.")
    if tracked and (not args["steps"] or "expected_plan_revision" not in args):
        _reject(row, "Tracked plans need at least one step and the current expected_plan_revision.")
    receipts = _current_receipts(row)
    previous = {step["id"]: step for step in prior["steps"]}
    criteria_ids = {item["id"] for item in row["criteria"]}
    seen, completed, steps = set(), set(), []
    for proposal in args["steps"]:
        if (proposal["id"] in seen or not set(proposal["depends_on"]) <= seen
                or not set(proposal["criterion_ids"]) <= criteria_ids):
            _reject(row, "Use unique step IDs, existing criterion IDs and topologically ordered dependencies without cycles.")
        seen.add(proposal["id"])
        step = copy.deepcopy(proposal)
        if not tracked and set(step) & STEP_PROPERTIES.keys():
            _reject(row, "Set track_execution=true to record step progress, output contracts or evidence.")
        old = previous.get(step["id"], {})
        unchanged = all(step.get(key) == old.get(key) for key in ("text", "depends_on", "criterion_ids"))
        if tracked and unchanged:
            for key in STEP_PROPERTIES:
                if key not in step and key in old:
                    step[key] = copy.deepcopy(old[key])
        step.setdefault("status", "pending")
        if tracked:
            if not step.get("expected_result") or not step.get("verification"):
                _reject(row, "Every tracked step needs a concrete expected_result and verification method.")
            if step["status"] in {"running", "done"} and not set(step["depends_on"]) <= completed:
                _reject(row, "Complete prerequisite steps before marking dependent steps running or done.")
            refs = step.get("evidence_refs", [])
            if not set(refs) <= receipts.keys():
                _reject(row, "Use only receipt IDs from this task's current goal, resource scope and context epoch.")
            if step["status"] == "blocked" and not step.get("blocker"):
                _reject(row, "A blocked step needs a specific blocker; request essential missing facts with ryn_request_input.")
            if step["status"] == "done":
                if not refs and not step.get("result_summary"):
                    _reject(row, "A finished step needs supporting receipts or a result_summary for model-only work.")
                if any(_failed(receipts[ref]) for ref in refs):
                    _reject(row, "Failed, unknown or business-error tool results cannot support a finished step; repair or mark it blocked.")
                completed.add(step["id"])
            if step["status"] != "blocked":
                step.pop("blocker", None)
            step["evidence_status"] = "linked_receipts" if refs else "model_only"
        steps.append(step)
    candidate = {"revision": prior["revision"] + 1, "goal_revision": row["goal"]["revision"], "steps": steps}
    if tracked:
        candidate.update(track_execution=True, **{key: copy.deepcopy(args.get(key, prior.get(key, [])))
                                                for key in ("deliverables", "constraints")})
        if prior.get("track_execution") and _structure(candidate) != _structure(prior) and not args.get("replan_reason"):
            _reject(row, "Changing the plan's step graph, deliverables or constraints requires a replan_reason. Progress and explanatory wording can be updated directly.")
        if args.get("replan_reason"):
            candidate["replan_reason"] = args["replan_reason"]
        history = row.setdefault("plan_history", [])
        if prior["steps"]:
            history.append(copy.deepcopy(prior))
            del history[:-3]
    row["plan"] = candidate
    return {"accepted": True, "plan_revision": candidate["revision"],
            "criteria_proposals_accepted": False, **({"planning_state": projection(row)} if tracked else {}),
            **({"execution_guidance": EXECUTION_GUIDANCE} if tracked and not prior.get("track_execution") else {})}


def unfinished(row):
    plan = row.get("plan") or {}
    if not plan.get("track_execution") or plan.get("goal_revision") != row["goal"]["revision"]:
        return []
    return [step["id"] for step in plan["steps"] if step["status"] != "done"]


def projection(row):
    plan = row.get("plan") or {}
    if not plan.get("track_execution"):
        return {}
    done = {step["id"] for step in plan["steps"] if step["status"] == "done"}
    recent = list(_current_receipts(row).values())[-8:]
    return {"plan_revision": plan["revision"], "unfinished_step_ids": unfinished(row),
            "ready_step_ids": [s["id"] for s in plan["steps"] if s["status"] == "pending" and set(s["depends_on"]) <= done],
            "blocked_step_ids": [s["id"] for s in plan["steps"] if s["status"] == "blocked"],
            "recent_receipts": [{"id": r["id"], "tool": r["tool"], "status": r["status"],
                                 "usable_for_done": not _failed(r)} for r in recent],
            "verification": "Model-reported progress. Linked receipts establish observations, not business correctness or goal completion."}


def completion_reminder(row):
    """One extra reconciliation turn per goal, inside existing budgets."""
    pending = unfinished(row)
    state = projection(row)
    actionable = state.get("ready_step_ids") or any(s["status"] == "running" for s in row["plan"]["steps"])
    if not pending or not actionable or row.get("plan_reconciliation_goal") == row["goal"]["revision"]:
        return None
    row["plan_reconciliation_goal"] = row["goal"]["revision"]
    return ("Runtime note: your tracked plan still has unfinished steps: " + ", ".join(pending)
            + ". Continue the requested work or update the plan from actual results. If essential information is missing, "
              "request it; if blocked, state the concrete blocker and remaining work. Do not claim full delivery from a plan alone.")


def note_incomplete(row):
    pending = unfinished(row)
    if not pending:
        return
    outcome = row["outcome"]
    if outcome["status"] == "verified":
        outcome["summary"] = "Registered checks passed, but tracked work remains unfinished."
    if outcome["status"] in {"unverified", "verified"}:
        outcome["status"] = "partial"
    outcome["unfinished_plan_steps"] = pending
    outcome.setdefault("uncertainties", []).append("Tracked work remains unfinished: " + ", ".join(pending))
