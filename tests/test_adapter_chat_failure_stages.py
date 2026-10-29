"""Classify real local HTTP/SSE failures without exposing upstream/callback text."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter

PRIVATE = "PRIVATE_URL_KEY_BODY"


@pytest.fixture
def upstream():
    state = {"mode": "sse", "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            state["calls"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            mode = state["mode"]
            self.send_response(503 if mode == "http_error" else 200)
            if mode == "http_error":
                self.send_header("Retry-After", "5")
            self.send_header("Content-Type", "text/event-stream" if mode in {"sse", "timeout"} else "application/json")
            self.end_headers()
            if mode == "timeout":
                self.wfile.flush()
                time.sleep(.15)
                return
            if mode == "sse":
                chunk = {"choices": [{"delta": {"role": "assistant", "content": "fixture"}, "finish_reason": state.get("finish", "stop")}],
                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
                raw = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
            elif mode == "json":
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "fixture"},
                                                "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
            else:
                raw = PRIVATE.encode()
            try:
                self.wfile.write(raw)
            except OSError:
                pass  # The deliberately cancelled/failed client already closed.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    adapter = OpenAICompatibleAdapter(base_url=f"http://127.0.0.1:{server.server_port}", model="fixture")
    try:
        yield adapter, state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def chat(adapter, callback=None, *, streaming=True, timeout=1):
    return adapter.chat({"messages": [{"role": "user", "content": "fixture"}], "max_tokens": 8,
                         "stream": streaming}, task_id="fixture-task", timeout_s=timeout, on_event=callback)


@pytest.mark.parametrize("mode,streaming", [("json", False), ("sse", True)])
def test_normal_chat_keeps_usage_without_retry(upstream, mode, streaming):
    adapter, state = upstream
    state["mode"] = mode
    chunks = []
    result = chat(adapter, chunks.append, streaming=streaming)
    assert result["text"] == "fixture"
    assert result["reported_usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert bool(chunks) == streaming and state["calls"] == 1


@pytest.mark.parametrize("kind", [TimeoutError, OSError, TypeError, RuntimeError])
def test_callback_failure_is_delivery_not_upstream_and_keeps_usage(upstream, kind):
    adapter, state = upstream

    def callback(_):
        raise kind(PRIVATE)

    with pytest.raises(AdapterError) as caught:
        chat(adapter, callback)
    error = caught.value
    assert error.code == str(error) == "stream_delivery_failed"
    assert error.reported_usage == {"input_tokens": 7, "output_tokens": 3}
    assert error.finish_reason == "stop"
    assert PRIVATE not in str(error) + json.dumps(vars(error))
    assert error.__suppress_context__ and state["calls"] == 1


@pytest.mark.parametrize("code,message", [("ai_permission_denied", "permission_revoked"),
                                         ("inference_failed", "task_cancelled")])
def test_callback_adapter_error_retains_identity_and_explicit_metadata(upstream, code, message):
    adapter, _ = upstream
    original = AdapterError(message, code=code, reported_usage={"input_tokens": 11, "output_tokens": 0},
                            finish_reason="cancelled")

    def callback(_):
        raise original

    with pytest.raises(AdapterError) as caught:
        chat(adapter, callback)
    assert caught.value is original
    assert original.code == code and str(original) == message
    assert original.reported_usage == {"input_tokens": 11, "output_tokens": 0}
    assert original.finish_reason == "cancelled"


@pytest.mark.parametrize("finish", [{"untrusted": PRIVATE}, [PRIVATE]])
def test_untrusted_finish_cannot_replace_callback_failure(upstream, finish):
    adapter, state = upstream
    state["finish"] = finish

    def callback(_):
        raise TimeoutError(PRIVATE)

    with pytest.raises(AdapterError) as caught:
        chat(adapter, callback)
    assert caught.value.code == "stream_delivery_failed"
    assert caught.value.finish_reason == "unknown"
    assert caught.value.reported_usage == {"input_tokens": 7, "output_tokens": 3}
    assert PRIVATE not in str(caught.value) + json.dumps(vars(caught.value))


@pytest.mark.parametrize("error", [TimeoutError(PRIVATE), AdapterError("permission_revoked", code="ai_permission_denied",
                                                                       reported_usage={"input_tokens": 11, "output_tokens": 0},
                                                                       finish_reason="cancelled")])
def test_local_cancellation_wins_over_callback_failure(upstream, error):
    adapter, state = upstream

    def callback(_):
        adapter.cancel("fixture-task")
        raise error

    with pytest.raises(AdapterError, match="^task_cancelled$") as caught:
        chat(adapter, callback)
    assert caught.value.reported_usage == (error.reported_usage if isinstance(error, AdapterError)
                                          else {"input_tokens": 7, "output_tokens": 3})
    assert caught.value.finish_reason == ("cancelled" if isinstance(error, AdapterError) else "stop")
    assert PRIVATE not in str(caught.value) + json.dumps(vars(caught.value))
    assert state["calls"] == 1


@pytest.mark.parametrize("mode,streaming,code", [("http_error", False, "upstream_http_failed"),
                                                ("http_error", True, "upstream_http_failed"),
                                                ("invalid_json", False, "upstream_response_invalid"),
                                                ("invalid_json", True, "upstream_response_invalid"),
                                                ("timeout", True, "inference_timeout")])
def test_upstream_failures_have_safe_distinct_codes(upstream, mode, streaming, code):
    adapter, state = upstream
    state["mode"] = mode
    with pytest.raises(AdapterError) as caught:
        chat(adapter, streaming=streaming, timeout=.04 if mode == "timeout" else 1)
    assert caught.value.code == code
    if mode == "http_error":
        assert caught.value.diagnostics == {"network_reason": "http", "network_phase": "request", "http_status": 503, "retry_after_seconds": 5}
    if mode == "timeout":
        assert caught.value.diagnostics == {"network_reason": "timeout", "network_phase": "read"}
    assert PRIVATE not in str(caught.value) + json.dumps(vars(caught.value))
    assert state["calls"] == 1
