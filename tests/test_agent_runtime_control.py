"""T03 control races and T05 provider-history isolation at actual store boundaries."""
from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace

import pytest
from test_agent_resources import file_ref
from test_agent_resources import resources as resource_fixture
from test_agent_runtime import advance, approve, assistant, make_runtime
from test_agent_runtime_retry import failing_model

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.tools import RynTools


@pytest.fixture
def resources(tmp_path, monkeypatch):
    return resource_fixture.__wrapped__(tmp_path, monkeypatch)


def steer(runtime, task_id, message="Do not share; inspect only."):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": message, "intent": "steer", "expected_control_revision": row["control_revision"],
                                        "expected_goal_revision": row["goal"]["revision"], "client_request_id": str(uuid.uuid4())})


def control(runtime, task_id, action, **extra):
    row = runtime.get(task_id)
    return runtime.action(task_id, {"action": action, "expected_control_revision": row["control_revision"], **extra})


def set_scope(runtime, task_id, resources=None):
    row = runtime.get(task_id)
    return runtime.update_scope(task_id, {"scope": {"resources": resources or [], "allow_model_data": bool(resources)},
                                        "expected_control_revision": row["control_revision"], "expected_scope_revision": row["scope_revision"],
                                        "expected_destination_revision": runtime.get_scope(task_id)["current_model_destination"]["destination_revision"],
                                        "client_request_id": str(uuid.uuid4())})


def pairs(messages):
    calls = {call["id"] for message in messages for call in message.get("tool_calls", [])}
    results = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
    return calls, results


