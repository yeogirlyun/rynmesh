"""In-memory simulation world for the Rynmesh credit + identity protocol.

This module deliberately avoids the file-backed ``FileCreditLedger`` so
sims can run thousands of peers and millions of events in seconds. It uses
the same ``CreditEvent`` dataclass and ``distribution_weight`` formula as
production so the numbers transfer directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from rynmesh.credits import (
    EVENT_WEIGHTS,
    CreditEvent,
    CreditPolicy,
    distribution_weight,
)
from rynmesh.crypto import public_key_from_private
from rynmesh.identity import (
    IdentityPolicy,
    IdentityTier,
    PeerVouch,
    ProofOfResource,
    StakeCommitment,
    assess_identity,
    sign_peer_vouch,
    sign_proof_of_resource,
    sign_stake_commitment,
)


def _key() -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate().private_bytes_raw()


@dataclass
class Peer:
    name: str
    private_key: bytes
    role: str = "honest"  # 'honest' | 'sybil' | 'colluder' | 'griefer'
    cluster: str = ""

    @property
    def peer_id(self) -> str:
        return public_key_from_private(self.private_key)


@dataclass
class World:
    """Mutable state of a sim run."""

    now: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    # Sim defaults *enable* both gates so adversarial scenarios exercise the
    # protective behavior. Production CreditPolicy() defaults are the safer
    # off-by-default values; pass an explicit CreditPolicy() if you want to
    # see production's pre-enforcement behavior.
    credit_policy: CreditPolicy = field(
        default_factory=lambda: CreditPolicy(
            enforce_issuer_tier=True,
            enforce_distribution_tier_cap=True,
        )
    )
    identity_policy: IdentityPolicy = field(default_factory=IdentityPolicy)
    peers: dict[str, Peer] = field(default_factory=dict)
    events: list[CreditEvent] = field(default_factory=list)
    vouches: list = field(default_factory=list)
    stakes: list = field(default_factory=list)
    proofs: list = field(default_factory=list)
    trusted_root_ids: set[str] = field(default_factory=set)

    def add_peer(self, peer: Peer, *, trusted_root: bool = False) -> Peer:
        self.peers[peer.peer_id] = peer
        if trusted_root:
            self.trusted_root_ids.add(peer.peer_id)
        return peer

    def tick(self, days: float = 1.0) -> None:
        self.now += timedelta(days=days)

    def record_event(
        self,
        *,
        kind: str,
        subject: Peer,
        issuer: Peer | None = None,
        category: str = "general",
    ) -> CreditEvent:
        amount = EVENT_WEIGHTS[kind]
        ev_issuer = issuer or subject
        event = CreditEvent(
            subject_peer_id=subject.peer_id,
            issuer_peer_id=ev_issuer.peer_id,
            kind=kind,
            amount=amount,
            category=category,
            created_at=self.now.isoformat(),
        )
        self.events.append(event)
        return event

    def issue_vouch(self, *, subject: Peer, issuer: Peer) -> None:
        if subject.peer_id == issuer.peer_id:
            return
        vouch = PeerVouch(
            subject_peer_id=subject.peer_id,
            issuer_peer_id=issuer.peer_id,
            issued_at=self.now.isoformat(),
        )
        self.vouches.append(sign_peer_vouch(vouch, private_key_bytes=issuer.private_key))

    def issue_stake(self, *, subject: Peer, amount: float) -> None:
        stake = StakeCommitment(
            subject_peer_id=subject.peer_id,
            stake_kind="cash",
            stake_amount=amount,
            evidence_hash="sha256:" + "0" * 64,
            issued_at=self.now.isoformat(),
        )
        self.stakes.append(sign_stake_commitment(stake, private_key_bytes=subject.private_key))

    def issue_proof(
        self,
        *,
        subject: Peer,
        issuer: Peer,
        metric: str,
        value: float,
    ) -> None:
        if subject.peer_id == issuer.peer_id:
            return
        proof = ProofOfResource(
            subject_peer_id=subject.peer_id,
            issuer_peer_id=issuer.peer_id,
            metric=metric,
            measured_value=value,
            window_start=(self.now - timedelta(days=7)).isoformat(),
            window_end=self.now.isoformat(),
            issued_at=self.now.isoformat(),
        )
        self.proofs.append(sign_proof_of_resource(proof, private_key_bytes=issuer.private_key))

    def _trusted_set(self) -> set[str]:
        """Trusted vouch issuers: roots, plus peers already promoted by roots.

        Computed in one pass: a peer is trusted iff (a) it's a root, or
        (b) it would reach attested/higher using only roots as trusted.
        This bounds Sybil propagation — a peer cannot become trusted just
        by vouching with other unverified peers.
        """

        trusted = set(self.trusted_root_ids)
        for peer_id in self.peers:
            if peer_id in trusted:
                continue
            tier = assess_identity(
                peer_id=peer_id,
                vouches=self._vouches_for(peer_id),
                stake_commitments=self._stakes_for(peer_id),
                proofs=self._proofs_for(peer_id),
                trusted_vouch_issuers=self.trusted_root_ids,
                policy=self.identity_policy,
                now=self.now,
            ).tier
            if tier in (IdentityTier.ATTESTED, IdentityTier.STAKED, IdentityTier.PROVEN):
                trusted.add(peer_id)
        return trusted

    def _vouches_for(self, peer_id: str) -> list:
        return [v for v in self.vouches if v.payload.get("subject_peer_id") == peer_id]

    def _stakes_for(self, peer_id: str) -> list:
        return [s for s in self.stakes if s.payload.get("subject_peer_id") == peer_id]

    def _proofs_for(self, peer_id: str) -> list:
        return [p for p in self.proofs if p.payload.get("subject_peer_id") == peer_id]

    def assess_tier(self, peer: Peer, *, trusted: set[str] | None = None) -> IdentityTier:
        active_trusted = trusted if trusted is not None else self._trusted_set()
        return assess_identity(
            peer_id=peer.peer_id,
            vouches=self._vouches_for(peer.peer_id),
            stake_commitments=self._stakes_for(peer.peer_id),
            proofs=self._proofs_for(peer.peer_id),
            trusted_vouch_issuers=active_trusted,
            policy=self.identity_policy,
            now=self.now,
        ).tier

    def score(
        self,
        peer: Peer,
        *,
        trusted: set[str] | None = None,
        events_index: dict[str, list[CreditEvent]] | None = None,
    ) -> tuple[float, float, IdentityTier]:
        """Compute (raw_score, distribution_weight, tier) for a peer.

        Mirrors ``FileCreditLedger.scoreboard`` math but on in-memory events.
        Pass ``trusted`` and ``events_index`` to avoid recomputation when
        scoring many peers in one pass.
        """

        positive_half_life = self.credit_policy.positive_half_life_days
        score = 0.0
        peer_events = (
            events_index[peer.peer_id]
            if events_index is not None
            else [e for e in self.events if e.subject_peer_id == peer.peer_id]
        )
        for event in peer_events:
            amount = event.amount
            if amount <= 0:
                score += amount
                continue
            issued = datetime.fromisoformat(event.created_at)
            age_days = max(0.0, (self.now - issued).total_seconds() / 86400.0)
            decay = 0.5 ** (age_days / max(1.0, positive_half_life))
            score += amount * decay
        tier = self.assess_tier(peer, trusted=trusted)
        # Mirror production semantics: only apply the tier cap when policy
        # explicitly enables it. Otherwise leave weight uncapped, exactly
        # like the production scoreboard with default CreditPolicy().
        identity_tier = tier if self.credit_policy.enforce_distribution_tier_cap else None
        weight = distribution_weight(score, self.credit_policy, identity_tier=identity_tier)
        return score, weight, tier

    def build_events_index(self) -> dict[str, list[CreditEvent]]:
        index: dict[str, list[CreditEvent]] = {}
        for event in self.events:
            index.setdefault(event.subject_peer_id, []).append(event)
        return index
