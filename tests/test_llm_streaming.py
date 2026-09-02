from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.crypto import sign_payload
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import CAPABILITY, ProviderService, install_llm_routes
from rynmesh.llm_package.stream_protocol import (
    DEFAULT_MAX_EVENT_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    STREAM_PROTOCOL_VERSION,
    StreamEventBroker,
    StreamSequenceVerifier,
    seal_stream_delta,
)
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import (
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)
from rynmesh.peer_http import HttpPeerClient, PeerTransportError
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore
from rynmesh.transport import StdlibHttpsTransport, TransportProfile


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


class _ChunkTransport:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, object]] = []

    def iter_post_bytes(self, url, body, **kwargs):
        self.calls.append({"url": url, "body": body, **kwargs})
        yield from self.chunks


def test_http_peer_ndjson_handles_fragmented_unicode_and_bounds_events() -> None:
    raw = '{"sequence":0,"delta":"世"}\n{"terminal":true}\n'.encode()
    transport = _ChunkTransport([raw[:25], raw[25:28], raw[28:]])
    client = HttpPeerClient("http://127.0.0.1:1", transport=transport)  # type: ignore[arg-type]
    assert list(client.iter_post_ndjson("/stream", {"private": "request"})) == [
        {"sequence": 0, "delta": "世"},
        {"terminal": True},
    ]
    assert transport.calls[0]["max_total_bytes"] == 8 * 1024 * 1024

    oversized = _ChunkTransport([b'{"delta":"12345"}\n'])
    client = HttpPeerClient("http://127.0.0.1:1", transport=oversized)  # type: ignore[arg-type]
    with pytest.raises(PeerTransportError, match="event_too_large"):
        list(client.iter_post_ndjson("/stream", {}, max_event_bytes=8, max_total_bytes=8))


def test_http_peer_stream_fails_closed_for_invalid_utf8_and_unsupported_transport() -> None:
    client = HttpPeerClient(
        "http://127.0.0.1:1", transport=_ChunkTransport([b'{"x":"\xff"}\n']),  # type: ignore[arg-type]
    )
    with pytest.raises(PeerTransportError, match="utf8_invalid"):
        list(client.iter_post_ndjson("/stream", {}))
    client = HttpPeerClient("http://127.0.0.1:1", transport=object())  # type: ignore[arg-type]
    with pytest.raises(PeerTransportError, match="stream_unsupported"):
        list(client.iter_post_ndjson("/stream", {}))


def test_stdlib_incremental_post_preserves_auth_and_total_limit(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["auth"] = str(self.headers.get("X-Ryn-Auth"))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b"123456789"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:3])
            self.wfile.flush()
            self.wfile.write(body[3:])

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("RYNMESH_NETWORK_KEY", "stream-secret")
        transport = StdlibHttpsTransport(TransportProfile())
        url = f"http://127.0.0.1:{server.server_port}/stream"
        with pytest.raises(Exception) as error:
            list(
                transport.iter_post_bytes(
                    url,
                    b"{}",
                    timeout_s=5,
                    max_chunk_bytes=3,
                    max_total_bytes=8,
                )
            )
        assert getattr(error.value, "reason", "") == "too_large"
        assert seen["auth"] and seen["auth"] != "stream-secret"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_stream_sequence_verifier_rejects_gap_duplicate_signer_and_limits(tmp_path) -> None:
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    consumer_pub = peer_box.public_key_b64(consumer_key)

    def event(
        sequence: int,
        delta: str = "ok",
        *,
        sender=provider,
        task_id: str = "task-stream",
        expires_at: str | None = None,
    ) -> dict:
        return seal_stream_delta(
            task_id=task_id,
            service_id="svc",
            sequence=sequence,
            delta=delta,
            sender_peer_id=sender.peer_id,
            recipient_peer_id=consumer.peer_id,
            sender_signing_key=sender.private_key_bytes,
            recipient_messaging_pub=consumer_pub,
            expires_at=expires_at or _expires(),
        )

    verifier = StreamSequenceVerifier(
        task_id="task-stream",
        service_id="svc",
        provider_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_key,
        max_event_bytes=4,
        max_output_bytes=6,
    )
    assert verifier.accept_delta(event(0)) == {"sequence": 0, "delta": "ok"}
    with pytest.raises(TaskProtocolError, match="contiguous"):
        verifier.accept_delta(event(2))
    with pytest.raises(TaskProtocolError, match="selected provider"):
        verifier.accept_delta(event(1, sender=attacker))
    with pytest.raises(TaskProtocolError, match="task mismatch"):
        verifier.accept_delta(event(1, task_id="other-task"))
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with pytest.raises(TaskProtocolError, match="expired"):
        verifier.accept_delta(event(1, expires_at=expired))
    with pytest.raises(TaskProtocolError, match="event limit"):
        verifier.accept_delta(event(1, "12345"))
    assert verifier.accept_delta(event(1, "1234"))["sequence"] == 1
    with pytest.raises(TaskProtocolError, match="total limit"):
        verifier.accept_delta(event(2, "x"))


