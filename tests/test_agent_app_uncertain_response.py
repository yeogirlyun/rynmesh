"""Real AppStore commits must stay uncertain until their original receipt is read."""
from __future__ import annotations

import copy
import json
import uuid

import pytest
from test_agent_business_plugin_handoff import BusinessModel
from test_agent_business_plugin_handoff import apps as apps
from test_agent_integrations import node as node
from test_agent_runtime import Catalog, advance, approve, assistant

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.node_apps.agent import ACTION
from rynmesh.node_apps.engine import AppError
from rynmesh.node_apps.plugin_agent import ALL_INFO
from rynmesh.node_apps.service import NodeApps, fingerprint
from rynmesh.node_apps.store import AppStore


def setup_runtime(apps, tmp_path, model=None):
    apps.manager.install("desk")
    model = model or BusinessModel("desk", "create_ticket")
    runtime = AgentRuntime(AgentStore(tmp_path / "agent", apps.key), model, apps.tools, Catalog(),
                           consumer_node_id=apps.peer_id)
    runtime.update_settings({"enabled": True, "model_id": "local:test", "permission_mode": "automatic",
                             "allowed_write_tools": [ACTION], "desktop_access": False})
    row = runtime.create({"message": "Create exactly one Desk ticket through the reviewed operation."})
    advance(runtime, 20)
    assert runtime.get(row["id"])["status"] == "waiting_approval"
    return runtime, model, row["id"]


def tool_result(messages, call_id):
    return json.loads(next(message["content"] for message in messages if message.get("tool_call_id") == call_id))


