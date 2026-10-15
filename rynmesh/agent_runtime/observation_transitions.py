"""Registered, attributable changes to historical preparation observations.

Only atomic friend sharing currently supplies an exact field transition. Other
writes and readers retain strict comparison with their original observation.
"""
from __future__ import annotations

import copy
import hashlib

from ..llm_package import model_settings
from .store import encoded

WRITER = "ryn_share_model_with_friend"
READERS = {"ryn_list_models", "ryn_inspect_services"}
VERSION = "atomic-sharing-observation-v1"
MAX_TRANSITION_BYTES = 24 * 1024
FIELDS = ("sharing", "share_scope", "rule_revision", "model_settings")


def model_fields(sources, record, model):
    policy = model_settings.public(record, model)
    return {"sharing": model in sources.shared_models(record), "share_scope": policy["effective"]["share_scope"],
            "rule_revision": policy["rule_revision"], "model_settings": policy}


def dependencies(receipt, current, retained_ids):
    """Only strictly later operations in the same server context can explain a read."""
    if receipt["tool"] not in READERS or receipt.get("status") != "confirmed":
        return []
    index = next(i for i, item in enumerate(current) if item["id"] == receipt["id"])
    later = [item for item in current[index + 1:]
            if item["tool"] == WRITER and item.get("effect") == "write"
            and item.get("status") == "confirmed"
            and all(item.get(k) == receipt.get(k) for k in ("goal_revision", "scope_revision", "context_epoch"))
            and receipt.get("confirmed_at") and item.get("started_at")
            and receipt["confirmed_at"] <= item["started_at"]]
    counts = {}
    for item in later:
        key = item.get("arguments", {}).get("model_id")
        counts[key] = counts.get(key, 0) + 1
    return [item["id"] for item in later if item["id"] in retained_ids and counts[item["arguments"].get("model_id")] == 1]


def context(row, criterion):
    """Build the dependency projection from this verification cycle, never tool data."""
    output = []
    for rid in criterion.get("observation_dependencies", []):
        receipt = next((r for r in row["receipts"] if r["id"] == rid), None)
        check = next((c for c in row["criteria"] if c.get("source_ref") == rid and c.get("source") == "tool"), None)
        if not receipt or not check or check.get("status") != "passed":
            continue
        evidence = next((e for e in row.get("verification_evidence", [])
                         if e["id"] in check.get("evidence_refs", []) and e.get("receipt_id") == rid
                         and e.get("criterion_id") == check["id"] and e.get("status") == "passed"
                         and e.get("goal_revision") == row["goal"]["revision"]
                         and e.get("scope_revision") == row["scope_revision"]), None)
        if evidence:
            output.append({"receipt": copy.deepcopy(receipt), "evidence_id": evidence["id"]})
    return output


def expected(tools, receipt, prior, *, context, cancelled):
    """Patch only co-committed before/after fields; all other facts remain strict."""
    result = copy.deepcopy(prior)
    applied = []
    if receipt["tool"] not in READERS:
        return result, applied
    candidates = (context or {}).get("observation_dependencies", [])
    # Multiple changes to one target require a fresh observation for now. Do not
    # guess a chain or use only its latest link to hide an intermediate drift.
    counts = {}
    for candidate in candidates:
        key = candidate["receipt"].get("arguments", {}).get("model_id")
        counts[key] = counts.get(key, 0) + 1
    for candidate in candidates:
        writer = candidate["receipt"]
        model_id = writer.get("arguments", {}).get("model_id")
        if (writer.get("tool") != WRITER or writer.get("status") != "confirmed" or counts[model_id] != 1
                or any(writer.get(k) != receipt.get(k) for k in ("goal_revision", "scope_revision", "context_epoch"))
                or not receipt.get("confirmed_at") or not writer.get("started_at")
                or receipt["confirmed_at"] > writer["started_at"]):
            continue
        # Revalidate current state AND exact durable attribution at the point of
        # use; a prior passed check or forged result is insufficient on its own.
        transition = tools.friend_sharing.observation_transition(writer, context=context, cancelled=cancelled)
        if not transition or transition.get("version") != VERSION:
            continue
        # Match the reader's credential-safe projection, without relaxing its
        # sanitizer or exposing private receipt metadata to the provider.
        from .tools import bounded_result
        transition = bounded_result(transition)
        if transition.get("truncated"):
            continue
        matches = [row for row in result.get("models", []) if row.get("id") == model_id]
        if len(matches) != 1 or any(matches[0].get(k) != transition["before"][k] for k in FIELDS):
            continue
        sid = writer["result"]["service_id"]
        if receipt["tool"] == "ryn_inspect_services":
            old = sorted((r for r in result.get("friend_grants", []) if r.get("service_id") == sid), key=encoded)
            if old != sorted(transition["before_grants"], key=encoded):
                continue
            result["friend_grants"] = [r for r in result["friend_grants"] if r.get("service_id") != sid] + copy.deepcopy(transition["after_grants"])
        matches[0].update(copy.deepcopy(transition["after"]))
        applied.append({"receipt_id": writer["id"], "evidence_id": candidate["evidence_id"], "model_id": model_id,
                        "changed_fields": list(FIELDS) + (["friend_grants:" + sid] if receipt["tool"] == "ryn_inspect_services" else [])})
    return result, applied


def compare(tools, receipt, prior, fresh, *, context, cancelled):
    expected_value, applied = expected(tools, receipt, prior, context=context, cancelled=cancelled)
    actual = copy.deepcopy(fresh)
    if applied and receipt["tool"] == "ryn_inspect_services":
        # Grant rows are a keyed collection; membership and every field remain
        # exact. Migration from the legacy grant file may change iteration order.
        expected_value["friend_grants"].sort(key=encoded)
        actual["friend_grants"].sort(key=encoded)
    provenance = ({"historical_observation_sha256": hashlib.sha256(encoded(prior)).hexdigest(),
                   "observation_transitions": applied,
                   "comparison_basis": "Original observation plus verified exact committed changes; every other fact unchanged."}
                  if applied else {})
    return expected_value == actual, provenance
