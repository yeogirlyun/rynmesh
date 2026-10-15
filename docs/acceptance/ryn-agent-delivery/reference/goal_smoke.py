"""Deterministic check of the unmodified upstream goal verifier/continuation."""
import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from worker_host import run_worker


def smoke(node, upstream):
    trials = []
    for initial_pass in (True, False):
        with tempfile.TemporaryDirectory(prefix="ryn-reference-goal-") as folder:
            database = Path(folder) / "session.sqlite"
            calls = []
            verifications = []

            def dispatch(method, params, *, calls=calls, verifications=verifications, initial_pass=initial_pass):
                assert method == "model"
                calls.append(params)
                if not params["tools"]:
                    assert "Verify whether the active session goal" in params["messages"][-1]["content"]
                    passed = initial_pass or bool(verifications)
                    verifications.append(passed)
                    content = json.dumps({"passed": passed, "reason": "Synthetic protocol evidence",
                                          "nextAction": "" if passed else "Complete the synthetic explanation."})
                else:
                    content = "Synthetic explanation complete."
                return {"message": {"role": "assistant", "content": content}}

            config = {"session_id": "persistent-goal-smoke", "session_db_path": str(database),
                      "provider_id": "ryn-test", "model_id": "fixture", "context_window": 1000000,
                      "max_model_calls": 8, "max_seconds": 30, "real_model": False,
                      "prompt": "Explain the synthetic fixture in one sentence.", "tools": [{"type": "function",
                          "function": {"name": "ryn_fixture_read", "description": "Read synthetic fixture",
                                       "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
                      "tool_metadata": {"ryn_fixture_read": {"effect": "read"}}}
            outcome = run_worker(config, dispatch, node=node, upstream=upstream)
            assert verifications == ([True] if initial_pass else [False, True])
            assert len(calls) == (2 if initial_pass else 4)
            assert outcome["result"]["target_status"] == "complete"
            # A separate connection proves the runtime closed a real persistent store.
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute("select count(*) from session").fetchone()[0] == 1
                assert connection.execute("select count(*) from message").fetchone()[0] >= 2
            trials.append({"first_verification_passed": initial_pass, "verifications": verifications, **outcome})
    return {"kind": "deterministic-upstream-goal-continuation", "formal_case_score": False, "trials": trials}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    body = json.dumps(smoke(args.node.resolve(), args.upstream.resolve()), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(body, encoding="utf-8")
    print(body)
