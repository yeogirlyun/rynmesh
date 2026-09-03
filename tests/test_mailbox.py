"""Peer mailbox: sealed envelopes, the registry spool, and the registry routes."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rynmesh.crypto import SignedPayload, public_key_from_private, sign_payload
from rynmesh.mailbox import (
    MAILBOX_VERSION,
    MAX_ACK_IDS,
    MAX_ENVELOPE_BYTES,
    MAX_POLL_LIMIT,
    MAX_POLL_RESPONSE_BYTES,
    MAX_TTL_S,
    POLL_KIND,
    MailboxError,
    build_poll_request,
    envelope_size_bytes,
    open_mailbox_message,
    rfc3339,
    seal_mailbox_message,
    verify_mailbox_envelope,
    verify_poll_request,
)
from rynmesh.mailbox_store import FileMailboxStore
from rynmesh.registry import FilePeerRegistry, HttpPeerRegistry, RegistryError

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _signing_key() -> bytes:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate().private_bytes_raw()


class _Peer:
    """A throwaway identity: Ed25519 signing key + X25519 messaging key."""

    def __init__(self, tmp_path: Path, name: str) -> None:
        from rynmesh.services import peer_box

        self.private_key_bytes = _signing_key()
        self.peer_id = public_key_from_private(self.private_key_bytes)
        self.messaging_key = peer_box.load_or_create_messaging_key(tmp_path / f"{name}.x25519")
        self.messaging_pub = peer_box.public_key_b64(self.messaging_key)


def _at(moment: datetime):
    return lambda: moment


def _seal(sender: _Peer, recipient: _Peer, **kwargs) -> SignedPayload:
    options = dict(
        kind="pair.accept",
        body={"note": "hello"},
        from_private_key_bytes=sender.private_key_bytes,
        to_peer_id=recipient.peer_id,
        to_messaging_pub=recipient.messaging_pub,
        now=_at(T0),
    )
    options.update(kwargs)
    return seal_mailbox_message(**options)


def _resign(signed: SignedPayload, *, private_key_bytes: bytes, **changes) -> SignedPayload:
    """Re-sign a modified envelope so verification fails on semantics, not bytes."""

    return sign_payload({**signed.payload, **changes}, private_key_bytes=private_key_bytes)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------- 1. seal


def test_seal_open_round_trip_and_identity_binding(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    carol = _Peer(tmp_path, "carol")

    signed = _seal(alice, bob, body={"note": "hello", "n": 7})
    envelope = verify_mailbox_envelope(signed, now=_at(T0))
    assert envelope.version == MAILBOX_VERSION
    assert envelope.from_peer_id == alice.peer_id
    assert envelope.to_peer_id == bob.peer_id
    # The registry-visible shell carries no plaintext.
    assert "hello" not in json.dumps(signed.to_dict())

    opened_envelope, body = open_mailbox_message(
        signed, my_peer_id=bob.peer_id, messaging_private_key=bob.messaging_key,
        kind="pair.accept", now=_at(T0),
    )
    assert body == {"note": "hello", "n": 7}
    assert opened_envelope.message_id == envelope.message_id

    with pytest.raises(MailboxError, match="kind_mismatch"):
        open_mailbox_message(signed, my_peer_id=bob.peer_id,
                             messaging_private_key=bob.messaging_key,
                             kind="pair.revoke", now=_at(T0))

    with pytest.raises(MailboxError, match="recipient_mismatch"):
        open_mailbox_message(signed, my_peer_id=carol.peer_id,
                             messaging_private_key=carol.messaging_key, now=_at(T0))

    # A validly signed envelope whose ciphertext was swapped fails the AEAD.
    raw = bytearray(base64.b64decode(signed.payload["ciphertext"]))
    raw[0] ^= 0xFF
    tampered = _resign(signed, private_key_bytes=alice.private_key_bytes,
                       ciphertext=base64.b64encode(bytes(raw)).decode("ascii"))
    with pytest.raises(MailboxError, match="open_failed"):
        open_mailbox_message(tampered, my_peer_id=bob.peer_id,
                             messaging_private_key=bob.messaging_key, now=_at(T0))

    # Carol signs an envelope that claims to come from Alice.
    forged = _resign(signed, private_key_bytes=carol.private_key_bytes)
    with pytest.raises(MailboxError, match="sender_mismatch"):
        verify_mailbox_envelope(forged, now=_at(T0))

    # Flipping a byte without re-signing is caught earlier still.
    bad_signature = SignedPayload(
        payload={**signed.payload, "kind": "pair.revoke"},
        signature=signed.signature,
        public_key=signed.public_key,
    )
    with pytest.raises(MailboxError, match="invalid_signature"):
        verify_mailbox_envelope(bad_signature, now=_at(T0))


# ----------------------------------------------------------------- 2. size/ttl


def test_seal_enforces_size_and_ttl_caps(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")

    with pytest.raises(MailboxError, match="envelope_too_large"):
        _seal(alice, bob, body={"blob": "x" * (70 * 1024)})
    with pytest.raises(MailboxError, match="invalid_ttl"):
        _seal(alice, bob, ttl_s=0)
    with pytest.raises(MailboxError, match="invalid_ttl"):
        _seal(alice, bob, ttl_s=MAX_TTL_S + 1)
    with pytest.raises(MailboxError, match="invalid_kind"):
        _seal(alice, bob, kind="pair accept")
    with pytest.raises(MailboxError, match="invalid_kind"):
        _seal(alice, bob, kind="k" * 97)
    # The upper bound itself is allowed.
    assert verify_mailbox_envelope(_seal(alice, bob, ttl_s=MAX_TTL_S), now=_at(T0))


# ------------------------------------------------------------------ 3. expiry


def test_verify_rejects_expired_and_future_envelopes(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")

    signed = _seal(alice, bob, ttl_s=60)
    assert verify_mailbox_envelope(signed, now=_at(T0 + timedelta(seconds=59)))
    with pytest.raises(MailboxError, match="expired"):
        verify_mailbox_envelope(signed, now=_at(T0 + timedelta(seconds=61)))

    ahead = _seal(alice, bob, now=_at(T0 + timedelta(minutes=10)))
    with pytest.raises(MailboxError, match="created_at_in_future"):
        verify_mailbox_envelope(ahead, now=_at(T0))
    # Inside the 300 s skew window it still verifies.
    assert verify_mailbox_envelope(
        _seal(alice, bob, now=_at(T0 + timedelta(seconds=120))), now=_at(T0)
    )

    # A hand-built envelope whose declared lifetime exceeds the cap is refused
    # even though each timestamp on its own looks sane.
    stretched = _resign(
        signed,
        private_key_bytes=alice.private_key_bytes,
        expires_at=rfc3339(T0 + timedelta(seconds=MAX_TTL_S + 60)),
    )
    with pytest.raises(MailboxError, match="invalid_ttl"):
        verify_mailbox_envelope(stretched, now=_at(T0))


def test_verify_rejects_oversized_and_malformed_peer_ids(tmp_path) -> None:
    """The registry re-checks the caps; it never trusts the sender's restraint."""

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    signed = _seal(alice, bob)

    # Correctly signed, but fattened past the 64 KiB cap after sealing.
    bloated = _resign(
        signed,
        private_key_bytes=alice.private_key_bytes,
        ciphertext=base64.b64encode(b"x" * (70 * 1024)).decode("ascii"),
    )
    with pytest.raises(MailboxError, match="envelope_too_large"):
        verify_mailbox_envelope(bloated, now=_at(T0))

    for bad in ("not base64!", base64.b64encode(b"short").decode("ascii")):
        with pytest.raises(MailboxError, match="invalid_peer_id"):
            verify_mailbox_envelope(
                _resign(signed, private_key_bytes=alice.private_key_bytes, to_peer_id=bad),
                now=_at(T0),
            )
        with pytest.raises(MailboxError, match="invalid_peer_id"):
            seal_mailbox_message(
                kind="pair.accept", body={}, from_private_key_bytes=alice.private_key_bytes,
                to_peer_id=bad, to_messaging_pub=bob.messaging_pub,
            )
    with pytest.raises(MailboxError, match="peer_id_required"):
        _seal(alice, bob, to_peer_id="")


