"""Peer directory for device drop: who a device is, how to reach it, and trust.

Reachability is proven, not inferred: a probe calls ``/api/peer/drop/hello`` on
each candidate endpoint and only accepts a response signed by the expected
identity, bound to a fresh nonce. The probe also yields the peer's messaging
key, authenticated by that same signature, so LAN-only peers (whose discovery
records do not carry it) are supported without trust-on-first-use.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from ..crypto import SignatureError, SignedPayload, sign_payload, verify_signed_payload
from .codec import PROTOCOL
from .p2p_link import is_p2p_endpoint, p2p_endpoint, peer_of

PROBE_TIMEOUT_S = 2.5
PROBE_OK_TTL_S = 8.0
PROBE_FAIL_TTL_S = 4.0
RECORDS_TTL_S = 5.0


def platform_label() -> str:
    import platform

    system = platform.system()
    return {"Darwin": "macOS"}.get(system, system)


def make_hello(*, peer_id: str, node_name: str, messaging_pub: str, nonce: str, private_key_bytes: bytes) -> dict:
    payload = {
        "protocol": PROTOCOL,
        "type": "hello",
        "peer_id": peer_id,
        "node_name": node_name,
        "messaging_pub": messaging_pub,
        "platform": platform_label(),
        "nonce": nonce,
        "ts": int(time.time()),
    }
    return sign_payload(payload, private_key_bytes=private_key_bytes).to_dict()


def verify_hello(data: Any, *, peer_id: str, nonce: str) -> dict[str, Any]:
    signed = SignedPayload.from_dict(data)
    verify_signed_payload(signed)
    payload = signed.payload
    if (signed.public_key != peer_id or payload.get("peer_id") != peer_id or payload.get("nonce") != nonce
            or payload.get("type") != "hello" or payload.get("protocol") != PROTOCOL
            or not isinstance(payload.get("messaging_pub"), str)):
        raise SignatureError("hello does not match the expected device")
    return payload


def classify_route(endpoint: str) -> str:
    """``lan`` for private/loopback/link-local hosts, ``public`` otherwise."""

    host = urlsplit(endpoint).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "public"
    if address.is_private or address.is_loopback or address.is_link_local:
        return "lan"
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
        return "lan"
    return "public"


def network_headers() -> dict[str, str]:
    from ..transport import network_key_header

    return dict(network_key_header())


def http_json(endpoint: str, method: str, path: str, body: Any = None, *, timeout: float = 15.0,
              network_auth: bool = True
              ) -> tuple[int, Any]:
    url = urlsplit(endpoint)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ConnectionError("unsupported endpoint")
    cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = cls(url.hostname, url.port or (443 if url.scheme == "https" else 80), timeout=timeout)
    try:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", **(network_headers() if network_auth else {})}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(64 * 1024 * 1024)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return resp.status, parsed
    except (OSError, http.client.HTTPException) as exc:
        raise ConnectionError(str(exc) or type(exc).__name__) from exc
    finally:
        conn.close()


@dataclass
class Probe:
    peer_id: str
    online: bool
    endpoint: str = ""
    route: str | None = None
    latency_ms: int | None = None
    messaging_pub: str = ""
    name: str = ""
    platform: str = ""
    checked_at: float = field(default_factory=time.monotonic)
    error: str = ""


class PeerDirectory:
    def __init__(
        self,
        *,
        store: Any,
        network_id: Callable[[], str],
        relationship: Callable[[str], str],
        extra_endpoints: Callable[[str], list[str]] | None = None,
        p2p: Any = None,
        network_auth: bool = True,
    ) -> None:
        self._store = store
        # Internet path (ICE hole punching + QUIC) for own devices and friends
        # whose HTTP endpoints are unreachable, e.g. both behind NAT.
        self.p2p = p2p
        self._network_id = network_id
        self._relationship = relationship
        self._extra_endpoints = extra_endpoints
        self._network_auth = network_auth
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._records_at = 0.0
        self._probes: dict[str, Probe] = {}
        self._last_seen: dict[str, str] = {}
        self._probe_locks: dict[str, threading.Lock] = {}

    @property
    def me(self) -> str:
        return self._store.peer_id

    def relationship(self, peer_id: str) -> str:
        return self._relationship(peer_id)

    # ---- records ----
    def records(self, *, fresh: bool = False) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not fresh and time.monotonic() - self._records_at < RECORDS_TTL_S:
                return dict(self._records)
        try:
            found = self._store.discover_peers(network_id=self._network_id(), include_self=False, cache_only=True)
        except Exception:
            found = {"peers": []}
        records: dict[str, dict[str, Any]] = {}
        for item in found.get("peers", []):
            peer_id = str(item.get("peer_id") or "")
            if not peer_id or peer_id == self.me:
                continue
            records[peer_id] = item
        with self._lock:
            self._records = records
            self._records_at = time.monotonic()
        return dict(records)

    def record(self, peer_id: str) -> dict[str, Any] | None:
        return self.records().get(peer_id)

    def is_lan_discovered(self, peer_id: str) -> bool:
        record = self.records().get(peer_id) or {}
        return bool(record.get("lan_endpoints"))

    def candidates(self, peer_id: str) -> list[str]:
        record = self.record(peer_id) or {}
        extra: list[str] = []
        if self._extra_endpoints is not None:
            try:
                extra = list(self._extra_endpoints(peer_id) or [])
            except Exception:
                extra = []
        out: list[str] = []
        for value in [*(record.get("lan_endpoints") or []), *(record.get("endpoints") or []),
                      (record.get("metadata") or {}).get("peer_endpoint") or "", *extra]:
            value = str(value or "").rstrip("/")
            if value.startswith(("http://", "https://")) and value not in out:
                out.append(value)
        # LAN first, then public.
        out.sort(key=lambda ep: 0 if classify_route(ep) == "lan" else 1)
        return out

    def name(self, peer_id: str) -> str:
        record = self.record(peer_id) or {}
        probe = self._probes.get(peer_id)
        return str(record.get("node_name") or (probe.name if probe else "") or "")

    def last_seen(self, peer_id: str) -> str | None:
        return self._last_seen.get(peer_id)

    # ---- probing ----
    def cached(self, peer_id: str) -> Probe | None:
        return self._probes.get(peer_id)

    def probe(self, peer_id: str, *, fresh: bool = False) -> Probe:
        cached = self._probes.get(peer_id)
        if cached and not fresh:
            ttl = PROBE_OK_TTL_S if cached.online else PROBE_FAIL_TTL_S
            if time.monotonic() - cached.checked_at < ttl:
                return cached
        lock = self._probe_locks.setdefault(peer_id, threading.Lock())
        with lock:
            cached = self._probes.get(peer_id)
            if cached and not fresh and time.monotonic() - cached.checked_at < 1.0:
                return cached
            result = self._probe_now(peer_id, urgent=fresh)
            self._probes[peer_id] = result
            if result.online:
                from .store import now_iso

                self._last_seen[peer_id] = now_iso()
            return result

    def request(self, endpoint: str, method: str, path: str, body: Any = None, *, timeout: float = 15.0
                ) -> tuple[int, Any]:
        """``http_json`` for HTTP endpoints, the QUIC link for ``ryn-p2p://`` ones."""
        if not is_p2p_endpoint(endpoint):
            return http_json(endpoint, method, path, body, timeout=timeout, network_auth=self._network_auth)
        if self.p2p is None:
            raise ConnectionError("p2p is not available")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", **(network_headers() if self._network_auth else {})}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self.p2p.request(peer_of(endpoint), method, path, body=data, headers=headers,
                                           timeout_s=timeout)
        except (TimeoutError, OSError, RuntimeError) as exc:
            raise ConnectionError(str(exc) or type(exc).__name__) from exc
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return status, parsed

    def _probe_now(self, peer_id: str, *, urgent: bool = False) -> Probe:
        candidates = self.candidates(peer_id)
        linked = self.p2p is not None and self.p2p.link_info(peer_id) is not None
        if linked:
            # A live internet link exists: only a LAN address could beat it.
            candidates = [ep for ep in candidates if classify_route(ep) == "lan"]
        last_error = "no_endpoint" if not candidates else "unreachable"
        for endpoint in candidates:
            nonce = os.urandom(16).hex()
            started = time.monotonic()
            try:
                status, body = http_json(endpoint, "GET", f"/api/peer/drop/hello?nonce={nonce}",
                                         timeout=PROBE_TIMEOUT_S, network_auth=self._network_auth)
            except ConnectionError as exc:
                last_error = f"unreachable: {exc}"
                continue
            if status != 200 or not isinstance(body, dict):
                last_error = f"http_{status}"
                continue
            try:
                hello = verify_hello(body, peer_id=peer_id, nonce=nonce)
            except (SignatureError, KeyError, TypeError, ValueError):
                last_error = "identity_mismatch"
                continue
            return Probe(
                peer_id, True, endpoint=endpoint, route=classify_route(endpoint),
                latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                messaging_pub=str(hello["messaging_pub"]), name=str(hello.get("node_name") or ""),
                platform=str(hello.get("platform") or ""),
            )
        return self._probe_p2p(peer_id, urgent=urgent) or Probe(peer_id, False, error=last_error)

    def _probe_p2p(self, peer_id: str, *, urgent: bool) -> Probe | None:
        p2p = self.p2p
        if p2p is None or not p2p.running or self._relationship(peer_id) not in ("own", "friend", "external"):
            return None
        try:
            info = p2p.connect(peer_id, urgent=urgent)
        except Exception as exc:  # backoff, no answer, NAT that cannot be punched ...
            return Probe(peer_id, False, error=f"p2p: {exc}"[:200])
        endpoint = p2p_endpoint(peer_id)
        nonce = os.urandom(16).hex()
        started = time.monotonic()
        try:
            status, body = self.request(endpoint, "GET", f"/api/peer/drop/hello?nonce={nonce}",
                                        timeout=PROBE_TIMEOUT_S * 2)
            if status != 200 or not isinstance(body, dict):
                return Probe(peer_id, False, error=f"p2p http_{status}")
            hello = verify_hello(body, peer_id=peer_id, nonce=nonce)
        except ConnectionError as exc:
            return Probe(peer_id, False, error=f"p2p: {exc}"[:200])
        except (SignatureError, KeyError, TypeError, ValueError):
            return Probe(peer_id, False, error="identity_mismatch")
        return Probe(
            peer_id, True, endpoint=endpoint, route=info["route"],
            latency_ms=max(1, int((time.monotonic() - started) * 1000)),
            messaging_pub=str(hello["messaging_pub"]), name=str(hello.get("node_name") or ""),
            platform=str(hello.get("platform") or ""),
        )

    def forget(self, peer_id: str) -> None:
        self._probes.pop(peer_id, None)
