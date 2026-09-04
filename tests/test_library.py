import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.digest import DigestService
from rynmesh.services.library import LibraryError, LibraryService
from rynmesh.services.library_imports import LibraryImportStore
from rynmesh.services.library_metadata import LibraryMetadataStore
from rynmesh.store import RynmeshStore


def _record(index: int, *, saved: bool = True, progress: float = 0.0):
    return {
        "item_id": f"item-{index}",
        "first_opened_unix": float(index),
        "last_opened_unix": float(index),
        "open_count": 1,
        "bookmarked": saved,
        "progress": progress,
        "completed": progress >= 1,
        "last_activity_unix": float(index),
        "item": {
            "item_id": f"item-{index}",
            "title": f"Private note {index}",
            "link": f"https://example.test/{index}",
            "source_id": "fixture",
            "source_title": "Fixture source",
            "content_kind": "article",
            "summary": "bounded summary",
        },
    }


def _service(tmp_path, count=0):
    consumption = ConsumptionStore(tmp_path / "consumption.json", max_items=20_000)
    if count:
        consumption._write({f"item-{index}": _record(index, progress=0.5 if index % 3 == 0 else 0) for index in range(count)})
    imports = LibraryImportStore(tmp_path / "imports")
    metadata = LibraryMetadataStore(tmp_path / "metadata.json")
    return LibraryService(consumption=consumption, imports=imports, metadata=metadata), imports


def test_library_cursor_filters_and_batch_actions_are_stable(tmp_path):
    service, _imports = _service(tmp_path, 130)
    first = service.list(view="saved", limit=50)
    second = service.list(view="saved", limit=50, cursor=first["next_cursor"])
    third = service.list(view="saved", limit=50, cursor=second["next_cursor"])
    ids = [item["library_id"] for page in (first, second, third) for item in page["items"]]
    assert len(ids) == 130
    assert len(ids) == len(set(ids))
    with pytest.raises(LibraryError, match="library_cursor_filter_mismatch"):
        service.list(view="history", cursor=first["next_cursor"])

    selected = ids[:2]
    assert service.actions({"action": "archive", "library_ids": selected})["ok"]
    assert service.list(view="archived")["items"][0]["state"]["archived"]
    assert service.actions({"action": "add_tag", "library_ids": selected, "tag": "research"})["ok"]
    assert service.get(selected[0])["tags"] == ["research"]
    assert service.actions({"action": "mark_completed", "library_ids": [selected[0]]})["ok"]
    assert service.get(selected[0])["state"]["completed"] is True
    assert service.actions({"action": "restart", "library_ids": [selected[0]]})["ok"]
    assert service.get(selected[0])["state"]["progress"] == 0
    assert service.actions({"action": "remove_saved", "library_ids": [selected[0]]})["ok"]
    assert service.get(selected[0])["state"]["saved"] is False


def test_library_10000_item_first_page_stays_bounded(tmp_path):
    service, _imports = _service(tmp_path, 10_000)
    started = time.perf_counter()
    page = service.list(query="Private note", limit=50)
    duration = time.perf_counter() - started
    assert len(page["items"]) == 50
    assert page["next_cursor"]
    assert duration < 3.0
    assert len(json.dumps(page)) < 100_000


def test_import_copy_is_private_bounded_and_original_is_never_deleted(tmp_path):
    service, imports = _service(tmp_path)
    original = tmp_path / "owner-notes.md"
    original.write_text("# Owner notes\n\nPRIVATE_LIBRARY_BODY", encoding="utf-8")
    before = original.read_bytes()
    imported = imports.import_bytes({
        "filename": "../../owner-notes.md",
        "mime": "text/markdown",
        "data_base64": base64.b64encode(before).decode(),
    })
    assert imported["filename"] == "owner-notes.md"
    page = service.list(view="files")
    assert len(page["items"]) == 1
    assert "PRIVATE_LIBRARY_BODY" not in json.dumps(page)
    library_id = page["items"][0]["library_id"]
    assert service.actions({"action": "delete_import", "library_ids": [library_id]})["ok"]
    assert original.read_bytes() == before


@pytest.mark.parametrize(
    ("filename", "mime", "data", "code"),
    [
        ("fake.pdf", "application/pdf", b"not pdf", "library_import_mime_mismatch"),
        ("fake.txt", "text/plain", b"%PDF-fake", "library_import_mime_mismatch"),
        ("image.png", "image/png", b"png", "library_import_type_unsupported"),
        ("bad.txt", "text/plain", b"\xff\xfe", "library_import_not_utf8"),
    ],
)
def test_import_type_confusion_fails_closed(tmp_path, filename, mime, data, code):
    _service_instance, imports = _service(tmp_path)
    with pytest.raises(ValueError) as raised:
        imports.import_bytes({"filename": filename, "mime": mime, "data_base64": base64.b64encode(data).decode()})
    assert getattr(raised.value, "code", "") == code


def test_library_http_import_ask_export_and_scoped_erase(tmp_path):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = create_app(store)
    with TestClient(app) as client:
        imported = client.post("/api/local/library/imports", json={
            "filename": "private.txt",
            "mime": "text/plain",
            "data_base64": base64.b64encode(b"PRIVATE_LIBRARY_BODY").decode(),
        })
        assert imported.status_code == 200
        item = imported.json()
        assert "PRIVATE_LIBRARY_BODY" not in imported.text
        prepared = client.post("/api/local/assistant/context/prepare", json={
            "source_kind": "library-item",
            "library_id": item["library_id"],
            "conversation_id": "owner",
        })
        assert prepared.status_code == 200
        exported = client.get("/api/local/privacy/export").json()
        assert "PRIVATE_LIBRARY_BODY" not in json.dumps(exported)
        assert exported["library"]["imports"][0]["filename"] == "private.txt"
        status = client.get("/api/local/privacy/status").json()
        assert status["library_import_records"] == 1
        erased = client.post("/api/local/privacy/erase", json={"scopes": ["library_imports", "library_metadata", "library_index"]})
        assert erased.status_code == 200
        assert client.get("/api/local/library?view=files").json()["items"] == []


def test_paused_source_and_watcher_keep_history_without_fetching(tmp_path):
    calls: list[str] = []

    def fetcher(url, _timeout):
        calls.append(url)
        raise AssertionError("paused entries must not fetch")

    digest = DigestService(tmp_path, fetcher=fetcher)
    digest._save("sources.json", [{
        "id": "source-a", "title": "Source A", "kind": "rss",
        "feed_url": "https://example.test/feed", "paused": True,
    }])
    digest._save("items.json", {"source-a": [{"item_id": "old", "title": "History remains"}]})
    digest._save("watchers.json", [{
        "id": "watch-a", "title": "Watch A", "url": "https://example.test/page",
        "content_hash": "old", "paused": True,
    }])
    refreshed = digest.refresh()
    assert calls == []
    assert refreshed["sources"][0]["paused"] is True
    assert refreshed["sources"][0]["item_count"] == 1
    assert digest.check_watchers(now_unix=time.time()) == 0
    assert digest.list_watchers()[0]["id"] == "watch-a"
    assert digest.set_source_paused("source-a", False)["paused"] is False
    assert digest.set_watcher_paused("watch-a", False)["paused"] is False
