"""Host-authorized read overlap with durable receipts and explicit publication.

Workers never change model history, its in-flight marker, or the pending queue.
The host must accept the final assistant before publish, and remove the consumed
queue prefix itself. Abort joins every admitted worker, including on mismatch.
"""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid

from jsonschema import ValidationError, validate

from . import budget, diagnostic_log, output_policy, results, verification
from .execution_policy import classify_operation, decide
from .prompt import INTERNAL_NAMES
from .protocol import digest, event, model_retry_authority, now, sanitized, task
from .store import AgentError, AgentToolRejected, encoded
from .stream_tools import StreamToolCoordinator
from .tool_dispatch import MAX_PARALLEL_TOOLS
from .tool_router import assert_schema_unchanged, normalize_tool_calls


def _stamp(row):
    return {"task_id": row["id"], "model_id": row["model_id"],
            "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"],
            "control_revision": row.get("_run_control_revision", row["control_revision"]),
            "context_epoch": row.get("context_epoch", 0),
            "model_destination": copy.deepcopy(row.get("model_destination")),
            "local_execution": copy.deepcopy(row.get("local_execution"))}


def _same(row, stamp):
    return _stamp(row) == stamp


def _safe_error(exc):
    code = str(exc)
    return code if isinstance(exc, AgentError) and re.fullmatch(r"agent_[a-z0-9_]{1,100}", code) else "agent_operation_failed"


def _tool_message(record, *, server_note=None):
    result = record["result"]
    shown = {**result, "server_note": server_note} if server_note and isinstance(result, dict) else result
    message = {"role": "tool", "tool_call_id": record["call"]["call_id"],
               "content": encoded(shown).decode(),
               "context_epoch": record["stamp"]["context_epoch"]}
    if record.get("origin"):
        message["resource_origin"] = copy.deepcopy(record["origin"])
    return message


def _apply_observed(row, record):
    bound = record["stamp"].get("local_execution") or {}
    for path, value in record.get("deferred", {}).items():
        observed = row.setdefault("observed_files", {})
        if value is None:
            observed.pop(path, None)
        else:
            observed[path] = {"sha256": value, "observed_at": now(), "scope_revision": row["scope_revision"],
                              "workspace_root": bound.get("workspace_root", ""),
                              "project_generation": bound.get("project_generation")}


def _publish(row, records, *, recover=False):
    if not records or row.get("status") != "running" or row.get("pending_control"):
        return False
    if any(record.get("persistence_error") or not _same(row, record["stamp"]) for record in records):
        return False
    records = sorted(records, key=lambda item: item["index"])
    messages = row.setdefault("messages", [])
    ids = [record["call"]["call_id"] for record in records]
    # Never nest an exchange inside a preceding incomplete assistant exchange.
    assistant = next((message for message in reversed(messages) if message.get("role") == "assistant"), {})
    offered = {call.get("id") for call in assistant.get("tool_calls", [])}
    answered = {message.get("tool_call_id") for message in messages if message.get("role") == "tool"}
    if not set(ids) <= offered:
        if not recover or offered - answered:
            return False
        messages.append({"role": "assistant", "content": "Recovered completed read calls from a partial model response; the original response did not finish.",
                         "tool_calls": [copy.deepcopy(record["raw_call"]) for record in records],
                         # The content is a note for the model; the UI shows its own wording.
                         "ui": {"kind": "partial_recovery"},
                         "context_epoch": row.get("context_epoch", 0)})
    unanswered = [record for record in records if record["call"]["call_id"] not in answered]
    notice = budget.notice(row) if unanswered else None
    for index, record in enumerate(unanswered):
        messages.append(_tool_message(record, server_note=notice if index == len(unanswered) - 1 else None))
        _apply_observed(row, record)
        event(row, "tool_result", tool=record["call"]["name"], call_id=record["call"]["call_id"],
              operation_id=record["call"]["operation_id"], result=record["result"], streamed=True)
    operations = {record["call"]["operation_id"] for record in records}
    row["stream_read_journal"] = [entry for entry in row.get("stream_read_journal", [])
                                  if entry["call"]["operation_id"] not in operations]
    return True


