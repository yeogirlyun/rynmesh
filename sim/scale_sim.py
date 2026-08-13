"""Rynmesh scale simulator — Bitcoin-shape adoption + YouTube-shape value.

Companion to sim/world.py (protocol-correctness scenarios). This module is the
*economic / network-dynamics* simulator: spawn nodes over a Bitcoin-shaped
adoption curve, have them produce and consume virtual content/services with
heavy-tailed value distributions (matching observed creator-economy power
laws), wire consumer attestations into the same EigenTrust primitive Rynmesh
will use in production, and surface anomalies — concentration, dead credit,
newcomer lock-out, monopolists — as the network grows.

Research grounding
------------------
- Adoption: Bass diffusion (Bass, "A New Product Growth Model for Consumer
  Durables", 1969). Bitcoin user growth fits a Bass-shape S-curve with
  empirical p~0.001 (innovation), q~0.4 (imitation), m=10^9 carrying
  capacity. We expose these as defaults; tune for any target trajectory.
- Content value: Pareto-distributed intrinsic value. Empirical YouTube
  studies (e.g. Cha et al. 2007, "Tube") find view-count distributions fit a
  power law with alpha~2; small fraction of videos drives most views.
- Service value: same family; service-call distributions are Zipfian.
- Per-creator productivity: also long-tailed; most nodes publish little, a
  few publish a lot. Pareto-distributed `productivity` weight per node.

Honest scope
------------
This is a *behavioral* simulator at per-node granularity, not a protocol-fidelity
simulator (that's rynnet/). Architected for sparse storage so it scales from
~1K (this commit's validated point) toward 10^4-10^5 with incremental work;
true 10^9 requires hierarchical aggregation per registry tier + streaming
EigenTrust, which is the next-phase scale-up track.

Stdlib only. Hooks rynmesh.eigentrust as the trust-recompute primitive.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Allow `python -m sim.scale_sim` from the repo root.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from rynmesh.eigentrust import eigentrust  # noqa: E402


# =========================================================== adoption ====
def bass_adopters(day: int, *, p: float = 0.001, q: float = 0.4, m: int = 10**9) -> int:
    """Cumulative adopters at integer `day` under the Bass diffusion model.

    p = innovation coefficient (external influence)
    q = imitation coefficient (word-of-mouth)
    m = carrying capacity (long-run population)

    Default (p=0.001, q=0.4, m=1e9) gives a Bitcoin-shape S-curve reaching
    ~half of m by ~day 1500 (sim-days ≈ real days; with 1s/day scaling that's
    ~25 minutes of wall time for the inflection).
    """
    if day < 0:
        return 0
    e = math.exp(-(p + q) * day)
    return int(round(m * (1.0 - e) / (1.0 + (q / p) * e)))


# ============================================================== value ====
def sample_pareto(rng: random.Random, alpha: float = 2.0, scale: float = 1.0) -> float:
    """Heavy-tailed sample: most values small, rare values very large.

    alpha=2 matches the empirical YouTube view-count power law roughly;
    smaller alpha = heavier tail (more extreme winners).
    """
    u = rng.random()
    # Bounded to avoid log(0); 1-u in (eps, 1].
    u = max(u, 1e-12)
    return scale * ((1.0 - u) ** (-1.0 / alpha) - 1.0)


# ============================================================== state ====
@dataclass
class NodeS:
    """Per-node simulator state. Compact; no rynmesh runtime objects."""
    nid: int
    joined_day: int
    productivity: float            # publish-rate weight (Pareto-sampled)
    activity: float                # consume-rate weight (Pareto-sampled, gentler)
    content_ids: list[int] = field(default_factory=list)
    # True (private) preferences, ground truth: dict[topic_id, weight] sum=1.
    preferences: dict[int, float] = field(default_factory=dict)
    # Recommender-observed preferences accumulated from "liked" fetches.
    learned_pref: dict[int, float] = field(default_factory=dict)
    fetched_total: int = 0
    liked_total: int = 0


@dataclass
class ContentMeta:
    """A virtual content/service item."""
    cid: int
    publisher: int
    value: float                   # intrinsic value (Pareto-sampled)
    published_day: int
    topic: int = 0                 # one of cfg.n_topics buckets
    fetched_count: int = 0


@dataclass
class World:
    """Sparse simulation world. Edges = consumer-attested serve events
    (rater = consumer, ratee = provider) — the exact input EigenTrust eats."""
    rng: random.Random
    nodes: dict[int, NodeS] = field(default_factory=dict)
    content: dict[int, ContentMeta] = field(default_factory=dict)
    # serve_edges[(consumer, provider)] = positive serve-credit accrued
    serve_edges: dict[tuple[int, int], float] = field(default_factory=lambda: defaultdict(float))
    next_nid: int = 0
    next_cid: int = 0
    pretrusted_nids: set[int] = field(default_factory=set)
    # Cached trust vector (recomputed on demand).
    trust: dict[int, float] = field(default_factory=dict)

    def add_node(
        self, day: int, *, prod_alpha: float = 1.5, act_alpha: float = 2.5,
        n_topics: int = 8,
    ) -> NodeS:
        nid = self.next_nid
        self.next_nid += 1
        # Sparse Pareto-weighted preferences over a few topics — most
        # people have a few strong tastes, not uniform interest.
        n_pref = self.rng.choice([1, 2, 2, 3, 3, 4])
        topics_picked = self.rng.sample(range(n_topics), min(n_pref, n_topics))
        weights = sorted(
            (sample_pareto(self.rng, alpha=2.0) + 1.0 for _ in topics_picked),
            reverse=True,
        )
        total = sum(weights) or 1.0
        prefs = {t: w / total for t, w in zip(topics_picked, weights)}
        node = NodeS(
            nid=nid,
            joined_day=day,
            productivity=sample_pareto(self.rng, alpha=prod_alpha),
            activity=1.0 + sample_pareto(self.rng, alpha=act_alpha),
            preferences=prefs,
        )
        self.nodes[nid] = node
        return node

    def publish(
        self, n: NodeS, day: int, *, value_alpha: float = 2.0,
        n_topics: int = 8, publisher_topic_bias: float = 0.8,
    ) -> ContentMeta:
        cid = self.next_cid
        self.next_cid += 1
        # Creators tend to publish in their niche (bias) but sometimes
        # explore (1 - bias) -> uniform pick. Matches real creator dynamics.
        if n.preferences and self.rng.random() < publisher_topic_bias:
            topics, weights = zip(*n.preferences.items())
            topic = self.rng.choices(topics, weights=weights, k=1)[0]
        else:
            topic = self.rng.randrange(n_topics)
        meta = ContentMeta(
            cid=cid,
            publisher=n.nid,
            value=sample_pareto(self.rng, alpha=value_alpha),
            published_day=day,
            topic=int(topic),
        )
        self.content[cid] = meta
        n.content_ids.append(cid)
        return meta

    def fetch(self, consumer: NodeS, provider_meta: ContentMeta) -> None:
        """Record a consumer-attested serve event (the F1 propagation
        primitive at sim scale: edge from consumer to provider)."""
        if consumer.nid == provider_meta.publisher:
            return  # don't self-fetch
        provider_meta.fetched_count += 1
        self.serve_edges[(consumer.nid, provider_meta.publisher)] += 1.0


# ========================================================== simulator ====
@dataclass
class SimConfig:
    horizon_days: int = 60
    target_pop: int = 1_000        # cap m for tractability; bass_adopters
                                   # uses this as the m parameter for the run.
    p: float = 0.0035              # tuned so target_pop is approached over ~horizon
    q: float = 0.55
    active_frac: float = 0.20      # fraction of pop that consumes on a given day
    publish_active_frac: float = 0.10
    consume_k_candidates: int = 20  # consumer reviews K candidates per day
    consume_picks: int = 2          # picks this many to fetch
    recompute_trust_every: int = 5  # days
    pretrust_seed_count: int = 3    # # of pre-trusted (anchor) nodes
    seed: int = 42
    # ---- B (vision OQ-14, sublinear power) ---------------------------------
    # weight = trust ** beta. beta=1.0 is the F3 baseline. beta<1 (e.g. 0.5
    # sqrt) compresses extreme trust ratios so legitimate-but-huge
    # contributors can never silently buy editorial control. Earning stays
    # uncapped; only the trust->distribution-weight mapping is sublinear.
    weight_transform_beta: float = 1.0
    # ---- C (vision OQ-2, newcomer carve-out) -------------------------------
    # Fraction of each consumer's per-day candidate slate reserved for
    # recently-joined publishers' content (uniform within that pool). 0.0
    # is the F3 baseline; ARCHITECTURE.md names 0.15 as the policy default.
    exploration_fraction: float = 0.0
    newcomer_window_days: int = 30
    # ---- Preference / recommender feedback loop ----------------------------
    # Each node has *true* preferences (private) over N_topics and *learned*
    # preferences observed by the recommender (initially empty). Content is
    # tagged with one topic at publish time, biased by the publisher's own
    # preferences (creators publish in their niche). On fetch, if the
    # consumer's TRUE pref for the item's topic >= like_threshold, the
    # consumer "likes" it and the recommender bumps its learned_pref. Future
    # ranking uses learned_pref, so the system should converge toward
    # consuming content the consumer actually likes — the xAI-style
    # recommendation alignment loop.
    n_topics: int = 8
    like_threshold: float = 0.15
    pref_weight_in_ranking: float = 3.0
    publisher_topic_bias: float = 0.8

    def adopters(self, day: int) -> int:
        return min(self.target_pop, bass_adopters(day, p=self.p, q=self.q, m=self.target_pop))


def run_simulation(cfg: SimConfig | None = None) -> dict[str, Any]:
    cfg = cfg or SimConfig()
    rng = random.Random(cfg.seed)
    world = World(rng=rng)
    daily: list[dict[str, Any]] = []
    t0 = time.time()

    for day in range(cfg.horizon_days):
        # ---- adoption ------------------------------------------------------
        target = cfg.adopters(day)
        while len(world.nodes) < target:
            n = world.add_node(day, n_topics=cfg.n_topics)
            # First few nodes are pre-trusted anchors (trusted roots).
            if len(world.pretrusted_nids) < cfg.pretrust_seed_count:
                world.pretrusted_nids.add(n.nid)

        pop = len(world.nodes)
        all_node_ids = list(world.nodes.keys())
        # Empty days are still recorded so len(daily) == horizon_days; the
        # downstream metric helpers all handle empty inputs.

        # ---- production (per-node productivity-weighted) -------------------
        n_publishers = max(1, int(pop * cfg.publish_active_frac))
        weights_pub = [world.nodes[i].productivity for i in all_node_ids]
        publishers = _weighted_sample_no_replace(rng, all_node_ids, weights_pub, n_publishers)
        published_today = 0
        for pid in publishers:
            n = world.nodes[pid]
            # Each publisher emits 1-3 items (geometric-ish to keep tail heavy).
            for _ in range(1 + (1 if rng.random() < 0.4 else 0)):
                world.publish(
                    n, day, n_topics=cfg.n_topics,
                    publisher_topic_bias=cfg.publisher_topic_bias,
                )
                published_today += 1

        # ---- consumption (consumer attests serve to provider) -------------
        consumers = _weighted_sample_no_replace(
            rng, all_node_ids, [world.nodes[i].activity for i in all_node_ids],
            max(1, int(pop * cfg.active_frac)),
        )
        fetched_today = 0
        liked_today = 0
        if world.content:
            cids = list(world.content.keys())
            value_w = [world.content[c].value for c in cids]
            # C: newcomer-publisher carve-out — PIE-style structured
            # exploration. A fraction of each consumer's slate is sampled
            # uniformly from publishers who joined within
            # newcomer_window_days, so good new contributors get observed
            # even before they have any trust mass.
            newcomer_cutoff = day - cfg.newcomer_window_days
            newcomer_pids = {
                nid for nid, n in world.nodes.items()
                if n.joined_day > newcomer_cutoff
            }
            explore_cids = [
                c for c in cids if world.content[c].publisher in newcomer_pids
            ]
            K = cfg.consume_k_candidates
            K_explore = int(round(K * cfg.exploration_fraction))
            K_main = max(0, K - K_explore)
            for cid_x in consumers:
                consumer = world.nodes[cid_x]
                main_sample = _weighted_sample_no_replace(
                    rng, cids, value_w, min(K_main, len(cids)),
                )
                explore_sample = (
                    _weighted_sample_no_replace(
                        rng, explore_cids, [1.0] * len(explore_cids),
                        min(K_explore, len(explore_cids)),
                    )
                    if K_explore > 0 and explore_cids
                    else []
                )
                seen: set[int] = set()
                sampled: list[int] = []
                for c in main_sample + explore_sample:
                    if c not in seen:
                        seen.add(c)
                        sampled.append(c)
                # Ranker: value * (1 + α_trust*trust) * (1 + α_pref*learned_pref[topic]).
                # learned_pref is initially empty so early ranking is pure
                # value+trust; as the consumer "likes" matching items below,
                # learned_pref grows and ranking pulls toward their niche.
                a_pref = cfg.pref_weight_in_ranking
                ranked = sorted(
                    sampled,
                    key=lambda c: -world.content[c].value
                    * (1.0 + 5.0 * world.trust.get(world.content[c].publisher, 0.0))
                    * (1.0 + a_pref * consumer.learned_pref.get(world.content[c].topic, 0.0)),
                )
                for pick in ranked[: cfg.consume_picks]:
                    meta = world.content[pick]
                    world.fetch(consumer, meta)
                    fetched_today += 1
                    consumer.fetched_total += 1
                    true_pref = consumer.preferences.get(meta.topic, 0.0)
                    if true_pref >= cfg.like_threshold:
                        consumer.liked_total += 1
                        consumer.learned_pref[meta.topic] = (
                            consumer.learned_pref.get(meta.topic, 0.0) + 1.0
                        )
                        liked_today += 1

        # ---- EigenTrust periodic recompute --------------------------------
        if day % cfg.recompute_trust_every == 0 and world.serve_edges:
            pretrust = {pid: 1.0 for pid in world.pretrusted_nids if pid in world.nodes}
            trust_raw = eigentrust(
                dict(world.serve_edges),
                pretrust or None,
                alpha=0.15,
                epsilon=1e-5,
                max_iter=60,
                extra_peers=list(world.nodes.keys()),
            )
            # B: sublinear/saturating transform from EigenTrust score to the
            # distribution weight that actually drives consumption + anomaly
            # observation. With beta=1.0 this is a no-op (F3 baseline).
            world.trust = _apply_weight_transform(trust_raw, cfg.weight_transform_beta)

        # ---- daily metrics -------------------------------------------------
        daily.append({
            "day": day,
            "pop": pop,
            "published_today": published_today,
            "fetched_today": fetched_today,
            "liked_today": liked_today,
            "alignment_today": (liked_today / fetched_today) if fetched_today else 0.0,
            "content_total": len(world.content),
            "serve_edges": len(world.serve_edges),
            "trust_sample_top": _top_share(world.trust, frac=0.01),
            "gini_trust": _gini(list(world.trust.values())),
            "newcomer_share_top10pct": _newcomer_share_among_top(
                world, frac=0.10, recent_days=10, today=day,
            ),
        })

    report = {
        "config": cfg.__dict__,
        "wall_s": round(time.time() - t0, 3),
        "daily": daily,
        "final": _final_summary(world, cfg),
        "anomalies": _detect_anomalies(world, daily),
    }
    return report


def _apply_weight_transform(trust_raw: dict[int, float], beta: float) -> dict[int, float]:
    """Sublinear/saturating transform from EigenTrust score to distribution
    weight (vision OQ-14). beta=1.0 is identity (F3 baseline); beta<1 (e.g.
    0.5 = sqrt) compresses ratios so power saturates at extreme legitimate
    concentration. Renormalized to a distribution for stable comparison."""
    if beta == 1.0 or not trust_raw:
        return dict(trust_raw)
    transformed = {k: (max(0.0, v) ** beta) for k, v in trust_raw.items()}
    total = sum(transformed.values())
    if total > 0:
        return {k: v / total for k, v in transformed.items()}
    return transformed


def _weighted_sample_no_replace(
    rng: random.Random, items: list, weights: list[float], k: int
) -> list:
    """Efraimidis-Spirakis weighted reservoir, O(n)."""
    if k <= 0 or not items:
        return []
    k = min(k, len(items))
    # key_i = u_i ** (1/w_i); pick top-k by key.
    eps = 1e-12
    keyed = []
    for i, w in zip(items, weights):
        ww = max(w, eps)
        u = rng.random()
        u = max(u, 1e-15)
        keyed.append((u ** (1.0 / ww), i))
    keyed.sort(reverse=True)
    return [i for _, i in keyed[:k]]


# ========================================================== anomalies ====
def _gini(values: list[float]) -> float:
    """Gini coefficient ∈ [0,1]. 0 = perfectly equal, 1 = single peer takes all."""
    if not values:
        return 0.0
    xs = sorted(max(0.0, v) for v in values)
    n = len(xs)
    total = sum(xs)
    if total <= 0:
        return 0.0
    cum = 0.0
    for i, v in enumerate(xs, start=1):
        cum += i * v
    return (2.0 * cum) / (n * total) - (n + 1.0) / n


def _top_share(trust: dict[int, float], *, frac: float) -> float:
    """Combined trust held by the top `frac` of peers."""
    if not trust:
        return 0.0
    xs = sorted(trust.values(), reverse=True)
    k = max(1, int(round(len(xs) * frac)))
    total = sum(xs)
    if total <= 0:
        return 0.0
    return sum(xs[:k]) / total


def _newcomer_share_among_top(
    world: World, *, frac: float, recent_days: int, today: int
) -> float:
    """Share of the top `frac` of peers (by trust) who joined within
    `recent_days`. A floor near 0 over time means newcomers can't break in."""
    if not world.trust:
        return 0.0
    ranked = sorted(world.trust.items(), key=lambda kv: -kv[1])
    k = max(1, int(round(len(ranked) * frac)))
    top = [nid for nid, _ in ranked[:k]]
    recent_in_top = sum(
        1 for nid in top if (today - world.nodes[nid].joined_day) <= recent_days
    )
    return recent_in_top / k


