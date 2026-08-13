"""Parameter sweeps for the Rynmesh credit + identity policies.

These run the scenario suite across a grid of policy values and report
which combinations break which scenarios. Use to tune ``half_life_days``,
``min_vouches_for_attested``, and tier caps before shipping a policy
change.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from rynmesh.credits import CreditPolicy
from rynmesh.identity import IdentityPolicy

from .scenarios import (
    scenario_collusion_ring,
    scenario_decay_fairness,
    scenario_promotion_path,
    scenario_sybil_farm,
)


@dataclass
class SweepCell:
    half_life_days: float
    min_vouches: int
    sybil_max_weight: float
    decay_steady_score: float
    decay_burst_score: float
    decay_margin: float
    promotion_ok: bool
    collusion_ok: bool

    def to_dict(self) -> dict:
        return self.__dict__


def _sample(half_life_days: float, min_vouches: int) -> SweepCell:
    # Match the sim's strict-by-default policy so the sweep is testing the
    # same protective behavior the scenarios assume.
    credit = CreditPolicy(
        positive_half_life_days=half_life_days,
        enforce_issuer_tier=True,
        enforce_distribution_tier_cap=True,
    )
    identity = IdentityPolicy(min_vouches_for_attested=min_vouches)

    sybil = scenario_sybil_farm(
        sybil_count=50, days=10, credit_policy=credit, identity_policy=identity
    )
    decay = scenario_decay_fairness(
        days=365, credit_policy=credit, identity_policy=identity
    )
    promotion = scenario_promotion_path(credit_policy=credit, identity_policy=identity)
    collusion = scenario_collusion_ring(credit_policy=credit, identity_policy=identity)
    return SweepCell(
        half_life_days=half_life_days,
        min_vouches=min_vouches,
        sybil_max_weight=sybil.metrics["max_sybil_weight"],
        decay_steady_score=decay.metrics["steady_score"],
        decay_burst_score=decay.metrics["burst_score"],
        decay_margin=decay.metrics["steady_score"] - decay.metrics["burst_score"],
        promotion_ok=promotion.metrics["after_proofs"] == "proven",
        collusion_ok=collusion.metrics["promoted_count"] == 0,
    )


def sweep_half_life_and_vouches(
    *,
    half_lives: list[float] | None = None,
    min_vouches: list[int] | None = None,
) -> list[SweepCell]:
    half_lives = half_lives or [30.0, 90.0, 180.0, 365.0]
    min_vouches = min_vouches or [2, 3, 5]
    return [_sample(hl, mv) for hl in half_lives for mv in min_vouches]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any swept cell exposes a policy weakness",
    )
    args = parser.parse_args(argv)

    cells = sweep_half_life_and_vouches()
    failures = sum(
        1
        for cell in cells
        if (
            not cell.promotion_ok
            or not cell.collusion_ok
            or cell.sybil_max_weight > 1.0 + 1e-6
            or cell.decay_margin < 0
        )
    )

    if args.json:
        print(json.dumps([cell.to_dict() for cell in cells], indent=2))
    else:
        print(
            f"{'half_life':>10} {'min_vouch':>10} {'sybil_cap':>10} "
            f"{'steady':>9} {'burst':>9} {'margin':>9} {'promote':>8} {'no_coll':>8}"
        )
        print("-" * 86)
        for cell in cells:
            promote = "yes" if cell.promotion_ok else "NO"
            coll = "yes" if cell.collusion_ok else "NO"
            print(
                f"{cell.half_life_days:>10.1f} {cell.min_vouches:>10d} "
                f"{cell.sybil_max_weight:>10.3f} {cell.decay_steady_score:>9.1f} "
                f"{cell.decay_burst_score:>9.1f} {cell.decay_margin:>9.1f} "
                f"{promote:>8} {coll:>8}"
            )
        print(f"\nfailing cells: {failures}/{len(cells)}")

    # Diagnostic by default; CI integrators opt into hard failure with --strict.
    return 1 if (args.strict and failures) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
