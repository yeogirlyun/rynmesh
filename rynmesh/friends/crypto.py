from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from rynmesh.crypto import SignedPayload, canonical_json, verify_signed_payload

INVITE_KIND = "ryn.friend-invite.v1"
PERMISSIONS = ("friend.message", "friend.attachment.small", "friend.content-card")
AUTH_WINDOW_SECONDS = 120


class FriendCryptoError(ValueError):
    pass


class FriendClockSkewError(FriendCryptoError):
    def __init__(self, server_time: int) -> None:
        super().__init__("friend_clock_skew")
        self.server_time = int(server_time)


def check_permissions(raw_permissions: object) -> tuple[list[str], list[str]]:
    if (
        not isinstance(raw_permissions, (list, tuple))
        or any(not isinstance(p, str) or not 0 < len(p) <= 128 for p in raw_permissions)
        or len(set(raw_permissions)) != len(raw_permissions)
        or not set(PERMISSIONS).issubset(raw_permissions)
    ):
        raise FriendCryptoError("invalid_invite")
    ignored = [p for p in raw_permissions if p not in PERMISSIONS]
    return list(PERMISSIONS), ignored


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def invite_uri(signed: SignedPayload, secret: bytes) -> str:
    envelope = {"signed": signed.to_dict(), "secret": b64url(secret)}
    return "rynmesh://join/" + b64url(canonical_json(envelope))


def parse_invite(
    uri: str, *, now: datetime | None = None, allow_loopback: bool = False,
    allow_expired: bool = False,
) -> tuple[SignedPayload, bytes]:
    if len(str(uri)) > 16384:
        raise FriendCryptoError("invalid_invite")
    parsed = urlparse(str(uri).strip())
    if parsed.scheme != "rynmesh":
        raise FriendCryptoError("invalid_invite")
    if parsed.netloc == "join" and parsed.path.startswith("/"):
        values = [parsed.path[1:]]
    elif parsed.netloc == "friend" and parsed.path == "/invite":
        values = parse_qs(parsed.query).get("data", [])
    else:
        values = []
    if len(values) != 1 or not values[0]:
        raise FriendCryptoError("invalid_invite")
    try:
        envelope = json.loads(unb64url(values[0]).decode("utf-8"))
        signed = SignedPayload.from_dict(envelope["signed"])
        secret = unb64url(str(envelope["secret"]))
        verify_signed_payload(signed)
    except Exception as exc:
        raise FriendCryptoError("invalid_invite") from exc
    payload = signed.payload
    if payload.get("kind") != INVITE_KIND or payload.get("peer_id") != signed.public_key:
        raise FriendCryptoError("invalid_invite")
    _, ignored_permissions = check_permissions(payload.get("permissions"))
    object.__setattr__(signed, "ignored_permissions", ignored_permissions)
    if payload.get('purpose') not in {None, 'friend', 'ai'}:
        raise FriendCryptoError('invalid_invite')
    if payload.get('purpose') == 'ai':
        ai = payload.get('ai')
        if (not isinstance(ai, dict) or not isinstance(ai.get('service'), dict)
                or not isinstance(ai['service'].get('package_id'), str) or not isinstance(ai.get('network_id'), str)):
            raise FriendCryptoError('invalid_invite')
    if not hmac.compare_digest(str(payload.get("secret_hash", "")), secret_hash(secret)):
        raise FriendCryptoError("invalid_invite")
    try:
        expires = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise FriendCryptoError("invalid_invite") from exc
    current = now or datetime.now(UTC)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= current and not allow_expired:
        raise FriendCryptoError("invite_expired")
    validate_endpoint(str(payload.get("endpoint", "")), allow_loopback=allow_loopback)
    return signed, secret


def secret_hash(secret: bytes) -> str:
    return hashlib.sha256(b"rynmesh-friend-invite:" + secret).hexdigest()


def validate_endpoint(endpoint: str, *, allow_loopback: bool = False) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FriendCryptoError("unsafe_endpoint")
    if parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.path not in {"", "/"}:
        raise FriendCryptoError("unsafe_endpoint")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise FriendCryptoError("unsafe_endpoint") from None
    host = parsed.hostname.lower().strip("[]")
    if host in {"metadata", "metadata.google.internal", "169.254.169.254"}:
        raise FriendCryptoError("unsafe_endpoint")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError as exc:
        # v1 accepts literal direct/LAN addresses only. Rejecting hostnames
        # closes the DNS-rebinding window between preflight and socket connect.
        raise FriendCryptoError("unsafe_endpoint") from exc
    for address in addresses:
        if (
            address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or (address.is_loopback and not allow_loopback)
        ):
            raise FriendCryptoError("unsafe_endpoint")
    return endpoint.rstrip("/")


def auth_headers(
    secret: bytes,
    *,
    method: str,
    path: str,
    body: bytes,
    sender: str,
    receiver: str,
    relationship_id: str,
    timestamp: int,
    nonce: str,
) -> dict[str, str]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"rynmesh-friend-request-mac-v1"
    ).derive(secret)
    fields = ["ryn.friend-request.v1", sender, receiver, method.upper(), path, str(timestamp), nonce, hashlib.sha256(body).hexdigest()]
    signature = hmac.new(key, "\n".join(fields).encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Ryn-Friend-Id": relationship_id,
        "X-Ryn-Friend-Peer": sender,
        "X-Ryn-Friend-Time": str(timestamp),
        "X-Ryn-Friend-Nonce": nonce,
        "X-Ryn-Friend-MAC": signature,
    }


def endpoint_category(endpoint: str) -> str:
    host = urlparse(endpoint).hostname or ""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "hostname"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_private:
        return "LAN private"
    return "public"


def verify_auth(
    secret: bytes,
    *,
    method: str,
    path: str,
    body: bytes,
    sender: str,
    receiver: str,
    relationship_id: str,
    timestamp: int,
    nonce: str,
    signature: str,
    now: int,
) -> None:
    if abs(now - timestamp) > AUTH_WINDOW_SECONDS:
        raise FriendClockSkewError(now)
    expected = auth_headers(
        secret,
        method=method,
        path=path,
        body=body,
        sender=sender,
        receiver=receiver,
        relationship_id=relationship_id,
        timestamp=timestamp,
        nonce=nonce,
    )["X-Ryn-Friend-MAC"]
    if not hmac.compare_digest(signature, expected):
        raise FriendCryptoError("friend_auth_failed")
