"""Owner-created conversation branches; history is data, never an execution queue.

The existing encrypted store is the commit point. Referenced bytes are written
first to a deterministic child namespace: an interrupted attempt is invisible,
and retrying the same request reuses that namespace instead of accumulating tasks.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from fastapi import Request

from ..atomic_io import atomic_write_bytes, fs_path
from ..file_transactions import file_transaction
from . import budget, context_compaction, event_compaction, lifecycle, projects, results
from .file_review import safe_path
from .input_attachments import MAX_FILE_BYTES
from .protocol import digest, event, now
from .state import initialize, owner_message_id
from .store import DEFAULT_SETTINGS, AgentError, AgentToolRejected, encoded

MAX_REFERENCE_BYTES = 40 * 1024 * 1024
MAX_REFERENCES = 256
MAX_FORK_BYTES = 2 * 1024 * 1024
MAX_FORK_TAIL_BYTES = 512 * 1024
RESULT_ID = re.compile(r"result_[a-f0-9]{32}")


def boundaries(row):
    """Persisted closed exchanges remain selectable after a damaged exchange."""
    pending, seen, choices = {}, set(), []
    fingerprint = hashlib.sha256(encoded({k: row.get(k) for k in (
        "id", "model_id", "model_destination", "context_epoch", "conversation_revision",
        "scope_revision", "scope", "project", "local_execution")}))
    for index, message in enumerate(row["messages"]):
        fingerprint.update(encoded(message))
        role = message.get("role")
        calls = message.get("tool_calls") or []
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending or pending[call_id] != message.get("context_epoch", 0):
                pending.clear()
                continue
            pending.pop(call_id)
        elif pending:
            pending.clear()
            continue
        elif calls:
            ids = [call.get("id") for call in calls]
            if role != "assistant" or any(not isinstance(cid, str) or not cid or cid in seen for cid in ids) or len(set(ids)) != len(ids):
                seen.update(cid for cid in ids if isinstance(cid, str) and cid)
                pending.clear()
                continue
            pending.update({cid: message.get("context_epoch", 0) for cid in ids})
            seen.update(ids)
        elif role not in {"user", "assistant", "system"}:
            pending.clear()
            continue
        if not pending and role != "system":
            text = (message.get("ui") or {}).get("text") or message.get("content") or ""
            choices.append({"message_count": index + 1, "token": fingerprint.hexdigest(),
                            "role": role, "preview": str(text)[:160]})
    return choices


def _repair_prefix(messages):
    """Make saved child history self-contained without replaying any tool."""
    repaired, pending, seen = [], {}, set()

    def close_missing():
        for call_id, epoch in pending.items():
            repaired.append({"role": "tool", "tool_call_id": call_id,
                "content": encoded({"error": "agent_tool_result_missing", "not_executed": False}).decode(),
                "is_error": True, "context_epoch": epoch})
        pending.clear()

    for original in messages:
        message = dict(original)
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id in pending and pending[call_id] == message.get("context_epoch", 0):
                repaired.append(message)
                pending.pop(call_id)
            continue
        close_missing()
        if role not in {"user", "assistant", "system"}:
            continue
        calls = message.get("tool_calls") or []
        if calls:
            kept = []
            if role == "assistant":
                for call in calls:
                    call_id = call.get("id")
                    if isinstance(call_id, str) and call_id and call_id not in seen:
                        seen.add(call_id)
                        kept.append(call)
                        pending[call_id] = message.get("context_epoch", 0)
            if kept:
                message["tool_calls"] = kept
            else:
                message.pop("tool_calls", None)
                if not message.get("content"):
                    message["content"] = "[Invalid or repeated historical tool call omitted.]"
        repaired.append(message)
    close_missing()
    return repaired


def _inherited_prefix(source, count):
    messages = copy.deepcopy(source["messages"][:count])
    for index, message in enumerate(messages):
        for key in ("provider_state", "reasoning_content", "owner_source_seq", "message_id"):
            message.pop(key, None)
        message["inherited_from"] = {"task_id": source["id"], "message_index": index}
    repaired = _repair_prefix(messages)
    return repaired, repaired != messages


def _compact_prefix(source, count):
    from .runtime import CONTINUATION_PREFIX

    messages, repaired = _inherited_prefix(source, count)
    call_names = {call["id"]: (call.get("function") or {}).get("name")
                  for message in messages for call in message.get("tool_calls", [])}
    read_events = []
    for message in messages:
        if message.get("role") != "tool" or call_names.get(message.get("tool_call_id")) != "ryn_file_read":
            continue
        try:
            result = json.loads(message.get("content", ""))
        except ValueError:
            continue  # Legacy plain-text output cannot establish exact file coverage.
        if isinstance(result, dict):
            read_events.append({"type": "tool_result", "tool": "ryn_file_read", "result": result,
                                "context_epoch": message.get("context_epoch", 0)})
    prefix = {**source, "messages": messages, "goal": {}, "events": read_events}
    checkpoint = source.get("context_checkpoint")
    if repaired or not checkpoint or checkpoint.get("through", 0) > count:
        prefix.pop("context_checkpoint", None)
    summary = event_compaction.continuation_summary(prefix)
    # The current continuation helper is text-only. Reuse the same bounded
    # server-derived facts as ordinary context checkpoints, for this prefix only.
    root = (source.get("project") or {}).get("path") or (source.get("local_execution") or {}).get("workspace_root")
    if root:
        summary += projects.read_progress({**prefix, "project": {"path": root},
            "context_checkpoint": {"through": len(messages)}})
    summary += context_compaction.activity_ledger(messages, len(messages), 12000)
    tail = []
    for index in reversed([i for i, message in enumerate(messages) if message.get("role") == "user"]):
        candidate = messages[index:]
        # Include the owner IDs initialize() will add, so persisted tail bytes
        # stay within the limit as well as the raw message bodies.
        sized = [{**message, "message_id": owner_message_id({"id": "agent_" + "0" * 32}, i + 1)}
                 if message.get("role") == "user" else message for i, message in enumerate(candidate)]
        if len(encoded(sized)) > MAX_FORK_TAIL_BYTES:
            break
        tail = candidate
    omitted = count - sum("inherited_from" in message for message in tail)
    return [{"role": "user", "content": CONTINUATION_PREFIX + summary,
             "context_epoch": source.get("context_epoch", 0)}, *tail], omitted


def list_boundaries(runtime, task_id):
    return runtime.store.inspect(lambda data: {"source_task_id": task_id,
        "boundaries": boundaries(lifecycle.find(runtime.store, data, task_id)), "shared_workspace": True})


def _replace_paths(value, paths):
    if isinstance(value, str):
        for old, new in paths.items():
            # Strings may themselves contain JSON tool envelopes.
            value = value.replace(old.replace("\\", "\\\\"), new.replace("\\", "\\\\"))
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace_paths(item, paths) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, paths) for key, item in value.items()}
    return value


def _check_portable(value, runtime):
    """Do not turn task-bound capabilities into misleading historical links."""
    if isinstance(value, dict):
        if value.get("native_descriptor") or value.get("provenance") == "native_computer_observation":
            raise AgentError("agent_fork_reference_requires_source_task")
        for key, item in value.items():
            if key in {"native_observation_artifacts", "image_artifacts", "mcp_artifacts"} and item:
                raise AgentError("agent_fork_reference_requires_source_task")
            if key in {"resource_ref", "directory_ref", "listing_snapshot", "cursor", "next_cursor"} and isinstance(item, str) and item.startswith("gAAAA"):
                resources = getattr(runtime.tools, "resources", None)
                if resources is None or resources._open(item).get("task_id"):
                    raise AgentError("agent_fork_reference_requires_source_task")
            _check_portable(item, runtime)
    elif isinstance(value, list):
        for item in value:
            _check_portable(item, runtime)


def _result_ids(value):
    """Read structured reference fields, never IDs mentioned in prose/code."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"result_ref", "result_id"}:
                identifier = item.get("id") if isinstance(item, dict) else item
                if isinstance(identifier, str) and RESULT_ID.fullmatch(identifier):
                    found.add(identifier)
            found.update(_result_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_result_ids(item))
    return found