class StreamedReads:
    def __init__(self, runtime, selected, specs, cancelled, parallel_reads, *, metadata=None):
        self.runtime, self.selected = runtime, copy.deepcopy(selected)
        self.specs, self.cancelled = copy.deepcopy(specs), cancelled
        self.metadata = metadata or runtime._metadata
        self.stamp = _stamp(selected)
        self.authority = copy.deepcopy(selected.get("_model_retry_authority"))
        self.request_id = selected.get("_request_id")
        self.parent_operation_id = None
        self.raw, self.records = {}, None
        self.existing_ids = {call["id"] for message in selected.get("messages", [])
                             for call in message.get("tool_calls", [])}
        self.coordinator = StreamToolCoordinator(self._normalize, self._admit, self._run,
            parallel_reads=parallel_reads, internal_names=INTERNAL_NAMES, cancelled=cancelled)

    def _event_context(self):
        return ({"source": "code_mode", "parent_operation_id": self.parent_operation_id}
                if self.parent_operation_id else {"streamed": True})

    def _normalize(self, raw):
        call = normalize_tool_calls([raw], self.specs, existing_call_ids=self.existing_ids,
                                    metadata=self.metadata, internal_names=INTERNAL_NAMES)[0]
        call["raw_arguments_sha256"] = call["arguments_sha256"]
        call["arguments_sha256"] = digest(call["arguments"])
        call.update(operation_id="op_" + uuid.uuid4().hex, internal=call["name"] in INTERNAL_NAMES,
                    goal_revision=self.stamp["goal_revision"], scope_revision=self.stamp["scope_revision"],
                    context_epoch=self.stamp["context_epoch"])
        return call

    def _authorized(self, data, row):
        return (not self.cancelled() and row.get("status") == "running" and not row.get("pending_control")
                and data["settings"].get("enabled") and _same(row, self.stamp)
                and (self.authority is None or model_retry_authority(data["settings"]) == self.authority)
                and (row.get("in_flight") or {}).get("kind") == "model"
                and (self.request_id is None or (row.get("in_flight") or {}).get("request_id") == self.request_id))

    def _check(self, data, row, call):
        if not self._authorized(data, row):
            return None
        if "extension_binding" in call and hasattr(self.runtime.tools, "verify_extension_binding"):
            self.runtime.tools.verify_extension_binding(call)
        meta = self.runtime.tools.metadata(call["name"])
        if meta.get("effect") != "read" or meta.get("requires_explicit_approval"):
            return None
        if "extension_binding" in call and meta.get("extension_binding") != call["extension_binding"]:
            return None
        if bool(meta.get("computer_access")) != bool(call.get("computer_access")):
            return None
        if call.get("computer_access") or meta.get("computer_access") or (call.get("extension_binding") or {}).get("computer_access"):
            if not data["settings"].get("desktop_access") or not (row.get("local_execution") or {}).get("desktop_access"):
                return None
        spec = next((item["function"] for item in self.runtime._business_specs(row)
                     if item["function"]["name"] == call["name"]), None)
        assert_schema_unchanged(call, spec)
        validate(call["arguments"], spec["parameters"])
        self.runtime._validate_context(row, cheap_only=True, control_state=data)
        if decide(call, meta, row, data["settings"]).action != "execute":
            return None
        if self.runtime._read_retries_exhausted(row, call):
            return None
        return meta

    def _admit(self, index, call):
        def admit(data):
            row = task(data, self.selected["id"])
            try:
                meta = self._check(data, row, call)
            except (AgentError, ValidationError):
                return None
            if meta is None:
                return None
            journal = row.setdefault("stream_read_journal", [])
            if len(journal) >= MAX_PARALLEL_TOOLS or any(entry["call"]["call_id"] == call["call_id"] for entry in journal):
                return None
            try:
                self.runtime.store.reserve(data, row["id"], output_policy.storage_growth(row["limits"]["max_output_chars"]) * (len(journal) + 1),
                                           extra_disk=results.MAX_RESULT_BYTES)
                # The code envelope already reserves/accounts for wall-clock time.
                reservation = budget.reserve(row, "tool", call["name"],
                                             **({"seconds_bound": 0} if self.parent_operation_id else {}))
            except AgentError as exc:
                if self.parent_operation_id:
                    raise
                if str(exc) in {"agent_tool_budget_exhausted", "agent_time_budget_exhausted", "agent_token_budget_exhausted",
                                "agent_storage_limit", "agent_storage_unavailable"}:
                    # Overlap is optional. The model may currently reserve the
                    # entire remaining budget; settle it before normal dispatch.
                    return None
                raise
            classification = classify_operation(call["name"], call["arguments"], meta,
                operation_id=call["operation_id"], task_id=row["id"],
                workspace_root=(row.get("local_execution") or {}).get("workspace_root", ""),
                goal_revision=row["goal"]["revision"], scope_revision=row["scope_revision"],
                context_epoch=row.get("context_epoch", 0), control_revision=row["control_revision"])
            flight = {**call, "kind": "tool", "dispatched": True, "authorization": "read", "preview": None,
                      "operation_classification": classification,
                      "permission_record": {"basis": "read", "operation_id": call["operation_id"],
                        "arguments_sha256": digest(call["arguments"]), "preview_sha256": digest(None),
                        "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"],
                        "context_epoch": row.get("context_epoch", 0), "control_revision": row["control_revision"]}}
            entry = {"index": index, "raw_call": copy.deepcopy(self.raw[index]), "call": copy.deepcopy(call),
                     "stamp": copy.deepcopy(self.stamp), "authority": model_retry_authority(data["settings"]),
                     "reservation_id": reservation["id"], "reserved_seconds": reservation["charged_seconds"],
                     "state": "intent", "request_id": self.request_id}
            if self.parent_operation_id:
                entry["parent_operation_id"] = self.parent_operation_id
                flight.update(self._event_context())
            journal.append(entry)
            verification.record(row, call, "intent", now(), flight=flight)
            event(row, "tool_started", tool=call["name"], call_id=call["call_id"], operation_id=call["operation_id"],
                  effect="read", step=row["steps"], **self._event_context())
            return {**copy.deepcopy(entry), "meta": copy.deepcopy(meta), "classification": classification}
        return self.runtime.store.transaction(admit)

    def _complete_record(self, prepared, outcome, elapsed):
        def finish(data):
            row = data["tasks"].get(self.selected["id"])
            if row is None:
                return
            entry = next((entry for entry in row.get("stream_read_journal", [])
                          if entry["call"]["operation_id"] == prepared["call"]["operation_id"]), None)
            budget.settle(row, prepared["reservation_id"], elapsed_seconds=0 if self.parent_operation_id else elapsed)
            if entry is None or entry.get("state") == "complete":
                return
            value = copy.deepcopy(outcome)
            if len(encoded(value["result"])) > self.selected["limits"]["max_output_chars"]:
                try:
                    reference = results.save(self.runtime.store, self.selected, prepared["call"], value["result"])
                    if value.get("origin"):
                        reference["resource_origin"] = value["origin"]
                    row.setdefault("result_refs", []).append(reference)
                    value.update(
                        reference=reference,
                        result=results.format_reference_result(
                            prepared["call"]["name"],
                            value["result"],
                            reference,
                            max_bytes=self.selected["limits"]["max_output_chars"],
                            note="Full output is encrypted locally. Use ryn_read_result for bounded pages.",
                        ),
                    )
                except AgentError as exc:
                    value.update(failed=True, result={"error": _safe_error(exc), "not_executed": False, "no_mutation": True})
            entry.update(value, state="complete")
            verification.record(row, prepared["call"], "failed" if value["failed"] else "confirmed", now(), result=value["result"])
            event(row, "tool_execution_completed", tool=prepared["call"]["name"], call_id=prepared["call"]["call_id"],
                  operation_id=prepared["call"]["operation_id"], result=value["result"],
                  context_epoch=self.stamp["context_epoch"], **self._event_context())
            if self.parent_operation_id:
                # Nested results belong to the JS promise, not standalone chat messages.
                self.runtime._apply_deferred_observed_files(row, self.selected, value.get("deferred") or {})
                row["stream_read_journal"].remove(entry)
        self.runtime.store.transaction(finish)

    def _run(self, prepared):
        started, deferred = time.monotonic(), {}
        call = prepared["call"]
        deadline = float("inf") if self.parent_operation_id else started + prepared["reserved_seconds"]
        def cancelled():
            if self.cancelled() or (prepared.get("_nested_cancelled") and prepared["_nested_cancelled"]()):
                cancelled.reason = getattr(self.cancelled, "reason", "agent_cancelled")
                return True
            if time.monotonic() >= deadline:
                cancelled.reason = "agent_time_budget_exhausted"
                return True
            return False
        cancelled.reason = "agent_cancelled"
        try:
            allowed = self.runtime.store.transaction(lambda data: self._check(data, task(data, self.selected["id"]), call) is not None)
            if not allowed or cancelled():
                raise AgentToolRejected(cancelled.reason)
            options = {"operation_id": call["operation_id"], "cancelled": cancelled}
            if prepared["meta"].get("scope_context_supported"):
                context = self.runtime._tool_context(self.selected, call["operation_id"])
                context.update(_deferred_observed_files=deferred,
                    _observed_files_snapshot=copy.deepcopy(self.selected.get("observed_files") or {}),
                    operation_classification=copy.deepcopy(prepared["classification"]), context_epoch=self.stamp["context_epoch"])
                if "extension_binding" in call:
                    context["extension_binding"] = copy.deepcopy(call["extension_binding"])
                options["context"] = context
            with diagnostic_log.context_scope(task_id=self.selected["id"], operation_id=call["operation_id"]):
                result = sanitized(self.runtime._execute_business_tool(self.selected, call["name"], copy.deepcopy(call["arguments"]), **options))
            outcome = {"result": result, "failed": False, "deferred": deferred,
                       "origin": copy.deepcopy(result.get("origin")) if isinstance(result, dict) and prepared["meta"].get("scope_context_supported") else None}
        except Exception as exc:
            extra = {}
            if isinstance(exc, AgentToolRejected):
                if getattr(exc, "correction", None):
                    extra["correction"] = sanitized(exc.correction[:1600])
                if getattr(exc, "details", None):
                    extra["details"] = sanitized(copy.deepcopy(exc.details))
            outcome = {"result": {"error": _safe_error(exc), "not_executed": isinstance(exc, AgentToolRejected), "no_mutation": True},
                       "failed": True, "deferred": deferred, "origin": None}
            outcome["result"].update(extra)
        prepared["_outcome"] = outcome
        prepared["_elapsed"] = max(0.0, time.monotonic() - started)
        self._complete_record(prepared, outcome, prepared["_elapsed"])
        return outcome

    def on_event(self, item):
        if isinstance(item, dict) and item.get("type") == "tool_call_complete":
            self.raw[item.get("index")] = copy.deepcopy(item.get("call"))
        self.coordinator.on_event(item)

    def _collect(self, records):
        if self.records is None:
            self.records = []
            for record in records:
                prepared = record["admission"]
                persistence_error = None
                if record["error"] is not None:
                    outcome = prepared.get("_outcome") or {"result": {"error": "agent_stream_read_interrupted", "no_mutation": True},
                                                           "failed": True, "deferred": {}, "origin": None}
                    try:
                        self._complete_record(prepared, outcome, prepared.get("_elapsed", 0.0))
                    except Exception as exc:
                        persistence_error = _safe_error(exc)
                try:
                    current = self.runtime.store.read()["tasks"].get(self.selected["id"], {})
                except Exception as exc:
                    current, persistence_error = {}, _safe_error(exc)
                entry = next((entry for entry in current.get("stream_read_journal", [])
                              if entry["call"]["operation_id"] == prepared["call"]["operation_id"]), None)
                if entry is not None and entry.get("state") == "complete":
                    self.records.append(copy.deepcopy(entry))
                else:
                    # Deletion/reset can discard a journal while its admitted
                    # read finishes. The host still receives its execution
                    # outcome; an obsolete stamp can never publish it anew.
                    outcome = prepared.get("_outcome") or record.get("result") or {
                        "result": {"error": "agent_stream_read_interrupted", "no_mutation": True},
                        "failed": True, "deferred": {}, "origin": None}
                    fallback = {key: copy.deepcopy(value) for key, value in prepared.items()
                                if key not in {"meta", "classification", "_outcome", "_elapsed"}} | outcome
                    if persistence_error:
                        fallback["persistence_error"] = persistence_error
                    self.records.append(fallback)
        return copy.deepcopy(self.records)

    def drain(self, final_calls):
        try:
            records = self._collect(self.coordinator.drain(final_calls))
            if any(record.get("persistence_error") for record in records):
                raise AgentError("agent_stream_read_persistence_failed")
            return records
        except BaseException as exc:
            exc.stream_results = self._collect(self.coordinator.close())
            raise

    def abort(self):
        return self._collect(self.coordinator.close())

    def publish(self, row, records):
        return _publish(row, records)

    def record_recovered(self, row, records):
        return _publish(row, records, recover=True)


