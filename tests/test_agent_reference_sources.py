"""Native source workflows and exact secure-input authority, offline only."""
import copy
import importlib.util
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs/acceptance/ryn-agent-delivery/reference"
REAL_MODEL = ROOT / "docs/acceptance/ryn-agent-delivery/real-model"


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.syspath_prepend(str(REFERENCE))
    monkeypatch.syspath_prepend(str(REAL_MODEL))
    spec = importlib.util.spec_from_file_location("native_source_controller", REAL_MODEL / "run_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case,scenario", [("C01", None), ("C02", "pre_save_failure"), ("C02", "post_save_conflict")])
def test_actual_pinned_native_source_workflow(controller, monkeypatch, tmp_path, case, scenario):
    executable = os.environ.get("RYN_REFERENCE_NODE") or shutil.which("node")
    upstream = Path(os.environ.get("RYN_REFERENCE_UPSTREAM", ROOT / ".codex-tmp/research/zcode-runtime-reference"))
    if not executable or not upstream.is_dir():
        pytest.skip("Pinned native dependencies absent; set RYN_REFERENCE_NODE and RYN_REFERENCE_UPSTREAM")
    for name in ("RYNMESH_HOME", "RYNMESH_REGISTRY_URL", "RYNMESH_AUTO_REGISTER", "RYNMESH_LAN_DISCOVERY", "RYNMESH_DEFAULT_DISCOVERY"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    output = tmp_path / "native-evidence"
    argv = [str(REAL_MODEL / "run_sources.py"), "--fixture-only", "--case", case, "--cohort", "zcode",
            "--node", executable, "--upstream", str(upstream), "--output", str(output)]
    if scenario:
        argv += ["--scenario", scenario]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(AgentRuntime, "run_once", lambda self: pytest.fail("Native reference invoked candidate planner"))
    controller.main()
    record = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert "error_type" not in record and all(record["checks"].values()), record.get("error_code")
    assert record["cohort"] == "zcode" and record["real_model"] is False and record["formal_case_score"] is False
    assert record["reference"]["upstream_commit"] == "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
    assert record["reference"]["result"]["persistent_session"] is True
    assert (Path(record["isolated_run_root"]) / "reference-session.sqlite").stat().st_size > 0
    assert record["task"]["messages"][0]["content"] == record["prompt"]
    assert record["task"]["status"] == "queued" and record["reference_current_task_model_unchanged"]
    inputs = list(record["reference_source_inputs"].values())
    assert len(inputs) == 1 and inputs[0]["state"] == "answered"
    assert inputs[0]["control_revision_after"] == inputs[0]["binding"]["control_revision"] + 3
    assert inputs[0]["credential"]["has_credential"] is True
    endpoint = inputs[0]["credential"]["base_url"]
    first_request = json.dumps(record["model_calls"][0]["request_messages"])
    assert endpoint not in first_request
    assert all(call["fixture_credential_egress_detected"] is False for call in record["model_calls"])
    counts, lifetime = record["reference"]["dispatch_counts"], record["reference_budget"]["lifetime"]
    assert counts["model"] == len(record["model_calls"])
    assert lifetime["model_calls"] == counts["model"] + len(record["upstream_audit"])
    verifications = record.get("reference_source_verifications", [])
    assert lifetime["tool_calls"] == counts["tool"] - 1 + len(verifications)
    assert lifetime["unsettled_operations"] == 0 and lifetime["cost"] is None
    assert len(record["owner_inputs"]) == 1 and record["reference_owner_wait_seconds"] > 0
    assert record["reference"]["result"]["owner_wait_seconds"] > 0
    if case == "C01":
        assert len(verifications) == counts["approval"] == 1 and verifications[0]["verification"]["status"] == "passed"
    elif scenario == "pre_save_failure":
        assert counts["approval"] == 0 and not verifications
        assert record["business_state"]["source_version"] == 7
        assert lifetime["usage_complete"] is False
    else:
        assert len(verifications) == counts["approval"] == 1 and verifications[0]["verification"]["status"] == "failed"
        assert record["business_state"]["source_version"] == 9
        assert record["business_state"]["rollback_conflict_rejected"] is None
        assert lifetime["usage_complete"] is False
        assert any('"source_version":9' in json.dumps(call["request_messages"]).replace(" ", "").replace('\\"', '"')
                   for call in record["model_calls"])


@pytest.fixture
def input_port(node, monkeypatch):  # noqa: F811
    monkeypatch.syspath_prepend(str(REFERENCE))
    from budget_host import ReferenceBudget
    from operation_host import ReferenceOperationAdmission
    from source_input_host import ReferenceSourceInput
    from tool_host import ReferenceToolHost
    app, store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    def owner(request):
        if request.headers.get("x-fixture-owner") != "yes":
            raise HTTPException(401)
    runtime = install_agent(app, store=store, home=store.home, workers=BackgroundWorkerRegistry(), local_control=owner,
        messaging_key=peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"), integrations=(bridge, tools, catalog))
    runtime.update_settings({"enabled": True, "model_id": model_id})
    task = runtime.create({"message": "Configure a private source through the secure form."})
    context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
    record, state = {}, {"mode": None, "http": [], "secret": "source-port-private-fixture-key"}
    with TestClient(app) as client:
        def fill(row):
            body = {"expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"],
                "pending_input_id": row["pending_input"]["id"], "client_request_id": str(uuid.uuid4()),
                "kind": "local", "base_url": "http://127.0.0.1:19999/v1", "api_key": state["secret"]}
            if state["mode"] == "wrong_pending":
                body["pending_input_id"] = "wrong-input"
            if state["mode"] == "wrong_revision":
                body["expected_control_revision"] -= 1
            if state["mode"] == "cancel":
                runtime.action(task["id"], {"action": "cancel"})
            response = client.post(f'/api/local/agent/tasks/{task["id"]}/source-credential',
                headers={"x-fixture-owner": "yes"}, json=body)
            state["http"].append(response.status_code)
            if state["mode"] == "steer_after_form":
                current = runtime.get(task["id"])
                runtime.add_message(task["id"], {"message": "New goal", "intent": "steer",
                    "expected_control_revision": current["control_revision"], "expected_goal_revision": current["goal"]["revision"]})
            if state["mode"] == "revoke_after_form":
                runtime.update_settings({"permission_mode": "read_only"})
            return response.json()
        source = ReferenceSourceInput(tools, runtime, context, fill, record)
        admission = ReferenceOperationAdmission(runtime, task["id"], context)
        gate = ReferenceToolHost(source, policy=lambda _: False, cancelled=lambda: False, context=context, admission=admission)
        meter = ReferenceBudget(runtime, task["id"], guard=gate.check_current_permissions, admission=admission)
        gate.cancelled = meter.cancelled
        source.bind(meter, admission)
        call = {"name": "ryn_request_input", "tool_call_id": "native-input-original", "arguments": {
            "goal_revision": 1, "kind": "credential_ref", "prompt": "Secure setup form",
            "fields": [{"id": "source", "label": "Source", "type": "credential_ref", "required": True}]}}
        yield runtime, task, gate, meter, call, state


def test_native_input_refreshes_only_sanctioned_binding_and_has_no_secret(input_port):
    runtime, task, gate, meter, call, state = input_port
    context_before = copy.deepcopy(gate.context)
    result = gate.execute(call)
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["status"] == "queued" and not row["pending_input"] and state["http"] == [200]
    assert gate.context["control_revision"] == context_before["control_revision"] + 3 == meter.control_revision
    assert {k: v for k, v in gate.context.items() if k != "control_revision"} == {k: v for k, v in context_before.items() if k != "control_revision"}
    assert result["grants_permission"] is False and result["credential"]["has_credential"] is True
    assert state["secret"] not in json.dumps([result, gate.events, row])
    meter.guard()
    with pytest.raises(ValueError, match="duplicate_dispatch"):
        gate.execute(call)
    assert state["http"] == [200] and meter.snapshot()["lifetime"]["model_calls"] == 0


@pytest.mark.parametrize("mode", ["wrong_pending", "wrong_revision", "cancel", "steer_after_form", "revoke_after_form"])
def test_native_input_does_not_adopt_wrong_late_or_unrelated_control(input_port, mode):
    runtime, task, gate, meter, call, state = input_port
    before = meter.control_revision
    state["mode"] = mode
    with pytest.raises(AgentError):
        gate.execute(call)
    assert meter.control_revision == before
    with pytest.raises(AgentError):
        meter.guard()
    row = runtime.store.read()["tasks"][task["id"]]
    assert not row.get("reference_operations") and not row.get("configuration_operations")
    assert list(row["reference_source_inputs"].values())[0]["state"] == "waiting_input"
    assert row["status"] == "cancelled" if mode == "cancel" else True
    assert state["secret"] not in json.dumps(gate.events)


@pytest.mark.parametrize("change", ["goal", "owner", "field_type"])
def test_invalid_native_input_dispatches_no_owner_form(input_port, change):
    _, _, gate, _, call, state = input_port
    if change == "goal":
        call["arguments"]["goal_revision"] = 2
    elif change == "owner":
        gate.context["owner_id"] = "wrong-owner"
    else:
        call["arguments"]["fields"][0]["type"] = "text"
    try:
        result = gate.execute(call)
        assert result["not_executed"] is True
    except AgentError:
        pass
    assert state["http"] == []
