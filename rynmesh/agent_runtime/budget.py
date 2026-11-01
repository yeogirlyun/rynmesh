"""Durable dispatch accounting, conservative unknown usage and lifetime limits."""
from __future__ import annotations

import copy
import math
import uuid

from .store import AgentError

DEFAULT_LIMITS = {"max_total_model_calls": 128, "max_total_tool_calls": 256,
                  "max_total_tokens": 2000000, "max_total_seconds": 3600}
RANGES = {"max_total_model_calls": (1, 1024), "max_total_tool_calls": (1, 4096),
          "max_total_tokens": (1000, 20000000), "max_total_seconds": (10, 86400)}
DEFAULT_CACHED_INPUT_WEIGHT = 0.1
PEER_CACHED_INPUT_WEIGHT = 1.0

# Where a limit came from. Only "default" limits are host policy that may be
# extended or continued automatically. "owner" limits were set explicitly and
# are hard. Rows saved before sources were recorded are "legacy_unknown" and
# stay hard: a value that happens to equal a default does not prove intent.
LIMIT_SOURCES = frozenset({"default", "owner", "legacy_unknown"})
# Per-run limits in row["limits"]. With a "default" source they only size an
# execution segment: reaching them checkpoints and schedules the next segment.
SEGMENT_LIMITS = ("max_steps", "max_seconds")
EXHAUSTED_CODES = {"max_total_model_calls": "agent_model_call_budget_exhausted",
                   "max_total_tool_calls": "agent_tool_budget_exhausted",
                   "max_total_tokens": "agent_token_budget_exhausted",
                   "max_total_seconds": "agent_time_budget_exhausted"}
LIMIT_CODES = {**{code: key for key, code in EXHAUSTED_CODES.items()},
               "agent_step_limit": "max_steps", "agent_time_limit": "max_seconds"}
NO_PROGRESS_CODE = "agent_no_progress"
# A wrap-up turn spends the last allowance of a hard limit; the run then stops on it.
WRAP_UP_CODES = {"token_budget_low": "agent_token_budget_exhausted",
                 "model_call_budget_low": "agent_model_call_budget_exhausted",
                 "step_budget_low": "agent_step_limit"}


def limit_source(row, key):
    sources = (row.get("budget") or {}).get("limit_sources")
    value = sources.get(key) if isinstance(sources, dict) else None
    return value if value in LIMIT_SOURCES else "legacy_unknown"


def is_hard(row, key):
    return limit_source(row, key) != "default"


def set_owner_limits(row, limits):
    """Apply an explicit owner budget; every submitted key becomes a hard limit."""
    state = initialize(row)
    validate_limits(limits)
    state["limits"].update(limits)
    sources = state.setdefault("limit_sources", {key: "legacy_unknown" for key in DEFAULT_LIMITS})
    for key in limits:
        sources[key] = "owner"


def segment_source(row, key):
    sources = row.get("limits_source")
    value = sources.get(key) if isinstance(sources, dict) else None
    return value if value in LIMIT_SOURCES else "legacy_unknown"


def segment_is_hard(row, key):
    return segment_source(row, key) != "default"


def hard_run_limit(row):
    """Return the code of a reached per-run limit the host must not continue past."""
    limits = row.get("limits") or {}
    if row.get("elapsed_seconds", 0) >= limits.get("max_seconds", float("inf")) and segment_is_hard(row, "max_seconds"):
        return "agent_time_limit"
    if row.get("steps", 0) >= limits.get("max_steps", float("inf")) and segment_is_hard(row, "max_steps"):
        return "agent_step_limit"
    return None


def exhausted_hard_limit(row):
    """First hard cumulative limit that leaves no room for another model request."""
    state = initialize(row)
    totals = _totals(state, state["records"], lifetime=True)
    if totals["charged_model_calls"] >= state["limits"]["max_total_model_calls"] and is_hard(row, "max_total_model_calls"):
        return EXHAUSTED_CODES["max_total_model_calls"]
    if totals["charged_tokens"] >= state["limits"]["max_total_tokens"] and is_hard(row, "max_total_tokens"):
        return EXHAUSTED_CODES["max_total_tokens"]
    if time_remaining(row) <= 0:
        return EXHAUSTED_CODES["max_total_seconds"]
    return None


