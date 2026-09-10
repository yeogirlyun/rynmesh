from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from test_friends import _pair

from rynmesh.friends.mailbox import wire_mailbox
from rynmesh.mailbox_client import MailboxClient
from rynmesh.registry import FilePeerRegistry


def mailboxes(tmp_path, alice, bob):
    registry = FilePeerRegistry(tmp_path / "mailbox-registry")
    clients = []
    for node in (alice, bob):
        store = SimpleNamespace(peer_id=node.peer_id, private_key_bytes=node.identity_private, registry=registry)
        client = MailboxClient(store=store, messaging_key=node.messaging_private,
                               home=node.home, resolve_messaging_pub=lambda key: "")
        wire_mailbox(mailbox=client, service=lambda node=node: node)
        clients.append(client)
    return clients


def test_offline_message_is_only_delivered_after_signed_receipt_and_survives_retry(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    sent = alice.send_message(bob.peer_id, text="For when you return", message_id="3" * 32)
    assert sent["delivery_state"] == "mailbox" and not sent["delivered"]
    assert bob.history(alice.peer_id) == []
    assert bob_mail.poll_once() == 1
    assert len(bob.history(alice.peer_id)) == 1
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "mailbox"
    assert alice_mail.poll_once() == 1
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "delivered"
    alice_mail.poll_once()
    bob_mail.poll_once()
    assert len(bob.history(alice.peer_id)) == 1


def test_oversize_mail_waits_for_direct_connection_and_expired_message_is_not_resent(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    sent = alice.send_message(bob.peer_id, attachment={"filename": "large.bin", "bytes": b"x" * 100000})
    assert sent["delivery_state"] == "queued" and not sent["delivered"]
    later = datetime.now(UTC) + timedelta(hours=2)
    alice.clock = lambda: later
    assert alice.retry(bob.peer_id)["delivered"] == 0
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "expired"
    assert bob.history(alice.peer_id) == []


def test_local_revocation_blocks_already_queued_mailbox_message(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    alice.send_message(bob.peer_id, text="No longer permitted")
    bob.revoke(bob.list_friends()[0]["relationship_id"])
    assert bob_mail.poll_once() == 0
    assert bob.history(alice.peer_id) == []
    assert alice_mail.poll_once() == 1  # Bob's signed removal notice arrives independently.
    assert alice.history(bob.peer_id)[0]["delivery_state"] == "mailbox"


def test_offline_removal_notice_is_durable_and_requires_remote_receipt(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    relation = alice.list_friends()[0]["relationship_id"]
    removed = alice.revoke(relation)
    assert removed["revocation_delivery"] == "pending"
    assert alice.store.secret(relation) is None
    assert alice.store.relationship(relation, active_only=False)["revocation_wire"]
    assert bob_mail.poll_once() == 1
    assert bob.store.relationship(relation) is None
    assert alice.store.relationship(relation, active_only=False)["revocation_delivery"] == "pending"
    assert alice_mail.poll_once() == 1
    assert alice.store.relationship(relation, active_only=False)["revocation_delivery"] == "delivered"
    assert alice.store.pending_revocation_secret(relation) is None
