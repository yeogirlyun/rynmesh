"""Local reading, playback, bookmark, and completion state for the owner."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from ..atomic_io import atomic_write_json

__all__ = ["ConsumptionError", "ConsumptionStore"]

_ACTIONS = {"opened", "bookmark", "unbookmark", "progress", "completed"}
# `ConsumptionStore.max_items` (below) times the worst-case size of one
# cleaned record (every capped string field in `_ITEM_FIELDS` at its 4000-char
# max, `tags`/`reasons` full at 64 entries of 160 chars) must stay under
# `atomic_io.MAX_RECORD_BYTES`: the whole history is written as one JSON
# record, and `atomic_write_json` hard-fails the write past that cap.
# `max_items=1000` (the size the rest of this bound was designed around)
# would already serialize to ~4x MAX_RECORD_BYTES at these per-field limits,
# so the default here is lowered instead of left to fail in production; see
# `tests/test_consumption.py::test_consumption_store_worst_case_stays_under_atomic_cap`,
# which fills a history to these exact limits and asserts the total stays
# under the cap. Re-run that test before raising `max_items` or any
# `_ITEM_FIELDS` truncation length.
_ITEM_FIELDS = {
    "item_id",
    "source_id",
    "source_title",
    "source_kind",
    "title",
    "link",
    "summary",
    "ai_summary",
    "author",
    "thumbnail",
    "media_url",
    "content_kind",
    "content_type",
    "tags",
    "published_unix",
    "score",
    "reasons",
}


class ConsumptionError(ValueError):
    """Raised when consumption state cannot be safely recorded."""


class ConsumptionStore:
    """Atomic, bounded history stored only beneath the local node home."""

    def __init__(self, path: str | Path, *, max_items: int = 200) -> None:
        self.path = Path(path)
        self.max_items = max(1, int(max_items))

    def list(self) -> list[dict[str, Any]]:
        records = list(self._load().values())
        return sorted(
            records,
            key=lambda record: float(record.get("last_activity_unix", 0.0) or 0.0),
            reverse=True,
        )

    def record(
        self,
        item: Mapping[str, Any],
        action: str,
        *,
        progress: float | None = None,
        now_unix: float | None = None,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in _ACTIONS:
            raise ConsumptionError("consumption_action_invalid")
        clean_item = self._clean_item(item)
        item_id = clean_item["item_id"]
        stamp = time.time() if now_unix is None else float(now_unix)
        records = self._load()
        record = dict(
            records.get(
                item_id,
                {
                    "item_id": item_id,
                    "first_opened_unix": 0.0,
                    "last_opened_unix": 0.0,
                    "open_count": 0,
                    "bookmarked": False,
                    "progress": 0.0,
                    "completed": False,
                },
            )
        )
        record["item"] = clean_item
        record["last_activity_unix"] = stamp
        if action == "opened":
            if not record.get("first_opened_unix"):
                record["first_opened_unix"] = stamp
            record["last_opened_unix"] = stamp
            record["open_count"] = int(record.get("open_count", 0) or 0) + 1
        elif action == "bookmark":
            record["bookmarked"] = True
        elif action == "unbookmark":
            record["bookmarked"] = False
        elif action == "progress":
            record["progress"] = max(
                float(record.get("progress", 0.0) or 0.0), self._clean_progress(progress)
            )
            if record["progress"] >= 0.95:
                record["completed"] = True
        elif action == "completed":
            record["progress"] = 1.0
            record["completed"] = True
        records[item_id] = record
        ordered = sorted(
            records.values(),
            key=lambda value: float(value.get("last_activity_unix", 0.0) or 0.0),
            reverse=True,
        )[: self.max_items]
        self._write({str(value["item_id"]): value for value in ordered})
        return record

    def clear(self) -> None:
        self._write({})

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    def _write(self, payload: Mapping[str, Any]) -> None:
        atomic_write_json(self.path, dict(payload), indent=2, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _clean_progress(value: float | None) -> float:
        try:
            return round(min(1.0, max(0.0, float(value))), 4)
        except (TypeError, ValueError):
            raise ConsumptionError("consumption_progress_invalid") from None

    @staticmethod
    def _clean_item(item: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(item, Mapping):
            raise ConsumptionError("consumption_item_invalid")
        item_id = str(item.get("item_id", "") or "").strip()[:256]
        if not item_id:
            raise ConsumptionError("consumption_item_id_required")
        clean: dict[str, Any] = {"item_id": item_id}
        for key in _ITEM_FIELDS - {"item_id"}:
            value = item.get(key)
            if key in {"tags", "reasons"}:
                clean[key] = [str(entry)[:160] for entry in value[:64]] if isinstance(value, list) else []
            elif key in {"published_unix", "score"}:
                try:
                    clean[key] = float(value or 0.0)
                except (TypeError, ValueError):
                    clean[key] = 0.0
            else:
                clean[key] = str(value or "")[:4000]
        if not clean.get("link", "").startswith(("http://", "https://")):
            raise ConsumptionError("consumption_item_link_invalid")
        return clean
