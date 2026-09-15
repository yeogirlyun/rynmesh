import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_friends import _pair

from rynmesh.friends.service import FriendError


def test_unverified_ok_response_cannot_claim_delivery_and_retry_preserves_identity(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    bob.post_json = lambda *args, **kwargs: {"ok": True}
    message_id = "1" * 32
    queued = bob.send_message(alice.peer_id, text="A real message", message_id=message_id)
    assert queued["delivery_state"] == "queued" and not queued["delivered"]
    assert alice.history(bob.peer_id) == []
    bob.post_json = mesh.post
    delivered = bob.send_message(alice.peer_id, text="A real message", message_id=message_id)
    assert delivered["delivered"]
    assert len(bob.history(alice.peer_id)) == len(alice.history(bob.peer_id)) == 1
    with pytest.raises(FriendError, match="message_id_conflict"):
        bob.send_message(alice.peer_id, text="Changed content", message_id=message_id)


def test_recipient_commit_with_lost_receipt_does_not_duplicate_message(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    calls = 0

    def lose_response(*args, **kwargs):
        nonlocal calls
        response = mesh.post(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("reply lost")
        return response

    bob.post_json = lose_response
    bob.send_message(alice.peer_id, text="One message", message_id="2" * 32)
    assert len(alice.history(bob.peer_id)) == 1
    assert bob.retry(alice.peer_id)["delivered"] == 1
    assert len(alice.history(bob.peer_id)) == 1
    assert bob.history(alice.peer_id)[0]["delivery_state"] == "delivered"


def test_simultaneous_two_way_sharing_completes_without_transaction_deadlock(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    barrier = threading.Barrier(2)
    alice.post_json = bob.post_json = lambda *args, **kwargs: (barrier.wait(5), mesh.post(*args, **kwargs))[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(alice.send_message, bob.peer_id, text="From Alice"),
                   pool.submit(bob.send_message, alice.peer_id, text="From Bob")]
        assert all(future.result(timeout=8)["delivered"] for future in futures)
    assert len(alice.history(bob.peer_id)) == len(bob.history(alice.peer_id)) == 2


def test_local_outbox_failure_never_sends_an_unrecorded_message(tmp_path, monkeypatch):
    mesh, alice, bob = _pair(tmp_path)
    monkeypatch.setattr(bob.messages, "append", lambda *args: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        bob.send_message(alice.peer_id, text="Must not be sent")
    assert alice.history(bob.peer_id) == []


def test_manual_revocation_retry_reuses_the_already_queued_mailbox_message(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    relation = alice.list_friends()[0]["relationship_id"]
    mesh.online.clear()
    deposits = []

    def queue_mail(relationship, path, wire, **kwargs):
        deposits.append(wire)
        return {"message_id": f"mbx{len(deposits)}"}

    alice.queue_mail = queue_mail
    revoked = alice.revoke(relation)
    assert revoked["revocation_delivery"] == "pending"
    assert len(deposits) == 1
    mailbox_id = alice.store.relationship(relation, active_only=False)["revocation_mailbox_id"]
    assert mailbox_id

    # The direct-delivery attempt still runs on every manual retry, but a new
    # mailbox message is only deposited when none is already queued.
    result = alice.retry_revocation(relation)
    assert result == {"delivered": False}
    assert len(deposits) == 1
    assert alice.store.relationship(relation, active_only=False)["revocation_mailbox_id"] == mailbox_id
