"""Native continuation stays private while ordinary client tools use one router."""
import importlib
import json

import pytest
from test_agent_runtime import advance, assistant, compaction_history, make_runtime

from rynmesh.agent_runtime import checkpoint_refs, output_policy, projects
from rynmesh.agent_runtime.model_events import ModelEventStream
from rynmesh.agent_runtime.prompt import provider_messages
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package.chat import completion, validate_chat, validate_provider_state
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.tool_capabilities import client_tools_contract
from rynmesh.llm_runtime_server import template_messages


def state(protocol="anthropic_messages"):
    return {"protocol": protocol, "scope": "a" * 64,
            "data": [{"type": "thinking", "thinking": "PRIVATE_NATIVE_CONTINUATION",
                      "signature": "PRIVATE_SIGNATURE"}]}


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages"])
def test_native_state_tool_turn_survives_encrypted_restart_without_public_projection(tmp_path, protocol):
    response = assistant("Inspecting service.", name="inspect_service", args={"service_id": "svc"}, call_id="native_call")
    private = response["message"]["provider_state"] = state(protocol)
    private["data"][0]["thinking"] *= 200  # private bytes exceed the visible prose cap
    runtime, model, tools, key = make_runtime(tmp_path, response, assistant("Done."))
    runtime.update_settings({"max_output_chars": 1000})
    task = runtime.create({"message": "Inspect service"})
    advance(runtime)
    stored = runtime.store.read()["tasks"][task["id"]]
    assert stored["messages"][-1]["provider_state"] == private
    assert private["data"][0]["thinking"] not in (tmp_path / "sessions.json").read_text()
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restored.reconcile()
    advance(restored, 2)
    assert restored.get(task["id"])["status"] == "completed"
    continued = model.calls[-1][1]
    assert next(m for m in continued if m.get("provider_state"))["provider_state"] == private
    tool_result = next(m for m in continued if m["role"] == "tool")
    assert tool_result["tool_call_id"] == "native_call"
    assert json.loads(tool_result["content"])["service_id"] == "svc"
    assert [call[0] for call in tools.calls] == ["inspect_service"]
    public = json.dumps([restored.get(task["id"]), restored.events(task["id"])])
    assert "provider_state" not in public and "PRIVATE_NATIVE" not in public and "PRIVATE_SIGNATURE" not in public


def test_native_state_excluded_from_display_finish_events_and_template_prose():
    message = {"role": "assistant", "content": "Visible answer.", "provider_state": state()}
    assert output_policy.visible_message(message) == {"role": "assistant", "content": "Visible answer."}
    observed = []
    stream = ModelEventStream(observed.append)
    response = {"message": message, "finish_reason": "stop"}
    stream.finish(response)
    assert "provider_state" not in observed[-1]["message"]
    assert response["message"]["provider_state"] == state()  # authoritative return value remains intact
    assert "provider_state" not in template_messages([message])[0]
    assert "provider_state" not in projects.summary_history([message])[0]
    assert "provider_state" not in checkpoint_refs.project([message])[0][0]
    projected = provider_messages([message])
    assert projected[0]["provider_state"] == state()
    projected[0]["provider_state"]["data"].clear()
    assert message["provider_state"] == state()


def test_public_chat_completion_does_not_expose_or_mutate_native_state():
    result = {"message": {"role": "assistant", "content": "ok", "provider_state": state(),
                          "reasoning_content": "Existing API compatibility"}}
    public = completion(result, "fixture", "id")
    assert "provider_state" not in json.dumps(public)
    assert public["choices"][0]["message"]["reasoning_content"] == "Existing API compatibility"
    assert result["message"]["provider_state"] == state()
    public["choices"][0]["message"]["content"] = "changed copy"
    assert result["message"]["content"] == "ok"


@pytest.mark.parametrize("kind,module_name", [("codex_cli", "codex_session"), ("antigravity_cli", "antigravity_session")])
def test_direct_cli_adapter_never_encodes_native_continuation(kind, module_name, monkeypatch):
    session = importlib.import_module("rynmesh.llm_package." + module_name)
    received = []
    def chat(adapter, body, **kwargs):
        received.append(body)
        return {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    monkeypatch.setattr(session, "chat", chat)
    original = {"messages": [{"role": "assistant", "content": "earlier", "provider_state": state()},
                             {"role": "user", "content": "continue"}]}
    CLIAgentAdapter(kind).chat(original, task_id="fixture", timeout_s=5)
    assert "provider_state" not in json.dumps(received)
    assert original["messages"][0]["provider_state"] == state()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
def test_native_protocol_labels_declare_the_same_client_tool_contract(protocol):
    assert client_tools_contract(protocol)
    assert not client_tools_contract(protocol, ["api-text-only"])


def test_native_state_is_removed_before_lossy_compaction(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Summary."), assistant("Done."))
    runtime.update_settings({"max_context_chars": 8000})
    task = runtime.create({"message": "Inspect"})
    compaction_history(runtime, task["id"])
    def add_private(data):
        for message in data["tasks"][task["id"]]["messages"]:
            if message["role"] == "assistant":
                message["provider_state"] = state()
    runtime.store.transaction(add_private)
    advance(runtime)
    assert not model.calls[0][2]  # the first request is the summarizer
    summary_request = json.dumps(model.calls[0][1])
    assert "provider_state" not in summary_request and "PRIVATE_NATIVE" not in summary_request
    archive = runtime.store.read()["tasks"][task["id"]]["messages"]
    assert any(message.get("provider_state") for message in archive)


@pytest.mark.parametrize("replacement", [None, [], "native", {"protocol": "other", "scope": "a" * 64, "data": []},
    {"protocol": [], "scope": "a" * 64, "data": []},
    {"protocol": "anthropic_messages", "scope": "wrong", "data": []},
    {"protocol": "anthropic_messages", "scope": "a" * 64, "data": [], "extra": True},
    {"protocol": "anthropic_messages", "scope": "a" * 64, "data": float("nan")},
    {"protocol": "anthropic_messages", "scope": "a" * 64, "data": {1: "wrong-key"}},
    {"protocol": "anthropic_messages", "scope": "a" * 64, "data": {"not_json": {1, 2}}}])
def test_private_state_rejects_malformed_namespaces_and_non_json(replacement):
    with pytest.raises(ValueError):
        validate_provider_state(replacement)


@pytest.mark.parametrize("role", ["user", "system", "developer", "tool"])
def test_private_state_is_only_allowed_on_assistant(role):
    with pytest.raises(ValueError, match="assistant"):
        validate_chat({"messages": [{"role": role, "content": "x", "provider_state": state()}]})


def test_private_state_size_and_complexity_are_bounded(monkeypatch):
    import rynmesh.llm_package.chat as chat
    monkeypatch.setattr(chat, "MAX_REQUEST", 500)
    oversized = state()
    oversized["data"] = "x" * 501
    with pytest.raises(ValueError, match="byte limit"):
        validate_provider_state(oversized)
    deep = state()
    deep["data"] = None
    for _ in range(66):
        deep["data"] = [deep["data"]]
    with pytest.raises(ValueError, match="complexity"):
        validate_provider_state(deep)
    valid = {"role": "assistant", "content": "ok", "provider_state": state()}
    assert validate_chat({"messages": [valid]})["messages"][0] == valid
