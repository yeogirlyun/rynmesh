"""Encrypted task archives and durable deletion tombstones.

Archive content is committed before its index; the active record remains the
authority if a crash occurs between those writes. Deletion never permits a late
worker to recreate a task, and create-request IDs survive archival/deletion.
"""
from __future__ import annotations

import copy
import hashlib

from ..file_transactions import file_transaction
from .control import ambiguous, check_version, remember_request, request_replayed
from .store import VERSION, AgentError, AgentStore


def archive_store(store, task_id):
    directory = hashlib.sha256(task_id.encode()).hexdigest()
    return AgentStore(store.root / "archives" / directory, store.key)


def purge_archive(store, task_id):
    archive = archive_store(store, task_id)
    if archive.root.exists():
        with file_transaction(archive.lock):
            archive.path.unlink(missing_ok=True)
            archive.purge_orphan_snapshots()


def find(store, data, task_id):
    if task_id in data["tasks"]:
        return data["tasks"][task_id]
    if task_id in data.get("deleted_tasks", {}):
        raise AgentError("agent_task_deleted")
    if task_id in data.get("archives", {}):
        row = archive_store(store, task_id).read()["tasks"].get(task_id)
        if row is None:
            raise AgentError("agent_state_unreadable")
        return row
    raise AgentError("agent_task_not_found")


def duplicate(store, data, request_id, fingerprint):
    if request_id is None:
        return None
    for row in [*data["tasks"].values(), *data.get("archives", {}).values(), *data.get("deleted_tasks", {}).values()]:
        if row.get("client_request_id") == request_id:
            if row.get("request_fingerprint") != fingerprint:
                raise AgentError("agent_request_conflict")
            return find(store, data, row["id"])
    return None


def eligible(row):
    return (not row.get("in_flight") and not row.get("queue") and not ambiguous(row)
            and row["status"] in {"completed", "failed", "cancelled", "paused"})


def _archive_in_tx(store, data, row, emit, *, on_mutate=None, reason=None, remember=None):
    task_id = row["id"]
    row["archived"] = True
    if reason:
        # Shown in the archive list; older versions ignore the field.
        row["archive_reason"] = reason
    emit(row, "archived", **({"reason": reason} if reason else {}))
    if remember is not None:
        remember(row)
    # Archive content is committed before the index that points at it.
    archive_store(store, task_id).transaction(lambda archived: archived.update(
        version=VERSION, schema_version=data["schema_version"], settings={}, tasks={task_id: row}))
    data.setdefault("archives", {})[task_id] = {key: row.get(key) for key in ("id", "client_request_id", "request_fingerprint", "updated_at")}
    data["tasks"].pop(task_id)
    if callable(on_mutate):
        on_mutate(data, task_id, "archive")


def archive_many(store, items, emit, *, on_mutate=None, reason=None):
    """Archive several unchanged, idle tasks in one commit of the active store.

    ``items`` carry each task's id and the control revision it was chosen at; a
    task that changed, started work, or was archived meanwhile is left alone.
    The caller holds each task's credential gate. Returns the archived ids.
    """
    def mutate(data):
        done = []
        for item in items:
            row = data["tasks"].get(item["id"])
            if (row is None or row.get("archived") or row.get("control_revision") != item["control_revision"]
                    or not eligible(row)):
                continue
            if len(data.get("archives", {})) >= 10000:
                break
            _archive_in_tx(store, data, row, emit, on_mutate=on_mutate, reason=reason)
            done.append(row["id"])
        return done
    return store.transaction(mutate)


def change(store, task_id, value, operation, emit, stamp, *, on_mutate=None, reason=None):
    if set(value) - {"expected_control_revision", "client_request_id"}:
        raise AgentError("agent_invalid_request")
    keyed = {**value, "lifecycle_action": operation}

    def mutate(data):
        deleted = data.get("deleted_tasks", {}).get(task_id)
        if deleted and operation == "delete":
            if value.get("client_request_id") and request_replayed(deleted, keyed):
                return {"id": task_id, "deleted": True}
            if not value.get("client_request_id"):
                check_version(deleted, value)
                return {"id": task_id, "deleted": True}
        row = copy.deepcopy(find(store, data, task_id))
        if request_replayed(row, keyed):
            return row
        check_version(row, value)
        if operation == "restore":
            if not row.get("archived"):
                raise AgentError("agent_invalid_transition")
            if len(data["tasks"]) >= 200:
                raise AgentError("agent_task_limit")
            row["archived"] = False
            row.pop("archive_reason", None)
            row.setdefault("local_execution", {})["desktop_access"] = bool(data.get("settings", {}).get("desktop_access", False))
            row.update(approved=None, pending_approval=None)
            emit(row, "restored")
            remember_request(row, keyed)
            data["tasks"][task_id] = row
            data["archives"].pop(task_id)
            if callable(on_mutate):
                on_mutate(data, task_id, operation)
            # Stale encrypted copies are unreachable after the atomic index
            # update. Keeping one until next archive avoids cross-file rollback.
            return row
        if not eligible(row):
            raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_task_busy")
        if operation == "archive":
            if row.get("archived"):
                raise AgentError("agent_invalid_transition")
            if len(data.get("archives", {})) >= 10000:
                raise AgentError("agent_archive_limit")
            _archive_in_tx(store, data, row, emit, on_mutate=on_mutate, reason=reason,
                           remember=lambda current: remember_request(current, keyed))
            return row
        if operation != "delete":
            raise AgentError("agent_invalid_action")
        if len(data.get("deleted_tasks", {})) >= 10000:
            raise AgentError("agent_archive_limit")
        tombstone = {key: row.get(key) for key in ("id", "client_request_id", "request_fingerprint")}
        operation_ids = {receipt["operation_id"] for receipt in row.get("receipts", [])}
        operation_ids.update(e["operation_id"] for e in row.get("events", []) if isinstance(e.get("operation_id"), str))
        tombstone.update(deleted_at=stamp(), control_requests={}, control_revision=row["control_revision"], cleanup_pending=True, operation_ids=sorted(operation_ids))
        remember_request(tombstone, keyed)
        data.setdefault("deleted_tasks", {})[task_id] = tombstone
        data["tasks"].pop(task_id, None)
        data.get("archives", {}).pop(task_id, None)
        if callable(on_mutate):
            on_mutate(data, task_id, operation)
        return {"id": task_id, "deleted": True}
    return store.transaction(mutate)
