"""Real local process replacement and persisted registration; no paid model."""
import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from test_agent_release_registry import activate, prepared, registry, save  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS = ROOT / "docs/acceptance/ryn-agent-delivery/real-model"


@pytest.fixture
def worker_dependencies(cohort):
    executable = os.environ.get("RYN_REFERENCE_NODE") or shutil.which("node")
    upstream = Path(os.environ.get("RYN_REFERENCE_UPSTREAM", ROOT / ".codex-tmp/research/zcode-runtime-reference"))
    if cohort == "reference" and (not executable or not upstream.is_dir()):
        pytest.skip("Pinned native dependencies absent; set RYN_REFERENCE_NODE and RYN_REFERENCE_UPSTREAM")
    return Path(executable), upstream


def policy():
    value = {"version": "synthetic-process-test", "source_id": "synthetic-planner", "model_id": "glm-5.3-flash",
             "generation": {"context_window": 32768, "max_output_tokens": 4096, "request_defaults": {}}}
    value["policy_sha256"] = registry.digest(value)
    return value


def activate_case(p, case_id, collector):
    manifest = copy.deepcopy(p["manifest"])
    manifest["covered_cases"] = [case_id]
    for cohort in ("ryn", "reference"):
        manifest["artifacts"][f"controller:{cohort}:{case_id}"] = manifest["artifacts"].pop(f"controller:{cohort}:Q01")
        manifest["artifacts"].pop(f"observer:{cohort}:Q01")
        manifest["artifacts"][f"observer:{cohort}:{case_id}"] = registry.binding(collector.__file__)
    path = Path(manifest["artifacts"]["model_policy"]["path"])
    value = registry.read(path)
    limits = value["case_limits"].pop("Q01")
    limits.update(max_model_calls=128, max_seconds=3600, max_input_chars=100000,
        round_limits=[{"max_steps": 24, "max_seconds": 600, "max_context_chars": 100000, "max_output_chars": 20000}],
        lifetime_limits=[{"max_total_model_calls": 128, "max_total_tool_calls": 256, "max_total_tokens": 2000000, "max_total_seconds": 3600}])
    if case_id == "R06":
        limits.update(max_model_calls=12, max_input_chars=12000,
            round_limits=[{**limits["round_limits"][0], "max_context_chars": 12000} for _ in range(3)],
            lifetime_limits=[{**limits["lifetime_limits"][0], "max_total_model_calls": cap} for cap in (4, 4, 12)])
    value["case_limits"][case_id] = limits
    for phase in value["phases"].values():
        phase.update(max_context_bytes=12000 if case_id == "R06" else 100000, max_output_tokens=4096)
    save(path, value)
    manifest["artifacts"]["model_policy"] = registry.binding(path)
    save(p["manifest_path"], manifest)
    review = registry.read(p["review_path"])
    review["manifest_sha256"] = registry.sha(p["manifest_path"])
    save(p["review_path"], review)
    activate(p)


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_registered_r02_actual_worker_restart_preserves_single_registration(prepared, monkeypatch, cohort, worker_dependencies):  # noqa: F811
    monkeypatch.syspath_prepend(str(CONTROLLERS))
    runner = importlib.import_module("run_sharing_recovery")
    collector = importlib.import_module("release_collection")
    executable, upstream = worker_dependencies
    if cohort == "reference":
        prepared["manifest"]["artifacts"]["reference_node"] = registry.binding(executable)
    activate_case(prepared, "R02", collector)
    output = prepared["root"] / "raw"
    result = runner.execute(output, home=prepared["root"] / "world",
        release_activation=prepared["activation"], fixture_planner_policy=policy(),
        cohort=cohort, node=executable, upstream=upstream)
    assert "error_code" not in result, result
    before = registry.read(output / "release-worker-start.json") if cohort == "ryn" else {
        "pid": result["before_restart"]["pid"], "task_id": result["before_restart"]["task"]["id"],
        "registration": registry.read(output / "release-worker-handoff.json")["registration"], "prior_dispatches": 0}
    after = registry.read(output / "release-worker-recover.json")
    assert before["pid"] != after["pid"] and before["task_id"] == after["task_id"]
    assert before["registration"] == after["registration"]
    assert before["prior_dispatches"] == 0 < after["prior_dispatches"]
    handoff = registry.read(output / "release-worker-handoff.json")
    assert handoff["registration"] == before["registration"]
    assert len(list((prepared["root"] / "trials").glob("*/registration.json"))) == 1
    events = [registry.read(p) for p in sorted(output.glob("release-request-*.json"))]
    assert len({(e["kind"], e["id"]) for e in events}) == len(events)
    assert all(e["task_id"] == before["task_id"] for e in events)
    assert result["release_collection"]["dispatch_audit"]["coverage"] == "all_model_and_tool_dispatches"
    assert result["formal_case_score"] is False
    if cohort == "reference":
        assert result["reference_recovery"]["case_status"] == "failed"
        assert result["reference_recovery"]["reason"] == "native_pending_execution_not_restored"
        assert len(list(output.glob("release-native-segment-*.json"))) == 2
        before_native = registry.read(output / "start-worker.json")["reference"]
        after_native = registry.read(output / "recover-worker.json")["reference"]
        assert before_native["worker_pid"] != after_native["worker_pid"]
        assert after_native["dispatch_counts"] == {"model": 0, "tool": 0, "approval": 0}
        assert after_native["result"]["persisted_history_unchanged"]