# -------------------------------------------------------------------- 4. poll


def test_poll_request_validation(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")

    good = build_poll_request(private_key_bytes=bob.private_key_bytes, now=_at(T0))
    payload = verify_poll_request(good, now=_at(T0))
    assert payload["kind"] == POLL_KIND
    assert payload["peer_id"] == bob.peer_id
    assert payload["limit"] == MAX_POLL_LIMIT

    tampered = SignedPayload(
        payload={**good.payload, "limit": 1},
        signature=good.signature,
        public_key=good.public_key,
    )
    with pytest.raises(MailboxError, match="invalid_signature"):
        verify_poll_request(tampered, now=_at(T0))

    impersonating = sign_payload(
        {**good.payload, "peer_id": alice.peer_id},
        private_key_bytes=bob.private_key_bytes,
    )
    with pytest.raises(MailboxError, match="poll_peer_mismatch"):
        verify_poll_request(impersonating, now=_at(T0))

    with pytest.raises(MailboxError, match="poll_skew"):
        verify_poll_request(
            build_poll_request(private_key_bytes=bob.private_key_bytes,
                               now=_at(T0 - timedelta(seconds=400))),
            now=_at(T0),
        )

    for bad_limit in (0, MAX_POLL_LIMIT + 1):
        with pytest.raises(MailboxError, match="invalid_limit"):
            build_poll_request(private_key_bytes=bob.private_key_bytes, limit=bad_limit)
        with pytest.raises(MailboxError, match="invalid_limit"):
            verify_poll_request(
                sign_payload({**good.payload, "limit": bad_limit},
                             private_key_bytes=bob.private_key_bytes),
                now=_at(T0),
            )

    too_many = [f"{index:032x}" for index in range(MAX_ACK_IDS + 1)]
    with pytest.raises(MailboxError, match="too_many_acks"):
        build_poll_request(private_key_bytes=bob.private_key_bytes, ack=too_many)
    with pytest.raises(MailboxError, match="too_many_acks"):
        verify_poll_request(
            sign_payload({**good.payload, "ack": too_many},
                         private_key_bytes=bob.private_key_bytes),
            now=_at(T0),
        )
    with pytest.raises(MailboxError, match="invalid_message_id"):
        build_poll_request(private_key_bytes=bob.private_key_bytes, ack=["../escape"])


# ------------------------------------------------------------------- 5. store


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


def test_file_mailbox_store_deposit_poll_ack_and_caps(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    carol = _Peer(tmp_path, "carol")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", sender_rate_per_minute=5000, now=clock)

    signed = _seal(alice, bob)
    message_id = signed.payload["message_id"]
    receipt = store.deposit(signed)
    assert receipt == {
        "message_id": message_id,
        "expires_at": signed.payload["expires_at"],
        "pending": 1,
    }

    digest = hashlib.sha256(bob.peer_id.encode("utf-8")).hexdigest()
    box_dir = tmp_path / "registry" / "mailbox" / digest[:2] / digest
    stored = box_dir / f"{message_id}.json"
    assert stored.exists()
    assert stored.stat().st_mode & 0o777 == 0o600
    assert box_dir.stat().st_mode & 0o777 == 0o700
    assert box_dir.parent.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "registry" / "mailbox").stat().st_mode & 0o777 == 0o700

    with pytest.raises(MailboxError, match="duplicate"):
        store.deposit(signed)

    polled = store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes, now=clock))
    assert [item.payload["message_id"] for item in polled] == [message_id]
    assert stored.exists(), "a plain poll must not consume mail"

    # Carol polling her own (empty) box never sees Bob's mail.
    assert store.poll(build_poll_request(private_key_bytes=carol.private_key_bytes,
                                         now=clock)) == []

    acked = store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                          ack=[message_id], now=clock))
    assert acked == []
    assert not stored.exists()


