"""Small, local-only metadata overlay for the Library projection."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping


class LibraryMetadataError(ValueError):
    pass


class LibraryMetadataStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return {}
            return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}

    def get(self, library_id: str) -> dict[str, Any]:
        return dict(self.all().get(library_id, {}))

    def patch(self, library_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        if not library_id or len(library_id) > 320:
            raise LibraryMetadataError("library_id_invalid")
        allowed = {"archived", "tags", "title"}
        if not set(changes).issubset(allowed):
            raise LibraryMetadataError("library_metadata_patch_invalid")
        with self._lock:
            records = self.all()
            current = dict(records.get(library_id, {}))
            if "archived" in changes:
                current["archived"] = bool(changes["archived"])
            if "title" in changes:
                current["title"] = str(changes["title"]).strip()[:200]
            if "tags" in changes:
                tags = changes["tags"]
                if not isinstance(tags, list) or len(tags) > 32:
                    raise LibraryMetadataError("library_tags_invalid")
                current["tags"] = sorted({str(tag).strip()[:48] for tag in tags if str(tag).strip()})
            records[library_id] = current
            self._write(records)
            return dict(current)

    def clear(self) -> None:
        with self._lock:
            self._write({})

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
