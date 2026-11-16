"""S01-I7 regression tests: relaxed native/chat stream decoding, early stop on terminal event, trailing ping, continuation fallback, and truncated tool stripping."""
from __future__ import annotations

import copy
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.agent_runtime.model_events import ModelEventStream
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    StreamDecoder,
    make_request,
    parse_response,
)

SCOPE = "a" * 64
MODEL = "native-model"
TOOL = {
    "type": "function",
    "function": {
        "name": "weather",
        "description": "Find weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _call(identity: str = "call_1", arguments: str = '{"city":"北京"}') -> dict:
    return {"id": identity, "type": "function", "function": {"name": "weather", "arguments": arguments}}


def _body(history=None) -> dict:
    return {
        "messages": history or [{"role": "user", "content": "天气？"}],
        "tools": [copy.deepcopy(TOOL)],
        "max_tokens": 600,
    }


def test_t1_unknown_types_at_every_original_raise_point_are_ignored_with_diagnostics():
    """T1 (Changes 1, 2, 3, 10): Feeding unknown types at every former raise point succeeds, preserves text/tools, strips unknown continuation items, and records ignored_stream_events."""
    # 1) Responses stream: unknown output_item, unknown content_part, unknown stream event, and final unknown item/part
    resp_final = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "PRIVATE_ENCRYPTED"},
            {"type": "web_search_call", "id": "ws_1", "status": "completed", "query": "beijing weather"},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Checking"},
                    {"type": "audio_transcript", "transcript": "ignored"},
                ],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "weather",
                "arguments": '{"city":"北京"}',
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp_events = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": resp_final["output"][0]},
        {"type": "response.output_item.done", "output_index": 0, "item": resp_final["output"][0]},
        {"type": "response.output_item.added", "output_index": 1, "item": {"type": "web_search_call", "id": "ws_1"}},
        {"type": "response.web_search_call.searching", "output_index": 1, "item_id": "ws_1"},
        {"type": "response.output_item.done", "output_index": 1, "item": resp_final["output"][1]},
        {"type": "response.output_item.added", "output_index": 2, "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": [], "status": "in_progress"}},
        {"type": "response.content_part.added", "output_index": 2, "content_index": 0, "part": {"type": "output_text", "text": ""}},
        {"type": "response.output_text.delta", "output_index": 2, "content_index": 0, "delta": "Checking"},
        {"type": "response.content_part.done", "output_index": 2, "content_index": 0, "part": {"type": "output_text", "text": "Checking"}},
        {"type": "response.content_part.added", "output_index": 2, "content_index": 1, "part": {"type": "audio_transcript", "transcript": "ignored"}},
        {"type": "response.content_part.done", "output_index": 2, "content_index": 1, "part": {"type": "audio_transcript", "transcript": "ignored"}},
        {"type": "response.output_item.done", "output_index": 2, "item": resp_final["output"][2]},
        {"type": "response.output_item.added", "output_index": 3, "item": {**resp_final["output"][3], "arguments": "", "status": "in_progress"}},
        {"type": "response.function_call_arguments.delta", "output_index": 3, "item_id": "fc_1", "delta": '{"city":"北京"}'},
        {"type": "response.function_call_arguments.done", "output_index": 3, "item_id": "fc_1", "arguments": '{"city":"北京"}'},
        {"type": "response.output_item.done", "output_index": 3, "item": resp_final["output"][3]},
        {"type": "response.custom_vendor_trace", "trace_id": "t_1"},
        {"type": "response.completed", "response": resp_final},
    ]
    r_dec = StreamDecoder(RESPONSES, MODEL, SCOPE)
    for ev in resp_events:
        r_dec.feed(ev)
    r_res = r_dec.result()
    assert r_res["message"]["content"] == "Checking"
    assert r_res["message"]["tool_calls"] == [_call()]
    assert r_dec.ignored_events == 6
    assert r_res["diagnostics"]["ignored_stream_events"] == 6

    # Continuation must keep reasoning/message/function_call and strip web_search_call + audio_transcript
    r_wire = make_request(
        _body([
            {"role": "user", "content": "q"},
            r_res["message"],
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]),
        RESPONSES,
        MODEL,
        SCOPE,
    )
    r_wire_json = json.dumps(r_wire)
    assert "PRIVATE_ENCRYPTED" in r_wire_json
    assert "web_search_call" not in r_wire_json
    assert "audio_transcript" not in r_wire_json

    # 2) Messages stream & parse_response: unknown block, unknown delta on text block, unknown top-level event
    m_dec = StreamDecoder(MESSAGES, MODEL, SCOPE)
    msg_events = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 8, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "mcp_tool_use", "id": "mcp_1", "name": "remote"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Checking"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "vendor_metadata_delta", "meta": "x"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call_1", "name": "weather", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"city":"北京"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "vendor_heartbeat", "seq": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    for ev in msg_events:
        m_dec.feed(ev)
    m_res = m_dec.result()
    assert m_res["message"]["content"] == "Checking"
    assert m_res["message"]["tool_calls"] == [_call()]
    assert m_dec.ignored_events == 5
    assert m_res["diagnostics"]["ignored_stream_events"] == 5

    # _from_messages + _continuation on raw response containing unknown block
    raw_msg = {
        "id": "msg_raw",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "PRIVATE_THINKING", "signature": "SIG"},
            {"type": "mcp_tool_result", "tool_use_id": "mcp_1", "content": "secret"},
            {"type": "text", "text": "Checking"},
            {"type": "tool_use", "id": "call_1", "name": "weather", "input": {"city": "北京"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 8, "output_tokens": 5},
    }
    parsed_msg = parse_response(raw_msg, MESSAGES, MODEL, SCOPE)
    assert parsed_msg["message"]["content"] == "Checking"
    assert parsed_msg["message"]["tool_calls"] == [_call()]
    m_wire = make_request(
        _body([
            {"role": "user", "content": "q"},
            parsed_msg["message"],
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]),
        MESSAGES,
        MODEL,
        SCOPE,
    )
    m_wire_json = json.dumps(m_wire)
    assert "PRIVATE_THINKING" in m_wire_json
    assert "mcp_tool_result" not in m_wire_json


