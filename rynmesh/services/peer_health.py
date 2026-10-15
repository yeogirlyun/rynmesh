"""Device presence and separate HTTP reachability for discovered mesh peers.

The browser cannot probe remote peer endpoints (CORS, mixed content, and the
x-ryn-auth network-key header), so the node does it: each peer's <endpoint>/health
is checked concurrently, with the network-key auth header when one is configured.
An unreachable advertised HTTP address is not proof that the device is offline:
devices behind NAT can still be present and connect over ICE/UDP.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
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


class PeerPresenceCache:
    """Latest probe observation per peer, shared by every view of the same device.

    The Devices list and the AI Services list must agree on whether a device is
    reachable, so both read what this node last *observed* here instead of a
    provider's self-reported flag. Entries expire: stale presence is "unconfirmed",
    never "online".
    """

    def __init__(self, *, ttl_s: float = 180.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def update(self, rows: list[dict[str, Any]]) -> None:
        now = self._clock()
        with self._lock:
            for row in rows:
                peer_id = str(row.get("peerId") or "")
                if peer_id:
                    self._rows[peer_id] = (now, dict(row))

    def get(self, peer_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._rows.get(peer_id)
            if entry is None:
                return None
            if self._clock() - entry[0] > self._ttl_s:
                del self._rows[peer_id]
                return None
            return dict(entry[1])


class PeerHealthProbe:
    def __init__(
        self,
        *,
        probe: Callable[[str, dict[str, str], float], bool] | None = None,
        network_key: str | None = None,
        timeout_s: float = 1.5,
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

    def _check_with_lan(self, peer: dict[str, Any]) -> tuple[bool, bool]:
        if peer.get("isSelf"):
            return True, False
        # Only endpoints from fresh, signed LAN discovery reach this field.
        for endpoint in peer.get("lanEndpoints") or []:
            if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
                continue
            try:
                if self._probe(endpoint.rstrip("/") + "/health", self.auth_header(), min(self._timeout_s, 1.0)):
                    return True, True
            except Exception:
                pass
        return self._check_one(peer), False

    def check(self, peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = self._now()
        checked_at = now.isoformat()
        results: dict[str, bool] = {}
        lan_results: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as ex:
            futures = {ex.submit(self._check_with_lan, p): str(p.get("id", "")) for p in peers}
            for fut, pid in futures.items():
                try:
                    results[pid], lan_results[pid] = fut.result()
                except Exception:
                    results[pid] = False
        output = []
        for peer in peers:
            peer_id = str(peer.get("id", ""))
            reachable = results.get(peer_id, False)
            lan_reachable = lan_results.get(peer_id, False)
            online, source = None, "unknown"
            last_seen = str(peer.get("lastSeen") or "")
            if peer.get("isSelf"):
                online, source = True, "self"
            elif reachable:
                online, source = True, "http"
            else:
                try:
                    seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    age = (now - seen).total_seconds() if seen.tzinfo else None
                except (ValueError, TypeError, OverflowError):
                    age = None
                # Registration refreshes every 30s. Allow a 2-minute expiry and
                # modest clock skew; stale cached data cannot prove an outage.
                if age is not None and -120 <= age <= 120:
                    online, source = True, "heartbeat"
                elif age is not None and age > 120 and peer.get("discoverySource") in {"registry", "capacity"}:
                    online, source = False, "heartbeat_expired"
            output.append({"peerId": peer_id, "online": online, "checkedAt": checked_at,
                           "httpReachable": reachable, "lanReachable": lan_reachable,
                           "presenceSource": source,
                           "lastSeen": last_seen})
        return output
