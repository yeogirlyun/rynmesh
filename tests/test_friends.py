from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rynmesh.crypto import public_key_from_private
from rynmesh.friends import (
    ACCEPT_VERSION,
    FriendError,
    FriendshipStore,
    decode_invite,
    encode_invite,
    validate_endpoint,
    verify_acceptance_request,
    verify_invite,
)
from rynmesh.registry import PeerRecord, sign_peer_record
from rynmesh.services.peer_box import load_or_create_messaging_key, public_key_b64

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def _key() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes_raw()


def _invite(store: FriendshipStore, key: bytes, **overrides):
    values = {
        "private_key_bytes": key,
        "node_name": "Alice's Ryn",
        "network_id": "rynmesh-main",
        "endpoints": ["https://alice.example:8791"],
        "permissions": ["private-ai.use"],
        "now": NOW,
    }
    values.update(overrides)
    return store.create_invite(**values)


def test_invite_is_signed_offline_verifiable_and_secret_is_not_persisted(tmp_path):
    key = _key()
    store = FriendshipStore(tmp_path / "friends.json")
    created = _invite(store, key)

    reviewed = verify_invite(created["link"], now=NOW + timedelta(seconds=1))
    assert reviewed["inviter_peer_id"] == public_key_from_private(key)
    assert reviewed["verified_fingerprint"] == reviewed["inviter_peer_id"]
    assert reviewed["permissions"] == ["private-ai.use"]
    assert created["invite"]["used_at"] is None

    raw_secret = reviewed["one_time_secret"]
    persisted = (tmp_path / "friends.json").read_text(encoding="utf-8")
    assert raw_secret not in persisted
    assert "secret_hash" in persisted
    assert "one_time_secret" not in json.dumps(store.list_invites())


def test_invite_tamper_expiry_cancel_and_endpoint_rules_fail_closed(tmp_path):
    key = _key()
    store = FriendshipStore(tmp_path / "friends.json")
    created = _invite(store, key, ttl_seconds=30)
    signed = decode_invite(created["link"])
    signed.payload["permissions"] = ["private-ai.use", "filesystem.read"]
    with pytest.raises(FriendError, match="invite_signature_invalid"):
        verify_invite(encode_invite(signed), now=NOW)
    with pytest.raises(FriendError, match="invite_expired"):
        verify_invite(created["link"], now=NOW + timedelta(seconds=31))

    payload = verify_invite(created["link"], now=NOW)
    store.cancel_invite(payload["invite_id"], now=NOW)
    with pytest.raises(FriendError, match="invite_not_found"):
        store.consume_invite(
            invite_id=payload["invite_id"],
            one_time_secret=payload["one_time_secret"],
            acceptor_peer_id="bob",
            display_name="Bob",
            network_id="rynmesh-main",
            endpoints=["https://bob.example:8791"],
            permissions=["private-ai.use"],
            now=NOW,
        )

    for endpoint in (
        "http://127.0.0.1:8791",
        "http://169.254.169.254/latest",
        "http://metadata.google.internal/",
        "ftp://example.com/file",
        "https://user:pass@example.com",
    ):
        with pytest.raises(FriendError):
            validate_endpoint(endpoint)
    with pytest.raises(FriendError, match="requires_review"):
        validate_endpoint("http://192.168.1.2:8791")
    assert validate_endpoint("http://192.168.1.2:8791", allow_private=True)


