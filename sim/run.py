"""CLI entry point for the Rynmesh credit-economy simulator.

Usage:

    python -m sim.run                # run all scenarios with defaults
    python -m sim.run --json         # machine-readable output
    python -m sim.run --scenario sybil_farm collusion_ring
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable

from .scenarios import ALL_SCENARIOS, ScenarioResult

VERDICT_GLYPH = {
    "protected": "PASS",
    "partial": "WARN",
    "broken": "FAIL",
}


def run_scenarios(names: Iterable[str] | None = None) -> list[ScenarioResult]:
    selected = list(ALL_SCENARIOS)
    if names:
        wanted = set(names)
        selected = [fn for fn in ALL_SCENARIOS if fn.__name__.replace("scenario_", "") in wanted]
        if not selected:
            raise SystemExit(f"No matching scenarios. Known: {[fn.__name__ for fn in ALL_SCENARIOS]}")
    return [fn() for fn in selected]


def _print_text(results: list[ScenarioResult]) -> int:
    width = max(len(r.name) for r in results) + 2
    print("Rynmesh credit/identity simulation report")
    print("=" * 60)
    failures = 0
    for r in results:
        if r.matched_expectation:
            glyph = "PASS" if r.expected_verdict == "protected" else "EXPECTED-FAIL"
        else:
            glyph = VERDICT_GLYPH.get(r.verdict, "?")
            failures += 1
        print(f"[{glyph}] {r.name:<{width}} {r.summary}")
        for key, value in r.metrics.items():
            if isinstance(value, float):
                print(f"      {key:<24} {value:.4f}")
            else:
                print(f"      {key:<24} {value}")
        print()
    print(f"Total scenarios: {len(results)}; failing expectation: {failures}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", nargs="*", help="Run only the named scenarios")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args(argv)

    results = run_scenarios(args.scenario)
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
        return 1 if any(not r.matched_expectation for r in results) else 0
    return _print_text(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
