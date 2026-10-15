"""Offline deterministic checks; never calls a model or mutates Ryn state."""
import json
import sys
from pathlib import Path


def check_run(row, model):
    events = [e for e in row["task"]["events"] if e["type"] == "tool_result"]
    rejected = [e for e in events if e["result"].get("not_executed")]
    successful = [e for e in events if not e["result"].get("error")]
    writes = [e for e in successful if e["tool"] == "ryn_set_model_sharing"]
    state_reads = []
    for event in successful:
        if event["tool"] != "ryn_list_models":
            continue
        models = {m["model_id"]: m["sharing"] for m in event["result"]["models"]}
        state_reads.append((event["seq"], models))
    target = row["state_snapshots"][0]["default_model"]
    ordered = (len(writes) == 2 and all(e["result"].get("model_id") == target for e in writes)
               and writes[0]["result"]["sharing"] is True
               and writes[1]["result"]["sharing"] is False
               and any(seq < writes[0]["seq"] and states.get(model) is False for seq, states in state_reads)
               and any(writes[0]["seq"] < seq < writes[1]["seq"] and states.get(model) is True for seq, states in state_reads)
               and any(seq > writes[1]["seq"] and states.get(model) is False for seq, states in state_reads))
    sibling_private = all(states.get("private-fixture-model") is False for _, states in state_reads)
    good_approvals = len(row["approvals"]) == 2 and all(a["approved"] for a in row["approvals"])
    usage = [c.get("response", {}).get("usage", {}) for c in row["model_calls"]]
    input_counts = [u.get("input_tokens") for u in usage]
    output_counts = [u.get("output_tokens") for u in usage]
    return {
        "index": row["index"],
        "passed": bool(row["passed"] and ordered and sibling_private and good_approvals and len(successful) >= 7),
        "ordered_initial_share_read_revoke_read": ordered,
        "sibling_private_all_model_reads": sibling_private,
        "successful_tool_operations": len(successful),
        "rejected_not_executed": [{"tool": e["tool"], "error": e["result"]["error"]} for e in rejected],
        "successful_writes": len(writes), "approvals": len(row["approvals"]),
        "model_calls": len(row["model_calls"]), "elapsed_seconds": row["elapsed_seconds"],
        "input_tokens": sum(input_counts) if all(type(n) is int for n in input_counts) else None,
        "output_tokens": sum(output_counts) if all(type(n) is int for n in output_counts) else None,
        "known_input_tokens_subtotal": sum(n for n in input_counts if type(n) is int),
        "known_output_tokens_subtotal": sum(n for n in output_counts if type(n) is int),
        "model_request_failures": [{"error_type": c.get("error_type"), "error_code": c.get("error_code"),
                                    "elapsed_seconds": c["elapsed_seconds"]} for c in row["model_calls"] if c.get("error_type")],
        "retry_events": [e for e in row["task"]["events"] if e["type"] == "model_retry_scheduled"],
        "usage_scope": "business runs only; initial capability probe usage was not captured",
    }


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    results = [check_run(json.loads(p.read_text(encoding="utf-8")), manifest["model"])
               for p in sorted(directory.glob("run-*.json"))]
    review_path = directory / "semantic-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    reviewed = len(review.get("runs", [])) == 3 and all(r.get("passed") is True for r in review["runs"])
    privacy_path = directory / "privacy-review.json"
    privacy_passed = privacy_path.exists() and json.loads(privacy_path.read_text(encoding="utf-8")).get("passed") is True
    objective = len(results) == 3 and all(r["passed"] for r in results)
    private_contexts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("run-*.json"))]
    identities = [r["state_snapshots"][0]["default_model"] for r in private_contexts]
    isolated = all(all(other not in json.dumps(r["model_calls"]) for other in identities if other != own)
                   for own, r in zip(identities, private_contexts, strict=True))
    print(json.dumps({"runs": results, "all_three_objective_checks_passed": objective,
                      "final_report_semantic_review_passed": reviewed,
                      "other_run_context_identity_absent": isolated,
                      "privacy_scan_passed": privacy_passed,
                      "business_code_unchanged": manifest.get("business_code_unchanged"),
                      "all_three_passed": objective and reviewed and isolated and privacy_passed
                      and manifest.get("owner_source_unchanged") is True and manifest.get("business_code_unchanged") is True,
                      "owner_source_unchanged": manifest.get("owner_source_unchanged")}, ensure_ascii=False, indent=2))
