"""S01-I2: Stream idle timeout vs task-budget total deadline across Chat, Native, Bridge, and P2P."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime import retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.diagnostic_log import safe_diagnostics
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import p2p
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.p2p_session import SessionPool
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.routes import _delivery_error_code


@contextmanager
def _sse_server(stream_writer):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                stream_writer(self.wfile)
            except OSError:
                return None

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_sse_data(wfile, payload: dict | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    wfile.write(f"data: {text}\n\n".encode("utf-8"))
    wfile.flush()


def _write_responses_stream(wfile, *, deltas: list[str], interval_s: float, pause_after_first_s: float = 0.0) -> None:
    _write_sse_data(wfile, {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})
    _write_sse_data(
        wfile,
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_1", "type": "message", "role": "assistant", "content": []},
        },
    )
    _write_sse_data(
        wfile,
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
    )
    output_text = ""
    for idx, piece in enumerate(deltas):
        if idx > 0:
            time.sleep(interval_s)
        output_text += piece
        _write_sse_data(
            wfile,
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": piece,
            },
        )
        if idx == 0 and pause_after_first_s > 0:
            time.sleep(pause_after_first_s)
            return
    _write_sse_data(
        wfile,
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": output_text},
        },
    )
    _write_sse_data(
        wfile,
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": output_text}],
            },
        },
    )
    _write_sse_data(
        wfile,
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": output_text}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": len(deltas)},
            },
        },
    )


def _write_messages_stream(wfile, *, deltas: list[str], interval_s: float, pause_after_first_s: float = 0.0) -> None:
    _write_sse_data(
        wfile,
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 6, "output_tokens": 0},
            },
        },
    )
    _write_sse_data(
        wfile,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    for idx, piece in enumerate(deltas):
        if idx > 0:
            time.sleep(interval_s)
        _write_sse_data(
            wfile,
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}},
        )
        if idx == 0 and pause_after_first_s > 0:
            time.sleep(pause_after_first_s)
            return
    _write_sse_data(wfile, {"type": "content_block_stop", "index": 0})
    _write_sse_data(
        wfile,
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": len(deltas)},
        },
    )
    _write_sse_data(wfile, {"type": "message_stop"})


def _configure_local_p2p(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    for name in (
        "RYNMESH_P2P_BIND_PORT",
        "RYNMESH_P2P_REQUIRE_PUBLIC",
        "RYNMESH_P2P_REQUIRE_PUBLIC_NAT",
        "RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC",
    ):
        monkeypatch.delenv(name, raising=False)


def test_t1_p2p_session_deltas_exceed_three_idle_windows_succeeds(monkeypatch):
    """T1: P2P session receives a delta every 1 s and succeeds beyond 3x stream_idle_seconds."""
    _configure_local_p2p(monkeypatch)
    idle_s = 1.3
    pool = SessionPool()
    events = []

    async def publish(offer):
        answer = asyncio.get_running_loop().create_future()

        def handler(body, emit):
            for idx in range(5):
                if idx > 0:
                    time.sleep(1.0)
                emit({"payload": {"kind": "llm_stream"}, "n": idx})
            time.sleep(0.2)
            return {"payload": {"kind": "llm_response"}, "task": body["task"], "ok": True}

        asyncio.create_task(
            p2p.provider_exchange(
                offer=offer,
                publish_answer=answer.set_result,
                handle_request=handler,
                handle_stream_request=handler,
                timeout_s=20.0,
            )
        )
        return await answer

    started = time.monotonic()
    try:
        response, evidence = asyncio.run(
            p2p.consumer_exchange(
                signed_request={"task": "long-stream"},
                publish_offer=publish,
                timeout_s=20.0,
                stream_idle_seconds=idle_s,
                on_event=events.append,
                session_pool=pool,
                session_key="peer-t1",
            )
        )
    finally:
        pool.close()

    elapsed = time.monotonic() - started
    assert elapsed > 3 * idle_s
    assert response["ok"] is True and response["task"] == "long-stream"
    assert [item["n"] for item in events] == [0, 1, 2, 3, 4]
    assert evidence["stream_events"] == 5


def test_t2_p2p_session_midstream_silence_raises_idle_timeout_and_total_timeout(monkeypatch):
    """T2: P2P session silence exceeding stream_idle_seconds raises idle_timeout (distinct from total_timeout)."""
    _configure_local_p2p(monkeypatch)
    idle_s = 1.0
    pool = SessionPool()

    stop_bg = threading.Event()

    async def publish_idle_stall(offer):
        answer = asyncio.get_running_loop().create_future()

        def handler(_body, emit):
            emit({"payload": {"kind": "llm_stream"}, "n": 0})
            stop_bg.wait(idle_s + 1.0)
            return {"payload": {"kind": "llm_response"}}

        asyncio.create_task(
            p2p.provider_exchange(
                offer=offer,
                publish_answer=answer.set_result,
                handle_request=handler,
                handle_stream_request=handler,
                timeout_s=15.0,
            )
        )
        return await answer

    async def publish_total_exceeded(offer):
        answer = asyncio.get_running_loop().create_future()

        def handler(_body, emit):
            for idx in range(8):
                if stop_bg.wait(0.35):
                    break
                try:
                    emit({"payload": {"kind": "llm_stream"}, "n": idx})
                except Exception:
                    break
            return {"payload": {"kind": "llm_response"}}

        asyncio.create_task(
            p2p.provider_exchange(
                offer=offer,
                publish_answer=answer.set_result,
                handle_request=handler,
                handle_stream_request=handler,
                timeout_s=15.0,
            )
        )
        return await answer

    try:
        seen = []
        with pytest.raises(p2p.P2PTimeoutError) as idle_err:
            asyncio.run(
                p2p.consumer_exchange(
                    signed_request={"task": "stall"},
                    publish_offer=publish_idle_stall,
                    timeout_s=15.0,
                    stream_idle_seconds=idle_s,
                    on_event=seen.append,
                    session_pool=pool,
                    session_key="peer-t2-idle",
                )
            )
        assert seen and seen[0]["n"] == 0
        assert idle_err.value.reason == "idle_timeout"
        assert _delivery_error_code(idle_err.value, transport="p2p") == "p2p_idle_timeout"

        with pytest.raises(p2p.P2PTimeoutError) as total_err:
            asyncio.run(
                p2p.consumer_exchange(
                    signed_request={"task": "over-budget"},
                    publish_offer=publish_total_exceeded,
                    timeout_s=1.1,
                    stream_idle_seconds=5.0,
                    on_event=lambda _: None,
                    session_pool=pool,
                    session_key="peer-t2-total",
                )
            )
        assert total_err.value.reason == "total_timeout"
        assert _delivery_error_code(total_err.value, transport="p2p") == "p2p_total_timeout"
    finally:
        stop_bg.set()
        pool.close()


def test_t3_shared_service_manifest_timeout_comes_from_source_settings(node):  # noqa: F811
    """T3: Shared service manifest timeout_seconds/stream_idle_seconds and SourceStore.adapter idle timeout come from settings."""
    app, _store = node
    record, selected = add_source(app)
    sources = app.state.llm_agent_services["source_store"]
    raw_record = sources.read()[record["id"]]
    model_name = raw_record["models"][0]
    sid = sources.model_service_id(raw_record, model_name)

    default_adapter = sources.adapter(raw_record, model_name)
    assert default_adapter.timeout_s == 120

    default_manager = app.state.llm_agent_services["provider_for"](sid)
    assert default_manager.manifest.timeout_seconds == 1800
    default_public = default_manager.public_status()["service"]
    assert default_public["timeout_seconds"] == 1800
    assert default_public["stream_idle_seconds"] == 120

    rule_rev = raw_record["model_settings"][model_name]["rule_revision"]
    updated_pub = sources.configure_model(
        record["id"],
        {
            "model_id": model_name,
            "expected_rule_revision": rule_rev,
            "configuration": {"stream_idle_seconds": 45, "max_request_seconds": 900},
        },
    )
    assert updated_pub["model_settings"][model_name]["effective"]["stream_idle_seconds"] == 45
    assert updated_pub["model_settings"][model_name]["effective"]["max_request_seconds"] == 900

    updated_raw = sources.read()[record["id"]]
    assert sources.adapter(updated_raw, model_name).timeout_s == 45

    updated_manager = app.state.llm_agent_services["provider_for"](sid)
    assert updated_manager.manifest.timeout_seconds == 900
    updated_service = updated_manager.public_status()["service"]
    assert updated_service["timeout_seconds"] == 900
    assert updated_service["stream_idle_seconds"] == 45


def test_t4_chat_stream_deltas_every_second_past_three_idle_windows_succeeds():
    """T4: OpenAICompatibleAdapter.chat succeeds when SSE deltas arrive every 1 s for > 3x idle_s."""
    idle_s = 1.3

    def writer(wfile):
        for idx in range(5):
            if idx > 0:
                time.sleep(1.0)
            _write_sse_data(
                wfile,
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": f"c{idx}"} if idx == 0 else {"content": f"c{idx}"},
                            "finish_reason": None,
                        }
                    ]
                },
            )
        time.sleep(0.2)
        _write_sse_data(
            wfile,
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5},
            },
        )
        _write_sse_data(wfile, "[DONE]")

    seen = []
    started = time.monotonic()
    with _sse_server(writer) as base_url:
        adapter = OpenAICompatibleAdapter(base_url=base_url, api_prefix="", model="chat-m", timeout_s=idle_s)
        result = adapter.chat(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            task_id="t4-chat",
            timeout_s=idle_s,
            on_event=seen.append,
        )
    elapsed = time.monotonic() - started
    assert elapsed > 3 * idle_s
    assert result["text"] == "c0c1c2c3c4"
    assert result["finish_reason"] == "stop"
    assert len(seen) == 6


def test_t5_native_responses_stream_deltas_every_second_past_three_idle_windows_succeeds():
    """T5: NativeProtocolAdapter (openai_responses) succeeds when SSE deltas arrive every 1 s for > 3x idle_s."""
    idle_s = 1.3

    def writer(wfile):
        _write_responses_stream(wfile, deltas=["r0", "r1", "r2", "r3", "r4"], interval_s=1.0)

    seen = []
    started = time.monotonic()
    with _sse_server(writer) as base_url:
        adapter = NativeProtocolAdapter(
            protocol="openai_responses",
            base_url=base_url,
            api_prefix="",
            model="resp-m",
            timeout_s=idle_s,
        )
        result = adapter.chat(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            task_id="t5-resp",
            timeout_s=idle_s,
            on_event=seen.append,
        )
    elapsed = time.monotonic() - started
    assert elapsed > 3 * idle_s
    assert result["text"] == "r0r1r2r3r4"
    assert seen


def test_t6_chat_stream_midstream_pause_raises_idle_timeout_and_retryable():
    """T6: OpenAICompatibleAdapter.chat midstream pause of idle+1 s raises inference_timeout with network_reason=idle_timeout."""
    idle_s = 1.0

    def writer(wfile):
        _write_sse_data(
            wfile,
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "partial"}, "finish_reason": None}]},
        )
        time.sleep(idle_s + 1.0)

    with _sse_server(writer) as base_url:
        adapter = OpenAICompatibleAdapter(base_url=base_url, api_prefix="", model="chat-m", timeout_s=idle_s)
        with pytest.raises(AdapterError) as caught:
            adapter.chat(
                {"messages": [{"role": "user", "content": "hi"}], "stream": True},
                task_id="t6-chat-idle",
                timeout_s=idle_s,
            )

    assert caught.value.code == "inference_timeout"
    assert caught.value.diagnostics.get("network_reason") == "idle_timeout"
    cleaned = safe_diagnostics(caught.value.diagnostics)
    assert cleaned.get("network_reason") == "idle_timeout"
    assert retry_policy.limit("agent_inference_timeout", cleaned) == 3
    assert retry_policy.limit("agent_inference_timeout", {"network_reason": "timeout"}) == 1


def test_t7_native_both_protocols_idle_behavior_and_bridge_ignores_manifest_timeout(node, monkeypatch):  # noqa: F811
    """T7: Both native protocols cover >3x idle success + idle+1 s idle_timeout, and bridge does not expire at manifest.timeout_seconds."""
    # 1) anthropic_messages > 3x idle_s (1 s per delta) succeeds
    idle_long = 1.3
    started = time.monotonic()
    with _sse_server(lambda wfile: _write_messages_stream(wfile, deltas=["m0", "m1", "m2", "m3", "m4"], interval_s=1.0)) as url:
        msg_adapter = NativeProtocolAdapter(
            protocol="anthropic_messages",
            base_url=url,
            api_prefix="",
            model="msg-m",
            timeout_s=idle_long,
        )
        msg_res = msg_adapter.chat(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            task_id="t7-msg-long",
            timeout_s=idle_long,
            on_event=lambda _: None,
        )
    assert time.monotonic() - started > 3 * idle_long
    assert msg_res["text"] == "m0m1m2m3m4"

    # 2) Both openai_responses and anthropic_messages midstream pause idle + 1 s -> inference_timeout + idle_timeout
    idle_short = 1.0
    for protocol, writer in (
        ("openai_responses", lambda w: _write_responses_stream(w, deltas=["a", "b"], interval_s=0.1, pause_after_first_s=idle_short + 1.0)),
        ("anthropic_messages", lambda w: _write_messages_stream(w, deltas=["a", "b"], interval_s=0.1, pause_after_first_s=idle_short + 1.0)),
    ):
        with _sse_server(writer) as url:
            nat_adapter = NativeProtocolAdapter(
                protocol=protocol,
                base_url=url,
                api_prefix="",
                model="nat-m",
                timeout_s=idle_short,
            )
            with pytest.raises(AdapterError) as caught:
                nat_adapter.chat(
                    {"messages": [{"role": "user", "content": "hi"}], "stream": True},
                    task_id=f"t7-{protocol}-idle",
                    timeout_s=idle_short,
                    on_event=lambda _: None,
                )
            assert caught.value.code == "inference_timeout"
            assert caught.value.diagnostics.get("network_reason") == "idle_timeout"
            assert safe_diagnostics(caught.value.diagnostics).get("network_reason") == "idle_timeout"
            assert retry_policy.limit("agent_inference_timeout", caught.value.diagnostics) == 3

    # 3) Bridge unit test: manifest.timeout_seconds smaller than actual stream duration does NOT trigger expired()
    app, store = node

    def bridge_writer(wfile):
        for idx in range(3):
            if idx > 0:
                time.sleep(0.45)
            _write_sse_data(
                wfile,
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": f"b{idx}"} if idx == 0 else {"content": f"b{idx}"},
                            "finish_reason": None,
                        }
                    ]
                },
            )
        _write_sse_data(
            wfile,
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )
        _write_sse_data(wfile, "[DONE]")

    with _sse_server(bridge_writer) as upstream_url:
        sources = app.state.llm_agent_services["source_store"]
        prepared = sources.prepare(
            {
                "name": "Local Stream Source",
                "kind": "local",
                "protocol": "openai_chat",
                "base_url": upstream_url,
                "models": ["stream-model"],
                "enabled": True,
            }
        )
        prepared["status"] = "ready"
        saved = sources.save(prepared)
        sid = sources.model_service_id(sources.read()[saved["id"]], "stream-model")
        selected_id = f"local/{sid}"
        bridge, _tools, _catalog = build_agent_integrations(app, store, store.home)
        orig_resolve = bridge.catalog.resolve
        monkeypatch.setattr(
            bridge.catalog,
            "resolve",
            lambda mid: {**orig_resolve(mid), "streaming": True} if mid == selected_id else orig_resolve(mid),
        )
        manager = app.state.llm_agent_services["provider_for"](sid)
        manager.manifest.timeout_seconds = 0.3

        policies = []
        streamed = []
        response = bridge.complete(
            selected_id,
            [{"role": "user", "content": "stream across manifest timeout"}],
            [],
            lambda: False,
            on_event=streamed.append,
            deadline=time.monotonic() + 20.0,
            on_request_policy=policies.append,
        )
        assert response["message"]["content"] == "b0b1b2"
        assert policies[0]["provider_timeout_seconds"] == 0.3
        assert policies[0]["effective_timeout_seconds"] > 10.0
        assert any(evt.get("type") == "text_delta" for evt in streamed)

        # Also succeeds when caller passes deadline=None (no task budget hard cap)
        policies_none = []
        response_no_deadline = bridge.complete(
            selected_id,
            [{"role": "user", "content": "no caller deadline"}],
            [],
            lambda: False,
            on_event=lambda _: None,
            deadline=None,
            on_request_policy=policies_none.append,
        )
        assert response_no_deadline["message"]["content"] == "b0b1b2"
        assert policies_none[0]["provider_timeout_seconds"] == 0.3
        assert policies_none[0]["effective_timeout_seconds"] is None

        # Still enforces caller task budget deadline when deadline itself expires
        with pytest.raises((AgentError, ValueError), match="agent_inference_timeout"):
            bridge.complete(
                selected_id,
                [{"role": "user", "content": "short task budget"}],
                [],
                lambda: False,
                on_event=lambda _: None,
                deadline=time.monotonic() + 0.25,
            )
