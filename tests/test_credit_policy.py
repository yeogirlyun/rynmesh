"""Tests for the configurable weight_transform_beta in CreditPolicy.

Default beta=0.5 (sqrt) is the historical behavior; smaller beta compresses
ratios more aggressively (vision OQ-14 sublinear weight transform).
"""
from __future__ import annotations

import math

from rynmesh.credits import CreditPolicy, distribution_weight


def test_default_beta_matches_historical_sqrt() -> None:
    """Default policy must yield the pre-existing sqrt behavior."""
    p = CreditPolicy()
    assert p.weight_transform_beta == 0.5
    for s in (1.0, 4.0, 25.0, 100.0):
        expected = min(p.max_distribution_weight, 1.0 + math.sqrt(s) / 4.0)
        assert math.isclose(distribution_weight(s, p), expected, abs_tol=1e-9)


def test_beta_one_is_linear() -> None:
    p = CreditPolicy(weight_transform_beta=1.0)
    s = 16.0  # well below the saturation cap
    # 1.0 + s/4 = 5.0 -> capped at max=5.0
    assert math.isclose(distribution_weight(s, p), 5.0, abs_tol=1e-9)
    # Smaller scores: linear in s
    assert math.isclose(distribution_weight(4.0, p), 2.0, abs_tol=1e-9)


def test_beta_aggressive_compresses_ratios() -> None:
    """β=0.33 compresses ratios more than β=0.5 (closer to flat)."""
    high, low = 100.0, 1.0
    sqrt_ratio = distribution_weight(high, CreditPolicy(weight_transform_beta=0.5)) / \
                 distribution_weight(low,  CreditPolicy(weight_transform_beta=0.5))
    cube_ratio = distribution_weight(high, CreditPolicy(weight_transform_beta=0.33)) / \
                 distribution_weight(low,  CreditPolicy(weight_transform_beta=0.33))
    assert cube_ratio < sqrt_ratio, (
        f"β=0.33 should compress more than β=0.5; got cube_ratio={cube_ratio:.3f} "
        f"vs sqrt_ratio={sqrt_ratio:.3f}"
    )


def test_caps_remain_enforced_under_all_betas() -> None:
    huge = 10_000.0
    for beta in (1.0, 0.5, 0.33, 0.1):
        p = CreditPolicy(weight_transform_beta=beta)
        w = distribution_weight(huge, p)
        assert p.min_distribution_weight <= w <= p.max_distribution_weight, (
            f"weight {w} out of band at β={beta}"
        )


def test_negative_scores_unchanged_by_beta() -> None:
    """β only affects the positive-score branch; the penalty branch is
    unchanged (negative score → small distribution weight clamped at min)."""
    s = -100.0
    p1 = CreditPolicy(weight_transform_beta=0.5)
    p2 = CreditPolicy(weight_transform_beta=0.33)
    assert math.isclose(distribution_weight(s, p1), distribution_weight(s, p2), abs_tol=1e-12)
