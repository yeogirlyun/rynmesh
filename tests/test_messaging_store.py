from __future__ import annotations

from rynmesh.services.messaging_store import MessagingStore

PEER = "G50wnVu2LhKNC5s6jUaZzlKLDXy7QlKaHgJBEiLrbNo="  # contains base64 chars

def test_append_and_history(tmp_path):
    s = MessagingStore(tmp_path)
    assert s.history(PEER) == []
    s.append(PEER, {"msg_id": "1", "dir": "out", "text": "hi"})
    s.append(PEER, {"msg_id": "2", "dir": "in", "text": "yo"})
    h = s.history(PEER)
    assert [r["msg_id"] for r in h] == ["1", "2"]
    assert h[1]["dir"] == "in"

def test_attachment_roundtrip(tmp_path):
    s = MessagingStore(tmp_path)
    s.save_attachment("msg-9", b"\x89PNGdata")
    assert s.load_attachment("msg-9") == b"\x89PNGdata"

def test_peer_id_safe_filename(tmp_path):
    s = MessagingStore(tmp_path)
    s.append("a/b=c+d", {"msg_id": "x"})        # slashes etc. must not break paths
    assert s.history("a/b=c+d")[0]["msg_id"] == "x"