def _final_summary(world: World, cfg: SimConfig) -> dict[str, Any]:
    return {
        "pop_final": len(world.nodes),
        "content_total": len(world.content),
        "serve_edges": len(world.serve_edges),
        "trust_known_for": len(world.trust),
        "gini_trust_final": _gini(list(world.trust.values())),
        "top_1pct_trust_share": _top_share(world.trust, frac=0.01),
        "top_10pct_trust_share": _top_share(world.trust, frac=0.10),
        "pretrusted_avg_trust": (
            sum(world.trust.get(p, 0.0) for p in world.pretrusted_nids)
            / max(1, len(world.pretrusted_nids))
        ),
        # Per-node alignment: fraction of own fetches that were "liked"
        # (true-preference >= threshold). Mean across nodes that fetched.
        "alignment_per_node_mean": _alignment_per_node_mean(world),
        # Random-baseline alignment (no learning, uniform pick): mean over
        # all nodes of sum_t(true_pref[t] * P(topic=t)) where P uses
        # the actual content distribution. Useful comparison floor.
        "alignment_random_baseline": _alignment_random_baseline(world),
    }


def _alignment_per_node_mean(world: World) -> float:
    fracs = [
        n.liked_total / n.fetched_total
        for n in world.nodes.values()
        if n.fetched_total > 0
    ]
    return sum(fracs) / len(fracs) if fracs else 0.0