def infer_cached_input_weight(row: dict | None = None, model: dict | None = None) -> float:
    """Return cached-input billing weight for the route (1.0 for peer routes, 0.1 for direct API routes)."""
    if isinstance(model, dict):
        src = str(model.get("source") or "").strip().lower()
        if src == "peer":
            return PEER_CACHED_INPUT_WEIGHT
        if src:
            return DEFAULT_CACHED_INPUT_WEIGHT
    if isinstance(row, dict):
        model_id = str(row.get("model_id") or "").strip().lower()
        dest = row.get("model_destination") if isinstance(row.get("model_destination"), dict) else {}
        cat_id = str(dest.get("catalog_model_id") or "").strip().lower()
        dest_src = str(dest.get("source") or "").strip().lower()
        if model_id.startswith("local/") or cat_id.startswith("local/") or (dest_src and dest_src != "peer"):
            return DEFAULT_CACHED_INPUT_WEIGHT
        if (
            model_id.startswith("peer/")
            or cat_id.startswith("peer/")
            or dest_src == "peer"
            or bool(dest.get("provider_peer_id"))
        ):
            return PEER_CACHED_INPUT_WEIGHT
    return DEFAULT_CACHED_INPUT_WEIGHT


def record_request_policy(row, policy):
    """Persist bounded dispatch durations, never an absolute process clock."""
    modes = {"adapter_timeout_and_cancel_watch", "consumer_cancel_watch_remote_transport_limits"}
    if not isinstance(policy, dict) or not isinstance(policy.get("enforcement"), str) or policy["enforcement"] not in modes:
        raise AgentError("agent_invalid_request_policy")
    safe = {"enforcement": policy["enforcement"], "measurement": "pre_admission_upper_bound"}
    for key in ("provider_timeout_seconds", "effective_timeout_seconds"):
        value = policy.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400):
            raise AgentError("agent_invalid_request_policy")
        safe[key] = value
    for record in reversed(initialize(row)["records"]):
        if record["kind"] == "model" and record["status"] == "reserved":
            record["request_policy"] = safe
            return
    raise AgentError("agent_request_policy_without_dispatch")


