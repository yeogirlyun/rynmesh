"""Exact business gates for the reference runner, shared Ryn connectors underneath.

This is a single-process acceptance host. Restart/control cases must use a durable
collector; these in-memory records never qualify as recovery evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

from rynmesh.agent_runtime.diagnostics import safe_label
from rynmesh.agent_runtime.store import AgentError, AgentReadFailure, AgentToolRejected


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


class ReferenceToolHost:
    def __init__(self, tools, *, policy, cancelled, context=None, admission=None,
                 session_id=None, before_approval=None, after_business=None):
        self.tools, self.policy, self.cancelled, self.context = tools, policy, cancelled, context
        self.admission = admission
        self.allowed = {item["function"]["name"] for item in tools.specs()}
        self.session = session_id or uuid.uuid4().hex
        self.before_approval, self.after_business = before_approval, after_business
        self.approvals, self.operations, self.events = {}, {}, []

    def _call(self, params):
        if not isinstance(params, dict) or set(params) != {"name", "arguments", "tool_call_id"}:
            raise ValueError("reference_invalid_call")
        name, arguments, call_id = params["name"], params["arguments"], params["tool_call_id"]
        if name not in self.allowed or not isinstance(arguments, dict):
            raise ValueError("reference_invalid_tool")
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
            raise ValueError("reference_invalid_call_id")
        self.tools._validate(name, arguments)
        if self.cancelled():
            raise AgentToolRejected("agent_cancelled")
        return name, arguments, call_id, fingerprint([name, arguments])

    def before_inference(self):
        """Every request, including upstream summaries/verifiers, needs this gate."""
        if self.cancelled():
            raise AgentToolRejected("agent_cancelled")
        if self.context is not None:
            self.tools.validate_context(self.context)

    def check_current_permissions(self):
        """No cancellation recursion and no cloud ancestry I/O in a watcher."""
        if self.context is not None:
            self.tools.validate_context(self.context, cheap_only=True)

    def approval(self, params):
        name, arguments, call_id, identity = self._call(params)
        if self.tools.metadata(name)["effect"] != "write" or call_id in self.operations:
            raise ValueError("reference_invalid_approval")
        prior = self.approvals.get(call_id)
        if prior:
            if prior["identity"] != identity:
                raise ValueError("reference_call_id_conflict")
            return {"approved": prior["approved"]}
        preview = self.tools.preview(name, arguments, context=self.context)
        request = {**copy.deepcopy(params), "preview": copy.deepcopy(preview)}
        if self.before_approval is not None:
            self.before_approval(self.session + ":" + call_id, request)
        approved = self.policy(copy.deepcopy(request)) is True
        if self.admission is not None:
            self.admission.approve(self.session + ":" + call_id, request, approved)
        self.approvals[call_id] = {"identity": identity, "approved": approved, "preview": copy.deepcopy(preview)}
        self.events.append({"type": "approval", **request, "approved": approved})
        return {"approved": approved}

    def execute(self, params):
        name, arguments, call_id, identity = self._call(params)
        metadata = self.tools.metadata(name)
        old = self.operations.get(call_id)
        if old:
            if old["identity"] != identity:
                raise ValueError("reference_call_id_conflict")
            # Resource read bodies are never replayed from cache after revocation.
            # Unknown writes are never dispatched twice.
            raise ValueError("reference_duplicate_dispatch")
        approval = self.approvals.get(call_id)
        preview = None
        if metadata["effect"] == "write":
            if not approval or not approval["approved"] or approval["identity"] != identity:
                raise AgentToolRejected("agent_approval_required")
            preview = approval["preview"]
            if self.tools.preview(name, arguments, context=self.context) != preview:
                raise AgentToolRejected("agent_approval_stale")
        operation = {"identity": identity, "state": "dispatched", "operation_id": self.session + ":" + call_id}
        if self.admission is not None and metadata["effect"] == "write":
            self.admission.admit(operation["operation_id"], {**copy.deepcopy(params), "preview": copy.deepcopy(preview)})
        self.operations[call_id] = operation
        options = {"operation_id": operation["operation_id"], "cancelled": self.cancelled}
        if metadata.get("scope_context_supported"):
            options["context"] = self.context
        if metadata.get("preview_precondition"):
            options["expected_preview"] = copy.deepcopy(preview)
        terminal = None
        try:
            result = self.tools.execute(name, arguments, **options)
        except AgentReadFailure as exc:
            if metadata["effect"] == "read" and str(exc) == "resource_read_timeout":
                operation["state"] = "failed"
                result = {"error": str(exc), "not_executed": False, "no_mutation": True,
                          **{key: safe_label(exc.connection.get(key)) for key in ("connection_id", "connection_name")}}
            else:
                operation["state"] = "unknown"
                result = {"error": "agent_tool_result_unknown"}
        except AgentToolRejected as exc:
            code = str(exc)
            if not re.fullmatch(r"agent_[a-z_]+", code):
                code = "agent_tool_rejected"
            operation["state"] = "not_executed"
            result = {"error": code, "not_executed": True}
            if code == "agent_cancelled":
                terminal = exc
        except AgentError as exc:
            # Meter failures must terminate the host, not become a correctable
            # model tool result. A dispatched write remains ambiguous.
            code = str(exc) if re.fullmatch(r"agent_[a-z_]+", str(exc)) else "agent_operation_failed"
            operation["state"] = "unknown" if metadata["effect"] == "write" else "failed"
            result = {"error": code}
            terminal = exc
        except Exception:
            operation["state"] = "unknown" if metadata["effect"] == "write" else "failed"
            result = {"error": "agent_tool_result_unknown" if metadata["effect"] == "write" else "agent_tool_failed"}
        else:
            operation["state"] = "returned"
            if self.context is not None and isinstance(result, dict) and isinstance(result.get("origin"), dict):
                origins = self.context.setdefault("resource_origins", [])
                if result["origin"] not in origins:
                    origins.append(copy.deepcopy(result["origin"]))
        if self.after_business is not None and metadata["effect"] == "write" and operation["state"] == "returned":
            self.after_business(operation["operation_id"], params)
        if self.admission is not None and metadata["effect"] == "write":
            self.admission.settle(operation["operation_id"], operation["state"], result)
        self.events.append({"type": "tool_result", **copy.deepcopy(params),
                            **copy.deepcopy(operation), "result": copy.deepcopy(result)})
        if terminal is not None:
            raise terminal
        return result
