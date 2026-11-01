"""Actual isolated role setup; loopback inference is not a model quality test."""
import importlib
from pathlib import Path

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.nas import NasPlugin
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case_id", ["F02", "F07", "Q05"])
def test_document_roles_use_actual_provider_and_consumer_stores(tmp_path, monkeypatch, case_id):
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    world_module = importlib.import_module("release_world")
    documents_module = importlib.import_module("document_cases")
    policy = {"version": "local-wiring-test", "source_id": "synthetic", "model_id": "glm-5.3-flash",
              "generation": {"context_window": 128000, "max_output_tokens": 8192, "request_defaults": {}}}
    policy["policy_sha256"] = world_module.composer.digest(policy)
    with world_module.ReleaseWorld(tmp_path / "roles", case_id, policy) as world:
        app, store = world.execution_app, world.execution_store
        assert store is world.instantiated_nodes["node-b"]
        assert world.sources.path.parent.parent == world.instantiated_nodes["node-a"].home
        assert len({s.peer_id for s in world.instantiated_nodes.values()}) == 4
        assert world.consumer.sources.read() == {}
        nas = app.state.nas_plugin = NasPlugin(store.home)
        nas.enable(True)
        with documents_module.DocumentCase(store.home, case_id) as documents:
            scope = documents.configure(nas)
            bridge, tools, catalog = build_agent_integrations(app, store, store.home)
            runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                bridge, tools, catalog, owner_id=documents.case["authorization"]["owner_id"], consumer_node_id=store.peer_id)
            app.state.agent_runtime = runtime
            runtime.update_settings({"enabled": True, "model_id": world.selected, "max_steps": 24,
                                     "max_seconds": 600, **documents.settings()})
            task = runtime.create({"message": documents.case["prompt"], "scope": scope,
                "expected_destination_revision": catalog.resolve(world.selected)["destination_revision"]})
            context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
            assert context["consumer_node_id"] == store.peer_id and context["owner_id"] == "owner-test"
            assert task["model_destination"]["provider_peer_id"] == world.owner_store.peer_id != store.peer_id
            assert {r["connection_id"] for r in context["resource_grants"]} == {r["connection_id"] for r in scope["resources"]}
            before = len(world.consumer.wire_audit)
            result = bridge.complete(world.selected, [{"role": "user", "content": "Synthetic role wiring only"}], [],
                lambda: False, expected_destination=context["model_destination"], max_output_tokens=32)
            assert result["message"]["role"] == "assistant"
            assert len(world.consumer.wire_audit) == before + 1
            assert world.consumer.wire_audit[-1]["response_received"]
            assert world.consumer.wire_audit[-1]["consumer_peer_id"] == store.peer_id
            assert world.consumer.wire_audit[-1]["provider_peer_id"] == world.owner_store.peer_id
            for identity in list(nas.sessions):
                nas._close_session(identity)


def test_registered_sharing_uses_real_owner_and_existing_exact_approvals(node, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    module = importlib.import_module("release_world")
    sharing = importlib.import_module("sharing_cases")
    app, store = node
    with module.ReleaseSharingWorld(tmp_path / "world", "C03") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
            sharing.SharingFixturePlanner(), tools, catalog, owner_id="owner-test", consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": world.bindings["model-c"], "max_steps": 24,
                                 "max_seconds": 600, "max_context_chars": 100000})
        seen = []
        world.on_task_created = lambda task: seen.append((task["id"], runtime.store.read()["tasks"][task["id"]]["steps"]))
        result = world.execute(runtime)
        assert seen == [(result["task"]["id"], 0)]
        assert world.instantiated_nodes["node-a"] is store
        assert world.instantiated_nodes["node-b"].peer_id == world.receiver.peer_id
        assert world.instantiated_nodes["node-c"].peer_id == world.other.peer_id
        assert selected == world.bindings["model-a"]
        assert result["task"]["model_id"] == selected
        assert world.grants.grant(world.sid, world.rid)["allowed"] is True
        assert runtime.settings()["model_id"] == selected
        assert all(item["approved"] for item in result["approvals"])


@pytest.mark.parametrize("case_id", ["Q02", "Q03"])
def test_registered_queries_preserve_original_facts_on_actual_consumer(node, tmp_path, monkeypatch, case_id):  # noqa: F811
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    module = importlib.import_module("release_world")
    app, store = node
    with module.ReleaseQueryWorld(tmp_path / "world", case_id) as world:
        selected = world.configure(app, store)
        consumer = world.access_query.consumer
        assert selected == consumer.selected and consumer.store.peer_id != store.peer_id
        assert len({n.peer_id for n in world.instantiated_nodes.values()}) == 4
        broken = world.bindings["source-broken"]
        if case_id == "Q02":
            assert broken not in world.sources.read()
            actual = consumer.sources.read()[broken]
            assert actual["status"] == "failed" and actual["name"] == "source-broken"
            assert actual["model_settings"]["offline"]["evidence"]["connection"]["error_code"] == "connection_refused"
            models = consumer.models.list_models()
            assert any(row["id"] == world.bindings["model-d"] for row in models)
        else:
            assert not consumer.sources.read() and broken in world.sources.read()
        facts = world.collect()
        assert facts["source_configuration_unchanged"] is True
        assert facts["source_readback"]["model-d"]["chat"] is False
        assert {r["payload"]["peer_id"] for r in facts["signed_registry_evidence"]} == {
            world.instantiated_nodes[n].peer_id for n in ("node-a", "node-c", "node-d")}



@pytest.mark.parametrize("scenario", ["pre_save_failure", "post_save_conflict"])
def test_registered_c02_reuses_actual_target_identity_and_initial_grant(tmp_path, monkeypatch, scenario):
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    module = importlib.import_module("release_world")
    source_module = importlib.import_module("source_update_case")
    with module.ReleaseWorld(tmp_path / "roles", "C02", None) as roles:
        initial = roles.sources.read()[roles.bindings["source-alpha"]]
        grant = roles.access_query.grants.grant(roles.bindings["model-a"].removeprefix("local/"), roles.access_query.rid)
        with source_module.SourceUpdateWorld(roles.sources, scenario, initial_source=initial) as target:
            assert target.source_id == roles.bindings["source-alpha"]
            actual = roles.sources.read()[target.source_id]
            assert actual["models"] == ["alpha", "beta"] and actual["shared_models"] == ["alpha"]
            assert target.logical_versions[actual["source_revision"]] == 7
            assert roles.access_query.grants.grant(roles.bindings["model-a"].removeprefix("local/"), roles.access_query.rid) == grant
            assert grant["allowed"] is True and grant["peer_id"] == roles.instantiated_nodes["node-b"].peer_id
            assert len([r for r in roles.sources.read().values() if r["name"] == "source-alpha"]) == 1
            assert target.saves == []
