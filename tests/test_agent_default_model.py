"""Real encrypted configuration transactions, approvals and recovery; fake inference."""
import copy
import json
from types import SimpleNamespace

import pytest
from agent_tool_fixtures import expose_on_create
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime.default_model import NAME
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.agent_runtime.tools import RynTools


def setup(tmp_path):
    runtime, model, _, key = make_runtime(tmp_path)
    rows = [{"id": identifier, "name": identifier, "source": "local_api", "source_id": identifier,
             "peer_id": "self", "destination_revision": identifier + "-v1", "available": True,
             "agent_supported": True} for identifier in ("local:test", "local:other")]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_destination_binding = True
    app = SimpleNamespace(state=SimpleNamespace(agent_runtime=runtime))
    tools = RynTools(app, SimpleNamespace(home=tmp_path, peer_id="self"), {}, runtime.catalog)
    runtime.tools = tools
    expose_on_create(runtime, [NAME, "ryn_connect_local_cli_source", "ryn_test_local_cli_source"])
    return runtime, model, tools, key, rows


def waiting(runtime, model):
    args = {"model_id": "local:other", "expected_settings_revision": runtime.configuration_snapshot()["settings_revision"]}
    model.responses.append(assistant(name=NAME, args=args))
    task = runtime.create({"message": "Set the new-task default to local:other; leave this task and all other settings unchanged."})
    advance(runtime, 2)
    pending = runtime.get(task["id"])
    assert pending["status"] == "waiting_approval"
    return pending, args


def test_approved_default_only_changes_future_tasks_and_has_current_readback(tmp_path):
    runtime, model, tools, _, _ = setup(tmp_path)
    original = runtime.settings()
    task, args = waiting(runtime, model)
    assert runtime.settings() == original
    preview = task["pending_approval"]["preview"]
    assert preview["before"]["model_id"] == "local:test" and preview["after"]["model_id"] == "local:other"
    assert preview["applies_to"] == "future_tasks_only"
    approve(runtime, task["id"])
    advance(runtime)
    raw = runtime.store.read()["tasks"][task["id"]]
    assert raw["model_id"] == "local:test" and raw.get("context_epoch", 0) == 0
    assert runtime.settings() == {**original, "model_id": "local:other"}
    receipt = raw["receipts"][-1]
    criteria = [{"verifier": "default_agent_model_state"}]
    result = tools.verify(receipt, criteria, context=runtime._execution_context(raw), cancelled=lambda: False)
    assert result["status"] == "passed"
    created = runtime.create({"message": "Future task"})
    assert created["model_id"] == "local:other"
    assert runtime.store.read()["tasks"][created["id"]]["model_destination_confirmed"] is True
    assert runtime.configuration_snapshot()["settings_revision"] == args["expected_settings_revision"] + 1


@pytest.mark.parametrize("interference", ["owner_change", "owner_aba", "destination", "unavailable", "permission"])
def test_stale_approval_cannot_override_owner_or_changed_destination(tmp_path, interference):
    runtime, model, _, _, rows = setup(tmp_path)
    task, _ = waiting(runtime, model)
    if interference == "owner_change":
        runtime.update_settings({"max_steps": 20})
    elif interference == "owner_aba":
        runtime.update_settings({"model_id": "local:other"})
        runtime.update_settings({"model_id": "local:test"})
    elif interference == "destination":
        rows[1]["destination_revision"] = "replacement"
    elif interference == "unavailable":
        rows[1]["available"] = False
    else:
        runtime.update_settings({"permission_mode": "read_only"})
    if interference != "permission":
        approve(runtime, task["id"])
    advance(runtime)
    raw = runtime.store.read()["tasks"][task["id"]]
    assert runtime.settings()["model_id"] == "local:test"
    assert not raw.get("configuration_operations")


def test_atomic_receipt_survives_missing_task_ack_and_restart_without_second_change(tmp_path):
    runtime, model, tools, key, _ = setup(tmp_path)
    task, _ = waiting(runtime, model)
    approve(runtime, task["id"])
    execute = tools.execute

    def lose_ack(name, args, **kwargs):
        execute(name, args, **kwargs)
        raise RuntimeError("After atomic business commit, before task receipt")

    tools.execute = lose_ack
    # Leave ambiguous work for a new process instance instead of automatic same-process reconciliation.
    tools.reconcile = lambda *args, **kwargs: {"outcome": "unknown"}
    advance(runtime)
    failed = runtime.get(task["id"])
    assert failed["outcome"]["status"] == "unknown"
    revision = runtime.configuration_snapshot()["settings_revision"]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    tools.app.state.agent_runtime = restarted
    tools.execute = execute
    del tools.reconcile
    restarted.reconcile()
    # Restart schedules read-only receipt reconciliation, never a second write.
    advance(restarted)
    recovered = restarted.get(task["id"])
    assert recovered["reconciliation"]["state"] == "executed"
    assert restarted.configuration_snapshot()["settings_revision"] == revision
    assert len([e for e in recovered["events"] if e["type"] == "tool_started" and e.get("tool") == NAME]) == 1
    assert len(restarted.store.read()["tasks"][task["id"]]["configuration_operations"]) == 1


