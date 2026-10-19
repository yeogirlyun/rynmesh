from __future__ import annotations

import copy
import json

import pytest

from rynmesh.agent_runtime.model_events import ModelEventStream
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    StreamDecoder,
    make_request,
    parse_response,
)

SCOPE = "a" * 64
OTHER_SCOPE = "b" * 64
MODEL = "native-model"
TOOL = {"type": "function", "function": {"name": "weather", "description": "Find weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "units": {"type": "string"}}, "required": ["city"]}}}


def call(identity="call_1", arguments='{"city":"北京"}'):
    return {"id": identity, "type": "function", "function": {"name": "weather", "arguments": arguments}}


def response(*, tools=True, text="Checking", status="completed", usage=True):
    output = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "PRIVATE_ENCRYPTED"},
              {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": text, "annotations": []}]}]
    if tools:
        output.append({"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "weather", "arguments": '{"city":"北京"}', "status": "completed"})
    result = {"id": "resp_1", "status": status, "output": output, "error": None}
    if usage:
        result["usage"] = {"input_tokens": 10, "output_tokens": 5}
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


def message(*, tools=True, text="Checking", stop=None, usage=True):
    content = [{"type": "thinking", "thinking": "PRIVATE_THINKING", "signature": "PRIVATE_SIGNATURE"},
               {"type": "redacted_thinking", "data": "PRIVATE_REDACTED"}, {"type": "text", "text": text}]
    if tools:
        content.append({"type": "tool_use", "id": "call_1", "name": "weather", "input": {"city": "北京"}})
    result = {"id": "msg_1", "type": "message", "role": "assistant", "content": content,
              "stop_reason": stop or ("tool_use" if tools else "end_turn")}
    if usage:
        result["usage"] = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}
    return result


def body(history=None):
    return {"messages": history or [{"role": "user", "content": "天气？"}], "tools": [copy.deepcopy(TOOL)], "max_tokens": 600}


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_native_request_does_not_mutate_schema_or_history(protocol):
    source = body()
    original = copy.deepcopy(source)
    wire = make_request(source, protocol, MODEL, SCOPE)
    assert source == original
    wire["tools"][0]["parameters" if protocol == RESPONSES else "input_schema"]["required"].append("units")
    assert source == original
    if protocol == RESPONSES:
        assert wire["tools"][0]["strict"] is False
        assert wire["store"] is False and wire["include"] == ["reasoning.encrypted_content"]
        assert wire["max_output_tokens"] == 600
    else:
        assert wire["max_tokens"] == 600


def test_responses_preserves_explicit_strict_and_reasoning_effort():
    source = body()
    source["tools"][0]["function"]["strict"] = True
    source["reasoning_effort"] = "low"
    wire = make_request(source, RESPONSES, MODEL, SCOPE)
    assert wire["tools"][0]["strict"] is True
    assert wire["tools"][0]["parameters"] == TOOL["function"]["parameters"]
    assert wire["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("choice,expected", [("auto", "auto"), ("none", "none"), ("required", "any")])
def test_native_tool_choice(choice, expected):
    source = {**body(), "tool_choice": choice}
    assert make_request(source, RESPONSES, MODEL, SCOPE)["tool_choice"] == choice
    assert make_request(source, MESSAGES, MODEL, SCOPE)["tool_choice"] == {"type": expected}


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_named_tool_choice(protocol):
    wire = make_request({**body(), "tool_choice": {"type": "function", "function": {"name": "weather"}}}, protocol, MODEL, SCOPE)
    assert wire["tool_choice"] == {"type": "function" if protocol == RESPONSES else "tool", "name": "weather"}
    with pytest.raises(ValueError):
        make_request({**body(), "tool_choice": {"type": "function", "function": {"name": "missing"}}}, protocol, MODEL, SCOPE)


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_parallel_tool_ids_and_errors_round_trip(protocol):
    history = [{"role": "system", "content": "system"}, {"role": "developer", "content": "developer"},
               {"role": "user", "content": "check"},
               {"role": "assistant", "content": "Checking", "tool_calls": [call(), call("call_2")]},
               {"role": "tool", "tool_call_id": "call_1", "content": '{"error":"not_found"}', "is_error": True},
               {"role": "tool", "tool_call_id": "call_2", "content": "sunny"},
               {"role": "user", "content": "continue"}]
    wire = make_request(body(history), protocol, MODEL, SCOPE)
    if protocol == MESSAGES:
        assert wire["system"] == [{"type": "text", "text": "system"}, {"type": "text", "text": "developer"}]
        assert wire["messages"][-1] == {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": '{"error":"not_found"}', "is_error": True},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "sunny"}, {"type": "text", "text": "continue"}]}
    else:
        assert [item["call_id"] for item in wire["input"] if item.get("type") == "function_call"] == ["call_1", "call_2"]
        assert [item["call_id"] for item in wire["input"] if item.get("type") == "function_call_output"] == ["call_1", "call_2"]
        assert [item["role"] for item in wire["input"][:2]] == ["system", "developer"]


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
@pytest.mark.parametrize("history", [
    [{"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "orphan", "content": "x"}],
    [{"role": "assistant", "content": None, "tool_calls": [call()]}],
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "user", "content": "intervening"}],
])
def test_missing_or_orphan_tool_results_fail_before_transport(protocol, history):
    with pytest.raises(ValueError):
        make_request(body(history), protocol, MODEL, SCOPE)


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_inline_images_map_without_loss(protocol):
    source = body([{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA==", "detail": "high"}}]}])
    wire = make_request(source, protocol, MODEL, SCOPE)
    if protocol == RESPONSES:
        assert wire["input"][0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,eA==", "detail": "high"}
    else:
        assert wire["messages"][0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "eA=="}}
    source["messages"][0]["content"][1]["image_url"]["url"] = "https://secret.invalid/image"
    with pytest.raises(ValueError):
        make_request(source, protocol, MODEL, SCOPE)


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_native_output_private_continuation_is_scoped_and_checked(protocol):
    native = response() if protocol == RESPONSES else message()
    parsed = parse_response(native, protocol, MODEL, SCOPE)
    assistant = parsed["message"]
    assert assistant["content"] == "Checking" and assistant["tool_calls"][0] == call()
    assert "reasoning_content" not in assistant
    assert parsed["finish_reason"] == "tool_calls"
    assert parsed["usage"] == {"prompt_tokens": 10 if protocol == RESPONSES else 15, "completion_tokens": 5}
    history = [{"role": "user", "content": "check"}, assistant, {"role": "tool", "tool_call_id": "call_1", "content": "ok"}]
    wire = make_request(body(history), protocol, MODEL, SCOPE)
    serialized = json.dumps(wire)
    assert "PRIVATE_" in serialized
    if protocol == RESPONSES:
        assert wire["input"][1:-1] == native["output"]
    else:
        assert wire["messages"][1]["content"] == native["content"]
    # Whitespace/member-order normalization is harmless; changed values are not.
    assistant["tool_calls"][0]["function"]["arguments"] = '{ "city" : "北京" }'
    make_request(body(history), protocol, MODEL, SCOPE)
    assistant["tool_calls"][0]["function"]["arguments"] = '{"city":"上海"}'
    with pytest.raises(ValueError, match="no longer matches"):
        make_request(body(history), protocol, MODEL, SCOPE)
    assistant["tool_calls"][0]["function"]["arguments"] = call()["function"]["arguments"]
    assistant["content"] += " changed"
    with pytest.raises(ValueError, match="no longer matches"):
        make_request(body(history), protocol, MODEL, SCOPE)
    # Switching destination reuses only public canonical history.
    other = make_request(body(history), protocol, MODEL, OTHER_SCOPE)
    assert "PRIVATE_" not in json.dumps(other)


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_unknown_usage_does_not_become_zero(protocol):
    native = response(tools=False, usage=False) if protocol == RESPONSES else message(tools=False, usage=False)
    assert parse_response(native, protocol, MODEL, SCOPE)["usage"] == {"prompt_tokens": None, "completion_tokens": None}
    native["usage"] = {"input_tokens": 0, "output_tokens": 0}
    assert parse_response(native, protocol, MODEL, SCOPE)["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    native["usage"] = {"input_tokens": True, "output_tokens": -1}
    assert parse_response(native, protocol, MODEL, SCOPE)["usage"] == {"prompt_tokens": None, "completion_tokens": None}


@pytest.mark.parametrize("protocol,options", [
    (MESSAGES, {"thinking": {"type": "enabled", "budget_tokens": 2048}}),
    (MESSAGES, {"enable_thinking": True}), (RESPONSES, {"enable_thinking": True}),
    (MESSAGES, {"reasoning_effort": "low"}), (RESPONSES, {"stop": ["STOP"]}),
    (RESPONSES, {"unknown_feature": True}),
])
def test_unsupported_native_features_are_explicit(protocol, options):
    with pytest.raises(ValueError):
        make_request({**body(), **options}, protocol, MODEL, SCOPE)


@pytest.mark.parametrize("options", [{"thinking": False}, {"thinking": {"type": "disabled"}}, {"enable_thinking": False}])
def test_disabled_thinking_maps_to_native_messages(options):
    assert make_request({**body(), **options}, MESSAGES, MODEL, SCOPE)["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("protocol,upper", [(MESSAGES, 1), (RESPONSES, 2)])
def test_native_temperature_follows_protocol_range_without_coercion(protocol, upper):
    assert "temperature" not in make_request(body(), protocol, MODEL, SCOPE)
    for temperature in (0, .5, upper):
        assert make_request({**body(), "temperature": temperature}, protocol, MODEL, SCOPE)["temperature"] == temperature
    for temperature in (-.1, upper + .1, True, False, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="temperature"):
            make_request({**body(), "temperature": temperature}, protocol, MODEL, SCOPE)


@pytest.mark.parametrize("stop,finish", [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"), ("refusal", "content_filter")])
def test_messages_safe_stop_reasons(stop, finish):
    assert parse_response(message(tools=False, stop=stop), MESSAGES, MODEL, SCOPE)["finish_reason"] == finish


@pytest.mark.parametrize("stop", [None, "pause_turn", "model_context_window_exceeded", "unknown"])
def test_messages_unknown_or_paused_stop_is_not_success(stop):
    native = message(tools=False)
    native["stop_reason"] = stop
    with pytest.raises(ValueError):
        parse_response(native, MESSAGES, MODEL, SCOPE)


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_truncated_response_cannot_propose_executable_tools(protocol):
    native = response(status="incomplete") if protocol == RESPONSES else message(stop="max_tokens")
    with pytest.raises(ValueError, match="safe tool completion"):
        parse_response(native, protocol, MODEL, SCOPE)


def response_stream(native=None):
    native = native or response()
    return [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": native["output"][0]},
        {"type": "response.output_item.done", "output_index": 0, "item": native["output"][0]},
        {"type": "response.output_item.added", "output_index": 1, "item": {**native["output"][1], "content": [], "status": "in_progress"}},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "Check"},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "ing"},
        {"type": "response.output_item.done", "output_index": 1, "item": native["output"][1]},
        {"type": "response.output_item.added", "output_index": 2, "item": {**native["output"][2], "arguments": "", "status": "in_progress"}},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_1", "delta": '{"city":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_1", "delta": '"北京"}'},
        {"type": "response.function_call_arguments.done", "output_index": 2, "item_id": "fc_1", "arguments": '{"city":"北京"}'},
        {"type": "response.output_item.done", "output_index": 2, "item": native["output"][2]},
        {"type": "response.completed", "response": native},
    ]


def message_stream(native=None):
    native = native or message()
    return [
        {"type": "message_start", "message": {"id": native["id"], "type": "message", "role": "assistant", "content": [],
            "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "PRIVATE_THINKING"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "PRIVATE_SIGNATURE"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": native["content"][1]},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "Check"}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "ing"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "call_1", "name": "weather", "input": {}}},
        {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
        {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '"北京"}'}},
        {"type": "content_block_stop", "index": 3},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("protocol,events,native", [(RESPONSES, response_stream, response), (MESSAGES, message_stream, message)])
def test_native_stream_preserves_private_state_and_does_not_complete_tools_early(protocol, events, native):
    decoder = StreamDecoder(protocol, MODEL, SCOPE)
    observer_events = []
    bridge = ModelEventStream(observer_events.append)
    wire_events = events()
    for event in wire_events[:-1]:
        for chunk in decoder.feed(event):
            assert "PRIVATE_" not in json.dumps(chunk)
            bridge.feed(chunk)
    assert not any(event["type"] == "tool_call_complete" for event in observer_events)
    with pytest.raises(ValueError, match="before completion"):
        decoder.result()
    for chunk in decoder.feed(wire_events[-1]):
        assert "PRIVATE_" not in json.dumps(chunk)
        bridge.feed(chunk)
    final = decoder.result()
    assert final == parse_response(native(), protocol, MODEL, SCOPE)
    bridge.finish(final)
    assert [event["call"] for event in observer_events if event["type"] == "tool_call_complete"] == [call()]
    assert "".join(event["text"] for event in observer_events if event["type"] == "text_delta") == "Checking"
    assert decoder.usage == final["usage"] and decoder.finish_reason == "tool_calls"
    with pytest.raises(ValueError, match="after termination"):
        decoder.feed(wire_events[-1])


@pytest.mark.parametrize("protocol,events", [(RESPONSES, response_stream), (MESSAGES, message_stream)])
def test_stream_error_retains_usage_without_leaking_raw_error(protocol, events):
    decoder = StreamDecoder(protocol, MODEL, SCOPE)
    for event in events()[:1]:
        decoder.feed(event)
    if protocol == RESPONSES:
        decoder.feed({"type": "response.in_progress", "response": {"id": "resp_1", "usage": {"input_tokens": 10, "output_tokens": 1}}})
    with pytest.raises(ValueError) as error:
        decoder.feed({"type": "error", "error": {"message": "PRIVATE_API_KEY"}})
    assert "PRIVATE_API_KEY" not in str(error.value)
    assert decoder.usage["prompt_tokens"] == (10 if protocol == RESPONSES else 15)
    with pytest.raises(ValueError):
        decoder.result()


@pytest.mark.parametrize("protocol,events", [(RESPONSES, response_stream), (MESSAGES, message_stream)])
def test_stream_truncated_tool_never_reaches_model_executor(protocol, events):
    decoder = StreamDecoder(protocol, MODEL, SCOPE)
    chunks = []
    stream = events()
    if protocol == RESPONSES:
        terminal = response(status="incomplete")
        stream[-1] = {"type": "response.incomplete", "response": terminal}
    else:
        stream[-2]["delta"]["stop_reason"] = "max_tokens"
    for event in stream[:-1]:
        chunks += decoder.feed(event)
    with pytest.raises(ValueError):
        decoder.feed(stream[-1])
    assert not any(choice.get("delta", {}).get("tool_calls") for chunk in chunks for choice in chunk.get("choices", []))
    assert decoder.finish_reason == "length"
    assert decoder.usage["completion_tokens"] == 5


def test_responses_terminal_can_supply_authoritative_buffered_output():
    decoder = StreamDecoder(RESPONSES, MODEL, SCOPE)
    chunks = decoder.feed({"type": "response.completed", "response": response()})
    assert "".join(choice.get("delta", {}).get("content", "") for chunk in chunks for choice in chunk.get("choices", [])) == "Checking"
    assert decoder.result()["message"]["tool_calls"] == [call()]


@pytest.mark.parametrize("change", ["text", "arguments", "id"])
def test_responses_final_cannot_change_streamed_public_projection(change):
    decoder = StreamDecoder(RESPONSES, MODEL, SCOPE)
    events = response_stream()
    for event in events[:-1]:
        decoder.feed(event)
    final = events[-1]
    if change == "text":
        final["response"]["output"][1]["content"][0]["text"] = "Changed"
    elif change == "arguments":
        final["response"]["output"][2]["arguments"] = '{"city":"上海"}'
    else:
        final["response"]["output"][2]["call_id"] = "other"
    with pytest.raises(ValueError):
        decoder.feed(final)


@pytest.mark.parametrize("protocol,native", [(RESPONSES, response), (MESSAGES, message)])
def test_server_tools_are_rejected_instead_of_dropped(protocol, native):
    raw = native(tools=False)
    raw["output" if protocol == RESPONSES else "content"].append({"type": "web_search_call" if protocol == RESPONSES else "server_tool_use", "id": "server_1"})
    with pytest.raises(ValueError, match="unsupported"):
        parse_response(raw, protocol, MODEL, SCOPE)
