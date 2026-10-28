"""Stable, paginated projection over private Library truth stores."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping

from .library_imports import LibraryImportStore
from .library_metadata import LibraryMetadataStore

LIBRARY_VERSION = "ryn.library-item.v1"
VALID_VIEWS = {"all", "saved", "continue", "history", "files", "archived"}


class LibraryError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _cursor_encode(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    try:
        return dict(json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise LibraryError("library_cursor_invalid") from exc


class LibraryService:
    def __init__(self, *, consumption: Any, imports: LibraryImportStore, metadata: LibraryMetadataStore):
        self.consumption = consumption
        self.imports = imports
        self.metadata = metadata

    @staticmethod
    def _filter_hash(view: str, query: str, kind: str, source: str) -> str:
        return hashlib.sha256(json.dumps([view, query, kind, source]).encode()).hexdigest()[:16]

    def _items(self) -> list[dict[str, Any]]:
        metadata = self.metadata.all()
        items: list[dict[str, Any]] = []
        for record in self.consumption.list():
            raw = dict(record.get("item", {}))
            link = str(raw.get("link", ""))
            content_id = str(raw.get("content_id", ""))
            library_id = f"content:{content_id}" if content_id else "url:" + hashlib.sha256(link.encode()).hexdigest()
            overlay = metadata.get(library_id, {})
            item = {
                "version": LIBRARY_VERSION,
                "library_id": library_id,
                "content_id": content_id or None,
                "kind": str(raw.get("content_kind", "article") or "article"),
                "title": str(overlay.get("title") or raw.get("title") or link)[:300],
                "source": {"id": str(raw.get("source_id", "")), "title": str(raw.get("source_title", "")), "url": link},
                "state": {
                    "saved": bool(record.get("bookmarked")),
                    "progress": float(record.get("progress", 0) or 0),
                    "completed": bool(record.get("completed")),
                    "archived": bool(overlay.get("archived")),
                },
                "capabilities": {"open": bool(link), "ask": True, "share": True},
                "local_copy": {"available": False, "size_bytes": 0},
                "last_activity_unix": float(record.get("last_activity_unix", 0) or 0),
                "tags": list(overlay.get("tags", [])),
                "summary": str(raw.get("summary", ""))[:500],
            }
            item["_search"] = " ".join([item["title"], item["source"]["title"], *item["tags"]]).lower()
            items.append(item)
        for record in self.imports.list():
            library_id = f"import:{record['import_id']}"
            overlay = metadata.get(library_id, {})
            try:
                extracted = self.imports.extracted_text(str(record["import_id"]))[:8192]
            except Exception:
                extracted = ""
            item = {
                "version": LIBRARY_VERSION,
                "library_id": library_id,
                "import_id": record["import_id"],
                "kind": record["kind"],
                "title": str(overlay.get("title") or record["filename"]),
                "source": {"id": "local-import", "title": "Local file", "url": ""},
                "state": {"saved": True, "progress": 0, "completed": False, "archived": bool(overlay.get("archived"))},
                "capabilities": {"open": True, "ask": record.get("state") == "ready", "share": True},
                "local_copy": {"available": True, "size_bytes": int(record["size_bytes"])},
                "last_activity_unix": float(record["created_at_unix"]),
                "tags": list(overlay.get("tags", [])),
                "summary": f"Private {record['kind']} import · {record['extracted_bytes']} extracted bytes",
                "import_state": record["state"],
                "_search": " ".join([str(overlay.get("title") or record["filename"]), extracted, *overlay.get("tags", [])]).lower(),
            }
            items.append(item)
        return items

    def _consumption_record(self, library_id: str) -> dict[str, Any]:
        for record in self.consumption.list():
            raw = dict(record.get("item", {}))
            link = str(raw.get("link", ""))
            content_id = str(raw.get("content_id", ""))
            candidate = f"content:{content_id}" if content_id else "url:" + hashlib.sha256(link.encode()).hexdigest()
            if candidate == library_id:
                return record
        raise LibraryError("library_item_not_found")

    def list(self, *, view: str = "all", query: str = "", kind: str = "", source: str = "", cursor: str = "", limit: int = 50) -> dict[str, Any]:
        if view not in VALID_VIEWS:
            raise LibraryError("library_view_invalid")
        if len(query) > 200:
            raise LibraryError("library_query_too_long")
        limit = max(1, min(int(limit), 100))
        filter_hash = self._filter_hash(view, query, kind, source)
        after: tuple[float, str] | None = None
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 1 or decoded.get("f") != filter_hash:
                raise LibraryError("library_cursor_filter_mismatch")
            after = (float(decoded["t"]), str(decoded["id"]))
        values = self._items()
        values = [item for item in values if self._matches(item, view, query, kind, source)]
        values.sort(key=lambda item: (-float(item["last_activity_unix"]), str(item["library_id"])))
        if after:
            values = [item for item in values if (-float(item["last_activity_unix"]), str(item["library_id"])) > (-after[0], after[1])]
        page = values[:limit]
        next_cursor = None
        if len(values) > limit and page:
            last = page[-1]
            next_cursor = _cursor_encode({"v": 1, "f": filter_hash, "t": last["last_activity_unix"], "id": last["library_id"]})
        for item in page:
            item.pop("_search", None)
        return {"version": "ryn.library-page.v1", "items": page, "next_cursor": next_cursor, "limit": limit}

    @staticmethod
    def _matches(item: Mapping[str, Any], view: str, query: str, kind: str, source: str) -> bool:
        state = item["state"]
        if view == "saved" and not state["saved"]:
            return False
        if view == "continue" and not (0 < state["progress"] < 1 and not state["completed"]):
            return False
        if view == "files" and not str(item["library_id"]).startswith("import:"):
            return False
        if view == "history" and str(item["library_id"]).startswith("import:"):
            return False
        if view == "archived" and not state["archived"]:
            return False
        if view != "archived" and state["archived"]:
            return False
        if kind and item["kind"] != kind:
            return False
        if source and item["source"]["id"] != source:
            return False
        return not query or query.lower() in str(item.get("_search", ""))

    def get(self, library_id: str) -> dict[str, Any]:
        try:
            item = next(value for value in self._items() if value["library_id"] == library_id)
        except StopIteration as exc:
            raise LibraryError("library_item_not_found") from exc
        item.pop("_search", None)
        return item

    def patch(self, library_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        self.get(library_id)
        if not set(changes).issubset({"archived", "title", "tags"}):
            raise LibraryError("library_metadata_patch_invalid")
        overlay = self.metadata.get(library_id)
        if "archived" in changes:
            overlay["archived"] = bool(changes["archived"])
        if "title" in changes:
            overlay["title"] = str(changes["title"])
        if "tags" in changes:
            overlay["tags"] = changes["tags"]
        self.metadata.patch(library_id, overlay)
        return self.get(library_id)

    def actions(self, body: Mapping[str, Any]) -> dict[str, Any]:
        ids = body.get("library_ids", [])
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            raise LibraryError("library_batch_invalid")
        action = str(body.get("action", ""))
        if action not in {"archive", "unarchive", "add_tag", "remove_tag", "delete_import", "mark_completed", "restart", "remove_saved"}:
            raise LibraryError("library_action_invalid")
        results = []
        for library_id in [str(value) for value in ids]:
            try:
                if action in {"archive", "unarchive"}:
                    self.patch(library_id, {"archived": action == "archive"})
                elif action in {"add_tag", "remove_tag"}:
                    item = self.get(library_id)
                    tag = str(body.get("tag", "")).strip()[:48]
                    if not tag:
                        raise LibraryError("library_tag_required")
                    tags = set(item["tags"])
                    tags.add(tag) if action == "add_tag" else tags.discard(tag)
                    self.patch(library_id, {"tags": sorted(tags)})
                elif action == "delete_import":
                    raise LibraryError("library_cleanup_review_required")
                elif action in {"mark_completed", "restart", "remove_saved"}:
                    record = self._consumption_record(library_id)
                    mapped = {"mark_completed": "completed", "restart": "restart", "remove_saved": "unbookmark"}[action]
                    self.consumption.record(record["item"], mapped)
                results.append({"library_id": library_id, "ok": True})
            except (LibraryError, ValueError) as exc:
                results.append({"library_id": library_id, "ok": False, "error": str(exc)})
        return {"ok": all(value["ok"] for value in results), "results": results}
