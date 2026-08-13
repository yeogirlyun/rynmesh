"""Ryn agent content recommender — Python adoption of X / xAI's pipeline shape.

Architectural source: github.com/xai-org/x-algorithm (the open-sourced
For-You pipeline, May 2026 refresh) — three stages: candidate sourcing →
ranking → filtering. We adopt the *architecture* and the published
algorithmic ideas (heterogeneous features, multi-signal weighted ranker,
diversity + safety filters) at Rynmesh's per-node scale. We do NOT run X's
Grok-class Phoenix transformer per node — that is GPU-class and Twitter-scale
infrastructure. The Ranker Protocol below is the explicit swap-in seam: a
heavier learned model (or an out-of-process call to phoenix/run_pipeline.py
when a checkpoint and accelerator are available) can be dropped in by
implementing `Ranker.score(...)`.

Trust signal comes from rynmesh.eigentrust over consumer-attested serve
receipts (the F1 propagation layer); user preference signal comes from local
feedback (fetched / dismissed / liked). Pure stdlib so a per-node agent can
run it anywhere.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import exp, log
from typing import Protocol

__all__ = [
    "Candidate",
    "UserState",
    "CandidateSource",
    "Ranker",
    "Filter",
    "PeerListSource",
    "SafetyFilter",
    "DedupFilter",
    "DismissedContentFilter",
    "DismissedPublisherFilter",
    "BaselineRanker",
    "Recommender",
]


# ---------------------------------------------------------------- types ----
@dataclass(frozen=True)
class Candidate:
    """One piece of recommendable content surfaced by a source.

    Mirrors the Rynmesh content manifest fields we already have; keep the
    shape narrow so a sourcing layer over the local node's discovery + fetch
    APIs can construct these directly.
    """
    content_id: str
    publisher_peer_id: str
    title: str
    tags: tuple[str, ...]
    safety_outcome: str       # "pass" | "warn" | "block"
    published_at_unix: float
    summary: str = ""


@dataclass
class UserState:
    """Local owner preference + history state. The agent updates this from
    fetch/dismiss/like actions and passes it into each recommendation call."""
    liked_tags: dict[str, float] = field(default_factory=dict)   # tag -> weight
    dismissed_publishers: set[str] = field(default_factory=set)
    fetched_content_ids: set[str] = field(default_factory=set)
    now_unix: float = 0.0     # caller-supplied "now" for recency / decay


# ------------------------------------------------------------ protocols ----
class CandidateSource(Protocol):
    def candidates(self, user: UserState) -> Iterable[Candidate]: ...


class Ranker(Protocol):
    """The Phoenix seam. `trust` is the publisher's EigenTrust score in [0,1]."""
    def score(self, candidate: Candidate, user: UserState, trust: float) -> float: ...


class Filter(Protocol):
    def keep(self, candidate: Candidate, user: UserState) -> bool: ...


# -------------------------------------------------------------- sources ----
@dataclass
class PeerListSource:
    """Candidates grouped by publisher peer. Drop-in for the discover-then-list
    path in store.list_peer_content() — the recommender stays agnostic to how
    the lists were fetched."""
    content_by_peer: Mapping[str, Iterable[Candidate]]

    def candidates(self, user: UserState) -> Iterable[Candidate]:
        for items in self.content_by_peer.values():
            yield from items


# -------------------------------------------------------------- filters ----
class SafetyFilter:
    """Drop content marked `block`. `warn` is penalized by the ranker, not
    filtered, so users can still see and inspect borderline content."""
    def keep(self, candidate: Candidate, user: UserState) -> bool:
        return candidate.safety_outcome != "block"


@dataclass
class DedupFilter:
    """Drop content already fetched OR already recommended this call.

    Also masks the F2 symptom (duplicate peer records returning the same
    content_id twice from discovery) at the recommender layer — fixing the
    upstream duplicate-records gap remains the right rynmesh-side change."""
    seen: set[str] = field(default_factory=set)

    def keep(self, candidate: Candidate, user: UserState) -> bool:
        if candidate.content_id in self.seen:
            return False
        if candidate.content_id in user.fetched_content_ids:
            return False
        self.seen.add(candidate.content_id)
        return True


