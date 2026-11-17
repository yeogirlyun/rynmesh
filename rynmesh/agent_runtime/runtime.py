"""Durable host controller for the model-driven AgentLoop.

The worker performs one external operation per tick. Intent is encrypted before
dispatch, and no write is retried after an uncertain result. The model can only
request tools: this module alone decides whether those tools may execute.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..background_workers import WorkerRunResult
from . import (
    automation_worker,
    automations,
    budget,
    credential_control,
    data_policy,
    diagnostic_log,
    event_compaction,
    image_capabilities,
    knowledge,
    lifecycle,
    memory_integration,
    memory_natural,
    model_profiles,
    model_switch,
    output_policy,
    projects,
    reasoning,
    results,
    retry_policy,
    storage_relief,
    subagents,
    timing,
    tool_menu,
    verification,
)
from .control import (
    acknowledgeable,
    allowed_actions,
    ambiguous,
    check_version,
    close_queue,
    reconciliation_active,
    recoverable_read_in_flight,
    remember_request,
    request_replayed,
    settle,
)
from .input_attachments import attach_to_message, parse_attachments
from .loop import ALL_INTERNAL_NAMES, ALL_INTERNAL_SPECS, OWNER_TURN_EVENTS, AgentLoop
from .prompt import (
    HISTORY_TOOL_NAMES,
    INTERNAL_NAMES,
    PROMPT_VERSION,
    select_base_instructions,
)
from .protocol import digest, event, hydrated_events, model_retry_authority, now, sanitized, task
from .state import (
    PUBLIC_STATE_FIELDS,
    compact_goal_history,
    initialize,
    owner_update,
    synchronize,
)
from .store import (
    DEFAULT_SETTINGS,
    PERMISSION_MODE_SOURCE,
    PERMISSION_MODES,
    AgentError,
    AgentModelResponseError,
    AgentReadFailure,
    AgentStore,
    AgentToolRejected,
    cancellation_reason,
    encoded,
)
from .streamed_reads import recover_stream_reads

TERMINAL = {"completed", "cancelled", "failed", "interrupted"}
# A worker continues queued steps for at most this long before yielding its slot.
CONTINUE_SECONDS = 20.0
# Completed streaming reads retain their receipts before inference retry. Writes
# remain behind accepted responses. Retries are bounded by task and lifetime budgets.
RETRYABLE_MODEL_ERRORS = {"agent_inference_timeout", "agent_model_connection_failed", "agent_model_busy",
                          "agent_model_starting",
                          "agent_model_upstream_failed", "agent_model_stream_interrupted",
                          "agent_invalid_model_response", "agent_compaction_size_limit", "agent_compaction_incomplete"}
PUBLIC_FIELDS = ("fork", "id", "client_request_id", "title", "model_id", "reasoning_effort", "status", "revision", "created_at", "updated_at",
                 "messages", "pending_user_messages", "events", "pending_approval", "steps", "result", "error", "connection_recovery", "limits", "permission_mode", "allowed_write_tools",
                 "model_destination", "local_execution", "project", "verification_evidence", "reconciliation", "archived", "result_refs", "file_artifact_revision",
                 "conversation_revision", "conversation_event_cursor", "source", "automation_id", "automation_run_id", "automation_kind",
                 "latest_turn_is_silent_heartbeat", "automation_notification", "parent_task_id", "subagent_ids",
                 "limits_source", "execution_segment", "archive_reason", "continued_from", "continued_to", *PUBLIC_STATE_FIELDS)
CONTINUATION_PREFIX = ("[Continued from an earlier conversation that grew too long. The summary below was "
                       "written by Ryn from that conversation; it is context, not a new instruction.]\n\n")
CONTINUATION_SEPARATOR = "\n\n[The owner's new message follows.]\n\n"


class ContinueInNewTask(Exception):
    """Raised inside a transaction (rolling it back) when a conversation must continue elsewhere."""


UNKNOWN_OUTCOME_NOTE = (
    "This step started but its result was never confirmed (the owner stopped the task, Ryn restarted, "
    "the request timed out, or the response was lost). It may have fully run, partly run, or not run at all. "
    "Before doing it again, check the actual state with read-only tools. Do not assume it failed and do not "
    "repeat it blindly."
)
TURN_ABORTED_NOTE = (
    "<turn_aborted>\n"
    "The user intentionally interrupted the previous turn. Background processes may still be running, "
    "and interrupted tools may have partially executed. Before continuing or retrying, use read-only "
    "tools to check the current state.\n"
    "</turn_aborted>"
)
MCP_UNKNOWN_OUTCOME_HINT = "先用只读工具核对再决定是否重试"
VISUAL_SUFFIXES = (".svg", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".webp")


def _flight_has_server_hint_read(flight: dict | None) -> bool:
    if not isinstance(flight, dict):
        return False
    if flight.get("server_hint_read") is True:
        return True
    binding = flight.get("extension_binding")
    return isinstance(binding, dict) and binding.get("server_hint_read") is True


def public_task(row: dict, *, summary: bool = False, settings: dict | None = None,
                active_run: dict | None = None, monotonic_now: float | None = None,
                data: dict | None = None) -> dict:
    synchronize(row)
    result = copy.deepcopy({key: row[key] for key in PUBLIC_FIELDS if key in row and not (summary and key in {"messages", "pending_user_messages", "events"})})
    if result.get("run_state") == "reconciling" and not reconciliation_active(row):
        # A finished "unknown" reconciliation is waiting for the owner, not running.
        result["run_state"] = "paused"
    if "events" in result:
        result["events"] = hydrated_events(row, result["events"])
    # Metadata changes advance revision too; only an actual completed response
    # identifies a new completion for clients that were away from the task.
    result["completion_event_seq"] = next((item["seq"] for item in reversed(row.get("events", []))
                                           if item["type"] == "completed"), 0)
    result.setdefault("archived", False)
    result["waiting_for_subagents"] = bool(row.get("subagent_wait") and row["status"] == "queued"
                                           and row.get("queue") and row["queue"][0]["call_id"] == row["subagent_wait"]["call_id"])
    result["skills"] = [{key: value for key, value in skill.items() if key != "content"} for skill in row.get("skills", [])]
    result["budget"] = budget.public(row)
    result["run_timing"] = timing.snapshot(row, active_run=active_run, monotonic_now=monotonic_now)
    if "_subagents_projection" in row:
        result["subagents"] = copy.deepcopy(row["_subagents_projection"])
    elif data is not None and row.get("subagent_ids"):
        result["subagents"] = subagents.project_subagents_for_parent(data, row)
    if not summary:
        result["streaming"] = None
    for message in [*result.get("messages", []), *result.get("pending_user_messages", [])]:
        # Some providers require their opaque reasoning continuation field back.
        # Keep it encrypted internally; it is never an owner-facing transcript.
        message.pop("reasoning_content", None)
        message.pop("provider_state", None)
        message.pop("resource_origin", None)
        message.pop("owner_source_seq", None)
    for reference in result.get("result_refs", []):
        reference.pop("resource_origin", None)
    if settings is not None:
        result["allowed_actions"] = allowed_actions(row, settings)
    return result


def detached(row: dict, *, summary: bool = False, data: dict | None = None) -> dict:
    """A row public_task may annotate without touching the store's cached copy.

    public_task deep-copies the fields it returns; it only writes run_state and
    the outcome, so those are the parts copied here.
    """
    # A legacy row without a budget derives its usage from events.
    # Keep the events reference for the read-only completion marker. Summaries
    # still omit both events and messages from the public deep copy.
    shallow = {key: value for key, value in row.items()
               if not (summary and "budget" in row and key == "messages")}
    if "outcome" in shallow:
        shallow["outcome"] = copy.deepcopy(shallow["outcome"])
    if data is not None and row.get("subagent_ids"):
        shallow["_subagents_projection"] = subagents.project_subagents_for_parent(data, row)
    return shallow


def revision(row: dict, expected: Any) -> None:
    if type(expected) is not int or row["revision"] != expected:
        raise AgentError("agent_revision_conflict")


def write_permission_revoked(row: dict, settings: dict) -> bool:
    from .execution_policy import decide, verified_read_classification
    flight = row.get("in_flight") or {}
    has_computer_access = (
        str(flight.get("name") or "").startswith(("ryn_browser_", "ryn_computer_"))
        or bool(flight.get("computer_access"))
        or bool((flight.get("extension_binding") or {}).get("computer_access"))
    )
    if has_computer_access and not (row.get("local_execution") or {}).get("desktop_access"):
        return True
    if flight.get("effect") != "write":
        return False
    if verified_read_classification(
        flight,
        task_id=row.get("id", ""),
        workspace_root=(row.get("local_execution") or {}).get("workspace_root", ""),
    ):
        return False
    if row["permission_mode"] == "read_only":
        return True
    if flight.get("authorization") == "automatic":
        return decide(flight, {"effect": flight["effect"],
            "computer_access": flight.get("computer_access"),
            "workstation_permission_tool": flight.get("workstation_permission_tool")}, row, settings).action != "execute"
    return False


def message_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError("agent_invalid_message")
    return value.strip()


def _summarize_shell_command(raw_cmd: object) -> str:
    if not isinstance(raw_cmd, str):
        return ""
    collapsed = " ".join(raw_cmd.strip().split())
    if len(collapsed) > 80:
        return collapsed[:77] + "..."
    return collapsed


def _note_lost_background_processes(row: dict, boot_id: str) -> None:
    if boot_id and row.get("process_note_boot") == boot_id:
        return
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    start_idx = 0
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict) and (msg.get("ui") or {}).get("kind") == "lost_background_processes":
            start_idx = idx + 1
    call_commands: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            cid = call.get("id")
            fn = call.get("function") or {}
            if (fn.get("name") or call.get("name")) != "ryn_shell_execute" or not cid:
                continue
            raw_args = fn.get("arguments") if "arguments" in fn else call.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except (ValueError, TypeError):
                    raw_args = None
            if isinstance(raw_args, dict):
                call_commands[cid] = _summarize_shell_command(raw_args.get("command"))
    active_cmds: dict[str, str] = {}
    for msg in messages[start_idx:]:
        if msg.get("role") != "tool":
            continue
        raw_content = msg.get("content")
        if not isinstance(raw_content, str) or "process_" not in raw_content:
            continue
        try:
            parsed = json.loads(raw_content)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        pid = parsed.get("process_id")
        if not isinstance(pid, str) or not pid.startswith("process_"):
            continue
        if parsed.get("running") is True:
            active_cmds[pid] = active_cmds.get(pid) or call_commands.get(msg.get("tool_call_id") or "") or ""
        elif "running" in parsed and not parsed.get("running"):
            active_cmds.pop(pid, None)
    if not active_cmds:
        return
    entries = "、".join(
        f"{pid}（{summary or '未知命令'}）" for pid, summary in active_cmds.items()
    )
    note_text = f"Ryn 节点已重启，以下后台进程已结束：{entries}。需要时重新启动命令，不要再读这些 id。"
    note_msg = {
        "role": "assistant",
        "content": note_text,
        "context_epoch": row.get("context_epoch", 0),
        "ui": {"kind": "lost_background_processes", "hidden": True},
    }
    insert_at = len(messages)
    if row.get("queue"):
        tail_tool_start = len(messages)
        while (
            tail_tool_start > 0
            and isinstance(messages[tail_tool_start - 1], dict)
            and messages[tail_tool_start - 1].get("role") == "tool"
        ):
            tail_tool_start -= 1
        if tail_tool_start > 0:
            prev = messages[tail_tool_start - 1]
            if isinstance(prev, dict) and prev.get("role") == "assistant" and prev.get("tool_calls"):
                expected_ids = {c.get("id") for c in prev["tool_calls"] if isinstance(c, dict) and c.get("id")}
                present_ids = {messages[i].get("tool_call_id") for i in range(tail_tool_start, len(messages))}
                if expected_ids - present_ids:
                    insert_at = tail_tool_start - 1
    messages.insert(insert_at, note_msg)
    if boot_id:
        row["process_note_boot"] = boot_id


class _ExtensionPolicyAdmitter:
    def __init__(self, runtime: AgentRuntime, row: dict) -> None:
        self.runtime = runtime
        self.row = row
        self.selected = {
            "id": row["id"],
            "control_revision": row.get("control_revision"),
            "model_id": row["model_id"],
            "model_destination": copy.deepcopy(row.get("model_destination")),
        }

    def __call__(self, snapshot: dict, persist: bool = True) -> dict:
        return self.runtime._admit_extension_policy(self.row, snapshot, persist=persist)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _ExtensionPolicyAdmitter)
            and self.runtime is other.runtime
            and self.selected == other.selected
        )

    def __deepcopy__(self, memo: dict) -> _ExtensionPolicyAdmitter:
        return self


class AgentRuntime(AgentLoop):
    def __init__(self, store: AgentStore, model: Any, tools: Any, catalog: Any, *, owner_id="local_owner", consumer_node_id=None):
        self.store, self.model, self.tools, self.catalog = store, model, tools, catalog
        from .tool_dispatch import ToolDispatchGate
        self._tool_dispatch_gate = ToolDispatchGate()
        if getattr(tools, "local", None) is not None:
            from .native_computer import bind_owner_native_computer

            tools.local.store = store
            tools.local.native_computer = bind_owner_native_computer(store, local_tools=tools.local)
            if hasattr(tools, "native_computer"):
                tools.native_computer = tools.local.native_computer
        if getattr(tools, "extensions", None) is not None:
            tools.extensions.bind_agent_store(store)
            self.extensions = tools.extensions
        else:
            self.extensions = None
        self.owner_id, self.consumer_node_id = owner_id, consumer_node_id or store.pub
        # Keep the scheduler gate for atomic task registration/recovery. Only
        # external IO runs concurrently; claiming a task remains exclusive.
        self._run_lock = threading.Lock()
        self._run_slots = threading.BoundedSemaphore(6)
        self._storage_relief_lock = threading.Lock()
        # First idle check shortly after start (an upgraded store may already be
        # full), then at most once a minute.
        self._next_idle_relief = time.monotonic() + 30
        self._active_lock = threading.Lock()
        self._active_runs = {}
        self._queued_since = {}
        self._step = threading.local()  # Whether this thread's last step ended normally.
        self._stream_lock = threading.Lock()
        self._streams = {}
        self._skill_sync_lock = threading.Lock()
        self._skill_sync_last_at: dict[str, float] = {}
        self._boot_id = "boot_" + uuid.uuid4().hex
        # Display only. Never used by admission, cancellation or persistence.
        self._active_run = None

    def _effective_task_permission_mode(self, selected: dict, settings: dict | None = None) -> str:
        task_mode = selected.get("permission_mode", "workspace")
        return task_mode if task_mode in PERMISSION_MODES else "read_only"

    def _execute_business_tool(self, selected, name, arguments, **options):
        if name == "ryn_code_exec":
            return super()._execute_business_tool(selected, name, arguments, **options)
        from .tool_dispatch import supports_parallel

        meta = self.tools.metadata(name)
        mode = self._effective_task_permission_mode(selected)
        with self._tool_dispatch_gate.enter(
            selected["id"],
            parallel=supports_parallel(name, meta, arguments, permission_mode=mode),
            cancelled=options["cancelled"],
        ):
            return self.tools.execute(name, arguments, **options)

    def _tool(self, selected: dict, cancelled, started: float) -> None:
        from .tool_dispatch import MAX_PARALLEL_TOOLS

        queue = selected.get("queue") or []
        if len(queue) >= 2 and self._effective_task_permission_mode(selected) == "automatic":
            batch: list[dict] = []
            for item in queue[:MAX_PARALLEL_TOOLS]:
                if (
                    item.get("error_result") is not None
                    or item.get("internal")
                    or item.get("name") in ALL_INTERNAL_NAMES
                    or item.get("effect") != "write"
                    or not _flight_has_server_hint_read(item)
                ):
                    break
                batch.append(item)
            if len(batch) >= 2 and self._mcp_hint_read_batch(selected, batch, cancelled, started):
                return
        super()._tool(selected, cancelled, started)

    def _mcp_hint_read_batch(self, selected: dict, batch: list[dict], cancelled, started: float) -> bool:
        from concurrent.futures import ThreadPoolExecutor

        from ..llm_package.chat import validate_tool_arguments
        from .execution_policy import classify_operation, decide
        from .tool_dispatch import supports_parallel
        from .tool_router import assert_schema_unchanged

        preparation_started = time.monotonic()
        claim_persistence_seconds = float(selected.get("_claim_persistence_seconds", 0.0))
        prep_persistence_seconds = 0.0
        specs = {item["function"]["name"]: item["function"] for item in self._business_specs(selected)}
        metas: dict[str, dict] = {}
        previews: dict[str, Any] = {}

        for call in batch:
            if "extension_binding" not in call or not _flight_has_server_hint_read(call):
                return False
            try:
                if hasattr(self.tools, "verify_extension_binding"):
                    self.tools.verify_extension_binding(call)
                meta = self.tools.metadata(call["name"])
            except AgentError:
                return False
            if (
                meta.get("effect") != "write"
                or call.get("effect") != "write"
                or meta.get("extension_binding") != call["extension_binding"]
                or not supports_parallel(call["name"], meta, call.get("arguments"), permission_mode="automatic")
                or call["name"] not in specs
            ):
                return False
            try:
                if "schema_snapshot" in call:
                    assert_schema_unchanged(call, specs[call["name"]])
                validate_tool_arguments(specs[call["name"]]["parameters"], call["arguments"])
                subagents.check_subagent_tool_policy(selected, call["name"], call["arguments"])
            except (ValueError, AgentError):
                return False
            preview = None
            if hasattr(self.tools, "preview"):
                try:
                    preview_ctx = (
                        self._execution_context(selected, call["operation_id"])
                        if meta.get("scope_context_supported")
                        else None
                    )
                    if preview_ctx is not None:
                        preview_ctx["extension_binding"] = copy.deepcopy(call["extension_binding"])
                    preview_options = {"context": preview_ctx} if preview_ctx is not None else {}
                    preview = sanitized(
                        self.tools.preview(call["name"], copy.deepcopy(call["arguments"]), **preview_options)
                    )
                except AgentError:
                    return False
                if len(encoded(preview)) > max(
                    selected["limits"]["max_output_chars"], output_policy.MAX_TOOL_CALL_ARGUMENT_BYTES
                ):
                    return False
            metas[call["call_id"]] = meta
            previews[call["call_id"]] = preview

        t0 = time.monotonic()
        self._reserve_output(selected)
        prep_persistence_seconds += max(0.0, time.monotonic() - t0)
        call_ids = [call["call_id"] for call in batch]
        authorized_classifications: dict[str, dict] = {}

        def authorize(data):
            row = task(data, selected["id"])
            if (
                row["status"] != "running"
                or not data["settings"]["enabled"]
                or row["control_revision"] != selected["_run_control_revision"]
                or self._effective_task_permission_mode(row, data["settings"]) != "automatic"
                or row.get("approved")
                or [item["call_id"] for item in row["queue"][: len(batch)]] != call_ids
            ):
                return None
            last_owner_at = next(
                (str(item.get("at") or "") for item in reversed(row["events"]) if item["type"] in OWNER_TURN_EVENTS),
                "",
            )
            for call in batch:
                if hasattr(self.tools, "verify_extension_binding"):
                    try:
                        self.tools.verify_extension_binding(call)
                    except AgentError:
                        return None
                meta = metas[call["call_id"]]
                decision = decide(call, meta, row, data["settings"])
                if decision.action != "execute" or decision.basis != "automatic":
                    return None
                args_hash = digest(call["arguments"])
                if row.get("model_history") and any(
                    receipt["goal_revision"] == row["goal"]["revision"]
                    and receipt["tool"] == call["name"]
                    and receipt["arguments_sha256"] == args_hash
                    and receipt["status"] == "confirmed"
                    and receipt.get("context_epoch", 0) != row.get("context_epoch", 0)
                    for receipt in row.get("receipts", [])
                ):
                    return None
                if any(
                    receipt["goal_revision"] == row["goal"]["revision"]
                    and receipt["tool"] == call["name"]
                    and receipt["arguments_sha256"] == args_hash
                    and (
                        receipt.get("result", {}).get("error") == "owner_denied_operation"
                        or (
                            str(receipt.get("started_at") or "") > last_owner_at
                            and isinstance(receipt.get("result"), dict)
                            and receipt["result"].get("side_effects") == "unknown"
                        )
                    )
                    for receipt in row.get("receipts", [])
                ):
                    return None
            records = budget.initialize(row)["records"]
            reservation_start = len(records)
            try:
                reservations = [budget.reserve(row, "tool", call["name"]) for call in batch]
            except AgentError:
                del records[reservation_start:]
                return None
            flights: dict[str, dict] = {}
            for call in batch:
                meta = metas[call["call_id"]]
                preview = previews[call["call_id"]]
                classification = classify_operation(
                    call["name"],
                    call["arguments"],
                    meta,
                    operation_id=call["operation_id"],
                    task_id=row["id"],
                    workspace_root=(row.get("local_execution") or {}).get("workspace_root", ""),
                    goal_revision=row["goal"]["revision"],
                    scope_revision=row["scope_revision"],
                    context_epoch=row.get("context_epoch", 0),
                    control_revision=row["control_revision"],
                )
                authorized_classifications[call["call_id"]] = classification
                event(
                    row,
                    "automatically_authorized",
                    tool=call["name"],
                    arguments_sha256=digest(call["arguments"]),
                    preview=preview,
                )
                flight = flights[call["call_id"]] = {
                    **call,
                    "kind": "tool",
                    "dispatched": True,
                    "authorization": "automatic",
                    "workstation_permission_tool": meta.get("workstation_permission_tool"),
                    "operation_classification": classification,
                    "goal_revision": row["goal"]["revision"],
                    "control_revision": row["control_revision"],
                    "scope_revision": row["scope_revision"],
                    "context_epoch": row.get("context_epoch", 0),
                    "authority": model_retry_authority(data["settings"]),
                    "preview": preview,
                    "permission_record": {
                        "basis": "automatic",
                        "operation_id": call["operation_id"],
                        "arguments_sha256": digest(call["arguments"]),
                        "preview_sha256": digest(preview),
                        "goal_revision": row["goal"]["revision"],
                        "scope_revision": row["scope_revision"],
                        "context_epoch": row.get("context_epoch", 0),
                        "control_revision": row["control_revision"],
                    },
                }
                verification.record(row, call, "intent", now(), flight=flight)
                event(
                    row,
                    "tool_started",
                    tool=call["name"],
                    call_id=call["call_id"],
                    operation_id=call["operation_id"],
                    effect="write",
                    step=row["steps"],
                )
            row["in_flight"] = {**flights[batch[0]["call_id"]], "batch_call_ids": call_ids}
            return reservations

        t0 = time.monotonic()
        reservations = self.store.transaction(authorize)
        prep_persistence_seconds += max(0.0, time.monotonic() - t0)
        if reservations is None:
            return False
        post_persistence_seconds = 0.0
        reservation_ids = [r["id"] for r in reservations]
        if cancelled():
            t0 = time.monotonic()
            self._settle_usage_batch(selected["id"], reservation_ids, elapsed=time.monotonic() - started)
            post_persistence_seconds += max(0.0, time.monotonic() - t0)
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")

        def run(call: dict):
            with diagnostic_log.context_scope(
                task_id=selected["id"],
                step=int(selected.get("steps", 0)),
                operation_id=call["operation_id"],
            ):
                call_started = time.monotonic()
                meta = metas[call["call_id"]]
                options: dict[str, Any] = {"operation_id": call["operation_id"], "cancelled": cancelled}
                if meta.get("scope_context_supported"):
                    ctx = self._tool_context(selected, call["operation_id"])
                    ctx["context_epoch"] = selected.get("context_epoch", 0)
                    ctx["extension_binding"] = copy.deepcopy(call["extension_binding"])
                    if call["call_id"] in authorized_classifications:
                        ctx["operation_classification"] = copy.deepcopy(authorized_classifications[call["call_id"]])
                    options["context"] = ctx
                if meta.get("preview_precondition"):
                    options["expected_preview"] = copy.deepcopy(previews[call["call_id"]])
                try:
                    res = sanitized(
                        self._execute_business_tool(
                            selected, call["name"], copy.deepcopy(call["arguments"]), **options
                        )
                    )
                    return res, "confirmed", False, max(0.0, time.monotonic() - call_started)
                except AgentToolRejected as exc:
                    code = str(exc) if str(exc).startswith("agent_") else "agent_operation_failed"
                    extra: dict[str, Any] = {}
                    if getattr(exc, "correction", None):
                        extra["correction"] = sanitized(exc.correction[:1600])
                    if isinstance(getattr(exc, "details", None), dict) and exc.details:
                        extra["details"] = sanitized(copy.deepcopy(exc.details))
                    return (
                        {"error": code, "not_executed": True, "no_mutation": True, **extra},
                        "not_executed",
                        True,
                        max(0.0, time.monotonic() - call_started),
                    )
                except AgentError as exc:
                    code = str(exc) if str(exc).startswith("agent_") else "agent_operation_failed"
                    if code == "agent_mcp_outcome_unknown_reported":
                        return (
                            {
                                "error": code,
                                "outcome": "unknown",
                                "not_executed": False,
                                "hint": MCP_UNKNOWN_OUTCOME_HINT,
                                "note": UNKNOWN_OUTCOME_NOTE,
                            },
                            "unknown",
                            True,
                            max(0.0, time.monotonic() - call_started),
                        )
                    return (
                        {"error": code, "outcome": "unknown", "not_executed": False, "note": UNKNOWN_OUTCOME_NOTE},
                        "unknown",
                        True,
                        max(0.0, time.monotonic() - call_started),
                    )
                except Exception:
                    return (
                        {
                            "error": "agent_mcp_outcome_unknown_reported",
                            "outcome": "unknown",
                            "not_executed": False,
                            "hint": MCP_UNKNOWN_OUTCOME_HINT,
                            "note": UNKNOWN_OUTCOME_NOTE,
                        },
                        "unknown",
                        True,
                        max(0.0, time.monotonic() - call_started),
                    )

        exec_started = time.monotonic()
        preparation_seconds = max(0.0, (exec_started - preparation_started) - prep_persistence_seconds)
        try:
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="ryn-agent-mcp-read") as pool:
                outcomes = list(pool.map(run, batch))
            tool_execution_seconds = max(0.0, time.monotonic() - exec_started)
        finally:
            t0 = time.monotonic()
            self._settle_usage_batch(selected["id"], reservation_ids, elapsed=time.monotonic() - started)
            post_persistence_seconds += max(0.0, time.monotonic() - t0)

        store_persistence_seconds = claim_persistence_seconds + prep_persistence_seconds + post_persistence_seconds
        lead_timing = {
            "scheduler_wait_seconds": round(float(selected.get("_scheduler_wait_seconds", 0.0)), 4),
            "preparation_seconds": round(preparation_seconds, 4),
            "tool_execution_seconds": round(tool_execution_seconds, 4),
            "store_persistence_seconds": round(store_persistence_seconds, 4),
        }

        def finish(data):
            row = task(data, selected["id"])
            row["in_flight"] = None
            row["elapsed_seconds"] += time.monotonic() - started
            notice = self._budget_notice(row)
            for index, (call, (result, rec_status, is_err, call_exec_seconds)) in enumerate(
                zip(batch, outcomes, strict=True)
            ):
                reference = None
                meta = metas[call["call_id"]]
                origin = (
                    copy.deepcopy(result.get("origin"))
                    if meta.get("scope_context_supported")
                    and isinstance(result.get("origin"), dict)
                    and "grant_id" in result["origin"]
                    else None
                )
                size = len(encoded(result))
                if size > selected["limits"]["max_output_chars"]:
                    reference = results.save(self.store, row, call, result)
                    if origin:
                        reference["resource_origin"] = origin
                    row.setdefault("result_refs", []).append(reference)
                    result = results.format_reference_result(
                        call["name"],
                        result,
                        reference,
                        max_bytes=selected["limits"]["max_output_chars"],
                    )
                shown = (
                    {**result, "server_note": notice}
                    if notice and index == len(batch) - 1 and isinstance(result, dict)
                    else result
                )
                message = {
                    "role": "tool",
                    "tool_call_id": call["call_id"],
                    "content": encoded(shown).decode(),
                    "context_epoch": selected.get("context_epoch", 0),
                }
                if is_err or (isinstance(result, dict) and result.get("error")):
                    message["is_error"] = True
                if origin:
                    message["resource_origin"] = origin
                row["messages"].append(message)
                verification.record(row, call, rec_status, now(), result=result if rec_status != "unknown" else None)
                if rec_status == "unknown":
                    self._record_auto_acknowledged_operation(row, call["operation_id"])
                row["queue"] = [item for item in row["queue"] if item["call_id"] != call["call_id"]]
                item_timing = (
                    {**lead_timing, "call_execution_seconds": round(call_exec_seconds, 4)}
                    if index == 0
                    else {
                        "scheduler_wait_seconds": 0.0,
                        "preparation_seconds": 0.0,
                        "tool_execution_seconds": 0.0,
                        "store_persistence_seconds": 0.0,
                        "call_execution_seconds": round(call_exec_seconds, 4),
                        "batch_shared": True,
                    }
                )
                event(
                    row,
                    "tool_result",
                    tool=call["name"],
                    call_id=call["call_id"],
                    operation_id=call["operation_id"],
                    step=row["steps"],
                    result=result,
                    context_epoch=selected.get("context_epoch", 0),
                    timing=item_timing,
                )
            if row["status"] == "running":
                row["status"] = "queued"
            memory_integration.settle_in_tx(data, row, event)

        self.store.transaction(finish)
        return True

    def _process_boot_id(self) -> str:
        proc_mgr = getattr(self.tools, "processes", None) or getattr(
            getattr(self.tools, "local", None), "processes", None
        )
        boot_id = getattr(proc_mgr, "boot_id", None)
        return boot_id if isinstance(boot_id, str) and boot_id else self._boot_id

    @property
    def _active_run(self):
        with self._active_lock:
            return self._active_runs.get(threading.get_ident())

    @_active_run.setter
    def _active_run(self, value):
        with self._active_lock:
            if value is None:
                self._active_runs.pop(threading.get_ident(), None)
            else:
                self._active_runs[threading.get_ident()] = value

    def _task_run(self, task_id):
        with self._active_lock:
            return next((run.copy() for run in self._active_runs.values() if run["task_id"] == task_id), None)

    def activity_counts(self) -> dict[str, Any]:
        """Lightweight task activity summary for the desktop watchdog without copying histories."""
        with self._active_lock:
            running = len(self._active_runs)
            active_task_ids = [
                run["task_id"]
                for run in self._active_runs.values()
                if isinstance(run, dict) and isinstance(run.get("task_id"), str)
            ]
        active_id_set = set(active_task_ids)

        def _view(data: dict) -> dict[str, Any]:
            tasks = data.get("tasks") or {}
            queued = 0
            retrying = 0
            waiting_approval = 0
            waiting_input = 0
            titles: list[str] = []
            seen_title_ids: set[str] = set()

            for tid in active_task_ids:
                if tid in seen_title_ids or len(titles) >= 3:
                    continue
                row = tasks.get(tid)
                if isinstance(row, dict):
                    seen_title_ids.add(tid)
                    raw_title = " ".join(str(row.get("title") or "").split())[:40]
                    titles.append(raw_title or tid[:40])

            queued_rows: list[tuple[str, str, dict]] = []
            for tid, row in tasks.items():
                if not isinstance(row, dict):
                    continue
                status = row.get("status")
                if status == "waiting_approval":
                    waiting_approval += 1
                    continue
                if status == "waiting_input":
                    waiting_input += 1
                    continue
                if tid in active_id_set:
                    continue
                if (row.get("fork") or {}).get("awaiting_instruction"):
                    continue
                recovery_state = (row.get("connection_recovery") or {}).get("state")
                is_retrying = recovery_state == "retrying" or (status == "queued" and not retry_policy.ready(row))
                if status == "queued" or is_retrying:
                    queued += 1
                    if is_retrying:
                        retrying += 1
                    queued_rows.append((str(row.get("updated_at") or ""), str(tid), row))

            if len(titles) < 3 and queued_rows:
                queued_rows.sort(key=lambda item: item[0])
                for _updated, tid, row in queued_rows:
                    if tid in seen_title_ids:
                        continue
                    seen_title_ids.add(tid)
                    raw_title = " ".join(str(row.get("title") or "").split())[:40]
                    titles.append(raw_title or tid[:40])
                    if len(titles) >= 3:
                        break

            return {
                "running": running,
                "queued": queued,
                "retrying": retrying,
                "waiting_approval": waiting_approval,
                "waiting_input": waiting_input,
                "agent_active_runs": running + queued,
                "agent_waiting": waiting_approval + waiting_input,
                "agent_task_titles": titles,
            }

        return self.store.inspect(_view)

    def _start_stream(self, selected, step):
        # Live text is a display projection, separate from encrypted recovery
        # checkpoints. The provider callback must not wait on a disk commit for
        # every token, and clients need not wait for the next checkpoint.
        stream = {"step": step, "context_epoch": selected.get("context_epoch", 0),
                  "control_revision": selected["_run_control_revision"], "text": ""}
        with self._stream_lock:
            self._streams[selected["id"]] = stream
        self._step.stream = stream
        return stream

    def _append_stream(self, task_id, stream, text):
        with self._stream_lock:
            if self._streams.get(task_id) is stream:
                stream["text"] += text

    def _clear_stream(self, task_id, stream):
        with self._stream_lock:
            # A following worker may already own the task's next model step.
            if self._streams.get(task_id) is stream:
                self._streams.pop(task_id, None)

    def _stream_snapshot(self, row):
        with self._stream_lock:
            stream = self._streams.get(row["id"])
            flight = row.get("in_flight") or {}
            if (not stream or not stream["text"] or row["status"] != "running"
                    or flight.get("kind") != "model" or not flight.get("dispatched")
                    or stream["control_revision"] != row["control_revision"]
                    or stream["step"] != row["steps"]
                    or stream["context_epoch"] != row.get("context_epoch", 0)):
                return None
            return {key: stream[key] for key in ("step", "context_epoch", "text")}

    def _context_limit(self, row):
        """A model-derived projection; never overwrite the owner's saved limit."""
        return self._context_policy(row)["effective_max_context_bytes"]

    def _context_policy(self, row):
        model = self._model_metadata(row["model_id"]) or {}
        return output_policy.context_budget(model, row["limits"].get("max_context_chars"),
                                            provenance=row.get("context_budget_provenance"))

    def _business_specs(self, row=None):
        if row is not None and getattr(self.tools, "supports_model_context", False) is True:
            return self.tools.specs(context=self._execution_context(row))
        return self.tools.specs()

    def _policy_context(self, row):
        destination = copy.deepcopy(row.get("model_destination") or {})
        context = {"consumer_node_id": self.consumer_node_id, "model_destination": destination,
                   "model_destination_verified": False}
        inspect_destination = getattr(self.model, "data_policy_context", None)
        if callable(inspect_destination):
            try:
                context.update(inspect_destination(row["model_id"], destination))
            except (AgentError, ValueError, KeyError):
                pass  # Unverifiable destinations never match an allowlist.
        return context

    def _admit_extension_policy(self, selected, snapshot, *, persist=True):
        # This host-only callback is carried through ordinary and Code Mode calls.
        # Persist before dispatch, even when the eventual tool call fails.
        current = self.store.inspect(lambda data: copy.deepcopy(task(data, selected["id"])))
        if (current.get("control_revision") != selected.get("control_revision")
                or current["model_id"] != selected["model_id"]
                or current.get("model_destination") != selected.get("model_destination")):
            raise AgentToolRejected("agent_extension_model_not_allowed")
        context = self._policy_context(current)
        merged = data_policy.merge_state(current.get("data_policy_state"), snapshot)
        if not data_policy.allows_task(merged, context):
            raise AgentToolRejected("agent_extension_model_not_allowed")
        if not persist:
            return
        def update(data):
            row = task(data, selected["id"])
            if (row.get("control_revision") != current.get("control_revision")
                    or row["model_id"] != current["model_id"]
                    or row.get("model_destination") != current.get("model_destination")):
                raise AgentToolRejected("agent_extension_model_not_allowed")
            state = data_policy.merge_state(row.get("data_policy_state"), snapshot)
            if not data_policy.allows_task(state, context):
                raise AgentToolRejected("agent_extension_model_not_allowed")
            changed = row.get("data_policy_state") != state
            row["data_policy_state"] = state
            automations.inherit_task_policy_in_tx(data, row)
            if data_policy.memory_denied(state) or data_policy.task_requires_model_restriction(state):
                from .memory import invalidate_task_policy_in_tx
                invalidate_task_policy_in_tx(data, row)
            if changed:
                event(row, "data_policy_restricted", policy_revision=state["revision"])
        self.store.transaction(update)

    def extension_model_availability(self, model_id):
        model = self._model_metadata(model_id)
        if not model:
            raise AgentError("agent_model_unavailable")
        context = self._policy_context({"model_id": model_id, "model_destination": self._destination(model)})
        extensions = getattr(self.tools, "extensions", None)
        servers = extensions.store.list_enabled_mcp_servers_internal() if extensions else []
        return {"servers": [{"id": server["id"], "name": server.get("name") or server["id"],
                             "allowed": data_policy.allows(server.get("data_policy"), context)}
                            for server in servers]}

    def _menu(self, row, *, business_specs=None, max_context_bytes=None):
        business_specs = self._business_specs(row) if business_specs is None else business_specs
        if any(spec["function"]["name"] in ALL_INTERNAL_NAMES for spec in business_specs):
            raise AgentError("agent_tool_definition_changed")
        # Tool schemas share the calibrated input capacity used by dispatch and compaction.
        max_context_bytes = self._effective_capacity(row, max_context_bytes)[0]
        owners = [message for message in row["messages"] if message["role"] == "user"]
        trailing_unanswered_owner = bool(row["messages"] and row["messages"][-1].get("role") == "user")
        pinned_through = max(0, len(owners) - (1 if trailing_unanswered_owner else 0))
        pinned = {**row, "messages": owners,
                  "context_checkpoint": {"through": pinned_through, "content": ""},
                  "_exclude_optional_memory": True,
                  "_max_context_bytes_override": max_context_bytes}
        authority_bytes = len(encoded(self._context(pinned)))
        desktop = tool_menu.BROWSER_TOOLS if (row.get("local_execution") or {}).get("desktop_access") else ()
        if desktop:
            # Expose installed desktop schemas immediately; dispatch still checks
            # the provider binding and workstation grants on every operation.
            computer_use = tuple(spec["function"]["name"] for spec in business_specs
                                 if spec["function"]["name"].startswith("mcp__computer_use__computer_"))
            desktop = computer_use + desktop
        skill_tools = ("ryn_read_skill_file",) if any(isinstance(s, dict) and s.get("files") for s in (row.get("skills") or [])) else ()
        resource_tools = (
            ("ryn_list_resource_roots", "ryn_list_resources", "ryn_create_directory", "ryn_upload_resource_file", "ryn_create_text_file")
            if (not row["scope"].get("resource_grants") and "selection" not in row["scope"]
                and self._execution_context(row).get("resource_grants"))
            else ()
        )
        selected_skill_ids = {skill.get("id") for skill in row.get("skills", [])}
        selected_plugins = {
            plugin["id"] for plugin in (self.extensions.plugin_catalog() if self.extensions and selected_skill_ids else [])
            if plugin["enabled"] and selected_skill_ids.intersection(plugin["skill_ids"])
        }
        preloads = tool_menu.plugin_preloads(business_specs, self._metadata, selected_plugins) if selected_plugins else ()
        return tool_menu.project(business_specs, self._internal_specs(row), row.get("tool_menu"),
            max_context_bytes=max_context_bytes, authority_bytes=authority_bytes,
            epoch=row.get("context_epoch", 0), goal_revision=row["goal"]["revision"],
            # Pins need room; in small windows the earlier seeds still apply.
            pinned_names=tool_menu.PLUGIN_DISCOVERY_TOOLS + (("ryn_project_inspect",) if row.get("project") else ()) + tool_menu.CORE_TOOLS + ("ryn_code_exec",) + desktop + skill_tools + resource_tools,
            initial_names=tool_menu.PLUGIN_DISCOVERY_TOOLS + preloads + (("ryn_project_inspect", "ryn_file_read", "ryn_file_search", "ryn_file_edit",
                           "ryn_shell_execute", "ryn_file_write", "ryn_file_list", "ryn_code_exec") if row.get("project") else
                          ("ryn_shell_execute", "ryn_file_read", "ryn_file_search", "ryn_code_exec")) + desktop + skill_tools + resource_tools), authority_bytes

    @staticmethod
    def _internal_specs(row):
        return [spec for spec in ALL_INTERNAL_SPECS
                if spec["function"]["name"] not in HISTORY_TOOL_NAMES or (row or {}).get("context_checkpoint")]

    def _specs(self, row=None) -> list:
        specs = self._business_specs(row)
        if any(spec["function"]["name"] in ALL_INTERNAL_NAMES for spec in specs):
            raise AgentError("agent_tool_definition_changed")
        if row is not None:
            return self._menu(row)[0][0]
        return [*specs, *(copy.deepcopy(spec) for spec in self._internal_specs(row) if spec["function"]["name"] != tool_menu.NAME)]

    def _metadata(self, name: str) -> dict:
        return {"effect": "read", "title": name, "internal": True} if name in ALL_INTERNAL_NAMES else self.tools.metadata(name)

    def _history(self, row: dict) -> list[dict]:
        if "_history_override" in row:
            return copy.deepcopy(row["_history_override"])
        epoch = row.get("context_epoch", 0)
        return [message for message in row["messages"] if message["role"] == "user" or message.get("context_epoch", 0) == epoch]

    def _execution_context(self, row: dict, operation_id=None, *, cheap_only=False) -> dict:
        origins = {}
        inherited = row.get("continuation_resource_origins") or {}
        if inherited.get("context_epoch") == row.get("context_epoch", 0):
            for origin in inherited.get("origins", []):
                origins[digest(origin)] = copy.deepcopy(origin)
        for message in self._history(row):
            if message.get("role") == "tool" and isinstance(message.get("resource_origin"), dict) and "grant_id" in message["resource_origin"]:
                origins[digest(message["resource_origin"])] = copy.deepcopy(message["resource_origin"])
        model_destination = copy.deepcopy(row.get("model_destination") or {"catalog_model_id": row["model_id"], "provider_peer_id": "", "source_id": "", "destination_revision": ""})
        resource_grants = copy.deepcopy(row["scope"]["resource_grants"])
        egress_grants = copy.deepcopy(row["scope"]["egress_grants"])
        if (not cheap_only and not resource_grants and "selection" not in row["scope"]
                and hasattr(self.tools, "default_scope")
                and getattr(self.model, "supports_destination_binding", False) is True
                and model_destination.get("destination_revision")):
            try:
                if self._current_destination(row["model_id"], require_bound=True) == model_destination:
                    auto_scope = self.tools.default_scope(
                        task_id=row["id"],
                        owner_id=self.owner_id,
                        consumer_node_id=self.consumer_node_id,
                        model_destination=model_destination,
                        scope_revision=row["scope_revision"],
                    )
                    if auto_scope.get("resource_grants"):
                        resource_grants = auto_scope["resource_grants"]
                        egress_grants = auto_scope["egress_grants"]
            except Exception:
                pass
        # Cancellation checks validate retained grants and local control state.
        # They must not discover models or synthesize new resource authority on
        # every stream chunk. Full checks still run at each dispatch boundary.
        policy_context = ({"consumer_node_id": self.consumer_node_id, "model_destination": model_destination,
                           "model_destination_verified": False} if cheap_only else self._policy_context(row))
        return {**policy_context,
                "_admit_extension_policy": _ExtensionPolicyAdmitter(self, row),
                "task_id": row["id"], "operation_id": operation_id, "owner_id": self.owner_id,
                "consumer_node_id": self.consumer_node_id, "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"], "scope_revision": row["scope_revision"],
                "settings_revision": self.store.inspect(lambda data: data.get("settings_revision", 0)),
                "local_execution": copy.deepcopy(row.get("local_execution") or {}),
                "model_destination": model_destination,
                "resource_grants": resource_grants,
                "egress_grants": egress_grants, "resource_origins": list(origins.values())}

    @staticmethod
    def _requires_destination_binding(row: dict, context: dict) -> bool:
        # Peer inference carries owner text even without resource access. Old
        # tasks already retain their selected destination; never replace that
        # snapshot with today's catalog while reopening them.
        return bool(row.get("project") or context["resource_grants"] or context["resource_origins"]
                    or row.get("model_destination_confirmed") or row.get("model_destination_required")
                    or row["model_id"].startswith("peer/"))

    def _validate_context(self, row: dict, *, cheap_only=False, control_state=None) -> None:
        if row.get("project"):
            if control_state is not None:
                projects.validate_binding(control_state, row["project"])
            elif hasattr(self.store, "inspect"):
                self.store.inspect(lambda data: projects.validate_binding(data, row["project"]))
            else:
                projects.validate_binding(self.store.read(), row["project"])
        context = self._execution_context(row, cheap_only=cheap_only)
        if self._requires_destination_binding(row, context):
            if not context["model_destination"].get("destination_revision") or getattr(self.model, "supports_destination_binding", False) is not True:
                raise AgentError("agent_model_destination_unverifiable")
            if not cheap_only and self._current_destination(row["model_id"], require_bound=True) != context["model_destination"]:
                raise AgentError("agent_model_destination_changed")
        if hasattr(self.tools, "validate_context"):
            if cheap_only:
                self.tools.validate_context(context, cheap_only=True)
            else:
                self.tools.validate_context(context)
        elif row["scope"]["resource_grants"]:
            raise AgentError("agent_resources_unavailable")

    @staticmethod
    def _destination(model: dict) -> dict:
        return {"catalog_model_id": model["id"], "provider_peer_id": model.get("peer_id", ""),
                "source_id": model.get("source_id", ""), "destination_revision": model.get("destination_revision", "")}

    def _current_destination(self, model_id: str, *, require_bound=False) -> dict:
        model = self._model_metadata(model_id)
        if model is None:
            raise AgentError("agent_model_unavailable")
        return self._bound_destination(model) if require_bound else self._destination(model)

    def _bound_destination(self, model: dict) -> dict:
        """Only locally authenticated catalog evidence can authorize egress."""
        from ..llm_package import peer_binding
        if (model.get("source") == "peer" and not model.get("destination_binding_supported")
                and hasattr(self.catalog, "refresh_destination")):
            model = self.catalog.refresh_destination(model["id"]) or model
        destination = self._destination(model)
        supported = model.get("source") == "local_api"
        if model.get("source") == "local_service":
            binding = model.get("local_model_binding")
            service_id = str(model.get("service_id", ""))
            native = model.get("runtime") == "native_llama_cpp"
            cli = model.get("runtime") == "external" and model.get("adapter") in {"codex_cli", "antigravity_cli"}
            expected_id = "local/" + service_id + ("/model/" + model["cli_model"] if cli and model.get("cli_model") else "")
            supported = ((native or cli)
                         and model.get("peer_id") == self.consumer_node_id
                         and model.get("id") == expected_id
                         and model.get("destination_binding_supported") is True
                         and peer_binding.valid(binding, model.get("service_id"))
                         and peer_binding.local_revision(binding, self.consumer_node_id) == destination["destination_revision"])
        if model.get("source") == "peer" and model.get("destination_binding_supported") is True:
            bound = model.get("expected_model_binding")
            if isinstance(bound, dict) and set(bound) in (
                    {"model_binding", "ai_permission", "consumer_peer_id"},
                    {"model_binding", "external_permission", "consumer_peer_id"},
                    {"model_binding", "space_permission", "consumer_peer_id", "consumer_access_generation"}):
                record = {**bound, "binding_verified": True, "peer_id": model.get("peer_id"),
                          "service_id": model.get("service_id")}
                supported = (model.get("consumer_peer_id") == self.consumer_node_id
                             and peer_binding.expected(record, self.consumer_node_id) is not None
                             and peer_binding.revision(record, self.consumer_node_id) == destination["destination_revision"])
        if (not supported or not destination["destination_revision"]
                or getattr(self.model, "supports_destination_binding", False) is not True):
            raise AgentError("agent_model_destination_unverifiable")
        return destination

    def _prepare_scope(self, selection: Any, destination: dict, expected_revision: Any) -> dict:
        if not hasattr(self.tools, "prepare_scope"):
            raise AgentError("agent_resources_unavailable")
        if isinstance(selection, dict) and selection.get("resources"):
            if not destination.get("destination_revision") or getattr(self.model, "supports_destination_binding", False) is not True:
                raise AgentError("agent_model_destination_unverifiable")
            if expected_revision != destination["destination_revision"]:
                raise AgentError("agent_model_destination_changed")
            if self._current_destination(destination["catalog_model_id"], require_bound=True) != destination:
                raise AgentError("agent_model_destination_changed")
        return self.tools.prepare_scope(selection, owner_id=self.owner_id, consumer_node_id=self.consumer_node_id,
                                        model_destination=destination)

    def _native_artifact_loader(self):
        from .native_computer import NativeComputerFacade

        # __init__ binds this facade to this exact store. Reading it here must not
        # create a worker or reuse another runtime owner's registry.
        facade = getattr(self.store, "_ryn_native_computer", None)
        if isinstance(facade, NativeComputerFacade) and facade._owner is self.store:
            return facade.verify_and_load_registered_artifact
        return None

    def _prepare_dispatch_messages(self, row: dict, messages: list[dict] | None = None):
        msgs = self._context(row) if messages is None else messages
        model = self._model_metadata(row["model_id"]) or {}
        return image_capabilities.materialize_operation_images(
            row,
            msgs,
            model_row=model,
            model_client=self.model,
            native_artifact_loader=self._native_artifact_loader(),
        )

    @staticmethod
    def _prompt_cache_key(row: dict) -> str | None:
        sub_meta = row.get("subagent_meta")
        is_subagent = isinstance(sub_meta, dict) and bool(sub_meta.get("root_task_id") or row.get("parent_task_id"))
        if is_subagent:
            root_id = str(sub_meta.get("root_task_id") or row.get("parent_task_id") or row.get("id") or "")
            raw_key = f"{root_id}:subagent" if root_id else ""
        elif isinstance(row.get("fork"), dict) and isinstance(row["fork"].get("source_task_id"), str) and row["fork"]["source_task_id"]:
            raw_key = row["fork"]["source_task_id"]
        else:
            raw_key = str(row.get("id") or "")
        if not raw_key:
            return None
        if len(raw_key.encode("utf-8")) <= 64 and re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", raw_key):
            return raw_key
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:48]
        return f"{digest}:subagent" if is_subagent else digest

    def _complete(self, row: dict, messages: list, specs: list, cancelled, on_event, *, nested=False, stage=None, output_cap=None, request_id=None, image_context=None, reasoning_override=None):
        # Measure the final provider-visible projection, including JSON string
        # escaping and validated inline image bounds, before reserving or dispatching inference.
        model = self._model_metadata(row["model_id"]) or {}
        context_policy = self._context_policy(row)

        def admit_effective_capacity(analysis):
            # Every model request (main, summary, checker, image inspection)
            # must also fit the window at this model's observed token density.
            size, capacity = self._effective_request_fit(row, analysis, context_policy)
            if size > capacity:
                raise AgentError("agent_context_limit")
        admit_effective_capacity(output_policy.admit_request_context(
            model,
            messages,
            specs,
            manual_limit=row["limits"].get("max_context_chars"),
            provenance=row.get("context_budget_provenance"),
            context_policy=context_policy,
        ))
        context = self._execution_context(row)
        options = {}
        discloses_memory = memory_integration.messages_disclose_memory(messages, row=row) or bool(row.get("memory_disclosed_in_epoch"))
        if self._requires_destination_binding(row, context):
            if not context["model_destination"].get("destination_revision") or getattr(self.model, "supports_destination_binding", False) is not True:
                raise AgentError("agent_model_destination_unverifiable")
            options["expected_destination"] = context["model_destination"]
        elif discloses_memory:
            ok_dest, verified_dest, _ = memory_integration.verify_task_destination_for_memory(self, row)
            if not ok_dest or verified_dest is None:
                raise AgentError("agent_model_destination_unverifiable")
            options["expected_destination"] = verified_dest
        if getattr(self.model, "supports_request_deadline", False) is True:
            if "_run_started" not in row or "_run_lifetime_remaining" not in row:
                raise AgentError("agent_request_deadline_unavailable")
            # Process-local monotonic values never enter durable task state.
            # Preparation and reservation time consume this same active tick.
            seconds_left = budget.run_seconds_left(row, row["_run_lifetime_remaining"])
            options["deadline"] = row["_run_started"] + seconds_left if math.isfinite(seconds_left) else None
            def record_request_policy(policy):
                self.store.transaction(lambda data: budget.record_request_policy(task(data, row["id"]), policy))
            options["on_request_policy"] = record_request_policy
        active_request_id = (
            request_id
            if isinstance(request_id, str) and request_id
            else row.get("_request_id")
            if not nested and isinstance(row.get("_request_id"), str) and row.get("_request_id")
            else "req_" + uuid.uuid4().hex
        )
        if active_request_id and getattr(self.model, "supports_request_id", False) is True:
            options["request_id"] = active_request_id

        override_effort = None
        if (reasoning_override is not None and "reasoning_effort" not in row
                and getattr(self.model, "supports_reasoning_effort", False) is True):
            try:
                override_effort = reasoning.validate(model, reasoning_override)
            except AgentError:
                override_effort = None
        if override_effort is not None:
            options["reasoning_effort"] = override_effort
        elif "reasoning_effort" in row:
            options["reasoning_effort"] = reasoning.resolve(model, row["reasoning_effort"])
            if getattr(self.model, "supports_reasoning_effort", False) is not True:
                raise AgentError("agent_reasoning_effort_unsupported")
        # The window's own conversation may continue a provider thread across
        # turns. Summaries (stage="compaction") and nested checks are separate requests.
        if not nested and stage is None and specs and getattr(self.model, "supports_conversation_key", False) is True:
            conv_key = (row["id"] + ":edit:" + str(row["conversation_revision"])
                        if row.get("conversation_revision") else row["id"])
            if row.get("memory_conversation_epoch"):
                conv_key = f"{conv_key}:mem:{row['memory_conversation_epoch']}"
            options["conversation"] = conv_key
        if getattr(self.model, "supports_cache_key", False) is True:
            cache_key = self._prompt_cache_key(row)
            if cache_key:
                options["cache_key"] = cache_key
        dispatch_messages = messages
        image_report = None
        if not nested and stage is None:
            dispatch_messages, image_report = image_capabilities.materialize_operation_images(
                row,
                messages,
                model_row=model,
                model_client=self.model,
                native_artifact_loader=self._native_artifact_loader(),
            )
            admit_effective_capacity(output_policy.admit_request_context(
                model,
                dispatch_messages,
                specs,
                manual_limit=row["limits"].get("max_context_chars"),
                provenance=row.get("context_budget_provenance"),
                context_policy=context_policy,
            ))
        # An image the selected route cannot carry becomes a short note, so a
        # picture in the history never fails this or any later request. Only an
        # explicit image inspection still fails as a tool error.
        if stage != "image_inspection" and image_capabilities.message_has_image_input(dispatch_messages):
            route_cap = image_capabilities.evaluate_image_capability(model, model_client=self.model)
            if route_cap["supports_image_input"] is not True:
                dispatch_messages, omitted, latest = image_capabilities.omit_images(dispatch_messages)
                if omitted and not nested:
                    self._note_images_omitted(row["id"], omitted, route_cap["image_capability_reason"], latest)
        if image_capabilities.message_has_image_input(dispatch_messages):
            image_capabilities.require_route_image_support(model, model_client=self.model)
        policy = output_policy.select(model, dispatch_messages, specs, request_cap=output_cap)
        profile_record = model_profiles.request_record(model_profiles.for_task(self, row, model),
                                                       dispatch_messages, options)
        meter_row = {**row, "_request_id": active_request_id} if active_request_id else row
        dispatched = False
        def _invoke_model(allowed):
            nonlocal dispatched
            # Read live state, including restrictions added by overlapped reads.
            state = self.store.inspect(lambda data: copy.deepcopy(task(data, row["id"]).get("data_policy_state")))
            if not data_policy.allows_task(state, self._policy_context(row)):
                raise AgentToolRejected("agent_extension_model_not_allowed")
            if data_policy.task_requires_model_restriction(state):
                if getattr(self.model, "supports_data_policy", False) is not True:
                    raise AgentToolRejected("agent_extension_model_not_allowed")
                options["data_policy_state"] = state
                options["expected_destination"] = self._bound_destination(model)
                if options["expected_destination"] != context["model_destination"]:
                    raise AgentToolRejected("agent_extension_model_not_allowed")
                if "conversation" in options:
                    options["conversation"] += ":policy:" + str(state["revision"])
            dispatched = True
            return self.model.complete(
                row["model_id"], dispatch_messages, specs, cancelled, on_event,
                **options, **({"max_output_tokens": allowed} if getattr(self.model, "supports_output_token_limit", False) else {}))
        def _metered():
            # The model tag lets later density calibration ignore another model's usage.
            return self._meter_model(meter_row, _invoke_model,
                input_bound=policy["estimated_input_tokens"], output_bound=policy["effective_cap"],
                output_policy_record={**policy, "model_id": row["model_id"], "model_profile": profile_record},
                nested=nested, stage=stage,
                can_reduce_output=getattr(self.model, "supports_output_token_limit", False) is True)
        try:
            try:
                response = _metered()
            except AgentError as exc:
                # The model or an older sharing computer refused the images: remember
                # that for this route and retry once without them, so the task goes on.
                if (str(exc) != "agent_model_image_rejected" or stage == "image_inspection"
                        or not image_capabilities.message_has_image_input(dispatch_messages)):
                    raise
                image_capabilities.mark_image_rejected(model)
                dispatch_messages, omitted, latest = image_capabilities.omit_images(dispatch_messages)
                if not nested:
                    self._note_images_omitted(row["id"], omitted, "upstream_rejected_image", latest)
                image_report = None
                dispatched = False
                response = _metered()
        except Exception:
            if dispatched:
                if image_report and image_report.get("has_candidate_artifacts") and image_report.get("image_prepared"):
                    attempted_linkage = image_capabilities.build_image_request_linkage(
                        request_id=active_request_id,
                        model_id=row["model_id"],
                        capability=image_report["route_capability"],
                        messages=dispatch_messages,
                        stage="main_loop_mcp_observation",
                        source_artifacts=image_report["materialized_artifacts"],
                        task_row=row,
                        dispatch_status="attempted",
                        semantic_response_received=False,
                    )
                    attempted_linkage["observation_status"] = image_report["status"]
                    attempted_linkage["rejected_artifacts"] = list(image_report["rejected_artifacts"])[:4]
                    self._record_image_request_linkage(row, attempted_linkage)
                elif isinstance(image_context, dict):
                    attempted_linkage = image_capabilities.build_image_request_linkage(
                        request_id=active_request_id,
                        model_id=row["model_id"],
                        capability=image_context.get("capability") or image_capabilities.evaluate_image_capability(model, model_client=self.model),
                        messages=dispatch_messages,
                        stage=str(image_context.get("stage") or stage or "image_inspection"),
                        operation_id=image_context.get("operation_id"),
                        source_artifacts=image_context.get("source_artifacts") or [],
                        task_row=row,
                        dispatch_status="attempted",
                        semantic_response_received=False,
                    )
                    self._record_image_request_linkage(row, attempted_linkage)
                    if isinstance(image_context.get("linkage_out"), dict):
                        image_context["linkage_out"].update(attempted_linkage)
                elif image_capabilities.message_has_image_input(dispatch_messages):
                    attempted_linkage = image_capabilities.build_image_request_linkage(
                        request_id=active_request_id,
                        model_id=row["model_id"],
                        capability=image_capabilities.evaluate_image_capability(model, model_client=self.model),
                        messages=dispatch_messages,
                        stage=str(stage or "model"),
                        task_row=row,
                        dispatch_status="attempted",
                        semantic_response_received=False,
                    )
                    self._record_image_request_linkage(row, attempted_linkage)
            raise
        resize = response.get("shared_image_resize") if isinstance(response, dict) else None
        if isinstance(resize, dict) and resize.get("resized") and not nested:
            self._note_shared_image_resize(row["id"], resize)
        if image_report and image_report.get("has_candidate_artifacts"):
            dispatch_status = "submitted" if image_report.get("image_prepared") else "not_dispatched"
            msg = response.get("message") if isinstance(response, dict) else None
            semantic_ok = bool(
                isinstance(msg, dict)
                and ((isinstance(msg.get("content"), str) and msg["content"].strip()) or msg.get("tool_calls"))
            )
            linkage = image_capabilities.build_image_request_linkage(
                request_id=active_request_id,
                model_id=row["model_id"],
                capability=image_report["route_capability"],
                messages=dispatch_messages,
                stage="main_loop_mcp_observation",
                source_artifacts=image_report["materialized_artifacts"],
                task_row=row,
                dispatch_status=dispatch_status,
                semantic_response_received=semantic_ok,
            )
            linkage["observation_status"] = image_report["status"]
            linkage["rejected_artifacts"] = list(image_report["rejected_artifacts"])[:4]
            self._record_image_request_linkage(row, linkage)
        return response

    def _cached_input_weight(self, row: dict) -> float:
        model = self._model_metadata(row.get("model_id", "")) if isinstance(row, dict) else None
        return budget.infer_cached_input_weight(row, model if isinstance(model, dict) else None)

    def _meter_model(self, selected, invoke, *, input_bound, output_bound, nested=False, stage=None, can_reduce_output=True, output_policy_record=None):
        def admit(data):
            row = task(data, selected["id"])
            if (row["status"] != "running" or row["control_revision"] != selected.get("_run_control_revision", selected["control_revision"])
                    or not data["settings"]["enabled"]):
                raise AgentToolRejected("agent_cancelled")
            budget.ensure_default_room(row, "max_total_tokens", input_bound + output_bound)
            state = budget.public(row)
            max_tokens = state["limits"]["max_total_tokens"]
            wrap_up_reserve = min(100000, max_tokens // 15) if nested and max_tokens >= 100000 else 0
            available = max_tokens - wrap_up_reserve - state["lifetime"]["charged_tokens"] - input_bound
            if available <= 0 or (available < output_bound and not can_reduce_output):
                raise AgentError("agent_token_budget_exhausted")
            flight = row.get("in_flight") or {}
            record = budget.reserve(row, "model", stage or flight.get("stage") or flight.get("kind", "model"),
                input_bound=input_bound, output_bound=min(output_bound, available), seconds_bound=0 if nested else None)
            if output_policy_record:
                policy = output_policy.budgeted(output_policy_record, record["output_reservation"])
                if selected.get("_request_id"):
                    policy["request_id"] = selected["_request_id"]
                if selected.get("_attempt"):
                    policy["attempt"] = selected["_attempt"]
                row["budget"]["records"][-1]["output_policy"] = policy
                event(row, "model_output_policy", **policy)
            return record
        reservation = self.store.transaction(admit)
        started = time.monotonic()
        response = None
        retryable_failure = False
        try:
            response = invoke(reservation["output_reservation"])
            return response
        except AgentModelResponseError as exc:
            response = {"usage": exc.usage}
            rejected_metadata = {"error": str(exc), "finish_reason": exc.finish_reason,
                                 "usage": exc.usage, "stage": reservation["stage"]}
            detail = str(getattr(exc, "detail", "") or "")[:300]
            informative = bool(detail) and reservation["stage"] == "model" and exc.finish_reason != "length"

            def rejected(data):
                row = data["tasks"].get(selected["id"])
                if row is not None:
                    event(row, "model_response_rejected", **rejected_metadata, **({"detail": detail} if detail else {}))
                    if informative:
                        row["rejected_response_note"] = detail
            self.store.transaction(rejected)
            raise
        except AgentError as exc:
            if str(exc) in RETRYABLE_MODEL_ERRORS:
                retryable_failure = True
            raise
        finally:
            if isinstance(response, dict) and response.get("server_model") and response.get("requested_model"):
                requested, served = response["requested_model"], response["server_model"]
                def normalized(name):
                    return re.sub(r"-\d{4}-?\d{2}-?\d{2}$", "", name.rsplit("/", 1)[-1].lower())
                if normalized(requested) != normalized(served):
                    def mismatch(data):
                        row = data["tasks"].get(selected["id"])
                        if row is not None and not row.get("model_mismatch_reported"):
                            row["model_mismatch_reported"] = True
                            event(row, "model_mismatch", requested_model=requested, server_model=served)
                    self.store.transaction(mismatch)
            usage = response.get("reported_usage", response.get("usage")) if isinstance(response, dict) else None
            if isinstance(usage, dict) and isinstance(response, dict):
                cached = response.get("cached_tokens")
                if type(cached) is int and cached >= 0:
                    usage = {**usage, "cached_tokens": cached}
            response_bytes = None
            if isinstance(response, dict) and isinstance(response.get("message"), dict):
                try:
                    response_bytes = len(encoded(response["message"]))
                except (TypeError, ValueError, RecursionError):
                    pass  # Invalid responses still settle conservatively before validation.
            self._settle_usage(selected["id"], reservation["id"], usage=usage, elapsed=0 if nested else time.monotonic() - started,
                               cached_input_weight=self._cached_input_weight(selected),
                               retryable_failure=retryable_failure,
                               response_bytes=response_bytes)

    def _settle_usage(self, task_id, reservation_id, *, usage=None, elapsed=None, cached_input_weight=None, retryable_failure=False, response_bytes=None):
        def finish(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                weight = cached_input_weight if cached_input_weight is not None else self._cached_input_weight(row)
                budget.settle(row, reservation_id, usage=usage, elapsed_seconds=elapsed, cached_input_weight=weight, retryable_failure=retryable_failure, response_bytes=response_bytes)
        self.store.transaction(finish)

    def _settle_usage_batch(self, task_id, reservation_ids, *, usage=None, elapsed=None, cached_input_weight=None):
        if not reservation_ids:
            return
        def finish(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                weight = cached_input_weight if cached_input_weight is not None else self._cached_input_weight(row)
                for reservation_id in reservation_ids:
                    budget.settle(row, reservation_id, usage=usage, elapsed_seconds=elapsed, cached_input_weight=weight)
        self.store.transaction(finish)

    def _record_image_request_linkage(self, selected_row: dict, linkage: dict) -> None:
        clean = copy.deepcopy(linkage)
        task_id = selected_row["id"]
        def record(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                clean["binding_current"] = bool(
                    row.get("model_id") == selected_row.get("model_id")
                    and (row.get("goal") or {}).get("revision") == (selected_row.get("goal") or {}).get("revision")
                    and row.get("scope_revision") == selected_row.get("scope_revision")
                    and row.get("control_revision") == selected_row.get("control_revision")
                    and row.get("context_epoch", 0) == selected_row.get("context_epoch", 0)
                )
                links = list(row.get("image_request_links") or [])
                links.append(clean)
                row["image_request_links"] = links[-16:]
        self.store.transaction(record)

    def _tool_context(self, selected, operation_id=None):
        context = self._execution_context(selected, operation_id)
        context["selected_skills"] = copy.deepcopy(selected.get("skills") or [])
        current_model = self._model_metadata(selected["model_id"]) or {}
        context["context_window_chars"] = output_policy.context_budget(
            current_model, selected["limits"].get("max_context_chars"),
            provenance=selected.get("context_budget_provenance"))["effective_max_context_bytes"]
        context["_image_capability"] = image_capabilities.evaluate_image_capability(
            current_model, model_client=self.model)
        def inspect_image(messages, cancelled, *, source_artifact=None, linkage_out=None):
            model = self._model_metadata(selected["model_id"]) or {}
            cap = image_capabilities.require_route_image_support(model, model_client=self.model)
            self._validate_context(selected)
            nested_request_id = "req_" + uuid.uuid4().hex
            nested_image_context = {
                "capability": cap,
                "stage": "image_inspection",
                "operation_id": operation_id,
                "source_artifacts": [source_artifact] if isinstance(source_artifact, dict) else [],
                "linkage_out": linkage_out,
            }
            for attempt in range(2):
                try:
                    response = self._complete(selected, messages, [], cancelled, lambda _event: None,
                                              nested=True, stage="image_inspection",
                                              output_cap=min(output_policy.configured_output(model), 16384),
                                              request_id=nested_request_id,
                                              image_context=nested_image_context)
                    break
                except AgentError as exc:
                    if attempt == 0 and str(exc) in {
                        "agent_invalid_model_response",
                        "agent_model_busy",
                        "agent_model_connection_failed",
                        "agent_model_upstream_failed",
                    } and not cancelled():
                        nested_request_id = "req_" + uuid.uuid4().hex
                        continue
                    raise
            message = response.get("message") or {}
            valid_observation = (
                isinstance(message.get("content"), str)
                and bool(message["content"].strip())
                and not message.get("tool_calls")
            )
            linkage = image_capabilities.build_image_request_linkage(
                request_id=nested_request_id,
                model_id=selected["model_id"],
                capability=cap,
                messages=messages,
                stage="image_inspection",
                operation_id=operation_id,
                source_artifacts=[source_artifact] if isinstance(source_artifact, dict) else [],
                task_row=selected,
                dispatch_status="submitted",
                semantic_response_received=valid_observation,
            )
            self._record_image_request_linkage(selected, linkage)
            if isinstance(linkage_out, dict):
                linkage_out.update(linkage)
            if not valid_observation:
                raise AgentToolRejected("agent_image_observation_unavailable")
            return message["content"]
        context["_vision_inference"] = inspect_image
        def probe(invoke, *, max_output_tokens, input_bytes, model_id, source_id, purpose="source_draft_probe"):
            run_started = selected.get("_run_started")
            if run_started is None:
                raise AgentError("agent_probe_budget_unavailable")
            deadline = run_started + budget.run_seconds_left(selected, selected.get("_run_lifetime_remaining", 0))
            def guard():
                data = self.store.read()
                current = task(data, selected["id"])
                if (current["status"] != "running" or current["control_revision"] != selected["_run_control_revision"]
                        or not data["settings"]["enabled"]):
                    raise AgentToolRejected("agent_cancelled")
                self._validate_context(current, cheap_only=True)
                if time.monotonic() >= deadline:
                    raise AgentError("agent_time_budget_exhausted")
            def bounded(allowed):
                guard()
                return invoke(allowed, min(15.0, deadline - time.monotonic()), guard)
            guard()
            return self._meter_model(selected, bounded, input_bound=input_bytes + 512, output_bound=max_output_tokens,
                                     nested=True, stage=purpose)
        context["_model_probe"] = probe
        return context

    def _read_call(self, selected, stage, invoke):
        reservation = self.store.transaction(lambda data: budget.reserve(task(data, selected["id"]), "tool", stage))
        started = time.monotonic()
        try:
            return invoke()
        finally:
            self._settle_usage(selected["id"], reservation["id"], elapsed=time.monotonic() - started)

    def _relieve_storage(self, trigger, *, exclude=(), need_bytes=0, reserve_bytes=0, need_tasks=0, force=False) -> int:
        """Archive the least recently updated finished tasks when the store is nearly full.

        ``need_bytes`` will stay stored (a restored task); ``reserve_bytes`` is
        only held while one step runs, so it triggers relief but is not part of
        the target. Returns how many tasks moved to their archive. Each move is
        an ordinary archive (nothing is deleted); a task that changed meanwhile
        is skipped.
        """
        with self._storage_relief_lock:
            usage = self.store.usage()
            if not force and not storage_relief.needed(usage, need_bytes=need_bytes + reserve_bytes, need_tasks=need_tasks):
                return 0
            plan = self.store.inspect(lambda data: storage_relief.candidates(data, usage["tasks"], set(exclude)))
            total, count, chosen = usage["total"] + need_bytes, usage["count"] + need_tasks, []
            for item in plan:
                if storage_relief.satisfied(total, count):
                    break
                chosen.append(item)
                total, count = total - item["bytes"], count - len(item["tree_ids"])
            if not chosen:
                return 0
            # One commit for the whole batch: each commit re-copies the active
            # state, so archiving dozens of tasks one by one would stall a step.
            with contextlib.ExitStack() as gates:
                for task_id in sorted({key for item in chosen for key in item["tree_ids"]}):
                    gates.enter_context(credential_control.gate(self.store, task_id))
                archived = len(lifecycle.archive_many(self.store, chosen, event, reason="storage",
                                                      on_mutate=memory_integration.on_task_lifecycle_in_tx))
            if archived:
                after = self.store.usage()
                diagnostic_log.storage_relief(trigger, archived=archived, before_bytes=usage["total"],
                                              after_bytes=after["total"], tasks=after["count"])
            return archived

    def _schedule_idle_storage_relief(self):
        """Archive old tasks while nothing runs, off the scheduler's lock."""
        current = time.monotonic()
        if current < self._next_idle_relief:
            return
        self._next_idle_relief = current + 60

        def relieve():
            try:
                self._relieve_storage("idle")
            except Exception as exc:
                diagnostic_log.failure("storage_relief", exc)
            try:
                self.prune_retired_operations()
            except Exception as exc:
                diagnostic_log.failure("prune_retired_operations", exc)
        threading.Thread(target=relieve, name="ryn-agent-storage-relief", daemon=True).start()

    def _operation_ledger(self):
        rw = getattr(self.tools, "resource_writes", None)
        return getattr(rw, "ledger", None)

    def prune_retired_operations(self, *, older_than_days: float = 30) -> dict:
        ledger_pruned = 0
        sharing_pruned = 0
        ledger = self._operation_ledger()
        if ledger is not None and hasattr(ledger, "prune_retired"):
            ledger_pruned = int(ledger.prune_retired(older_than_days=older_than_days))
        sharing = getattr(self.tools, "friend_sharing", None)
        if sharing is not None and hasattr(sharing, "prune_retired"):
            sharing_pruned = int(sharing.prune_retired(older_than_days=older_than_days))
        return {"ledger_pruned": ledger_pruned, "sharing_pruned": sharing_pruned}

    def operation_ledger_status(self) -> dict:
        ledger = self._operation_ledger()
        if ledger is None or not hasattr(ledger, "health"):
            return {"state": "ok", "error": None, "reset_supported": False}
        return ledger.health()

    def reset_operation_ledger(self, value: dict | None = None) -> dict:
        if not isinstance(value, dict) or value.get("confirm") is not True or set(value) - {"confirm"}:
            raise AgentError("agent_invalid_request")
        ledger = self._operation_ledger()
        if ledger is None or not hasattr(ledger, "reset"):
            raise AgentError("agent_operation_ledger_unavailable")
        ledger_res = ledger.reset()

        def mark_inflight_unknown(data):
            affected: list[str] = []
            timestamp = now()
            for task_id, row in data.get("tasks", {}).items():
                if row.get("status") in TERMINAL:
                    continue
                flight = row.get("in_flight")
                if not isinstance(flight, dict) or flight.get("kind") != "tool":
                    continue
                if flight.get("effect") == "read" and not flight.get("dispatched"):
                    continue
                if flight.get("effect") == "read":
                    continue
                prior_rec = row.get("reconciliation") if isinstance(row.get("reconciliation"), dict) else {}
                row.update(
                    status="interrupted",
                    approved=None,
                    pending_approval=None,
                    error="agent_write_outcome_unknown",
                )
                verification.record(row, flight, "unknown", timestamp, flight=flight)
                row["reconciliation"] = {
                    "state": "unknown",
                    "attempts": prior_rec.get("attempts", 0),
                    "operation_id": flight.get("operation_id"),
                    "reason": "agent_operation_ledger_reset",
                    "requires_owner_review": True,
                    "at": timestamp,
                }
                event(
                    row,
                    "operation_ledger_reset",
                    operation_id=flight.get("operation_id"),
                    tool=flight.get("name"),
                    outcome="unknown",
                    requires_owner_review=True,
                )
                affected.append(task_id)
            return affected

        affected_tasks = self.store.transaction(mark_inflight_unknown)
        return {
            "ok": True,
            "quarantine_id": ledger_res.get("quarantine_id"),
            "affected_tasks": affected_tasks,
            "operation_ledger": self.operation_ledger_status(),
        }

    def _reserve_output(self, selected):
        # Free room before this step reserves its output, instead of failing
        # the step's commit after the model has already answered.
        is_model_step = not selected.get("queue") and not selected.get("verification")
        step_limit = output_policy.answer_limit(selected) if is_model_step else selected["limits"]["max_output_chars"]
        self._relieve_storage("step", exclude={selected["id"]},
                              reserve_bytes=output_policy.storage_growth(
                                  step_limit, row=selected, kind="model" if is_model_step else "tool"
                              )
                              + results.MAX_RESULT_BYTES)

        def reserve(data):
            row = task(data, selected["id"])
            if row.get("in_flight"):
                model_flight = (row["in_flight"].get("kind") == "model")
                limit = output_policy.answer_limit(row) if model_flight else row["limits"]["max_output_chars"]
                self.store.reserve(
                    data,
                    row["id"],
                    output_policy.storage_growth(limit, row=row, kind="model" if model_flight else "tool"),
                    extra_disk=results.MAX_RESULT_BYTES if row["queue"] else 0,
                )
        self.store.transaction(reserve)

    def scope_catalog(self) -> dict:
        result = sanitized(self.tools.scope_catalog()) if hasattr(self.tools, "scope_catalog") else {"connections": [], "supported_formats": []}
        result["create_scope_supported"] = hasattr(self.tools, "prepare_scope") and getattr(self.model, "supports_destination_binding", False) is True
        result["file_scope_supported"] = result["create_scope_supported"]
        return result

    def get_scope(self, task_id: str) -> dict:
        row = lifecycle.find(self.store, self.store.read(), task_id)
        try:
            current = self._current_destination(row["model_id"])
        except AgentError:
            current = None
        return {"scope": copy.deepcopy(row["scope"]), "scope_revision": row["scope_revision"], "control_revision": row["control_revision"],
                "model_destination": self._execution_context(row)["model_destination"], "current_model_destination": current}

    def update_scope(self, task_id: str, value: dict) -> dict:
        if set(value) - {"scope", "expected_control_revision", "expected_scope_revision", "expected_destination_revision", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        snapshot = self.store.read()
        selected = task(snapshot, task_id)
        if request_replayed(selected, value):
            return public_task(selected, settings=snapshot["settings"])
        check_version(selected, value)
        if type(value.get("expected_scope_revision")) is not int or value["expected_scope_revision"] != selected["scope_revision"]:
            raise AgentError("agent_scope_revision_conflict")
        selection = value.get("scope")
        destination = (self._current_destination(selected["model_id"]) if isinstance(selection, dict) and selection.get("resources")
                       else self._execution_context(selected)["model_destination"])
        scope = self._prepare_scope(selection, destination, value.get("expected_destination_revision"))

        def apply(data):
            row = task(data, task_id)
            if row.get("continued_to"):
                raise AgentError("agent_task_continued")
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            if row["scope_revision"] != value["expected_scope_revision"]:
                raise AgentError("agent_scope_revision_conflict")
            if ambiguous(row) or row["status"] == "cancelled":
                raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_invalid_transition")
            row["scope_revision"] += 1
            row["scope"] = {**copy.deepcopy(scope), "revision": row["scope_revision"], "selection": copy.deepcopy(value["scope"])}
            row["model_destination"] = copy.deepcopy(destination)
            # Do not erase encrypted audit evidence. A new provider projection
            # excludes every old tool body, derived assistant text and summary.
            row["context_epoch"] = row.get("context_epoch", 0) + 1
            row.pop("context_checkpoint", None)
            if row.get("memory_disclosed_in_epoch") or row.get("memory_projection_watermark"):
                row["memory_conversation_epoch"] = int(row.get("memory_conversation_epoch", 0)) + 1
                row["memory_disclosed_in_epoch"] = False
            row["plan"] = {"revision": row["plan"]["revision"] + 1, "goal_revision": row["goal"]["revision"], "steps": []}
            row["criteria_proposals"] = []
            row["outcome"].update(status="unverified", criteria_results=[], evidence_refs=[], uncertainties=["Resource permissions changed; prior results are not current verification."])
            for criterion in row["criteria"]:
                criterion.update(status="pending", evidence_refs=[])
            row.update(pending_approval=None, approved=None, pending_input=None)
            row["pending_control"] = {"kind": "scope", "remain_paused": row["status"] in {"paused", "completed", "failed", "interrupted"} or not data["settings"]["enabled"]}
            event(row, "scope_changed", scope_revision=row["scope_revision"])
            close_queue(row, event, "agent_scope_changed")
            memory_integration.settle_in_tx(data, row, event)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(apply)

    def settings(self) -> dict:
        snapshot = self.configuration_snapshot(display_only=True)
        return {
            **snapshot["settings"],
            "context_budget_policy": snapshot["context_budget_policy"],
            "operation_ledger": self.operation_ledger_status(),
        }

    def list_projects(self) -> list[dict]:
        return [copy.deepcopy(item) for item in self.store.read().get("projects", {}).values() if item.get("enabled")]

    def load_project(self, value: dict) -> dict:
        def register_and_sync(data):
            res = projects.register(data, value, store=self.store)
            memory_integration.propagate_memory_revocation_in_tx(data)
            return res
        return self.store.transaction(register_and_sync)

    def rename_project(self, project_id: str, value: dict) -> dict:
        return self.store.transaction(lambda data: projects.rename(data, project_id, value))

    def project_selection(self) -> str:
        data = self.store.read()
        ident = data.get("selected_project_id", "")
        return ident if data.get("projects", {}).get(ident, {}).get("enabled") else ""

    def select_project(self, value: dict) -> dict:
        if set(value) != {"project_id"} or not isinstance(value["project_id"], str):
            raise AgentError("agent_invalid_project_id")
        def select(data):
            ident = value["project_id"]
            if ident:
                projects.selected(data, ident)
            data["selected_project_id"] = ident
            return {"selected_project_id": ident}
        return self.store.transaction(select)

    def remove_project(self, project_id: str) -> dict:
        def remove(data):
            project = data.get("projects", {}).get(project_id)
            if not project:
                raise AgentError("agent_project_unavailable")
            project["enabled"] = False
            if data.get("selected_project_id") == project_id:
                data["selected_project_id"] = ""
            memory_integration.propagate_memory_revocation_in_tx(data)
            for row in data["tasks"].values():
                if (row.get("project") or {}).get("id") == project_id and row["status"] not in TERMINAL:
                    row.update(approved=None, pending_approval=None, pending_control={"kind": "pause"}, status="paused")
                    event(row, "permissions_changed", reason="agent_project_permission_revoked")
                    settle(row, event, data=data)
            return {"id": project_id, "removed": True}
        return self.store.transaction(remove)

    def bind_workspace(self, task_id: str, value: dict) -> dict:
        """Owner-only initial directory grant, separate from ordinary chat answers."""
        if set(value) - {"workspace_root", "expected_control_revision", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        from pathlib import Path

        from .local_tools import validate_configuration
        validate_configuration({"workspace_root": value.get("workspace_root")})
        if not value.get("workspace_root"):
            raise AgentError("agent_invalid_workspace")
        root = str(Path(value["workspace_root"]).resolve(strict=True))
        def apply(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            if not data["settings"]["enabled"]:
                raise AgentError("agent_disabled")
            if (row["status"] not in {"failed", "paused", "waiting_input", "completed"}
                    or row.get("in_flight") or ambiguous(row) or row.get("pending_approval")
                    or (row.get("local_execution") or {}).get("workspace_root")):
                raise AgentError("agent_invalid_transition")
            current = data["settings"].get("workspace_root", "")
            if current and Path(current).resolve() != Path(root):
                raise AgentError("agent_workspace_changed_new_task_required")
            if not current:
                data["settings"]["workspace_root"] = root
                data["settings_revision"] = data.get("settings_revision", 0) + 1
            row.setdefault("local_execution", {})["workspace_root"] = data["settings"]["workspace_root"]
            row["observed_files"] = {}
            close_queue(row, event, "agent_owner_changed")
            row.update(pending_input=None, approved=None, status="queued", error=None, result="", steps=0, elapsed_seconds=0.0)
            row.pop("verification", None)
            budget.new_round(row)
            budget.restart_segment_audit(row)
            row["messages"].append({"role": "user", "message_id": "owner_" + uuid.uuid4().hex,
                "content": "The owner selected this working directory in task settings: " + root
                    + ". Continue the original request. Existing write approvals still apply."})
            event(row, "workspace_bound", workspace_root=root)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(apply)

    COMPACT_PROMPT_MAX_BYTES = 4000
    _EXTRA_SETTINGS = {"compact_prompt": ""}

    def configuration_snapshot(self, *, display_only=False) -> dict:
        data = self.store.inspect(lambda state: {key: state[key] for key in
            ("settings", "settings_revision", "settings_context_budget_provenance") if key in state})
        settings = {**DEFAULT_SETTINGS, **self._EXTRA_SETTINGS, **data["settings"]}
        policy = self._settings_context_policy(settings, data.get("settings_context_budget_provenance"),
                                               display_only=display_only)
        return {"settings": settings, "settings_revision": data.get("settings_revision", 0), "context_budget_policy": policy}

    def _settings_context_policy(self, settings, provenance, *, display_only=False):
        display_model = getattr(self.catalog, "display_model", None)
        if display_only and callable(display_model):
            return output_policy.context_budget(display_model(settings["model_id"]) or {},
                settings.get("max_context_chars"), provenance=provenance)
        return self._context_policy({"model_id": settings["model_id"], "limits": settings,
                                     "context_budget_provenance": provenance})

    def update_settings(self, values: dict) -> dict:
        if isinstance(values, dict) and set(values) == {"reset_operation_ledger"}:
            if values["reset_operation_ledger"] is not True:
                raise AgentError("agent_invalid_settings")
            reset_res = self.reset_operation_ledger({"confirm": True})
            return {**self.settings(), "operation_ledger_reset": reset_res}
        if not isinstance(values, dict) or set(values) - (set(DEFAULT_SETTINGS) | set(self._EXTRA_SETTINGS)):
            raise AgentError("agent_invalid_settings")
        if "compact_prompt" in values:
            cp = values["compact_prompt"]
            if not isinstance(cp, str) or len(cp.encode("utf-8")) > self.COMPACT_PROMPT_MAX_BYTES or "\x00" in cp:
                raise AgentError("agent_invalid_settings")
        from .local_tools import validate_configuration
        validate_configuration(values)
        if "enabled" in values and type(values["enabled"]) is not bool:
            raise AgentError("agent_invalid_settings")
        for key in ("verify_text_responses", "interrupt_message_enabled"):
            if key in values and type(values[key]) is not bool:
                raise AgentError("agent_invalid_settings")
        if "model_id" in values and (not isinstance(values["model_id"], str) or len(values["model_id"]) > 1024):
            raise AgentError("agent_invalid_settings")
        if "permission_mode" in values and values["permission_mode"] not in PERMISSION_MODES:
            raise AgentError("agent_invalid_settings")
        if "allowed_write_tools" in values:
            allowed = values["allowed_write_tools"]
            names = {row["name"] for row in self.tool_catalog() if row["effect"] == "write"}
            if not isinstance(allowed, list) or any(not isinstance(name, str) or name not in names for name in allowed) or len(set(allowed)) != len(allowed):
                raise AgentError("agent_invalid_settings")
        if values.get("model_id"):
            selected_model = self._model_metadata(values["model_id"])
            if selected_model is None or not selected_model.get("available") or selected_model.get("agent_supported") is not True:
                raise AgentError("agent_model_unavailable")
        if "default_subagent_model_id" in values:
            model_id = values["default_subagent_model_id"]
            if not isinstance(model_id, str) or len(model_id) > 1024:
                raise AgentError("agent_invalid_settings")
            if model_id:
                model = self._model_metadata(model_id)
                if model is None or not model.get("available") or model.get("agent_supported") is not True:
                    raise AgentError("agent_model_unavailable")
        if values.get("default_subagent_reasoning_effort") is not None:
            try:
                reasoning.validate_effort(values["default_subagent_reasoning_effort"])
            except ValueError:
                raise AgentError("agent_invalid_reasoning_effort") from None
        if ("max_context_chars" in values and values["max_context_chars"] is not None
                and (type(values["max_context_chars"]) is not int or not 4000 <= values["max_context_chars"] <= 2000000)):
            raise AgentError("agent_invalid_settings")
        for key, low, high in (("max_steps", 1, 128), ("max_seconds", 10, 3600), ("max_output_chars", 1000, 40000)):
            if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
                raise AgentError("agent_invalid_settings")
        if model_profiles.SETTING in values:
            try:
                values = {**values, model_profiles.SETTING: model_profiles.validate_overrides(
                    values[model_profiles.SETTING], self._model_metadata)}
            except ValueError:
                raise AgentError("agent_invalid_settings") from None

        def update(data):
            workspace_changed = "workspace_root" in values and values["workspace_root"] != data["settings"].get("workspace_root")
            desktop_revoked = bool(data["settings"].get("desktop_access")) and values.get("desktop_access") is False
            permission_changed = desktop_revoked or any(key in values and values[key] != data["settings"].get(key) for key in ("allowed_write_tools", "workspace_root"))
            changed = any(data["settings"].get(key) != value for key, value in values.items())
            for key in budget.SEGMENT_LIMITS:
                if key in values:
                    # Stores never recorded who wrote these persisted defaults; only
                    # an explicit settings update marks a per-run limit as the owner's.
                    data.setdefault("settings_limit_sources", {})[key] = "owner"
            if "max_context_chars" in values:
                provenance = "owner_auto" if values["max_context_chars"] is None else "owner_manual"
                changed = changed or data.get("settings_context_budget_provenance") != provenance
                data["settings_context_budget_provenance"] = provenance
            if "model_id" in values and data.get("default_model_destination"):
                data.pop("default_model_destination")
                changed = True  # Explicit owner reselection supersedes the old binding.
            if changed:
                data["settings_revision"] = data.get("settings_revision", 0) + 1
            if "permission_mode" in values:
                # The owner's own pick is never replaced by a later shipped default.
                data[PERMISSION_MODE_SOURCE] = "owner"
            data["settings"].update(values)
            if permission_changed:
                for row in data["tasks"].values():
                    if row["status"] not in TERMINAL:
                        if desktop_revoked:
                            row.setdefault("local_execution", {})["desktop_access"] = False
                        previous_updated_at = row["updated_at"]
                        preserve_activity = row["status"] in {"paused", "waiting_approval", "waiting_input"}
                        row.update(approved=None, pending_approval=None)
                        if workspace_changed:
                            row["observed_files"] = {}
                        if row["status"] == "waiting_approval":
                            row["status"] = "paused"
                        if row.get("in_flight") and (row.get("pending_control") or {}).get("kind") not in {"pause", "stop", "cancel"}:
                            prior_control = row.get("pending_control") or {}
                            row["pending_control"] = {
                                **prior_control,
                                "kind": prior_control.get("kind", "permissions"),
                                "remain_paused": bool(prior_control.get("remain_paused")) or row["status"] != "running" or not data["settings"]["enabled"],
                            }
                        event(row, "permissions_changed")
                        if preserve_activity:
                            row["updated_at"] = previous_updated_at
                        if row.get("pending_input"):
                            row["pending_input"]["control_revision"] = row["control_revision"]
                        memory_integration.settle_in_tx(data, row, event)
            if values.get("enabled") is False:
                for row in data["tasks"].values():
                    if (row.get("pending_control") or {}).get("kind") == "edit_message":
                        row["pending_control"]["remain_paused"] = True
                        continue
                    if row["status"] in {"queued", "running", "waiting_approval", "waiting_input"} or ambiguous(row):
                        row["status"] = "interrupted" if ambiguous(row) else "paused"
                        row["pending_control"] = {"kind": "pause"}
                        event(row, "paused", reason="agent_disabled")
                        memory_integration.settle_in_tx(data, row, event)
            return {"settings": data["settings"], "provenance": data.get("settings_context_budget_provenance")}
        saved = self.store.transaction(update)
        # Display metadata must not hold the encrypted state lock or refresh
        # providers. Task admission still resolves its current model separately.
        settings = {**DEFAULT_SETTINGS, **self._EXTRA_SETTINGS, **saved["settings"]}
        policy = self._settings_context_policy(settings, saved["provenance"], display_only=True)
        return {
            **settings,
            "context_budget_policy": policy,
            "operation_ledger": self.operation_ledger_status(),
        }

    def models(self) -> list[dict]:
        return sanitized(getattr(self.catalog, "display_models", self.catalog.list_models)())

    def _model_metadata(self, model_id: str) -> dict | None:
        if hasattr(self.catalog, "get_model"):
            return sanitized(self.catalog.get_model(model_id))
        return next((row for row in self.models() if row.get("id") == model_id), None)

    def _resolve_subagent_spawn(self, parent: dict, args: dict) -> dict:
        """Resolve catalog capabilities outside the active-store transaction."""
        settings = self.settings()
        fork_all = args.get("fork_turns") == "all"
        model_id = (parent["model_id"] if fork_all else
                    args.get("model_id") or settings.get("default_subagent_model_id") or parent["model_id"])
        model = self._model_metadata(model_id)
        if model is None or not model.get("available") or model.get("agent_supported") is not True:
            choices = sorted(row["id"] for row in self.models() if row.get("available") and row.get("agent_supported") is True)[:5]
            raise AgentToolRejected("agent_subagent_model_unknown", correction="Choose an available agent model: " + ", ".join(choices))
        explicit = not fork_all and args.get("reasoning_effort") is not None
        requested = (parent.get("reasoning_effort") if fork_all else args.get("reasoning_effort") or
                     settings.get("default_subagent_reasoning_effort") or parent.get("reasoning_effort"))
        effort = None
        if requested is not None:
            try:
                effort = reasoning.validate(model, requested)
            except AgentError:
                if explicit:
                    supported = reasoning.metadata(model.get("supported_reasoning_efforts")).get("supported_reasoning_efforts", [])
                    raise AgentToolRejected("agent_reasoning_effort_unsupported",
                        correction="Supported reasoning efforts for this model: " + (", ".join(supported) or "none exposed")) from None
        capabilities = reasoning.metadata(model.get("supported_reasoning_efforts"), model.get("default_reasoning_effort"))
        return {**args, "resolved_model_id": model_id, "resolved_effort": effort,
                "effective_effort": effort or capabilities.get("default_reasoning_effort"),
                "resolved_destination": self._destination(model)}

    def preview_model_switch(self, task_id: str, model_id: str) -> dict:
        return model_switch.preview(self, task_id, model_id)

    def switch_model(self, task_id: str, value: dict) -> dict:
        res = model_switch.change(self, task_id, value)
        def sync_after_switch(data):
            row = data.get("tasks", {}).get(task_id)
            if row is not None and (row.get("memory_disclosed_in_epoch") or row.get("memory_projection_watermark")):
                row["memory_conversation_epoch"] = int(row.get("memory_conversation_epoch", 0)) + 1
                row["memory_disclosed_in_epoch"] = False
        self.store.transaction(sync_after_switch)
        return res

    SKILL_SYNC_THROTTLE_SECONDS = 10.0

    def _maybe_sync_workspace_skills(
        self,
        workspace_root: str | None,
        *,
        desktop_access: bool = False,
    ) -> None:
        if self.extensions is None or not workspace_root:
            return
        ws_path = Path(workspace_root).expanduser()
        try:
            ws_key = os.path.normcase(str(ws_path.resolve()))
        except OSError:
            ws_key = os.path.normcase(str(ws_path))
        now_mono = time.monotonic()
        with self._skill_sync_lock:
            last_at = self._skill_sync_last_at.get(ws_key)
            if last_at is not None and (now_mono - last_at) < self.SKILL_SYNC_THROTTLE_SECONDS:
                return
            self._skill_sync_last_at[ws_key] = now_mono
        self.extensions.store.sync_discovered_skills(
            workspace_roots=[ws_path],
            desktop_access=desktop_access,
        )

    def skills(self) -> dict:
        if self.extensions is not None:
            ws = self.store.inspect(lambda d: (d.get("settings") or {}).get("workspace_root"))
            if not ws:
                ws = self.store.default_workspace()
            return {"skills": self.extensions.store.enabled_skills_catalog(workspace_root=ws)}
        return {"skills": knowledge.catalog()}

    def _resolve_skills(
        self,
        explicit_ids: Any,
        message_text_val: str = "",
        *,
        workspace_root: str | None = None,
        default_project: bool = False,
        warnings_out: list[dict] | None = None,
    ) -> list[dict]:
        enabled_catalog = (
            self.extensions.store.enabled_skills_catalog(workspace_root=workspace_root)
            if self.extensions is not None
            else knowledge.catalog()
        )
        enabled_by_lower: dict[str, str] = {}
        for item in enabled_catalog:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                cid = str(item["id"])
                enabled_by_lower[cid.lower()] = cid
                raw_name = item.get("name")
                if isinstance(raw_name, str) and raw_name:
                    enabled_by_lower.setdefault(raw_name.lower(), cid)
        explicit_warnings: list[dict] = []
        if self.extensions is not None:
            selected = list(
                self.extensions.store.select_skills(
                    explicit_ids,
                    workspace_root=workspace_root,
                    warnings=explicit_warnings,
                )
            )
        else:
            selected = knowledge.select(explicit_ids)
        if isinstance(explicit_ids, list) and len(explicit_ids) > 0 and not selected and explicit_warnings:
            raise AgentError(str(explicit_warnings[0].get("reason") or "agent_skill_integrity_failed"))
        seen_ids = {str(item.get("id")) for item in selected if isinstance(item, dict)}
        skipped_ids = {str(w.get("skill_id")) for w in explicit_warnings if w.get("skill_id")}
        all_warnings: list[dict] = list(explicit_warnings)

        if isinstance(message_text_val, str) and "$" in message_text_val:
            mentioned_ids: list[str] = []
            missing_ids = self.extensions.store.missing_skill_ids() if self.extensions is not None else set()
            for match in re.finditer(
                r"(?<![A-Za-z0-9_-])\$([A-Za-z0-9][A-Za-z0-9_.-]{0,63})(?![A-Za-z0-9_-])",
                message_text_val,
            ):
                token = match.group(1).rstrip(".")
                canonical_id = enabled_by_lower.get(match.group(1).lower()) or enabled_by_lower.get(token.lower())
                if canonical_id is None and token.lower() in missing_ids:
                    raise AgentError("agent_invalid_skills")
                if (
                    canonical_id
                    and canonical_id not in seen_ids
                    and canonical_id not in skipped_ids
                    and canonical_id not in mentioned_ids
                ):
                    mentioned_ids.append(canonical_id)
            if mentioned_ids:
                mention_warnings: list[dict] = []
                if self.extensions is not None:
                    mentioned_selected = list(
                        self.extensions.store.select_skills(
                            mentioned_ids,
                            workspace_root=workspace_root,
                            warnings=mention_warnings,
                        )
                    )
                else:
                    mentioned_selected = knowledge.select(mentioned_ids)
                for item in mentioned_selected:
                    if item["id"] not in seen_ids:
                        selected.append(item)
                        seen_ids.add(item["id"])
                all_warnings.extend(mention_warnings)

        if warnings_out is not None:
            warnings_out.extend(all_warnings)
        return selected

    @staticmethod
    def _merge_task_skills(
        existing: list[dict] | None, additions: list[dict] | None
    ) -> tuple[list[dict], list[str]]:
        merged = [copy.deepcopy(item) for item in (existing or []) if isinstance(item, dict)]
        seen = {str(item.get("id")) for item in merged if item.get("id")}
        dropped: list[str] = []
        for item in additions or []:
            if isinstance(item, dict) and item.get("id"):
                sid = str(item["id"])
                if sid not in seen and sid not in dropped:
                    if len(merged) >= 8:
                        dropped.append(sid)
                    else:
                        merged.append(copy.deepcopy(item))
                        seen.add(sid)
        return merged, dropped

    @staticmethod
    def _record_skill_events_in_tx(
        row: dict,
        *,
        skipped_warnings: list[dict] | None = None,
        dropped_skill_ids: list[str] | None = None,
    ) -> None:
        for warn in skipped_warnings or []:
            if isinstance(warn, dict) and warn.get("skill_id"):
                event(
                    row,
                    "skill_skipped",
                    skill_id=str(warn["skill_id"]),
                    reason=str(warn.get("reason") or "agent_skill_integrity_failed"),
                )
        if dropped_skill_ids:
            prior = [
                str(sid) for sid in (row.get("dropped_skill_ids") or [])
                if isinstance(sid, str) and sid
            ]
            for sid in dropped_skill_ids:
                if sid not in prior:
                    prior.append(sid)
            row["dropped_skill_ids"] = prior
            event(row, "skills_limit_reached", dropped_skill_ids=list(dropped_skill_ids), limit=8)

    def preferences(self) -> dict:
        return knowledge.preferences(self.store.read())

    def update_preferences(self, value: dict) -> dict:
        return self.store.transaction(lambda data: knowledge.update_preferences(data, value))

    def memory_snapshot(
        self,
        scope_kind: str | None = None,
        project_id: str | None = None,
        status: str = "active",
    ) -> dict:
        return memory_integration.get_memory_snapshot(
            self,
            scope_kind=scope_kind,
            project_id=project_id,
            status=status,
        )

    def memory_destinations(self) -> dict:
        return memory_integration.list_memory_destinations(self)

    def update_memory_settings(self, value: dict) -> dict:
        return memory_integration.update_owner_memory_settings(self, value)

    def create_memory_entry(self, value: dict) -> dict:
        return memory_integration.create_owner_memory_entry(self, value)

    def update_memory_entry(self, entry_id: str, value: dict) -> dict:
        return memory_integration.update_owner_memory_entry(self, entry_id, value)

    def delete_memory_entry(self, entry_id: str, value: dict) -> dict:
        return memory_integration.delete_owner_memory_entry(self, entry_id, value)

    def decide_memory_candidate(self, candidate_id: str, value: dict) -> dict:
        return memory_integration.decide_owner_memory_candidate(self, candidate_id, value)

    def search_memory(
        self,
        query: str = "",
        scope_kind: str | None = None,
        project_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
    ) -> dict:
        return memory_integration.search_owner_memories(
            self,
            query=query,
            scope_kind=scope_kind,
            project_id=project_id,
            category=category,
            limit=limit,
        )

    def reset_memory(self, value: dict) -> dict:
        return memory_integration.reset_owner_memory(self, value)

    def list_automations(self) -> dict:
        return self.store.transaction(lambda data: automations.list_automations_in_tx(self.store, data))

    def create_automation(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise AgentError("agent_invalid_request")
        item = self.store.transaction(lambda data: automations.create_or_update_automation_in_tx(self.store, data, value))
        return {"automation": item}

    def update_automation(self, automation_id: str, value: dict) -> dict:
        if not isinstance(value, dict) or not str(automation_id or "").strip():
            raise AgentError("agent_invalid_request")
        def _upd(data):
            automations.ensure_automation_state(data)
            if automation_id not in data["automations"]:
                raise AgentError("agent_automation_not_found")
            return automations.create_or_update_automation_in_tx(self.store, data, value, automation_id=automation_id)
        item = self.store.transaction(_upd)
        return {"automation": item}

    def delete_automation(self, automation_id: str) -> dict:
        if not str(automation_id or "").strip():
            raise AgentError("agent_invalid_request")
        deleted = self.store.transaction(lambda data: automations.delete_automation_in_tx(self.store, data, automation_id))
        if not deleted:
            raise AgentError("agent_automation_not_found")
        return {"deleted": True, "id": automation_id}

    def run_automation_now(self, automation_id: str) -> dict:
        if not str(automation_id or "").strip():
            raise AgentError("agent_invalid_request")
        outcome = self.store.transaction(
            lambda data: automation_worker.trigger_automation_in_tx(self, data, automation_id, force=True)
        )
        if outcome.get("task_id"):
            self._queued_since[str(outcome["task_id"])] = time.monotonic()
        return outcome

    def source_credential(self, task_id: str, value: dict, revoke=False) -> dict:
        return credential_control.change(self, task_id, value, revoke=revoke)

    def update_budget(self, task_id: str, value: dict) -> dict:
        if set(value) - {"expected_control_revision", "client_request_id", "limits", "run_max_seconds", "release_legacy"}:
            raise AgentError("agent_invalid_request")
        release_legacy = value.get("release_legacy", False)
        if not isinstance(release_legacy, bool):
            raise AgentError("agent_invalid_budget")
        requested = value.get("limits")
        if requested is None and release_legacy:
            requested = {}
        budget.validate_limits(requested)
        run_max = value.get("run_max_seconds")
        if "run_max_seconds" in value and (type(run_max) is not int or not 10 <= run_max <= 3600):
            raise AgentError("agent_invalid_budget")
        def update(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            if not allowed_actions(row, data["settings"])["budget"]["enabled"]:
                raise AgentError("agent_invalid_transition")
            state = budget.initialize(row)
            released = budget.release_legacy_limits(row) if release_legacy else []
            if requested:
                budget.set_owner_limits(row, requested)
            if run_max is not None:
                row["limits"]["max_seconds"] = run_max
                row.setdefault("limits_source", {})["max_seconds"] = "owner"
            event(row, "budget_changed", limits=state["limits"], run_max_seconds=row["limits"]["max_seconds"],
                  owner_limits=sorted(requested),
                  **({"released_legacy_limits": released} if release_legacy else {}),
                  limit_sources=budget.public(row)["limit_sources"])
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(update)

    def tool_catalog(self) -> list[dict]:
        return [sanitized({"name": spec["function"]["name"], **self.tools.metadata(spec["function"]["name"])}) for spec in self.tools.specs()]

    def list_tasks(self, archived: bool = False) -> list[dict]:
        if archived:
            data = self.store.read()
            rows = [
                lifecycle.find(self.store, data, task_id)
                for task_id in data.get("archives", {})
            ]
            stamp = time.monotonic()
            return [
                public_task(row, summary=True, settings=data["settings"], active_run=self._task_run(row["id"]), monotonic_now=stamp, data=data)
                for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)
                if not row.get("parent_task_id")
            ]

        def view(data):
            # Summaries never include transcripts, so nothing else is copied.
            stamp = time.monotonic()
            return [
                public_task(detached(row, summary=True, data=data), summary=True, settings=data["settings"],
                            active_run=self._task_run(row["id"]), monotonic_now=stamp, data=data)
                for row in sorted(data["tasks"].values(), key=lambda row: row["created_at"], reverse=True)
                if not row.get("parent_task_id")
            ]
        return self.store.inspect(view)

    def get(self, task_id: str, since_revision: int | None = None) -> dict:
        def view(data):
            row = data["tasks"].get(task_id)
            if row is None:
                return None
            current = public_task(detached(row, summary=since_revision == row["revision"], data=data), settings=data["settings"],
                                  active_run=self._task_run(task_id), monotonic_now=time.monotonic(),
                                  summary=since_revision == row["revision"], data=data)
            current["streaming"] = self._stream_snapshot(row)
            if since_revision != row["revision"]:
                return current
            # Unchanged since the caller's copy: only fields that move on their own.
            return {"id": task_id, "revision": row["revision"], "unchanged": True,
                    **{key: current[key] for key in ("run_timing", "budget", "allowed_actions", "run_state", "streaming", "subagents", "waiting_for_subagents") if key in current}}
        found = self.store.inspect(view)
        if found is not None:
            return found
        data = self.store.read()
        return public_task(lifecycle.find(self.store, data, task_id), settings=data["settings"],
                           active_run=self._task_run(task_id), monotonic_now=time.monotonic(), data=data)

    def lifecycle(self, task_id: str, value: dict, operation: str) -> dict:
        data = self.store.read()
        deleted = data.get("deleted_tasks", {}).get(task_id)
        if deleted:
            ids = [task_id, *deleted.get("descendant_ids", [])]
        else:
            row = lifecycle.find(self.store, data, task_id)
            ids = [child["id"] for child in lifecycle.tree_rows(self.store, data, row)]
        with contextlib.ExitStack() as gates:
            for key in sorted(set(ids)):
                gates.enter_context(credential_control.gate(self.store, key))
            return self._lifecycle(task_id, value, operation)

    def rename_task(self, task_id: str, value: dict) -> dict:
        if set(value) - {"title", "expected_control_revision", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        title = value.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 160 or any(ord(char) < 32 for char in title):
            raise AgentError("agent_invalid_title")
        keyed = {**value, "metadata_action": "rename"}

        def rename(data):
            row = lifecycle.find(self.store, data, task_id)
            if request_replayed(row, keyed):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            row["title"] = title.strip()
            # A display label changes the poll revision, never execution authority.
            # Advancing control_revision here would discard an active model result
            # or invalidate the owner's pending tool approval.
            event(row, "title_changed")
            remember_request(row, keyed)
            if row.get("archived"):
                lifecycle.archive_store(self.store, task_id).transaction(lambda archive: archive["tasks"].update({task_id: row}))
                data["archives"][task_id]["updated_at"] = row["updated_at"]
            return public_task(row, settings=data["settings"])
        return self.store.transaction(rename)

    def _lifecycle(self, task_id: str, value: dict, operation: str) -> dict:
        if operation == "restore":
            try:
                archived = lifecycle.find(self.store, self.store.read(), task_id)
            except AgentError:
                archived = None  # change() reports the precise error.
            if archived is not None and archived.get("archived"):
                rows = lifecycle.restore_rows(self.store, self.store.read(), archived)
                self._relieve_storage("restore", exclude={child["id"] for child in rows},
                                      need_bytes=sum(len(encoded(child)) for child in rows), need_tasks=len(rows))
        result = lifecycle.change(self.store, task_id, value, operation, event, now,
                                  on_mutate=memory_integration.on_task_lifecycle_in_tx)
        if result.get("deleted"):
            ids = [*result.get("descendant_ids", []), task_id]
            result["cleanup_pending"] = self._cleanup_deleted_tasks(ids)
        return result if result.get("deleted") else public_task(result, settings=self.settings())

    def _cleanup_deleted_tasks(self, ids):
        any_pending = False
        for task_id in ids:
            pending = True
            try:
                results.purge(self.store, task_id)
                lifecycle.purge_archive(self.store, task_id)
                self.store.transaction(lambda _data: self.store.purge_orphan_snapshots())
                if hasattr(self.tools, "forget_task"):
                    self.tools.forget_task(task_id, operation_ids=self.store.read()["deleted_tasks"][task_id].get("operation_ids", []))
                pending = False
            except Exception:
                pass  # Tombstone wins; the same DELETE safely retries cleanup.
            self.store.transaction(lambda data: data["deleted_tasks"][task_id].update(cleanup_pending=pending))
            any_pending |= pending
        return any_pending

    def get_result(self, task_id: str, result_id: str, offset=0, limit=8192) -> dict:
        row = lifecycle.find(self.store, self.store.read(), task_id)
        return results.page(self.store, row, result_id, offset, limit)

    def _file_artifact_row(self, task_id: str) -> dict:
        from . import workspace_artifacts
        collector = getattr(self.tools, "collect_command_artifacts", None)
        pending = bool(collector(task_id)) if collector else False
        row = lifecycle.find(self.store, self.store.read(), task_id)
        for receipt in row.get("receipts", []):
            tool_name = str(receipt.get("tool") or "")
            if tool_name not in {"ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_file_read",
                                 "ryn_render_inspect", "ryn_download_file", "ryn_image_generate", "ryn_shell_execute", "ryn_process_read", "ryn_process_stop"} and not tool_name.startswith("mcp__"):
                continue
            result = receipt.get("result")
            if isinstance(result, dict) and result.get("result_ref"):
                try:
                    receipt["result"], _ = results.read(self.store, row, result["result_ref"])
                except AgentError:
                    pass
        referenced = {state["id"] for state in workspace_artifacts.history(row).values() if state["source"] == "read"}
        if referenced - set(row.get("referenced_file_ids", [])):
            def remember(data):
                current = lifecycle.find(self.store, data, task_id)
                current["referenced_file_ids"] = sorted(set(current.get("referenced_file_ids", [])) | referenced)
            self.store.transaction(remember)
            row["referenced_file_ids"] = sorted(set(row.get("referenced_file_ids", [])) | referenced)
        row["pending_file_commands"] = pending
        return row

    def artifacts(self, task_id: str) -> dict:
        from . import artifacts, workspace_artifacts
        row = self._file_artifact_row(task_id)
        return {**artifacts.listing(self, row), **workspace_artifacts.listing(row), "pending_files": row["pending_file_commands"]}

    def read_file_artifact(self, task_id: str, file_id: str) -> dict:
        from . import workspace_artifacts
        row = self._file_artifact_row(task_id)
        return workspace_artifacts.read(row, file_id)

    def open_file_artifact(self, task_id: str, file_id: str, action: str) -> dict:
        from . import workspace_artifacts
        return workspace_artifacts.open_file(self._file_artifact_row(task_id), file_id, action)

    def read_artifact(self, task_id: str, operation_id: str) -> dict:
        from . import artifacts
        row = lifecycle.find(self.store, self.store.read(), task_id)
        return artifacts.read(self, row, operation_id)

    def events(self, task_id: str, after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise AgentError("agent_invalid_cursor")
        row = lifecycle.find(self.store, self.store.read(), task_id)
        return {"events": hydrated_events(row, [item for item in row["events"] if item["seq"] > after]), "cursor": row["event_cursor"], "status": row["status"], "revision": row["revision"], "control_revision": row["control_revision"]}

    def create(self, value: dict, *, _continuation=None) -> dict:
        if set(value) - {"message", "model_id", "title", "client_request_id", "scope", "expected_destination_revision", "skill_ids", "budget_limits", "response_language", "project_id", "reasoning_effort", "attachments"}:
            raise AgentError("agent_invalid_request")
        if value.get("response_language") not in {None, "zh-CN", "en"}:
            raise AgentError("agent_invalid_request")
        prepared = parse_attachments(value.get("attachments"))
        content = message_text(value.get("message") or ("Please review the attached files." if prepared else ""))
        budget.validate_limits(value.get("budget_limits", {}))
        request_id = value.get("client_request_id")
        if request_id is not None:
            try:
                if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                    raise ValueError
            except ValueError:
                raise AgentError("agent_invalid_request") from None
        fingerprint = digest({"request": value, "continuation_from": _continuation["id"]}) if _continuation else digest(value)
        configuration = self.store.read()
        if request_id:
            existing = lifecycle.duplicate(self.store, configuration, request_id, fingerprint)
            if existing is not None:
                return public_task(existing, settings=configuration["settings"])
        cfg_settings = {**DEFAULT_SETTINGS, **configuration["settings"]}
        task_workspace_root = cfg_settings.get("workspace_root") or self.store.default_workspace()
        if value.get("project_id") is not None:
            proj_id = value["project_id"]
            proj_row = (configuration.get("projects") or {}).get(proj_id) if isinstance(proj_id, str) else None
            if isinstance(proj_row, dict) and proj_row.get("enabled") and proj_row.get("path"):
                task_workspace_root = str(proj_row["path"])
        elif _continuation:
            cont_src = configuration.get("tasks", {}).get(_continuation["id"])
            if isinstance(cont_src, dict):
                cont_ws = (cont_src.get("local_execution") or {}).get("workspace_root")
                if cont_ws:
                    task_workspace_root = str(cont_ws)
        if cfg_settings.get("enabled"):
            self._maybe_sync_workspace_skills(
                task_workspace_root,
                desktop_access=bool(cfg_settings.get("desktop_access", False)),
            )
        skipped_skills: list[dict] = []
        skills = self._resolve_skills(
            value.get("skill_ids"),
            content,
            workspace_root=task_workspace_root,
            warnings_out=skipped_skills,
        )
        uses_default = not value.get("model_id")
        model_id = value.get("model_id") or configuration["settings"]["model_id"]
        if not isinstance(model_id, str) or not model_id:
            raise AgentError("agent_model_required")
        selected_model = self._model_metadata(model_id)
        if selected_model is None or not selected_model.get("available") or selected_model.get("agent_supported") is not True:
            raise AgentError("agent_model_unavailable")
        effort = reasoning.validate(selected_model, value["reasoning_effort"]) if "reasoning_effort" in value else None
        peer_destination = selected_model.get("source") == "peer"
        project_destination = value.get("project_id") is not None
        destination = self._bound_destination(selected_model) if peer_destination or project_destination else self._destination(selected_model)
        # The UI sends its selected id explicitly even when it uses the saved
        # default. Preserve that approval's destination for both entry paths.
        default_binding = configuration.get("default_model_destination") if model_id == configuration["settings"]["model_id"] else None
        if default_binding and self._bound_destination(selected_model) != default_binding:
            raise AgentError("agent_model_destination_changed")
        scope = self._prepare_scope(value["scope"], destination, value.get("expected_destination_revision")) if "scope" in value else None
        title = value.get("title", content[:80].replace("\n", " "))
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise AgentError("agent_invalid_title")
        self._relieve_storage("create", need_tasks=1, exclude={_continuation["id"]} if _continuation else set())

        def create(data):
            if request_id:
                existing = lifecycle.duplicate(self.store, data, request_id, fingerprint)
                if existing is not None:
                    return public_task(existing, settings=data["settings"])
            settings = {**DEFAULT_SETTINGS, **data["settings"]}
            if not settings["enabled"]:
                raise AgentError("agent_disabled")
            if (uses_default or default_binding) and data.get("settings_revision", 0) != configuration.get("settings_revision", 0):
                raise AgentError("agent_settings_revision_conflict")
            if len(data["tasks"]) >= 200 or sum(row["status"] not in TERMINAL and not row.get("parent_task_id") for row in data["tasks"].values()) >= 16:
                raise AgentError("agent_task_limit")
            source = None
            if _continuation:
                source = lifecycle.find(self.store, data, _continuation["id"])
                if source.get("continued_to"):
                    child = lifecycle.find(self.store, data, source["continued_to"])
                    if child.get("continuation_request_fingerprint") != _continuation["fingerprint"]:
                        raise AgentError("agent_continuation_already_exists")
                    return public_task(child, settings=settings, data=data)
                pending = source.get("pending_control")
                unsettled = pending and not (source["status"] == "cancelled" and pending.get("kind") == "cancel")
                if (source["revision"] != _continuation["revision"] or not lifecycle.eligible(source)
                        or source.get("pending_approval") or source.get("pending_input") or unsettled):
                    raise AgentError("agent_operation_unresolved" if ambiguous(source) else "agent_task_busy")
                if source.get("model_destination") != destination:
                    raise AgentError("agent_model_destination_changed")
            row = {"id": "agent_" + uuid.uuid4().hex, "client_request_id": request_id,
                   "request_fingerprint": fingerprint, "title": title.strip(), "model_id": model_id,
                   "status": "queued", "revision": 0, "created_at": now(), "updated_at": now(),
                   "messages": [{"role": "user", "content": content}], "events": [], "pending_approval": None,
                   "steps": 0, "result": "", "error": None,
                   "limits": {key: settings[key] for key in DEFAULT_SETTINGS if key.startswith("max_")},
                   "permission_mode": settings["permission_mode"], "allowed_write_tools": list(settings["allowed_write_tools"]), "queue": [], "in_flight": None,
                   "approved": None, "elapsed_seconds": 0.0}
            # New tasks pin the Ryn base for restart/model-switch continuity.
            # Legacy sessions stay readable and use the documented fallback.
            row["base_instructions"] = select_base_instructions()
            row["context_budget_provenance"] = data.get("settings_context_budget_provenance", "default_auto" if settings["max_context_chars"] is None else "legacy_ambiguous_manual")
            initialize(row)
            if effort is not None:
                row["reasoning_effort"] = effort
            row["local_execution"] = {"workspace_root": settings.get("workspace_root") or self.store.default_workspace(), "desktop_access": settings.get("desktop_access", False)}
            if value.get("project_id") is not None:
                row["project"] = copy.deepcopy(projects.selected(data, value["project_id"], refresh=True))
                row["local_execution"].update(workspace_root=row["project"]["path"],
                                              project_id=row["project"]["id"], project_generation=row["project"]["generation"])
                truncation = projects.instructions_truncation(row)
                if truncation is not None:
                    event(row, "project_instructions_truncated", **truncation)
            if source is None:
                merged_initial, dropped_skills = self._merge_task_skills([], skills)
                row["skills"] = merged_initial
            else:
                dropped_skills = []
                row["skills"] = skills
            row["verify_text_responses"] = settings.get("verify_text_responses", False)
            row["response_language"] = value.get("response_language") or "en"
            budget.initialize(row)
            budget.set_owner_limits(row, value.get("budget_limits", {}))
            row["limits_source"] = {key: (data.get("settings_limit_sources") or {}).get(key, "default")
                                    for key in budget.SEGMENT_LIMITS}
            row["model_destination"] = copy.deepcopy(destination)
            if peer_destination or project_destination:
                # A dispatch constraint, not resource/egress authorization or a
                # claim that the owner approved a business mutation.
                row["model_destination_required"] = True
            if default_binding:
                row["model_destination_confirmed"] = True
            if scope is not None:
                row["scope"] = {**copy.deepcopy(scope), "revision": row["scope_revision"], "selection": copy.deepcopy(value["scope"])}
            if prepared:
                attach_to_message(self.store, row, row["messages"][0], prepared)
                row["goal"]["text"] = row["messages"][0]["content"]
                row["goal"]["constraints"][0]["text"] = row["messages"][0]["content"]
                (future_specs, _), authority_bytes = self._menu(row)
                if authority_bytes + len(encoded(future_specs)) > self._context_limit(row):
                    raise AgentError("agent_context_limit")
            row["prompt_version"] = PROMPT_VERSION
            if source is not None:
                # Continuation is the same work, unlike an owner-created fork.
                # Never import approvals, dispatch queues, processes or provider state.
                for key in ("limits", "limits_source", "budget", "permission_mode", "allowed_write_tools",
                            "local_execution", "project", "model_destination", "model_destination_required",
                            "model_destination_confirmed", "scope", "scope_revision", "context_epoch",
                            "data_policy_state", "base_instructions", "prompt_version", "skills",
                            "dropped_skill_ids", "reasoning_effort", "response_language", "context_budget_provenance",
                            "verify_text_responses"):
                    if key in source:
                        row[key] = copy.deepcopy(source[key])
                row["local_execution"]["desktop_access"] = bool(row["local_execution"].get("desktop_access") and settings.get("desktop_access"))
                merged_cont, dropped_skills = self._merge_task_skills(row.get("skills"), skills)
                row["skills"] = merged_cont
                if effort is not None:
                    row["reasoning_effort"] = effort
                if not row["scope"].get("resource_grants"):
                    row["scope"]["selection"] = {"resources": [], "allow_model_data": False}
                row["continuation_resource_origins"] = {
                    "context_epoch": row.get("context_epoch", 0),
                    "origins": self._execution_context(source, cheap_only=True)["resource_origins"]}
                # Retained summaries still carry source data. Validate the same
                # grants/origins under the child ID; task-bound references fail
                # closed, as in explicit forks, instead of losing provenance.
                self._validate_context(row, control_state=data)
                budget.new_round(row)
                exhausted = budget.exhausted_hard_limit(row)
                if exhausted:
                    raise AgentError(exhausted)
                row["continued_from"] = source["id"]
                row["continuation_request_fingerprint"] = _continuation["fingerprint"]
                row["continuation_root"] = source.get("continuation_root", source["id"])
                source["continued_to"] = row["id"]
                event(source, "continued_in_new_task", task_id=row["id"])
                event(row, "continued_from", task_id=source["id"])
            self._record_skill_events_in_tx(
                row,
                skipped_warnings=skipped_skills,
                dropped_skill_ids=dropped_skills,
            )
            memory_natural.stamp_task_owner_messages_in_tx(data, row)
            memory_integration.apply_owner_turn_memory_in_tx(self, data, row)
            event(row, "created")
            data["tasks"][row["id"]] = row
            return public_task(row, settings=settings)
        created = self.store.transaction(create)
        if created.get("status") == "queued":
            self._queued_since[created["id"]] = time.monotonic()
        return created

    def add_message(self, task_id: str, value: dict) -> dict:
        if set(value) - {"message", "intent", "expected_revision", "expected_control_revision", "expected_goal_revision", "client_request_id", "reasoning_effort", "edit_message_index", "attachments", "skill_ids"}:
            raise AgentError("agent_invalid_request")
        prepared = parse_attachments(value.get("attachments"))
        content = message_text(value.get("message") or ("Please review the attached files." if prepared else ""))

        def _task_ws(data: dict) -> str | None:
            row = data.get("tasks", {}).get(task_id)
            if isinstance(row, dict):
                ws = (row.get("local_execution") or {}).get("workspace_root")
                if ws:
                    return str(ws)
            return (data.get("settings") or {}).get("workspace_root") or self.store.default_workspace()

        task_ws = self.store.inspect(_task_ws)
        skipped_skills: list[dict] = []
        added_skills = self._resolve_skills(
            value.get("skill_ids"),
            content,
            workspace_root=task_ws,
            warnings_out=skipped_skills,
        )
        if "edit_message_index" in value:
            return self._edit_message(
                task_id,
                value,
                content,
                prepared,
                added_skills=added_skills,
                skipped_skills=skipped_skills,
            )
        intent = value.get("intent")
        if intent not in {None, "steer", "information"}:
            raise AgentError("agent_invalid_request")

        def add(data):
            row = task(data, task_id)
            if row.get("continued_to"):
                raise ContinueInNewTask()
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value, goal=intent is not None)
            if not data["settings"]["enabled"]:
                raise AgentError("agent_disabled")
            if acknowledgeable(row):
                if intent is not None and not allowed_actions(row, data["settings"])[intent]["enabled"]:
                    raise AgentError("agent_operation_unresolved")
                self._acknowledge_unknown_in_tx(row)
            resumable_interrupted = row["status"] == "interrupted" and (not row["in_flight"] or recoverable_read_in_flight(row)) and not ambiguous(row)
            if intent is None and ((row["status"] not in {"completed", "failed", "paused", "cancelled"} and not resumable_interrupted) or (row["in_flight"] and not resumable_interrupted) or row["queue"] or row["pending_approval"] or row["pending_input"]):
                raise AgentError("agent_task_busy")
            if intent is not None and not allowed_actions(row, data["settings"])[intent]["enabled"]:
                raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_invalid_transition")
            # A long conversation is condensed, never refused. Only if it is
            # still too large does it continue in a new linked conversation.
            size = len(encoded(row))
            if event_compaction.needs_compaction(row, size):
                # Full goal copies from older builds go first: they repeat every earlier message.
                shrunk = compact_goal_history(row)
                if event_compaction.compact_until_small(row) or shrunk:
                    size = len(encoded(row))
            if event_compaction.over_hard_limit(row, size, reserve_events=2 + len(skipped_skills) + 1):
                raise ContinueInNewTask()
            effort = reasoning.validate(self._model_metadata(row["model_id"]), value["reasoning_effort"]) if "reasoning_effort" in value else None
            if row["status"] == "interrupted" and row["in_flight"]:
                if not recoverable_read_in_flight(row) or ambiguous(row):
                    raise AgentError("agent_operation_unresolved")
                flight = row["in_flight"]
                err_code = row.get("error") or "agent_read_interrupted"
                cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=err_code)
                if not cleanup_ok:
                    raise AgentError("agent_operation_unresolved")
                if flight.get("effect") == "write":
                    self._settle_interrupted_read(row, flight, err_code, recovery=recovery)
                row["in_flight"] = None
                row["approved"] = None
            # Recover old, unbound conversations when the owner continues them.
            # Never relocate an existing workspace or change an in-flight grant.
            if (not (row.get("local_execution") or {}).get("workspace_root")
                    and not data["settings"].get("workspace_root")
                    and not row.get("in_flight") and not ambiguous(row)):
                row.setdefault("local_execution", {})["workspace_root"] = self.store.default_workspace()
            # Original evidence survives compaction, so its archive size is not
            # the next prompt size. Only immutable owner constraints form a hard
            # context floor; the normal loop may compact the remaining history.
            message = {"role": "user", "content": content, "message_id": "owner_" + uuid.uuid4().hex,
                       "ui": {"kind": "steer" if intent == "steer" else "information" if intent == "information" else "message", "text": content}}
            attach_to_message(self.store, row, message, prepared)
            merged_skills, dropped_skills = self._merge_task_skills(row.get("skills"), added_skills)
            prospective_dropped = list(
                dict.fromkeys([*(row.get("dropped_skill_ids") or []), *dropped_skills])
            )
            owners = [message for message in [*row["messages"], *row.get("pending_user_messages", [])] if message["role"] == "user"]
            prospective = {**row, "messages": [*owners, message], "skills": merged_skills,
                           "dropped_skill_ids": prospective_dropped,
                           "context_checkpoint": None, "tool_menu": None}
            # Fixed instruction layers plus compacted-rule owner messages (including
            # this new message), an empty checkpoint and tool schemas form the pinned
            # floor. Continue in a linked new conversation only when that floor no
            # longer fits capacity; a message too large on its own is still refused.
            capacity = self._effective_capacity(row)[0]
            try:
                (future_specs, _), authority_bytes = self._menu(prospective)
                too_large = authority_bytes + len(encoded(future_specs)) > capacity
            except AgentError as exc:
                if str(exc) != "agent_tool_schema_limit":
                    raise
                too_large = True
            if too_large:
                if not self._continuation_fits(row, prospective, message, content):
                    raise AgentError("agent_context_limit")
                raise ContinueInNewTask()
            if len(encoded(row)) + len(encoded(message)) > event_compaction.TASK_BYTES_HARD_LIMIT:
                raise ContinueInNewTask()
            memory_natural.stamp_single_owner_message_in_tx(data, message)
            row["skills"] = merged_skills
            self._record_skill_events_in_tx(
                row,
                skipped_warnings=skipped_skills,
                dropped_skill_ids=dropped_skills,
            )
            ended = row["status"] in {"completed", "failed", "interrupted", "cancelled"} or bool(row.get("turn_stopped"))
            if effort is not None:
                # Running requests already own an immutable task snapshot.
                # This preference starts with the next admitted request.
                row["reasoning_effort"] = effort
            budget.new_round(row)
            if ended:
                wrap_reason = (row.get("outcome") or {}).get("budget_wrap_up_reason")
                budget.extend_defaults_for_owner_turn(row, wrap_reason)
                b_state = budget.public(row)
                b_limits = b_state["limits"]
                b_life = b_state["lifetime"]
                if budget.is_hard(row, "max_total_tokens") and b_life["charged_tokens"] >= b_limits["max_total_tokens"]:
                    raise AgentError("agent_token_budget_exhausted")
                if budget.is_hard(row, "max_total_model_calls") and b_life["charged_model_calls"] >= b_limits["max_total_model_calls"]:
                    raise AgentError("agent_model_call_budget_exhausted")
                if budget.time_remaining(row) <= 0:
                    raise AgentError("agent_time_budget_exhausted")
            elif row["status"] == "paused" and not row.get("in_flight"):
                exhausted = budget.exhausted_hard_limit(row)
                if exhausted:
                    raise AgentError(exhausted)  # A new turn never bypasses a hard budget.
            if intent != "information" or ended:
                owner_update(row, message)
            # Clearing automation_run_id switches off unattended developer instructions
            # and post-turn automation settlement while keeping source/automation_id for UI provenance.
            row.update(approved=None, pending_approval=None, pending_input=None, result="",
                       latest_turn_is_silent_heartbeat=False, automation_run_id=None)
            row.pop("unattended_blocked", None)
            if row.get("fork"):
                row["fork"]["awaiting_instruction"] = False
            row.setdefault("pending_user_messages", []).append(message)
            was_stopped = row.pop("turn_stopped", False)
            row["pending_control"] = {"kind": "steer", "remain_paused": row["status"] == "paused" and bool(row.get("in_flight")) and intent is not None and not was_stopped}
            if ended or intent is None or was_stopped or (row["status"] == "paused" and not row.get("in_flight")):
                row.update(steps=0, elapsed_seconds=0.0)
                budget.restart_segment_audit(row)
            event(row, "goal_changed" if intent == "steer" else "information_added" if intent else "message_added", goal_revision=row["goal"]["revision"])
            # A suspended wait must return its steering receipt before the
            # owner turn cancels the remaining proposals and enters history.
            if not (row.get("subagent_wait") and row["status"] == "queued" and row.get("queue")):
                row.pop("subagent_wait", None)
                close_queue(row, event, "agent_owner_changed")
                settle(row, event, data=data)
            if row.get("subagent_ids") and "@" in content:
                routed_ids: set[str] = set()
                for token in re.findall(r"@([A-Za-z0-9_-]+)", content):
                    tok_low = token.strip().lower()
                    if not tok_low or tok_low in {"all", "*"}:
                        continue
                    try:
                        matched = subagents.resolve_subagent_targets(data, row, token)
                        for child in matched:
                            cid = child.get("id")
                            if not cid or cid in routed_ids:
                                continue
                            meta = child.get("subagent_meta") or {}
                            nick_low = str(meta.get("nickname") or "").lower()
                            tname_low = str(meta.get("task_name") or "").lower()
                            if tok_low not in {nick_low, tname_low}:
                                continue
                            if not meta.get("closed"):
                                routed_ids.add(cid)
                                if child.get("status") == "running" or child.get("in_flight"):
                                    subagents.send_message_in_tx(data, row, {"target": cid, "message": content})
                                else:
                                    subagents.followup_task_in_tx(data, row, {"target": cid, "prompt": content})
                    except Exception:
                        pass
            remember_request(row, value)
            return public_task(row, settings=data["settings"], data=data)
        try:
            return self.store.transaction(add)
        except ContinueInNewTask:
            return self._continue_in_new_task(task_id, value, content)

    def _continuation_fits(self, row: dict, prospective: dict, message: dict, content: str) -> bool:
        """Whether this message, as the first message of a continued conversation, fits the model."""
        first_text = CONTINUATION_PREFIX + event_compaction.continuation_summary(row) + CONTINUATION_SEPARATOR + content
        first = {**message, "content": first_text if isinstance(message.get("content"), str) else message["content"]}
        alone = {**prospective, "messages": [first], "goal": {**row["goal"], "text": first_text, "constraints": []}}
        try:
            (specs, _), authority_bytes = self._menu(alone)
        except AgentError:
            return False
        return authority_bytes + len(encoded(specs)) <= self._effective_capacity(row)[0]

    def _continue_in_new_task(self, task_id: str, value: dict, content: str) -> dict:
        """The conversation is too large even after condensing: continue it in a new, linked one."""
        old = lifecycle.find(self.store, self.store.read(), task_id)
        fingerprint = digest({"source_task_id": task_id, **{k: v for k, v in value.items()
                              if k not in {"expected_revision", "expected_control_revision", "expected_goal_revision"}}})
        if old.get("continued_to"):
            data = self.store.read()
            child = lifecycle.find(self.store, data, old["continued_to"])
            if child.get("continuation_request_fingerprint") != fingerprint:
                raise AgentError("agent_continuation_already_exists")
            return public_task(child, settings=data["settings"], data=data)
        summary = event_compaction.continuation_summary(old)
        body = {"message": CONTINUATION_PREFIX + summary + CONTINUATION_SEPARATOR + content,
                "model_id": old["model_id"],
                "title": event_compaction.continued_title(old.get("title") or "")}
        if value.get("client_request_id"):
            body["client_request_id"] = value["client_request_id"]
        if value.get("attachments"):
            body["attachments"] = value["attachments"]
        for key in ("skill_ids", "reasoning_effort"):
            if key in value:
                body[key] = value[key]
        if isinstance(old.get("project"), dict) and old["project"].get("id"):
            body["project_id"] = old["project"]["id"]
        return self.create(body, _continuation={"id": task_id, "revision": old["revision"], "fingerprint": fingerprint})

    def _edit_message(
        self,
        task_id: str,
        value: dict,
        content: str,
        prepared=None,
        *,
        added_skills=None,
        skipped_skills=None,
    ) -> dict:
        if value.get("intent") not in {None, "steer", "information"} or type(value["edit_message_index"]) is not int:
            raise AgentError("agent_invalid_request")

        def edit(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value, goal=value.get("intent") is not None)
            if not data["settings"]["enabled"]:
                raise AgentError("agent_disabled")
            if not allowed_actions(row, data["settings"])["edit_message"]["enabled"]:
                raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_invalid_transition")
            index = value["edit_message_index"]
            if index < 0 or index >= len(row["messages"]) or row["messages"][index].get("role") != "user":
                raise AgentError("agent_invalid_message_index")
            if event_compaction.needs_compaction(row, len(encoded(row))):
                event_compaction.compact_until_small(row)
            if len(row["events"]) >= 998 or len(encoded(row)) + len(content.encode()) > 2 * 1024 * 1024:
                raise AgentError("agent_task_limit")
            replacement = {"role": "user", "content": content, "message_id": "owner_" + uuid.uuid4().hex,
                           "ui": {"kind": "message", "text": content}}
            if prepared is None:
                replacement["attachments"] = copy.deepcopy(row["messages"][index].get("attachments", []))
            attach_to_message(self.store, row, replacement, prepared)
            if len(encoded(row)) + len(encoded(replacement)) > 2 * 1024 * 1024:
                raise AgentError("agent_task_limit")
            # Test the replacement owner's pinned floor before committing stop
            # intent. Discarded future constraints cannot consume its context.
            merged_skills, dropped_skills = self._merge_task_skills(row.get("skills"), added_skills)
            prospective_dropped = list(
                dict.fromkeys([*(row.get("dropped_skill_ids") or []), *dropped_skills])
            )
            retained = [*row["messages"][:index], replacement]
            owner_ids = {message.get("message_id") for message in retained if message["role"] == "user"}
            prospective = {**row, "messages": retained, "skills": merged_skills,
                           "dropped_skill_ids": prospective_dropped,
                           "context_checkpoint": None, "tool_menu": None,
                           "goal": {**row["goal"], "constraints": [item for item in row["goal"]["constraints"] if item.get("message_id") in owner_ids]}}
            (future_specs, _), authority_bytes = self._menu(prospective)
            if authority_bytes + len(encoded(future_specs)) > self._context_limit(row):
                raise AgentError("agent_context_limit")
            memory_natural.stamp_single_owner_message_in_tx(data, replacement)
            row["skills"] = merged_skills
            self._record_skill_events_in_tx(
                row,
                skipped_warnings=skipped_skills,
                dropped_skill_ids=dropped_skills,
            )
            if "reasoning_effort" in value:
                row["reasoning_effort"] = reasoning.validate(self._model_metadata(row["model_id"]), value["reasoning_effort"])
            row.update(status="paused", approved=None, pending_approval=None, pending_input=None,
                       latest_turn_is_silent_heartbeat=False, automation_run_id=None,
                       pending_control={"kind": "edit_message", "message_index": index, "message": replacement})
            row.pop("unattended_blocked", None)
            event(row, "message_edit_requested", message_index=index, operation_in_flight=bool(row["in_flight"]))
            close_queue(row, event, "agent_owner_changed")
            settle(row, event, data=data)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(edit)

    def action(self, task_id: str, value: dict) -> dict:
        if set(value) - {"action", "permission_mode", "expected_revision", "expected_control_revision", "expected_goal_revision", "approval_id", "pending_input_id", "answers", "client_request_id", "subagent_id", "target"}:
            raise AgentError("agent_invalid_request")
        action = value.get("action")

        def apply(data):
            row = task(data, task_id)
            if row.get("continued_to") and action in {"resume", "approve", "answer"}:
                raise AgentError("agent_task_continued")
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"], data=data)
            if action not in {"pause", "stop", "cancel", "interrupt_subagent", "stop_all_subagents"}:
                check_version(row, value, goal=action == "answer")
            if action == "interrupt_subagent":
                target_id = str(value.get("subagent_id") or value.get("target") or "").strip()
                if not target_id:
                    raise AgentError("agent_invalid_request")
                try:
                    subagents.interrupt_agent_in_tx(data, row, {"target": target_id})
                except AgentToolRejected as exc:
                    raise AgentError(str(exc)) from None
                remember_request(row, value)
                return public_task(row, settings=data["settings"], data=data)
            elif action == "stop_all_subagents":
                subagents.interrupt_agent_in_tx(data, row, {"target": "all"})
                remember_request(row, value)
                return public_task(row, settings=data["settings"], data=data)
            elif action == "cancel":
                retry_policy.stop(row)
                if row["status"] not in {"completed", "failed", "cancelled"}:
                    row.update(status="cancelled", pending_approval=None, approved=None, pending_input=None, pending_control={"kind": "cancel"})
                    event(row, "cancelled", operation_in_flight=bool(row["in_flight"]))
                    close_queue(row, event, "agent_cancelled")
                    settle(row, event, data=data)
                    subagents.on_subagent_settled_in_tx(data, row)
                    subagents.cascade_stop_subagents_in_tx(data, row, reason="owner_cancel", close=True)
            elif action == "stop":
                retry_policy.stop(row)
                if ambiguous(row):
                    row["pending_control"] = {"kind": "stop"}
                    event(row, "paused", operation_in_flight=True, reason="owner_stopped")
                elif row["status"] in {"queued", "running", "waiting_approval", "waiting_input", "paused"}:
                    # Capture the accepted text already shown in the composer
                    # before cancelling transport; a late response is discarded.
                    stream = self._stream_snapshot(row)
                    if stream:
                        row["messages"].append({"role": "assistant", "content": stream["text"],
                                                "context_epoch": row.get("context_epoch", 0), "interrupted": True})
                    if data["settings"].get("interrupt_message_enabled", True):
                        while row["messages"] and row["messages"][-1].get("ui", {}).get("kind") == "turn_aborted":
                            row["messages"].pop()
                        row["messages"].append({"role": "user", "content": TURN_ABORTED_NOTE,
                                                "ui": {"kind": "turn_aborted", "hidden": True},
                                                "context_epoch": row.get("context_epoch", 0)})
                    row.update(status="paused", pending_approval=None, approved=None, pending_input=None,
                               pending_control={"kind": "stop"}, turn_stopped=True)
                    event(row, "paused", operation_in_flight=bool(row["in_flight"]), reason="owner_stopped")
                    close_queue(row, event, "agent_cancelled")
                    settle(row, event, data=data)
                    subagents.on_subagent_settled_in_tx(data, row)
                    subagents.cascade_stop_subagents_in_tx(data, row, reason="owner_stop", close=False)
            elif action == "pause":
                retry_policy.stop(row)
                row.pop("turn_stopped", None)
                if ambiguous(row):
                    # Stop intent survives reconciliation, even while the write's
                    # outcome is unknown. Pausing never clears that marker.
                    row["pending_control"] = {"kind": "pause"}
                    event(row, "paused", operation_in_flight=True)
                    remember_request(row, value)
                    return public_task(row, settings=data["settings"], data=data)
                if row["status"] in {"paused", "completed", "failed", "cancelled", "interrupted"}:
                    remember_request(row, value)
                    return public_task(row, settings=data["settings"], data=data)
                if row["status"] not in {"queued", "running", "waiting_approval", "waiting_input"}:
                    raise AgentError("agent_invalid_transition")
                row["status"] = "paused"
                row["pending_control"] = {"kind": "pause"}
                event(row, "paused", operation_in_flight=bool(row["in_flight"]))
                settle(row, event, data=data)
                subagents.on_subagent_settled_in_tx(data, row)
                subagents.cascade_stop_subagents_in_tx(data, row, reason="owner_pause", close=False)
            elif action == "set_permission":
                if not allowed_actions(row, data["settings"])["set_permission"]["enabled"]:
                    raise AgentError("agent_invalid_transition")
                mode = value.get("permission_mode")
                if not isinstance(mode, str) or mode not in PERMISSION_MODES:
                    raise AgentError("agent_invalid_permission_mode")
                old_mode = row["permission_mode"]
                row.update(permission_mode=mode, approved=None, pending_approval=None)
                row.pop("task_grants", None)
                if row["status"] == "waiting_approval":
                    row["status"] = "paused"
                row["control_revision"] += 1
                event(row, "permission_changed", old_mode=old_mode, new_mode=mode)
                if row.get("pending_input"):
                    row["pending_input"]["control_revision"] = row["control_revision"]
            elif action == "resume":
                if (row.get("fork") or {}).get("awaiting_instruction"):
                    raise AgentError("agent_fork_instruction_required")
                if row["status"] not in {"paused", "interrupted"}:
                    raise AgentError("agent_invalid_transition")
                if not data["settings"]["enabled"]:
                    raise AgentError("agent_disabled")
                row.pop("turn_stopped", None)
                if row["in_flight"]:
                    if not recoverable_read_in_flight(row) or ambiguous(row):
                        raise AgentError("agent_operation_unresolved")
                    flight = row["in_flight"]
                    err_code = row.get("error") or "agent_read_interrupted"
                    cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=err_code)
                    if not cleanup_ok:
                        raise AgentError("agent_operation_unresolved")
                    if flight.get("effect") == "write":
                        self._settle_interrupted_read(row, flight, err_code, recovery=recovery)
                    row["in_flight"] = None
                    row["approved"] = None
                    settle(row, event, data=data)
                if not row["pending_input"] and not row["pending_approval"]:
                    exhausted = budget.exhausted_hard_limit(row)
                    if exhausted:
                        raise AgentError(exhausted)  # Resume never bypasses a hard budget.
                if (row.get("steps", 0) >= (row.get("limits") or {}).get("max_steps", 16)
                        or row.get("elapsed_seconds", 0.0) >= (row.get("limits") or {}).get("max_seconds", 180)):
                    row.update(steps=0, elapsed_seconds=0.0, model_retry_count=0)
                    row.pop("model_retry_authority", None)
                    budget.new_round(row)
                budget.restart_segment_audit(row)
                for key in ("budget_limited", "budget_stop", "budget_wrap_up_reason"):
                    row.get("outcome", {}).pop(key, None)
                retry_policy.resume(row)
                row["status"] = "waiting_input" if row["pending_input"] else "waiting_approval" if row["pending_approval"] else "queued"
                row["error"] = None
                event(row, "resumed")
                if row["pending_approval"]:
                    row["pending_approval"]["control_revision"] = row["control_revision"]
                if row["pending_input"]:
                    row["pending_input"]["control_revision"] = row["control_revision"]
            elif action in {"approve", "deny"}:
                approval = row["pending_approval"]
                if row["status"] != "waiting_approval" or not approval or value.get("approval_id") != approval["id"]:
                    raise AgentError("agent_approval_mismatch")
                call = row["queue"][0] if row["queue"] else None
                if not call or approval["arguments_sha256"] != digest(call["arguments"]) or call["call_id"] != approval["call_id"] or call["name"] != approval["tool"]:
                    raise AgentError("agent_approval_mismatch")
                if approval.get("goal_revision") != row["goal"]["revision"] or approval.get("scope_revision") != row["scope_revision"] or approval.get("control_revision") != row["control_revision"]:
                    raise AgentError("agent_approval_mismatch")
                if not data["settings"]["enabled"]:
                    raise AgentError("agent_disabled")
                if action == "approve":
                    if row["permission_mode"] == "read_only":
                        raise AgentError("agent_permission_denied")
                    row["approved"] = {**approval, "revision": row["revision"]}
                    event(row, "approved", approval_id=approval["id"], tool=approval["tool"])
                else:
                    from .execution_policy import OWNER_DENIED_CORRECTION
                    denied = {"error": "owner_denied_operation", "not_executed": True, "no_mutation": True,
                              "correction": OWNER_DENIED_CORRECTION}
                    row["messages"].append({"role": "tool", "tool_call_id": call["call_id"], "content": encoded(denied).decode(), "context_epoch": call.get("context_epoch", 0)})
                    verification.record(row, call, "not_executed", now(), result=denied)
                    row["queue"].pop(0)
                    event(row, "denied", approval_id=approval["id"], tool=approval["tool"])
                row.update(pending_approval=None, status="queued")
            elif action == "answer":
                pending = row.get("pending_input")
                if row["status"] != "waiting_input" or not pending or value.get("pending_input_id") != pending["id"]:
                    raise AgentError("agent_input_mismatch")
                if not data["settings"]["enabled"]:
                    raise AgentError("agent_disabled")
                if pending["goal_revision"] != row["goal"]["revision"] or pending["scope_revision"] != row["scope_revision"] or pending["control_revision"] != row["control_revision"]:
                    raise AgentError("agent_input_mismatch")
                if pending["kind"] == "credential_ref":
                    raise AgentError("agent_secure_credential_form_required")
                answers = value.get("answers")
                fields = {field["id"]: field for field in pending["fields"]}
                if not isinstance(answers, dict) or set(answers) - set(fields):
                    raise AgentError("agent_invalid_answer")
                for key, field in fields.items():
                    answer = answers.get(key)
                    if answer is None and not field["required"]:
                        continue
                    if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000 or (field["type"] == "choice" and answer not in {o["value"] for o in field["options"]}):
                        raise AgentError("agent_invalid_answer")
                message = {"role": "user", "message_id": "owner_" + uuid.uuid4().hex,
                           "content": "Owner supplied information (not new permission): " + encoded(answers).decode(),
                           "ui": {"kind": "input_answer", "prompt": pending["prompt"], "pending_input_id": pending["id"],
                                  "fields": [{"label": fields[key]["label"], "value": next((option["label"] for option in fields[key].get("options", []) if option["value"] == answer), answer)}
                                             for key, answer in answers.items()]}}
                memory_natural.stamp_single_owner_message_in_tx(data, message)
                row["messages"].append(message)
                budget.new_round(row)
                row.update(pending_input=None, status="queued")
                event(row, "input_answered", pending_input_id=pending["id"])
            elif action == "reconcile":
                if not allowed_actions(row, data["settings"])["reconcile"]["enabled"] or not hasattr(self.tools, "reconcile"):
                    raise AgentError("agent_invalid_transition")
                if recoverable_read_in_flight(row) and not ambiguous(row):
                    flight = row["in_flight"]
                    err_code = row.get("error") or "agent_read_interrupted"
                    attempts = row.get("reconciliation", {}).get("attempts", 0) + 1
                    cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=err_code)
                    if not cleanup_ok:
                        row["reconciliation"] = {
                            "state": "unknown",
                            "attempts": attempts,
                            "operation_id": flight.get("operation_id"),
                            "reason": "agent_operation_unresolved",
                            "checked_at": now(),
                        }
                        event(row, "reconciliation_finished", outcome="unknown", operation_id=flight.get("operation_id"), continued=False)
                    else:
                        outcome = "not_executed"
                        if flight.get("effect") == "write":
                            outcome = self._settle_interrupted_read(row, flight, err_code, recovery=recovery)
                        may_continue = self._read_may_continue_after_restart(row, flight, data)
                        row["in_flight"] = None
                        row["approved"] = None
                        settle(row, event, data=data)
                        row["reconciliation"] = {
                            "state": "executed" if outcome == "confirmed" else "not_executed",
                            "attempts": attempts,
                            "operation_id": flight.get("operation_id"),
                            "checked_at": now(),
                        }
                        row.update(status="queued" if may_continue else "paused", error=None)
                        event(row, "reconciliation_finished", outcome=row["reconciliation"]["state"], operation_id=flight.get("operation_id"), continued=may_continue)
                else:
                    row["reconciliation"] = {"state": "pending", "attempts": row.get("reconciliation", {}).get("attempts", 0),
                                             "requested_at": now()}
                    event(row, "reconciliation_requested")
            else:
                raise AgentError("agent_invalid_action")
            remember_request(row, value)
            return public_task(row, settings=data["settings"], data=data)
        return self.store.transaction(apply)

    def _recover_read_operation_details(
        self, row: dict, flight: dict | None, *, error: str = "agent_read_interrupted"
    ) -> tuple[bool, dict]:
        from .execution_policy import WORKSPACE_TOOLS
        recover = getattr(self.tools, "recover_operation", None)
        if not callable(recover) and getattr(self.tools, "local", None) is not None:
            recover = getattr(self.tools.local, "recover_operation", None)
        if not callable(recover):
            ok = (flight or {}).get("name") not in WORKSPACE_TOOLS
            return ok, (
                {
                    "ok": True,
                    "ledger_outcome": "failed",
                    "settled_result": {"error": error, "not_executed": False, "no_mutation": True},
                }
                if ok
                else {"ok": False}
            )
        try:
            stopped = recover(
                row["id"],
                (flight or {}).get("operation_id"),
                error=error,
                operation_classification=(flight or {}).get("operation_classification"),
            )
        except Exception:
            return False, {"ok": False}
        ok = (
            isinstance(stopped, dict)
            and stopped.get("still_running") is False
            and not stopped.get("cleanup_error")
            and not stopped.get("process_ids")
            and stopped.get("ok", True) is not False
            and stopped.get("ledger_settled", True) is not False
        )
        return ok, (stopped if isinstance(stopped, dict) else {"ok": False})

    def _recover_read_operation(self, row: dict, flight: dict | None, *, error: str = "agent_read_interrupted") -> bool:
        ok, _ = self._recover_read_operation_details(row, flight, error=error)
        return ok

    def _note_images_omitted(self, task_id: str, count: int, reason: str, latest: bool) -> None:
        """Tell the owner once per turn that images were not sent to the model."""
        def note(data):
            row = data["tasks"].get(task_id)
            if row is None:
                return
            owner_seq = max((item["seq"] for item in row["events"] if item["type"] in OWNER_TURN_EVENTS), default=0)
            if any(item["type"] == "images_omitted" and item["seq"] > owner_seq for item in row["events"]):
                return
            event(row, "images_omitted", count=count, reason=str(reason or "")[:80], latest_message=bool(latest))
        try:
            self.store.transaction(note)
        except AgentError:
            pass  # A notice must never fail the request itself.

    def _note_shared_image_resize(self, task_id: str, report: dict) -> None:
        def note(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                event(row, "images_resized_for_shared_route",
                      **{key: report[key] for key in ("carrier", "images", "resized", "original_bytes", "final_bytes")
                         if type(report.get(key)) in (int, str)})
        try:
            self.store.transaction(note)
        except AgentError:
            pass

    def _truncate_unknown_error_details(self, text: str | None) -> str | None:
        if not isinstance(text, str) or not text.strip():
            return None
        cleaned = "".join(ch if ch in ("\n", "\t") or ord(ch) >= 0x20 else " " for ch in sanitized(text))
        cleaned = re.sub(r"(?i)\bBasic\s+[A-Za-z0-9._~+/-]+=*", "Basic [redacted]", cleaned).strip()
        if not cleaned:
            return None
        raw = cleaned.encode("utf-8")
        if len(raw) <= 900:
            return cleaned
        return raw[:900].decode("utf-8", errors="ignore")

    def _prior_unknown_count(self, row: dict, flight: dict) -> int:
        def _is_owner_boundary(ev: dict) -> bool:
            ev_type = ev.get("type")
            if ev_type == "unknown_outcome_acknowledged":
                return not bool(ev.get("auto_acknowledged"))
            return ev_type in OWNER_TURN_EVENTS

        epoch = flight.get("context_epoch", row.get("context_epoch", 0))
        tool_name = flight.get("name")
        consecutive = 0
        last_owner_at = ""
        for prev_event in reversed(row.get("events", [])):
            if _is_owner_boundary(prev_event):
                last_owner_at = str(prev_event.get("at") or "")
                break
        for prev_event in reversed(row.get("events", [])):
            if _is_owner_boundary(prev_event):
                break
            if prev_event.get("type") != "tool_result":
                continue
            if prev_event.get("context_epoch", 0) != epoch:
                break
            prev_res = prev_event.get("result")
            prev_err = prev_res.get("error") if isinstance(prev_res, dict) else None
            if prev_event.get("tool") == tool_name and prev_err in {
                "agent_write_outcome_unknown",
                "agent_mcp_outcome_unknown_reported",
            }:
                consecutive += 1
            else:
                break
        same_call = 0
        if "arguments" in flight and tool_name:
            arg_hash = digest(flight["arguments"])
            op_id = flight.get("operation_id")
            goal_rev = row["goal"]["revision"]
            for receipt in row.get("receipts", []):
                if (
                    receipt.get("operation_id") != op_id
                    and receipt.get("goal_revision") == goal_rev
                    and receipt.get("context_epoch", 0) == epoch
                    and str(receipt.get("started_at") or "") > last_owner_at
                    and receipt.get("tool") == tool_name
                    and receipt.get("arguments_sha256") == arg_hash
                    and receipt.get("status") == "unknown"
                ):
                    same_call += 1
        return max(consecutive, same_call)

    def _auto_acknowledge_allowed(
        self, row: dict, data: dict, flight: dict | None, *, code: str | None = None
    ) -> bool:
        if not isinstance(flight, dict) or not flight:
            return False
        settings = data.get("settings") or {}
        is_hint_read_unknown = (
            code == "agent_mcp_outcome_unknown_reported" and _flight_has_server_hint_read(flight)
        )
        if not is_hint_read_unknown and (
            row.get("permission_mode") != "automatic"
            or flight.get("authorization") != "automatic"
        ):
            return False
        if (
            not settings.get("enabled")
            or row.get("pending_control")
            or row.get("pending_approval")
            or row.get("pending_input")
            or row.get("status") in {"cancelled", "paused"}
            or budget.hard_run_limit(row)
            or budget.exhausted_hard_limit(row)
            or budget.time_remaining(row) <= 0
        ):
            return False
        budget_state = budget.public(row)
        limits = budget_state["limits"]
        lifetime = budget_state["lifetime"]
        if budget.is_hard(row, "max_total_tool_calls") and lifetime["charged_tool_calls"] >= limits["max_total_tool_calls"]:
            return False
        op_id = flight.get("operation_id")
        if isinstance(op_id, str) and op_id in (row.get("auto_acknowledged_operations") or []):
            return False
        if self._prior_unknown_count(row, flight) >= 2:
            return False
        return True

    def _record_auto_acknowledged_operation(self, row: dict, operation_id: Any) -> None:
        if not isinstance(operation_id, str) or not operation_id:
            return
        prior = [item for item in (row.get("auto_acknowledged_operations") or []) if isinstance(item, str) and item != operation_id]
        prior.append(operation_id)
        row["auto_acknowledged_operations"] = prior[-20:]

    def _acknowledge_unknown_in_tx(
        self,
        row: dict,
        *,
        resume: bool = False,
        error_details: str | None = None,
        repeated_unknown: bool = False,
        error_code: str = "agent_write_outcome_unknown",
        hint: str | None = None,
        keep_sibling_queue: bool = False,
    ) -> None:
        """The owner (or automatic mode) continues past an unconfirmed write.

        The model is told the step may have run; the operation itself is never
        queued or dispatched again, and its receipt stays "unknown".
        """
        flight = row.get("in_flight") or {}
        call_id = flight.get("call_id")
        epoch = flight.get("context_epoch", row.get("context_epoch", 0))
        result: dict[str, Any] = {
            "error": error_code,
            "outcome": "unknown",
            "not_executed": False,
            "note": UNKNOWN_OUTCOME_NOTE,
        }
        if isinstance(hint, str) and hint:
            result["hint"] = hint
        truncated_details = self._truncate_unknown_error_details(error_details)
        if truncated_details is not None:
            result["details"] = truncated_details
        if repeated_unknown:
            result["correction"] = (
                "This tool already produced an unconfirmed outcome on the previous attempt. "
                "Inspect the actual state with read-only tools or report the uncertainty "
                "instead of repeating the unconfirmed write."
            )
        if call_id and not any(item.get("role") == "tool" and item.get("tool_call_id") == call_id
                               for item in row["messages"]):
            row["messages"].append({"role": "tool", "tool_call_id": call_id, "content": encoded(result).decode(),
                                    "context_epoch": epoch, "is_error": True})
            event(row, "tool_result", tool=flight.get("name"), call_id=call_id,
                  operation_id=flight.get("operation_id"), result=result, context_epoch=epoch)
        row["queue"] = [call for call in row.get("queue", []) if call.get("call_id") != call_id]
        if not keep_sibling_queue:
            close_queue(row, event, "agent_reconciliation_requires_review", keep_flight=False)
        if (row.get("pending_control") or {}).get("kind") in {"pause", "stop", "permissions"}:
            row.pop("pending_control", None)
        row["acknowledged_unknown"] = {"operation_id": flight.get("operation_id"), "tool": flight.get("name"), "at": now()}
        row.update(in_flight=None, approved=None, pending_approval=None, error=None)
        if resume:
            if row["status"] != "cancelled":
                row["status"] = "queued"
        elif row["status"] not in {"interrupted", "cancelled"}:
            row["status"] = "interrupted"
        event(
            row,
            "unknown_outcome_acknowledged",
            operation_id=flight.get("operation_id"),
            tool=flight.get("name"),
            **({"auto_acknowledged": True} if resume else {}),
        )

    def _settle_interrupted_read(
        self, row: dict, flight: dict, error_code: str, *, recovery: dict | None = None
    ) -> str:
        call_id = flight.get("call_id")
        outcome = (recovery or {}).get("ledger_outcome")
        if outcome not in {"confirmed", "not_executed", "failed"}:
            outcome = "failed"
        if not call_id:
            return outcome
        raw_result = (recovery or {}).get("settled_result")
        if isinstance(raw_result, dict):
            result = sanitized(copy.deepcopy(raw_result))
        elif outcome == "not_executed":
            result = {"error": error_code, "not_executed": True, "no_mutation": True}
        else:
            result = {"error": error_code, "not_executed": False, "no_mutation": True}
        epoch = flight.get("context_epoch", row.get("context_epoch", 0))
        if outcome == "confirmed":
            result_size = len(encoded(result))
            if result_size > row["limits"]["max_output_chars"]:
                reference = results.save(self.store, row, flight, result)
                row.setdefault("result_refs", []).append(reference)
                result = {
                    "result_ref": reference["id"],
                    "output_sha256": reference["sha256"],
                    "bytes": result_size,
                    "note": "Full output is encrypted locally. Use ryn_read_result for bounded pages; this reference is not proof of the requested outcome.",
                }
        row["messages"].append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": encoded(result).decode(),
            "context_epoch": epoch,
        })
        verification.record(row, flight, outcome, now(), result=result, flight=flight)
        row["queue"] = [call for call in row.get("queue", []) if call.get("call_id") != call_id]
        event(
            row,
            "tool_result",
            tool=flight.get("name"),
            call_id=call_id,
            operation_id=flight.get("operation_id"),
            result=result,
            context_epoch=epoch,
        )
        return outcome

    def _may_continue_after_restart(self, row: dict, data: dict) -> bool:
        if (
            not data["settings"].get("enabled")
            or row.get("status") in {"cancelled", "paused", "editing"}
            or (row.get("pending_control") and row["pending_control"].get("kind") != "steer")
            or row.get("pending_input")
            or row.get("pending_approval")
            or write_permission_revoked(row, data["settings"])
            or budget.hard_run_limit(row)
            or budget.time_remaining(row) <= 0
        ):
            return False
        budget_state = budget.public(row)
        limits = budget_state["limits"]
        lifetime = budget_state["lifetime"]
        if (
            lifetime["charged_tokens"] >= limits["max_total_tokens"]
            or lifetime["charged_model_calls"] >= limits["max_total_model_calls"]
            or lifetime["charged_tool_calls"] >= limits["max_total_tool_calls"]
        ):
            return False
        bound = row.get("local_execution") or {}
        if bound.get("project_id"):
            try:
                project = projects.validate_binding(
                    data,
                    {
                        "id": bound["project_id"],
                        "path": bound.get("workspace_root"),
                        "generation": bound.get("project_generation"),
                    },
                )
                current_root = project["path"]
            except AgentError:
                return False
        else:
            current_root = data["settings"].get("workspace_root") or self.store.default_workspace()
        if bound.get("workspace_root", "") != current_root:
            return False
        try:
            self._validate_context(row, cheap_only=True, control_state=data)
        except AgentError:
            return False
        return True

    def _read_may_continue_after_restart(self, row: dict, flight: dict, data: dict) -> bool:
        steer = (row.get("pending_control") or {}).get("kind") == "steer"
        return bool(
            self._may_continue_after_restart(row, data)
            and (steer or (flight.get("control_revision") == row["control_revision"]
                           and flight.get("goal_revision") == row["goal"]["revision"]))
            and flight.get("scope_revision") == row["scope_revision"]
            and flight.get("context_epoch", 0) == row.get("context_epoch", 0)
            and flight.get("authority") == model_retry_authority(data["settings"])
        )

    def adopt_shipped_permission_mode(self, mode: str) -> bool:
        """Record legacy mode provenance without changing a saved mode.

        Older builds saved "workspace" without recording who chose it, so an
        explicit owner choice cannot be distinguished from the old default.
        Fresh stores use the shipped mode; existing settings and tasks stay as
        saved. Called at node construction, after reconcile and before workers.
        """
        if mode not in PERMISSION_MODES:
            raise AgentError("agent_invalid_settings")
        if not self.store.path.exists() or self.store.inspect(lambda state: PERMISSION_MODE_SOURCE in state):
            return False

        def adopt(data):
            if PERMISSION_MODE_SOURCE in data:
                return False
            data[PERMISSION_MODE_SOURCE] = "legacy"
            return False
        return self.store.transaction(adopt)

    _note_lost_background_processes = staticmethod(_note_lost_background_processes)

    def reconcile(self) -> None:
        """Called once at node construction, before worker startup."""
        if not self.store.path.exists():
            return
        boot_id = self._process_boot_id()

        def reconcile(data):
            from .execution_policy import verified_read_classification
            for row in data["tasks"].values():
                journal = row.get("stream_read_journal", [])
                if journal:
                    recover_stream_reads(row, allow_publish=bool(data["settings"].get("enabled") and all(
                        entry["authority"] == model_retry_authority(data["settings"]) for entry in journal)))
                budget.abandon(row)
                flight = row.get("in_flight")
                if flight or row["status"] == "running" or row.get("approved"):
                    was_cancelled = row["status"] == "cancelled" or (row.get("pending_control") or {}).get("kind") == "cancel"
                    was_paused = row["status"] == "paused" or (row.get("pending_control") or {}).get("kind") in {"pause", "stop"}
                    was_editing = (row.get("pending_control") or {}).get("kind") == "edit_message"
                    steer_pending = (row.get("pending_control") or {}).get("kind") in {"steer", "scope", "permissions"}
                    workspace_root = (row.get("local_execution") or {}).get("workspace_root", "")
                    is_verified_read = bool(
                        flight
                        and flight.get("dispatched")
                        and verified_read_classification(flight, task_id=row["id"], workspace_root=workspace_root)
                    )
                    ambiguous_write = bool(
                        flight
                        and flight.get("effect") == "write"
                        and flight.get("dispatched")
                        and not is_verified_read
                    )
                    cleanup_ok = True
                    recovery = None
                    if is_verified_read or (flight and flight.get("effect") == "read" and flight.get("dispatched")):
                        cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error="agent_node_restarted")
                    if (
                        ambiguous_write
                        and not was_cancelled
                        and not was_paused
                        and not was_editing
                        and self._auto_acknowledge_allowed(row, data, flight)
                    ):
                        repeated = self._prior_unknown_count(row, flight) >= 1
                        op_id = flight.get("operation_id")
                        verification.record(row, flight, "unknown", now(), flight=flight)
                        self._acknowledge_unknown_in_tx(row, resume=True, repeated_unknown=repeated)
                        self._record_auto_acknowledged_operation(row, op_id)
                        row.update(status="queued", error=None)
                        event(
                            row,
                            "interrupted",
                            ambiguous_write=True,
                            continued=True,
                            reason="node_restarted",
                            auto_acknowledged=True,
                        )
                    else:
                        if ambiguous_write:
                            row.update(status="interrupted", approved=None, error="agent_write_outcome_unknown")
                            verification.record(row, flight, "unknown", now(), flight=flight)
                            self._schedule_reconciliation(row)
                            event(row, "interrupted", ambiguous_write=True)
                        else:
                            read_flight = bool(flight and flight.get("dispatched") and (is_verified_read or flight.get("effect") == "read"))
                            may_continue = (self._read_may_continue_after_restart(row, flight, data) if read_flight
                                            else self._may_continue_after_restart(row, data))
                            settled_outcome = None
                            if cleanup_ok:
                                if read_flight:
                                    settled_outcome = self._settle_interrupted_read(row, flight, "agent_node_restarted", recovery=recovery)
                                row["in_flight"] = None
                                row["approved"] = None
                                settle(row, event, data=data)
                            owner_continued = (cleanup_ok and steer_pending and row["status"] == "queued"
                                               and not was_cancelled and not was_paused and not was_editing)
                            auto_continued = (cleanup_ok and may_continue and not owner_continued
                                              and not was_cancelled and not was_paused and not was_editing
                                              and row.get("restart_auto_continued") != row["goal"]["revision"])
                            if was_editing and cleanup_ok:
                                continued = row["status"] == "queued"
                            elif was_cancelled or was_paused:
                                row.update(status="cancelled" if was_cancelled else "paused", error="agent_node_restarted")
                                continued = False
                            elif owner_continued or auto_continued:
                                row.update(status="queued", error=None)
                                continued = True
                                if auto_continued:
                                    row["restart_auto_continued"] = row["goal"]["revision"]
                                    note = {"role": "assistant", "content": "Ryn 在任务运行中重启；从已记录历史继续，重复任何操作前先查状态。",
                                            "context_epoch": row.get("context_epoch", 0), "ui": {"kind": "information", "hidden": True}}
                                    messages = row["messages"]
                                    index = len(messages) - 1 if row.get("queue") and messages[-1].get("tool_calls") else len(messages)
                                    messages.insert(index, note)
                            else:
                                row.update(status="interrupted", approved=None, error="agent_node_restarted")
                                continued = False
                            event(row, "interrupted", ambiguous_write=False, continued=continued,
                                  reason="owner_message_pending" if owner_continued else "node_restarted",
                                  **({"cleanup_confirmed": cleanup_ok} if read_flight else {}),
                                  **({"ledger_outcome": settled_outcome} if settled_outcome else {}))
                if row.get("status") not in {"completed", "cancelled", "failed"}:
                    _note_lost_background_processes(row, boot_id)
            deleted_ids = []
            # Old releases removed only the parent. Repair only explicit children;
            # top-level conversations must never be inferred to be orphans.
            for row in list(data["tasks"].values()):
                parent = row.get("parent_task_id")
                if row["id"] not in data["tasks"] or not parent or parent in data["tasks"]:
                    continue
                rows = lifecycle.tree_rows(self.store, data, copy.deepcopy(row))
                if parent in data.get("deleted_tasks", {}):
                    lifecycle.delete_rows_in_tx(data, rows, now, on_mutate=memory_integration.on_task_lifecycle_in_tx)
                    deleted_ids.extend(child["id"] for child in rows)
                else:
                    subagents.cascade_stop_subagents_in_tx(data, row, close=True)
                    rows = lifecycle.tree_rows(self.store, data, copy.deepcopy(row))
                    lifecycle._archive_tree(self.store, data, rows, event,
                        on_mutate=memory_integration.on_task_lifecycle_in_tx,
                        reason="parent_archived" if parent in data.get("archives", {}) else "orphan")
            return deleted_ids
        deleted_ids = self.store.transaction(reconcile)
        if deleted_ids:
            self._cleanup_deleted_tasks(deleted_ids)

    def _schedule_reconciliation(self, row):
        prior = row.get("reconciliation")
        if hasattr(self.tools, "reconcile") and (prior is None or prior.get("state") == "running"):
            row["reconciliation"] = {"state": "pending", "attempts": (prior or {}).get("attempts", 0), "requested_at": now()}

    def _reconcile_operation(self, selected: dict) -> None:
        """Query the original operation only; never dispatch the mutation again."""
        started = time.monotonic()
        flight = selected["in_flight"]
        operation_id = flight.get("operation_id")
        receipt = next((r for r in selected.get("receipts", []) if r["operation_id"] == operation_id), None)

        def claim(data):
            row = task(data, selected["id"])
            pending = row.get("reconciliation", {})
            if not ambiguous(row) or pending.get("state") != "pending" or row["control_revision"] != selected["control_revision"]:
                return False
            if pending.get("attempts", 0) >= 3 or budget.hard_run_limit(row) == "agent_time_limit" or receipt is None:
                row["reconciliation"] = {**pending, "state": "unknown", "reason": "agent_reconciliation_limit"}
                return False
            row["reconciliation"] = {"state": "running", "attempts": pending.get("attempts", 0) + 1, "operation_id": operation_id,
                                     "started_at": now()}
            self.store.reserve(data, row["id"], output_policy.storage_growth(row["limits"]["max_output_chars"]))
            event(row, "reconciliation_started", operation_id=operation_id)
            return True
        try:
            claimed = self.store.transaction(claim)
        except AgentError as exc:
            code = str(exc)
            self.store.transaction(lambda data: task(data, selected["id"])["reconciliation"].update(state="unknown", reason=code))
            return
        if not claimed:
            return

        self._active_run = {"task_id": selected["id"], "started": started,
                            "recorded_seconds": selected["elapsed_seconds"], "revision": selected["revision"]}

        def cancelled():
            current = self.store.read()
            row = task(current, selected["id"])
            return (not current["settings"]["enabled"] or row["control_revision"] != selected["control_revision"] or
                    row["reconciliation"]["state"] != "running" or
                    (budget.segment_is_hard(selected, "max_seconds") and
                     selected["elapsed_seconds"] + time.monotonic() - started >= selected["limits"]["max_seconds"]))
        result = {"outcome": "unknown", "reason": "agent_reconciliation_failed"}
        context_valid = False
        try:
            context = self._tool_context(selected, operation_id)
            context["operation"] = copy.deepcopy(receipt)
            result = sanitized(self._read_call(selected, "reconcile", lambda: self.tools.reconcile(operation_id, context=context, cancelled=cancelled)))
            if len(encoded(result)) > selected["limits"]["max_output_chars"] or result.get("outcome") not in {"executed", "not_executed", "unknown"}:
                raise AgentError("agent_invalid_reconciliation_response")
            if result["outcome"] != "unknown":
                proven = result.get("receipt") or {}
                if any(proven.get(key) != receipt[key] for key in ("operation_id", "tool", "arguments_sha256")) or not isinstance(proven.get("result"), dict):
                    raise AgentError("agent_invalid_reconciliation_response")
                if result["outcome"] == "not_executed" and proven["result"].get("not_executed") is not True:
                    raise AgentError("agent_invalid_reconciliation_response")
            # This check authorizes only future inference, never another write.
            # Preserve a proven receipt even if resource egress was revoked.
            try:
                self._validate_context(selected)
                context_valid = True
            except AgentError:
                pass
        except Exception:
            result = {"outcome": "unknown", "reason": "agent_reconciliation_failed"}

        def save(data):
            row = task(data, selected["id"])
            row["elapsed_seconds"] += time.monotonic() - started
            if row["control_revision"] != selected["control_revision"] or not data["settings"]["enabled"]:
                row["reconciliation"].update(state="unknown", reason="agent_reconciliation_interrupted")
                return
            row["reconciliation"].update(state=result["outcome"], checked_at=now())
            if result["outcome"] == "unknown":
                row["reconciliation"]["reason"] = str(result.get("reason", "No attributable operation evidence."))[:2000]
                event(row, "reconciliation_finished", outcome="unknown", operation_id=operation_id)
                return
            proven_result = result["receipt"]["result"]
            may_continue = (
                result["outcome"] == "executed" and context_valid
                and row["status"] == "interrupted" and not row.get("pending_control")
                and not row.get("pending_input") and not row.get("pending_approval")
                and flight.get("authorization") in {"approved", "automatic"}
                and flight.get("control_revision") == row["control_revision"]
                and flight.get("goal_revision") == row["goal"]["revision"]
                and flight.get("scope_revision") == row["scope_revision"]
                and flight.get("context_epoch") == row.get("context_epoch", 0)
                and flight.get("authority") == model_retry_authority(data["settings"])
                and not write_permission_revoked(row, data["settings"])
                and not budget.hard_run_limit(row)
            )
            verification.record(row, flight, "confirmed" if result["outcome"] == "executed" else "not_executed", now(), result=proven_result)
            reconciled_message = {"role": "tool", "tool_call_id": flight["call_id"], "content": encoded(proven_result).decode(), "context_epoch": flight.get("context_epoch", 0)}
            if isinstance(proven_result, dict) and (proven_result.get("is_error") or proven_result.get("error")):
                reconciled_message["is_error"] = True  # The recorded reply was a tool-reported error.
            row["messages"].append(reconciled_message)
            row["queue"] = [call for call in row["queue"] if call["operation_id"] != operation_id]
            row["in_flight"] = None
            close_queue(row, event, "agent_reconciliation_requires_review", keep_flight=False)
            was_cancelled = row["status"] == "cancelled" or row.get("pending_control", {}).get("kind") == "cancel"
            settle(row, event, data=data)
            row.update(status="cancelled" if was_cancelled else "queued" if may_continue else "paused", error=None, approved=None, pending_approval=None)
            row["outcome"].update(status="unverified", uncertainties=["Original operation reconciled; complete goal verification is still required."])
            event(row, "reconciliation_finished", outcome=result["outcome"], operation_id=operation_id, continued=may_continue)
        self.store.transaction(save)

    def _fail(
        self,
        task_id: str,
        code: str,
        *,
        elapsed: float = 0.0,
        read_cleanup_ok: bool | None = None,
        read_recovery: dict | None = None,
        error_details: str | None = None,
        diagnostics: dict | None = None,
    ) -> None:
        def fail(data):
            from .execution_policy import verified_read_classification
            row = task(data, task_id)
            row["elapsed_seconds"] += elapsed
            flight = row["in_flight"]
            workspace_root = (row.get("local_execution") or {}).get("workspace_root", "")
            is_verified_read = bool(
                flight
                and flight.get("dispatched")
                and verified_read_classification(flight, task_id=row["id"], workspace_root=workspace_root)
            )
            ambiguous_write = bool(
                flight
                and flight.get("effect") == "write"
                and flight.get("dispatched")
                and not is_verified_read
            )
            if ambiguous_write:
                is_hint_read_unknown = (
                    code == "agent_mcp_outcome_unknown_reported" and _flight_has_server_hint_read(flight)
                )
                if code not in budget.LIMIT_CODES and self._auto_acknowledge_allowed(row, data, flight, code=code):
                    repeated = self._prior_unknown_count(row, flight) >= 1
                    op_id = flight.get("operation_id")
                    verification.record(row, flight, "unknown", now(), flight=flight)
                    self._acknowledge_unknown_in_tx(
                        row,
                        resume=True,
                        error_details=error_details or code,
                        repeated_unknown=repeated,
                        error_code=code if is_hint_read_unknown else "agent_write_outcome_unknown",
                        hint=MCP_UNKNOWN_OUTCOME_HINT if is_hint_read_unknown else None,
                        keep_sibling_queue=is_hint_read_unknown,
                    )
                    self._record_auto_acknowledged_operation(row, op_id)
                    row.update(status="queued", error=None)
                    event(row, "interrupted", ambiguous_write=not is_hint_read_unknown, continued=True, auto_acknowledged=True)
                    return
                if (row.get("pending_control") or {}).get("kind") == "permissions":
                    row.pop("pending_control", None)
                verification.record(row, flight, "unknown", now(), flight=flight)
                row.update(status="interrupted", error="agent_write_outcome_unknown", approved=None, pending_approval=None)
                self._schedule_reconciliation(row)
                event(row, "interrupted", error=row["error"], ambiguous_write=True)
                subagents.on_subagent_settled_in_tx(data, row)
            else:
                if is_verified_read:
                    if read_cleanup_ok is None:
                        cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=code)
                    else:
                        cleanup_ok, recovery = read_cleanup_ok, read_recovery
                    if not cleanup_ok:
                        if (row.get("pending_control") or {}).get("kind") == "permissions":
                            row.pop("pending_control", None)
                        row.update(status="interrupted", error=code, approved=None, pending_approval=None)
                        event(
                            row,
                            "interrupted",
                            error=code,
                            ambiguous_write=False,
                            continued=False,
                            cleanup_confirmed=False,
                        )
                        subagents.on_subagent_settled_in_tx(data, row)
                        return
                    self._settle_interrupted_read(row, flight, code, recovery=recovery)
                row["in_flight"] = None
                if (row.get("pending_control") or {}).get("kind") == "permissions" and code not in {
                    "agent_owner_changed",
                    "agent_cancelled",
                    "agent_permission_denied",
                }:
                    row.pop("pending_control", None)
                if row.get("pending_control"):
                    settle(row, event, data=data)
                elif row["status"] in {"running", "queued"} and code in budget.LIMIT_CODES:
                    budget.stop_for_limit(row, code)
                elif row["status"] in {"running", "queued"}:
                    safe_diag = diagnostic_log.safe_diagnostics(diagnostics)
                    if row.get("connection_recovery"):
                        retry_policy.reset(row)
                    row.update(status="failed", error=code, approved=None)
                    event(
                        row,
                        "failed",
                        error=code,
                        **({"http_status": safe_diag["http_status"]} if "http_status" in safe_diag else {}),
                        **({"diagnostics": safe_diag} if safe_diag else {}),
                    )
                subagents.on_subagent_settled_in_tx(data, row)
        self.store.transaction(fail)

    def _operation_error(
        self,
        selected: dict,
        code: str,
        started: float,
        *,
        definitely_not_executed=False,
        read_connection=None,
        correction=None,
        details=None,
        diagnostics=None,
        error_text: str | None = None,
    ) -> None:
        try:
            self._handle_operation_error(
                selected,
                code,
                started,
                definitely_not_executed=definitely_not_executed,
                read_connection=read_connection,
                correction=None if code == "agent_owner_changed" else correction,
                details=details,
                diagnostics=diagnostics,
                error_text=error_text,
            )
        except AgentError as exc:
            if str(exc) not in {"agent_task_not_found", "agent_task_deleted"}:
                raise
            # A late operation from a retired process cannot resurrect a task
            # deleted after restart reconciliation cleared its read flight.

    def _handle_operation_error(
        self,
        selected: dict,
        code: str,
        started: float,
        *,
        definitely_not_executed=False,
        read_connection=None,
        correction=None,
        details=None,
        diagnostics=None,
        error_text: str | None = None,
    ) -> None:
        """Only proven no-side-effect failures may return to the correction loop."""
        if selected.get("verification"):
            def verification_failed(data):
                row = task(data, selected["id"])
                flight = row.get("in_flight") or {}
                row["in_flight"] = None
                row["elapsed_seconds"] += time.monotonic() - started
                if row.get("pending_control"):
                    settle(row, event, data=data)
                elif row["status"] == "running" and row["control_revision"] == selected["_run_control_revision"]:
                    pending = row.get("verification") or {}
                    if (code == "agent_invalid_verification_response" and flight.get("kind") == "verification_model"
                            and pending.get("format_retries", 0) < 1 and not budget.hard_run_limit(row)
                            and data["settings"]["enabled"]):
                        pending["format_retries"] = pending.get("format_retries", 0) + 1
                        row["status"] = "queued"
                        event(row, "verification_format_retry_scheduled", error=code, retry_count=pending["format_retries"])
                        return
                    verification.finish(row, error=code)
                    event(row, "verification_finished", outcome=row["outcome"]["status"], error=code)
                    event(row, "completed")
                    subagents.on_subagent_settled_in_tx(data, row)
            self.store.transaction(verification_failed)
            return
        # A full store fails the commit, not the model. Once older finished
        # tasks are archived, the step retries once from its recorded evidence.
        storage_freed = (code == "agent_storage_limit" and not selected["queue"]
                         and self._relieve_storage("commit_failed", exclude={selected["id"]}, force=True) > 0)
        if (code in RETRYABLE_MODEL_ERRORS or storage_freed) and not selected["queue"]:
            now_mono = time.monotonic()
            req_started = selected.get("_request_started")
            prep_started = selected.get("_preparation_started", started)
            prep_persist = float(selected.get("_prep_persistence_seconds", 0.0))
            claim_persist = float(selected.get("_claim_persistence_seconds", 0.0))
            sched_wait = float(selected.get("_scheduler_wait_seconds", 0.0))
            failed_timing = {
                "scheduler_wait_seconds": round(sched_wait, 4),
                "preparation_seconds": round(max(0.0, (req_started if req_started is not None else now_mono) - prep_started), 4),
                "preparation_persistence_seconds": round(prep_persist, 4),
                "model_call_seconds": round(max(0.0, now_mono - req_started) if req_started is not None else 0.0, 4),
                "store_persistence_seconds": round(claim_persist + prep_persist, 4),
            }
            safe_diag = diagnostic_log.safe_diagnostics(diagnostics)
            req_id = selected.get("_request_id")
            attempt = selected.get("_attempt", int(selected.get("model_retry_count", 0)) + 1)
            diagnostic_log.failure(
                "model_inference",
                code,
                task_id=selected["id"],
                step=selected.get("_step") or selected.get("steps"),
                request_id=req_id,
                attempt=attempt,
                elapsed_seconds=now_mono - started,
                model_call_seconds=failed_timing["model_call_seconds"],
                diagnostics=safe_diag,
            )

            def retry_model(data):
                row = task(data, selected["id"])
                flight = row.get("in_flight") or {}
                if flight.get("kind") != "model" or not flight.get("dispatched") or row["queue"] or row["pending_approval"] or row["approved"]:
                    return False
                row["elapsed_seconds"] += time.monotonic() - started
                row["in_flight"] = None
                if row.get("pending_control") and row["pending_control"].get("kind") != "permissions":
                    settle(row, event, data=data)
                    return True
                if row["status"] != "running" or not data["settings"]["enabled"]:
                    row.pop("pending_control", None)
                    return True  # Pause/cancel/disable wins over late failure.
                authority = model_retry_authority(data["settings"])
                network = code in retry_policy.NETWORK_ERRORS
                maximum = retry_policy.limit(code, safe_diag)
                count = row.get("model_retry_count", 0)
                run_limit = budget.hard_run_limit(row)
                error = ("agent_permission_denied" if authority != selected.get("_model_retry_authority") else
                         run_limit if run_limit else
                         "agent_model_unavailable" if code == "agent_model_starting" and count >= maximum else
                         code if not network and maximum is not None and count >= maximum else None)
                row.pop("pending_control", None)
                extra_event = {
                    "step": row["steps"],
                    "attempt": attempt,
                    "timing": failed_timing,
                    **({"request_id": req_id} if req_id else {}),
                    **({"diagnostics": safe_diag} if safe_diag else {}),
                }
                if error in budget.LIMIT_CODES:
                    budget.stop_for_limit(row, error, **extra_event)
                    subagents.on_subagent_settled_in_tx(data, row)
                    return True
                if error:
                    if code == "agent_model_starting" and error == "agent_model_unavailable":
                        row.pop("connection_recovery", None)
                    row.update(status="failed", error=error)
                    event(row, "failed", error=error, **extra_event)
                    subagents.on_subagent_settled_in_tx(data, row)
                    return True
                waiting_for_network = network and maximum is None
                if network:
                    recovery = {"state": "retrying", "retry_count": count, "max_retries": maximum,
                                "error_code": code, "diagnostics": safe_diag,
                                **({"waiting_for_network": True} if waiting_for_network else {})}
                    row["connection_recovery"] = recovery
                    if maximum is not None and count >= maximum:
                        recovery["state"] = "disconnected"
                        recovery.pop("waiting_for_network", None)
                        row.update(status="paused", error=None)
                        event(row, "connection_paused", error=code, retry_count=count, **extra_event)
                        subagents.on_subagent_settled_in_tx(data, row)
                        return True
                    wait = retry_policy.delay(count + 1, safe_diag, code)
                    remaining = budget.run_seconds_left(row, budget.time_remaining(row))
                    if wait >= remaining:
                        # Honor long Retry-After without exceeding time budgets or retrying early.
                        recovery["state"] = "disconnected"
                        recovery["reason"] = "retry_wait_exceeds_budget"
                        recovery.pop("waiting_for_network", None)
                        row.update(status="paused", error=None)
                        event(row, "connection_paused", error=code, retry_count=count, **extra_event)
                        subagents.on_subagent_settled_in_tx(data, row)
                        return True
                    recovery.update(retry_count=count + 1, wait_started_at=retry_policy.now(),
                                    next_retry_at=retry_policy.now() + wait)
                row.update(status="queued", error=None, model_retry_count=count + 1, model_retry_authority=authority)
                event(row, "model_retry_scheduled", error=code, retry_count=count + 1,
                      **({"waiting_for_network": True} if waiting_for_network else {}),
                      **extra_event)
                return True
            if self.store.transaction(retry_model):
                return
        elif not selected["queue"]:
            diagnostic_log.failure(
                "model_inference",
                code,
                task_id=selected["id"],
                step=selected.get("_step") or selected.get("steps"),
                request_id=selected.get("_request_id"),
                attempt=selected.get("_attempt"),
                elapsed_seconds=time.monotonic() - started,
                diagnostics=diagnostic_log.safe_diagnostics(diagnostics),
            )
        from .execution_policy import verified_read_classification
        current = task(self.store.read(), selected["id"])
        flight = current.get("in_flight") or {}
        queued_call = selected["queue"][0] if selected["queue"] else {}
        is_verified_read = bool(
            selected["queue"]
            and verified_read_classification(
                flight,
                task_id=current["id"],
                workspace_root=(current.get("local_execution") or {}).get("workspace_root", ""),
                operation_id=queued_call.get("operation_id"),
                tool=queued_call.get("name"),
                arguments=queued_call.get("arguments"),
            )
        )
        read_cleanup_ok = True
        read_recovery = None
        if is_verified_read and flight.get("dispatched"):
            if code == "agent_local_operation_unconfirmed":
                read_cleanup_ok = False
            else:
                read_cleanup_ok, read_recovery = self._recover_read_operation_details(current, flight, error=code)
        elif flight.get("effect") == "read" and flight.get("dispatched"):
            read_cleanup_ok, read_recovery = self._recover_read_operation_details(current, flight, error=code)
        if selected["queue"] and (
            definitely_not_executed
            or (selected["queue"][0]["effect"] == "read" and read_cleanup_ok)
            or (is_verified_read and read_cleanup_ok)
            or not flight.get("dispatched")
        ):
            call = selected["queue"][0]
            recovery_outcome = (read_recovery or {}).get("ledger_outcome")
            if recovery_outcome == "confirmed" and isinstance((read_recovery or {}).get("settled_result"), dict):
                result = sanitized(copy.deepcopy(read_recovery["settled_result"]))
                record_status = "confirmed"
            else:
                not_executed = (
                    (call["effect"] == "write" and not is_verified_read)
                    or (is_verified_read and (definitely_not_executed or recovery_outcome == "not_executed"))
                    or not flight.get("dispatched")
                )
                result = {"error": code, "not_executed": not_executed, "no_mutation": True}
                if isinstance(correction, str) and correction:
                    result["correction"] = sanitized(correction[:1600])
                if isinstance(details, dict) and details:
                    result["details"] = sanitized(copy.deepcopy(details))
                record_status = "not_executed" if result["not_executed"] else "failed"
            if code == "agent_ripgrep_unavailable_use_file_search":
                result["correction"] = (
                    "ripgrep (rg) is not installed in this node's command environment. Nothing was executed. "
                    "Use the built-in ryn_file_search with a path and literal query, or ryn_file_list to find files. "
                    "Search alternative terms in separate calls; do not repeat the unavailable rg command."
                )
            if call["name"] == "ryn_request_input" and code == "agent_invalid_control_proposal":
                result["correction"] = (
                    "Use kind=information/choice with text/choice fields for model names and other facts. "
                    "Use kind=credential_ref with ONLY credential_ref fields for credentials. "
                    "The secure form already collects the endpoint and credential. "
                    "Do not mix these field types or ask for secrets in chat."
                )
            if call["name"] == "ryn_update_plan" and code in {"agent_goal_revision_conflict", "agent_invalid_control_proposal", "agent_plan_revision_conflict"}:
                result.update(expected_goal_revision=current["goal"]["revision"],
                              expected_plan_revision=current["plan"]["revision"],
                              allowed_criterion_ids=[item["id"] for item in current["criteria"]])
            if call["effect"] == "read" and read_connection:
                from .diagnostics import safe_label
                result.update({key: safe_label(read_connection.get(key)) for key in ("connection_id", "connection_name")})

            step_elapsed = max(0.0, time.monotonic() - started)
            was_not_executed = bool(result.get("not_executed"))
            error_timing = {
                "scheduler_wait_seconds": round(float(selected.get("_scheduler_wait_seconds", 0.0)), 4),
                "preparation_seconds": round(step_elapsed if was_not_executed else 0.0, 4),
                "tool_execution_seconds": round(0.0 if was_not_executed else step_elapsed, 4),
                "store_persistence_seconds": round(float(selected.get("_claim_persistence_seconds", 0.0)), 4),
            }
            can_continue_cleanly = False

            def reject(data):
                nonlocal can_continue_cleanly
                row = task(data, selected["id"])
                if (code == "agent_unattended_approval_blocked" and row.get("automation_run_id")
                        and row.get("automation_run_id") == selected.get("automation_run_id")):
                    blocked = row.setdefault("unattended_blocked", [])
                    if len(blocked) < 20:
                        blocked.append({"tool": call["name"], "reason": details["reason"]})
                row.update(in_flight=None, approved=None, pending_approval=None)
                row["elapsed_seconds"] += time.monotonic() - started
                consecutive_same_error = 0
                if call["effect"] == "read" and record_status == "failed":
                    self._note_last_read_attempt(row, call, result)
                if record_status != "confirmed":
                    for prev_event in reversed(row.get("events", [])):
                        if prev_event.get("type") != "tool_result":
                            continue
                        if prev_event.get("context_epoch", 0) != call.get("context_epoch", 0):
                            break
                        prev_res = prev_event.get("result")
                        prev_err = prev_res.get("error") if isinstance(prev_res, dict) else None
                        if prev_event.get("tool") == call["name"] and prev_err == code and code is not None:
                            consecutive_same_error += 1
                        else:
                            break
                    if consecutive_same_error >= 1 and "correction" not in result:
                        result["correction"] = (
                            "This tool failed with the same error on the previous attempt. "
                            "Adjust the arguments, switch to an alternative tool, or report the limitation "
                            "instead of repeating the same failing call."
                        )
                row["messages"].append({"role": "tool", "tool_call_id": call["call_id"], "content": encoded(result).decode(), "context_epoch": call.get("context_epoch", 0)})
                verification.record(row, call, record_status, now(), result=result)
                row["queue"].pop(0)
                event(row, "tool_result", tool=call["name"], call_id=call["call_id"], operation_id=call["operation_id"],
                      step=row["steps"], result=result, internal=call["name"] in INTERNAL_NAMES,
                      context_epoch=call.get("context_epoch", 0), timing=error_timing)
                if row["status"] == "running":
                    if code in budget.LIMIT_CODES:
                        budget.stop_for_limit(row, code)
                        subagents.on_subagent_settled_in_tx(data, row)
                    else:
                        row["status"] = "queued"
                settle(row, event, data=data)
                budget_state = budget.public(row)
                b_limits = budget_state["limits"]
                b_life = budget_state["lifetime"]
                within_budget = (
                    budget.time_remaining(row) > 0
                    and b_life["charged_tokens"] < b_limits["max_total_tokens"]
                    and (bool(row.get("queue")) or b_life["charged_model_calls"] < b_limits["max_total_model_calls"])
                    and (not row.get("queue") or b_life["charged_tool_calls"] < b_limits["max_total_tool_calls"])
                )
                can_continue_cleanly = bool(
                    (result.get("no_mutation") is True or record_status == "confirmed")
                    and consecutive_same_error == 0
                    and row["status"] == "queued"
                    and data["settings"].get("enabled")
                    and row["control_revision"] == selected["_run_control_revision"]
                    and not row.get("pending_control")
                    and not row.get("pending_approval")
                    and not row.get("approved")
                    and not row.get("pending_input")
                    and budget.run_may_continue(row)
                    and within_budget
                )
            self.store.transaction(reject)
            if can_continue_cleanly:
                self._step.clean = True
        else:
            fail_details = error_text
            if not fail_details:
                if isinstance(details, str) and details:
                    fail_details = f"{code}: {details}"
                elif isinstance(details, dict) and details:
                    fail_details = f"{code}: {encoded(sanitized(details)).decode()}"
                elif isinstance(diagnostics, dict) and diagnostics:
                    fail_details = f"{code}: {encoded(sanitized(diagnostics)).decode()}"
                else:
                    fail_details = code
            self._fail(
                selected["id"],
                code,
                elapsed=time.monotonic() - started,
                read_cleanup_ok=read_cleanup_ok,
                read_recovery=read_recovery,
                error_details=fail_details,
                diagnostics=diagnostics,
            )

    def run_continuously(self, cancelled=None) -> WorkerRunResult:
        """Worker entry point: consecutive steps without a scheduler interval between them.

        run_once stays exactly one step. Only a step that ended normally continues
        at once; a failed step (including a scheduled model retry) returns to the
        worker's ordinary interval. Bounded so worker slots rotate between tasks.
        """
        deadline = time.monotonic() + CONTINUE_SECONDS
        activity = False
        while True:
            if cancelled is not None and cancelled.is_set():
                return WorkerRunResult(activity=activity)
            result = self.run_once()
            if not result.activity:
                return WorkerRunResult(activity=activity)
            activity = True
            if not getattr(self._step, "clean", False) or time.monotonic() >= deadline:
                return result

    def run_once(self) -> WorkerRunResult:
        self._step.clean = False
        self._step.stream = None
        if not self._run_slots.acquire(blocking=False):
            return WorkerRunResult()
        if not self._run_lock.acquire(blocking=False):
            self._run_slots.release()
            return WorkerRunResult()
        gate_owned = True
        selected = None
        scope_cm = None
        started = time.monotonic()
        try:
            # Idle ticks only look: copying every task's history three times a
            # second per worker cost more CPU than the work it was checking for.
            peek = self.store.inspect(lambda data: {
                "enabled": data["settings"]["enabled"],
                "computer_activity": {key: {"status": row["status"],
                    "pending_control": bool(row.get("pending_control")),
                    "kind": (row.get("in_flight") or {}).get("kind")}
                    for key, row in data["tasks"].items()},
                "queued": any(row["status"] == "queued" and retry_policy.ready(row) and subagents.wait_ready(data, row) for row in data["tasks"].values()),
                "waiting": any(row["status"] == "waiting_approval" and row.get("queue") for row in data["tasks"].values()),
                "unresolved": next((row["id"] for row in data["tasks"].values()
                                    if ambiguous(row) and row.get("reconciliation", {}).get("state") == "pending"), None)})
            native_computer = getattr(self.tools, "native_computer", None)
            overlay = getattr(native_computer, "overlay", None)
            if overlay is not None:
                overlay.sync(peek["computer_activity"] if peek["enabled"] else {})
            if peek["enabled"]:
                if peek["waiting"]:
                    self._refresh_waiting_reads(self._waiting_policy_snapshot())
                    # A released dialog is claimable in this same tick.
                    peek["queued"] = self.store.inspect(lambda data: any(row["status"] == "queued" and retry_policy.ready(row) and subagents.wait_ready(data, row) for row in data["tasks"].values()))
                unresolved = task(self.store.read(), peek["unresolved"]) if peek["unresolved"] else None
                if unresolved:
                    self._run_lock.release()
                    gate_owned = False
                    self._reconcile_operation(unresolved)
                    return WorkerRunResult(activity=True)
            def claim(data):
                if not data["settings"]["enabled"]:
                    return None
                rows = sorted(data["tasks"].values(), key=lambda row: row["updated_at"])
                for row in rows:
                    if (row.get("fork") or {}).get("awaiting_instruction"):
                        if row["status"] == "queued":
                            row["status"] = "paused"
                        continue
                    if row["status"] != "queued" or not retry_policy.ready(row) or not subagents.wait_ready(data, row):
                        continue
                    if row.get("subagent_wait") and (not row["queue"] or row["queue"][0]["call_id"] != row["subagent_wait"]["call_id"]):
                        row.pop("subagent_wait")
                    retry_policy.charge_wait(row)
                    if budget.time_remaining(row) <= 0:
                        budget.stop_for_limit(row, "agent_time_budget_exhausted")
                        subagents.on_subagent_settled_in_tx(data, row)
                        continue
                    if row.get("model_retry_count", 0) and row.get("model_retry_authority") != model_retry_authority(data["settings"]):
                        row.update(status="failed", error="agent_permission_denied")
                        event(row, "failed", error=row["error"])
                        subagents.on_subagent_settled_in_tx(data, row)
                        continue
                    boundary = budget.segment_boundary(row)
                    if boundary and not budget.segment_is_hard(row, boundary):
                        # Default per-run limits only size a segment. Checkpointing
                        # here, at a claim, also lets older queued tasks run first.
                        if budget.continue_segment(row, boundary):
                            budget.stop_for_no_progress(row)
                            subagents.on_subagent_settled_in_tx(data, row)
                            continue
                        boundary = budget.segment_boundary(row)
                    if boundary:
                        code = "agent_time_limit" if boundary == "max_seconds" else "agent_step_limit"
                        if row.get("verification"):
                            verification.finish(row, error=code)
                            event(row, "verification_finished", outcome=row["outcome"]["status"], error=code)
                            event(row, "completed")
                            subagents.on_subagent_settled_in_tx(data, row)
                            continue
                        budget.stop_for_limit(row, code)
                        subagents.on_subagent_settled_in_tx(data, row)
                        continue
                    row["status"] = "running"
                    row["in_flight"] = {"kind": "verification" if row.get("verification") else "tool" if row["queue"] else "model", "dispatched": False,
                                        "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"], "scope_revision": row["scope_revision"]}
                    queued_at = self._queued_since.pop(row["id"], None)
                    scheduler_wait_seconds = max(0.0, claim_started - queued_at) if queued_at is not None else 0.0
                    return {**row, "_model_retry_authority": model_retry_authority(data["settings"]), "_run_control_revision": row["control_revision"],
                            "_run_lifetime_remaining": budget.time_remaining(row),
                            "_scheduler_wait_seconds": scheduler_wait_seconds}
                return None
            # Avoid rewriting encrypted history every idle worker tick.
            if not peek["enabled"] or not peek["queued"]:
                if peek["enabled"]:
                    self._schedule_idle_storage_relief()
                return WorkerRunResult()
            claim_started = time.monotonic()
            selected = self.store.transaction(claim)
            if selected is None:
                return WorkerRunResult()
            if overlay is not None:
                peek["computer_activity"][selected["id"]] = {
                    "status": selected.get("status"),
                    "pending_control": bool(selected.get("pending_control")),
                    "kind": (selected.get("in_flight") or {}).get("kind"),
                }
                overlay.sync(peek["computer_activity"])
            selected["_claim_persistence_seconds"] = max(0.0, time.monotonic() - claim_started)
            self._run_lock.release()
            gate_owned = False
            scope_cm = diagnostic_log.context_scope(task_id=selected["id"], step=selected.get("steps", 0))
            scope_cm.__enter__()
            started = time.monotonic()
            selected["_run_started"] = started
            self._active_run = {"task_id": selected["id"], "started": started,
                                "recorded_seconds": selected["elapsed_seconds"], "revision": selected["revision"]}

            def cancelled():
                current = self.store.read_control(selected["id"])
                row = task(current, selected["id"])
                if selected["_run_lifetime_remaining"] <= time.monotonic() - started:
                    cancelled.reason = "agent_time_budget_exhausted"
                    return True
                # Business tools call this again under their mutation lock.
                # Consuming one-shot approval does not immunize a queued write
                # against a subsequent owner permission revocation.
                # A default segment boundary never cancels work in flight; the
                # next claim checkpoints it. Only an owner/legacy run limit cuts.
                reason = ("agent_permission_denied" if write_permission_revoked(row, current["settings"]) else
                          "agent_owner_changed" if row["control_revision"] != selected["_run_control_revision"] else
                          "agent_cancelled" if not current["settings"]["enabled"] or row["status"] != "running" else
                          "agent_time_limit" if (budget.segment_is_hard(selected, "max_seconds") and
                                                 selected["elapsed_seconds"] + time.monotonic() - started >= selected["limits"]["max_seconds"]) else None)
                if reason is None:
                    try:
                        self._validate_context(row, cheap_only=True, control_state=current)
                    except AgentError as exc:
                        reason = str(exc)
                cancelled.reason = reason
                return reason is not None

            if selected.get("verification"):
                self._verify(selected, cancelled, started)
            elif selected["queue"]:
                self._tool(selected, cancelled, started)
            else:
                self._model(selected, cancelled, started)
            self._step.clean = True
            return WorkerRunResult(activity=True)
        except AgentToolRejected as exc:
            if selected:
                code = str(exc)
                if code == "agent_cancelled":
                    code = getattr(cancelled, "reason", None) or code
                self._operation_error(
                    selected,
                    code,
                    started,
                    definitely_not_executed=True,
                    correction=getattr(exc, "correction", None),
                    details=getattr(exc, "details", None),
                    diagnostics=getattr(exc, "diagnostics", None),
                )
            return WorkerRunResult(activity=bool(selected))
        except AgentError as exc:
            if selected:
                code = cancellation_reason(cancelled) if str(exc) == "agent_cancelled" else str(exc)
                if code == "agent_inference_failed" and exc.__context__ is not None:
                    self._record_runtime_fault(selected, exc.__context__)
                err_text = None
                if selected["queue"] and exc.__cause__ is not None:
                    cause_msg = str(exc.__cause__).strip()
                    err_text = f"{code}: {type(exc.__cause__).__name__}: {cause_msg}" if cause_msg else f"{code}: {type(exc.__cause__).__name__}"
                self._operation_error(
                    selected,
                    code,
                    started,
                    read_connection=exc.connection if isinstance(exc, AgentReadFailure) else None,
                    details=getattr(exc, "details", None),
                    diagnostics=getattr(exc, "diagnostics", None),
                    error_text=err_text,
                )
            return WorkerRunResult(activity=bool(selected))
        except ValueError as exc:
            # Only explicitly enumerated boundary codes may reach the owner.
            # Arbitrary provider exceptions can contain URLs or credentials.
            safe = {"agent_context_limit", "agent_inference_failed", "agent_invalid_model_response",
                    "agent_model_busy", "agent_model_output_limit", "agent_model_unavailable", "agent_model_starting",
                    "agent_model_tools_unsupported", "agent_output_truncated", "agent_tool_call_limit", "agent_unknown_tool",
                    "agent_approval_preview_changed", "agent_friend_inactive", "agent_friend_model_not_shareable",
                    "agent_model_not_shareable", "agent_model_uses_personal_space_access",
                    "agent_permission_revision_conflict", "agent_personal_space_required", "agent_tool_arguments_invalid"}
            if selected:
                code = str(exc) if str(exc) in safe else "agent_operation_failed"
                if code == "agent_operation_failed":
                    self._record_runtime_fault(selected, exc)
                if str(exc) == "agent_cancelled":
                    code = getattr(cancelled, "reason", None) or ("agent_time_limit" if (budget.segment_is_hard(selected, "max_seconds") and selected["elapsed_seconds"] + time.monotonic() - started >= selected["limits"]["max_seconds"]) else "agent_cancelled")
                err_text = f"{type(exc).__name__}: {exc}" if selected["queue"] and str(exc).strip() else None
                self._operation_error(selected, code, started, error_text=err_text)
            return WorkerRunResult(activity=bool(selected))
        except Exception as exc:
            if selected:
                self._record_runtime_fault(selected, exc)
                err_text = f"{type(exc).__name__}: {exc}" if selected["queue"] and str(exc).strip() else None
                self._operation_error(selected, "agent_operation_failed", started, error_text=err_text)
            return WorkerRunResult(activity=bool(selected))
        finally:
            if selected is not None:
                self._clear_stream(selected["id"], self._step.stream)
                try:
                    post = self.store.read_control(selected["id"]).get("tasks", {}).get(selected["id"]) or {}
                    if post.get("status") == "queued":
                        self._queued_since[selected["id"]] = time.monotonic()
                    else:
                        self._queued_since.pop(selected["id"], None)
                    if overlay is not None and post:
                        peek["computer_activity"][selected["id"]] = {
                            "status": post.get("status"),
                            "pending_control": bool(post.get("pending_control")),
                            "kind": (post.get("in_flight") or {}).get("kind"),
                        }
                        overlay.sync(peek["computer_activity"])
                except Exception:
                    pass
            if scope_cm is not None:
                scope_cm.__exit__(None, None, None)
            self._active_run = None
            if gate_owned:
                self._run_lock.release()
            self._run_slots.release()

    def _record_runtime_fault(self, selected, exc):
        # Record code locations, never exception text, locals, paths, URLs or
        # upstream payloads, so an unexpected error can actually be diagnosed.
        frames, tb = [], exc.__traceback__
        while tb:
            code = tb.tb_frame.f_code
            filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
            frames.append({"file": filename, "function": code.co_name, "line": tb.tb_lineno})
            tb = tb.tb_next
        self.store.transaction(lambda data: event(task(data, selected["id"]), "runtime_fault",
            exception_type=type(exc).__name__, frames=frames[-6:]))

    def _change_review(self, row: dict):
        """Net file changes this task made through file tools, or None when there is nothing to review."""
        tool = getattr(self.tools, "change_review", None)
        root = (row.get("local_execution") or {}).get("workspace_root", "")
        goal = row["goal"]["revision"]
        if tool is None or not root or not any(
                r.get("effect") == "write" and r.get("status") == "confirmed" and r.get("goal_revision") == goal
                and str(r.get("tool", "")).startswith("ryn_file_") for r in row.get("receipts", [])):
            return None
        try:
            review = tool(row["id"], root)
        except Exception:
            return None
        if not isinstance(review, dict) or not review.get("files"):
            return None
        # A new plain-text deliverable has no original code/tests to compare.
        # Its content and current bytes are still checked by normal goal verification.
        if all(entry.get("status") == "created" and str(entry.get("path", "")).lower().endswith((".txt", ".md"))
               for entry in review["files"]):
            return None
        # Rendered appearance is evidence only for the exact bytes that were rendered.
        inspected = {receipt["result"].get("sha256") for receipt in row.get("receipts", [])
                     if receipt.get("tool") == "ryn_render_inspect" and receipt.get("status") == "confirmed"
                     and isinstance(receipt.get("result"), dict)}
        for entry in review["files"]:
            if entry.get("status") != "deleted" and str(entry.get("path", "")).lower().endswith(VISUAL_SUFFIXES):
                entry["render_inspected"] = entry.get("sha256") in inspected
        return review

    def _verify(self, selected: dict, cancelled, started: float) -> None:
        """One read-only check per worker tick, then a separate no-tool assessment."""
        self._reserve_output(selected)
        self._validate_context(selected)
        pending = selected["verification"]
        if cancelled():
            raise AgentError(cancelled.reason)
        if pending["phase"] == "mechanical":
            check = pending["checks"][pending["index"]]
            receipt = next(r for r in selected["receipts"] if r["id"] == check["receipt_id"])
            if isinstance(receipt.get("result"), dict) and receipt["result"].get("result_ref"):
                original, _ = results.read(self.store, selected, receipt["result"]["result_ref"], provider=True)
                receipt = {**receipt, "result": original}
            criterion = next(c for c in selected["criteria"] if c["id"] == check["criterion_id"])
            verify_context = self._tool_context(selected, receipt["operation_id"])
            dependencies = verification.observation_transitions.context(selected, criterion)
            for dependency in dependencies:
                item = dependency["receipt"]
                if isinstance(item.get("result"), dict) and item["result"].get("result_ref"):
                    item["result"], _ = results.read(self.store, selected, item["result"]["result_ref"], provider=True)
            if dependencies:
                verify_context["observation_dependencies"] = dependencies
            if criterion["verifier"] is None:
                result = {"status": "unknown", "evidence": {"reason": "No registered business verifier."}, "checked_at": now()}
            else:
                result = sanitized(self._read_call(selected, "verify", lambda: self.tools.verify(copy.deepcopy(receipt), [copy.deepcopy(criterion)],
                    context=verify_context, cancelled=cancelled)))
            if not isinstance(result, dict) or result.get("status") not in {"passed", "failed", "unknown"} or not isinstance(result.get("evidence"), dict):
                raise AgentError("agent_invalid_verification_response")
            if len(encoded(result)) > selected["limits"]["max_output_chars"]:
                raise AgentError("agent_output_limit")
            if cancelled():
                raise AgentError(cancelled.reason)

            def save_check(data):
                row = task(data, selected["id"])
                row["in_flight"] = None
                row["elapsed_seconds"] += time.monotonic() - started
                if row["status"] != "running" or row["control_revision"] != selected["_run_control_revision"]:
                    settle(row, event, data=data)
                    return
                evidence_id = "evidence_" + uuid.uuid4().hex
                row.setdefault("verification_evidence", []).append({"id": evidence_id, "criterion_id": criterion["id"], "receipt_id": receipt["id"],
                    "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"], "kind": "mechanical", **result})
                current = next(c for c in row["criteria"] if c["id"] == criterion["id"])
                current.update(status=result["status"], evidence_refs=[evidence_id])
                row["verification"]["index"] += 1
                if row["verification"]["index"] == len(row["verification"]["checks"]):
                    row["verification"]["phase"] = "semantic"
                row["status"] = "queued"
                event(row, "criterion_checked", criterion_id=criterion["id"], status=result["status"], evidence_id=evidence_id)
            self.store.transaction(save_check)
            return

        if len({ref for criterion in selected["criteria"] if criterion.get("required") for ref in criterion["evidence_refs"]}) > 128:
            raise AgentError("agent_verification_evidence_limit")
        checker_state = copy.deepcopy(selected)
        change_review = self._change_review(selected)
        if change_review:
            checker_state["change_review"] = change_review
        for receipt in checker_state.get("receipts", []):
            if (receipt["goal_revision"] == selected["goal"]["revision"] and receipt["context_epoch"] == selected.get("context_epoch", 0)
                    and isinstance(receipt.get("result"), dict) and receipt["result"].get("result_ref")):
                receipt["result"], _ = results.read(self.store, selected, receipt["result"]["result_ref"], provider=True)
        # Build and pre-check with the same effective capacity as dispatch:
        # optional metadata is dropped first; owner requirements, the answer,
        # criteria and evidence are never cut, so an unfittable core is an
        # explicit capacity error before any reservation or model call.
        capacity, _ = self._effective_capacity(selected)
        messages = verification.checker_messages(checker_state, context_limit=capacity)
        size, capacity = self._effective_request_fit(selected, output_policy.analyze_request(messages, []))
        if size > capacity:
            raise AgentError("agent_verification_context_limit")

        def mark(data):
            row = task(data, selected["id"])
            if row["status"] != "running" or row["control_revision"] != selected["_run_control_revision"]:
                row["in_flight"] = None
                row["elapsed_seconds"] += time.monotonic() - started
                settle(row, event, data=data)
                return False
            if row["steps"] >= row["limits"]["max_steps"] and budget.segment_is_hard(row, "max_steps"):
                raise AgentError("agent_step_limit")
            row["steps"] += 1
            row["in_flight"].update(kind="verification_model", dispatched=True)
            event(row, "verification_model_started", step=row["steps"])
            return True
        if not self.store.transaction(mark):
            return
        try:
            response = self._complete(selected, messages, [], cancelled, lambda _metadata: None)
        except AgentModelResponseError as exc:
            # The bridge can reject an empty, length-exhausted response before
            # JSON parsing. It belongs to the same ONE bounded format-repair
            # attempt as truncated JSON, not a separate retry allowance. The
            # meter already persisted safe stop/usage metadata and charged it.
            # Filters, cancellations and unknown failures retain their policy.
            if str(exc) == "agent_invalid_model_response" and exc.finish_reason == "length":
                raise AgentError("agent_invalid_verification_response") from None
            raise
        if cancelled():
            raise AgentError(cancelled.reason)
        if not isinstance(response, dict) or not isinstance(response.get("message"), dict):
            raise AgentError("agent_invalid_verification_response")
        if len(encoded(response)) > output_policy.MAX_RAW_RESPONSE_BYTES:
            raise AgentError("agent_model_output_limit")
        if len(encoded(output_policy.visible_message(response["message"]))) > selected["limits"]["max_output_chars"]:
            raise AgentError("agent_output_limit")
        try:
            assessment = sanitized(verification.parse_assessment(response, selected))
        except AgentError:
            def save_invalid(data):
                row = task(data, selected["id"])
                content = (response.get("message") or {}).get("content", "") if isinstance(response, dict) else ""
                content = content if isinstance(content, str) else ""
                row.setdefault("verification_rejected_responses", []).append({"goal_revision": selected["goal"]["revision"],
                    "context_epoch": selected.get("context_epoch", 0), "at": now(), "content": sanitized(content),
                    "response_sha256": digest(response), "usage": sanitized(response.get("usage")),
                    "metadata": sanitized(response.get("metadata")), "finish_reason": response.get("finish_reason")})
                event(row, "verification_response_rejected", error="agent_invalid_verification_response", response_sha256=digest(response))
            self.store.transaction(save_invalid)
            raise

        def finish_check(data):
            row = task(data, selected["id"])
            row["in_flight"] = None
            row["elapsed_seconds"] += time.monotonic() - started
            if row["status"] != "running" or row["control_revision"] != selected["_run_control_revision"]:
                settle(row, event, data=data)
                return
            if budget.hard_run_limit(row) == "agent_time_limit":
                verification.finish(row, error="agent_time_limit")
            else:
                if assessment.get("reported_status"):
                    event(row, "verification_assessment_normalized", reported_status=assessment["reported_status"],
                          status=assessment["status"], reason=assessment["normalization_reason"])
                verification.finish(row, assessment=assessment)
            event(row, "verification_finished", outcome=row["outcome"]["status"])
            if verification.repair(row, assessment):
                event(row, "verification_retry_scheduled", repair_count=row["verification_cycle"]["repairs"], goal_revision=row["goal"]["revision"])
            else:
                event(row, "completed")
                subagents.on_subagent_settled_in_tx(data, row)
        self.store.transaction(finish_check)
