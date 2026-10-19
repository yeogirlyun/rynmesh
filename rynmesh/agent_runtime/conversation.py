"""Owner-authored conversation branches without replaying a discarded future."""
from __future__ import annotations

import copy

from . import budget


def rewrite(row: dict, control: dict, emit) -> None:
    """Apply an edit only after the old operation has a known outcome.

    Receipts and events remain the durable audit trail. The previous suffix is
    encrypted with the task, but never becomes input to the replacement turn.
    The valid retained prefix keeps native signatures required by some providers;
    the runtime uses a fresh conversation key for the replacement branch.
    """
    index, replacement = control["message_index"], control["message"]
    messages = row["messages"]
    row.setdefault("conversation_branches", []).append({
        "revision": row.get("conversation_revision", 0),
        "message_index": index,
        "event_cursor": row["event_cursor"],
        "messages": copy.deepcopy(messages[index:]),
        "goal": copy.deepcopy(row["goal"]),
    })
    previous_epoch = row.get("context_epoch", 0)
    epoch = previous_epoch + 1
    row["context_epoch"] = epoch
    row["conversation_revision"] = row.get("conversation_revision", 0) + 1
    row["messages"] = [*messages[:index], replacement]
    replacement["context_epoch"] = epoch
    for message in row["messages"]:
        # Keep prior model/scope exclusions closed. Only the active prefix
        # becomes replacement context; response provenance remains immutable.
        if message["role"] == "user" or message.get("context_epoch", 0) == previous_epoch:
            message.setdefault("original_context_epoch", message.get("context_epoch", 0))
            message["context_epoch"] = epoch
    for key in ("context_checkpoint", "answer_continuation", "verification", "verification_evidence",
                "criteria_proposals", "rejected_response_note", "model_retry_authority", "tool_menu",
                "project_inspection", "pending_user_messages", "turn_stopped"):
        row.pop(key, None)
    row["observed_files"] = {}
    row.setdefault("goal_history", []).append(copy.deepcopy(row["goal"]))
    owners = [message for message in row["messages"] if message["role"] == "user"]
    revision = row["goal"]["revision"] + 1
    first = owners[0]
    old_constraints = {item.get("message_id") for item in row["goal"].get("constraints", [])}
    row["goal"] = {
        "revision": revision, "kind": "unclassified", "original_message_id": first["message_id"],
        "text": first["content"],
        "constraints": [
            {"id": "constraint:" + message["message_id"], "text": message["content"],
             "source": "user", "message_id": message["message_id"], "required": True}
            for message in owners if message["message_id"] in old_constraints or message is replacement
        ],
    }
    row["criteria"] = [{
        "id": "goal-coverage", "goal_revision": revision, "source": "user", "source_ref": first["message_id"],
        "required": True, "kind": "semantic", "description": "Satisfy the owner's complete current goal and constraints.",
        "verifier": None, "status": "pending", "evidence_refs": [],
    }]
    row["criteria_proposals"] = []
    row["plan"] = {"revision": row["plan"]["revision"] + 1, "goal_revision": revision, "steps": []}
    row["outcome"] = {"status": "unverified", "goal_revision": revision, "criteria_results": [], "summary": "",
                      "evidence_refs": [], "uncertainties": ["Current owner requirements have not been independently verified."]}
    budget.new_round(row)
    row.update(status="paused" if control.get("remain_paused") else "queued", error=None, result="", steps=0,
               elapsed_seconds=0.0, model_retry_count=0, approved=None, pending_approval=None, pending_input=None)
    emit(row, "message_edited", message_index=index, message_id=replacement["message_id"],
         user_turn_index=len(owners) - 1, conversation_revision=row["conversation_revision"], goal_revision=revision)
    row["conversation_event_cursor"] = row["event_cursor"]
