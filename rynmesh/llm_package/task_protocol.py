"""Signed end-to-end encrypted LLM task envelopes and metadata-only records."""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.atomic_io import atomic_write_json
from rynmesh.crypto import SignedPayload, sign_payload, verify_signed_payload
from rynmesh.services import peer_box

from .safety import check_disk

_logger = logging.getLogger(__name__)

TASK_ENVELOPE_VERSION = "rynmesh.llm.e2ee.v1"
TERMINAL_STATES = {"succeeded", "failed", "timed_out", "cancelled", "rejected"}
ALL_STATES = {"created", "accepted", "running", *TERMINAL_STATES}
ALLOWED_TRANSITIONS = {
    "created": {"accepted", "failed", "timed_out", "cancelled", "rejected"},
    "accepted": {"running", "failed", "timed_out", "cancelled", "rejected"},
    "running": {"succeeded", "failed", "timed_out", "cancelled", "rejected"},
}


class TaskProtocolError(RuntimeError):
    pass


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


# A paid, succeeded answer must survive until the ask worker archives it and
# calls mark_acknowledged, even across retention purges and expiry sweeps.
# This is a hard ceiling, not a promise of indefinite retention: a record
# stuck unacknowledged (e.g. a permanently offline consumer) still loses its
# encrypted response after this bound.
UNACKNOWLEDGED_SUCCESS_GRACE = timedelta(days=7)


def _awaiting_archive(record: dict[str, Any], now: datetime) -> bool:
    """True while a succeeded record's encrypted response must be kept for
    the ask worker to archive, overriding retention-zero and expiry purges.

    The 7-day grace is measured from the original success, never from a
    later body-free checkpoint (e.g. a `settlement_dispatched` note written
    on every node restart by `_recover_consumer_orders`, or right after
    settlement in the normal request path) — `TaskOrderStore.checkpoint`
    re-appends the record's current state with a fresh timestamp, so those
    entries are excluded here to keep the 7-day clock from being reset by
    unrelated bookkeeping.
    """
    if record.get("state") != "succeeded":
        return False
    if record.get("acknowledged_at"):
        return False
    if not record.get("encrypted_response"):
        return False
    top_completed_at = record.get("completed_at")
    history_completed_at = next(
        (item.get("at") for item in reversed(record.get("history") or [])
         if item.get("state") == "succeeded" and item.get("at") and not item.get("checkpoint")),
        "",
    )
    candidates: list[datetime] = []
    for raw_ts in (top_completed_at, history_completed_at):
        if raw_ts:
            try:
                candidates.append(_parse_time(str(raw_ts)))
            except (TypeError, ValueError):
                continue
    if not candidates:
        return False
    completed = min(candidates)
    return now - completed < UNACKNOWLEDGED_SUCCESS_GRACE


def _has_settlement_pending(record: dict[str, Any]) -> bool:
    if record.get("settlement_dispatched"):
        return False
    if record.get("settlement_pending"):
        return True
    history = record.get("history") or []
    if any(isinstance(item, dict) and item.get("settlement_dispatched") for item in history):
        return False
    return any(isinstance(item, dict) and item.get("settlement_pending") for item in history)


def seal_task(*, body: dict[str, Any], task_id: str, kind: str, sender_peer_id: str,
              recipient_peer_id: str, sender_signing_key: bytes,
              recipient_messaging_pub: str, expires_at: str) -> SignedPayload:
    ephemeral = X25519PrivateKey.generate()
    nonce, ciphertext = peer_box.seal(
        ephemeral, recipient_messaging_pub,
        json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
    )
    outer = {
        "version": TASK_ENVELOPE_VERSION, "kind": kind, "task_id": task_id,
        "from_peer_id": sender_peer_id, "to_peer_id": recipient_peer_id,
        "ephemeral_pub": peer_box.public_key_b64(ephemeral), "nonce": nonce,
        "ciphertext": ciphertext, "expires_at": expires_at,
    }
    return sign_payload(outer, private_key_bytes=sender_signing_key)


