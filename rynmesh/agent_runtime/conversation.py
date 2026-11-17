"""Owner-authored conversation branches without replaying a discarded future."""
from __future__ import annotations

import copy
import hashlib
import sys
from typing import Any

from . import budget, diagnostic_log, lifecycle
from .state import record_goal_history
from .store import AgentError, AgentStore, encoded

BRANCH_METADATA_LIMIT = 20
_BRANCH_META_KEYS = (
    "revision",
    "message_index",
    "event_cursor",
    "retired_count",
    "retired_sha256",
    "archive_ref",
)


def _enclosing_store() -> AgentStore | None:
    frame = sys._getframe(1)
    depth = 0
    while frame is not None and depth < 24:
        if frame.f_code.co_name == "transaction":
            candidate = frame.f_locals.get("self")
            if isinstance(candidate, AgentStore):
                return candidate
        frame = frame.f_back
        depth += 1
    return None


def _branch_archive_ref(revision: Any, retired_sha256: str) -> str:
    return f"branch:{revision}:{retired_sha256[:16]}"


def _build_branch_entry(
    *,
    revision: Any,
    message_index: Any,
    event_cursor: Any,
    retired_messages: list[dict],
    retired_goal: dict | None,
    can_archive: bool,
) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
    raw_messages = copy.deepcopy(retired_messages)
    retired_sha256 = hashlib.sha256(encoded(raw_messages)).hexdigest()
    archive_ref = _branch_archive_ref(revision, retired_sha256) if can_archive else None
    meta: dict[str, Any] = {
        "revision": revision,
        "message_index": message_index,
        "event_cursor": event_cursor,
        "retired_count": len(raw_messages),
        "retired_sha256": retired_sha256,
        "archive_ref": archive_ref,
    }
    if not can_archive or archive_ref is None:
        return meta, None
    payload: dict[str, Any] = {
        "revision": revision,
        "message_index": message_index,
        "event_cursor": event_cursor,
        "retired_count": len(raw_messages),
        "retired_sha256": retired_sha256,
        "messages": raw_messages,
    }
    if retired_goal is not None:
        payload["goal"] = copy.deepcopy(retired_goal)
    return meta, (archive_ref, payload)


def rewrite(row: dict, control: dict, emit, *, data: dict | None = None, store: Any = None) -> None:
    """Apply an edit only after the old operation has a known outcome.

    Receipts and events remain the durable audit trail. The previous suffix is
    archived in the task's encrypted archive store, and never becomes input to
    the replacement turn. The valid retained prefix keeps native signatures
    required by some providers; the runtime uses a fresh conversation key for
    the replacement branch.
    """
    from . import memory, memory_integration

    index, replacement = control["message_index"], control["message"]
    messages = row["messages"]
    previous_revision = row.get("conversation_revision", 0)
    retired_messages = copy.deepcopy(messages[index:])
    active_store = store if isinstance(store, AgentStore) else _enclosing_store()
    can_archive = active_store is not None and isinstance(row.get("id"), str) and bool(row["id"])

    normalized_branches: list[dict[str, Any]] = []
    to_archive: dict[str, dict[str, Any]] = {}
    newly_archived_metas: list[dict[str, Any]] = []
    for existing in row.get("conversation_branches") or []:
        if not isinstance(existing, dict):
            continue
        if "messages" in existing or "goal" in existing:
            legacy_messages = existing.get("messages") if isinstance(existing.get("messages"), list) else []
            legacy_goal = existing.get("goal") if isinstance(existing.get("goal"), dict) else None
            meta, pair = _build_branch_entry(
                revision=existing.get("revision", 0),
                message_index=existing.get("message_index", 0),
                event_cursor=existing.get("event_cursor", 0),
                retired_messages=legacy_messages,
                retired_goal=legacy_goal,
                can_archive=can_archive,
            )
            if pair is not None:
                to_archive[pair[0]] = pair[1]
                newly_archived_metas.append(meta)
            normalized_branches.append(meta)
        else:
            normalized_branches.append({key: existing.get(key) for key in _BRANCH_META_KEYS})

    new_meta, new_pair = _build_branch_entry(
        revision=previous_revision,
        message_index=index,
        event_cursor=row["event_cursor"],
        retired_messages=retired_messages,
        retired_goal=row.get("goal") if isinstance(row.get("goal"), dict) else None,
        can_archive=can_archive,
    )
    if new_pair is not None:
        to_archive[new_pair[0]] = new_pair[1]
        newly_archived_metas.append(new_meta)
    normalized_branches.append(new_meta)

    if can_archive and to_archive:
        try:
            lifecycle.archive_conversation_branches(active_store, row["id"], to_archive)
        except (AgentError, OSError, ValueError) as exc:
            for meta in newly_archived_metas:
                meta["archive_ref"] = None
            diagnostic_log.failure("conversation_branch_archive", exc, task_id=row.get("id"))

    row["conversation_branches"] = normalized_branches[-BRANCH_METADATA_LIMIT:]
    previous_epoch = row.get("context_epoch", 0)
    epoch = previous_epoch + 1
    row["context_epoch"] = epoch
    row["conversation_revision"] = previous_revision + 1
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
    if isinstance(data, dict):
        try:
            memory.record_branch_rewrite_fence(
                data,
                task_id=row["id"],
                previous_conversation_revision=previous_revision,
                new_conversation_revision=row["conversation_revision"],
                retired_messages=retired_messages,
            )
        except AgentError:
            try:
                if isinstance(data.get("memory"), dict):
                    memory.compact_for_lifecycle(data["memory"])
                memory.record_branch_rewrite_fence(
                    data,
                    task_id=row["id"],
                    previous_conversation_revision=previous_revision,
                    new_conversation_revision=row["conversation_revision"],
                    retired_messages=retired_messages,
                )
            except AgentError as exc:
                memory.record_branch_rewrite_fence_minimal(
                    data,
                    task_id=row["id"],
                    retired_messages=retired_messages,
                )
                data["memory_ledger_degraded"] = {
                    "at": memory._now(),
                    "code": str(exc),
                    "task_id": row["id"],
                    "operation": "branch_rewrite",
                }
        memory_integration.propagate_memory_revocation_in_tx(data, exclude_task_id=row.get("id"))
        if row.get("memory_projection_watermark") is not None or row.get("memory_disclosed_in_epoch"):
            from . import memory_runtime
            row["memory_projection_watermark"] = memory_runtime.memory_projection_watermark(data, row).get("watermark", "")
            row["memory_disclosed_in_epoch"] = False
        memory_integration.apply_owner_turn_memory_in_tx(None, data, row)
    row["observed_files"] = {}
    record_goal_history(row)
    owners = [message for message in row["messages"]
              if message["role"] == "user" and not message.get("ui", {}).get("hidden")]
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
