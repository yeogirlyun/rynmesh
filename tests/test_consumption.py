from fastapi.testclient import TestClient

from rynmesh.atomic_io import MAX_RECORD_BYTES
from rynmesh.peer_http import create_app
from rynmesh.services.consumption import _ITEM_FIELDS, ConsumptionError, ConsumptionStore
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


def test_consumption_store_worst_case_stays_under_atomic_cap(tmp_path):
    """`max_items` records, every capped field maxed out, must fit `MAX_RECORD_BYTES`.

    `ConsumptionStore` writes its whole history as one JSON record via
    `atomic_write_json`, which hard-fails past `MAX_RECORD_BYTES`. Nothing
    ties `max_items` to the per-item field caps automatically, so this test
    is the guard: it fills a store to its declared limits (every string
    field long enough to hit its 4000-char cap, `tags`/`reasons` long enough
    to hit their 64-entries-of-160-chars cap) and asserts the serialized
    file still fits. Re-run this after raising `max_items` or any
    `_ITEM_FIELDS` truncation length in `rynmesh/services/consumption.py`.
    """
    store = ConsumptionStore(tmp_path / "consumption.json")
    long_string = "x" * 5000  # longer than the 4000-char per-field cap
    long_tag = "y" * 300  # longer than the 160-char per-tag cap
    many_tags = [long_tag] * 100  # longer than the 64-entry cap

    for index in range(store.max_items):
        item = {"item_id": f"item-{index:05d}", "link": "https://example.com/" + str(index)}
        for field in _ITEM_FIELDS - {"item_id", "link", "tags", "reasons", "published_unix", "score"}:
            item[field] = long_string
        item["tags"] = many_tags
        item["reasons"] = many_tags
        item["published_unix"] = 1234567890.123456
        item["score"] = 0.999999
        store.record(item, "completed", now_unix=float(index))

    assert len(store.list()) == store.max_items
    raw_bytes = (tmp_path / "consumption.json").read_bytes()
    assert len(raw_bytes) < MAX_RECORD_BYTES, (
        f"worst-case consumption history ({len(raw_bytes)} bytes) exceeds "
        f"MAX_RECORD_BYTES ({MAX_RECORD_BYTES}); lower max_items or the "
        "per-field caps in ConsumptionStore"
    )


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
