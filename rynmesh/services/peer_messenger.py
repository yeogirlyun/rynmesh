"""Send/receive orchestration for 1:1 peer messaging. Pure of network/clock via
injected `transport`, `resolve_pubkey`, `now`, `new_id` (unit-testable)."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Callable

from rynmesh.services import peer_box
from rynmesh.services.messaging_store import MessagingStore

MAX_INLINE_BYTES = 5 * 1024 * 1024

# A sealed header carries the attachment as base64, so a 5 MiB attachment makes a
# ~6.7 MiB header. The mailbox envelope caps at 64 KiB *after* the header is wrapped,
# encrypted (+16 byte tag) and base64'd (x4/3), plus the envelope's own fields and
# signature — so the largest header that actually fits is a little over 48,600 bytes.
# This gate sits just under that: a header above it can only ever be rejected by the
# registry, and offering it would burn a sender's rate-limit token on a message that
# cannot fit. Such messages stay direct-only.
MAX_MAILBOX_HEADER_BYTES = 47 * 1024


class MessengerError(RuntimeError):
    pass


def _kind(text: str, attachment: dict[str, Any] | None) -> str:
    if not attachment:
        return "text"
    mime = str(attachment.get("mime", ""))
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


class PeerMessenger:
    def __init__(
        self,
        *,
        my_peer_id: str,
        my_priv: Any,
        store: MessagingStore,
        resolve_pubkey: Callable[[str], str],
        transport: Callable[[str, dict[str, Any]], int],
        now: Callable[[], str],
        new_id: Callable[[], str] | None = None,
        fallback: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> None:
        self._me = my_peer_id
        self._priv = my_priv
        self._store = store
        self._resolve_pubkey = resolve_pubkey
        self._transport = transport
        self._now = now
        self._new_id = new_id or (lambda: uuid.uuid4().hex)
        # store-and-forward: `fallback(peer_id, sealed_header) -> True when queued`.
        # Called only after a genuine direct-delivery failure; the header handed to
        # it is the very same sealed one the transport tried, so the recipient runs
        # the identical `receive` path whichever way it arrives.
        self._fallback = fallback

    def _queue_for_later(self, peer_id: str, header: dict[str, Any]) -> bool:
        """Offer an undelivered header to the store-and-forward fallback."""

        if self._fallback is None:
            return False
        try:
            if len(json.dumps(header)) > MAX_MAILBOX_HEADER_BYTES:
                return False
            return bool(self._fallback(peer_id, header))
        except Exception:
            # A full, rate-limited or unreachable mailbox is not a send error: the
            # record already says `delivered: False`, and the sender can retry.
            return False

    # ---- send ----
    def send(self, peer_id: str, *, text: str = "", attachment: dict[str, Any] | None = None) -> dict[str, Any]:
        att_bytes = attachment.get("bytes") if attachment else None
        if att_bytes is not None and len(att_bytes) > MAX_INLINE_BYTES:
            raise MessengerError(f"attachment exceeds {MAX_INLINE_BYTES} bytes — use file transfer")
        msg_id = self._new_id()
        ts = self._now()
        inner: dict[str, Any] = {"msg_id": msg_id, "ts": ts, "kind": _kind(text, attachment), "text": text}
        if attachment is not None:
            inner["attachment"] = {
                "filename": attachment.get("filename", "file"),
                "mime": attachment.get("mime", "application/octet-stream"),
                "size": len(att_bytes or b""),
                "bytes": base64.b64encode(att_bytes or b"").decode("ascii"),
            }
        their_pub = self._resolve_pubkey(peer_id)
        nonce, ct = peer_box.seal(self._priv, their_pub, json.dumps(inner).encode("utf-8"))
        header = {"v": 1, "from": self._me, "to": peer_id, "nonce": nonce, "ciphertext": ct,
                  "from_pub": peer_box.public_key_b64(self._priv)}
        delivered = False
        try:
            delivered = 200 <= int(self._transport(peer_id, header)) < 300
        except Exception:
            delivered = False
        queued = False if delivered else self._queue_for_later(peer_id, header)
        # persist OUR copy (attachment bytes to blob, not in the history line)
        record = {**{k: v for k, v in inner.items() if k != "attachment"},
                  "dir": "out", "from": self._me, "to": peer_id, "delivered": delivered}
        # `via` is absent when neither path took the message — the caller sees the
        # same undelivered record it saw before store-and-forward existed.
        if delivered:
            record["via"] = "direct"
        elif queued:
            record["via"] = "mailbox"
        if attachment is not None:
            self._store.save_attachment(msg_id, att_bytes or b"")
            record["attachment"] = {k: v for k, v in inner["attachment"].items() if k != "bytes"}
        self._store.append(peer_id, record)
        return record

    def _already_received(self, sender: str, msg_id: str) -> dict[str, Any] | None:
        """The stored inbound record for `msg_id`, if this peer already sent it.

        Only `dir == "in"` records count: a sender who reuses one of *our*
        outbound ids must not be able to make its own message disappear.
        """

        if not msg_id:
            return None
        for stored in self._store.history(sender):
            if stored.get("msg_id") == msg_id and stored.get("dir") == "in":
                return stored
        return None

    # ---- receive ----
    def receive(self, header: dict[str, Any]) -> dict[str, Any]:
        sender = str(header.get("from", ""))
        if not sender:
            raise MessengerError("missing sender")
        their_pub = self._resolve_pubkey(sender)
        plain = peer_box.open_sealed(self._priv, their_pub, str(header["nonce"]), str(header["ciphertext"]))
        inner = json.loads(plain.decode("utf-8"))
        # Delivery is at-least-once in both directions: a direct send whose
        # response was lost after we processed it comes back through the
        # mailbox, and a crash between here and the mailbox client's seen-cache
        # write redelivers too. Re-appending would double the history line and
        # the SSE record, so the second arrival is a no-op that reports itself.
        seen = self._already_received(sender, str(inner.get("msg_id", "")))
        if seen is not None:
            return {**seen, "duplicate": True}
        record = {**{k: v for k, v in inner.items() if k != "attachment"},
                  "dir": "in", "from": sender, "to": self._me, "delivered": True}
        att = inner.get("attachment")
        if att is not None:
            self._store.save_attachment(inner["msg_id"], base64.b64decode(att["bytes"]))
            record["attachment"] = {k: v for k, v in att.items() if k != "bytes"}
        self._store.append(sender, record)
        return record

    def history(self, peer_id: str) -> list[dict[str, Any]]:
        return self._store.history(peer_id)