def _history_payloads(message):
    """Decode only protocol JSON slots; conversation text remains opaque."""
    yield message
    if message.get("role") == "tool" and isinstance(message.get("content"), str):
        try:
            yield json.loads(message["content"])
        except ValueError:
            pass  # Legacy textual tool output is historical prose.
    for call in message.get("tool_calls") or []:
        arguments = (call.get("function") or {}).get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                # Cannot attest that undecodable arguments contain no capability.
                raise AgentError("agent_fork_invalid_tool_arguments") from None
        if not isinstance(arguments, dict):
            raise AgentError("agent_fork_invalid_tool_arguments")
        yield arguments


def _write_copy(store, target, raw, boundary, plaintext_hash=None):
    """Publish immutable references without overwriting a file created meanwhile."""
    safe_path(boundary, str(target))
    # Boundary checks use the plain path; file calls use the long-path form,
    # because nested result/attachment targets can pass 260 chars on Windows.
    native = fs_path(target)
    native.parent.mkdir(parents=True, exist_ok=True)
    safe_path(boundary, str(target))
    if native.exists():
        if native.stat().st_size > (3 * results.MAX_RESULT_BYTES if plaintext_hash else MAX_FILE_BYTES):
            raise AgentError("agent_fork_staging_changed")
        saved = native.read_bytes()
        saved_hash = hashlib.sha256(saved).hexdigest()
        if plaintext_hash:
            from ..services import peer_box
            try:
                envelope = json.loads(saved)
                saved = peer_box.open_sealed(store.key, store.pub, envelope["nonce"], envelope["ciphertext"], info=results.CHANNEL)
            except Exception:
                raise AgentError("agent_fork_staging_changed") from None
            matches = hashlib.sha256(saved).hexdigest() == plaintext_hash
        else:
            matches = saved == raw
        if not matches:
            raise AgentError("agent_fork_staging_changed")
        return saved_hash
    temporary = fs_path(target.with_name(target.name + ".fork-" + uuid.uuid4().hex + ".tmp"))
    try:
        atomic_write_bytes(temporary, raw)
        safe_path(boundary, str(target))
        # Windows rename refuses to overwrite; POSIX link is also create-only.
        if os.name == "nt":
            os.rename(temporary, native)
        else:
            os.link(temporary, native)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def _copy_references(runtime, source, child, written):
    store = runtime.store
    root = Path(child["local_execution"]["workspace_root"])
    paths, total, writes = {}, 0, []
    todo = set()
    # Only explicit attachment metadata is read; never follow arbitrary paths in prose.
    for message in child["messages"]:
        for payload in _history_payloads(message):
            _check_portable(payload, runtime)
            todo.update(_result_ids(payload))
        for attachment in message.get("attachments", []):
            if attachment.get("reference_only"):
                continue
            old = str(attachment.get("path", ""))
            if old in paths:
                continue
            path = safe_path(root, old)
            source_dir = root / ".ryn" / "attachments" / source["id"]
            # Older forks can retain a different attachment origin only in prose.
            if path.parent != source_dir or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise AgentError("agent_fork_attachment_unavailable")
            if total + path.stat().st_size > MAX_REFERENCE_BYTES or len(writes) >= MAX_REFERENCES:
                raise AgentError("agent_fork_reference_limit")
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) != attachment.get("size") or hashlib.sha256(raw).hexdigest() != attachment.get("sha256"):
                raise AgentError("agent_fork_attachment_unavailable")
            target = safe_path(root, str(root / ".ryn" / "attachments" / child["id"] / path.name))
            paths[old] = str(target)
            total += len(raw)
            writes.append((target, raw, root, None))
    child["messages"] = _replace_paths(child["messages"], paths)
    child["goal"] = _replace_paths(child["goal"], paths)
    if child.get("context_checkpoint"):
        child["context_checkpoint"] = _replace_paths(child["context_checkpoint"], paths)
    # Result IDs are scoped to task storage. Keep IDs so nested references stay valid,
    # but authenticate and copy every reachable blob into the child's namespace.
    _check_portable(child.get("context_checkpoint"), runtime)
    todo.update(_result_ids(child.get("context_checkpoint")))
    copied = set()
    child["result_refs"] = []
    while todo:
        result_id = todo.pop()
        if result_id in copied:
            continue
        safe_path(store.root, str(results.path(store, source["id"], result_id)))
        value, reference = results.read(store, source, result_id)
        _check_portable(value, runtime)
        raw_value = encoded(value)
        total += len(raw_value)
        if total > MAX_REFERENCE_BYTES or len(writes) >= MAX_REFERENCES:
            raise AgentError("agent_fork_reference_limit")
        todo.update(_result_ids(value) - copied - {result_id})
        # Re-seal if an envelope contains copied attachment paths.
        value = _replace_paths(value, paths)
        raw_value = encoded(value)
        from ..services import peer_box
        nonce, ciphertext = peer_box.seal(store.key, store.pub, raw_value, info=results.CHANNEL)
        reference = {**copy.deepcopy(reference), "task_id": child["id"],
                     "sha256": hashlib.sha256(raw_value).hexdigest(), "bytes": len(raw_value)}
        child["result_refs"].append(reference)
        target = results.path(store, child["id"], result_id)
        writes.append((target, encoded({"nonce": nonce, "ciphertext": ciphertext}), store.root, reference["sha256"]))
        copied.add(result_id)
    if total > MAX_REFERENCE_BYTES or len(writes) > MAX_REFERENCES:
        raise AgentError("agent_fork_reference_limit")
    if len(encoded(child)) > MAX_FORK_BYTES:
        raise AgentError("agent_fork_too_large")
    for target, raw, boundary, plaintext_hash in writes:
        checksum = _write_copy(store, target, raw, boundary, plaintext_hash)
        written.append((target, boundary, checksum))


