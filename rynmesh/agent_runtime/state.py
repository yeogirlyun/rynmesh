"""Authoritative task state and conservative encrypted-payload migration.

Model proposals never author authority fields. Only trusted runtime entry points
construct goal/criterion sources; business verification is a separate concern.
"""
from __future__ import annotations

import copy

SCHEMA_VERSION = 2
CONTROL_EVENTS = {"created", "workspace_bound", "message_added", "approval_requested", "approved", "denied", "paused",
                  "resumed", "cancelled", "goal_changed", "information_added", "input_requested", "input_answered", "scope_changed", "permissions_changed", "reconciliation_requested", "archived", "restored", "budget_changed", "source_credential_saved", "source_credential_revoked", "model_changed"}
CONTROL_EVENTS.add("approval_no_longer_required")
CONTROL_EVENTS.add("connection_paused")
CONTROL_EVENTS.update({"message_edit_requested", "message_edited"})
PUBLIC_STATE_FIELDS = ("schema_version", "goal", "criteria", "plan", "control_revision", "event_cursor",
                       "run_state", "pending_input", "outcome", "scope_revision")


def owner_message_id(row: dict, index: int) -> str:
    return f"{row['id']}:owner:{index}"


def initialize(row: dict, *, legacy: bool = False) -> None:
    owners = []
    for index, message in enumerate(row["messages"]):
        if message.get("role") == "user":
            message.setdefault("message_id", owner_message_id(row, index))
            owners.append(message)
    original = owners[0] if owners else {"message_id": None, "content": ""}
    row.update(schema_version=SCHEMA_VERSION, control_revision=0, event_cursor=max((e.get("seq", 0) for e in row["events"]), default=0),
               goal={"revision": 1, "kind": "unclassified", "original_message_id": original["message_id"],
                     "text": original["content"], "constraints": [
                         {"id": "constraint:" + message["message_id"], "text": message["content"], "source": "user",
                          "message_id": message["message_id"], "required": True} for index, message in enumerate(owners)]},
               criteria=[{"id": "goal-coverage", "goal_revision": 1, "source": "user", "source_ref": original["message_id"],
                          "required": True, "kind": "semantic", "description": "Satisfy the owner's complete current goal and constraints.",
                          "verifier": None, "status": "pending", "evidence_refs": []}],
               plan={"revision": 0, "goal_revision": 1, "steps": []}, criteria_proposals=[],
               pending_input=None, scope_revision=1, scope={"resource_grants": [], "egress_grants": []},
               observed_files={},
               outcome={"status": "unverified", "goal_revision": 1, "criteria_results": [],
                        "summary": "", "evidence_refs": [], "uncertainties": ["Independent goal verification is not available."]})
    if legacy:
        row["control_revision"] = 1
        row["migration"] = {"from_schema": 1, "prior_status": row["status"]}
        if row.get("pending_approval") or row.get("approved"):
            row["migration"]["approval_invalidated"] = True
            row.update(pending_approval=None, approved=None)
            if row["status"] == "waiting_approval":
                row["status"] = "queued"
    synchronize(row)


def synchronize(row: dict) -> None:
    row["run_state"] = ({"completed": "ended", "failed": "ended", "cancelled": "ended",
                         "interrupted": "reconciling" if row.get("error") == "agent_write_outcome_unknown" else "paused"}
                        .get(row["status"], row["status"]))
    if row.get("pending_control") and row.get("in_flight") and row["status"] != "cancelled":
        row["run_state"] = "pausing"
    if row.get("error") == "agent_write_outcome_unknown":
        row["outcome"].update(status="unknown", uncertainties=["A dispatched write has no confirmed outcome."])


def migrate(data: dict) -> dict:
    version = data.get("schema_version", 1)
    revision = data.setdefault("settings_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid Agent settings revision")
    if type(version) is not int or version not in {1, SCHEMA_VERSION}:
        raise ValueError("unsupported Agent payload schema")
    for row in data["tasks"].values():
        row_version = row.get("schema_version", 1)
        if type(row_version) is not int or row_version not in {1, SCHEMA_VERSION}:
            raise ValueError("unsupported Agent task schema")
        if row_version == 1:
            initialize(row, legacy=True)
        else:
            for required in ("goal", "criteria", "plan", "control_revision", "event_cursor", "pending_input", "outcome", "scope_revision", "scope"):
                if required not in row:
                    raise ValueError("incomplete Agent task state")
            row.setdefault("observed_files", {})
            synchronize(row)
    # Numeric defaults and owner overrides were saved in the same field. Neither
    # 120000, 600000 nor the old migration marker proves automatic intent. Keep
    # ambiguous numeric settings/task limits, and label that uncertainty. Only
    # an explicit automatic provenance can retire a former numeric default.
    def context_mode(container, limits, key):
        provenance = container.get(key)
        if provenance in {"default_auto", "owner_auto"}:
            limits["max_context_chars"] = None
        elif "max_context_chars" not in limits or limits["max_context_chars"] is None:
            limits["max_context_chars"] = None
            provenance = "default_auto"
        elif provenance != "owner_manual":
            provenance = "legacy_ambiguous_manual"
        container[key] = provenance
    context_mode(data, data["settings"], "settings_context_budget_provenance")
    for row in data["tasks"].values():
        context_mode(row, row.setdefault("limits", {}), "context_budget_provenance")
    from . import memory
    memory.ensure_memory_state(data)
    from . import automations
    automations.ensure_automation_state(data)
    data["context_budget_version"] = 2
    data["schema_version"] = SCHEMA_VERSION
    return data


def owner_update(row: dict, message: dict) -> None:
    """Called only after the authenticated owner entry point validates input."""
    row.setdefault("goal_history", []).append(copy.deepcopy(row["goal"]))
    goal = row["goal"]
    goal["revision"] += 1
    goal["constraints"].append({"id": "constraint:" + message["message_id"], "text": message["content"], "source": "user",
                                "message_id": message["message_id"], "required": True})
    for criterion in row["criteria"]:
        criterion.update(goal_revision=goal["revision"], status="pending", evidence_refs=[])
    row["plan"] = {"revision": row["plan"]["revision"] + 1, "goal_revision": goal["revision"], "steps": []}
    row["criteria_proposals"] = []
    row["outcome"] = {"status": "unverified", "goal_revision": goal["revision"], "criteria_results": [],
                      "summary": "", "evidence_refs": [], "uncertainties": ["Current owner requirements have not been independently verified."]}
