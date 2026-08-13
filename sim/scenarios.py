"""Adversarial scenarios against the Rynmesh credit + identity protocol.

Each scenario function builds a ``World``, simulates honest and adversarial
behavior, and returns a structured report. Scenarios MUST NOT mutate global
state — re-runs with the same seed should be reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from rynmesh.credits import CreditPolicy
from rynmesh.identity import IdentityPolicy

from .world import Peer, World, _key


@dataclass
class ScenarioResult:
    name: str
    summary: str
    metrics: dict[str, Any]
    verdict: str  # 'protected' | 'partial' | 'broken'
    expected_verdict: str = "protected"  # what a healthy run should report

    @property
    def matched_expectation(self) -> bool:
        return self.verdict == self.expected_verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "expected_verdict": self.expected_verdict,
            "matched_expectation": self.matched_expectation,
            "summary": self.summary,
            "metrics": self.metrics,
        }


def _make_peer(name: str, **kwargs) -> Peer:
    return Peer(name=name, private_key=_key(), **kwargs)


def _new_world(
    *, credit_policy: CreditPolicy | None = None, identity_policy: IdentityPolicy | None = None
) -> World:
    """Build a sim World, preserving the strict-by-default policy when caller omits it."""

    kwargs: dict = {}
    if credit_policy is not None:
        kwargs["credit_policy"] = credit_policy
    if identity_policy is not None:
        kwargs["identity_policy"] = identity_policy
    return World(**kwargs)


def _build_trust_seed(world: World, count: int = 5) -> list[Peer]:
    """Spin up a set of mutually-vouching trusted root peers."""

    roots = [_make_peer(f"root-{i}") for i in range(count)]
    for root in roots:
        world.add_peer(root, trusted_root=True)
    # Roots vouch for each other so they reach attested quickly.
    for issuer in roots:
        for subject in roots:
            if issuer.peer_id != subject.peer_id:
                world.issue_vouch(subject=subject, issuer=issuer)
    # Roots also accumulate publishing credit to give them weight.
    for root in roots:
        for _ in range(20):
            world.record_event(kind="content_published", subject=root)
    return roots


def scenario_sybil_farm(
    *,
    sybil_count: int = 200,
    days: int = 30,
    credit_policy: CreditPolicy | None = None,
    identity_policy: IdentityPolicy | None = None,
) -> ScenarioResult:
    """Attacker spins up N fresh peers, each publishes spam content.

    Identity tiering should keep their distribution_weight at the unverified
    cap regardless of how much content they publish. Without tiering, each
    sybil would accumulate a meaningful weight from publish credits.
    """

    rng = random.Random(7)
    world = _new_world(credit_policy=credit_policy, identity_policy=identity_policy)
    roots = _build_trust_seed(world)

    sybils = [_make_peer(f"sybil-{i}", role="sybil", cluster="farm-A") for i in range(sybil_count)]
    for sybil in sybils:
        world.add_peer(sybil)
    # Each sybil publishes 50 items over the simulated window.
    for _day in range(days):
        world.tick(1.0)
        for sybil in sybils:
            for _ in range(rng.randint(1, 3)):
                world.record_event(kind="content_published", subject=sybil)

    trusted = world._trusted_set()
    index = world.build_events_index()
    sybil_weights = [world.score(s, trusted=trusted, events_index=index) for s in sybils]
    root_weights = [world.score(r, trusted=trusted, events_index=index) for r in roots]

    max_sybil_weight = max(w for _, w, _ in sybil_weights)
    avg_sybil_weight = sum(w for _, w, _ in sybil_weights) / len(sybil_weights)
    avg_root_weight = sum(w for _, w, _ in root_weights) / len(root_weights)

    verdict = "protected" if max_sybil_weight <= 1.0 + 1e-6 else (
        "partial" if max_sybil_weight < avg_root_weight else "broken"
    )

    return ScenarioResult(
        name="sybil_farm",
        summary=(
            f"{sybil_count} fresh sybils published {days * 2} items each over {days} days. "
            f"Max sybil weight {max_sybil_weight:.3f} vs. avg root weight {avg_root_weight:.3f}."
        ),
        metrics={
            "sybil_count": sybil_count,
            "max_sybil_weight": max_sybil_weight,
            "avg_sybil_weight": avg_sybil_weight,
            "avg_root_weight": avg_root_weight,
            "max_sybil_score": max(s for s, _, _ in sybil_weights),
        },
        verdict=verdict,
    )


def scenario_collusion_ring(
    *,
    ring_size: int = 10,
    vouches_each: int = 9,
    credit_policy: CreditPolicy | None = None,
    identity_policy: IdentityPolicy | None = None,
) -> ScenarioResult:
    """Attacker creates a cluster that vouches for itself.

    Vouches are only honored when they come from already-trusted peers.
    A self-isolated ring should never reach the attested tier because none
    of its members are trusted roots.
    """

    world = _new_world(credit_policy=credit_policy, identity_policy=identity_policy)
    _build_trust_seed(world)

    ring = [_make_peer(f"ring-{i}", role="colluder", cluster="ring-A") for i in range(ring_size)]
    for member in ring:
        world.add_peer(member)
    # Ring members vouch for every other member.
    for issuer in ring:
        for subject in ring:
            if issuer.peer_id != subject.peer_id:
                world.issue_vouch(subject=subject, issuer=issuer)

    trusted = world._trusted_set()
    ring_tiers = [world.assess_tier(r, trusted=trusted) for r in ring]
    promoted = sum(1 for t in ring_tiers if t.value != "unverified")

    verdict = "protected" if promoted == 0 else "broken"
    return ScenarioResult(
        name="collusion_ring",
        summary=(
            f"{ring_size}-peer ring exchanged {ring_size * (ring_size - 1)} vouches with no "
            f"external trust. {promoted} reached attested or higher."
        ),
        metrics={
            "ring_size": ring_size,
            "promoted_count": promoted,
            "ring_tiers": [t.value for t in ring_tiers],
        },
        verdict=verdict,
    )


def scenario_slash_grief(
    *,
    attacker_count: int = 50,
    credit_policy: CreditPolicy | None = None,
    identity_policy: IdentityPolicy | None = None,
) -> ScenarioResult:
    """Attackers try to issue penalty events against an honest peer.

    The credit primitive rejects negative events from non-trusted issuers,
    so any unattested attacker should fail at signing-verification time.
    Score the honest victim before/after the attack.
    """

    from rynmesh.credits import (
        EVENT_WEIGHTS,
        CreditEvent,
        CreditLedgerError,
        sign_credit_event,
        verify_credit_event_with_policy,
    )
    from rynmesh.identity import IdentityTier

    world = _new_world(credit_policy=credit_policy, identity_policy=identity_policy)
    _build_trust_seed(world)
    victim = _make_peer("honest-victim")
    world.add_peer(victim)
    for _ in range(50):
        world.record_event(kind="content_published", subject=victim)

    rejected = 0
    accepted = 0
    for i in range(attacker_count):
        attacker = _make_peer(f"griefer-{i}", role="griefer")
        world.add_peer(attacker)

        event = CreditEvent(
            subject_peer_id=victim.peer_id,
            issuer_peer_id=attacker.peer_id,
            kind="protocol_violation",
            amount=EVENT_WEIGHTS["protocol_violation"],
            created_at=world.now.isoformat(),
        )
        signed = sign_credit_event(event, private_key_bytes=attacker.private_key)
        try:
            verify_credit_event_with_policy(
                signed,
                policy=world.credit_policy,
                issuer_tier=IdentityTier.UNVERIFIED,
            )
            accepted += 1
        except CreditLedgerError:
            rejected += 1

    verdict = "protected" if accepted == 0 else "broken"
    return ScenarioResult(
        name="slash_grief",
        summary=(
            f"{attacker_count} attackers attempted unsanctioned protocol_violation slashes. "
            f"{rejected} rejected, {accepted} accepted."
        ),
        metrics={
            "attacker_count": attacker_count,
            "rejected": rejected,
            "accepted": accepted,
        },
        verdict=verdict,
    )


def scenario_decay_fairness(
    *,
    days: int = 365,
    credit_policy: CreditPolicy | None = None,
    identity_policy: IdentityPolicy | None = None,
) -> ScenarioResult:
    """Long-running honest peer vs. flash burst attacker — does decay work?

    Both peers are attested (so the unverified cap does not mask scores).
    Honest peer publishes 1/day for a year. Burst attacker publishes 200
    items in a single day at month 11. After decay, the steady contributor
    should retain meaningful weight relative to the burst attacker.
    """

    world = _new_world(credit_policy=credit_policy, identity_policy=identity_policy)
    roots = _build_trust_seed(world)
    steady = _make_peer("steady-honest")
    burst = _make_peer("burst-attacker", role="sybil")
    world.add_peer(steady)
    world.add_peer(burst)
    for root in roots[:3]:
        world.issue_vouch(subject=steady, issuer=root)
        world.issue_vouch(subject=burst, issuer=root)

    for day in range(days):
        world.tick(1.0)
        world.record_event(kind="content_published", subject=steady)
        if day == 330:
            for _ in range(200):
                world.record_event(kind="content_published", subject=burst)

    trusted = world._trusted_set()
    index = world.build_events_index()
    steady_score, steady_weight, _ = world.score(steady, trusted=trusted, events_index=index)
    burst_score, burst_weight, _ = world.score(burst, trusted=trusted, events_index=index)

    # The steady contributor has more lifetime decayed credit than a single
    # one-day burst that has also already decayed for a month.
    verdict = "protected" if steady_score > burst_score else "partial"
    return ScenarioResult(
        name="decay_fairness",
        summary=(
            f"After {days} days, steady score {steady_score:.1f} (weight {steady_weight:.3f}) "
            f"vs month-11 burst attacker score {burst_score:.1f} (weight {burst_weight:.3f})."
        ),
        metrics={
            "steady_score": steady_score,
            "burst_score": burst_score,
            "steady_weight": steady_weight,
            "burst_weight": burst_weight,
        },
        verdict=verdict,
    )


def scenario_promotion_path(
    *,
    days: int = 60,
    credit_policy: CreditPolicy | None = None,
    identity_policy: IdentityPolicy | None = None,
) -> ScenarioResult:
    """Honest new peer gets vouched, stakes, and serves bytes — does it promote?

    This is the positive-control: confirms a legitimate peer can actually
    reach proven tier by accumulating evidence. If this scenario fails to
    promote, the policy is too strict.
    """

    world = _new_world(credit_policy=credit_policy, identity_policy=identity_policy)
    roots = _build_trust_seed(world)
    candidate = _make_peer("rising-honest")
    world.add_peer(candidate)

    # All root vouches: now attested (uses every available trust root so
    # this scenario does not artificially fail under stricter min_vouches).
    for issuer in roots:
        world.issue_vouch(subject=candidate, issuer=issuer)
    after_vouches = world.assess_tier(candidate)

    world.issue_stake(subject=candidate, amount=10.0)
    after_stake = world.assess_tier(candidate)

    # Roots observe candidate serving bytes
    for issuer in roots:
        world.issue_proof(subject=candidate, issuer=issuer, metric="full_served", value=100.0)
        world.issue_proof(subject=candidate, issuer=issuer, metric="uptime_hours", value=240.0)
    after_proofs = world.assess_tier(candidate)

    verdict = "protected" if after_proofs.value == "proven" else "broken"
    return ScenarioResult(
        name="promotion_path",
        summary=(
            f"Honest candidate progression: vouches→{after_vouches.value}, "
            f"stake→{after_stake.value}, proofs→{after_proofs.value}."
        ),
        metrics={
            "after_vouches": after_vouches.value,
            "after_stake": after_stake.value,
            "after_proofs": after_proofs.value,
        },
        verdict=verdict,
    )


def scenario_sybil_farm_without_enforcement(*, sybil_count: int = 200, days: int = 30) -> ScenarioResult:
    """Same Sybil farm, but with the production *default* policy (no cap).

    This is a contrast scenario: it is *expected* to report ``broken``. Its
    job is to document, in CI output, what happens when an operator deploys
    with ``CreditPolicy()`` (cap disabled) and a Sybil farm shows up.
    """

    from rynmesh.credits import CreditPolicy

    open_policy = CreditPolicy()  # explicitly default-off enforcement
    inner = scenario_sybil_farm(
        sybil_count=sybil_count,
        days=days,
        credit_policy=open_policy,
    )
    return ScenarioResult(
        name="sybil_farm_without_enforcement",
        summary="(contrast) " + inner.summary,
        metrics=inner.metrics,
        verdict=inner.verdict,
        expected_verdict="broken",
    )


ALL_SCENARIOS = [
    scenario_sybil_farm,
    scenario_sybil_farm_without_enforcement,
    scenario_collusion_ring,
    scenario_slash_grief,
    scenario_decay_fairness,
    scenario_promotion_path,
]
