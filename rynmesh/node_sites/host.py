"""Hosting side: answer signed site requests from authorized peers.

Every request is checked in this order before anything reaches the site
backend: envelope signature and addressing, freshness and replay, directory
membership with the bound messaging key, the owner's switch and audience, a
per-caller rate limit, and the provider's live ``site.open`` authorization
(cached briefly, so a page load is not one remote round trip per file).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable

from ..drop import codec
from ..drop.codec import DropProtocolError
from ..drop.engine import DropError
from .protocol import (
    CAPABILITY,
    LIST_INFO,
    LIST_KIND,
    MAX_REQUEST_BODY,
    MAX_RESPONSE_BODY,
    METHODS,
    REQUEST_HEADERS,
    REQUEST_INFO,
    REQUEST_KIND,
    REQUEST_TIMEOUT_S,
    RESPONSE_HEADERS,
    RESPONSE_INFO,
    SiteError,
    b64,
    clean_headers,
    identity_headers,
    request_path,
    site_id,
    unb64,
)
from .store import SiteStore

AUTH_TTL_S = 60.0
NONCE_TTL_S = 900.0
MAX_NONCES = 8192
PER_CALLER_CONCURRENCY = 4
PER_CALLER_PER_MINUTE = 300

ForwardFn = Callable[[int, str, str, dict[str, str], bytes], tuple[int, list[list[str]], bytes]]
ForwardHandler = Callable[[str, str, dict[str, str], bytes], tuple[int, list[list[str]], bytes]]


def forward_loopback(port: int, method: str, path: str, headers: dict[str, str], body: bytes
                     ) -> tuple[int, list[list[str]], bytes]:
    """One request to the site backend on 127.0.0.1; no redirects followed, bounded body."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=REQUEST_TIMEOUT_S)
    try:
        conn.request(method, path, body=body if body or method in {"POST", "PUT", "PATCH"} else None,
                     headers={**headers, "Host": f"127.0.0.1:{port}", "Connection": "close"})
        response = conn.getresponse()
        data = response.read(MAX_RESPONSE_BODY + 1)
        if len(data) > MAX_RESPONSE_BODY:
            raise SiteError("site_response_too_large", 502)
        return response.status, [[k, v] for k, v in response.getheaders()], data
    except (OSError, http.client.HTTPException):
        raise SiteError("site_backend_unavailable", 502) from None
    finally:
        conn.close()


def _location(value: str, port: int) -> str | None:
    """Keep a redirect inside the site: relative, or this backend's own loopback origin."""
    for prefix in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        if value.startswith(prefix + "/"):
            value = value[len(prefix):]
            break
    try:
        return request_path(value)
    except SiteError:
        return None


