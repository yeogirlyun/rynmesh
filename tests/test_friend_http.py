from __future__ import annotations

import json

from fastapi.testclient import TestClient

from rynmesh.crypto import canonical_json
from rynmesh.peer_http import HttpPeerClient, create_app
from rynmesh.store import RynmeshStore


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def post_bytes(self, url, body, *, timeout_s, max_bytes, headers=None):
        self.calls.append((url, body, dict(headers or {})))
        return canonical_json({"ok": True})


def test_http_peer_post_json_uses_transport_and_preserves_headers() -> None:
    transport = RecordingTransport()
    result = HttpPeerClient("http://192.168.1.20:8791", transport=transport).post_json(
        "/api/peer/friends/message", {"hello": "world"}, headers={"X-Ryn-Friend-MAC": "proof"}
    )
    assert result == {"ok": True}
    url, body, headers = transport.calls[0]
    assert url == "http://192.168.1.20:8791/api/peer/friends/message"
    assert json.loads(body) == {"hello": "world"}
    assert headers["X-Ryn-Friend-MAC"] == "proof"


def test_friend_local_api_review_is_offline_and_secrets_stay_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "local-test")
    monkeypatch.setenv("RYNMESH_PEER_ENDPOINT", "http://127.0.0.1:18881")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network", node_name="Alice")
    app = create_app(store)
    headers = {"X-Ryn-Local-Token": "local-test"}
    with TestClient(app) as client:
        created = client.post("/api/local/friends/invites", json={"ttl_minutes": 15}, headers=headers)
        assert created.status_code == 200
        payload = created.json()
        assert payload["invite_uri"].startswith("rynmesh://join/")
        reviewed = client.post(
            "/api/local/friends/invites/inspect",
            json={"invite_uri": payload["invite_uri"]},
            headers=headers,
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["address_category"] == "loopback"
        status = client.get("/api/local/privacy/status", headers=headers).json()
        assert status["friend_relationships"] == 0
        exported = client.get("/api/local/privacy/export", headers=headers).json()
        serialized = json.dumps(exported)
        assert "secret_hash" not in serialized
        assert "invite_secret" not in serialized


def test_invalid_friend_probe_is_generic_404(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    with TestClient(create_app(store)) as client:
        response = client.post("/api/peer/friends/message", json={"from": "attacker"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
