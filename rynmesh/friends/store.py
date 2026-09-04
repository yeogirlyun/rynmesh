from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


class FriendStore:
    """Atomic local relationship state; secrets never appear in public records."""

    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "friends"
        self.state_path = self.root / "state.json"
        self.secrets_path = self.root / "secrets.json"
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"invites": {}, "relationships": {}, "nonces": {}}

    def _read(self, path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return deepcopy(fallback)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else deepcopy(fallback)
        except (OSError, json.JSONDecodeError):
            return deepcopy(fallback)

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._read(self.state_path, self._empty())

    def secrets(self) -> dict[str, str]:
        with self._lock:
            return self._read(self.secrets_path, {})

    def put_invite(self, invite_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            state = self._read(self.state_path, self._empty())
            state["invites"][invite_id] = deepcopy(record)
            self._write(self.state_path, state)

    def list_invites(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for row in self.state()["invites"].values() if isinstance(row, dict)]

    def cancel_invite(self, invite_id: str, cancelled_at: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read(self.state_path, self._empty())
            invite = state["invites"].get(invite_id)
            if not isinstance(invite, dict):
                return None
            if invite.get("status") == "active":
                invite["status"] = "cancelled"
                invite["cancelled_at"] = cancelled_at
                self._write(self.state_path, state)
            return deepcopy(invite)

    def consume_invite(self, invite_id: str, secret_hash: str, now_iso: str) -> bool:
        with self._lock:
            state = self._read(self.state_path, self._empty())
            invite = state["invites"].get(invite_id)
            if not invite or invite.get("status") != "active" or invite.get("secret_hash") != secret_hash:
                return False
            if str(invite.get("expires_at", "")) <= now_iso:
                invite["status"] = "expired"
                self._write(self.state_path, state)
                return False
            invite["status"] = "used"
            invite["used_at"] = now_iso
            self._write(self.state_path, state)
            return True

    def put_relationship(self, record: dict[str, Any], secret: bytes) -> None:
        import base64

        rid = str(record["relationship_id"])
        with self._lock:
            state = self._read(self.state_path, self._empty())
            state["relationships"][rid] = deepcopy(record)
            secrets = self._read(self.secrets_path, {})
            secrets[rid] = base64.b64encode(secret).decode("ascii")
            self._write(self.secrets_path, secrets)
            self._write(self.state_path, state)

    def relationship(self, relationship_id: str, *, active_only: bool = True) -> dict[str, Any] | None:
        record = self.state()["relationships"].get(relationship_id)
        if not isinstance(record, dict) or (active_only and record.get("status") != "active"):
            return None
        return deepcopy(record)

    def relationship_for_peer(self, peer_id: str) -> dict[str, Any] | None:
        for record in self.state()["relationships"].values():
            if isinstance(record, dict) and record.get("peer_id") == peer_id and record.get("status") == "active":
                return deepcopy(record)
        return None

    def list_relationships(self) -> list[dict[str, Any]]:
        rows = [deepcopy(row) for row in self.state()["relationships"].values() if isinstance(row, dict)]
        return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)

    def secret(self, relationship_id: str) -> bytes | None:
        import base64

        value = self.secrets().get(relationship_id)
        try:
            return base64.b64decode(value) if value else None
        except ValueError:
            return None

    def revoke(
        self, relationship_id: str, revoked_at: str, *, retain_secret: bool = False
    ) -> dict[str, Any] | None:
        with self._lock:
            state = self._read(self.state_path, self._empty())
            record = state["relationships"].get(relationship_id)
            if not isinstance(record, dict):
                return None
            record["status"] = "revoked"
            record["revoked_at"] = revoked_at
            secrets = self._read(self.secrets_path, {})
            value = secrets.pop(relationship_id, None)
            if retain_secret and value:
                secrets[f"revocation:{relationship_id}"] = value
            self._write(self.secrets_path, secrets)
            self._write(self.state_path, state)
            return deepcopy(record)

    def pending_revocation_secret(self, relationship_id: str) -> bytes | None:
        import base64

        value = self.secrets().get(f"revocation:{relationship_id}")
        try:
            return base64.b64decode(value) if value else None
        except ValueError:
            return None

    def set_revocation_delivery(
        self, relationship_id: str, *, state_value: str, wire: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            state = self._read(self.state_path, self._empty())
            record = state["relationships"].get(relationship_id)
            if not isinstance(record, dict):
                return
            record["revocation_delivery"] = state_value
            if wire is None:
                record.pop("revocation_wire", None)
            else:
                record["revocation_wire"] = deepcopy(wire)
            secrets = self._read(self.secrets_path, {})
            if state_value == "delivered":
                secrets.pop(f"revocation:{relationship_id}", None)
                self._write(self.secrets_path, secrets)
            self._write(self.state_path, state)

    def remember_nonce(self, relationship_id: str, nonce: str, *, limit: int = 512) -> bool:
        with self._lock:
            state = self._read(self.state_path, self._empty())
            seen = list(state["nonces"].get(relationship_id, []))
            if nonce in seen:
                return False
            seen.append(nonce)
            state["nonces"][relationship_id] = seen[-limit:]
            self._write(self.state_path, state)
            return True
