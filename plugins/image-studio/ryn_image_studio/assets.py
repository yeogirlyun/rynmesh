"""Persistent private and public image assets for the image studio."""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import Any, Callable

from . import sizes

ASSET_KINDS = {"product", "logo", "style"}
ASSET_LIMIT = 100
MAX_ASSET_BYTES = 8 * 1024 * 1024
PUBLIC = "public"

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_owner ON assets(owner, created_at);
"""


class AssetError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class AssetStore:
    def __init__(self, root: Path, db: sqlite3.Connection, lock, clock: Callable[[], float]) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = db
        self._lock = lock
        self._clock = clock
        self._db.executescript(SCHEMA)

    def _original(self, asset_id: str) -> Path:
        return self.root / f"{asset_id}.img"

    def _thumb(self, asset_id: str) -> Path:
        return self.root / f"{asset_id}.jpg"

    def _visible(self, caller: str, asset_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT id, owner, kind FROM assets WHERE id=? AND owner IN (?, ?)",
                               (asset_id, caller, PUBLIC)).fetchone()
        if row is None:
            raise AssetError("image_asset_not_found", 404)
        return row

    def list(self, caller: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"mine": [], "public": []}
        with self._lock:
            for row in self._db.execute(
                "SELECT id, owner, name, kind, created_at FROM assets WHERE owner IN (?, ?) ORDER BY created_at DESC, id",
                (caller, PUBLIC),
            ):
                asset = dict(row)
                group = "public" if asset.pop("owner") == PUBLIC else "mine"
                result[group].append(asset)
        return result

    def add(self, caller: str, *, name: str, kind: str, raw: bytes, public: bool, is_owner: bool) -> dict[str, Any]:
        if public and not is_owner:
            raise AssetError("image_asset_public_forbidden", 403)
        name = name.strip()
        if not name or len(name) > 80 or kind not in ASSET_KINDS:
            raise AssetError("image_bad_asset")
        try:
            sizes.check_upload(raw, MAX_ASSET_BYTES)
            thumb = sizes.thumbnail(raw)
        except sizes.SizeError as exc:
            raise AssetError(str(exc), 413 if str(exc) == "image_upload_too_large" else 400) from None
        except OSError:
            raise AssetError("image_upload_type") from None

        asset_id, created_at = secrets.token_hex(12), self._clock()
        owner = PUBLIC if public else caller
        original_path, thumb_path = self._original(asset_id), self._thumb(asset_id)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                count = self._db.execute("SELECT COUNT(*) FROM assets WHERE owner=?", (owner,)).fetchone()[0]
                if count >= ASSET_LIMIT:
                    raise AssetError("image_asset_limit", 409)
                original_path.write_bytes(raw)
                thumb_path.write_bytes(thumb)
                self._db.execute("INSERT INTO assets(id, owner, name, kind, created_at) VALUES (?,?,?,?,?)",
                                 (asset_id, owner, name, kind, created_at))
                self._db.commit()
            except Exception:
                self._db.rollback()
                original_path.unlink(missing_ok=True)
                thumb_path.unlink(missing_ok=True)
                raise
        return {"id": asset_id, "name": name, "kind": kind, "created_at": created_at}

    def delete(self, caller: str, asset_id: str, *, is_owner: bool) -> None:
        with self._lock:
            row = self._visible(caller, asset_id)
            if row["owner"] == PUBLIC and not is_owner:
                raise AssetError("image_asset_public_forbidden", 403)
            self._db.execute("DELETE FROM assets WHERE id=?", (row["id"],))
            self._original(row["id"]).unlink(missing_ok=True)
            self._thumb(row["id"]).unlink(missing_ok=True)

    def kind(self, caller: str, asset_id: str) -> str:
        with self._lock:
            return self._visible(caller, asset_id)["kind"]

    def _read_bytes(self, caller: str, asset_id: str, *, thumbnail: bool) -> bytes:
        with self._lock:
            row = self._visible(caller, asset_id)
            path = self._thumb(row["id"]) if thumbnail else self._original(row["id"])
            try:
                return path.read_bytes()
            except FileNotFoundError:
                raise AssetError("image_asset_not_found", 404) from None

    def thumbnail(self, caller: str, asset_id: str) -> bytes:
        return self._read_bytes(caller, asset_id, thumbnail=True)

    def reference(self, caller: str, asset_id: str) -> bytes:
        return sizes.as_png(self._read_bytes(caller, asset_id, thumbnail=False))
