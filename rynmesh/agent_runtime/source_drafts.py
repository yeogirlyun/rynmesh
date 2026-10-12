"""Owner-bound credentials and tested source drafts; no model-visible secrets."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from pathlib import Path

from ..atomic_io import atomic_write_bytes
from ..file_transactions import file_transaction
from ..llm_package.adapters import AdapterError, validate_local_url
from .resource_writes import ResourceWrites
from .store import AgentError, AgentToolRejected, encoded

MAX_BYTES = 4 * 1024 * 1024
CONFIG_FIELDS = {"name", "kind", "provider", "base_url", "models", "context_window", "max_output_tokens", "request_defaults", "enabled"}
NAMES = {"ryn_prepare_source_draft", "ryn_test_source_draft", "ryn_commit_source_draft"}


def reject(code):
    raise AgentToolRejected(code)


def source_revision(row, sources):
    # Legacy stores use ciphertext identity, never a plaintext-key digest.
    return row.get("source_revision") or hashlib.sha256(sources.path.read_bytes()).hexdigest()


class SourceDrafts:
    def __init__(self, sources, home, ledger, services):
        self.sources, self.home, self.ledger_provider, self.services = sources, Path(home) if home else None, ledger, services
        self.path = self.home / "agent/source-drafts.enc" if self.home else None

    def _read(self):
        if not self.path or not self.sources:
            reject("agent_source_configuration_unavailable")
        if not self.path.exists():
            return {"credentials": {}, "drafts": {}, "requests": {}, "hmac_key": secrets.token_hex(32)}
        try:
            raw = self.path.read_bytes()
            if len(raw) > MAX_BYTES * 2:
                raise ValueError
            value = json.loads(self.sources.cipher.decrypt(raw))
            if len(encoded(value)) > MAX_BYTES:
                raise ValueError
            return value
        except Exception:
            raise AgentError("agent_source_drafts_unreadable") from None

    def _write(self, value):
        raw = encoded(value)
        if len(raw) > MAX_BYTES or len(value["drafts"]) > 128 or len(value["credentials"]) > 128 or len(value["requests"]) > 512:
            reject("agent_source_draft_capacity")
        atomic_write_bytes(self.path, self.sources.cipher.encrypt(raw), max_bytes=MAX_BYTES * 2)

    def _lock(self):
        if not self.path or not self.sources:
            reject("agent_source_configuration_unavailable")
        return file_transaction(self.path.with_suffix(".lock"))

    @staticmethod
    def _binding(context):
        return {key: context[key] for key in ("task_id", "owner_id", "consumer_node_id", "goal_revision")}

    @staticmethod
    def _endpoint(body):
        kind = body.get("kind")
        if kind not in {"api", "local"}:
            reject("agent_source_configuration_invalid")
        try:
            endpoint = validate_local_url(body.get("base_url", ""), allow_non_loopback=kind == "api")
        except (ValueError, TypeError, AttributeError):
            reject("agent_source_configuration_invalid")
        if kind == "api" and not endpoint.startswith("https://"):
            reject("agent_source_configuration_invalid")
        return kind, endpoint

    def credential(self, task_id, body, *, owner_id, consumer_node_id, goal_revision, revoke=False):
        allowed = {"client_request_id", "kind", "base_url", "source_id"} | (set() if revoke else {"api_key", "reuse_existing", "expected_source_revision"})
        if not isinstance(body, dict) or set(body) - allowed:
            reject("agent_source_credential_invalid")
        request_id = body.get("client_request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            reject("agent_source_credential_invalid")
        kind, endpoint = self._endpoint(body)
        source_id = body.get("source_id", "")
        if not isinstance(source_id, str) or len(source_id) > 128 or type(body.get("reuse_existing", False)) is not bool:
            reject("agent_source_credential_invalid")
        key = body.get("api_key", "")
        reused_source_revision = None
        if not revoke:
            if body.get("reuse_existing"):
                if "api_key" in body or not source_id:
                    reject("agent_source_credential_invalid")
                with self.sources.lock:
                    old = self.sources.read().get(source_id)
                    if not old or old["kind"] != kind or old["base_url"] != endpoint:
                        reject("agent_source_credential_destination_mismatch")
                    reused_source_revision = source_revision(old, self.sources)
                    if body.get("expected_source_revision") is not None and body["expected_source_revision"] != reused_source_revision:
                        reject("agent_source_revision_conflict")
                    key = old.get("api_key", "")
            if not isinstance(key, str) or not 1 <= len(key) <= 8192 or any(c in key for c in "\r\n"):
                reject("agent_source_credential_invalid")
        binding = {"task_id": task_id, "owner_id": owner_id, "consumer_node_id": consumer_node_id, "goal_revision": goal_revision}
        slot = {**binding, "source_id": source_id, "kind": kind, "base_url": endpoint}
        slot_id = hashlib.sha256(encoded(slot)).hexdigest()
        with self._lock():
            state = self._read()
            fingerprint = hmac.new(bytes.fromhex(state["hmac_key"]), encoded({"slot": slot, "key": key, "revoke": revoke,
                                    "reused_source_revision": reused_source_revision}), hashlib.sha256).hexdigest()
            rid = task_id + ":" + request_id
            prior = state["requests"].get(rid)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    reject("agent_source_credential_request_conflict")
                return copy.deepcopy(prior["result"])
            version = uuid.uuid4().hex
            result = {"has_credential": not revoke, "kind": kind, "base_url": endpoint, "source_id": source_id,
                      "credential_version": version, "expires_at": time.time() + 3600 if not revoke else time.time()}
            state["credentials"][slot_id] = {**slot, **result, "api_key": key if not revoke else "", "revoked": revoke,
                                             "reused_source_revision": reused_source_revision}
            state["requests"][rid] = {"task_id": task_id, "fingerprint": fingerprint, "result": result}
            self._write(state)
            return copy.deepcopy(result)

    def _credential(self, state, context, config, source_id):
        slot = {**self._binding(context), "source_id": source_id, "kind": config["kind"], "base_url": config["base_url"]}
        value = state["credentials"].get(hashlib.sha256(encoded(slot)).hexdigest())
        if value and not value["revoked"] and value["expires_at"] > time.time():
            if value.get("reused_source_revision"):
                current = self.sources.read().get(source_id)
                if not current or source_revision(current, self.sources) != value["reused_source_revision"]:
                    reject("agent_source_reused_credential_stale")
            return value
        if config["kind"] == "api":
            reject("agent_source_owner_credential_required")
        return None

    def _draft(self, state, args, context):
        row = state["drafts"].get(args.get("draft_id"))
        if not row or row["binding"] != self._binding(context):
            reject("agent_source_draft_not_found")
        if row["version"] != args.get("expected_draft_version"):
            reject("agent_source_draft_version_conflict")
        credential = self._credential(state, context, row["configuration"], row["source_id"])
        if row["credential_version"] != (credential or {}).get("credential_version", ""):
            reject("agent_source_credential_changed")
        return row, credential

    @staticmethod
    def public(row):
        value = {key: copy.deepcopy(row.get(key)) for key in ("draft_id", "version", "source_id", "configuration", "has_credential", "test", "committed_source_id")}
        if value.get("test"):
            value["test"].pop("credential_version", None)
        value["draft_only"] = True
        return value

    def list_sources(self):
        # Keep the legacy ciphertext generation and its fields in one snapshot.
        with self.sources.lock:
            return {"sources": [{**{k: copy.deepcopy(row[k]) for k in CONFIG_FIELDS if k in row}, "source_id": row["id"],
                                 "has_credential": bool(row.get("api_key")), "source_revision": source_revision(row, self.sources),
                                 "sharing": self.sources.shared_models(row),
                                 "status": row.get("status"), "tool_support": row.get("tool_support", {})}
                                for row in self.sources.read().values()]}

    def _prepare(self, args, context):
        config = args.get("configuration")
        if not isinstance(config, dict) or set(config) - CONFIG_FIELDS or not {"name", "kind", "base_url", "models"} <= set(config):
            reject("agent_source_configuration_invalid")
        if not isinstance(config["models"], list) or not 1 <= len(config["models"]) <= 8:
            reject("agent_source_configuration_invalid")
        source_id = args.get("source_id", "")
        if not isinstance(source_id, str):
            reject("agent_source_configuration_invalid")
        kind, endpoint = self._endpoint(config)
        with self.sources.lock, self._lock():
            state = self._read()
            old = self.sources.read().get(source_id)
            if source_id and not old:
                reject("agent_source_not_found")
            config = {**config, "kind": kind, "base_url": endpoint}
            credential = self._credential(state, context, config, source_id)
            try:
                record = self.sources.prepare({**config, "api_key": (credential or {}).get("api_key", ""),
                                               "clear_key": not credential}, source_id)
            except (ValueError, TypeError, OverflowError):
                reject("agent_source_configuration_invalid")
            record["api_key"] = ""
            row = {"draft_id": args.get("draft_id") or "draft_" + uuid.uuid4().hex, "version": 1,
                   "binding": self._binding(context), "source_id": source_id,
                   "expected_source_revision": source_revision(old, self.sources) if old else None,
                   "configuration": {k: record[k] for k in CONFIG_FIELDS}, "has_credential": bool(credential),
                   "credential_version": (credential or {}).get("credential_version", ""), "test": None}
            if args.get("draft_id"):
                prior, _ = self._draft(state, args, context)
                if prior.get("committed_source_id") or prior["source_id"] != source_id:
                    reject("agent_source_draft_immutable")
                row["version"] = prior["version"] + 1
            return state, row

    def preview(self, name, args, context):
        if name == "ryn_prepare_source_draft":
            _state, row = self._prepare(args, context)
            return {"title": "Prepare source configuration draft", "source_id": row["source_id"], "configuration": row["configuration"],
                    "has_credential": row["has_credential"], "draft_version": row["version"], "active_configuration_changes": False,
                    "credential_version": row["credential_version"], "expected_source_revision": row["expected_source_revision"]}
        with self.sources.lock, self._lock():
            row, _ = self._draft(self._read(), args, context)
            return {"title": "Test source draft" if name == "ryn_test_source_draft" else "Save tested source draft",
                    "draft_id": row["draft_id"], "source_id": row["source_id"], "version": row["version"], "configuration": row["configuration"],
                    "has_credential": row["has_credential"], "credential_version": row["credential_version"],
                    "expected_source_revision": row["expected_source_revision"], "test": row["test"],
                    "probe_requests_maximum": len(row["configuration"]["models"]) * (3 if args.get("test_tools") else 1) if name == "ryn_test_source_draft" else 0,
                    "post_save_probe_requests_maximum": len(row["configuration"]["models"]) if name == "ryn_commit_source_draft" else 0}

    def _record(self, row, credential):
        old = self.sources.read().get(row["source_id"]) if row["source_id"] else None
        if (source_revision(old, self.sources) if old else None) != row["expected_source_revision"]:
            reject("agent_source_revision_conflict")
        record = self.sources.prepare({**row["configuration"], "api_key": (credential or {}).get("api_key", ""),
                                       "clear_key": not credential}, row["source_id"])
        record["tool_support"] = {}
        return record

    def _probe(self, record, *, test_tools, cancelled, authorize=lambda: None, progress=None, meter=None):
        if not callable(meter):
            raise AgentError("agent_probe_budget_unavailable")
        progress = progress if progress is not None else {"requests": 0}
        evidence = []
        for model in record["models"]:
            if cancelled():
                reject("agent_cancelled")
            adapter = self.sources.adapter(record, model)
            class BoundedAdapter:
                def __init__(self, inner, model_id):
                    self.inner, self.model_id = inner, model_id

                def chat(self, body, *, task_id, timeout_s):
                    if cancelled():
                        reject("agent_cancelled")
                    def invoke(allowed, remaining_seconds, budget_guard):
                        probe_started = time.monotonic()
                        if cancelled():
                            reject("agent_cancelled")
                        budget_guard()
                        authorize()  # Recheck after admission, immediately before dispatch.
                        budget_guard()
                        if not callable(getattr(self.inner, "cancel", None)):
                            raise AgentError("agent_probe_cancellation_unavailable")
                        if remaining_seconds <= 0:
                            raise AgentError("agent_time_budget_exhausted")
                        done = threading.Event()
                        failure = []
                        def check():
                            if cancelled():
                                reject("agent_cancelled")
                            budget_guard()
                            authorize()
                        def watch():
                            while not done.wait(0.05):
                                try:
                                    check()
                                except Exception as exc:
                                    failure.append(exc if isinstance(exc, AgentError) else AgentError("agent_probe_guard_failed"))
                                    try:
                                        self.inner.cancel(task_id)
                                    except Exception:
                                        pass
                                    return
                        watcher = threading.Thread(target=watch, name="ryn-source-probe-cancel", daemon=True)
                        watcher.start()
                        try:
                            try:
                                timeout = min(15, timeout_s, remaining_seconds - (time.monotonic() - probe_started))
                                if timeout <= 0:
                                    raise AgentError("agent_time_budget_exhausted")
                                budget_guard()
                                progress["requests"] += 1
                                result = self.inner.chat({**body, "max_tokens": allowed}, task_id=task_id,
                                                         timeout_s=timeout)
                            except Exception:
                                if failure:
                                    raise failure[0] from None
                                check()  # Cancellation/withdrawal wins over transport errors.
                                raise
                            if failure:
                                raise failure[0]
                            check()
                            return result
                        finally:
                            done.set()
                            watcher.join(timeout=0.2)
                    from ..llm_package.model_settings import effective
                    maximum = min(body["max_tokens"], effective(record, self.model_id)["max_output_tokens"])
                    result = meter(invoke, max_output_tokens=maximum, input_bytes=len(encoded(body)),
                                   model_id=self.model_id, source_id=record.get("id", ""))
                    if cancelled():
                        reject("agent_cancelled")
                    return result
            bounded = BoundedAdapter(adapter, model)
            item = {"model_id": model, "chat": "failed", "tool_support": "unknown", "checked_at": time.time()}
            try:
                response = bounded.chat({"messages": [{"role": "user", "content": "Reply briefly: OK"}],
                                         "max_tokens": min(1024, record["max_output_tokens"])},
                                        task_id="source-draft-test-" + uuid.uuid4().hex, timeout_s=15)
                if not response.get("text"):
                    raise ValueError()
                item["chat"] = "passed"
                if test_tools:
                    self.sources.probe_adapter_tools(bounded, min(2048, record["max_output_tokens"]))
                    item["tool_support"] = "supported"
            except AgentError:
                raise
            except (AdapterError, ValueError, OSError, KeyError, TypeError):
                item["error_code"] = "agent_source_tool_probe_failed" if item["chat"] == "passed" else "agent_source_chat_probe_failed"
                if test_tools:
                    item["tool_support"] = "unsupported"
            evidence.append(item)
        return {"chat_passed": all(e["chat"] == "passed" for e in evidence), "models": evidence,
                "checked_at": time.time(), "probe_performed": progress["requests"] > 0, "probe_requests": progress["requests"],
                "usage": "unknown", "cost": "unknown"}

    def execute(self, name, args, *, context, operation_id, expected_preview, cancelled):
        if cancelled():
            reject("agent_cancelled")
        preview = self.preview(name, args, context)
        if name == "ryn_commit_source_draft" and preview != expected_preview or expected_preview is not None and preview != expected_preview:
            reject("agent_approval_preview_changed")
        ledger = self.ledger_provider()
        binding = ResourceWrites.binding(name, args, operation_id, context)
        previous, created = ledger.begin(binding, preview, receipt_reserve=24576)
        if not created:
            if previous["status"] == "confirmed":
                return previous["result"]
            if previous["status"] == "not_executed":
                reject(previous["error"])
            raise AgentError("agent_write_outcome_unknown")
        dispatched = False
        progress = {"requests": 0}
        try:
            if name == "ryn_prepare_source_draft":
                with self.sources.lock, self._lock():
                    if cancelled() or self.preview(name, args, context) != preview:
                        reject("agent_source_draft_precondition_changed")
                    state, row = self._prepare(args, context)
                    state["drafts"][row["draft_id"]] = row
                    ledger.update(operation_id, "dispatched")
                    dispatched = True
                    self._write(state)
                    result = self.public(row)
            elif name == "ryn_test_source_draft":
                with self.sources.lock, self._lock():
                    row, credential = self._draft(self._read(), args, context)
                    record = self._record(row, credential)
                    snapshot = copy.deepcopy(row)
                ledger.update(operation_id, "dispatched")
                dispatched = True
                def authorize_probe():
                    with self.sources.lock, self._lock():
                        current, current_credential = self._draft(self._read(), args, context)
                        if current != snapshot:
                            reject("agent_source_draft_version_conflict")
                        self._record(current, current_credential)
                test = self._probe(record, test_tools=args.get("test_tools", False), cancelled=cancelled,
                                   authorize=authorize_probe, progress=progress, meter=context.get("_model_probe"))
                with self.sources.lock, self._lock():
                    state = self._read()
                    current, credential = self._draft(state, args, context)
                    self._record(current, credential)
                    if current != snapshot or cancelled():
                        reject("agent_source_test_stale")
                    test["draft_version"] = row["version"]
                    test["credential_version"] = row["credential_version"]
                    current["test"] = test
                    self._write(state)
                    result = self.public(current)
            else:
                with self.sources.lock, self._lock():
                    state = self._read()
                    row, credential = self._draft(state, args, context)
                    if self.preview(name, args, context) != expected_preview or cancelled():
                        reject("agent_source_draft_precondition_changed")
                    if row.get("committed_source_id"):
                        reject("agent_source_draft_immutable")
                    test = row.get("test") or {}
                    if (not test.get("chat_passed") or test.get("draft_version") != row["version"] or
                            test.get("credential_version") != row["credential_version"] or time.time() - test.get("checked_at", 0) > 900):
                        reject("agent_source_test_required")
                    record = self._record(row, credential)
                    record.update(status="ready", tool_support={e["model_id"]: e["tool_support"] for e in test["models"]})
                    ledger.update(operation_id, "dispatched")
                    dispatched = True
                    saved = self.sources.save(record)
                    row["committed_source_id"] = saved["id"]
                    row["committed_revision"] = saved["source_revision"]
                    if credential and credential.get("reused_source_revision"):
                        # Only this exact approved CAS commit may advance the
                        # reused-key binding. External rotations never do so.
                        credential["reused_source_revision"] = saved["source_revision"]
                    self._write(state)
                    result = {"ok": True, "source_id": saved["id"], "source_revision": saved["source_revision"],
                              "models": saved["models"], "shared_models": saved["shared_models"], "status": saved["status"],
                              "post_save_retest": "pending", "draft_id": row["draft_id"], "version": row["version"]}
            ledger.update(operation_id, "confirmed", result=result, confirmed_at=time.time())
        except AgentError as exc:
            if not isinstance(exc, AgentToolRejected) and not str(exc).endswith("_budget_exhausted"):
                raise
            if name == "ryn_test_source_draft":
                with self._lock():
                    state = self._read()
                    current = state["drafts"].get(args["draft_id"])
                    if current and current["binding"] == self._binding(context) and current["version"] == args["expected_draft_version"]:
                        current["test"] = None
                        self._write(state)
                result = {"draft_id": args["draft_id"], "version": args["expected_draft_version"], "draft_only": True,
                          "test": {"chat_passed": False, "status": "interrupted", "evidence_valid": False, "error_code": str(exc),
                                   "probe_performed": progress["requests"] > 0, "probe_requests": progress["requests"], "usage": "unknown", "cost": "unknown"}}
                ledger.update(operation_id, "confirmed", result=result, confirmed_at=time.time())
                if not isinstance(exc, AgentToolRejected):
                    raise  # Budget exhaustion must stop the owning task, not look like a failed model capability.
                return result
            if dispatched:
                # Test traffic or storage dispatch has already happened; do not
                # claim definite no-effect merely because a later guard failed.
                raise AgentError("agent_source_operation_interrupted") from None
            ledger.update(operation_id, "not_executed", error=str(exc))
            raise
        if name == "ryn_commit_source_draft":
            try:
                self.services.get("active_source_managers", lambda: None)()
                self.services.get("refresh_publication", lambda: None)()
            except Exception:
                # The durable source and business receipt remain authoritative.
                result = {**result, "publication": "refresh_pending"}
        return result

    def reconcile(self, operation_id, *, context, cancelled):
        if cancelled():
            reject("agent_cancelled")
        operation = context.get("operation") or {}
        if operation.get("tool") not in NAMES or operation.get("operation_id") != operation_id:
            return {"outcome": "unknown"}
        row = self.ledger_provider().get(operation_id)
        binding = ResourceWrites.binding(operation["tool"], operation["arguments"], operation_id, context)
        if not row or row["binding"] != binding or binding["arguments_sha256"] != operation.get("arguments_sha256"):
            return {"outcome": "unknown"}
        if row["status"] not in {"confirmed", "not_executed"}:
            return {"outcome": "unknown"}
        return {"outcome": "executed" if row["status"] == "confirmed" else "not_executed", "receipt": {
            "operation_id": operation_id, "tool": operation["tool"], "arguments_sha256": operation["arguments_sha256"],
            "result": row["result"] if row["status"] == "confirmed" else {"not_executed": True, "error": row["error"]}}}

    def verify(self, receipt, *, context, cancelled):
        if cancelled():
            reject("agent_cancelled")
        result = receipt.get("result") or {}
        with self.sources.lock, self._lock():
            state = self._read()
            if receipt["tool"] in {"ryn_prepare_source_draft", "ryn_test_source_draft"}:
                row = state["drafts"].get(result.get("draft_id"))
                if not row or row["binding"] != self._binding(context):
                    return {"status": "unknown", "evidence": {"reason": "source_draft_unavailable"}, "checked_at": time.time()}
                if receipt["tool"] == "ryn_prepare_source_draft":
                    return {"status": "passed", "evidence": {"draft_id": row["draft_id"], "current_version": row["version"],
                        "coverage": "Task-bound configuration draft exists. This does not establish active source save or connectivity.",
                        "current_state_matches": True}, "checked_at": time.time()}
                test = row.get("test") or {}
                matches = test.get("draft_version") == result.get("version") and bool(test.get("chat_passed"))
                return {"status": "passed" if matches else "failed", "evidence": {"draft_id": row["draft_id"],
                    "current_state_matches": matches, "chat_probe_evidence": self.public(row)["test"],
                    "coverage": "Stored fixed-prompt draft probe result, not a fresh connectivity check or proof of saved configuration."}, "checked_at": time.time()}
            row, credential = self._draft(state, receipt["arguments"], context)
            record = self.sources.read().get(row.get("committed_source_id"))
            if not record or record.get("source_revision") != result.get("source_revision"):
                return {"status": "unknown", "evidence": {"reason": "source_changed_after_save", "current_state_matches": False}, "checked_at": time.time()}
            snapshot = copy.deepcopy(record)
        def authorize_probe():
            with self.sources.lock, self._lock():
                self._draft(self._read(), receipt["arguments"], context)
                if self.sources.read().get(snapshot["id"]) != snapshot:
                    reject("agent_source_revision_conflict")
        test = self._probe(snapshot, test_tools=False, cancelled=cancelled, authorize=authorize_probe,
                           meter=context.get("_model_probe"))
        with self.sources.lock:
            matches = self.sources.read().get(record["id"]) == snapshot
        return {"status": "passed" if matches and test["chat_passed"] else "failed", "evidence": {
            "current_state_matches": matches, "post_save_retest": test, "source_id": record["id"],
            "source_revision": record["source_revision"], "saved_configuration_retained": True,
            "operation_attribution": "business_receipt_separate_from_current_state",
            "coverage": "Exact saved source revision and fresh chat probes for every configured model; no automatic rollback."}, "checked_at": time.time()}

    def forget_task(self, task_id):
        if not self.path or not self.path.exists():
            return
        with self._lock():
            state = self._read()
            for key in ("credentials", "drafts", "requests"):
                state[key] = {k: v for k, v in state[key].items() if v.get("task_id", v.get("binding", {}).get("task_id")) != task_id}
            self._write(state)
            for orphan in self.path.parent.glob("." + self.path.name + ".*.tmp"):
                orphan.unlink()
