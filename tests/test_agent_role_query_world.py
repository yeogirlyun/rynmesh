"""Opt-in owner-role query collection, with no external model calls."""
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS = ROOT / "docs/acceptance/ryn-agent-delivery/real-model"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTROLLERS))
    return importlib.import_module("role_query_world"), importlib.import_module("run_queries")


def runtime_for(world, app, store, planner, *, owner_id="owner-test", consumer_id=None):
    _, tools, catalog = build_agent_integrations(app, store, store.home)
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), planner, tools, catalog,
                           owner_id=owner_id, consumer_node_id=consumer_id or store.peer_id)
    app.state.agent_runtime = runtime
    runtime.update_settings({"enabled": True, "model_id": world.selected, "max_steps": 24,
                             "max_seconds": 600, "max_context_chars": 100000, "permission_mode": "read_only"})
    return runtime


@pytest.mark.parametrize("case_id", ["Q01", "Q06"])
def test_owner_role_is_real_store_and_tools_not_relabeled_receiver(node, tmp_path, modules, case_id):  # noqa: F811
    app, store = node
    module, runner = modules
    with module.RoleQueryWorld(tmp_path / "world", case_id) as world:
        world.configure(app, store)
        assert world.bindings["node-a"] == store.peer_id
        assert len({world.bindings[key] for key in module.composer.LOGICAL_NODES}) == 4
        runtime = runtime_for(world, app, store, runner.FixturePlanner(case_id))
        task = world.create_task(runtime)
        context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
        assert context["owner_id"] == "owner-test" and context["consumer_node_id"] == store.peer_id
        assert not context["resource_grants"] and not context["egress_grants"]
        models = runtime.tools.execute("ryn_list_models", {}, operation_id="owner-models", context=context, cancelled=lambda: False)
        assert {row["fixture_identity"] for row in models["models"]} == set(world.world["models"])
        assert all(row["peer_id"] == store.peer_id and row["sharing_configuration_known"] for row in models["models"])
        services = runtime.tools.execute("ryn_inspect_services", {}, operation_id="owner-grants", context=context, cancelled=lambda: False)
        assert services["friend_grants_scope"]["provider_peer_id"] == store.peer_id
        assert len(services["friend_grants"]) == 1
        assert services["friend_grants"][0]["peer_id"] == world.bindings["node-b"]
        devices = runtime.tools.execute("ryn_list_devices", {}, operation_id="owner-devices", context=context, cancelled=lambda: False)
        own = next(row for row in devices["devices"] if row["peer_id"] == store.peer_id)
        assert own["http_probe_performed"] is False and own["evidence_source"] == "self"
        assert world.owner_health_probe["status"] == 200 and world.health_requests
        with pytest.raises(AgentToolRejected, match="agent_permission_denied"):
            runtime.tools.execute("ryn_set_model_sharing", {"model_id": world.bindings["model-b"], "enabled": True},
                                  operation_id="denied", context=context, cancelled=lambda: False)
        evidence = world.collect()
        binding = evidence["actual_role_binding"]
        assert binding["execution_peer_id"] == binding["tools_store_peer_id"] == store.peer_id
        assert binding["task_context_owner_id"] == "owner-test"
        assert binding["stored_task_id"] == task["id"]
        assert binding["receiver_peer_id"] != store.peer_id
        assert evidence["receiver_evidence"]["agent_state_isolation"]["consumer_task_ids"] == []
        assert evidence["receiver_evidence"]["agent_state_isolation"]["provider_task_ids"] == [task["id"]]
        assert evidence["access_query"]["read_only_boundary_passed"] and evidence["metrics"]["writes"] == 0
        assert [p["result"] for p in evidence["access_query"]["setup_admission_probes"]] == ["allowed", "denied", "denied", "denied"]
        effective = evidence["effective_input"]
        assert effective["identity_binding"]["status"] == "declared_bindings_consistent"
        assert effective["effective_case"]["prompt"] == runner.QueryWorld(tmp_path / "unused", case_id).case["prompt"]
        assert effective["effective_sha256"] == module.composer.digest({k: v for k, v in effective.items() if k != "effective_sha256"})
        assert not effective["formal_ready"] and not effective["enabled"]
        assert world.upstream.secret not in json.dumps(evidence)


