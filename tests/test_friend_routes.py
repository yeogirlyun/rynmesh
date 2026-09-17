from __future__ import annotations

import time
import uuid
from copy import deepcopy

from fastapi.testclient import TestClient

from rynmesh.crypto import canonical_json
from rynmesh.friends.crypto import auth_headers
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
    assert alice.get("/api/local/friends/invitation-context").status_code in {401, 403}
    context = alice.get("/api/local/friends/invitation-context", headers=auth).json()
    assert context == {"endpoint": "http://127.0.0.1:18901", "address_category": "loopback"}
    assert alice.get("/api/local/friends/invites", headers=auth).json() == {"invites": []}
    assert calls == []
    changed = alice.post("/api/local/friends/invites", headers=auth,
                         json={"reviewed_endpoint": "http://192.168.1.2:8791"})
    assert changed.status_code == 409
    assert changed.json()["detail"] == "invite_endpoint_changed"
    assert alice.get("/api/local/friends/invites", headers=auth).json() == {"invites": []}
    assert alice.get("/api/local/friends/abc%2Fdef/messages", headers=auth).json() == {"messages": []}
    invitation = alice.post("/api/local/friends/invites", json={"reviewed_endpoint": context["endpoint"]}, headers=auth).json()
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
    import base64
    attachment_bytes = b"x" * (5 * 1024 * 1024)
    attached = bob.post(f"/api/local/friends/{peer}/messages", json={
        "message_id": "d" * 32,
        "attachment": {"filename": "boundary.txt", "mime": "text/plain", "data_base64": base64.b64encode(attachment_bytes).decode()},
    }, headers=auth)
    assert attached.status_code == 200, attached.text
    assert attached.json()["delivery_state"] == "delivered"
    downloaded = alice.get(f"/api/local/friends/{sender}/attachments/{'d' * 32}", headers=auth)
    assert downloaded.content == attachment_bytes
    import time
    article_url = "https://example.test/private-reading"
    bob_app, alice_app = apps[1][0], apps[0][0]
    bob_app.state.reader_cache.put(article_url, {"title": "Reading to share", "source_url": article_url,
                                               "blocks": [{"tag": "p", "text": "Private article body, sent only on request."}]}, now=time.time())
    bob_app.state.consumption_store.record({"item_id": "reading-1", "title": "Reading to share", "link": article_url}, "opened")
    share_body = {"peer_id": peer, "item_id": "reading-1", "card_id": "b" * 32}
    shared = bob.post("/api/local/friends/share", json=share_body, headers=auth)
    assert shared.status_code == 200, shared.text
    assert shared.json()["delivery_state"] == "delivered"
    assert alice_app.state.friends.content.imports.list() == []
    received_card = alice.get("/api/local/friends/cards", headers=auth).json()["cards"][0]
    assert received_card["card"]["source_url"] == article_url
    assert received_card["fetch_state"] == "available"
    fetched = alice.post(f"/api/local/friends/cards/{'b' * 32}/fetch", headers=auth)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["sha256_verified"]
    imported_id = fetched.json()["library_id"].removeprefix("import:")
    document_url = f"/api/local/friends/documents/{imported_id}/body"
    assert alice.get(document_url, headers=auth).json()["text"] == "Private article body, sent only on request."
    assert len(alice_app.state.consumption_store.list()) == 1
    assert alice.delete(f"/api/local/friends/documents/{imported_id}", headers=auth).status_code == 409
    reviewed = alice.post('/api/local/privacy/documents/preview', json={'scope': imported_id}, headers=auth).json()
    cleared = alice.request('DELETE', f"/api/local/friends/documents/{imported_id}",
                            json={'review_token': reviewed['review_token']}, headers=auth)
    assert cleared.status_code == 200 and cleared.json()["removed"] == 1
    assert alice.get("/api/local/friends/cards", headers=auth).json()["cards"][0]["fetch_state"] == "unavailable"
    repaired = alice.post(f"/api/local/friends/cards/{'b' * 32}/fetch", json={"repair": True}, headers=auth)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["library_id"].removeprefix("import:") == imported_id
    assert len(alice_app.state.consumption_store.list()) == 1
    assert bob.post("/api/local/friends/share", json=share_body, headers=auth).json()["card_id"] == "b" * 32
    assert len(alice.get("/api/local/friends/cards", headers=auth).json()["cards"]) == 1
    assert bob.post("/api/local/friends/share", json={**share_body, "item_id": "another-article"}, headers=auth).status_code == 409
    denied = bob.post("/api/local/friends/share", json={**share_body, "card_id": "c" * 32}, headers=auth)
    assert denied.status_code == 200, denied.text
    relation = joined.json()["relationship_id"]
    assert bob.delete(f"/api/local/friends/{relation}", headers=auth).status_code == 200
    assert bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth).status_code == 409
    assert alice.post(f"/api/local/friends/cards/{'c' * 32}/fetch", headers=auth).status_code == 409
    assert alice.get(document_url, headers=auth).status_code == 200  # An explicit saved copy remains local.
    assert alice.post("/api/peer/friends/accept", json={"invite": "bad"}).status_code == 200


