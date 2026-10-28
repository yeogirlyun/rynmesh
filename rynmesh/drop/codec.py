"""Wire helpers for device drop: signed envelopes, transfer keys and frames.

Every peer request is an Ed25519 ``SignedPayload`` from the sender identity
(``peer_id`` *is* the base64 Ed25519 public key). Content is sealed with the
existing ``peer_box`` X25519 construction. File bytes use a per-transfer master
key derived from an ephemeral X25519 key of the sender and the receiver's
messaging key; each file gets its own subkey so (salt, offset) nonces never
collide across files.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import SignatureError, SignedPayload, sign_payload, verify_signed_payload

PROTOCOL = "rynmesh.drop.v1"
FRAME_PLAINTEXT_BYTES = 1024 * 1024  # 1 MiB plaintext per frame
CHUNK_BYTES = 8 * 1024 * 1024  # hash / commit granularity
MAX_FRAME_WIRE_BYTES = FRAME_PLAINTEXT_BYTES + 16
ENVELOPE_SKEW_S = 600
JS_SAFE_INTEGER = 2**53 - 1

MESSAGE_INFO = b"rynmesh-drop-message-v1"
OFFER_INFO = b"rynmesh-drop-offer-v1"


class DropProtocolError(ValueError):
    """A peer request or response failed verification."""


def new_id() -> str:
    return uuid.uuid4().hex


def json_bytes(value: int | None) -> int | str | None:
    """Lossless JSON byte count: a number while JavaScript can hold it exactly."""

    if value is None:
        return None
    value = int(value)
    return value if -JS_SAFE_INTEGER <= value <= JS_SAFE_INTEGER else str(value)


def parse_bytes(value: Any) -> int:
    if isinstance(value, bool):
        raise DropProtocolError("byte count must be an integer")
    if isinstance(value, int):
        out = value
    elif isinstance(value, str) and value.isdigit():
        out = int(value)
    else:
        raise DropProtocolError("byte count must be an integer")
    if out < 0:
        raise DropProtocolError("byte count cannot be negative")
    return out


# ---- envelopes -------------------------------------------------------------


def make_envelope(
    kind: str, payload: dict[str, Any], *, sender: str, to: str, private_key_bytes: bytes
) -> dict[str, Any]:
    body = {
        **payload,
        "protocol": PROTOCOL,
        "type": kind,
        "from": sender,
        "to": to,
        "ts": int(time.time()),
        "nonce": os.urandom(12).hex(),
    }
    return sign_payload(body, private_key_bytes=private_key_bytes).to_dict()


def open_envelope(
    data: Any, *, kind: str, me: str, expect_from: str | None = None, now: float | None = None
) -> dict[str, Any]:
    """Verify a signed envelope addressed to ``me``; return its payload."""

    try:
        signed = SignedPayload.from_dict(data)
        verify_signed_payload(signed)
    except (KeyError, TypeError, ValueError, SignatureError) as exc:
        raise DropProtocolError("invalid signature") from exc
    payload = signed.payload
    if payload.get("protocol") != PROTOCOL or payload.get("type") != kind:
        raise DropProtocolError("unexpected envelope type")
    if payload.get("from") != signed.public_key:
        raise DropProtocolError("envelope signer is not its sender")
    if payload.get("to") != me:
        raise DropProtocolError("envelope is addressed to another device")
    if expect_from is not None and payload.get("from") != expect_from:
        raise DropProtocolError("envelope is from an unexpected device")
    ts = payload.get("ts")
    current = time.time() if now is None else now
    if not isinstance(ts, int) or abs(current - ts) > ENVELOPE_SKEW_S:
        raise DropProtocolError("envelope timestamp is outside the accepted window")
    return payload


def sign_response(payload: dict[str, Any], *, me: str, to: str, private_key_bytes: bytes) -> dict[str, Any]:
    return make_envelope("response", payload, sender=me, to=to, private_key_bytes=private_key_bytes)


def open_response(data: Any, *, me: str, expect_from: str) -> dict[str, Any]:
    return open_envelope(data, kind="response", me=me, expect_from=expect_from)


# ---- sealing ---------------------------------------------------------------


def _hkdf(secret: bytes, *, salt: bytes | None, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def seal_for(priv: X25519PrivateKey, their_pub_b64: str, plaintext: bytes, info: bytes) -> dict[str, str]:
    from ..services import peer_box

    nonce, ct = peer_box.seal(priv, their_pub_b64, plaintext, info=info)
    return {"nonce": nonce, "ciphertext": ct}


def open_from(priv: X25519PrivateKey, their_pub_b64: str, sealed: dict[str, Any], info: bytes) -> bytes:
    from ..services import peer_box

    try:
        return peer_box.open_sealed(
            priv, their_pub_b64, str(sealed["nonce"]), str(sealed["ciphertext"]), info=info
        )
    except Exception as exc:  # InvalidTag, KeyError, binascii errors
        raise DropProtocolError("sealed content could not be opened") from exc


# ---- transfer keys ---------------------------------------------------------


def new_ephemeral() -> tuple[bytes, str]:
    key = X25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    return key.private_bytes_raw(), pub


def sender_master_key(eph_private: bytes, receiver_pub_b64: str, transfer_id: str) -> bytes:
    shared = X25519PrivateKey.from_private_bytes(eph_private).exchange(
        X25519PublicKey.from_public_bytes(base64.b64decode(receiver_pub_b64))
    )
    return _hkdf(shared, salt=transfer_id.encode("ascii"), info=b"rynmesh-drop-transfer-v1")


def receiver_master_key(messaging_priv: X25519PrivateKey, eph_pub_b64: str, transfer_id: str) -> bytes:
    shared = messaging_priv.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(eph_pub_b64)))
    return _hkdf(shared, salt=transfer_id.encode("ascii"), info=b"rynmesh-drop-transfer-v1")


def file_key(master: bytes, file_id: str) -> bytes:
    return _hkdf(master, salt=None, info=b"rynmesh-drop-file-v1:" + file_id.encode("ascii"))


def put_auth(master: bytes, transfer_id: str, file_id: str, offset: int, salt: bytes) -> str:
    message = f"put|{transfer_id}|{file_id}|{offset}|{salt.hex()}".encode("ascii")
    return hmac.new(master, message, hashlib.sha256).hexdigest()


def check_put_auth(master: bytes, transfer_id: str, file_id: str, offset: int, salt: bytes, value: str) -> bool:
    return hmac.compare_digest(put_auth(master, transfer_id, file_id, offset, salt), str(value or ""))


def _aad(transfer_id: str, file_id: str, offset: int, length: int) -> bytes:
    return f"{PROTOCOL}|{transfer_id}|{file_id}|{offset}|{length}".encode("ascii")


def _nonce(salt: bytes, offset: int) -> bytes:
    return salt + struct.pack(">Q", offset)


class FrameSealer:
    def __init__(self, key: bytes, transfer_id: str, file_id: str, salt: bytes) -> None:
        if len(salt) != 4:
            raise ValueError("frame salt must be 4 bytes")
        self._aead = ChaCha20Poly1305(key)
        self._transfer_id = transfer_id
        self._file_id = file_id
        self._salt = salt

    def seal(self, offset: int, plaintext: bytes) -> bytes:
        ct = self._aead.encrypt(
            _nonce(self._salt, offset), plaintext, _aad(self._transfer_id, self._file_id, offset, len(plaintext))
        )
        return struct.pack(">I", len(ct)) + ct


class FrameOpener:
    def __init__(self, key: bytes, transfer_id: str, file_id: str, salt: bytes) -> None:
        if len(salt) != 4:
            raise DropProtocolError("frame salt must be 4 bytes")
        self._aead = ChaCha20Poly1305(key)
        self._transfer_id = transfer_id
        self._file_id = file_id
        self._salt = salt

    def open(self, offset: int, ciphertext: bytes) -> bytes:
        length = len(ciphertext) - 16
        if length <= 0 or length > FRAME_PLAINTEXT_BYTES:
            raise DropProtocolError("frame size is invalid")
        try:
            return self._aead.decrypt(
                _nonce(self._salt, offset), ciphertext, _aad(self._transfer_id, self._file_id, offset, length)
            )
        except Exception as exc:
            raise DropProtocolError("frame authentication failed") from exc


def chunk_root(chunk_hashes: list[str]) -> str:
    """Root over the ordered 8 MiB chunk SHA-256 digests (hex)."""

    digest = hashlib.sha256(b"rynmesh-drop-root-v1")
    for value in chunk_hashes:
        digest.update(bytes.fromhex(value))
    return digest.hexdigest()


def verification_code(peer_a: str, peer_b: str) -> str:
    """Six-digit code both devices compute from the two signed identities.

    Each side derives it from the identity key it actually verified in the
    signed hello/envelope. A device that substitutes another identity produces
    a different code on the honest screen.
    """

    low, high = sorted([peer_a, peer_b])
    digest = hashlib.sha256(f"rynmesh-drop-sas-v1|{low}|{high}".encode("utf-8")).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"