@pytest.mark.parametrize("mismatch", ["owner", "node"])
def test_owner_role_rejects_runtime_identity_mismatch_before_task_creation(node, tmp_path, modules, mismatch):  # noqa: F811
    app, store = node
    module, runner = modules
    with module.RoleQueryWorld(tmp_path / "world", "Q06") as world:
        world.configure(app, store)
        runtime = runtime_for(world, app, store, runner.FixturePlanner("Q06"),
            owner_id="wrong-owner" if mismatch == "owner" else "owner-test",
            consumer_id=world.bindings["node-b"] if mismatch == "node" else None)
        with pytest.raises(RuntimeError, match="role_query_runtime_identity_mismatch"):
            world.create_task(runtime)
        assert runtime.store.read()["tasks"] == {}


def test_v2_kind_is_rejected_by_existing_v1_normalizer(tmp_path, modules):
    normalizer = importlib.import_module("normalize_diagnostic")
    source = tmp_path / "run.json"
    source.write_text(json.dumps({"case_id": "Q06", "cohort": "ryn", "task": {},
                                 "kind": "local-role-query-v2-draft"}), encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="query_collector_kind_required"):
        normalizer.normalize(source, tmp_path / "normalized")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not (tmp_path / "normalized").exists()


def test_draft_controller_rejects_non_query_role_cases(tmp_path, modules):
    module, _ = modules
    with pytest.raises(ValueError, match="role_query_case_unsupported"):
        module.RoleQueryWorld(tmp_path, "Q02")


def test_internal_runtime_tool_is_captured_without_altering_proposal(node, tmp_path, modules):  # noqa: F811
    app, store = node
    module, runner = modules
    with module.RoleQueryWorld(tmp_path / "world", "Q01") as world:
        world.configure(app, store)
        class Planner(runner.FixturePlanner):
            proposed = False
            def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
                if not self.proposed and specs:
                    self.proposed = True
                    return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "plan-proposal", "type": "function",
                        "function": {"name": "ryn_update_plan", "arguments": '{"goal_revision":1,"steps":[]}'}}]}}
                return super().complete(model_id, messages, specs, cancelled, on_event, **options)
        runtime = runtime_for(world, app, store, Planner("Q01"))
        output = tmp_path / "capture"
        output.mkdir()
        record = {"case_id": "Q01", "cohort": "ryn", "run_id": "c" * 32, "run_nonce": "d" * 32,
                  "dataset_version": module.composer.DATASET_VERSION, "collector_profile": "synthetic-internal-tool-test"}
        collector = module.DraftDispatchCollector(output, record)
        world.on_task_created = lambda task: collector.register(world, runtime, task)
        outcome = world.execute(runtime)
        proposals = [event for event in collector.events if event.get("tool_name") == "ryn_update_plan"]
        assert len(proposals) == 1
        assert proposals[0]["task_binding_basis"] == "durable_runtime_queue_internal_tool_entry"
        response = next(json.loads(m["content"]) for m in outcome["task"]["messages"] if m.get("tool_call_id") == "plan-proposal")
        assert response["accepted"] is True and response["criteria_proposals_accepted"] is False
        assert outcome["task"]["permission_mode"] == "read_only" and outcome["metrics"]["writes"] == 0


