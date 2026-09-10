from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


def test_owner_pairing_message_receipt_and_signed_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "route-owner")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    clients = {}
    apps = []
    auth = {"x-ryn-local-token": "route-owner"}
    for i, name in enumerate(("Alice", "Bob", "Carol")):
        home = tmp_path / name
        monkeypatch.setenv("RYNMESH_HOME", str(home))
        endpoint = f"http://127.0.0.1:{18901 + i}"
        monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", endpoint)
        app = create_app(RynmeshStore(home=home, node_name=name, network_dir=tmp_path / "network"))
        client = TestClient(app)
        clients[endpoint] = client
        apps.append((app, client))

    calls = []
    def post(endpoint, path, payload, headers, **kwargs):
        calls.append(path)
        response = clients[endpoint].post(path, json=payload, headers=headers or {})
        response.raise_for_status()
        return response.json()

    for app, _ in apps:
        app.state.friends.service.post_json = post
    alice, bob, carol = [client for _, client in apps]
    assert alice.get("/api/local/friends").status_code in {401, 403}
    assert alice.get("/api/local/friends/abc%2Fdef/messages", headers=auth).json() == {"messages": []}
    invitation = alice.post("/api/local/friends/invites", json={}, headers=auth).json()
    uri = {"invite_uri": invitation["invite_uri"]}
    assert bob.post("/api/local/friends/invites/inspect", json=uri, headers=auth).status_code == 200
    assert calls == []
    joined = bob.post("/api/local/friends/join", json=uri, headers=auth)
    assert joined.status_code == 200, joined.text
    assert carol.post("/api/local/friends/join", json=uri, headers=auth).json()["detail"] == "invite_used"
    peer = joined.json()["peer_id"]
    request = {"message_id": "a" * 32, "text": "第一次分享"}
    sent = bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth)
    assert sent.status_code == 200, sent.text
    assert sent.json()["delivery_state"] == "delivered"
    assert bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth).json()["msg_id"] == "a" * 32
    sender = apps[1][0].state.friends.service.peer_id
    rows = alice.get(f"/api/local/friends/{sender}/messages", headers=auth).json()["messages"]
    assert len(rows) == 1 and rows[0]["text"] == "第一次分享"
    relation = joined.json()["relationship_id"]
    assert bob.delete(f"/api/local/friends/{relation}", headers=auth).status_code == 200
    assert bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth).status_code == 409
    assert alice.post("/api/peer/friends/accept", json={"invite": "bad"}).status_code == 200


def test_replay_capacity_never_evicts_still_valid_requests(tmp_path):
    from rynmesh.friends.store import FriendStore
    store = FriendStore(tmp_path)
    assert store.remember_nonce("rel", "a", now=100, timestamp=100, limit=2)
    assert store.remember_nonce("rel", "b", now=101, timestamp=101, limit=2)
    assert not store.remember_nonce("rel", "c", now=102, timestamp=102, limit=2)
    assert not store.remember_nonce("rel", "a", now=103, timestamp=100, limit=2)
    assert store.remember_nonce("rel", "c", now=221, timestamp=221, limit=2)