@dataclass
class DismissedContentFilter:
    """Drop individual items the owner explicitly hid."""

    content_ids: set[str] = field(default_factory=set)

    def keep(self, candidate: Candidate, user: UserState) -> bool:
        return candidate.content_id not in self.content_ids


class DismissedPublisherFilter:
    def keep(self, candidate: Candidate, user: UserState) -> bool:
        return candidate.publisher_peer_id not in user.dismissed_publishers


# -------------------------------------------------------------- rankers ----
@dataclass
class BaselineRanker:
    """MVP weighted-feature ranker — the *Phoenix seam* implementation.

    Weights are deliberately exposed and tunable; the agent can adjust them
    from feedback (e.g., raise w_user_match as the user dismisses off-tag
    items). A learned model can replace this class wholesale by implementing
    Ranker.score(...) — no other code changes."""
    w_trust: float = 1.0
    w_user_match: float = 0.8
    w_recency: float = 0.4
    w_safety_warn_penalty: float = 0.5
    half_life_s: float = 7 * 24 * 3600.0   # week-scale recency decay

    def score(self, candidate: Candidate, user: UserState, trust: float) -> float:
        # User-tag match: sum of liked-tag weights matching the candidate's tags.
        match = sum(user.liked_tags.get(tag, 0.0) for tag in candidate.tags)
        # Recency: exponential half-life decay against the user-supplied "now".
        age = max(0.0, user.now_unix - candidate.published_at_unix)
        if self.half_life_s > 0:
            decay = exp(-age * log(2.0) / self.half_life_s)
        else:
            decay = 1.0
        safety_penalty = (
            -self.w_safety_warn_penalty if candidate.safety_outcome == "warn" else 0.0
        )
        return (
            self.w_trust * trust
            + self.w_user_match * match
            + self.w_recency * decay
            + safety_penalty
        )


# -------------------------------------------------------- orchestration ----
@dataclass
class Recommender:
    """Three-stage pipeline: sources → rank → filters → top-k.

    Filtering runs *after* ranking so DedupFilter is order-stable
    (recommend the highest-scoring representative of any duplicated id)."""
    sources: Sequence[CandidateSource]
    trust: Mapping[str, float]    # publisher_peer_id -> EigenTrust score
    ranker: Ranker
    filters: Sequence[Filter] = ()
    # Wires CreditPolicy.exploration_fraction (vision OQ-2). When > 0 and a
    # `newcomer_predicate` is supplied, the recommender reserves roughly
    # `round(k * exploration_fraction)` of the final slots for newcomer
    # publishers (rank-preserved among themselves). PIE-style structured
    # exploration; gives genuinely good newcomers a path into the slate
    # while leaving the rest of the ranking unchanged.
    exploration_fraction: float = 0.0
    newcomer_predicate: "Callable[[Candidate], bool] | None" = None

    def recommend(self, user: UserState, k: int = 10) -> list[tuple[Candidate, float]]:
        # 1. Sourcing — union by content_id; first occurrence wins.
        seen: dict[str, Candidate] = {}
        for source in self.sources:
            for cand in source.candidates(user):
                seen.setdefault(cand.content_id, cand)
        # 2. Ranking — trust feature from EigenTrust over the publisher.
        scored = [
            (cand, self.ranker.score(cand, user, self.trust.get(cand.publisher_peer_id, 0.0)))
            for cand in seen.values()
        ]
        scored.sort(key=lambda pair: -pair[1])

        # 3. Filtering — applied in rank order. With exploration_fraction>0
        # we fill the carve-out slots first from newcomer publishers in rank
        # order, then top the slate up from the full ranked list (deduped).
        kept: list[tuple[Candidate, float]] = []
        chosen_ids: set[str] = set()

        k_explore = int(round(k * self.exploration_fraction))
        if k_explore > 0 and self.newcomer_predicate is not None:
            for cand, score in scored:
                if len(kept) >= k_explore:
                    break
                if not self.newcomer_predicate(cand):
                    continue
                if all(flt.keep(cand, user) for flt in self.filters):
                    kept.append((cand, score))
                    chosen_ids.add(cand.content_id)

        for cand, score in scored:
            if len(kept) >= k:
                break
            if cand.content_id in chosen_ids:
                continue
            if all(flt.keep(cand, user) for flt in self.filters):
                kept.append((cand, score))
                chosen_ids.add(cand.content_id)
        return kept
