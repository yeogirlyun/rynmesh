from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh import peer_transit_service
from rynmesh.crypto import SignedPayload
from rynmesh.llm_package.p2p import (
    apply_remote_signal,
    gather_signal,
    new_connection,
)
from rynmesh.peer_transit import (
    PathMetrics,
    PeerTransitError,
    RouteManager,
    RoutePolicy,
    RouteState,
    TransitCipher,
    TransitSessionOpen,
    messaging_public_key,
    new_session_id,
    receive_encrypted_stream,
    relay_bidirectional_once,
    send_encrypted_stream,
    sign_session_open,
    transit_evidence,
    verify_session_open,
)
from rynmesh.peer_transit_service import (
    CAPACITY_MAX_AGE_HOURS,
    DEFAULT_CAPACITY_REFRESH_S,
    PeerTransitWorker,
    send_file_adaptive,
    send_file_direct,
)
from rynmesh.registry import FilePeerRegistry
from rynmesh.store import RynmeshStore
from scripts import audit_peer_transit as audit_module
from scripts.audit_peer_transit import AuditError, audit_peer_transit, audit_soak_report


def _future(seconds: float = 300) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_worker_refreshes_capacity_before_discovery_record_expires(tmp_path, monkeypatch) -> None:
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    worker = PeerTransitWorker(store, role="transit", network_id="refresh-test")
    assert DEFAULT_CAPACITY_REFRESH_S < CAPACITY_MAX_AGE_HOURS * 3600

    registrations: list[int] = []
    polls = 0
    stop = threading.Event()

    def register() -> dict[str, str]:
        registrations.append(len(registrations) + 1)
        return {"status": "registered"}

    def serve_once() -> int:
        nonlocal polls
        polls += 1
        if polls == 2:
            stop.set()
        return 0

    clock = iter((0.0, 1.0, DEFAULT_CAPACITY_REFRESH_S + 1.0))
    monkeypatch.setattr(worker, "register", register)
    monkeypatch.setattr(worker, "serve_once", serve_once)
    monkeypatch.setattr(peer_transit_service.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(peer_transit_service.time, "sleep", lambda _seconds: None)

    worker.serve_forever(stop_event=stop)

    assert registrations == [1, 2]


def test_soak_auditor_fails_closed_on_resource_or_lifecycle_gaps(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_module,
        "audit_peer_transit",
        lambda value: {"protocol_version": "rynmesh.peer-transit.v1"},
    )
    report = {
        "result": "pass",
        "completed_duration": True,
        "duration_target_s": 86400,
        "elapsed_s": 86401,
        "sessions_completed": 100,
        "failures": [],
        "plaintext_found_on_transit": False,
        "memory_growth_bytes": 1024,
        "memory_growth_limit_bytes": 2048,
        "partial_files": 0,
        "worker_threads_stopped": True,
        "transit_frames": 300,
        "transit_bytes": 4096,
        "last_evidence": {"session_id": "abc"},
    }
    assert audit_soak_report(
        report,
        require_duration_s=86400,
        min_sessions=100,
    )["ok"] is True

    for key, bad_value, message in (
        ("result", "running", "complete"),
        ("failures", [{"error": "boom"}], "failed"),
        ("plaintext_found_on_transit", True, "plaintext"),
        ("memory_growth_bytes", 4096, "memory"),
        ("partial_files", 1, "partial"),
        ("worker_threads_stopped", False, "threads"),
    ):
        tampered = {**report, key: bad_value}
        with pytest.raises(AuditError, match=message):
            audit_soak_report(
                tampered,
                require_duration_s=86400,
                min_sessions=100,
            )


def test_signed_session_open_binds_source_target_expiry_and_one_hop(tmp_path) -> None:
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "network")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "network")
    ephemeral = X25519PrivateKey.generate()
    session = TransitSessionOpen(
        session_id=new_session_id(),
        source_peer_id=source.peer_id,
        target_peer_id=target.peer_id,
        source_ephemeral_pub=messaging_public_key(ephemeral),
        expires_at=_future(),
    )
    signed = sign_session_open(session, source_signing_key=source.private_key_bytes)
    assert verify_session_open(signed, expected_target_peer_id=target.peer_id) == session

    forged = signed.to_dict()
    forged["payload"]["target_peer_id"] = source.peer_id
    with pytest.raises(PeerTransitError, match="invalid signed"):
        verify_session_open(forged, expected_target_peer_id=target.peer_id)

    expired = TransitSessionOpen(
        **{**session.to_dict(), "expires_at": _future(-1)}
    )
    with pytest.raises(PeerTransitError, match="expired"):
        verify_session_open(
            sign_session_open(expired, source_signing_key=source.private_key_bytes)
        )

    recursive = TransitSessionOpen(**{**session.to_dict(), "hop_limit": 2})
    with pytest.raises(PeerTransitError, match="hop_limit=1"):
        verify_session_open(
            sign_session_open(recursive, source_signing_key=source.private_key_bytes)
        )


