"""Provider limits intersect active durable budgets without changing legacy APIs."""
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source, node  # noqa: F401
from test_agent_reference_budget import ReferenceBudget, setup
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.bridge import AgentModelBridge, build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentStore
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.native_model import ManagedAdapter


def local_bridge(node, monkeypatch, provider=180):  # noqa: F811
    app, store = node
    _, selected = add_source(app)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    manager = app.state.llm_agent_services["provider_for"](catalog.resolve(selected)["service_id"])
    manager.manifest.timeout_seconds = provider
    now = [1000.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr("rynmesh.agent_runtime.bridge.time", clock)
    monkeypatch.setattr("rynmesh.llm_package.adapters.time", clock)
    seen = []
    def chat(body, *, task_id, timeout_s, on_event):
        seen.append(timeout_s)
        return {"message": {"role": "assistant", "content": "ok"}, "input_tokens": 7, "output_tokens": 3, "finish_reason": "stop"}
    monkeypatch.setattr(manager.adapter.inner, "chat", chat)
    return bridge, selected, manager, now, seen


@pytest.mark.parametrize("provider,remaining,want", [(180, 600, 180), (180, 17, 17), (25, 600, 25)])
def test_manifest_and_task_bounds_reach_real_source_gate(node, monkeypatch, provider, remaining, want):  # noqa: F811
    bridge, selected, _, now, seen = local_bridge(node, monkeypatch, provider)
    policies = []
    response = bridge.complete(selected, [{"role": "user", "content": "hello"}], [], lambda: False,
                               deadline=now[0] + remaining, on_request_policy=policies.append)
    assert seen == [want] and policies == [response["request_policy"]]
    assert policies[0]["provider_timeout_seconds"] == provider
    assert policies[0]["effective_timeout_seconds"] == want
    assert "deadline" not in json.dumps(policies)


@pytest.mark.parametrize("during", ["before", "prepare", "admission", "policy_record"])
def test_expiry_before_inner_is_zero_upstream_dispatch(node, monkeypatch, during):  # noqa: F811
    bridge, selected, manager, now, seen = local_bridge(node, monkeypatch)
    deadline = now[0] + 10
    if during == "before":
        now[0] += 11
    if during == "admission":
        original = manager.adapter._start
        def delayed():
            policy = original()
            now[0] += 11
            return policy
        monkeypatch.setattr(manager.adapter, "_start", delayed)
    def delay(_):
        now[0] += 11
    with pytest.raises(AgentError, match="agent_inference_timeout"):
        bridge.complete(selected, [{"role": "user", "content": "hello"}], [], lambda: False, deadline=deadline,
                        on_event=delay if during == "prepare" else None,
                        on_request_policy=delay if during == "policy_record" else None)
    assert seen == [] and manager._running == 0


def test_admission_delay_reduces_inner_timeout_without_bypassing_gate(node, monkeypatch):  # noqa: F811
    bridge, selected, manager, now, seen = local_bridge(node, monkeypatch)
    original = manager.adapter._start
    def delayed():
        policy = original()
        now[0] += 6
        return policy
    monkeypatch.setattr(manager.adapter, "_start", delayed)
    bridge.complete(selected, [{"role": "user", "content": "hello"}], [], lambda: False, deadline=now[0] + 17)
    assert seen == [11]


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_late_response_is_never_usable_but_received_usage_survives(node, monkeypatch, stop):  # noqa: F811
    bridge, selected, manager, now, _ = local_bridge(node, monkeypatch)
    stopped = [False]
    def late(*args, **kwargs):
        now[0] += 11
        stopped[0] = stop == "cancel"
        return {"message": {"role": "assistant", "content": "PRIVATE_LATE"}, "input_tokens": 7, "output_tokens": 3}
    monkeypatch.setattr(manager.adapter.inner, "chat", late)
    with pytest.raises(AgentModelResponseError, match="agent_cancelled" if stop == "cancel" else "agent_inference_timeout") as error:
        bridge.complete(selected, [{"role": "user", "content": "hello"}], [], lambda: stopped[0], deadline=now[0] + 10)
    assert error.value.usage == {"input_tokens": 7, "output_tokens": 3}
    assert "PRIVATE" not in str(error.value)


def test_source_revision_change_still_rejected_before_inner(node, monkeypatch):  # noqa: F811
    bridge, selected, manager, now, seen = local_bridge(node, monkeypatch)
    sources = manager.adapter.sources
    def change(_):
        row = sources.read()[manager.adapter.source_id]
        row["enabled"] = False
        sources.save(row)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        bridge.complete(selected, [{"role": "user", "content": "hello"}], [], lambda: False,
                        deadline=now[0] + 10, on_request_policy=change)
    assert seen == []


@pytest.mark.parametrize("round_seconds,lifetime,want", [(600, 3600, 600), (600, 30, 30), (15, 3600, 15)])
def test_runtime_uses_round_and_persisted_lifetime_remaining(tmp_path, round_seconds, lifetime, want):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": round_seconds})
    model.supports_request_deadline = True
    seen = []
    def complete(*args, deadline, on_request_policy):
        seen.append(deadline - time.monotonic())
        on_request_policy({"provider_timeout_seconds": 180, "effective_timeout_seconds": min(180, want),
                           "enforcement": "adapter_timeout_and_cancel_watch", "private": "never persist"})
        raise AgentError("agent_inference_timeout")
    model.complete = complete
    task = runtime.create({"message": "Explain", "budget_limits": {"max_total_seconds": lifetime}})
    advance(runtime)
    assert want - 2 < seen[0] <= want
    state = runtime.store.read()["tasks"][task["id"]]
    record = state["budget"]["records"][0]
    assert record["input_tokens"] is None and record["output_tokens"] is None and record["charged_tokens"] > 0
    assert record["request_policy"]["provider_timeout_seconds"] == 180
    assert "never persist" not in json.dumps(state) and "deadline" not in json.dumps(state)


def test_reference_wait_elapsed_and_reopen_rebuild_deadline(tmp_path):
    runtime, model, tools, key, task_id = setup(tmp_path, max_seconds=60, max_total_seconds=30)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0])
    now[0] += 4
    with meter.owner_wait():
        now[0] += 600
    assert 25 < meter.request_deadline() - time.monotonic() <= 26
    def elapsed(_):
        now[0] += 7
        return {"usage": {"input_tokens": 1, "output_tokens": 1}}
    meter.model(elapsed, input_bytes=8, max_output_tokens=10)
    assert 18 < meter.request_deadline() - time.monotonic() <= 19
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restored = ReferenceBudget(reopened, task_id, guard=lambda: None)
    assert 22 < restored.request_deadline() - time.monotonic() <= 23
    assert "deadline" not in json.dumps(reopened.store.read())


