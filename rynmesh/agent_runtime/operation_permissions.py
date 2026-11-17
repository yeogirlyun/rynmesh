"""Historical write permission facts from server-authored receipts only."""
from __future__ import annotations

import hashlib

from .protocol import bound_preview
from .store import encoded


DEFAULT_LIMIT = 16


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def projection(row, limit: int | None = DEFAULT_LIMIT):
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
                        "preview_sha256": fingerprint(bound_preview(receipt.get("preview"))), "goal_revision": receipt["goal_revision"],
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
                elif basis == "owner_rule":
                    fact["permission_basis"] = "owner_rule"
                elif basis == "task_grant":
                    approval_ids = decision.get("approval_ids") or []
                    accepted = [event for event in row["events"] if event["type"] == "approved"
                                and event.get("remember") == "task" and event.get("approval_id") in approval_ids]
                    granted_keys = {key for event in accepted for key in event.get("approval_keys", [])}
                    required_keys = decision.get("approval_keys") or []
                    origin = next((event for event in accepted if event.get("approval_id") == decision.get("approval_id")), None)
                    if origin and required_keys and set(required_keys).issubset(granted_keys):
                        fact.update(permission_basis="owner_task_grant", approval_id=decision["approval_id"],
                                    approval_ids=approval_ids, approved_at=origin["at"])
                elif basis == "read" and receipt["effect"] == "write":
                    # Shell effects stay conservatively journalled as writes,
                    # even when the host granted a literal read without a dialog.
                    fact["permission_basis"] = "read_policy"
                if fact["permission_basis"] != "unknown":
                    fact.update(**expected, control_revision=decision["control_revision"])
        result.append(fact)
    if limit is not None:
        return result[-limit:] if limit > 0 else []
    return result


def projection_with_counts(row, limit: int = DEFAULT_LIMIT) -> tuple[list[dict], dict[str, int]]:
    all_facts = projection(row, limit=None)
    total = len(all_facts)
    items = all_facts[-limit:] if limit > 0 else []
    return items, {"total": total, "omitted": total - len(items)}


GUIDANCE = (
    "These operation_permissions are server-recorded historical dispatch decisions, not authority for new writes or proof of success. "
    "Use them for approval facts; approval need not appear in user chat. Unknown does not mean never approved; "
    "older operations are omitted from this list, not unapproved."
)