def test_transit_cipher_is_end_to_end_authenticated_and_replay_safe(tmp_path) -> None:
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "network")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "network")
    target_key = X25519PrivateKey.generate()
    session_id = new_session_id()
    source_cipher, ephemeral = TransitCipher.for_source(
        session_id=session_id,
        source_peer_id=source.peer_id,
        target_peer_id=target.peer_id,
        target_messaging_pub=messaging_public_key(target_key),
    )
    session = TransitSessionOpen(
        session_id=session_id,
        source_peer_id=source.peer_id,
        target_peer_id=target.peer_id,
        source_ephemeral_pub=messaging_public_key(ephemeral),
        expires_at=_future(),
    )
    target_cipher = TransitCipher.for_target(
        session=session,
        target_messaging_key=target_key,
    )

    marker = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
    frame = source_cipher.seal("request", marker, final=True)
    assert marker not in frame

    tampered = bytearray(frame)
    tampered[-1] ^= 1
    with pytest.raises(PeerTransitError, match="authentication failed"):
        target_cipher.open("request", bytes(tampered))

    plaintext, header = target_cipher.open("request", frame)
    assert plaintext == marker
    assert header.final is True
    with pytest.raises(PeerTransitError, match="replayed"):
        target_cipher.open("request", frame)


def test_route_manager_uses_hysteresis_for_degrade_transit_and_recovery() -> None:
    manager = RouteManager(RoutePolicy(
        degraded_hold_s=30,
        transit_min_hold_s=60,
        recovery_hold_s=120,
        recovery_probe_count=5,
    ))
    healthy = PathMetrics(reachable=True, rtt_p95_ms=40, loss_ratio=0)
    poor = PathMetrics(reachable=True, rtt_p95_ms=320, loss_ratio=0.18)
    transit = PathMetrics(reachable=True, rtt_p95_ms=80, loss_ratio=0.01)

    assert manager.update(direct=healthy, transit=transit, now_monotonic=0) == "direct"
    assert manager.update(direct=poor, transit=transit, now_monotonic=1) == "direct"
    assert manager.state == RouteState.DEGRADED
    assert manager.update(direct=poor, transit=transit, now_monotonic=31) == "peer_transit"
    assert manager.state == RouteState.PEER_TRANSIT

    # Minimum hold prevents an immediate return and recovery itself has a
    # second stability window.
    assert manager.update(direct=healthy, transit=transit, now_monotonic=60) == "peer_transit"
    assert manager.update(direct=healthy, transit=transit, now_monotonic=92) == "peer_transit"
    assert manager.state == RouteState.RECOVERING
    manager.update(direct=healthy, transit=transit, now_monotonic=150)
    manager.update(direct=healthy, transit=transit, now_monotonic=180)
    manager.update(direct=healthy, transit=transit, now_monotonic=211)
    assert manager.update(direct=healthy, transit=transit, now_monotonic=212) == "direct"
    assert manager.state == RouteState.DIRECT


def test_route_manager_hard_failure_switches_without_quality_hold() -> None:
    manager = RouteManager()
    failed = PathMetrics(
        reachable=False,
        rtt_p95_ms=0,
        loss_ratio=1,
        consecutive_failures=3,
    )
    transit = PathMetrics(reachable=True, rtt_p95_ms=90, loss_ratio=0.01)
    assert manager.update(direct=failed, transit=transit, now_monotonic=10) == "peer_transit"
    assert manager.events[-1]["reason"] == "hard_failure"


