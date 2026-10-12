"""Read-only, credential-free diagnostics for the trusted current task.

Configuration/capability records are not fresh inference or connection probes.
Device observations come only from the existing injected evidence provider.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime

from .store import AgentError, AgentToolRejected

_TITLES = {
    "ryn_diagnose_current_task": "Inspect only the current task's state, safe error code and next steps; never other task contents.",
    "ryn_diagnose_task": "Inspect safe failure and operation metadata of one existing task owned by this node. No task text, arguments, resource contents or credentials are returned.",
    "ryn_get_agent_configuration": "Read saved Agent enablement, actual default model and limits. Model availability is configuration evidence, not a live probe.",
    "ryn_get_system_diagnostics": "Inspect credential-free model capability records and existing device presence/reachability evidence. Never infer untested connections as healthy.",
}
NAMES = frozenset(_TITLES)
_MODEL_FIELDS = ("id", "name", "source", "source_id", "source_name", "model_id", "service_id", "peer_id", "adapter", "destination_revision")
_LIMITS = ("max_steps", "max_seconds", "max_context_chars", "max_output_chars")
_STATES = {"queued", "running", "paused", "waiting_input", "waiting_approval", "completed", "cancelled", "failed", "interrupted"}
_RUN_STATES = _STATES | {"pausing", "reconciling", "ended"}
_OUTCOMES = {"unverified", "verified", "partial", "not_achieved", "unknown"}
_ERROR_GUIDANCE = {
    "agent_invalid_control_proposal": ("correct_input_request", "Correct the control arguments. Credential requests accept only credential_ref fields; ordinary facts use a separate information/choice request. Never put a key in chat.", "/agent"),
    "agent_model_unavailable": ("review_models", "Review the selected model's availability and explicitly select a usable model.", "/services/sources"),
    "agent_model_tools_unsupported": ("verify_tools", "Run the model's structured tool verification, then review the result before retrying.", "/services/sources"),
    "agent_inference_timeout": ("review_connection", "Check the source connection and model timeout; do not assume the model responded.", "/services/sources"),
    "agent_model_connection_failed": ("review_connection", "Check the source connection using its existing test action.", "/services/sources"),
    "agent_model_busy": ("wait_for_capacity", "Wait for model capacity, then start an explicit retry.", "/agent"),
    "agent_step_limit": ("review_budget", "Review the task's step limit and narrow the request before starting another task.", "/agent"),
    "agent_time_limit": ("review_budget", "Review the task's time limit and narrow the request before starting another task.", "/agent"),
    "agent_node_restarted": ("resume_if_allowed", "Review current state and use Resume only when the server permits it.", "/agent"),
    "agent_write_outcome_unknown": ("reconcile_original_write", "Check the original operation receipt; do not repeat the write to guess its outcome.", "/agent"),
    "agent_scope_changed": ("review_scope", "Review current task data permissions and explicitly resume if appropriate.", "/agent"),
    "agent_permission_denied": ("review_permissions", "The recorded operation was rejected by permission enforcement. Review current task and Agent permissions; this code alone does not identify a hash mismatch or prove that approval was never staged. Do not retry a write without current authorization.", "/agent"),
    "agent_resource_scope_denied": ("review_scope", "Review this task's authorized directories; ordinary messages do not grant access.", "/agent"),
    "agent_model_destination_changed": ("review_destination", "Review the current model destination and explicitly renew data consent.", "/agent"),
    "agent_cancelled": ("review_receipts", "Review any dispatched operation receipts before deciding on a new task.", "/agent"),
    "agent_invalid_tool_call": ("review_tools", "Review model tool compatibility; no proposed operation should be assumed executed.", "/services/sources"),
    "resource_read_timeout": ("review_resource_connection", "The authorized resource read timed out. Check the named connection using the owner Drives interface; do not modify it or rerun the task automatically. This does not establish a credential or quota problem.", "/drives"),
}


def safe_label(value, limit=256):
    if not isinstance(value, str):
        return ""
    # Remote display labels are untrusted. Never forward recognizable credentials
    # or URLs (including embedded credentials/query strings) in diagnostic labels.
    value = re.sub(r"(?i)\b(?:https?|smb|file)://\S+|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+", "[redacted]", value)
    return value[:limit]


_text = safe_label


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _bool(value):
    return value if type(value) is bool else None


def _model_evidence(row):
    # A read performs no probes. That says nothing about the model's history;
    # retain safe recorded observations without forwarding arbitrary probe text.
    raw = row.get("evidence")
    raw = raw if isinstance(raw, dict) else {}
    recorded = {}
    for name in ("connection", "model_exists", "tools"):
        item = raw.get(name)
        item = item if isinstance(item, dict) else {}
        stamp = item.get("checked_at")
        try:
            if not isinstance(stamp, str) or len(stamp) > 64 or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                stamp = None
        except ValueError:
            stamp = None
        status = item.get("status")
        status = status if isinstance(status, str) and status in {"passed", "failed", "present", "missing", "supported", "unsupported", "unknown"} else "unknown"
        method = item.get("method")
        method = method if isinstance(method, str) and method in {"fixed_chat", "models_listing", "two_turn_client_tool", "actual_loopback_socket_probe"} else "unknown"
        recorded[name] = {"status": status, "checked_at": stamp, "method": method}
    return {"kind": "catalog_configuration_and_capability_record",
            "read_operation": {"connection_probe_performed": False, "inference_probe_performed": False},
            "recorded_probes": recorded, "history_coverage": "not_established",
            "note": "This read performs no new probe. Saved records retain their original times; missing records do not mean never tested. Records do not prove current reachability."}


class DiagnosticTools:
    def __init__(self, app, catalog, device_evidence):
        self.app, self.catalog, self.device_evidence = app, catalog, device_evidence

    def specs(self):
        return [{"type": "function", "function": {"name": name, "description": title,
                "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "minLength": 1, "maxLength": 128}} if name == "ryn_diagnose_task" else {}, "required": ["task_id"] if name == "ryn_diagnose_task" else [], "additionalProperties": False}}}
                for name, title in _TITLES.items()]

    def metadata(self, name):
        if name not in NAMES:
            raise AgentToolRejected("agent_unknown_tool")
        return {"effect": "read", "risk": "low", "title": _TITLES[name], "scope_context_supported": True}

    def _runtime(self):
        runtime = getattr(self.app.state, "agent_runtime", None)
        if runtime is None:
            raise AgentToolRejected("agent_diagnostics_unavailable")
        return runtime

    def _models(self):
        try:
            rows = self.catalog.list_models()
        except Exception:
            raise AgentToolRejected("agent_diagnostics_catalog_unavailable") from None
        output = []
        for row in rows[:64]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            output.append({**{key: _text(row[key]) for key in _MODEL_FIELDS if key in row},
                "available": _bool(row.get("available")), "agent_supported": _bool(row.get("agent_supported")),
                "tool_support": row.get("tool_support") if row.get("tool_support") in {"supported", "unsupported", "unknown"} else "unknown",
                "evidence": _model_evidence(row)})
        return output

    def _configuration(self):
        runtime = self._runtime()
        try:
            snapshot = runtime.configuration_snapshot()
            settings = snapshot["settings"]
        except Exception:
            raise AgentToolRejected("agent_diagnostics_settings_unavailable") from None
        model_id = _text(settings.get("model_id"))
        models = self._models()
        return {"enabled": _bool(settings.get("enabled")), "default_model_id": model_id,
                "settings_revision": snapshot["settings_revision"],
                "default_model": next((row for row in models if row["id"] == model_id), None),
                "default_model_in_catalog": any(row["id"] == model_id for row in models),
                "permission_mode": settings.get("permission_mode") if settings.get("permission_mode") in {"read_only", "confirm_changes", "automatic"} else "unknown",
                "limits": {key: _number(settings.get(key)) for key in _LIMITS},
                "evidence": {"kind": "persisted_agent_settings", "default_model_selected_from": "settings.model_id"},
                "note": "Agent enablement is optional. Catalog availability/tool support is not a fresh connection, inference, quota or performance test."}

    def _current_task(self, context, target_id=None):
        runtime = self._runtime()
        if target_id is not None and (not isinstance(context, dict) or context.get("owner_id") != runtime.owner_id or context.get("consumer_node_id") != runtime.consumer_node_id):
            raise AgentToolRejected("agent_diagnostics_context_denied")
        task_id = target_id if target_id is not None else context.get("task_id") if isinstance(context, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise AgentToolRejected("agent_task_context_required")
        try:
            row = runtime.get(task_id)
        except AgentError:
            raise AgentToolRejected("agent_diagnostics_task_unavailable") from None
        raw_error = row.get("error")
        error = raw_error if isinstance(raw_error, str) and raw_error in _ERROR_GUIDANCE else "agent_unknown_error" if raw_error else None
        guidance = [_ERROR_GUIDANCE[error]] if error in _ERROR_GUIDANCE else []
        if error == "agent_unknown_error":
            guidance.append(("review_task", "Review the current task and safe local service logs; the original exception is not sent to the model.", "/agent"))
        state = row.get("status") if row.get("status") in _STATES else "unknown"
        if not error and state == "waiting_approval":
            guidance.append(("review_approval", "Review the exact pending change in the owner interface; diagnostics cannot approve it.", "/agent"))
        if not error and state == "waiting_input":
            guidance.append(("answer_current_question", "Answer the current bound question in the owner interface; never paste credentials into a normal answer.", "/agent"))
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        result = {"task_id": _text(task_id), "status": state, "run_state": row.get("run_state") if row.get("run_state") in _RUN_STATES else "unknown",
                "error": error, "steps": _number(row.get("steps")), "elapsed_seconds": _number(row.get("elapsed_seconds")),
                "usage": {key: _number(usage.get(key)) for key in ("input_tokens", "output_tokens", "total_tokens")},
                "outcome_status": row.get("outcome", {}).get("status") if isinstance(row.get("outcome"), dict) and row["outcome"].get("status") in _OUTCOMES else "unknown",
                "suggestions": [{"id": key, "instruction": text, "owner_path": path, "executed": False} for key, text, path in guidance],
                "evidence": {"kind": "current_task_runtime_record", "other_tasks_read": False},
                "note": "All states and counters describe task_id only, not the diagnosing task. Error codes classify observed failures, not every underlying cause. Missing time/usage stays null. A finished turn is not proof of goal completion."}
        # A recoverable tool rejection need not set row.error. The diagnosing
        # task needs the same bounded operation facts as an owner-selected task.
        result.update(self._operations(row))
        if target_id is not None:
            result["evidence"] = {"kind": "owner_node_task_metadata_projection", "target_task_metadata_read": True,
                                  "other_task_contents_read": False, "other_tasks_read": target_id != context.get("task_id")}
        return result

    @staticmethod
    def _operations(row):
        operations = {}
        read_count = write_count = 0
        for entry in row.get("events", []):
            if not isinstance(entry, dict) or entry.get("type") not in {"tool_started", "tool_result"}:
                continue
            if entry.get("internal") is True:
                receipt = entry.get("result") if isinstance(entry.get("result"), dict) else {}
                if entry["type"] != "tool_result" or receipt.get("error") not in _ERROR_GUIDANCE:
                    continue
            operation_id = entry.get("operation_id") or entry.get("call_id")
            if not isinstance(operation_id, str):
                continue
            operation = operations.setdefault(operation_id, {"tool": _text(entry.get("tool")), "effect": "unknown", "dispatched": False, "not_executed": None, "error": None})
            if entry["type"] == "tool_started":
                effect = entry.get("effect")
                if effect in {"read", "write"}:
                    first_dispatch = not operation["dispatched"]
                    operation.update(effect=effect, dispatched=True)
                    read_count += first_dispatch and effect == "read"
                    write_count += first_dispatch and effect == "write"
            else:
                receipt = entry.get("result") if isinstance(entry.get("result"), dict) else {}
                error = receipt.get("error")
                operation.update(not_executed=_bool(receipt.get("not_executed")), error=error if isinstance(error, str) and error in _ERROR_GUIDANCE else "agent_unknown_error" if error else None)
                if operation["error"] in _ERROR_GUIDANCE:
                    key, instruction, path = _ERROR_GUIDANCE[operation["error"]]
                    operation["suggestion"] = {"id": key, "instruction": instruction, "owner_path": path, "executed": False}
                # Connection metadata is safe operational evidence. Never forward
                # arbitrary argument/result content or an underlying file path.
                for key in ("connection_id", "connection_name"):
                    if isinstance(receipt.get(key), str):
                        operation[key] = _text(receipt[key])
        return {"operations": list(operations.values())[:64], "operations_truncated": len(operations) > 64,
                "read_dispatched": read_count > 0, "write_dispatched": write_count > 0,
                "read_dispatch_count": read_count, "write_dispatch_count": write_count,
                "operation_evidence_note": "Dispatch describes runtime invocation attempts, not proof that a business mutation occurred. Only stored event metadata is projected; no task data permissions are granted."}

    def _devices(self, cancelled):
        if self.device_evidence is None:
            return {"status": "unavailable", "devices": [], "note": "No device evidence provider is installed; no probe was inferred."}
        try:
            value = self.device_evidence(cancelled)
        except AgentToolRejected:
            raise
        except Exception:
            raise AgentToolRejected("agent_diagnostics_devices_unavailable") from None
        devices = []
        for row in value.get("devices", [])[:64]:
            http, lan = row.get("http_probe_performed") is True, row.get("lan_probe_performed") is True
            devices.append({**{key: _text(row[key]) for key in ("peer_id", "node_name", "name", "last_seen", "checked_at") if key in row},
                "online": _bool(row.get("online")), "evidence_source": row.get("evidence_source") if row.get("evidence_source") in {"unknown", "self", "http", "heartbeat", "heartbeat_expired"} else "unknown",
                "http_probe_performed": http, "lan_probe_performed": lan,
                "http_reachable": _bool(row.get("http_reachable")) if http else None,
                "lan_reachable": _bool(row.get("lan_reachable")) if lan else None})
        return {"status": "available", "devices": devices, "note": "Self presence proves the executing process is alive only. Untested reachability is null; membership or a configured endpoint is not connectivity proof."}

    def execute(self, name, args, *, context, cancelled):
        self.metadata(name)
        if not isinstance(args, dict) or (name != "ryn_diagnose_task" and args) or (name == "ryn_diagnose_task" and (set(args) != {"task_id"} or not isinstance(args.get("task_id"), str) or not 1 <= len(args["task_id"]) <= 128)):
            raise AgentToolRejected("agent_tool_arguments_invalid")
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        if name == "ryn_diagnose_current_task":
            result = self._current_task(context)
        elif name == "ryn_diagnose_task":
            result = self._current_task(context, args["task_id"])
        elif name == "ryn_get_agent_configuration":
            result = self._configuration()
        else:
            result = {"models": self._models(), "devices": self._devices(cancelled),
                      "note": "Model records do not measure current connection, inference, quotas, or throughput. Diagnostics is read only and grants no permissions."}
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        return copy.deepcopy(result)
