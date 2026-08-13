from __future__ import annotations

import pytest

from rynmesh.services import peer_box
from rynmesh.services.messaging_store import MessagingStore
from rynmesh.services.peer_messenger import MAX_INLINE_BYTES, MessengerError, PeerMessenger


def _messenger(tmp_path, name, peers):
    """peers: dict name->(peer_id, x25519_pub_b64, priv). Wires a loopback transport."""
    me = peers[name]
    inbox = {}  # peer_id -> list of headers delivered
    def transport(peer_id, header):       # "POST" to a peer
        inbox.setdefault(peer_id, []).append(header); return 200
    def resolve_pubkey(peer_id):
        return next(p[1] for p in peers.values() if p[0] == peer_id)
    m = PeerMessenger(
        my_peer_id=me[0], my_priv=me[2],
        store=MessagingStore(tmp_path / name),
        resolve_pubkey=resolve_pubkey, transport=transport,
        now=lambda: "2026-06-06T00:00:00+00:00",
        new_id=lambda: "fixed-id",
    )
    return m, inbox

def _peers(tmp_path):
    a = peer_box.load_or_create_messaging_key(tmp_path / "ka")
    b = peer_box.load_or_create_messaging_key(tmp_path / "kb")
    return {"A": ("peerA", peer_box.public_key_b64(a), a),
            "B": ("peerB", peer_box.public_key_b64(b), b)}

def test_send_then_receive_text(tmp_path):
    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    rec = a.send("peerB", text="hello")
    assert rec["dir"] == "out" and rec["text"] == "hello" and rec["delivered"] is True
    header = inbox["peerB"][0]
    got = b.receive(header)
    assert got["dir"] == "in" and got["text"] == "hello" and got["from"] == "peerA"
    assert b.history("peerA")[0]["text"] == "hello"
    assert a.history("peerB")[0]["text"] == "hello"

def test_send_receive_attachment(tmp_path):
    peers = _peers(tmp_path)
    a, inbox = _messenger(tmp_path, "A", peers)
    b, _ = _messenger(tmp_path, "B", peers)
    a.send("peerB", text="pic", attachment={"filename": "x.png", "mime": "image/png", "bytes": b"\x89PNG"})
    got = b.receive(inbox["peerB"][0])
    assert got["kind"] == "image" and got["attachment"]["filename"] == "x.png"
    assert b._store.load_attachment(got["msg_id"]) == b"\x89PNG"  # bytes stored locally, not in history line

def test_oversize_attachment_rejected(tmp_path):
    peers = _peers(tmp_path)
    a, _ = _messenger(tmp_path, "A", peers)
    with pytest.raises(MessengerError):
        a.send("peerB", attachment={"filename": "big", "mime": "application/octet-stream",
                                     "bytes": b"x" * (MAX_INLINE_BYTES + 1)})
