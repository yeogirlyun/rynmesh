"""Tests for S01-I15 remote NDJSON stream byte limits and closed error propagation (T4, T5)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from test_agent_peer_binding import paired  # noqa: F401
from test_agent_resources import resources  # noqa: F401

from rynmesh import peer_http
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentStore
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package.routes import _delivery_error_code, install_llm_routes
from rynmesh.llm_package.stream_protocol import (
    seal_stream_delta,
)
from rynmesh.llm_package.task_protocol import seal_task
from rynmesh.peer_http import HttpPeerClient, PeerTransportError
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore
from rynmesh.transport import TransportError

EXPECTED_FEEDBACK = (
    "上一次回复超过大小上限，没有执行任何工具。请把大段写入拆成多次较小的 apply_patch/写文件调用"
)


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


class _RecordingStreamTransport:
    def __init__(self, chunks: list[bytes], *, fail_after_first: Exception | None = None) -> None:
        self.chunks = chunks
        self.fail_after_first = fail_after_first
        self.calls: list[dict[str, object]] = []

    def iter_post_bytes(self, url: str, body: bytes, **kwargs):
        self.calls.append({"url": url, "body": body, **kwargs})
        max_total = int(kwargs["max_total_bytes"])
        seen = 0
        for idx, chunk in enumerate(self.chunks):
            seen += len(chunk)
            if seen > max_total:
                raise TransportError("stream response too large", reason="too_large")
            yield chunk
            if idx == 0 and self.fail_after_first is not None:
                raise self.fail_after_first


def test_t5_iter_post_ndjson_total_limit_scales_with_max_output_tokens_up_to_32mib() -> None:
    """T5: iter_post_ndjson scales max_total_bytes as min(32 MiB, max(8 MiB, max_output_tokens * 16))."""
    assert peer_http.peer_stream_total_limit(None) == 8 * 1024 * 1024
    assert peer_http.peer_stream_total_limit(4096) == 8 * 1024 * 1024
    assert peer_http.peer_stream_total_limit(1_048_576) == 16 * 1024 * 1024
    assert peer_http.peer_stream_total_limit(4_000_000) == 32 * 1024 * 1024

    # 1) Default (omitted or small max_output_tokens) -> 8 MiB
    small_transport = _RecordingStreamTransport([b'{"ok":true}\n'])
    client = HttpPeerClient("http://127.0.0.1:9", transport=small_transport)  # type: ignore[arg-type]
    assert list(client.iter_post_ndjson("/stream", {"q": 1})) == [{"ok": True}]
    assert small_transport.calls[0]["max_total_bytes"] == 8 * 1024 * 1024

    # 2) Scaled (max_output_tokens = 1_048_576 -> 16 MiB): a ~10 MiB stream succeeds!
    line = (json.dumps({"d": "x" * (128 * 1024)}) + "\n").encode("utf-8")
    ten_mib_chunks = [line for _ in range(80)]  # ~10 MiB total (> 8 MiB, < 16 MiB)
    scaled_transport = _RecordingStreamTransport(ten_mib_chunks)
    scaled_client = HttpPeerClient("http://127.0.0.1:9", transport=scaled_transport)  # type: ignore[arg-type]
    events = list(scaled_client.iter_post_ndjson("/stream", {"q": 2}, max_output_tokens=1_048_576))
    assert len(events) == 80
    assert scaled_transport.calls[0]["max_total_bytes"] == 16 * 1024 * 1024

    # Same ~10 MiB stream fails with peer_stream_too_large under 4096 tokens (8 MiB cap)
    under_transport = _RecordingStreamTransport(ten_mib_chunks)
    under_client = HttpPeerClient("http://127.0.0.1:9", transport=under_transport)  # type: ignore[arg-type]
    with pytest.raises(PeerTransportError, match="^peer_stream_too_large$"):
        list(under_client.iter_post_ndjson("/stream", {"q": 3}, max_output_tokens=4096))

    # 3) Capped at 32 MiB even when max_output_tokens * 16 > 32 MiB
    cap_transport = _RecordingStreamTransport([b'{"done":true}\n'])
    cap_client = HttpPeerClient("http://127.0.0.1:9", transport=cap_transport)  # type: ignore[arg-type]
    assert list(cap_client.iter_post_ndjson("/stream", {"q": 4}, max_output_tokens=5_000_000)) == [{"done": True}]
    assert cap_transport.calls[0]["max_total_bytes"] == 32 * 1024 * 1024


def test_t4_peer_stream_too_large_propagates_closed_code_and_feedback_retries_distinct_from_interruption(
    paired, resources, tmp_path, monkeypatch  # noqa: F811
) -> None:
    """T4: peer_stream_too_large reaches bridge via closed code and triggers feedback retry once, distinct from stream interruption."""
    assert _delivery_error_code(PeerTransportError("peer_stream_too_large"), transport="direct") == "peer_stream_too_large"
    assert _delivery_error_code(PeerTransportError("peer_stream_event_too_large"), transport="direct") == "peer_stream_too_large"

    p = paired
    # Advertise stream-v1 delivery protocol and high max_output_tokens on Alice's shared service
    p.providers["a"].manifest.capabilities = [*p.providers["a"].manifest.capabilities, "streaming"]
    p.providers["a"].manifest.max_output_tokens = 786432  # 786432 * 16 == 12 MiB
    p.providers["a"].manifest.context_window = 2_000_000
    p.providers["a"].manifest.pricing.input_per_1k = 0.0
    p.providers["a"].manifest.pricing.output_per_1k = 0.0
    p.consumer.refresh(p.alice.peer_id)

    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    monkeypatch.setattr(store, "lan_peer_endpoints", lambda *_a, **_kw: ["http://127.0.0.1:19999"])
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    app.state.nas_plugin = resources[1]
    install_llm_routes(
        app,
        store=store,
        home=store.home,
        messaging_key=p.bob.messaging_private,
        resolve_endpoint=lambda _: "http://127.0.0.1:19999",
        resolve_pubkey=lambda _: "",
    )
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

    mode = {"kind": "too_large", "observed_limits": [], "attempt": 0}

    def fake_peer_stream(url, body, *, timeout_s, on_event, response_factory=None):
        mode["attempt"] += 1
        outer_payload = body["payload"]
        task_id = outer_payload["task_id"]
        service_id = p.providers["a"].manifest.package_id
        # Emit one valid encrypted stream event first so sequence == 1 (> 0)
        first_event = seal_task(
            body={
                "task_id": task_id,
                "service_id": service_id,
                "sequence": 0,
                "chunk": {"choices": [{"index": 0, "delta": {"content": "part1"}}]},
            },
            task_id=task_id,
            kind="llm_stream",
            sender_peer_id=p.store.peer_id,
            recipient_peer_id=store.peer_id,
            sender_signing_key=p.store.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(p.bob.messaging_private),
            expires_at=_expires(),
        ).to_dict()
        on_event(first_event)
        if mode["kind"] == "too_large" and mode["attempt"] == 1:
            raise PeerTransportError("peer_stream_too_large")
        if mode["kind"] == "interrupted":
            raise PeerTransportError("peer_stream_failed")
        return p.providers["a"].handle(body, on_event=lambda _: None)

    monkeypatch.setattr("rynmesh.llm_package.routes.peer_stream", fake_peer_stream)
    # Prevent P2P fallback from masking direct HTTP stream behavior
    p2p_calls = []

    async def fail_p2p(**_kwargs):
        p2p_calls.append(True)
        raise AssertionError("P2P fallback should not run after stream started or exceeded size limit")

    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", fail_p2p)

    try:
        # Part A: peer_stream_too_large -> feedback retry once -> succeeds on attempt 2!
        created = runtime.create(
            {
                "message": "Write a large module.",
                "expected_destination_revision": peer_model["destination_revision"],
            }
        )
        task_id = created["id"]
        runtime.run_once()
        after_first = runtime.store.read()["tasks"][task_id]
        assert after_first["status"] == "queued"
        assert after_first["model_retry_count"] == 1
        assert after_first.get("rejected_response_note") == EXPECTED_FEEDBACK
        rejected_ev = next(e for e in after_first["events"] if e["type"] == "model_response_rejected")
        assert rejected_ev["detail"] == EXPECTED_FEEDBACK
        assert p2p_calls == []

        # Attempt 2 succeeds and receives the feedback note in the prompt sent to Alice
        runtime.run_once()
        after_second = runtime.get(task_id)
        assert after_second["status"] == "completed"
        assert any(EXPECTED_FEEDBACK in str(m.get("content", "")) for m in p.traces[-1]["messages"])

        # Part B: stream interruption (mid-stream drop, NOT too_large) -> agent_model_stream_interrupted (NOT feedback retry!)
        mode["kind"] = "interrupted"
        with pytest.raises(AgentError) as interrupted_exc:
            bridge.complete(
                peer_model["id"],
                [{"role": "user", "content": "Stream then drop"}],
                [],
                lambda: False,
                on_event=lambda _ev: None,
                expected_destination=catalog.destination(peer_model),
            )
        assert not isinstance(interrupted_exc.value, AgentModelResponseError)
        assert str(interrupted_exc.value) == "agent_model_stream_interrupted"

        # Part C: Also verify stream-v1 iter_post_ndjson inside execute_order scales max_total_bytes by max_tokens
        seen_ndjson_limits: list[int] = []

        def fake_iter_post_ndjson(self, path, payload, *, max_event_bytes=256 * 1024, max_total_bytes=None, max_output_tokens=None):
            effective = max_total_bytes if max_total_bytes is not None else peer_http.peer_stream_total_limit(max_output_tokens)
            seen_ndjson_limits.append(effective)
            task_req_id = payload["payload"]["task_id"]
            delta_env = seal_stream_delta(
                task_id=task_req_id,
                service_id=p.providers["a"].manifest.package_id,
                sequence=0,
                delta="hello",
                sender_peer_id=p.store.peer_id,
                recipient_peer_id=store.peer_id,
                sender_signing_key=p.store.private_key_bytes,
                recipient_messaging_pub=peer_box.public_key_b64(p.bob.messaging_private),
                expires_at=_expires(),
            )
            yield delta_env
            raise PeerTransportError("peer_stream_too_large")

        monkeypatch.setattr(HttpPeerClient, "iter_post_ndjson", fake_iter_post_ndjson)
        monkeypatch.setattr("rynmesh.transport.get_transport", lambda: _RecordingStreamTransport([]))
        import asyncio

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as http_exc:
            asyncio.run(
                app.state.llm_agent_services["execute_order"](
                    {
                        "network_id": catalog.network,
                        "provider_peer_id": p.alice.peer_id,
                        "service_id": p.providers["a"].manifest.package_id,
                        "prompt": "hi",
                        "max_tokens": 786432,
                        "response_mode": "stream-v1",
                        "transport": "direct",
                        "expected_model_binding": peer_model["expected_model_binding"],
                    }
                )
            )
        assert seen_ndjson_limits == [786432 * 16]
        assert http_exc.value.detail == "peer_delivery_failed:peer_stream_too_large"
    finally:
        app.state.llm_shutdown()
