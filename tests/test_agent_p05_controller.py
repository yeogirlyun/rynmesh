"""P05 bootstrap/scanner checks only: real DAV and local NAS, no model calls."""
import copy
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.resources import ResourceTools
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.nas import mount_nas


@pytest.fixture
def runner(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("p05_controller", folder / "run_p05.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_created_read_only_connections_support_exact_file_scope_without_restart(runner, tmp_path):
    app = FastAPI()
    mount_nas(app, tmp_path / "node", lambda: None)
    resources = ResourceTools(app)  # Existing runtime created before NAS enable/configure.
    assert app.state.nas_plugin.cipher is None
    logged = []
    with runner.P05Documents(tmp_path / "documents") as documents, TestClient(app) as client:
        def private_request(method, path, body):
            response = client.request(method, path, json=body)
            assert response.status_code == 200, response.text
            return response.json()
        def request(method, path, body):
            logged.append(body)
            return private_request(method, path, body)
        selection = documents.configure(request, private_request)
        assert runner.CREDENTIAL not in json.dumps(logged)
        nas = app.state.nas_plugin
        encrypted = (nas.directory / "state.enc").read_bytes()
        assert runner.CREDENTIAL.encode() not in encrypted
        assert runner.CREDENTIAL in nas.cipher.decrypt(encrypted).decode()
        assert all(not source["writable"] for source in nas.state["sources"])
        context = {"task_id": "p05-offline", "owner_id": "owner-test", "consumer_node_id": "node-b",
            "model_destination": {"catalog_model_id": "peer-glm", "provider_peer_id": "node-a", "destination_revision": "bound"},
            "scope_revision": 1, "resource_origins": []}
        scope = resources.prepare(selection, **{key: context[key] for key in ("owner_id", "consumer_node_id", "model_destination")})
        context.update(scope)
        grant = context["resource_grants"][0]
        assert grant["root_kind"] == "file" and grant["recursive"] is False and grant["actions"] == ["read_ai"]
        original = resources._open(grant["root_resource_ref"])
        read = resources.execute("ryn_read_resource", {"resource_ref": grant["root_resource_ref"]}, context=context, cancelled=lambda: False)
        assert read["text"] == (runner.FIXTURES / "documents/orion/progress.md").read_text(encoding="utf-8")
        for path in ("projects/orion/week-41", "projects/orion/week-41/decisions.txt", "private/private-canary.txt"):
            forged = resources._seal({**original, "path": path})
            with pytest.raises(AgentToolRejected):
                resources.execute("ryn_read_resource", {"resource_ref": forged}, context=context, cancelled=lambda: False)
        assert not any(e["method"] == "GET" for e in documents.servers["docs-beta"].audit)
        for source_id in list(nas.sessions):
            nas._close_session(source_id)


def test_per_request_scans_detect_each_forbidden_channel_and_wrong_destination(runner):
    selected = {"peer_id": "node-a", "service_id": "glm-service"}
    rows = [{"kind": "consumer_order_start", "agent_task_id": "other-task", "request": {"task_id": "unrelated"}}]
    for name, marker in runner.CANARIES.items():
        rows.append({"kind": "consumer_order_start", "agent_task_id": "p05", "request": {
            "task_id": name, "provider_peer_id": "node-a", "service_id": "glm-service",
            "chat": {"messages": [{"role": "assistant", "reasoning_content": marker}]}}})
    rows.append({"kind": "consumer_order_start", "agent_task_id": "p05", "request": {
        "task_id": "wrong-peer", "provider_peer_id": "node-c", "service_id": "glm-service"}})
    scans = runner.scan_requests(rows, "p05", selected)
    assert len(scans) == 4
    assert {tuple(s["forbidden_canaries"]) for s in scans[:3]} == {(name,) for name in runner.CANARIES}
    assert scans[-1]["unapproved_destination"]
    assert all(s["usage"] == [] for s in scans)  # No response means unknown, not zero.
    assert not runner.scan_requests([], "p05", selected)


def test_provider_scan_requires_all_order_captures_and_retains_usage(runner, tmp_path, monkeypatch):
    selected = {"peer_id": "node-a", "service_id": "glm-service"}
    prior = {"task_id": "p05", "selected_model": selected, "consumer_request_scans": [{"order_id": "one"}, {"order_id": "two"}]}
    run = tmp_path / "run.json"
    run.write_text(json.dumps(prior), encoding="utf-8")
    audit = tmp_path / "provider.jsonl"
    rows = [{"kind": "upstream_dispatch", "task_id": "one", "model": "glm-5.3-flash", "body": {"messages": []}},
            {"kind": "upstream_response", "task_id": "one", "result": {"input_tokens": 9, "output_tokens": 4}}]
    audit.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    output = tmp_path / "scan"
    monkeypatch.setattr("sys.argv", ["run_p05.py", "--scan", "--run-file", str(run), "--provider-audit", str(audit), "--output", str(output)])
    runner.main()
    result = json.loads((output / "provider-scan.json").read_text(encoding="utf-8"))
    assert not result["all_orders_captured_once"] and result["formal_pass"] is False
    assert result["request_scans"][0]["usage"][0]["input_tokens"] == 9


@pytest.mark.parametrize("change", [{"source": "local_api"}, {"peer_id": "node-c"}, {"model_id": "another-model"}, {"destination_binding_supported": False}])
def test_unapproved_destination_fails_before_fixture_configuration_or_dispatch(runner, change):
    selected = {"id": "selected", "source": "peer", "peer_id": "node-a", "model_id": "glm-5.3-flash",
        "available": True, "agent_supported": True, "destination_binding_supported": True, "destination_revision": "bound", **change}
    calls = []
    def request(method, path, body=None):
        calls.append((method, path))
        return {"models": [selected]}
    with pytest.raises(RuntimeError, match="authenticated_node_a_glm_binding_required"):
        runner.execute(request, lambda *_: pytest.fail("credential setup called"), SimpleNamespace(), "selected", "node-a", {})
    assert calls == [("GET", "/api/local/agent/models")]


def binding_fixture():
    model_binding = {"version": "ryn.model-binding.v1", "service_id": "glm-service",
                     "model_generation": "generation", "rule_revision": "rule"}
    consumer = {"id": "selected", "peer_id": "node-a", "service_id": "glm-service",
        "model_id": "Two-host GLM development · glm-5.3-flash", "destination_revision": "bound",
        "expected_model_binding": {"model_binding": model_binding, "consumer_peer_id": "node-b"}}
    selected = {**copy.deepcopy(consumer), "source": "peer", "available": True,
                "agent_supported": True, "destination_binding_supported": True}
    binding = {"consumer": consumer, "provider": {"peer_id": "node-a", "service_id": "glm-service",
        "model_alias": consumer["model_id"], "model_id": "glm-5.3-flash", "model_binding": copy.deepcopy(model_binding),
        "owner_sources_sha256": "a" * 64}, "upstream_observation": {"kind": "upstream_dispatch", "task_id": "prior-order",
        "peer_id": "node-a", "service_id": "glm-service", "model": "glm-5.3-flash", "owner_sources_sha256": "a" * 64},
        "provenance": {**dict.fromkeys(("consumer_capture_sha256", "provider_capture_sha256",
                                       "provider_audit_sha256", "prior_provider_order_capture_sha256"), "b" * 64),
                       "consumer_captured_at": datetime.now(timezone.utc).isoformat(),
                       "provider_captured_at": datetime.now(timezone.utc).isoformat()}}
    return selected, binding


def test_reviewed_exact_alias_requires_matching_actual_provider_model_and_hash(runner, tmp_path):
    selected, binding = binding_fixture()
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    loaded = runner.load_model_binding(path, digest)
    assert runner.selected_peer(lambda *_: {"models": [selected]}, "selected", "node-a", loaded) == selected
    path.write_text(json.dumps({**binding, "unreviewed": True}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="reviewed_model_binding_hash_mismatch"):
        runner.load_model_binding(path, digest)


@pytest.mark.parametrize("section,key,value", [
    ("consumer", "id", "other"), ("consumer", "peer_id", "node-c"),
    ("consumer", "service_id", "other"), ("consumer", "model_id", "glm-5.3-flash"),
    ("consumer", "destination_revision", "stale"), ("consumer", "expected_model_binding", {}),
    ("provider", "peer_id", "node-c"), ("provider", "service_id", "other"),
    ("provider", "model_alias", "prefix glm-5.3-flash suffix"), ("provider", "model_id", "other"),
    ("provider", "model_binding", {}), ("provider", "owner_sources_sha256", "c" * 64),
    ("upstream_observation", "peer_id", "node-c"), ("upstream_observation", "service_id", "other"),
    ("upstream_observation", "model", "other"), ("upstream_observation", "task_id", ""),
    ("provenance", "provider_capture_sha256", ""), ("provenance", "prior_provider_order_capture_sha256", ""),
    ("provenance", "provider_captured_at", "2000-01-01T00:00:00+00:00"),
    ("provenance", "consumer_captured_at", "not-a-date"),
])
def test_each_model_proof_mismatch_fails_before_resource_changes_or_dispatch(runner, section, key, value):
    selected, binding = binding_fixture()
    binding[section][key] = value
    calls = []
    def request(method, path, body=None):
        calls.append((method, path))
        return {"models": [selected]}
    with pytest.raises(RuntimeError, match="authenticated_node_a_glm_binding_required"):
        runner.execute(request, lambda *_: pytest.fail("credential setup called"), SimpleNamespace(), "selected", "node-a", binding)
    assert calls == [("GET", "/api/local/agent/models")]


def registered_controller_fixture(runner, *, registration_change=None, create_error=False, status="failed"):
    """Synthetic HTTP receipts only; no inference or formal business verdict."""
    selected, model_binding = binding_fixture()
    release = {"dataset_version": "test-release", "manifest_sha256": "c" * 64}
    bindings = {"docs-alpha": "actual-alpha", "docs-beta": "actual-beta"}
    task = {"id": "p05-fresh", "status": status, "events": [], "result": "",
            "outcome": {"status": "failed", "reason": "synthetic business failure"}}
    bound = {**release, "case_id": "P05", "cohort": "ryn", "trial": 2, "run_id": "run", "run_nonce": "nonce"}
    receipt = {"task": copy.deepcopy(task), "registration": {"path": "/remote/registration.json", "sha256": "d" * 64},
               "registration_binding": copy.deepcopy(bound), "registration_observation_task_id": task["id"]}
    if registration_change:
        section, key, value = registration_change
        receipt[section][key] = value
    final = {**bound, "task": copy.deepcopy(task), "prompt": runner.load_case()["prompt"],
             "real_model": True, "planner_model": "glm-5.3-flash", "provider": "recorded-provider",
             "bindings": copy.deepcopy(bindings), "model_calls": [{"error_type": "SyntheticProviderFailure"}],
             "started_at": "server-started", "ended_at": "server-ended", "output": "/remote/collector",
             "code_snapshot": {"original.py": "e" * 64}, "original_extra_fact": {"unmodified": True},
             "release_collection": {"registration": copy.deepcopy(receipt["registration"]),
                 "task_readback": {"id": task["id"]}, "dispatch_audit": {"coverage": "partial_dispatch_collection", "events": []},
                 "authenticated_runtime": {"owner_id": "owner-test", "run_nonce": "nonce"}}}
    calls, saved = [], {}
    documents = SimpleNamespace(bindings=bindings, servers={label: SimpleNamespace(audit=[]) for label in bindings})
    def configure(request, private_request):
        calls.append(("FIXTURE", "configure", None))
        return runner.exact_scope(bindings["docs-alpha"])
    documents.configure = configure
    def request(method, path, body=None):
        calls.append((method, path, copy.deepcopy(body)))
        if path == "/api/local/two-host/evidence":
            return {"runtime_owner_id": "owner-test"}
        if path == "/api/local/agent/models":
            return {"models": [selected]}
        if path == "/api/local/agent/settings":
            return {"enabled": True, "permission_mode": "read_only"}
        if path == "/api/local/agent/resources":
            return {"connections": [{"connection_id": value, "agent_supported": True, "enabled": True, "writable": False}
                                    for value in bindings.values()]}
        if path.startswith("/api/local/ask/conversations/"):
            if method == "PUT":
                saved[path] = copy.deepcopy(body)
            return saved[path]
        if path == "/api/local/two-host/registered-task":
            if create_error:
                raise RuntimeError("owner_api_400_registration_rejected")
            return copy.deepcopy(receipt)
        if path.endswith("/finalize"):
            return copy.deepcopy(final)
        if path.endswith("/actions"):
            task["status"] = "cancelled"
            return copy.deepcopy(task)
        if path.endswith("/scope"):
            return runner.exact_scope(bindings["docs-alpha"])
        if path == "/api/local/agent/tasks":
            assert method == "GET", "ordinary creation bypassed registration"
            return {"tasks": []}
        if path == "/api/local/agent/tasks/p05-fresh":
            return copy.deepcopy(task)
        pytest.fail(f"unexpected endpoint: {method} {path}")
    return request, documents, release, model_binding, calls, receipt, final


def test_release_controller_uses_atomic_registered_endpoint_and_preserves_original_failed_record(runner):
    request, documents, release, binding, calls, receipt, final = registered_controller_fixture(runner)
    evidence = {"formal_pass": False, "formal_trials_registered": 0}
    result = runner.execute(request, lambda *_: pytest.fail("secret request unexpectedly used"), documents,
                            "selected", "node-a", binding, release=release, trial=2, evidence=evidence)
    creation = [row for row in calls if row[0] == "POST" and row[1].endswith("registered-task")]
    assert len(creation) == 1
    body = creation[0][2]
    assert body["case_id"] == "P05" and body["trial"] == 2 and body["fixture_bindings"] == documents.bindings
    assert body["task"]["message"] == runner.load_case()["prompt"]
    assert body["task"]["scope"] == runner.exact_scope(documents.bindings["docs-alpha"])
    assert body["task"]["budget_limits"] == {"max_total_model_calls": 24, "max_total_seconds": 600}
    assert not any(method == "POST" and path == "/api/local/agent/tasks" for method, path, _ in calls)
    assert calls.index(creation[0]) < next(i for i, row in enumerate(calls) if row[1].endswith("/finalize"))
    assert result["registered_task_receipt"] == receipt and result["registered_final_record"] == final
    assert all(result[key] == value for key, value in final.items())
    assert result["task"]["outcome"]["reason"] == "synthetic business failure"
    assert result["formal_trials_registered"] == 1 and result["formal_pass"] is False
    assert result["mechanical_checks"]["completed"] is False
    assert result["mechanical_checks"]["runtime_verified"] is False


def test_release_registration_failure_never_falls_back_to_ordinary_creation(runner):
    request, documents, release, binding, calls, _receipt, _final = registered_controller_fixture(runner, create_error=True)
    evidence = {"formal_pass": False, "formal_trials_registered": 0}
    with pytest.raises(RuntimeError, match="registration_rejected"):
        runner.execute(request, lambda *_: None, documents, "selected", "node-a", binding,
                       release=release, trial=2, evidence=evidence)
    assert evidence["formal_trials_registered"] == 0 and evidence["formal_pass"] is False
    assert not any(method == "POST" and path == "/api/local/agent/tasks" for method, path, _ in calls)
    assert not any(path.endswith("/finalize") for _, path, _ in calls)
    assert evidence["other_session_seed"]["readback_verified"] is True


@pytest.mark.parametrize("change", [
    ("registration_binding", "case_id", "P04"), ("registration_binding", "cohort", "reference"),
    ("registration_binding", "trial", 1), ("registration_binding", "manifest_sha256", "e" * 64),
    ("registration_binding", "dataset_version", "another-release"),
    ("registration_binding", "run_nonce", ""), ("task", "id", "unregistered-task"),
])
def test_release_receipt_mismatch_cancels_and_keeps_failure_without_fallback(runner, change):
    request, documents, release, binding, calls, _receipt, _final = registered_controller_fixture(runner, registration_change=change, status="queued")
    evidence = {"formal_pass": False, "formal_trials_registered": 0}
    # An altered task ID has its own cleanup reads, all captured without dispatch.
    if change[0] == "task":
        original = request
        def request(method, path, body=None):
            return original(method, path.replace("unregistered-task", "p05-fresh"), body)
    with pytest.raises(RuntimeError, match="release_registration_identity_mismatch"):
        runner.execute(request, lambda *_: None, documents, "selected", "node-a", binding,
                       release=release, trial=2, evidence=evidence)
    assert evidence["formal_trials_registered"] == 0 and evidence["formal_pass"] is False
    assert evidence["cleanup_cancel_receipt"]["status"] == "cancelled"
    assert not any(method == "POST" and path == "/api/local/agent/tasks" for method, path, _ in calls)
    assert evidence["registered_final_record"]
    assert evidence["release_finalize_error"] in {"release_registration_identity_mismatch", "release_final_record_identity_mismatch"}
    assert "fixture_http_audit" in evidence


@pytest.mark.parametrize("field,value", [("case_id", "P04"), ("cohort", "reference"), ("trial", 3),
    ("manifest_sha256", "e" * 64), ("run_nonce", "another-nonce"), ("real_model", False),
    ("bindings", {"docs-alpha": "another-connection", "docs-beta": "actual-beta"})])
def test_finalize_binding_failure_retains_raw_record_and_explicit_failure(runner, field, value):
    original, documents, release, binding, _calls, _receipt, _final = registered_controller_fixture(runner)
    def request(method, path, body=None):
        result = original(method, path, body)
        if path.endswith("/finalize"):
            result[field] = value
        return result
    evidence = {"formal_pass": False, "formal_trials_registered": 0}
    runner.execute(request, lambda *_: None, documents, "selected", "node-a", binding,
                   release=release, trial=2, evidence=evidence)
    assert evidence["registered_final_record"][field] == value
    assert evidence["release_finalize_error"] == "release_final_record_identity_mismatch"
    assert evidence["formal_pass"] is False and evidence["formal_trials_registered"] == 1
    assert evidence["task"]["outcome"]["status"] == "failed"
    assert evidence["mechanical_checks"]["runtime_verified"] is False
    assert "release_collection" not in evidence


def test_development_execute_preserves_ordinary_path_without_release_registration(runner):
    original, documents, _release, binding, calls, receipt, _final = registered_controller_fixture(runner)
    ordinary = []
    def request(method, path, body=None):
        if method == "POST" and path == "/api/local/agent/tasks":
            ordinary.append(copy.deepcopy(body))
            return copy.deepcopy(receipt["task"])
        return original(method, path, body)
    evidence = runner.execute(request, lambda *_: None, documents, "selected", "node-a", binding)
    assert len(ordinary) == 1 and ordinary[0]["message"] == runner.load_case()["prompt"]
    assert not any("registered-task" in path for _, path, _ in calls)
    assert "registered_final_record" not in evidence


def test_release_runtime_owner_must_already_be_set_before_fixture_changes(runner):
    calls = []
    def request(method, path, body=None):
        calls.append((method, path))
        return {"runtime_owner_id": "default-owner"}
    with pytest.raises(RuntimeError, match="release_runtime_owner_must_be_set_at_launch"):
        runner.execute(request, lambda *_: pytest.fail("credential setup called"), SimpleNamespace(),
                       "selected", "node-a", {}, release={"manifest_sha256": "a" * 64}, trial=1)
    assert calls == [("GET", "/api/local/two-host/evidence")]


def test_release_activation_identity_is_read_without_loading_case_contracts(runner, tmp_path):
    activation = tmp_path / "activation.json"
    value = {"kind": "ryn.agent.release-activation", "dataset_version": "test-release", "formal_pass": False,
             "formal_trials_registered": 0, "manifest": {"path": "/remote/manifest.json", "sha256": "c" * 64}}
    activation.write_text(json.dumps(value), encoding="utf-8")
    observed = runner.load_release_activation(activation)
    assert observed["manifest_sha256"] == "c" * 64
    assert observed["activation"]["sha256"] == hashlib.sha256(activation.read_bytes()).hexdigest()
    value["formal_pass"] = True
    activation.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError, match="release_activation_identity_invalid"):
        runner.load_release_activation(activation)


def test_release_activation_cannot_be_used_for_retrospective_scan(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_p05.py", "--scan", "--release-activation", "activation.json",
        "--run-file", "run.json", "--provider-audit", "audit.jsonl", "--output", str(tmp_path / "output")])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2 and not (tmp_path / "output").exists()
