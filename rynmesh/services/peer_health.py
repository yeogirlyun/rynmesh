"""Device presence and separate HTTP reachability for discovered mesh peers.

The browser cannot probe remote peer endpoints (CORS, mixed content, and the
x-ryn-auth network-key header), so the node does it: each peer's <endpoint>/health
is checked concurrently, with the network-key auth header when one is configured.
An unreachable advertised HTTP address is not proof that the device is offline:
devices behind NAT can still be present and connect over ICE/UDP.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import ipaddress
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from ..transport import _NoRedirect, network_key_header

_PROBE_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)
_BLOCKED_PROBE_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "metadata", "localhost"})


def _parse_iso_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text == "local":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _probe_endpoint_allowed(endpoint: str) -> bool:
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        return False
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or parsed.fragment:
        return False
    normalized = parsed.hostname.strip("[]").lower()
    if normalized in _BLOCKED_PROBE_HOSTS or normalized.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return True
    return not (ip.is_link_local or ip.is_loopback or ip.is_unspecified)


def _default_probe(url: str, headers: dict[str, str], timeout_s: float) -> bool:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _PROBE_OPENER.open(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
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
        self._rows: dict[str, tuple[float, datetime | None, dict[str, Any]]] = {}

    def update(self, rows: list[dict[str, Any]]) -> None:
        now_mono = self._clock()
        with self._lock:
            for row in rows:
                peer_id = str(row.get("peerId") or "")
                if not peer_id:
                    continue
                observed_at = str(row.get("observed_at") or row.get("checkedAt") or "") or datetime.now(UTC).isoformat()
                obs_dt = _parse_iso_utc(observed_at)
                source = str(row.get("source") or row.get("presenceSource") or "unknown")
                existing = self._rows.get(peer_id)
                if existing is not None and now_mono - existing[0] <= self._ttl_s:
                    prev_dt = existing[1]
                    if prev_dt is not None and obs_dt is not None and obs_dt < prev_dt:
                        continue
                normalized = {
                    **row,
                    "peerId": peer_id,
                    "observed_at": observed_at,
                    "checkedAt": str(row.get("checkedAt") or observed_at),
                    "source": source,
                    "presenceSource": str(row.get("presenceSource") or source),
                }
                self._rows[peer_id] = (now_mono, obs_dt, normalized)

    def get(self, peer_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._rows.get(peer_id)
            if entry is None:
                return None
            if self._clock() - entry[0] > self._ttl_s:
                del self._rows[peer_id]
                return None
            return dict(entry[2])


def classify_agent_presence(
    health: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    max_age_s: float = 180.0,
) -> dict[str, Any]:
    """Normalize a presence observation for Agent device tools.

    Offline is concluded ONLY when ``observed_at`` is within ``max_age_s``;
    stale or missing observations become ``unconfirmed`` (``online=None``).
    """
    current_now = now or datetime.now(UTC)
    if not health:
        return {"online": None, "presence_state": "unconfirmed", "observed_at": "", "source": "unknown"}
    observed_at = str(health.get("observed_at") or health.get("checkedAt") or "")
    raw_source = str(health.get("source") or health.get("presenceSource") or "unknown")
    norm_source = (
        "heartbeat"
        if raw_source in {"heartbeat", "heartbeat_expired"}
        else raw_source if raw_source in {"self", "http", "drop_probe"} else "unknown"
    )
    if raw_source == "self":
        return {"online": True, "presence_state": "online", "observed_at": observed_at, "source": "self"}
    obs_dt = _parse_iso_utc(observed_at)
    age = (current_now - obs_dt).total_seconds() if obs_dt is not None else None
    is_fresh = age is not None and -120.0 <= age <= max_age_s
    raw_online = health.get("online")
    if is_fresh and raw_online is True:
        return {"online": True, "presence_state": "online", "observed_at": observed_at, "source": norm_source}
    if is_fresh and raw_online is False:
        return {"online": False, "presence_state": "offline", "observed_at": observed_at, "source": norm_source}
    return {"online": None, "presence_state": "unconfirmed", "observed_at": observed_at, "source": norm_source}


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
        self._explicit_network_key = network_key
        self._timeout_s = timeout_s
        self._max_concurrency = max(1, max_concurrency)
        self._now = now or (lambda: datetime.now(UTC))

    def auth_header(self) -> dict[str, str]:
        return {k.lower(): v for k, v in network_key_header(self._explicit_network_key).items()}

    def _check_one(self, peer: dict[str, Any]) -> bool:
        if peer.get("isSelf"):
            return True
        endpoint = str(peer.get("endpoint", "") or "")
        if not _probe_endpoint_allowed(endpoint):
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
            if not _probe_endpoint_allowed(endpoint):
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
                seen = _parse_iso_utc(last_seen)
                age = (now - seen).total_seconds() if seen is not None else None
                # Registration refreshes every 30s. Allow a 2-minute expiry and
                # modest clock skew; stale cached data cannot prove an outage.
                if age is not None and -120 <= age <= 120:
                    online, source = True, "heartbeat"
                elif age is not None and age > 120 and peer.get("discoverySource") in {"registry", "capacity"}:
                    online, source = False, "heartbeat_expired"
            output.append({"peerId": peer_id, "online": online, "checkedAt": checked_at,
                           "observed_at": checked_at, "source": source,
                           "httpReachable": reachable, "lanReachable": lan_reachable,
                           "presenceSource": source,
                           "lastSeen": last_seen})
        return output
