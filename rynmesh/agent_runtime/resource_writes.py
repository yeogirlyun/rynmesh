"""Narrow no-overwrite SMB/WebDAV creates with durable operation attribution."""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from ..nas import NasError, relative_path
from .operation_ledger import OperationLedger
from .resources import CREATE_PROTOCOLS
from .store import AgentError, AgentToolRejected, encoded

NAMES = {
    "ryn_create_text_file": "create_file",
    "ryn_upload_resource_file": "create_file",
    "ryn_create_directory": "create_directory",
}
MAX_TEXT_BYTES = 16 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def reject(code):
    raise AgentToolRejected(code)


class ResourceWrites:
    def __init__(self, resources, home):
        self.resources, self.home = resources, home
        self._ledger = None

    @property
    def ledger(self):
        if not self.home:
            reject("agent_operation_ledger_unavailable")
        if self._ledger is None:
            self._ledger = OperationLedger(self.home)
        return self._ledger

    def forget_task(self, task_id):
        if self.home and (Path(self.home) / "agent" / "business-operations.json").exists():
            self.ledger.forget_task(task_id)

    def target(self, name, args, context):
        item, grant = self.resources._resolve(args["directory_ref"], context, NAMES[name])
        if not item["directory"]:
            reject("agent_scope_directory_required")
        if not grant["recursive"] and item["path"] != self.resources._open(grant["root_resource_ref"])["path"]:
            reject("agent_resource_scope_denied")
        ws_path = args.get("workspace_path")
        if name == "ryn_upload_resource_file" or (name == "ryn_create_text_file" and ws_path):
            ws_root_raw = (context.get("local_execution") or {}).get("workspace_root") or ""
            if not ws_root_raw:
                reject("agent_workspace_required")
            try:
                ws_root = Path(ws_root_raw).resolve(strict=True)
                raw_rel = str(ws_path or "").strip()
                if not raw_rel:
                    reject("agent_file_name_invalid")
                ws_file = (ws_root / raw_rel).resolve(strict=True)
            except (OSError, ValueError):
                reject("agent_local_file_not_found")
            if not ws_file.is_relative_to(ws_root) or not ws_file.is_file():
                reject("agent_local_path_outside_workspace")
            filename = args.get("name") or ws_file.name
            try:
                if relative_path(filename) != filename or "/" in filename or not filename or len(filename) > 128:
                    reject("agent_file_name_invalid")
            except NasError:
                reject("agent_file_name_invalid")
            try:
                if ws_file.stat().st_size > MAX_UPLOAD_BYTES:
                    reject("agent_resource_size_limit")
                data = ws_file.read_bytes()
            except OSError:
                reject("agent_local_file_not_found")
            if len(data) > MAX_UPLOAD_BYTES:
                reject("agent_resource_size_limit")
        else:
            filename = args["name"]
            try:
                if relative_path(filename) != filename or "/" in filename or not filename or len(filename) > 128:
                    reject("agent_file_name_invalid")
            except NasError:
                reject("agent_file_name_invalid")
            if name == "ryn_create_text_file" and PurePosixPath(filename).suffix.lower() not in {".txt", ".md"}:
                reject("agent_resource_format_unsupported")
            data = args.get("content", "").encode("utf-8")
            if len(data) > MAX_TEXT_BYTES:
                reject("agent_resource_size_limit")
        with self.resources.nas.lock:
            source = self.resources._source(grant["connection_id"])
            if source["protocol"] not in CREATE_PROTOCOLS:
                reject("agent_resource_create_backend_unsupported")
            if not source.get("writable"):
                reject("agent_resource_read_only")
            if source["protocol"] == "webdav" and "%" in filename:
                reject("agent_file_name_invalid")
        path = (item["path"] + "/" if item["path"] else "") + filename
        return item, grant, path, data

    def preview(self, name, args, context):
        _item, grant, path, data = self.target(name, args, context)
        filename = PurePosixPath(path).name
        if name == "ryn_upload_resource_file":
            return {"title": "Upload workspace file to drive",
                    "directory_ref": args["directory_ref"], "name": filename,
                    "workspace_path": args["workspace_path"], "overwrite": False,
                    "connection_id": grant["connection_id"], "connection_access_generation": grant["connection_access_generation"],
                    "scope_revision": context["scope_revision"], "grant_id": grant["grant_id"],
                    "content_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                    "action": NAMES[name]}
        return {"title": "Create a new text file" if name == "ryn_create_text_file" else "Create a new directory",
                "directory_ref": args["directory_ref"], "name": filename, "overwrite": False,
                "connection_id": grant["connection_id"], "connection_access_generation": grant["connection_access_generation"],
                "scope_revision": context["scope_revision"], "grant_id": grant["grant_id"],
                "content_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "proposed_content": args.get("content", ""), "action": NAMES[name]}

    @staticmethod
    def binding(name, args, operation_id, context):
        return {"operation_id": operation_id, "tool": name, "arguments_sha256": hashlib.sha256(encoded(args)).hexdigest(),
                "task_id": context["task_id"], "scope_revision": context["scope_revision"],
                "goal_revision": context["goal_revision"], "owner_id": context["owner_id"], "consumer_node_id": context["consumer_node_id"],
                "scope_sha256": hashlib.sha256(encoded(context["resource_grants"])).hexdigest()}

    def execute(self, name, args, *, operation_id, context, expected_preview, cancelled):
        # Acquire before target()/NAS locks; operate() may take a nested lease.
        # This keeps disable/uninstall from racing a dispatched approved write.
        try:
            with self.resources.nas.access():
                return self._execute(name, args, operation_id=operation_id, context=context,
                                     expected_preview=expected_preview, cancelled=cancelled)
        except NasError as exc:
            if str(exc) in {"plugin_disabled", "plugin_busy", "plugin_not_installed", "craft_build_not_verified"}:
                reject("agent_resource_permission_changed")
            raise

    def _execute(self, name, args, *, operation_id, context, expected_preview, cancelled):
        if cancelled():
            reject("agent_cancelled")
        preview = self.preview(name, args, context)
        if expected_preview != preview:
            reject("agent_approval_preview_changed")
        item, grant, path, data = self.target(name, args, context)
        is_file = name in {"ryn_create_text_file", "ryn_upload_resource_file"}
        filename = PurePosixPath(path).name
        reference = self.resources._seal({"path": path, "directory": not is_file,
                     **{k: item[k] for k in ("grant_id", "owner_id", "consumer_node_id")}, "task_id": context["task_id"]})
        result = {"ok": True, "operation_id": operation_id, "name": filename, "resource_ref": reference,
                  "created": "file" if is_file else "directory", "overwrite": False,
                  "bytes": len(data), "content_sha256": hashlib.sha256(data).hexdigest(), "business_acknowledged": True}
        if len(encoded(result)) > 3072:
            reject("agent_resource_output_limit")  # Fits the ledger's 4096-byte receipt reservation.
        binding = self.binding(name, args, operation_id, context)
        try:
            row, created = self.ledger.begin(binding, preview)
        except AgentToolRejected:
            raise
        except (AgentError, OSError):
            reject("agent_operation_ledger_unavailable")
        if not created:
            if row["status"] == "confirmed":
                return row["result"]
            if row["status"] == "not_executed":
                reject(row["error"])
            raise AgentError("agent_write_outcome_unknown")
        plugin = self.resources.nas
        with plugin.lock:
            gate = plugin.source_locks.setdefault(grant["connection_id"], threading.RLock())
        # Match NAS lock order (source gate, then configuration lock). Keep the
        # approved generation stable through this one bounded network request.
        # A control change after dispatch cannot undo an already sent write.
        with gate, plugin.lock:
            try:
                self.resources.validate_context(context, cheap_only=True)
                if cancelled():
                    reject("agent_cancelled")
            except AgentToolRejected as exc:
                self.ledger.update(operation_id, "not_executed", error=str(exc))
                raise
            try:
                self.ledger.update(operation_id, "dispatched")
            except (AgentError, OSError):
                reject("agent_operation_ledger_unavailable")
            try:
                plugin.operate(grant["connection_id"], "write" if is_file else "mkdir", path, data)
            except NasError as exc:
                if str(exc) in {"file_exists", "file_exists_or_parent_missing"}:
                    code = "agent_resource_already_exists" if str(exc) == "file_exists" else "agent_resource_create_conflict"
                    self.ledger.update(operation_id, "not_executed", error=code)
                    reject(code)
                raise  # Connection loss after a remote commit stays unknown.
        # This durable business receipt precedes the Agent task transaction.
        self.ledger.update(operation_id, "confirmed", result=result, confirmed_at=datetime.now(timezone.utc).isoformat())
        return result

    def reconcile(self, operation_id, *, context, cancelled):
        if cancelled():
            reject("agent_cancelled")
        operation = context.get("operation") or {}
        if operation.get("operation_id") != operation_id or operation.get("tool") not in NAMES:
            return {"outcome": "unknown"}
        row = self.ledger.get(operation_id)
        if row is None:
            return {"outcome": "unknown"}
        expected = self.binding(operation["tool"], operation["arguments"], operation_id, context)
        if row["binding"] != expected or expected["arguments_sha256"] != operation.get("arguments_sha256"):
            return {"outcome": "unknown"}
        if row["status"] == "confirmed":
            return {"outcome": "executed", "receipt": {"operation_id": operation_id, "tool": operation["tool"],
                    "arguments_sha256": operation["arguments_sha256"], "result": row["result"]}}
        if row["status"] == "not_executed":
            return {"outcome": "not_executed", "receipt": {"operation_id": operation_id, "tool": operation["tool"],
                    "arguments_sha256": operation["arguments_sha256"], "result": {"not_executed": True, "error": row["error"]}}}
        return {"outcome": "unknown", "reason": "No confirmed business acknowledgement; no write was retried."}

    def verify(self, receipt, *, context, cancelled):
        if cancelled():
            reject("agent_cancelled")
        name, args = receipt["tool"], receipt["arguments"]
        item, grant, path, data = self.target(name, args, context)
        if name in {"ryn_create_text_file", "ryn_upload_resource_file"}:
            # This exact newly-created object is read for verification. Never
            # expose its raw bytes to the model as a bypass of read_ai.
            read_limit = (MAX_TEXT_BYTES if name == "ryn_create_text_file" and not args.get("workspace_path") else len(data)) + 1
            plugin = self.resources.nas
            with plugin.lock:
                source = self.resources._source(grant["connection_id"])
                gate = plugin.source_locks.setdefault(source["id"], threading.RLock())
            with gate:
                with plugin.lock:
                    if self.resources._source(source["id"]) is not source:
                        reject("agent_resource_permission_changed")
                with plugin.connection(source, reuse=True) as connector:
                    raw = connector.read(path, read_limit)
            matches = raw == data
            evidence = {"content_sha256": hashlib.sha256(raw).hexdigest(), "expected_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(raw)}
        else:
            page = self.resources.nas.operate(grant["connection_id"], "list", item["path"])
            matches = any(entry["path"] == path and entry["directory"] for entry in page)
            evidence = {"directory_exists": matches}
        self.resources.validate_context(context)
        if cancelled():
            reject("agent_cancelled")
        return {"status": "passed" if matches else "failed", "evidence": {**evidence, "current_state_matches": matches,
                "operation_attribution": "not_established_by_state_readback"}, "checked_at": datetime.now(timezone.utc).isoformat()}
