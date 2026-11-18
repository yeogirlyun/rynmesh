"""Tests for S01-I15 size-limit error codes, unified adapter bounds, bridge public-only sizing, P2P frame limit, and remote large-request precheck/compaction (T1, T2, T3, T6, T7)."""
from __future__ import annotations

import email
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from test_agent_peer_binding import paired  # noqa: F401
from test_agent_peer_binding import response as decode_peer_response
from test_agent_resources import resources  # noqa: F401

from rynmesh import native_transport
from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.bridge import AgentModelBridge, build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package import adapters, p2p
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.routes import _delivery_error_code, install_llm_routes
from rynmesh.llm_package.sources import SourceStore
from rynmesh.llm_package.task_protocol import TaskOrderStore, open_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

EXPECTED_FEEDBACK = (
    "上一次回复超过大小上限，没有执行任何工具。请把大段写入拆成多次较小的 apply_patch/写文件调用"
)


class _FakeHTTPResponse:
    def __init__(self, body: bytes | list[bytes], content_type: str = "text/event-stream") -> None:
        self.headers = email.message_from_string(f"Content-Type: {content_type}\r\n")
        if isinstance(body, bytes):
            self._buf = io.BytesIO(body)
        else:
            self._buf = io.BytesIO(b"".join(body))

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def readline(self, limit: int = -1) -> bytes:
        return self._buf.readline(limit)

    def close(self) -> None:
        self._buf.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _make_local_api_runtime(tmp_path, server_url: str, *, protocol: str = "openai_chat"):
    home = tmp_path / f"home_{protocol}"
    home.mkdir(parents=True, exist_ok=True)
    store = RynmeshStore(home=home, network_dir=tmp_path / f"net_{protocol}")
    app = FastAPI()
    mkey = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=mkey,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    source_store: SourceStore = app.state.llm_agent_services["source_store"]
    prepared = source_store.prepare(
        {
            "name": f"local-{protocol}",
            "kind": "local",
            "provider": "custom",
            "protocol": protocol,
            "base_url": server_url,
            "api_key": "sk-test-local",
            "enabled": True,
            "models": ["test-model"],
        }
    )
    prepared.update(status="ready", tool_support={"test-model": "supported"})
    source_store.save(prepared)
    bridge, tools, catalog = build_agent_integrations(app, store, home)
    orig_resolve = catalog.resolve

    def _streaming_resolve(model_id):
        row = orig_resolve(model_id)
        row["streaming"] = True
        return row

    catalog.resolve = _streaming_resolve
    agent_store = AgentStore(tmp_path / f"agent_{protocol}", mkey)
    runtime = AgentRuntime(agent_store, bridge, tools, catalog)
    model = next(m for m in catalog.list_models() if m["source"] == "local_api")
    runtime.update_settings({"enabled": True, "model_id": model["id"]})
    return app, runtime, bridge, catalog, model