class SiteHost:
    def __init__(self, engine: Any, store: SiteStore, *, forward: ForwardFn = forward_loopback,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.engine = engine
        self.store = store
        self.forward = forward
        self.clock = clock
        self._lock = threading.Lock()
        self._auth: dict[tuple[str, str, str], float] = {}
        self._nonces: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._active: dict[str, int] = {}
        self._recent: dict[str, deque[float]] = {}
        # In-process sites: site id -> fn(method, path, headers, body) -> (status, headers, body).
        self.builtins: dict[str, ForwardHandler] = {}

    # ---- shared checks ---------------------------------------------------------
    def _open(self, envelope: Any, kind: str, keys: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            payload = codec.open_envelope(envelope, kind=kind, me=self.engine.me)
        except DropProtocolError:
            raise SiteError("site_bad_request") from None
        if set(payload) != keys or not isinstance(payload.get("provider_resource"), str):
            raise SiteError("site_bad_request")
        sender = payload["from"]
        with self._lock:
            now = self.clock()
            while self._nonces and next(iter(self._nonces.values())) < now:
                self._nonces.popitem(last=False)
            marker = (sender, str(payload.get("nonce")))
            if marker in self._nonces:
                raise SiteError("site_replayed", 409)
            self._nonces[marker] = now + NONCE_TTL_S
            while len(self._nonces) > MAX_NONCES:
                self._nonces.popitem(last=False)
        try:
            member = self.engine.provider_member(payload["provider_resource"], sender)
        except DropError:
            raise SiteError("site_denied", 403) from None
        if payload.get("from_pub") != member.get("messaging_pub"):
            raise SiteError("site_denied", 403)
        return payload, member

    @staticmethod
    def _audience_allows(row: dict[str, Any], member: dict[str, Any]) -> bool:
        audience = row.get("audience") or {}
        if audience.get("mode") == "all":
            return True
        return audience.get("mode") == "members" and str(member.get("member_id") or "") in audience.get("members", [])

    def _authorized(self, resource: str, sender: str, sid: str) -> bool:
        key = (resource, sender, sid)
        with self._lock:
            if self._auth.get(key, 0.0) > self.clock():
                return True
        allowed = self.engine.provider_allows(resource, sender, self.engine.me, CAPABILITY, sid)
        with self._lock:
            if allowed:
                self._auth[key] = self.clock() + AUTH_TTL_S
            else:
                self._auth.pop(key, None)
        return allowed

    def revoke_cache(self) -> None:
        with self._lock:
            self._auth.clear()

    def _admit(self, sender: str) -> None:
        with self._lock:
            now = self.clock()
            recent = self._recent.setdefault(sender, deque())
            while recent and recent[0] <= now - 60.0:
                recent.popleft()
            if len(recent) >= PER_CALLER_PER_MINUTE or self._active.get(sender, 0) >= PER_CALLER_CONCURRENCY:
                raise SiteError("site_rate_limited", 429)
            recent.append(now)
            self._active[sender] = self._active.get(sender, 0) + 1

    def _release(self, sender: str) -> None:
        with self._lock:
            left = self._active.get(sender, 1) - 1
            if left > 0:
                self._active[sender] = left
            else:
                self._active.pop(sender, None)

    def _reply(self, payload: dict[str, Any], sealed: dict[str, str]) -> dict[str, Any]:
        return codec.sign_response({"request_nonce": payload["nonce"], "sealed": sealed}, me=self.engine.me,
                                   to=payload["from"], private_key_bytes=self.engine.node.private_key_bytes)

    # ---- peer protocol ---------------------------------------------------------
    def handle_list(self, envelope: Any) -> dict[str, Any]:
        payload, member = self._open(envelope, LIST_KIND, {
            "protocol", "type", "from", "to", "ts", "nonce", "provider_resource", "from_pub"})
        sites = [{"site_id": sid, "title": row["title"], "entry": row["entry"]}
                 for sid, row in sorted(self.store.sites().items())
                 if row.get("enabled") and (not row.get("builtin") or sid in self.builtins)
                 and self._audience_allows(row, member)]
        secret = json.dumps({"sites": sites}).encode("utf-8")
        return self._reply(payload, codec.seal_for(self.engine.msg_priv, payload["from_pub"], secret, LIST_INFO))

    def handle_request(self, envelope: Any) -> dict[str, Any]:
        payload, member = self._open(envelope, REQUEST_KIND, {
            "protocol", "type", "from", "to", "ts", "nonce", "provider_resource", "site_id", "from_pub", "sealed"})
        sender, resource = payload["from"], payload["provider_resource"]
        sid = site_id(payload.get("site_id"))
        row = self.store.site(sid)
        # Unknown, closed and not-for-you sites are indistinguishable to the caller.
        if row is None or not row.get("enabled") or not self._audience_allows(row, member):
            raise SiteError("site_denied", 403)
        self._admit(sender)
        try:
            if not self._authorized(resource, sender, sid):
                raise SiteError("site_denied", 403)
            try:
                request = json.loads(codec.open_from(self.engine.msg_priv, payload["from_pub"],
                                                     payload["sealed"], REQUEST_INFO))
            except (DropProtocolError, ValueError, TypeError):
                raise SiteError("site_bad_request") from None
            if not isinstance(request, dict) or set(request) != {"method", "path", "headers", "body"}:
                raise SiteError("site_bad_request")
            method = request["method"]
            if method not in METHODS:
                raise SiteError("site_bad_request")
            path = request_path(request["path"])
            body = unb64(request["body"], MAX_REQUEST_BODY)
            headers = {name: value for name, value in clean_headers(request["headers"], REQUEST_HEADERS)}
            headers.update(identity_headers(token=row["token"], caller=sender, member=member, resource=resource))
            if row.get("builtin"):
                handler = self.builtins.get(sid)
                if handler is None:
                    raise SiteError("site_denied", 403)
                status, raw_headers, data = handler(method, path, headers, body)
            else:
                status, raw_headers, data = self.forward(int(row["port"]), method, path, headers, body)
            out_headers: list[list[str]] = []
            kept: list[list[str]] = []
            for pair in raw_headers[:64]:
                try:
                    kept.extend(clean_headers([pair], RESPONSE_HEADERS))
                except SiteError:
                    continue  # a malformed backend header is dropped, not fatal
            for name, value in kept:
                if name == "location":
                    value = _location(value, int(row["port"])) or ""
                    if not value:
                        continue
                out_headers.append([name, value])
            if method == "HEAD":
                data = b""
            secret = json.dumps({"status": int(status), "headers": out_headers, "body": b64(data)}).encode("utf-8")
            return self._reply(payload, codec.seal_for(self.engine.msg_priv, payload["from_pub"], secret,
                                                       RESPONSE_INFO))
        finally:
            self._release(sender)
