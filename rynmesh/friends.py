"""Friend Mesh invitations, durable relationships, and request authentication.

The module deliberately keeps friendship separate from identity trust.  Invite
links are signed bearer capabilities, while durable relationship credentials
are stored in a restricted sidecar file and are never returned by list APIs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

from .crypto import SignedPayload, canonical_json, sign_payload, verify_signed_payload

INVITE_VERSION = "rynmesh.friend-invite.v1"
ACCEPT_VERSION = "rynmesh.friend-accept.v1"
REVOCATION_VERSION = "rynmesh.friend-revocation.v1"
AUTH_SCHEME = "Rynmesh-Friend"
ALLOWED_PERMISSIONS = frozenset({"private-ai.use", "peer.messaging", "peer.discovery"})
DEFAULT_INVITE_TTL_SECONDS = 15 * 60
MAX_INVITE_TTL_SECONDS = 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
AUTH_CLOCK_SKEW_SECONDS = 60
MAX_ENDPOINTS = 8
MAX_ENDPOINT_LENGTH = 2048
_BLOCKED_HOSTS = frozenset({"metadata", "metadata.google.internal", "169.254.169.254"})


class FriendError(ValueError):
    """A fail-closed Friend Mesh validation or authorization error."""


def _now_utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _now_utc(value).isoformat()


def _parse_time(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise FriendError(f"invite_{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise FriendError(f"invite_{field}_invalid")
    return parsed.astimezone(UTC)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise FriendError("invite_encoding_invalid") from exc


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(payload))).hexdigest()


def _secret_hash(secret: str, *, salt: bytes | None = None) -> str:
    active_salt = salt or os.urandom(16)
    derived = hashlib.scrypt(
        secret.encode("utf-8"), salt=active_salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt-v1${_b64url(active_salt)}${_b64url(derived)}"


def _secret_matches(secret: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, expected_text = encoded.split("$", 2)
        if algorithm != "scrypt-v1":
            return False
        actual = _secret_hash(secret, salt=_unb64url(salt_text)).split("$", 2)[2]
    except (FriendError, ValueError):
        return False
    return hmac.compare_digest(actual, expected_text)


def validate_endpoint(endpoint: str, *, allow_private: bool = False) -> str:
    """Validate a reviewed Friend Mesh endpoint without resolving or contacting it."""

    import ipaddress

    cleaned = str(endpoint or "").strip().rstrip("/")
    if not cleaned or len(cleaned) > MAX_ENDPOINT_LENGTH:
        raise FriendError("invite_endpoint_invalid")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FriendError("invite_endpoint_invalid")
    if parsed.username or parsed.password or parsed.fragment:
        raise FriendError("invite_endpoint_invalid")
    hostname = parsed.hostname.strip("[]").lower()
    if hostname in _BLOCKED_HOSTS or hostname == "localhost":
        raise FriendError("invite_endpoint_blocked")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return cleaned
    if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
        raise FriendError("invite_endpoint_blocked")
    if address.is_private and not allow_private:
        raise FriendError("invite_private_endpoint_requires_review")
    return cleaned


def encode_invite(signed: SignedPayload) -> str:
    return "rynmesh://join/" + _b64url(canonical_json(signed.to_dict()))


def decode_invite(link: str) -> SignedPayload:
    prefix = "rynmesh://join/"
    value = str(link or "").strip()
    if not value.startswith(prefix) or len(value) > 16_384:
        raise FriendError("invite_link_invalid")
    try:
        decoded = json.loads(_unb64url(value[len(prefix) :]).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise TypeError
        return SignedPayload.from_dict(decoded)
    except (FriendError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FriendError("invite_link_invalid") from exc


def verify_invite(
    link: str,
    *,
    now: datetime | None = None,
    allow_private_endpoints: bool = False,
) -> dict[str, Any]:
    """Verify an invite entirely offline and return its reviewed signed fields."""

    signed = decode_invite(link)
    try:
        verify_signed_payload(signed)
    except ValueError as exc:
        raise FriendError("invite_signature_invalid") from exc
    payload = deepcopy(signed.payload)
    if payload.get("version") != INVITE_VERSION:
        raise FriendError("invite_version_unsupported")
    if payload.get("inviter_peer_id") != signed.public_key:
        raise FriendError("invite_peer_key_mismatch")
    for field in ("invite_id", "node_name", "network_id", "one_time_secret"):
        if not str(payload.get(field, "")).strip():
            raise FriendError(f"invite_{field}_required")
    if len(_unb64url(str(payload["one_time_secret"]))) < 32:
        raise FriendError("invite_secret_too_short")
    permissions = payload.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        raise FriendError("invite_permissions_required")
    normalized_permissions = [str(item) for item in permissions]
    if len(set(normalized_permissions)) != len(normalized_permissions):
        raise FriendError("invite_permissions_invalid")
    if not set(normalized_permissions).issubset(ALLOWED_PERMISSIONS):
        raise FriendError("invite_permission_not_allowed")
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list) or not 0 < len(endpoints) <= MAX_ENDPOINTS:
        raise FriendError("invite_endpoints_required")
    payload["endpoints"] = [
        validate_endpoint(str(endpoint), allow_private=allow_private_endpoints)
        for endpoint in endpoints
    ]
    issued_at = _parse_time(payload.get("issued_at"), field="issued_at")
    expires_at = _parse_time(payload.get("expires_at"), field="expires_at")
    active_now = _now_utc(now)
    if issued_at > active_now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise FriendError("invite_not_yet_valid")
    if expires_at <= active_now:
        raise FriendError("invite_expired")
    if expires_at <= issued_at or expires_at - issued_at > timedelta(seconds=MAX_INVITE_TTL_SECONDS):
        raise FriendError("invite_expiry_policy_invalid")
    payload["verified_fingerprint"] = signed.public_key
    payload["signed_payload_hash"] = _payload_hash(signed.payload)
    return payload


def verify_acceptance_request(
    body: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Verify the acceptor identity and fresh signed binding before consuming an invite."""

    from .registry import RegistryError, verify_peer_record

    try:
        signed_record = SignedPayload.from_dict(dict(body["acceptor_peer_record"]))
        peer_record = verify_peer_record(signed_record)
        proof = SignedPayload.from_dict(dict(body["proof"]))
        verify_signed_payload(proof)
    except (KeyError, TypeError, ValueError, RegistryError) as exc:
        raise FriendError("friend_acceptance_invalid") from exc
    payload = proof.payload
    peer_id = peer_record.peer_id
    x25519_pub = str(body.get("acceptor_x25519_pub", ""))
    permissions = body.get("permissions")
    invite_id = str(body.get("invite_id", ""))
    if (
        proof.public_key != peer_id
        or payload.get("version") != ACCEPT_VERSION
        or payload.get("acceptor_peer_id") != peer_id
        or payload.get("invite_id") != invite_id
        or payload.get("acceptor_x25519_pub") != x25519_pub
        or payload.get("network_id") != peer_record.network_id
        or payload.get("permissions") != permissions
    ):
        raise FriendError("friend_acceptance_binding_invalid")
    if not isinstance(permissions, list) or not set(permissions).issubset(ALLOWED_PERMISSIONS):
        raise FriendError("friend_acceptance_permission_invalid")
    try:
        if len(base64.b64decode(x25519_pub, validate=True)) != 32:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise FriendError("friend_acceptance_key_invalid") from exc
    nonce = str(payload.get("nonce", ""))
    if len(nonce) < 16 or len(nonce) > 256:
        raise FriendError("friend_acceptance_nonce_invalid")
    signed_at = _parse_time(payload.get("signed_at"), field="acceptance_time")
    if abs((_now_utc(now) - signed_at).total_seconds()) > MAX_CLOCK_SKEW_SECONDS:
        raise FriendError("friend_acceptance_expired")
    endpoints = [validate_endpoint(item, allow_private=True) for item in peer_record.endpoints]
    if not endpoints:
        raise FriendError("friend_acceptance_endpoints_required")
    return {
        "peer_id": peer_id,
        "display_name": peer_record.node_name,
        "network_id": peer_record.network_id,
        "endpoints": endpoints,
        "permissions": list(permissions),
        "x25519_pub": x25519_pub,
        "signed_peer_record": signed_record.to_dict(),
    }