def test_file_mailbox_store_rate_limit_replay_and_capacity(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)

    limited = FileMailboxStore(tmp_path / "limited", sender_rate_per_minute=3, now=clock)
    for _ in range(3):
        limited.deposit(_seal(alice, bob))
    with pytest.raises(MailboxError, match="rate_limited"):
        limited.deposit(_seal(alice, bob))
    # One token is worth 20 s at 3/minute.
    clock.advance(21)
    assert limited.deposit(_seal(alice, bob, now=clock))["pending"] == 4

    replay_clock = _Clock(T0)
    replayed = FileMailboxStore(tmp_path / "replay", now=replay_clock)
    poll = build_poll_request(private_key_bytes=bob.private_key_bytes,
                              now=replay_clock, nonce="a" * 32)
    assert replayed.poll(poll) == []
    with pytest.raises(MailboxError, match="replay"):
        replayed.poll(poll)

    cap_clock = _Clock(T0)
    full = FileMailboxStore(tmp_path / "full", sender_rate_per_minute=5000, now=cap_clock)
    for index in range(full.max_pending_per_recipient):
        full.deposit(_seal(alice, bob, message_id=f"{index:032x}"))
    with pytest.raises(MailboxError, match="recipient_full"):
        full.deposit(_seal(alice, bob))


