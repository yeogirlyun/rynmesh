"""Consumer-owned app tools; even private reads require reviewed data release."""

from datetime import datetime, timezone

from ..agent_runtime.store import AgentToolRejected, encoded
from .catalog_data import CATALOG
from .engine import AppError
from .service import fingerprint, preview_for

ACTION = "ryn_node_app_action"
LIST = "ryn_list_node_apps"
NAMES = {ACTION, LIST}
SPECS = [
    {
        "type": "function",
        "function": {
            "name": LIST,
            "description": "List explicitly shared free node apps, including Resume, Recruit, Desk and the Craft editor catalog. Craft editor_info describes pinned builds; it cannot edit documents or install/launch software. Contains capabilities, never private workspace records.",
            "parameters": {
                "type": "object",
                "properties": {"provider_peer": {"type": "string", "maxLength": 128}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ACTION,
            "description": "Read or change one node-app workspace using a listed operation. Stages exact owner approval, including private reads and automatic mode. Optional reviewed_context pins a specific workspace revision; if omitted, the current workspace context is resolved automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider_peer": {"type": "string", "maxLength": 128},
                    "app": {"enum": list(CATALOG)},
                    "action": {"type": "string", "maxLength": 128},
                    "arguments": {"type": "object"},
                    "reviewed_context": {
                        "type": "object",
                        "properties": {
                            "app": {"enum": list(CATALOG)},
                            "provider_peer": {"type": "string"},
                            "consumer_peer": {"type": "string"},
                            "access_generation": {"type": "string"},
                            "revision": {"type": "integer", "minimum": 0},
                            "state_hash": {"type": "string"},
                        },
                        "required": [
                            "app",
                            "provider_peer",
                            "consumer_peer",
                            "access_generation",
                            "revision",
                            "state_hash",
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": ["app", "action", "arguments"],
                "additionalProperties": False,
            },
        },
    },
]
VERIFIER = {
    "id": "node_app_receipt_state",
    "tool_name": ACTION,
    "kind": "mechanical",
    "effect": "write",
    "description": "Verify original app receipt and unchanged current workspace revision; no new write or data release.",
    "target_fields": ["app", "action"],
}


class AppTools:
    def __init__(self, app):
        self.app = app

    @property
    def service(self):
        service = getattr(self.app.state, "node_apps", None)
        if service is None:
            raise AgentToolRejected("agent_node_apps_unavailable")
        return service

    def preview(self, args, context):
        if not isinstance(context, dict) or context.get("consumer_node_id") != self.service.peer_id:
            raise AgentToolRejected("agent_node_apps_context_required")
        try:
            return {
                "title": "Review app operation and model data release",
                "app_preview": self.service.preview(
                    args.get("provider_peer", ""),
                    args["app"],
                    args["action"],
                    args["arguments"],
                    args.get("reviewed_context"),
                ),
                "model_data_recipient": context["model_destination"],
            }
        except AppError as exc:
            raise AgentToolRejected(str(exc)) from None

    def execute(self, name, args, operation_id, cancelled, expected_preview=None, context=None):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        if name == LIST:
            try:
                return self.service.call(args.get("provider_peer", ""), "catalog", {})
            except AppError as exc:
                raise AgentToolRejected(str(exc)) from None
        preview = self.preview(args, context)
        if preview != expected_preview:
            raise AgentToolRejected("agent_approval_preview_changed")
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        try:
            return self.service.invoke(
                args.get("provider_peer", ""),
                {
                    "app": args["app"],
                    "action": args["action"],
                    "arguments": args["arguments"],
                    "operation_id": operation_id,
                    "preview_hash": preview["app_preview"]["preview_hash"],
                },
            )
        except AppError as exc:
            # These failures leave the consumer intent pending in NodeApps.
            # A provider may already have committed; never claim no mutation
            # or discard the original operation's reconciliation identity.
            if str(exc) in {"apps_response_unconfirmed", "apps_response_invalid", "apps_store_unavailable"}:
                from ..agent_runtime.store import AgentError

                raise AgentError("agent_write_outcome_unknown") from None
            raise AgentToolRejected(str(exc)) from None

    def _receipt(self, args, operation_id):
        receipt = self.service.call(
            args.get("provider_peer", ""),
            "receipt",
            {"app": args["app"], "operation_id": operation_id},
        )
        if (
            not receipt
            or receipt.get("consumer_peer") != self.service.peer_id
            or receipt.get("provider_peer") != (args.get("provider_peer") or self.service.peer_id)
            or receipt.get("app") != args["app"]
            or receipt.get("action") != args["action"]
            or receipt.get("arguments_hash") != fingerprint(args["arguments"])
        ):
            return None
        reviewed = args.get("reviewed_context")
        if reviewed is not None:
            expected_req_hash = fingerprint(
                {
                    "app": args["app"],
                    "action": args["action"],
                    "arguments": args["arguments"],
                    "operation_id": operation_id,
                    "preview_hash": preview_for(reviewed, args["action"], args["arguments"])["preview_hash"],
                }
            )
            if receipt.get("request_hash") != expected_req_hash:
                return None
        return receipt

    def reconcile(self, operation_id, context, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        operation = context.get("operation") or {}
        if operation.get("tool") != ACTION or operation.get("operation_id") != operation_id:
            return {"outcome": "unknown"}
        args = operation["arguments"]
        import hashlib

        if hashlib.sha256(encoded(args)).hexdigest() != operation.get("arguments_sha256"):
            return {"outcome": "unknown"}
        try:
            receipt = self._receipt(args, operation_id)
            if receipt:
                reviewed = args.get("reviewed_context")
                preview_hash = (
                    preview_for(reviewed, args["action"], args["arguments"])["preview_hash"]
                    if reviewed is not None
                    else ((operation.get("preview") or {}).get("app_preview") or {}).get("preview_hash", "")
                )
                command = {"app": args["app"], "action": args["action"], "arguments": args["arguments"],
                           "operation_id": operation_id, "preview_hash": preview_hash}
                receipt = self.service.confirm_receipt(args.get("provider_peer", ""), command, receipt)
        except (AppError, ValueError, OSError, TypeError, KeyError, AttributeError):
            return {"outcome": "unknown"}
        return (
            {
                "outcome": "executed",
                "receipt": {
                    "operation_id": operation_id,
                    "tool": ACTION,
                    "arguments_sha256": operation["arguments_sha256"],
                    "result": receipt,
                },
            }
            if receipt
            else {"outcome": "unknown"}
        )

    def verify(self, receipt, context, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        args = receipt["arguments"]
        try:
            original = self._receipt(args, receipt["operation_id"])
            current = self.service.call(
                args.get("provider_peer", ""), "context", {"app": args["app"]}
            )
            passed = original and current["revision"] == original["revision"]
        except (AppError, ValueError, OSError):
            passed = False
        return {
            "status": "passed" if passed else "unknown",
            "evidence": {
                "operation_id": receipt["operation_id"],
                "original_receipt_and_revision_match": bool(passed),
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
