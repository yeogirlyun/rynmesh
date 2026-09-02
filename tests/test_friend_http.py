from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

from rynmesh.crypto import SignedPayload, public_key_from_private, sign_payload
from rynmesh.friends import ACCEPT_VERSION, verify_invite
from rynmesh.peer_http import create_app
from rynmesh.registry import PeerRecord, sign_peer_record, verify_peer_record
from rynmesh.services.peer_box import open_sealed, public_key_b64
from rynmesh.store import RynmeshStore
from rynmesh.transport import network_key_header


def _acceptance_body(link: str, acceptor_key: bytes, messaging_key: X25519PrivateKey):
    reviewed = verify_invite(link, allow_private_endpoints=True)
    peer_id = public_key_from_private(acceptor_key)
    x25519_pub = public_key_b64(messaging_key)
    record = PeerRecord(
        peer_id=peer_id,
        node_name="Bob",
        endpoints=("https://bob.example:8791",),
        network_id=reviewed["network_id"],
        updated_at=datetime.now(UTC).isoformat(),
    )
    signed_record = sign_peer_record(record, private_key_bytes=acceptor_key)
    proof = sign_payload(
        {
            "version": ACCEPT_VERSION,
            "invite_id": reviewed["invite_id"],
            "acceptor_peer_id": peer_id,
            "acceptor_x25519_pub": x25519_pub,
            "network_id": reviewed["network_id"],
            "permissions": reviewed["permissions"],
            "signed_at": datetime.now(UTC).isoformat(),
            "nonce": "fresh-http-acceptance-nonce",
        },
        private_key_bytes=acceptor_key,
    )
    return {
        "invite_id": reviewed["invite_id"],
        "one_time_secret": reviewed["one_time_secret"],
        "acceptor_peer_record": signed_record.to_dict(),
        "acceptor_x25519_pub": x25519_pub,
        "permissions": reviewed["permissions"],
        "proof": proof.to_dict(),
    }


def test_local_invite_then_public_one_time_accept_rotates_encrypted_credential(
    tmp_path, monkeypatch
):
    home = tmp_path / "provider"
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "mesh-wide-secret-must-not-enter-link")
    provider_store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    client = TestClient(create_app(provider_store))
    created = client.post(
        "/api/local/friends/invites",
        json={
            "endpoints": ["https://alice.example:8791"],
            "permissions": ["private-ai.use"],
        },
    )
    assert created.status_code == 200
    link = created.json()["link"]
    assert "mesh-wide-secret-must-not-enter-link" not in link
    review = client.post(
        "/api/local/friends/invites/review", json={"link": link}
    ).json()
    assert "one_time_secret" not in review

    acceptor_key = Ed25519PrivateKey.generate().private_bytes_raw()
    messaging_key = X25519PrivateKey.generate()
    body = _acceptance_body(link, acceptor_key, messaging_key)
    accepted = client.post("/api/peer/friends/accept", json=body)
    assert accepted.status_code == 200
    response = accepted.json()
    assert (
        verify_peer_record(SignedPayload.from_dict(response["inviter_peer_record"])).peer_id
        == provider_store.peer_id
    )
    plaintext = open_sealed(
        messaging_key,
        response["inviter_x25519_pub"],
        response["credential"]["nonce"],
        response["credential"]["ciphertext"],
    )
    credential = json.loads(plaintext)
    assert credential["relationship_secret"] != body["one_time_secret"]
    assert credential["permissions"] == ["private-ai.use"]

    public_state = client.get("/api/local/friends").json()
    assert public_state[0]["peer_id"] == public_key_from_private(acceptor_key)
    assert credential["relationship_secret"] not in json.dumps(public_state)
    assert body["one_time_secret"] not in (home / "friends.json").read_text(encoding="utf-8")

    replay = client.post("/api/peer/friends/accept", json=body)
    assert replay.status_code == 404
    wrong = dict(body)
    wrong["one_time_secret"] = "wrong"
    assert client.post("/api/peer/friends/accept", json=wrong).status_code == 404

    # Normal peer routes still require the mesh key; only one-time acceptance bypasses it.
    assert client.get("/api/peer/pubkey").status_code == 404
    assert client.get("/api/peer/pubkey", headers=network_key_header()).status_code == 200


def test_invalid_x25519_key_cannot_consume_the_invite(tmp_path, monkeypatch):
    home = tmp_path / "provider"
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    provider_store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    client = TestClient(create_app(provider_store))
    link = client.post(
        "/api/local/friends/invites",
        json={"endpoints": ["https://alice.example:8791"]},
    ).json()["link"]
    acceptor_key = Ed25519PrivateKey.generate().private_bytes_raw()
    valid_messaging_key = X25519PrivateKey.generate()
    invalid = _acceptance_body(link, acceptor_key, valid_messaging_key)
    zero_key = base64.b64encode(bytes(32)).decode("ascii")
    invalid["acceptor_x25519_pub"] = zero_key
    proof_payload = dict(invalid["proof"]["payload"])
    proof_payload["acceptor_x25519_pub"] = zero_key
    invalid["proof"] = sign_payload(proof_payload, private_key_bytes=acceptor_key).to_dict()

    assert client.post("/api/peer/friends/accept", json=invalid).status_code == 404
    outstanding = client.get("/api/local/friends/invites").json()
    assert outstanding[0]["used_at"] is None

    valid = _acceptance_body(link, acceptor_key, valid_messaging_key)
    assert client.post("/api/peer/friends/accept", json=valid).status_code == 200
