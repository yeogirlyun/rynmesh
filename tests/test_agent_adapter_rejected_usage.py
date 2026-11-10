"""Actual JSON/SSE parsing preserves received counters through rejected inference."""
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source, node  # noqa: F401
from test_agent_reference_budget import ReferenceBudget

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentStore
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.services import peer_box


def transport(node, monkeypatch, mode, stop, usage, finish="tool_calls", on_stop=lambda: None):  # noqa: F811
    app, store = node
    source, selected = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    manager = app.state.llm_agent_services["provider_for"](catalog.resolve(selected)["service_id"])
    adapter = manager.adapter.inner
    now, stopped, requests = [time.monotonic()], [False], []
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr("rynmesh.llm_package.adapters.time", clock)
    monkeypatch.setattr("rynmesh.agent_runtime.bridge.time", clock)
    if mode != "json":
        resolve = catalog.resolve
        monkeypatch.setattr(catalog, "resolve", lambda model_id: {**resolve(model_id), "streaming": True})
    message = {"role": "assistant", "content": None, "reasoning_content": "PRIVATE_REASONING",
               "tool_calls": [{"id": "late", "type": "function", "function": {"name": "ryn_set_model_sharing",
                   "arguments": json.dumps({"model_id": selected, "enabled": True})}}]}
    def stop_now():
        if stop == "cancel":
            stopped[0] = True
            for task_id in list(adapter._active_responses):
                adapter.cancel(task_id)
        elif stop == "deadline":
            now[0] += 601
        on_stop()
    class Response:
        headers = {"content-type": "text/event-stream" if mode == "sse" else "application/json"}
        count = 0
        def close(self):
            pass  # Simulate a provider delivering buffered data after close.
        def read(self, _limit):
            stop_now()
            return json.dumps({"choices": [{"message": message, "finish_reason": finish}], "usage": usage}).encode()
        def readline(self, _limit):
            self.count += 1
            if self.count == 1:
                delta = {**message, "tool_calls": [{**message["tool_calls"][0], "index": 0}]}
                return b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}).encode() + b"\n"
            stop_now()
            if stop == "disconnect":
                raise OSError("PRIVATE_TRANSPORT_ERROR")
            return b"data: [DONE]\n"
    @contextmanager
    def urlopen(request, **kwargs):
        requests.append(json.loads(request.data))
        yield Response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return bridge, tools, catalog, selected, source, now, stopped, requests


