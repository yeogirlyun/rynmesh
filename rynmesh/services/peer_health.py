"""Server-side reachability probe for discovered mesh peers.

The browser cannot probe remote peer endpoints (CORS, mixed content, and the
x-ryn-auth network-key header), so the node does it: each peer's <endpoint>/health
is checked concurrently, with the network-key auth header when one is configured.
"""
from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Callable


def _default_probe(url: str, headers: dict[str, str], timeout_s: float) -> bool:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


class PeerHealthProbe:
    def __init__(
        self,
        *,
        probe: Callable[[str, dict[str, str], float], bool] | None = None,
        network_key: str | None = None,
        timeout_s: float = 5.0,
        max_concurrency: int = 8,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._probe = probe or _default_probe
        key = network_key if network_key is not None else os.environ.get("RYNMESH_NETWORK_KEY", "")
        self._network_key = key.strip()
        self._timeout_s = timeout_s
        self._max_concurrency = max(1, max_concurrency)
        self._now = now or (lambda: datetime.now(UTC))

    def auth_header(self) -> dict[str, str]:
        if not self._network_key:
            return {}
        token = hashlib.sha256(("rynmesh-net-key:" + self._network_key).encode("utf-8")).hexdigest()
        return {"x-ryn-auth": token}

    def _check_one(self, peer: dict[str, Any]) -> bool:
        if peer.get("isSelf"):
            return True
        endpoint = str(peer.get("endpoint", "") or "")
        if not endpoint.startswith(("http://", "https://")):
            return False
        url = endpoint.rstrip("/") + "/health"
        try:
            return bool(self._probe(url, self.auth_header(), self._timeout_s))
        except Exception:
            return False

    def check(self, peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        checked_at = self._now().isoformat()
        results: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as ex:
            futures = {ex.submit(self._check_one, p): str(p.get("id", "")) for p in peers}
            for fut, pid in futures.items():
                try:
                    results[pid] = fut.result()
                except Exception:
                    results[pid] = False
        return [
            {"peerId": str(p.get("id", "")), "online": results.get(str(p.get("id", "")), False), "checkedAt": checked_at}
            for p in peers
        ]