def test_t2_messages_stream_with_server_tool_use_and_web_search_result_blocks():
    """T2 (Changes 2, 3, 10): Messages stream with server_tool_use and web_search_tool_result ignores their deltas/stops and passes message_stop integrity check."""
    decoder = StreamDecoder(MESSAGES, MODEL, SCOPE)
    events = [
        {"type": "message_start", "message": {"id": "msg_ws", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 12, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query":"beijing"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [{"type": "web_search_result", "url": "https://example.com"}]}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "晴，22℃"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]
    chunks = []
    for ev in events:
        chunks.extend(decoder.feed(ev))
    res = decoder.result()
    assert res["message"]["content"] == "晴，22℃"
    assert "tool_calls" not in res["message"]
    assert res["finish_reason"] == "stop"
    assert res["diagnostics"]["ignored_stream_events"] == 5


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_t3_ping_and_empty_frame_after_completed_do_not_error(protocol):
    """T3 (Change 5): After _final is set, ping or {} returns [] without error, while non-ping events still raise."""
    decoder = StreamDecoder(protocol, MODEL, SCOPE)
    if protocol == RESPONSES:
        decoder.feed({
            "type": "response.completed",
            "response": {
                "id": "resp_p",
                "status": "completed",
                "output": [{"type": "message", "id": "m1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Done"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        })
    else:
        for ev in [
            {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 3, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Done"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]:
            decoder.feed(ev)

    assert decoder.feed({"type": "ping"}) == []
    assert decoder.feed({}) == []
    assert decoder.result()["message"]["content"] == "Done"
    with pytest.raises(ValueError, match="after termination"):
        decoder.feed({"type": "late_real_event", "x": 1})


def test_t4_incomplete_interrupted_preserves_partial_text_and_errors_when_empty():
    """T4 (Change 6): response.incomplete with reason=interrupted (or other) returns finish_reason=length and preserves partial text without dispatching tools, and raises when empty."""
    raw_with_text = {
        "id": "resp_int",
        "status": "incomplete",
        "incomplete_details": {"reason": "interrupted"},
        "output": [
            {
                "type": "message",
                "id": "msg_int",
                "role": "assistant",
                "status": "incomplete",
                "content": [{"type": "output_text", "text": "Partial output before interrupt"}],
            },
            {
                "type": "function_call",
                "id": "fc_int",
                "call_id": "call_int",
                "name": "weather",
                "arguments": '{"city":"北',
                "status": "in_progress",
            },
        ],
        "usage": {"input_tokens": 6, "output_tokens": 4},
    }
    parsed = parse_response(raw_with_text, RESPONSES, MODEL, SCOPE)
    assert parsed["finish_reason"] == "length"
    assert parsed["message"]["content"] == "Partial output before interrupt"
    assert "tool_calls" not in parsed["message"]

    # StreamDecoder + ModelEventStream path
    decoder = StreamDecoder(RESPONSES, MODEL, SCOPE)
    emitted = []
    stream_bridge = ModelEventStream(emitted.append)
    for ev in [
        {"type": "response.created", "response": {"id": "resp_int", "status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": "Partial output "},
        {"type": "response.incomplete", "response": raw_with_text},
    ]:
        for chunk in decoder.feed(ev):
            stream_bridge.feed(chunk)
    final = decoder.result()
    stream_bridge.finish(final)
    assert final["finish_reason"] == "length"
    assert final["message"]["content"] == "Partial output before interrupt"
    assert not any(e["type"] == "tool_call_complete" for e in emitted)

    # Empty incomplete=interrupted still raises ValueError
    raw_empty = {
        "id": "resp_empty",
        "status": "incomplete",
        "incomplete_details": {"reason": "interrupted"},
        "output": [],
    }
    with pytest.raises(ValueError):
        parse_response(raw_empty, RESPONSES, MODEL, SCOPE)


@contextmanager
def _hanging_sse_server(frames: list[str], hold_open: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for raw_frame in frames:
                    self.wfile.write(raw_frame.encode("utf-8"))
                    self.wfile.flush()
                hold_open.wait(timeout=5.0)
            except OSError:
                return

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        hold_open.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_t5_native_sse_returns_immediately_after_completed_without_socket_eof():
    """T5 (Changes 7, 10): NativeProtocolAdapter stops reading as soon as decoder._final is set without waiting for socket EOF, and skips <3 bad JSON frames while counting ignored_stream_events."""
    hold_open = threading.Event()
    completed_payload = {
        "type": "response.completed",
        "response": {
            "id": "resp_live",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_live",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "fast return"}],
                }
            ],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
    }
    frames = [
        'data: {"type":"response.created","response":{"id":"resp_live","status":"in_progress"}}\n\n',
        "data: {not-valid-json-frame\n\n",
        'data: {"type":"response.vendor_custom_event","step":1}\n\n',
        f"data: {json.dumps(completed_payload)}\n\n",
    ]
    with _hanging_sse_server(frames, hold_open) as base_url:
        adapter = NativeProtocolAdapter(
            protocol="openai_responses",
            base_url=base_url,
            api_prefix="",
            api_key="tok",
            model="test-model",
            timeout_s=0.8,
        )
        t0 = time.monotonic()
        result = adapter.chat(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            task_id="t5-live",
            timeout_s=0.8,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 0.6
        assert not hold_open.is_set()
        assert result["text"] == "fast return"
        assert result["diagnostics"]["ignored_stream_events"] == 2

    # 3 consecutive bad JSON frames must still fail with source_protocol_response_invalid
    hold_bad = threading.Event()
    hold_bad.set()
    bad_frames = [
        "data: {bad1\n\n",
        "data: {bad2\n\n",
        "data: {bad3\n\n",
        f"data: {json.dumps(completed_payload)}\n\n",
    ]
    with _hanging_sse_server(bad_frames, hold_bad) as base_url:
        adapter = NativeProtocolAdapter(
            protocol="openai_responses",
            base_url=base_url,
            api_prefix="",
            api_key="tok",
            model="test-model",
        )
        with pytest.raises(AdapterError) as caught:
            adapter.chat(
                {"messages": [{"role": "user", "content": "hi"}], "stream": True},
                task_id="t5-bad3",
                timeout_s=2.0,
            )
        assert caught.value.code == "source_protocol_response_invalid"


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_t6_mismatched_or_malformed_provider_state_falls_back_to_public_projection_with_diagnostic_count(protocol):
    """T6 (Change 4): When assistant provider_state disagrees with public content/calls or has invalid shape, make_request falls back to public projection without error and increments diagnostics.provider_state_discarded."""
    if protocol == RESPONSES:
        raw = {
            "id": "resp_c",
            "status": "completed",
            "output": [
                {"type": "reasoning", "id": "rs_c", "summary": [], "encrypted_content": "PRIVATE_CONTINUATION_SECRET"},
                {"type": "message", "id": "m_c", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Original"}]},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
    else:
        raw = {
            "id": "msg_c",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "PRIVATE_CONTINUATION_SECRET", "signature": "SIG_1"},
                {"type": "text", "text": "Original"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }

    parsed = parse_response(raw, protocol, MODEL, SCOPE)
    assistant_msg = parsed["message"]
    assistant_msg["content"] = "Edited public content"

    diagnostics: dict[str, int] = {}
    wire = make_request(
        _body([
            {"role": "user", "content": "hello"},
            assistant_msg,
            {"role": "user", "content": "follow up"},
        ]),
        protocol,
        MODEL,
        SCOPE,
        diagnostics=diagnostics,
    )
    serialized = json.dumps(wire)
    assert "PRIVATE_CONTINUATION_SECRET" not in serialized
    assert "Edited public content" in serialized
    assert diagnostics == {"provider_state_discarded": 1}

    # Invalid provider_state.data shape also falls back cleanly and increments count
    malformed_msg = {
        "role": "assistant",
        "content": "Second reply",
        "provider_state": {"protocol": protocol, "scope": SCOPE, "data": {"unexpected_key": []}},
    }
    diag2: dict[str, int] = {}
    wire2 = make_request(
        _body([
            {"role": "user", "content": "hello"},
            malformed_msg,
            {"role": "user", "content": "again"},
        ]),
        protocol,
        MODEL,
        SCOPE,
        diagnostics=diag2,
    )
    assert "Second reply" in json.dumps(wire2)
    assert diag2 == {"provider_state_discarded": 1}


def test_t7_chat_sse_skips_isolated_bad_json_lines_and_fails_on_three_consecutive_or_truncated():
    """T7 (Changes 8, 10): OpenAICompatibleAdapter.chat skips <3 malformed JSON SSE data lines, counts ignored_stream_events, fails on 3 consecutive bad lines, and still checks accumulator.finished."""
    scenario = ["one_bad"]

    class ChatSSEHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            cur = scenario[0]
            if cur == "one_bad":
                lines = [
                    'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello "},"finish_reason":null}]}\n\n',
                    "data: <<<malformed-proxy-keepalive>>>\n\n",
                    'data: {"choices":[{"index":0,"delta":{"content":"world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n',
                    "data: [DONE]\n\n",
                ]
            elif cur == "three_bad":
                lines = [
                    "data: bad1\n\n",
                    "data: bad2\n\n",
                    "data: bad3\n\n",
                    'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
                    "data: [DONE]\n\n",
                ]
            else:
                lines = [
                    'data: {"choices":[{"index":0,"delta":{"content":"cut"},"finish_reason":null}]}\n\n',
                    'data: {"choices":[{"index":0,"delta":{"content":"half\n\n',
                ]
            for item in lines:
                self.wfile.write(item.encode("utf-8"))
            self.wfile.flush()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ChatSSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        adapter = OpenAICompatibleAdapter(base_url=url, api_prefix="", api_key="k", model="chat-m")
        seen: list[dict] = []
        res = adapter.chat(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            task_id="chat-ok",
            timeout_s=3.0,
            on_event=seen.append,
        )
        assert res["text"] == "Hello world"
        assert res["finish_reason"] == "stop"
        assert res["diagnostics"]["ignored_stream_events"] == 1

        scenario[0] = "three_bad"
        with pytest.raises(AdapterError) as err3:
            adapter.chat(
                {"messages": [{"role": "user", "content": "hi"}], "stream": True},
                task_id="chat-3bad",
                timeout_s=3.0,
            )
        assert err3.value.code == "upstream_response_invalid"

        scenario[0] = "truncated_after_bad"
        with pytest.raises(AdapterError) as err_trunc:
            adapter.chat(
                {"messages": [{"role": "user", "content": "hi"}], "stream": True},
                task_id="chat-trunc",
                timeout_s=3.0,
            )
        assert err_trunc.value.code == "upstream_stream_truncated"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_t8_max_output_tokens_truncation_with_complete_and_incomplete_calls_keeps_text_drops_calls_and_next_request_succeeds(protocol):
    """T8 (Change 9): max_output_tokens / max_tokens truncation with one completed call and one unfinished call preserves text, drops all tool_calls, sets finish_reason=length, strips calls from provider_state.data, and allows next make_request without missing native tool results."""
    if protocol == RESPONSES:
        raw = {
            "id": "resp_len",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {"type": "reasoning", "id": "rs_len", "summary": [], "encrypted_content": "PRIVATE_LEN_ENC"},
                {
                    "type": "message",
                    "id": "msg_len",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": "Drafting big file in steps"}],
                },
                {
                    "type": "function_call",
                    "id": "fc_done",
                    "call_id": "call_done",
                    "name": "weather",
                    "arguments": '{"city":"北京"}',
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "fc_cut",
                    "call_id": "call_cut",
                    "name": "weather",
                    "arguments": '{"city":"上',
                    "status": "in_progress",
                },
            ],
            "usage": {"input_tokens": 15, "output_tokens": 600},
        }
        parsed = parse_response(raw, RESPONSES, MODEL, SCOPE)
    else:
        # Exercise StreamDecoder for Messages with 1 completed tool_use + 1 truncated partial_json tool_use at max_tokens
        decoder = StreamDecoder(MESSAGES, MODEL, SCOPE)
        events = [
            {"type": "message_start", "message": {"id": "msg_len", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 15, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "PRIVATE_LEN_ENC", "signature": "SIG_L"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "Drafting big file in steps"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call_done", "name": "weather", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"city":"北京"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "call_cut", "name": "weather", "input": {}}},
            {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '{"city":"上'}},
            {"type": "content_block_stop", "index": 3},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None}, "usage": {"output_tokens": 600}},
            {"type": "message_stop"},
        ]
        for ev in events:
            decoder.feed(ev)
        parsed = decoder.result()

    assert parsed["finish_reason"] == "length"
    assert parsed["message"]["content"] == "Drafting big file in steps"
    assert "tool_calls" not in parsed["message"]

    state_items = parsed["message"]["provider_state"]["data"]["output" if protocol == RESPONSES else "content"]
    assert not any(item.get("type") in {"function_call", "tool_use"} for item in state_items)

    # Next make_request with no tool results must succeed without "missing native tool results"
    next_wire = make_request(
        _body([
            {"role": "user", "content": "start"},
            parsed["message"],
            {"role": "user", "content": "continue"},
        ]),
        protocol,
        MODEL,
        SCOPE,
    )
    assert "PRIVATE_LEN_ENC" in json.dumps(next_wire)
