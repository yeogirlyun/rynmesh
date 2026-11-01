"""Real pinned worker, synthetic model: approval exceeds every active deadline.

Wiring/timing evidence only. No real-model quality or reference score.
"""
import argparse
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from budget_host import ReferenceBudget  # noqa: E402
from test_agent_reference_host import Tools  # noqa: E402
from test_agent_runtime import make_runtime  # noqa: E402
from tool_host import ReferenceToolHost  # noqa: E402
from worker_host import run_worker  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    args = parser.parse_args()
    home = Path(tempfile.mkdtemp(prefix="ryn-reference-owner-wait-"))
    runtime, _, _, _ = make_runtime(home)
    runtime.update_settings({"max_seconds": 10})
    task = runtime.create({"message": "Change the synthetic fixture after exact approval."})
    backend, model_calls, waits = Tools(), [], []
    meter = ReferenceBudget(runtime, task["id"], guard=lambda: None, defer_start=True)

    def policy(request):
        before = len(backend.calls)
        with meter.owner_wait():
            time.sleep(21)  # Longer than active 10 s plus parent pipe grace 10 s.
        waits.append({"no_dispatch_during_wait": len(backend.calls) == before, "seconds": meter.owner_wait_seconds()})
        return request["name"] == "write" and request["arguments"] == {"enabled": True}

    gate = ReferenceToolHost(backend, policy=policy, cancelled=meter.cancelled)

    def dispatch(method, params):
        if method == "approval":
            return gate.approval(params)
        if method == "tool":
            return meter.tool(lambda: gate.execute(params))
        model_calls.append(params)
        if len(model_calls) == 1:
            value = {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "wait-write",
                "type": "function", "function": {"name": "write", "arguments": '{"enabled":true}'}}]}}
        else:
            assert any(m.get("role") == "tool" and "true" in m.get("content", "") for m in params["messages"])
            value = {"message": {"role": "assistant", "content": "Synthetic write confirmed after approval."}}
        return meter.model(lambda _: {**value, "usage": {"input_tokens": 8, "output_tokens": 4}},
                           input_bytes=100, max_output_tokens=20)

    specs = [{"type": "function", "function": {"name": name, "description": "Synthetic timing fixture",
        "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                       "required": ["enabled"], "additionalProperties": False}}} for name in ("read", "write")]
    files = [Path(__file__), *[Path(__file__).with_name(n) for n in
        ("budget_host.py", "worker_host.py", "tool_host.py", "zcode-adapter.mjs", "zcode-worker.mjs")]]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    record = {"kind": "pinned-reference-approval-wait-fixture", "formal_case_score": False,
              "real_model": False, "isolated_home": str(home), "code_snapshot": hashes}
    try:
        result = record["reference"] = run_worker({"session_id": gate.session, "provider_id": "fixture", "model_id": "fixture",
            "context_window": 100000, "max_model_calls": 8, "max_seconds": 10, "real_model": False,
            "prompt": "Change the synthetic fixture after exact approval.", "tools": specs,
            "tool_metadata": {name: backend.metadata(name) for name in ("read", "write")}}, dispatch,
            node=args.node.resolve(), upstream=args.upstream.resolve(), owner_wait_seconds=meter.owner_wait_seconds)
        record["checks"] = {"two_model_calls": len(model_calls) == 2, "one_write": len(backend.calls) == 1,
            "long_owner_wait_excluded": result["owner_wait_seconds"] >= 21 and result["active_wall_seconds"] < 10,
            "native_wait_excluded": result["result"]["owner_wait_seconds"] >= 21,
            "no_dispatch_during_wait": len(waits) == 1 and waits[0]["no_dispatch_during_wait"]}
        record["checks_passed"] = all(record["checks"].values())
    except Exception as exc:
        record.update(checks_passed=False, error_type=type(exc).__name__)
        from rynmesh.agent_runtime.store import AgentError
        if isinstance(exc, AgentError):
            record["error_code"] = str(exc)
    record.update(budget=meter.snapshot(), waits=waits, events=gate.events,
                  code_unchanged=all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in hashes.items()))
    output = Path(__file__).parent / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-approval-wait-fixture")
    output.mkdir()
    (output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "checks_passed": record["checks_passed"]}))


if __name__ == "__main__":
    main()