class NestedReadDispatch(StreamedReads):
    """Code-mode children use the same read admission, budget and durable receipts.

    Code mode retains its read-only tool contract. Writes continue through the
    direct queue, where previews and interactive approval can suspend safely.
    """

    def __init__(self, runtime, selected, specs, cancelled, parent_operation_id):
        super().__init__(runtime, selected, specs, cancelled, ())
        self.parent_operation_id = parent_operation_id
        self.ordinal = 0
        self.lock = threading.Lock()

    def __deepcopy__(self, memo):
        return self

    def _authorized(self, data, row):
        flight = row.get("in_flight") or {}
        return (not self.cancelled() and row.get("status") == "running" and not row.get("pending_control")
                and data["settings"].get("enabled") and _same(row, self.stamp)
                and flight.get("operation_id") == self.parent_operation_id
                and flight.get("name") == "ryn_code_exec" and flight.get("dispatched")
                and flight.get("authority") == model_retry_authority(data["settings"]))

    def _check(self, data, row, call):
        from .subagents import check_subagent_tool_policy
        check_subagent_tool_policy(row, call["name"], call["arguments"])
        return super()._check(data, row, call)

    def __call__(self, name, arguments, *, operation_id, cancelled, context=None):
        with self.lock:
            index = self.ordinal
            self.ordinal += 1
        raw = {"id": "nested_" + uuid.uuid4().hex, "type": "function",
               "function": {"name": name, "arguments": encoded(arguments).decode()}}
        call = self._normalize(raw)
        if call.get("error_result"):
            raise AgentToolRejected(call["error_result"]["error"])
        call["operation_id"] = operation_id
        self.raw[index] = raw
        try:
            # Fail with the actual owner budget error, rather than the optional
            # overlap fallback used by model streams.
            prepared = self._admit(index, call)
            if prepared is None:
                raise AgentToolRejected("agent_permission_denied")
            prepared["_nested_cancelled"] = cancelled
            outcome = self._run(prepared)
            if outcome["failed"]:
                raise AgentToolRejected(outcome["result"]["error"])
            return outcome["result"]
        finally:
            self.raw.pop(index, None)


def recover_stream_reads(row, *, allow_publish=False):
    """Close durable read exchanges after restart, without executing any tool.

    The caller applies current settings/authority checks before allowing history
    publication. A stopped/stale row retains receipts only, never a new exchange.
    """
    records = row.get("stream_read_journal", [])
    for record in records:
        if record.get("state") != "complete":
            record.update(state="complete", failed=True, deferred={}, origin=None,
                          result={"error": "agent_stream_read_interrupted", "no_mutation": True})
            verification.record(row, record["call"], "failed", now(), result=record["result"])
            event(row, "tool_execution_completed", tool=record["call"]["name"], call_id=record["call"]["call_id"],
                  operation_id=record["call"]["operation_id"], result=record["result"],
                  **({"source": "code_mode", "parent_operation_id": record["parent_operation_id"]}
                     if record.get("parent_operation_id") else {"streamed": True}))
    if allow_publish:
        _publish(row, [record for record in records if not record.get("parent_operation_id")], recover=True)
    # Never replay a code cell or manufacture top-level calls for its children.
    row["stream_read_journal"] = [record for record in row.get("stream_read_journal", [])
                                  if not record.get("parent_operation_id")]
    return copy.deepcopy(records)
