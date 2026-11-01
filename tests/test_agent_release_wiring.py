"""Offline v3 registration/dispatch wiring; no synthetic business passes."""
import copy
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest
from test_agent_integrations import node  # noqa: F401
from test_agent_release_registry import (  # noqa: F401
    activate,
    pending,
    prepared,
    register,
    registry,
    save,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures/agent_acceptance"
sys.path.insert(0, str(FIXTURES))
spec = importlib.util.spec_from_file_location("release_grade_test", FIXTURES / "role_grade_v3.py")
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)


def actual_fixture_policy(manifest):
    """Register this offline fixture's real profile, not the negative 12k policy."""
    path = Path(manifest["artifacts"]["model_policy"]["path"])
    policy = registry.read(path)
    policy["case_limits"]["Q01"].update(max_model_calls=128, max_seconds=3600, max_input_chars=100000,
        round_limits=[{"max_steps": 24, "max_seconds": 600, "max_context_chars": 100000, "max_output_chars": 20000}],
        lifetime_limits=[{"max_total_model_calls": 128, "max_total_tool_calls": 256, "max_total_tokens": 2000000, "max_total_seconds": 3600}])
    for phase in policy["phases"].values():
        phase.update(max_context_bytes=100000, max_output_tokens=4096)
    save(path, policy)
    manifest["artifacts"]["model_policy"] = registry.binding(path)


def test_nested_probe_admitted_before_real_callback_and_each_id_consumed(prepared):  # noqa: F811
    activate(prepared)
    path = register(prepared)
    gate = registry.DispatchGate(path, prepared["observer"].observe)
    pending(prepared, "tool", "operation-original")
    seen = []

    def tool():
        seen.append("tool")
        prepared["observer"].STATE["prior_dispatch_count"] += 1
        pending(prepared, "model", "usage-original-probe")
        return gate.dispatch("model", "usage-original-probe", lambda: seen.append("probe"))

    gate.dispatch("tool", "operation-original", tool)
    assert seen == ["tool", "probe"]
    assert len(list((path.parent / "dispatch").glob("*.json"))) == 2
    pending(prepared, "tool", "operation-original")
    with pytest.raises(ValueError, match="registry_request_id_reused"):
        gate.dispatch("tool", "operation-original", lambda: pytest.fail("duplicate"))


def test_inflight_callback_does_not_allow_duplicate_request(prepared):  # noqa: F811
    activate(prepared)
    path = register(prepared)
    gate = registry.DispatchGate(path, prepared["observer"].observe)
    pending(prepared)
    started, finish = threading.Event(), threading.Event()
    errors = []

    def invoke():
        started.set()
        assert finish.wait(5)

    def run():
        try:
            gate.dispatch("model", "native-rpc-1", invoke)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(5)
        with pytest.raises(ValueError, match="registry_request_id_reused"):
            gate.dispatch("model", "native-rpc-1", lambda: pytest.fail("duplicate inflight"))
    finally:
        finish.set()
        thread.join(5)
    assert not errors and not thread.is_alive()


def test_v3_requires_fresh_registration_and_rejects_draft_envelopes(tmp_path):
    grader = grade.ReleaseRoleGrader()
    draft = grade.DraftRoleGrader().template("Q01")
    assert grader.grade(draft, tmp_path)["formal_pass"] is False
    assert grader.grade(draft, tmp_path)["contract_integrity_failed"] is True
    assert grade.DraftRoleGrader().registration_sampling_kind() == "new_v2_draft_sampling"
    assert grader.registration_sampling_kind() == "new_v3_registered_sampling"


def test_v3_template_preserves_all_thirty_original_assertions(prepared):  # noqa: F811
    activate(prepared)
    path = register(prepared)
    grader = grade.ReleaseRoleGrader()
    envelope = grader.template_registered(path)
    registered = registry.read(path)
    assert envelope["run_id"] == registered["binding"]["run_id"]
    assert envelope["run_nonce"] == registered["binding"]["run_nonce"]
    assert envelope["run"]["dataset_version"] == registry.VERSION
    original = grade.DraftRoleGrader()
    assert len(grader.cases) == 30
    assert grader.cases == original.cases
    assert grader.suite["cases"] == original.suite["cases"]
    assert grader.grade(envelope, prepared["root"])["formal_pass"] is False