def test_acked_message_leaves_a_tombstone_until_it_expires(tmp_path) -> None:
    """An acked id stays spent for its TTL, so a captured envelope cannot replay."""

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", now=clock)

    signed = _seal(alice, bob, ttl_s=600, now=clock)
    message_id = signed.payload["message_id"]
    store.deposit(signed)
    assert len(store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                             now=clock))) == 1
    store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                  ack=[message_id], now=clock))

    digest = hashlib.sha256(bob.peer_id.encode("utf-8")).hexdigest()
    box = tmp_path / "registry" / "mailbox" / digest[:2] / digest
    tombstone = box / f"{message_id}.acked"
    assert not (box / f"{message_id}.json").exists()
    assert tombstone.exists()
    assert json.loads(tombstone.read_text()) == {"expires_at": signed.payload["expires_at"]}
    assert tombstone.stat().st_mode & 0o777 == 0o600

    # Re-depositing the very same signed envelope is refused for its whole TTL.
    with pytest.raises(MailboxError, match="duplicate"):
        store.deposit(signed)

    # Past the TTL the tombstone is swept and the box directory is reclaimed.
    clock.advance(601)
    assert store.sweep() == 1
    assert not tombstone.exists()
    assert not box.exists()


def test_ack_keeps_mail_it_cannot_read(tmp_path) -> None:
    """A read failure during ack must not delete the envelope tombstone-less.

    The expiry lives in the file, so an unreadable file yields no tombstone —
    deleting anyway would free the message id early and reopen the replay hole.
    """

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", now=clock)

    signed = _seal(alice, bob, ttl_s=600, now=clock)
    message_id = signed.payload["message_id"]
    store.deposit(signed)
    assert len(store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                             now=clock))) == 1

    digest = hashlib.sha256(bob.peer_id.encode("utf-8")).hexdigest()
    box = tmp_path / "registry" / "mailbox" / digest[:2] / digest
    envelope_path = box / f"{message_id}.json"
    tombstone = box / f"{message_id}.acked"
    original = store._load

    def flaky(path: Path):
        if path == envelope_path:
            raise OSError("device busy")
        return original(path)

    store._load = flaky
    store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                  ack=[message_id], now=clock))
    assert envelope_path.exists(), "an unreadable envelope must survive its ack"
    assert not tombstone.exists(), "no tombstone can be written without an expiry"

    # The next ack, with the read working again, completes the delivery.
    store._load = original
    store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                  ack=[message_id], now=clock))
    assert not envelope_path.exists()
    assert json.loads(tombstone.read_text()) == {"expires_at": signed.payload["expires_at"]}
    with pytest.raises(MailboxError, match="duplicate"):
        store.deposit(signed)