def test_replay_capacity_never_evicts_still_valid_requests(tmp_path):
    from rynmesh.friends.store import FriendStore
    store = FriendStore(tmp_path)
    assert store.remember_nonce("rel", "a", now=100, timestamp=100, limit=2)
    assert store.remember_nonce("rel", "b", now=101, timestamp=101, limit=2)
    assert not store.remember_nonce("rel", "c", now=102, timestamp=102, limit=2)
    assert not store.remember_nonce("rel", "a", now=103, timestamp=100, limit=2)
    assert store.remember_nonce("rel", "c", now=221, timestamp=221, limit=2)


def test_private_card_fetch_returns_safe_denial_for_inactive_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/peer/friends/content-card/fetch", json={"v": 1, "card_id": "a" * 32})
    assert response.status_code == 403
    assert response.json() == {"detail": "friend_request_rejected"}


def _paired_three_nodes(tmp_path, monkeypatch, base_port):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "route-owner")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    auth = {"x-ryn-local-token": "route-owner"}
    clients, apps = {}, {}
    for i, name in enumerate(("Alice", "Bob", "Carol")):
        home = tmp_path / name
        monkeypatch.setenv("RYNMESH_HOME", str(home))
        endpoint = f"http://127.0.0.1:{base_port + i}"
        monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", endpoint)
        app = create_app(RynmeshStore(home=home, node_name=name, network_dir=tmp_path / "network"))
        clients[endpoint] = TestClient(app)
        apps[name] = app

    def post(endpoint, path, payload, headers, **kwargs):
        response = clients[endpoint].post(path, json=payload, headers=headers or {})
        response.raise_for_status()
        return response.json()

    for app in apps.values():
        app.state.friends.service.post_json = post

    alice = clients[apps["Alice"].state.friends.service.endpoint]
    relationships = {}
    for name in ("Bob", "Carol"):
        invitation = alice.post("/api/local/friends/invites", json={}, headers=auth).json()
        joined = clients[apps[name].state.friends.service.endpoint].post(
            "/api/local/friends/join", json={"invite_uri": invitation["invite_uri"]}, headers=auth)
        assert joined.status_code == 200, joined.text
        relationships[name] = joined.json()["relationship_id"]
    return apps, clients, relationships, auth


