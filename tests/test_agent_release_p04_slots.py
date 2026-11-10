"""P04 identity/dispatch plumbing only; no model or isolation business verdict."""
import copy
import importlib
from pathlib import Path

import pytest
from test_agent_release_registry import activate, prepared, registry, save  # noqa: F401
from test_agent_release_wiring import actual_fixture_policy
from test_agent_role_query_world import runtime_for

from rynmesh.agent_runtime.bridge import build_agent_integrations

ROOT = Path(__file__).resolve().parents[1]


def test_p04_four_original_slots_share_real_b_store_but_not_task_or_dispatch(prepared, monkeypatch):  # noqa: F811
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    worlds = importlib.import_module("release_world")
    core = importlib.import_module("release_collection")
    normalizer = importlib.import_module("normalize_release")
    p = prepared
    manifest = copy.deepcopy(p["manifest"])
    actual_fixture_policy(manifest)
    manifest["covered_cases"].append("P04")
    manifest["artifacts"]["projection:P04"] = registry.binding(save(p["root"] / "p04-projection.json", registry.p04_projection()))
    for name, implementation in (("normalizer", normalizer.__file__), ("grader", normalizer.ReleaseRoleGrader.__module__)):
        implementation = importlib.import_module(implementation).__file__ if name == "grader" else implementation
        descriptor = Path(manifest["artifacts"][name]["path"])
        value = registry.read(descriptor)
        value["implementation"] = registry.binding(implementation)
        save(descriptor, value)
        manifest["artifacts"][name] = registry.binding(descriptor)
    for cohort in ("ryn", "reference"):
        manifest["artifacts"][f"controller:{cohort}:P04"] = manifest["artifacts"][f"controller:{cohort}:Q01"]
        manifest["artifacts"][f"observer:{cohort}:P04"] = registry.binding(core.__file__)
    policy_path = Path(manifest["artifacts"]["model_policy"]["path"])
    limits = registry.read(policy_path)
    limits["case_limits"]["P04"] = copy.deepcopy(limits["case_limits"]["Q01"])
    save(policy_path, limits)
    manifest["artifacts"]["model_policy"] = registry.binding(policy_path)
    save(p["manifest_path"], manifest)
    review = registry.read(p["review_path"])
    review["manifest_sha256"] = registry.sha(p["manifest_path"])
    save(p["review_path"], review)
    activate(p)
    policy = {"version": "synthetic-test", "source_id": "synthetic-planner", "model_id": "glm-5.3-flash",
              "generation": {"context_window": 32768, "max_output_tokens": 4096, "request_defaults": {}}}
    policy["policy_sha256"] = registry.digest(policy)
    scenarios = registry.subscenarios("P04")
    assert set(scenarios) == {f"provider_agent_{mode}-session{session}" for mode in ("enabled", "disabled") for session in "AB"}
    with worlds.ReleaseWorld(p["root"] / "world", "P04", policy) as world:
        bridge, _tools, _catalog = build_agent_integrations(world.execution_app, world.execution_store, world.execution_store.home)
        runtime = runtime_for(world, world.execution_app, world.execution_store, bridge)
        collectors = []
        for scenario in scenarios:
            task = runtime.create({"message": scenarios[scenario]["prompt"], "model_id": world.selected})
            collector = world.collector(p["activation"], p["root"] / scenario, "ryn", 1, runtime, task, subscenario=scenario)
            collector.register()
            collector.attach_candidate()
            collectors.append(collector)
        assert len({c.task_id for c in collectors}) == 4
        assert len({registry.read(c.registration_path)["binding"]["run_nonce"] for c in collectors}) == 4
        assert len({c.initial["store_path"] for c in collectors}) == 1
        for collector in reversed(collectors):
            runtime.store.transaction(lambda data, identity=collector.task_id: data["tasks"][identity].update(status="running"))
            task = collector._task()
            called = []
            def invoke(_allowed, selected=collector, calls=called):
                calls.append(selected.model_request()["task_id"])
                return {"usage": {"input_tokens": 2, "output_tokens": 2}}
            runtime._meter_model(task, invoke, input_bound=10, output_bound=10)
            assert called == [collector.task_id]
            runtime.store.transaction(lambda data, identity=collector.task_id: data["tasks"][identity].update(status="completed"))
        assert all(len(c.events) == 1 and c.events[0]["task_id"] == c.task_id for c in collectors)
        assert len({c.events[0]["id"] for c in collectors}) == 4
        with pytest.raises(ValueError, match="unregistered_task_dispatch"):
            runtime._meter_model({"id": "foreign"}, lambda _: pytest.fail("unregistered"), input_bound=10, output_bound=10)
        raw_paths = {}
        for collector in collectors:
            observed = collector.finalize()
            assert observed["dispatch_audit"]["coverage"] == "all_model_and_tool_dispatches"
            bound = registry.read(collector.registration_path)["binding"]
            raw = {**bound, "release_collection": observed, "task": runtime.get(collector.task_id),
                   "prompt": world.case["prompt"], "session_prompt": scenarios[collector.subscenario]["prompt"],
                   "session_projection": manifest["artifacts"]["projection:P04"], "real_model": False,
                   "bindings": world.bindings, "started_at": collector.initial["at"], "ended_at": core.stamp(),
                   "model_calls": [], "planner_model": "synthetic", "provider": "offline", "isolated_run_root": str(world.home)}
            raw_paths[collector.subscenario] = registry.binding(save(collector.output / "run.json", raw))
        source = save(p["root"] / "bundle.json", {"case_id": "P04", "release_subscenarios": raw_paths})
        destination = p["root"] / "normalized"
        envelope = normalizer.normalize_bundle(source, destination)
        grader = normalizer.ReleaseRoleGrader()
        result = grader.grade(envelope, destination)
        assert not result["formal_pass"]
        assert not any("invalid_or_unreadable_release" in message for message in result["failures"])
        assert any("goal_outcome: assertion false" in message and "session" in message for message in result["failures"])
        entries, documents = grader._documents(envelope["run"], destination)
        aggregate_trace = next(documents[key] for key, item in entries.items() if item["kind"] == "operation_trace")
        assert aggregate_trace["isolation"] == {} and aggregate_trace["security"] == {}
        assert any("missing universal security audit" in item for item in result["pending"])
        incomplete = copy.deepcopy(envelope)
        incomplete["components"].pop(next(reversed(incomplete["components"])))
        assert "complete fixed subscenario bundle required" in grader.grade(incomplete, destination)["pending"]
        duplicate = copy.deepcopy(envelope)
        names = list(duplicate["components"])
        duplicate["components"][names[1]] = duplicate["components"][names[0]]
        assert "invalid_or_unreadable_release_bundle" in grader.grade(duplicate, destination)["failures"]
        fifth = runtime.create({"message": next(iter(scenarios.values()))["prompt"], "model_id": world.selected})
        other = world.collector(p["activation"], p["root"] / "wrong-trial", "ryn", 2, runtime, fifth,
                                subscenario=next(iter(scenarios)))
        with pytest.raises(ValueError, match="trial_identity_reused"):
            other.register()
