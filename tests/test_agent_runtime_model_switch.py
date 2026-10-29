"""Local transactional model switches; no provider or network calls."""
import copy
import json
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_resources import resources  # noqa: F401
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.background_workers import BackgroundWorkerRegistry


def setup(tmp_path, *responses):
    runtime, model, tools, key = make_runtime(tmp_path, *responses)
    rows = [{"id": identifier, "name": "Same model name", "source": "local_api", "source_name": identifier,
             "source_id": identifier, "peer_id": "self", "destination_revision": identifier + "-v1",
             "available": True, "agent_supported": True} for identifier in ("local:test", "local:other")]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_destination_binding = True
    row = runtime.create({"message": "Inspect service without changing it"})
    runtime.action(row["id"], {"action": "pause"})
    return runtime, model, tools, key, row["id"], rows


def body(runtime, task_id, model_id="local:other", **extra):
    preview = runtime.preview_model_switch(task_id, model_id)
    return {"model_id": model_id, "expected_control_revision": preview["control_revision"],
            "expected_scope_revision": preview["scope_revision"], "expected_destination": preview["destination"],
            "confirm_data_destination": True, "transfer_resource_scope": False,
            "client_request_id": str(uuid.uuid4()), **extra}


def scope_fixture(runtime, task_id):
    def add(data):
        row = data["tasks"][task_id]
        row["scope"] = {"resource_grants": [{"id": "old", "generation": "1"}],
                        "egress_grants": [{"destination": copy.deepcopy(row["model_destination"])}],
                        "selection": {"resources": [{"connection_id": "docs", "root_path": "allowed", "actions": ["list", "read_ai"]}], "allow_model_data": True}}
        row["messages"].extend([{"role": "assistant", "content": "DERIVED-PRIVATE", "context_epoch": 0},
                                {"role": "tool", "content": "SOURCE-PRIVATE", "tool_call_id": "old-read", "context_epoch": 0,
                                 "resource_origin": {"private": "old-origin"}}])
        row["context_checkpoint"] = {"through": 2, "content": "SUMMARY-PRIVATE"}
        row["plan"]["steps"] = [{"text": "PLAN-PRIVATE"}]
    runtime.store.transaction(add)
    runtime.tools.validate_context = lambda context, **kwargs: None
    prepared = []
    def prepare(selection, *, owner_id, consumer_node_id, model_destination):
        prepared.append(copy.deepcopy(selection))
        return {"resource_grants": [{"id": "new", "generation": "1"}],
                "egress_grants": [{"destination": copy.deepcopy(model_destination)}]}
    runtime.tools.prepare_scope = prepare
    return prepared


def test_switch_retains_goal_budget_audit_but_closes_old_projection_and_does_not_run(tmp_path):
    runtime, model, tools, key, task_id, _ = setup(tmp_path)
    scope_fixture(runtime, task_id)
    before = runtime.store.read()["tasks"][task_id]
    request = body(runtime, task_id)
    changed = runtime.switch_model(task_id, request)
    assert changed["model_id"] == "local:other" and changed["status"] == "paused"
    assert changed["goal"] == before["goal"] and changed["budget"] == runtime.get(task_id)["budget"]
    stored = runtime.store.read()["tasks"][task_id]
    assert stored["budget"] == before["budget"] and stored["control_revision"] == before["control_revision"] + 1
    assert stored["scope"]["resource_grants"] == stored["scope"]["egress_grants"] == []
    assert stored["scope"]["selection"]["resources"] == before["scope"]["selection"]["resources"]
    assert stored["scope"]["selection"]["allow_model_data"] is False
    assert stored["plan_history"][-1] == before["plan"] and not stored["plan"]["steps"]
    projected = json.dumps(runtime._context(stored))
    assert all(text not in projected for text in ("SOURCE-PRIVATE", "DERIVED-PRIVATE", "SUMMARY-PRIVATE", "PLAN-PRIVATE"))
    assert "SOURCE-PRIVATE" in json.dumps(stored)
    assert runtime._execution_context(stored)["resource_origins"] == []
    advance(runtime, 2)
    assert not model.calls and not tools.calls
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    assert restarted.switch_model(task_id, request) == restarted.get(task_id)
    assert restarted.get(task_id)["control_revision"] == changed["control_revision"]
    with pytest.raises(AgentError, match="agent_request_conflict"):
        restarted.switch_model(task_id, {**request, "transfer_resource_scope": True})


