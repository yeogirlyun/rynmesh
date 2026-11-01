"""Local SQLite persistence for device drop transfers and node messages.

Byte offsets and sizes are SQLite INTEGER (signed 64-bit) and Python ints, so
nothing here overflows at 2 GiB or 4 GiB. The database lives beside the
identity it belongs to (``<home>/drop/drop.sqlite3``).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
  transfer_id TEXT PRIMARY KEY,
  offer_id TEXT NOT NULL,
  peer_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  file_count INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  confirmed_bytes INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  route TEXT,
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  error_need INTEGER,
  error_free INTEGER,
  note TEXT NOT NULL DEFAULT '',
  save_dir TEXT NOT NULL DEFAULT '',
  final_path TEXT NOT NULL DEFAULT '',
  staging_dir TEXT NOT NULL DEFAULT '',
  client_id TEXT,
  eph_private BLOB,
  eph_pub TEXT NOT NULL DEFAULT '',
  master_key BLOB,
  receiver_pub TEXT NOT NULL DEFAULT '',
  paused INTEGER NOT NULL DEFAULT 0,
  offer_delivered INTEGER NOT NULL DEFAULT 0,
  needs_verification INTEGER NOT NULL DEFAULT 0,
  source_kind TEXT NOT NULL DEFAULT 'paths',
  upload_batch TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS transfers_client ON transfers(client_id) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS transfers_peer ON transfers(peer_id, created_at);
CREATE TABLE IF NOT EXISTS transfer_files (
  transfer_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  rel_path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  source_path TEXT NOT NULL DEFAULT '',
  committed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  root TEXT NOT NULL DEFAULT '',
  final_path TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (transfer_id, file_id)
);
CREATE TABLE IF NOT EXISTS chunks (
  transfer_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY (transfer_id, file_id, idx)
);
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  peer_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  delivered_at TEXT,
  error TEXT NOT NULL DEFAULT '',
  client_id TEXT,
  held INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_client ON messages(client_id) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_peer ON messages(peer_id, created_at);
CREATE TABLE IF NOT EXISTS peer_prefs (
  peer_id TEXT PRIMARY KEY,
  auto_accept INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0,
  last_read_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

ACTIVE_OUT = ("connecting", "waiting_online", "awaiting_accept", "transferring", "reconnecting", "verifying",
              "insufficient_space")
FINAL = ("completed", "rejected", "cancelled", "source_changed", "verify_failed")

TRANSFER_FIELDS = {
    "offer_id", "peer_id", "direction", "kind", "name", "file_count", "total_bytes", "confirmed_bytes",
    "status", "route", "error_code", "error_message", "error_need", "error_free", "note", "save_dir",
    "final_path", "staging_dir", "client_id", "eph_private", "eph_pub", "master_key", "receiver_pub",
    "paused", "offer_delivered", "needs_verification", "source_kind", "upload_batch", "completed_at",
}
MESSAGE_FIELDS = {"status", "delivered_at", "error", "held", "attempts", "text", "kind"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class DropStore:
    def __init__(self, home: Path) -> None:
        self.root = Path(home) / "drop"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "drop.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    # ---- settings ----
    def setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    # ---- peer prefs ----
    def prefs(self, peer_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM peer_prefs WHERE peer_id=?", (peer_id,))
        return row or {"peer_id": peer_id, "auto_accept": 0, "verified": 0, "blocked": 0, "last_read_at": ""}

    def all_prefs(self) -> dict[str, dict[str, Any]]:
        return {row["peer_id"]: row for row in self._all("SELECT * FROM peer_prefs")}

    def set_prefs(self, peer_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"auto_accept", "verified", "blocked", "last_read_at"}
        current = self.prefs(peer_id)
        current.update({k: v for k, v in values.items() if k in allowed})
        with self.tx() as db:
            db.execute(
                "INSERT INTO peer_prefs(peer_id,auto_accept,verified,blocked,last_read_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET auto_accept=excluded.auto_accept, verified=excluded.verified, "
                "blocked=excluded.blocked, last_read_at=excluded.last_read_at",
                (peer_id, int(current["auto_accept"]), int(current["verified"]), int(current["blocked"]),
                 str(current["last_read_at"] or "")),
            )
        return current

    # ---- transfers ----
    def insert_transfer(self, record: dict[str, Any], files: list[dict[str, Any]]) -> None:
        stamp = now_iso()
        row = {"created_at": stamp, "updated_at": stamp, "version": 1, **record}
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self.tx() as db:
            db.execute(f"INSERT INTO transfers({columns}) VALUES({marks})", tuple(row.values()))
            db.executemany(
                "INSERT INTO transfer_files(transfer_id,file_id,idx,rel_path,size,mtime_ns,source_path,committed,status) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(row["transfer_id"], f["file_id"], f["idx"], f["rel_path"], int(f["size"]), int(f.get("mtime_ns", 0)),
                  f.get("source_path", ""), int(f.get("committed", 0)), f.get("status", "pending")) for f in files],
            )

    def transfer(self, transfer_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,))

    def transfer_by_client(self, client_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM transfers WHERE client_id=?", (client_id,))

    def transfers(self, *, peer_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if peer_id:
            return self._all("SELECT * FROM transfers WHERE peer_id=? ORDER BY created_at DESC LIMIT ?", (peer_id, limit))
        return self._all("SELECT * FROM transfers ORDER BY created_at DESC LIMIT ?", (limit,))

    def transfers_in(self, statuses: tuple[str, ...], direction: str | None = None) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM transfers WHERE status IN ({marks})"
        args: tuple = tuple(statuses)
        if direction:
            sql += " AND direction=?"
            args += (direction,)
        return self._all(sql, args)

    def update_transfer(self, transfer_id: str, **values: Any) -> dict[str, Any]:
        bad = set(values) - TRANSFER_FIELDS
        if bad:
            raise ValueError(f"unknown transfer fields: {sorted(bad)}")
        assignments = ",".join(f"{key}=?" for key in values)
        with self.tx() as db:
            db.execute(
                f"UPDATE transfers SET {assignments}{',' if values else ''} updated_at=?, version=version+1 "
                "WHERE transfer_id=?",
                (*values.values(), now_iso(), transfer_id),
            )
            row = db.execute("SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise KeyError(transfer_id)
        return dict(row)

    def files(self, transfer_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM transfer_files WHERE transfer_id=? ORDER BY idx", (transfer_id,))

    def files_head(self, transfer_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM transfer_files WHERE transfer_id=? ORDER BY idx LIMIT ?", (transfer_id, limit))

    def pending_files(self, transfer_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM transfer_files WHERE transfer_id=? AND committed<size ORDER BY idx LIMIT ?",
                         (transfer_id, limit))

    def committed_total(self, transfer_id: str) -> int:
        row = self._one("SELECT COALESCE(SUM(committed),0) AS n FROM transfer_files WHERE transfer_id=?",
                        (transfer_id,))
        return int(row["n"]) if row else 0

    def file(self, transfer_id: str, file_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM transfer_files WHERE transfer_id=? AND file_id=?", (transfer_id, file_id))

    def update_file(self, transfer_id: str, file_id: str, **values: Any) -> None:
        allowed = {"committed", "status", "root", "final_path", "mtime_ns", "size"}
        bad = set(values) - allowed
        if bad:
            raise ValueError(f"unknown file fields: {sorted(bad)}")
        assignments = ",".join(f"{key}=?" for key in values)
        with self.tx() as db:
            db.execute(f"UPDATE transfer_files SET {assignments} WHERE transfer_id=? AND file_id=?",
                       (*values.values(), transfer_id, file_id))

    def set_chunks(self, transfer_id: str, file_id: str, chunks: list[tuple[int, str]]) -> None:
        if not chunks:
            return
        with self.tx() as db:
            db.executemany(
                "INSERT INTO chunks(transfer_id,file_id,idx,sha256) VALUES(?,?,?,?) "
                "ON CONFLICT(transfer_id,file_id,idx) DO UPDATE SET sha256=excluded.sha256",
                [(transfer_id, file_id, idx, value) for idx, value in chunks],
            )

    def commit_chunk(self, transfer_id: str, file_id: str, idx: int, sha256: str, committed: int) -> None:
        """Record one fsynced chunk and the new committed offset atomically."""

        with self.tx() as db:
            db.execute(
                "INSERT INTO chunks(transfer_id,file_id,idx,sha256) VALUES(?,?,?,?) "
                "ON CONFLICT(transfer_id,file_id,idx) DO UPDATE SET sha256=excluded.sha256",
                (transfer_id, file_id, idx, sha256),
            )
            db.execute("UPDATE transfer_files SET committed=?, status='transferring' WHERE transfer_id=? AND file_id=?",
                       (committed, transfer_id, file_id))

    def chunks(self, transfer_id: str, file_id: str) -> list[str]:
        rows = self._all("SELECT idx, sha256 FROM chunks WHERE transfer_id=? AND file_id=? ORDER BY idx",
                         (transfer_id, file_id))
        out: list[str] = []
        for expected, row in enumerate(rows):
            if row["idx"] != expected:
                break
            out.append(row["sha256"])
        return out

    def drop_chunks_from(self, transfer_id: str, file_id: str, idx: int) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM chunks WHERE transfer_id=? AND file_id=? AND idx>=?", (transfer_id, file_id, idx))

    # ---- messages ----
    def insert_message(self, record: dict[str, Any]) -> dict[str, Any]:
        row = {"version": 1, **record}
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self.tx() as db:
            db.execute(f"INSERT INTO messages({columns}) VALUES({marks})", tuple(row.values()))
        return self.message(row["message_id"])  # type: ignore[return-value]

    def message(self, message_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM messages WHERE message_id=?", (message_id,))

    def message_by_client(self, client_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM messages WHERE client_id=?", (client_id,))

    def update_message(self, message_id: str, **values: Any) -> dict[str, Any]:
        bad = set(values) - MESSAGE_FIELDS
        if bad:
            raise ValueError(f"unknown message fields: {sorted(bad)}")
        assignments = ",".join(f"{key}=?" for key in values)
        with self.tx() as db:
            db.execute(f"UPDATE messages SET {assignments}, version=version+1 WHERE message_id=?",
                       (*values.values(), message_id))
            row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        return dict(row)

    def messages(self, peer_id: str, *, limit: int = 300) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM messages WHERE peer_id=? ORDER BY created_at DESC LIMIT ?", (peer_id, limit))
        rows.reverse()
        return rows

    def outbox(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM messages WHERE direction='out' AND status IN ('waiting_online','sending') "
                         "ORDER BY created_at")

    def delete_held(self, peer_id: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM messages WHERE peer_id=? AND held=1", (peer_id,))

    def release_held(self, peer_id: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE messages SET held=0, version=version+1 WHERE peer_id=? AND held=1", (peer_id,))

    def conversation_peers(self) -> list[str]:
        rows = self._all("SELECT peer_id FROM messages UNION SELECT peer_id FROM transfers")
        return [row["peer_id"] for row in rows]

    def last_activity(self, peer_id: str) -> dict[str, Any] | None:
        message = self._one("SELECT kind, text, created_at FROM messages WHERE peer_id=? AND held=0 "
                            "ORDER BY created_at DESC LIMIT 1", (peer_id,))
        transfer = self._one("SELECT name, created_at FROM transfers WHERE peer_id=? ORDER BY created_at DESC LIMIT 1",
                             (peer_id,))
        if transfer and (not message or transfer["created_at"] > message["created_at"]):
            return {"kind": "transfer", "text": transfer["name"], "at": transfer["created_at"]}
        if message:
            return {"kind": message["kind"], "text": message["text"], "at": message["created_at"]}
        return None

    def unread(self, peer_id: str, since: str) -> int:
        row = self._one(
            "SELECT (SELECT COUNT(*) FROM messages WHERE peer_id=? AND direction='in' AND held=0 AND created_at>?) + "
            "(SELECT COUNT(*) FROM transfers WHERE peer_id=? AND direction='in' AND created_at>?) AS n",
            (peer_id, since, peer_id, since),
        )
        return int(row["n"]) if row else 0