def test_runtime_reopen_subtracts_durable_charged_time_not_owner_wait(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": 60})
    task = runtime.create({"message": "Explain", "budget_limits": {"max_total_seconds": 30}})
    def used(data):
        row = data["tasks"][task["id"]]
        flight = budget.reserve(row, "model", "main", input_bound=4, output_bound=8)
        budget.settle(row, flight["id"], usage={"input_tokens": 1, "output_tokens": 1}, elapsed_seconds=7)
    runtime.store.transaction(used)
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    model.supports_request_deadline = True
    seen = []
    def complete(*args, deadline, on_request_policy):
        seen.append(deadline - time.monotonic())
        return assistant("done")
    model.complete = complete
    advance(restored)
    assert 21 < seen[0] <= 23


def test_unknown_history_rule_reaches_main_model(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("done"))
    runtime.create({"message": "Explain"})
    advance(runtime)
    system = "\n".join(message["content"] for message in model.calls[0][1]
                       if message["role"] in {"system", "developer"})
    assert "unknown/missing is not no/never" in system
    assert "Claims need matching subject/scope/time" in system


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
@pytest.mark.parametrize("transport_code", ["inference_failed", "runtime_connection_failed"])
def test_watcher_requests_transport_cancellation(node, monkeypatch, stop, transport_code):  # noqa: F811
    bridge, selected, manager, now, seen = local_bridge(node, monkeypatch)
    interrupted = threading.Event()
    stopped = [False]
    monkeypatch.setattr(manager.adapter.inner, "cancel", lambda _: interrupted.set())
    def wait_for_cancel(*args, **kwargs):
        if stop == "deadline":
            now[0] += 11
        else:
            stopped[0] = True
        assert interrupted.wait(2)
        # Killing a Codex app-server at the deadline closes its stdout pipe;
        # that EOF must retain the deadline/owner cause, not become a crash.
        raise AdapterError("Codex app-server disconnected", code=transport_code)
    monkeypatch.setattr(manager.adapter.inner, "chat", wait_for_cancel)
    with pytest.raises(ValueError, match="agent_cancelled" if stop == "cancel" else "agent_inference_timeout"):
        bridge.complete(selected, [{"role": "user", "content": "hello"}], [], lambda: stopped[0], deadline=now[0] + 10)
    assert manager._running == 0


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_late_stream_delta_never_becomes_public(node, monkeypatch, stop):  # noqa: F811
    bridge, selected, manager, now, _ = local_bridge(node, monkeypatch)
    resolve = bridge.catalog.resolve
    monkeypatch.setattr(bridge.catalog, "resolve", lambda model_id: {**resolve(model_id), "streaming": True})
    stopped, events = [False], []
    def stream(body, *, on_event, **kwargs):
        assert on_event is not None
        on_event({"choices": [{"delta": {"content": "before"}}]})
        now[0] += 11
        stopped[0] = stop == "cancel"
        on_event({"choices": [{"delta": {"content": "PRIVATE_LATE"}}]})
        return {"message": {"role": "assistant", "content": "PRIVATE_LATE"}, "input_tokens": 7, "output_tokens": 3}
    monkeypatch.setattr(manager.adapter.inner, "chat", stream)
    specs = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {}}}}]
    with pytest.raises(AgentModelResponseError):
        bridge.complete(selected, [{"role": "user", "content": "hello"}], specs, lambda: stopped[0],
                        deadline=now[0] + 10, on_event=events.append)
    assert [event["text"] for event in events if event["type"] == "text_delta"] == ["before"]
    assert "PRIVATE" not in json.dumps(events)


