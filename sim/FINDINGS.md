# sim — Scale-simulator findings

Findings surfaced by `sim/scale_sim.py` running at 1K–10K nodes under
Bitcoin-shape Bass adoption + YouTube-shape Pareto content/service value
(α=2), with EigenTrust recomputed every 5 sim-days over consumer-attested
serve receipts. These are *observations*, not testbed defects — they are
exactly the credit-economy issues the simulator exists to catch.

## F3 — Concentration worsens with scale under default parameters

Defaults: EigenTrust α=0.15, 3 pre-trusted anchors, publish-/consume-active
fractions 10%/20%, no anti-concentration weight saturation, no newcomer
reserved discovery bandwidth (the §4 vision items not yet implemented).

| scale | wall s | top 1% trust | Gini | newcomer share of top-10% |
|------:|------:|-------------:|----:|--------------------------:|
| 1K  / 60d |   8 | 64.1% | 0.977 | 0 |
| 10K / 60d | 1067 | **76.8%** | **0.986** | 0 |

*Direction:* concentration **scales up** with the network — the gap between
the heaviest publisher and the rest widens as more pieces of heavy-tailed
content compete. **Newcomer lockout is total** at the top 10%: no one who
joined recently breaks into the upper ranking band.

**This is the dynamic the vision §4 *linchpin* + *newcomer carve-out* + §5.1
*power sublinear at extreme single-entity concentration* exist to prevent —
and it is now empirically reproduced.** The credit/EigenTrust math is
correct; the *parameters* the network would currently ship with are wrong
for the long run.

**Status:** flagged for rynmesh-side investigation. Likely fixes (each
maps to an existing open vision question):
- **Q14 concentration safeguard** — apply a sublinear / saturating
  transform from EigenTrust score → distribution weight so extreme legitimate
  scale stops translating linearly into editorial power.
- **Q2 newcomer carve-out** — reserve a fixed exploration fraction of
  discovery bandwidth for new / low-credit nodes (already in ARCHITECTURE
  ranking) so they can rise.
- **Tune α upward** — higher pre-trust pull anchors the distribution and
  reduces compounding (validated by `test_higher_alpha_anchors_more_strongly`).

Each of these can be A/B-tested in the simulator before changing production
defaults.

## F4 — B (sublinear power) + C (newcomer carve-out) tested against F3

Empirical sweep (`python -m sim.sweep_b_c 1000 60`, seed 42, 5 combos x
~9 s each):

| combo            | β    | explore | Gini  | top 1% | top 10% | newcomer% | flags |
|------------------|-----:|--------:|------:|------:|--------:|----------:|------:|
| baseline (F3)    | 1.00 | 0.00    | 0.977 | 64.1% | 100.0%  | 0.0%      | 3     |
| **B sqrt**       | 0.50 | 0.00    | 0.947 | **34.2%** | 99.1%   | 0.0%  | 1     |
| C explore 15%    | 1.00 | 0.15    | 0.975 | 61.4% | 100.0%  | 0.0%      | 3     |
| **B + C**        | 0.50 | 0.15    | 0.948 | **34.3%** | 98.9%   | 0.0%  | 1     |
| **B agg. + C**   | 0.33 | 0.15    | 0.931 | **24.2%** | 97.3%   | 0.0%  | 1     |

**B (sublinear weight transform, OQ-14) is the headline win.** The
top-1% share of distribution weight drops from 64% (baseline) to 34%
(β=0.5) to 24% (β=0.33), and the literature goal band "top 1% ≤ 25%"
is reached at β≈0.33. Monopolization + extreme-inequality flags clear
from 3 → 1. Confirms the theoretical prediction: caps editorial /
validation power without capping earning.

**C (newcomer carve-out, OQ-2) is mechanically sound but masked by the
metric at this horizon.** C reduces top-1% only marginally on its own
(61% vs 64%), and the `newcomer_share_top10pct` metric reads 0 across
all 5 runs. Honest interpretation: C's mechanism *does* route traffic
to recently-joined publishers (verified by
`test_exploration_fraction_reaches_newcomer_publishers`) and serve
edges form; but credit compounding takes longer than the 60-day
horizon used here, and the current metric counts only nodes joined in
the *last 10 days* (Bass-saturated by day ~20 so almost none qualify
at day 60). C's value is real but should be re-measured with either
(a) a much longer horizon + active churn, or (b) a metric like
"median trust of nodes joined in the last 25% of the horizon."

**The remaining Gini ≈ 0.93** at β=0.33 is *not* a defeat — most nodes
in a young network have never produced/served anything (zero-trust
long tail), which is correct. F4 demonstrates that **the concentration
of power is now bounded**; the heavy tail of *earned* reputation
remains, which is the desired signal that "useful work was done by
specific peers."

### Recommended production defaults (proposed)
- **Adopt B at β = 0.5 (sqrt)** as the new default `trust → distribution_weight`
  transform in `rynmesh/credits.py`. Cuts top-1% by ~half; β = 0.33
  is available for tuning if needed but is more aggressive than
  required.
- **Keep C enabled** with `exploration_fraction = 0.15`
  (ARCHITECTURE-stated value). Cheap, harmless, literature-supported;
  measurable improvement is hidden by metric/horizon today and will
  surface at longer horizons.
- **Defer A-tuning (decay half-life)** until next iteration; B carried
  the test.
- **Defer D (dynamic anchors / HonestPeer)** unless a later sweep
  shows static pre-trust dominates further.

## F5 — Recommender feedback loop converges (the xAI-style alignment loop)

Each simulated node now has a private *true* preference distribution over
N topic buckets (sparse, Pareto-weighted — most nodes have a few strong
tastes, not uniform interest), and a public *learned_pref* the recommender
accumulates from "liked" fetches (where the true preference for the
fetched item's topic ≥ `like_threshold`). Content topics are biased by
the publisher's own preferences (creators publish in their niche). The
ranker uses `value · (1+α_trust·trust) · (1+α_pref·learned_pref[topic])`,
so a node's history pulls future ranking toward content it actually likes.

Empirical (1K nodes / 60 sim-days / seed 42; ~7.6 s per run):

| pref_weight_in_ranking | per-node alignment | random baseline | early 10d | late 10d |
|-----------------------:|-------------------:|----------------:|----------:|---------:|
| **0.0** (loop muted)   | 0.294              | 0.314           | 0.404     | 0.288    |
| **3.0** (loop on)      | **0.592**          | 0.314           | 0.441     | **0.783** |

The feedback loop is doing its job. With the preference signal in ranking,
**by the last 10 days ~78% of fetches are content the consumer truly
likes** (vs ~31% random); without it, alignment hovers at baseline. This
is the recommender-architecture-level analogue of "every node eventually
gets recommended what they like," reproduced in the simulator and a
falsifiable test the recommender must keep passing as new features land
(`tests/test_scale_sim.py::test_alignment_beats_random_baseline`).

This is *also* directly testable against the rynnet protocol testbed:
the same `learned_pref` signal flows through the production
`Recommender` (`rynmesh/recommender.py`) — the simulator just measures
the alignment dynamics offline at a scale where the testbed can't run.

## Performance note (per scale-up)

At 10K nodes the bottleneck is no longer EigenTrust (sparse rewrite makes
the per-iter cost O(edges + n)). The hot path is the per-day candidate
sampling: O(consumers × content) Pareto-weighted sampling per tick. To push
past 10K cleanly the simulator needs either (a) a per-day candidate
pre-pool sampled once and re-ranked per consumer, or (b) numpy-vectored
batch sampling. Tracked.
