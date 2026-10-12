"""Opt-in registered collection from live controller objects, never a verdict."""
from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests/fixtures/agent_acceptance"))
import release_registry as registry  # noqa: E402


def stamp():
    return datetime.now(timezone.utc).isoformat()


def add_arguments(parser):
    parser.add_argument("--release-activation", type=Path)
    parser.add_argument("--trial", choices=(1, 2, 3), type=int, default=1)


class ReleaseCollection:
    """Controller objects establish identity; caller JSON cannot substitute.

    node_stores/source_stores/connectors/history_stores are original live store
    objects keyed by logical node. No permission or missing deployment is created
    by this collector. Collectors and operators share the host trust boundary.
    """

    def __init__(self, activation, output, case_id, cohort, trial, *, runtime, task_id,
                 node_stores, source_stores, connectors, history_stores, bindings,
                 planner_policy, native=None, remote_observers=None, subscenario=None, _restore_registration=None):
        self.activation, self.output = Path(activation), Path(output)
        self.case_id, self.cohort, self.trial = case_id, cohort, trial
        self.subscenario = subscenario
        self.runtime, self.task_id = runtime, task_id
        self.nodes, self.sources = dict(node_stores), dict(source_stores)
        self.connectors, self.histories = dict(connectors), dict(history_stores)
        self.bindings, self.planner_policy = dict(bindings), copy.deepcopy(planner_policy)
        self.native, self.events, self.pending = native, [], threading.local()
        self.remote_observers, self.remote_payloads = dict(remote_observers or {}), {}
        self.attached = None
        self.registration_path = self.gate = None
        self.reservation_path = None
        self._active, self.manifest = registry._active(self.activation)
        self.role = registry.composer().load_effective_case(case_id)["role_contract"]
        if (set(self.nodes) & set(self.remote_observers) or self.role["agent_execution_node"] not in self.nodes
                or set(self.nodes) | set(self.remote_observers) != set(registry.composer().LOGICAL_NODES)):
            raise ValueError("release_actual_node_objects_incomplete")
        self.output.mkdir(parents=True, exist_ok=True)
        if self.remote_observers:
            if _restore_registration:
                self.reservation_path = Path(registry.read(_restore_registration)["reservation"]["path"])
            else:
                self.reservation_path = registry.reserve(self.activation, self.manifest["registry_root"], case_id, cohort, trial, subscenario=self.subscenario)
            self._refresh_remote(None)
        self.initial = {"task_id": task_id, "task_sha256": registry.digest(self._task()),
                        "store_path": str(runtime.store.path.resolve()), "node_bindings": self._nodes(),
                        "deployment": self.deployment(), "at": stamp()}
        self.clean = self.output / "release-initial-world.json"
        if _restore_registration is None:
            registry._new(self.clean, self.initial)
        else:
            self._restore(_restore_registration)

    @classmethod
    def restore(cls, registration_path, output, **objects):
        """Reattach original live stores; a path alone is never an identity proof."""
        row = registry.read(registration_path)
        reservation = registry.read(row["reservation"]["path"])
        bound = row["binding"]
        return cls(reservation["activation"]["path"], output, bound["case_id"], bound["cohort"], bound["trial"],
                   subscenario=bound.get("subscenario"), _restore_registration=registration_path, **objects)

    def _restore(self, registration_path):
        path = Path(registration_path).resolve()
        row = registry.read(path)
        registry._check_file(row["reservation"])
        reservation = registry.read(row["reservation"]["path"])
        registry._check_file(reservation["activation"])
        bound = row["binding"]
        slot = registry.slot_path(self.manifest, bound)
        if (path != slot / "registration.json" or Path(row["reservation"]["path"]).resolve() != slot / "reservation.json"
                or bound != reservation["binding"] or bound["manifest_sha256"] != self._active["manifest"]["sha256"]):
            raise ValueError("release_restore_registration_mismatch")
        old = registry.read(self.clean)
        before = row["observation"]
        if (registry.sha(self.clean) != before["clean_world_sha256"] or old["task_id"] != self.task_id
                or old["store_path"] != self.initial["store_path"] or old["node_bindings"] != self._nodes()
                or registry.digest(self.runtime.store.pub) != before["store_key_id_sha256"]):
            raise ValueError("release_restore_store_identity_mismatch")
        self.initial = old
        self._check_deployment()
        self._check_snapshot()
        self.events = [registry.read(p) for p in sorted(self.output.glob("release-request-*.json"))]
        admissions = sorted((slot / "dispatch").glob("*.json"))
        previous_hash = None
        if len(admissions) != len(self.events):
            raise ValueError("release_restore_dispatch_coverage_mismatch")
        for index, (admission, event) in enumerate(zip(admissions, self.events, strict=True), 1):
            receipt = registry.read(admission)
            if (receipt["binding"] != bound or receipt["registration_sha256"] != registry.sha(path)
                    or receipt["sequence"] != index or receipt["previous_sha256"] != previous_hash
                    or receipt["request"] != {"kind": event["kind"], "id": event["id"]}
                    or event["task_id"] != self.task_id or event["sampling_binding"] != row["role_proof"]["binding"]
                    or event["registration_sha256"] != registry.sha(path)):
                raise ValueError("release_restore_dispatch_history_mismatch")
            previous_hash = registry.sha(admission)
        self._restore_dispatch_links()
        self.registration_path, self.reservation_path = path, Path(row["reservation"]["path"])
        self.gate = registry.DispatchGate(path, self.observe)
        self.restored_event_count = len(self.events)
        if self.cohort == "reference":
            if not self.native or self.native.get("session_id") != before["reference"]["native_session_id"]:
                raise ValueError("release_restore_native_session_mismatch")
            actual = self._reference()
            if any(actual[k] != before["reference"][k] for k in ("commit", "node_sha256", "native_session_id")):
                raise ValueError("release_restore_native_identity_mismatch")
        # Reconstruct the same actual owner/task context before installing hooks.
        current = self.observe(bound, None)
        stable = set(before) - {"observed_at", "prior_dispatch_count", "pending_request", "reference", "actual_deployment"}
        if any(current[k] != before[k] for k in stable):
            raise ValueError("release_restore_live_identity_mismatch")

    def _restore_dispatch_links(self):
        """Rebuild derived links from original sidecars and actual durable usage."""
        events = {event["id"]: event for event in self.events}
        records = {record["id"]: record for record in self._task()["budget"]["records"]}
        immutable = ("id", "kind", "stage", "round", "input_reservation", "output_reservation")
        for path in sorted(self.output.glob("release-budget-*.json")):
            link = registry.read(path)
            reserved = link["reservation"]
            record = records.get(reserved["id"])
            parent = events.get(link["parent_dispatch_id"])
            if (path.name != f"release-budget-{reserved['id']}.json" or reserved["status"] != "reserved"
                    or record is None or any(record[k] != reserved[k] for k in immutable)
                    or parent is None or parent["basis"] != "original_native_rpc"):
                raise ValueError("release_restore_budget_link_mismatch")
            if reserved["kind"] == "model" and "probe" in reserved["stage"]:
                event = events.get(reserved["id"])
                if (event is None or event["basis"] != "original_native_probe"
                        or event.get("parent_dispatch_id") != parent["id"]
                        or event.get("budget_record_id") != reserved["id"]):
                    raise ValueError("release_restore_budget_probe_mismatch")
            else:
                if parent["kind"] != reserved["kind"] or parent.get("budget_record_id", reserved["id"]) != reserved["id"]:
                    raise ValueError("release_restore_budget_parent_mismatch")
                parent["budget_record_id"] = reserved["id"]
        for path in sorted(self.output.glob("release-nested-*.json")):
            nested = registry.read(path)
            parent = events.get(nested["parent_dispatch_id"])
            if (parent is None or parent.get("tool_name") != "ryn_get_system_diagnostics"
                    or nested.get("tool_name") != "ryn_list_devices"):
                raise ValueError("release_restore_nested_parent_mismatch")
            parent.setdefault("nested_calls", []).append(nested)

    def _task(self):
        return self.runtime.store.read()["tasks"][self.task_id]

    def _refresh_remote(self, request):
        if not self.remote_observers:
            return
        bound = registry.read(self.reservation_path)["binding"]
        snapshot = registry.read(self.manifest["artifacts"]["candidate_snapshot"]["path"])
        pins = {str(Path(item["path"]).resolve()): item["sha256"] for item in snapshot["files"]}
        payloads = {}
        for label, remote in self.remote_observers.items():
            verifier = remote["verify"]
            source = inspect.getsourcefile(verifier)
            if source is None or pins.get(str(Path(source).resolve())) != registry.sha(source):
                raise ValueError("release_remote_verifier_not_frozen")
            challenge = {"challenge": uuid.uuid4().hex, "task_id": self.task_id, "request": request,
                         "run_nonce": bound["run_nonce"], "manifest_sha256": bound["manifest_sha256"]}
            envelope = remote["observe"](copy.deepcopy(challenge))
            payload = verifier(envelope, challenge, remote["peer_id"])
            if payload["peer_id"] != remote["peer_id"]:
                raise ValueError("release_remote_peer_changed")
            files = payload["state"]["code"]["python_files"]
            expected = {Path(path).relative_to(ROOT).as_posix(): sha for path, sha in pins.items()
                        if Path(path).is_relative_to(ROOT) and Path(path).suffix == ".py"
                        and Path(path).relative_to(ROOT).parts[0] == "rynmesh"}
            if not expected or any(files.get(path) != sha for path, sha in expected.items()):
                raise ValueError("release_remote_source_snapshot_changed")
            if payload["state"]["code"]["capture_implementation_sha256"] != registry.sha(source):
                raise ValueError("release_remote_capture_implementation_changed")
            registry._new(self.output / f"release-remote-{label}-{challenge['challenge']}.json", envelope)
            payloads[label] = payload
        self.remote_payloads = payloads

    def _nodes(self):
        nodes = {name: store.peer_id for name, store in self.nodes.items()}
        nodes.update({name: payload["peer_id"] for name, payload in self.remote_payloads.items()})
        if len(set(nodes.values())) != len(nodes):
            raise ValueError("release_node_identity_collision")
        for store in self.nodes.values():
            if not (store.home / "identity.ed25519").is_file():
                raise ValueError("release_node_identity_not_persisted")
        return nodes

    def deployment(self):
        """Keep absence explicit; never turn declared ownership into existence."""
        result = {"source_owners": {}, "connections": {}, "histories": {}, "connection_accesses": []}
        owners = {**self.role["source_owners"], **self.role["new_source_owners"]}
        node_ids = self._nodes()
        source_rows = {name: list(store.read().values()) for name, store in self.sources.items()}
        source_rows.update({name: payload["state"]["source_store"]["records"] for name, payload in self.remote_payloads.items()})
        for logical, owner in owners.items():
            identity = self.bindings.get(logical)
            for other, rows in source_rows.items():
                if other != owner and any((r["id"] == identity if identity else r.get("name") == logical) for r in rows):
                    raise ValueError("release_source_owner_changed")
            store = self.sources.get(owner)
            observed = {"state": "not_instantiated"}
            if store is not None or owner in self.remote_payloads:
                if store is not None and store.path.resolve() != (self.nodes[owner].home / "llm/sources.enc").resolve():
                    raise ValueError("release_source_store_wrong_node")
                rows = source_rows.get(owner, [])
                identity = self.bindings.get(logical)
                matches = [r for r in rows if r["id"] == identity] if identity else [r for r in rows if r.get("name") == logical]
                if len(matches) > 1:
                    raise ValueError("release_source_identity_ambiguous")
                if matches:
                    observed = {"state": "instantiated", "actual_owner_peer_id": node_ids[owner],
                                "source_id": matches[0]["id"]}
            result["source_owners"][logical] = observed
        for logical, owner in self.role["connection_hosts"].items():
            plugin = self.connectors.get(owner)
            observed = {"state": "not_instantiated"}
            if plugin is not None:
                if plugin.directory.resolve() != (self.nodes[owner].home / "plugins/nas").resolve():
                    raise ValueError("release_connector_wrong_node")
                with plugin.lock:
                    identity = self.bindings.get(logical)
                    rows = [r for r in plugin.state["sources"] if r["id"] == identity]
                if len(rows) == 1:
                    observed = {"state": "instantiated", "actual_node_id": self.nodes[owner].peer_id,
                                "instance_id": rows[0]["id"]}
            elif owner in self.remote_payloads:
                rows = self.remote_payloads[owner]["state"]["connections"]["instances"]
                matches = [r for r in rows if r["id"] == self.bindings.get(logical)]
                if len(matches) == 1:
                    observed = {"state": "instantiated", "actual_node_id": node_ids[owner], "instance_id": matches[0]["id"]}
            result["connections"][logical] = observed
        for logical, owner in self.role["history_owners"].items():
            store = self.histories.get(owner)
            observed = {"state": "not_instantiated"}
            if store is not None:
                if store.root.resolve() != (self.nodes[owner].home / "agent").resolve():
                    raise ValueError("release_history_wrong_node")
                task = store.read()["tasks"].get(self.bindings.get(logical, logical))
                if task:
                    observed = {"state": "instantiated", "actual_node_id": self.nodes[owner].peer_id,
                                "stored_task_id": task["id"], "readback_sha256": registry.digest(task)}
            elif owner in self.remote_payloads:
                rows = self.remote_payloads[owner]["state"]["history"]["task_ids_readback"]
                matches = [r for r in rows if r["id"] == self.bindings.get(logical, logical)]
                if len(matches) == 1:
                    observed = {"state": "instantiated", "actual_node_id": node_ids[owner],
                                "stored_task_id": matches[0]["id"], "readback_sha256": matches[0]["sha256"]}
            result["histories"][logical] = observed
        for event in self.events:
            connection = event.get("connection_id")
            if connection:
                result["connection_accesses"].append({"connection_id": connection, "request_id": event["id"]})
        return result

    def _check_deployment(self):
        current = self.deployment()
        # Identity and owner stay bound. Source settings/version/sharing are
        # business state and may change through the original authorized tools.
        for group in ("source_owners", "connections", "histories"):
            for logical, before in self.initial["deployment"][group].items():
                after = current[group][logical]
                if before["state"] == "instantiated":
                    keys = set(before) - {"readback_sha256"}
                    if any(after.get(key) != before[key] for key in keys):
                        raise ValueError("release_bound_deployment_changed")
        return current

    def _check_snapshot(self):
        name = "candidate_snapshot" if self.cohort == "ryn" else "reference_snapshot"
        snapshot = self.manifest["artifacts"][name]
        registry._check_file(snapshot)
        for item in registry.read(snapshot["path"])["files"]:
            registry._check_file(item)
        policy = self.manifest["artifacts"]["model_policy"]
        registry._check_file(policy)
        expected = registry.read(policy["path"])
        if not self.planner_policy or self.planner_policy.get("model_id") != expected["model"]:
            raise ValueError("release_live_planner_policy_missing")
        # Read the original policy whitelist, never raw source credentials.
        public = {k: self.planner_policy[k] for k in ("version", "source_id", "model_id", "generation")}
        if registry.digest(public) != self.planner_policy.get("policy_sha256"):
            raise ValueError("release_planner_policy_changed")
        for phase in expected["phases"].values():
            if phase["request_defaults"] != public["generation"]["request_defaults"]:
                raise ValueError("release_phase_policy_mismatch")
        task = self._task()
        limits = expected["case_limits"][self.case_id]
        rounds = limits.get("round_limits", [])
        lifetimes = limits.get("lifetime_limits", [])
        round_index = task["budget"]["round"] - 1
        if (not isinstance(lifetimes, list) or len(lifetimes) != len(rounds)
                or not 0 <= round_index < len(rounds) or task["limits"] != rounds[round_index]
                or task["budget"]["limits"] != lifetimes[round_index]):
            raise ValueError("release_actual_budget_policy_mismatch")
        # Flat fields describe the overall ceiling; each original round (e.g.
        # R06's explicit 4→12 lifetime extension) must also match exactly.
        flat = {key: max(row[field] for row in lifetimes) for key, field in
                (("max_model_calls", "max_total_model_calls"), ("max_tool_calls", "max_total_tool_calls"),
                 ("max_tokens", "max_total_tokens"), ("max_seconds", "max_total_seconds"))}
        flat["max_input_chars"] = max(row["max_context_chars"] for row in rounds)
        if any(limits.get(key) != value for key, value in flat.items()):
            raise ValueError("release_actual_budget_policy_mismatch")
        model = self.runtime.catalog.resolve(task["model_id"])
        if any(model.get(key) != public["generation"].get(key) for key in ("context_window", "max_output_tokens")):
            raise ValueError("release_actual_generation_policy_mismatch")
        self.current_policy = expected
        return snapshot["sha256"], policy["sha256"]

    def _check_model_reservation(self, record):
        self._check_snapshot()
        stage = record["stage"]
        phase = "probe" if "probe" in stage else "summary" if stage in {"compact", "summary"} else "checker" if stage in {"verify", "verification", "checker"} else "main"
        cap = self.current_policy["phases"][phase]
        if stage == "reference_inference":
            phases = self.current_policy["phases"]
            if any(phases[p] != phases["main"] for p in ("checker", "summary")):
                raise ValueError("release_native_phase_binding_missing")
        if record["input_reservation"] > cap["max_context_bytes"] + 512 or record["output_reservation"] > cap["max_output_tokens"]:
            raise ValueError("release_actual_request_cap_mismatch")

    def attach_reference_meter(self, meter):
        """Bind the actual meter instance, including probes without model RPCs."""
        if self.cohort != "reference" or meter.runtime is not self.runtime or meter.task_id != self.task_id:
            raise ValueError("release_reference_meter_mismatch")
        original = meter._run

        def run(kind, stage, invoke, **options):
            def reserved(allowed):
                records = [r for r in self._task()["budget"]["records"] if r["kind"] == kind and r["status"] == "reserved"]
                if len(records) != 1:
                    raise ValueError("release_reference_reservation_ambiguous")
                record = records[0]
                if kind == "model":
                    self._check_model_reservation(record)
                parent = getattr(self.pending, "request", None)
                event = next((e for e in reversed(self.events) if parent and e["id"] == parent["id"]), None)
                if event is None or event.get("basis") != "original_native_rpc":
                    raise ValueError("release_reference_rpc_parent_missing")
                registry._new(self.output / f"release-budget-{record['id']}.json",
                              {"parent_dispatch_id": parent["id"], "reservation": copy.deepcopy(record)})
                if kind == "model" and "probe" in stage:
                    return self.dispatch("model", record["id"], lambda: invoke(allowed), basis="original_native_probe",
                                         parent_dispatch_id=parent["id"], budget_record_id=record["id"], stage=stage)
                if parent["kind"] != kind:
                    raise ValueError("release_reference_dispatch_kind_mismatch")
                event["budget_record_id"] = record["id"]
                return invoke(allowed)
            return original(kind, stage, reserved, **options)

        meter._run = run
        self.reference_meter = (meter, run)

    def observe(self, challenge, request):
        self._refresh_remote(request)
        task = self._task()
        if self.case_id == "P04":
            expected_prompt = registry.subscenarios("P04")[self.subscenario]["prompt"]
            owners = [m["content"] for m in task["messages"] if m["role"] == "user"]
            if owners != [expected_prompt]:
                raise ValueError("release_p04_actual_session_prompt_mismatch")
        context = self.runtime._execution_context(task)
        nodes = self._nodes()
        execution = {"runtime_consumer_node_id": self.runtime.consumer_node_id,
                     "agent_store_consumer_node_id": context["consumer_node_id"], "owner_id": context["owner_id"]}
        expected = registry.composer().load_effective_case(self.case_id, node_bindings=nodes,
                                                          execution_identity=execution)["identity_binding"]
        execution_node = self.nodes[self.role["agent_execution_node"]]
        if (self.runtime.tools.store is not execution_node or self.runtime.owner_id != context["owner_id"]
                or self.runtime.store.root.resolve() != (execution_node.home / "agent").resolve()):
            raise ValueError("release_runtime_storage_identity_mismatch")
        actual_deployment = self._check_deployment()
        snapshot, policy = self._check_snapshot()
        actual_pending = getattr(self.pending, "request", None)
        if request != actual_pending:
            raise ValueError("release_original_pending_request_mismatch")
        if request is not None and getattr(self.pending, "basis", None) == "model_reservation":
            records = task.get("budget", {}).get("records", [])
            if not any(r["id"] == request["id"] and r["kind"] == "model" and r["status"] == "reserved" for r in records):
                raise ValueError("release_model_reservation_not_pending")
        reference = self._reference() if self.cohort == "reference" else None
        return {"binding": challenge, "observed_at": stamp(), "task_id": task["id"], "node_bindings": nodes,
                "execution_identity": execution,
                "deployment": {key: expected[key] for key in ("source_owner_ids", "new_source_owner_ids", "connection_host_ids",
                    "history_owner_ids", "model_provider_ids", "model_recipient_ids", "default_agent_owner_id")},
                "actual_deployment": actual_deployment,
                "store_path_sha256": registry.digest(str(self.runtime.store.path.resolve())),
                "store_key_id_sha256": registry.digest(self.runtime.store.pub), "decrypted_task_id": task["id"],
                "source_snapshot_sha256": snapshot, "model_policy_sha256": policy, "clean_world_sha256": registry.sha(self.clean),
                "prior_dispatch_count": len(self.events), "pending_request": actual_pending, "reference": reference}

    def _reference(self):
        if not self.native:
            raise ValueError("release_native_session_not_observed")
        home, node, upstream = (Path(self.native[k]).resolve() for k in ("home", "node", "upstream"))
        database = home / "reference-session.sqlite"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            sessions = [r[0] for r in connection.execute("select id from session")]
        if sessions != [self.native["session_id"]]:
            raise ValueError("release_native_session_mismatch")
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=upstream, text=True).strip()
        return {"commit": commit, "working_tree_clean": not dirty, "node_sha256": registry.sha(node),
                "native_session_id": sessions[0], "native_session_store_sha256": registry.sha(database),
                "rpc_audit_ready": Path(self.native["audit_path"]).is_file()}

    def register(self):
        if self.registration_path is not None:
            raise ValueError("release_already_registered")
        if self.events or any(r["kind"] == "model" for r in self._task().get("budget", {}).get("records", [])):
            raise ValueError("release_task_already_dispatched")
        if self.reservation_path is None:
            self.reservation_path = registry.reserve(self.activation, self.manifest["registry_root"], self.case_id,
                                                       self.cohort, self.trial, subscenario=self.subscenario)
        self.registration_path = registry.register_reserved(self.reservation_path, self.observe)
        self.gate = registry.DispatchGate(self.registration_path, self.observe)
        row = registry.read(self.registration_path)
        registry._new(self.output / "release-task-binding.json", {"role_proof": {"kind": "task_binding",
            "binding": row["role_proof"]["binding"], "observations": {"task_id": self._task()["id"],
            "registration_sha256": registry.sha(self.registration_path), "bound_at": stamp()}}})
        return self.registration_path

    def native_config(self):
        if self.cohort != "reference" or self.native is None:
            raise ValueError("release_native_reservation_invalid")
        segments = sorted(self.output.glob("release-native-segment-*.json"))
        audit = Path(self.native["audit_path"]).resolve()
        if audit.exists() or any(registry.read(p)["audit_path"] == str(audit) for p in segments):
            raise ValueError("release_native_audit_path_reused")
        if self.reservation_path is None:
            self.reservation_path = registry.reserve(self.activation, self.manifest["registry_root"], self.case_id,
                                                     self.cohort, self.trial, subscenario=self.subscenario)
        elif self.registration_path is None:
            raise ValueError("release_native_reservation_invalid")
        registry._new(self.output / f"release-native-segment-{len(segments) + 1:06}.json",
                      {"audit_path": str(audit), "sampling_binding": registry.read(self.reservation_path)["binding"]})
        return {"sampling_binding": registry.read(self.reservation_path)["binding"], "task_id": self.task_id,
                # Explicitly unregistered until the first actual native session
                # and pending RPC have been read; this is not a fake digest.
                "registration_sha256": None, "audit_path": str(audit)}

    def model_request(self):
        request = getattr(self.pending, "request", None)
        if not request or request["kind"] != "model" or not self.events or self.events[-1]["id"] != request["id"]:
            raise ValueError("release_original_model_dispatch_missing")
        return copy.deepcopy(self.events[-1])

    def dispatch(self, kind, identity, invoke, *, basis, **facts):
        if self.gate is None:
            raise ValueError("release_registration_required_before_dispatch")
        previous = (getattr(self.pending, "request", None), getattr(self.pending, "basis", None))
        self.pending.request, self.pending.basis = {"kind": kind, "id": identity}, basis
        def execute():
            event = {"id": identity, "kind": kind, "task_id": self.task_id, "basis": basis,
                     "dispatched_at": stamp(), "registration_sha256": registry.sha(self.registration_path), **facts}
            event["sampling_binding"] = registry.read(self.registration_path)["role_proof"]["binding"]
            registry._new(self.output / f"release-request-{len(self.events) + 1:06}.json", event)
            self.events.append(event)
            return invoke()
        try:
            return self.gate.dispatch(kind, identity, execute)
        finally:
            self.pending.request, self.pending.basis = previous

    def attach_candidate(self):
        if self.cohort != "ryn" or self.gate is None:
            raise ValueError("release_candidate_not_registered")
        runtime = self.runtime
        names = ("_meter_model", "execute", "_internal_tool", "_read_call")
        owners = (runtime, runtime.tools, runtime, runtime)
        mux = getattr(runtime, "_release_candidate_mux", None)
        if mux is None:
            mux = {"original": tuple(getattr(owner, name) for owner, name in zip(owners, names, strict=True)),
                   "collectors": {}, "hooks": {}}
            runtime._release_candidate_mux = mux
        elif self.task_id in mux["collectors"] or any(
                self.case_id != "P04" or other.case_id != "P04" or self.trial != other.trial
                or self.cohort != other.cohort or self._nodes() != other._nodes()
                for other in mux["collectors"].values()):
            raise ValueError("release_shared_runtime_not_original_p04")
        meter, execute, internal, read_call = mux["original"]
        def model(selected, invoke, **options):
            if selected["id"] != self.task_id:
                raise ValueError("release_other_task_dispatch")
            def original(allowed):
                records = self._task()["budget"]["records"]
                reserved = [r for r in records if r["kind"] == "model" and r["status"] == "reserved"]
                if len(reserved) != 1:
                    raise ValueError("release_model_reservation_ambiguous")
                self._check_model_reservation(reserved[0])
                return self.dispatch("model", reserved[0]["id"], lambda: invoke(allowed), basis="model_reservation",
                                     stage=reserved[0]["stage"])
            return meter(selected, original, **options)
        runtime._meter_model = model
        def tool(name, arguments, **options):
            identity = options.get("operation_id")
            parent = getattr(self.pending, "request", None)
            parent_event = next((e for e in reversed(self.events) if parent and e["id"] == parent["id"]), None)
            if (not identity and parent_event and parent_event.get("tool_name") == "ryn_get_system_diagnostics"
                    and name == "ryn_list_devices" and arguments == {} and runtime.tools.metadata(name)["effect"] == "read"):
                # This synchronous child has no independent persisted request
                # ID in production. Preserve its actual parent, never invent one.
                nested = {"parent_dispatch_id": parent["id"], "tool_name": name, "dispatched_at": stamp()}
                children = parent_event.setdefault("nested_calls", [])
                registry._new(self.output / f"release-nested-{registry.digest(parent)}-{len(children) + 1}.json", nested)
                children.append(nested)
                return execute(name, arguments, **options)
            if not identity:
                raise ValueError("release_original_tool_operation_missing")
            context = options.get("context")
            if context is not None and context.get("task_id") != self.task_id:
                raise ValueError("release_tool_task_mismatch")
            durable = self._task()
            flight = durable.get("in_flight") or {}
            reserved = [r for r in durable["budget"]["records"] if r["kind"] == "tool" and r["status"] == "reserved"]
            normal = (flight.get("kind") == "tool" and flight.get("dispatched") is True
                      and flight.get("operation_id") == identity and flight.get("name") == name
                      and flight.get("arguments") == arguments)
            readback = (parent and getattr(self.pending, "basis", None) == "read_reservation"
                        and identity.startswith("verification-read-") and runtime.tools.metadata(name)["effect"] == "read"
                        and any(r.get("status") == "confirmed" and r["tool"] == name and r["arguments"] == arguments
                                for r in durable.get("receipts", [])))
            if not (normal or readback) or len(reserved) != 1:
                raise ValueError("release_tool_durable_intent_mismatch")
            return self.dispatch("tool", identity, lambda: execute(name, arguments, **options),
                                 basis="original_tool_operation", tool_name=name, arguments_sha256=registry.digest(arguments),
                                 budget_record_id=reserved[0]["id"])
        runtime.tools.execute = tool
        def internal_tool(selected, started):
            durable = self._task()
            call = selected["queue"][0]
            if not durable["queue"] or registry.digest(call) != registry.digest(durable["queue"][0]):
                raise ValueError("release_internal_queue_mismatch")
            return self.dispatch("tool", call["operation_id"], lambda: internal(selected, started),
                                 basis="durable_internal_queue", tool_name=call["name"])
        runtime._internal_tool = internal_tool
        def read(selected, stage, invoke):
            def original():
                reserved = [r for r in self._task()["budget"]["records"] if r["kind"] == "tool" and r["status"] == "reserved"]
                if len(reserved) != 1:
                    raise ValueError("release_read_reservation_ambiguous")
                return self.dispatch("tool", reserved[0]["id"], invoke, basis="read_reservation", stage=stage)
            return read_call(selected, stage, original)
        runtime._read_call = read
        self.attached = (model, tool, internal_tool, read)
        mux["collectors"][self.task_id] = self
        mux["hooks"][self.task_id] = self.attached
        if "dispatchers" not in mux:
            def selected_hook(index, identity):
                if identity not in mux["hooks"]:
                    raise ValueError("release_unregistered_task_dispatch")
                return mux["hooks"][identity][index]

            def dispatch_model(selected, *args, **kwargs):
                return selected_hook(0, selected["id"])(selected, *args, **kwargs)

            def dispatch_tool(name, arguments, **options):
                context = options.get("context") or {}
                identity = context.get("task_id")
                if identity is None:
                    candidates = [key for key, item in mux["collectors"].items()
                                  if getattr(item.pending, "request", None)
                                  or (options.get("operation_id") and (item._task().get("in_flight") or {}).get("operation_id")
                                      == options["operation_id"])]
                    if not candidates and len(mux["collectors"]) == 1:
                        candidates = list(mux["collectors"])
                    if len(candidates) != 1:
                        raise ValueError("release_tool_task_ambiguous")
                    identity = candidates[0]
                return selected_hook(1, identity)(name, arguments, **options)

            def dispatch_internal(selected, *args, **kwargs):
                return selected_hook(2, selected["id"])(selected, *args, **kwargs)

            def dispatch_read(selected, *args, **kwargs):
                return selected_hook(3, selected["id"])(selected, *args, **kwargs)

            mux["dispatchers"] = (dispatch_model, dispatch_tool, dispatch_internal, dispatch_read)
        for owner, name, dispatch in zip(owners, names, mux["dispatchers"], strict=True):
            setattr(owner, name, dispatch)

    def invoke(self, method, params, request_context, invoke):
        """Optional native observer protocol; called around business_dispatch."""
        if method == "approval":
            return invoke()
        if method not in {"model", "tool"} or not isinstance(request_context, dict):
            raise ValueError("release_native_original_request_missing")
        if self.native is None:
            raise ValueError("release_native_configuration_missing")
        reserved = registry.read(self.reservation_path)["binding"]
        if (request_context.get("sampling_binding") != reserved or request_context.get("task_id") != self.task_id
                or request_context.get("method") != method or request_context.get("registration_sha256") is not None
                or request_context.get("id") != f"{request_context.get('session_id')}:rpc:{request_context.get('rpc_id')}"):
            raise ValueError("release_native_reservation_mismatch")
        if self.native.get("session_id") not in {None, request_context["session_id"]}:
            raise ValueError("release_native_session_changed")
        self.native["session_id"] = request_context["session_id"]
        # The worker fsyncs this original RPC before handing it to the host.
        # Do not accept a manually composed identity as a live native request.
        native_rows = [json.loads(line) for line in Path(self.native["audit_path"]).read_text(encoding="utf-8").splitlines()]
        if not native_rows or native_rows[-1] != request_context:
            raise ValueError("release_native_rpc_not_persisted")
        if self.registration_path is None:
            self.register()
        return self.dispatch(method, request_context["id"], invoke, basis="original_native_rpc",
                             native_rpc=copy.deepcopy(request_context),
                             **({"tool_name": params["name"], "tool_call_id": params["tool_call_id"]} if method == "tool" else {}))

    def finalize(self):
        task = self._task()
        context = self.runtime._execution_context(task)
        mux = getattr(self.runtime, "_release_candidate_mux", {})
        complete = (mux.get("hooks", {}).get(self.task_id) == self.attached and self.attached is not None
                    and mux.get("dispatchers") == (self.runtime._meter_model, self.runtime.tools.execute,
                                                  self.runtime._internal_tool, self.runtime._read_call))
        if self.cohort == "ryn":
            model_ids = {r["id"] for r in task["budget"]["records"] if r["kind"] == "model"}
            tool_ids = {r["id"] for r in task["budget"]["records"] if r["kind"] == "tool"}
            captured_models = {e["id"] for e in self.events if e["kind"] == "model" and e["basis"] != "original_receiver_probe"}
            captured_tools = {e.get("budget_record_id", e["id"]) for e in self.events if e["kind"] == "tool"}
            complete = complete and model_ids == captured_models and tool_ids <= captured_tools
        native_readback = None
        if self.cohort == "reference" and self.registration_path is not None:
            rows = []
            for segment in sorted(self.output.glob("release-native-segment-*.json")):
                metadata = registry.read(segment)
                if metadata["sampling_binding"] != registry.read(self.registration_path)["binding"]:
                    raise ValueError("release_native_segment_binding_changed")
                rows.extend(json.loads(line) for line in Path(metadata["audit_path"]).read_text(encoding="utf-8").splitlines())
            actual = [r for r in rows if r["method"] in {"model", "tool"}]
            complete = bool(actual) and actual == [e["native_rpc"] for e in self.events if e["basis"] == "original_native_rpc"]
            budget_ids = {r["id"] for r in task["budget"]["records"]}
            captured_ids = {e.get("budget_record_id") for e in self.events}
            attached = getattr(self, "reference_meter", None)
            old_native_only = (getattr(self, "restored_event_count", 0) > 0
                and all(e["basis"] == "original_receiver_probe" for e in self.events[self.restored_event_count:])
                and all(r["status"] != "reserved" for r in task["budget"]["records"]))
            meter_bound = attached is not None and attached[0]._run is attached[1]
            complete = complete and (meter_bound or old_native_only) and budget_ids <= captured_ids
            database = Path(self.native["home"]) / "reference-session.sqlite"
            snapshot = self.output / "native-session-readback.sqlite"
            with snapshot.open("xb"):
                pass
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
                sessions = [r[0] for r in target.execute("select id from session")]
            if sessions != [self.native["session_id"]]:
                raise ValueError("release_native_final_session_mismatch")
            native_readback = {"path": str(snapshot.resolve()), "sha256": registry.sha(snapshot),
                "session_id": sessions[0], "observed_request_ids": [e["id"] for e in self.events if e["kind"] == "model"],
                "original_budget_records": copy.deepcopy(task["budget"]["records"]),
                "proof_scope": "native_session_and_request_readback", "task_id": self.task_id,
                "run_nonce": registry.read(self.registration_path)["binding"]["run_nonce"]}
        external = [e for e in self.events if e["basis"] == "original_receiver_probe"]
        original_receivers = sorted(self.output.glob("release-receiver-*.json"))
        if external or original_receivers:
            complete = complete and len(external) == len(original_receivers)
            for event in external:
                descriptor = event.get("original_request", {})
                registry._check_file(descriptor)
                proof = registry.read(descriptor["path"])
                if (Path(descriptor["path"]).resolve() not in {p.resolve() for p in original_receivers}
                        or proof.get("request_id") != event["id"]
                        or proof.get("sender_peer_id") != event.get("sender_peer_id")
                        or proof.get("provider_peer_id") != event.get("provider_peer_id")):
                    raise ValueError("release_receiver_request_readback_mismatch")
            if native_readback is not None:
                native_readback["external_receiver_requests"] = [e["original_request"] for e in external]
                native_readback["external_request_scope"] = "Signed controller postcondition probes; not native model RPCs."
        registered = registry.read(self.registration_path) if self.registration_path else None
        readback = {k: copy.deepcopy(task[k]) for k in
                    ("id", "status", "model_id", "permission_mode", "scope_revision", "model_destination")}
        return {"registration": registry.binding(self.registration_path) if self.registration_path else None,
                "dispatch_audit": {"coverage": "all_model_and_tool_dispatches" if complete else "partial_dispatch_collection",
                                   "events": copy.deepcopy(self.events)},
                "model_requests": [copy.deepcopy(e) for e in self.events if e["kind"] == "model"],
                "deployment": self.deployment(), "task_readback": readback, "task_readback_sha256": registry.digest(readback),
                "decrypted_from_store": True,
                "reference_session_readback": native_readback,
                "authenticated_runtime": {"run_nonce": registered["binding"]["run_nonce"] if registered else None,
                    "peer_id": self.runtime.consumer_node_id, "owner_id": self.runtime.owner_id,
                    "method": "in_process_owner_key_and_runtime",
                    "identity_key_sha256": registry.sha(self.runtime.tools.store.home / "identity.ed25519")},
                "runtime": {"runtime_consumer_node_id": self.runtime.consumer_node_id, "runtime_owner_id": self.runtime.owner_id,
                    "task_context_consumer_node_id": context["consumer_node_id"], "task_context_owner_id": context["owner_id"],
                    "tools_store_peer_id": self.runtime.tools.store.peer_id,
                    "task_context_binding_basis": "Actual Runtime._execution_context of decrypted task and original store objects.",
                    "runtime_role": "ryn_agent_executor" if self.cohort == "ryn" else "reference_accounting_and_tool_permission_host",
                    "first_dispatch_at": self.events[0]["dispatched_at"] if self.events else None},
                "agent_store_path": str(self.runtime.store.path), "agent_store_encryption_public_key": self.runtime.store.pub,
                "finished_at": stamp()}
