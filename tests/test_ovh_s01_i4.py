"""S01-I4 integration tests for streaming error classification, 4xx rejection, and content_filter."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime import retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import protocol_codecs
from rynmesh.llm_package.adapters import RUNTIME_ERROR_CODES, AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.protocol_codecs import MESSAGES, RESPONSES, StreamDecoder, _finish, parse_response
from rynmesh.services import peer_box


@contextmanager
def _scripted_server(steps):
    requests = []
    queue = list(steps)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append({"path": self.path, "body": json.loads(body) if body else None})
            step = queue.pop(0)
            status = step.get("status", 200)
            ctype = step.get("content_type", "application/json")
            raw = step["raw"]
            if not isinstance(raw, bytes):
                raw = (raw if isinstance(raw, str) else json.dumps(raw)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (step.get("headers") or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _setup_runtime_source(app, store, base_url: str, *, protocol: str = "openai_chat", suffix: str = "x"):
    sources = app.state.llm_agent_services["source_store"]
    record = sources.prepare(
        {
            "name": f"S01-I4-{protocol}-{suffix}",
            "kind": "local",
            "protocol": protocol,
            "base_url": base_url + "/v1",
            "models": ["fixture-model"],
        }
    )
    record.update(status="ready", tool_support={"fixture-model": "supported"})
    sources.save(record)
    model_id = "local/" + sources.model_service_id(record, "fixture-model")
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    runtime = AgentRuntime(
        AgentStore(
            store.home / f"agent-s01-i4-{protocol}-{suffix}",
            peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
        ),
        bridge,
        tools,
        catalog,
    )
    runtime.update_settings({"enabled": True, "model_id": model_id})
    return runtime, bridge, catalog, model_id


def test_t5_t6_chat_stream_overloaded_error_chunk_maps_to_retryable_code_and_recovers(node, monkeypatch):  # noqa: F811
    assert "upstream_stream_error" in RUNTIME_ERROR_CODES
    assert "upstream_request_rejected" in RUNTIME_ERROR_CODES
    assert 529 in retry_policy.RETRYABLE_HTTP_STATUSES

    sse_error = (
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},"finish_reason":null}]}\n\n'
        'data: {"error":{"type":"overloaded_error","message":"PRIVATE_CHAT_OVERLOADED"}}\n\n'
    )
    sse_ok = (
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"recovered reply"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":4}}\n\n'
        "data: [DONE]\n\n"
    )
    app, store = node
    with _scripted_server(
        [
            {"status": 200, "content_type": "text/event-stream", "raw": sse_error},
            {"status": 200, "content_type": "text/event-stream", "raw": sse_ok},
        ]
    ) as (url, requests):
        # Direct adapter check (T5/T6)
        adapter = OpenAICompatibleAdapter(base_url=url + "/v1", api_prefix="", model="fixture-model")
        with pytest.raises(AdapterError) as caught:
            adapter.chat({"messages": [{"role": "user", "content": "hi"}], "stream": True}, task_id="t5", timeout_s=3)
        assert caught.value.code == "upstream_stream_error"
        assert caught.value.diagnostics.get("overloaded") is True
        assert "PRIVATE_CHAT_OVERLOADED" not in str(caught.value) + json.dumps(caught.value.diagnostics)

    # Full bridge + runtime retry check
    with _scripted_server(
        [
            {"status": 200, "content_type": "text/event-stream", "raw": sse_error},
            {"status": 200, "content_type": "text/event-stream", "raw": sse_ok},
        ]
    ) as (url, requests):
        clock = [1000.0]
        monkeypatch.setattr(retry_policy, "now", lambda: clock[0])
        monkeypatch.setattr(retry_policy.random, "uniform", lambda _a, _b: 1.0)
        runtime, _, catalog, _ = _setup_runtime_source(app, store, url, protocol="openai_chat", suffix="t5")
        orig_resolve = catalog.resolve
        monkeypatch.setattr(catalog, "resolve", lambda mid, **kw: {**orig_resolve(mid, **kw), "streaming": True})

        created = runtime.create({"message": "Test chat stream overloaded recovery."})
        runtime.run_once()
        mid_row = runtime.get(created["id"])
        assert mid_row["status"] == "queued"
        scheduled = [e for e in mid_row["events"] if e["type"] == "model_retry_scheduled"]
        assert len(scheduled) == 1
        assert scheduled[0]["error"] == "agent_model_upstream_failed"
        assert scheduled[0]["diagnostics"].get("overloaded") is True
        # Overloaded doubles finite start delay from 2s to 4s
        clock[0] += 4.5
        runtime.run_once()
        done_row = runtime.get(created["id"])
        assert done_row["status"] == "completed"
        assert "recovered reply" in done_row["result"]
        assert len(requests) == 2


def test_t7_messages_sse_overloaded_error_event_uses_native_stream_error_and_retries(node, monkeypatch):  # noqa: F811
    assert hasattr(protocol_codecs, "NativeStreamError"), "NativeStreamError must exist in protocol_codecs"
    scope = "a" * 64
    decoder = StreamDecoder(MESSAGES, "claude-test", scope)
    decoder.feed({"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 9, "output_tokens": 1}}})
    with pytest.raises(protocol_codecs.NativeStreamError) as dec_err:
        decoder.feed({"type": "error", "error": {"type": "overloaded_error", "message": "PRIVATE_MSG_OVERLOAD"}})
    assert dec_err.value.code == "upstream_stream_error"
    assert dec_err.value.diagnostics.get("overloaded") is True
    assert "PRIVATE_MSG_OVERLOAD" not in str(dec_err.value) + json.dumps(dec_err.value.diagnostics)

    sse_fail = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","type":"message","role":"assistant","content":[],"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"PRIVATE_MSG_OVERLOAD"}}\n\n'
    )
    sse_ok = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"m2","type":"message","role":"assistant","content":[],"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"anthropic ok"}}\n\n'
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    app, store = node
    with _scripted_server(
        [
            {"status": 200, "content_type": "text/event-stream", "raw": sse_fail},
            {"status": 200, "content_type": "text/event-stream", "raw": sse_ok},
        ]
    ) as (url, requests):
        clock = [2000.0]
        monkeypatch.setattr(retry_policy, "now", lambda: clock[0])
        monkeypatch.setattr(retry_policy.random, "uniform", lambda _a, _b: 1.0)
        runtime, _, catalog, _ = _setup_runtime_source(app, store, url, protocol="anthropic_messages", suffix="t7")
        orig_resolve = catalog.resolve
        monkeypatch.setattr(catalog, "resolve", lambda mid, **kw: {**orig_resolve(mid, **kw), "streaming": True})

        created = runtime.create({"message": "Test anthropic overloaded error event."})
        runtime.run_once()
        mid_row = runtime.get(created["id"])
        assert mid_row["status"] == "queued"
        assert mid_row["connection_recovery"]["error_code"] == "agent_model_upstream_failed"
        assert mid_row["connection_recovery"]["diagnostics"].get("overloaded") is True
        clock[0] += 4.5
        runtime.run_once()
        done_row = runtime.get(created["id"])
        assert done_row["status"] == "completed"
        assert "anthropic ok" in done_row["result"]
        assert len(requests) == 2


def test_t8_responses_failed_server_error_retries_and_content_filter_finish_kept(node, monkeypatch):  # noqa: F811
    scope = "b" * 64
    assert _finish({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, RESPONSES) == "content_filter"
    assert _finish({"stop_reason": "refusal"}, MESSAGES) == "content_filter"

    sse_failed = (
        'data: {"type":"response.created","response":{"id":"r1","status":"in_progress","usage":{"input_tokens":6,"output_tokens":0}}}\n\n'
        'data: {"type":"response.failed","response":{"id":"r1","status":"failed","error":{"code":"server_error","message":"PRIVATE_RESP_ERR"},"output":[],"usage":{"input_tokens":6,"output_tokens":1}}}\n\n'
    )
    sse_ok = (
        'data: {"type":"response.created","response":{"id":"r2","status":"in_progress"}}\n\n'
        'data: {"type":"response.completed","response":{"id":"r2","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"responses ok"}]}],"usage":{"input_tokens":6,"output_tokens":2}}}\n\n'
    )
    app, store = node
    with _scripted_server(
        [
            {"status": 200, "content_type": "text/event-stream", "raw": sse_failed},
            {"status": 200, "content_type": "text/event-stream", "raw": sse_ok},
        ]
    ) as (url, requests):
        clock = [3000.0]
        monkeypatch.setattr(retry_policy, "now", lambda: clock[0])
        monkeypatch.setattr(retry_policy.random, "uniform", lambda _a, _b: 1.0)
        runtime, _, catalog, _ = _setup_runtime_source(app, store, url, protocol="openai_responses", suffix="t8")
        orig_resolve = catalog.resolve
        monkeypatch.setattr(catalog, "resolve", lambda mid, **kw: {**orig_resolve(mid, **kw), "streaming": True})

        created = runtime.create({"message": "Test responses.failed server_error retry."})
        runtime.run_once()
        mid_row = runtime.get(created["id"])
        assert mid_row["status"] == "queued"
        assert mid_row["connection_recovery"]["error_code"] == "agent_model_upstream_failed"
        clock[0] += 2.5
        runtime.run_once()
        done_row = runtime.get(created["id"])
        assert done_row["status"] == "completed"
        assert "responses ok" in done_row["result"]
        assert len(requests) == 2
        assert "PRIVATE_RESP_ERR" not in json.dumps(done_row)


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses"])
def test_t9_http_400_invalid_request_fails_directly_with_http_status_not_paused(node, protocol):  # noqa: F811
    app, store = node
    err_body = {"error": {"type": "invalid_request_error", "code": "invalid_request", "message": "PRIVATE_REJECT_DETAIL"}}
    with _scripted_server([{"status": 400, "raw": err_body}]) as (url, requests):
        runtime, _, _, _ = _setup_runtime_source(app, store, url, protocol=protocol, suffix=f"t9-{protocol}")
        created = runtime.create({"message": "Trigger 400 invalid request."})
        runtime.run_once()
        row = runtime.get(created["id"])
        assert row["status"] == "failed"
        assert row["error"] == "agent_model_request_rejected"
        assert len(requests) == 1
        assert not any(e["type"] == "connection_paused" for e in row["events"])
        failed_events = [e for e in row["events"] if e["type"] == "failed"]
        assert len(failed_events) == 1
        assert failed_events[0].get("http_status") == 400
        assert "PRIVATE_REJECT_DETAIL" not in json.dumps(row)


def test_t9_unrecognised_400_body_conservatively_pauses_and_40kib_json_uses_shared_reader(node):  # noqa: F811
    app, store = node
    # Unrecognised HTML 400 from a relay -> conservative connection_paused
    with _scripted_server([{"status": 400, "content_type": "text/html", "raw": b"<html>400 Bad Gateway</html>"}]) as (url, _):
        runtime, _, _, _ = _setup_runtime_source(app, store, url, protocol="openai_chat", suffix="t9-html")
        created = runtime.create({"message": "Relay HTML 400."})
        runtime.run_once()
        row = runtime.get(created["id"])
        assert row["status"] == "paused"
        assert any(e["type"] == "connection_paused" for e in row["events"])

    # Non-streaming _json path also uses read_http_failure for 40 KiB bodies (Change 11)
    big_401_quota = {
        "error": {
            "code": "insufficient_quota",
            "type": "insufficient_quota",
            "message": "quota " + ("q" * (40 * 1024)),
        }
    }
    with _scripted_server([{"status": 429, "raw": big_401_quota}]) as (url, _):
        adapter = OpenAICompatibleAdapter(base_url=url + "/v1", api_prefix="", model="fixture-model")
        with pytest.raises(AdapterError) as caught:
            adapter.chat({"messages": [{"role": "user", "content": "hi"}], "stream": False}, task_id="t9-big", timeout_s=3)
        assert caught.value.diagnostics.get("network_reason") == "quota"


@pytest.mark.parametrize("content_text", ["", "Filtered partial output"])
def test_change6_content_filter_empty_and_non_empty_report_agent_model_content_filtered(node, content_text):  # noqa: F811
    app, store = node
    body = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content_text}, "finish_reason": "content_filter"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    with _scripted_server([{"status": 200, "raw": body}, {"status": 200, "raw": body}]) as (url, requests):
        runtime, _, _, _ = _setup_runtime_source(app, store, url, protocol="openai_chat", suffix=f"cf-{len(content_text)}")
        created = runtime.create({"message": "Trigger content filter."})
        runtime.run_once()
        first_row = runtime.get(created["id"])
        assert first_row["status"] == "queued"
        assert any(e["type"] == "content_filter_guidance" for e in first_row["events"])
        runtime.run_once()
        row = runtime.get(created["id"])
        assert row["status"] == "failed"
        assert row["error"] == "agent_model_content_filtered"
        assert len(requests) == 2
        assert not any(e["type"] in {"model_retry_scheduled", "connection_paused"} for e in row["events"])

    # Native parse_response also allows empty content when finish == "content_filter"
    scope = "c" * 64
    parsed = parse_response(
        {"type": "message", "role": "assistant", "content": [], "stop_reason": "refusal", "usage": {"input_tokens": 3, "output_tokens": 0}},
        MESSAGES,
        "claude-test",
        scope,
    )
    assert parsed["finish_reason"] == "content_filter"
    assert NativeProtocolAdapter is not None
