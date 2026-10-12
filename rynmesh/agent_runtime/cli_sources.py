"""Bounded installed-CLI setup; no model-supplied commands, paths or credentials."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_json
from ..file_transactions import file_transaction
from ..llm_package.adapters import adapter_from_manifest
from ..llm_package.cli_adapter import CLIAgentAdapter
from ..llm_package.lifecycle import connect_cli
from .default_model import DefaultModelTool
from .resource_writes import ResourceWrites
from .store import AgentError, AgentToolRejected, encoded

DISCOVER = "ryn_discover_local_cli_sources"
CONNECT = "ryn_connect_local_cli_source"
TEST = "ryn_test_local_cli_source"
NAMES = {DISCOVER, CONNECT, TEST}
KINDS = ("codex_cli", "claude_cli", "antigravity_cli")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class CLISources:
    def __init__(self, app, home, services, ledger):
        self.app, self.services, self.ledger_provider = app, services, ledger
        self.path = Path(home) / "llm/cli-services.json" if home else None
        self.owner = DefaultModelTool(app)

    def check(self, context, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        runtime = self.owner.runtime()
        data = runtime.store.read()
        row = self.owner.row(runtime, data, context)
        if not data["settings"]["enabled"] or data["settings"]["permission_mode"] == "read_only" and context.get("_cli_write"):
            raise AgentToolRejected("agent_permission_denied")
        return row

    def read(self):
        if not self.path:
            raise AgentToolRejected("agent_cli_configuration_unavailable")
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise AgentToolRejected("agent_cli_configuration_invalid")
        return value

    def snapshot(self, kind):
        adapter = CLIAgentAdapter(kind)
        try:
            executable = Path(adapter._command_path())
            stat = executable.stat()
            executable_revision = digest([str(executable), stat.st_size, stat.st_mtime_ns])
        except (OSError, RuntimeError):
            executable_revision = "unavailable"
        return {"configuration_revision": digest(self.read().get(kind)), "executable_revision": executable_revision}

    def preview(self, name, args, context):
        self.check(context, lambda: False)
        kind = args.get("kind")
        if name == DISCOVER:
            return {"title": "Discover supported installed CLI sources"}
        if kind not in KINDS:
            raise AgentToolRejected("agent_cli_kind_invalid")
        state = self.snapshot(kind)
        entry = self.read().get(kind, {})
        return {"title": "Connect installed CLI (preserve existing settings)" if name == CONNECT else "Test CLI client tools",
                "kind": kind, **state, "sharing": bool(entry.get("publication_enabled")), "changes_sharing": False,
                "probe_data": "fixed synthetic messages only", "changes_default_model": False}

    def metered(self, adapter, context, cancelled):
        outer = self
        class Probe:
            def call(self, method, body, maximum, task_id, timeout_s):
                meter = context.get("_model_probe")
                if meter is None:
                    raise AgentToolRejected("agent_probe_budget_unavailable")
                def invoke(allowed, remaining, guard):
                    outer.check(context, cancelled)
                    done = threading.Event()
                    failure = []
                    def watch():
                        while not done.wait(.1):
                            try:
                                outer.check(context, cancelled)
                                guard()
                            except Exception as exc:
                                failure.append(exc)
                                adapter.cancel(task_id)
                                return
                    watcher = threading.Thread(target=watch, daemon=True)
                    watcher.start()
                    try:
                        if method == "infer":
                            result = adapter.infer(prompt=body, max_tokens=allowed, task_id=task_id,
                                                   timeout_s=min(timeout_s, remaining))
                        else:
                            result = adapter.chat({**body, "max_tokens": allowed}, task_id=task_id,
                                                  timeout_s=min(timeout_s, remaining))
                        if failure:
                            raise failure[0]
                        outer.check(context, cancelled)
                        guard()
                        return result
                    finally:
                        done.set()
                        watcher.join(timeout=.2)
                return meter(invoke, max_output_tokens=maximum, input_bytes=len(encoded(body)),
                             model_id="installed-cli", source_id="local-cli", purpose="local_cli_probe")
            def chat(self, body, *, task_id, timeout_s):
                return self.call("chat", body, min(body["max_tokens"], 4096), task_id, timeout_s)
        return Probe()

    def execute(self, name, args, *, context, operation_id, expected_preview, cancelled):
        context = {**context, "_cli_write": name == CONNECT}
        self.check(context, cancelled)
        if name == DISCOVER:
            value = self.services["cli_services"]()
            return {**value, "supports_connect": True, "supports_client_tool_test": True}
        ledger = self.ledger_provider()
        binding = ResourceWrites.binding(name, args, operation_id, context)
        saved = ledger.get(operation_id)
        if saved:
            if saved["binding"] != binding:
                raise AgentToolRejected("agent_operation_id_conflict")
            if saved["status"] == "confirmed":
                return copy.deepcopy(saved["result"])
            raise AgentError("agent_write_outcome_unknown")
        preview = self.preview(name, args, context)
        if name == CONNECT and expected_preview != preview:
            raise AgentToolRejected("agent_approval_preview_changed")
        prior, created = ledger.begin(binding, preview)
        if not created:
            if prior["status"] == "confirmed":
                return copy.deepcopy(prior["result"])
            raise AgentError("agent_write_outcome_unknown")
        kind = args["kind"]
        ledger.update(operation_id, "dispatched")
        if name == CONNECT:
            existing = self.read().get(kind)
            if existing and existing.get("manifest"):
                # Never reset an owner's existing sharing/configuration on repeat setup.
                result = {"configured": True, "kind": kind, "already_configured": True,
                          "model_id": "local/" + kind.replace("_", "-"),
                          "publication_enabled": bool(existing.get("publication_enabled")), "chat_probe": "not_run"}
            else:
                def self_test(manifest):
                    probe = self.metered(adapter_from_manifest(manifest), context, cancelled)
                    result = probe.call("infer", "Reply with exactly: RYNMESH SELF TEST OK", 64,
                                        "cli-selftest-" + uuid.uuid4().hex, 60)
                    if not str(result.get("text") or "").strip():
                        raise AgentToolRejected("agent_cli_chat_probe_failed")
                    return {"ok": True}
                connected = connect_cli(kind=kind, root=self.path.parent, cancel_check=cancelled, self_test_fn=self_test)
                result = {"configured": True, "kind": kind, "already_configured": False,
                          "model_id": "local/" + kind.replace("_", "-"), "publication_enabled": False, "chat_probe": "passed"}
                with file_transaction(self.path.with_suffix(".lock")):
                    self.check(context, cancelled)
                    if self.preview(name, args, context) != preview:
                        raise AgentToolRejected("agent_approval_preview_changed")
                    settings = self.read()
                    settings[kind] = {"manifest": connected["manifest"], "publication_enabled": False,
                                      "agent_operation": {"binding": binding, "result": result}}
                    atomic_write_json(self.path, settings)
                self.services["active_cli_managers"]()
        else:
            manager = self.services["active_cli_managers"]().get(kind)
            if manager is None:
                raise AgentToolRejected("agent_cli_not_configured")
            probe = self.metered(manager.adapter, context, cancelled)
            tested = self.services["probe_service_tools"](manager.manifest.package_id, probe)
            self.check(context, cancelled)
            result = {**tested, "kind": kind, "model_id": "local/" + manager.manifest.package_id,
                      "agent_usable": tested["tool_support"] == "supported",
                      "snapshot": self.snapshot(kind), "checked_at": datetime.now(timezone.utc).isoformat()}
        ledger.update(operation_id, "confirmed", result=result)
        return result

    def reconcile(self, operation_id, *, context, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        operation = context.get("operation") or {}
        record = self.ledger_provider().get(operation_id)
        if record and record["status"] != "confirmed":
            kind = operation.get("arguments", {}).get("kind")
            written = self.read().get(kind, {}).get("agent_operation", {})
            if written.get("binding") == record["binding"]:
                record = {**record, "status": "confirmed", "result": written["result"]}
        valid = bool(record and record["status"] == "confirmed" and
                     all(record["binding"].get(key) == context.get(key) for key in ("task_id", "owner_id", "consumer_node_id")) and
                     record["binding"]["tool"] == operation.get("tool") and
                     record["binding"]["arguments_sha256"] == operation.get("arguments_sha256") == digest(operation.get("arguments")))
        if not valid:
            return {"outcome": "unknown", "reason": "No attributable CLI receipt; no setup or probe was retried."}
        return {"outcome": "executed", "receipt": {"operation_id": operation_id, "tool": record["binding"]["tool"],
                "arguments_sha256": record["binding"]["arguments_sha256"], "result": record["result"]}}

    def verify(self, receipt, *, context, cancelled):
        operation = {key: receipt.get(key) for key in ("operation_id", "tool", "arguments_sha256", "arguments")}
        proof = self.reconcile(receipt["operation_id"], context={**context, "operation": operation}, cancelled=cancelled)
        proven = proof.get("outcome") == "executed" and proof["receipt"]["result"] == receipt.get("result")
        kind = receipt["arguments"]["kind"]
        result = receipt.get("result") or {}
        current = self.services["active_cli_managers"]().get(kind)
        match = bool(current and (receipt["tool"] != TEST or result.get("snapshot") == self.snapshot(kind)))
        if receipt["tool"] == CONNECT and proven:
            entry = self.read().get(kind, {})
            if result.get("already_configured"):
                record = self.ledger_provider().get(receipt["operation_id"])
                match = match and record["preview"]["configuration_revision"] == self.snapshot(kind)["configuration_revision"]
            else:
                marker = entry.get("agent_operation", {})
                match = match and marker.get("result") == result and marker.get("binding", {}).get("operation_id") == receipt["operation_id"]
            match = match and bool(entry.get("publication_enabled")) == result.get("publication_enabled")
        if receipt["tool"] == TEST:
            match = match and result.get("agent_usable") is True
        return {"status": "passed" if proven and match else "unknown", "checked_at": datetime.now(timezone.utc).isoformat(),
                "evidence": {"operation_attribution": "durable_cli_receipt" if proven else "not_established",
                             "kind": kind, "configuration_current": match, "scope": "local CLI setup/tool capability; not receiver connectivity"}}