@pytest.mark.parametrize("case_id", ["Q01", "Q06"])
@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_fixture_cli_both_cohorts_retain_v2_draft_identity_and_evidence(case_id, cohort, modules):
    node_binary = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    upstream = ROOT / ".codex-tmp/research/zcode-runtime-reference"
    if cohort == "reference" and (not node_binary.exists() or not upstream.exists()):
        pytest.skip("Pinned reference engine/runtime unavailable; remains pending")
    command = [sys.executable, str(CONTROLLERS / "run_queries.py"), "--case", case_id,
               "--fixture-only", "--cohort", cohort, "--role-contract-v2-draft"]
    if cohort == "reference":
        command += ["--node", str(node_binary), "--upstream", str(upstream)]
    run = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", timeout=150)
    assert run.returncode == 0, run.stdout + run.stderr
    outputs = [json.loads(line)["output"] for line in run.stdout.splitlines() if line.startswith('{"output":')]
    assert outputs, run.stdout
    output = Path(outputs[-1])
    record = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert "error_type" not in record, {"path": str(output), "error": record.get("error_type")}
    assert record["kind"] == "local-role-query-v2-draft" and not record["real_model"]
    assert record["code_unchanged"] and record["source_configuration_unchanged"]
    assert record["access_query"]["read_only_boundary_passed"] and record["metrics"]["writes"] == 0
    assert record["model_calls"] and all(not call["credential_egress_detected"] for call in record["model_calls"])
    effective = json.loads((output / "effective-input.json").read_text(encoding="utf-8"))
    registration = json.loads((output / "registration.json").read_text(encoding="utf-8"))
    binding = json.loads((output / "task-binding.json").read_text(encoding="utf-8"))
    composer = modules[0].composer
    assert record["effective_input_sha256"] == effective["effective_sha256"]
    assert binding["registration_sha256"] == hashlib.sha256((output / "registration.json").read_bytes()).hexdigest()
    assert registration["effective_input_sha256"] == effective["effective_sha256"]
    assert registration["registration_kind"] == "draft_development_collection_only"
    assert not registration["formal_trial_registered"] and not record["formal_trial_registered"]
    actual = record["actual_role_binding"]
    assert actual["runtime_owner_id"] == actual["task_context_owner_id"] == "owner-test"
    assert actual["execution_peer_id"] == actual["runtime_consumer_node_id"] == record["bindings"]["node-a"]
    assert actual["receiver_peer_id"] == record["bindings"]["node-b"] != actual["execution_peer_id"]
    assert actual["task_readback_sha256"] == composer.digest(actual["task_readback"])
    assert actual["stored_task_id"] == binding["task_id"]
    assert record["model_requests"] and record["dispatch_audit"]["completed_collection"]
    audit = json.loads((output / "dispatch-audit.json").read_text(encoding="utf-8"))["dispatch_audit"]
    durable = [json.loads(line) for line in (output / "dispatch-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert audit["events"] == durable == record["dispatch_audit"]["events"]
    assert audit["coverage"] == "all_model_and_tool_dispatches"
    assert record["first_dispatch_at"] == durable[0]["dispatched_at"]
    model_events = [event for event in durable if event["kind"] == "model"]
    assert model_events == record["model_requests"] == [call["dispatch_provenance"] for call in record["model_calls"]]
    from datetime import datetime
    for event in durable:
        assert event["sampling_binding"] == record["sampling_binding"]
        assert event["task_id"] == binding["task_id"]
        assert event["registration_sha256"] == binding["registration_sha256"]
        assert datetime.fromisoformat(registration["registered_at"]) <= datetime.fromisoformat(binding["bound_at"])
        assert datetime.fromisoformat(binding["bound_at"]) <= datetime.fromisoformat(event["dispatched_at"]) <= datetime.fromisoformat(record["ended_at"])
    # Collection bindings are not inserted into prompts, specs or tool replies.
    for call in record["model_calls"]:
        assert record["run_nonce"] not in json.dumps([call["request_messages"], call["tools"]])
    assert record["receiver_evidence"]["agent_state_isolation"]["consumer_task_ids"] == []
    if cohort == "ryn":
        assert record["task"]["status"] == "completed" and record["task"]["outcome"]["status"] == "unverified"
        assert record["role_execution_evidence"]["runtime_role"] == "ryn_agent_executor"
        tool_results = [json.loads(message["content"]) for message in record["task"]["messages"] if message["role"] == "tool"]
        assert tool_results and all("error" not in result for result in tool_results), tool_results
        assert any(result.get("models") for result in tool_results)
        assert len([event for event in durable if event["kind"] == "tool"]) >= 2 * len(tool_results)
        assert record["task"]["verification_evidence"]
        assert all(row["status"] == "passed" for row in record["task"]["verification_evidence"])
    else:
        assert record["reference_accounting_task"]["id"] == binding["task_id"]
        assert (Path(actual["agent_store_path"]).parent.parent / "reference-session.sqlite").exists()
        assert record["role_execution_evidence"]["runtime_role"] == "reference_accounting_and_tool_permission_host"
        assert record["role_execution_evidence"]["reference_native_session"]["exists"]
        native = record["reference_session_readback"]
        assert native["status"] == "observed", native
        native_rows = [json.loads(line) for line in (output / "native-rpc-events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert native["observed_request_ids"] == [event["id"] for event in model_events]
        assert native["observed_request_ids"] == [row["id"] for row in native_rows if row["method"] == "model"]
        assert all(row["session_id"] == native["session_id"] for row in native_rows)
        assert all(set(row) == {"id", "rpc_id", "session_id", "method", "sampling_binding", "task_id", "registration_sha256", "emitted_at"} for row in native_rows)
        assert native["sha256"] == record["role_execution_evidence"]["reference_native_session"]["sha256"]
        tool_results = [event["result"] for event in record["tool_events"] if event["type"] == "tool_result"]
        assert tool_results and all("error" not in result for result in tool_results), tool_results
        assert any(result.get("models") for result in tool_results)