@pytest.mark.parametrize("error", ["apps_response_unconfirmed", "apps_response_invalid", "apps_store_unavailable"])
def test_postcommit_app_response_error_preserves_unknown_and_recovers_original_receipt_once(apps, tmp_path, monkeypatch, error):
    runtime, model, task_id = setup_runtime(apps, tmp_path)
    original_call = apps.service.call
    operations, committed = [], []

    def lose_after_real_commit(peer, operation, value):
        operations.append((operation, copy.deepcopy(value)))
        result = original_call(peer, operation, value)
        if operation == "invoke" and value["action"] == "create_ticket":
            committed.append(copy.deepcopy(result))
            assert result["operation_id"] == value["operation_id"]
            assert result["result"]["record"]["name"] == "Created through discovered Ryn tools"
            if len(committed) == 1:
                raise AppError(error)  # Only after the real encrypted provider commit.
        return result

    monkeypatch.setattr(apps.service, "call", lose_after_real_commit)
    approve(runtime, task_id)
    runtime.run_once()
    stored = original_call("", "view", {"app": "desk"})
    assert len(stored["records"]["tickets"]) == 1 and len(committed) == 1
    saved = runtime.store.read()["tasks"][task_id]
    operation_id = committed[0]["operation_id"]
    assert apps.service.pending()[0]["operation_id"] == operation_id
    assert saved["status"] == "interrupted" and saved["error"] == "agent_write_outcome_unknown"
    assert saved["in_flight"]["operation_id"] == operation_id and saved["in_flight"]["dispatched"]
    receipt = next(receipt for receipt in saved["receipts"] if receipt["operation_id"] == operation_id)
    assert receipt["status"] == "unknown"
    assert not receipt.get("result", {}).get("not_executed")
    assert not any(message.get("tool_call_id") == "business-action" for message in saved["messages"])
    assert saved["reconciliation"]["state"] == "pending"
    intent = apps.service.store.read()["intents"][operation_id]
    command = intent["command"]
    # Neither a missing response nor a forged binding closes the live intent.
    for bad_receipt in (None, {**committed[0], "request_hash": "forged"},
                        {**committed[0], "operation_id": "different-operation"},
                        {**committed[0], "consumer_peer": "different-consumer"}):
        real_call = apps.service.call
        with monkeypatch.context() as patch:
            patch.setattr(apps.service, "call", lambda peer, operation, value, bad=bad_receipt, call=real_call:
                copy.deepcopy(bad) if operation == "receipt" else call(peer, operation, value))
            assert apps.tools.node_apps.reconcile(operation_id, {"operation": receipt}, lambda: False) == {"outcome": "unknown"}
        assert apps.service.store.read()["intents"][operation_id] == intent
    changed_command = {**copy.deepcopy(command), "arguments": {"name": "Tampered pending command"}}
    changed_receipt = {**committed[0], "request_hash": fingerprint(changed_command),
                       "arguments_hash": fingerprint(changed_command["arguments"])}
    with pytest.raises(AppError, match="apps_operation_changed"):
        apps.service.confirm_receipt("", changed_command, changed_receipt)
    assert apps.service.store.read()["intents"][operation_id] == intent
    orphan_command = {**command, "operation_id": "no-existing-intent"}
    orphan_receipt = {**committed[0], "operation_id": orphan_command["operation_id"],
                      "request_hash": fingerprint(orphan_command)}
    with pytest.raises(AppError, match="apps_operation_unavailable"):
        apps.service.confirm_receipt("", orphan_command, orphan_receipt)
    assert "no-existing-intent" not in apps.service.store.read()["intents"]
    before_queries = len(operations)
    runtime.run_once()  # Reconciliation queries evidence, never another invoke.
    assert operations[before_queries:] == [("receipt", {"app": "desk", "operation_id": operation_id})]
    recovered = runtime.store.read()["tasks"][task_id]
    assert recovered["in_flight"] is None and recovered["queue"] == [] and recovered["status"] == "queued"
    receipt = next(receipt for receipt in recovered["receipts"] if receipt["operation_id"] == operation_id)
    assert receipt["status"] == "confirmed" and receipt["result"] == committed[0]
    assert tool_result(recovered["messages"], "business-action") == committed[0]
    assert apps.service.pending() == []
    assert apps.service.store.read()["intents"][operation_id]["status"] == "committed"
    runtime.run_once()
    runtime.run_once()
    done = runtime.store.read()["tasks"][task_id]
    assert done["status"] == "completed" and model.receipt == committed[0]
    assert len(committed) == 1 and len(original_call("", "view", {"app": "desk"})["records"]["tickets"]) == 1
    assert len([message for message in done["messages"] if message.get("tool_call_id") == "business-action"]) == 1
    assert len([receipt for receipt in done["receipts"] if receipt["tool"] == ACTION]) == 1
    # Reload the encrypted app store, query the same historical receipt, and
    # settle it twice. No invocation is needed, and committed data cannot change.
    reloaded = NodeApps(AppStore(apps.service.store.path.parent.parent, apps.key), apps.peer_id,
                        lambda: None, plugins=apps.manager)
    queried = reloaded.call("", "receipt", {"app": "desk", "operation_id": operation_id})
    before_bytes = reloaded.store.path.read_bytes()
    assert reloaded.confirm_receipt("", command, queried) == committed[0]
    assert reloaded.confirm_receipt("", command, queried) == committed[0]
    assert reloaded.store.path.read_bytes() == before_bytes
    different_result = {**queried, "result": {"record": {"name": "Forged replacement"}}}
    with pytest.raises(AppError, match="apps_operation_changed"):
        reloaded.confirm_receipt("", command, different_result)
    assert reloaded.store.read()["intents"][operation_id]["result"] == committed[0]
    # A fresh, distinct command now follows normal review without pending_required.
    args = {"name": "Distinct reviewed command after recovery"}
    fresh = reloaded.preview("", "desk", "create_ticket", args)
    next_result = reloaded.invoke("", {"app": "desk", "action": "create_ticket", "arguments": args,
        "operation_id": "new_after_" + uuid.uuid4().hex, "preview_hash": fresh["preview_hash"]})
    assert next_result["result"]["record"]["name"] == args["name"] and reloaded.pending() == []
    tickets = reloaded.call("", "view", {"app": "desk"})["records"]["tickets"]
    assert len(tickets) == 2 and sum(ticket["name"] == "Created through discovered Ryn tools" for ticket in tickets) == 1


