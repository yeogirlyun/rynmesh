"""Shared, host-authored event and protocol primitives."""
from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any

from .state import CONTROL_EVENTS
from .store import AgentError, encoded

SENSITIVE = re.compile(r"^(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|private[_-]?key|credential|credentials)$", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def sanitized(value: Any, depth: int = 0) -> Any:
    """Defense in depth; integrations must also expose only explicit safe fields."""
    if depth > 20:
        raise AgentError("agent_output_limit")
    if isinstance(value, dict):
        return {str(key): "[redacted]" if SENSITIVE.fullmatch(str(key)) else sanitized(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item, depth + 1) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value)
    if value is None or type(value) in (int, float, bool):
        return value
    raise AgentError("agent_invalid_output")


# A tool result is already kept verbatim on its operation's receipt. These
# events keep only its short scalar fields (error, path, status...) plus a
# marker; event_result()/hydrated_events() return the receipt's full value.
RESULT_ON_RECEIPT = "_ryn_result_on_receipt"
_RECEIPT_BACKED_EVENTS = frozenset({"tool_result", "tool_execution_completed"})
_COMPACT_MIN_BYTES = 1024
_SCALAR_TEXT_LIMIT = 256


def _receipt(row: dict, operation_id: Any) -> dict | None:
    if not isinstance(operation_id, str):
        return None
    return next((item for item in reversed(row.get("receipts") or [])
                 if isinstance(item, dict) and item.get("operation_id") == operation_id), None)


def _receipt_backed(row: dict, kind: str, fields: dict) -> dict:
    result = fields.get("result")
    if kind not in _RECEIPT_BACKED_EVENTS or not isinstance(result, dict) or RESULT_ON_RECEIPT in result:
        return fields
    if len(encoded(result)) < _COMPACT_MIN_BYTES:
        return fields
    receipt = _receipt(row, fields.get("operation_id"))
    if receipt is None or receipt.get("result") != result:
        return fields  # Only a verbatim copy may be replaced by a reference.
    compact = {key: value for key, value in result.items()
               if value is None or isinstance(value, (bool, int, float))
               or (isinstance(value, str) and len(value) <= _SCALAR_TEXT_LIMIT)}
    compact[RESULT_ON_RECEIPT] = True
    return {**fields, "result": compact}


def event_result(row: dict, item: dict) -> Any:
    """The full result an event recorded, whether stored inline or on its receipt."""
    result = item.get("result")
    if not isinstance(result, dict) or not result.get(RESULT_ON_RECEIPT):
        return result
    receipt = _receipt(row, item.get("operation_id"))
    if receipt is not None and isinstance(receipt.get("result"), dict):
        return receipt["result"]
    return {key: value for key, value in result.items() if key != RESULT_ON_RECEIPT}


def hydrated_events(row: dict, events: list) -> list:
    """Events for owner-facing reads, with receipt-backed results filled in (copies only)."""
    receipts = None
    out = []
    for item in events:
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, dict) and result.get(RESULT_ON_RECEIPT):
            if receipts is None:
                receipts = {receipt.get("operation_id"): receipt for receipt in row.get("receipts") or []
                            if isinstance(receipt, dict)}
            receipt = receipts.get(item.get("operation_id")) or {}
            full = (receipt["result"] if isinstance(receipt.get("result"), dict)
                    else {key: value for key, value in result.items() if key != RESULT_ON_RECEIPT})
            item = {**item, "result": copy.deepcopy(full)}
        out.append(item)
    return out


def event(row: dict, kind: str, **fields) -> None:
    fields = _receipt_backed(row, kind, fields)
    row["event_cursor"] += 1
    if kind in CONTROL_EVENTS:
        row["control_revision"] += 1
    elapsed = row.get("elapsed_seconds")
    timing = ({"run_elapsed_seconds": elapsed}
              if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else {})
    row["events"].append({"seq": row["event_cursor"], "type": kind, "at": now(), "context_epoch": row.get("context_epoch", 0), **timing, **fields})
    row["revision"] += 1
    row["updated_at"] = now()


def task(data: dict, task_id: str) -> dict:
    row = data["tasks"].get(task_id)
    if row is None:
        raise AgentError("agent_task_not_found")
    return row


def model_retry_authority(settings: dict) -> dict:
    """A queued inference retry cannot outlive an owner permission change."""
    return {"permission_mode": settings["permission_mode"],
            "allowed_write_tools": sorted(settings.get("allowed_write_tools", []))}