def test_reservation_without_live_registration_cannot_dispatch_or_grade(prepared):  # noqa: F811
    activate(prepared)
    p = registry.reserve(prepared["activation"], prepared["root"] / "trials", "Q01", "ryn", 1)
    assert not (p.parent / "registration.json").exists()
    with pytest.raises((ValueError, KeyError)):
        grade.ReleaseRoleGrader().template_registered(p)
    with pytest.raises(ValueError):
        registry.DispatchGate(p.parent / "registration.json", prepared["observer"].observe)
    registered = registry.register_reserved(p, prepared["observer"].observe)
    assert registry.read(registered)["binding"] == registry.read(p)["binding"]
    with pytest.raises(ValueError, match="already_registered"):
        registry.register_reserved(p, prepared["observer"].observe)


def test_actual_q01_controller_registers_original_ids_and_normalizes_without_fake_pass(prepared, node, monkeypatch):  # noqa: F811
    import importlib

    from test_agent_role_query_world import runtime_for
    controllers = FIXTURES.parents[2] / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(controllers))
    module = importlib.import_module("role_query_world")
    runner = importlib.import_module("run_queries")
    collector_module = importlib.import_module("release_collection")
    normalizer = importlib.import_module("normalize_release")
    app, store = node
    p = prepared
    manifest = copy.deepcopy(p["manifest"])
    actual_fixture_policy(manifest)
    manifest["artifacts"]["observer:ryn:Q01"] = registry.binding(collector_module.__file__)
    for name, path in (("grader", grade.__file__), ("normalizer", normalizer.__file__)):
        descriptor = Path(manifest["artifacts"][name]["path"])
        value = registry.read(descriptor)
        value["implementation"] = registry.binding(path)
        save(descriptor, value)
        manifest["artifacts"][name] = registry.binding(descriptor)
    save(p["manifest_path"], manifest)
    review = registry.read(p["review_path"])
    review["manifest_sha256"] = registry.sha(p["manifest_path"])
    save(p["review_path"], review)
    activate(p)
    output = p["root"] / "raw"
    policy = {"version": "synthetic-test", "source_id": "synthetic-planner", "model_id": "glm-5.3-flash",
              "generation": {"context_window": 32768, "max_output_tokens": 4096, "request_defaults": {}}}
    policy["policy_sha256"] = registry.digest(policy)
    with module.RoleQueryWorld(p["root"] / "world", "Q01") as world:
        world.configure(app, store)
        runtime = runtime_for(world, app, store, runner.FixturePlanner("Q01"))
        task = world.create_task(runtime)
        collector = collector_module.ReleaseCollection(p["activation"], output, "Q01", "ryn", 1,
            runtime=runtime, task_id=task["id"], node_stores={**world.instantiated_nodes, "node-b": world.access_query.receiver_store},
            source_stores={"node-a": world.sources}, connectors={}, history_stores={}, bindings=world.bindings, planner_policy=policy)
        path = collector.register()
        collector.attach_candidate()
        with pytest.raises(ValueError, match="durable_intent"):
            runtime.tools.execute("ryn_list_models", {}, operation_id="unpersisted-review-id", cancelled=lambda: False)
        original_sources = world.sources.read()
        changed_sources = copy.deepcopy(original_sources)
        changed_sources.pop(world.bindings["source-alpha"])
        world.sources._write(changed_sources)
        with pytest.raises(ValueError, match="bound_deployment_changed"):
            collector.dispatch("tool", "removed-source-review", lambda: pytest.fail("Deleted source admitted"), basis="review")
        world.sources._write(original_sources)
        actual_limits = copy.deepcopy(collector._task()["limits"])
        runtime.store.transaction(lambda data: data["tasks"][task["id"]]["limits"].update(max_context_chars=actual_limits["max_context_chars"] + 1))
        with pytest.raises(ValueError, match="actual_budget_policy"):
            collector.dispatch("tool", "budget-drift-review", lambda: pytest.fail("Budget drift admitted"), basis="review")
        runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(limits=actual_limits))
        with pytest.raises(ValueError, match="actual_request_cap"):
            collector._check_model_reservation({"stage": "model", "input_reservation": 100513, "output_reservation": 4096})
        assert not collector.events and not list((path.parent / "dispatch").glob("*.json"))
        runtime.run_once()
        original_events = copy.deepcopy(collector.events)
        assert original_events
        runtime = runtime_for(world, app, store, runner.FixturePlanner("Q01"))
        restore_objects = dict(runtime=runtime, task_id=task["id"],
            node_stores={**world.instantiated_nodes, "node-b": world.access_query.receiver_store},
            source_stores={"node-a": world.sources}, connectors={}, history_stores={}, bindings=world.bindings,
            planner_policy=policy)
        with pytest.raises(ValueError, match="node_identity_collision|restore_store_identity"):
            collector_module.ReleaseCollection.restore(path, output,
                **{**restore_objects, "node_stores": {**restore_objects["node_stores"],
                                                     "node-c": world.access_query.receiver_store}})
        collector = collector_module.ReleaseCollection.restore(path, output, **restore_objects)
        assert collector.events == original_events and collector.registration_path == path
        collector.attach_candidate()
        with pytest.raises(ValueError, match="request_id_reused"):
            collector.dispatch(original_events[0]["kind"], original_events[0]["id"],
                lambda: pytest.fail("resumed original request replayed"), basis="review")
        for _ in range(15):
            if runtime.get(task["id"])["status"] not in {"queued", "running"}:
                break
            runtime.run_once()
        observed = collector.finalize()
        assert observed["dispatch_audit"]["coverage"] == "all_model_and_tool_dispatches"
        row = runtime.store.read()["tasks"][task["id"]]
        model_ids = {r["id"] for r in row["budget"]["records"] if r["kind"] == "model"}
        assert model_ids and model_ids == {e["id"] for e in observed["model_requests"]}
        assert all(e["id"].startswith("usage_") for e in observed["model_requests"])
        assert any(e.get("tool_name") == "ryn_list_models" and e["id"].startswith("op_") for e in collector.events)
        registered = registry.read(path)
        raw = {**registered["binding"], "release_collection": observed, "prompt": world.case["prompt"],
               "task": runtime.get(task["id"]), "started_at": collector.initial["at"], "ended_at": collector_module.stamp(),
               "real_model": False, "bindings": world.bindings, "isolated_run_root": str(store.home),
               "provider": "offline-fixture", "planner_model": "fixture", "model_calls": []}
        save(output / "run.json", raw)
        envelope = normalizer.normalize(output / "run.json", p["root"] / "normalized")
        graded = registry.read(p["root"] / "normalized/grade.json")
        assert not graded["contract_integrity_failed"], graded
        assert graded["partition"] == "development"
        assert not graded["role_contract_failed"], graded
        assert graded["formal_trial_registered"] is True and graded["formal_pass"] is False
        aggregate = grade.ReleaseRoleGrader().aggregate([p["root"] / "normalized/run.json"] * 2)
        assert aggregate["results"]["duplicate_trials"] == [("ryn", "Q01", 1)]
        assert aggregate["status"] == "failed" and not aggregate["formal_pass"]
        assert "complete original registry dispatch coverage missing" not in graded["pending"]
        assert envelope["run_id"] == registered["binding"]["run_id"]
        original = (output / "run.json").read_bytes()
        changed = json.loads(original)
        changed["run_nonce"] = "0" * 32
        save(output / "tampered.json", changed)
        with pytest.raises(ValueError, match="registration_mismatch"):
            normalizer.normalize(output / "tampered.json", p["root"] / "tampered-normalized")
        from rynmesh.agent_runtime import budget
        runtime.store.transaction(lambda data: budget.reserve(data["tasks"][task["id"]], "model", "unobserved_review"))
        assert collector.finalize()["dispatch_audit"]["coverage"] == "partial_dispatch_collection"


