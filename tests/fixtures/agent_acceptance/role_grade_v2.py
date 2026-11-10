"""Draft-only development role grading with a pinned common evidence engine.

Reuses the unchanged v1 grade/aggregate function *code* in independent globals.
A non-filesystem token represents the effective suite. Real Paths always retain
byte SHA-256 semantics. No version is rewritten to v1 and no imported globals
are patched. Role evidence still relies on trusted collectors and independent
review; this offline tool is not a remote-attestation or anti-forgery system.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import types
import uuid
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DOCS = REPO / "docs/acceptance/ryn-agent-delivery"
SCHEMA_PATH = DOCS / "role-run-v2.schema.json"
VERSION = "ryn-agent-role-grader.v2-draft.1"
PINS = {
    DOCS / "cases-v1.json": "27a50f909a09a6972fc7b11df55f7798412ec415b8ef87ccadef8ddc85bd06b5",
    HERE / "world.json": "fbeb1e3466870df1d65685ed11835a4cdf078817a3b922e912a83453be09ba82",
    HERE / "validate.py": "5261f59e1d4abe931eb5fc8c0ddb86f24b47c73882a67ee7be969199e82928f4",
    HERE / "run.schema.json": "8a587ce7ed3902afcfaff97268ea7a8fc45e551744ecd4ac60393e24396dbb99",
    HERE / "role_contract_v2.py": "cee82c3574de9dd017b16e21075ee19c5fa7cbdf2fce62aea624bb81b6656c36",
    DOCS / "role-overlay-v2-draft.json": "a90280553577ec075e6fe7e131a07c4ca830d4e60ea2600a2ae2c28b93e407e5",
    DOCS / "role-overlay-v2.schema.json": "e16af76b875957bd0e85ad2537b4580beef909b17b7d1a6faec9a6b52efe353f",
    DOCS / "role-contract-v2-decision.json": "d80ba913f728ff6404f556c1211cf25de3ab764aab438cf999c4e74181c87273",
}
ROLE_KINDS = {"registration": "raw_trace", "task_binding": "raw_trace", "runtime": "raw_trace",
              "storage": "raw_receipt", "deployment": "raw_trace", "physical_hosts": "dependency_probe",
              "review": "independent_review"}
SOURCE_REQUIREMENTS = {
    "Q01": ("source-alpha", "source-other", "source-broken"), "Q02": ("source-broken",),
    "Q06": ("source-alpha",), "C01": ("source-new",), "C02": ("source-alpha",),
    "C03": ("source-alpha", "source-other"), "C04": ("source-alpha",),
    "R02": ("source-alpha",), "R04": ("source-alpha",), "P03": ("source-alpha",),
}


class RoleGradeError(ValueError):
    pass


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _time(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("timestamp needs timezone")
    return result


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class _SuiteToken:
    """Intentionally no __fspath__: never interchangeable with a real file."""


class _VirtualSuiteRoot:
    def __init__(self, token):
        self.token = token

    def __truediv__(self, name):
        if name != "cases-v1.json":
            raise RoleGradeError("only_development_suite_supported")
        return self.token


class DraftRoleGrader:
    def registration_sampling_kind(self):
        return "new_v2_draft_sampling"

    def __init__(self):
        self._check_pins()
        self.common = _import("role_common_validator", HERE / "validate.py")
        self.composer = _import("role_case_composer", HERE / "role_contract_v2.py")
        self.schema = self.common.read(SCHEMA_PATH)
        self.runtime_hashes = {Path(__file__): file_digest(Path(__file__)), SCHEMA_PATH: file_digest(SCHEMA_PATH)}
        Draft202012Validator.check_schema(self.schema)
        self.common_schema = self.common.read(HERE / "run.schema.json")
        self.cases = {case_id: self.composer.load_effective_case(case_id) for case_id in self.composer.CASE_IDS}
        self.suite_hash = self.composer.digest({
            "dataset_version": self.composer.DATASET_VERSION,
            "case_hashes": {key: value["effective_sha256"] for key, value in self.cases.items()},
            "decision_sha256": PINS[DOCS / "role-contract-v2-decision.json"],
            "loader_sha256": PINS[HERE / "role_contract_v2.py"],
        })
        self.suite = {"dataset_version": self.composer.DATASET_VERSION,
                      "cases": [value["effective_case"] for value in self.cases.values()]}
        self.token = _SuiteToken()
        overrides = {"suite": self._suite, "digest": self._digest, "DOCS": _VirtualSuiteRoot(self.token)}
        self._common_grade = self._rebind(self.common.grade, overrides)
        self._common_template = self._rebind(self.common.template, overrides)
        self._common_aggregate = self._rebind(self.common.aggregate, overrides)

    @staticmethod
    def _check_pins():
        for path, expected in PINS.items():
            if file_digest(path) != expected:
                raise RoleGradeError("common_or_role_contract_version_changed")

    @staticmethod
    def _rebind(function, overrides):
        context = {**function.__globals__, **overrides}
        return types.FunctionType(function.__code__, context, function.__name__, function.__defaults__, function.__closure__)

    def _suite(self, partition="development"):
        if partition != "development":
            raise RoleGradeError("only_development_suite_supported")
        return copy.deepcopy(self.suite)

    def _digest(self, value):
        return self.suite_hash if value is self.token else file_digest(value)

    def _binding(self, case_id):
        value = self.cases[case_id]
        return {
            "effective_suite_sha256": self.suite_hash,
            "effective_case_sha256": value["effective_sha256"],
            "effective_world_sha256": self.composer.digest({"base_world": value["base_world"],
                                                          "role_contract": value["role_contract"]}),
            "base_cases_sha256": value["base"]["cases"]["sha256"],
            "base_world_sha256": value["base"]["world"]["sha256"],
            "overlay_sha256": value["overlay_sha256"],
            "overlay_schema_sha256": value["overlay_schema_sha256"],
            "loader_sha256": PINS[HERE / "role_contract_v2.py"],
            "decision_sha256": PINS[DOCS / "role-contract-v2-decision.json"],
            "grader_sha256": self.runtime_hashes[Path(__file__)],
            "run_schema_sha256": self.runtime_hashes[SCHEMA_PATH],
            "common_validator_sha256": PINS[HERE / "validate.py"],
            "common_run_schema_sha256": PINS[HERE / "run.schema.json"],
        }

    def template(self, case_id, cohort="ryn", trial=1):
        if case_id not in self.cases or cohort not in {"ryn", "reference"} or type(trial) is not int or trial not in (1, 2, 3):
            raise RoleGradeError("invalid_development_trial")
        body = self._common_template(case_id)
        body.update(cohort=cohort, trial=trial)
        return {
            "schema_version": 2, "kind": "ryn.agent.role-evidence-run",
            "dataset_version": self.composer.DATASET_VERSION, "status": "draft/not_formal_ready",
            "formal_trial_registered": False, "run_id": uuid.uuid4().hex, "run_nonce": uuid.uuid4().hex,
            "task_id": "", "node_bindings": {}, "contract": self._binding(case_id),
            "run": body, "role_evidence": dict.fromkeys(ROLE_KINDS),
        }

    @staticmethod
    def proof_binding(envelope):
        run = envelope["run"]
        return {key: envelope[key] for key in ("run_id", "run_nonce", "dataset_version")} | {
            key: run[key] for key in ("case_id", "cohort", "trial")
        } | {"effective_case_sha256": envelope["contract"]["effective_case_sha256"],
             "overlay_sha256": envelope["contract"]["overlay_sha256"]}

    def _documents(self, run, root):
        documents, entries = {}, {}
        for entry in run["evidence"]:
            path = self.common.confined(root, entry["path"])
            if path.is_file() and file_digest(path) == entry["sha256"]:
                entries[entry["id"]] = entry
                if entry["kind"] != "artifact_readback":
                    documents[entry["id"]] = self.common.read(path)
        return entries, documents

    def _role_checks(self, envelope, root):
        run = envelope["run"]
        expected = self.cases[run["case_id"]]["role_contract"]
        failures, pending = [], []
        entries, documents = self._documents(run, root)
        binding = self.proof_binding(envelope)
        nodes = envelope["node_bindings"]
        if not nodes:
            return [], ["role node identity mapping absent"]
        try:
            execution_id = nodes[expected["agent_execution_node"]]
            self.composer.load_effective_case(run["case_id"], node_bindings=nodes, execution_identity={
                "runtime_consumer_node_id": execution_id, "agent_store_consumer_node_id": execution_id,
                "owner_id": self.cases[run["case_id"]]["effective_case"]["authorization"]["owner_id"],
            })
        except (KeyError, self.composer.RoleContractError):
            return ["role node identity mapping invalid"], []
        owner_id = self.cases[run["case_id"]]["effective_case"]["authorization"]["owner_id"]
        refs, proofs = envelope["role_evidence"], {}
        two_hosts = any(d["id"] == "two_physical_nodes" for d in self.cases[run["case_id"]]["effective_case"]["dependencies"])
        required_roles = set(ROLE_KINDS) - (set() if two_hosts else {"physical_hosts"})
        for name in required_roles:
            ref = refs[name]
            if not ref or ref not in documents:
                pending.append(f"role proof missing: {name}")
                continue
            entry, document = entries[ref], documents[ref]
            producer = "reviewer" if name == "review" else "collector"
            if entry["kind"] != ROLE_KINDS[name] or entry["producer"] != producer or not isinstance(document, dict):
                failures.append(f"role proof invalid source: {name}")
                continue
            proof = document.get("role_review" if name == "review" else "role_proof")
            if not isinstance(proof, dict):
                pending.append(f"role proof observations absent: {name}")
                continue
            if proof.get("binding") != binding:
                failures.append(f"role proof binding mismatch: {name}")
                continue
            if name != "review" and (proof.get("kind") != name or not isinstance(proof.get("observations"), dict)):
                failures.append(f"role proof type mismatch: {name}")
                continue
            proofs[name] = proof

        def require(name, fields):
            proof = proofs.get(name)
            if proof is None:
                return None
            obs = proof["observations"]
            if not set(fields).issubset(obs):
                pending.append(f"role observations incomplete: {name}")
                return None
            return obs

        registration = require("registration", ("registered_at", "sampling_kind", "first_dispatch_not_started"))
        task_binding = require("task_binding", ("task_id", "registration_sha256", "bound_at"))
        runtime = require("runtime", ("runtime_consumer_node_id", "runtime_owner_id", "task_context_consumer_node_id",
                                     "task_context_owner_id", "tools_store_peer_id", "agent_store_path",
                                     "agent_store_encryption_public_key", "task_context_binding_basis",
                                     "authentication_source_ids", "first_dispatch_at", "runtime_role"))
        storage = require("storage", ("agent_store_path", "agent_store_encryption_public_key", "task_readback",
                                     "task_readback_sha256", "decrypted_from_store"))
        deployment = require("deployment", ("source_owners", "connections", "histories", "connection_accesses"))
        physical = require("physical_hosts", ("node_host_bindings", "physical_host_ids", "host_probe_source_ids")) if two_hosts else None
        request_ids, dispatch_sources = self._dispatch_checks(
            envelope, entries, documents, runtime, task_binding, failures, pending)
        if not envelope["task_id"]:
            pending.append("actual task binding absent")
        if registration and (registration["sampling_kind"] != self.registration_sampling_kind()
                             or registration["first_dispatch_not_started"] is not True):
            failures.append("draft pre-dispatch registration invalid")
        if task_binding:
            if task_binding["task_id"] != envelope["task_id"]:
                failures.append("registered task binding mismatch")
            entry = entries.get(refs["registration"], {})
            if not entry:
                pending.append("original registration bytes unavailable")
            elif task_binding["registration_sha256"] != entry.get("sha256"):
                failures.append("task binding does not reference original registration bytes")
        if runtime:
            expected_runtime_role = ("ryn_agent_executor" if run["cohort"] == "ryn"
                                     else "reference_accounting_and_tool_permission_host")
            if runtime["runtime_role"] != expected_runtime_role:
                failures.append("runtime role does not match actual cohort executor")
            expected_fields = {"runtime_consumer_node_id": execution_id, "task_context_consumer_node_id": execution_id,
                               "tools_store_peer_id": execution_id, "runtime_owner_id": owner_id, "task_context_owner_id": owner_id}
            if any(runtime[key] != value for key, value in expected_fields.items()):
                failures.append("actual runtime/context identity mismatch")
            if not _text(runtime["task_context_binding_basis"]):
                pending.append("runtime-derived context basis missing")
            auth_refs = runtime["authentication_source_ids"]
            if not isinstance(auth_refs, list) or not auth_refs:
                pending.append("authenticated runtime source missing")
            else:
                for ref in auth_refs:
                    entry, doc = entries.get(ref, {}), documents.get(ref, {})
                    observation = doc.get("authenticated_runtime") if isinstance(doc, dict) else None
                    if (entry.get("kind") not in {"raw_trace", "dependency_probe"} or entry.get("producer") != "collector"
                            or not isinstance(observation, dict)):
                        pending.append("authenticated runtime raw observation missing")
                        continue
                    if not {"run_nonce", "peer_id", "owner_id", "method", "identity_key_sha256"}.issubset(observation):
                        pending.append("authenticated runtime provenance incomplete")
                        continue
                    if (observation.get("run_nonce") != envelope["run_nonce"] or observation.get("peer_id") != execution_id
                            or observation.get("owner_id") != owner_id):
                        failures.append("authenticated runtime raw binding mismatch")
                    if (observation.get("method") not in {"in_process_owner_key_and_runtime", "local_control_challenge"}
                            or not _hash(observation.get("identity_key_sha256"))):
                        pending.append("authenticated runtime provenance incomplete")
            if run["cohort"] == "reference":
                ref = runtime.get("reference_execution_evidence_id")
                entry, document = entries.get(ref, {}), documents.get(ref, {})
                native = document.get("reference_session_readback") if isinstance(document, dict) else None
                if (entry.get("kind") != "raw_receipt" or entry.get("producer") != "collector"
                        or not isinstance(native, dict)):
                    pending.append("actual reference native session execution evidence absent")
                else:
                    artifact = entries.get(native.get("artifact_evidence_id"), {})
                    if (artifact.get("kind") != "artifact_readback" or artifact.get("sha256") != native.get("sha256")
                            or not _text(native.get("session_id"))
                            or native.get("proof_scope") != "native_session_and_request_readback"):
                        pending.append("reference session file existence is not execution proof")
                    native_ids = native.get("observed_request_ids")
                    if not isinstance(native_ids, list) or not native_ids or not all(_text(v) for v in native_ids):
                        pending.append("reference native request readback absent")
                    elif len(set(native_ids)) != len(native_ids) or (request_ids and set(native_ids) != request_ids):
                        failures.append("reference native requests do not match original model dispatches")
                    elif not request_ids:
                        pending.append("reference requests lack original model dispatch provenance")
                    if native.get("run_nonce") != envelope["run_nonce"] or native.get("task_id") != envelope["task_id"]:
                        failures.append("reference session belongs to another run/task")
        if runtime and storage:
            if (not _text(storage["agent_store_path"]) or not _text(storage["agent_store_encryption_public_key"])
                    or storage["agent_store_path"] != runtime["agent_store_path"]
                    or storage["agent_store_encryption_public_key"] != runtime["agent_store_encryption_public_key"]):
                failures.append("actual AgentStore directory/public-key mismatch")
            task = storage["task_readback"]
            if not isinstance(task, dict) or task.get("id") != envelope["task_id"]:
                failures.append("actual task readback missing or mismatched")
            elif self.composer.digest(task) != storage["task_readback_sha256"]:
                failures.append("task readback hash mismatch")
            if isinstance(task, dict) and ("owner_id" in task or "consumer_node_id" in task):
                failures.append("persisted task identity fields invented")
            if storage["decrypted_from_store"] is not True:
                pending.append("AgentStore decryption readback not established")
        if registration and task_binding and runtime:
            try:
                started, ended = _time(run["started_at"]), _time(run["ended_at"])
                registered, bound = _time(registration["registered_at"]), _time(task_binding["bound_at"])
                first = _time(runtime["first_dispatch_at"])
                if not started <= registered <= bound <= first <= ended:
                    failures.append("registration/task binding was not before first dispatch")
            except ValueError:
                pending.append("pre-dispatch registration chronology incomplete")
        if deployment:
            self._deployment_checks(run["case_id"], expected, nodes, deployment, failures, pending)
        if physical:
            hosts = physical["node_host_bindings"]
            if (not isinstance(hosts, dict) or set(hosts) != {nodes["node-a"], nodes["node-b"]}
                    or not all(isinstance(v, str) and v for v in hosts.values()) or len(set(hosts.values())) != 2
                    or set(physical["physical_host_ids"]) != set(hosts.values())):
                failures.append("two actual distinct physical host bindings not established")
            host_refs = physical["host_probe_source_ids"]
            if not isinstance(host_refs, list) or not host_refs:
                pending.append("physical host raw probes absent")
            else:
                observed, probes_complete = {}, True
                for ref in host_refs:
                    entry, doc = entries.get(ref, {}), documents.get(ref, {})
                    probe = doc.get("physical_host_probe") if isinstance(doc, dict) else None
                    if (entry.get("kind") not in {"raw_trace", "dependency_probe"} or entry.get("producer") != "collector"
                            or not isinstance(probe, dict) or probe.get("identity_source") != "os_machine_identity"):
                        pending.append("physical host raw probe provenance incomplete")
                        probes_complete = False
                        continue
                    if probe.get("run_nonce") != envelope["run_nonce"]:
                        failures.append("physical host probe belongs to another run")
                    observed[probe.get("peer_id")] = probe.get("physical_host_id")
                if probes_complete and observed != hosts:
                    failures.append("physical host probes do not match node bindings")
        review = proofs.get("review")
        if review:
            if (not review.get("reviewer_id") or review.get("independent_of_task_model") is not True
                    or review.get("independent_of_collector") is not True):
                pending.append("independent role provenance review absent")
            checks = review.get("checks")
            for name in required_roles - {"review"}:
                matches = [check for check in checks if isinstance(check, dict) and check.get("id") == name] if isinstance(checks, list) else []
                if len(matches) != 1 or matches[0].get("status") not in {"passed", "failed"}:
                    pending.append(f"role provenance review pending: {name}")
                elif matches[0]["status"] == "failed":
                    failures.append(f"role provenance review failed: {name}")
                else:
                    sources = matches[0].get("source_ids", [])
                    required = {refs[name]}
                    if name == "runtime" and runtime:
                        required.update(runtime["authentication_source_ids"] if isinstance(runtime["authentication_source_ids"], list) else [])
                        required.update(dispatch_sources)
                        if run["cohort"] == "reference":
                            required.add(runtime.get("reference_execution_evidence_id"))
                    if name == "physical_hosts" and physical:
                        required.update(physical["host_probe_source_ids"] if isinstance(physical["host_probe_source_ids"], list) else [])
                    if (not isinstance(sources, list) or not required.issubset(sources)
                            or any(ref not in entries or entries[ref]["producer"] != "collector" for ref in sources)):
                        pending.append(f"role review lacks inspected raw sources: {name}")
        return failures, pending

    def _dispatch_checks(self, envelope, entries, documents, runtime, task_binding, failures, pending):
        """Require collection-time binding, not a nonce added to the final report.

        Completeness remains a collector/reviewer responsibility. Hashes and
        matching timestamps alone do not attest that a document is authentic.
        """
        expected = self.proof_binding(envelope)
        model_events, audit_events, inspected = {}, {}, set()
        registration = entries.get(envelope["role_evidence"]["registration"], {})

        def bound_event(event, kind):
            if not isinstance(event, dict):
                failures.append("dispatch observation malformed")
                return None
            needed = {"sampling_binding", "task_id", "registration_sha256", "dispatched_at", "id"}
            if not needed.issubset(event):
                pending.append("original dispatch sampling provenance absent")
                return None
            if (event["sampling_binding"] != expected or event["task_id"] != envelope["task_id"]
                    or (registration and event["registration_sha256"] != registration["sha256"])):
                failures.append("original dispatch sampling binding mismatch")
                return None
            if not _text(event["id"]) or kind not in {"model", "tool"}:
                failures.append("dispatch identity or kind invalid")
                return None
            try:
                dispatched = _time(event["dispatched_at"])
                if task_binding and not _time(task_binding["bound_at"]) <= dispatched <= _time(envelope["run"]["ended_at"]):
                    failures.append("dispatch outside registered task interval")
            except ValueError:
                pending.append("original dispatch time absent")
                return None
            return (kind, event["id"]), event

        for ref, entry in entries.items():
            if entry["kind"] != "model_exchange":
                continue
            inspected.add(ref)
            doc = documents.get(ref)
            rows = doc.get("model_requests") if isinstance(doc, dict) else None
            if not isinstance(rows, list) or not rows:
                pending.append("original model exchange sampling provenance absent")
                continue
            for row in rows:
                validated = bound_event(row, "model")
                if validated:
                    key, event = validated
                    if key in model_events:
                        failures.append("duplicate original model request identity")
                    model_events[key] = event
        refs = runtime.get("dispatch_audit_source_ids") if runtime else None
        audit_complete = isinstance(refs, list) and bool(refs)
        if not isinstance(refs, list) or not refs:
            pending.append("complete model/tool dispatch audit absent")
        else:
            for ref in refs:
                inspected.add(ref)
                entry, doc = entries.get(ref, {}), documents.get(ref, {})
                audit = doc.get("dispatch_audit") if isinstance(doc, dict) else None
                if (entry.get("kind") != "raw_trace" or entry.get("producer") != "collector"
                        or not isinstance(audit, dict) or audit.get("coverage") != "all_model_and_tool_dispatches"
                        or not isinstance(audit.get("events"), list)):
                    pending.append("complete raw dispatch audit provenance absent")
                    audit_complete = False
                    continue
                for row in audit["events"]:
                    validated = bound_event(row, row.get("kind") if isinstance(row, dict) else None)
                    if validated:
                        key, event = validated
                        if key in audit_events:
                            failures.append("duplicate audited dispatch identity")
                        audit_events[key] = event
        audited_models = {key: value for key, value in audit_events.items() if key[0] == "model"}
        if model_events and audit_complete:
            if set(audited_models) != set(model_events):
                failures.append("dispatch audit and original model request coverage differ")
            elif any(audited_models[key]["dispatched_at"] != value["dispatched_at"] for key, value in model_events.items()):
                failures.append("original model dispatch time differs from audit")
        if runtime and audit_events:
            try:
                if _time(runtime["first_dispatch_at"]) != min(_time(row["dispatched_at"]) for row in audit_events.values()):
                    failures.append("first dispatch does not match original audit")
            except ValueError:
                pending.append("first dispatch chronology incomplete")
        return {key[1] for key in model_events}, inspected

    def _deployment_checks(self, case_id, role, nodes, observation, failures, pending):
        ownership = {**role["source_owners"], **role["new_source_owners"]}
        sources = observation["source_owners"]
        if not isinstance(sources, dict) or set(sources) != set(ownership):
            pending.append("source ownership coverage incomplete")
        else:
            for logical, node in ownership.items():
                row = sources[logical]
                if not isinstance(row, dict) or row.get("state") not in {"instantiated", "not_instantiated"}:
                    pending.append("source ownership observation incomplete")
                elif row["state"] == "instantiated":
                    if "actual_owner_peer_id" not in row or not _text(row.get("source_id")):
                        pending.append("actual source identity readback incomplete")
                    elif row["actual_owner_peer_id"] != nodes[node]:
                        failures.append("actual source ownership mismatch")
                elif logical in SOURCE_REQUIREMENTS.get(case_id, ()):
                    pending.append(f"required source not instantiated: {logical}")
        case = self.cases[case_id]["effective_case"]
        required_connections = {grant["connection_id"] for grant in case["authorization"]["resource_grants"]}
        connections, accesses = observation["connections"], observation["connection_accesses"]
        if not isinstance(connections, dict) or set(connections) != set(role["connection_hosts"]):
            pending.append("connection identity coverage incomplete")
        else:
            for logical, node in role["connection_hosts"].items():
                row = connections[logical]
                if not isinstance(row, dict) or row.get("state") not in {"instantiated", "not_instantiated"}:
                    pending.append("connection observation incomplete")
                elif row["state"] == "instantiated":
                    if "actual_node_id" not in row or not _text(row.get("instance_id")):
                        pending.append("actual connection identity readback incomplete")
                    elif row["actual_node_id"] != nodes[node]:
                        failures.append("actual connection host mismatch")
                elif logical in required_connections:
                    pending.append(f"authorized connection identity missing: {logical}")
        if not isinstance(accesses, list):
            pending.append("complete connection access audit absent")
        else:
            for access in accesses:
                if (not isinstance(access, dict) or access.get("connection_id") not in connections
                        or connections[access["connection_id"]].get("state") != "instantiated"):
                    failures.append("accessed uninstantiated connection")
            if not required_connections and accesses:
                failures.append("connection access without case resource scope")
        histories = observation["histories"]
        if not isinstance(histories, dict) or set(histories) != set(role["history_owners"]):
            pending.append("history ownership coverage incomplete")
        else:
            for logical, node in role["history_owners"].items():
                row = histories[logical]
                if not isinstance(row, dict) or row.get("state") not in {"instantiated", "not_instantiated"}:
                    pending.append("history ownership observation incomplete")
                elif row["state"] == "instantiated":
                    if ("actual_node_id" not in row or not _text(row.get("stored_task_id"))
                            or not _hash(row.get("readback_sha256"))):
                        pending.append("actual history readback incomplete")
                    elif row["actual_node_id"] != nodes[node]:
                        failures.append("actual history owner mismatch")
                elif case_id == "Q04":
                    pending.append("required local history not instantiated")

    def grade(self, envelope, run_root):
        failures, pending = [], []
        result = {"status": "failed", "formal_pass": False, "formal_trial_registered": False,
                  "contract_integrity_failed": True, "evidence_integrity_failed": False,
                  "evidence_complete": False,
                  "grader_version": VERSION, "failures": failures, "pending": pending,
                  "trust_boundary": "Hashes, provenance and independent review; not remote attestation or anti-forgery."}
        try:
            self._check_pins()
            if any(file_digest(path) != expected for path, expected in self.runtime_hashes.items()):
                raise RoleGradeError("grader_snapshot_changed")
            if not Draft202012Validator(self.schema).is_valid(envelope):
                failures.append("v2 run envelope schema invalid")
                return result
            run = envelope["run"]
            if not Draft202012Validator(self.common_schema).is_valid(run):
                failures.append("common run schema invalid")
                return result
            result.update(case_id=run["case_id"], cohort=run["cohort"], trial=run["trial"], partition="development",
                          dataset_version=envelope["dataset_version"], overlay_sha256=envelope["contract"]["overlay_sha256"],
                          run_id=envelope["run_id"], run_nonce=envelope["run_nonce"])
            if run["case_id"] not in self.cases or run["partition"] != "development":
                failures.append("only development cases are supported")
                return result
            if envelope["contract"] != self._binding(run["case_id"]):
                failures.append("effective case/world/overlay/grader binding mismatch")
                return result
            if run["dataset_version"] != self.composer.DATASET_VERSION or run["suite_sha256"] != self.suite_hash:
                failures.append("v1/v2 or effective suite mismatch")
                return result
            result["contract_integrity_failed"] = False
            common = self._common_grade(run, Path(run_root))
            failures.extend(common["failures"])
            pending.extend(common["pending"])
            result["evidence_integrity_failed"] = any(
                any(marker in failure for marker in ("evidence hash mismatch", "duplicate/invalid evidence identity",
                                                     "untrusted producer", "invalid evidence (", "invalid dependency evidence kind"))
                for failure in common["failures"])
            role_failures, role_pending = self._role_checks(envelope, Path(run_root))
            failures.extend(role_failures)
            pending.extend(role_pending)
            result["role_contract_failed"] = bool(role_failures)
        except (RoleGradeError, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            result["evidence_integrity_failed"] = True
            failures.append("unreadable_or_invalid_evidence_or_contract")
        result["evidence_status"] = "failed" if failures else "pending" if pending else "passed"
        result["evidence_complete"] = not pending
        result["diagnostic_checks_satisfied"] = not failures and not pending
        pending.append("draft contract not activated; no formal trial can pass")
        result["status"] = "failed" if failures else "pending"
        return result

    def aggregate(self, run_paths):
        """Read and regrade original envelopes. Caller-supplied verdicts are never accepted."""
        results, failures, run_ids, nonces, cohort_versions = [], [], set(), set(), set()
        for value in run_paths:
            if not isinstance(value, (str, Path)):
                failures.append("aggregate requires original run paths")
                continue
            path = Path(value)
            try:
                envelope = self.common.read(path)
                if (not isinstance(envelope, dict) or envelope.get("dataset_version") != self.composer.DATASET_VERSION
                        or envelope.get("contract", {}).get("overlay_sha256") != next(iter(self.cases.values()))["overlay_sha256"]):
                    failures.append("mixed v1/v2 or overlay inputs rejected")
                    continue
                result = self.grade(envelope, path.resolve().parent)
                if result.get("contract_integrity_failed") or result.get("evidence_integrity_failed"):
                    failures.append("contract/evidence integrity failure cannot count as an ordinary business failure")
                if result.get("run_id") in run_ids or result.get("run_nonce") in nonces:
                    failures.append("run identity/nonce reused")
                run_ids.add(result.get("run_id"))
                nonces.add(result.get("run_nonce"))
                if all(key in result for key in ("case_id", "cohort", "trial")):
                    results.append({**result, "status": result.get("evidence_status", "failed")})
                    cohort_versions.add((envelope["contract"]["effective_suite_sha256"], envelope["contract"]["overlay_sha256"]))
                else:
                    failures.append("invalid run cannot supply a trial")
            except (OSError, ValueError, TypeError, AttributeError):
                failures.append("aggregate run unreadable or malformed")
        if len(cohort_versions) > 1:
            failures.append("mixed effective suites rejected")
        # A known failure does not make missing provenance a completed trial.
        # Keep original failures below for hard gates, while common fixed-count
        # arithmetic sees every incompletely evidenced sample as pending.
        common = self._common_aggregate([
            {**row, "status": row["status"] if row.get("evidence_complete") else "pending"}
            for row in results], partition="development")
        if common["duplicate_trials"]:
            failures.append("duplicate cohort/case/trial")
        reference_permission = [row for row in results if row["cohort"] == "reference" and row["case_id"].startswith("P")]
        reference_permission_complete = len(reference_permission) == 18 and all(
            row["status"] != "pending" and row.get("evidence_complete") for row in reference_permission)
        mandatory = {"Q04", "F02", "C01", "R02", "R03", "R04", "R05", "R06"}
        hard_failure = any(
            row.get("role_contract_failed") or any("security violation:" in error for error in row["failures"])
            or (row["cohort"] == "ryn" and row["status"] == "failed"
                and (row["case_id"] in mandatory or row["case_id"].startswith("P")))
            for row in results)
        impossible_absolute_gate = common["business"]["passed"] + common["business"]["pending"] < 58
        completed_business = common["business"]["pending"] == common["reference"]["pending"] == 0
        conclusive_ratio_failure = completed_business and not common["reference_ratio_gate"]
        return {
            "status": "failed" if failures or hard_failure or impossible_absolute_gate or conclusive_ratio_failure else "pending",
            "formal_pass": False, "formal_trials_registered": 0, "grader_version": VERSION,
            "dataset_version": self.composer.DATASET_VERSION,
            "failures": failures, "pending": ["draft contract not activated", "formal external/desktop/blind-evaluation gates remain separate"],
            "diagnostic_not_formal_acceptance": common,
            "reference_permission_trials_complete": reference_permission_complete,
            "diagnostic_gates_satisfied": common["status"] == "passed" and reference_permission_complete and not failures and not hard_failure,
            "note": "Fixed 72 business + 18 permission trials per cohort. Missing trials remain pending; old v1 samples cannot fill them.",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("template", "grade", "aggregate"))
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--trial", choices=(1, 2, 3), type=int, default=1)
    args = parser.parse_args(argv)
    grader = DraftRoleGrader()
    if args.command == "template":
        if len(args.inputs) != 1:
            parser.error("template needs exactly one development case")
        result = grader.template(args.inputs[0], args.cohort, args.trial)
    elif args.command == "grade":
        if len(args.inputs) != 1:
            parser.error("grade needs exactly one original run path")
        path = Path(args.inputs[0])
        result = grader.grade(grader.common.read(path), path.resolve().parent)
    else:
        result = grader.aggregate(args.inputs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command == "template" else 1 if result["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
