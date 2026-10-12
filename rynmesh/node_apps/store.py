"""Encrypted atomic workspace, grant and original-operation snapshots."""

import json
from copy import deepcopy
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .engine import AppError

VERSION = "ryn.node-apps.v1"
CHANNEL = b"rynmesh-node-app-storage-v1"
MAX_FILE = 32 * 1024 * 1024


class AppStore:
    def __init__(self, home, key):
        self.path = Path(home) / "node-apps/state.json"
        self.lock = self.path.with_suffix(".lock")
        self.key, self.pub = key, peer_box.public_key_b64(key)

    def _read(self):
        if not self.path.exists():
            return {"version": VERSION, "grants": {}, "workspaces": {}, "intents": {}}
        try:
            envelope = read_json(self.path, max_bytes=MAX_FILE)
            if envelope["version"] != VERSION:
                raise ValueError
            data = json.loads(
                peer_box.open_sealed(
                    self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL
                )
            )
            if (
                data["version"] != VERSION
                or not isinstance(data["workspaces"], dict)
                or not isinstance(data["grants"], dict)
            ):
                raise ValueError
            data.setdefault("intents", {})
            return data
        except Exception:
            raise AppError("apps_store_unavailable") from None

    def read(self):
        with file_transaction(self.lock):
            return self._read()

    def mutate(self, change):
        with file_transaction(self.lock):
            data = self._read()
            before = canonical_json(data)
            result = change(data)
            raw = canonical_json(data)
            if before != raw:
                if (
                    len(raw)
                    + sum(
                        256 * 1024
                        for intent in data.get("intents", {}).values()
                        if intent["status"] == "pending"
                    )
                    > 20 * 1024 * 1024
                    or len(data["workspaces"]) > 96
                ):
                    raise AppError("apps_capacity")
                nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=CHANNEL)
                wire = {"version": VERSION, "nonce": nonce, "ciphertext": ciphertext}
                if len(canonical_json(wire)) > MAX_FILE - 4096:
                    raise AppError("apps_capacity")
                atomic_write_json(self.path, wire, max_bytes=MAX_FILE)
            return deepcopy(result)
