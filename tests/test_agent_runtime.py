"""Real encrypted store and worker state machine, deterministic model boundary."""
from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.background_workers import BackgroundWorkerRegistry


def assistant(content="", name=None, args=None, call_id=None):
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{"id": call_id or "call_" + uuid.uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}]
    return {"message": message}


class Catalog:
    def list_models(self):
        return [{"id": "local:test", "name": "Test", "source": "local_api", "available": True, "agent_supported": True}]


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, model_id, messages, tool_specs, cancelled, on_event):
        self.calls.append((model_id, messages, tool_specs))
        return self.responses.pop(0)


class Tools:
    def __init__(self):
        self.calls = []
        self.shared = False

    def specs(self):
        return [{"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {"service_id": {"type": "string"}}, "required": ["service_id"], "additionalProperties": False}}} for name in ("inspect_service", "share_service")]

    def metadata(self, name):
        if name not in {"inspect_service", "share_service"}:
            raise ValueError("unavailable")
        return {"effect": "write" if name == "share_service" else "read", "title": name}

    def preview(self, name, arguments):
        return {"service_id": arguments["service_id"], "before": {"shared": self.shared}, "after": {"shared": True}}

    def execute(self, name, arguments, *, operation_id, cancelled):
        self.calls.append((name, arguments, operation_id))
        if name == "share_service":
            self.shared = True
        return {"service_id": arguments["service_id"], "shared": self.shared, "api_key": "never-expose-test-secret"}


def make_runtime(tmp_path, *responses):
    key = X25519PrivateKey.generate()
    model, tools = Model(*responses), Tools()
    runtime = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    return runtime, model, tools, key


def advance(runtime, count=1):
    for _ in range(count):
        runtime.run_once()


def instruction_text(messages):
    """Inspect instruction layers without treating contextual user data as authority."""
    return "\n".join(message["content"] for message in messages if message["role"] in {"system", "developer"})


def approve(runtime, task_id):
    row = runtime.get(task_id)
    return runtime.action(task_id, {"action": "approve", "expected_revision": row["revision"], "approval_id": row["pending_approval"]["id"]})


def test_encrypted_loop_inspect_approve_change_verify(tmp_path):
    runtime, model, tools, key = make_runtime(
        tmp_path,
        assistant(name="inspect_service", args={"service_id": "svc1"}),
        assistant(name="share_service", args={"service_id": "svc1"}),
        assistant(name="inspect_service", args={"service_id": "svc1"}),
        assistant("Verified svc1 is shared."),
    )
    row = runtime.create({"message": "Inspect and share svc1"})
    advance(runtime, 4)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_approval"
    assert waiting["pending_approval"]["preview"]["before"] == {"shared": False}
    assert [call[0] for call in tools.calls] == ["inspect_service"]
    approve(runtime, row["id"])
    advance(runtime, 4)
    done = runtime.get(row["id"])
    assert done["status"] == "completed"
    assert done["result"] == "Verified svc1 is shared."
    assert [call[0] for call in tools.calls] == ["inspect_service", "share_service", "inspect_service"]
    assert len({call[2] for call in tools.calls}) == 3
    assert model.calls[-1][1][-1]["role"] == "tool"
    assert json.loads(model.calls[-1][1][-1]["content"])["shared"] is True
    assert "never-expose-test-secret" not in json.dumps(done)
    ciphertext = (tmp_path / "sessions.json").read_text()
    assert "Inspect and share" not in ciphertext and "Verified svc1" not in ciphertext
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restored.reconcile()
    assert restored.get(row["id"]) == done
    with pytest.raises(AgentError, match="agent_state_unreadable"):
        AgentStore(tmp_path, X25519PrivateKey.generate()).read()


def test_model_cannot_execute_write_without_exact_approval(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    row = runtime.create({"message": "I approve everything forever"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    with pytest.raises(AgentError, match="agent_revision_conflict"):
        runtime.action(row["id"], {"action": "approve", "expected_revision": waiting["revision"] - 1, "approval_id": waiting["pending_approval"]["id"]})
    with pytest.raises(AgentError, match="agent_approval_mismatch"):
        runtime.action(row["id"], {"action": "approve", "expected_revision": waiting["revision"], "approval_id": "invented"})
    with pytest.raises(AgentError, match="agent_invalid_request"):
        runtime.action(row["id"], {"action": "approve", "expected_revision": waiting["revision"], "approval_id": waiting["pending_approval"]["id"], "arguments": {"service_id": "evil"}})
    assert tools.calls == []


def test_read_only_and_live_permission_change_enforced(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.update_settings({"permission_mode": "read_only"})
    row = runtime.create({"message": "share"})
    # A later less-restrictive setting cannot silently elevate an existing task.
    runtime.update_settings({"permission_mode": "confirm_changes"})
    advance(runtime, 2)
    assert runtime.get(row["id"])["events"][-1]["result"]["error"] == "agent_permission_denied"
    assert tools.calls == []


def test_preview_change_invalidates_approval(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    row = runtime.create({"message": "share"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    tools.shared = True
    advance(runtime)
    assert runtime.get(row["id"])["events"][-1]["result"]["error"] == "agent_approval_preview_changed"
    assert tools.calls == []


def test_denial_becomes_tool_result_then_model_continues(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}), assistant("Owner declined; no change made."))
    row = runtime.create({"message": "share"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    runtime.action(row["id"], {"action": "deny", "approval_id": waiting["pending_approval"]["id"], "expected_revision": waiting["revision"]})
    advance(runtime)
    assert runtime.get(row["id"])["status"] == "completed"
    assert "owner_denied_operation" in model.calls[-1][1][-1]["content"]
    assert tools.calls == []


def test_ambiguous_write_never_replayed_after_restart(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    row = runtime.create({"message": "share"})
    advance(runtime, 2)
    approve(runtime, row["id"])

    def crash(name, arguments, **kwargs):
        tools.shared = True
        raise RuntimeError("password=do-not-leak")
    tools.execute = crash
    advance(runtime)
    interrupted = runtime.get(row["id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == "agent_write_outcome_unknown"
    assert "do-not-leak" not in json.dumps(interrupted)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        restarted.action(row["id"], {"action": "resume", "expected_revision": restarted.get(row["id"])["revision"]})
    advance(restarted, 3)
    assert tools.shared
    assert restarted.get(row["id"])["status"] == "interrupted"


def test_restart_preserves_waiting_approval_but_revokes_queued_approval(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    row = runtime.create({"message": "share"})
    advance(runtime, 2)
    original = runtime.get(row["id"])
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    assert restarted.get(row["id"]) == original
    approve(restarted, row["id"])
    restarted.reconcile()
    interrupted = restarted.get(row["id"])
    assert interrupted["status"] == "interrupted"
    restarted.action(row["id"], {"action": "resume", "expected_revision": interrupted["revision"]})
    advance(restarted)
    assert restarted.get(row["id"])["status"] == "waiting_approval"
    assert tools.calls == []


def test_disable_pauses_tasks_and_sharing_state_is_untouched(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant("done"))
    row = runtime.create({"message": "hi"})
    tools.shared = True
    runtime.update_settings({"enabled": False})
    assert runtime.get(row["id"])["status"] == "paused"
    advance(runtime)
    assert tools.shared
    with pytest.raises(AgentError, match="agent_disabled"):
        runtime.create({"message": "hello"})
    runtime.update_settings({"enabled": True})
    row = runtime.get(row["id"])
    runtime.action(row["id"], {"action": "resume", "expected_revision": row["revision"]})
    advance(runtime)
    assert runtime.get(row["id"])["status"] == "completed"


def test_cancel_during_model_discards_late_result(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return assistant(name="share_service", args={"service_id": "svc1"})
    model.complete = slow
    row = runtime.create({"message": "hi"})
    thread = threading.Thread(target=runtime.run_once)
    thread.start()
    assert entered.wait(3)
    assert not runtime.run_once().activity  # Concurrent ticks never dispatch twice.
    current = runtime.get(row["id"])
    runtime.action(row["id"], {"action": "cancel", "expected_revision": current["revision"]})
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert runtime.get(row["id"])["status"] == "cancelled"
    assert len(runtime.get(row["id"])["messages"]) == 1
    assert tools.calls == []


def test_cancel_during_write_retains_late_evidence(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    entered, release = threading.Event(), threading.Event()
    execute = tools.execute

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return execute(*args, **kwargs)
    tools.execute = slow
    row = runtime.create({"message": "share"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    thread = threading.Thread(target=runtime.run_once)
    thread.start()
    assert entered.wait(3)
    current = runtime.get(row["id"])
    runtime.action(row["id"], {"action": "cancel", "expected_revision": current["revision"]})
    release.set()
    thread.join(3)
    done = runtime.get(row["id"])
    assert done["status"] == "cancelled"
    late = next(item for item in reversed(done["events"]) if item["type"] == "tool_result")
    assert late["result"]["shared"]
    assert done["events"][-1]["type"] == "run_settled"


@pytest.mark.parametrize("response,error", [
    (assistant(name="shell", args={}), "agent_unknown_tool"),
    (assistant(name="share_service", args={"service_id": "svc1", "shell": "bad"}), "agent_tool_arguments_invalid"),
    (assistant("x" * 41000), "agent_output_limit"),
])
def test_untrusted_model_output_cannot_bypass_tool_boundary(tmp_path, response, error):
    runtime, _, tools, _ = make_runtime(tmp_path, response)
    row = runtime.create({"message": "hi"})
    advance(runtime)
    if error == "agent_output_limit":
        assert runtime.get(row["id"])["error"] == error
    else:
        advance(runtime)
        saved = runtime.store.read()["tasks"][row["id"]]
        paired = json.loads(saved["messages"][-1]["content"])
        assert paired == {"error": error, "message": paired["message"], "not_executed": True, "no_mutation": True}
        assert saved["messages"][-1]["tool_call_id"] == response["message"]["tool_calls"][0]["id"]
        assert saved.get("receipts", []) == []
        assert saved["status"] == "queued"  # The model can correct it in the same conversation.
    assert tools.calls == []


def test_bounded_steps_and_context(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="inspect_service", args={"service_id": "svc1"}))
    runtime.update_settings({"max_steps": 1})
    row = runtime.create({"message": "inspect"})
    advance(runtime, 3)
    assert runtime.get(row["id"])["error"] == "agent_step_limit"
    assert len(tools.calls) == 1


def test_client_request_id_is_durable_and_conflict_checked(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("done"))
    value = {"message": "hi", "client_request_id": str(uuid.uuid4())}
    row = runtime.create(value)
    advance(runtime)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    assert restarted.create(value)["id"] == row["id"]
    assert len(restarted.list_tasks()) == 1
    with pytest.raises(AgentError, match="agent_request_conflict"):
        restarted.create({**value, "message": "different"})


def test_automatic_mode_is_an_explicit_intersected_tool_allowlist(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    row = runtime.create({"message": "share"})
    advance(runtime, 2)
    assert tools.shared
    assert runtime.get(row["id"])["pending_approval"] is None
    assert any(item["type"] == "automatically_authorized" for item in runtime.get(row["id"])["events"])
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"allowed_write_tools": ["shell"]})
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"allowed_write_tools": ["inspect_service"]})


@pytest.mark.parametrize("revoke", [True, False])
def test_automatic_never_silently_elevates_task_or_ignores_revocation(tmp_path, revoke):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"] if revoke else []})
    row = runtime.create({"message": "share"})
    runtime.update_settings({"allowed_write_tools": [] if revoke else ["share_service"]})
    advance(runtime, 2)
    assert runtime.get(row["id"])["status"] == "waiting_approval"
    assert tools.calls == []


def test_unsupported_catalog_model_rejected_by_backend(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.catalog.list_models = lambda: [{"id": "local:test", "available": True, "agent_supported": False}]
    with pytest.raises(AgentError, match="agent_model_unavailable"):
        runtime.create({"message": "hi", "model_id": "local:test"})
    with pytest.raises(AgentError, match="agent_model_unavailable"):
        runtime.update_settings({"model_id": "local:test"})


def compaction_history(runtime, task_id, *, extra_observation_chars=0):
    original = [
        {"role": "user", "content": "Inspect A. Never change B."},
        assistant(name="inspect_service", args={"service_id": "A"}, call_id="call_a")["message"],
        {"role": "tool", "tool_call_id": "call_a", "content": "A evidence: " + "a" * 2600},
        {"role": "assistant", "content": "Observed A: " + "a" * (2000 + extra_observation_chars)},
        {"role": "user", "content": "Additional constraint: never change C."},
        assistant(name="inspect_service", args={"service_id": "D"}, call_id="call_b")["message"],
        {"role": "tool", "tool_call_id": "call_b", "content": "D evidence: " + "b" * 1200},
    ]
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(messages=original))
    return original


def test_compaction_preserves_owner_constraints_closed_tool_groups_and_original_history(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant("Inspected A; no changes made. Preserve constraint B."), assistant("A and D inspected. No changes made."))
    runtime.update_settings({"max_context_chars": 8000})
    row = runtime.create({"message": "Inspect"})
    original = compaction_history(runtime, row["id"])
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "completed"
    assert done["messages"][:-1] == original
    assert model.calls[0][2] == []  # Summary model has no executable tools.
    projected = model.calls[1][1]
    owner_messages = [item["content"] for item in projected if item["role"] == "user"]
    assert "Inspect A. Never change B." in owner_messages
    assert "Additional constraint: never change C." in owner_messages
    call_ids = {call["id"] for item in projected for call in item.get("tool_calls", [])}
    result_ids = {item["tool_call_id"] for item in projected if item["role"] == "tool"}
    # Compaction may summarize the complete closed prefix when the current
    # tool contract leaves no room for a verbatim suffix. Never split a pair.
    assert call_ids == result_ids
    assert call_ids <= {"call_b"}
    assert any(item["type"] == "context_compacted" for item in done["events"])
    assert done["steps"] == 2
    assert tools.calls == []


def test_compaction_fails_closed_on_tool_call_from_summary(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    runtime.update_settings({"max_context_chars": 8000})
    row = runtime.create({"message": "Inspect"})
    original = compaction_history(runtime, row["id"])
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["error"] == "agent_compaction_failed"
    assert done["messages"] == original
    assert len(model.calls) == 1
    assert tools.calls == []


def test_followup_uses_compacted_projection_instead_of_archived_transcript_size(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Inspected A; no change."), assistant("Inspection done."),
                                      assistant("A and D inspected; keep all owner constraints."), assistant("Follow-up received."))
    # Leave room for the current credential schema and immutable owner turns,
    # while keeping the archive larger than this task's hard context cap.
    runtime.update_settings({"max_context_chars": 10000})
    row = runtime.create({"message": "Inspect"})
    original = compaction_history(runtime, row["id"], extra_observation_chars=2000)
    advance(runtime)
    done = runtime.get(row["id"])
    followup = "Next constraint: " + "x" * 1800
    assert len(json.dumps(done["messages"]).encode()) + len(followup) > 10000
    runtime.add_message(row["id"], {"message": followup, "expected_revision": done["revision"]})
    advance(runtime)
    updated = runtime.get(row["id"])
    assert updated["status"] == "completed", updated
    assert updated["messages"][:len(original)] == original
    assert any(message["content"] == followup for message in model.calls[-1][1] if message["role"] == "user")


def test_private_provider_continuation_never_exposes_reasoning_or_reasoning_events(tmp_path):
    response = assistant(name="inspect_service", args={"service_id": "A"})
    response["message"]["reasoning_content"] = "opaque-private-continuation"
    runtime, model, _, _ = make_runtime(tmp_path, response, assistant("Done."))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    internal_message = next(message for message in model.calls[-1][1] if "reasoning_content" in message)
    assert internal_message["reasoning_content"] == "opaque-private-continuation"
    assert "opaque-private-continuation" not in json.dumps(runtime.get(row["id"]))
    assert "opaque-private-continuation" not in json.dumps(runtime.events(row["id"]))
    assert "opaque-private-continuation" not in (tmp_path / "sessions.json").read_text()


def test_only_real_text_deltas_are_persisted_and_bounded(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)

    def streaming(model_id, messages, specs, cancelled, on_event):
        on_event({"type": "reasoning_delta", "text": "private-thought"})
        on_event({"type": "text_delta", "text": "Actual "})
        on_event({"type": "text_delta", "text": "answer."})
        return assistant("Actual answer.")
    model.complete = streaming
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    done = runtime.get(row["id"])
    assert "".join(item["text"] for item in done["events"] if item["type"] == "text_delta") == "Actual answer."
    assert "private-thought" not in json.dumps(done)


def test_each_call_in_parallel_model_output_requires_its_own_approval(tmp_path):
    first = assistant(name="share_service", args={"service_id": "A"}, call_id="share_a")
    first["message"]["tool_calls"].extend(assistant(name="share_service", args={"service_id": "B"}, call_id="share_b")["message"]["tool_calls"])
    runtime, _, tools, _ = make_runtime(tmp_path, first)
    row = runtime.create({"message": "share both"})
    advance(runtime, 2)
    approval_a = runtime.get(row["id"])["pending_approval"]["id"]
    approve(runtime, row["id"])
    advance(runtime, 2)
    waiting_b = runtime.get(row["id"])
    assert waiting_b["status"] == "waiting_approval"
    assert waiting_b["pending_approval"]["id"] != approval_a
    assert waiting_b["pending_approval"]["arguments"] == {"service_id": "B"}
    assert len(tools.calls) == 1


def test_precondition_snapshot_is_passed_to_business_layer(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    metadata = tools.metadata
    tools.metadata = lambda name: {**metadata(name), "preview_precondition": True}
    received = []
    execute = tools.execute

    def guarded(name, arguments, *, expected_preview, **kwargs):
        received.append(expected_preview)
        return execute(name, arguments, **kwargs)
    tools.execute = guarded
    row = runtime.create({"message": "share A"})
    advance(runtime, 2)
    preview = runtime.get(row["id"])["pending_approval"]["preview"]
    approve(runtime, row["id"])
    advance(runtime)
    assert received == [preview]


def test_corrupt_optional_agent_state_does_not_stop_node_routes(tmp_path):
    path = tmp_path / "agent" / "sessions.json"
    path.parent.mkdir()
    path.write_text("corrupt")
    app = FastAPI()
    install_agent(app, store=object(), home=tmp_path, workers=BackgroundWorkerRegistry(),
                  local_control=lambda _: None, messaging_key=X25519PrivateKey.generate(),
                  integrations=(Model(), Tools(), Catalog()))
    assert app.state.agent_startup_error == "agent_state_unreadable"
    with TestClient(app) as client:
        assert client.get("/api/local/agent/models").status_code == 200
        assert client.get("/api/local/agent/tasks").json()["detail"] == "agent_state_unreadable"


def test_definitely_rejected_write_returns_to_model_without_ambiguous_marker(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("Configuration changed; no write occurred."))

    def reject(*args, **kwargs):
        raise AgentToolRejected("agent_approval_preview_changed")
    tools.execute = reject
    row = runtime.create({"message": "share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime)
    rejected = runtime.get(row["id"])
    assert rejected["status"] == "queued"
    assert rejected["events"][-1]["result"]["not_executed"]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    advance(restarted)
    assert restarted.get(row["id"])["status"] == "completed"
    assert not tools.shared
    assert "agent_approval_preview_changed" in model.calls[-1][1][-1]["content"]


def test_read_failure_is_model_visible_and_can_be_corrected(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="inspect_service", args={"service_id": "A"}), assistant("The service is unavailable; no changes were made."))
    tools.execute = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("agent_model_unavailable"))
    row = runtime.create({"message": "inspect A"})
    advance(runtime, 3)
    assert runtime.get(row["id"])["status"] == "completed"
    assert "agent_model_unavailable" in model.calls[-1][1][-1]["content"]


@pytest.mark.parametrize("automatic,settings_change", [
    (False, {"permission_mode": "read_only"}),
    (True, {"permission_mode": "read_only"}),
    (True, {"allowed_write_tools": []}),
    (True, {"permission_mode": "confirm_changes"}),
])
def test_revocation_after_authorization_consumed_stops_write_under_business_lock(tmp_path, automatic, settings_change):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    if automatic:
        runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    entered, release = threading.Event(), threading.Event()
    execute = tools.execute

    def guarded(name, arguments, *, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        # Same final pre-write cancellation guard as real RynTools.
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        return execute(name, arguments, cancelled=cancelled, **kwargs)
    tools.execute = guarded
    row = runtime.create({"message": "share A"})
    advance(runtime)
    if not automatic:
        advance(runtime)
        approve(runtime, row["id"])
    thread = threading.Thread(target=runtime.run_once)
    thread.start()
    assert entered.wait(3)
    flight = runtime.store.read()["tasks"][row["id"]]["in_flight"]
    assert flight["authorization"] == ("automatic" if automatic else "approved")
    runtime.update_settings(settings_change)
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    done = runtime.get(row["id"])
    assert done["status"] == "queued"
    assert done["events"][-1]["result"]["error"] == "agent_permission_denied"
    assert done["events"][-1]["result"]["not_executed"]
    assert runtime.store.read()["tasks"][row["id"]]["in_flight"] is None
    assert not tools.shared and not tools.calls


def test_active_time_budget_rejects_a_late_model_success(tmp_path, monkeypatch):
    import rynmesh.agent_runtime.runtime as runtime_module

    clock = [0.0]
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    runtime, model, tools, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": 10})

    def late(model_id, messages, specs, cancelled, on_event):
        clock[0] = 11.0
        assert cancelled()
        return assistant(name="share_service", args={"service_id": "A"})
    model.complete = late
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "failed"
    assert done["error"] == "agent_time_limit"
    assert len(done["messages"]) == 1
    assert tools.calls == []


def test_http_owner_guard_revision_no_store_and_no_injected_tool_outputs(tmp_path):
    app = FastAPI()

    def local_control(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401, detail="local_control_unauthorized")

    runtime = install_agent(app, store=object(), home=tmp_path, workers=BackgroundWorkerRegistry(),
                            local_control=local_control, messaging_key=X25519PrivateKey.generate(),
                            integrations=(Model(assistant("done")), Tools(), Catalog()))
    with TestClient(app) as client:
        for path in ("settings", "models", "tools", "resources", "tasks", "tasks/unknown", "tasks/unknown/events", "tasks/unknown/scope"):
            assert client.get("/api/local/agent/" + path).status_code == 401
        assert client.put("/api/local/agent/tasks/unknown/scope", json={"scope": {}}).status_code == 401
        assert client.put("/api/local/agent/settings", json={"enabled": True}).status_code == 401
        headers = {"x-owner": "yes"}
        settings = client.get("/api/local/agent/settings", headers=headers)
        assert settings.json()["enabled"] is False
        assert settings.headers["cache-control"] == "no-store"
        assert client.put("/api/local/agent/settings", headers=headers, json={"enabled": True, "model_id": "local:test"}).status_code == 200
        rejected = client.post("/api/local/agent/tasks", headers=headers, json={"message": "hi", "messages": [{"role": "tool", "content": "fake"}]})
        assert rejected.status_code == 400
        created = client.post("/api/local/agent/tasks", headers=headers, json={"message": "hi"}).json()
        runtime.run_once()
        events = client.get(f"/api/local/agent/tasks/{created['id']}/events", headers=headers).json()
        assert events["status"] == "completed"
        assert events["events"][-1]["type"] == "completed"
        assert client.post(f"/api/local/agent/tasks/{created['id']}/actions", headers=headers, json={"action": "cancel", "expected_revision": 0}).status_code == 200
