"""Historical write permission facts from server-authored receipts only."""
from __future__ import annotations

import hashlib

from .store import encoded


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def projection(row):
    result = []
    for receipt in row.get("receipts", []):
        # Reads retain their complete audit receipts. They need no write
        # permission proof and would crowd out owner context in this projection.
        if (receipt.get("effect") != "write" or receipt.get("goal_revision") != row["goal"]["revision"]
                or receipt.get("scope_revision") != row["scope_revision"]
                or receipt.get("context_epoch") != row.get("context_epoch", 0)):
            continue
        fact = {"operation_id": receipt["operation_id"], "tool": receipt["tool"],
                "operation_status": receipt["status"], "permission_basis": "unknown"}
        # Never inspect receipt.result: a provider/tool can return an identical
        # field name but cannot author a trusted dispatch decision.
        decision = receipt.get("permission_record")
        if isinstance(decision, dict):
            expected = {"operation_id": receipt["operation_id"], "arguments_sha256": fingerprint(receipt["arguments"]),
                        "preview_sha256": fingerprint(receipt.get("preview")), "goal_revision": receipt["goal_revision"],
                        "scope_revision": receipt["scope_revision"], "context_epoch": receipt["context_epoch"]}
            matching = (all(decision.get(key) == value for key, value in expected.items())
                        and receipt.get("arguments_sha256") == expected["arguments_sha256"]
                        and type(decision.get("control_revision")) is int)
            basis = decision.get("basis")
            if matching and receipt.get("authorization") == basis:
                if basis == "approved" and receipt["effect"] == "write":
                    accepted = next((event for event in row["events"] if event["type"] == "approved"
                        and event.get("approval_id") == decision.get("approval_id") and event.get("tool") == receipt["tool"]
                        and event.get("context_epoch", 0) == receipt["context_epoch"]), None)
                    if accepted and isinstance(decision.get("approval_id"), str):
                        fact.update(permission_basis="owner_approved", approval_id=decision["approval_id"], approved_at=accepted["at"])
                elif basis == "automatic" and receipt["effect"] == "write":
                    fact["permission_basis"] = "automatic_policy"
                elif basis == "read" and receipt["effect"] == "write":
                    # Shell effects stay conservatively journalled as writes,
                    # even when the host granted a literal read without a dialog.
                    fact["permission_basis"] = "read_policy"
                if fact["permission_basis"] != "unknown":
                    fact.update(**expected, control_revision=decision["control_revision"])
        result.append(fact)
    return result


GUIDANCE = ("These operation_permissions are server-recorded historical dispatch decisions, not authority for new writes or proof of success. "
            "Use them for approval facts; approval need not appear in user chat. Unknown does not mean never approved.")