class FriendshipStore:
    """Atomic local invite/friend state with a separate restricted secret store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.secrets_path = self.path.with_name(f"{self.path.stem}.secrets.json")
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + 5.0
            descriptor: int | None = None
            while descriptor is None:
                try:
                    descriptor = os.open(
                        self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                except FileExistsError:
                    try:
                        stale = time.time() - self.lock_path.stat().st_mtime > 30
                    except OSError:
                        stale = False
                    if stale:
                        try:
                            self.lock_path.unlink()
                        except OSError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise FriendError("friend_store_busy") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.close(descriptor)
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _read(self) -> dict[str, Any]:
        default = {"version": 1, "invites": {}, "friends": {}, "revocations": {}, "nonces": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if not isinstance(loaded, dict):
            return default
        return {
            "version": 1,
            "invites": dict(loaded.get("invites") or {}),
            "friends": dict(loaded.get("friends") or {}),
            "revocations": dict(loaded.get("revocations") or {}),
            "nonces": dict(loaded.get("nonces") or {}),
        }

    def _read_secrets(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "relationships": {}}
        return {"version": 1, "relationships": dict(loaded.get("relationships") or {})}

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{secrets.token_hex(8)}.tmp")
        temporary.write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)

    def _write(self, data: dict[str, Any], secret_data: dict[str, Any] | None = None) -> None:
        self._atomic_write(self.path, data)
        if secret_data is not None:
            self._atomic_write(self.secrets_path, secret_data)

    def create_invite(
        self,
        *,
        private_key_bytes: bytes,
        node_name: str,
        network_id: str,
        endpoints: list[str],
        permissions: list[str] | None = None,
        ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS,
        allow_private_endpoints: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        active_now = _now_utc(now)
        if not 1 <= int(ttl_seconds) <= MAX_INVITE_TTL_SECONDS:
            raise FriendError("invite_ttl_invalid")
        requested = list(permissions or ["private-ai.use"])
        if not requested or not set(requested).issubset(ALLOWED_PERMISSIONS):
            raise FriendError("invite_permission_not_allowed")
        reviewed_endpoints = [
            validate_endpoint(item, allow_private=allow_private_endpoints) for item in endpoints
        ]
        if not 0 < len(reviewed_endpoints) <= MAX_ENDPOINTS:
            raise FriendError("invite_endpoints_required")
        one_time_secret = _b64url(os.urandom(32))
        invite_id = "invite_" + secrets.token_hex(16)
        from .crypto import public_key_from_private

        payload = {
            "version": INVITE_VERSION,
            "invite_id": invite_id,
            "inviter_peer_id": public_key_from_private(private_key_bytes),
            "node_name": str(node_name).strip()[:256],
            "network_id": str(network_id).strip()[:256],
            "endpoints": reviewed_endpoints,
            "permissions": requested,
            "issued_at": _iso(active_now),
            "expires_at": _iso(active_now + timedelta(seconds=int(ttl_seconds))),
            "one_time_secret": one_time_secret,
        }
        if not payload["node_name"] or not payload["network_id"]:
            raise FriendError("invite_identity_required")
        signed = sign_payload(payload, private_key_bytes=private_key_bytes)
        link = encode_invite(signed)
        record = {
            "version": INVITE_VERSION,
            "invite_id": invite_id,
            "secret_hash": _secret_hash(one_time_secret),
            "issued_at": payload["issued_at"],
            "expires_at": payload["expires_at"],
            "network_id": payload["network_id"],
            "used_at": None,
            "cancelled_at": None,
            "accepted_peer_id": None,
            "signed_payload_hash": _payload_hash(payload),
            "permissions": requested,
            "endpoints": reviewed_endpoints,
        }
        with self._locked():
            data = self._read()
            data["invites"][invite_id] = record
            self._write(data)
        return {"invite": self._public_invite(record), "link": link}

    @staticmethod
    def _public_invite(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in record.items() if key != "secret_hash"}

    @staticmethod
    def _public_friend(record: Mapping[str, Any]) -> dict[str, Any]:
        return deepcopy(dict(record))

    def list_invites(self) -> list[dict[str, Any]]:
        with self._locked():
            records = self._read()["invites"].values()
            return [self._public_invite(record) for record in records]

    def list_friends(self) -> list[dict[str, Any]]:
        with self._locked():
            records = self._read()["friends"].values()
            return [self._public_friend(record) for record in records]

    def cancel_invite(self, invite_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        with self._locked():
            data = self._read()
            record = data["invites"].get(invite_id)
            if not isinstance(record, dict) or record.get("used_at"):
                raise FriendError("invite_not_found")
            if not record.get("cancelled_at"):
                record["cancelled_at"] = _iso(_now_utc(now))
                self._write(data)
            return self._public_invite(record)

    def consume_invite(
        self,
        *,
        invite_id: str,
        one_time_secret: str,
        acceptor_peer_id: str,
        display_name: str,
        network_id: str,
        endpoints: list[str],
        permissions: list[str],
        now: datetime | None = None,
        relationship_secret: str | None = None,
    ) -> dict[str, Any]:
        """Atomically consume one invite and rotate to a distinct credential."""

        active_now = _now_utc(now)
        with self._locked():
            data = self._read()
            secret_data = self._read_secrets()
            record = data["invites"].get(invite_id)
            generic = FriendError("invite_not_found")
            if not isinstance(record, dict):
                raise generic
            if record.get("used_at") or record.get("cancelled_at"):
                raise generic
            if _parse_time(record.get("expires_at"), field="expires_at") <= active_now:
                raise generic
            if not _secret_matches(one_time_secret, str(record.get("secret_hash", ""))):
                raise generic
            approved = list(record.get("permissions") or [])
            if list(permissions) != approved or str(network_id) != record.get("network_id"):
                raise generic
            peer_id = str(acceptor_peer_id or "").strip()
            if not peer_id:
                raise generic
            reviewed_endpoints = [validate_endpoint(item, allow_private=True) for item in endpoints]
            rotated_secret = relationship_secret or _b64url(os.urandom(32))
            if rotated_secret == one_time_secret or len(_unb64url(rotated_secret)) < 32:
                raise FriendError("relationship_secret_invalid")
            timestamp = _iso(active_now)
            friend = {
                "peer_id": peer_id,
                "display_name": str(display_name or peer_id).strip()[:256],
                "network_id": str(network_id).strip()[:256],
                "reviewed_endpoints": reviewed_endpoints,
                "granted_permissions": approved,
                "received_permissions": [],
                "credential_ref": f"friend:{peer_id}",
                "state": "active",
                "created_at": timestamp,
                "accepted_at": timestamp,
                "last_contact_at": timestamp,
                "revoked_at": None,
                "last_delivery_error": None,
                "source_invite_id": invite_id,
            }
            record["used_at"] = timestamp
            record["accepted_peer_id"] = peer_id
            data["friends"][peer_id] = friend
            secret_data["relationships"][peer_id] = rotated_secret
            self._write(data, secret_data)
            return {"friend": self._public_friend(friend), "relationship_secret": rotated_secret}

    def register_received_relationship(
        self,
        *,
        peer_id: str,
        relationship_secret: str,
        display_name: str,
        network_id: str,
        endpoints: list[str],
        received_permissions: list[str],
        source_invite_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if len(_unb64url(relationship_secret)) < 32:
            raise FriendError("relationship_secret_invalid")
        if not set(received_permissions).issubset(ALLOWED_PERMISSIONS):
            raise FriendError("friend_permission_not_allowed")
        timestamp = _iso(_now_utc(now))
        record = {
            "peer_id": peer_id,
            "display_name": str(display_name or peer_id).strip()[:256],
            "network_id": str(network_id).strip()[:256],
            "reviewed_endpoints": [validate_endpoint(item, allow_private=True) for item in endpoints],
            "granted_permissions": [],
            "received_permissions": list(received_permissions),
            "credential_ref": f"friend:{peer_id}",
            "state": "active",
            "created_at": timestamp,
            "accepted_at": timestamp,
            "last_contact_at": timestamp,
            "revoked_at": None,
            "last_delivery_error": None,
            "source_invite_id": source_invite_id,
        }
        with self._locked():
            data = self._read()
            secret_data = self._read_secrets()
            data["friends"][peer_id] = record
            secret_data["relationships"][peer_id] = relationship_secret
            self._write(data, secret_data)
        return self._public_friend(record)

    def is_authorized(self, peer_id: str, permission: str) -> bool:
        with self._locked():
            record = self._read()["friends"].get(peer_id)
            return bool(
                isinstance(record, dict)
                and record.get("state") == "active"
                and permission in record.get("granted_permissions", [])
            )

    @staticmethod
    def _auth_message(
        *, peer_id: str, method: str, path: str, timestamp: str, nonce: str, body: bytes
    ) -> tuple[bytes, str]:
        digest = hashlib.sha256(body).hexdigest()
        message = "\n".join(
            ["rynmesh.friend-auth.v1", peer_id, method.upper(), path, timestamp, nonce, digest]
        ).encode("utf-8")
        return message, digest

    def make_auth_headers(
        self,
        peer_id: str,
        *,
        method: str,
        path: str,
        body: bytes = b"",
        now: datetime | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        timestamp = str(int(_now_utc(now).timestamp()))
        active_nonce = nonce or _b64url(os.urandom(18))
        with self._locked():
            record = self._read()["friends"].get(peer_id)
            secret = self._read_secrets()["relationships"].get(peer_id)
        if not isinstance(record, dict) or record.get("state") != "active" or not secret:
            raise FriendError("friend_not_authorized")
        message, digest = self._auth_message(
            peer_id=peer_id,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=active_nonce,
            body=body,
        )
        mac = _b64url(hmac.new(_unb64url(secret), message, hashlib.sha256).digest())
        return {
            "Authorization": f"{AUTH_SCHEME} v1={mac}",
            "X-Rynmesh-Friend-Peer": peer_id,
            "X-Rynmesh-Friend-Timestamp": timestamp,
            "X-Rynmesh-Friend-Nonce": active_nonce,
            "X-Rynmesh-Body-SHA256": digest,
        }

    def verify_auth_headers(
        self,
        headers: Mapping[str, str],
        *,
        method: str,
        path: str,
        body: bytes = b"",
        application_peer_id: str,
        required_permission: str,
        now: datetime | None = None,
    ) -> str:
        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        peer_id = lowered.get("x-rynmesh-friend-peer", "")
        if peer_id != application_peer_id:
            raise FriendError("friend_sender_mismatch")
        authorization = lowered.get("authorization", "")
        prefix = f"{AUTH_SCHEME} v1="
        if not authorization.startswith(prefix):
            raise FriendError("friend_auth_invalid")
        timestamp = lowered.get("x-rynmesh-friend-timestamp", "")
        nonce = lowered.get("x-rynmesh-friend-nonce", "")
        if not timestamp or not nonce or len(nonce) > 256:
            raise FriendError("friend_auth_invalid")
        try:
            request_time = datetime.fromtimestamp(int(timestamp), UTC)
        except (ValueError, OverflowError):
            raise FriendError("friend_auth_invalid") from None
        if abs((_now_utc(now) - request_time).total_seconds()) > AUTH_CLOCK_SKEW_SECONDS:
            raise FriendError("friend_auth_expired")
        message, body_digest = self._auth_message(
            peer_id=peer_id,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        if not hmac.compare_digest(lowered.get("x-rynmesh-body-sha256", ""), body_digest):
            raise FriendError("friend_body_mismatch")
        with self._locked():
            data = self._read()
            secret_data = self._read_secrets()
            record = data["friends"].get(peer_id)
            secret = secret_data["relationships"].get(peer_id)
            if (
                not isinstance(record, dict)
                or record.get("state") != "active"
                or required_permission not in record.get("granted_permissions", [])
                or not secret
            ):
                raise FriendError("friend_not_authorized")
            expected = _b64url(hmac.new(_unb64url(secret), message, hashlib.sha256).digest())
            if not hmac.compare_digest(expected, authorization[len(prefix) :]):
                raise FriendError("friend_auth_invalid")
            nonce_key = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
            peer_nonces = dict(data["nonces"].get(peer_id) or {})
            cutoff = int(_now_utc(now).timestamp()) - AUTH_CLOCK_SKEW_SECONDS
            peer_nonces = {key: value for key, value in peer_nonces.items() if int(value) >= cutoff}
            if nonce_key in peer_nonces:
                raise FriendError("friend_auth_replayed")
            peer_nonces[nonce_key] = int(timestamp)
            data["nonces"][peer_id] = peer_nonces
            record["last_contact_at"] = _iso(_now_utc(now))
            self._write(data)
        return peer_id

    def revoke(
        self,
        peer_id: str,
        *,
        private_key_bytes: bytes,
        local_peer_id: str,
        reason_code: str = "owner_revoked",
        now: datetime | None = None,
    ) -> SignedPayload:
        timestamp = _iso(_now_utc(now))
        with self._locked():
            data = self._read()
            secret_data = self._read_secrets()
            record = data["friends"].get(peer_id)
            if not isinstance(record, dict):
                raise FriendError("friend_not_found")
            if record.get("state") == "revoked" and record.get("revocation"):
                return SignedPayload.from_dict(record["revocation"])
            payload = {
                "version": REVOCATION_VERSION,
                "revocation_id": "revoke_" + secrets.token_hex(16),
                "peer_ids": sorted([local_peer_id, peer_id]),
                "relationship_ref": record.get("source_invite_id", ""),
                "revoked_at": timestamp,
                "reason_code": str(reason_code or "owner_revoked")[:128],
            }
            signed = sign_payload(payload, private_key_bytes=private_key_bytes)
            if signed.public_key != local_peer_id:
                raise FriendError("revocation_signer_mismatch")
            record["state"] = "revoked"
            record["revoked_at"] = timestamp
            record["revocation"] = signed.to_dict()
            data["revocations"][payload["revocation_id"]] = signed.to_dict()
            data["nonces"].pop(peer_id, None)
            secret_data["relationships"].pop(peer_id, None)
            self._write(data, secret_data)
            return signed

    def apply_revocation(self, signed: SignedPayload, *, local_peer_id: str) -> dict[str, Any]:
        try:
            verify_signed_payload(signed)
        except ValueError as exc:
            raise FriendError("revocation_signature_invalid") from exc
        payload = signed.payload
        if payload.get("version") != REVOCATION_VERSION:
            raise FriendError("revocation_version_unsupported")
        peer_ids = payload.get("peer_ids")
        if not isinstance(peer_ids, list) or len(peer_ids) != 2 or local_peer_id not in peer_ids:
            raise FriendError("revocation_relationship_mismatch")
        if signed.public_key not in peer_ids:
            raise FriendError("revocation_signer_mismatch")
        remote_peer_id = next(peer_id for peer_id in peer_ids if peer_id != local_peer_id)
        with self._locked():
            data = self._read()
            secret_data = self._read_secrets()
            record = data["friends"].get(remote_peer_id)
            if not isinstance(record, dict):
                raise FriendError("revocation_relationship_mismatch")
            if record.get("source_invite_id", "") != payload.get("relationship_ref", ""):
                raise FriendError("revocation_relationship_mismatch")
            revocation_id = str(payload.get("revocation_id", ""))
            if not revocation_id:
                raise FriendError("revocation_id_required")
            existing = data["revocations"].get(revocation_id)
            if existing and existing != signed.to_dict():
                raise FriendError("revocation_id_conflict")
            record["state"] = "revoked"
            record["revoked_at"] = str(payload.get("revoked_at", ""))
            record["revocation"] = signed.to_dict()
            data["revocations"][revocation_id] = signed.to_dict()
            data["nonces"].pop(remote_peer_id, None)
            secret_data["relationships"].pop(remote_peer_id, None)
            self._write(data, secret_data)
            return self._public_friend(record)
