"""Tests for the EigenTrust port.

Verifies the algorithmic properties Rynmesh relies on: convergence, the
pre-trust anchor effect (Sybil-bounding), transitive trust, deterministic
ordering, and clipping of negative scores.
"""
from __future__ import annotations

import math

import pytest

from rynmesh.eigentrust import eigentrust, normalize_pretrust


def _is_distribution(t: dict, tol: float = 1e-6) -> bool:
    return math.isclose(sum(t.values()), 1.0, abs_tol=tol)


def test_empty_returns_empty() -> None:
    assert eigentrust({}, {}) == {}


def test_single_pretrusted_peer_is_certain() -> None:
    t = eigentrust({}, {"a": 1.0})
    assert t == {"a": 1.0}


def test_uniform_when_no_anchor_or_edges() -> None:
    # Just listing peers (extra_peers) with nothing else -> uniform.
    t = eigentrust({}, None, extra_peers=["a", "b", "c", "d"])
    assert _is_distribution(t)
    for v in t.values():
        assert math.isclose(v, 0.25, abs_tol=1e-9)


def test_mutual_symmetric_trust_is_symmetric() -> None:
    # Two peers rate each other equally and are equally pre-trusted.
    t = eigentrust(
        {("a", "b"): 1.0, ("b", "a"): 1.0},
        {"a": 1.0, "b": 1.0},
    )
    assert _is_distribution(t)
    assert math.isclose(t["a"], t["b"], abs_tol=1e-6)


def test_transitive_chain_orders_by_distance_from_anchor() -> None:
    # Only 'a' is pre-trusted; a->b->c->d. Expect a > b > c > d.
    scores = {
        ("a", "b"): 1.0,
        ("b", "c"): 1.0,
        ("c", "d"): 1.0,
    }
    t = eigentrust(scores, {"a": 1.0}, alpha=0.15)
    assert _is_distribution(t)
    assert t["a"] > t["b"] > t["c"] > t["d"]


def test_sybil_cluster_bounded_by_anchor() -> None:
    # One honest pre-trusted peer h. A Sybil cluster s0..s9 rates itself
    # mutually with huge weights but is never rated by h.
    # The anchor effect MUST keep the sybils' combined share well below h.
    sybils = [f"s{i}" for i in range(10)]
    scores: dict[tuple[str, str], float] = {}
    for i in sybils:
        for j in sybils:
            if i != j:
                scores[(i, j)] = 1000.0  # blatantly inflated mutual praise
    # No edge from h at all.
    t = eigentrust(scores, {"h": 1.0}, alpha=0.15, extra_peers=["h"])
    assert _is_distribution(t)
    sybil_total = sum(t[s] for s in sybils)
    assert t["h"] > sybil_total, (
        f"anchor h={t['h']:.3f} should dominate combined sybils={sybil_total:.3f}"
    )
    assert t["h"] > 0.5


def test_negative_scores_are_clipped() -> None:
    # Negative ratings are ignored (the algorithm uses positive trust;
    # penalties belong to the credit ledger / slashing layer, not here).
    t_pos = eigentrust({("a", "b"): 1.0}, {"a": 1.0})
    t_mixed = eigentrust({("a", "b"): 1.0, ("a", "c"): -5.0}, {"a": 1.0})
    # c is irrelevant in either case; a and b should be the same.
    assert math.isclose(t_pos["a"], t_mixed["a"], abs_tol=1e-6)
    assert math.isclose(t_pos["b"], t_mixed["b"], abs_tol=1e-6)
    # c is present (it appeared in scores) but its mass should be ~ alpha*p_c=0.
    assert t_mixed.get("c", 0.0) < 1e-3


def test_stranded_rater_defers_to_pretrust() -> None:
    # 'isolated' rates nothing and isn't rated. With pretrusted={a:1},
    # isolated's trust should be small but non-zero (anchor seeded), and
    # the algorithm must converge (no NaN / runaway).
    t = eigentrust({}, {"a": 1.0}, extra_peers=["isolated"], alpha=0.1)
    assert _is_distribution(t)
    assert t["a"] > t["isolated"]
    assert all(math.isfinite(v) for v in t.values())


def test_converges_within_max_iter_on_dense_graph() -> None:
    # 20 peers, every pair rates every other peer randomly but deterministically.
    import random

    rng = random.Random(42)
    peers = [f"n{i}" for i in range(20)]
    scores = {
        (i, j): rng.random()
        for i in peers
        for j in peers
        if i != j
    }
    pretrusted = {peers[0]: 1.0}
    t = eigentrust(scores, pretrusted, alpha=0.1, max_iter=200, epsilon=1e-9)
    assert _is_distribution(t)
    # The pre-trusted peer should at least be among the top half.
    rank = sorted(t.items(), key=lambda kv: -kv[1])
    top_ids = {p for p, _ in rank[: len(peers) // 2]}
    assert peers[0] in top_ids


def test_higher_alpha_anchors_more_strongly() -> None:
    # Anchor a -> b with NO back-edge. With low alpha trust flows down the
    # chain (b takes most of the mass); with high alpha the anchor pulls it
    # back. Closed form: t[a] = 1 / (2 - alpha), so t[a] strictly increases
    # with alpha.
    scores = {("a", "b"): 1.0}
    pretrusted = {"a": 1.0}
    t_low = eigentrust(scores, pretrusted, alpha=0.05, epsilon=1e-9, max_iter=1000)
    t_high = eigentrust(scores, pretrusted, alpha=0.5, epsilon=1e-9, max_iter=1000)
    assert t_high["a"] > t_low["a"]
    assert math.isclose(t_low["a"], 1.0 / (2.0 - 0.05), abs_tol=1e-4)
    assert math.isclose(t_high["a"], 1.0 / (2.0 - 0.5), abs_tol=1e-4)


def test_normalize_pretrust_is_a_distribution() -> None:
    peers = ["a", "b", "c", "d"]
    p = normalize_pretrust({"a": 2.0, "c": 3.0}, peers)
    assert math.isclose(sum(p), 1.0, abs_tol=1e-9)
    assert p[0] > 0 and p[2] > 0 and p[1] == 0 and p[3] == 0
    # Ratio preserved.
    assert math.isclose(p[2] / p[0], 1.5, abs_tol=1e-9)


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        eigentrust({}, {"a": 1.0}, alpha=-0.1)
    with pytest.raises(ValueError):
        eigentrust({}, {"a": 1.0}, alpha=1.1)
    with pytest.raises(ValueError):
        eigentrust({}, {"a": 1.0}, epsilon=0.0)
    with pytest.raises(ValueError):
        eigentrust({}, {"a": 1.0}, max_iter=0)
