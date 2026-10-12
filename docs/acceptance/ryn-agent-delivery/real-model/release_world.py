"""Shared isolated provider/consumer setup for explicit registered controllers.

Reuses the existing signed local transport. It proves separate persisted logical
nodes, not two physical hosts, ICE, or business success.
"""
from __future__ import annotations

import copy

from access_query import AccessQuery
from fastapi import FastAPI
from query_cases import QueryWorld
from role_query_world import composer
from sharing_cases import SharingWorld

from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class ReleaseWorld(QueryWorld):
    def __init__(self, home, case_id, planner_policy):
        # Only the existing public frozen world is reused. Task prompts below
        # come from the selected unchanged case, never from this setup case.
        super().__init__(home, "Q06")
        effective = composer.load_effective_case(case_id)
        self.case = copy.deepcopy(effective["effective_case"])
        self.role = effective["role_contract"]
        self.planner_policy = planner_policy
        self.instantiated_nodes = {}

    def registry_store(self, label, store):
        actual = store if label == "node-a" else QueryWorld.registry_store(self, label, store)
        self.instantiated_nodes[label] = actual
        return actual

    def configure_access(self, app, store):
        self.access_query = AccessQuery(self, app, store)
        self.instantiated_nodes["node-b"] = self.access_query.receiver_store

    def __enter__(self):
        super().__enter__()
        try:
            self.owner_store = RynmeshStore(home=self.home / "node-a", network_dir=self.home / "fixed-network")
            self.owner_app = FastAPI()
            key = peer_box.load_or_create_messaging_key(self.owner_store.home / "messaging.x25519")
            install_llm_routes(self.owner_app, store=self.owner_store, home=self.owner_store.home, messaging_key=key,
                               resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
            self.stack.callback(self.owner_app.state.llm_shutdown)
            self.configure(self.owner_app, self.owner_store)
            self.consumer = self.access_query.consumer
            self.execution_store = self.owner_store if self.role["agent_execution_node"] == "node-a" else self.consumer.store
            self.execution_app = self.owner_app if self.role["agent_execution_node"] == "node-a" else self.consumer.app
            self.provider_selected = self.bindings["model-a"]
            self.selected = self.provider_selected if self.role["agent_execution_node"] == "node-a" else self.consumer.selected
            return self
        except BaseException:
            self.stack.close()
            raise

    def source_stores(self):
        return {"node-a": self.sources, "node-b": self.consumer.sources}

    def install_provider(self, adapter, unchanged):
        """Use the existing live adapter only after exact source-policy checks."""
        from planner_policy import assert_mapping

        from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
        services = self.owner_app.state.llm_agent_services
        original = services["provider_for"]

        def provider_for(service_id):
            manager = original(service_id)
            if service_id == self.provider_selected.removeprefix("local/"):
                unchanged()
                assert_mapping(self.sources, self.provider_selected, self.planner_policy)
                if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                    raise RuntimeError("acceptance_model_dispatch_gate_missing")
                manager.adapter.inner = adapter
            return manager

        services["provider_for"] = provider_for

    def collector(self, activation, output, cohort, trial, runtime, task, *, connectors=None, histories=None,
                  bindings=None, node=None, upstream=None, subscenario=None):
        from release_collection import ReleaseCollection
        return ReleaseCollection(activation, output, self.case["id"], cohort, trial,
            runtime=runtime, task_id=task["id"], node_stores=self.instantiated_nodes, source_stores=self.source_stores(),
            connectors=connectors or {}, history_stores=histories or {}, bindings={**self.bindings, **(bindings or {})},
            planner_policy=self.planner_policy, subscenario=subscenario,
            native={"home": self.execution_store.home, "node": node, "upstream": upstream,
                    "audit_path": output / "native-rpc-events.jsonl"} if cohort == "reference" else None)


class ReleaseSharingWorld(SharingWorld):
    """Keep the existing exact sharing interventions with real owner identities."""
    def __init__(self, home, case_id):
        super().__init__(home, case_id)
        self.registered_roles = True
        self.instantiated_nodes = {}

    registry_store = ReleaseWorld.registry_store

    def configure(self, app, store):
        selected = super().configure(app, store)
        self.instantiated_nodes["node-b"] = self.receiver_store
        if self.other_store.peer_id != self.instantiated_nodes["node-c"].peer_id:
            raise ValueError("release_sharing_other_identity_mismatch")
        return selected


def bind_owner_collection(args, world, runtime, planner_policy, record):
    """Register the original owner-controller task before its first tick."""
    from release_collection import ReleaseCollection, registry

    def on_task_created(task):
        collector = world.release_collector = ReleaseCollection(args.release_activation, args.output,
            world.case["id"], "ryn", args.trial, runtime=runtime, task_id=task["id"],
            node_stores=world.instantiated_nodes, source_stores={"node-a": world.sources},
            connectors={}, history_stores={}, bindings=world.bindings, planner_policy=planner_policy)
        path = collector.register()
        collector.attach_candidate()
        bind_receiver_dispatch(world, collector)
        record.update(registry.read(path)["binding"], formal_trial_registered=True)
    world.on_task_created = on_task_created


class ReleaseQueryWorld(QueryWorld):
    """Original Q02/Q03 tools observe a separate real consumer registry/store."""
    def __init__(self, home, case_id):
        super().__init__(home, case_id)
        effective = composer.load_effective_case(case_id)
        self.case, self.role = effective["effective_case"], effective["role_contract"]
        self.instantiated_nodes = {}

    registry_store = ReleaseWorld.registry_store
    configure_access = ReleaseWorld.configure_access

    def configure(self, app, store):
        import hashlib
        selected = super().configure(app, store)
        if self.case["id"] == "Q02":
            identity = self.bindings["source-broken"]
            source = self.sources.read()[identity]
            self.access_query.consumer.sources.save(source)
            self.sources.delete(identity)
            self.sources_before = hashlib.sha256(self.sources.path.read_bytes()).hexdigest()
            access = self.access_query
            access.before = {name: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                             for name, path in access.paths.items()}
            access.initial_models = access.model_facts()
        return selected

    def source_records(self):
        return {**self.sources.read(), **(self.access_query.consumer.sources.read() if self.access_query else {})}


def bind_receiver_dispatch(world, collector):
    """Retain the existing signed receiver request before its provider callback."""
    from release_collection import registry

    def dispatch(identity, wire, invoke):
        # The original signed wire is already formed by SharingWorld and its
        # production provider will independently authenticate/decrypt it.
        from rynmesh.llm_package.task_protocol import open_task
        envelope, request = open_task(wire, recipient_peer_id=world.store.peer_id,
            recipient_messaging_key=world.owner.messaging_private, expected_kind="llm_request")
        if request["task_id"] != identity or envelope["from_peer_id"] not in {world.receiver.peer_id, world.other.peer_id}:
            raise ValueError("release_receiver_original_request_mismatch")
        path = collector.output / ("release-receiver-" + registry.digest(identity) + ".json")
        registry._new(path, {"request_id": identity, "signed_wire": wire,
                            "sender_peer_id": envelope["from_peer_id"], "provider_peer_id": world.store.peer_id})
        return collector.dispatch("model", identity, invoke, basis="original_receiver_probe",
            original_request=registry.binding(path), sender_peer_id=envelope["from_peer_id"], provider_peer_id=world.store.peer_id)
    world.receiver_dispatch = dispatch
