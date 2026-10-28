"""Q06's real per-model friend permissions and shared read-only boundary.

Setup probes use signed production requests and the synthetic loopback source,
before the task starts. They are collector evidence, never added to model input.
This is one physical host, not a two-node acceptance result.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.ai_access.catalog import PATH, FriendAICatalog
from rynmesh.ai_access.routes import AIAccessState
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.crypto import canonical_json
from rynmesh.friends.service import FriendService
from rynmesh.llm_package.model_settings import effective
from rynmesh.llm_package.routes import _expires
from rynmesh.llm_package.task_protocol import TaskProtocolError, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class AccessQuery:
    def __init__(self, world, app, store, *, owner_execution=False):
        self.world, self.store = world, store
        self.owner_execution = owner_execution
        self.sources = world.sources
        self.services = app.state.llm_agent_services
        self.nodes, self.catalog_audit, self.setup_admission = {}, [], []
        self.rejected_tools, self.read_tools = [], []
        self.source_replacements, self.grant_changes = [], []

        def post(endpoint, path, body, headers, **_):
            target = self.nodes[endpoint]
            if path == "/api/peer/friends/accept":
                return target.accept(body)
            relationship = target.verify_request(path=path, body=canonical_json(body), headers=headers)
            if path != PATH or target is not self.owner:
                raise RuntimeError("query_unknown_peer_route")
            self.catalog_audit.append({"peer_id": relationship["peer_id"], "authenticated": True})
            return self.publisher.respond(body, relationship)

        def friend(node_store, name, port):
            key = peer_box.load_or_create_messaging_key(node_store.home / "messaging.x25519")
            endpoint = "http://127.0.0.1:" + str(port)
            service = FriendService(home=node_store.home, peer_id=node_store.peer_id, node_name=name,
                endpoint=endpoint, identity_private=node_store.private_key_bytes, messaging_private=key,
                post_json=post, allow_loopback=True)
            self.nodes[endpoint] = service
            return service

        self.owner = friend(store, "node-a" if owner_execution else "owner-provider", 18921)
        self.receiver_store = RynmeshStore(home=world.home / "receiver", network_dir=world.home / "fixed-network")
        self.receiver = friend(self.receiver_store, "node-b", 18922)
        # Reuse the signed registry node-c identity, rather than introducing a
        # second node with the same user-visible name and different authority.
        self.other = friend(RynmeshStore(home=world.home / "node-c", network_dir=world.home / "fixed-network"), "node-c", 18923)
        assert self.other.peer_id == world.bindings["node-c"]
        for receiver in (self.receiver, self.other):
            receiver.join(self.owner.create_invite()["invite_uri"])
        world.bindings["node-b"] = self.receiver.peer_id
        app.state.friends = SimpleNamespace(service=self.owner)
        self.grants = AIAccessStore(store.home, relationship=self.owner.store.relationship, sources=self.sources)
        app.state.ai_access = AIAccessState(self.grants, lambda _: None)
        self.publisher = FriendAICatalog(friends=lambda: self.owner, grants=lambda: self.grants,
            provider=lambda: [p.public_status() for p in self.services["active_source_managers"]().values()])
        self.catalogs = {name: FriendAICatalog(friends=lambda peer=peer: peer, grants=lambda: None, provider=lambda: None)
                         for name, peer in (("node-b", self.receiver), ("node-c", self.other))}
        source_id = world.bindings["source-alpha"]
        source = self.sources.read()[source_id]
        self.sources.configure_model(source_id, {"model_id": "alpha",
            "expected_rule_revision": source["model_settings"]["alpha"]["rule_revision"],
            "configuration": {"share_scope": "friends"}})
        self.rid = self.owner.store.relationship_for_peer(self.receiver.peer_id)["relationship_id"]
        self.grant_a = self.grants.set(world.bindings["model-a"].removeprefix("local/"), self.rid,
                                       allowed=True, expected_revision=0)
        self.visible = {name: [row["service"]["package_id"] for row in catalog.refresh(store.peer_id)["services"]]
                        for name, catalog in self.catalogs.items()}
        self.setup_admission = [self.probe("model-a", self.receiver),
                                self.probe("model-b", self.other),
                                self.probe("model-a", self.other, copied_grant=self.grant_a),
                                self.probe("model-b", self.receiver, copied_grant=self.grant_a)]
        if ([p["result"] for p in self.setup_admission] != ["allowed", "denied", "denied", "denied"]
                or [p["upstream_requests"] for p in self.setup_admission] != [1, 0, 0, 0]):
            raise RuntimeError("query_initial_admission_mismatch")
        self.setup_http_count = len(world.upstream.audit)
        from query_consumer import QueryConsumer
        self.consumer = QueryConsumer(self)
        self.paths = {"sources": self.sources.path, "grants": self.grants.path,
                      "consumer_sources": self.consumer.sources.path,
                      "consumer_grants": self.consumer.app.state.ai_access.store.path,
                      "owner_relationships": self.owner.store.state_path,
                      "node_b_relationships": self.receiver.store.state_path,
                      "node_c_relationships": self.other.store.state_path}
        self.before = {name: digest(path) for name, path in self.paths.items()}
        self.relationships_before = self.relationship_business_hashes()
        self.nonce_counts_before = self.nonce_counts()
        self.initial_grants = copy.deepcopy(self.grants.list())
        self.initial_models = self.model_facts()
        for logical, expected in world.world["models"].items():
            actual = self.initial_models[logical]
            if (actual["sharing"] != expected["sharing"] or actual["receiver_peer_ids"] !=
                    sorted(world.bindings[name] for name in expected["receivers"])):
                raise RuntimeError("query_initial_permissions_mismatch")

        original_write = self.sources._write
        def observed_write(records):
            before = digest(self.sources.path)
            try:
                return original_write(records)
            finally:
                self.source_replacements.append({"before_sha256": before, "after_sha256": digest(self.sources.path)})
        self.sources._write = observed_write
        original_set = self.grants.set
        def observed_grant(*args, **kwargs):
            before = copy.deepcopy(self.grants.list())
            try:
                return original_set(*args, **kwargs)
            finally:
                self.grant_changes.append({"changed": before != self.grants.list()})
        self.grants.set = observed_grant

    def probe(self, logical, sender, *, copied_grant=None):
        """Independent setup/test-only admission, never a model-facing tool."""
        sid = self.world.bindings[logical].removeprefix("local/")
        relationship = self.owner.store.relationship_for_peer(sender.peer_id)
        grant = copied_grant or self.grants.grant(sid, relationship["relationship_id"])
        permission = {"relationship_id": (grant or relationship)["relationship_id"],
                      "revision": (grant or {}).get("revision", 0)}
        task_id = "query-setup-" + uuid.uuid4().hex
        body = {"task_id": task_id, "service_id": sid, "max_tokens": 10, "max_amount": .01,
            "chat": {"messages": [{"role": "user", "content": "Synthetic authorization probe"}], "max_tokens": 10},
            "ai_permission": permission, "reply_messaging_pub": peer_box.public_key_b64(sender.messaging_private)}
        wire = seal_task(body=body, task_id=task_id, kind="llm_request", sender_peer_id=sender.peer_id,
            recipient_peer_id=self.store.peer_id, sender_signing_key=sender.identity_private,
            recipient_messaging_pub=peer_box.public_key_b64(self.owner.messaging_private), expires_at=_expires(300)).to_dict()
        before = len(self.world.upstream.audit)
        result = {"model": logical, "receiver_peer_id": sender.peer_id, "signed_request": True,
                  "copied_other_grant": copied_grant is not None, "result": "unknown"}
        try:
            provider = self.services["provider_for"](sid)
            reply = provider.handle(wire)
            _, response = open_task(reply, recipient_peer_id=sender.peer_id,
                recipient_messaging_key=sender.messaging_private, expected_kind="llm_response")
            result.update(state=response.get("state"), error_code=response.get("error_code"), response_authenticated=True)
            if response.get("state") == "succeeded":
                result["result"] = "allowed"
            elif response.get("state") == "rejected" and response.get("error_code") in {"ai_permission_denied", "service_paused"}:
                result["result"] = "denied"
        except TaskProtocolError as exc:
            if str(exc) == "This device is not allowed to use this personal-space AI service.":
                result.update(result="denied", error_code="personal_space_access_denied")
            else:
                result["error_type"] = type(exc).__name__
        result["upstream_requests"] = len(self.world.upstream.audit) - before
        return result

    def attach(self, tools):
        if (tools.store is not self.store if self.owner_execution else tools is not self.consumer.tools):
            raise RuntimeError("query_consumer_tools_required")
        if getattr(tools, "_access_query_read_only", False):
            return
        execute = tools.execute
        def read_only(name, *args, **kwargs):
            metadata = tools.metadata(name)
            if metadata["effect"] != "read":
                self.rejected_tools.append(name)
                raise AgentToolRejected("agent_permission_denied")
            self.read_tools.append(name)
            return execute(name, *args, **kwargs)
        tools.execute = read_only
        tools._access_query_read_only = True

    def relationship_business_hashes(self):
        # FriendStore.state() intentionally omits secrets; hash the complete
        # stored object so a key change cannot hide behind nonce exclusions.
        return {name: hashlib.sha256(canonical_json({key: value for key, value in
                    json.loads(peer.store.state_path.read_text(encoding="utf-8")).items()
                    if key != "nonces"})).hexdigest()
                for name, peer in (("owner_relationships", self.owner), ("node_b_relationships", self.receiver),
                                   ("node_c_relationships", self.other))}

    def nonce_counts(self):
        return {name: sum(len(values) for values in peer.store.state().get("nonces", {}).values())
                for name, peer in (("owner_relationships", self.owner), ("node_b_relationships", self.receiver),
                                   ("node_c_relationships", self.other))}

    def model_facts(self):
        records, grants = self.world.source_records(), self.grants.list()
        return {logical: {"id": self.world.bindings[logical], "sharing": facts["upstream_id"] in
                    self.sources.shared_models(records[self.world.bindings[facts["source_id"]]]),
                "share_scope": effective(records[self.world.bindings[facts["source_id"]]], facts["upstream_id"])["share_scope"],
                "receiver_peer_ids": sorted(g["peer_id"] for g in grants if g["effective"] and
                    g["service_id"] == self.world.bindings[logical].removeprefix("local/"))}
                for logical, facts in self.world.world["models"].items()}

    def collect(self):
        after = {name: digest(path) for name, path in self.paths.items()}
        relationships_after = self.relationship_business_hashes()
        unchanged = (all(after[name] == self.before[name] for name in self.paths if name not in relationships_after)
            and relationships_after == self.relationships_before
            and self.grants.list() == self.initial_grants and self.model_facts() == self.initial_models)
        writes = len(self.source_replacements) + sum(g["changed"] for g in self.grant_changes)
        return {"access_query": {"initial_models": copy.deepcopy(self.initial_models), "current_models": self.model_facts(),
                    "initial_grants": copy.deepcopy(self.initial_grants), "current_grants": self.grants.list(),
                    "hashes_before": self.before, "hashes_after": after, "configuration_and_grants_unchanged": unchanged,
                    "relationship_business_hashes_before": self.relationships_before,
                    "relationship_business_hashes_after": relationships_after,
                    "relationship_hash_exclusions": ["nonces"],
                    "nonce_counts_before": self.nonce_counts_before, "nonce_counts_after": self.nonce_counts(),
                    "nonce_write_scope": "Authenticated catalog anti-replay protocol state only; all other friend state remains compared.",
                    "setup_receiver_visible_services": self.visible, "setup_admission_probes": self.setup_admission,
                    "setup_fixture_http_audit": copy.deepcopy(self.world.upstream.audit[:self.setup_http_count]),
                    "task_fixture_http_audit": copy.deepcopy(self.world.upstream.audit[self.setup_http_count:]),
                    "http_audit_scope": "Synthetic source only; planner traffic is recorded separately in model_calls.",
                    "catalog_authentication": copy.deepcopy(self.catalog_audit), "rejected_tools": self.rejected_tools,
                    "executed_read_tools": self.read_tools, "source_replacements": self.source_replacements,
                    "grant_changes": self.grant_changes, "read_only_boundary_passed": unchanged and writes == 0},
                **({"receiver_evidence": self.consumer.evidence()} if self.owner_execution else self.consumer.evidence()),
                "source_readback_scope": "Provider business truth for collector review only; model facts must come from actual execution-node tool observations.",
                "metrics": {"writes": writes}, "physical_hosts": 1,
                "write_counting_scope": "Task interval only, after fixture initialization and signed setup probes; source replacements plus changed grant mutations.",
                "two_physical_nodes": "pending", "normalizer_status": "pending integration",
                "actual_source_http_audit": copy.deepcopy(self.world.upstream.audit)}
