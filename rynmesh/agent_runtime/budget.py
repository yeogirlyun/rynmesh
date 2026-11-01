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
        row["budget"] = {"limits": validate_limits({key: row["limits"][key] for key in DEFAULT_LIMITS if key in row["limits"]}),
                         "round": 1, "records": [], "legacy_usage_unknown": legacy,
                         "legacy_model_calls": legacy_models if legacy else 0, "legacy_tool_calls": legacy_tools,
                         "legacy_seconds": row.get("elapsed_seconds", 0.0) if legacy else 0.0}
    return row["budget"]


def new_round(row):
    initialize(row)["round"] += 1


def _totals(state, records, *, lifetime):
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
            "charged_seconds": sum(record["charged_seconds"] for record in records) + (state["legacy_seconds"] if lifetime else 0),
            "active_seconds": None if legacy or any(record.get("elapsed_seconds") is None for record in records) else sum(record["elapsed_seconds"] for record in records),
            "cost": None, "cost_status": "unknown", "usage_complete": not unknown and not legacy,
            "legacy_usage_unknown": legacy,
            "unsettled_operations": sum(record["status"] == "reserved" for record in records)}


def public(row):
    state = initialize(row)
    return {"limits": copy.deepcopy(state["limits"]), "round": state["round"],
            "lifetime": _totals(state, state["records"], lifetime=True),
            "current_round": _totals(state, [record for record in state["records"] if record["round"] == state["round"]], lifetime=False),
            "reservation_policy": "Unknown model usage retains the conservative dispatch reservation; cost is unknown without trusted pricing."}


def reserve(row, kind, stage, *, input_bound=0, output_bound=0, seconds_bound=None):
    state = initialize(row)
    if kind not in {"model", "tool"}:
        raise AgentError("agent_invalid_budget")
    total = _totals(state, state["records"], lifetime=True)
    limits = state["limits"]
    if total["charged_model_calls"] + (kind == "model") > limits["max_total_model_calls"]:
        raise AgentError("agent_model_call_budget_exhausted")
    if total["charged_tool_calls"] + (kind == "tool") > limits["max_total_tool_calls"]:
        raise AgentError("agent_tool_budget_exhausted")
    tokens = input_bound + output_bound if kind == "model" else 0
    if type(tokens) is not int or tokens < 0 or total["charged_tokens"] + tokens > limits["max_total_tokens"]:
        raise AgentError("agent_token_budget_exhausted")
    remaining = limits["max_total_seconds"] - total["charged_seconds"]
    if remaining <= 0 and seconds_bound != 0:
        raise AgentError("agent_time_budget_exhausted")
    seconds = min(remaining, seconds_bound if seconds_bound is not None else row["limits"]["max_seconds"])
    record = {"id": "usage_" + uuid.uuid4().hex, "kind": kind, "stage": stage, "round": state["round"],
              "status": "reserved", "input_tokens": None, "output_tokens": None,
              "charged_tokens": tokens, "charged_seconds": seconds, "elapsed_seconds": None,
              "input_reservation": input_bound, "output_reservation": output_bound}
    state["records"].append(record)
    return copy.deepcopy(record)


def settle(row, reservation_id, *, usage=None, elapsed_seconds=None):
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
        record["charged_tokens"] = sum(record[key] if record[key] is not None else record[bound] for key, bound in
                                       (("input_tokens", "input_reservation"), ("output_tokens", "output_reservation")))
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
    return max(0.0, state["limits"]["max_total_seconds"] - used)


def notice(row: dict) -> str | None:
    """Tell the model, once per threshold, how much of its step budget is gone."""
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