@pytest.mark.parametrize("delay", [6, 11])
def test_native_gate_rechecks_deadline_after_admission(monkeypatch, delay):
    now, seen = [1000.0], []
    monkeypatch.setattr("rynmesh.llm_package.adapters.time", SimpleNamespace(monotonic=lambda: now[0]))
    gate = object.__new__(ManagedAdapter)
    gate.owner = SimpleNamespace(manifest=SimpleNamespace(max_output_tokens=4096))
    gate.maximum = 4096
    @contextmanager
    def admitted():
        now[0] += delay
        yield SimpleNamespace(chat=lambda body, **kwargs: seen.append(kwargs["timeout_s"]))
    monkeypatch.setattr(gate, "request", admitted)
    if delay > 10:
        with pytest.raises(AdapterError, match="request_deadline_exhausted"):
            gate.chat({}, timeout_s=10, deadline=1010)
        assert seen == []
    else:
        gate.chat({}, timeout_s=10, deadline=1010)
        assert seen == [4]


def test_peer_late_result_rejected_without_invented_transport_timeout(monkeypatch):
    now, requests, policies = [1000.0], [], []
    monkeypatch.setattr("rynmesh.agent_runtime.bridge.time", SimpleNamespace(monotonic=lambda: now[0]))
    async def execute(request):
        requests.append(request)
        now[0] += 11
        return {"state": "succeeded", "message": {"role": "assistant", "content": "PRIVATE"},
                "reported_usage": {"input_tokens": 1, "output_tokens": 2}}
    bridge = AgentModelBridge({"execute_order": execute, "cancel_order": lambda _: None},
        SimpleNamespace(network="net", resolve=lambda _: {"source": "peer", "peer_id": "node-b", "service_id": "svc"}))
    with pytest.raises(AgentModelResponseError, match="agent_inference_timeout") as error:
        bridge.complete("peer-model", [{"role": "user", "content": "hello"}], [], lambda: False,
                        deadline=1010, on_request_policy=policies.append)
    assert error.value.usage == {"input_tokens": 1, "output_tokens": 2}
    assert policies[0]["provider_timeout_seconds"] is None
    assert policies[0]["enforcement"] == "consumer_cancel_watch_remote_transport_limits"
    assert not {"timeout", "timeout_s", "deadline"} & requests[0].keys()
