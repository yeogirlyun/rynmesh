"""Owner-only optimistic control, idempotency and safe history closure."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from .store import AgentError, encoded

# A reconciliation holds owner input only while it can still finish. Local
# commands never produce a completion receipt, so an "unknown" result must not
# lock the conversation: the owner may continue and the model is told.
RECONCILIATION_PENDING_SECONDS = 60
RECONCILIATION_RUNNING_SECONDS = 120


def check_version(row, value, *, goal=False):
    key = "expected_control_revision" if "expected_control_revision" in value else "expected_revision"
    current = row["control_revision"] if key == "expected_control_revision" else row["revision"]
    if type(value.get(key)) is not int or value[key] != current:
        raise AgentError("agent_control_revision_conflict" if key == "expected_control_revision" else "agent_revision_conflict")
    if goal and (type(value.get("expected_goal_revision")) is not int or value["expected_goal_revision"] != row["goal"]["revision"]):
        raise AgentError("agent_goal_revision_conflict")


def request_replayed(row, value):
    request_id = value.get("client_request_id")
    if request_id is None:
        return False
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except ValueError:
        raise AgentError("agent_invalid_request") from None
    prior = row.get("control_requests", {}).get(request_id)
    fingerprint = hashlib.sha256(encoded(value)).hexdigest()
    if prior and prior != fingerprint:
        raise AgentError("agent_request_conflict")
    if not prior and len(row.get("control_requests", {})) >= 256:
        raise AgentError("agent_task_limit")
    return prior is not None


def remember_request(row, value):
    if value.get("client_request_id"):
        row.setdefault("control_requests", {})[value["client_request_id"]] = hashlib.sha256(encoded(value)).hexdigest()


def recoverable_read_in_flight(row):
    from .execution_policy import verified_read_classification
    flight = row.get("in_flight") or {}
    if not flight:
        return False
    if flight.get("effect") == "read":
        return True
    return verified_read_classification(
        flight,
        task_id=row.get("id", ""),
        workspace_root=(row.get("local_execution") or {}).get("workspace_root", ""),
    )


def ambiguous(row):
    from .execution_policy import verified_read_classification
    flight = row.get("in_flight") or {}
    if row.get("error") == "agent_write_outcome_unknown":
        return True
    if row["status"] == "interrupted" and flight.get("effect") == "write" and flight.get("dispatched"):
        return not verified_read_classification(
            flight,
            task_id=row.get("id", ""),
            workspace_root=(row.get("local_execution") or {}).get("workspace_root", ""),
        )
    return False


def _age_seconds(stamp, current):
    if not isinstance(stamp, str):
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (current - then).total_seconds()


def reconciliation_active(row, current=None):
    """True only while a pending/running reconciliation is fresh enough to finish."""
    record = row.get("reconciliation") or {}
    state = record.get("state")
    if state not in {"pending", "running"}:
        return False
    age = _age_seconds(record.get("started_at" if state == "running" else "requested_at"),
                       current or datetime.now(timezone.utc))
    limit = RECONCILIATION_RUNNING_SECONDS if state == "running" else RECONCILIATION_PENDING_SECONDS
    return age is not None and 0 <= age < limit


def acknowledgeable(row, current=None):
    """An unconfirmed write the owner may acknowledge by continuing the conversation."""
    return ambiguous(row) and not reconciliation_active(row, current)


def owner_removable(row, current=None):
    """The owner may archive or delete any task that is not working right now.

    Removing a record never repeats a write. Automatic archiving keeps its
    stricter rule (lifecycle.eligible).
    """
    status, flight = row["status"], row.get("in_flight")
    if ambiguous(row):
        return not reconciliation_active(row, current)
    if status == "interrupted" and flight and not row.get("queue") and recoverable_read_in_flight(row):
        return True  # A read cut short by a restart changes nothing; its record may go.
    return (not flight and not row.get("queue")
            and status in {"completed", "failed", "cancelled", "paused", "interrupted"})


def allowed_actions(row, settings):
    status, flight = row["status"], row.get("in_flight")
    enabled = settings.get("enabled", False)
    uncertain = ambiguous(row)
    # Sending a message, switching model, archiving or deleting never repeats
    # the unconfirmed write; only a fresh reconciliation holds them back.
    held = uncertain and reconciliation_active(row)
    continuable = uncertain and not held
    recoverable_read = status == "interrupted" and recoverable_read_in_flight(row) and not uncertain
    active = status in {"queued", "running", "waiting_approval", "waiting_input"}
    mutable = enabled and (continuable or (not uncertain and (status in {"queued", "running", "waiting_approval", "waiting_input", "paused", "completed", "failed"}
                                                              or (status == "interrupted" and (not flight or recoverable_read))
                                                              or (status == "cancelled" and not flight))))
    possible = {"pause": active, "stop": active, "cancel": status not in {"completed", "failed", "cancelled"},
                "resume": enabled and status in {"paused", "interrupted"} and (not flight or recoverable_read) and not uncertain,
                "approve": enabled and status == "waiting_approval" and bool(row["pending_approval"]) and "read_only" not in {row["permission_mode"], settings["permission_mode"]},
                "deny": enabled and status == "waiting_approval" and bool(row["pending_approval"]),
                "answer": enabled and status == "waiting_input" and bool(row.get("pending_input")) and row["pending_input"]["kind"] != "credential_ref",
                "steer": mutable, "information": mutable, "scope": not uncertain and status != "cancelled",
                "reconcile": enabled and ((uncertain and row.get("reconciliation", {}).get("attempts", 0) < 3) or recoverable_read)
                             and row.get("reconciliation", {}).get("state") not in {"pending", "running"}}
    safe_lifecycle = not flight and not row.get("queue") and not uncertain and status in {"completed", "failed", "cancelled", "paused"}
    removable = owner_removable(row)
    possible.update(rename=True, archive=removable and not row.get("archived"), restore=bool(row.get("archived")), delete=removable)
    possible["budget"] = safe_lifecycle and status in {"paused", "completed", "failed"}
    possible["source_credential"] = enabled and not flight and not uncertain and status in {"queued", "waiting_input", "paused", "completed", "failed"}
    possible["model"] = continuable or (not flight and not uncertain and status in {"paused", "completed", "failed", "waiting_input", "waiting_approval", "interrupted", "cancelled"}
                                        and (not row.get("pending_control") or (status == "cancelled" and row["pending_control"].get("kind") == "cancel")))
    possible["edit_message"] = enabled and not uncertain and status != "interrupted" and not row.get("pending_user_messages")
    if (row.get("fork") or {}).get("awaiting_instruction"):
        possible["resume"] = False
        possible["edit_message"] = False
    if row.get("archived"):
        possible = {action: allowed if action in {"rename", "restore", "delete"} else False for action, allowed in possible.items()}
    return {action: {"enabled": bool(allowed), "requires_control_revision": action not in {"pause", "stop", "cancel"},
                     "reason": None if allowed else "agent_reconciliation_running" if held else "agent_operation_unresolved" if uncertain else "agent_disabled" if not enabled and action not in {"pause", "stop", "cancel"} else "agent_invalid_transition"}
            for action, allowed in possible.items()}


def close_queue(row, emit, reason, *, keep_flight=True):
    flight_id = (row.get("in_flight") or {}).get("call_id") if keep_flight else None
    # Before dispatch marking the initial claim has no call_id yet.
    if keep_flight and (row.get("in_flight") or {}).get("kind") == "tool" and not flight_id and row["queue"]:
        flight_id = row["queue"][0]["call_id"]
    retained = []
    for call in row["queue"]:
        if call["call_id"] == flight_id:
            retained.append(call)
            continue
        result = {"error": reason, "not_executed": True, "no_mutation": True}
        row["messages"].append({"role": "tool", "tool_call_id": call["call_id"], "content": encoded(result).decode(),
                                "context_epoch": call.get("context_epoch", row.get("context_epoch", 0)), "is_error": True})
        emit(row, "tool_result", tool=call["name"], call_id=call["call_id"], operation_id=call["operation_id"], result=result,
             internal=call.get("internal", False), context_epoch=call.get("context_epoch", row.get("context_epoch", 0)))
    row["queue"] = retained


def settle(row, emit, data=None):
    """Called after an old operation has a known response and clears its flight."""
    control = row.get("pending_control")
    if control and control["kind"] in {"pause", "stop", "cancel"}:
        from . import retry_policy
        retry_policy.stop(row)
    if not control or row.get("in_flight"):
        return
    if control["kind"] != "pause":
        row.pop("verification", None)
        close_queue(row, emit, "agent_owner_changed" if control["kind"] in {"steer", "scope", "permissions"} else "agent_cancelled", keep_flight=False)
    if control["kind"] == "edit_message":
        from .conversation import rewrite
        rewrite(row, control, emit, data=data)
        row.pop("pending_control", None)
        return
    pending_users = row.pop("pending_user_messages", [])
    row["messages"].extend(pending_users)
    if pending_users and isinstance(data, dict):
        from . import memory_integration
        memory_integration.apply_owner_turn_memory_in_tx(None, data, row)
    if control["kind"] in {"steer", "scope", "permissions"}:
        row["status"] = "paused" if control.get("remain_paused") else "queued"
        row["error"] = None
        from . import retry_policy
        retry_policy.reset(row)
    if control["kind"] in {"pause", "stop", "cancel"}:
        stop_type = "paused" if control["kind"] in {"pause", "stop"} else "cancelled"
        stopped = next((item for item in reversed(row["events"]) if item["type"] == stop_type), {})
        if stopped.get("operation_in_flight") is True:
            # The stop intent precedes its final accounting. Persist the settled
            # clock before a later owner turn can reset the current run counter.
            emit(row, "run_settled", reason=control["kind"], operation_in_flight=False)
    row.pop("pending_control", None)
