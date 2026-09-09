"""Local-only message history + attachment blobs. Each node stores ONLY its own
conversations under RYNMESH_HOME/messages/. No other node holds this data."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("rynmesh.messaging_store")


def _safe(peer_id: str) -> str:
    return base64.urlsafe_b64encode(peer_id.encode("utf-8")).decode("ascii").rstrip("=")


def _peer_hash(peer_id: str) -> str:
    """A stable, non-reversible handle for a conversation, safe to log."""

    return hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:16]


class MessagingStore:
    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "messages"
        #: Lines `history()` could not parse since this store was constructed.
        self.skipped_history_lines = 0

    def _conv_path(self, peer_id: str) -> Path:
        return self.root / f"{_safe(peer_id)}.jsonl"

    def append(self, peer_id: str, record: dict[str, Any]) -> None:
        path = self._conv_path(peer_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def history(self, peer_id: str) -> list[dict[str, Any]]:
        """Every stored record for one conversation; unparseable lines are skipped.

        The file is append-only, so a truncated tail (a crash mid-write, a full
        disk) or one corrupt line would otherwise take the whole conversation
        down with it — including the dedupe check `PeerMessenger.receive` runs
        against this list. Only a hash of the peer id and a count are logged;
        the line itself is message content and never reaches the log.
        """

        path = self._conv_path(peer_id)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                skipped += 1
        if skipped:
            self.skipped_history_lines += skipped
            log.warning(
                "messaging history: skipped %d unparseable line(s) conversation=%s",
                skipped,
                _peer_hash(peer_id),
            )
        return records

    def save_attachment(self, msg_id: str, data: bytes) -> str:
        d = self.root / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        path = d / _safe(msg_id)
        path.write_bytes(data)
        return str(path)

    def load_attachment(self, msg_id: str) -> bytes:
        return (self.root / "attachments" / _safe(msg_id)).read_bytes()
