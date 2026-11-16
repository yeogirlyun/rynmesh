"""X25519 messaging keypair + authenticated seal/open for peer messaging.

Defense-in-depth over the Nebula transport: a message is sealed for the
recipient's X25519 messaging key (ECDH -> HKDF-SHA256 -> ChaCha20Poly1305), so
relays/operators see only ciphertext. Uses only `cryptography` (no new deps).
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..private_permissions import (
    ensure_private_file_acl,
    read_windows_dacl,
    restrict_windows_acl,
    write_private_bytes_exclusive,
)

_logger = logging.getLogger(__name__)
_INFO = b"rynmesh-peer-msg-v1"


def _read_and_validate_messaging_key(path: Path) -> X25519PrivateKey:
    raw = path.read_bytes()
    if len(raw) != 32:
        raise ValueError("messaging_key_invalid")
    ensure_private_file_acl(
        path,
        track_key_status=True,
        acl_fn=restrict_windows_acl,
        dacl_fn=read_windows_dacl,
        logger=_logger,
    )
    return X25519PrivateKey.from_private_bytes(raw)


def load_or_create_messaging_key(path: str | Path) -> X25519PrivateKey:
    path = Path(path)
    if path.exists():
        return _read_and_validate_messaging_key(path)
    key = X25519PrivateKey.generate()
    raw = key.private_bytes_raw()
    try:
        write_private_bytes_exclusive(
            path,
            raw,
            acl_fn=restrict_windows_acl,
            dacl_fn=read_windows_dacl,
            logger=_logger,
        )
    except FileExistsError:
        return _read_and_validate_messaging_key(path)
    return key


def public_key_b64(priv: X25519PrivateKey) -> str:
    return base64.b64encode(priv.public_key().public_bytes_raw()).decode("ascii")


def _shared(priv: X25519PrivateKey, their_pub_b64: str, info: bytes = _INFO) -> bytes:
    their_pub = X25519PublicKey.from_public_bytes(base64.b64decode(their_pub_b64))
    raw = priv.exchange(their_pub)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(raw)


def seal(
    priv: X25519PrivateKey,
    their_pub_b64: str,
    plaintext: bytes,
    *,
    info: bytes = _INFO,
) -> tuple[str, str]:
    """Seal for `their_pub_b64`. `info` domain-separates one channel from another.

    The default is the direct peer-message channel. A caller that seals for a
    different channel (the registry-hosted mailbox, say) passes its own label so
    a ciphertext captured on one channel cannot be replayed into the other: the
    HKDF output differs, so the AEAD open fails.
    """

    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_shared(priv, their_pub_b64, info)).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce).decode("ascii"), base64.b64encode(ct).decode("ascii")


def open_sealed(
    priv: X25519PrivateKey,
    their_pub_b64: str,
    nonce_b64: str,
    ct_b64: str,
    *,
    info: bytes = _INFO,
) -> bytes:
    """Open a message sealed with the same `info` label. Others fail to decrypt."""

    return ChaCha20Poly1305(_shared(priv, their_pub_b64, info)).decrypt(
        base64.b64decode(nonce_b64), base64.b64decode(ct_b64), None
    )
