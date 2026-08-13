"""Local-only message history + attachment blobs. Each node stores ONLY its own
conversations under RYNMESH_HOME/messages/. No other node holds this data."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any


def _safe(peer_id: str) -> str:
    return base64.urlsafe_b64encode(peer_id.encode("utf-8")).decode("ascii").rstrip("=")


class MessagingStore:
    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "messages"

    def _conv_path(self, peer_id: str) -> Path:
        return self.root / f"{_safe(peer_id)}.jsonl"

    def append(self, peer_id: str, record: dict[str, Any]) -> None:
        path = self._conv_path(peer_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def history(self, peer_id: str) -> list[dict[str, Any]]:
        path = self._conv_path(peer_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def save_attachment(self, msg_id: str, data: bytes) -> str:
        d = self.root / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        path = d / _safe(msg_id)
        path.write_bytes(data)
        return str(path)

    def load_attachment(self, msg_id: str) -> bytes:
        return (self.root / "attachments" / _safe(msg_id)).read_bytes()
