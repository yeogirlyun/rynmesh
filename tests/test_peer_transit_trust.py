from __future__ import annotations

import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import rynmesh.peer_transit_service as peer_transit_service
from rynmesh.friends.store import FriendStore
from rynmesh.peer_transit import (
    PeerTransitError,
    TransitCipher,
    TransitSessionOpen,
    messaging_public_key,
    new_session_id,
    sign_session_open,
)
from rynmesh.peer_transit_service import (
    DIRECT_OPERATION,
    PeerTransitWorker,
    send_file_direct,
    send_file_via_peer,
)
from rynmesh.personal_space import PersonalSpace
from rynmesh.registry import FilePeerRegistry
from rynmesh.services.peer_box import load_or_create_messaging_key
from rynmesh.store import RynmeshStore


def _expires(seconds: float = 120.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _pair_friend_on_target(target: RynmeshStore, source: RynmeshStore, *, status: str = "active") -> str:
    rid = f"rel-{source.peer_id[:16]}"
    source_box = load_or_create_messaging_key(source.home / "messaging.x25519")
    FriendStore(target.home).put_relationship(
        {
            "relationship_id": rid,
            "peer_id": source.peer_id,
            "node_name": source.node_name,
            "messaging_pub": messaging_public_key(source_box),
            "endpoint": "https://friend.example.invalid",
            "status": status,
            "created_at": "2026-11-15T00:00:00+00:00",
        },
        b"\x11" * 32,
    )
    return rid


def _join_personal_space(coordinator_store: RynmeshStore, member_store: RynmeshStore) -> tuple[PersonalSpace, PersonalSpace]:
    coord = PersonalSpace(coordinator_store)
    coord.create("OwnSpace")
    invitation = coord.act("invite", {"hours": 24})["invitation"]
    member = PersonalSpace(member_store)
    member.join(invitation, "MemberDevice")
    member.tick()
    coord.tick()
    member.tick()
    assert coord.status()["membership"] == "active"
    assert member.status()["membership"] == "active"
    return coord, member


def test_stranger_session_rejected_before_connection_and_sink(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "trust-reject-stranger"
    registry = FilePeerRegistry(tmp_path / "registry")
    stranger = RynmeshStore(home=tmp_path / "stranger", network_dir=tmp_path / "stranger-net")
    revoked_friend = RynmeshStore(home=tmp_path / "revoked", network_dir=tmp_path / "revoked-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    for store in (stranger, revoked_friend, target):
        store.registry = registry

    rid = _pair_friend_on_target(target, revoked_friend, status="active")
    FriendStore(target.home).revoke(rid, "2026-11-15T01:00:00+00:00")

    inbox = tmp_path / "target-inbox"
    worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=inbox,
        timeout_s=5,
    )
    worker.register()

    connection_calls = 0

    def forbid_connection(*args, **kwargs):
        nonlocal connection_calls
        connection_calls += 1
        raise AssertionError("new_connection must not be called for untrusted sources")

    monkeypatch.setattr(peer_transit_service, "new_connection", forbid_connection)

    target_pub = messaging_public_key(load_or_create_messaging_key(target.home / "messaging.x25519"))

    for sender in (stranger, revoked_friend):
        session_id = new_session_id()
        _cipher, ephemeral = TransitCipher.for_source(
            session_id=session_id,
            source_peer_id=sender.peer_id,
            target_peer_id=target.peer_id,
            target_messaging_pub=target_pub,
        )
        signed_open = sign_session_open(
            TransitSessionOpen(
                session_id=session_id,
                source_peer_id=sender.peer_id,
                target_peer_id=target.peer_id,
                source_ephemeral_pub=messaging_public_key(ephemeral),
                expires_at=_expires(),
            ),
            source_signing_key=sender.private_key_bytes,
        )
        submitted = sender.submit_work_order(
            provider_peer_id=target.peer_id,
            capability=peer_transit_service.TRANSIT_CAPABILITY,
            operation=DIRECT_OPERATION,
            params={
                "source_ice_offer": {"ufrag": "u", "pwd": "p", "candidates": []},
                "signed_session_open": signed_open.to_dict(),
            },
            network_id=network_id,
            expires_in_hours=0.1,
        )
        order_id = str(submitted["order"]["work_order_id"])
        assert worker.serve_once() == 1
        results = sender.list_work_results(
            work_order_id=order_id,
            network_id=network_id,
            provider_peer_id=target.peer_id,
            requester_peer_id=sender.peer_id,
        )["work_results"]
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert "transit_source_not_trusted" in str(results[0]["message"])

    assert connection_calls == 0
    assert not inbox.exists()


def test_active_friend_session_accepted_and_relay_role_unaffected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "trust-friend-via-unpaired-relay"
    registry = FilePeerRegistry(tmp_path / "registry")
    friend_source = RynmeshStore(home=tmp_path / "friend", network_dir=tmp_path / "friend-net")
    unpaired_relay = RynmeshStore(home=tmp_path / "relay", network_dir=tmp_path / "relay-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    for store in (friend_source, unpaired_relay, target):
        store.registry = registry

    # Only friend_source is paired on target; unpaired_relay has no trust relation with either side.
    _pair_friend_on_target(target, friend_source, status="active")

    inbox = tmp_path / "target-inbox"
    relay_worker = PeerTransitWorker(
        unpaired_relay,
        role="transit",
        network_id=network_id,
        timeout_s=10,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=inbox,
        timeout_s=10,
    )
    relay_worker.register()
    target_worker.register()

    payload = tmp_path / "from-friend.bin"
    payload.write_bytes(b"friend-trusted-payload-" * 2048)

    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        relay_future = pool.submit(relay_worker.serve_forever, poll_interval_s=0.01, stop_event=stop)
        target_future = pool.submit(target_worker.serve_forever, poll_interval_s=0.01, stop_event=stop)
        try:
            evidence = send_file_via_peer(
                friend_source,
                payload,
                relay_peer_id=unpaired_relay.peer_id,
                target_peer_id=target.peer_id,
                network_id=network_id,
                timeout_s=10,
            )
        finally:
            stop.set()
            relay_future.result(timeout=10)
            target_future.result(timeout=10)

    assert evidence["result"] == "pass"
    assert evidence["source_sha256"] == evidence["target_sha256"]
    delivered = list(inbox.glob("*-from-friend.bin"))
    assert len(delivered) == 1
    assert delivered[0].read_bytes() == payload.read_bytes()


def test_personal_space_own_device_session_accepted_until_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "trust-own-device"
    registry = FilePeerRegistry(tmp_path / "registry")
    device_a = RynmeshStore(home=tmp_path / "device-a", network_dir=tmp_path / "device-a-net", node_name="Desktop")
    device_b = RynmeshStore(home=tmp_path / "device-b", network_dir=tmp_path / "device-b-net", node_name="Laptop")
    device_a.registry = registry
    device_b.registry = registry

    coord_space, _member_space = _join_personal_space(device_a, device_b)

    inbox_a = tmp_path / "inbox-a"
    worker_a = PeerTransitWorker(
        device_a,
        role="target",
        network_id=network_id,
        inbox=inbox_a,
        timeout_s=10,
    )
    worker_a.register()

    payload = tmp_path / "own-device.bin"
    payload.write_bytes(b"own-space-payload-" * 1024)

    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_future = pool.submit(worker_a.serve_forever, poll_interval_s=0.01, stop_event=stop)
        try:
            evidence = send_file_direct(
                device_b,
                payload,
                target_peer_id=device_a.peer_id,
                network_id=network_id,
                timeout_s=10,
            )
            assert evidence["source_sha256"] == evidence["target_sha256"]
            delivered = list(inbox_a.glob("*-own-device.bin"))
            assert len(delivered) == 1
            assert delivered[0].read_bytes() == payload.read_bytes()

            # Removing device_b from personal space immediately revokes trust.
            coord_space.act("remove", {"peer_id": device_b.peer_id})
            with pytest.raises(PeerTransitError, match="transit_source_not_trusted"):
                send_file_direct(
                    device_b,
                    payload,
                    target_peer_id=device_a.peer_id,
                    network_id=network_id,
                    timeout_s=5,
                )
        finally:
            stop.set()
            worker_future.result(timeout=10)


def test_default_max_file_bytes_is_512_mib_and_env_override_works(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RYNMESH_TRANSIT_MAX_FILE_BYTES", raising=False)
    expected_default = 512 * 1024 * 1024
    assert peer_transit_service.DEFAULT_MAX_FILE_BYTES == expected_default
    assert peer_transit_service._max_file_bytes() == expected_default

    empty_sha = "sha256:" + hashlib.sha256(b"").hexdigest()
    oversized_manifest = {
        "kind": "file",
        "filename": "too-big.bin",
        "transfer_id": new_session_id(),
        "size_bytes": expected_default + 1,
        "sha256": empty_sha,
        "offset_bytes": 0,
        "segment_size_bytes": 64 * 1024 * 1024,
        "segment_sha256": empty_sha,
        "prefix_sha256": empty_sha,
        "final": False,
    }
    sink = peer_transit_service._TargetFileSink(
        tmp_path / "inbox",
        session_id=new_session_id(),
        source_peer_id="trusted-peer",
        max_file_bytes=peer_transit_service._max_file_bytes(),
    )
    with pytest.raises(PeerTransitError, match="transit file exceeds target size policy"):
        sink.write(b"M" + json.dumps(oversized_manifest).encode("utf-8"))

    override_bytes = 768 * 1024 * 1024
    monkeypatch.setenv("RYNMESH_TRANSIT_MAX_FILE_BYTES", str(override_bytes))
    assert peer_transit_service._max_file_bytes() == override_bytes
    bigger_sink = peer_transit_service._TargetFileSink(
        tmp_path / "inbox-override",
        session_id=new_session_id(),
        source_peer_id="trusted-peer",
        max_file_bytes=peer_transit_service._max_file_bytes(),
    )
    try:
        bigger_sink.write(b"M" + json.dumps(oversized_manifest).encode("utf-8"))
    finally:
        bigger_sink.abort()


def test_worker_cli_allow_any_source_flag_defaults_off_and_enables_bypass(
    tmp_path, monkeypatch
) -> None:
    store = RynmeshStore(home=tmp_path / "cli-store", network_dir=tmp_path / "cli-net")
    created_workers: list[PeerTransitWorker] = []
    monkeypatch.setattr(peer_transit_service, "RynmeshStore", lambda: store)

    def fake_serve_forever(self: PeerTransitWorker, *args, **kwargs) -> None:
        created_workers.append(self)

    monkeypatch.setattr(PeerTransitWorker, "serve_forever", fake_serve_forever)

    monkeypatch.setattr(
        sys,
        "argv",
        ["peer_transit_service", "worker", "--role", "target", "--network-id", "cli-net"],
    )
    assert peer_transit_service.main() == 0
    assert len(created_workers) == 1
    assert created_workers[0].trusted_source("unpaired-stranger-peer") is False

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "peer_transit_service",
            "worker",
            "--role",
            "target",
            "--network-id",
            "cli-net",
            "--allow-any-source",
        ],
    )
    assert peer_transit_service.main() == 0
    assert len(created_workers) == 2
    assert created_workers[1].trusted_source("unpaired-stranger-peer") is True