def validate_limits(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
        raise AgentError("agent_invalid_budget")
    for key, limit in value.items():
        low, high = RANGES[key]
        if type(limit) is not int or not low <= limit <= high:
            raise AgentError("agent_invalid_budget")
    return {**DEFAULT_LIMITS, **value}


def initialize(row):
    if "budget" not in row:
        # Old histories do not reveal complete prior usage. Count known model
        # attempts, but never present the legacy aggregate as complete.
        dispatches = [event for event in row.get("events", []) if event.get("type") in {"model_started", "tool_started"}]
        legacy = bool(row.get("steps") or row.get("elapsed_seconds") or dispatches)
        legacy_tools = len({event.get("operation_id") or event.get("call_id") or event.get("seq") for event in dispatches if event["type"] == "tool_started"})
        legacy_models = max(row.get("steps", 0), sum(event["type"] == "model_started" for event in dispatches))
        explicit = {key: row["limits"][key] for key in DEFAULT_LIMITS if key in row["limits"]}
        row["budget"] = {"limits": validate_limits(explicit),
                         "round": 1, "records": [], "legacy_usage_unknown": legacy,
                         "legacy_model_calls": legacy_models if legacy else 0, "legacy_tool_calls": legacy_tools,
                         "legacy_seconds": row.get("elapsed_seconds", 0.0) if legacy else 0.0,
                         # A row with history, or limits stored in the old place, cannot prove default intent.
                         "limit_sources": {key: "legacy_unknown" if legacy or key in explicit else "default"
                                           for key in DEFAULT_LIMITS}}
    else:
        for rec in row["budget"].get("records", []):
            if (
                isinstance(rec, dict)
                and rec.get("kind") == "model"
                and rec.get("status") == "settled"
                and rec.get("input_tokens") is None
                and rec.get("output_tokens") is None
                and int(rec.get("output_reservation") or 0) > 32768
                and int(rec.get("charged_tokens") or 0) > 64
            ):
                rec["charged_tokens"] = max(1, min(int(rec.get("input_reservation") or 64), 64))
                rec["charge_basis"] = "retryable_transport_failure"
    return row["budget"]


def new_round(row):
    initialize(row)["round"] += 1


def _totals(state, records, *, lifetime):
    waits = state.get("retry_wait_by_round", {})
    retry_wait = sum(waits.values()) if lifetime else waits.get(str(state["round"]), 0)
    models = [record for record in records if record["kind"] == "model"]
    unknown = any(record.get("input_tokens") is None or record.get("output_tokens") is None for record in models)
    legacy = state["legacy_usage_unknown"] and (lifetime or state["round"] == 1)
    known_input = sum(record.get("input_tokens") or 0 for record in models)
    known_output = sum(record.get("output_tokens") or 0 for record in models)
    known_cached = sum(record.get("cached_input_tokens") or 0 for record in models)
    has_cached = any(record.get("cached_input_tokens") is not None for record in models)
    model_floor = len(models) + (state["legacy_model_calls"] if lifetime else 0)
    tool_floor = len(records) - len(models) + (state.get("legacy_tool_calls", 0) if lifetime else 0)
    return {"model_calls": None if legacy else model_floor,
            "tool_calls": None if legacy else tool_floor,
            "charged_model_calls": model_floor, "charged_tool_calls": tool_floor,
            "known_input_tokens": known_input, "known_output_tokens": known_output,
            "known_cached_input_tokens": known_cached,
            "cached_input_tokens": known_cached if has_cached and not legacy else None,
            "input_tokens": None if legacy or any(record.get("input_tokens") is None for record in models) else known_input,
            "output_tokens": None if legacy or any(record.get("output_tokens") is None for record in models) else known_output,
            "total_tokens": None if unknown or legacy else known_input + known_output,
            "charged_tokens": sum(record["charged_tokens"] for record in records),
            "charged_seconds": sum(record["charged_seconds"] for record in records) + (state["legacy_seconds"] if lifetime else 0) + retry_wait,
            "active_seconds": None if legacy or any(record.get("elapsed_seconds") is None for record in records) else sum(record["elapsed_seconds"] for record in records),
            "cost": None, "cost_status": "unknown", "usage_complete": not unknown and not legacy,
            "legacy_usage_unknown": legacy,
            "unsettled_operations": sum(record["status"] == "reserved" for record in records)}


def public(row):
    state = initialize(row)
    return {"limits": copy.deepcopy(state["limits"]), "round": state["round"],
            "limit_sources": {key: limit_source(row, key) for key in DEFAULT_LIMITS},
            "default_ceilings": {key: RANGES[key][1] for key in DEFAULT_LIMITS},
            "lifetime": _totals(state, state["records"], lifetime=True),
            "current_round": _totals(state, [record for record in state["records"] if record["round"] == state["round"]], lifetime=False),
            "reservation_policy": "Unknown model usage retains the conservative dispatch reservation; cost is unknown without trusted pricing."}


def _room(row, state, key, used, need):
    """Admit `need` more units; only a default limit grows, never past its ceiling."""
    limits = state["limits"]
    if used + need <= limits[key]:
        return True
    ceiling = RANGES[key][1]
    if is_hard(row, key) or used + need > ceiling:
        return False
    previous = limits[key]
    limits[key] = min(ceiling, max(previous + DEFAULT_LIMITS[key], int(math.ceil(used + need))))
    if "event_cursor" in row:
        from .protocol import event
        event(row, "budget_auto_extended", limits=copy.deepcopy(limits), extended={key: limits[key]},
              previous={key: previous}, reason="default_policy", round=state["round"])
    return True


def ensure_default_room(row, key, need):
    """Grow a default allowance before an admission check that reads the limit directly."""
    state = initialize(row)
    if is_hard(row, key):
        return
    totals = _totals(state, state["records"], lifetime=True)
    used = {"max_total_model_calls": totals["charged_model_calls"], "max_total_tool_calls": totals["charged_tool_calls"],
            "max_total_tokens": totals["charged_tokens"], "max_total_seconds": totals["charged_seconds"]}[key]
    _room(row, state, key, used, min(need, max(0, RANGES[key][1] - used)))


def reserve(row, kind, stage, *, input_bound=0, output_bound=0, seconds_bound=None):
    state = initialize(row)
    if kind not in {"model", "tool"}:
        raise AgentError("agent_invalid_budget")
    total = _totals(state, state["records"], lifetime=True)
    limits = state["limits"]
    if not _room(row, state, "max_total_model_calls", total["charged_model_calls"], int(kind == "model")):
        raise AgentError("agent_model_call_budget_exhausted")
    if not _room(row, state, "max_total_tool_calls", total["charged_tool_calls"], int(kind == "tool")):
        raise AgentError("agent_tool_budget_exhausted")
    tokens = input_bound + output_bound if kind == "model" else 0
    if type(tokens) is not int or tokens < 0 or not _room(row, state, "max_total_tokens", total["charged_tokens"], tokens):
        raise AgentError("agent_token_budget_exhausted")
    remaining = limits["max_total_seconds"] - total["charged_seconds"]
    if remaining <= 0 and seconds_bound != 0:
        if not _room(row, state, "max_total_seconds", total["charged_seconds"], 1):
            raise AgentError("agent_time_budget_exhausted")
        remaining = limits["max_total_seconds"] - total["charged_seconds"]
    seconds = min(remaining, seconds_bound if seconds_bound is not None else row["limits"]["max_seconds"])
    record = {"id": "usage_" + uuid.uuid4().hex, "kind": kind, "stage": stage, "round": state["round"],
              "status": "reserved", "input_tokens": None, "output_tokens": None,
              "charged_tokens": tokens, "charged_seconds": seconds, "elapsed_seconds": None,
              "input_reservation": input_bound, "output_reservation": output_bound}
    state["records"].append(record)
    return copy.deepcopy(record)


def settle(row, reservation_id, *, usage=None, elapsed_seconds=None, cached_input_weight=None, retryable_failure=False):
    state = initialize(row)
    record = next((item for item in state["records"] if item["id"] == reservation_id), None)
    if record is None or record["status"] != "reserved":
        return  # Late or duplicate settlements cannot refund the account twice.
    record["status"] = "settled"
    usage = usage if isinstance(usage, dict) else {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        record[key] = value if type(value) is int and value >= 0 else None
    cached_val = usage.get("cached_tokens", usage.get("cached_input_tokens"))
    if type(cached_val) is int and cached_val >= 0:
        record["cached_input_tokens"] = cached_val
    if record["kind"] == "model":
        weight = (
            float(cached_input_weight)
            if isinstance(cached_input_weight, (int, float))
            and not isinstance(cached_input_weight, bool)
            and math.isfinite(cached_input_weight)
            and 0.0 <= float(cached_input_weight) <= 1.0
            else infer_cached_input_weight(row)
        )
        inp = record["input_tokens"]
        out = record["output_tokens"] if record["output_tokens"] is not None else record["output_reservation"]
        cached = record.get("cached_input_tokens")
        if inp is None and record["output_tokens"] is None and (retryable_failure or int(record.get("output_reservation") or 0) > 32768):
            record["charged_tokens"] = max(1, min(int(record.get("input_reservation") or 64), 64))
            record["charge_basis"] = "retryable_transport_failure"
            record["cached_input_weight"] = weight
        elif inp is not None and type(cached) is int and 0 < cached <= inp and weight < 1.0:
            effective_input = (inp - cached) + math.ceil(cached * weight)
            record["charged_tokens"] = effective_input + out
            record["charge_basis"] = "cached_discounted"
            record["cached_input_weight"] = weight
        else:
            record["charged_tokens"] = (inp if inp is not None else record["input_reservation"]) + out
            record["charge_basis"] = "raw"
            record["cached_input_weight"] = weight
    if isinstance(elapsed_seconds, (int, float)) and not isinstance(elapsed_seconds, bool) and math.isfinite(elapsed_seconds) and elapsed_seconds >= 0:
        record["elapsed_seconds"] = elapsed_seconds
        record["charged_seconds"] = elapsed_seconds


def abandon(row):
    """A crashed process's unobserved usage stays conservatively charged."""
    for record in initialize(row)["records"]:
        if record["status"] == "reserved":
            record["status"] = "unknown_after_restart"


def time_remaining(row):
    state = initialize(row)
    # The current flight's time is checked by its own monotonic callback.
    used = sum(record["charged_seconds"] for record in state["records"] if record["status"] != "reserved") + state["legacy_seconds"]
    limit = state["limits"]["max_total_seconds"]
    if not is_hard(row, "max_total_seconds"):
        # A default allowance grows at admission; in-flight work is bounded by its ceiling.
        limit = max(limit, RANGES["max_total_seconds"][1])
    return max(0.0, limit - used - row.get("retry_wait_seconds", 0))


def notice(row: dict) -> str | None:
    """Tell the model, once per threshold, how much of its step budget is gone."""
    if not segment_is_hard(row, "max_steps"):
        return None  # A default segment continues automatically; do not push an early wrap-up.
    steps, cap = row["steps"], row["limits"]["max_steps"]
    level = 2 if steps >= cap * 0.85 else 1 if steps >= cap * 0.6 else 0
    if level == 0:
        row["budget_notice_level"] = 0
        return None
    if level <= row.get("budget_notice_level", 0):
        return None
    row["budget_notice_level"] = level
    wrote = any(r.get("effect") == "write" and r.get("status") == "confirmed" and r.get("goal_revision") == row["goal"]["revision"]
                for r in row.get("receipts", []))
    advice = ("Finish the remaining fixes, run focused tests and report; reserve steps for review." if wrote else
              "Conclude from the evidence collected and report remaining uncertainties. "
              "If the owner requested changes, focus on the necessary change and its verification; do not broaden exploration.")
    return f"Server budget notice: {steps} of {cap} model steps are used. {advice}"


def progress_summary(row):
    """Host-written progress for a limit stop. It costs no model request."""
    state = initialize(row)
    totals = _totals(state, state["records"], lifetime=True)
    lines = [f"Stopped before completion after {totals['charged_model_calls']} model requests "
             f"and {totals['charged_tool_calls']} tool calls."]
    changed = []
    for receipt in row.get("receipts", []):
        if receipt.get("effect") == "write" and receipt.get("status") == "confirmed":
            arguments = receipt.get("arguments") if isinstance(receipt.get("arguments"), dict) else {}
            label = arguments.get("path") or receipt.get("tool")
            if isinstance(label, str) and label not in changed:
                changed.append(label)
    if changed:
        lines.append("Confirmed changes: " + ", ".join(changed[:20]))
    steps = [step.get("description") or step.get("title") or step.get("id")
             for step in (row.get("plan") or {}).get("steps", []) if isinstance(step, dict)]
    if steps:
        lines.append("Latest model plan (not verified): " + "; ".join(str(item) for item in steps[:10]))
    lines.append("The task is not complete. Remaining work continues from the saved history once the limit allows it.")
    return "\n".join(lines)


def stop_for_limit(row, code, *, summary=None, wrap_up_reason=None, **fields):
    """End a run at a hard limit. Never reported as completed.

    The status stays "failed" (the existing contract for budget stops that
    clients and acceptance runners key on); outcome.status "budget_limited",
    outcome.budget_stop and the budget_limit_reached event tell it apart from
    an unrecoverable error. The owner continues with a new turn once the limit
    allows it.
    """
    from .control import close_queue
    from .protocol import event
    key = LIMIT_CODES[code]
    source = limit_source(row, key) if key in DEFAULT_LIMITS else segment_source(row, key)
    if not row.get("in_flight"):
        close_queue(row, event, code, keep_flight=False)
    row.update(status="failed", error=code, approved=None, pending_approval=None)
    outcome = row.setdefault("outcome", {})
    outcome.update(status="budget_limited", budget_limited=True, assessment_requested=False,
                   budget_stop={"code": code, "limit": key, "source": source},
                   summary=summary if isinstance(summary, str) and summary.strip() else progress_summary(row),
                   uncertainties=[f"Stopped at {key} ({source}) before the task was complete."])
    if wrap_up_reason:
        outcome["budget_wrap_up_reason"] = wrap_up_reason
    else:
        outcome.pop("budget_wrap_up_reason", None)
    event(row, "budget_limit_reached", error=code, limit=key, source=source, **fields)


def segment_boundary(row):
    """Per-run limit reached between steps; queued calls drain before a step boundary."""
    limits = row.get("limits") or {}
    if row.get("elapsed_seconds", 0) >= limits.get("max_seconds", float("inf")):
        return "max_seconds"
    if not row.get("queue") and row.get("steps", 0) >= limits.get("max_steps", float("inf")):
        return "max_steps"
    return None


def _segment_successes(row, since_seq):
    count = 0
    for item in row.get("events", []):
        if item.get("seq", 0) <= since_seq or item.get("type") != "tool_result" or item.get("internal"):
            continue
        result = item.get("result")
        if isinstance(result, dict) and not result.get("error") and not result.get("not_executed"):
            count += 1
    return count


def continue_segment(row, reason):
    """Checkpoint a default segment and start the next one in the same task.

    Returns NO_PROGRESS_CODE instead when continuing would only repeat failing
    work: a full step segment with no successful tool result, or two segments
    in a row (for example slow ones that ended on time) without one.
    """
    from .protocol import event
    segment = row.setdefault("execution_segment", {"index": 1, "started_seq": 0})
    successes = _segment_successes(row, segment.get("started_seq", 0))
    steps, elapsed = row.get("steps", 0), row.get("elapsed_seconds", 0.0)
    idle = successes == 0 and steps > 0
    segment["no_progress"] = int(segment.get("no_progress", 0)) + 1 if idle else 0
    if idle and (reason == "max_steps" or segment["no_progress"] >= 2):
        return NO_PROGRESS_CODE
    # Only default counters restart; an owner/legacy per-run limit keeps counting.
    if not segment_is_hard(row, "max_steps"):
        row["steps"] = 0
    if not segment_is_hard(row, "max_seconds"):
        row["elapsed_seconds"] = 0.0
    segment["index"] = int(segment.get("index", 1)) + 1
    event(row, "execution_segment_continued", segment=segment["index"], reason=reason,
          previous_steps=steps, previous_elapsed_seconds=elapsed, successful_tool_results=successes)
    segment["started_seq"] = row["event_cursor"]
    return None


def restart_segment_audit(row):
    """An owner turn or resume starts a fresh run and a fresh progress audit."""
    if "event_cursor" in row:
        row["execution_segment"] = {"index": 1, "started_seq": row["event_cursor"]}


def stop_for_no_progress(row):
    from .protocol import event
    row.update(status="failed", error=NO_PROGRESS_CODE, approved=None, pending_approval=None)
    outcome = row.setdefault("outcome", {})
    outcome.update(status="unverified", assessment_requested=False, summary=progress_summary(row),
                   uncertainties=["A full execution segment produced no successful tool result; "
                                  "the host stopped instead of repeating failing work."])
    event(row, "no_progress_stopped", error=NO_PROGRESS_CODE, steps=row.get("steps", 0))


def run_seconds_left(row, lifetime_remaining):
    """Seconds in-flight work may use: a default segment boundary never cuts it."""
    if segment_is_hard(row, "max_seconds"):
        return min(row["limits"]["max_seconds"] - row["elapsed_seconds"], lifetime_remaining)
    return lifetime_remaining


def run_may_continue(row):
    """False only when the next claim must stop at an owner/legacy per-run limit."""
    boundary = segment_boundary(row)
    return boundary is None or not segment_is_hard(row, boundary)
