"""Encrypted transactional Agent state using the node's existing messaging key."""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .state import migrate

VERSION = "ryn.agent.v1"
CHANNEL = b"rynmesh-agent-state-v1"
MAX_PLAINTEXT = 24 * 1024 * 1024
MAX_BYTES = 34 * 1024 * 1024
DEFAULT_SETTINGS = {
    "enabled": False, "model_id": "", "permission_mode": "confirm_changes", "allowed_write_tools": [],
    "max_steps": 16, "max_seconds": 180, "max_context_chars": 60000,
    "max_output_chars": 20000,
}


class AgentError(ValueError):
    """Stable public error code, never an external exception's raw message."""


def cancellation_reason(cancelled) -> str:
    """Distinguish budget exhaustion from cancellation; never export callback text."""
    reason = getattr(cancelled, "reason", None)
    if isinstance(reason, str) and reason in {
        "agent_time_limit", "agent_time_budget_exhausted",
    }:
        return reason
    return "agent_cancelled"


class AgentModelResponseError(AgentError):
    """Rejected response with safe accounting metadata, never provider text."""

    def __init__(self, usage, finish_reason, code="agent_invalid_model_response"):
        super().__init__(code)
        usage = usage if isinstance(usage, dict) else {}
        self.usage = {name: usage.get(name) if type(usage.get(name)) is int and usage[name] >= 0 else None
                      for name in ("input_tokens", "output_tokens")}
        self.finish_reason = finish_reason if isinstance(finish_reason, str) and finish_reason in {"stop", "length", "tool_calls", "content_filter", "cancelled"} else "unknown"


class AgentToolRejected(AgentError):
    """Business layer guarantees this operation caused no side effects.

    Raise only before the mutation boundary; never wrap an uncertain write or
    a post-write publication/verification failure in this exception.
    """


class AgentReadFailure(AgentError):
    """A dispatched read failed; only bounded connection labels may accompany it."""

    def __init__(self, code, *, connection_id, connection_name):
        super().__init__(code)
        self.connection = {"connection_id": connection_id, "connection_name": connection_name}


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class AgentStore:
    def __init__(self, root: str | Path, messaging_key: Any):
        self.root = Path(root)
        self.path = self.root / "sessions.json"
        self.lock = self.root / ".sessions.lock"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": VERSION, "schema_version": 2, "settings": dict(DEFAULT_SETTINGS), "tasks": {}}
        try:
            envelope = read_json(self.path, max_bytes=MAX_BYTES)
            if not isinstance(envelope, dict) or envelope.get("version") != VERSION:
                raise AgentError("agent_state_version_unsupported")
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
            if len(plaintext) > MAX_PLAINTEXT:
                raise AgentError("agent_storage_limit")
            data = json.loads(plaintext)
            if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("tasks"), dict) or not isinstance(data.get("settings"), dict):
                raise AgentError("agent_state_unreadable")
            return migrate(data)
        except AgentError:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise AgentError("agent_state_unreadable") from None

    def _write(self, data: dict) -> None:
        migrate(data)
        reservations = data.get("storage_reservations", {})
        for task_id in list(reservations):
            if not data["tasks"].get(task_id, {}).get("in_flight"):
                reservations.pop(task_id)
        plaintext = encoded(data)
        remaining = sum(max(0, item["bytes"] - max(0, len(encoded(data["tasks"][task_id])) - item["baseline"]))
                        for task_id, item in reservations.items())
        if len(plaintext) + remaining > MAX_PLAINTEXT:
            raise AgentError("agent_storage_limit")
        nonce, ciphertext = peer_box.seal(self.key, self.pub, plaintext, info=CHANNEL)
        atomic_write_json(self.path, {"version": VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_BYTES)

    def reserve(self, data: dict, task_id: str, expected_growth: int, *, extra_disk: int = 0) -> None:
        """Called in the dispatch transaction, before any external operation.

        Other task/control transactions must respect this remaining reservation.
        As this task's receipt/events grow, actual bytes replace reserved bytes.
        Clearing its in-flight marker releases the unused portion atomically.
        """
        current = len(encoded(data))
        root = self.root if self.root.exists() else self.root.parent
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            raise AgentError("agent_storage_unavailable") from None
        if free < 2 * current + 3 * (expected_growth + extra_disk) + 64 * 1024:
            raise AgentError("agent_storage_limit")
        data.setdefault("storage_reservations", {})[task_id] = {"bytes": expected_growth, "baseline": len(encoded(data["tasks"][task_id]))}

    def purge_orphan_snapshots(self) -> None:
        """Caller holds this store's transaction lock; no writer owns a temp."""
        if self.root.exists():
            for path in self.root.iterdir():
                if re.fullmatch(r"\.sessions\.json\.[a-f0-9]{32}\.tmp", path.name):
                    path.unlink()

    def read(self) -> dict:
        with file_transaction(self.lock):
            return self._read()

    def transaction(self, mutate: Callable[[dict], Any]) -> Any:
        with file_transaction(self.lock):
            data = self._read()
            result = mutate(data)
            self._write(data)
            return copy.deepcopy(result)
