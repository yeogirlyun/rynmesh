"""Exercise the actual urllib transport against a loopback protocol endpoint."""
import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter


@contextmanager
def endpoint(replies):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            kind, body = replies.pop(0)
            raw = (json.dumps(body) if kind == "application/json" else body).encode()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def message(content, reason="end_turn", **extra):
    return {"type": "message", "role": "assistant", "content": content,
            "stop_reason": reason, "usage": {"input_tokens": 12, "output_tokens": 3}, **extra}


def test_native_messages_real_http_tool_roundtrip_and_auth():
    replies = [("application/json", message([{"type": "tool_use", "id": "toolu_1", "name": "inspect",
                                             "input": {"path": "a\nb\"c"}}], "tool_use")),
               ("application/json", message([{"type": "text", "text": "actual result"}]))]
    with endpoint(replies) as (url, requests):
        adapter = NativeProtocolAdapter(protocol="anthropic_messages", base_url=url, api_prefix="",
                                        api_key="local-secret", model="test-model")
        tools = [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}]
        history = [{"role": "user", "content": "inspect"}]
        first = adapter.chat({"messages": history, "tools": tools}, task_id="a", timeout_s=3)
        call = first["message"]["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"path": "a\nb\"c"}
        assert requests[0]["path"] == "/v1/messages"
        headers = {key.lower(): value for key, value in requests[0]["headers"].items()}
        assert headers["x-api-key"] == "local-secret" and "authorization" not in headers
        assert headers["anthropic-version"] == "2023-06-01"
        history += [first["message"], {"role": "tool", "tool_call_id": "toolu_1", "content": "result"}]
        final = adapter.chat({"messages": history, "tools": tools}, task_id="b", timeout_s=3)
        assert final["text"] == "actual result"
        result = requests[1]["body"]["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
        assert final["reported_usage"] == {"input_tokens": 12, "output_tokens": 3}


def sse(events):
    return ": keepalive\n\n" + "".join("event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n" for value in events)


def text_events():
    return [{"type": "message_start", "message": message([], None)},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "visible"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"}]


@pytest.mark.parametrize("truncated", [False, True])
def test_native_sse_requires_terminal_and_preserves_usage(truncated):
    events = text_events()
    if truncated:
        events = events[:-1]
    with endpoint([("text/event-stream", sse(events))]) as (url, requests):
        adapter = NativeProtocolAdapter(protocol="anthropic_messages", base_url=url, api_prefix="", model="test-model")
        seen = []
        if truncated:
            with pytest.raises(AdapterError) as caught:
                adapter.chat({"messages": [{"role": "user", "content": "go"}], "stream": True},
                             task_id="a", timeout_s=3, on_event=seen.append)
            assert caught.value.code == "source_protocol_response_invalid"
            assert caught.value.reported_usage["output_tokens"] == 4
        else:
            result = adapter.chat({"messages": [{"role": "user", "content": "go"}], "stream": True},
                                  task_id="a", timeout_s=3, on_event=seen.append)
            assert result["text"] == "visible"
            assert result["output_tokens"] == 4
        assert len(requests) == 1
        assert "visible" in json.dumps(seen)


def test_responses_json_endpoint_and_no_fake_stream_deltas():
    raw = {"id": "resp_1", "object": "response", "status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}],
        "usage": {"input_tokens": 2, "output_tokens": 0}}
    with endpoint([("application/json", raw)]) as (url, requests):
        adapter = NativeProtocolAdapter(protocol="openai_responses", base_url=url, api_prefix="", api_key="token", model="test-model")
        seen = []
        result = adapter.chat({"messages": [{"role": "user", "content": "go"}], "stream": True},
                              task_id="a", timeout_s=3, on_event=seen.append)
        assert result["text"] == "hello" and result["output_tokens"] == 0
        assert seen == []  # The provider sent a buffered response, no token stream.
        assert requests[0]["path"] == "/v1/responses"
        assert requests[0]["body"]["store"] is False


def test_callback_error_no_retry_or_secret_exception():
    with endpoint([("text/event-stream", sse(text_events()))]) as (url, requests):
        adapter = NativeProtocolAdapter(protocol="anthropic_messages", base_url=url, api_prefix="", model="test-model")
        def fail(_):
            raise RuntimeError("PRIVATE CALLBACK CONTENT")
        with pytest.raises(AdapterError) as caught:
            adapter.chat({"messages": [{"role": "user", "content": "go"}], "stream": True},
                         task_id="a", timeout_s=3, on_event=fail)
        assert caught.value.code == "stream_delivery_failed"
        assert "PRIVATE" not in str(caught.value) and len(requests) == 1


def test_cancel_before_dispatch_is_not_a_paid_request():
    with endpoint([]) as (url, requests):
        adapter = NativeProtocolAdapter(protocol="anthropic_messages", base_url=url, api_prefix="", model="test-model")
        adapter.cancel("cancel-me")
        with pytest.raises(AdapterError, match="task_cancelled"):
            adapter.chat({"messages": [{"role": "user", "content": "go"}]}, task_id="cancel-me", timeout_s=3)
        assert requests == []


def test_cancel_after_complete_body_retains_actual_usage(monkeypatch):
    with endpoint([("application/json", message([{"type": "text", "text": "PRIVATE BODY"}]))]) as (url, requests):
        adapter = NativeProtocolAdapter(protocol="anthropic_messages", base_url=url, api_prefix="", model="test-model")
        original = urllib.request.urlopen
        class Response:
            def __init__(self, inner):
                self.inner, self.headers = inner, inner.headers
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.inner.close()
            def close(self):
                self.inner.close()
            def read(self, size):
                raw = self.inner.read(size)
                adapter.cancel("a")
                return raw
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Response(original(*a, **kw)))
        with pytest.raises(AdapterError, match="task_cancelled") as caught:
            adapter.chat({"messages": [{"role": "user", "content": "go"}]}, task_id="a", timeout_s=3)
        assert caught.value.reported_usage == {"input_tokens": 12, "output_tokens": 3}
        assert "PRIVATE" not in str(caught.value)
        assert len(requests) == 1
