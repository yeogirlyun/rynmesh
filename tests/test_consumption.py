from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.consumption import ConsumptionError, ConsumptionStore
from rynmesh.store import RynmeshStore

ITEM = {
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


def test_consumption_store_records_history_bookmarks_and_progress(tmp_path):
    store = ConsumptionStore(tmp_path / "consumption.json")
    opened = store.record(ITEM, "opened", now_unix=10)
    assert opened["open_count"] == 1
    assert opened["first_opened_unix"] == 10

    saved = store.record(ITEM, "bookmark", now_unix=11)
    assert saved["bookmarked"] is True
    progress = store.record(ITEM, "progress", progress=0.72, now_unix=12)
    assert progress["progress"] == 0.72
    assert progress["completed"] is False

    completed = store.record(ITEM, "progress", progress=0.96, now_unix=13)
    assert completed["completed"] is True
    assert store.list()[0]["item"]["title"] == "A useful article"


def test_consumption_store_is_bounded_and_can_be_cleared(tmp_path):
    store = ConsumptionStore(tmp_path / "consumption.json", max_items=2)
    for index in range(3):
        store.record(
            {**ITEM, "item_id": f"item-{index}", "link": f"https://example.com/{index}"},
            "opened",
            now_unix=index,
        )
    assert [record["item_id"] for record in store.list()] == ["item-2", "item-1"]
    store.clear()
    assert store.list() == []


def test_consumption_store_rejects_invalid_actions_and_items(tmp_path):
    store = ConsumptionStore(tmp_path / "consumption.json")
    try:
        store.record(ITEM, "unknown")
        raise AssertionError("invalid action accepted")
    except ConsumptionError as exc:
        assert str(exc) == "consumption_action_invalid"
    try:
        store.record({**ITEM, "link": "file:///secret"}, "opened")
        raise AssertionError("invalid link accepted")
    except ConsumptionError as exc:
        assert str(exc) == "consumption_item_link_invalid"


def test_consumption_http_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    client = TestClient(create_app(RynmeshStore()))

    saved = client.post(
        "/api/local/consumption",
        json={"item": ITEM, "action": "bookmark"},
    )
    assert saved.status_code == 200
    assert saved.json()["bookmarked"] is True
    assert client.get("/api/local/consumption").json()[0]["item_id"] == "item-1"

    bad = client.post(
        "/api/local/consumption",
        json={"item": ITEM, "action": "invalid"},
    )
    assert bad.status_code == 400
    assert client.delete("/api/local/consumption").json() == {"ok": True}
    assert client.get("/api/local/consumption").json() == []