def test_document_roles_reopen_in_new_process_without_reseeding(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(CONTROLLERS))
    resume = importlib.import_module("release_resume")
    config = {}
    with ExitStack() as stack:
        spec = importlib.util.spec_from_file_location("resume_dav", ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        dav_root = tmp_path / "dav"
        dav_root.mkdir()
        (dav_root / ".ryn-acceptance-isolated").touch()
        dav = stack.enter_context(fixture.FixtureDAV(dav_root))
        world = resume.prepare_documents(stack, config, tmp_path, "R03", policy(), dav.url)
        sources = world.sources.path.read_bytes()
        # Use the actual grant store path rather than assuming its filename.
        grants = world.owner_app.state.ai_access.store.path
        original_grants = grants.read_bytes()
        config_path = save(tmp_path / "worker.json", config)
        worker = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(config_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=45)
        assert worker.returncode == 0, worker.stderr
        observed = json.loads(worker.stdout.strip().splitlines()[-1])
        assert observed["ids"] == config["release_world"]["node_ids"]
        assert observed["provider"] == observed["ids"]["node-a"] != observed["ids"]["node-b"]
        assert observed["consumer"] == observed["ids"]["node-b"]
        assert observed["response"] is True and observed["local_sources"] == {}
        assert world.sources.path.read_bytes() == sources and grants.read_bytes() == original_grants
        missing = tmp_path / "missing"
        with pytest.raises(ValueError, match="identity_missing"):
            resume.existing_identity(missing)
        assert not missing.exists()


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_r03_real_worker_restore_keeps_original_operation_and_one_put(prepared, monkeypatch, cohort, worker_dependencies):  # noqa: F811
    monkeypatch.syspath_prepend(str(CONTROLLERS))
    resume = importlib.import_module("release_resume")
    collector = importlib.import_module("release_collection")
    proof = importlib.import_module("consumer_process_proof")
    executable, upstream = worker_dependencies
    prepared["manifest"]["artifacts"]["reference_node"] = registry.binding(executable)
    activate_case(prepared, "R03", collector)
    root, output = prepared["root"], prepared["root"] / "raw"
    output.mkdir()
    owner = root / "owner"
    (owner / "llm").mkdir(parents=True)
    (owner / "llm/sources.enc").write_bytes(b"offline test placeholder; test worker uses only scripted adapter")
    case = registry.composer().load_effective_case("R03")["effective_case"]
    config = {"case_id": "R03", "cohort": cohort, "phase": "crash", "live": False,
        "trial": 1, "release_activation": str(prepared["activation"]), "output": str(output),
        "owner_home": str(owner), "owner_source_sha256": registry.sha(owner / "llm/sources.enc"),
        "source_id": "src-7de9fb1d36c44bc8", "planner_policy": policy(), "prompt": case["prompt"],
        "node": str(executable), "upstream": str(upstream)}
    with ExitStack() as stack:
        spec = importlib.util.spec_from_file_location("resume_r03_dav", ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        dav_root = root / "dav"
        (dav_root / "reports/acceptance").mkdir(parents=True)
        (dav_root / ".ryn-acceptance-isolated").touch()
        dav = stack.enter_context(fixture.FixtureDAV(dav_root))
        config["dav_url"] = dav.url
        resume.prepare_documents(stack, config, root, "R03", policy(), dav.url)
        home = Path(config["home"])
        config_path = save(root / "worker.json", config)
        with (output / "crash.log").open("w", encoding="utf-8") as log:
            first = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(config_path), "R03"],
                cwd=ROOT, stdout=log, stderr=log)
            owned = proof.OwnedConsumerProcess(first)
            try:
                deadline = time.monotonic() + 45
                while not (home / "crash-ready.json").exists() and first.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                assert (home / "crash-ready.json").exists(), (output / "crash.log").read_text(encoding="utf-8")
                pid = registry.read(home / "worker-crash.json")["worker_pid"]
                owned.bind_worker(pid)
                fault = registry.read(home / "crash-ready.json")
                assert owned.terminate()["confirmed_terminal"]
            finally:
                owned.terminate()
                owned.close()
        handoff = registry.read(output / "release-worker-handoff.json")
        initial_events = sorted(output.glob("release-request-*.json"))
        config["phase"] = "recover"
        save(config_path, config)
        second = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(config_path), "R03"],
            cwd=ROOT, capture_output=True, text=True, timeout=45)
        assert second.returncode == 0, second.stderr
        assert registry.read(home / "worker-recover.json")["worker_pid"] != pid
        assert registry.read(output / "release-worker-handoff.json") == handoff
        assert registry.read(output / "release-worker-recover.json")["prior_dispatches"] == len(initial_events)
        assert (dav_root / "reports/acceptance/recovery.md").read_text() == "RECOVERY_ONCE_731"
        assert sum(row["method"] == "PUT" for row in dav.audit) == 1
        observed = registry.read(output / "release-collection.json")
        assert observed["dispatch_audit"]["coverage"] == "all_model_and_tool_dispatches"
        task = registry.read(home / "consumer-result.json")
        if cohort == "reference":
            native = registry.read(output / "native-recover.json")
            assert native["reference_recovery"]["reason"] == "native_pending_execution_not_restored"
            assert native["reference"]["dispatch_counts"] == {"model": 0, "tool": 0, "approval": 0}
            assert native["reference_business_operations"][fault["operation_id"]]["state"] == "dispatched"
        else:
            assert task["reconciliation"]["state"] == "executed"


