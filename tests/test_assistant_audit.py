"""Local assistant audit and privacy-control coverage."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.assistant_audit import AssistantAuditStore
from rynmesh.store import RynmeshStore


def test_audit_store_is_bounded_and_clearable(tmp_path):
    store = AssistantAuditStore(tmp_path / "audit.json", max_events=2)
    store.append("rec", "one", now_unix=1)
    store.append("fetch", "two", details={"network_access": True}, now_unix=2)
    store.append("verify", "three", now_unix=3)

    assert [event["text"] for event in store.list()] == ["three", "two"]
    assert store.list()[1]["details"] == {"network_access": True}
    store.clear()
    assert store.list() == []


def test_privacy_export_status_and_granular_erasure(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    client = TestClient(create_app(RynmeshStore()))

    item = {
        "item_id": "item-1",
        "source_id": "source-1",
        "source_title": "Example",
        "source_kind": "news",
        "title": "A useful article",
        "link": "https://example.com/article",
        "content_kind": "document",
        "tags": ["technology"],
        "reasons": ["fresh"],
    }
    assert (
        client.post("/api/local/consumption", json={"item": item, "action": "opened"}).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/local/recommendations/profile",
            json={"direction": "more local AI", "topics": ["ai-agents"]},
        ).status_code
        == 200
    )

    status = client.get("/api/local/privacy/status").json()
    assert status["storage_root"] == str(tmp_path / "node")
    assert status["reading_history_items"] == 1
    assert status["learned_signals"] > 0
    assert status["audit_events"] == 2

    exported = client.get("/api/local/privacy/export").json()
    assert exported["storage"] == "local"
    assert exported["recommendation_profile"]["direction"] == "more local AI"
    assert exported["reading_history"][0]["item_id"] == "item-1"
    assert len(exported["assistant_audit"]) == 2
    assert "smtp_password" not in str(exported)

    erased = client.post(
        "/api/local/privacy/erase",
        json={"scopes": ["history", "profile", "cache", "audit"]},
    )
    assert erased.status_code == 200
    assert erased.json()["erased"] == ["audit", "cache", "history", "profile"]
    after = client.get("/api/local/privacy/status").json()
    assert after["reading_history_items"] == 0
    assert after["feedback_items"] == 0
    assert after["learned_signals"] == 0
    assert after["cached_discovery_items"] == 0
    assert after["audit_events"] == 0


def test_privacy_erasure_rejects_unknown_or_empty_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    client = TestClient(create_app(RynmeshStore()))

    assert client.post("/api/local/privacy/erase", json={"scopes": []}).status_code == 400
    assert client.post("/api/local/privacy/erase", json={"scopes": ["identity"]}).status_code == 400


def test_desktop_node_blocks_cloud_model_until_owner_enables_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured-but-not-authorized")
    client = TestClient(create_app(RynmeshStore()))

    assert client.get("/api/local/settings").json()["cloud_access"] is False
    assert client.get("/api/local/ai/status").json() == {"provider": None, "model": None}