def test_pending_approval_is_closed_and_old_approved_capability_cannot_execute(tmp_path):
    runtime, model, tools, _, task_id, _ = setup(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime, 2)
    old = runtime.get(task_id)["pending_approval"]
    changed = runtime.switch_model(task_id, body(runtime, task_id))
    assert changed["pending_approval"] is None
    result = json.loads(changed["messages"][-1]["content"])
    assert result["not_executed"] and result["error"] == "agent_model_changed"
    with pytest.raises(AgentError, match="agent_approval_mismatch"):
        runtime.action(task_id, {"action": "approve", "approval_id": old["id"], "expected_control_revision": changed["control_revision"]})
    assert not tools.calls


def test_consumed_queued_approval_requires_pause_then_is_discarded(tmp_path):
    runtime, _, tools, _, task_id, _ = setup(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime, 2)
    request = body(runtime, task_id)
    approve(runtime, task_id)
    with pytest.raises(AgentError, match="agent_control_revision_conflict"):
        runtime.switch_model(task_id, request)
    runtime.action(task_id, {"action": "pause"})
    runtime.switch_model(task_id, body(runtime, task_id))
    stored = runtime.store.read()["tasks"][task_id]
    assert stored["approved"] is None and stored["queue"] == [] and not tools.calls


@pytest.mark.parametrize("kind", ["model", "tool", "unknown"])
def test_inflight_or_unknown_operation_never_switches(tmp_path, kind):
    runtime, _, _, _, task_id, _ = setup(tmp_path)
    request = body(runtime, task_id)
    def mark(data):
        row = data["tasks"][task_id]
        row["in_flight"] = {"kind": kind, "effect": "write" if kind == "unknown" else "read", "dispatched": True}
        if kind == "unknown":
            row.update(status="interrupted", error="agent_write_outcome_unknown")
    runtime.store.transaction(mark)
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        runtime.switch_model(task_id, request)
    assert runtime.get(task_id)["model_id"] == "local:test"
    assert runtime.get(task_id)["allowed_actions"]["model"]["enabled"] is False


def test_explicit_scope_transfer_revalidates_and_rebinds_destination(tmp_path):
    runtime, _, _, _, task_id, _ = setup(tmp_path)
    prepared = scope_fixture(runtime, task_id)
    request = body(runtime, task_id, transfer_resource_scope=True)
    runtime.switch_model(task_id, request)
    stored = runtime.store.read()["tasks"][task_id]
    assert prepared == [stored["scope"]["selection"]]
    assert stored["scope"]["resource_grants"][0]["id"] == "new"
    assert stored["scope"]["egress_grants"][0]["destination"] == request["expected_destination"]
    assert stored["status"] == "paused" and runtime._execution_context(stored)["resource_origins"] == []


def test_revoked_old_scope_does_not_gain_new_authority_by_transfer(tmp_path):
    runtime, _, _, _, task_id, _ = setup(tmp_path)
    prepared = scope_fixture(runtime, task_id)
    def rejected(*args, **kwargs):
        raise AgentError("agent_resource_permission_changed")
    runtime.tools.validate_context = rejected
    with pytest.raises(AgentError, match="agent_resource_permission_changed"):
        runtime.switch_model(task_id, body(runtime, task_id, transfer_resource_scope=True))
    assert not prepared and runtime.get(task_id)["model_id"] == "local:test"
    runtime.switch_model(task_id, body(runtime, task_id))  # Revocation itself remains possible.


def test_concurrent_owner_change_wins_over_prepared_scope_transfer(tmp_path):
    runtime, _, _, _, task_id, _ = setup(tmp_path)
    scope_fixture(runtime, task_id)
    entered, release = threading.Event(), threading.Event()
    original = runtime.tools.prepare_scope
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    runtime.tools.prepare_scope = delayed
    request = body(runtime, task_id, transfer_resource_scope=True)
    errors = []
    def switch():
        try:
            runtime.switch_model(task_id, request)
        except AgentError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=switch)
    thread.start()
    assert entered.wait(2)
    runtime.action(task_id, {"action": "cancel"})
    release.set()
    thread.join(3)
    assert errors == ["agent_control_revision_conflict"]
    assert runtime.get(task_id)["status"] == "cancelled" and runtime.get(task_id)["model_id"] == "local:test"


def test_same_name_stable_ids_exact_generation_and_no_fallback_for_unbound_models(tmp_path):
    runtime, _, _, _, task_id, rows = setup(tmp_path)
    request = body(runtime, task_id)
    rows[1]["destination_revision"] = "new-model-rule-version"
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime.switch_model(task_id, request)
    for source in ("peer", "local_service"):
        rows[1]["source"] = source
        with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
            runtime.preview_model_switch(task_id, "local:other")
    rows[1].update(source="local_api", available=False)
    with pytest.raises(AgentError, match="agent_model_unavailable"):
        runtime.preview_model_switch(task_id, "local:other")