def test_maximum_stream_output_still_fits_terminal_wire_event(tmp_path) -> None:
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    output = "x" * DEFAULT_MAX_OUTPUT_BYTES
    envelope = seal_task(
        body={
            "task_id": "bounded-terminal",
            "state": "succeeded",
            "service_id": "svc",
            "output": output,
            "input_tokens": 1,
            "output_tokens": 1,
            "duration_ms": 1,
            "amount": 0.001,
        },
        task_id="bounded-terminal",
        kind="llm_response",
        sender_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        sender_signing_key=provider.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(consumer_key),
        expires_at=_expires(),
    ).to_dict()
    wire = (json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    assert len(wire) <= DEFAULT_MAX_EVENT_BYTES


def test_stream_broker_is_bounded_and_replays_from_sequence() -> None:
    broker = StreamEventBroker(max_tasks=1, max_events_per_task=2)
    broker.publish("one", {"event": "delta", "sequence": 0, "delta": "a"})
    broker.publish("one", {"event": "delta", "sequence": 1, "delta": "b"})
    broker.publish("one", {"event": "complete", "state": "succeeded"})
    assert broker.replay("one", after_sequence=0) == [
        {"event": "delta", "sequence": 1, "delta": "b"},
        {"event": "complete", "state": "succeeded"},
    ]
    broker.publish("two", {"event": "state", "state": "running"})
    assert broker.replay("one") == []

    reconnect = StreamEventBroker(max_tasks=1, max_events_per_task=2)
    reconnect.publish("task", {"event": "delta", "sequence": 0, "delta": "a"})
    reconnect.publish("task", {"event": "delta", "sequence": 1, "delta": "b"})
    reconnect.publish("task", {"event": "delta", "sequence": 2, "delta": "c"})
    assert reconnect.replay("task", after_sequence=-1) == [
        {"event": "delta", "sequence": 2, "delta": "abc", "snapshot": True},
    ]


class _SSEHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        frames = [
            b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"世界"}}]}\n\n'.encode(),
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
            b"data: [DONE]\n\n",
        ]
        body = b"".join(frames)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for frame in frames:
            self.wfile.write(frame)
            self.wfile.flush()

    def log_message(self, *_args):
        pass


def test_openai_adapter_streams_deltas_and_uses_final_usage() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = OpenAICompatibleAdapter(
            base_url=f"http://127.0.0.1:{server.server_port}", model="test",
        )
        deltas: list[str] = []
        result = adapter.infer_stream(
            prompt="secret",
            max_tokens=8,
            task_id="stream-adapter",
            timeout_s=5,
            on_delta=deltas.append,
        )
        assert deltas == ["Hello ", "世界"]
        assert result["text"] == "Hello 世界"
        assert result["input_tokens"] == 3
        assert result["output_tokens"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_openai_adapter_cancel_closes_active_stream(monkeypatch) -> None:
    closed = threading.Event()
    entered = threading.Event()

    class Response:
        headers = {"Content-Type": "text/event-stream"}

        def readline(self, _limit):
            entered.set()
            assert closed.wait(timeout=3)
            raise OSError("private frame must not escape")

        def close(self):
            closed.set()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="test")
    errors: list[Exception] = []

    def run() -> None:
        try:
            adapter.infer_stream(
                prompt="private",
                max_tokens=8,
                task_id="cancel-me",
                timeout_s=5,
                on_delta=lambda _delta: None,
            )
        except Exception as exc:  # expected result captured across the thread
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert entered.wait(timeout=2)
    assert adapter.cancel("cancel-me") is True
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AdapterError)
    assert str(errors[0]) == "task_cancelled"
    assert "private frame" not in str(errors[0])


def test_openai_stream_request_accepts_bounded_ordinary_json_without_retry(monkeypatch) -> None:
    calls = 0

    class Response:
        headers = {"Content-Type": "application/json"}

        def read(self, _limit):
            return (
                b'{"choices":[{"message":{"content":"one response"}}],'
                b'"usage":{"prompt_tokens":2,"completion_tokens":2}}'
            )

        def close(self):
            pass

    def open_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_once)
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="test")
    deltas: list[str] = []
    result = adapter.infer_stream(
        prompt="private",
        max_tokens=8,
        task_id="ordinary-json",
        timeout_s=5,
        on_delta=deltas.append,
    )
    assert calls == 1
    assert deltas == ["one response"]
    assert result["text"] == "one response"