def test_deposit_charges_the_sender_for_rejected_mail(tmp_path) -> None:
    """A sender looping on duplicates burns budget like one delivering mail."""

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", sender_rate_per_minute=2, now=clock)

    signed = _seal(alice, bob, now=clock)
    store.deposit(signed)
    with pytest.raises(MailboxError, match="duplicate"):
        store.deposit(signed)
    with pytest.raises(MailboxError, match="rate_limited"):
        store.deposit(signed)


def test_sweep_keeps_mail_it_cannot_read(tmp_path) -> None:
    """A transient read failure must never be mistaken for corruption."""

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", now=clock)

    keep = _seal(alice, bob, ttl_s=60, message_id="a" * 32, now=clock)
    drop = _seal(alice, bob, ttl_s=60, message_id="b" * 32, now=clock)
    store.deposit(keep)
    store.deposit(drop)

    digest = hashlib.sha256(bob.peer_id.encode("utf-8")).hexdigest()
    box = tmp_path / "registry" / "mailbox" / digest[:2] / digest
    unreadable = box / f"{'a' * 32}.json"
    original = store._load

    def flaky(path: Path):
        if path == unreadable:
            raise OSError("device busy")
        return original(path)

    store._load = flaky
    clock.advance(120)
    assert store.sweep() == 1
    assert unreadable.exists(), "an unreadable file must survive the sweep"
    assert not (box / f"{'b' * 32}.json").exists()

    store._load = original
    assert store.sweep() == 1
    assert not unreadable.exists()


def test_poll_response_is_capped_below_the_client_read_limit(tmp_path) -> None:
    """A full mailbox drains over several polls instead of one unreadable one."""

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", sender_rate_per_minute=5000, now=clock)

    expected = set()
    for index in range(40):
        signed = _seal(alice, bob, body={"blob": "x" * 45000},
                       message_id=f"{index:032x}", ttl_s=MAX_TTL_S, now=clock)
        size = envelope_size_bytes(signed)
        assert 32 * 1024 < size <= MAX_ENVELOPE_BYTES
        store.deposit(signed)
        expected.add(signed.payload["message_id"])

    delivered: set[str] = set()
    batches = 0
    ack: list[str] = []
    while True:
        polled = store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                               ack=ack, now=clock))
        if not polled:
            break
        batches += 1
        # Nothing the registry hands back may exceed what the client will read.
        assert sum(envelope_size_bytes(item) for item in polled) <= MAX_POLL_RESPONSE_BYTES
        assert len(polled) < 40, "the byte budget, not the limit, must bind here"
        ack = [item.payload["message_id"] for item in polled]
        delivered.update(ack)

    assert batches >= 2
    assert delivered == expected


