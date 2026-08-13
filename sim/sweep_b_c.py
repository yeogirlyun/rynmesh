"""B+C sweep against F3.

Runs the scale simulator at fixed scale (1K nodes, 60 sim-days, seed 42) over
five (weight_transform_beta, exploration_fraction) combinations:

  baseline (F3)     : beta=1.0  explore=0.0
  B-only (sqrt)     : beta=0.5  explore=0.0
  C-only (carve-out): beta=1.0  explore=0.15
  B+C               : beta=0.5  explore=0.15
  B+C aggressive    : beta=0.33 explore=0.15

Prints a comparison table on (Gini, top-1%, top-10%, newcomer-share, flag
count) and saves the full per-combo report to JSON. The goal band from the
literature: Gini in [0.3, 0.5], top-1% <= 0.25, newcomer-share-of-top-10%
>= 0.2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.scale_sim import SimConfig, run_simulation  # noqa: E402


COMBOS = [
    ("baseline (F3)",     1.00, 0.00),
    ("B sqrt",            0.50, 0.00),
    ("C explore 15%",     1.00, 0.15),
    ("B+C",               0.50, 0.15),
    ("B aggressive + C",  0.33, 0.15),
]


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def main() -> int:
    target_pop = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/sweep_b_c.json")

    print(f"running sweep at target_pop={target_pop} horizon_days={horizon}\n")
    summary = []
    full = {}
    for label, beta, explore in COMBOS:
        cfg = SimConfig(
            horizon_days=horizon,
            target_pop=target_pop,
            seed=42,
            weight_transform_beta=beta,
            exploration_fraction=explore,
            recompute_trust_every=5,
        )
        rep = run_simulation(cfg)
        final = rep["final"]
        last_daily = rep["daily"][-1] if rep["daily"] else {}
        row = {
            "label": label,
            "beta": beta,
            "explore": explore,
            "wall_s": rep["wall_s"],
            "gini": final["gini_trust_final"],
            "top_1pct": final["top_1pct_trust_share"],
            "top_10pct": final["top_10pct_trust_share"],
            "newcomer_share_top10pct": last_daily.get("newcomer_share_top10pct", 0.0),
            "flags": rep["anomalies"]["flags"],
        }
        summary.append(row)
        full[label] = rep

    # Pretty table.
    headers = ["combo", "beta", "explore", "Gini", "top1%", "top10%", "newcomer%", "flags"]
    print(" | ".join(h.ljust(20) for h in headers))
    print("-" * 140)
    for r in summary:
        print(" | ".join([
            r["label"].ljust(20),
            f"{r['beta']:.2f}".ljust(20),
            f"{r['explore']:.2f}".ljust(20),
            _fmt(r["gini"]).ljust(20),
            _fmt(r["top_1pct"]).ljust(20),
            _fmt(r["top_10pct"]).ljust(20),
            _fmt(r["newcomer_share_top10pct"]).ljust(20),
            str(len(r["flags"])).ljust(20),
        ]))

    out_path.write_text(json.dumps({"summary": summary, "full": full}, indent=2))
    print(f"\nfull JSON -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