def offline_r03_worker(config_path):
    """Test-only adapter, reached through the real signed B-to-A provider gate."""
    import run_recovery

    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.tool_menu import NAME
    config = registry.read(config_path)
    if config["cohort"] == "reference":
        def forbidden(*_):
            raise AssertionError("native worker must not use candidate planner")
        AgentRuntime.run_once = forbidden

    class Adapter:
        def health(self):
            return {"ok": True}

        def chat(self, body, **_):
            specs, messages = body.get("tools", []), body["messages"]
            if not specs:
                content = json.dumps({"status": "unknown", "summary": "Offline process wiring only.",
                    "uncovered_requirements": ["independent semantic assessment"], "evidence_refs": []})
                message = {"role": "assistant", "content": content}
            else:
                receipts = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                roots = next((r["roots"] for r in receipts if "roots" in r), None)
                created = any(r.get("ok") and (r.get("name") == "recovery.md" or r.get("path", "").endswith("recovery.md")) for r in receipts)
                name, args = "ryn_list_resource_roots", {}
                if roots:
                    name, args = "ryn_create_text_file", {"directory_ref": roots[0]["resource_ref"], "name": "recovery.md", "content": "RECOVERY_ONCE_731"}
                if name not in {s["function"]["name"] for s in specs}:
                    name, args = NAME, {"load": [name]}
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "offline-" + str(time.time_ns()),
                    "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                if created:
                    message = {"role": "assistant", "content": "Offline process check finished."}
            return {"message": message, "text": message.get("content") or "", "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "input_tokens": 8, "output_tokens": 4, "reported_usage": {"input_tokens": 8, "output_tokens": 4}, "duration_ms": 0}
    run_recovery.read_planner = lambda *_: ({}, Adapter(), config["planner_policy"])
    run_recovery.child(Path(config_path))


def test_r06_real_collector_restart_keeps_compaction_and_original_4_plus_8(prepared, monkeypatch):  # noqa: F811
    monkeypatch.syspath_prepend(str(CONTROLLERS))
    resume = importlib.import_module("release_resume")
    collector = importlib.import_module("release_collection")
    controller = importlib.import_module("run_r06")
    activate_case(prepared, "R06", collector)
    root, output = prepared["root"], prepared["root"] / "raw"
    output.mkdir()
    owner = root / "owner"
    (owner / "llm").mkdir(parents=True)
    (owner / "llm/sources.enc").write_bytes(b"offline test adapter only")
    config = {"case_id": "R06", "cohort": "ryn", "phase": "before", "live": False,
        "trial": 1, "release_activation": str(prepared["activation"]), "output": str(output),
        "owner_home": str(owner), "owner_source_sha256": registry.sha(owner / "llm/sources.enc"),
        "source_id": "src-7de9fb1d36c44bc8", "planner_policy": policy()}
    with ExitStack() as stack:
        spec = importlib.util.spec_from_file_location("resume_r06_dav", ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        dav_root = root / "dav"
        (dav_root / "projects/orion/week-41").mkdir(parents=True)
        (dav_root / "reports/acceptance").mkdir(parents=True)
        (dav_root / ".ryn-acceptance-isolated").touch()
        shutil.copyfile(ROOT / "tests/fixtures/agent_acceptance/documents/orion/long-notes.txt",
                        dav_root / "projects/orion/week-41/long-notes.txt")
        dav = stack.enter_context(fixture.FixtureDAV(dav_root))
        config["dav_url"] = dav.url
        resume.prepare_documents(stack, config, root, "R06", policy(), dav.url)
        home = Path(config["home"])
        observations = []
        for phase in ("before", "after"):
            config["phase"] = phase
            config_path = save(root / (phase + ".json"), config)
            with (output / (phase + ".log")).open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(config_path), "R06"],
                    cwd=ROOT, stdout=log, stderr=log)
                owned = controller.OwnedConsumerProcess(process)
                try:
                    marker = controller.wait_file(home / ("worker-" + phase + ".json"), process, 10)
                    assert marker, (output / (phase + ".log")).read_text(encoding="utf-8")
                    owned.bind_worker(marker["worker_pid"])
                    result_path = home / ("safe-checkpoint.json" if phase == "before" else "result-after.json")
                    observed = controller.wait_file(result_path, process, 45)
                    assert observed, (output / (phase + ".log")).read_text(encoding="utf-8")
                    observations.append(observed)
                finally:
                    assert owned.terminate()["confirmed_terminal"]
                    owned.close()
            if phase == "before":
                config.update(checkpoint=observed["safe_checkpoint"]["checkpoint"], identity=observed["identity"])
        before = registry.read(output / "release-worker-before.json")
        after = registry.read(output / "release-worker-after.json")
        assert before["registration"] == after["registration"] and before["pid"] != after["pid"]
        assert observations[1]["owner_continue"]["budget_request"]["limits"]["max_total_model_calls"] == 12
        assert observations[1]["owner_continue"]["calls_after_budget_before_owner_continue"] == 0
        assert observations[1]["owner_continue"]["continue_request"]["message"] == controller.CONTINUE
        assert registry.read(home / "restart-readback.json")["before_reconcile"] == config["checkpoint"]
        assert registry.read(output / "release-collection.json")["dispatch_audit"]["coverage"] == "all_model_and_tool_dispatches"
        assert not any(row["method"] == "PUT" for row in dav.audit)