def test_concurrent_invite_consumption_has_exactly_one_winner(tmp_path):
    store = FriendshipStore(tmp_path / "friends.json")
    payload = verify_invite(_invite(store, _key())["link"], now=NOW)

    def accept(peer_id: str):
        try:
            result = store.consume_invite(
                invite_id=payload["invite_id"],
                one_time_secret=payload["one_time_secret"],
                acceptor_peer_id=peer_id,
                display_name=peer_id,
                network_id="rynmesh-main",
                endpoints=[f"https://{peer_id}.example:8791"],
                permissions=["private-ai.use"],
                now=NOW,
            )
            return result
        except FriendError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, ["bob", "mallory"]))
    winners = [result for result in results if isinstance(result, dict)]
    losers = [result for result in results if isinstance(result, FriendError)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert str(losers[0]) == "invite_not_found"
    assert winners[0]["relationship_secret"] != payload["one_time_secret"]
    assert len(store.list_friends()) == 1


def test_friend_hmac_binds_sender_route_body_and_nonce_then_revocation_denies(tmp_path):
    alice_key = _key()
    alice_peer = public_key_from_private(alice_key)
    store = FriendshipStore(tmp_path / "friends.json")
    payload = verify_invite(_invite(store, alice_key)["link"], now=NOW)
    accepted = store.consume_invite(
        invite_id=payload["invite_id"],
        one_time_secret=payload["one_time_secret"],
        acceptor_peer_id="bob",
        display_name="Bob",
        network_id="rynmesh-main",
        endpoints=["https://bob.example:8791"],
        permissions=["private-ai.use"],
        now=NOW,
    )
    body = b'{"task":"hello"}'
    headers = store.make_auth_headers(
        "bob", method="POST", path="/api/peer/llm/tasks", body=body, now=NOW, nonce="n-1"
    )
    assert (
        store.verify_auth_headers(
            headers,
            method="POST",
            path="/api/peer/llm/tasks",
            body=body,
            application_peer_id="bob",
            required_permission="private-ai.use",
            now=NOW,
        )
        == "bob"
    )
    with pytest.raises(FriendError, match="replayed"):
        store.verify_auth_headers(
            headers,
            method="POST",
            path="/api/peer/llm/tasks",
            body=body,
            application_peer_id="bob",
            required_permission="private-ai.use",
            now=NOW,
        )

    wrong_path = store.make_auth_headers(
        "bob", method="POST", path="/api/peer/llm/tasks", body=body, now=NOW, nonce="n-2"
    )
    with pytest.raises(FriendError, match="invalid"):
        store.verify_auth_headers(
            wrong_path,
            method="POST",
            path="/api/peer/llm/admin",
            body=body,
            application_peer_id="bob",
            required_permission="private-ai.use",
            now=NOW,
        )
    with pytest.raises(FriendError, match="sender_mismatch"):
        store.verify_auth_headers(
            wrong_path,
            method="POST",
            path="/api/peer/llm/tasks",
            body=body,
            application_peer_id="mallory",
            required_permission="private-ai.use",
            now=NOW,
        )

    signed_revocation = store.revoke(
        "bob", private_key_bytes=alice_key, local_peer_id=alice_peer, now=NOW
    )
    assert signed_revocation.payload["peer_ids"] == sorted([alice_peer, "bob"])
    assert not store.is_authorized("bob", "private-ai.use")
    assert "bob" not in (tmp_path / "friends.secrets.json").read_text(encoding="utf-8")
    with pytest.raises(FriendError, match="not_authorized"):
        store.make_auth_headers("bob", method="POST", path="/api/peer/llm/tasks", body=body)
    assert accepted["relationship_secret"] not in json.dumps(store.list_friends())


def test_remote_revocation_is_idempotent_and_cannot_target_unrelated_pair(tmp_path):
    alice_key = _key()
    alice_peer = public_key_from_private(alice_key)
    bob_store = FriendshipStore(tmp_path / "bob-friends.json")
    relationship_secret = "A" * 43
    bob_store.register_received_relationship(
        peer_id=alice_peer,
        relationship_secret=relationship_secret,
        display_name="Alice",
        network_id="rynmesh-main",
        endpoints=["https://alice.example:8791"],
        received_permissions=["private-ai.use"],
        source_invite_id="invite-shared",
        now=NOW,
    )
    alice_store = FriendshipStore(tmp_path / "alice-friends.json")
    alice_store.register_received_relationship(
        peer_id="bob",
        relationship_secret=relationship_secret,
        display_name="Bob",
        network_id="rynmesh-main",
        endpoints=["https://bob.example:8791"],
        received_permissions=[],
        source_invite_id="invite-shared",
        now=NOW,
    )
    # Alice's record represents the inviter side for this focused revocation test.
    state = json.loads((tmp_path / "alice-friends.json").read_text(encoding="utf-8"))
    state["friends"]["bob"]["granted_permissions"] = ["private-ai.use"]
    (tmp_path / "alice-friends.json").write_text(json.dumps(state), encoding="utf-8")
    revocation = alice_store.revoke(
        "bob", private_key_bytes=alice_key, local_peer_id=alice_peer, now=NOW
    )
    first = bob_store.apply_revocation(revocation, local_peer_id="bob")
    second = bob_store.apply_revocation(revocation, local_peer_id="bob")
    assert first["state"] == second["state"] == "revoked"
    with pytest.raises(FriendError, match="relationship_mismatch"):
        bob_store.apply_revocation(revocation, local_peer_id="carol")


def test_acceptance_request_binds_signed_peer_invite_key_scope_and_time(tmp_path):
    acceptor_key = _key()
    acceptor_peer = public_key_from_private(acceptor_key)
    messaging_key = load_or_create_messaging_key(tmp_path / "messaging.x25519")
    x25519_pub = public_key_b64(messaging_key)
    record = PeerRecord(
        peer_id=acceptor_peer,
        node_name="Bob",
        endpoints=("https://bob.example:8791",),
        network_id="rynmesh-main",
        updated_at=NOW.isoformat(),
    )
    signed_record = sign_peer_record(record, private_key_bytes=acceptor_key)
    from rynmesh.crypto import sign_payload

    proof = sign_payload(
        {
            "version": ACCEPT_VERSION,
            "invite_id": "invite-1",
            "acceptor_peer_id": acceptor_peer,
            "acceptor_x25519_pub": x25519_pub,
            "network_id": "rynmesh-main",
            "permissions": ["private-ai.use"],
            "signed_at": NOW.isoformat(),
            "nonce": "a-fresh-acceptance-nonce",
        },
        private_key_bytes=acceptor_key,
    )
    body = {
        "invite_id": "invite-1",
        "one_time_secret": "not-verified-by-this-helper",
        "acceptor_peer_record": signed_record.to_dict(),
        "acceptor_x25519_pub": x25519_pub,
        "permissions": ["private-ai.use"],
        "proof": proof.to_dict(),
    }
    verified = verify_acceptance_request(body, now=NOW)
    assert verified["peer_id"] == acceptor_peer
    tampered = deepcopy(body)
    tampered["permissions"] = ["peer.messaging"]
    with pytest.raises(FriendError, match="binding_invalid"):
        verify_acceptance_request(tampered, now=NOW)
    with pytest.raises(FriendError, match="expired"):
        verify_acceptance_request(body, now=NOW + timedelta(minutes=6))


def test_store_recovers_a_stale_crash_lock(tmp_path):
    store = FriendshipStore(tmp_path / "friends.json")
    store.lock_path.write_text("crashed", encoding="utf-8")
    old = time.time() - 60
    os.utime(store.lock_path, (old, old))
    assert store.list_friends() == []
    assert not store.lock_path.exists()
