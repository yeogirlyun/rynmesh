"""Model item boundaries are independent of provider framing and tool policy."""
from __future__ import annotations

import copy
import io
import json
import threading
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.model_events import (
    ModelEventError,
    ModelEventStream,
    project_leading_developer,
)
from rynmesh.agent_runtime.store import AgentModelResponseError
from rynmesh.llm_package.adapters import OllamaAdapter, OpenAICompatibleAdapter


def call(arguments='{"path":"fixture.txt"}', name="ryn_file_read", call_id="call_fixture"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def result(calls=None, text="", finish="tool_calls"):
    return {"message": {"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})},
            "finish_reason": finish, "input_tokens": 0, "output_tokens": 0}


def bridge_with(chat, *, streaming=True, supports_developer=False):
    target = {"source": "local_service", "service_id": "fixture", "streaming": streaming,
              "adapter": "fixture", "max_output_tokens": 512, "context_window": 32768}
    adapter = SimpleNamespace(chat=chat, cancel=lambda _: None, supports_developer_role=supports_developer)
    manager = SimpleNamespace(adapter=adapter, manifest=SimpleNamespace(timeout_seconds=10),
                              _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    bridge = AgentModelBridge({"provider_for": lambda _: manager}, SimpleNamespace(resolve=lambda _: target))
    return bridge, manager


def kinds(events):
    return [event["type"] for event in events if event["type"] != "provider_selected"]


def test_chat_tool_waits_for_terminal_marker_and_emits_once_before_response():
    events = []
    stream = ModelEventStream(events.append)
    stream.feed({"choices": [{"delta": {"content": "Reading"}}]})
    stream.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_fixture", "type": "function",
                                                              "function": {"name": "ryn_file_read", "arguments": '{"path":'}}]}}]})
    stream.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"fixture.txt"}'}}]}}]})
    # Even balanced JSON remains an unfinished model item until the marker.
    assert kinds(events) == ["text_delta"]
    stream.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    assert kinds(events) == ["text_delta", "tool_call_complete"]
    assert events[-1]["call"] == call()
    stream.feed({"choices": [], "usage": {"completion_tokens": 0}})
    stream.finish({"message": result([call()], text="Reading")["message"], "finish_reason": "tool_calls", "usage": {"output_tokens": 0}})
    assert kinds(events) == ["text_delta", "tool_call_complete", "response_complete"]
    with pytest.raises(ModelEventError, match="duplicate"):
        stream.finish({"message": result([call()])["message"], "finish_reason": "tool_calls"})


def test_explicit_item_done_can_precede_later_text_without_exposing_arguments_delta():
    events = []
    stream = ModelEventStream(events.append)
    stream.feed({"type": "response.output_item.done", "output_index": 0,
                 "item": {"type": "function_call", "call_id": "call_fixture", "name": "ryn_file_read", "arguments": '{"path":"fixture.txt"}'}})
    stream.feed({"type": "text_delta", "text": "The item is complete."})
    assert kinds(events) == ["tool_call_complete", "text_delta"]
    assert events[0]["index"] == 0 and events[0]["provider_index"] == 0
    stream.finish({"message": result([call()], text="The item is complete.")["message"], "finish_reason": "tool_calls"})
    assert kinds(events).count("tool_call_complete") == 1


def test_explicit_item_namespace_is_not_dropped_into_a_flat_tool():
    events = []
    stream = ModelEventStream(events.append)
    stream.feed({"type": "response.output_item.done", "output_index": 0, "item": {
        "type": "function_call", "id": "fc_transport_item", "call_id": "call_fixture",
        "namespace": "unregistered_namespace", "name": "ryn_file_read", "arguments": "{}"}})
    assert events[0]["call"]["id"] == "call_fixture"
    assert events[0]["call"]["namespace"] == "unregistered_namespace"


def response_item(index, tool=None, kind="message"):
    item = {"type": kind} if tool is None else {"type": "function_call", "id": "fc_" + tool["id"],
        "call_id": tool["id"], **tool["function"]}
    return {"type": "response.output_item.done", "output_index": index, "item": item}