@pytest.mark.parametrize("mode", ["json", "sse", "stream_json"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_real_adapter_rejection_keeps_known_usage_and_closed_finish(node, monkeypatch, mode, stop):  # noqa: F811
    bridge, tools, _, selected, _, now, stopped, requests = transport(node, monkeypatch, mode, stop,
        {"prompt_tokens": 7, "completion_tokens": 3})
    with pytest.raises(AgentModelResponseError, match="agent_cancelled" if stop == "cancel" else "agent_inference_timeout") as error:
        bridge.complete(selected, [{"role": "user", "content": "inspect"}], tools.specs(), lambda: stopped[0],
                        deadline=now[0] + 30, on_event=lambda _: None)
    assert error.value.usage == {"input_tokens": 7, "output_tokens": 3}
    assert error.value.finish_reason == "tool_calls"
    assert len(requests) == 1 and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("usage,finish,expected", [
    ({"prompt_tokens": 0, "completion_tokens": 0}, "stop", {"input_tokens": 0, "output_tokens": 0}),
    ({"prompt_tokens": True, "completion_tokens": -3}, ["PRIVATE"], {"input_tokens": None, "output_tokens": None}),
    ({"prompt_tokens": "7", "completion_tokens": 3}, {"PRIVATE": True}, {"input_tokens": None, "output_tokens": 3}),
    (None, "PRIVATE", {"input_tokens": None, "output_tokens": None}),
    (["PRIVATE"], "PRIVATE", {"input_tokens": None, "output_tokens": None}),
])
def test_untrusted_metadata_never_becomes_actual_usage(node, monkeypatch, mode, usage, finish, expected):  # noqa: F811
    bridge, tools, _, selected, _, now, stopped, _ = transport(node, monkeypatch, mode, "cancel", usage, finish)
    with pytest.raises(AgentModelResponseError) as error:
        bridge.complete(selected, [{"role": "user", "content": "inspect"}], tools.specs(), lambda: stopped[0],
                        deadline=now[0] + 30, on_event=lambda _: None)
    assert error.value.usage == expected
    assert error.value.finish_reason == (finish if finish == "stop" else "unknown")
    assert "PRIVATE" not in str(error.value) and "PRIVATE" not in json.dumps(vars(error.value))


def test_stream_usage_survives_subsequent_disconnect(node, monkeypatch):  # noqa: F811
    bridge, tools, _, selected, _, now, _, _ = transport(node, monkeypatch, "sse", "disconnect",
        {"prompt_tokens": 7, "completion_tokens": 3})
    with pytest.raises(AgentModelResponseError, match="agent_model_connection_failed") as error:
        bridge.complete(selected, [{"role": "user", "content": "inspect"}], tools.specs(), lambda: False,
                        deadline=now[0] + 30, on_event=lambda _: None)
    assert error.value.usage == {"input_tokens": 7, "output_tokens": 3}


@pytest.mark.parametrize("host", ["candidate", "reference"])
@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.parametrize("usage", [{"prompt_tokens": 7, "completion_tokens": 3}, None, {"prompt_tokens": 0, "completion_tokens": 0}])
def test_received_failure_is_settled_by_both_hosts_without_late_tools(node, monkeypatch, host, mode, stop, usage):  # noqa: F811
    task_holder = {}
    def owner_cancel():
        if stop == "cancel":
            task_holder["runtime"].action(task_holder["id"], {"action": "cancel"})
    bridge, tools, catalog, selected, source, now, stopped, requests = transport(node, monkeypatch, mode, stop,
        usage, on_stop=owner_cancel)
    _, store = node
    runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": selected, "max_seconds": 600})
    task = runtime.create({"message": "Inspect"})
    task_holder.update(runtime=runtime, id=task["id"])
    sources = bridge.services["source_store"]
    before = sources.path.read_bytes()
    if host == "candidate":
        runtime.run_once()
        runtime.run_once()  # No late assistant tool call can enter dispatch.
    else:
        meter = ReferenceBudget(runtime, task["id"], guard=lambda: None, clock=lambda: now[0])
        with pytest.raises(AgentError):
            meter.model(lambda _: bridge.complete(selected, [{"role": "user", "content": "inspect"}], tools.specs(),
                meter.cancelled, deadline=now[0] + 30, on_event=lambda _: None,
                on_request_policy=meter.record_request_policy), input_bytes=80, max_output_tokens=4096)
    row = runtime.store.read()["tasks"][task["id"]]
    flight = row["budget"]["records"][0]
    if usage is None:
        assert flight["input_tokens"] is None and flight["output_tokens"] is None
        assert flight["charged_tokens"] == flight["input_reservation"] + flight["output_reservation"] > 0
    else:
        assert flight["input_tokens"] == usage["prompt_tokens"] and flight["output_tokens"] == usage["completion_tokens"]
        assert flight["charged_tokens"] == sum(usage.values())
    assert flight["request_policy"]["provider_timeout_seconds"] == 180
    assert len(requests) == 1 and sources.path.read_bytes() == before
    assert "PRIVATE" not in json.dumps(runtime.get(task["id"]))
    assert not row.get("operation_receipts")


def test_legacy_adapter_error_remains_unknown():
    error = AdapterError("legacy failure")
    assert error.reported_usage is None and error.finish_reason == "unknown"


def test_reviewer_reproduction_complete_json_then_adapter_cancel(monkeypatch):
    adapter = OpenAICompatibleAdapter(base_url="https://vendor.test/v1", model="synthetic", allow_non_loopback=True)
    def received(*args, task_id, **kwargs):
        adapter.cancel(task_id)
        return {"choices": [{"message": {"role": "assistant", "content": "PRIVATE"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    monkeypatch.setattr(adapter, "_json", received)
    with pytest.raises(AdapterError, match="task_cancelled") as error:
        adapter.chat({"messages": [{"role": "user", "content": "hello"}]}, task_id="reviewer-reproduction", timeout_s=30)
    assert error.value.reported_usage == {"input_tokens": 7, "output_tokens": 3}
    assert error.value.finish_reason == "stop"
    assert "PRIVATE" not in str(error.value) and "PRIVATE" not in json.dumps(vars(error.value))