def test_file_mailbox_store_sweeps_expired_mail(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    clock = _Clock(T0)
    store = FileMailboxStore(tmp_path / "registry", now=clock)

    short = _seal(alice, bob, ttl_s=60, now=clock)
    store.deposit(short)
    digest = hashlib.sha256(bob.peer_id.encode("utf-8")).hexdigest()
    stored = (tmp_path / "registry" / "mailbox" / digest[:2] / digest
              / f"{short.payload['message_id']}.json")
    assert stored.exists()

    clock.advance(120)
    assert store.sweep() == 1
    assert not stored.exists()
    assert store.sweep() == 0

    # A poll after the TTL also drains the box even without an explicit sweep.
    clock.moment = T0
    store.deposit(_seal(alice, bob, ttl_s=60, now=clock))
    clock.advance(120)
    assert store.poll(build_poll_request(private_key_bytes=bob.private_key_bytes,
                                         now=clock)) == []
    assert list((tmp_path / "registry" / "mailbox" / digest[:2] / digest).glob("*.json")) == []


# ----------------------------------------------------------- 6. registry HTTP


def _registry_client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.registry_http import create_app

    registry = FilePeerRegistry(tmp_path / "registry")
    return registry, TestClient(create_app(registry))


def test_registry_http_mailbox_deposit_and_poll(tmp_path) -> None:
    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    carol = _Peer(tmp_path, "carol")
    registry, client = _registry_client(tmp_path)

    for_bob = _seal(alice, bob, body={"note": "for-bob"}, now=None)
    for_carol = _seal(alice, carol, body={"note": "for-carol"}, now=None)
    assert client.post("/api/v1/mailbox/deposit", json=for_bob.to_dict()).status_code == 200
    assert client.post("/api/v1/mailbox/deposit", json=for_carol.to_dict()).status_code == 200

    duplicate = client.post("/api/v1/mailbox/deposit", json=for_bob.to_dict())
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "duplicate"}

    poll = build_poll_request(private_key_bytes=bob.private_key_bytes)
    response = client.post("/api/v1/mailbox/poll", json=poll.to_dict())
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [item["payload"]["message_id"] for item in messages] == [
        for_bob.payload["message_id"]
    ]
    # Carol's sealed bytes must never appear in Bob's response.
    assert for_carol.payload["ciphertext"] not in response.text

    opened = open_mailbox_message(
        SignedPayload.from_dict(messages[0]),
        my_peer_id=bob.peer_id,
        messaging_private_key=bob.messaging_key,
    )
    assert opened[1] == {"note": "for-bob"}

    ack = build_poll_request(private_key_bytes=bob.private_key_bytes,
                             ack=[for_bob.payload["message_id"]])
    assert client.post("/api/v1/mailbox/poll", json=ack.to_dict()).json()["messages"] == []
    assert client.post(
        "/api/v1/mailbox/poll",
        json=build_poll_request(private_key_bytes=bob.private_key_bytes).to_dict(),
    ).json()["messages"] == []

    replayed = client.post("/api/v1/mailbox/poll", json=ack.to_dict())
    assert replayed.status_code == 409
    assert replayed.json() == {"detail": "replay"}

    stale = _seal(alice, bob, ttl_s=60, now=_at(T0))
    refused = client.post("/api/v1/mailbox/deposit", json=stale.to_dict())
    assert refused.status_code == 400
    assert refused.json() == {"detail": "expired"}
    assert registry.mailbox.max_pending_per_recipient == 256


