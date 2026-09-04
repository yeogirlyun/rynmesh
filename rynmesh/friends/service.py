from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
MAX_SHARED_CONTENT_BYTES = 5 * 1024 * 1024
MAX_SHARED_RESPONSE_BYTES = ((MAX_SHARED_CONTENT_BYTES + 2) // 3) * 4 + 256 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CARD_ID = re.compile(r"^[0-9a-f]{32}$")


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
        post_json: Callable[..., dict[str, Any]],
        resolve_content: Callable[[str], dict[str, Any] | None] | None = None,
        import_content: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
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
        self.resolve_content = resolve_content
        self.import_content = import_content
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

    def _request_wire(
        self,
        record: dict[str, Any],
        secret: bytes,
        path: str,
        wire: dict[str, Any],
        *,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
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
        kwargs = {"max_response_bytes": max_response_bytes} if max_response_bytes else {}
        return self.post_json(str(record["endpoint"]), path, wire, headers, **kwargs)

    def _send_wire(self, record: dict[str, Any], secret: bytes, path: str, wire: dict[str, Any]) -> bool:
        try:
            return bool(self._request_wire(record, secret, path, wire).get("ok"))
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

    @staticmethod
    def _clean_card(card: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {
            "version": "ryn.shared-content-card.v1",
            "library_id": str(card.get("library_id", ""))[:320],
            "content_id": str(card.get("content_id", ""))[:256],
            "title": str(card.get("title", ""))[:300],
            "summary": str(card.get("summary", ""))[:500],
            "kind": str(card.get("kind", ""))[:64],
            "source": str(card.get("source", ""))[:300],
            "source_url": str(card.get("source_url", ""))[:2048],
            "publisher_peer_id": str(card.get("publisher_peer_id", ""))[:256],
            "manifest_ref": str(card.get("manifest_ref", ""))[:256],
            "filename": str(card.get("filename", ""))[:180],
            "mime": str(card.get("mime", "application/octet-stream"))[:127],
            "size_bytes": max(0, min(int(card.get("size_bytes", 0) or 0), MAX_SHARED_CONTENT_BYTES)),
            "sha256": str(card.get("sha256", "")).lower(),
            "fetch_available": bool(card.get("fetch_available")),
        }
        if clean["fetch_available"] and (
            not clean["library_id"]
            or not clean["filename"]
            or not _SHA256.fullmatch(clean["sha256"])
            or not 0 < clean["size_bytes"] <= MAX_SHARED_CONTENT_BYTES
        ):
            raise FriendError("friend_card_reference_invalid")
        return clean

    def send_content_card(self, peer_id: str, card: dict[str, Any]) -> dict[str, Any]:
        record, secret = self._relationship(peer_id)
        clean = self._clean_card(card)
        if self.resolve_content and clean["library_id"]:
            resource = self.resolve_content(str(clean["library_id"]))
            if resource:
                data = bytes(resource.get("data", b""))
                if 0 < len(data) <= MAX_SHARED_CONTENT_BYTES:
                    clean.update(
                        {
                            "content_id": str(resource.get("content_id", clean["content_id"]))[:256],
                            "publisher_peer_id": str(
                                resource.get("publisher_peer_id", self.peer_id)
                            )[:256],
                            "manifest_ref": str(resource.get("manifest_ref", ""))[:256],
                            "filename": str(resource.get("filename", "shared-content"))[:180],
                            "mime": str(
                                resource.get("mime", "application/octet-stream")
                            )[:127],
                            "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "fetch_available": True,
                        }
                    )
        clean = self._clean_card(clean)
        inner = {
            "version": "ryn.shared-content-card.v1",
            "card_id": uuid.uuid4().hex,
            "created_at": self.clock().isoformat(),
            "card": clean,
        }
        self.store.put_card(
            {
                **inner,
                "from": self.peer_id,
                "to": peer_id,
                "dir": "out",
                "relationship_id": record["relationship_id"],
                "delivery_state": "sending",
            }
        )
        nonce, ciphertext = peer_box.seal(self.messaging_private, str(record["messaging_pub"]), canonical_json(inner))
        wire = {"v": 1, "relationship_id": record["relationship_id"], "from": self.peer_id, "to": peer_id, "nonce": nonce, "ciphertext": ciphertext}
        delivered = self._send_wire(record, secret, "/api/peer/friends/content-card", wire)
        self.store.patch_card(
            inner["card_id"],
            {"delivery_state": "delivered" if delivered else "queued"},
        )
        return {**inner, "from": self.peer_id, "to": peer_id, "delivered": delivered}

    def receive_content_card(self, wire: dict[str, Any]) -> dict[str, Any]:
        relationship = self.store.relationship(str(wire.get("relationship_id", "")))
        if not relationship or relationship.get("peer_id") != wire.get("from") or wire.get("to") != self.peer_id:
            raise FriendError("friend_auth_failed")
        plain = peer_box.open_sealed(self.messaging_private, str(relationship["messaging_pub"]), str(wire["nonce"]), str(wire["ciphertext"]))
        inner = json.loads(plain.decode("utf-8"))
        if (
            inner.get("version") != "ryn.shared-content-card.v1"
            or not _CARD_ID.fullmatch(str(inner.get("card_id", "")))
            or not isinstance(inner.get("card"), dict)
        ):
            raise FriendError("friend_card_invalid")
        clean = self._clean_card(dict(inner["card"]))
        row = {
            **inner,
            "card": clean,
            "from": wire["from"],
            "to": self.peer_id,
            "dir": "in",
            "relationship_id": relationship["relationship_id"],
            "fetch_state": "available" if clean["fetch_available"] else "metadata_only",
        }
        self.store.put_card(row)
        return row

    def content_cards(self) -> list[dict[str, Any]]:
        rows = self.store.list_cards()
        path = self.home / "friends" / "content-cards.jsonl"
        if path.exists():
            known = {str(row.get("card_id", "")) for row in rows}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    legacy = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(legacy, dict) and str(legacy.get("card_id", "")) not in known:
                    rows.append(legacy)
        return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)[:500]

    def serve_content_card(
        self, request: dict[str, Any], relationship: dict[str, Any]
    ) -> dict[str, Any]:
        card_id = str(request.get("card_id", ""))
        row = self.store.card(card_id)
        if (
            request.get("v") != 1
            or not row
            or row.get("dir") != "out"
            or row.get("from") != self.peer_id
            or row.get("to") != relationship.get("peer_id")
            or row.get("relationship_id") != relationship.get("relationship_id")
            or request.get("from") != relationship.get("peer_id")
            or request.get("to") != self.peer_id
            or request.get("relationship_id") != relationship.get("relationship_id")
        ):
            raise FriendError("friend_card_not_found")
        card = self._clean_card(dict(row.get("card") or {}))
        if not card["fetch_available"] or not self.resolve_content:
            raise FriendError("friend_card_content_unavailable")
        resource = self.resolve_content(str(card["library_id"]))
        data = bytes((resource or {}).get("data", b""))
        if (
            len(data) != card["size_bytes"]
            or hashlib.sha256(data).hexdigest() != card["sha256"]
        ):
            raise FriendError("friend_card_content_changed")
        response = {
            "version": "ryn.shared-content-fetch.v1",
            "card_id": card_id,
            "filename": card["filename"],
            "mime": card["mime"],
            "size_bytes": len(data),
            "sha256": card["sha256"],
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
        nonce, ciphertext = peer_box.seal(
            self.messaging_private,
            str(relationship["messaging_pub"]),
            canonical_json(response),
        )
        return {
            "ok": True,
            "v": 1,
            "relationship_id": relationship["relationship_id"],
            "from": self.peer_id,
            "to": relationship["peer_id"],
            "nonce": nonce,
            "ciphertext": ciphertext,
        }

    def fetch_content_card(self, card_id: str) -> dict[str, Any]:
        row = self.store.card(card_id)
        if not row or row.get("dir") != "in":
            raise FriendError("friend_card_not_found")
        if row.get("fetch_state") == "fetched" and row.get("fetched_library_id"):
            return {
                "ok": True,
                "card_id": card_id,
                "library_id": row["fetched_library_id"],
                "sha256_verified": True,
                "already_fetched": True,
            }
        peer_id = str(row.get("from", ""))
        relationship, secret = self._relationship(peer_id)
        if row.get("relationship_id") != relationship.get("relationship_id"):
            raise FriendError("friend_card_not_found")
        card = self._clean_card(dict(row.get("card") or {}))
        if not card["fetch_available"] or not self.import_content:
            raise FriendError("friend_card_content_unavailable")
        request = {
            "v": 1,
            "relationship_id": relationship["relationship_id"],
            "card_id": card_id,
            "from": self.peer_id,
            "to": peer_id,
        }
        try:
            wire = self._request_wire(
                relationship,
                secret,
                "/api/peer/friends/content-card/fetch",
                request,
                max_response_bytes=MAX_SHARED_RESPONSE_BYTES,
            )
            if (
                wire.get("v") != 1
                or wire.get("relationship_id") != relationship["relationship_id"]
                or wire.get("from") != peer_id
                or wire.get("to") != self.peer_id
            ):
                raise FriendError("friend_card_fetch_invalid")
            plain = peer_box.open_sealed(
                self.messaging_private,
                str(relationship["messaging_pub"]),
                str(wire["nonce"]),
                str(wire["ciphertext"]),
            )
            content = json.loads(plain.decode("utf-8"))
            data = base64.b64decode(str(content.get("data_base64", "")), validate=True)
        except FriendError:
            raise
        except Exception as exc:
            raise FriendError("friend_card_fetch_failed") from exc
        if (
            content.get("version") != "ryn.shared-content-fetch.v1"
            or content.get("card_id") != card_id
            or int(content.get("size_bytes", -1)) != len(data)
            or len(data) != card["size_bytes"]
            or str(content.get("sha256", "")) != card["sha256"]
            or hashlib.sha256(data).hexdigest() != card["sha256"]
        ):
            raise FriendError("friend_card_hash_mismatch")
        imported = self.import_content(
            {
                "filename": card["filename"],
                "mime": card["mime"],
                "data": data,
                "card": card,
            }
        )
        library_id = str(imported.get("library_id", ""))
        if not library_id:
            raise FriendError("friend_card_import_failed")
        self.store.patch_card(
            card_id,
            {
                "fetch_state": "fetched",
                "fetched_library_id": library_id,
                "fetched_at": self.clock().isoformat(),
                "sha256_verified": True,
            },
        )
        return {
            "ok": True,
            "card_id": card_id,
            "library_id": library_id,
            "library_item": imported,
            "sha256_verified": True,
            "already_fetched": False,
        }

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
