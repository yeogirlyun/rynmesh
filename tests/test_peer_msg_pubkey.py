"""Tests for S13-I22: /api/peer/msg verifies incoming from_pub before caching and rejects mismatches."""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _make_nodes(tmp_path, monkeypatch):
    net_dir = tmp_path / "shared-net"
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_NETWORK_ID", "rynmesh-main")

    home_a = tmp_path / "node-a"
    monkeypatch.setenv("RYNMESH_HOME", str(home_a))
    store_a = RynmeshStore(home=home_a, network_dir=net_dir)
    a_priv = peer_box.load_or_create_messaging_key(home_a / "messaging.x25519")
    a_pub = peer_box.public_key_b64(a_priv)
    store_a.register_node(endpoints=(), network_id="rynmesh-main")

    home_b = tmp_path / "node-b"
    monkeypatch.setenv("RYNMESH_HOME", str(home_b))
    store_b = RynmeshStore(home=home_b, network_dir=net_dir)
    app_b = create_app(store_b)
    return store_a, a_priv, a_pub, store_b, app_b


def _sealed_header(sender_id: str, sender_priv, sender_pub: str, recipient_id: str, recipient_pub: str, msg_id: str, text: str) -> dict:
    inner = {"msg_id": msg_id, "ts": "2026-06-06T00:00:00+00:00", "kind": "text", "text": text}
    nonce, ct = peer_box.seal(sender_priv, recipient_pub, json.dumps(inner).encode("utf-8"))
    return {
        "v": 1,
        "from": sender_id,
        "to": recipient_id,
        "nonce": nonce,
        "ciphertext": ct,
        "from_pub": sender_pub,
    }


def test_s13_i22_t1_forged_from_with_new_pubkey_does_not_alter_cache_and_records_mismatch(tmp_path, monkeypatch):
    store_a, a_priv, real_a_pub, store_b, app_b = _make_nodes(tmp_path, monkeypatch)
    fake_priv = peer_box.load_or_create_messaging_key(tmp_path / "attacker" / "messaging.x25519")
    fake_pub = peer_box.public_key_b64(fake_priv)
    assert fake_pub != real_a_pub

    with TestClient(app_b) as cb:
        b_pub = cb.get("/api/peer/pubkey").json()["x25519_pub"]
        sub_q: asyncio.Queue = asyncio.Queue()
        app_b.state.message_subscribers.append(sub_q)
        try:
            # 1) Cache does not have store_a yet: forged from_pub mismatches registry -> rejected, cache untouched.
            forged_1 = _sealed_header(store_a.peer_id, fake_priv, fake_pub, store_b.peer_id, b_pub, "forged-1", "spoof 1")
            r1 = cb.post("/api/peer/msg", json=forged_1)
            assert r1.status_code == 403
            assert r1.json()["detail"] == "peer_pubkey_mismatch"
            assert store_a.peer_id not in app_b.state.pubkey_cache
            assert cb.get("/api/local/messages", params={"peer_id": store_a.peer_id}).json() == []

            warn_1 = sub_q.get_nowait()
            assert warn_1["code"] == "peer_pubkey_mismatch"
            assert warn_1["from"] == store_a.peer_id
            assert "对方身份密钥与记录不一致" in warn_1["warning"]

            activity_1 = cb.get("/api/local/activity").json()
            assert any(
                ev.get("kind") == "security"
                and (ev.get("details") or {}).get("code") == "peer_pubkey_mismatch"
                and (ev.get("details") or {}).get("peer_id") == store_a.peer_id
                for ev in activity_1
            )

            # Unknown sender with no registry record is also rejected without polluting cache.
            forged_unknown = _sealed_header("unknown-peer-id", fake_priv, fake_pub, store_b.peer_id, b_pub, "forged-u", "spoof u")
            ru = cb.post("/api/peer/msg", json=forged_unknown)
            assert ru.status_code == 403
            assert ru.json()["detail"] == "peer_pubkey_mismatch"
            assert "unknown-peer-id" not in app_b.state.pubkey_cache
            _ = sub_q.get_nowait()

            # 2) Populate cache legitimately, then send mismatched key: existing cached key is NOT overwritten.
            legit = _sealed_header(store_a.peer_id, a_priv, real_a_pub, store_b.peer_id, b_pub, "legit-1", "real hello")
            ok_resp = cb.post("/api/peer/msg", json=legit)
            assert ok_resp.status_code == 200
            assert app_b.state.pubkey_cache[store_a.peer_id] == real_a_pub
            _ = sub_q.get_nowait()  # consume legit SSE message

            forged_2 = _sealed_header(store_a.peer_id, fake_priv, fake_pub, store_b.peer_id, b_pub, "forged-2", "spoof 2")
            r2 = cb.post("/api/peer/msg", json=forged_2)
            assert r2.status_code == 403
            assert r2.json()["detail"] == "peer_pubkey_mismatch"
            assert app_b.state.pubkey_cache[store_a.peer_id] == real_a_pub

            warn_2 = sub_q.get_nowait()
            assert warn_2["code"] == "peer_pubkey_mismatch"
            assert warn_2["from"] == store_a.peer_id
            assert "对方身份密钥与记录不一致" in warn_2["warning"]
            assert [m["text"] for m in cb.get("/api/local/messages", params={"peer_id": store_a.peer_id}).json()] == ["real hello"]
        finally:
            app_b.state.message_subscribers.remove(sub_q)


def test_s13_i22_t2_matching_registry_pubkey_writes_cache_and_accepts_message(tmp_path, monkeypatch):
    store_a, a_priv, real_a_pub, store_b, app_b = _make_nodes(tmp_path, monkeypatch)

    with TestClient(app_b) as cb:
        assert store_a.peer_id not in app_b.state.pubkey_cache
        b_pub = cb.get("/api/peer/pubkey").json()["x25519_pub"]

        msg = _sealed_header(store_a.peer_id, a_priv, real_a_pub, store_b.peer_id, b_pub, "m-ok", "verified hello")
        resp = cb.post("/api/peer/msg", json=msg)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "msg_id": "m-ok"}
        assert app_b.state.pubkey_cache[store_a.peer_id] == real_a_pub

        hist = cb.get("/api/local/messages", params={"peer_id": store_a.peer_id}).json()
        assert len(hist) == 1
        assert hist[0]["text"] == "verified hello"
        assert hist[0]["dir"] == "in"
        assert getattr(app_b.state, "security_events", []) == []
