"""Real persisted peer signatures feed the collector; loopback is not two hosts."""
import copy
import importlib
from pathlib import Path

import pytest
from test_agent_release_registry import activate, prepared, registry, save  # noqa: F401
from test_agent_release_wiring import actual_fixture_policy
from test_agent_role_query_world import runtime_for

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]


def test_signed_provider_readback_binds_fresh_dispatch_and_rejects_replay_or_source_loss(prepared, monkeypatch):  # noqa: F811
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/two-host"))
    core = importlib.import_module("release_collection")
    remote = importlib.import_module("release_observation")
    worlds = importlib.import_module("release_world")
    p = prepared
    manifest = copy.deepcopy(p["manifest"])
    actual_fixture_policy(manifest)
    manifest["covered_cases"].append("Q03")
    for cohort in ("ryn", "reference"):
        manifest["artifacts"][f"controller:{cohort}:Q03"] = manifest["artifacts"][f"controller:{cohort}:Q01"]
        manifest["artifacts"][f"observer:{cohort}:Q03"] = registry.binding(core.__file__)
    policy_path = Path(manifest["artifacts"]["model_policy"]["path"])
    limits = registry.read(policy_path)
    limits["case_limits"]["Q03"] = copy.deepcopy(limits["case_limits"]["Q01"])
    save(policy_path, limits)
    manifest["artifacts"]["model_policy"] = registry.binding(policy_path)
    snapshot_path = Path(manifest["artifacts"]["candidate_snapshot"]["path"])
    runtime_source = ROOT / "rynmesh/agent_runtime/runtime.py"
    save(snapshot_path, {"files": [registry.binding(remote.__file__), registry.binding(runtime_source)]})
    manifest["artifacts"]["candidate_snapshot"] = registry.binding(snapshot_path)
    save(p["manifest_path"], manifest)
    review = registry.read(p["review_path"])
    review["manifest_sha256"] = registry.sha(p["manifest_path"])
    save(p["review_path"], review)
    activate(p)
    policy = {"version": "synthetic-test", "source_id": "synthetic-planner", "model_id": "glm-5.3-flash",
              "generation": {"context_window": 32768, "max_output_tokens": 4096, "request_defaults": {}}}
    policy["policy_sha256"] = registry.digest(policy)
    with worlds.ReleaseWorld(p["root"] / "world", "Q03", policy) as world:
        app, owner = world.owner_app, world.owner_store
        bridge, tools, catalog = build_agent_integrations(app, owner, owner.home)
        key = peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519")
        app.state.agent_runtime = AgentRuntime(AgentStore(owner.home / "agent", key), bridge, tools, catalog,
                                               owner_id="owner-test", consumer_node_id=owner.peer_id)
        bridge, _tools, _catalog = build_agent_integrations(world.execution_app, world.execution_store, world.execution_store.home)
        runtime = runtime_for(world, world.execution_app, world.execution_store, bridge)
        task = runtime.create({"message": world.case["prompt"], "model_id": world.selected})
        captures = []
        def observe(challenge):
            signed = remote.capture_observation(app, owner, challenge,
                lambda: {"python_files": {"agent_runtime/runtime.py": registry.sha(runtime_source)}})
            captures.append(signed)
            return signed
        observer = {"peer_id": owner.peer_id, "observe": observe, "verify": remote.verify_observation}
        collector = core.ReleaseCollection(p["activation"], p["root"] / "raw", "Q03", "ryn", 1,
            runtime=runtime, task_id=task["id"], node_stores={k: v for k, v in world.instantiated_nodes.items() if k != "node-a"},
            source_stores={"node-b": world.consumer.sources}, connectors={}, history_stores={}, bindings=world.bindings,
            planner_policy=policy, remote_observers={"node-a": observer})
        collector.register()
        collector.attach_candidate()
        runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(status="running"))
        called = []
        def invoke(_allowed):
            called.append(collector.model_request()["id"])
            return {"usage": {"input_tokens": 1, "output_tokens": 1}}
        runtime._meter_model(collector._task(), invoke, input_bound=10, output_bound=10)
        assert len(called) == 1 and captures[-1]["payload"]["challenge"]["request"] == {"kind": "model", "id": called[0]}
        assert len({row["payload"]["challenge"]["challenge"] for row in captures}) == len(captures)
        assert collector.initial["deployment"]["source_owners"]["source-alpha"]["source_id"] == world.bindings["source-alpha"]
        previous = captures[-1]
        observer["observe"] = lambda _challenge: previous
        with pytest.raises(ValueError, match="binding_mismatch"):
            collector.dispatch("tool", "replayed-observation", lambda: pytest.fail("replayed signature admitted"), basis="negative-test")
        observer["observe"] = observe
        sources = world.sources.read()
        changed = copy.deepcopy(sources)
        changed.pop(world.bindings["source-alpha"])
        world.sources._write(changed)
        with pytest.raises(ValueError, match="bound_deployment_changed"):
            collector.dispatch("tool", "missing-provider-source", lambda: pytest.fail("missing source admitted"), basis="negative-test")
        world.sources._write(sources)
        assert len(collector.events) == 1
        assert len(list(collector.output.glob("release-remote-node-a-*.json"))) >= 3