def test_native_first_rpc_finalizes_reservation_from_actual_session_before_callback(prepared, node, monkeypatch):  # noqa: F811
    import importlib

    from test_agent_role_query_world import runtime_for

    root = FIXTURES.parents[2]
    executable = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    upstream = root / ".codex-tmp/research/zcode-runtime-reference"
    if not executable.is_file() or not upstream.is_dir():
        pytest.skip("Pinned native dependencies absent; registration remains pending")
    monkeypatch.syspath_prepend(str(root / "docs/acceptance/ryn-agent-delivery/real-model"))
    monkeypatch.syspath_prepend(str(root / "docs/acceptance/ryn-agent-delivery/reference"))
    collector_module = importlib.import_module("release_collection")
    world_module = importlib.import_module("role_query_world")
    runner = importlib.import_module("run_queries")
    execute = importlib.import_module("documents").execute
    p = prepared
    manifest = copy.deepcopy(p["manifest"])
    actual_fixture_policy(manifest)
    manifest["artifacts"]["observer:reference:Q01"] = registry.binding(collector_module.__file__)
    manifest["artifacts"]["reference_node"] = registry.binding(executable)
    save(p["manifest_path"], manifest)
    review = registry.read(p["review_path"])
    review["manifest_sha256"] = registry.sha(p["manifest_path"])
    save(p["review_path"], review)
    activate(p)
    policy = {"version": "synthetic-test", "source_id": "synthetic-planner", "model_id": "glm-5.3-flash",
              "generation": {"context_window": 32768, "max_output_tokens": 4096, "request_defaults": {}}}
    policy["policy_sha256"] = registry.digest(policy)
    app, store = node
    with world_module.RoleQueryWorld(p["root"] / "world", "Q01") as world:
        selected = world.configure(app, store)
        runtime = runtime_for(world, app, store, runner.FixturePlanner("Q01"))
        task = world.create_task(runtime)
        output = p["root"] / "raw-native"
        collector = collector_module.ReleaseCollection(p["activation"], output, "Q01", "reference", 1,
            runtime=runtime, task_id=task["id"], node_stores={**world.instantiated_nodes, "node-b": world.access_query.receiver_store},
            source_stores={"node-a": world.sources}, connectors={}, history_stores={}, bindings=world.bindings, planner_policy=policy,
            native={"home": store.home, "node": executable, "upstream": upstream, "audit_path": output / "native-rpc-events.jsonl"})
        provenance = collector.native_config()
        assert provenance["registration_sha256"] is None and collector.registration_path is None
        assert not (store.home / "reference-session.sqlite").exists()
        before = runtime.model.complete
        callbacks = []

        def complete(*args, **kwargs):
            registered = registry.read(collector.registration_path)
            assert registered["observation"]["reference"]["native_session_id"] == collector.native["session_id"]
            assert collector.events[-1]["id"] == collector.model_request()["id"]
            assert list((collector.registration_path.parent / "dispatch").glob("*.json"))
            callbacks.append(collector.events[-1]["id"])
            if not args[2] and args[1][-1]["content"].startswith("Verify whether"):
                return {"message": {"role": "assistant", "content": json.dumps({"pass": True, "reason": "Offline RPC fixture", "nextAction": ""})},
                        "usage": {"input_tokens": 5, "output_tokens": 5}}
            return before(*args, **kwargs)

        runtime.model.complete = complete
        record = {"prompt": world.case["prompt"], "approvals": []}
        execute(runtime=runtime, tools=runtime.tools, catalog=runtime.catalog, selected=selected, task=task,
                bridge=runtime.model, record=record, home=store.home, node=executable, upstream=upstream,
                real_model=False, dispatch_observer=collector, dispatch_provenance=provenance,
                allow_write=lambda _: pytest.fail("Q01 is read-only"))
        assert callbacks and all(":rpc:" in identity for identity in callbacks)
        originals = {path: registry.sha(path) for path in output.glob("release-request-*.json")}
        expected_events = copy.deepcopy(collector.events)
        restored = collector_module.ReleaseCollection.restore(collector.registration_path, output,
            runtime=runtime, task_id=task["id"], node_stores=collector.nodes, source_stores=collector.sources,
            connectors={}, history_stores={}, bindings=world.bindings, planner_policy=policy, native=collector.native)
        assert restored.events == expected_events
        assert {path: registry.sha(path) for path in originals} == originals
        sidecar = next(output.glob("release-budget-*.json"))
        original_link = registry.read(sidecar)
        changed_link = copy.deepcopy(original_link)
        changed_link["reservation"]["input_reservation"] += 1
        save(sidecar, changed_link)
        with pytest.raises(ValueError, match="restore_budget_link_mismatch"):
            restored._restore_dispatch_links()
        save(sidecar, original_link)
        collector = restored
        assert collector.finalize()["dispatch_audit"]["coverage"] == "all_model_and_tool_dispatches"
        first = collector.events[0]["native_rpc"]
        with pytest.raises(ValueError, match="rpc_not_persisted"):
            collector.invoke("model", {}, first, lambda: pytest.fail("old native ID replayed"))