@pytest.mark.parametrize("model_choice", [{}, {"model_id": "local:other"}])
def test_default_binding_stops_future_task_after_source_retarget(tmp_path, model_choice):
    runtime, model, _, _, rows = setup(tmp_path)
    task, _ = waiting(runtime, model)
    approve(runtime, task["id"])
    advance(runtime)
    rows[1]["destination_revision"] = "replacement-provider"
    with pytest.raises(AgentError, match="destination_changed"):
        runtime.create({"message": "Do not send this to a substituted source", **model_choice})
    assert len(runtime.store.read()["tasks"]) == 1


def test_tool_cannot_mutate_settings_without_a_runtime_dispatch_capability(tmp_path):
    runtime, model, tools, _, _ = setup(tmp_path)
    task, args = waiting(runtime, model)
    raw = runtime.store.read()["tasks"][task["id"]]
    context = runtime._execution_context(raw)
    with pytest.raises(AgentToolRejected, match="authority_changed"):
        tools.execute(NAME, args, operation_id="forged", context=context,
                      expected_preview=task["pending_approval"]["preview"], cancelled=lambda: False)
    assert runtime.settings()["model_id"] == "local:test"
    assert "configuration_operations" not in json.dumps(runtime.get(task["id"]))


@pytest.mark.parametrize("interference", ["settings", "pause"])
def test_owner_update_between_preview_and_atomic_commit_wins(tmp_path, interference):
    runtime, model, tools, _, _ = setup(tmp_path)
    task, _ = waiting(runtime, model)
    approve(runtime, task["id"])
    preview = tools.default_model.preview
    called = 0
    def raced(args, context):
        nonlocal called
        result = preview(args, context)
        called += 1
        if called == 2:  # Inside execute, after the runtime's dispatch marker.
            if interference == "settings":
                runtime.update_settings({"max_steps": 21})
            else:
                runtime.action(task["id"], {"action": "pause"})
        return result
    tools.default_model.preview = raced
    advance(runtime)
    assert called == 2
    assert runtime.settings()["model_id"] == "local:test"
    assert not runtime.store.read()["tasks"][task["id"]].get("configuration_operations")


def test_real_worker_finishes_only_after_default_readback_and_separate_checker(tmp_path):
    runtime, model, _, _, _ = setup(tmp_path)
    runtime.update_settings({"verify_text_responses": True})
    task, _ = waiting(runtime, model)
    approve(runtime, task["id"])
    advance(runtime)
    model.responses.append(assistant("The default was changed for future tasks only."))
    advance(runtime, 2)  # Candidate answer then mechanical readback.
    pending = runtime.get(task["id"])
    assert pending["outcome"]["status"] == "unverified"
    assert pending["verification_evidence"][0]["evidence"]["current_settings_match"]
    refs = [e["id"] for e in pending["verification_evidence"]]
    model.responses.append(assistant(json.dumps({"status": "passed", "summary": "Synthetic checker",
        "uncovered_requirements": [], "evidence_refs": refs})))
    advance(runtime)
    assert runtime.get(task["id"])["outcome"]["status"] == "verified"
    assert all(call[0] == "local:test" for call in model.calls)


@pytest.mark.parametrize("source", ["peer", "local_service"])
def test_unverified_destination_cannot_become_agent_selected_default(tmp_path, source):
    runtime, model, _, _, rows = setup(tmp_path)
    rows[1]["source"] = source
    model.responses.append(assistant(name=NAME, args={"model_id": "local:other", "expected_settings_revision": 1}))
    task = runtime.create({"message": "Select the new default"})
    advance(runtime, 2)
    assert runtime.get(task["id"])["pending_approval"] is None
    assert runtime.settings()["model_id"] == "local:test"


def test_default_task_create_rejects_a_configuration_race(tmp_path):
    runtime, model, _, _, rows = setup(tmp_path)
    task, _ = waiting(runtime, model)
    approve(runtime, task["id"])
    advance(runtime)
    def raced_catalog():
        runtime.update_settings({"max_steps": 19})
        return copy.deepcopy(rows)
    runtime.catalog.list_models = raced_catalog
    with pytest.raises(AgentError, match="settings_revision_conflict"):
        runtime.create({"message": "New task using saved default", "model_id": "local:other"})
    assert len(runtime.store.read()["tasks"]) == 1


def test_atomic_receipt_cannot_prove_a_different_operation_or_other_task(tmp_path):
    runtime, model, tools, _, _ = setup(tmp_path)
    task, _ = waiting(runtime, model)
    approve(runtime, task["id"])
    advance(runtime)
    raw = runtime.store.read()["tasks"][task["id"]]
    receipt = copy.deepcopy(raw["receipts"][-1])
    context = {**runtime._execution_context(raw), "operation": receipt}
    assert tools.reconcile(receipt["operation_id"], context=context, cancelled=lambda: False)["outcome"] == "executed"
    receipt["arguments"]["model_id"] = "forged"
    assert tools.reconcile(receipt["operation_id"], context=context, cancelled=lambda: False)["outcome"] == "unknown"
    receipt["arguments"]["model_id"] = "local:other"
    context["owner_id"] = "other-owner"
    assert tools.reconcile(receipt["operation_id"], context=context, cancelled=lambda: False)["outcome"] == "unknown"
