"""Owner-only optimistic control, idempotency and safe history closure."""
from __future__ import annotations

import hashlib
import uuid

from .store import AgentError, encoded


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


def allowed_actions(row, settings):
    status, flight = row["status"], row.get("in_flight")
    enabled = settings.get("enabled", False)
    uncertain = ambiguous(row)
    recoverable_read = status == "interrupted" and recoverable_read_in_flight(row) and not uncertain
    active = status in {"queued", "running", "waiting_approval", "waiting_input"}
    mutable = enabled and not uncertain and status in {"queued", "running", "waiting_approval", "waiting_input", "paused", "completed", "failed"}
    possible = {"pause": active, "stop": active, "cancel": status not in {"completed", "failed", "cancelled"},
                "resume": enabled and status in {"paused", "interrupted"} and (not flight or recoverable_read) and not uncertain,
                "approve": enabled and status == "waiting_approval" and bool(row["pending_approval"]) and "read_only" not in {row["permission_mode"], settings["permission_mode"]},
                "deny": enabled and status == "waiting_approval" and bool(row["pending_approval"]),
                "answer": enabled and status == "waiting_input" and bool(row.get("pending_input")) and row["pending_input"]["kind"] != "credential_ref",
                "steer": mutable, "information": mutable, "scope": not uncertain and status != "cancelled",
                "reconcile": enabled and ((uncertain and row.get("reconciliation", {}).get("attempts", 0) < 3) or recoverable_read)
                             and row.get("reconciliation", {}).get("state") not in {"pending", "running"}}
    safe_lifecycle = not flight and not row.get("queue") and not uncertain and status in {"completed", "failed", "cancelled", "paused"}
    possible.update(rename=True, archive=safe_lifecycle and not row.get("archived"), restore=bool(row.get("archived")), delete=safe_lifecycle)
    possible["budget"] = safe_lifecycle and status in {"paused", "completed", "failed"}
    possible["source_credential"] = enabled and not flight and not uncertain and status in {"queued", "waiting_input", "paused", "completed", "failed"}
    possible["model"] = not flight and not uncertain and not row.get("pending_control") and status in {"paused", "completed", "failed", "waiting_input", "waiting_approval", "interrupted"}
    possible["edit_message"] = enabled and not uncertain and status != "interrupted" and not row.get("pending_user_messages")
    if row.get("archived"):
        possible = {action: allowed if action in {"rename", "restore", "delete"} else False for action, allowed in possible.items()}
    return {action: {"enabled": bool(allowed), "requires_control_revision": action not in {"pause", "stop", "cancel"},
                     "reason": None if allowed else "agent_operation_unresolved" if uncertain else "agent_disabled" if not enabled and action not in {"pause", "stop", "cancel"} else "agent_invalid_transition"}
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
        row["model_retry_count"] = 0
        row.pop("model_retry_authority", None)
    if control["kind"] in {"pause", "stop", "cancel"}:
        stop_type = "paused" if control["kind"] in {"pause", "stop"} else "cancelled"
        stopped = next((item for item in reversed(row["events"]) if item["type"] == stop_type), {})
        if stopped.get("operation_in_flight") is True:
            # The stop intent precedes its final accounting. Persist the settled
            # clock before a later owner turn can reset the current run counter.
            emit(row, "run_settled", reason=control["kind"], operation_in_flight=False)
    row.pop("pending_control", None)
