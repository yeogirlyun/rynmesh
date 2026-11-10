import json

import pytest

from rynmesh.llm_package.api_formats import StreamFormat, convert_response
from rynmesh.llm_package.chat import completion


def final(message=None, reason="stop"):
    return completion({"message": message or {"role": "assistant", "content": "buffered answer"},
                       "finish_reason": reason, "reported_usage": {"input_tokens": 1, "output_tokens": 2}}, "test", "one")


def payloads(events):
    return [json.loads(event.split("data: ", 1)[1]) for event in events if "[DONE]" not in event]


@pytest.mark.parametrize("protocol", ["chat", "messages", "responses"])
def test_buffered_upstream_answer_survives_stream_projection(protocol):
    formatter = StreamFormat(protocol, "test", "one")
    values = payloads(formatter.finish(final()))
    assert "buffered answer" in json.dumps(values)
    if protocol == "responses":
        assert values[-1]["response"]["output"][0]["content"][0]["text"] == "buffered answer"


@pytest.mark.parametrize("protocol", ["messages", "responses"])
def test_split_tool_name_late_id_and_buffered_final_are_preserved(protocol):
    formatter = StreamFormat(protocol, "test", "one")
    # Starting a target tool block with 'we' would permanently misname it.
    assert formatter.delta({"choices": [{"delta": {"tool_calls": [{"index": 0,
        "function": {"name": "we", "arguments": "{"}}]}}]}) == []
    assert formatter.delta({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
        "function": {"name": "ather", "arguments": "}"}}]}}]}) == []
    formatter.delta({"choices": [{"delta": {"content": "Checking"}}]})
    message = {"role": "assistant", "content": "Checking", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}]}
    values = payloads(formatter.finish(final(message, "tool_calls")))
    if protocol == "messages":
        starts = [value["content_block"] for value in values if value["type"] == "content_block_start"]
        assert starts[0]["name"] == "weather"
        assert starts[1]["type"] == "text"
    else:
        assert [value["type"] for value in values[-1]["response"]["output"]] == ["function_call", "message"]
        assert values[-1]["response"]["output"][0]["name"] == "weather"


@pytest.mark.parametrize("protocol", ["messages", "responses"])
def test_missing_streamed_final_suffix_is_emitted_once(protocol):
    formatter = StreamFormat(protocol, "test", "one")
    before = formatter.delta({"choices": [{"delta": {"content": "buffered "}}]})
    after = formatter.finish(final())
    values = payloads([*before, *after])
    if protocol == "messages":
        pieces = [v["delta"]["text"] for v in values if v["type"] == "content_block_delta"]
    else:
        pieces = [v["delta"] for v in values if v["type"] == "response.output_text.delta"]
    assert "".join(pieces) == "buffered answer"


def test_truncation_is_not_a_successful_tool_turn():
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}]}
    assert convert_response(final(message, "length"), "messages")["stop_reason"] == "max_tokens"
    assert convert_response(final(message, "content_filter"), "responses")["status"] == "incomplete"
