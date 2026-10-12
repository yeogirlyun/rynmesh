"""Conservative mechanical evidence summary, never a formal semantic grader."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from node import digest, write


def check(provider_snapshot, consumer_snapshot, provider_events, consumer_events):
    p = provider_snapshot["/api/local/two-host/evidence"]["response"]
    c = consumer_snapshot["/api/local/two-host/evidence"]["response"]
    pi, ci = p["identity"], c["identity"]
    def both_wire():
        import urllib.parse
        ph = urllib.parse.urlsplit(pi["endpoint"]).hostname
        ch = urllib.parse.urlsplit(ci["endpoint"]).hostname
        return (ph != ch and ph not in {"127.0.0.1", "localhost"} and ch not in {"127.0.0.1", "localhost"}
                and any(e["kind"] == "peer_http" and e.get("client", [None])[0] == ch for e in provider_events)
                and any(e["kind"] == "peer_http" and e.get("client", [None])[0] == ph for e in consumer_events))
    tests = {"distinct_observed_hostname": pi["hostname"] != ci["hostname"],
        "distinct_peer_identity": pi["peer_id"] != ci["peer_id"],
        "same_installed_python_code": pi["code"]["sha256"] == ci["code"]["sha256"],
        "same_launcher_code": pi["code"]["launcher_sha256"] == ci["code"]["launcher_sha256"],
        "bidirectional_nonloopback_peer_requests": both_wire(),
        "owner_source_unchanged_at_snapshot": p["owner_source_unchanged"] is True,
        "provider_has_zero_agent_tasks_at_snapshot": not p["tasks"] and not p["agent_store"]["decrypted_bindings"],
        "consumer_has_no_local_sources": not c["sources"],
        "consumer_runtime_is_actual_consumer": c["runtime_consumer_node_id"] == ci["peer_id"]}
    rounds = {}
    for event in consumer_events:
        if event["kind"] == "agent_model_result" and event.get("result", {}).get("message", {}).get("tool_calls"):
            task_id = event["task_id"]
            rounds[task_id] = rounds.get(task_id, 0) + 1
    tests["two_sessions_with_two_tool_call_rounds"] = len([v for v in rounds.values() if v >= 2]) >= 2
    dispatched = [e for e in provider_events if e["kind"] == "upstream_dispatch"]
    tests["only_authorized_glm_dispatched"] = bool(dispatched) and all(e.get("model") == "glm-5.3-flash" for e in dispatched)
    history = p.get("manual_conversations", [])
    canaries = [m["content"] for row in history for m in row.get("messages", [])
                if m.get("content", "").startswith("PROVIDER_PRIVATE_HISTORY_")]
    observed_requests = json.dumps([e.get("body") for e in dispatched], ensure_ascii=False)
    tests["provider_history_canary_absent_from_observed_upstream"] = bool(canaries and dispatched) and all(marker not in observed_requests for marker in canaries)
    return {"kind": "two-physical-host-development-evidence-summary", "formal_trial_registered": False,
        "grading_activation": "pending", "checks": {k: "passed" if v else "not_demonstrated" for k, v in tests.items()},
        "tool_call_rounds_by_task": rounds, "upstream_dispatch_count": len(dispatched),
        "manual_review_required": ["signed task destination and recipient binding against saved task",
            "two consumer markers isolated across interleaved sessions and followups",
            "unauthorized, cross-model and revoked probes rejected with zero matching upstream dispatch",
            "Agent off/on manual sharing, disconnect/reconnect and persistence after restart",
            "same-code wheel provenance and actual host/process attestations",
            "tool outcomes and final answer semantic correctness"],
        "limits": "Absence applies only to captured requests/snapshots. No arbitrary natural-language verdict. No formal acceptance count."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("provider-snapshot", "consumer-snapshot", "provider-audit", "consumer-audit", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    def events(path):
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    result = check(read(args.provider_snapshot), read(args.consumer_snapshot), events(args.provider_audit), events(args.consumer_audit))
    result["input_sha256"] = {p: digest(p) for p in (args.provider_snapshot, args.consumer_snapshot, args.provider_audit, args.consumer_audit)}
    write(args.out, result)
    print(json.dumps({"saved": args.out, "checks": result["checks"]}))


if __name__ == "__main__":
    main()