def test_responses_reasoning_text_indices_become_tool_ordinals():
    events = []
    stream = ModelEventStream(events.append)
    first, second = call(call_id="first"), call(call_id="second")
    stream.feed(response_item(0, kind="reasoning"))
    stream.feed(response_item(1, first))
    stream.feed(response_item(2))
    stream.feed(response_item(3, second))
    assert [(event["index"], event["provider_index"]) for event in events] == [(0, 1), (1, 3)]
    stream.finish(result([first, second]))
    assert kinds(events) == ["tool_call_complete", "tool_call_complete", "response_complete"]


def test_responses_eight_tools_plus_other_output_do_not_hit_provider_index_limit():
    events = []
    stream = ModelEventStream(events.append)
    calls = [call(call_id=f"call_{index}") for index in range(8)]
    for index, tool in enumerate(calls):
        stream.feed(response_item(index * 2, kind="reasoning" if index == 0 else "message"))
        stream.feed(response_item(index * 2 + 1, tool))
    stream.finish(result(calls))
    assert [event["index"] for event in events[:-1]] == list(range(8))
    assert events[-2]["provider_index"] == 15


def test_out_of_order_responses_done_waits_for_known_output_prefix():
    events = []
    stream = ModelEventStream(events.append)
    first, second = call(call_id="first"), call(call_id="second")
    stream.feed(response_item(3, second))
    stream.feed(response_item(1, first))
    assert events == []
    stream.feed(response_item(0, kind="reasoning"))
    assert events[0]["call"] == first and events[0]["index"] == 0
    stream.feed(response_item(2))
    assert events[1]["call"] == second and events[1]["index"] == 1
    stream.finish(result([first, second]))


@pytest.mark.parametrize("provider_index", [None, 9])
def test_unproven_responses_order_defers_until_final_without_false_envelope_error(provider_index):
    events = []
    stream = ModelEventStream(events.append)
    stream.feed(response_item(provider_index, call()))
    assert events == []
    stream.finish(result([call()]))
    assert events[0]["index"] == 0 and events[0]["call"] == call()
    assert kinds(events) == ["tool_call_complete", "response_complete"]


def test_chat_batch_byte_limit_is_checked_before_any_item_is_emitted():
    events = []
    stream = ModelEventStream(events.append)
    arguments = '{"text":"' + "x" * 70000 + '"}'
    fragments = [{"index": index, **call(arguments=arguments, call_id=f"call_{index}")} for index in range(4)]
    with pytest.raises(ModelEventError, match="batch limit"):
        stream.feed({"choices": [{"delta": {"tool_calls": fragments}, "finish_reason": "tool_calls"}]})
    assert events == []


@pytest.mark.parametrize("bad_call", [call(name="not_loaded"), call(arguments="{bad"), call(arguments="[]"),
                                     call(arguments={"path": "fixture.txt"}), call(name=None),
                                     {"id": "call_fixture", "type": "function"}])
def test_bridge_leaves_paired_tool_errors_for_router(bad_call):
    original = copy.deepcopy(bad_call)
    response = AgentModelBridge._response(result([bad_call]), {"source": "local_service"}, [], {})
    assert response["message"]["tool_calls"] == [original]
    assert response["usage"] == {"input_tokens": 0, "output_tokens": 0}
    with pytest.raises(AgentModelResponseError):
        AgentModelBridge._response(result([call(call_id="")]), {"source": "local_service"}, [], {})
    with pytest.raises(AgentModelResponseError):
        AgentModelBridge._response(result([call(), call()]), {"source": "local_service"}, [], {})


def test_developer_projection_preserves_order_and_never_elevates_project_user():
    messages = [{"role": "developer", "content": "base"}, {"role": "developer", "content": "host"},
                {"role": "developer", "content": "app"}, {"role": "user", "content": "project context"},
                {"role": "developer", "content": "later context"}]
    original = copy.deepcopy(messages)
    legacy = project_leading_developer(messages)
    assert [m["role"] for m in legacy] == ["system", "system", "system", "user", "developer"]
    assert [m["content"] for m in legacy] == [m["content"] for m in messages]
    assert project_leading_developer(messages, supports_developer=True) == messages == original


