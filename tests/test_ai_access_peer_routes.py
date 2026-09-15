"""HTTP-level tests for the friend-authenticated AI access peer route.

Unlike tests/test_ai_catalog.py (which drives FriendAICatalog.respond/refresh
directly at the service level) these tests go through the real FastAPI route
in rynmesh/ai_access/routes.py, built by rynmesh.peer_http.create_app, the
same way tests/test_friend_routes.py exercises the friend peer routes.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi.testclient import TestClient

from rynmesh.ai_access.catalog import PATH
from rynmesh.crypto import canonical_json
from rynmesh.friends.crypto import auth_headers
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.peer_http import create_app
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

OWNER = {"x-ryn-local-token": "route-owner"}


def _pair(tmp_path, monkeypatch, names=("Alice", "Bob", "Carol")):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "route-owner")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    clients, apps = {}, {}
    for i, name in enumerate(names):
        home = tmp_path / name
        monkeypatch.setenv("RYNMESH_HOME", str(home))
        endpoint = f"http://127.0.0.1:{18941 + i}"
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

    alice = clients["http://127.0.0.1:18941"]
    relationships = {}
    # Pair every other node with Alice via the normal invite/join HTTP flow.
    for name in names[1:]:
        invitation = alice.post("/api/local/friends/invites", json={}, headers=OWNER).json()
        joined = clients[apps[name].state.friends.service.endpoint].post(
            "/api/local/friends/join", json={"invite_uri": invitation["invite_uri"]}, headers=OWNER)
        assert joined.status_code == 200, joined.text
        relationships[name] = joined.json()["relationship_id"]

    return apps, clients, relationships


def _send(alice, alice_peer_id, secret, *, relationship_id, sender_peer_id, body, path=PATH):
    payload_bytes = canonical_json(body)
    headers = auth_headers(
        secret, method="POST", path=path, body=payload_bytes,
        sender=sender_peer_id, receiver=alice_peer_id, relationship_id=relationship_id,
        timestamp=int(time.time()), nonce=uuid.uuid4().hex,
    )
    return alice.post(path, content=payload_bytes, headers={**headers, "content-type": "application/json"})


def test_peer_ai_access_route_serves_grant_rejects_after_revoke_and_binds_relationship(tmp_path, monkeypatch):
    apps, clients, relationships = _pair(tmp_path, monkeypatch)
    alice_app, bob_app = apps["Alice"], apps["Bob"]
    alice = clients["http://127.0.0.1:18941"]
    alice_peer_id = alice_app.state.friends.service.peer_id
    bob_peer_id = bob_app.state.friends.service.peer_id
    bob_relationship_id = relationships["Bob"]
    carol_relationship_id = relationships["Carol"]
    bob_record, bob_secret = bob_app.state.friends.service._relationship(alice_peer_id)

    manifest = LLMPackageManifest(package_id="model-x", mode="openai_compatible",
                                   public_model_alias="private-alias", base_url="http://127.0.0.1:1").public_dict()
    alice_app.state.ai_access.provider = lambda: {"configured": True, "service": manifest,
        "capacity": {"available": 1}, "online": True, "ready": True, "network_id": "rynmesh-main"}

    def request_body():
        return {"request_id": uuid.uuid4().hex, "relationship_id": bob_relationship_id, "from": bob_peer_id, "to": alice_peer_id}

    # No grant yet: request is accepted but reports the friend as unauthorized.
    not_yet = _send(alice, alice_peer_id, bob_secret, relationship_id=bob_relationship_id, sender_peer_id=bob_peer_id, body=request_body())
    assert not_yet.status_code == 200, not_yet.text
    plain = json.loads(peer_box.open_sealed(bob_app.state.friends.service.messaging_private, bob_record["messaging_pub"],
                                             not_yet.json()["nonce"], not_yet.json()["ciphertext"]))
    assert plain["status"] == "not_authorized" and plain["services"] == []

    # Grant AI access for Bob's relationship; the catalogue for that grant comes back.
    grant = alice.put(f"/api/local/ai-access/model-x/{bob_relationship_id}", json={"allowed": True, "expected_revision": 0}, headers=OWNER)
    assert grant.status_code == 200, grant.text

    ok = _send(alice, alice_peer_id, bob_secret, relationship_id=bob_relationship_id, sender_peer_id=bob_peer_id, body=request_body())
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert set(payload) == {"nonce", "ciphertext"}
    plain = json.loads(peer_box.open_sealed(bob_app.state.friends.service.messaging_private, bob_record["messaging_pub"],
                                             payload["nonce"], payload["ciphertext"]))
    assert plain["status"] == "authorized"
    assert plain["services"][0]["service"]["package_id"] == "model-x"
    assert plain["services"][0]["ai_permission"] == {"relationship_id": bob_relationship_id, "revision": 1}
    assert "private-alias" not in json.dumps(payload)  # sealed response must not leak the alias in cleartext

    # A header relationship id (Bob's, A) paired with a body relationship id
    # naming a different real relationship (Carol's, B) must be rejected --
    # the receiver's own grant/catalogue for A must never be substitutable.
    mismatched = _send(alice, alice_peer_id, bob_secret, relationship_id=bob_relationship_id, sender_peer_id=bob_peer_id,
                        body={"request_id": uuid.uuid4().hex, "relationship_id": carol_relationship_id, "from": bob_peer_id, "to": alice_peer_id})
    assert mismatched.status_code == 403
    assert mismatched.json() == {"detail": "ai_catalog_request_rejected"}

    # Revoking the friendship itself must reject the peer route entirely (auth fails upstream of the grant).
    revoke = alice.delete(f"/api/local/friends/{bob_relationship_id}", headers=OWNER)
    assert revoke.status_code == 200, revoke.text
    after_revoke = _send(alice, alice_peer_id, bob_secret, relationship_id=bob_relationship_id, sender_peer_id=bob_peer_id, body=request_body())
    assert after_revoke.status_code == 403
    assert after_revoke.json() == {"detail": "ai_catalog_request_rejected"}