def offline_r06_worker(config_path):
    import run_r06

    from rynmesh.agent_runtime.tool_menu import NAME
    config = registry.read(config_path)

    class Adapter:
        def health(self):
            return {"ok": True}

        def chat(self, body, **_):
            specs, messages = body.get("tools", []), body["messages"]
            if not specs:
                content = "Offline observations retained: no writes, only listed resource roots."
                if any('"evidence"' in str(m.get("content")) for m in messages):
                    content = json.dumps({"status": "unknown", "summary": "Offline restart wiring only.",
                        "uncovered_requirements": ["actual report"], "evidence_refs": []})
                message = {"role": "assistant", "content": content}
            elif any(m.get("role") == "user" and m.get("content") == run_r06.CONTINUE for m in messages):
                message = {"role": "assistant", "content": "Offline control check finished; no report claimed."}
            else:
                name, args = "ryn_list_resource_roots", {}
                available = name in {s["function"]["name"] for s in specs}
                if not available:
                    name, args = NAME, {"load": [name]}
                content = "Synthetic retained observation. " * 160 if config["phase"] == "before" and available else ""
                message = {"role": "assistant", "content": content, "tool_calls": [{"id": "offline-" + str(time.time_ns()),
                    "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            return {"message": message, "text": message.get("content") or "", "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "input_tokens": 8, "output_tokens": 4, "reported_usage": {"input_tokens": 8, "output_tokens": 4}, "duration_ms": 0}
    run_r06.read_planner = lambda *_: ({}, Adapter(), config["planner_policy"])
    run_r06.child(Path(config_path))


if __name__ == "__main__":
    sys.path.insert(0, str(CONTROLLERS))
    if len(sys.argv) == 3:
        (offline_r03_worker if sys.argv[2] == "R03" else offline_r06_worker)(Path(sys.argv[1]))
        raise SystemExit(0)
    from release_resume import reopen_documents

    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.services import peer_box
    with ExitStack() as stack:
        world = reopen_documents(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), stack)
        consumer = world.consumer
        runtime = AgentRuntime(AgentStore(consumer.home / "agent", peer_box.load_or_create_messaging_key(consumer.home / "messaging.x25519")),
            consumer.bridge, consumer.tools, consumer.models, owner_id="owner-test", consumer_node_id=consumer.store.peer_id)
        runtime.update_settings({"enabled": True, "model_id": world.selected})
        task = runtime.create({"message": "Offline wiring check"})
        destination = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])["model_destination"]
        world.consumer.bridge.complete(world.selected, [{"role": "user", "content": "Offline wiring check"}], [],
            lambda: False, max_output_tokens=32, expected_destination=destination)
        audit = world.consumer.wire_audit[-1]
        print(json.dumps({"ids": {k: v.peer_id for k, v in world.instantiated_nodes.items()},
            "provider": audit["provider_peer_id"], "consumer": audit["consumer_peer_id"],
            "response": audit["response_received"], "local_sources": world.consumer.sources.read()}))