def create(runtime, source_id, value):
    from .runtime import TERMINAL, public_task
    if set(value) != {"client_request_id", "title", "message_count", "token", "shared_workspace_confirmed"}:
        raise AgentError("agent_invalid_fork_request")
    try:
        request_id = str(uuid.UUID(value["client_request_id"]))
        if request_id != value["client_request_id"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AgentError("agent_invalid_request_id") from None
    if not isinstance(value["title"], str) or not value["title"].strip() or len(value["title"]) > 160:
        raise AgentError("agent_invalid_title")
    if value["shared_workspace_confirmed"] is not True or type(value["message_count"]) is not int:
        raise AgentError("agent_invalid_fork_request")
    fingerprint = digest({"fork_source": source_id, **value})
    relieve = getattr(runtime, "_relieve_storage", None)
    if callable(relieve):
        relieve("fork", exclude={source_id}, need_tasks=1)

    def prepare(data, *, force_compact=False):
        existing = lifecycle.duplicate(runtime.store, data, request_id, fingerprint)
        if existing is not None:
            return None, public_task(copy.deepcopy(existing), settings=data["settings"])
        settings = {**DEFAULT_SETTINGS, **data["settings"]}
        if not settings["enabled"]:
            raise AgentError("agent_disabled")
        if len(data["tasks"]) >= 200 or sum(row["status"] not in TERMINAL and not row.get("parent_task_id") for row in data["tasks"].values()) >= 16:
            raise AgentError("agent_task_limit")
        source = lifecycle.find(runtime.store, data, source_id)
        boundary = next((b for b in boundaries(source) if b["message_count"] == value["message_count"] and b["token"] == value["token"]), None)
        if boundary is None:
            raise AgentError("agent_fork_boundary_changed")
        child_id = "agent_" + uuid.uuid5(uuid.NAMESPACE_URL, "ryn-fork:" + request_id).hex
        messages, repaired = _inherited_prefix(source, value["message_count"])

        def build(messages, *, compacted=False, omitted=0):
            row = {"id": child_id, "client_request_id": request_id, "request_fingerprint": fingerprint,
                   "title": value["title"].strip(), "model_id": source["model_id"], "status": "paused", "revision": 0,
                   "created_at": now(), "updated_at": now(), "messages": messages, "events": [],
                   "pending_approval": None, "approved": None, "queue": [], "in_flight": None,
                   "steps": 0, "elapsed_seconds": 0.0, "result": "", "error": None,
                   "limits": {k: settings[k] for k in DEFAULT_SETTINGS if k.startswith("max_")},
                   "permission_mode": source["permission_mode"],
                   "allowed_write_tools": list(source.get("allowed_write_tools", [])),
                   "fork": {"source_task_id": source_id, "source_title": source["title"],
                            "message_count": value["message_count"], "boundary_token": value["token"],
                            "shared_workspace": True, "awaiting_instruction": True}}
            if compacted:
                row["fork"].update(compacted=True, omitted_messages=omitted)
            initialize(row)
            # Persist the intersection at creation, so later global relaxation cannot
            # give this child authority that it never had when it was forked.
            # Strictest first; automatic (full access) only when both allow it.
            modes = {source["permission_mode"], settings["permission_mode"]}
            for mode in ("read_only", "confirm_changes", "workspace", "automatic"):
                if mode in modes:
                    row["permission_mode"] = mode
                    break
            # Config only. Never copy receipts, approvals, queues, processes, callbacks,
            # provider sessions, verification evidence, budgets used, or live plans.
            for key in ("local_execution", "project", "model_destination", "model_destination_required",
                        "model_destination_confirmed", "scope", "scope_revision", "context_epoch", "data_policy_state",
                        "base_instructions", "prompt_version", "skills", "reasoning_effort", "response_language",
                        "context_budget_provenance"):
                if key in source:
                    row[key] = copy.deepcopy(source[key])
            if not row["scope"].get("resource_grants"):
                row["scope"]["selection"] = {"resources": [], "allow_model_data": False}
            if not (row.get("local_execution") or {}).get("workspace_root"):
                raise AgentError("agent_fork_workspace_unavailable")
            row["local_execution"]["desktop_access"] = bool(row["local_execution"].get("desktop_access") and settings.get("desktop_access"))
            row["verify_text_responses"] = settings.get("verify_text_responses", False)
            budget.initialize(row)
            # A fork is a new task using current settings, not a migrated legacy task.
            row["limits_source"] = {key: (data.get("settings_limit_sources") or {}).get(key, "default")
                                    for key in budget.SEGMENT_LIMITS}
            checkpoint = source.get("context_checkpoint")
            active_count = len(runtime._history(row))
            if not compacted and not repaired and checkpoint and 0 < checkpoint.get("through", 0) <= active_count:
                row["context_checkpoint"] = copy.deepcopy(checkpoint)
            event(row, "created")
            event(row, "fork_created", source_task_id=source_id, message_count=value["message_count"], shared_workspace=True)
            return row

        row = build(messages)
        if force_compact or len(encoded(row)) > MAX_FORK_BYTES:
            messages, omitted = _compact_prefix(source, value["message_count"])
            row = build(messages, compacted=True, omitted=omitted)
            if len(encoded(row)) > MAX_FORK_BYTES:
                raise AgentError("agent_fork_too_large")
        # Current resource grants/origins must still be legal under the child ID.
        # Task-bound cloud references fail closed rather than silently losing context.
        runtime._validate_context(row, control_state=data)
        local = getattr(runtime.tools, "local", None)
        if local is not None:
            local.policy(runtime._execution_context(row), data)
        elif row["local_execution"]["workspace_root"] != (settings.get("workspace_root") or runtime.store.default_workspace()):
            raise AgentError("agent_workspace_changed_new_task_required")
        return source, row

    child_id = "agent_" + uuid.uuid5(uuid.NAMESPACE_URL, "ryn-fork:" + request_id).hex
    # The namespace is deterministic. Serialize retries of this request only;
    # unrelated tasks and forks never wait for its file copies.
    try:
        with file_transaction(runtime.store.root / "forks" / (child_id + ".lock")):
            written = []
            try:
                source, staged = runtime.store.inspect(prepare)
                if source is None:
                    return staged
                try:
                    _copy_references(runtime, source, staged, written)
                except AgentError as exc:
                    if str(exc) != "agent_fork_too_large" or staged["fork"].get("compacted"):
                        raise
                    source, staged = runtime.store.inspect(lambda data: prepare(data, force_compact=True))
                    _copy_references(runtime, source, staged, written)
                source_hashes = {ref["id"]: ref["sha256"] for ref in source.get("result_refs", [])
                                 if any(item["id"] == ref["id"] for item in staged["result_refs"])}

                def commit(data):
                    current, row = prepare(data)
                    if current is None:
                        return row
                    hashes = {ref["id"]: ref["sha256"] for ref in current.get("result_refs", [])}
                    if (any(hashes.get(key) != checksum for key, checksum in source_hashes.items())
                            or current.get("context_checkpoint") != source.get("context_checkpoint")):
                        raise AgentError("agent_fork_boundary_changed")
                    # Validate the bytes actually published, including reused files;
                    # no decryption, encryption or file publication in this transaction.
                    for target, boundary, checksum in written:
                        safe_path(boundary, str(target))
                        with fs_path(target).open("rb") as stream:
                            actual = hashlib.sha256()
                            while chunk := stream.read(1024 * 1024):
                                actual.update(chunk)
                        if actual.hexdigest() != checksum:
                            raise AgentError("agent_fork_staging_changed")
                    for key in ("messages", "goal", "context_checkpoint", "result_refs", "fork"):
                        if key in staged:
                            row[key] = copy.deepcopy(staged[key])
                        else:
                            row.pop(key, None)
                    if len(encoded(row)) > MAX_FORK_BYTES:
                        raise AgentError("agent_fork_too_large")
                    data["tasks"][child_id] = row
                    return public_task(copy.deepcopy(row), settings={**DEFAULT_SETTINGS, **data["settings"]})

                return runtime.store.transaction(commit)
            except Exception as exc:
                # Stale/changed copies cannot be reused. Disk failures retain the
                # existing retry behavior; the same request can reuse valid copies.
                discard = str(exc) in {"agent_fork_boundary_changed", "agent_fork_staging_changed"}
                if discard and written and not runtime.store.inspect(lambda data: child_id in data["tasks"]):
                    for target, boundary, _checksum in written:
                        safe_path(boundary, str(target))
                        fs_path(target).unlink(missing_ok=True)
                if isinstance(exc, AgentToolRejected):
                    raise AgentError(str(exc)) from None
                raise
    except OSError:
        raise AgentError("agent_fork_storage_unavailable") from None


def install_fork_routes(app, runtime, local_control, body):
    import asyncio

    from fastapi import HTTPException

    @app.get("/api/local/agent/tasks/{task_id}/fork-boundaries")
    async def get_boundaries(task_id: str, request: Request):
        local_control(request)
        try:
            return await asyncio.to_thread(list_boundaries, runtime, task_id)
        except AgentError as exc:
            raise HTTPException(409, detail=str(exc)) from None

    @app.post("/api/local/agent/tasks/{task_id}/fork")
    async def fork_task(task_id: str, request: Request):
        value = await body(request)
        try:
            return await asyncio.to_thread(create, runtime, task_id, value)
        except AgentError as exc:
            raise HTTPException(409, detail=str(exc)) from None
