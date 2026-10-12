"""Narrow facts established by fresh reads, separate from whole-goal coverage."""
from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timezone

from . import observation_transitions
from .diagnostics import NAMES as DIAGNOSTIC_NAMES
from .store import AgentToolRejected, encoded

READ_SPECS = [
    {"id": name + "_facts", "tool_name": name, "kind": "mechanical", "effect": "read",
     "description": description, "target_fields": fields}
    for name, description, fields in [
        ("ryn_list_models", "Compare observer-visible catalog facts; no complete remote inventory, policy or invocation proof.", []),
        ("ryn_inspect_services", "Compare visible catalog and local outgoing grant observations within their provider/recipient scope.", []),
        ("ryn_list_devices", "Recheck device presence evidence; unknown connectivity remains unknown.", []),
        ("ryn_list_resource_roots", "Revalidate the current task's authorized root facts.", []),
        ("ryn_list_resources", "Check only the directory entries in this observed page, not recursive or complete directory coverage.", ["resource_ref", "cursor"]),
        ("ryn_read_resource", "Re-read this authorized text fragment and compare its version, range and content.", ["resource_ref", "cursor"]),
        ("ryn_search_resource", "Repeat the literal search and verify these bounded excerpts, offsets and source version.", ["resource_ref", "query", "case_sensitive", "cursor"]),
        ("ryn_list_source_configurations", "Re-read the safe source configuration projection, never credentials or inferred connectivity.", []),
        ("ryn_get_agent_configuration", "Re-read actual saved Agent preferences and default model configuration.", []),
        ("ryn_get_system_diagnostics", "Recheck current catalog and device evidence classifications; unknown reachability stays unknown.", []),
        ("ryn_diagnose_current_task", "Recheck this task's safe state/error classification; counters are live observations, not a goal-completion proof.", []),
        ("ryn_diagnose_task", "Recheck safe state, failure and dispatch metadata of the specified owner-node task.", ["task_id"]),
    ]
]
READ_NAMES = {s["tool_name"] for s in READ_SPECS}


def verify_read(tools, receipt, *, context, cancelled):
    stamp = datetime.now(timezone.utc).isoformat()
    name, args, prior = receipt["tool"], receipt["arguments"], receipt.get("result")
    if (receipt.get("status") != "confirmed" or not isinstance(prior, dict) or
            prior.get("error") and name not in DIAGNOSTIC_NAMES or
            prior.get("truncated") and "preview" in prior):
        return {"status": "unknown", "evidence": {"checked_facts": [], "coverage": "No complete confirmed read receipt.",
                "uncertainties": ["read_receipt_unavailable"]}, "checked_at": stamp}
    operation_id = "verification-read-" + uuid.uuid4().hex
    try:
        fresh = tools.execute(name, args, operation_id=operation_id, context=context, cancelled=cancelled)
    finally:
        # A verification read is never a reusable task operation receipt.
        with tools.lock:
            tools.completed.pop(operation_id, None)
    if cancelled():
        raise AgentToolRejected("agent_cancelled")
    evidence = {"operation_attribution": "read_only_observation", "uncertainties": []}
    if name in DIAGNOSTIC_NAMES:
        def stable(value):
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items() if k not in {"checked_at", "last_seen", "elapsed_seconds", "steps", "usage"}}
            if isinstance(value, list):
                return [stable(v) for v in value]
            return value
        matches = stable(prior) == stable(fresh)
        evidence.update(checked_facts=fresh, coverage="Current safe diagnostic classifications and registered configuration facts. Live elapsed/step/usage counters are observations, not compared as stable facts. Unknown or untested reachability is not connectivity proof; dispatch is not mutation proof.")
    elif name == "ryn_search_resource":
        fields = ("matches", "content_version", "total_characters", "search_from_character", "query", "case_sensitive", "extraction_truncated")
        matches = all(prior.get(k) == fresh.get(k) for k in fields)
        evidence.update(checked_facts={"content_version": fresh["content_version"], "total_characters": fresh["total_characters"],
            "match_ranges": [{k: v for k, v in m.items() if k != "text"} for m in fresh["matches"]], "query": fresh["query"]}, coverage=fresh["coverage"])
        if fresh.get("truncated"):
            evidence["uncertainties"].append("More search results or extraction truncation remain outside this response.")
    elif name == "ryn_read_resource":
        fields = ("text", "content_version", "character_offset", "characters_returned", "total_characters", "format", "extraction_truncated")
        matches = all(prior.get(k) == fresh.get(k) for k in fields)
        evidence.update(checked_facts={k: fresh.get(k) for k in fields if k != "text"},
                        coverage="Exact authorized fragment only. Hash/version stability does not establish that the entire document or its meaning was covered.")
        if prior.get("truncated"):
            evidence["uncertainties"].append("This fragment does not cover the whole document; later fragments require their own evidence.")
    elif name == "ryn_list_resources":
        def entries(value):
            return {encoded(tools.resources._open(e["resource_ref"])): {k: v for k, v in e.items() if k != "resource_ref"}
                    for e in value["entries"]}
        old_entries, new_entries = entries(prior), entries(fresh)
        matches = all(new_entries.get(key) == value for key, value in old_entries.items())
        evidence.update(checked_facts={"observed_entries_still_match": matches, "observed_entry_count": len(old_entries),
                                      "current_page_entry_count": len(new_entries)},
                        coverage="Only the named entries from the observed page. Additional entries may exist; this is not proof of complete directory contents, recursive search, or a transaction snapshot.")
    elif name == "ryn_list_devices":
        def stable(value):
            value = copy.deepcopy(value)
            for row in value.get("devices", []):
                row.pop("checked_at", None)
                row.pop("last_seen", None)
            return value
        matches = stable(prior) == stable(fresh)
        unknown = [row["peer_id"] for row in fresh.get("devices", []) if row.get("online") is None]
        evidence.update(checked_facts={"devices": fresh.get("devices", []), "membership": fresh.get("membership")},
                        coverage=fresh["evidence_note"])
        if unknown:
            evidence["uncertainties"].append({"presence_unknown_for": unknown})
        evidence["uncertainties"].append("Self presence proves only this executing process; unperformed HTTP/LAN probes provide no reachability evidence.")
        evidence["checked_facts"]["presence_classification_matches"] = matches
        evidence["coverage"] += " A passed check confirms the observed evidence classification, including unknown; it does not prove an unknown device is connected or offline."
    else:
        matches, provenance = observation_transitions.compare(tools, receipt, prior, fresh, context=context, cancelled=cancelled)
        evidence.update(provenance)
        evidence.update(checked_facts=fresh, coverage=("Current task-authorized roots only; no directory contents inspected." if name == "ryn_list_resource_roots" else
                        "Matching observed catalog/config/grants only in provider/recipient scope; no new inference, global absence or causal proof."))
    evidence.update(current_state_matches=matches, observation_sha256=hashlib.sha256(encoded(fresh)).hexdigest())
    return {"status": "passed" if matches else "failed", "evidence": evidence, "checked_at": stamp}