def test_stream_cursor_does_not_invalidate_pause_and_cancel_is_idempotent(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def stream(model_id, messages, specs, cancelled, on_event):
        for _ in range(10):
            on_event({"type": "text_delta", "text": "x" * 512})
        entered.set()
        assert release.wait(3)
        return assistant("Late text must not be published")
    model.complete = stream
    row = runtime.create({"message": "Inspect"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    current = runtime.get(row["id"])
    assert current["control_revision"] == row["control_revision"]
    assert current["event_cursor"] > row["event_cursor"]
    paused = runtime.action(row["id"], {"action": "pause", "expected_revision": 0})
    assert paused["run_state"] == "pausing"
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    paused = runtime.get(row["id"])
    assert paused["status"] == "paused" and paused["run_state"] == "paused"
    assert not any(m.get("content") == "Late text must not be published" for m in paused["messages"])
    control(runtime, row["id"], "resume")
    body = {"action": "cancel", "expected_control_revision": 0, "client_request_id": str(uuid.uuid4())}
    cancelled = runtime.action(row["id"], body)
    assert runtime.action(row["id"], body) == cancelled
    assert cancelled["status"] == "cancelled"


def test_steer_during_model_discards_late_tools_and_restarts_with_latest_goal(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(3)
        return assistant("Old model proposal", name="share_service", args={"service_id": "A"})
    failing_model(model, blocked, assistant("Inspection only"))
    row = runtime.create({"message": "Inspect then share A"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    changed = steer(runtime, row["id"])
    assert changed["goal"]["revision"] == 2 and changed["run_state"] == "pausing"
    release.set()
    worker.join(3)
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and not tools.calls
    assert "Do not share" in json.dumps(model.calls[-1][1])
    assert "Old model proposal" not in json.dumps(done["messages"])
    assert done["outcome"]["status"] == "unverified"


def test_steer_closes_old_approval_pairs_and_cannot_reuse_approval(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("Inspect only"))
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    old = runtime.get(row["id"])
    changed = steer(runtime, row["id"])
    assert changed["pending_approval"] is None
    assert pairs(changed["messages"])[0] == pairs(changed["messages"])[1]
    assert json.loads(changed["messages"][-2]["content"])["not_executed"] is True
    with pytest.raises(AgentError, match="agent_approval_mismatch"):
        control(runtime, row["id"], "approve", approval_id=old["pending_approval"]["id"])
    advance(runtime)
    assert not tools.shared and not tools.calls
    assert pairs(model.calls[-1][1])[0] == pairs(model.calls[-1][1])[1]


def test_steer_after_write_commit_keeps_real_receipt_before_new_owner_message(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("The earlier share already happened."))
    entered, release = threading.Event(), threading.Event()
    execute = tools.execute

    def committed(*args, **kwargs):
        result = execute(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    tools.execute = committed
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    steer(runtime, row["id"])
    release.set()
    worker.join(3)
    advance(runtime)
    done = runtime.get(row["id"])
    assert tools.shared and len(tools.calls) == 1
    assert pairs(done["messages"])[0] == pairs(done["messages"])[1]
    tool_index = next(i for i, m in enumerate(done["messages"]) if m["role"] == "tool")
    correction_index = next(i for i, m in enumerate(done["messages"]) if "Do not share" in m.get("content", ""))
    assert tool_index < correction_index
    assert json.loads(done["messages"][tool_index]["content"])["shared"] is True


def test_question_answer_is_bound_and_exact_replay_is_safe(tmp_path):
    runtime, _, _, key = make_runtime(tmp_path, assistant(name="ryn_request_input", args={"goal_revision": 1, "kind": "information", "prompt": "Which service?",
                                      "fields": [{"id": "service", "label": "Service", "type": "text", "required": True}]}), assistant("Got the information"))
    row = runtime.create({"message": "Inspect my chosen service"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    body = {"action": "answer", "pending_input_id": waiting["pending_input"]["id"], "answers": {"service": "A"},
            "expected_control_revision": waiting["control_revision"], "expected_goal_revision": 1, "client_request_id": str(uuid.uuid4())}
    with pytest.raises(AgentError, match="agent_control_revision_conflict"):
        runtime.action(row["id"], {**body, "expected_control_revision": 0})
    answered = runtime.action(row["id"], body)
    assert answered["pending_input"] is None and answered["status"] == "queued"
    assert answered["messages"][-1]["ui"] == {"kind": "input_answer", "prompt": "Which service?",
        "pending_input_id": waiting["pending_input"]["id"], "fields": [{"label": "Service", "value": "A"}]}
    runtime = AgentRuntime(AgentStore(tmp_path, key), runtime.model, runtime.tools, runtime.catalog)
    runtime.reconcile()
    assert runtime.action(row["id"], body) == runtime.get(row["id"])
    with pytest.raises(AgentError, match="agent_request_conflict"):
        runtime.action(row["id"], {**body, "answers": {"service": "B"}})
    assert len([m for m in runtime.get(row["id"])["messages"] if "Owner supplied information" in m.get("content", "")]) == 1
    assert runtime.store.read()["tasks"][row["id"]]["scope"]["resource_grants"] == []
    advance(runtime)
    assert all("ui" not in message for message in runtime.model.calls[-1][1])


def test_question_answer_cannot_accept_credential_text(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant(name="ryn_request_input", args={"goal_revision": 1, "kind": "credential_ref", "prompt": "Use secure form",
                                      "fields": [{"id": "credential", "label": "Secure reference", "type": "credential_ref", "required": True}]}))
    row = runtime.create({"message": "Configure source"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    assert not waiting["allowed_actions"]["answer"]["enabled"]
    with pytest.raises(AgentError, match="agent_secure_credential_form_required"):
        control(runtime, row["id"], "answer", pending_input_id=waiting["pending_input"]["id"], expected_goal_revision=1, answers={"credential": "secret-value"})
    assert "secret-value" not in json.dumps(runtime.store.read())


def test_mixed_credential_request_reports_correction_and_recovers(tmp_path):
    base = {"goal_revision": 1, "kind": "credential_ref", "prompt": "Configure Gemini"}
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="ryn_request_input", args={**base, "fields": [
            {"id": "models", "label": "Models", "type": "text", "required": True},
            {"id": "credential", "label": "Credential", "type": "credential_ref", "required": True}]}),
        assistant(name="ryn_request_input", args={**base, "fields": [
            {"id": "credential", "label": "Credential", "type": "credential_ref", "required": True}]}))
    row = runtime.create({"message": "Configure Gemini"})
    advance(runtime, 2)
    rejected = runtime.get(row["id"])
    receipt = json.loads(next(m["content"] for m in rejected["messages"] if m["role"] == "tool"))
    assert receipt["error"] == "agent_invalid_control_proposal"
    assert receipt["no_mutation"] is True and "ONLY credential_ref" in receipt["correction"]
    assert rejected["pending_input"] is None and not tools.calls
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_input"
    assert waiting["pending_input"]["kind"] == "credential_ref"
    assert not waiting["allowed_actions"]["answer"]["enabled"]
    assert "ONLY credential_ref" in json.dumps(model.calls[-1][1])


def resource_runtime(tmp_path, resources):
    resource_tools, plugin, _context, state = resources
    runtime, model, _, key = make_runtime(tmp_path)
    runtime.tools = RynTools(resource_tools.app, SimpleNamespace(), {}, None)
    catalog_row = {**runtime.catalog.list_models()[0], "source_id": "test-api", "destination_revision": "generation-1"}
    runtime.catalog.list_models = lambda: [catalog_row]
    model.supports_destination_binding = True
    model.verification_auto = True
    model.destination = runtime._destination(catalog_row)
    row = runtime.create({"message": "Inspect the permitted document"})
    set_scope(runtime, row["id"], [{"connection_id": "docs", "root_path": "allowed", "actions": ["list", "read_ai"]}])
    stored = runtime.store.read()["tasks"][row["id"]]
    ref = file_ref(resource_tools, runtime._execution_context(stored))
    return runtime, model, plugin, state, row, ref, key


def test_scope_to_empty_excludes_old_tool_assistant_plan_and_summary(tmp_path, resources):
    runtime, model, _, state, row, ref, _ = resource_runtime(tmp_path, resources)
    state["body"] = b"PRIVATE_DOCUMENT_847"
    failing_model(model, assistant(name="ryn_read_resource", args={"resource_ref": ref}), assistant("Derived PRIVATE_DOCUMENT_847"), assistant("No current document grant."))
    advance(runtime, 5)
    def checkpoint(data):
        from rynmesh.agent_runtime.checkpoint_refs import label
        task = data["tasks"][row["id"]]
        task["context_checkpoint"] = {"through": 2, "content": "Summary PRIVATE_DOCUMENT_847", "source_sha256": "irrelevant-test", "references": {label(ref): ref}}
        task["plan"]["steps"] = [{"id": "leak", "text": "Plan PRIVATE_DOCUMENT_847", "status": "pending"}]
    runtime.store.transaction(checkpoint)
    assert runtime._execution_context(runtime.store.read()["tasks"][row["id"]])["resource_origins"]
    set_scope(runtime, row["id"])
    control(runtime, row["id"], "resume")
    advance(runtime)
    assert "PRIVATE_DOCUMENT_847" not in json.dumps(model.calls[-1][1])
    assert ref not in json.dumps(model.calls[-1][1])
    stored = runtime.store.read()["tasks"][row["id"]]
    assert "PRIVATE_DOCUMENT_847" in json.dumps(stored["messages"])
    assert runtime._execution_context(stored)["resource_origins"] == []
    assert "context_checkpoint" not in stored


def test_underlying_permission_revocation_stops_next_model_request(tmp_path, resources):
    runtime, model, plugin, _, row, ref, _ = resource_runtime(tmp_path, resources)
    failing_model(model, assistant(name="ryn_read_resource", args={"resource_ref": ref}))
    advance(runtime, 2)
    def pin_reference(data):
        from rynmesh.agent_runtime.checkpoint_refs import label
        task = data["tasks"][row["id"]]
        task["context_checkpoint"] = {"through": len(task["messages"]), "content": "Read evidence retained.",
                                      "references": {label(ref): ref}, "source_sha256": "fixture"}
    runtime.store.transaction(pin_reference)
    plugin.update_source("docs", {"allow_ai": False})
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "failed" and done["error"] == "agent_resource_ai_not_allowed"
    assert len(model.calls) == 1


def test_scope_changed_during_read_cannot_reemit_late_body(tmp_path, resources):
    runtime, model, _, state, row, ref, _ = resource_runtime(tmp_path, resources)
    state["body"] = b"LATE_RESOURCE_581"
    failing_model(model, assistant(name="ryn_read_resource", args={"resource_ref": ref}), assistant("No permission now"))
    advance(runtime)
    entered, release = threading.Event(), threading.Event()
    execute = runtime.tools.execute

    def delayed(*args, **kwargs):
        result = execute(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    runtime.tools.execute = delayed
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    set_scope(runtime, row["id"])
    release.set()
    worker.join(3)
    advance(runtime)
    assert "LATE_RESOURCE_581" not in json.dumps(model.calls[-1][1])
    assert "LATE_RESOURCE_581" in json.dumps(runtime.store.read()["tasks"][row["id"]]["messages"])


def test_unknown_write_blocks_scope_and_new_messages(tmp_path, resources):
    runtime, _, _, _, row, _, _ = resource_runtime(tmp_path, resources)
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(status="interrupted", error="agent_write_outcome_unknown",
        in_flight={"kind": "tool", "effect": "write", "dispatched": True, "operation_id": "unknown-write"}))
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        set_scope(runtime, row["id"])
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        steer(runtime, row["id"])
    assert not runtime.get(row["id"])["allowed_actions"]["resume"]["enabled"]


def test_create_scope_is_atomic_version_bound_and_idempotent(tmp_path, resources):
    runtime, model, _, _, existing, _, _ = resource_runtime(tmp_path, resources)
    control(runtime, existing["id"], "cancel")
    selection = {"resources": [{"connection_id": "docs", "root_path": "allowed", "actions": ["list", "read_ai"]}], "allow_model_data": True}
    body = {"message": "Read permitted material", "scope": selection, "expected_destination_revision": "generation-1", "client_request_id": str(uuid.uuid4())}
    count = len(runtime.list_tasks())
    for invalid in ({**body, "expected_destination_revision": "old"}, {**body, "scope": {**selection, "allow_model_data": False}}):
        with pytest.raises(AgentError):
            runtime.create(invalid)
        assert len(runtime.list_tasks()) == count
    created = runtime.create(body)
    created_scope = runtime.get_scope(created["id"])["scope"]
    assert created["status"] == "queued" and created_scope["resource_grants"]
    assert created_scope["selection"] == selection
    assert created["model_destination"] == model.destination
    assert runtime.create(body)["id"] == created["id"]
    assert len(runtime.list_tasks()) == count + 1
    with pytest.raises(AgentError, match="agent_request_conflict"):
        runtime.create({**body, "scope": {"resources": [], "allow_model_data": False}})
    assert runtime.scope_catalog()["create_scope_supported"] is True
    scope = runtime.get_scope(created["id"])
    assert scope["current_model_destination"] == scope["model_destination"] == model.destination
    failing_model(model, assistant("Authorized first request"))
    advance(runtime)
    assert len(model.calls) == 1


def test_source_edit_revalidated_same_id_cannot_receive_old_history(tmp_path, resources):
    runtime, model, _, state, row, ref, _ = resource_runtime(tmp_path, resources)
    state["body"] = b"OLD_RECEIVER_ONLY_291"
    failing_model(model, assistant(name="ryn_read_resource", args={"resource_ref": ref}), assistant("Derived OLD_RECEIVER_ONLY_291"))
    advance(runtime, 5)
    current_model = runtime.catalog.list_models()[0]
    current_model["destination_revision"] = "generation-2"
    # Capability was revalidated under the same stable catalog/source identity.
    assert current_model["available"] and current_model["agent_supported"]
    runtime.add_message(row["id"], {"message": "Continue analysis", "expected_revision": runtime.get(row["id"])["revision"]})
    advance(runtime)
    rejected = runtime.get(row["id"])
    assert rejected["status"] == "failed" and rejected["error"] == "agent_model_destination_changed"
    assert len(model.calls) == 3
    scope = runtime.get_scope(row["id"])
    assert scope["model_destination"]["destination_revision"] == "generation-1"
    assert scope["current_model_destination"]["destination_revision"] == "generation-2"
    selection = scope["scope"]["selection"]
    body = {"scope": selection, "expected_control_revision": rejected["control_revision"], "expected_scope_revision": rejected["scope_revision"],
            "expected_destination_revision": "generation-1"}
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime.update_scope(row["id"], body)
    runtime.update_scope(row["id"], {**body, "expected_destination_revision": "generation-2"})
    model.destination = runtime._destination(current_model)
    failing_model(model, assistant("New receiver gets no old document"))
    control(runtime, row["id"], "resume")
    advance(runtime)
    assert "OLD_RECEIVER_ONLY_291" not in json.dumps(model.calls[-1][1])


def test_unbound_bridge_cannot_accept_resource_scope(tmp_path, resources):
    runtime, model, _, _, row, _, _ = resource_runtime(tmp_path, resources)
    model.supports_destination_binding = False
    assert runtime.scope_catalog()["create_scope_supported"] is False
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
        set_scope(runtime, row["id"], runtime.get_scope(row["id"])["scope"]["selection"]["resources"])
    advance(runtime)
    assert runtime.get(row["id"])["error"] == "agent_model_destination_unverifiable"
    assert not model.calls