def test_t1_sse_line_over_1mib_triggers_feedback_retry_once(tmp_path) -> None:
    """T1: Over-1-MiB single SSE line raises upstream_response_too_large and triggers feedback retry once in runtime."""
    captured_requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return None

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            req_body = json.loads(self.rfile.read(length).decode("utf-8"))
            captured_requests.append(req_body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if len(captured_requests) == 1:
                # Single SSE data line > 1 MiB
                huge_line = b'data: {"choices":[{"index":0,"delta":{"content":"' + (b"x" * (1024 * 1024 + 128)) + b'"}}]}\n\n'
                self.wfile.write(huge_line)
                self.wfile.flush()
                return
            if self.path.endswith("/responses"):
                ev1 = {"type": "response.output_text.delta", "delta": "split ok"}
                ev2 = {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_2",
                        "model": "test-model",
                        "status": "completed",
                        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "split ok"}]}],
                        "usage": {"input_tokens": 12, "output_tokens": 4},
                    },
                }
                self.wfile.write(f"data: {json.dumps(ev1)}\n\ndata: {json.dumps(ev2)}\n\n".encode("utf-8"))
            else:
                chunk1 = {"choices": [{"index": 0, "delta": {"content": "split ok"}, "finish_reason": None}]}
                chunk2 = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 4}}
                self.wfile.write(f"data: {json.dumps(chunk1)}\n\ndata: {json.dumps(chunk2)}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()

    for proto in ("openai_chat", "openai_responses"):
        captured_requests.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        app = None
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1"
            app, runtime, _bridge, _catalog, model = _make_local_api_runtime(tmp_path, url, protocol=proto)
            created = runtime.create(
                {
                    "message": "Generate output.",
                    "expected_destination_revision": model["destination_revision"],
                }
            )
            task_id = created["id"]

            # Attempt 1: > 1 MiB SSE line -> rejected with feedback, queued for 1 retry
            runtime.run_once()
            assert captured_requests and captured_requests[0].get("stream") is True
            stored = runtime.store.read()["tasks"][task_id]
            assert stored["status"] == "queued"
            assert stored["model_retry_count"] == 1
            assert stored.get("rejected_response_note") == EXPECTED_FEEDBACK
            rej_ev = next(e for e in stored["events"] if e["type"] == "model_response_rejected")
            assert rej_ev["detail"] == EXPECTED_FEEDBACK

            # Attempt 2: receives feedback in request context and completes cleanly
            runtime.run_once()
            done = runtime.get(task_id)
            assert done["status"] == "completed"
            assert done["result"] == "split ok"
            assert len(captured_requests) == 2
            assert EXPECTED_FEEDBACK in json.dumps(captured_requests[1], ensure_ascii=False)
        finally:
            if app is not None:
                app.state.llm_shutdown()
            server.shutdown()
            server.server_close()


def test_t2_infer_stream_and_chat_share_unified_limits_and_diagnostics(monkeypatch) -> None:
    """T2: infer_stream, _json, chat, and NativeProtocolAdapter share unified constants, upstream_response_too_large, limit_bytes diagnostics, and Chinese message."""
    assert "upstream_response_too_large" in adapters.RUNTIME_ERROR_CODES
    assert adapters.MAX_JSON_RESPONSE_BYTES == 4 * 1024 * 1024
    assert adapters.MAX_SSE_LINE_BYTES == 1024 * 1024
    assert adapters.MAX_STREAM_BYTES == 32 * 1024 * 1024
    assert adapters.MAX_STREAM_TEXT_BYTES == 4 * 1024 * 1024

    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:9", model="m1")
    native = NativeProtocolAdapter(protocol="openai_responses", base_url="http://127.0.0.1:9", model="m1")

    # 1) 300 KiB SSE line (> old 256 KiB infer_stream limit, <= 1 MiB unified MAX_SSE_LINE_BYTES) now succeeds in infer_stream!
    medium_text = "中" * (100 * 1024)  # 300 KiB UTF-8
    sse_ok = (
        f'data: {json.dumps({"choices": [{"index": 0, "delta": {"content": medium_text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 100}}, ensure_ascii=False)}\n\n'
        "data: [DONE]\n\n"
    ).encode("utf-8")
    monkeypatch.setattr(adapters, "model_urlopen", lambda *_a, **_kw: _FakeHTTPResponse(sse_ok))
    deltas: list[str] = []
    res = adapter.infer_stream(prompt="hi", max_tokens=200, task_id="t_300k", timeout_s=10.0, on_delta=deltas.append)
    assert res["text"] == medium_text
    assert "".join(deltas) == medium_text

    # 2) > 1 MiB SSE line fails identically in both infer_stream and chat and NativeProtocolAdapter
    over_1m = b'data: {"choices":[{"index":0,"delta":{"content":"' + (b"a" * (adapters.MAX_SSE_LINE_BYTES + 32)) + b'"}}]}\n\n'
    for caller in (
        lambda: adapter.infer_stream(prompt="hi", max_tokens=64, task_id="t_1m_is", timeout_s=10.0, on_delta=lambda _d: None),
        lambda: adapter.chat({"messages": [{"role": "user", "content": "hi"}], "stream": True}, task_id="t_1m_chat", timeout_s=10.0),
        lambda: native.chat({"messages": [{"role": "user", "content": "hi"}], "stream": True}, task_id="t_1m_nat", timeout_s=10.0),
    ):
        monkeypatch.setattr(adapters, "model_urlopen", lambda *_a, **_kw: _FakeHTTPResponse(over_1m))
        monkeypatch.setattr("rynmesh.llm_package.protocol_adapter.model_urlopen", lambda *_a, **_kw: _FakeHTTPResponse(over_1m))
        with pytest.raises(AdapterError) as exc_info:
            caller()
        assert exc_info.value.code == "upstream_response_too_large"
        assert exc_info.value.diagnostics.get("limit_bytes") == adapters.MAX_SSE_LINE_BYTES
        assert "模型回复超过 1 MiB 上限，请让模型分多次输出" == str(exc_info.value)

    # 3) > 4 MiB JSON response fails with upstream_response_too_large and limit_bytes=4 MiB across _json, chat non-SSE, infer_stream JSON, and NativeProtocolAdapter non-SSE
    over_4m_json = b'{"choices":[{"message":{"role":"assistant","content":"' + (b"b" * (adapters.MAX_JSON_RESPONSE_BYTES + 64)) + b'"}}]}'
    for json_caller in (
        lambda: adapter.infer(prompt="hi", max_tokens=64, task_id="t_4m_json", timeout_s=10.0),
        lambda: adapter.chat({"messages": [{"role": "user", "content": "hi"}], "stream": True}, task_id="t_4m_chat_json", timeout_s=10.0),
        lambda: adapter.infer_stream(prompt="hi", max_tokens=64, task_id="t_4m_is_json", timeout_s=10.0, on_delta=lambda _d: None),
        lambda: native.chat({"messages": [{"role": "user", "content": "hi"}], "stream": False}, task_id="t_4m_nat_json", timeout_s=10.0),
    ):
        monkeypatch.setattr(adapters, "model_urlopen", lambda *_a, **_kw: _FakeHTTPResponse(over_4m_json, content_type="application/json"))
        monkeypatch.setattr("rynmesh.llm_package.protocol_adapter.model_urlopen", lambda *_a, **_kw: _FakeHTTPResponse(over_4m_json, content_type="application/json"))
        with pytest.raises(AdapterError) as exc_4m:
            json_caller()
        assert exc_4m.value.code == "upstream_response_too_large"
        assert exc_4m.value.diagnostics.get("limit_bytes") == adapters.MAX_JSON_RESPONSE_BYTES
        assert "模型回复超过 4 MiB 上限，请让模型分多次输出" == str(exc_4m.value)

    # 4) infer_stream text output > MAX_STREAM_TEXT_BYTES (4 MiB across smaller lines) -> upstream_response_too_large with limit_bytes=4 MiB
    chunk_text = "x" * (512 * 1024)
    line_bytes = f'data: {json.dumps({"choices": [{"index": 0, "delta": {"content": chunk_text}}]})}\n\n'.encode("utf-8")
    nine_chunks = [line_bytes] * 9  # 4.5 MiB text output across 512 KiB lines
    monkeypatch.setattr(adapters, "model_urlopen", lambda *_a, **_kw: _FakeHTTPResponse(nine_chunks))
    with pytest.raises(AdapterError) as exc_text:
        adapter.infer_stream(prompt="hi", max_tokens=1000, task_id="t_text_4m", timeout_s=10.0, on_delta=lambda _d: None)
    assert exc_text.value.code == "upstream_response_too_large"
    assert exc_text.value.diagnostics.get("limit_bytes") == adapters.MAX_STREAM_TEXT_BYTES
    assert "模型回复超过 4 MiB 上限，请让模型分多次输出" == str(exc_text.value)


def test_t3_bridge_measures_public_fields_caps_provider_state_truncates_reasoning_and_retries_overflow(tmp_path) -> None:
    """T3: Bridge allows 10 KiB content + 600 KiB provider_state, drops 3 MiB provider_state, truncates >256 KiB reasoning_content, and feedback-retries >256 KiB public content once."""
    scope_hex = "a" * 64
    content_10k = "x" * (10 * 1024)

    # 3a) 10 KiB content + 600 KiB provider_state -> succeeds and provider_state is preserved!
    ps_600k = {
        "protocol": "openai_responses",
        "scope": scope_hex,
        "data": [{"type": "reasoning", "encrypted_content": "r" * (600 * 1024)}],
    }
    res_keep = AgentModelBridge._response(
        {
            "message": {
                "role": "assistant",
                "content": content_10k,
                "provider_state": ps_600k,
                "reasoning_content": "思" * (100 * 1024),  # ~300 KiB UTF-8 (> 256 KiB)
            },
            "reported_usage": {"input_tokens": 50, "output_tokens": 200},
            "finish_reason": "stop",
        },
        {"source": "local_api"},
        [],
        {},
    )
    assert res_keep["message"]["content"] == content_10k
    assert res_keep["message"]["provider_state"] == ps_600k
    size = len(res_keep["message"]["reasoning_content"].encode("utf-8"))
    assert output_policy.MAX_REASONING_CONTENT_BYTES - 3 <= size <= output_policy.MAX_REASONING_CONTENT_BYTES

    # 3b) 10 KiB content + 3 MiB provider_state -> drops provider_state and still succeeds!
    ps_3m = {
        "protocol": "openai_responses",
        "scope": scope_hex,
        "data": [{"type": "reasoning", "encrypted_content": "z" * (3 * 1024 * 1024)}],
    }
    res_drop = AgentModelBridge._response(
        {
            "message": {
                "role": "assistant",
                "content": content_10k,
                "provider_state": ps_3m,
            },
            "reported_usage": {"input_tokens": 50, "output_tokens": 200},
            "finish_reason": "stop",
        },
        {"source": "local_api"},
        [],
        {},
    )
    assert res_drop["message"]["content"] == content_10k
    assert "provider_state" not in res_drop["message"]

    # 3c) Public content > 256 KiB -> feedback retry once in runtime (not immediate failure!)
    calls = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return None

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            calls["n"] += 1
            calls["last_req"] = req
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            text = "y" * (output_policy.MAX_RAW_RESPONSE_BYTES + 1024) if calls["n"] == 1 else "batched result"
            payload = {
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 100},
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app = None
    try:
        app, runtime, _bridge, _catalog, model = _make_local_api_runtime(
            tmp_path, f"http://127.0.0.1:{server.server_port}/v1"
        )
        # Disable streaming on source or let chat run; bridge uses streaming=True by default when on_event is given,
        # and non-SSE JSON response in chat still goes through AgentModelBridge._response!
        created = runtime.create(
            {
                "message": "Write big text.",
                "expected_destination_revision": model["destination_revision"],
            }
        )
        tid = created["id"]
        runtime.run_once()
        mid = runtime.store.read()["tasks"][tid]
        assert mid["status"] == "queued"
        assert mid["model_retry_count"] == 1
        assert mid.get("rejected_response_note") == EXPECTED_FEEDBACK

        runtime.run_once()
        final = runtime.get(tid)
        assert final["status"] == "completed"
        assert final["result"] == "batched result"
        assert EXPECTED_FEEDBACK in json.dumps(calls["last_req"], ensure_ascii=False)
    finally:
        if app is not None:
            app.state.llm_shutdown()
        server.shutdown()
        server.server_close()


def test_t6_native_transport_oversized_frame_raises_p2p_message_too_large() -> None:
    """T6: native_transport._frame raises P2PMessageTooLarge (a P2PError subclass) and maps to peer_request_too_large."""
    assert hasattr(p2p, "P2PMessageTooLarge")
    assert issubclass(p2p.P2PMessageTooLarge, p2p.P2PError)

    small_frame = native_transport._frame({"hello": "world"})
    assert int.from_bytes(small_frame[:4], "big") == len(small_frame) - 4

    with pytest.raises(p2p.P2PMessageTooLarge) as exc_info:
        native_transport._frame({"payload": "x" * (native_transport.MAX_FRAME_BYTES + 32)})
    assert isinstance(exc_info.value, p2p.P2PError)
    assert not isinstance(exc_info.value, ValueError)
    assert _delivery_error_code(exc_info.value, transport="p2p") == "peer_request_too_large"


def test_t7_remote_large_request_precheck_and_compaction_flow(
    paired, resources, tmp_path, monkeypatch  # noqa: F811
) -> None:
    """T7: Oversized input fails local admission before any order; a remote task compacts large history before dispatch."""
    p = paired
    p.providers["a"].manifest.context_window = 16_000_000
    p.providers["a"].manifest.max_output_tokens = 4096
    p.providers["a"].manifest.pricing.input_per_1k = 0.0
    p.providers["a"].manifest.pricing.output_per_1k = 0.0
    p.consumer.refresh(p.alice.peer_id)

    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    app.state.nas_plugin = resources[1]
    install_llm_routes(
        app,
        store=store,
        home=store.home,
        messaging_key=p.bob.messaging_private,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )

    exchange_calls: list[dict] = []

    async def spy_exchange(*, signed_request, **_kwargs):
        exchange_calls.append(signed_request)
        _, opened = open_task(
            signed_request,
            recipient_peer_id=p.alice.peer_id,
            recipient_messaging_key=p.alice.messaging_private,
            expected_kind="llm_request",
        )
        reply = p.providers["a"].handle(signed_request, on_event=lambda _: None)
        p.last_response = decode_peer_response(p, reply)
        return reply, {"transport": "local_signed_fixture", "relay_used": False, "opened_len": len(json.dumps(opened))}

    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", spy_exchange)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    runtime = AgentRuntime(
        AgentStore(tmp_path / "consumer-agent", p.bob.messaging_private),
        bridge,
        tools,
        catalog,
        consumer_node_id=p.bob.peer_id,
    )
    peer_model = next(row for row in catalog.list_models() if row["source"] == "peer")
    runtime.update_settings({"enabled": True, "model_id": peer_model["id"]})

    try:
        # S12-I15 admits input locally before constructing the oversized peer envelope.
        big_chunk = "A" * 3_100_000  # prompt + chat (~6.2 MiB) -> base64 ciphertext > 8.2 MiB (> 7.5 MiB)
        with pytest.raises(AgentError) as overflow_exc:
            bridge.complete(
                peer_model["id"],
                [{"role": "user", "content": big_chunk}],
                [],
                lambda: False,
                expected_destination=catalog.destination(peer_model),
            )
        assert str(overflow_exc.value) == "agent_context_limit"
        assert exchange_calls == []
        balance_path = store.home / "llm" / "task-balance.json"
        if balance_path.exists():
            balance_raw = json.loads(balance_path.read_text("utf-8"))
            assert balance_raw.get("held", 0.0) == 0.0 and balance_raw["available"] == 100.0
        assert TaskOrderStore(store.home / "llm" / "consumer-orders").list() == []

        # Part 7b: Remote task in AgentRuntime with large multi-turn history exceeding remote window capacity
        # compacts history first and then succeeds on remote retry/dispatch!
        p.providers["a"].manifest.context_window = 32768
        p.providers["a"].manifest.max_output_tokens = 2048
        runtime.update_settings({"max_context_chars": 28000})
        p.consumer.refresh(p.alice.peer_id)
        catalog_peer = next(row for row in catalog.list_models() if row["source"] == "peer")
        created = runtime.create(
            {
                "message": "Summarize everything and finish.",
                "model_id": catalog_peer["id"],
                "expected_destination_revision": catalog_peer["destination_revision"],
            }
        )
        tid = created["id"]

        def seed_large_history(data):
            msgs = data["tasks"][tid]["messages"]
            for idx in range(12):
                msgs.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"c_{idx}",
                                "type": "function",
                                "function": {"name": "inspect_service", "arguments": '{"service_id":"svc1"}'},
                            }
                        ],
                        "context_epoch": 0,
                    }
                )
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": f"c_{idx}",
                        "content": json.dumps({"note": f"entry-{idx}-" + ("w" * 14000)}),
                        "context_epoch": 0,
                    }
                )
            msgs.append({"role": "assistant", "content": "Intermediate progress.", "context_epoch": 0})
            msgs.append({"role": "user", "content": "Now provide the final answer.", "context_epoch": 0})

        runtime.store.transaction(seed_large_history)
        seeded = runtime.store.read()["tasks"][tid]
        assert runtime._request_context_bytes(seeded, specs=runtime._specs(seeded)) > runtime._context_limit(seeded)
        for _ in range(4):
            runtime.run_once()
            if runtime.get(tid)["status"] == "completed":
                break
        final_task = runtime.get(tid)
        assert final_task["status"] == "completed", final_task.get("error")
        assert any(ev["type"] == "context_compacted" for ev in final_task["events"])
        assert len(exchange_calls) >= 2  # 1 compaction request + 1 compacted main request
    finally:
        app.state.llm_shutdown()
