"""Durable model-transport retries; no sleeping workers and no tool replay."""
import random
import time

NETWORK_ERRORS = {"agent_inference_timeout", "agent_model_connection_failed", "agent_model_busy", "agent_model_upstream_failed", "agent_model_stream_interrupted"}
CONNECTION_REASONS = frozenset({"reset", "refused", "connection", "dns_temporary", "timeout"})
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
DEFAULT_STREAM_MAX_RETRIES = 5
MAX_STREAM_MAX_RETRIES = 20


def now():
    return time.time()


def _stream_max_retries(diagnostics):
    raw = diagnostics.get("stream_max_retries")
    if type(raw) is int:
        return max(0, min(raw, MAX_STREAM_MAX_RETRIES))
    return DEFAULT_STREAM_MAX_RETRIES


def limit(code, diagnostics):
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    if code not in NETWORK_ERRORS:
        return 1  # Existing malformed response / compaction recovery.
    reason = diagnostics.get("network_reason")
    status = diagnostics.get("http_status")
    if reason in {"tls_certificate", "tls", "dns_not_found", "quota"}:
        return 0
    if status is not None and status not in RETRYABLE_HTTP_STATUSES:
        return 0
    if code == "agent_model_connection_failed" and status is None and reason in CONNECTION_REASONS:
        return None
    # urllib cannot distinguish connect from waiting for response headers.
    # Treat an ambiguous timeout conservatively as a slow model response, while
    # mid-stream idle timeouts are retryable stream transport stalls.
    if code == "agent_inference_timeout" and reason != "idle_timeout":
        return 1
    if code == "agent_model_busy":
        return DEFAULT_STREAM_MAX_RETRIES
    return _stream_max_retries(diagnostics)


def delay(count, diagnostics, code):
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    unlimited = limit(code, diagnostics) is None
    overloaded = diagnostics.get("overloaded") is True
    step = max(1, count)
    start, cap = ((10 if overloaded else 5), 60) if unlimited else ((4 if overloaded else 2), 32)
    base = min(cap, start * (2 ** (step - 1)))
    return max(base * random.uniform(0.9, 1.1), diagnostics.get("retry_after_seconds", 0) or 0)


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
        code = previous.get("error_code") or "agent_model_connection_failed"
        max_retries = limit(code, previous.get("diagnostics") or {})
        row["connection_recovery"] = {"state": "retrying", "retry_count": 0, "max_retries": max_retries}


def recovered(row):
    recovery = row.get("connection_recovery")
    if recovery and recovery["state"] == "retrying":
        row["connection_recovery"] = {"state": "recovered", "recovered_at": now(),
                                      "retry_count": recovery["retry_count"], "max_retries": recovery["max_retries"]}