def test_confirmed_destination_is_checked_even_after_scope_is_cleared(tmp_path):
    runtime, model, _, _, task_id, rows = setup(tmp_path)
    runtime.switch_model(task_id, body(runtime, task_id))
    rows[1]["destination_revision"] = "edited-endpoint-or-key"
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime)
    assert runtime.get(task_id)["error"] == "agent_model_destination_changed" and not model.calls


def test_real_resource_grants_transfer_exact_selection_then_revoke_blocks_transfer(tmp_path, resources):  # noqa: F811
    runtime, _, _, _, task_id, _ = setup(tmp_path / "agent")
    resource_tools, plugin, _, _ = resources
    runtime.tools = RynTools(resource_tools.app, SimpleNamespace(), {}, None)
    row = runtime.get(task_id)
    selection = {"resources": [{"connection_id": "docs", "root_path": "allowed", "actions": ["list", "read_ai"]}], "allow_model_data": True}
    runtime.update_scope(task_id, {"expected_control_revision": row["control_revision"], "expected_scope_revision": row["scope_revision"],
                                 "expected_destination_revision": "local:test-v1", "scope": selection})
    before = runtime.store.read()["tasks"][task_id]
    assert runtime.preview_model_switch(task_id, "local:other")["current_scope_authorized"]
    runtime.switch_model(task_id, body(runtime, task_id, transfer_resource_scope=True))
    after = runtime.store.read()["tasks"][task_id]
    assert after["scope"]["selection"] == selection
    old_grant, new_grant = before["scope"]["resource_grants"][0], after["scope"]["resource_grants"][0]
    assert old_grant["grant_id"] != new_grant["grant_id"]
    assert old_grant["connection_access_generation"] == new_grant["connection_access_generation"]
    runtime._validate_context(after)
    plugin.update_source("docs", {"allow_ai": False})
    assert not runtime.preview_model_switch(task_id, "local:test")["current_scope_authorized"]
    with pytest.raises(AgentError):
        runtime.switch_model(task_id, body(runtime, task_id, "local:test", transfer_resource_scope=True))
    assert runtime.get(task_id)["model_id"] == "local:other"


def test_http_model_switch_owner_guard_confirmation_and_exact_retry(tmp_path):
    initial, model, tools, key, _, rows = setup(tmp_path / "setup")
    app = FastAPI()
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    runtime = install_agent(app, store=object(), home=tmp_path / "http", workers=BackgroundWorkerRegistry(), local_control=guard,
                            messaging_key=key, integrations=(model, tools, initial.catalog))
    runtime.update_settings({"enabled": True, "model_id": rows[0]["id"]})
    task_id = runtime.create({"message": "Inspect only"})["id"]
    runtime.action(task_id, {"action": "pause"})
    path = f"/api/local/agent/tasks/{task_id}/model"
    with TestClient(app) as client:
        assert client.get(path, params={"model_id": rows[1]["id"]}).status_code == 401
        assert client.put(path, json={}).status_code == 401
        headers = {"x-owner": "yes"}
        preview = client.get(path, params={"model_id": rows[1]["id"]}, headers=headers)
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        request = body(runtime, task_id)
        assert client.put(path, json={**request, "confirm_data_destination": False}, headers=headers).status_code == 400
        result = client.put(path, json=request, headers=headers)
        assert result.status_code == 200 and result.json()["status"] == "paused"
        assert client.put(path, json=request, headers=headers).json() == result.json()
    assert not model.calls and not tools.calls


def test_new_model_cannot_repeat_confirmed_old_write_in_same_goal(tmp_path):
    runtime, model, tools, _, task_id, _ = setup(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("Done"))
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime, 2)
    approve(runtime, task_id)
    advance(runtime, 2)
    assert len(tools.calls) == 1
    runtime.switch_model(task_id, body(runtime, task_id))
    model.responses.append(assistant(name="share_service", args={"service_id": "A"}))
    original = model.complete
    model.complete = lambda *args, **kwargs: original(*args)  # Deterministic transport accepts bound destination.
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime, 2)
    row = runtime.get(task_id)
    assert len(tools.calls) == 1 and row["pending_approval"] is None
    assert json.loads(row["messages"][-1]["content"])["error"] == "agent_operation_already_confirmed"
