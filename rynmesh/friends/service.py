from __future__ import annotations

import base64
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from rynmesh.crypto import SignedPayload, canonical_json, sign_payload, verify_signed_payload
from rynmesh.services import peer_box
from rynmesh.services.messaging_store import MessagingStore

from .crypto import (
    PERMISSIONS,
    FriendCryptoError,
    auth_headers,
    endpoint_category,
    invite_uri,
    parse_invite,
    secret_hash,
    validate_endpoint,
    verify_auth,
)
from .store import FriendStore

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class FriendError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class FriendService:
    def __init__(
        self,
        *,
        home: Any,
        peer_id: str,
        node_name: str,
        endpoint: str,
        identity_private: bytes,
        messaging_private: Any,
        post_json: Callable[[str, str, dict[str, Any], dict[str, str] | None], dict[str, Any]],
        allow_loopback: bool = False,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.home = home
        self.peer_id = peer_id
        self.node_name = node_name
        self.endpoint = endpoint.rstrip("/")
        self.identity_private = identity_private
        self.messaging_private = messaging_private
        self.post_json = post_json
        self.allow_loopback = allow_loopback
        self.clock = clock
        self.store = FriendStore(home)
        self.messages = MessagingStore(home)

    def create_invite(self, *, ttl_minutes: int = 15) -> dict[str, Any]:
        ttl = max(1, min(int(ttl_minutes), 24 * 60))
        endpoint = validate_endpoint(self.endpoint, allow_loopback=self.allow_loopback)
        created = self.clock()
        invite_id = uuid.uuid4().hex
        secret = os.urandom(32)
        payload = {
            "kind": "ryn.friend-invite.v1",
            "invite_id": invite_id,
            "peer_id": self.peer_id,
            "node_name": self.node_name,
            "endpoint": endpoint,
            "endpoints": [endpoint],
            "network_id": os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"),
            "messaging_pub": peer_box.public_key_b64(self.messaging_private),
            "permissions": list(PERMISSIONS),
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(minutes=ttl)).isoformat(),
            "secret_hash": secret_hash(secret),
        }
        signed = sign_payload(payload, private_key_bytes=self.identity_private)
        self.store.put_invite(
            invite_id,
            {**{k: v for k, v in payload.items() if k != "messaging_pub"}, "status": "active"},
        )
        return {"invite_uri": invite_uri(signed, secret), "invite": self._public_invite(payload)}

    def inspect_invite(self, uri: str) -> dict[str, Any]:
        signed, _ = parse_invite(uri, now=self.clock(), allow_loopback=self.allow_loopback)
        validate_endpoint(str(signed.payload["endpoint"]), allow_loopback=self.allow_loopback)
        return self._public_invite(signed.payload)

    def list_invites(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in row.items() if key != "secret_hash"}
            for row in self.store.list_invites()
        ]

    def cancel_invite(self, invite_id: str) -> dict[str, Any]:
        record = self.store.cancel_invite(invite_id, self.clock().isoformat())
        if not record:
            raise FriendError("invite_not_found")
        return {key: value for key, value in record.items() if key != "secret_hash"}

    @staticmethod
    def _public_invite(payload: dict[str, Any]) -> dict[str, Any]:
        public = {key: payload[key] for key in ("invite_id", "peer_id", "node_name", "endpoint", "permissions", "created_at", "expires_at")}
        public["endpoints"] = list(payload.get("endpoints") or [payload["endpoint"]])
        public["network_id"] = str(payload.get("network_id") or "rynmesh-main")
        public["address_category"] = endpoint_category(str(payload["endpoint"]))
        public["fingerprint"] = str(payload["peer_id"])[:12] + "…" + str(payload["peer_id"])[-8:]
        return public

    def join(self, uri: str) -> dict[str, Any]:
        signed, secret = parse_invite(uri, now=self.clock(), allow_loopback=self.allow_loopback)
        endpoint = validate_endpoint(str(signed.payload["endpoint"]), allow_loopback=self.allow_loopback)
        join_payload = {
            "kind": "ryn.friend-join.v1",
            "invite": signed.to_dict(),
            "invite_secret": base64.urlsafe_b64encode(secret).decode("ascii").rstrip("="),
            "peer_id": self.peer_id,
            "node_name": self.node_name,
            "endpoint": validate_endpoint(self.endpoint, allow_loopback=self.allow_loopback),
            "messaging_pub": peer_box.public_key_b64(self.messaging_private),
            "created_at": self.clock().isoformat(),
            "timestamp": int(self.clock().timestamp()),
            "nonce": uuid.uuid4().hex,
            "permissions": list(PERMISSIONS),
        }
        join_payload["proof"] = sign_payload(join_payload, private_key_bytes=self.identity_private).to_dict()
        try:
            response = self.post_json(endpoint, "/api/peer/friends/accept", join_payload, None)
            inviter_pub = str(response["messaging_pub"])
            relationship_secret = peer_box.open_sealed(
                self.messaging_private,
                inviter_pub,
                str(response["secret_nonce"]),
                str(response["secret_ciphertext"]),
            )
        except Exception as exc:
            raise FriendError("could_not_join_friend") from exc
        record = {
            "relationship_id": str(response["relationship_id"]),
            "peer_id": str(signed.payload["peer_id"]),
            "node_name": str(signed.payload["node_name"]),
            "endpoint": endpoint,
            "messaging_pub": inviter_pub,
            "permissions": list(PERMISSIONS),
            "status": "active",
            "created_at": self.clock().isoformat(),
        }
        self.store.put_relationship(record, relationship_secret)
        return self.public_relationship(record)

    def accept(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            signed_invite = SignedPayload.from_dict(body["invite"])
            verify_signed_payload(signed_invite)
            secret = base64.urlsafe_b64decode(str(body["invite_secret"]) + "=" * (-len(str(body["invite_secret"])) % 4))
            proof = SignedPayload.from_dict(body["proof"])
            unsigned = {key: value for key, value in body.items() if key != "proof"}
            verify_signed_payload(proof)
            if proof.public_key != body.get("peer_id") or proof.payload != unsigned:
                raise FriendError("invalid_join")
            if tuple(body.get("permissions") or ()) != PERMISSIONS:
                raise FriendError("invalid_join")
            if abs(int(self.clock().timestamp()) - int(body.get("timestamp", 0))) > 120:
                raise FriendError("invalid_join")
            payload = signed_invite.payload
            if payload.get("peer_id") != self.peer_id or payload.get("secret_hash") != secret_hash(secret):
                raise FriendError("invalid_join")
            validate_endpoint(str(body.get("endpoint", "")), allow_loopback=self.allow_loopback)
            now_iso = self.clock().isoformat()
            if not self.store.consume_invite(str(payload["invite_id"]), secret_hash(secret), now_iso):
                raise FriendError("invalid_join")
        except Exception as exc:
            raise FriendError("invalid_join") from exc
        relationship_id = uuid.uuid4().hex
        relationship_secret = os.urandom(32)
        record = {
            "relationship_id": relationship_id,
            "peer_id": str(body["peer_id"]),
            "node_name": str(body.get("node_name") or "Friend"),
            "endpoint": str(body["endpoint"]).rstrip("/"),
            "messaging_pub": str(body["messaging_pub"]),
            "permissions": list(PERMISSIONS),
            "status": "active",
            "created_at": now_iso,
        }
        self.store.put_relationship(record, relationship_secret)
        nonce, ciphertext = peer_box.seal(self.messaging_private, record["messaging_pub"], relationship_secret)
        return {
            "relationship_id": relationship_id,
            "messaging_pub": peer_box.public_key_b64(self.messaging_private),
            "secret_nonce": nonce,
            "secret_ciphertext": ciphertext,
        }

    @staticmethod
    def public_relationship(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"messaging_pub", "revocation_wire"}
        }

    def list_friends(self) -> list[dict[str, Any]]:
        return [self.public_relationship(row) for row in self.store.list_relationships()]

    def _relationship(self, peer_id: str) -> tuple[dict[str, Any], bytes]:
        record = self.store.relationship_for_peer(peer_id)
        if not record:
            raise FriendError("active_friend_required")
        secret = self.store.secret(str(record["relationship_id"]))
        if not secret:
            raise FriendError("active_friend_required")
        return record, secret

    def _send_wire(self, record: dict[str, Any], secret: bytes, path: str, wire: dict[str, Any]) -> bool:
        body = canonical_json(wire)
        headers = auth_headers(
            secret,
            method="POST",
            path=path,
            body=body,
            sender=self.peer_id,
            receiver=str(record["peer_id"]),
            relationship_id=str(record["relationship_id"]),
            timestamp=int(time.time()),
            nonce=uuid.uuid4().hex,
        )
        try:
            response = self.post_json(str(record["endpoint"]), path, wire, headers)
            return bool(response.get("ok"))
        except Exception:
            return False

    def send_message(self, peer_id: str, *, text: str = "", attachment: dict[str, Any] | None = None) -> dict[str, Any]:
        record, secret = self._relationship(peer_id)
        if len(text.encode("utf-8")) > 32 * 1024:
            raise FriendError("message_too_large")
        latest = {str(row.get("msg_id")): row for row in self.messages.history(peer_id)}
        if sum(row.get("delivery_state") == "queued" for row in latest.values()) >= 100:
            raise FriendError("friend_queue_full")
        attachment = attachment or None
        raw = attachment.get("bytes") if attachment else None
        if raw is not None and len(raw) > MAX_ATTACHMENT_BYTES:
            raise FriendError("attachment_too_large")
        msg_id = uuid.uuid4().hex
        inner: dict[str, Any] = {"msg_id": msg_id, "kind": "file" if attachment else "text", "text": text, "ts": self.clock().isoformat()}
        if attachment:
            inner["attachment"] = {
                "filename": str(attachment.get("filename") or "file")[:255],
                "mime": str(attachment.get("mime") or "application/octet-stream")[:127],
                "size": len(raw or b""),
                "bytes": base64.b64encode(raw or b"").decode("ascii"),
            }
        nonce, ciphertext = peer_box.seal(self.messaging_private, str(record["messaging_pub"]), canonical_json(inner))
        wire = {"v": 1, "relationship_id": record["relationship_id"], "from": self.peer_id, "to": peer_id, "from_pub": peer_box.public_key_b64(self.messaging_private), "nonce": nonce, "ciphertext": ciphertext}
        delivered = self._send_wire(record, secret, "/api/peer/friends/message", wire)
        local = {key: value for key, value in inner.items() if key != "attachment"}
        local.update({"dir": "out", "from": self.peer_id, "to": peer_id, "delivered": delivered, "delivery_state": "delivered" if delivered else "queued", "wire": wire if not delivered else None})
        if attachment:
            self.messages.save_attachment(msg_id, raw or b"")
            local["attachment"] = {key: value for key, value in inner["attachment"].items() if key != "bytes"}
        self.messages.append(peer_id, local)
        return {key: value for key, value in local.items() if key != "wire"}

    def receive_message(self, wire: dict[str, Any]) -> dict[str, Any]:
        relationship = self.store.relationship(str(wire.get("relationship_id", "")))
        if not relationship or relationship.get("peer_id") != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        plain = peer_box.open_sealed(self.messaging_private, str(relationship["messaging_pub"]), str(wire["nonce"]), str(wire["ciphertext"]))
        inner = json.loads(plain.decode("utf-8"))
        local = {key: value for key, value in inner.items() if key != "attachment"}
        local.update({"dir": "in", "from": wire["from"], "to": self.peer_id, "delivered": True, "delivery_state": "delivered"})
        attachment = inner.get("attachment")
        if attachment:
            data = base64.b64decode(attachment.get("bytes", ""), validate=True)
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise FriendError("attachment_too_large")
            self.messages.save_attachment(str(inner["msg_id"]), data)
            local["attachment"] = {key: value for key, value in attachment.items() if key != "bytes"}
        if not any(item.get("msg_id") == local.get("msg_id") for item in self.messages.history(str(wire["from"]))):
            self.messages.append(str(wire["from"]), local)
        return local

    def verify_request(self, *, path: str, body: bytes, headers: Any) -> dict[str, Any]:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        relationship_id = normalized.get("x-ryn-friend-id", "")
        sender = normalized.get("x-ryn-friend-peer", "")
        relationship = self.store.relationship(relationship_id)
        secret = self.store.secret(relationship_id)
        if not relationship or not secret or relationship.get("peer_id") != sender:
            raise FriendError("friend_auth_failed")
        try:
            timestamp = int(normalized.get("x-ryn-friend-time", "0"))
            nonce = normalized.get("x-ryn-friend-nonce", "")
            verify_auth(secret, method="POST", path=path, body=body, sender=sender, receiver=self.peer_id, relationship_id=relationship_id, timestamp=timestamp, nonce=nonce, signature=normalized.get("x-ryn-friend-mac", ""), now=int(time.time()))
            if not nonce or not self.store.remember_nonce(relationship_id, nonce):
                raise FriendCryptoError("friend_auth_failed")
        except Exception as exc:
            raise FriendError("friend_auth_failed") from exc
        return relationship

    def send_content_card(self, peer_id: str, card: dict[str, Any]) -> dict[str, Any]:
        record, secret = self._relationship(peer_id)
        clean = {key: str(card.get(key, ""))[:2048] for key in ("library_id", "title", "summary", "kind", "source")}
        inner = {"card_id": uuid.uuid4().hex, "created_at": self.clock().isoformat(), "card": clean}
        nonce, ciphertext = peer_box.seal(self.messaging_private, str(record["messaging_pub"]), canonical_json(inner))
        wire = {"v": 1, "relationship_id": record["relationship_id"], "from": self.peer_id, "to": peer_id, "nonce": nonce, "ciphertext": ciphertext}
        delivered = self._send_wire(record, secret, "/api/peer/friends/content-card", wire)
        return {**inner, "from": self.peer_id, "to": peer_id, "delivered": delivered}

    def receive_content_card(self, wire: dict[str, Any]) -> dict[str, Any]:
        relationship = self.store.relationship(str(wire.get("relationship_id", "")))
        if not relationship or relationship.get("peer_id") != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        plain = peer_box.open_sealed(self.messaging_private, str(relationship["messaging_pub"]), str(wire["nonce"]), str(wire["ciphertext"]))
        inner = json.loads(plain.decode("utf-8"))
        root = self.home / "friends" / "content-cards.jsonl"
        root.parent.mkdir(parents=True, exist_ok=True)
        row = {**inner, "from": wire["from"], "to": self.peer_id, "dir": "in"}
        with root.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def content_cards(self) -> list[dict[str, Any]]:
        path = self.home / "friends" / "content-cards.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[-500:]

    def revoke(self, relationship_id: str, *, notify: bool = True) -> dict[str, Any]:
        relationship = self.store.relationship(relationship_id)
        if not relationship:
            raise FriendError("friend_not_found")
        secret = self.store.secret(relationship_id)
        revoked = self.store.revoke(
            relationship_id, self.clock().isoformat(), retain_secret=bool(notify)
        )
        if notify and secret:
            unsigned = {"kind": "ryn.friend-revocation.v1", "relationship_id": relationship_id, "from": self.peer_id, "to": relationship["peer_id"], "timestamp": int(self.clock().timestamp())}
            wire = {**unsigned, "proof": sign_payload(unsigned, private_key_bytes=self.identity_private).to_dict()}
            delivered = self._send_wire(relationship, secret, "/api/peer/friends/revoke", wire)
            self.store.set_revocation_delivery(
                relationship_id,
                state_value="delivered" if delivered else "pending",
                wire=None if delivered else wire,
            )
            revoked = self.store.relationship(relationship_id, active_only=False)
        return self.public_relationship(revoked or relationship)

    def retry_revocation(self, relationship_id: str) -> dict[str, Any]:
        relationship = self.store.relationship(relationship_id, active_only=False)
        if not relationship or relationship.get("status") != "revoked":
            raise FriendError("friend_not_found")
        wire = relationship.get("revocation_wire")
        secret = self.store.pending_revocation_secret(relationship_id)
        if not isinstance(wire, dict) or not secret:
            return {"delivered": relationship.get("revocation_delivery") == "delivered"}
        delivered = self._send_wire(relationship, secret, "/api/peer/friends/revoke", wire)
        self.store.set_revocation_delivery(
            relationship_id,
            state_value="delivered" if delivered else "pending",
            wire=None if delivered else wire,
        )
        return {"delivered": delivered}

    def receive_revoke(self, wire: dict[str, Any]) -> None:
        proof = SignedPayload.from_dict(wire["proof"])
        verify_signed_payload(proof)
        unsigned = {key: value for key, value in wire.items() if key != "proof"}
        if proof.payload != unsigned or proof.public_key != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        self.store.revoke(str(wire["relationship_id"]), self.clock().isoformat())

    def history(self, peer_id: str) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.messages.history(peer_id):
            latest[str(row.get("msg_id", ""))] = row
        return [{key: value for key, value in row.items() if key != "wire"} for row in latest.values()]

    def retry(self, peer_id: str) -> dict[str, int]:
        relationship, secret = self._relationship(peer_id)
        attempted = delivered = 0
        latest = {str(row.get("msg_id")): row for row in self.messages.history(peer_id)}
        for row in latest.values():
            wire = row.get("wire")
            if row.get("delivery_state") != "queued" or not isinstance(wire, dict):
                continue
            attempted += 1
            ok = self._send_wire(relationship, secret, "/api/peer/friends/message", wire)
            if ok:
                delivered += 1
                self.messages.append(
                    peer_id,
                    {**row, "delivered": True, "delivery_state": "delivered", "wire": None},
                )
        return {"attempted": attempted, "delivered": delivered}
