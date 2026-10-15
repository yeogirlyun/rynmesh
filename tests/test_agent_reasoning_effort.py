"""Reasoning controls reach the selected CLI without changing task authority."""
import copy
import json
import threading
import uuid
from collections import deque
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_model_switch import body as switch_body
from test_agent_runtime_model_switch import setup as switch_setup

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package import codex_session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.api_formats import normalize
from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter


def codex_model(**extra):
    return {"id": "local:test", "name": "Codex test", "source": "local_service", "adapter": "codex_cli",
            "service_id": "codex-cli", "peer_id": "self", "cli_model": "gpt-test",
            "available": True, "agent_supported": True, "context_window": 32768, "max_output_tokens": 4096,
            "supported_reasoning_efforts": ["low", "medium", "high", "ultra"], "default_reasoning_effort": "low", **extra}


def effort_runtime(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path)
    rows = [codex_model()]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_reasoning_effort = True
    calls = []
    def complete(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return assistant("done")
    model.complete = complete
    return runtime, model, calls, rows


def test_task_create_message_exact_retry_and_readback_preserve_selected_effort(tmp_path):
    runtime, model, calls, _ = effort_runtime(tmp_path)
    request = {"message": "hello", "reasoning_effort": "ultra", "client_request_id": str(uuid.uuid4())}
    created = runtime.create(request)
    assert created["reasoning_effort"] == "ultra"
    assert runtime.create(request)["id"] == created["id"]
    with pytest.raises(AgentError, match="request_conflict"):
        runtime.create({**request, "reasoning_effort": "low"})
    advance(runtime)
    assert calls[-1]["reasoning_effort"] == "ultra"
    current = runtime.get(created["id"])
    message = {"message": "next", "reasoning_effort": "high", "expected_revision": current["revision"],
               "client_request_id": str(uuid.uuid4())}
    changed = runtime.add_message(created["id"], message)
    assert changed["reasoning_effort"] == "high"
    assert runtime.add_message(created["id"], message)["reasoning_effort"] == "high"
    with pytest.raises(AgentError, match="request_conflict"):
        runtime.add_message(created["id"], {**message, "reasoning_effort": "low"})
    advance(runtime)
    assert calls[-1]["reasoning_effort"] == "high"
    # Omitting the option continues the task's explicit preference.
    runtime.add_message(created["id"], {"message": "again", "expected_revision": runtime.get(created["id"])["revision"]})
    advance(runtime)
    assert calls[-1]["reasoning_effort"] == "high"
    assert runtime.get(created["id"])["reasoning_effort"] == "high"
    reopened = AgentStore(runtime.store.root, runtime.store.key)
    assert reopened.read()["tasks"][created["id"]]["reasoning_effort"] == "high"


@pytest.mark.parametrize("effort", [None, "", "automatic", True, 3, {"effort": "low"}])
def test_invalid_effort_rejected_without_creating_a_task(tmp_path, effort):
    runtime, _, _, _ = effort_runtime(tmp_path)
    with pytest.raises(AgentError, match="agent_invalid_reasoning_effort"):
        runtime.create({"message": "hello", "reasoning_effort": effort})
    assert not runtime.store.read()["tasks"]


@pytest.mark.parametrize("change", [
    {"adapter": "openai_compatible", "source": "local_api"},
    {"adapter": "antigravity_cli"},
    {"supported_reasoning_efforts": ["medium"]},
    {"supported_reasoning_efforts": []},
])
def test_only_selected_codex_capabilities_authorize_effort(tmp_path, change):
    runtime, _, calls, rows = effort_runtime(tmp_path)
    rows[0].update(change)
    with pytest.raises(AgentError, match="agent_reasoning_effort_unsupported"):
        runtime.create({"message": "hello", "reasoning_effort": "low"})
    # Legacy clients can still use the configured model's defaults.
    created = runtime.create({"message": "hello"})
    assert "reasoning_effort" not in created
    advance(runtime)
    assert "reasoning_effort" not in calls[-1]


def test_message_does_not_mutate_effort_of_an_inflight_request(tmp_path):
    runtime, model, calls, _ = effort_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def complete(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return assistant("done")
    model.complete = complete
    created = runtime.create({"message": "hello", "reasoning_effort": "low"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    try:
        assert entered.wait(5)
        current = runtime.get(created["id"])
        runtime.add_message(created["id"], {"message": "do more", "intent": "steer", "reasoning_effort": "high",
            "expected_control_revision": current["control_revision"], "expected_goal_revision": current["goal"]["revision"],
            "client_request_id": str(uuid.uuid4())})
        assert calls[0]["reasoning_effort"] == "low"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    advance(runtime)
    assert calls[-1]["reasoning_effort"] == "high"


def test_model_switch_clears_incompatible_effort_without_changing_scope_rules(tmp_path):
    runtime, _, _, _, task_id, rows = switch_setup(tmp_path)
    rows[0].update(codex_model(id="local:test", destination_revision="local:test-v1"))
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(reasoning_effort="ultra"))
    changed = runtime.switch_model(task_id, switch_body(runtime, task_id))
    assert changed["model_id"] == "local:other" and changed["status"] == "paused"
    assert "reasoning_effort" not in changed
    assert runtime.store.read()["tasks"][task_id]["scope"]["resource_grants"] == []


def test_model_switch_retains_effort_only_when_new_model_supports_it(tmp_path):
    runtime, _, _, _, task_id, rows = switch_setup(tmp_path)
    for row in rows:
        row.update(adapter="codex_cli", supported_reasoning_efforts=["low", "ultra"], default_reasoning_effort="low")
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(reasoning_effort="ultra"))
    changed = runtime.switch_model(task_id, switch_body(runtime, task_id))
    assert changed["reasoning_effort"] == "ultra" and changed["status"] == "paused"


def test_changed_capabilities_reject_saved_effort_before_dispatch(tmp_path):
    runtime, _, calls, rows = effort_runtime(tmp_path)
    created = runtime.create({"message": "hello", "reasoning_effort": "ultra"})
    rows[0]["supported_reasoning_efforts"] = ["low"]
    advance(runtime)
    assert runtime.get(created["id"])["error"] == "agent_reasoning_effort_unsupported"
    assert not calls


def test_real_runtime_bridge_cli_proposal_path_sends_effort_to_turn_start(tmp_path, monkeypatch):
    runtime, _, _, rows = effort_runtime(tmp_path)
    wire = []
    class RPC:
        def __init__(self, *_):
            self.root, self.process = tmp_path, SimpleNamespace()
            self.pending = deque()
        def initialize(self): pass
        def call(self, method, params):
            wire.append((method, copy.deepcopy(params)))
            if method == "thread/start":
                return {"thread": {"id": "thread"}}
            answer = json.dumps({"content": "done", "tool_calls": []})
            self.pending.extend([
                {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {"type": "agentMessage", "text": answer}}},
                {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}},
            ])
            return {"turn": {"id": "turn"}}
        def close(self): pass
    monkeypatch.setattr(codex_session, "CodexRPC", RPC)
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "test-cli")
    manager = SimpleNamespace(adapter=CLIAgentAdapter("codex_cli"), manifest=SimpleNamespace(timeout_seconds=60),
        _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    catalog = SimpleNamespace(resolve=lambda _: copy.deepcopy(rows[0]), destination=ModelCatalog.destination)
    runtime.model = AgentModelBridge({"provider_for": lambda _: manager}, catalog)
    created = runtime.create({"message": "hello", "reasoning_effort": "ultra"})
    advance(runtime)
    assert runtime.get(created["id"])["status"] == "completed"
    turn = next(params for method, params in wire if method == "turn/start")
    thread = next(params for method, params in wire if method == "thread/start")
    assert turn["effort"] == "ultra" and "outputSchema" in turn
    assert thread["model"] == "gpt-test" and thread["sandbox"] == "read-only"
    assert thread["dynamicTools"] == [] and thread["environments"] == turn["environments"] == []


def test_peer_cli_effort_is_sealed_with_chat_without_changing_destination(tmp_path):
    target = codex_model(id="peer/test", source="peer")
    catalog = SimpleNamespace(resolve=lambda _: target, destination=ModelCatalog.destination, network="fixture")
    sent = []
    async def execute_order(request):
        sent.append(request)
        return {"state": "succeeded", "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
    bridge = AgentModelBridge({"execute_order": execute_order}, catalog)
    bridge.complete(target["id"], [{"role": "user", "content": "hello"}], [], lambda: False, reasoning_effort="high")
    assert sent[0]["chat"]["reasoning_effort"] == "high"
    assert sent[0]["cli_model"] == "gpt-test" and sent[0]["provider_peer_id"] == "self"


def test_chat_contract_preserves_effort_and_other_cli_rejects_it():
    body = {"messages": [{"role": "user", "content": "hello"}], "reasoning_effort": "ultra"}
    assert normalize(body, "chat")["reasoning_effort"] == "ultra"
    with pytest.raises(ValueError, match="reasoning_effort"):
        validate_chat({**body, "reasoning_effort": "invented"})
    with pytest.raises(AdapterError, match="does not support reasoning_effort"):
        CLIAgentAdapter("antigravity_cli").chat(body, task_id="test", timeout_s=1)
