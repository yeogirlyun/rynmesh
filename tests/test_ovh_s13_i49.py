"""S13-I49: truthful starter metadata and local status/settings responses."""
import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.recommendation_profile import starter_items
from rynmesh.store import RynmeshStore


def test_t1_starters_do_not_claim_verification():
    items = starter_items({}, seed_key="test", now_unix=1_800_000_000)
    assert items
    for item in items:
        assert item["starter"] is True
        assert item["identity_tier"] == "unknown"
        assert item["safety_outcome"] == "not_applicable"
        assert item["provenance_status"] == "not_applicable"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    return TestClient(create_app(store))


def test_pending_recs_tracks_real_unread_and_seen(client):
    service = client.app.state.digest_service
    service._save("discovery-state.json", {"unread_item_ids": ["one", "two"]})
    response = client.get("/api/local/node/status")
    assert response.status_code == 200
    assert response.json()["pending_recs"] == 2
    assert client.post("/api/local/discovery/seen").status_code == 200
    assert client.get("/api/local/node/status").json()["pending_recs"] == 0


def test_pending_recs_omitted_when_discovery_unavailable(client, monkeypatch):
    def unavailable():
        raise OSError("discovery cache unavailable")
    monkeypatch.setattr(client.app.state.digest_service, "discovery_status", unavailable)
    response = client.get("/api/local/node/status")
    assert response.status_code == 200
    assert "pending_recs" not in response.json()


def test_pending_recs_omitted_when_service_has_no_unread_count(client, monkeypatch):
    monkeypatch.setattr(client.app.state.digest_service, "discovery_status", lambda: {"item_count": 8})
    response = client.get("/api/local/node/status")
    assert response.status_code == 200
    assert "pending_recs" not in response.json()


def test_settings_never_fabricate_usage(client):
    for response in [client.get("/api/local/settings"),
                     client.patch("/api/local/settings", json={"fetch_timeout_s": 25})]:
        assert response.status_code == 200
        assert "fetch_used_mb" not in response.json()
        assert "fetch_budget_mb" in response.json()
