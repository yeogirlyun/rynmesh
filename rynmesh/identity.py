"""Rynmesh identity tiers and Sybil resistance.

Plain node-creation is cheap — a peer keypair takes one syscall. The credit
ledger therefore must not weight a fresh-from-zero peer the same as a peer
that has accumulated verifiable work or independent vouches. This module
formalises that with a small tier ladder plus signed evidence:

  unverified  - default; brand-new peer with no attestations
  attested    - vouched for by ``min_vouches`` distinct trusted peers
  staked      - committed an external stake (e.g. payment receipt) and locked it
  proven      - meaningful proof-of-resource history (bandwidth served, uptime)

Tier is a *floor* on trust, not a permission system: even a ``proven`` peer
can be slashed. Tier sets the maximum distribution weight, the maximum stake
a peer can attract from others, and which credit events are emittable.

All evidence is signed with the existing Ed25519 + canonical-JSON pattern so
identity claims slot into the same verification flow as manifests, safety
receipts, and credit events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable

from .crypto import (
    SignatureError,
    SignedPayload,
    sign_payload,
    verify_signed_payload,
)
from .types import now_iso

IDENTITY_VERSION = "rynmesh-identity-v0.1"


class IdentityTier(str, Enum):
    UNVERIFIED = "unverified"
    ATTESTED = "attested"
    STAKED = "staked"
    PROVEN = "proven"


# Caps the maximum distribution_weight a peer can reach regardless of credits.
TIER_DISTRIBUTION_CAPS: dict[IdentityTier, float] = {
    IdentityTier.UNVERIFIED: 1.0,
    IdentityTier.ATTESTED: 2.0,
    IdentityTier.STAKED: 3.5,
    IdentityTier.PROVEN: 5.0,
}

# Tier required to issue particular credit-event kinds. Unverified peers can
# self-publish but cannot vouch for or attest others.
TIER_MIN_TO_ISSUE: dict[str, IdentityTier] = {
    "node_joined": IdentityTier.UNVERIFIED,
    "content_published": IdentityTier.UNVERIFIED,
    "clip_published": IdentityTier.UNVERIFIED,
    "preview_served": IdentityTier.UNVERIFIED,
    "full_served": IdentityTier.UNVERIFIED,
    "registry_operated": IdentityTier.ATTESTED,
    "availability_attested": IdentityTier.ATTESTED,
    "safety_blocked": IdentityTier.ATTESTED,
    "spam_reported": IdentityTier.ATTESTED,
    "protocol_violation": IdentityTier.STAKED,
    "illegal_content": IdentityTier.STAKED,
}


class IdentityError(ValueError):
    """Raised when an identity claim fails verification."""


@dataclass(frozen=True)
class IdentityPolicy:
    """Rules a node applies when promoting peers between tiers."""

    min_vouches_for_attested: int = 3
    min_vouch_issuer_tier: IdentityTier = IdentityTier.ATTESTED
    stake_min_amount: float = 1.0
    proven_min_full_served: int = 50
    proven_min_uptime_hours: float = 168.0  # one week
    vouch_validity_days: float = 365.0


@dataclass(frozen=True)
class PeerVouch:
    """Signed statement that ``issuer_peer_id`` vouches for ``subject_peer_id``."""

    subject_peer_id: str
    issuer_peer_id: str
    issued_at: str = field(default_factory=now_iso)
    reason: str = ""
    identity_version: str = IDENTITY_VERSION

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "issued_at": self.issued_at,
            "subject_peer_id": self.subject_peer_id,
            "issuer_peer_id": self.issuer_peer_id,
            "reason": self.reason,
            "claim_type": "peer_vouch",
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PeerVouch":
        if payload.get("claim_type") != "peer_vouch":
            raise IdentityError("not_a_peer_vouch")
        return cls(
            identity_version=str(payload.get("identity_version", IDENTITY_VERSION)),
            issued_at=str(payload["issued_at"]),
            subject_peer_id=str(payload["subject_peer_id"]),
            issuer_peer_id=str(payload["issuer_peer_id"]),
            reason=str(payload.get("reason", "")),
        )


@dataclass(frozen=True)
class StakeCommitment:
    """Signed external stake commitment a peer locks against their identity."""

    subject_peer_id: str
    stake_kind: str
    stake_amount: float
    evidence_hash: str
    issued_at: str = field(default_factory=now_iso)
    locked_until: str = ""
    identity_version: str = IDENTITY_VERSION

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "issued_at": self.issued_at,
            "subject_peer_id": self.subject_peer_id,
            "stake_kind": self.stake_kind,
            "stake_amount": float(self.stake_amount),
            "evidence_hash": self.evidence_hash,
            "locked_until": self.locked_until,
            "claim_type": "stake_commitment",
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "StakeCommitment":
        if payload.get("claim_type") != "stake_commitment":
            raise IdentityError("not_a_stake_commitment")
        return cls(
            identity_version=str(payload.get("identity_version", IDENTITY_VERSION)),
            issued_at=str(payload["issued_at"]),
            subject_peer_id=str(payload["subject_peer_id"]),
            stake_kind=str(payload["stake_kind"]),
            stake_amount=float(payload["stake_amount"]),
            evidence_hash=str(payload.get("evidence_hash", "")),
            locked_until=str(payload.get("locked_until", "")),
        )


@dataclass(frozen=True)
class ProofOfResource:
    """Signed measurement of useful work a peer has actually performed.

    These are issued by *observer* peers (consumers) not by the subject itself,
    so a peer cannot self-promote. The credit ledger aggregates these into the
    proven-tier check.
    """

    subject_peer_id: str
    issuer_peer_id: str
    metric: str  # 'full_served', 'preview_served', 'uptime_hours', ...
    measured_value: float
    window_start: str
    window_end: str
    evidence_hash: str = ""
    issued_at: str = field(default_factory=now_iso)
    identity_version: str = IDENTITY_VERSION

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "issued_at": self.issued_at,
            "subject_peer_id": self.subject_peer_id,
            "issuer_peer_id": self.issuer_peer_id,
            "metric": self.metric,
            "measured_value": float(self.measured_value),
            "window_start": self.window_start,
            "window_end": self.window_end,
            "evidence_hash": self.evidence_hash,
            "claim_type": "proof_of_resource",
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ProofOfResource":
        if payload.get("claim_type") != "proof_of_resource":
            raise IdentityError("not_a_proof_of_resource")
        return cls(
            identity_version=str(payload.get("identity_version", IDENTITY_VERSION)),
            issued_at=str(payload["issued_at"]),
            subject_peer_id=str(payload["subject_peer_id"]),
            issuer_peer_id=str(payload["issuer_peer_id"]),
            metric=str(payload["metric"]),
            measured_value=float(payload["measured_value"]),
            window_start=str(payload["window_start"]),
            window_end=str(payload["window_end"]),
            evidence_hash=str(payload.get("evidence_hash", "")),
        )


def sign_peer_vouch(vouch: PeerVouch, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(vouch.unsigned_payload(), private_key_bytes=private_key_bytes)


def sign_stake_commitment(stake: StakeCommitment, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(stake.unsigned_payload(), private_key_bytes=private_key_bytes)


def sign_proof_of_resource(proof: ProofOfResource, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(proof.unsigned_payload(), private_key_bytes=private_key_bytes)


def verify_peer_vouch(signed: SignedPayload) -> PeerVouch:
    try:
        verify_signed_payload(signed)
    except SignatureError as exc:
        raise IdentityError(f"identity_signature_invalid: {exc}") from exc
    vouch = PeerVouch.from_payload(signed.payload)
    if vouch.issuer_peer_id != signed.public_key:
        raise IdentityError("vouch_issuer_key_mismatch")
    if vouch.subject_peer_id == vouch.issuer_peer_id:
        raise IdentityError("vouch_self_issued")
    return vouch


def verify_stake_commitment(signed: SignedPayload) -> StakeCommitment:
    try:
        verify_signed_payload(signed)
    except SignatureError as exc:
        raise IdentityError(f"identity_signature_invalid: {exc}") from exc
    stake = StakeCommitment.from_payload(signed.payload)
    if stake.subject_peer_id != signed.public_key:
        raise IdentityError("stake_subject_key_mismatch")
    if stake.stake_amount <= 0 or not math.isfinite(stake.stake_amount):
        raise IdentityError("stake_amount_invalid")
    return stake


def verify_proof_of_resource(signed: SignedPayload) -> ProofOfResource:
    try:
        verify_signed_payload(signed)
    except SignatureError as exc:
        raise IdentityError(f"identity_signature_invalid: {exc}") from exc
    proof = ProofOfResource.from_payload(signed.payload)
    if proof.issuer_peer_id != signed.public_key:
        raise IdentityError("proof_issuer_key_mismatch")
    if proof.subject_peer_id == proof.issuer_peer_id:
        raise IdentityError("proof_self_issued")
    if proof.measured_value < 0 or not math.isfinite(proof.measured_value):
        raise IdentityError("proof_value_invalid")
    return proof


@dataclass(frozen=True)
class IdentityAssessment:
    peer_id: str
    tier: IdentityTier
    vouch_count: int
    stake_amount: float
    proof_metrics: dict[str, float]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "tier": self.tier.value,
            "vouch_count": self.vouch_count,
            "stake_amount": round(self.stake_amount, 6),
            "proof_metrics": {k: round(v, 6) for k, v in sorted(self.proof_metrics.items())},
            "reasons": list(self.reasons),
        }


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def assess_identity(
    *,
    peer_id: str,
    vouches: Iterable[SignedPayload] = (),
    stake_commitments: Iterable[SignedPayload] = (),
    proofs: Iterable[SignedPayload] = (),
    trusted_vouch_issuers: Iterable[str] = (),
    policy: IdentityPolicy | None = None,
    now: datetime | None = None,
) -> IdentityAssessment:
    """Compute the tier a peer qualifies for given the evidence on hand.

    The caller is responsible for collecting evidence from the credit ledger
    or signed-claim store. ``trusted_vouch_issuers`` is the set of peers whose
    vouches count toward ``attested``; in practice this is the running node's
    own configured trust roots plus peers it has already promoted.
    """

    active_policy = policy or IdentityPolicy()
    active_now = now or datetime.now(timezone.utc)
    trusted = set(trusted_vouch_issuers)
    reasons: list[str] = []

    valid_vouches: set[str] = set()
    for signed in vouches:
        try:
            vouch = verify_peer_vouch(signed)
        except IdentityError:
            continue
        if vouch.subject_peer_id != peer_id:
            continue
        if vouch.issuer_peer_id == peer_id:
            continue
        issued = _parse_iso(vouch.issued_at)
        if issued is None:
            continue
        if active_policy.vouch_validity_days > 0:
            age = active_now - issued
            if age > timedelta(days=active_policy.vouch_validity_days):
                continue
        if trusted and vouch.issuer_peer_id not in trusted:
            continue
        valid_vouches.add(vouch.issuer_peer_id)

    total_stake = 0.0
    for signed in stake_commitments:
        try:
            stake = verify_stake_commitment(signed)
        except IdentityError:
            continue
        if stake.subject_peer_id != peer_id:
            continue
        locked_until = _parse_iso(stake.locked_until)
        if locked_until and locked_until <= active_now:
            continue
        total_stake += stake.stake_amount

    proof_totals: dict[str, float] = {}
    for signed in proofs:
        try:
            proof = verify_proof_of_resource(signed)
        except IdentityError:
            continue
        if proof.subject_peer_id != peer_id:
            continue
        if trusted and proof.issuer_peer_id not in trusted:
            continue
        proof_totals[proof.metric] = proof_totals.get(proof.metric, 0.0) + proof.measured_value

    tier = IdentityTier.UNVERIFIED

    if len(valid_vouches) >= active_policy.min_vouches_for_attested:
        tier = IdentityTier.ATTESTED
        reasons.append(f"attested:{len(valid_vouches)}_distinct_vouches")
    else:
        reasons.append(
            f"need_{active_policy.min_vouches_for_attested - len(valid_vouches)}_more_vouches"
        )

    if total_stake >= active_policy.stake_min_amount:
        # Stake only promotes if the peer has at least been attested by *some*
        # vouches; pure cash should not buy unconstrained trust. The stake tier
        # is a maximum bound rather than a leapfrog.
        if tier is IdentityTier.UNVERIFIED:
            reasons.append("stake_present_but_unverified_peer")
        else:
            tier = IdentityTier.STAKED
            reasons.append(f"staked:{total_stake:.2f}_committed")

    if (
        proof_totals.get("full_served", 0.0) >= active_policy.proven_min_full_served
        and proof_totals.get("uptime_hours", 0.0) >= active_policy.proven_min_uptime_hours
        and tier is not IdentityTier.UNVERIFIED
    ):
        tier = IdentityTier.PROVEN
        reasons.append("proven:resource_thresholds_met")

    return IdentityAssessment(
        peer_id=peer_id,
        tier=tier,
        vouch_count=len(valid_vouches),
        stake_amount=total_stake,
        proof_metrics=proof_totals,
        reasons=tuple(reasons),
    )


def tier_distribution_cap(tier: IdentityTier) -> float:
    return TIER_DISTRIBUTION_CAPS.get(tier, TIER_DISTRIBUTION_CAPS[IdentityTier.UNVERIFIED])


def tier_can_issue(tier: IdentityTier, event_kind: str) -> bool:
    required = TIER_MIN_TO_ISSUE.get(event_kind, IdentityTier.STAKED)
    order = [
        IdentityTier.UNVERIFIED,
        IdentityTier.ATTESTED,
        IdentityTier.STAKED,
        IdentityTier.PROVEN,
    ]
    return order.index(tier) >= order.index(required)
