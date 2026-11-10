"""Exact, approved future-task default changes in the Agent store transaction."""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone

from .model_switch import resolve
from .store import AgentToolRejected, encoded

NAME = "ryn_set_default_agent_model"


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def binding(runtime, row):
    return {"task_id": row["id"], "owner_id": runtime.owner_id, "consumer_node_id": runtime.consumer_node_id,
            "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"],
            "scope_revision": row["scope_revision"], "model_destination": row.get("model_destination")}


class DefaultModelTool:
    def __init__(self, app):
        self.app = app

    def runtime(self):
        runtime = getattr(self.app.state, "agent_runtime", None)
        if runtime is None:
            raise AgentToolRejected("agent_configuration_unavailable")
        return runtime

    def row(self, runtime, data, context):
        row = data["tasks"].get((context or {}).get("task_id"))
        if row is None or any(context.get(key) != value for key, value in binding(runtime, row).items()):
            raise AgentToolRejected("agent_configuration_context_changed")
        return row

    def preview(self, args, context):
        runtime = self.runtime()
        data = runtime.store.read()
        self.row(runtime, data, context)
        if data.get("settings_revision", 0) != args["expected_settings_revision"]:
            raise AgentToolRejected("agent_settings_revision_conflict")
        if getattr(runtime.model, "supports_destination_binding", False) is not True:
            raise AgentToolRejected("agent_model_destination_unverifiable")
        model, destination = resolve(runtime, args["model_id"])
        return {"title": "Set default model for new tasks", "settings_revision": data.get("settings_revision", 0),
                "before": {"model_id": data["settings"]["model_id"]}, "after": {"model_id": args["model_id"]},
                "model": {key: model.get(key) for key in ("id", "name", "source", "source_name", "source_id", "peer_id")},
                "destination": destination, "applies_to": "future_tasks_only",
                "changes_current_task": False, "changes_enablement_permissions_or_sharing": False}

    def execute(self, args, *, operation_id, context, expected_preview, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        runtime = self.runtime()
        preview = self.preview(args, context)
        if expected_preview != preview:
            raise AgentToolRejected("agent_approval_preview_changed")
        args_hash = fingerprint(args)

        def apply(data):
            row = self.row(runtime, data, context)
            flight = row.get("in_flight") or {}
            if (row["status"] != "running" or not data["settings"]["enabled"]
                    or flight.get("operation_id") != operation_id or flight.get("name") != NAME
                    or flight.get("arguments") != args or flight.get("dispatched") is not True
                    or flight.get("authorization") not in {"approved", "automatic"}
                    or flight.get("preview") != expected_preview):
                raise AgentToolRejected("agent_configuration_authority_changed")
            if "read_only" in {row["permission_mode"], data["settings"]["permission_mode"]}:
                raise AgentToolRejected("agent_permission_denied")
            if flight["authorization"] == "automatic" and (
                    row["permission_mode"] != "automatic" or data["settings"]["permission_mode"] != "automatic"
                    or NAME not in row["allowed_write_tools"] or NAME not in data["settings"]["allowed_write_tools"]):
                raise AgentToolRejected("agent_permission_denied")
            if data.get("settings_revision", 0) != args["expected_settings_revision"]:
                raise AgentToolRejected("agent_settings_revision_conflict")
            operations = row.setdefault("configuration_operations", {})
            if operation_id in operations:
                raise AgentToolRejected("agent_operation_already_confirmed")
            before = copy.deepcopy(data["settings"])
            data["settings"]["model_id"] = args["model_id"]
            data["default_model_destination"] = copy.deepcopy(preview["destination"])
            data["settings_revision"] = args["expected_settings_revision"] + 1
            result = {"ok": True, "operation_id": operation_id, "default_model_id": args["model_id"],
                      "settings_revision": data["settings_revision"], "destination": preview["destination"],
                      "applies_to": "future_tasks_only", "current_task_model_id": row["model_id"],
                      "before": before, "after": copy.deepcopy(data["settings"])}
            operations[operation_id] = {"operation_id": operation_id, "tool": NAME,
                "arguments_sha256": args_hash, "binding": binding(runtime, row), "result": copy.deepcopy(result)}
            return result

        # Mutation and attributable receipt commit in one encrypted transaction.
        # Unexpected storage errors remain ambiguous, never claimed no-side-effect.
        return runtime.store.transaction(apply)

    def reconcile(self, operation_id, *, context, cancelled):
        unknown = {"outcome": "unknown", "reason": "No matching atomic configuration receipt; no write was retried."}
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        runtime = self.runtime()
        data = runtime.store.read()
        row = data["tasks"].get((context or {}).get("task_id"))
        record = (row or {}).get("configuration_operations", {}).get(operation_id)
        requested = (context or {}).get("operation") or {}
        # A later owner control update may stop continuation, but must not hide
        # an attributable write. Only immutable owner/task/node identity here.
        if not record or any(record["binding"].get(key) != context.get(key)
                             for key in ("task_id", "owner_id", "consumer_node_id")):
            return unknown
        if (requested.get("operation_id") != operation_id or requested.get("tool") != NAME
                or requested.get("arguments_sha256") != record["arguments_sha256"]
                or fingerprint(requested.get("arguments")) != record["arguments_sha256"]):
            return unknown
        return {"outcome": "executed", "receipt": {key: copy.deepcopy(record[key])
                for key in ("operation_id", "tool", "arguments_sha256", "result")}}

    def verify(self, receipt, *, context, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        runtime = self.runtime()
        data = runtime.store.read()
        row = data["tasks"].get((context or {}).get("task_id"))
        record = (row or {}).get("configuration_operations", {}).get(receipt.get("operation_id"))
        proven = bool(record and record["arguments_sha256"] == receipt.get("arguments_sha256")
                      and fingerprint(receipt.get("arguments")) == record["arguments_sha256"]
                      and record["result"] == receipt.get("result")
                      and all(record["binding"].get(key) == context.get(key)
                              for key in ("task_id", "owner_id", "consumer_node_id")))
        settings_match = proven and data["settings"] == record["result"]["after"]
        destination_matches = False
        if proven:
            try:
                destination_matches = resolve(runtime, record["result"]["default_model_id"])[1] == record["result"]["destination"]
            except ValueError:
                pass
        return {"status": "passed" if settings_match and destination_matches else "unknown",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "evidence": {"operation_attribution": "atomic_configuration_receipt" if proven else "not_established",
                    "current_settings_match": bool(settings_match), "current_destination_match": destination_matches,
                    "default_model_id": data["settings"]["model_id"], "settings_revision": data.get("settings_revision", 0),
                    "enabled": data["settings"]["enabled"], "scope": "future_task_default_only; no sharing or inference probe"}}
