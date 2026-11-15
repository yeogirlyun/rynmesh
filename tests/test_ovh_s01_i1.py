"""Acceptance tests for S01-I1: mid-stream EOF becomes retryable stream interruption."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_agent_integrations import add_source, node  # noqa: F401
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import RETRYABLE_MODEL_ERRORS, AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package.adapters import (
    RUNTIME_ERROR_CODES,
    AdapterError,
    OpenAICompatibleAdapter,
)
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.services import peer_box


@contextmanager
def _sse_server(responses: list[bytes]):
    requests: list[dict] = []
    queue = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            requests.append({"path": self.path, "body": json.loads(raw_body.decode("utf-8"))})
            payload = queue.pop(0)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_t1_chat_sse_truncated_after_two_deltas_raises_upstream_stream_truncated():
    """T1: Fake Chat SSE server sends two deltas then closes -> code='upstream_stream_truncated' with usage."""
    assert "upstream_stream_truncated" in RUNTIME_ERROR_CODES
    frames = (
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello "}}],'
        b'"usage":{"prompt_tokens":11,"completion_tokens":2}}\n\n'
        b'data: {"choices":[{"index":0,"delta":{"content":"world"}}]}\n\n'
    )
    with _sse_server([frames]) as (base_url, requests):
        adapter = OpenAICompatibleAdapter(base_url=base_url, model="fixture-chat")
        seen: list[dict] = []
        with pytest.raises(AdapterError) as caught:
            adapter.chat(
                {"messages": [{"role": "user", "content": "greet"}], "stream": True},
                task_id="s01-i1-t1",
                timeout_s=5.0,
                on_event=seen.append,
            )
    err = caught.value
    assert err.code == "upstream_stream_truncated"
    assert str(err) == "upstream_stream_truncated"
    assert err.reported_usage == {"input_tokens": 11, "output_tokens": 2}
    assert len(seen) == 2
    assert len(requests) == 1


def test_t2_native_responses_and_messages_truncated_midstream_raise_upstream_stream_truncated():
    """T2: Native Responses and Messages truncated mid-stream raise 'upstream_stream_truncated'; _failed stays invalid."""
    responses_events = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_t2",
                "status": "in_progress",
                "usage": {"input_tokens": 13, "output_tokens": 1},
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_t2", "type": "message", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Part 1 "},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Part 2"},
    ]
    responses_sse = "".join(f"data: {json.dumps(ev)}\n\n" for ev in responses_events).encode("utf-8")

    messages_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_t2",
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 15, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Alpha "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Beta"}},
    ]
    messages_sse = "".join(
        f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n" for ev in messages_events
    ).encode("utf-8")

    invalid_sse = b'data: {"type":"error","error":{"message":"malformed"}}\n\n'

    with _sse_server([responses_sse, messages_sse, invalid_sse]) as (base_url, requests):
        resp_adapter = NativeProtocolAdapter(
            protocol="openai_responses",
            base_url=base_url,
            api_prefix="",
            model="resp-model",
        )
        seen_resp: list[dict] = []
        with pytest.raises(AdapterError) as caught_resp:
            resp_adapter.chat(
                {"messages": [{"role": "user", "content": "go"}], "stream": True},
                task_id="s01-i1-t2-resp",
                timeout_s=5.0,
                on_event=seen_resp.append,
            )
        assert caught_resp.value.code == "upstream_stream_truncated"
        assert caught_resp.value.reported_usage == {"input_tokens": 13, "output_tokens": 1}
        assert len(seen_resp) == 2

        msg_adapter = NativeProtocolAdapter(
            protocol="anthropic_messages",
            base_url=base_url,
            api_prefix="",
            model="msg-model",
        )
        seen_msg: list[dict] = []
        with pytest.raises(AdapterError) as caught_msg:
            msg_adapter.chat(
                {"messages": [{"role": "user", "content": "go"}], "stream": True},
                task_id="s01-i1-t2-msg",
                timeout_s=5.0,
                on_event=seen_msg.append,
            )
        assert caught_msg.value.code == "upstream_stream_truncated"
        assert caught_msg.value.reported_usage == {"input_tokens": 15, "output_tokens": 1}
        assert len(seen_msg) == 2

        with pytest.raises(AdapterError) as caught_invalid:
            resp_adapter.chat(
                {"messages": [{"role": "user", "content": "go"}], "stream": True},
                task_id="s01-i1-t2-invalid",
                timeout_s=5.0,
            )
        assert caught_invalid.value.code == "source_protocol_response_invalid"
        assert len(requests) == 3


def test_t3_bridge_and_runtime_schedule_retry_and_block_partial_tools(node, monkeypatch):  # noqa: F811
    """T3 (end-to-end HTTP SSE + bridge + runtime): truncated stream -> agent_model_stream_interrupted -> model_retry_scheduled without dispatching partial tool."""
    assert "agent_model_stream_interrupted" in RETRYABLE_MODEL_ERRORS
    assert "agent_model_stream_interrupted" in retry_policy.NETWORK_ERRORS
    assert retry_policy.limit("agent_model_stream_interrupted", {}) == 5

    monkeypatch.setattr(retry_policy, "delay", lambda *_: 0)
    app, store = node

    truncated_tool_sse = (
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Checking..."}}],'
        b'"usage":{"prompt_tokens":9,"completion_tokens":3}}\n\n'
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_partial_1",'
        b'"type":"function","function":{"name":"ryn_set_model_sharing","arguments":"{\\"model_id\\":\\"m\\""}}]}}]}\n\n'
    )
    complete_sse = (
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"All clear."},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":12,"completion_tokens":4}}\n\n'
        b"data: [DONE]\n\n"
    )

    with _sse_server([truncated_tool_sse, complete_sse]) as (base_url, requests):
        sources = app.state.llm_agent_services["source_store"]
        record = sources.prepare(
            {
                "name": "Truncated SSE Fixture",
                "kind": "local",
                "base_url": f"{base_url}/v1",
                "models": ["sse-fixture"],
            }
        )
        record.update(status="ready", tool_support={"sse-fixture": "supported"})
        sources.save(record)
        selected_model = "local/" + sources.model_service_id(record, "sse-fixture")

        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        orig_resolve = catalog.resolve
        monkeypatch.setattr(
            catalog,
            "resolve",
            lambda model_id, **kw: {**orig_resolve(model_id, **kw), "streaming": True},
        )

        runtime = AgentRuntime(
            AgentStore(
                store.home / "s01-i1-agent",
                peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
            ),
            bridge,
            tools,
            catalog,
        )
        runtime.update_settings({"enabled": True, "model_id": selected_model})
        sources_before = sources.path.read_bytes()

        created = runtime.create({"message": "Inspect status without mutating sharing."})
        runtime.run_once()

        after_first = runtime.store.read()["tasks"][created["id"]]
        assert after_first["status"] == "queued"
        assert after_first["model_retry_count"] == 1
        assert after_first["queue"] == []
        assert after_first["pending_approval"] is None
        assert not after_first.get("operation_receipts")
        assert sources.path.read_bytes() == sources_before
        retry_events = [ev for ev in after_first["events"] if ev["type"] == "model_retry_scheduled"]
        assert len(retry_events) == 1
        assert retry_events[0]["error"] == "agent_model_stream_interrupted"
        assert after_first["connection_recovery"]["error_code"] == "agent_model_stream_interrupted"
        assert after_first["connection_recovery"]["max_retries"] == 5
        assert not any(ev["type"] == "tool_call" for ev in after_first["events"])

        runtime.run_once()
        done = runtime.get(created["id"])
        assert done["status"] == "completed"
        assert done["result"] == "All clear."
        assert len(requests) == 2


def test_t3b_stream_interrupted_retains_completed_read_and_never_dispatches_partial_write(
    tmp_path, monkeypatch
):
    """T3 / Change 6: completed streamed read is retained on agent_model_stream_interrupted while partial write tool is never dispatched."""
    from test_agent_streamed_reads import Reads

    monkeypatch.setattr(retry_policy, "delay", lambda *_: 0)
    runtime, model, _, _ = make_runtime(tmp_path)
    tools = Reads()
    runtime.tools = tools
    model.supports_model_events = True
    attempt_num = [0]

    def complete(model_id, messages, specs, cancelled, on_event, **options):
        attempt_num[0] += 1
        model.calls.append((model_id, messages, specs))
        if attempt_num[0] == 1:
            on_event(
                {
                    "type": "tool_call_complete",
                    "index": 0,
                    "call": {
                        "id": "call_read_ok",
                        "type": "function",
                        "function": {
                            "name": "ryn_file_read",
                            "arguments": json.dumps({"path": "notes.txt"}),
                        },
                    },
                }
            )
            on_event(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 1,
                                        "id": "call_write_half",
                                        "type": "function",
                                        "function": {
                                            "name": "write",
                                            "arguments": '{"path": "notes.',
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            raise AgentError("agent_model_stream_interrupted")
        return assistant("Completed using retained read.")

    model.complete = complete
    row = runtime.create({"message": "Read notes.txt safely"})
    advance(runtime)

    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["status"] == "queued"
    assert stored["events"][-1]["type"] == "model_retry_scheduled"
    assert stored["events"][-1]["error"] == "agent_model_stream_interrupted"
    assert tools.calls == [("ryn_file_read", {"path": "notes.txt"})]
    assert stored["queue"] == []
    assert stored["pending_approval"] is None
    assert any(r["tool"] == "ryn_file_read" and r["status"] == "confirmed" for r in stored["receipts"])
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_read_ok" for m in stored["messages"])

    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "completed"
    assert done["result"] == "Completed using retained read."
    assert tools.calls == [("ryn_file_read", {"path": "notes.txt"})]