def test_registry_http_mailbox_rate_limit_returns_429(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.registry_http import create_app

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    registry = FilePeerRegistry(tmp_path / "registry")
    # Force the lazy spool to exist with a tiny sender bucket.
    registry._mailbox = FileMailboxStore(registry.root, sender_rate_per_minute=2)
    client = TestClient(create_app(registry))

    for _ in range(2):
        assert client.post(
            "/api/v1/mailbox/deposit",
            json=_seal(alice, bob, now=None).to_dict(),
        ).status_code == 200
    throttled = client.post(
        "/api/v1/mailbox/deposit", json=_seal(alice, bob, now=None).to_dict()
    )
    assert throttled.status_code == 429
    assert throttled.json() == {"detail": "rate_limited"}


def test_registry_http_mailbox_hidden_without_network_key(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.registry_http import create_app

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "mailbox-secret")
    client = TestClient(create_app(FilePeerRegistry(tmp_path / "registry")))
    auth = hashlib.sha256(b"rynmesh-net-key:mailbox-secret").hexdigest()

    envelope = _seal(alice, bob, now=None).to_dict()
    assert client.post("/api/v1/mailbox/deposit", json=envelope).status_code == 404
    poll = build_poll_request(private_key_bytes=bob.private_key_bytes).to_dict()
    assert client.post("/api/v1/mailbox/poll", json=poll).status_code == 404
    assert client.post(
        "/api/v1/mailbox/deposit", json=envelope, headers={"X-Ryn-Auth": auth}
    ).status_code == 200
    assert client.post(
        "/api/v1/mailbox/poll", json=poll, headers={"X-Ryn-Auth": auth}
    ).status_code == 200


# ------------------------------------------------------------- 7. HTTP client


def test_http_peer_registry_mailbox_round_trip_through_server(tmp_path) -> None:
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")

    from rynmesh.registry_http import create_app

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    registry = FilePeerRegistry(tmp_path / "registry")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(registry), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "registry server did not start"

        client = HttpPeerRegistry(f"http://127.0.0.1:{port}")
        signed = _seal(alice, bob, body={"note": "over-the-wire"}, now=None)
        receipt = client.deposit_mailbox(signed)
        assert receipt["message_id"] == signed.payload["message_id"]
        assert receipt["pending"] == 1

        messages = client.poll_mailbox(
            build_poll_request(private_key_bytes=bob.private_key_bytes)
        )
        assert len(messages) == 1
        assert client.dropped_mailbox_messages == 0
        envelope, body = open_mailbox_message(
            messages[0], my_peer_id=bob.peer_id, messaging_private_key=bob.messaging_key
        )
        assert body == {"note": "over-the-wire"}

        # A rejection carries the HTTP status and short code, so a caller can
        # tell "already delivered" from "slow down" without parsing a message.
        with pytest.raises(RegistryError) as duplicate:
            client.deposit_mailbox(signed)
        assert duplicate.value.status == 409
        assert duplicate.value.detail == "duplicate"

        assert client.poll_mailbox(
            build_poll_request(private_key_bytes=bob.private_key_bytes,
                               ack=[envelope.message_id])
        ) == []
        assert client.poll_mailbox(
            build_poll_request(private_key_bytes=bob.private_key_bytes)
        ) == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_http_poll_mailbox_drops_mail_addressed_to_someone_else(tmp_path) -> None:
    """A broken or hostile registry cannot push another peer's mail onto us."""

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")
    carol = _Peer(tmp_path, "carol")
    client = HttpPeerRegistry("http://127.0.0.1:9")

    for_bob = _seal(alice, bob, now=None)
    for_carol = _seal(alice, carol, now=None)
    client._json = lambda req: {
        "messages": [for_carol.to_dict(), for_bob.to_dict(), {"not": "an envelope"}]
    }

    records = client.poll_mailbox(build_poll_request(private_key_bytes=bob.private_key_bytes))
    assert [item.payload["message_id"] for item in records] == [for_bob.payload["message_id"]]
    assert client.dropped_mailbox_messages == 2


def test_fallback_chain_carries_mailbox_traffic_past_a_dead_registry(tmp_path) -> None:
    from rynmesh.registry_resilience import FallbackRegistryChain

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")

    class _DeadRegistry:
        def deposit_mailbox(self, signed: SignedPayload) -> dict:
            raise RegistryError("registry_http_error: unreachable")

        def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]:
            raise RegistryError("registry_http_error: unreachable")

    live = FilePeerRegistry(tmp_path / "registry")
    chain = FallbackRegistryChain([_DeadRegistry(), live])

    signed = _seal(alice, bob, now=None)
    assert chain.deposit_mailbox(signed)["message_id"] == signed.payload["message_id"]
    polled = chain.poll_mailbox(build_poll_request(private_key_bytes=bob.private_key_bytes))
    assert [item.payload["message_id"] for item in polled] == [signed.payload["message_id"]]
    # The message really landed in the second registry, not the dead one.
    assert live.poll_mailbox(
        build_poll_request(private_key_bytes=bob.private_key_bytes)
    )[0].payload["message_id"] == signed.payload["message_id"]


def test_fallback_chain_does_not_retry_a_mailbox_verdict_on_the_next_registry(tmp_path) -> None:
    """`duplicate` is a verdict about the message, not a dead registry.

    Falling through would deliver one message twice, once per mirror.
    """

    from rynmesh.registry_resilience import FallbackRegistryChain

    alice = _Peer(tmp_path, "alice")
    bob = _Peer(tmp_path, "bob")

    class _RejectingRegistry:
        def deposit_mailbox(self, signed: SignedPayload) -> dict:
            raise MailboxError("duplicate")

    class _SpyRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def deposit_mailbox(self, signed: SignedPayload) -> dict:
            self.calls += 1
            return {"message_id": signed.payload["message_id"]}

    spy = _SpyRegistry()
    chain = FallbackRegistryChain([_RejectingRegistry(), spy])
    with pytest.raises(MailboxError, match="duplicate"):
        chain.deposit_mailbox(_seal(alice, bob, now=None))
    assert spy.calls == 0
