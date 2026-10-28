import hashlib
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentToolRejected, encoded
from rynmesh.ai_access.store import AIAccessStore

node = integration_node  # Reuse the isolated real-service fixture.


def receipt(tool, arguments, operation_id="op-test"):
    return {"tool": tool, "operation_id": operation_id, "arguments": arguments,
            "arguments_sha256": hashlib.sha256(encoded(arguments)).hexdigest()}


def test_sharing_verifier_reads_current_target_without_claiming_operation_attribution(node):
    app, store = node
    record, model_id = add_source(app, models=["model-a", "model-b"])
    sources = app.state.llm_agent_services["source_store"]
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    operation = receipt("ryn_set_model_sharing", {"model_id": model_id, "enabled": True})
    criteria = [{"id": "criterion-1", "verifier": "model_sharing_state"}]
    assert tools.verify(operation, criteria, context={}, cancelled=lambda: False)["status"] == "failed"
    record["shared_models"] = ["model-a"]
    sources.save(record)  # Independent state change, not this operation.
    result = tools.verify(operation, criteria, context={}, cancelled=lambda: False)
    assert result["status"] == "passed" and result["evidence"]["current_state_matches"]
    assert result["evidence"]["operation_attribution"] == "not_established_by_state_readback"
    assert tools.reconcile("op-test", context={"operation": operation}, cancelled=lambda: False)["outcome"] == "unknown"
    assert sources.read()[record["id"]]["shared_models"] == ["model-a"]
    assert tools.verify(operation, [{"verifier": "all_goals_succeeded"}], context={}, cancelled=lambda: False)["status"] == "unknown"


def test_only_exact_completed_operation_can_reconcile_until_restart(node):
    app, store = node
    _record, model_id = add_source(app)
    store.personal_space.create("Verification")
    store.personal_space.set_policy("space")
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    arguments = {"model_id": model_id, "enabled": True}
    operation = receipt("ryn_set_model_sharing", arguments)
    result = tools.execute(operation["tool"], arguments, operation_id=operation["operation_id"], cancelled=lambda: False,
                           expected_preview=tools.preview(operation["tool"], arguments))
    reconciled = tools.reconcile("op-test", context={"operation": operation}, cancelled=lambda: False)
    assert reconciled["outcome"] == "executed" and reconciled["receipt"]["result"] == result
    assert tools.reconcile("op-test", context={"operation": {**operation, "arguments_sha256": "forged"}}, cancelled=lambda: False)["outcome"] == "unknown"
    assert tools.reconcile("different-op", context={"operation": operation}, cancelled=lambda: False)["outcome"] == "unknown"
    _model, restarted, _catalog = build_agent_integrations(app, store, store.home)
    assert restarted.reconcile("op-test", context={"operation": operation}, cancelled=lambda: False)["outcome"] == "unknown"


def test_friend_verifier_reads_exact_grant_and_keeps_relationship_evidence(node):
    app, store = node
    relation = {"relationship_id": "a" * 32, "peer_id": "friend", "node_name": "Friend", "status": "active"}
    permissions = AIAccessStore(store.home, relationship=lambda _: relation)
    app.state.ai_access = SimpleNamespace(store=permissions)
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    args = {"service_id": "friend-model", "relationship_id": relation["relationship_id"], "allowed": True, "expected_revision": 0}
    operation = receipt("ryn_set_friend_model_access", args)
    criteria = [{"verifier": "friend_model_access_state"}]
    assert tools.verify(operation, criteria, context={}, cancelled=lambda: False)["status"] == "failed"
    permissions.set("friend-model", relation["relationship_id"], allowed=True, expected_revision=0)
    verified = tools.verify(operation, criteria, context={}, cancelled=lambda: False)
    assert verified["status"] == "passed" and verified["evidence"]["revision"] == 1
    assert verified["evidence"]["relationship_active"] is True
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        tools.verify(operation, criteria, context={}, cancelled=lambda: True)