def test_peer_request_rejects_header_relationship_bound_to_a_different_body_relationship(tmp_path, monkeypatch):
    """The X-Ryn-Friend-* headers authenticate relationship A (Bob's real,
    active relationship with Alice); the sealed wire inside the body claims a
    different real relationship B (Carol's). Every peer path that carries its
    own 'relationship_id' in the body must bind it to the header-authenticated
    relationship and reject the mismatch, for message, content-card and the
    content-card/fetch (delivery) path alike.
    """
    apps, clients, relationships, auth = _paired_three_nodes(tmp_path, monkeypatch, base_port=18981)
    alice_app, bob_app = apps["Alice"], apps["Bob"]
    alice = clients[alice_app.state.friends.service.endpoint]
    alice_service = alice_app.state.friends.service
    alice_peer_id = alice_service.peer_id
    bob_peer_id = bob_app.state.friends.service.peer_id
    carol_peer_id = apps["Carol"].state.friends.service.peer_id
    bob_relationship_id = relationships["Bob"]
    carol_relationship_id = relationships["Carol"]
    bob_secret = bob_app.state.friends.service._relationship(alice_peer_id)[1]
    # The side effect to rule out is on Alice, the node under attack: Bob is the
    # attacker and his own stores are never written by Alice's handlers at all.
    history_before = {peer: deepcopy(alice_service.history(peer)) for peer in (bob_peer_id, carol_peer_id)}
    cards_before = deepcopy(alice_service.store.list_cards())

    def send(path, body):
        payload_bytes = canonical_json(body)
        headers = auth_headers(bob_secret, method="POST", path=path, body=payload_bytes, sender=bob_peer_id,
                                receiver=alice_peer_id, relationship_id=bob_relationship_id,
                                timestamp=int(time.time()), nonce=uuid.uuid4().hex)
        return alice.post(path, content=payload_bytes, headers={**headers, "content-type": "application/json"})

    # message: a syntactically well-formed wire, truthfully "from" Bob, but
    # naming Carol's relationship. Sanity check first that Alice's own real
    # relationship with Carol exists and is unrelated to Bob's.
    message_wire = {"v": 1, "relationship_id": carol_relationship_id, "from": bob_peer_id, "to": alice_peer_id,
                     "from_pub": "not-used-before-rejection", "nonce": "AA==", "ciphertext": "AA=="}
    rejected_message = send("/api/peer/friends/message", message_wire)
    assert rejected_message.status_code == 403
    assert rejected_message.json() == {"detail": "friend_request_rejected"}
    assert {peer: alice_service.history(peer) for peer in history_before} == history_before

    # content-card: same shape of attack against the notification wire.
    card_wire = {"v": 1, "relationship_id": carol_relationship_id, "from": bob_peer_id, "to": alice_peer_id,
                 "nonce": "AA==", "ciphertext": "AA=="}
    rejected_card = send("/api/peer/friends/content-card", card_wire)
    assert rejected_card.status_code == 403
    assert rejected_card.json() == {"detail": "friend_request_rejected"}
    assert alice_service.store.list_cards() == cards_before
    assert {peer: alice_service.history(peer) for peer in history_before} == history_before

    # content-card/fetch ("receipt" path): use a real card Alice actually
    # shared with Bob, over Bob's real relationship, so the mismatch being
    # tested is specifically the body's claimed relationship_id -- not a
    # missing or misattributed card.
    article_url = "https://example.test/receipt-path-article"
    alice_app.state.reader_cache.put(article_url, {"title": "Receipt path article", "source_url": article_url,
                                      "blocks": [{"tag": "p", "text": "Body."}]}, now=time.time())
    alice_app.state.consumption_store.record({"item_id": "receipt-item", "title": "Receipt path article", "link": article_url}, "opened")
    card_id = "e" * 32
    shared = alice.post("/api/local/friends/share", json={"peer_id": bob_peer_id, "item_id": "receipt-item", "card_id": card_id}, headers=auth)
    assert shared.status_code == 200, shared.text
    assert shared.json()["delivery_state"] == "delivered"

    fetch_body = {"v": 1, "card_id": card_id, "from": bob_peer_id, "to": alice_peer_id, "relationship_id": carol_relationship_id}
    rejected_fetch = send("/api/peer/friends/content-card/fetch", fetch_body)
    assert rejected_fetch.status_code == 403
    assert rejected_fetch.json() == {"detail": "friend_request_rejected"}

    # Sanity: the identical request bound to Bob's own real relationship id succeeds,
    # proving the rejection above was specifically about the mismatched relationship_id.
    honest_fetch = send("/api/peer/friends/content-card/fetch", {**fetch_body, "relationship_id": bob_relationship_id})
    assert honest_fetch.status_code == 200, honest_fetch.text


def test_imports_belong_to_the_injected_node_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "ambient-home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    store = RynmeshStore(home=tmp_path / "injected-home", network_dir=tmp_path / "network")
    app = create_app(store)
    assert app.state.friends.content.imports.root == (store.home / "library-imports").resolve()
