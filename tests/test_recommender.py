"""Tests for the Ryn agent recommender MVP.

Verifies the architectural pipeline behaves as advertised: trust dominates
when other features tie, user-tag match boosts, recency decays, safety/dismiss
filters drop correctly, dedup masks F2 duplicate-id storms, top-k cuts.
"""
from __future__ import annotations

from rynmesh.recommender import (
    BaselineRanker,
    Candidate,
    DedupFilter,
    DismissedPublisherFilter,
    PeerListSource,
    Recommender,
    SafetyFilter,
    UserState,
)


def _c(cid: str, pid: str, *, tags=(), safety="pass", age_s=0.0, title="t") -> Candidate:
    return Candidate(
        content_id=cid,
        publisher_peer_id=pid,
        title=title,
        tags=tuple(tags),
        safety_outcome=safety,
        published_at_unix=10_000.0 - age_s,
    )


def _user(**kw) -> UserState:
    kw.setdefault("now_unix", 10_000.0)
    return UserState(**kw)


def test_trust_dominates_when_other_features_tied() -> None:
    src = PeerListSource({"hi": [_c("x", "hi")], "lo": [_c("y", "lo")]})
    rec = Recommender(
        sources=[src],
        trust={"hi": 0.9, "lo": 0.1},
        ranker=BaselineRanker(),
    )
    result = rec.recommend(_user(), k=2)
    assert [c.content_id for c, _ in result] == ["x", "y"]


def test_user_tag_match_boosts_off_trust_winner() -> None:
    src = PeerListSource({
        "hi": [_c("x", "hi", tags=("offtopic",))],
        "lo": [_c("y", "lo", tags=("loves",))],
    })
    rec = Recommender(
        sources=[src],
        trust={"hi": 0.9, "lo": 0.1},
        ranker=BaselineRanker(w_user_match=5.0),  # crank match weight to flip ordering
    )
    user = _user(liked_tags={"loves": 1.0})
    result = rec.recommend(user, k=2)
    assert [c.content_id for c, _ in result] == ["y", "x"]


def test_recency_decays() -> None:
    src = PeerListSource({"p": [
        _c("fresh", "p", age_s=0.0),
        _c("stale", "p", age_s=60 * 24 * 3600.0),  # ~60 days
    ]})
    rec = Recommender(
        sources=[src], trust={"p": 0.5},
        ranker=BaselineRanker(w_trust=0.0, w_user_match=0.0, w_recency=1.0),
    )
    result = rec.recommend(_user(), k=2)
    assert [c.content_id for c, _ in result] == ["fresh", "stale"]


def test_safety_filter_drops_blocked() -> None:
    src = PeerListSource({"p": [
        _c("ok", "p"),
        _c("bad", "p", safety="block"),
    ]})
    rec = Recommender(
        sources=[src], trust={"p": 0.5},
        ranker=BaselineRanker(),
        filters=[SafetyFilter()],
    )
    result = rec.recommend(_user(), k=5)
    assert [c.content_id for c, _ in result] == ["ok"]


def test_warn_is_penalized_not_dropped() -> None:
    src = PeerListSource({"p": [_c("ok", "p"), _c("warn", "p", safety="warn")]})
    rec = Recommender(
        sources=[src], trust={"p": 0.5},
        ranker=BaselineRanker(),
        filters=[SafetyFilter()],
    )
    ids = [c.content_id for c, _ in rec.recommend(_user(), k=5)]
    assert "warn" in ids and ids.index("ok") < ids.index("warn")


def test_dedup_drops_duplicates_and_fetched() -> None:
    # Simulate F2: same content_id surfaced twice (duplicate registration).
    src = PeerListSource({"p": [_c("dup", "p"), _c("dup", "p"), _c("old", "p")]})
    rec = Recommender(
        sources=[src], trust={"p": 0.5},
        ranker=BaselineRanker(),
        filters=[DedupFilter()],
    )
    user = _user(fetched_content_ids={"old"})
    ids = [c.content_id for c, _ in rec.recommend(user, k=5)]
    assert ids == ["dup"]   # dup deduped; old already-fetched filtered


def test_dismissed_publisher_filtered() -> None:
    src = PeerListSource({"banned": [_c("x", "banned")], "ok": [_c("y", "ok")]})
    rec = Recommender(
        sources=[src], trust={"banned": 0.9, "ok": 0.1},
        ranker=BaselineRanker(),
        filters=[DismissedPublisherFilter()],
    )
    user = _user(dismissed_publishers={"banned"})
    ids = [c.content_id for c, _ in rec.recommend(user, k=5)]
    assert ids == ["y"]


def test_topk_cuts() -> None:
    src = PeerListSource({"p": [_c(f"c{i}", "p") for i in range(10)]})
    rec = Recommender(sources=[src], trust={"p": 0.5}, ranker=BaselineRanker())
    assert len(rec.recommend(_user(), k=3)) == 3


def test_empty_sources_returns_empty() -> None:
    rec = Recommender(sources=[], trust={}, ranker=BaselineRanker())
    assert rec.recommend(_user(), k=5) == []


def test_exploration_fraction_reserves_slots_for_newcomers() -> None:
    """exploration_fraction puts newcomer-publisher items in the slate
    even though their trust is ~0 and the ranker would normally bury them."""
    src = PeerListSource({
        "veteran": [_c(f"old{i}", "veteran") for i in range(8)],
        "rookie":  [_c("new1", "rookie"), _c("new2", "rookie")],
    })
    rec = Recommender(
        sources=[src],
        trust={"veteran": 0.9, "rookie": 0.001},
        ranker=BaselineRanker(),
        exploration_fraction=0.25,
        newcomer_predicate=lambda c: c.publisher_peer_id == "rookie",
    )
    result = rec.recommend(_user(), k=4)
    ids = [c.content_id for c, _ in result]
    rookie_ids = [c for c in ids if c.startswith("new")]
    # k=4, exploration_fraction=0.25 -> reserve 1 newcomer slot.
    assert len(rookie_ids) >= 1, ids
    # And without exploration, no rookie would appear (trust 0.001 buries them).
    rec_baseline = Recommender(
        sources=[src],
        trust={"veteran": 0.9, "rookie": 0.001},
        ranker=BaselineRanker(),
    )
    baseline_ids = [c.content_id for c, _ in rec_baseline.recommend(_user(), k=4)]
    assert all(not c.startswith("new") for c in baseline_ids), baseline_ids


def test_exploration_fraction_zero_is_identity() -> None:
    """exploration_fraction=0 must produce the same result as no carve-out."""
    src = PeerListSource({"p": [_c(f"c{i}", "p") for i in range(6)]})
    rec_a = Recommender(sources=[src], trust={"p": 0.5}, ranker=BaselineRanker())
    rec_b = Recommender(
        sources=[src], trust={"p": 0.5}, ranker=BaselineRanker(),
        exploration_fraction=0.0, newcomer_predicate=lambda c: True,
    )
    assert [c.content_id for c, _ in rec_a.recommend(_user(), k=3)] == \
           [c.content_id for c, _ in rec_b.recommend(_user(), k=3)]