def open_task(signed: SignedPayload | dict[str, Any], *, recipient_peer_id: str,
              recipient_messaging_key: X25519PrivateKey, expected_kind: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = signed if isinstance(signed, SignedPayload) else SignedPayload.from_dict(signed)
    verify_signed_payload(envelope)
    outer = dict(envelope.payload)
    if outer.get("version") != TASK_ENVELOPE_VERSION:
        raise TaskProtocolError("task envelope version unsupported")
    if outer.get("from_peer_id") != envelope.public_key:
        raise TaskProtocolError("task sender identity/signature mismatch")
    if outer.get("to_peer_id") != recipient_peer_id:
        raise TaskProtocolError("task recipient mismatch")
    if expected_kind and outer.get("kind") != expected_kind:
        raise TaskProtocolError("task envelope kind mismatch")
    try:
        if _parse_time(str(outer["expires_at"])) < datetime.now(timezone.utc):
            raise TaskProtocolError("task envelope expired")
        plaintext = peer_box.open_sealed(
            recipient_messaging_key, str(outer["ephemeral_pub"]),
            str(outer["nonce"]), str(outer["ciphertext"]),
        )
        body = json.loads(plaintext.decode("utf-8"))
    except TaskProtocolError:
        raise
    except Exception as exc:
        raise TaskProtocolError("task envelope authentication/decryption failed") from exc
    if not isinstance(body, dict) or str(body.get("task_id")) != str(outer.get("task_id")):
        raise TaskProtocolError("task body/id mismatch")
    return outer, body


class TaskOrderStore:
    """Durable order metadata and optional encrypted response; never body plaintext."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._count = sum(1 for _ in self.root.glob("*.json"))
        self.quarantined = 0
        self._memory_responses = False
        self._response_ttl_seconds = 60.0
        self._responses: dict[str, tuple[float, dict[str, Any]]] = {}
        self._response_timer = None

    def _schedule_expiry(self):
        if self._response_timer is not None or not self._responses:
            return
        reference = weakref.ref(self)
        def expire():
            store = reference()
            if store is not None:
                with store._lock:
                    store._response_timer = None
                    store._prune_responses()
                    store._schedule_expiry()
        delay = max(0.01, min(expiry for expiry, _ in self._responses.values()) - time.monotonic())
        self._response_timer = threading.Timer(delay, expire)
        self._response_timer.daemon = True
        self._response_timer.start()

    def use_memory_responses(self, ttl_seconds: float | None = None) -> None:
        """Bounded encrypted retry cache; never persist provider response bodies."""
        with self._lock:
            if ttl_seconds is not None and isinstance(ttl_seconds, (int, float)) and ttl_seconds > 0:
                self._response_ttl_seconds = (
                    max(self._response_ttl_seconds, float(ttl_seconds))
                    if self._memory_responses
                    else float(ttl_seconds)
                )
            if self._memory_responses:
                return
            for record in self.list():
                self.purge_encrypted_response(record["task_id"])
            self._memory_responses = True

    def _prune_responses(self):
        now = time.monotonic()
        for key in [k for k, (expires, _) in self._responses.items() if expires <= now]:
            self._responses.pop(key, None)

    def get(self, task_id: str) -> dict[str, Any] | None:
        # Windows can reject reads/replaces while another thread holds the file.
        # Use the same reentrant lock as transitions and checkpoints.
        with self._lock:
            return self._read(task_id)

    def _quarantine_file(self, path: Path, task_id: str) -> None:
        if not path.exists():
            return
        q_dir = self.root / "quarantine"
        q_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = q_dir / f"{path.name}.{stamp}"
        if dest.exists():
            dest = q_dir / f"{path.name}.{stamp}_{self.quarantined}"
        try:
            path.replace(dest)
        except FileNotFoundError:
            return
        self.quarantined += 1
        self._count = max(0, self._count - 1)
        if task_id:
            self._responses.pop(task_id, None)
        _logger.warning("隔离 1 条")
        self._changed.notify_all()

    def _read(self, task_id: str) -> dict[str, Any] | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TaskProtocolError(f"cannot read task record: {exc}") from exc
        if not isinstance(value, dict):
            raise TaskProtocolError("task record root must be a JSON object")
        self._prune_responses()
        if task_id in self._responses:
            value["encrypted_response"] = self._responses[task_id][1]
        return value

    def _read_or_quarantine(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                path = self._path(task_id)
            except TaskProtocolError:
                raw_path = self.root / f"{task_id}.json"
                self._quarantine_file(raw_path, "")
                return None
            if not path.exists():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._quarantine_file(path, task_id)
                return None
            if not isinstance(value, dict):
                self._quarantine_file(path, task_id)
                return None
            self._prune_responses()
            if task_id in self._responses:
                value["encrypted_response"] = self._responses[task_id][1]
            return value

    def wait_terminal(self, task_id: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._changed:
            while True:
                record = self._read(task_id)
                if record is None:
                    return None
                if record.get("state") in TERMINAL_STATES:
                    return record
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._changed.wait(timeout=remaining)

    def claim(self, *, task_id: str, bindings: dict[str, str]) -> tuple[dict[str, Any], bool]:
        """Atomically create a task or validate an exact idempotent duplicate."""
        cleaned = {str(key): str(value) for key, value in bindings.items()}
        if not cleaned or any(not value for value in cleaned.values()):
            raise TaskProtocolError("task bindings must be non-empty strings")
        if any(len(value) > 4096 for value in cleaned.values()):
            raise TaskProtocolError("task binding exceeds limit")
        with self._lock:
            existing = self.get(task_id)
            if existing is not None:
                if dict(existing.get("bindings") or {}) != cleaned:
                    raise TaskProtocolError("task idempotency conflict")
                return existing, False
            now = datetime.now(timezone.utc).isoformat()
            if self._count >= 10000:
                raise TaskProtocolError("task_record_limit_reached")
            record = {
                "task_id": task_id,
                "state": "created",
                "bindings": cleaned,
                "created_at": now,
                "updated_at": now,
                "history": [{"state": "created", "at": now}],
            }
            self._write(record)
            return record, True

    def transition(self, *, task_id: str, state: str, metadata: dict[str, Any] | None = None,
                   encrypted_response: dict[str, Any] | None = None,
                   allow_recovery: bool = False) -> dict[str, Any]:
        if state not in ALL_STATES:
            raise TaskProtocolError("invalid task state")
        with self._lock:
            record = self.get(task_id)
            if record is None:
                if state != "created":
                    raise TaskProtocolError("task must be created before transitioning")
                now = datetime.now(timezone.utc).isoformat()
                record = {
                    "task_id": task_id,
                    "state": "created",
                    "created_at": now,
                    "updated_at": now,
                    "history": [{"state": "created", "at": now, **dict(metadata or {})}],
                }
                self._write(record)
                return record
            current_state = record.get("state")
            recovery = allow_recovery and current_state in {"failed", "timed_out"} and state == "succeeded"
            if current_state in TERMINAL_STATES and not recovery:
                return record
            if state == current_state:
                return record
            if not recovery and state not in ALLOWED_TRANSITIONS.get(str(current_state), set()):
                raise TaskProtocolError(f"invalid task transition: {current_state} -> {state}")
            now = datetime.now(timezone.utc).isoformat()
            event = {"state": state, "at": now, **dict(metadata or {})}
            if recovery:
                event["recovered_after_reconnect"] = True
            forbidden = {"prompt", "response", "text", "messages", "context", "api_key", "model_path"}
            if forbidden.intersection(event):
                raise TaskProtocolError("task metadata contains a forbidden body/secret field")
            record.update({"state": state, "updated_at": now})
            if state == "succeeded":
                record["completed_at"] = now
            if metadata and metadata.get("response_expires_at"):
                record["response_expires_at"] = metadata["response_expires_at"]
            record.setdefault("history", []).append(event)
            if encrypted_response is not None:
                record["encrypted_response"] = encrypted_response
            self._write(record)
            return record

    def checkpoint(self, *, task_id: str, metadata: dict[str, Any],
                   encrypted_response: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist body-free progress without changing the task state."""
        with self._lock:
            record = self.get(task_id)
            if record is None:
                raise TaskProtocolError("task must exist before checkpointing")
            now = datetime.now(timezone.utc).isoformat()
            event = {"state": str(record.get("state") or "created"), "at": now,
                     "checkpoint": True, **dict(metadata)}
            forbidden = {"prompt", "response", "text", "messages", "context", "api_key", "model_path"}
            if forbidden.intersection(event):
                raise TaskProtocolError("task metadata contains a forbidden body/secret field")
            record["updated_at"] = now
            if metadata and metadata.get("response_expires_at"):
                record["response_expires_at"] = metadata["response_expires_at"]
            record.setdefault("history", []).append(event)
            if encrypted_response is not None:
                record["encrypted_response"] = encrypted_response
            self._write(record)
            return record

    def list(self) -> list[dict[str, Any]]:
        records = []
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                record = self._read_or_quarantine(path.stem)
                if record:
                    records.append({k: v for k, v in record.items() if k != "encrypted_response"})
        return records

    def purge_encrypted_response(self, task_id: str) -> bool:
        with self._lock:
            record = self.get(task_id)
            if record is None or "encrypted_response" not in record:
                return False
            record.pop("encrypted_response", None)
            self._responses.pop(task_id, None)
            record["response_purged_at"] = datetime.now(timezone.utc).isoformat()
            self._write(record)
            return True

    def mark_acknowledged(self, task_id: str) -> bool:
        """Record that a consumer has archived this succeeded result, so
        retention/expiry purges are free to reclaim its encrypted response."""
        with self._lock:
            record = self.get(task_id)
            if record is None:
                return False
            record["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
            self._write(record)
            return True

    def delete(self, task_id: str) -> bool:
        with self._lock:
            path = self._path(task_id)
            if not path.exists():
                return False
            path.unlink()
            self._count = max(0, self._count - 1)
            self._responses.pop(task_id, None)
            self._changed.notify_all()
            return True

    def purge_expired_responses(self) -> int:
        purged = 0
        now = datetime.now(timezone.utc)
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                record = self._read_or_quarantine(path.stem)
                if not record or _awaiting_archive(record, now):
                    continue
                expires_at = record.get("response_expires_at") or next(
                    (item.get("response_expires_at") for item in reversed(record.get("history") or [])
                     if item.get("response_expires_at")),
                    "",
                )
                if expires_at:
                    try:
                        if _parse_time(str(expires_at)) <= now:
                            purged += int(self.purge_encrypted_response(str(record["task_id"])))
                    except (TypeError, ValueError):
                        purged += int(self.purge_encrypted_response(str(record["task_id"])))
        return purged

    def prune_terminal(self, *, older_than: datetime, skip_awaiting_archive: bool = True) -> int:
        """Delete terminal records older than the provider retention boundary."""
        boundary = older_than.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        removed = 0
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                record = self._read_or_quarantine(path.stem)
                if not record or record.get("state") not in TERMINAL_STATES:
                    continue
                if skip_awaiting_archive and (_awaiting_archive(record, now) or _has_settlement_pending(record)):
                    continue
                try:
                    updated_at = _parse_time(str(record.get("updated_at") or ""))
                except (TypeError, ValueError):
                    updated_at = datetime.min.replace(tzinfo=timezone.utc)
                if updated_at <= boundary:
                    if path.exists():
                        path.unlink(missing_ok=True)
                        self._count = max(0, self._count - 1)
                    self._responses.pop(str(record.get("task_id") or path.stem), None)
                    removed += 1
            if removed:
                self._changed.notify_all()
        return removed

    def _path(self, task_id: str) -> Path:
        if not task_id or len(task_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in task_id):
            raise TaskProtocolError("invalid task id")
        return self.root / f"{task_id}.json"

    def _write(self, value: dict[str, Any]) -> None:
        with self._changed:
            value = dict(value)
            if self._memory_responses:
                encrypted = value.pop("encrypted_response", None)
                if encrypted is not None:
                    self._prune_responses()
                    if len(self._responses) >= 32 and value["task_id"] not in self._responses:
                        self._responses.pop(next(iter(self._responses)))
                    self._responses[value["task_id"]] = (
                        time.monotonic() + self._response_ttl_seconds,
                        encrypted,
                    )
                    self._schedule_expiry()
            path = self._path(str(value["task_id"]))
            is_new = not path.exists()
            if len(value.get("history", [])) > 64:
                value["history"] = [value["history"][0], *value["history"][-63:]]
            encoded = json.dumps(value, indent=2, sort_keys=True)
            if len(encoded.encode()) > 16 * 1024 * 1024:
                raise TaskProtocolError("task record size limit exceeded")
            check_disk(self.root)
            atomic_write_json(path, value, indent=2, sort_keys=True)
            if is_new:
                self._count += 1
            self._changed.notify_all()