def _alignment_random_baseline(world: World) -> float:
    """Mean over nodes of E[match | uniform topic pick by content frequency]."""
    if not world.content or not world.nodes:
        return 0.0
    # Topic frequency in the published corpus.
    counts: dict[int, int] = {}
    for m in world.content.values():
        counts[m.topic] = counts.get(m.topic, 0) + 1
    total = sum(counts.values()) or 1
    freq = {t: c / total for t, c in counts.items()}
    out = []
    for n in world.nodes.values():
        # P(match) = sum_t freq[t] * 1[true_pref[t] >= like_threshold]
        # Using node's preferences keys at any non-zero weight.
        liked_topics = {t for t, w in n.preferences.items() if w > 0}
        p = sum(freq.get(t, 0.0) for t in liked_topics)
        out.append(p)
    return sum(out) / len(out) if out else 0.0


def _detect_anomalies(world: World, daily: list[dict[str, Any]]) -> dict[str, Any]:
    """Surface conditions worth investigating after a run."""
    flags: list[str] = []
    if daily and daily[-1]["serve_edges"] == 0:
        flags.append("no_serve_edges: credit graph never formed")
    if world.trust:
        top1 = _top_share(world.trust, frac=0.01)
        if top1 > 0.5:
            flags.append(f"monopolization: top 1% holds {top1:.2%} of trust")
        gini = _gini(list(world.trust.values()))
        if gini > 0.95:
            flags.append(f"extreme_inequality: Gini={gini:.3f}")
        # newcomer floor at end of run
        nc = daily[-1]["newcomer_share_top10pct"] if daily else 0.0
        if nc == 0 and len(world.nodes) > 50:
            flags.append("newcomer_lockout: 0% of top-10% are recent joiners")
    if world.trust and any(t < 0 or not math.isfinite(t) for t in world.trust.values()):
        flags.append("trust_corruption: non-finite or negative trust values")
    return {"flags": flags, "count": len(flags)}


# ================================================================ CLI ====
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Rynmesh scale simulator")
    ap.add_argument("--horizon-days", type=int, default=60)
    ap.add_argument("--target-pop", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--weight-beta", type=float, default=1.0,
                    help="B: sublinear weight transform (1.0=linear F3; 0.5=sqrt)")
    ap.add_argument("--exploration-fraction", type=float, default=0.0,
                    help="C: newcomer carve-out (0.0=F3; 0.15=ARCHITECTURE default)")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    cfg = SimConfig(
        horizon_days=args.horizon_days,
        target_pop=args.target_pop,
        seed=args.seed,
        weight_transform_beta=args.weight_beta,
        exploration_fraction=args.exploration_fraction,
    )
    report = run_simulation(cfg)
    blob = json.dumps(report, indent=2)
    if args.out:
        __import__("pathlib").Path(args.out).write_text(blob)
        print(f"wrote {args.out}")
    print(json.dumps({
        "wall_s": report["wall_s"],
        "final": report["final"],
        "anomalies": report["anomalies"],
    }, indent=2))
    return 0 if report["anomalies"]["count"] == 0 else 0   # report only; never fails the run


if __name__ == "__main__":
    raise SystemExit(main())
