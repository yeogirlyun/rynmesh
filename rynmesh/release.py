"""Pure release-manifest logic for secure auto-update.

A release manifest is a signed payload. Authorization requires BOTH a valid
signature AND that the signer's public key is in the node's PINNED publisher set
(verify_signed_payload alone only proves "signed by whoever owns the embedded
key" — pinning the key is what makes it an *authorized* publisher).
"""
from __future__ import annotations

from typing import Any, Iterable

from .crypto import SignatureError, SignedPayload, verify_signed_payload

RELEASE_KIND = "rynmesh.release"


class ReleaseError(ValueError):
    pass


def build_manifest(
    *, version: str, wheel_sha256: str, wheel_size: int, wheel_filename: str,
    requires_python: str, published_at: str, notes: str,
) -> dict[str, Any]:
    return {
        "kind": RELEASE_KIND,
        "version": str(version),
        "wheel_sha256": str(wheel_sha256),
        "wheel_size": int(wheel_size),
        "wheel_filename": str(wheel_filename),
        "requires_python": str(requires_python),
        "published_at": str(published_at),
        "notes": str(notes or ""),
    }


def verify_manifest(signed: SignedPayload, pinned_pubkeys: Iterable[str]) -> dict[str, Any]:
    pinned = {str(k).strip() for k in pinned_pubkeys if str(k).strip()}
    if not pinned:
        raise ReleaseError("no pinned publisher keys configured")
    if signed.public_key not in pinned:
        raise ReleaseError("release signed by a non-pinned publisher key")
    try:
        verify_signed_payload(signed)
    except SignatureError as exc:
        raise ReleaseError(f"release signature invalid: {exc}") from exc
    payload = signed.payload
    if payload.get("kind") != RELEASE_KIND:
        raise ReleaseError(f"not a release manifest: kind={payload.get('kind')!r}")
    if not payload.get("version") or not payload.get("wheel_sha256"):
        raise ReleaseError("release manifest missing version/wheel_sha256")
    return dict(payload)


def _parse(v: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(v).split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return _parse(candidate) > _parse(current)
