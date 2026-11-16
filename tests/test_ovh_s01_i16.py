"""S01-I16: Compatible tool-call ID normalization, missing index handling, same-index ID split, and StreamFormat alignment."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
from email import message_from_string
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.model_events import (
    ModelEventError,
    ModelEventStream,
    response_calls,
)
from rynmesh.llm_package import chat as chat_mod
from rynmesh.llm_package.adapters import OpenAICompatibleAdapter
from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.api_formats import StreamFormat
from rynmesh.llm_package.chat import ChatAccumulator, completion
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    StreamDecoder,
    _call,
    make_request,
    parse_response,
)

SCOPE = "a" * 64
OTHER_SCOPE = "b" * 64
MODEL = "native-model"
TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _sse_response(chunks: list[dict]) -> io.BytesIO:
    payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    class _Resp(io.BytesIO):
        headers = message_from_string("Content-Type: text/event-stream\r\n")

    return _Resp(payload.encode("utf-8"))


def _json_response(body: dict) -> io.BytesIO:
    payload = json.dumps(body).encode("utf-8")

    class _Resp(io.BytesIO):
        headers = message_from_string("Content-Type: application/json\r\n")

    return _Resp(payload)


def _bridge_for_adapter(adapter: OpenAICompatibleAdapter, *, streaming: bool = True) -> AgentModelBridge:
    target = {
        "source": "local_service",
        "service_id": "fixture",
        "streaming": streaming,
        "adapter": "fixture",
        "max_output_tokens": 512,
        "context_window": 32768,
    }
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=SimpleNamespace(timeout_seconds=10),
        _slots=threading.BoundedSemaphore(1),
        _lock=threading.Lock(),
        _running=0,
    )
    return AgentModelBridge({"provider_for": lambda _: manager}, SimpleNamespace(resolve=lambda _: target))


def test_t1_normalize_call_id_and_compatible_single_tool_variants(monkeypatch) -> None:
    """T1: functions.write_file:0 id, empty id, and missing-index single-tool fragments succeed on first attempt."""
    normalize_call_id = getattr(chat_mod, "normalize_call_id", None)
    assert callable(normalize_call_id), "normalize_call_id must be defined in rynmesh.llm_package.chat"

    expected_colon_id = "call_" + hashlib.sha256(b"functions.write_file:0").hexdigest()[:24]
    assert normalize_call_id("call_valid-1", 0, "nonce123") == "call_valid-1"
    assert normalize_call_id("functions.write_file:0", 0, "nonce123") == expected_colon_id
    assert normalize_call_id("", 2, "nonce123") == "call_nonce123_2"
    assert normalize_call_id(None, 1, "nonce123") == "call_nonce123_1"

    # Variant A: Streaming with colon/dot ID AND missing index across multiple fragments
    chunks_colon_no_index = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "functions.write_file:0",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "function": {"arguments": '"a.txt"}'},
                            }
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 3}},
    ]
    open_calls = 0

    def open_colon(*_args, **_kwargs):
        nonlocal open_calls
        open_calls += 1
        return _sse_response(chunks_colon_no_index)

    monkeypatch.setattr("urllib.request.urlopen", open_colon)
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="fixture")
    bridge = _bridge_for_adapter(adapter, streaming=True)
    events_a: list[dict] = []
    resp_a = bridge.complete("fixture", [{"role": "user", "content": "write"}], [TOOL], lambda: False, events_a.append)
    assert open_calls == 1
    assert resp_a["message"]["tool_calls"] == [
        {
            "id": expected_colon_id,
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"a.txt"}'},
        }
    ]

    # Variant B: Streaming with empty ID ("") and missing index
    chunks_empty_id = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "function": {"arguments": '"b.txt"}'},
                            }
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _sse_response(chunks_empty_id))
    events_b: list[dict] = []
    resp_b = bridge.complete("fixture", [{"role": "user", "content": "write"}], [TOOL], lambda: False, events_b.append)
    gen_id = resp_b["message"]["tool_calls"][0]["id"]
    assert gen_id.startswith("call_") and gen_id.endswith("_0")
    assert resp_b["message"]["tool_calls"][0]["function"] == {"name": "write_file", "arguments": '{"path":"b.txt"}'}

    # Variant C: Missing-index rules in ChatAccumulator (match by id; fallback to 0 when multiple calls exist)
    acc = ChatAccumulator()
    acc.add(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_zero", "type": "function", "function": {"name": "write_file", "arguments": '{"a":'}},
                            {"index": 1, "id": "functions.read:1", "type": "function", "function": {"name": "read_file", "arguments": '{"b":'}},
                        ]
                    },
                }
            ]
        }
    )
    # Missing index WITH id matches slot 1; missing index WITHOUT id when 2 calls exist falls back to index 0
    acc.add(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"id": "functions.read:1", "function": {"arguments": "2}"}},
                            {"function": {"arguments": "1}"}},
                        ]
                    },
                }
            ]
        }
    )
    msg_c = acc.message()
    assert msg_c["tool_calls"] == [
        {"id": "call_zero", "type": "function", "function": {"name": "write_file", "arguments": '{"a":1}'}},
        {
            "id": "call_" + hashlib.sha256(b"functions.read:1").hexdigest()[:24],
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"b":2}'},
        },
    ]


def test_t2_streamed_and_final_call_ids_match_and_nonstream_branches_normalize(monkeypatch) -> None:
    """T2: Streamed tool_call_complete IDs match final response IDs (both hashed and nonce-generated), plus non-SSE JSON branches."""
    expected_colon_id = "call_" + hashlib.sha256(b"functions.write_file:0").hexdigest()[:24]
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "functions.write_file:0",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":"x.txt"}'},
                            },
                            {
                                "index": 1,
                                "id": "",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":"y.txt"}'},
                            },
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _sse_response(chunks))
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="fixture")
    bridge = _bridge_for_adapter(adapter, streaming=True)
    events: list[dict] = []
    final_resp = bridge.complete("fixture", [{"role": "user", "content": "write both"}], [TOOL], lambda: False, events.append)

    streamed_calls = [ev["call"] for ev in events if ev["type"] == "tool_call_complete"]
    final_calls = final_resp["message"]["tool_calls"]
    assert len(streamed_calls) == 2
    assert streamed_calls == final_calls
    assert final_calls[0]["id"] == expected_colon_id
    assert final_calls[1]["id"].startswith("call_") and final_calls[1]["id"].endswith("_1")

    # Non-streaming branch (stream=False) and buffered JSON branch under stream=True (not is_sse)
    buffered_payload = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "functions.write_file:0",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":"nonstream.txt"}'},
                        },
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":"empty.txt"}'},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4},
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _json_response(buffered_payload))

    # 1) stream=True but server returns application/json (not is_sse)
    events_buffered: list[dict] = []
    resp_buffered = bridge.complete(
        "fixture", [{"role": "user", "content": "write"}], [TOOL], lambda: False, events_buffered.append
    )
    assert resp_buffered["message"]["tool_calls"][0]["id"] == expected_colon_id
    assert resp_buffered["message"]["tool_calls"][1]["id"].startswith("call_")
    assert [ev["call"] for ev in events_buffered if ev["type"] == "tool_call_complete"] == resp_buffered["message"]["tool_calls"]

    # 2) stream=False (non-streaming branch)
    bridge_nonstream = _bridge_for_adapter(adapter, streaming=False)
    events_nonstream: list[dict] = []
    resp_nonstream = bridge_nonstream.complete(
        "fixture", [{"role": "user", "content": "write"}], [TOOL], lambda: False, events_nonstream.append
    )
    assert resp_nonstream["message"]["tool_calls"][0]["id"] == expected_colon_id
    assert resp_nonstream["message"]["tool_calls"][1]["id"].startswith("call_")

    # Defense-in-depth (Change 7): response_calls still rejects un-normalized invalid or duplicate IDs
    with pytest.raises(ModelEventError, match="invalid or duplicate tool call id"):
        response_calls(
            {
                "tool_calls": [
                    {"id": "functions.write_file:0", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}
                ]
            }
        )
    with pytest.raises(ModelEventError, match="invalid or duplicate tool call id"):
        response_calls(
            {
                "tool_calls": [
                    {"id": "call_dup", "type": "function", "function": {"name": "write_file", "arguments": "{}"}},
                    {"id": "call_dup", "type": "function", "function": {"name": "write_file", "arguments": "{}"}},
                ]
            }
        )


def test_t3_same_index_changing_id_splits_into_independent_calls_and_model_events_guards(monkeypatch) -> None:
    """T3: Same index receiving two different non-empty IDs splits into two calls; direct ModelEventStream.feed guards against conflicting IDs."""
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "functions.write_file:0",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"first.txt"}'}},
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "functions.write_file:1",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"second.txt"}'}},
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _sse_response(chunks))
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="fixture")
    bridge = _bridge_for_adapter(adapter, streaming=True)
    events: list[dict] = []
    resp = bridge.complete("fixture", [{"role": "user", "content": "both"}], [TOOL], lambda: False, events.append)

    id_0 = "call_" + hashlib.sha256(b"functions.write_file:0").hexdigest()[:24]
    id_1 = "call_" + hashlib.sha256(b"functions.write_file:1").hexdigest()[:24]
    expected_calls = [
        {"id": id_0, "type": "function", "function": {"name": "write_file", "arguments": '{"path":"first.txt"}'}},
        {"id": id_1, "type": "function", "function": {"name": "write_file", "arguments": '{"path":"second.txt"}'}},
    ]
    assert resp["message"]["tool_calls"] == expected_calls
    completed_events = [(ev["index"], ev["call"]) for ev in events if ev["type"] == "tool_call_complete"]
    assert completed_events == [(0, expected_calls[0]), (1, expected_calls[1])]

    # Direct defense-in-depth on ModelEventStream.feed: slot with existing ID receiving a different ID raises ModelEventError
    direct_stream = ModelEventStream(lambda _ev: None)
    direct_stream.feed(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_one", "type": "function", "function": {"name": "write_file", "arguments": "{"}}
                        ]
                    },
                }
            ]
        }
    )
    with pytest.raises(ModelEventError):
        direct_stream.feed(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_two", "type": "function", "function": {"name": "write_file", "arguments": "}"}}
                            ]
                        },
                    }
                ]
            }
        )


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_t4_native_illegal_id_normalized_and_replayed_with_original_id(protocol: str) -> None:
    """T4: Native illegal call ID is normalized in canonical history while provider_state preserves and replays the raw ID."""
    raw_illegal_id = "functions.write_file:0"
    expected_norm_id = "call_" + hashlib.sha256(raw_illegal_id.encode("utf-8")).hexdigest()[:24]

    # Direct _call check
    normalized_single = _call({"id": raw_illegal_id, "function": {"name": "write_file", "arguments": '{"path":"x.txt"}'}})
    assert normalized_single["id"] == expected_norm_id

    if protocol == RESPONSES:
        native_payload = {
            "id": "resp_1",
            "status": "completed",
            "error": None,
            "output": [
                {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC_STATE"},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": raw_illegal_id,
                    "name": "write_file",
                    "arguments": '{"path":"x.txt"}',
                    "status": "completed",
                },
            ],
            "usage": {"input_tokens": 8, "output_tokens": 4},
        }
        stream_events = [
            {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0, "item": native_payload["output"][0]},
            {"type": "response.output_item.done", "output_index": 0, "item": native_payload["output"][0]},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**native_payload["output"][1], "arguments": "", "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "item_id": "fc_1",
                "delta": '{"path":"x.txt"}',
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 1,
                "item_id": "fc_1",
                "arguments": '{"path":"x.txt"}',
            },
            {"type": "response.output_item.done", "output_index": 1, "item": native_payload["output"][1]},
            {"type": "response.completed", "response": native_payload},
        ]
    else:
        native_payload = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "THINK", "signature": "SIG"},
                {"type": "tool_use", "id": raw_illegal_id, "name": "write_file", "input": {"path": "x.txt"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 8, "output_tokens": 4},
        }
        stream_events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "usage": {"input_tokens": 8, "output_tokens": 1},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "THINK"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": raw_illegal_id, "name": "write_file", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"x.txt"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ]

    # Check both StreamDecoder + ModelEventStream and parse_response
    decoder = StreamDecoder(protocol, MODEL, SCOPE)
    emitted: list[dict] = []
    event_stream = ModelEventStream(emitted.append)
    for ev in stream_events:
        for chunk in decoder.feed(ev):
            event_stream.feed(chunk)
    stream_result = decoder.result()
    parsed = parse_response(native_payload, protocol, MODEL, SCOPE)
    assert stream_result == parsed
    event_stream.finish(stream_result)

    assistant_msg = parsed["message"]
    assert assistant_msg["tool_calls"][0]["id"] == expected_norm_id
    assert [e["call"]["id"] for e in emitted if e["type"] == "tool_call_complete"] == [expected_norm_id]

    # Replay to the SAME scope: wire request MUST use raw_illegal_id in both the call and the tool output!
    history = [
        {"role": "user", "content": "write x.txt"},
        copy.deepcopy(assistant_msg),
        {"role": "tool", "tool_call_id": expected_norm_id, "content": "written"},
        {"role": "user", "content": "next"},
    ]
    wire_same = make_request({"messages": history, "tools": [copy.deepcopy(TOOL)], "max_tokens": 256}, protocol, MODEL, SCOPE)
    if protocol == RESPONSES:
        fn_calls = [item for item in wire_same["input"] if item.get("type") == "function_call"]
        fn_outputs = [item for item in wire_same["input"] if item.get("type") == "function_call_output"]
        assert [c["call_id"] for c in fn_calls] == [raw_illegal_id]
        assert [o["call_id"] for o in fn_outputs] == [raw_illegal_id]
    else:
        tool_uses = [b for m in wire_same["messages"] if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
        tool_results = [b for m in wire_same["messages"] if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
        assert [u["id"] for u in tool_uses] == [raw_illegal_id]
        assert [r["tool_use_id"] for r in tool_results] == [raw_illegal_id]

    # Replay to OTHER_SCOPE: falls back to public canonical projection using expected_norm_id consistently
    wire_other = make_request(
        {"messages": history, "tools": [copy.deepcopy(TOOL)], "max_tokens": 256}, protocol, MODEL, OTHER_SCOPE
    )
    if protocol == RESPONSES:
        fn_calls_other = [item for item in wire_other["input"] if item.get("type") == "function_call"]
        fn_outputs_other = [item for item in wire_other["input"] if item.get("type") == "function_call_output"]
        assert [c["call_id"] for c in fn_calls_other] == [expected_norm_id]
        assert [o["call_id"] for o in fn_outputs_other] == [expected_norm_id]
    else:
        tool_uses_other = [b for m in wire_other["messages"] if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
        tool_results_other = [b for m in wire_other["messages"] if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
        assert [u["id"] for u in tool_uses_other] == [expected_norm_id]
        assert [r["tool_use_id"] for r in tool_results_other] == [expected_norm_id]


@pytest.mark.parametrize("protocol,path,native_proto", [
    ("responses", "responses", RESPONSES),
    ("messages", "messages", MESSAGES),
])
@pytest.mark.parametrize("scenario", ["index_from_3", "shared_index_0", "no_stream_id_order"])
def test_t5_inference_api_stream_format_noncontiguous_and_shared_index(
    tmp_path, protocol: str, path: str, native_proto: str, scenario: str
) -> None:
    """T5: Upstream index starting at 3 or two calls sharing index 0 convert cleanly in Responses/Messages streams and match final response."""

    class _WeirdIndexAdapter:
        def health(self):
            return {"ok": True}

        def cancel(self, _task_id):
            return True

        def chat(self, body, *, task_id, timeout_s, on_event=None):
            if scenario == "index_from_3":
                final_calls = [
                    {
                        "id": "call_idx3",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Shanghai"}'},
                    }
                ]
                deltas = [
                    {"tool_calls": [{"index": 3, "id": "call_idx3", "type": "function", "function": {"name": "weather", "arguments": '{"city":'}}]},
                    {"tool_calls": [{"index": 3, "function": {"arguments": '"Shanghai"}'}}]},
                ]
            elif scenario == "shared_index_0":
                final_calls = [
                    {
                        "id": "call_first",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Beijing"}'},
                    },
                    {
                        "id": "call_second",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Shenzhen"}'},
                    },
                ]
                deltas = [
                    {"tool_calls": [{"index": 0, "id": "call_first", "type": "function", "function": {"name": "weather", "arguments": '{"city":'}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"Beijing"}'}}]},
                    {"tool_calls": [{"index": 0, "id": "call_second", "type": "function", "function": {"name": "weather", "arguments": '{"city":'}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"Shenzhen"}'}}]},
                ]
            else:
                final_calls = [
                    {
                        "id": "call_ord_0",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Hangzhou"}'},
                    }
                ]
                deltas = [
                    {"tool_calls": [{"index": 5, "type": "function", "function": {"name": "weather", "arguments": '{"city":'}}]},
                    {"tool_calls": [{"index": 5, "function": {"arguments": '"Hangzhou"}'}}]},
                ]
            message = {"role": "assistant", "content": "Checking", "tool_calls": final_calls}
            if body.get("stream") and on_event:
                on_event({"choices": [{"index": 0, "delta": {"content": "Checking"}, "finish_reason": None}]})
                for d in deltas:
                    on_event({"choices": [{"index": 0, "delta": d, "finish_reason": None}]})
                on_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
                on_event({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}})
            return {
                "text": "Checking",
                "message": message,
                "finish_reason": "tool_calls",
                "input_tokens": 9,
                "output_tokens": 5,
                "duration_ms": 3,
            }

    adapter = _WeirdIndexAdapter()
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=LLMPackageManifest(
            package_id="test",
            mode="openai_compatible",
            public_model_alias="test-model",
            base_url="http://127.0.0.1:1",
        ),
        _slots=threading.BoundedSemaphore(1),
        _lock=threading.Lock(),
        _running=0,
    )
    app = FastAPI()
    install_inference_api(
        app,
        home=tmp_path,
        store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: manager,
        discover=lambda _: [],
        execute_order=None,
        cancel_order=None,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    key = client.post("/api/local/llm/api-keys", json={"name": "agent", "output_token_limit": 1000}).json()
    client.headers["Authorization"] = "Bearer " + key["key"]

    if protocol == "responses":
        req_body = {
            "model": "local/test",
            "input": "weather?",
            "max_output_tokens": 64,
            "tools": [{"type": "function", "name": "weather", "parameters": {"type": "object"}}],
        }
    else:
        req_body = {
            "model": "local/test",
            "messages": [{"role": "user", "content": "weather?"}],
            "max_tokens": 64,
            "tools": [{"name": "weather", "input_schema": {"type": "object"}}],
        }

    # 1) Streaming response via local inference API
    stream_res = client.post(f"/v1/{path}", json={**req_body, "stream": True})
    assert stream_res.status_code == 200, stream_res.text
    assert "error" not in [
        line.removeprefix("event: ").strip()
        for line in stream_res.text.splitlines()
        if line.startswith("event: ")
    ], stream_res.text

    # Decode SSE stream with native StreamDecoder to prove wire validity and exact agreement
    decoder = StreamDecoder(native_proto, "local/test", SCOPE)
    for frame in stream_res.text.split("\n\n"):
        data_lines = [ln[6:] for ln in frame.splitlines() if ln.startswith("data: ")]
        if data_lines:
            decoder.feed(json.loads("\n".join(data_lines)))
    decoded_stream = decoder.result()

    # 2) Non-streaming response via local inference API
    nonstream_res = client.post(f"/v1/{path}", json={**req_body, "stream": False})
    assert nonstream_res.status_code == 200, nonstream_res.text
    parsed_nonstream = parse_response(nonstream_res.json(), native_proto, "local/test", SCOPE)

    assert decoded_stream["message"]["content"] == parsed_nonstream["message"]["content"] == "Checking"
    assert decoded_stream["message"]["tool_calls"] == parsed_nonstream["message"]["tool_calls"]
    assert decoded_stream["finish_reason"] == parsed_nonstream["finish_reason"] == "tool_calls"
