from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


def _node(tmp_path, name, port_env, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / name))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")  # no registry in this test
    store = RynmeshStore()
    return TestClient(create_app(store)), store

def test_pubkey_endpoint(tmp_path, monkeypatch):
    c, _ = _node(tmp_path, "n1", "1", monkeypatch)
    r = c.get("/api/peer/pubkey")
    assert r.status_code == 200 and "x25519_pub" in r.json()

def test_peer_msg_delivery_and_history(tmp_path, monkeypatch):
    # Node B receives a sealed envelope and exposes it via local history.
    cb, sb = _node(tmp_path, "B", "2", monkeypatch)
    b_pub = cb.get("/api/peer/pubkey").json()["x25519_pub"]
    # craft a sealed message from a synthetic sender A using peer_box directly
    import json

    from rynmesh.services import peer_box
    a_priv = peer_box.load_or_create_messaging_key(tmp_path / "A" / "messaging.x25519")
    a_pub = peer_box.public_key_b64(a_priv)
    inner = {"msg_id": "m1", "ts": "2026-06-06T00:00:00+00:00", "kind": "text", "text": "hi B"}
    nonce, ct = peer_box.seal(a_priv, b_pub, json.dumps(inner).encode())
    # B must be able to resolve A's pubkey — inject it via the test header field
    header = {"v": 1, "from": "peerA", "to": sb.peer_id, "nonce": nonce, "ciphertext": ct,
              "from_pub": a_pub}  # v1: sender includes its messaging pubkey (TOFU)
    r = cb.post("/api/peer/msg", json=header)
    assert r.status_code == 200
    rh = cb.get("/api/local/messages", params={"peer_id": "peerA"})
    assert rh.status_code == 200
    hist = rh.json()
    assert hist[-1]["text"] == "hi B" and hist[-1]["dir"] == "in"

def test_history_peer_id_with_slash(tmp_path, monkeypatch):
    # Regression: base64 peer ids contain '/', '+', '=' — these must work as a
    # query param (a path segment would 404). See peer_http.local_history.
    cb, sb = _node(tmp_path, "B2", "3", monkeypatch)
    b_pub = cb.get("/api/peer/pubkey").json()["x25519_pub"]
    import json

    from rynmesh.services import peer_box
    a_priv = peer_box.load_or_create_messaging_key(tmp_path / "A2" / "messaging.x25519")
    a_pub = peer_box.public_key_b64(a_priv)
    sender = "peer/A+x="
    inner = {"msg_id": "m2", "ts": "2026-06-06T00:00:00+00:00", "kind": "text", "text": "slash hi"}
    nonce, ct = peer_box.seal(a_priv, b_pub, json.dumps(inner).encode())
    header = {"v": 1, "from": sender, "to": sb.peer_id, "nonce": nonce, "ciphertext": ct,
              "from_pub": a_pub}
    r = cb.post("/api/peer/msg", json=header)
    assert r.status_code == 200
    rh = cb.get("/api/local/messages", params={"peer_id": sender})
    assert rh.status_code == 200  # the bug: path-segment routing returned 404
    hist = rh.json()
    assert hist[-1]["text"] == "slash hi" and hist[-1]["dir"] == "in"

def test_peer_routes_gated_by_network_key(tmp_path, monkeypatch):
    # Active-probe resistance: with a network key set, /api/peer/* must require
    # the same X-Ryn-Auth header as /api/v1. A bare probe gets an
    # indistinguishable 404; the correct header gets a real 200.
    import hashlib
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "swordfish")
    c, _ = _node(tmp_path, "gated", "9", monkeypatch)

    # No auth header -> indistinguishable 404 (node fingerprint hidden).
    assert c.get("/api/peer/pubkey").status_code == 404

    # Correct salted-hash header -> real response.
    auth = hashlib.sha256(b"rynmesh-net-key:swordfish").hexdigest()
    r = c.get("/api/peer/pubkey", headers={"X-Ryn-Auth": auth})
    assert r.status_code == 200 and "x25519_pub" in r.json()
