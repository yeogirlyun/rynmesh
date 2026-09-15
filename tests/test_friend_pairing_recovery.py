"""Invitation acceptance is a durable, authenticated, retryable transaction."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from test_friends import Mesh, _node, _pair

from rynmesh.friends.service import FriendError
from rynmesh.friends.store import FriendStore


def test_lost_acceptance_reply_retries_to_one_relationship_even_after_expiry(tmp_path):
    mesh = Mesh()
    alice = _node(tmp_path, "alice", 18081, mesh)
    bob = _node(tmp_path, "bob", 18082, mesh)
    invite = alice.create_invite(ttl_minutes=1)["invite_uri"]
    calls = 0

    def lose_reply(*args, **kwargs):
        nonlocal calls
        response = mesh.post(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("response lost")
        return response

    bob.post_json = lose_reply
    with pytest.raises(FriendError, match="could_not_join_friend"):
        bob.join(invite)
    assert len(alice.list_friends()) == 1 and bob.list_friends() == []
    later = datetime.now(UTC) + timedelta(minutes=2)
    alice.clock = bob.clock = lambda: later
    first = bob.join(invite)
    assert bob.join(invite) == first
    assert len(alice.list_friends()) == len(bob.list_friends()) == 1
    assert alice.store.secret(first["relationship_id"]) == bob.store.secret(first["relationship_id"])
    assert calls == 2


def test_two_acceptors_race_with_only_one_complete_relationship(tmp_path):
    mesh = Mesh()
    alice = _node(tmp_path, "alice", 18081, mesh)
    bob = _node(tmp_path, "bob", 18082, mesh)
    carol = _node(tmp_path, "carol", 18083, mesh)
    invite = alice.create_invite()["invite_uri"]
    barrier = threading.Barrier(2)

    def join(node):
        barrier.wait()
        try:
            return node.join(invite)
        except FriendError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(join, (bob, carol)))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert "invite_used" in results
    assert len(alice.list_friends()) == 1
    relation = alice.list_friends()[0]
    assert alice.store.secret(relation["relationship_id"])


def test_failed_acceptance_commit_leaves_invite_usable_and_retry_cannot_revive_revocation(tmp_path, monkeypatch):
    mesh = Mesh()
    alice = _node(tmp_path, "alice", 18081, mesh)
    bob = _node(tmp_path, "bob", 18082, mesh)
    invite = alice.create_invite()["invite_uri"]
    before = alice.store.state_path.read_bytes()
    write = alice.store._write
    monkeypatch.setattr(alice.store, "_write", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(FriendError):
        bob.join(invite)
    assert alice.store.state_path.read_bytes() == before
    monkeypatch.setattr(alice.store, "_write", write)
    relation = bob.join(invite)
    bob.revoke(relation["relationship_id"])
    with pytest.raises(FriendError, match="friend_revoked"):
        bob.join(invite)


def test_tampered_acceptance_is_not_trusted_and_inspection_never_contacts_endpoint(tmp_path):
    mesh = Mesh()
    alice = _node(tmp_path, "alice", 18081, mesh)
    bob = _node(tmp_path, "bob", 18082, mesh)
    invite = alice.create_invite()["invite_uri"]
    calls = []

    def tamper(*args, **kwargs):
        calls.append(args)
        response = mesh.post(*args, **kwargs)
        response["relationship_id"] = "attacker-chosen"
        return response

    bob.post_json = tamper
    assert bob.inspect_invite(invite)["node_name"] == "alice"
    assert calls == []
    with pytest.raises(FriendError, match="friend_acceptance_invalid"):
        bob.join(invite)
    assert bob.list_friends() == []


def test_legacy_friend_state_migrates_once_and_unknown_fields_survive(tmp_path):
    store = FriendStore(tmp_path)
    store.root.mkdir()
    old = {"invites": {}, "relationships": {}, "nonces": {}, "cards": {}, "extension": {"retain": True}}
    store.state_path.write_text(json.dumps(old))
    store.secrets_path.write_text(json.dumps({"old-key": "retained"}))
    assert store.state()["extension"] == {"retain": True}
    assert store.secrets()["old-key"] == "retained"
    assert json.loads(store.state_path.with_suffix(".json.migrated").read_text()) == old
    assert json.loads(store.secrets_path.with_suffix(".json.migrated").read_text()) == {"old-key": "retained"}
    store.put_invite("new", {"status": "active"})
    assert store.state()["extension"] == {"retain": True}
    assert "secrets" not in store.state()
    for raw in ('{"version":"future"}', '{"broken":'):
        store.state_path.write_text(raw)
        with pytest.raises((OSError, ValueError)):
            store.put_invite("new", {})
        assert store.state_path.read_text() == raw


def test_revoke_without_a_stored_secret_is_undeliverable_and_never_retried_by_the_worker(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    relation = alice.list_friends()[0]["relationship_id"]
    with alice.store._guard():
        state = alice.store._read(alice.store.state_path, alice.store._empty())
        state["secrets"].pop(relation, None)
        alice.store._write(alice.store.state_path, state)
    assert alice.store.secret(relation) is None

    revoked = alice.revoke(relation)
    assert revoked["status"] == "revoked"
    assert revoked["revocation_delivery"] == "undeliverable"
    assert "revocation_wire" not in alice.store.relationship(relation, active_only=False)

    # Mirrors the worker's tick() selection in friends/routes.py: only active friends
    # or ones with a pending removal notice are ever polled again.
    friend_row = next(row for row in alice.list_friends() if row["relationship_id"] == relation)
    worker_would_select = friend_row["status"] == "active" or friend_row.get("revocation_delivery") == "pending"
    assert worker_would_select is False

    result = alice.retry_revocation(relation)
    assert result == {"delivered": False, "reason": "friend_credentials_unavailable"}