class _StreamingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def health(self):
        return {"ok": True, "model": "fake"}

    def cancel(self, _task_id):
        return True

    def infer_stream(self, *, prompt, max_tokens, task_id, timeout_s, on_delta):
        self.calls += 1
        on_delta("first ")
        on_delta("世界")
        return {
            "text": "first 世界",
            "input_tokens": 5,
            "output_tokens": 2,
            "duration_ms": 7,
        }


class _GatedStreamingAdapter(_StreamingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.first_delta_sent = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def infer_stream(self, *, prompt, max_tokens, task_id, timeout_s, on_delta):
        self.calls += 1
        on_delta("early")
        self.first_delta_sent.set()
        assert self.release.wait(timeout=5)
        on_delta(" late")
        self.completed.set()
        return {
            "text": "early late",
            "input_tokens": 2,
            "output_tokens": 2,
            "duration_ms": 25,
        }


def test_provider_yields_first_delta_before_generation_finishes(tmp_path) -> None:
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_key = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _GatedStreamingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc",
            mode="openai_compatible",
            public_model_alias="alias",
            base_url="http://127.0.0.1:1",
        ),
        adapter=adapter,  # type: ignore[arg-type]
        store=provider,
        task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=provider_key,
    )
    request = seal_task(
        body={
            "task_id": "timing-stream",
            "service_id": "svc",
            "prompt": "private",
            "max_tokens": 8,
            "max_amount": 1,
            "reply_messaging_pub": peer_box.public_key_b64(consumer_key),
            "response_mode": "stream-v1",
            "stream_event_max_bytes": 1024,
        },
        task_id="timing-stream",
        kind="llm_request",
        sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id,
        sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_key),
        expires_at=_expires(),
    ).to_dict()
    source = service.handle_stream(request)
    first = next(source)
    assert adapter.first_delta_sent.is_set()
    assert not adapter.completed.is_set()
    verifier = StreamSequenceVerifier(
        task_id="timing-stream",
        service_id="svc",
        provider_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_key,
    )
    assert verifier.accept_delta(first)["delta"] == "early"
    adapter.release.set()
    remaining = list(source)
    assert adapter.completed.is_set()
    assert verifier.accept_delta(remaining[0])["delta"] == " late"
    assert verifier.accept_terminal(remaining[1])["state"] == "succeeded"


def test_provider_stream_seals_deltas_retains_only_terminal_and_settles_once(tmp_path) -> None:
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_key = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _StreamingAdapter()
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc",
            mode="openai_compatible",
            public_model_alias="alias",
            base_url="http://127.0.0.1:1",
        ),
        adapter=adapter,  # type: ignore[arg-type]
        store=provider,
        task_store=orders,
        balance=balance,
        messaging_key=provider_key,
    )
    request = seal_task(
        body={
            "task_id": "task-stream",
            "service_id": "svc",
            "prompt": "UNIQUE_PRIVATE_PROMPT",
            "max_tokens": 8,
            "max_amount": 1,
            "reply_messaging_pub": peer_box.public_key_b64(consumer_key),
            "response_mode": "stream-v1",
            "stream_event_max_bytes": 1024,
        },
        task_id="task-stream",
        kind="llm_request",
        sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id,
        sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_key),
        expires_at=_expires(),
    ).to_dict()
    envelopes = list(service.handle_stream(request))
    assert len(envelopes) == 3
    verifier = StreamSequenceVerifier(
        task_id="task-stream",
        service_id="svc",
        provider_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_key,
    )
    assert [verifier.accept_delta(item)["delta"] for item in envelopes[:-1]] == [
        "first ",
        "世界",
    ]
    result = verifier.accept_terminal(envelopes[-1])
    assert result["output"] == "first 世界"
    record_text = (tmp_path / "orders" / "task-stream.json").read_text(encoding="utf-8")
    assert "UNIQUE_PRIVATE_PROMPT" not in record_text
    assert "first 世界" not in record_text
    assert STREAM_PROTOCOL_VERSION in record_text
    assert adapter.calls == 1

    # Same signed task replays only the retained terminal result and never
    # reruns inference, which makes disconnect recovery debit-safe.
    replay = list(service.handle_stream(request))
    assert replay == [envelopes[-1]]
    assert adapter.calls == 1

    settlement = sign_payload(
        {
            "kind": "llm_settlement",
            "task_id": "task-stream",
            "from_peer_id": consumer.peer_id,
            "to_peer_id": provider.peer_id,
            "amount": result["amount"],
            "service_id": "svc",
            "settlement_id": "settle:task-stream",
        },
        private_key_bytes=consumer.private_key_bytes,
    ).to_dict()
    first = service.settle_earning(settlement)
    second = service.settle_earning(settlement)
    assert first["event_id"] == second["event_id"] == "earning:task-stream"
    assert balance.summary()["earned"] == result["amount"]