def test_bridge_preserves_typed_provider_turn_flags_in_terminal_event():
    events = []
    def chat(body, **kwargs):
        return {**result(text="More work follows."), "end_turn": False, "needs_follow_up": True}
    bridge, _ = bridge_with(chat, streaming=False)
    response = bridge.complete("fixture", [{"role": "user", "content": "fixture"}], [], lambda: False, events.append)
    assert response["end_turn"] is False and response["needs_follow_up"] is True
    assert events[-1]["end_turn"] is False and events[-1]["needs_follow_up"] is True


@pytest.mark.parametrize("flags", [{"end_turn": 0}, {"needs_follow_up": "true"}, {"end_turn": None}])
def test_bridge_rejects_untyped_provider_turn_flags(flags):
    with pytest.raises(AgentModelResponseError):
        AgentModelBridge._response({**result(text="fixture"), **flags}, {"source": "local_service"}, [], {})


def test_bridge_stream_delivers_complete_tool_before_adapter_returns():
    events = []
    messages = [{"role": "developer", "content": "base"}, {"role": "developer", "content": "host"},
                {"role": "user", "content": "project"}]
    def chat(body, *, task_id, timeout_s, on_event):
        assert body["stream"] and [m["role"] for m in body["messages"]] == ["system", "system", "user"]
        on_event({"choices": [{"delta": {"tool_calls": [{"index": 0, **call(name="not_loaded")} ]}}]})
        assert "tool_call_complete" not in kinds(events)
        on_event({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        assert kinds(events) == ["tool_call_complete"]
        return result([call(name="not_loaded")])
    bridge, manager = bridge_with(chat)
    response = bridge.complete("fixture", messages, [], lambda: False, events.append)
    assert kinds(events) == ["tool_call_complete", "response_complete"]
    assert events[-1]["message"] == response["message"]
    assert response["usage"] == {"input_tokens": 0, "output_tokens": 0} and manager._running == 0


def test_nonstream_result_emits_complete_items_without_invented_text_deltas():
    events = []
    def chat(body, *, task_id, timeout_s, on_event):
        assert on_event is None and [m["role"] for m in body["messages"]] == ["developer", "user"]
        return result([call()], text="Buffered")
    bridge, _ = bridge_with(chat, streaming=False, supports_developer=True)
    bridge.complete("fixture", [{"role": "developer", "content": "base"}, {"role": "user", "content": "project"}],
                    [], lambda: False, events.append)
    assert kinds(events) == ["tool_call_complete", "response_complete"]


def test_cancelled_stream_suppresses_late_tool_items_and_terminal_event():
    events, stopped = [], [False]
    def chat(body, *, task_id, timeout_s, on_event):
        on_event({"choices": [{"delta": {"content": "before"}}]})
        stopped[0] = True
        on_event({"type": "tool_call_complete", "index": 0, "call": call()})
        on_event({"choices": [{"delta": {"content": "PRIVATE_LATE"}}]})
        return result([call()])
    bridge, manager = bridge_with(chat)
    with pytest.raises(AgentModelResponseError, match="agent_cancelled"):
        bridge.complete("fixture", [{"role": "user", "content": "read"}], [], lambda: stopped[0], events.append)
    assert kinds(events) == ["text_delta"] and events[-1]["text"] == "before" and manager._running == 0


@pytest.mark.parametrize("adapter_type", [OpenAICompatibleAdapter, OllamaAdapter])
def test_http_adapter_tool_item_is_delivered_before_sse_usage_tail(monkeypatch, adapter_type):
    events, observed_before_usage = [], []
    chunks = [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **call()}]}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
              {"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}]
    payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    class Response(io.BytesIO):
        headers = {"content-type": "text/event-stream"}

        def readline(self, *args):
            line = super().readline(*args)
            if b'"usage"' in line:
                observed_before_usage.append("tool_call_complete" in kinds(events))
            return line

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(payload.encode()))
    adapter = adapter_type(base_url="http://127.0.0.1:1", model="fixture")
    bridge, _ = bridge_with(adapter.chat)
    response = bridge.complete("fixture", [{"role": "user", "content": "read"}], [], lambda: False, events.append)
    assert observed_before_usage == [True]
    assert kinds(events) == ["tool_call_complete", "response_complete"]
    assert response["usage"] == {"input_tokens": 0, "output_tokens": 0}
