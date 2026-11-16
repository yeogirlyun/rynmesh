"""Tests for S01-I27: stream-v2 preview truncation, 4 MiB terminal limit, v1 compatibility, and broker eviction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from rynmesh.llm_package import stream_protocol
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
from rynmesh.llm_package.task_protocol import TaskOrderStore, TaskProtocolError, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


class _LargeStreamingAdapter:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls = 0
        self.finished_full_inference = False

    def health(self) -> dict[str, Any]:
        return {"ok": True, "model": "fake-large"}

    def cancel(self, _task_id: str) -> bool:
        return True

    def infer_stream(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float, on_delta: Any) -> dict[str, Any]:
        self.calls += 1
        for chunk in self.chunks:
            on_delta(chunk)
        self.finished_full_inference = True
        full_text = "".join(self.chunks)
        return {
            "text": full_text,
            "input_tokens": 16,
            "output_tokens": max(1, len(full_text) // 4),
            "duration_ms": 42,
        }


def test_t1_v2_constants_negotiation_and_verifier_broker_truncation(tmp_path) -> None:
    """T1: Verify v2 protocol constants, verifier non-throwing truncation + 4 MiB terminal limit, and broker truncation."""
    assert getattr(stream_protocol, "STREAM_PROTOCOL_VERSION_V2", None) == "rynmesh.llm.stream.v2"
    assert getattr(stream_protocol, "DEFAULT_MAX_TERMINAL_BYTES", None) == 4 * 1024 * 1024
    assert getattr(stream_protocol, "PREVIEW_MAX_OUTPUT_BYTES", None) == 1024 * 1024

    v2 = stream_protocol.STREAM_PROTOCOL_VERSION_V2
    max_terminal = stream_protocol.DEFAULT_MAX_TERMINAL_BYTES

    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    consumer_pub = peer_box.public_key_b64(consumer_key)

    verifier = StreamSequenceVerifier(
        task_id="t1-v2",
        service_id="svc",
        provider_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_key,
        max_event_bytes=DEFAULT_MAX_EVENT_BYTES,
        max_output_bytes=10,
        max_terminal_bytes=max_terminal,
        protocol_version=v2,
    )

    def make_v2_delta(seq: int, text: str) -> dict[str, Any]:
        return seal_stream_delta(
            task_id="t1-v2",
            service_id="svc",
            sequence=seq,
            delta=text,
            stream_version=v2,
            sender_peer_id=provider.peer_id,
            recipient_peer_id=consumer.peer_id,
            sender_signing_key=provider.private_key_bytes,
            recipient_messaging_pub=consumer_pub,
            expires_at=_expires(),
        )

    assert verifier.accept_delta(make_v2_delta(0, "123456")) == {"sequence": 0, "delta": "123456"}
    assert not verifier.truncated
    # Exceeding max_output_bytes (6 + 6 > 10) marks truncated without raising, and subsequent deltas are ignored
    second = verifier.accept_delta(make_v2_delta(1, "789012"))
    assert verifier.truncated is True
    assert second.get("truncated") is True
    third = verifier.accept_delta(make_v2_delta(2, "more"))
    assert third.get("ignored") is True

    # Terminal envelope > 256 KiB (e.g. 512 KiB) succeeds under v2's 4 MiB terminal limit
    big_text = "A" * (512 * 1024)
    terminal_env = seal_task(
        body={
            "task_id": "t1-v2",
            "state": "succeeded",
            "service_id": "svc",
            "stream_protocol": v2,
            "output": big_text,
            "input_tokens": 4,
            "output_tokens": 1024,
            "duration_ms": 10,
            "amount": 0.001,
        },
        task_id="t1-v2",
        kind="llm_response",
        sender_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        sender_signing_key=provider.private_key_bytes,
        recipient_messaging_pub=consumer_pub,
        expires_at=_expires(),
    ).to_dict()
    assert len(json.dumps(terminal_env).encode("utf-8")) > DEFAULT_MAX_EVENT_BYTES
    opened = verifier.accept_terminal(terminal_env)
    assert opened["output"] == big_text

    # StreamEventBroker marks truncated instead of raising when snapshot exceeds max_snapshot_bytes
    broker = StreamEventBroker(max_tasks=4, max_events_per_task=16, max_snapshot_bytes=8)
    broker.publish("t1-broker", {"event": "delta", "sequence": 0, "delta": "12345"})
    broker.publish("t1-broker", {"event": "delta", "sequence": 1, "delta": "67890"})
    broker.publish("t1-broker", {"event": "delta", "sequence": 2, "delta": "ignored"})
    replayed = broker.replay("t1-broker")
    assert [e for e in replayed if e.get("event") == "state" and e.get("state") == "preview_truncated"] == [
        {"event": "state", "state": "preview_truncated"}
    ]
    assert [e["delta"] for e in replayed if e.get("event") == "delta"] == ["12345"]


def test_t2_v2_two_mib_output_truncates_preview_once_and_completes_with_full_terminal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2: Under v2, a 2 MiB output emits preview_truncated once, finishes inference, and returns the full 2 MiB terminal output."""
    v2 = getattr(stream_protocol, "STREAM_PROTOCOL_VERSION_V2", "rynmesh.llm.stream.v2")
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_key = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")

    # 64 chunks * 32 KiB = 2 MiB total output (> 1 MiB preview limit, < 4 MiB terminal wire limit)
    chunks = [f"{idx:04d}" + ("x" * (32 * 1024 - 4)) for idx in range(64)]
    expected_full_output = "".join(chunks)
    assert len(expected_full_output.encode("utf-8")) == 2 * 1024 * 1024

    adapter = _LargeStreamingAdapter(chunks)
    manifest = LLMPackageManifest.from_dict(
        {
            "package_id": "svc-v2",
            "mode": "openai_compatible",
            "public_model_alias": "alias-v2",
            "base_url": "http://127.0.0.1:1",
            "context_window": 1_048_576,
            "max_output_tokens": 600_000,
            "pricing": {
                "currency": "DEV_TASK_BALANCE",
                "input_per_1k": 0.0,
                "output_per_1k": 0.0,
                "minimum": 0.001,
                "maximum_per_task": 10.0,
            },
        }
    )
    provider_orders = TaskOrderStore(tmp_path / "provider-orders")
    provider_balance = TaskBalanceLedger(tmp_path / "provider-balance.json")
    service = ProviderService(
        manifest=manifest,
        adapter=adapter,  # type: ignore[arg-type]
        store=provider,
        task_store=provider_orders,
        balance=provider_balance,
        messaging_key=provider_key,
        access_check=lambda *_args: None,
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

    consumer_app = FastAPI()
    install_llm_routes(
        consumer_app,
        store=consumer,
        home=tmp_path / "consumer",
        messaging_key=consumer_key,
        resolve_endpoint=lambda peer_id: "http://127.0.0.1:9" if peer_id == provider.peer_id else "",
        resolve_pubkey=lambda _peer_id: peer_box.public_key_b64(provider_key),
    )

    with TestClient(consumer_app) as c_client:
        from rynmesh.peer_http import HttpPeerClient

        class _StreamTransport:
            def iter_post_bytes(self, _url: str, body: bytes, *, timeout_s: float, max_chunk_bytes: int, max_total_bytes: int, headers=None):
                signed = json.loads(body.decode("utf-8"))
                total = 0
                for env in service.handle_stream(signed):
                    raw_line = (json.dumps(env, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
                    total += len(raw_line)
                    assert total <= max_total_bytes
                    for off in range(0, len(raw_line), max_chunk_bytes):
                        yield raw_line[off : off + max_chunk_bytes]

            def post_bytes(self, url: str, body: bytes, *, timeout_s: float, max_bytes: int, headers=None) -> bytes:
                assert url.endswith("/api/peer/llm/settlements")
                earning = service.settle_earning(json.loads(body.decode("utf-8")))
                return json.dumps({"ok": True, "event_id": earning["event_id"]}).encode("utf-8")

        class RealNdjsonPeerClient(HttpPeerClient):
            def __init__(self, endpoint: str, *, timeout_s: float = 20.0, transport=None) -> None:
                super().__init__(endpoint, timeout_s=timeout_s, transport=_StreamTransport())

        monkeypatch.setattr("rynmesh.peer_http.HttpPeerClient", RealNdjsonPeerClient)

        response = c_client.post(
            "/api/local/llm/orders",
            json={
                "network_id": "stream-net",
                "provider_peer_id": provider.peer_id,
                "service_id": "svc-v2",
                "prompt": "Write a 2 MiB technical report",
                "max_tokens": 550_000,
                "transport": "direct",
                "response_mode": "stream-v1",
                "task_id": "v2-2mib-order",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["state"] == "succeeded"
        assert data["output"] == expected_full_output
        assert data["transport_evidence"]["stream_protocol"] == v2
        assert adapter.finished_full_inference is True

        with c_client.stream("GET", "/api/local/llm/orders/v2-2mib-order/events") as sse_resp:
            sse_body = sse_resp.read().decode("utf-8")
        assert sse_body.count('"state":"preview_truncated"') == 1
        assert "event: complete" in sse_body

    provider_record = (tmp_path / "provider-orders" / "v2-2mib-order.json").read_text(encoding="utf-8")
    assert v2 in provider_record


def test_t3_v1_peer_still_enforces_128_kib_limit(tmp_path) -> None:
    """T3: When either peer uses v1, the 128 KiB output limit is still strictly enforced."""
    v2 = getattr(stream_protocol, "STREAM_PROTOCOL_VERSION_V2", "rynmesh.llm.stream.v2")
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_key = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_key = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")

    # 1) v1 consumer -> v2 provider: 5 * 32 KiB = 160 KiB (> 128 KiB) fails at provider
    chunks = ["y" * (32 * 1024) for _ in range(5)]
    adapter = _LargeStreamingAdapter(chunks)
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc",
            mode="openai_compatible",
            public_model_alias="alias",
            base_url="http://127.0.0.1:1",
            context_window=262_144,
            max_output_tokens=100_000,
        ),
        adapter=adapter,  # type: ignore[arg-type]
        store=provider,
        task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=provider_key,
        access_check=lambda *_args: None,
    )
    v1_request = seal_task(
        body={
            "task_id": "v1-over-128k",
            "service_id": "svc",
            "prompt": "long output",
            "max_tokens": 4096,
            "max_amount": 0.5,
            "reply_messaging_pub": peer_box.public_key_b64(consumer_key),
            "response_mode": "stream-v1",
            "stream_event_max_bytes": DEFAULT_MAX_EVENT_BYTES,
        },
        task_id="v1-over-128k",
        kind="llm_request",
        sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id,
        sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_key),
        expires_at=_expires(),
    ).to_dict()
    envelopes = list(service.handle_stream(v1_request))
    verifier_v1 = StreamSequenceVerifier(
        task_id="v1-over-128k",
        service_id="svc",
        provider_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_key,
    )
    for item in envelopes[:-1]:
        verifier_v1.accept_delta(item)
    terminal = verifier_v1.accept_terminal(envelopes[-1])
    assert terminal["state"] == "failed"

    # 2) v2 consumer -> v1 provider: provider sends v1 deltas, so consumer verifier falls back to 128 KiB and raises
    consumer_pub = peer_box.public_key_b64(consumer_key)
    verifier_negotiating = StreamSequenceVerifier(
        task_id="v2-consumer-v1-provider",
        service_id="svc",
        provider_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_key,
        max_event_bytes=DEFAULT_MAX_EVENT_BYTES,
        max_output_bytes=getattr(stream_protocol, "PREVIEW_MAX_OUTPUT_BYTES", 1024 * 1024),
        protocol_version=v2,
        negotiate=True,
    )
    for seq in range(4):
        v1_delta = seal_stream_delta(
            task_id="v2-consumer-v1-provider",
            service_id="svc",
            sequence=seq,
            delta="z" * (32 * 1024),
            sender_peer_id=provider.peer_id,
            recipient_peer_id=consumer.peer_id,
            sender_signing_key=provider.private_key_bytes,
            recipient_messaging_pub=consumer_pub,
            expires_at=_expires(),
        )
        verifier_negotiating.accept_delta(v1_delta)
    assert verifier_negotiating.output_bytes == DEFAULT_MAX_OUTPUT_BYTES
    overflow_v1_delta = seal_stream_delta(
        task_id="v2-consumer-v1-provider",
        service_id="svc",
        sequence=4,
        delta="!",
        sender_peer_id=provider.peer_id,
        recipient_peer_id=consumer.peer_id,
        sender_signing_key=provider.private_key_bytes,
        recipient_messaging_pub=consumer_pub,
        expires_at=_expires(),
    )
    with pytest.raises(TaskProtocolError, match="stream output exceeds total limit"):
        verifier_negotiating.accept_delta(overflow_v1_delta)


def test_t4_stream_broker_evicts_finished_task_before_in_progress_task() -> None:
    """T4: When the broker ring is full, finished tasks (_expires set) are evicted before older in-progress tasks."""
    broker = StreamEventBroker(max_tasks=3, max_events_per_task=16)

    # running-1 is the OLDEST task, still in progress (no terminal event -> not in _expires)
    broker.publish("running-1", {"event": "state", "state": "running"})
    broker.publish("running-1", {"event": "delta", "sequence": 0, "delta": "part-0 "})

    # done-2 arrives second and finishes (_expires is set)
    broker.publish("done-2", {"event": "state", "state": "running"})
    broker.publish("done-2", {"event": "complete", "state": "succeeded"})
    assert "done-2" in broker._expires
    assert "running-1" not in broker._expires

    # running-3 arrives third and stays in progress
    broker.publish("running-3", {"event": "state", "state": "running"})
    broker.publish("running-3", {"event": "delta", "sequence": 0, "delta": "r3"})

    # Adding running-4 when max_tasks=3 must evict done-2 (finished), NOT running-1 (oldest in-progress)
    broker.publish("running-4", {"event": "state", "state": "running"})
    assert broker.replay("done-2") == []
    assert len(broker.replay("running-1")) == 2
    assert len(broker.replay("running-3")) == 2

    # running-1 can continue publishing contiguous deltas without losing its preview
    broker.publish("running-1", {"event": "delta", "sequence": 1, "delta": "part-1"})
    assert [e.get("delta") for e in broker.replay("running-1") if e.get("event") == "delta"] == ["part-0 ", "part-1"]

    # Now all 3 tasks (running-1, running-3, running-4) are in progress; adding running-5 evicts oldest (running-1)
    broker.publish("running-5", {"event": "state", "state": "running"})
    assert broker.replay("running-1") == []
    assert len(broker.replay("running-3")) == 2
    assert len(broker.replay("running-4")) == 1
    assert len(broker.replay("running-5")) == 1
