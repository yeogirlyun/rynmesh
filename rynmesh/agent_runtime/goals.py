"""Model tools over the persistent thread_goal, separate from owner constraints."""
from __future__ import annotations

import copy
import uuid

from . import budget
from .protocol import event, now
from .store import AgentToolRejected

TOOL_NAMES = frozenset({"ryn_get_goal", "ryn_create_goal", "ryn_update_goal"})
STATUSES = frozenset({"active", "paused", "blocked", "budget_limited", "usage_limited", "complete"})
CORRECTIONS = {
    "agent_goal_subagent_forbidden": "Subagents cannot manage persistent goals; report to the parent.",
    "agent_goal_not_found": "No goal exists. Only create one on an explicit owner or system request.",
    "agent_goal_exists": "An unfinished goal exists. Continue it; do not replace it through create.",
    "agent_goal_creation_not_allowed": "The owner has not enabled model goal creation for this task.",
    "agent_goal_invalid_objective": "Use a nonempty objective of at most 4000 characters after trimming.",
    "agent_goal_invalid_budget": "Use a positive integer token budget within the allowed maximum, only if explicitly supplied by the owner.",
    "agent_goal_id_conflict": "Read the current goal and use its expected_goal_id.",
    "agent_goal_invalid_status": "Model updates accept only complete, blocked or paused.",
    "agent_goal_blocked_too_early": "Blocked requires the same blocker for at least 3 consecutive goal turns; keep making progress or ask for missing information.",
    "agent_goal_pause_not_requested": "Paused requires the latest owner message to explicitly request pausing the goal.",
}


def reject(code):
    raise AgentToolRejected(code, correction=CORRECTIONS[code])


def check_subagent(row):
    if row.get("parent_task_id"):
        reject("agent_goal_subagent_forbidden")


def available(row):
    return bool(row and not row.get("parent_task_id")
                and (row.get("thread_goal") or row.get("allow_model_goal_creation")))


def _goal(row):
    check_subagent(row)
    if not row.get("thread_goal"):
        reject("agent_goal_not_found")
    return row["thread_goal"]


def projection(row, *, usage=None):
    goal = row.get("thread_goal")
    if not goal:
        return None
    lifetime = usage if usage is not None else budget.public(row)["lifetime"]
    result = copy.deepcopy(goal)
    result["tokens_used"] = max(0, lifetime["charged_tokens"] - goal["tokens_at_start"])
    result["seconds_used"] = max(0, lifetime["charged_seconds"] - goal["seconds_at_start"])
    cap = goal["token_budget"]
    result["remainingTokens"] = max(0, cap - result["tokens_used"]) if cap is not None else None
    return result


def _result(row, *, usage=None):
    goal = projection(row, usage=usage)
    result = {"goal": goal, "remainingTokens": goal["remainingTokens"]}
    if goal["status"] == "complete" and goal["token_budget"] is not None:
        result["report_usage"] = "Report the final token usage to the owner."
    return result


def tool_get(row, args):
    _goal(row)
    return _result(row)


def tool_create(row, args):
    check_subagent(row)
    existing = row.get("thread_goal")
    if existing and existing["status"] != "complete":
        reject("agent_goal_exists")
    if not row.get("allow_model_goal_creation"):
        reject("agent_goal_creation_not_allowed")
    objective = args.get("objective")
    if not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 4000:
        reject("agent_goal_invalid_objective")
    accounting = budget.public(row)
    cap = args.get("token_budget", accounting["limits"]["max_total_tokens"])
    if type(cap) is not int or not 1 <= cap <= budget.RANGES["max_total_tokens"][1]:
        reject("agent_goal_invalid_budget")
    lifetime = accounting["lifetime"]
    stamp = now()
    row["thread_goal"] = {
        "id": "goal_" + uuid.uuid4().hex, "objective": objective.strip(), "status": "active", "origin": "model",
        "token_budget": cap, "tokens_at_start": lifetime["charged_tokens"],
        "seconds_at_start": lifetime["charged_seconds"], "created_at": stamp, "updated_at": stamp,
        "continuations": 0, "no_progress_turns": 0, "blocked_turns": 0,
    }
    event(row, "thread_goal_set", goal_id=row["thread_goal"]["id"], origin="model")
    return _result(row, usage=lifetime)


def _owner_requested_pause(row):
    owner = next((m for m in reversed(row.get("messages", []))
                  if m.get("role") == "user" and m.get("message_id") and not m.get("source")), None)
    content = str((owner or {}).get("content") or "").strip().casefold().strip(".!。！")
    return content in {"pause", "pause the goal", "pause this goal", "pause goal", "pause the task",
                       "please pause", "please pause the goal", "please pause this goal", "stop for now",
                       "暂停", "先暂停", "暂停目标", "暂停任务", "请暂停", "请暂停目标", "请暂停任务"}


def tool_update(row, args):
    goal = _goal(row)
    if args.get("expected_goal_id") != goal["id"]:
        reject("agent_goal_id_conflict")
    status = args.get("status")
    if status not in {"complete", "blocked", "paused"}:
        reject("agent_goal_invalid_status")
    if status == "blocked" and goal.get("blocked_turns", 0) < 3:
        reject("agent_goal_blocked_too_early")
    if status == "paused" and not _owner_requested_pause(row):
        reject("agent_goal_pause_not_requested")
    # Model dispatch accounting has settled before the internal tool runs.
    # Capture that ledger before changing the target status in this transaction.
    usage = budget.public(row)["lifetime"]
    goal.update(status=status, updated_at=now())
    event(row, "thread_goal_status_changed", goal_id=goal["id"], status=status)
    return _result(row, usage=usage)
