"""Shared, host-authored event and protocol primitives."""
from __future__ import annotations

import hashlib
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


def event(row: dict, kind: str, **fields) -> None:
    row["event_cursor"] += 1
    if kind in CONTROL_EVENTS:
        row["control_revision"] += 1
    row["events"].append({"seq": row["event_cursor"], "type": kind, "at": now(), "context_epoch": row.get("context_epoch", 0), **fields})
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
