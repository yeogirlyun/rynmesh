"""Explicit Q01/Q06 owner-admin draft collection, never a formal v2 trial."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from access_query import AccessQuery
from query_cases import FIXTURES, QueryWorld

from rynmesh.agent_runtime.store import AgentStore

_spec = importlib.util.spec_from_file_location("query_role_composer", FIXTURES / "role_contract_v2.py")
composer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(composer)
SUPPORTED = frozenset({"Q01", "Q06"})


class DraftDispatchCollector:
    """Opt-in collection-time provenance, never a verdict or model input."""

    def __init__(self, output, record):
        self.output, self.record = Path(output), record
        self.binding = self.task_id = self.registration_hash = None
        self.events, self.model_requests, self.native_requests = [], [], []
        self.errors = []
        self._lock, self._active = threading.RLock(), threading.local()
        self.path = self.output / "dispatch-events.jsonl"
        # Existing files are not resumed or silently rewritten as fresh samples.
        with self.path.open("x", encoding="utf-8"):
            pass

    def _reject(self, code):
        self.errors.append(code)
        raise RuntimeError(code)

    @staticmethod
    def _write_new(path, value):
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def register(self, controller, runtime, task):
        if self.binding is not None or self.events:
            self._reject("draft_registration_already_started")
        effective = controller.effective_input
        contract = {"effective_case_sha256": composer.load_effective_case(controller.case["id"])["effective_sha256"],
            "effective_input_sha256": effective["effective_sha256"], "overlay_sha256": effective["overlay_sha256"],
            "base_cases_sha256": effective["base"]["cases"]["sha256"],
            "base_world_sha256": effective["base"]["world"]["sha256"]}
        binding = {key: self.record[key] for key in ("run_id", "run_nonce", "dataset_version", "case_id", "cohort")}
        binding.update(trial=1, effective_case_sha256=contract["effective_case_sha256"],
                       overlay_sha256=contract["overlay_sha256"])
        registration = {"registered_at": datetime.now(timezone.utc).isoformat(),
            "registration_kind": "draft_development_collection_only", "sampling_kind": "new_v2_draft_sampling",
            "first_dispatch_not_started": True, "formal_trial_registered": False,
            "collector_profile": self.record["collector_profile"], **binding, **contract}
        root = FIXTURES.parents[2]
        registration["collector_contract_hashes"] = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (FIXTURES / "role_grade_v2.py", root / "docs/acceptance/ryn-agent-delivery/role-run-v2.schema.json",
                         root / "docs/acceptance/ryn-agent-delivery/role-contract-v2-decision.json")}
        registration["role_proof"] = {"kind": "registration", "binding": copy.deepcopy(binding),
            "observations": {key: registration[key] for key in
                             ("registered_at", "sampling_kind", "first_dispatch_not_started")}}
        self._write_new(self.output / "effective-input.json", effective)
        digest = self._write_new(self.output / "registration.json", registration)
        task_binding = {"task_id": task["id"], "bound_at": datetime.now(timezone.utc).isoformat(),
            "registration_sha256": digest,
            "created_task_readback_sha256": composer.digest(runtime.store.read()["tasks"][task["id"]])}
        task_binding["role_proof"] = {"kind": "task_binding", "binding": copy.deepcopy(binding),
                                     "observations": copy.deepcopy(task_binding)}
        self._write_new(self.output / "task-binding.json", task_binding)
        self.binding, self.task_id, self.registration_hash = binding, task["id"], digest
        self.runtime = runtime
        self.record.update(role_contract=contract, registration=registration, task_binding=task_binding,
                           sampling_binding=copy.deepcopy(binding), registration_file_sha256=digest,
                           collector_state="draft_registered_before_first_task_dispatch")
        self.attach_tools(runtime.tools)
        if self.record["cohort"] == "ryn" and hasattr(runtime, "_internal_tool"):
            original_internal = runtime._internal_tool

            def observed_internal(selected, started):
                row = runtime.store.read()["tasks"].get(self.task_id, {})
                queued = (row.get("queue") or [{}])[0]
                call = (selected.get("queue") or [{}])[0]
                if (selected.get("id") != self.task_id or not queued.get("operation_id")
                        or composer.digest(queued) != composer.digest(call)):
                    self._reject("draft_internal_tool_durable_queue_mismatch")
                self._begin("tool", tool_name=call["name"], operation_id=call["operation_id"],
                            task_binding_basis="durable_runtime_queue_internal_tool_entry",
                            durable_intent_sha256=composer.digest(queued))
                return original_internal(selected, started)

            runtime._internal_tool = observed_internal

    def _registered(self):
        if self.binding is None:
            self._reject("draft_dispatch_registration_missing_or_changed")
        try:
            unchanged = hashlib.sha256((self.output / "registration.json").read_bytes()).hexdigest() == self.registration_hash
        except OSError:
            self._reject("draft_dispatch_registration_unreadable")
        if not unchanged:
            self._reject("draft_dispatch_registration_missing_or_changed")

    def _begin(self, kind, *, identity=None, native=None, **fields):
        with self._lock:
            self._registered()
            event = {"kind": kind, "id": identity or f"{self.task_id}:{kind}:{len(self.events) + 1}",
                "sampling_binding": copy.deepcopy(self.binding), "task_id": self.task_id,
                "registration_sha256": self.registration_hash,
                "dispatched_at": datetime.now(timezone.utc).isoformat(), **fields}
            if native is not None:
                event["native_rpc"] = copy.deepcopy(native)
            if any(row["id"] == event["id"] for row in self.events):
                self._reject("draft_duplicate_dispatch_identity")
            # This durable entry precedes the underlying bridge/tool/gateway.
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                self._reject("draft_dispatch_audit_write_failed")
            self.events.append(event)
            if kind == "model":
                self.model_requests.append(copy.deepcopy(event))
            return copy.deepcopy(event)

    def model_request(self):
        active = getattr(self._active, "event", None)
        if active is not None:
            if active["kind"] != "model":
                self._reject("draft_native_dispatch_kind_mismatch")
            return copy.deepcopy(active)
        if self.record["cohort"] == "reference":
            self._reject("draft_native_request_context_missing")
        return self._begin("model")

    def attach_tools(self, tools):
        execute = tools.execute

        def observed(name, arguments, **options):
            context = options.get("context")
            active = getattr(self._active, "event", None)
            parent = getattr(self._active, "executing_tool", None)
            fields = {"task_binding_basis": "runtime_task_context"}
            if context is not None and context.get("task_id") != self.task_id:
                self._reject("draft_tool_task_binding_mismatch")
            if parent is not None:
                # Production diagnostics synchronously read the device directory
                # through the same gateway. This is a child of the bound call,
                # not another model proposal or native RPC. Allow only that
                # explicit read edge; never synthesize permissions or context.
                if (parent.get("tool_name") != "ryn_get_system_diagnostics"
                        or name != "ryn_list_devices" or arguments != {}
                        or tools.metadata(name).get("effect") != "read"):
                    self._reject("draft_unregistered_nested_tool")
                child = self._begin("tool", tool_name=name, operation_id=options.get("operation_id"),
                    task_binding_basis="nested_read_of_bound_tool", parent_dispatch_id=parent["id"])
                self._active.executing_tool = child
                try:
                    return execute(name, arguments, **options)
                finally:
                    self._active.executing_tool = parent
            if context is None and active is None:
                # Ordinary catalog reads deliberately have no scope context.
                # Bind their unchanged call to the actual durable runtime intent.
                row = self.runtime.store.read()["tasks"].get(self.task_id, {})
                flight = row.get("in_flight") or {}
                if (flight.get("kind") != "tool" or flight.get("dispatched") is not True
                        or flight.get("name") != name or flight.get("operation_id") != options.get("operation_id")):
                    self._reject("draft_tool_durable_intent_mismatch")
                fields = {"task_binding_basis": "durable_runtime_in_flight",
                          "durable_intent_sha256": composer.digest(flight)}
            if active is not None:
                if active["kind"] != "tool" or active.get("tool_name") != name:
                    self._reject("draft_native_tool_context_mismatch")
                dispatch = active
            elif self.record["cohort"] == "reference":
                self._reject("draft_native_request_context_missing")
            else:
                dispatch = self._begin("tool", tool_name=name, operation_id=options.get("operation_id"), **fields)
            self._active.executing_tool = dispatch
            try:
                return execute(name, arguments, **options)
            finally:
                self._active.executing_tool = None

        tools.execute = observed

    @contextmanager
    def reference_dispatch(self, method, params, native):
        self._registered()
        if (not isinstance(native, dict) or native.get("sampling_binding") != self.binding
                or native.get("task_id") != self.task_id or native.get("registration_sha256") != self.registration_hash
                or native.get("method") != method or getattr(self._active, "event", None) is not None):
            self._reject("draft_native_sampling_binding_mismatch")
        self.native_requests.append(copy.deepcopy(native))
        if method == "approval":
            yield
            return
        event = self._begin(method, identity=native["id"], native=native, task_binding_basis="native_worker_pipe_rpc",
                            **({"tool_name": params["name"], "tool_call_id": params["tool_call_id"]} if method == "tool" else {}))
        self._active.event = event
        try:
            yield
        finally:
            self._active.event = None

    def native_config(self):
        self._registered()
        return {"sampling_binding": copy.deepcopy(self.binding), "task_id": self.task_id,
                "registration_sha256": self.registration_hash,
                "audit_path": str(self.output / "native-rpc-events.jsonl")}

    def finalize(self, native_session=None):
        complete = "error_type" not in self.record and not self.errors
        audit = {"coverage": "all_model_and_tool_dispatches" if complete else "partial_dispatch_collection",
                 "events": copy.deepcopy(self.events),
                 "scope": "Task-time bridge/gateway attempts, including readback/checker; excludes collector setup probes.",
                 "completed_collection": complete, "capture_errors": list(self.errors)}
        self._write_new(self.output / "dispatch-audit.json", {"dispatch_audit": audit})
        self._write_new(self.output / "model-requests.json", {"model_requests": self.model_requests})
        self.record.update(dispatch_audit=audit, model_requests=copy.deepcopy(self.model_requests),
                           first_dispatch_at=self.events[0]["dispatched_at"] if self.events else None)
        if native_session is not None:
            self.record["reference_session_readback"] = self._native_readback(Path(native_session))

    def _native_readback(self, path):
        result = {"status": "pending", "task_id": self.task_id, "run_nonce": self.record["run_nonce"],
            "proof_scope": "native_session_and_request_readback",
            "request_id_basis": "Native worker pipe RPC sequence, collected before send in a separate audit; not SQLite request fields.",
            "native_session_path": str(path), "observed_request_ids": []}
        audit_path = self.output / "native-rpc-events.jsonl"
        try:
            rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            result["native_rpc_audit_sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
            if rows != self.native_requests:
                result.update(status="failed", error_code="draft_native_rpc_readback_mismatch")
                return result
            sessions = {row["session_id"] for row in rows}
            if len(sessions) != 1:
                return result
            session_id = next(iter(sessions))
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                actual = [row[0] for row in connection.execute("select id from session")]
                count = connection.execute("select count(*) from message where session_id=?", (session_id,)).fetchone()[0]
            if actual != [session_id] or count < 1:
                result.update(status="failed", error_code="draft_native_session_mismatch")
                return result
            ids = [row["id"] for row in rows if row["method"] == "model"]
            if ids != [row["id"] for row in self.model_requests]:
                result.update(status="failed", error_code="draft_native_model_request_mismatch")
                return result
            result.update(status="observed", session_id=session_id, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          message_count=count, observed_request_ids=ids)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            result["error_code"] = "draft_native_readback_incomplete"
        return result


class RoleQueryWorld(QueryWorld):
    def __init__(self, home, case_id):
        if case_id not in SUPPORTED:
            raise ValueError("role_query_case_unsupported")
        super().__init__(home, case_id)
        self.effective_input = composer.load_effective_case(case_id)
        self.case = copy.deepcopy(self.effective_input["effective_case"])
        self.world = copy.deepcopy(self.effective_input["base_world"])
        self.runtime = None

    def registry_store(self, label, store):
        # The owner itself signs A's record: never a second identity named A.
        node = store if label == "node-a" else super().registry_store(label, store)
        if not hasattr(self, "instantiated_nodes"):
            self.instantiated_nodes = {}
        self.instantiated_nodes[label] = node
        return node

    def configure_access(self, app, store):
        if (self.bindings["node-a"] != store.peer_id
                or self.sources.path.resolve() != (store.home / "llm/sources.enc").resolve()):
            raise RuntimeError("role_query_owner_registry_mismatch")
        # Collector setup evidence only. A's self-presence tool must still say
        # it did not perform an HTTP probe; this record is never model input.
        endpoint = f"http://127.0.0.1:{self.health.server_port}/health"
        with urlopen(endpoint, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("role_query_owner_health_failed")
        self.owner_health_probe = {"peer_id": store.peer_id, "endpoint": endpoint, "status": 200,
            "checked_at": datetime.now(timezone.utc).isoformat(), "scope": "collector_setup_loopback_fixture_endpoint"}
        self.access_query = AccessQuery(self, app, store, owner_execution=True)

    def select_model(self):
        return self.bindings["model-a"]

    def create_task(self, runtime):
        expected_owner = self.case["authorization"]["owner_id"]
        if (runtime.owner_id != expected_owner or runtime.consumer_node_id != self.store.peer_id
                or runtime.tools.store is not self.store
                or runtime.store.root.resolve() != (self.store.home / "agent").resolve()
                or runtime.store.pub != AgentStore(self.store.home / "agent", self.access_query.owner.messaging_private).pub
                or runtime.settings()["permission_mode"] != "read_only"):
            raise RuntimeError("role_query_runtime_identity_mismatch")
        self.runtime = runtime
        self.attach(runtime.tools)
        task = runtime.create({"message": self.case["prompt"], "model_id": self.selected})
        self.task_id = task["id"]
        stored = runtime.store.read()["tasks"][task["id"]]
        context = runtime._execution_context(stored)
        if (context["owner_id"] != expected_owner or context["consumer_node_id"] != self.bindings["node-a"]
                or stored["scope"]["resource_grants"] or stored["scope"]["egress_grants"]):
            raise RuntimeError("role_query_task_binding_mismatch")
        self.effective_input = composer.load_effective_case(self.case["id"],
            node_bindings={name: self.bindings[name] for name in composer.LOGICAL_NODES},
            execution_identity={"runtime_consumer_node_id": runtime.consumer_node_id,
                "agent_store_consumer_node_id": context["consumer_node_id"], "owner_id": context["owner_id"]})
        if getattr(self, "on_task_created", None):
            self.on_task_created(task)
        return task

    def execute(self, runtime):
        task = self.create_task(runtime)
        for _ in range(160):
            task = runtime.get(task["id"])
            if task["status"] not in {"queued", "running"}:
                break
            runtime.run_once()
        return {"case_id": self.case["id"], "prompt": self.case["prompt"], "task": runtime.get(task["id"]),
                "formal_case_score": False, **self.collect()}

    def collect(self):
        collected = super().collect()
        runtime = self.runtime
        stored = runtime.store.read()["tasks"].get(self.task_id) if runtime and getattr(self, "task_id", None) else None
        context = runtime._execution_context(stored) if stored else None
        task_readback = ({key: copy.deepcopy(stored[key]) for key in
                         ("id", "status", "model_id", "permission_mode", "scope_revision", "model_destination")}
                        if stored else None)
        source_rows = self.sources.read()
        role = self.effective_input["role_contract"]
        collected.update(dataset_version=self.effective_input["dataset_version"],
            role_contract_status="draft/not_formal_ready", formal_case_score=False, formal_trial_registered=False,
            effective_input=copy.deepcopy(self.effective_input), effective_input_sha256=self.effective_input["effective_sha256"],
            execution_role="owner-admin", consumer_peer_id=self.store.peer_id, provider_peer_id=self.store.peer_id,
            primary_model=self.selected,
            owner_health_setup_probe=copy.deepcopy(self.owner_health_probe),
            actual_role_binding={"agent_execution_node": "node-a", "execution_peer_id": self.store.peer_id,
                "runtime_consumer_node_id": runtime.consumer_node_id if runtime else None,
                "runtime_owner_id": runtime.owner_id if runtime else None,
                "task_context_consumer_node_id": context["consumer_node_id"] if context else None,
                "task_context_owner_id": context["owner_id"] if context else None,
                "task_context_binding_basis": "Runtime._execution_context of decrypted created task; owner/node are runtime attributes, not persisted task-row fields.",
                "stored_task_id": stored["id"] if stored else None,
                "task_readback": task_readback,
                "task_readback_sha256": composer.digest(task_readback) if task_readback else None,
                "stored_task_readback_sha256": composer.digest(stored) if stored else None,
                "agent_store_path": str(runtime.store.path) if runtime else None,
                "agent_store_file_sha256": hashlib.sha256(runtime.store.path.read_bytes()).hexdigest() if runtime and runtime.store.path.exists() else None,
                "agent_store_encryption_public_key": runtime.store.pub if runtime else None,
                "tools_store_peer_id": runtime.tools.store.peer_id if runtime else None,
                "source_store_path": str(self.sources.path),
                "source_owners": {logical: {"logical_owner": role["source_owners"][logical],
                    "actual_owner_peer_id": self.store.peer_id, "source_id": self.bindings[logical],
                    "present_in_owner_store": self.bindings[logical] in source_rows}
                    for logical in self.world["sources"]},
                "receiver_peer_id": self.access_query.receiver.peer_id,
                "receiver_home": str(self.access_query.receiver_store.home),
                "identity_evidence_scope": "Actual runtime/tool objects and encrypted task readback in isolated logical node homes; no physical-host or process-isolation proof.",
                "uninstantiated_deployments": {"connection_hosts": copy.deepcopy(role["connection_hosts"]),
                    "history_owners": copy.deepcopy(role["history_owners"]),
                    "status": "not_instantiated_or_observed_in_Q01_Q06; no resources/history authorized or supplied"}},
            normalizer_status="v2_draft_unsupported_no_formal_grading")
        return collected