def test_route_policy_loads_validated_operator_thresholds(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_TRANSIT_LATENCY_THRESHOLD_MS", "180")
    monkeypatch.setenv("RYNMESH_TRANSIT_LOSS_THRESHOLD", "0.12")
    monkeypatch.setenv("RYNMESH_TRANSIT_DEGRADED_HOLD_S", "15")
    manager = RouteManager()
    assert manager.policy.latency_threshold_ms == 180
    assert manager.policy.loss_threshold == 0.12
    assert manager.policy.degraded_hold_s == 15

    monkeypatch.setenv("RYNMESH_TRANSIT_LOSS_THRESHOLD", "1.5")
    with pytest.raises(PeerTransitError, match="ratios"):
        RouteManager()


def test_adaptive_sender_retries_via_peer_after_hard_direct_failure(monkeypatch) -> None:
    from rynmesh import peer_transit_service as service

    calls: list[str] = []
    direct_timeouts: list[float] = []

    def fail_direct(*args, **kwargs):
        calls.append("direct")
        direct_timeouts.append(kwargs["timeout_s"])
        raise PeerTransitError("direct ICE failed")

    def pass_transit(*args, **kwargs):
        calls.append("peer_transit")
        return {"path_mode": "peer_transit", "result": "pass"}

    monkeypatch.setattr(service, "send_file_direct", fail_direct)
    monkeypatch.setattr(service, "send_file_via_peer", pass_transit)
    result = service.send_file_adaptive(
        object(),
        "unused.bin",
        target_peer_id="target",
        relay_peer_id="relay",
    )
    assert calls == ["direct", "peer_transit"]
    assert direct_timeouts == [8.0]
    assert result["direct_attempt_timeout_s"] == 8.0
    assert result["selected_path"] == "peer_transit"
    assert "direct ICE failed" in result["direct_fallback_error"]
    assert result["route_events"][-1]["reason"] == "hard_failure"

    with pytest.raises(PeerTransitError, match="within"):
        service.send_file_adaptive(
            object(),
            "unused.bin",
            target_peer_id="target",
            relay_peer_id="relay",
            direct_attempt_timeout_s=11,
        )


def test_two_direct_ice_legs_forward_only_ciphertext_without_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)

    source_store = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "network")
    transit_store = RynmeshStore(home=tmp_path / "transit", network_dir=tmp_path / "network")
    target_store = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "network")
    target_key = X25519PrivateKey.generate()
    marker = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
    body = marker + (b"-opaque-payload" * 32768)

    async def scenario() -> dict:
        source_connection = new_connection(controlling=True)
        transit_left = new_connection(controlling=False)
        transit_right = new_connection(controlling=True)
        target_connection = new_connection(controlling=False)
        connections = [source_connection, transit_left, transit_right, target_connection]
        try:
            signals = await asyncio.gather(*(gather_signal(item) for item in connections))
            await asyncio.gather(
                apply_remote_signal(source_connection, signals[1]),
                apply_remote_signal(transit_left, signals[0]),
                apply_remote_signal(transit_right, signals[3]),
                apply_remote_signal(target_connection, signals[2]),
            )
            await asyncio.gather(*(item.connect() for item in connections))

            session_id = new_session_id()
            source_cipher, ephemeral = TransitCipher.for_source(
                session_id=session_id,
                source_peer_id=source_store.peer_id,
                target_peer_id=target_store.peer_id,
                target_messaging_pub=messaging_public_key(target_key),
            )
            session = TransitSessionOpen(
                session_id=session_id,
                source_peer_id=source_store.peer_id,
                target_peer_id=target_store.peer_id,
                source_ephemeral_pub=messaging_public_key(ephemeral),
                expires_at=_future(),
            )
            signed = sign_session_open(
                session,
                source_signing_key=source_store.private_key_bytes,
            )
            verified = verify_session_open(
                SignedPayload.from_dict(signed.to_dict()),
                expected_target_peer_id=target_store.peer_id,
            )
            target_cipher = TransitCipher.for_target(
                session=verified,
                target_messaging_key=target_key,
            )

            captured_frames: list[bytes] = []
            target_received = bytearray()
            source_response = bytearray()

            async def target_side() -> dict:
                received = await receive_encrypted_stream(
                    target_connection,
                    target_cipher,
                    direction="request",
                    sink=target_received.extend,
                    timeout_s=10,
                )
                response = json_bytes({"received_sha256": received["sha256"]})
                await send_encrypted_stream(
                    target_connection,
                    target_cipher,
                    direction="response",
                    chunks=[response],
                    timeout_s=10,
                )
                return received

            relay_task = asyncio.create_task(relay_bidirectional_once(
                transit_left,
                transit_right,
                session_id=session_id,
                timeout_s=10,
                audit_frame=captured_frames.append,
            ))
            target_task = asyncio.create_task(target_side())
            source_sent = await send_encrypted_stream(
                source_connection,
                source_cipher,
                direction="request",
                chunks=(body[index:index + 128 * 1024] for index in range(0, len(body), 128 * 1024)),
                timeout_s=10,
            )
            await receive_encrypted_stream(
                source_connection,
                source_cipher,
                direction="response",
                sink=source_response.extend,
                timeout_s=10,
            )
            counters, target_received_evidence = await asyncio.gather(relay_task, target_task)

            assert bytes(target_received) == body
            assert marker not in b"".join(captured_frames)
            response_payload = __import__("json").loads(source_response)
            assert response_payload["received_sha256"] == source_sent["sha256"]
            assert target_received_evidence["sha256"] == source_sent["sha256"]

            return transit_evidence(
                source_peer_id=source_store.peer_id,
                transit_peer_id=transit_store.peer_id,
                target_peer_id=target_store.peer_id,
                source_connection=source_connection,
                target_connection=transit_right,
                counters=counters,
                source_sha256=source_sent["sha256"],
                target_sha256=target_received_evidence["sha256"],
                plaintext_found_on_transit=False,
            )
        finally:
            await asyncio.gather(*(item.close() for item in connections))

    evidence = asyncio.run(scenario())
    assert evidence["result"] == "pass"
    assert evidence["path_mode"] == "peer_transit"
    assert evidence["ice_relay_candidate_used"] is False
    assert evidence["transit_rx_bytes"] > len(body)
    assert evidence["transit_tx_bytes"] > len(body)
    assert evidence["source_sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()


def json_bytes(value: dict) -> bytes:
    import json

    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def test_registry_signaling_carries_no_file_body_and_workers_stream_via_peer(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)
    network_id = "three-node-peer-transit"
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    relay = RynmeshStore(home=tmp_path / "relay", network_dir=tmp_path / "relay-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    source.registry = registry
    relay.registry = registry
    target.registry = registry

    marker = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
    source_file = tmp_path / "large-secret.bin"
    source_file.write_bytes(marker + (b"0123456789abcdef" * 131072))
    captured: list[bytes] = []
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=20,
        audit_frame=captured.append,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=tmp_path / "target-inbox",
        timeout_s=20,
    )
    relay_worker.register()
    target_worker.register()
    adaptive_manager = RouteManager(RoutePolicy(degraded_hold_s=30))
    poor = PathMetrics(True, 340, 0.18)
    good_transit = PathMetrics(True, 70, 0.01)
    adaptive_manager.update(direct=poor, transit=good_transit, now_monotonic=0)
    adaptive_manager.update(direct=poor, transit=good_transit, now_monotonic=31)

    def wait_for_order(store: RynmeshStore, operation: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            orders = store.poll_work_orders(
                network_id=network_id,
                capability="rynmesh.peer-transit.v1",
            )["work_orders"]
            if any(item["operation"] == operation for item in orders):
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {operation}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        source_future = pool.submit(
            send_file_adaptive,
            source,
            source_file,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            direct_metrics=poor,
            transit_metrics=good_transit,
            route_manager=adaptive_manager,
            network_id=network_id,
            timeout_s=20,
        )
        wait_for_order(relay, "open_peer_transit")
        relay_future = pool.submit(relay_worker.serve_once)
        wait_for_order(target, "accept_peer_transit")
        target_future = pool.submit(target_worker.serve_once)
        evidence = source_future.result(timeout=30)
        assert relay_future.result(timeout=10) == 1
        assert target_future.result(timeout=10) == 1

    assert evidence["result"] == "pass"
    assert evidence["ice_relay_candidate_used"] is False
    assert evidence["source_sha256"] == evidence["target_sha256"]
    assert evidence["transit_rx_bytes"] > source_file.stat().st_size
    assert evidence["transit_tx_bytes"] > source_file.stat().st_size
    assert evidence["selected_path"] == "peer_transit"
    assert any(event["reason"] == "transit_better" for event in evidence["route_events"])
    assert marker not in b"".join(captured)
    assert audit_peer_transit(evidence)["ok"] is True

    tampered = __import__("copy").deepcopy(evidence)
    tampered["ice_relay_candidate_used"] = True
    with pytest.raises(AuditError, match="TURN"):
        audit_peer_transit(tampered)

    tampered = __import__("copy").deepcopy(evidence)
    tampered["target_result"]["result_refs"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(AuditError, match="signature"):
        audit_peer_transit(tampered)

    delivered = list((tmp_path / "target-inbox").glob("*-large-secret.bin"))
    assert len(delivered) == 1
    assert delivered[0].read_bytes() == source_file.read_bytes()

    # Registry stores signed offers/answers and session metadata only.  The
    # unique body marker must not appear anywhere in its mailbox files.
    for path in (tmp_path / "registry").rglob("*.json"):
        assert marker not in path.read_bytes()


def test_direct_file_path_uses_one_non_turn_ice_pair(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "direct-file"
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    source.registry = registry
    target.registry = registry
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=tmp_path / "inbox",
        timeout_s=10,
    )
    target_worker.register()
    source_file = tmp_path / "direct.bin"
    source_file.write_bytes(b"direct-peer-file" * 8192)

    with ThreadPoolExecutor(max_workers=2) as pool:
        source_future = pool.submit(
            send_file_direct,
            source,
            source_file,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=10,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if target.poll_work_orders(
                network_id=network_id,
                capability="rynmesh.peer-transit.v1",
            )["work_orders"]:
                break
            time.sleep(0.01)
        target_future = pool.submit(target_worker.serve_once)
        evidence = source_future.result(timeout=15)
        assert target_future.result(timeout=5) == 1

    assert evidence["path_mode"] == "direct"
    assert evidence["ice_relay_candidate_used"] is False
    assert evidence["source_hop"]["remote"]["type"] != "relay"
    assert evidence["source_sha256"] == evidence["target_sha256"]
