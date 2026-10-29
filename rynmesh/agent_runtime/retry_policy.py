"""Durable model-transport retries; no sleeping workers and no tool replay."""
import random
import time

NETWORK_ERRORS = {"agent_inference_timeout", "agent_model_connection_failed", "agent_model_busy", "agent_model_upstream_failed"}


def now():
    return time.time()


def limit(code, diagnostics):
    if code not in NETWORK_ERRORS:
        return 1  # Existing malformed response / compaction recovery.
    reason = diagnostics.get("network_reason")
    status = diagnostics.get("http_status")
    if reason in {"tls_certificate", "tls", "dns_not_found", "quota"}:
        return 0
    if status is not None and status not in {408, 429, 500, 502, 503, 504}:
        return 0
    # urllib cannot distinguish connect from waiting for response headers.
    # Treat an ambiguous timeout conservatively as a slow model response.
    return 1 if code == "agent_inference_timeout" else 3


def delay(count, diagnostics):
    return max((2, 5, 10)[min(count - 1, 2)] * random.uniform(0.9, 1.1),
               diagnostics.get("retry_after_seconds", 0))


def ready(row):
    recovery = row.get("connection_recovery") or {}
    return now() >= recovery.get("next_retry_at", 0)


def charge_wait(row):
    recovery = row.get("connection_recovery") or {}
    start = recovery.pop("wait_started_at", None)
    end = recovery.pop("next_retry_at", None)
    if start is not None and end is not None:
        # Count the scheduled backoff, not downtime while the app was closed.
        elapsed = max(0, min(now(), end) - start)
        row["elapsed_seconds"] += elapsed
        row["retry_wait_seconds"] = row.get("retry_wait_seconds", 0) + elapsed
        from . import budget
        state = budget.initialize(row)
        waits = state.setdefault("retry_wait_by_round", {})
        key = str(state["round"])
        waits[key] = waits.get(key, 0) + elapsed


def stop(row):
    recovery = row.get("connection_recovery")
    if recovery and recovery["state"] == "retrying":
        charge_wait(row)
        recovery["state"] = "stopped"


def reset(row):
    charge_wait(row)
    row.pop("connection_recovery", None)
    row["model_retry_count"] = 0
    row.pop("model_retry_authority", None)


def resume(row):
    previous = row.get("connection_recovery")
    reset(row)
    if previous and previous["state"] in {"stopped", "disconnected"}:
        row["connection_recovery"] = {"state": "retrying", "retry_count": 0, "max_retries": 3}


def recovered(row):
    recovery = row.get("connection_recovery")
    if recovery and recovery["state"] == "retrying":
        row["connection_recovery"] = {"state": "recovered", "recovered_at": now(),
                                      "retry_count": recovery["retry_count"], "max_retries": recovery["max_retries"]}