def test_consumer_direct_stream_publishes_local_sse_and_keeps_settlement_terminal_only(
    tmp_path, monkeypatch,
) -> None:
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_key = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    manifest = LLMPackageManifest(
        package_id="svc",
        mode="openai_compatible",
        public_model_alias="alias",
        base_url="http://127.0.0.1:1",
    )
    provider.register_node(capabilities=[CAPABILITY], network_id="stream-net")
    provider.register_job_capacity(
        capabilities=[CAPABILITY],
        network_id="stream-net",
        capacity_units=1,
        max_concurrent=1,
        metadata={
            "llm_service": {
                "online": True,
                "service": manifest.public_dict(),
                "node_messaging_pub": peer_box.public_key_b64(provider_key),
                "capacity": {"available": 1},
                "delivery_protocols": ["complete-v1", "stream-v1"],
            },
        },
    )

    class FakePeerClient:
        settlements = 0

        def __init__(self, _endpoint, *, timeout_s):
            self.timeout_s = timeout_s

        def iter_post_ndjson(self, _path, signed, **_kwargs):
            outer, body = open_task(
                signed,
                recipient_peer_id=provider.peer_id,
                recipient_messaging_key=provider_key,
                expected_kind="llm_request",
            )
            assert body["response_mode"] == "stream-v1"
            reply_pub = str(body["reply_messaging_pub"])
            for sequence, delta in enumerate(("live ", "answer")):
                yield seal_stream_delta(
                    task_id=str(outer["task_id"]),
                    service_id="svc",
                    sequence=sequence,
                    delta=delta,
                    sender_peer_id=provider.peer_id,
                    recipient_peer_id=consumer.peer_id,
                    sender_signing_key=provider.private_key_bytes,
                    recipient_messaging_pub=reply_pub,
                    expires_at=_expires(),
                )
            yield seal_task(
                body={
                    "task_id": str(outer["task_id"]),
                    "state": "succeeded",
                    "service_id": "svc",
                    "model_alias": "alias",
                    "output": "live answer",
                    "input_tokens": 2,
                    "output_tokens": 2,
                    "duration_ms": 9,
                    "amount": 0.001,
                    "currency": "DEV_TASK_BALANCE",
                },
                task_id=str(outer["task_id"]),
                kind="llm_response",
                sender_peer_id=provider.peer_id,
                recipient_peer_id=consumer.peer_id,
                sender_signing_key=provider.private_key_bytes,
                recipient_messaging_pub=reply_pub,
                expires_at=_expires(),
            ).to_dict()

        def post_json(self, path, _payload, *, max_bytes):
            assert path == "/api/peer/llm/settlements"
            assert max_bytes > 0
            type(self).settlements += 1
            return {"ok": True}

    monkeypatch.setattr("rynmesh.peer_http.HttpPeerClient", FakePeerClient)
    app = FastAPI()
    install_llm_routes(
        app,
        store=consumer,
        home=tmp_path / "consumer",
        messaging_key=consumer_key,
        resolve_endpoint=lambda peer_id: "http://127.0.0.1:9" if peer_id == provider.peer_id else "",
        resolve_pubkey=lambda _peer_id: peer_box.public_key_b64(provider_key),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/local/llm/orders",
            json={
                "network_id": "stream-net",
                "provider_peer_id": provider.peer_id,
                "service_id": "svc",
                "prompt": "PRIVATE_CONSUMER_PROMPT",
                "max_tokens": 8,
                "transport": "direct",
                "response_mode": "stream-v1",
                "task_id": "consumer-stream",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["output"] == "live answer"
        with client.stream("GET", "/api/local/llm/orders/consumer-stream/events") as stream:
            event_text = stream.read().decode()
        assert "event: delta" in event_text
        assert '"delta":"live "' in event_text
        assert "event: complete" in event_text
        assert FakePeerClient.settlements == 1

    stored = (tmp_path / "consumer" / "llm" / "consumer-orders" / "consumer-stream.json").read_text()
    assert "PRIVATE_CONSUMER_PROMPT" not in stored
    assert '"delta"' not in stored
