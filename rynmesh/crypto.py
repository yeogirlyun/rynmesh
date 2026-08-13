"""Rynmesh canonical signing helpers.

The first Rynmesh proof-of-concept uses Ed25519 signatures over stable
canonical JSON. This keeps the wire objects language-neutral while reusing
a strict canonical-JSON signing discipline.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

SIGNATURE_ALG = "ed25519"


class SignatureError(ValueError):
    """Raised when a signed Rynmesh payload cannot be verified."""


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON bytes for signing and hashing."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


@dataclass(frozen=True)
class SignedPayload:
    """A signed JSON payload with its public verification key attached."""

    payload: dict[str, Any]
    signature: str
    public_key: str
    alg: str = SIGNATURE_ALG

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "alg": self.alg,
            "public_key": self.public_key,
            "signature": self.signature,
            "payload": copy.deepcopy(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignedPayload":
        return cls(
            payload=copy.deepcopy(dict(data["payload"])),
            signature=str(data["signature"]),
            public_key=str(data["public_key"]),
            alg=str(data.get("alg", SIGNATURE_ALG)),
        )

    @property
    def subject_hash(self) -> str:
        return sha256_bytes(canonical_json(self.payload))


def public_key_from_private(private_key_bytes: bytes) -> str:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh signing requires `cryptography`") from exc
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return b64(private_key.public_key().public_bytes_raw())


def sign_payload(payload: dict[str, Any], *, private_key_bytes: bytes) -> SignedPayload:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh signing requires `cryptography`") from exc
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return SignedPayload(
        payload=copy.deepcopy(dict(payload)),
        signature=b64(private_key.sign(canonical_json(payload))),
        public_key=b64(private_key.public_key().public_bytes_raw()),
    )


def verify_signed_payload(signed: SignedPayload) -> None:
    if signed.alg != SIGNATURE_ALG:
        raise SignatureError(f"unsupported signature algorithm: {signed.alg}")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh signature verification requires `cryptography`") from exc

    try:
        public_key = Ed25519PublicKey.from_public_bytes(unb64(signed.public_key))
        public_key.verify(unb64(signed.signature), canonical_json(signed.payload))
    except (InvalidSignature, ValueError) as exc:
        raise SignatureError("signed payload verification failed") from exc