class CorrectedPreconditionModel(BusinessModel):
    def complete(self, model_id, messages, specs, cancelled, on_event):
        step = len(self.calls)
        if step < 5:
            return super().complete(model_id, messages, specs, cancelled, on_event)
        self.calls.append((model_id, copy.deepcopy(messages), copy.deepcopy(specs)))
        if step == 5:
            feedback = tool_result(messages, "business-action")
            assert feedback["error"] == "apps_review_changed" and feedback["not_executed"] is True
            return assistant(name=ALL_INFO, args={"app": "desk"}, call_id="fresh-info")
        if step == 6:
            info = tool_result(messages, "fresh-info")
            return assistant(name=ACTION, args={"app": "desk", "action": "create_ticket",
                "arguments": {"name": "Created through discovered Ryn tools"},
                "reviewed_context": info["workspace_context"]}, call_id="corrected-action")
        assert step == 7
        self.receipt = tool_result(messages, "corrected-action")
        return assistant("The corrected reviewed creation completed once.")


def test_known_review_precondition_rejection_is_not_executed_and_can_be_corrected(apps, tmp_path, monkeypatch):
    runtime, model, task_id = setup_runtime(apps, tmp_path, CorrectedPreconditionModel("desk", "create_ticket"))
    original_call, changed = apps.service.call, []

    def intervene_before_provider_precondition(peer, operation, value):
        if operation == "invoke" and value["action"] == "create_ticket" and not changed:
            # Another actual provider commit invalidates the approved revision.
            # Bypass only the consumer intent queue, as another client would.
            args = {"name": "Independent intervening provider ticket"}
            preview = apps.service.preview("", "desk", "create_ticket", args)
            original_call("", "invoke", {"app": "desk", "action": "create_ticket", "arguments": args,
                "operation_id": "intervening_" + uuid.uuid4().hex, "preview_hash": preview["preview_hash"]})
            changed.append(True)
        return original_call(peer, operation, value)

    monkeypatch.setattr(apps.service, "call", intervene_before_provider_precondition)
    approve(runtime, task_id)
    runtime.run_once()
    rejected = runtime.store.read()["tasks"][task_id]
    assert rejected["status"] == "queued" and rejected["in_flight"] is None
    result = tool_result(rejected["messages"], "business-action")
    assert result["error"] == "apps_review_changed" and result["not_executed"] is True and result["no_mutation"] is True
    assert original_call("", "view", {"app": "desk"})["records"]["tickets"][0]["name"] == "Independent intervening provider ticket"
    advance(runtime, 8)
    assert runtime.get(task_id)["status"] == "waiting_approval"
    approve(runtime, task_id)
    advance(runtime, 3)
    done = runtime.store.read()["tasks"][task_id]
    assert done["status"] == "completed" and model.receipt["action"] == "create_ticket"
    tickets = original_call("", "view", {"app": "desk"})["records"]["tickets"]
    assert len(tickets) == 2
    assert len([ticket for ticket in tickets if ticket["name"] == "Created through discovered Ryn tools"]) == 1
    receipts = [receipt for receipt in done["receipts"] if receipt["tool"] == ACTION]
    assert [receipt["status"] for receipt in receipts] == ["not_executed", "confirmed"]
    rejected_id = receipts[0]["operation_id"]
    rejected_intent = apps.service.store.read()["intents"][rejected_id]
    assert rejected_intent["status"] == "rejected"
    forged = {**model.receipt, "operation_id": rejected_id,
              "request_hash": fingerprint(rejected_intent["command"])}
    with pytest.raises(AppError, match="apps_operation_rejected"):
        apps.service.confirm_receipt("", rejected_intent["command"], forged)
    assert apps.service.store.read()["intents"][rejected_id] == rejected_intent
