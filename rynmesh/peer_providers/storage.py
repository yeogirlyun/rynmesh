"""Host-key sealed credentials; no plaintext or token-bearing cache on disk."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..services import peer_box
from .validation import PeerProviderError

CHANNEL = b"rynmesh-peer-provider-credentials-v1"


class SealedStore:
    def __init__(self, home, messaging_key):
        self.path = Path(home) / "peer-providers" / "state.json"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {}
            try:
                envelope = read_json(self.path, max_bytes=4 * 1024 * 1024)
                raw = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError
                return value
            except Exception:
                raise PeerProviderError("peer_provider_storage_unavailable") from None

    def write(self, value):
        with self.lock:
            try:
                raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError
                nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=CHANNEL)
                atomic_write_json(self.path, {"version": 1, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=4 * 1024 * 1024)
            except Exception:
                raise PeerProviderError("peer_provider_storage_unavailable") from None
