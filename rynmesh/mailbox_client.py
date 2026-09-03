"""Node-side mailbox client: poll, verify, open, dispatch, ack.

The registry holds sealed envelopes for peers that cannot reach each other
directly (see ``rynmesh.mailbox`` / ``rynmesh.mailbox_store``). This module is
the other half: the node's own loop over its box.

Delivery is at-least-once by construction — an ack rides along with the *next*
poll, so a crash between "handler ran" and "ack sent" redelivers the message.
Two things keep that safe:

* a persistent **seen cache** (message id -> expiry) rejects a redelivery of a
  message a handler already completed, so handlers see each message once in
  the normal case;
* a per-message **attempt counter** retries a failing handler across polls and
  gives up after ``max_attempts``, so one poisonous message cannot wedge the
  box forever.

Nothing here logs a body, a ciphertext, or a key: log records carry the
verified ``kind``, the 32-hex message id, and an exception *class* name only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .mailbox import (
    DEFAULT_TTL_S,
    MAX_ACK_IDS,
    MAX_POLL_LIMIT,
    MailboxEnvelope,
    MailboxError,
    build_poll_request,
    open_mailbox_message,
    rfc3339,
    seal_mailbox_message,
)

log = logging.getLogger("rynmesh.mailbox_client")

Handler = Callable[[MailboxEnvelope, dict[str, Any]], None]

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_SEEN_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_id(value: Any) -> str:
    """A message id is only safe to log once it looks like one.

    Ids arrive inside a not-yet-verified payload, so an arbitrary string could
    otherwise be written straight into the node's log by a remote sender.
    """

    text = str(value or "")
    return text if _HEX32.match(text) else "invalid"


class MailboxClient:
    """One node's mailbox: send with ``deposit``, receive with ``poll_once``."""

    def __init__(
        self,
        *,
        store: Any,
        messaging_key: X25519PrivateKey,
        home: str | Path,
        resolve_messaging_pub: Callable[[str], str],
        now: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
        seen_capacity: int = 5000,
    ) -> None:
        self._store = store
        self._messaging_key = messaging_key
        self._home = Path(home).expanduser()
        self._resolve_messaging_pub = resolve_messaging_pub
        self._now = now or _utcnow
        self._max_attempts = max(1, int(max_attempts))
        self._seen_capacity = max(1, int(seen_capacity))
        self._lock = threading.RLock()
        self._handlers: dict[str, Handler] = {}
        self._pending_acks: list[str] = []
        self._attempts: dict[str, int] = {}
        # message_id -> expiry, epoch seconds. Insertion-ordered so the bound
        # evicts the oldest entry rather than an arbitrary one.
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._handled_total = 0
        self._dropped_total = 0
        self._pending_last = 0
        self._last_poll_at = ""
        self._last_error = ""
        self._seen_path = self._home / "mailbox" / "seen.json"
        self._load_seen()

    # ---------------------------------------------------------------- public

    def register_handler(self, kind: str, handler: Handler, *, replace: bool = False) -> None:
        """Bind one message kind to its handler. Kinds are claimed exactly once."""

        cleaned = str(kind or "").strip()
        if not cleaned:
            raise ValueError("mailbox handler kind must not be empty")
        if not callable(handler):
            raise TypeError("mailbox handler must be callable")
        with self._lock:
            if cleaned in self._handlers and not replace:
                raise ValueError(f"mailbox handler already registered: {cleaned}")
            self._handlers[cleaned] = handler

    def deposit(
        self,
        to_peer_id: str,
        kind: str,
        body: dict[str, Any],
        *,
        ttl_s: int = DEFAULT_TTL_S,
        to_messaging_pub: str | None = None,
    ) -> dict[str, Any]:
        """Seal ``body`` for a peer and hand the envelope to the registry."""

        registry = getattr(self._store, "registry", None)
        if registry is None:
            raise MailboxError("no_registry")
        pub = str(to_messaging_pub or "") or str(self._resolve_messaging_pub(to_peer_id) or "")
        signed = seal_mailbox_message(
            kind=kind,
            body=body,
            from_private_key_bytes=self._store.private_key_bytes,
            to_peer_id=to_peer_id,
            to_messaging_pub=pub,
            ttl_s=ttl_s,
            now=self._now,
        )
        receipt = registry.deposit_mailbox(signed)
        result = dict(receipt) if isinstance(receipt, dict) else {}
        result.setdefault("message_id", str(signed.payload.get("message_id", "")))
        return result

    def poll_once(self) -> int:
        """Fetch, dispatch and ack one batch. Returns the number handled."""

        registry = getattr(self._store, "registry", None)
        if registry is None:
            with self._lock:
                self._last_error = "no_registry"
            return 0

        with self._lock:
            # `build_poll_request` refuses more than MAX_ACK_IDS, and a batch is
            # capped at MAX_POLL_LIMIT, so this slice never starves an ack: the
            # remainder rides the next poll.
            acks = list(self._pending_acks[:MAX_ACK_IDS])
        try:
            signed_poll = build_poll_request(
                private_key_bytes=self._store.private_key_bytes,
                ack=acks,
                limit=MAX_POLL_LIMIT,
                now=self._now,
            )
            messages = registry.poll_mailbox(signed_poll)
        except Exception as exc:
            with self._lock:
                self._last_error = type(exc).__name__
            raise

        # Handlers run under the lock: one worker owns `poll_once`, and holding
        # it for the batch keeps the seen cache, the ack queue and the counters
        # consistent. The only reader it can delay is `status()`.
        with self._lock:
            # The acked ids were deleted server-side; anything queued while the
            # request was in flight stays pending.
            acked = set(acks)
            self._pending_acks = [item for item in self._pending_acks if item not in acked]
            handled = self._dispatch(messages)
            self._pending_last = len(messages)
            self._last_poll_at = rfc3339(self._resolve_now())
            self._last_error = ""
        return handled

    def status(self) -> dict[str, Any]:
        """Counters and handler names only — never a body or an error message."""

        with self._lock:
            return {
                "handled_total": self._handled_total,
                "dropped_total": self._dropped_total,
                "pending_last": self._pending_last,
                "last_poll_at": self._last_poll_at,
                "last_error": self._last_error,
                "handlers": sorted(self._handlers),
            }

    # --------------------------------------------------------------- private

    def _dispatch(self, messages: Any) -> int:
        """Open, dispatch and ack every message in one polled batch."""

        handled = 0
        changed = False
        for signed_message in messages or ():
            payload = getattr(signed_message, "payload", None)
            raw_id = payload.get("message_id") if isinstance(payload, dict) else ""
            message_id = str(raw_id or "")

            if message_id and message_id in self._seen:
                # Already completed by a handler; the ack simply never landed.
                self._ack(message_id)
                self._dropped_total += 1
                log.warning("mailbox replay dropped id=%s", _safe_id(message_id))
                continue

            try:
                envelope, body = open_mailbox_message(
                    signed_message,
                    my_peer_id=self._store.peer_id,
                    messaging_private_key=self._messaging_key,
                    now=self._now,
                )
            except Exception as exc:
                # Broad on purpose: one unopenable message — a bad envelope, a
                # non-envelope a hostile registry pushed at us — must not wedge
                # the box. `kind` is unverified here, so it is not logged.
                self._ack(message_id)
                self._dropped_total += 1
                log.warning(
                    "mailbox envelope rejected id=%s error=%s",
                    _safe_id(message_id),
                    type(exc).__name__,
                )
                continue

            message_id = envelope.message_id
            handler = self._handlers.get(envelope.kind)
            if handler is None:
                self._ack(message_id)
                self._dropped_total += 1
                log.warning(
                    "mailbox message dropped kind=%s id=%s reason=unknown_kind",
                    envelope.kind,
                    _safe_id(message_id),
                )
                continue

            try:
                handler(envelope, body)
            except Exception as exc:
                attempts = self._attempts.get(message_id, 0) + 1
                self._attempts[message_id] = attempts
                log.error(
                    "mailbox handler failed kind=%s id=%s attempt=%d error=%s",
                    envelope.kind,
                    _safe_id(message_id),
                    attempts,
                    type(exc).__name__,
                )
                if attempts >= self._max_attempts:
                    # Poison message: stop redelivering it and let it expire.
                    self._attempts.pop(message_id, None)
                    self._ack(message_id)
                    self._dropped_total += 1
                continue

            self._attempts.pop(message_id, None)
            changed = self._remember(message_id, envelope.expires_at) or changed
            self._ack(message_id)
            self._handled_total += 1
            handled += 1

        if changed:
            self._save_seen()
        return handled

    def _ack(self, message_id: str) -> None:
        """Queue an ack for the next poll (ids are deduped and order-stable)."""

        if not message_id or not _HEX32.match(message_id):
            return
        if message_id not in self._pending_acks:
            self._pending_acks.append(message_id)

    def _resolve_now(self) -> datetime:
        moment = self._now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _epoch(self) -> float:
        return self._resolve_now().timestamp()

    def _remember(self, message_id: str, expires_at: str) -> bool:
        """Record a completed message until its envelope would have expired."""

        try:
            parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        self._seen[message_id] = parsed.timestamp()
        self._seen.move_to_end(message_id)
        self._prune_seen()
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)
        return True

    def _prune_seen(self) -> None:
        moment = self._epoch()
        for key in [key for key, expiry in self._seen.items() if expiry <= moment]:
            self._seen.pop(key, None)

    def _load_seen(self) -> None:
        try:
            raw = json.loads(self._seen_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return
        restored: list[tuple[str, float]] = []
        for key, value in entries.items():
            if not _HEX32.match(str(key)):
                continue
            try:
                restored.append((str(key), float(value)))
            except (TypeError, ValueError):
                continue
        # The file is written with sorted keys for a stable diff, so insertion
        # order does not survive a round trip. Expiry is the age proxy that
        # does, and it is what the capacity bound should evict by.
        for key, expiry in sorted(restored, key=lambda item: item[1]):
            self._seen[key] = expiry
        self._prune_seen()
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)

    def _save_seen(self) -> None:
        """Atomic 0600 write; a failure here must never lose a delivery."""

        self._prune_seen()
        directory = self._seen_path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, _DIR_MODE)
        except OSError:
            return
        value = {"version": _SEEN_VERSION, "entries": dict(self._seen)}
        tmp = self._seen_path.with_name(self._seen_path.name + ".tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, self._seen_path)
        except OSError:
            # The cache is an optimization over an at-least-once channel: a
            # write failure costs a possible duplicate delivery, never a lost
            # message, so it must not fail the poll.
            tmp.unlink(missing_ok=True)
            log.warning("mailbox seen cache write failed")


__all__ = ["Handler", "MailboxClient"]
