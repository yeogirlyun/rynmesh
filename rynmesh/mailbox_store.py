"""Registry-side mailbox file store.

The registry holds sealed envelopes for peers that cannot reach each other
directly. It is a dumb, self-draining spool: it validates the envelope
*shell* (signature, identity binding, freshness, size), never the body, and
deletes messages once acked or expired.

Abuse controls are deliberately local and cheap: one pending cap per
recipient, one in-memory token bucket per sender, and a nonce replay cache per
poll. Nothing here logs, and every raised message is a short stable code that
a registry HTTP handler can return verbatim without leaking peer data.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .crypto import SignedPayload
from .mailbox import (
    POLL_SKEW_S,
    MailboxError,
    rfc3339,
    verify_mailbox_envelope,
    verify_poll_request,
)

MAX_REPLAY_ENTRIES = 4096
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _recipient_hash(peer_id: str) -> str:
    return hashlib.sha256(peer_id.encode("utf-8")).hexdigest()


class FileMailboxStore:
    """Per-recipient spool of sealed envelopes under ``<root>/mailbox``."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_pending_per_recipient: int = 256,
        sender_rate_per_minute: int = 120,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.mailbox_dir = self.root / "mailbox"
        self.max_pending_per_recipient = max(1, int(max_pending_per_recipient))
        self.sender_rate_per_minute = max(1, int(sender_rate_per_minute))
        self._now = now or _utcnow
        self._lock = threading.RLock()
        # from_peer_id -> (tokens, last refill epoch seconds)
        self._buckets: dict[str, tuple[float, float]] = {}
        # (peer_id, nonce) -> expiry epoch seconds
        self._seen_nonces: dict[tuple[str, str], float] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._private_dir(self.mailbox_dir)

    # ------------------------------------------------------------------ API

    def deposit(self, signed: SignedPayload) -> dict[str, Any]:
        """Accept one sealed envelope for its recipient."""

        envelope = verify_mailbox_envelope(signed, now=self._now)
        with self._lock:
            recipient_dir = self._recipient_dir(envelope.to_peer_id)
            path = recipient_dir / f"{envelope.message_id}.json"
            if path.exists():
                raise MailboxError("duplicate")
            self._sweep_dir(recipient_dir)
            if path.exists():  # re-check: the sweep may have removed a stale copy
                raise MailboxError("duplicate")
            pending = self._pending_count(recipient_dir)
            if pending >= self.max_pending_per_recipient:
                raise MailboxError("recipient_full")
            self._take_token(envelope.from_peer_id)
            self._private_dir(recipient_dir)
            _atomic_write_json(
                path,
                {"stored_at": rfc3339(self._moment()), "signed": signed.to_dict()},
            )
            return {
                "message_id": envelope.message_id,
                "expires_at": envelope.expires_at,
                "pending": pending + 1,
            }

    def poll(self, signed_poll: SignedPayload) -> list[SignedPayload]:
        """Return (and ack/expire) mail for the peer that signed the request."""

        payload = verify_poll_request(signed_poll, now=self._now)
        peer_id = str(payload["peer_id"])
        limit = int(payload["limit"])
        acks = [str(item) for item in payload.get("ack", [])]
        with self._lock:
            self._remember_nonce(peer_id, str(payload["nonce"]))
            recipient_dir = self._recipient_dir(peer_id)
            for message_id in acks:
                # verify_poll_request already constrained these to 32 hex
                # characters, so they cannot escape the recipient directory.
                (recipient_dir / f"{message_id}.json").unlink(missing_ok=True)
            self._sweep_dir(recipient_dir)
            found: list[tuple[str, str, SignedPayload]] = []
            for path in sorted(recipient_dir.glob("*.json")):
                signed = self._load(path)
                if signed is None:
                    continue
                try:
                    envelope = verify_mailbox_envelope(signed, now=self._now)
                except MailboxError:
                    path.unlink(missing_ok=True)
                    continue
                if envelope.to_peer_id != peer_id:
                    continue
                found.append((envelope.created_at, envelope.message_id, signed))
            found.sort(key=lambda item: (item[0], item[1]))
            return [signed for _, _, signed in found[:limit]]

    def sweep(self) -> int:
        """Delete every expired envelope in the spool; returns the count."""

        removed = 0
        with self._lock:
            for recipient_dir in sorted(self.mailbox_dir.glob("*/*")):
                if recipient_dir.is_dir():
                    removed += self._sweep_dir(recipient_dir)
        return removed

    # -------------------------------------------------------------- internals

    def _moment(self) -> datetime:
        moment = self._now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _recipient_dir(self, peer_id: str) -> Path:
        digest = _recipient_hash(peer_id)
        return self.mailbox_dir / digest[:2] / digest

    def _private_dir(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        cursor = path
        while True:
            try:
                os.chmod(cursor, _DIR_MODE)
            except OSError:
                pass
            if cursor == self.mailbox_dir or cursor == cursor.parent:
                break
            cursor = cursor.parent
        return path

    def _pending_count(self, recipient_dir: Path) -> int:
        if not recipient_dir.is_dir():
            return 0
        return sum(1 for _ in recipient_dir.glob("*.json"))

    def _load(self, path: Path) -> SignedPayload | None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            return SignedPayload.from_dict(dict(record["signed"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _sweep_dir(self, recipient_dir: Path) -> int:
        """Drop expired (or unreadable) envelopes without decrypting anything."""

        if not recipient_dir.is_dir():
            return 0
        moment = self._moment()
        removed = 0
        for path in sorted(recipient_dir.glob("*.json")):
            signed = self._load(path)
            expires_at = ""
            if signed is not None:
                expires_at = str(signed.payload.get("expires_at") or "")
            if not expires_at:
                path.unlink(missing_ok=True)
                removed += 1
                continue
            try:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= moment:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _take_token(self, sender_peer_id: str) -> None:
        """Per-sender token bucket: ``rate`` burst, ``rate/60`` refilled a second."""

        rate = float(self.sender_rate_per_minute)
        seconds = self._moment().timestamp()
        tokens, last = self._buckets.get(sender_peer_id, (rate, seconds))
        elapsed = max(0.0, seconds - last)
        tokens = min(rate, tokens + elapsed * (rate / 60.0))
        if tokens < 1.0:
            self._buckets[sender_peer_id] = (tokens, seconds)
            raise MailboxError("rate_limited")
        self._buckets[sender_peer_id] = (tokens - 1.0, seconds)

    def _remember_nonce(self, peer_id: str, nonce: str) -> None:
        seconds = self._moment().timestamp()
        expired = [key for key, expiry in self._seen_nonces.items() if expiry <= seconds]
        for key in expired:
            self._seen_nonces.pop(key, None)
        key = (peer_id, nonce)
        if key in self._seen_nonces:
            raise MailboxError("replay")
        if len(self._seen_nonces) >= MAX_REPLAY_ENTRIES:
            # Bounded memory: shed the entries closest to expiry first.
            for stale, _ in sorted(self._seen_nonces.items(), key=lambda item: item[1])[:64]:
                self._seen_nonces.pop(stale, None)
        self._seen_nonces[key] = seconds + 2 * POLL_SKEW_S


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """0600 temp file in the same directory, then an atomic rename."""

    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(tmp, _FILE_MODE)
    except OSError:
        pass
    os.replace(tmp, path)


__all__ = ["FileMailboxStore", "MAX_REPLAY_ENTRIES"]
