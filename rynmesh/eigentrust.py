"""EigenTrust — global per-peer reputation from pairwise positive trust.

Faithful pure-Python port of the canonical algorithm in
Kamvar, Schlosser & Garcia-Molina, "The EigenTrust Algorithm for Reputation
Management in P2P Networks" (WWW 2003), with the same shape as the production
reference implementation in Karma3Labs/GoEigentrust.

This is the algorithmic primitive Rynmesh's vision §4 calls for: each rater's
judgment is weighted by their own credit, so a Sybil swarm of fresh nodes
cannot manufacture credit for itself. Use this to aggregate signed
consumer-attested serve receipts into a global per-peer distribution weight
that survives an open spec (§5.5: trust by key custody, not by obscurity).

Algorithm
---------
Let `s[i][j]` be the (clipped non-negative) trust rater i expresses for peer j.
Normalize per-rater into a row-stochastic matrix C:

    c[i][j] = max(0, s[i][j]) / sum_k max(0, s[i][k])

If rater i has no positive trust at all, defer to the pre-trust distribution p
(anchor peers everyone trusts a priori; in Rynmesh: trusted roots / proven
identities). Otherwise mass leaks and the iteration drifts.

Global trust t satisfies the fixed point

    t = (1 - alpha) * C^T t + alpha * p

solved by power iteration. `alpha` (~0.1) is the pre-trust weight that
prevents the matrix from being dominated by a colluding cluster.

Stdlib only: O(n^2) per iteration is fine for hundreds of peers. For larger
graphs, swap the dense matrix for a sparse dict-of-dicts (interface
unchanged).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeVar

Peer = TypeVar("Peer")

__all__ = ["eigentrust", "normalize_pretrust"]


def normalize_pretrust(
    pretrusted: Mapping[Peer, float] | None,
    all_peers: list[Peer],
) -> list[float]:
    """Return the pre-trust vector p over `all_peers`, summing to 1.

    If no pre-trust given, falls back to uniform. Otherwise restricted to the
    supplied weights and renormalized.
    """
    n = len(all_peers)
    if n == 0:
        return []
    if pretrusted:
        total = sum(max(0.0, v) for v in pretrusted.values())
        if total > 0:
            return [max(0.0, pretrusted.get(p, 0.0)) / total for p in all_peers]
    return [1.0 / n] * n


def eigentrust(
    scores: Mapping[tuple[Peer, Peer], float],
    pretrusted: Mapping[Peer, float] | None = None,
    *,
    alpha: float = 0.1,
    epsilon: float = 1e-6,
    max_iter: int = 100,
    extra_peers: Iterable[Peer] = (),
) -> dict[Peer, float]:
    """Compute global trust per peer.

    Parameters
    ----------
    scores : map (rater, ratee) -> raw positive score (negatives are clipped to 0).
    pretrusted : map peer -> prior anchor weight (Rynmesh: trusted roots).
        If empty/None, uniform pre-trust is used (least-anchored, weakest Sybil
        resistance — supply real anchors in production).
    alpha : weight of the pre-trust pull each iteration. Typical 0.1.
        Higher alpha = more anchored / Sybil-resistant, less responsive to graph;
        lower alpha = more graph influence, more vulnerable to collusion.
    epsilon : L1 convergence threshold on the trust vector.
    max_iter : iteration cap (the algorithm converges fast; ~10-30 usually).
    extra_peers : peers that appear in pretrust or should be scored but are not
        in any (rater, ratee) edge. Ensures stranded raters/anchors are scored.

    Returns
    -------
    dict peer -> trust in [0, 1], summing to 1 across all known peers.
    Empty input -> empty dict.
    """
    if alpha < 0.0 or alpha > 1.0:
        raise ValueError("alpha must be in [0, 1]")
    if epsilon <= 0.0:
        raise ValueError("epsilon must be positive")
    if max_iter < 1:
        raise ValueError("max_iter must be >= 1")

    # Union of all peers seen as rater, ratee, anchor, or extra.
    universe: set[Peer] = set(extra_peers)
    for (rater, ratee) in scores:
        universe.add(rater)
        universe.add(ratee)
    if pretrusted:
        universe.update(pretrusted.keys())
    if not universe:
        return {}

    peers = sorted(universe, key=lambda p: str(p))
    n = len(peers)
    idx = {p: i for i, p in enumerate(peers)}

    p_vec = normalize_pretrust(pretrusted, peers)

    # Sparse trust matrix: rater_idx -> {ratee_idx: positive_score}.
    # Real Rynmesh graphs are sparse (each consumer rates a tiny fraction of
    # peers) so this collapses the per-iter cost from O(n^2) to O(edges + n).
    raw_row_sum: list[float] = [0.0] * n
    raw: dict[int, dict[int, float]] = {}
    for (rater, ratee), s in scores.items():
        if s <= 0:
            continue
        i, j = idx[rater], idx[ratee]
        raw_row_sum[i] += float(s)
        row = raw.setdefault(i, {})
        row[j] = row.get(j, 0.0) + float(s)

    # Normalize per rater; raters with no positive trust are 'stranded' and
    # defer to the pre-trust distribution p_vec (canonical EigenTrust).
    # `C` matches the EigenTrust paper's notation for the normalized
    # trust matrix; renaming it would obscure the reference.
    C: dict[int, dict[int, float]] = {}  # noqa: N806
    stranded: list[int] = []
    for i in range(n):
        if raw_row_sum[i] > 0:
            rs = raw_row_sum[i]
            C[i] = {j: v / rs for j, v in raw[i].items()}
        else:
            stranded.append(i)

    # Power iteration: t_{k+1} = (1-alpha) * C^T t_k + alpha * p
    t = list(p_vec)
    one_minus_alpha = 1.0 - alpha
    p_nonzero = [j for j in range(n) if p_vec[j] > 0.0]
    for _ in range(max_iter):
        new_t = [alpha * p_vec[j] for j in range(n)]
        # Stranded raters contribute their mass times the pre-trust vector.
        s_stranded = 0.0
        for i in stranded:
            s_stranded += t[i]
        if s_stranded > 0.0:
            contrib = one_minus_alpha * s_stranded
            for j in p_nonzero:
                new_t[j] += contrib * p_vec[j]
        # Active raters: only iterate their actual outgoing edges.
        for i, row in C.items():
            ti = t[i]
            if ti == 0.0:
                continue
            mul = one_minus_alpha * ti
            for j, c_ij in row.items():
                new_t[j] += mul * c_ij
        total = sum(new_t)
        if total > 0:
            new_t = [x / total for x in new_t]
        delta = sum(abs(new_t[i] - t[i]) for i in range(n))
        t = new_t
        if delta < epsilon:
            break

    return {peers[i]: t[i] for i in range(n)}
