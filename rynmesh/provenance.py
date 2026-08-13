"""Rynmesh provenance chain.

Each piece of content carries a cryptographic chain of signed links that
prove how it was produced, scanned, attested, and accepted. The chain root
binds the content hash; every later link references the canonical hash of
its predecessor and is independently signed by the issuer responsible for
that step (publisher, scanner, peer attester, etc.).

A single chain head hash is enough to verify the whole production +
distribution provenance: if any link is dropped, reordered, or substituted,
the head hash will not match. Manifests carry both the chain itself and the
expected head hash so receivers can verify atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .crypto import (
    SignatureError,
    SignedPayload,
    canonical_json,
    sha256_bytes,
    sign_payload,
    verify_signed_payload,
)
from .types import now_iso

PROVENANCE_VERSION = "rynmesh-provenance-v0.2"

LINK_GENESIS = "genesis"
LINK_WORK_ENVELOPE = "work_envelope"
LINK_SAFETY_SCAN = "safety_scan"
LINK_PEER_ATTESTATION = "peer_attestation"
LINK_MODEL_ATTESTATION = "model_attestation"

KNOWN_LINK_TYPES = frozenset(
    {
        LINK_GENESIS,
        LINK_WORK_ENVELOPE,
        LINK_SAFETY_SCAN,
        LINK_PEER_ATTESTATION,
        LINK_MODEL_ATTESTATION,
    }
)

REQUIRED_LINK_FIELDS: dict[str, frozenset[str]] = {
    LINK_GENESIS: frozenset({"content_hash", "run_id", "model_id"}),
    LINK_WORK_ENVELOPE: frozenset({"work_id", "envelope_hash"}),
    LINK_SAFETY_SCAN: frozenset({"scanner_id", "outcome", "policy_version"}),
    LINK_PEER_ATTESTATION: frozenset({"peer_id", "bytes_seen_at"}),
    LINK_MODEL_ATTESTATION: frozenset({"model_id", "model_hash"}),
}

EMPTY_PREV = ""


class ProvenanceError(ValueError):
    """Raised when a Rynmesh provenance chain fails verification."""


@dataclass(frozen=True)
class ProvenanceLink:
    sequence: int
    prev_link_hash: str
    link_type: str
    content_id: str
    issuer_peer_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    issued_at: str = field(default_factory=now_iso)
    chain_version: str = PROVENANCE_VERSION

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "chain_version": self.chain_version,
            "sequence": int(self.sequence),
            "prev_link_hash": self.prev_link_hash,
            "link_type": self.link_type,
            "content_id": self.content_id,
            "issuer_peer_id": self.issuer_peer_id,
            "payload": dict(self.payload),
            "issued_at": self.issued_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ProvenanceLink":
        return cls(
            chain_version=str(payload.get("chain_version", PROVENANCE_VERSION)),
            sequence=int(payload["sequence"]),
            prev_link_hash=str(payload.get("prev_link_hash", EMPTY_PREV)),
            link_type=str(payload["link_type"]),
            content_id=str(payload["content_id"]),
            issuer_peer_id=str(payload["issuer_peer_id"]),
            payload=dict(payload.get("payload", {})),
            issued_at=str(payload.get("issued_at", now_iso())),
        )


def signed_link_hash(signed_link: SignedPayload) -> str:
    """Hash of the canonical signed link (payload + signature + key)."""

    return sha256_bytes(canonical_json(signed_link.to_dict()))


def sign_link(link: ProvenanceLink, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(link.unsigned_payload(), private_key_bytes=private_key_bytes)


def make_genesis_link(
    *,
    content_id: str,
    publisher_peer_id: str,
    private_key_bytes: bytes,
    run_id: str,
    model_id: str,
    prompt_hash: str = "",
    extra: dict[str, Any] | None = None,
) -> SignedPayload:
    """Build the chain-rooting genesis link signed by the publisher."""

    payload: dict[str, Any] = {
        "content_hash": content_id,
        "run_id": run_id,
        "model_id": model_id,
        "prompt_hash": prompt_hash,
    }
    if extra:
        payload.update(extra)
    link = ProvenanceLink(
        sequence=0,
        prev_link_hash=EMPTY_PREV,
        link_type=LINK_GENESIS,
        content_id=content_id,
        issuer_peer_id=publisher_peer_id,
        payload=payload,
    )
    return sign_link(link, private_key_bytes=private_key_bytes)


def append_link(
    chain: list[SignedPayload],
    *,
    link_type: str,
    issuer_peer_id: str,
    private_key_bytes: bytes,
    payload: dict[str, Any] | None = None,
    content_id: str | None = None,
) -> SignedPayload:
    """Append a new signed link to ``chain`` and return the new tail link."""

    if not chain:
        raise ProvenanceError("provenance_chain_missing_genesis")
    if link_type not in KNOWN_LINK_TYPES:
        raise ProvenanceError(f"provenance_unknown_link_type: {link_type}")
    prev_signed = chain[-1]
    prev_link = ProvenanceLink.from_payload(prev_signed.payload)
    resolved_content_id = content_id or prev_link.content_id
    if resolved_content_id != prev_link.content_id:
        raise ProvenanceError("provenance_content_id_drift")
    link = ProvenanceLink(
        sequence=prev_link.sequence + 1,
        prev_link_hash=signed_link_hash(prev_signed),
        link_type=link_type,
        content_id=resolved_content_id,
        issuer_peer_id=issuer_peer_id,
        payload=dict(payload or {}),
    )
    signed = sign_link(link, private_key_bytes=private_key_bytes)
    chain.append(signed)
    return signed


@dataclass(frozen=True)
class ProvenanceVerification:
    content_id: str
    head_hash: str
    link_count: int
    link_types: tuple[str, ...]
    issuers: tuple[str, ...]


def verify_chain(
    signed_links: Iterable[SignedPayload | dict[str, Any]],
    *,
    expected_content_id: str | None = None,
    expected_head_hash: str | None = None,
) -> ProvenanceVerification:
    """Validate signatures, link order, and content binding for ``signed_links``.

    Returns a structured verification summary or raises ``ProvenanceError``.
    """

    materialized: list[SignedPayload] = []
    for raw in signed_links:
        if isinstance(raw, SignedPayload):
            materialized.append(raw)
        else:
            try:
                materialized.append(SignedPayload.from_dict(raw))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProvenanceError(f"provenance_link_payload_invalid: {exc}") from exc

    if not materialized:
        raise ProvenanceError("provenance_chain_empty")

    expected_prev = EMPTY_PREV
    content_id = ""
    link_types: list[str] = []
    issuers: list[str] = []

    for index, signed in enumerate(materialized):
        try:
            verify_signed_payload(signed)
        except SignatureError as exc:
            raise ProvenanceError(f"provenance_link_signature_invalid: {exc}") from exc
        try:
            link = ProvenanceLink.from_payload(signed.payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProvenanceError(f"provenance_link_payload_invalid: {exc}") from exc

        if link.chain_version != PROVENANCE_VERSION:
            raise ProvenanceError(f"provenance_chain_version_unsupported: {link.chain_version}")
        if link.link_type not in KNOWN_LINK_TYPES:
            raise ProvenanceError(f"provenance_unknown_link_type: {link.link_type}")
        if link.sequence != index:
            raise ProvenanceError("provenance_link_sequence_mismatch")
        if link.prev_link_hash != expected_prev:
            raise ProvenanceError("provenance_link_prev_hash_mismatch")
        if link.issuer_peer_id != signed.public_key:
            raise ProvenanceError("provenance_link_issuer_key_mismatch")

        required = REQUIRED_LINK_FIELDS[link.link_type]
        missing = required - set(link.payload.keys())
        if missing:
            raise ProvenanceError(f"provenance_link_payload_missing_fields: {sorted(missing)}")

        if index == 0:
            if link.link_type != LINK_GENESIS:
                raise ProvenanceError("provenance_chain_root_not_genesis")
            content_id = link.content_id
            if not content_id:
                raise ProvenanceError("provenance_genesis_missing_content_id")
            if str(link.payload.get("content_hash", "")) != content_id:
                raise ProvenanceError("provenance_genesis_content_hash_mismatch")
            if expected_content_id and content_id != expected_content_id:
                raise ProvenanceError("provenance_content_id_unexpected")
        else:
            if link.link_type == LINK_GENESIS:
                raise ProvenanceError("provenance_duplicate_genesis_link")
            if link.content_id != content_id:
                raise ProvenanceError("provenance_content_id_drift")

        link_types.append(link.link_type)
        issuers.append(link.issuer_peer_id)
        expected_prev = signed_link_hash(signed)

    head_hash = signed_link_hash(materialized[-1])
    if expected_head_hash and head_hash != expected_head_hash:
        raise ProvenanceError("provenance_head_hash_mismatch")

    return ProvenanceVerification(
        content_id=content_id,
        head_hash=head_hash,
        link_count=len(materialized),
        link_types=tuple(link_types),
        issuers=tuple(issuers),
    )


def chain_head_hash(signed_links: Iterable[SignedPayload | dict[str, Any]]) -> str:
    """Compute the chain head hash without re-running full verification."""

    last: SignedPayload | None = None
    for raw in signed_links:
        last = raw if isinstance(raw, SignedPayload) else SignedPayload.from_dict(raw)
    if last is None:
        raise ProvenanceError("provenance_chain_empty")
    return signed_link_hash(last)


def chain_to_payload(signed_links: Iterable[SignedPayload]) -> list[dict[str, Any]]:
    """Serialize a chain for embedding in a manifest payload."""

    return [signed.to_dict() for signed in signed_links]
