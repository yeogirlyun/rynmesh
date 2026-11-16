"""Pluggable, camouflaged transport for Rynmesh peer traffic.

Rationale (see docs/RYNMESH_TRANSPORT_CENSORSHIP.md): a censor like the GFW does
not block "HTTPS"; it blocks *identifiable* things — known SNIs, IPs, distinctive
handshakes, and unknown fully-encrypted protocols. The winning strategy is NOT a
bespoke protocol (that is *easier* to fingerprint) but to look like ordinary
HTTPS the censor cannot afford to block, and to keep the wire format swappable.

This module is that seam. Rynmesh's real "private protocol" stays at the
application layer (Ed25519-signed, content-addressed objects); the transport
below is deliberately boring and camouflaged.

Design:
- `Transport` — minimal interface the peer client needs (GET bytes; stream to
  file), so obfuscating transports can be added without touching call sites.
- `StdlibHttpsTransport` — the default, **zero new dependencies** (urllib + ssl).
  Supports camouflage headers, a TLS profile (min version + ALPN), an outbound
  proxy (HTTP CONNECT / SOCKS-via-env for a pluggable-transport or Tor bridge),
  a shared network-key auth header, and redirect suppression (SSRF-safe).
- `register_transport()` — plugin point for heavier transports later
  (CDN-WebSocket, XTLS-REALITY mimicry, QUIC masquerade, obfs4/Snowflake PT).

Profiles are selected with `RYNMESH_TRANSPORT` (default: `camouflage`).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import tempfile
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

# A realistic, current desktop-Chrome User-Agent. The UA travels *inside* TLS,
# so this is about not self-identifying as "Rynmesh" in logs / plaintext HTTP
# and blending with ordinary browser traffic, not about defeating passive DPI
# (which only sees the TLS ClientHello).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

NETWORK_KEY_HEADER = "X-Ryn-Auth"
SERVER_TIME_HEADER = "X-Ryn-Server-Time"


def _parse_server_time(raw: Any) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class TransportError(RuntimeError):
    """Transport-layer failure. ``reason`` lets callers map to domain errors.

    ``status`` carries the HTTP status code when the failure was an HTTP
    response, so a keyed peer's 404 (network-key mismatch) is distinguishable
    from network loss without leaking anything from the response body.
    """

    def __init__(self, message: str, *, reason: str = "http_error",
                 status: int | None = None, server_time: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status
        self.server_time = server_time



@dataclass(frozen=True)
class TransportProfile:
    """How outbound peer traffic should look on the wire."""

    name: str = "camouflage"
    headers: dict[str, str] = field(default_factory=dict)
    tls_min: str = "1.2"  # "1.2" | "1.3"
    alpn: tuple[str, ...] = ("h2", "http/1.1")
    verify_tls: bool = True
    proxies: dict[str, str] = field(default_factory=dict)  # {"https": "...", "http": "..."}
    # Advanced (CDN front / ECH): connect to one host but present another SNI.
    # The stdlib transport documents these as a plugin-only capability.
    sni: str = ""
    connect_host: str = ""
    requested_unknown: bool = False


def network_key() -> str:
    """Shared peer-network key (enables active-probe resistance when set)."""
    return os.environ.get("RYNMESH_NETWORK_KEY", "").strip()


def network_key_header(key: str | None = None) -> dict[str, str]:
    """Outbound auth header for peers running probe resistance.

    Returns ``{X-Ryn-Auth: <salted hash>}`` when a network key is configured,
    else ``{}``. Single source of truth for the header name + value derivation
    so every peer→peer caller authenticates identically.
    """
    resolved = network_key() if key is None else key.strip()
    if not resolved:
        return {}
    # Sent as a salted hash so the raw key is not echoed into peer logs.
    return {
        NETWORK_KEY_HEADER: hashlib.sha256(
            ("rynmesh-net-key:" + resolved).encode("utf-8")
        ).hexdigest()
    }


def _profile_headers(profile: TransportProfile) -> dict[str, str]:
    headers = dict(profile.headers)
    headers.update(network_key_header())
    return headers


def _system_proxies() -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in urllib.request.getproxies().items():
        key = str(k).strip().lower()
        val = str(v or "").strip()
        if key in {"http", "https"} and val:
            out[key] = val
    return out


def _resolve_proxies() -> dict[str, str]:
    settings_path = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh"))) / "settings.json"
    try:
        stored = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    proxy_cfg = stored.get("network_proxy") if isinstance(stored, dict) else None
    if isinstance(proxy_cfg, dict):
        mode = proxy_cfg.get("mode", "off")
        if mode == "system":
            return _system_proxies()
        if mode == "off":
            return {}
        if mode == "manual":
            url = str(proxy_cfg.get("url") or "").strip()
            return {"https": url, "http": url} if url else {}

    proxies: dict[str, str] = {}
    https_proxy = os.environ.get("RYNMESH_HTTPS_PROXY", "").strip()
    http_proxy = os.environ.get("RYNMESH_HTTP_PROXY", "").strip()
    if https_proxy:
        proxies["https"] = https_proxy
    if http_proxy:
        proxies["http"] = http_proxy
    if proxies:
        return proxies
    if os.environ.get("RYNMESH_USE_SYSTEM_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return _system_proxies()
    return {}


def resolve_profile() -> TransportProfile:
    """Build the active profile from node settings + RYNMESH_TRANSPORT + env overrides."""
    name = os.environ.get("RYNMESH_TRANSPORT", "camouflage").strip().lower() or "camouflage"
    proxies = _resolve_proxies()

    if name == "direct":
        # Today's plain behavior: identify as Rynmesh, default TLS.
        return TransportProfile(
            name="direct",
            headers={"User-Agent": "Rynmesh/0.1"},
            tls_min="1.2",
            alpn=(),
            proxies=proxies,
        )
    if name == "hardened":
        # Strongest blend: browser headers, TLS 1.3 only (pairs with ECH).
        return TransportProfile(
            name="hardened",
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.3",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    if name == "cdn-ws":
        # CDN-WebSocket: browser headers + TLS 1.2+ (broad CDN compatibility).
        return TransportProfile(
            name="cdn-ws",
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.2",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    if name == "reality":
        # REALITY: curl_cffi Chrome fingerprint + SNI/connect-host split.
        return TransportProfile(
            name="reality",
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.3",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    if name in ("meek", "ech"):
        # Meek / ECH: browser headers; TLS 1.2+ for broad compatibility.
        return TransportProfile(
            name=name,
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.2",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    # Default: camouflage — browser-like, broad TLS compatibility. A name that
    # matches a registered plugin transport is preserved so get_transport can
    # dispatch to it (with camouflage defaults as its base profile).
    known = name == "camouflage" or name in _REGISTRY
    return TransportProfile(
        name=name if name in _REGISTRY else "camouflage",
        headers=dict(_BROWSER_HEADERS),
        tls_min="1.2",
        alpn=("h2", "http/1.1"),
        proxies=proxies,
        sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
        connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        requested_unknown=not known,
    )


class Transport(Protocol):
    """The minimal bounded-I/O surface the peer client needs."""

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes: ...

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes: ...

    def iter_post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_chunk_bytes: int,
        max_total_bytes: int, headers: dict[str, str] | None = None,
    ) -> Iterator[bytes]: ...

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress redirects: a peer must not bounce us to an unvalidated host
    (SSRF) or to a censor-controlled endpoint."""

    def redirect_request(self, *args: Any, **kwargs: Any):  # noqa: D401
        return None


def _ssl_context(profile: TransportProfile) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = (
            ssl.TLSVersion.TLSv1_3 if profile.tls_min == "1.3" else ssl.TLSVersion.TLSv1_2
        )
    except (ValueError, AttributeError):  # pragma: no cover
        pass
    if profile.alpn:
        try:
            ctx.set_alpn_protocols(list(profile.alpn))
        except NotImplementedError:  # pragma: no cover
            pass
    if not profile.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class StdlibHttpsTransport:
    """Default transport: urllib + ssl, no third-party dependencies.

    Covers the high-value, low-risk camouflage levers (browser headers, TLS
    min/ALPN, outbound proxy, redirect suppression, network-key auth). When
    SNI/connect-host splitting is requested, `get_transport` selects
    `FrontedHttpsTransport` instead.
    """

    def __init__(self, profile: TransportProfile) -> None:
        self.profile = profile
        self._opener = self._build_opener(profile)

    def _build_opener(self, profile: TransportProfile) -> urllib.request.OpenerDirector:
        ctx = _ssl_context(profile)
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=ctx),
            _NoRedirect(),
            # Ignore ambient OS proxy env unless explicitly configured.
            urllib.request.ProxyHandler(profile.proxies or {}),
        ]
        return urllib.request.build_opener(*handlers)

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        merged = _profile_headers(self.profile)
        if extra:
            merged.update(extra)
        # Caller-supplied headers must not suppress mesh authentication.
        return stamp_network_key(merged)

    def _request(self, method: str, url: str, *, body: bytes | None, timeout_s: float,
                 max_bytes: int, headers: dict[str, str] | None) -> bytes:
        req = urllib.request.Request(url, data=body, method=method, headers=self._headers(headers))
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                _check_content_encoding(resp)
                return read_bounded(resp, max_bytes)
        except TransportError:
            raise
        except HTTPError as exc:
            if exc.code == 407:
                raise TransportError(
                    "proxy_auth_required", reason="proxy_auth_required", status=407
                ) from exc
            st = (
                _parse_server_time(exc.headers.get(SERVER_TIME_HEADER))
                if exc.code == 409 and exc.headers is not None
                else None
            )
            raise TransportError(f"http status {exc.code}", reason="http_error",
                                 status=exc.code, server_time=st) from exc
        except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            if "Tunnel connection failed: 407" in str(exc):
                raise TransportError(
                    "proxy_auth_required", reason="proxy_auth_required", status=407
                ) from exc
            raise TransportError("http request failed", reason="http_error") from None

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes:
        return self._request("GET", url, body=None, timeout_s=timeout_s,
                             max_bytes=max_bytes, headers=headers)

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._request("POST", url, body=body, timeout_s=timeout_s,
                             max_bytes=max_bytes, headers=headers)

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path:
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, method="GET", headers=self._headers(headers))
        try:
            with self._opener.open(req, timeout=timeout_s) as resp, dest.open("wb") as handle:
                _check_content_encoding(resp)
                total = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TransportError("media too large", reason="too_large")
                    handle.write(chunk)
        except TransportError:
            dest.unlink(missing_ok=True)
            raise
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError):
            dest.unlink(missing_ok=True)
            raise TransportError("http request failed", reason="http_error") from None
        return dest

    def iter_post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_chunk_bytes: int,
        max_total_bytes: int, headers: dict[str, str] | None = None,
    ) -> Iterator[bytes]:
        """Yield a bounded POST response without buffering it in memory.

        The response object is owned by the iterator and is closed even when a
        caller cancels iteration.  Error text is deliberately stable so a
        remote response body can never be copied into logs by exception
        formatting.
        """
        if max_chunk_bytes < 1 or max_total_bytes < 1:
            raise TransportError("invalid stream limit", reason="invalid_limit")
        req = urllib.request.Request(
            url, data=body, method="POST", headers=self._headers(headers),
        )
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                _check_content_encoding(resp)
                total = 0
                read_chunk = getattr(resp, "read1", resp.read)
                while True:
                    # HTTPResponse.read(n) may wait for n bytes or EOF, which
                    # turns a small NDJSON/SSE stream back into a buffered
                    # complete response. read1(n) returns currently available
                    # bytes while preserving the same upper bound.
                    chunk = read_chunk(max_chunk_bytes)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_total_bytes:
                        raise TransportError("stream response too large", reason="too_large")
                    yield chunk
        except TransportError:
            raise
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            raise TransportError("stream request failed", reason="http_error") from exc



def _proxy_for_scheme(proxies: dict[str, str], scheme: str) -> str:
    if not proxies:
        return ""
    if scheme in ("https", "wss"):
        return str(proxies.get("https") or "").strip()
    return str(proxies.get("http") or proxies.get("https") or "").strip()


def _connect_via_proxy(
    proxy_url: str, target_host: str, target_port: int, *, timeout_s: float
) -> Any:
    import base64
    import socket
    from urllib.parse import unquote

    parsed = urlparse(proxy_url)
    p_scheme = (parsed.scheme or "").lower()
    if p_scheme not in ("http", "https") or not parsed.hostname:
        raise TransportError(
            "outbound proxy URL must use http:// or https://", reason="config_error"
        )
    proxy_host = parsed.hostname
    proxy_port = parsed.port or (443 if p_scheme == "https" else 80)
    try:
        raw = socket.create_connection((proxy_host, proxy_port), timeout=timeout_s)
    except OSError:
        raise TransportError("http request failed", reason="http_error") from None

    try:
        if p_scheme == "https":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=proxy_host)
        authority = (
            f"[{target_host}]:{target_port}"
            if (":" in target_host and not target_host.startswith("["))
            else f"{target_host}:{target_port}"
        )
        lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        if parsed.username is not None or parsed.password is not None:
            creds = f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}".encode(
                "utf-8"
            )
            token = base64.b64encode(creds).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        raw.sendall("\r\n".join(lines).encode("latin-1") + b"\r\n\r\n")

        buf = bytearray()
        max_header_bytes = 64 * 1024
        while not buf.endswith(b"\r\n\r\n"):
            if len(buf) >= max_header_bytes:
                raise TransportError("proxy connect header too large", reason="http_error")
            ch = raw.recv(1)
            if not ch:
                raise TransportError("proxy closed connection during CONNECT", reason="http_error")
            buf.extend(ch)

        first_line = bytes(buf).split(b"\r\n", 1)[0]
        parts = first_line.split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if status == 407:
            raise TransportError(
                "proxy_auth_required", reason="proxy_auth_required", status=407
            )
        if status != 200:
            raise TransportError(
                f"proxy connect failed: status {status}",
                reason="http_error",
                status=status or None,
            )
        return raw
    except TransportError:
        raw.close()
        raise
    except (OSError, ValueError):
        raw.close()
        raise TransportError("http request failed", reason="http_error") from None


def _dial_target(
    connect_host: str,
    port: int,
    *,
    scheme: str,
    proxies: dict[str, str],
    timeout_s: float,
) -> Any:
    import socket

    proxy_url = _proxy_for_scheme(proxies, scheme)
    if proxy_url:
        return _connect_via_proxy(proxy_url, connect_host, port, timeout_s=timeout_s)
    try:
        return socket.create_connection((connect_host, port), timeout=timeout_s)
    except OSError:
        raise TransportError("http request failed", reason="http_error") from None


class FrontedHttpsTransport:
    """Transport with a real SNI / connect-host / Host split (stdlib only).

    Defeats the GFW's dominant TLS method — SNI filtering — by decoupling three
    things urllib conflates:

    - **connect_host**: the IP/host the TCP socket actually opens to (e.g. a CDN
      edge or an unblocked IP).
    - **sni** (`RYNMESH_TLS_SNI`): the server name presented in the *cleartext*
      TLS ClientHello — set this to a benign/allowed name the censor will not
      block. Certificate validation (when enabled) is done against this name.
    - **Host header**: the real backend the URL names, so a CDN/origin routes
      correctly.

    Pure `socket` + `ssl` + `http.client`; no third-party dependency. This is the
    practical substitute for Encrypted Client Hello (ECH), which Python's `ssl`
    cannot yet do (it needs OpenSSL 3.5+ ECH APIs not exposed by CPython).
    """

    def __init__(self, profile: TransportProfile) -> None:
        self.profile = profile
        self._ctx = _ssl_context(profile)

    def _headers(self, host_header: str, extra: dict[str, str] | None) -> dict[str, str]:
        merged = _profile_headers(self.profile)
        if extra:
            merged.update(extra)
        # These fields define the fronting split and connection lifecycle; a
        # generic caller header must not be able to redirect the inner request
        # to another virtual host or keep the one-shot socket alive.
        merged["Host"] = host_header  # real backend; routed by CDN/origin
        merged["Connection"] = "close"  # one socket per request
        merged = stamp_network_key(merged)
        return merged

    def _open(
        self, url: str, timeout_s: float, extra_headers: dict[str, str] | None,
        *, method: str = "GET", body: bytes | None = None,
    ):
        import http.client
        import socket

        parts = urlparse(url)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        connect_host = self.profile.connect_host or host
        sni = self.profile.sni or host
        is_default_port = port == (443 if scheme == "https" else 80)
        host_header = host if is_default_port else f"{host}:{port}"

        raw = _dial_target(
            connect_host,
            port,
            scheme=scheme,
            proxies=self.profile.proxies,
            timeout_s=timeout_s,
        )
        try:
            if scheme == "https":
                raw = self._ctx.wrap_socket(raw, server_hostname=sni)
            conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
            conn.sock = raw  # use our pre-dialed (and TLS-wrapped) socket
            request_headers = self._headers(host_header, extra_headers)
            if body is not None:
                request_headers["Content-Length"] = str(len(body))
            conn.request(method, path, body=body, headers=request_headers)
            resp = conn.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            try:
                raw.close()
            except OSError:
                pass
            raise TransportError("http request failed", reason="http_error") from None
        if (method == "POST" and not 200 <= resp.status < 300) or resp.status >= 400:
            st = _parse_server_time(resp.getheader(SERVER_TIME_HEADER)) if resp.status == 409 else None
            conn.close()
            raise TransportError(f"http status {resp.status}", reason="http_error", status=resp.status, server_time=st)
        try:
            _check_content_encoding(resp)
        except TransportError:
            conn.close()
            raise
        return conn, resp

    def _request(self, method: str, url: str, *, body: bytes | None, timeout_s: float,
                 max_bytes: int, headers: dict[str, str] | None) -> bytes:
        conn, resp = self._open(url, timeout_s, headers, method=method, body=body)
        try:
            return read_bounded(resp, max_bytes)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError("http request failed", reason="http_error") from None
        finally:
            conn.close()

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes:
        return self._request("GET", url, body=None, timeout_s=timeout_s,
                             max_bytes=max_bytes, headers=headers)

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._request("POST", url, body=body, timeout_s=timeout_s,
                             max_bytes=max_bytes, headers=headers)

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path:
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        conn, resp = self._open(url, timeout_s, headers)
        try:
            with dest.open("wb") as handle:
                total = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TransportError("media too large", reason="too_large")
                    handle.write(chunk)
        except TransportError:
            dest.unlink(missing_ok=True)
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            dest.unlink(missing_ok=True)
            raise TransportError("http request failed", reason="http_error") from None
        finally:
            conn.close()
        return dest

    def iter_post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_chunk_bytes: int,
        max_total_bytes: int, headers: dict[str, str] | None = None,
    ) -> Iterator[bytes]:
        """Yield a bounded response over the connect-host/SNI split socket."""
        import http.client

        if max_chunk_bytes < 1 or max_total_bytes < 1:
            raise TransportError("invalid stream limit", reason="invalid_limit")
        conn, resp = self._open(url, timeout_s, headers, method="POST", body=body)
        try:
            total = 0
            read_chunk = getattr(resp, "read1", resp.read)
            while True:
                chunk = read_chunk(max_chunk_bytes)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_total_bytes:
                    raise TransportError("stream response too large", reason="too_large")
                yield chunk
        except TransportError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError("stream request failed", reason="http_error") from exc
        finally:
            conn.close()



def get_pinned_transport(url: str, connect_ip: str) -> FrontedHttpsTransport:
    """Connect to one validated IP while preserving URL Host/SNI verification.

    Friend invitation endpoints are attacker-controlled. Resolving them before
    policy checks is insufficient if the socket later resolves the hostname a
    second time. This transport dials the already-validated address directly,
    while TLS certificate validation and HTTP routing remain bound to the URL
    hostname. Explicit outbound proxies are rejected because they own DNS and
    cannot provide this pinning guarantee through the current seam.
    """

    import ipaddress

    parts = urlparse(url)
    host = parts.hostname or ""
    if parts.scheme not in {"http", "https"} or not host:
        raise TransportError("pinned endpoint invalid", reason="endpoint_invalid")
    try:
        ipaddress.ip_address(connect_ip)
    except ValueError as exc:
        raise TransportError("pinned address invalid", reason="endpoint_invalid") from exc
    profile = resolve_profile()
    for p_url in profile.proxies.values():
        cleaned_p = str(p_url or "").strip()
        if not cleaned_p:
            continue
        parsed_p = urlparse(cleaned_p)
        if (parsed_p.scheme or "").lower() not in {"http", "https"} or not parsed_p.hostname:
            raise TransportError(
                "pinned transport only supports http(s) CONNECT proxies because other proxy protocols may re-resolve DNS instead of dialing the pinned IP",
                reason="pinned_proxy_unsupported",
            )
    return FrontedHttpsTransport(replace(profile, connect_host=connect_ip, sni=host))


class _BufferedSocket:
    """Socket wrapper preserving any frame bytes read past ``\\r\\n\\r\\n`` during handshake."""

    def __init__(self, sock: Any, initial: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(initial)

    def recv(self, n: int) -> bytes:
        if self._buf:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        return self._sock.recv(n)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


def _parse_ws_handshake_status(header_bytes: bytes | bytearray) -> str:
    first_line = bytes(header_bytes).split(b"\r\n", 1)[0]
    if first_line.startswith(b"HTTP/"):
        parts = first_line.split()
        if len(parts) >= 2 and len(parts[1]) == 3 and all(48 <= b <= 57 for b in parts[1]):
            return parts[1].decode("ascii")
    return "unknown"


class CdnWebSocketTransport:
    """Tunnel peer traffic over WebSocket to a CDN/relay endpoint.

    What the censor sees: a browser WebSocket upgrade to a major CDN edge
    (Cloudflare, Fastly, …). That edge is shared by millions of sites — blocking
    it causes enormous collateral damage, so it is one of the hardest edges to
    block. This is the "collateral damage" strategy in practice.

    What actually happens: each peer request is sent as a single
    binary WebSocket frame (the raw HTTP/1.1 request bytes); the server
    (rynmesh node behind the CDN) reads that frame and returns the response
    as a binary frame. The CDN just proxies WebSocket traffic — it never sees
    Rynmesh-specific content because all objects are already encrypted at the
    application layer.

    Server-side requirement: a Rynmesh node must be fronted by a CDN/reverse-proxy
    configured to pass WebSocket upgrades through (Cloudflare Flexible/Full, nginx
    `proxy_read_timeout`, etc.) — see docs/RYNMESH_TRANSPORT_CENSORSHIP.md §8.

    Zero third-party dependencies: the WebSocket upgrade and frame codec are
    implemented directly over `ssl.wrap_socket`. Only binary text frames are
    used (opcode 0x82) with a fresh random 4-byte mask per frame (required by
    the WebSocket spec for client→server frames).

    Config:
    - `RYNMESH_TRANSPORT=cdn-ws`
    - `RYNMESH_CDN_WS_URL`    base wss:// URL of the WebSocket relay endpoint
                               e.g. wss://ryn.cdn.example.com/ryn-ws
    - `RYNMESH_TLS_SNI`        override ClientHello SNI (optional; defaults to
                               the CDN hostname)
    - `RYNMESH_CONNECT_HOST`   override TCP connect host (optional; defaults to
                               the CDN hostname — use this to dial a specific
                               CDN edge IP)
    """

    _WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, profile: TransportProfile) -> None:
        self.profile = profile
        self._ctx = _ssl_context(profile)
        ws_url = os.environ.get("RYNMESH_CDN_WS_URL", "").strip()
        if not ws_url:
            raise TransportError(
                "RYNMESH_CDN_WS_URL is required for cdn-ws transport", reason="config_error"
            )
        p = urlparse(ws_url)
        if p.scheme not in ("ws", "wss"):
            raise TransportError(
                "RYNMESH_CDN_WS_URL must start with ws:// or wss://", reason="config_error"
            )
        self._ws_scheme = p.scheme
        self._ws_host = p.hostname or ""
        self._ws_port = p.port or (443 if p.scheme == "wss" else 80)
        self._ws_path = (p.path or "/ryn-ws") + (("?" + p.query) if p.query else "")

    def _ws_connect(self, timeout_s: float):
        """Open a fresh WebSocket connection, return the ssl-wrapped socket."""
        import socket

        connect_host = self.profile.connect_host or self._ws_host
        sni = self.profile.sni or self._ws_host
        raw = _dial_target(
            connect_host,
            self._ws_port,
            scheme=self._ws_scheme,
            proxies=self.profile.proxies,
            timeout_s=timeout_s,
        )
        if self._ws_scheme == "wss":
            raw = self._ctx.wrap_socket(raw, server_hostname=sni)

        # WebSocket opening handshake (RFC 6455 §4.1).
        import base64 as _b64
        import os as _os

        nonce = _b64.b64encode(_os.urandom(16)).decode("ascii")
        host_header = (
            self._ws_host
            if self._ws_port in (80, 443)
            else f"{self._ws_host}:{self._ws_port}"
        )
        headers = {
            **_profile_headers(self.profile),
            "Host": host_header,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": nonce,
            "Sec-WebSocket-Version": "13",
        }
        req_lines = [f"GET {self._ws_path} HTTP/1.1"]
        req_lines.extend(f"{k}: {v}" for k, v in headers.items())
        req_lines += ["", ""]
        raw.sendall("\r\n".join(req_lines).encode("utf-8"))

        # Read the 101 Switching Protocols response (bounded header block).
        buf = bytearray()
        max_handshake_bytes = 64 * 1024
        while b"\r\n\r\n" not in buf:
            if len(buf) >= max_handshake_bytes:
                raise TransportError(
                    f"ws handshake failed: status {_parse_ws_handshake_status(buf)}",
                    reason="http_error",
                )
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                if not buf:
                    raise TransportError("ws handshake: connection closed", reason="http_error")
                raise TransportError(
                    f"ws handshake failed: status {_parse_ws_handshake_status(buf)}",
                    reason="http_error",
                )
            buf.extend(chunk)
        sep = buf.index(b"\r\n\r\n")
        header_block = bytes(buf[:sep])
        leftover = bytes(buf[sep + 4 :])
        lines = header_block.split(b"\r\n")
        status_line = lines[0] if lines else b""
        if not (status_line == b"HTTP/1.1 101" or status_line.startswith(b"HTTP/1.1 101 ")):
            raise TransportError(
                f"ws handshake failed: status {_parse_ws_handshake_status(header_block)}",
                reason="http_error",
            )
        expected_accept = _b64.b64encode(
            hashlib.sha1(nonce.encode("ascii") + self._WS_MAGIC).digest()
        ).decode("ascii")
        accept_values: list[str] = []
        for line in lines[1:]:
            name, colon, value = line.partition(b":")
            if colon and name.strip().lower() == b"sec-websocket-accept":
                accept_values.append(value.strip().decode("latin-1"))
        if accept_values != [expected_accept]:
            raise TransportError(
                "ws handshake failed: invalid Sec-WebSocket-Accept",
                reason="http_error",
            )
        return _BufferedSocket(raw, leftover) if leftover else raw

    @staticmethod
    def _ws_send_frame(sock: Any, payload: bytes, *, opcode: int = 0x02) -> None:
        """Send a masked WebSocket frame (client→server MUST be masked)."""
        import os as _os

        mask = _os.urandom(4)
        n = len(payload)
        masked = (
            b""
            if n == 0
            else (
                int.from_bytes(payload, "big")
                ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")
            ).to_bytes(n, "big")
        )
        first_byte = 0x80 | (opcode & 0x0F)
        if n < 126:
            header = bytes([first_byte, 0x80 | n]) + mask
        elif n < 65536:
            header = bytes([first_byte, 0xFE]) + n.to_bytes(2, "big") + mask
        else:
            header = bytes([first_byte, 0xFF]) + n.to_bytes(8, "big") + mask
        sock.sendall(header + masked)

    @staticmethod
    def _ws_iter_message(sock: Any, max_bytes: int) -> Iterator[bytes]:
        """Yield payload chunks of one complete WebSocket data message."""

        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise TransportError("ws: connection closed mid-frame", reason="http_error")
                buf.extend(chunk)
            return bytes(buf)

        started = False
        total_bytes = 0
        while True:
            header = recv_exact(2)
            fin = bool(header[0] & 0x80)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F
            if length == 126:
                length = int.from_bytes(recv_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(recv_exact(8), "big")
            mask_key = recv_exact(4) if masked else b""

            if opcode >= 0x08:
                if opcode == 0x08:
                    raise TransportError("ws: server sent close frame", reason="http_error")
                if not fin or length > 125:
                    raise TransportError("ws: invalid control frame", reason="http_error")
                ctrl_payload = recv_exact(length)
                if mask_key and ctrl_payload:
                    cn = len(ctrl_payload)
                    ctrl_payload = (
                        int.from_bytes(ctrl_payload, "big")
                        ^ int.from_bytes((mask_key * (cn // 4 + 1))[:cn], "big")
                    ).to_bytes(cn, "big")
                if opcode == 0x09:
                    CdnWebSocketTransport._ws_send_frame(sock, ctrl_payload, opcode=0x0A)
                    continue
                if opcode == 0x0A:
                    continue
                raise TransportError("ws: unsupported opcode", reason="http_error")

            if not started:
                if opcode not in (0x01, 0x02):
                    raise TransportError("ws: unexpected continuation frame", reason="http_error")
                started = True
            elif opcode != 0x00:
                raise TransportError("ws: interleaved data frame", reason="http_error")

            if total_bytes + length > max_bytes:
                raise TransportError("ws: frame too large", reason="too_large")
            total_bytes += length

            remaining = length
            offset = 0
            while remaining > 0:
                step = min(64 * 1024, remaining)
                chunk = recv_exact(step)
                if mask_key:
                    cn = len(chunk)
                    rotated = bytes(mask_key[(offset + i) % 4] for i in range(4))
                    chunk = (
                        int.from_bytes(chunk, "big")
                        ^ int.from_bytes((rotated * (cn // 4 + 1))[:cn], "big")
                    ).to_bytes(cn, "big")
                remaining -= len(chunk)
                offset += len(chunk)
                yield chunk
            if fin:
                return

    @staticmethod
    def _ws_recv_frame(sock: Any, max_bytes: int) -> bytes:
        """Receive one WebSocket message (reassembling fragments and answering pings)."""
        return b"".join(CdnWebSocketTransport._ws_iter_message(sock, max_bytes))

    def _build_tunneled_request(
        self,
        url: str,
        extra_headers: dict[str, str] | None,
        *,
        method: str = "GET",
        body: bytes | None = None,
    ) -> bytes:
        parts = urlparse(url)
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        default_port = port in (80, 443)
        host_header = host if default_port else f"{host}:{port}"

        req_headers = dict(_profile_headers(self.profile))
        if extra_headers:
            req_headers.update(extra_headers)
        req_headers = stamp_network_key(req_headers)
        req_headers["Host"] = host_header
        if body is not None:
            req_headers["Content-Length"] = str(len(body))
        req_lines = [f"{method} {path} HTTP/1.1"]
        req_lines.extend(f"{k}: {v}" for k, v in req_headers.items())
        req_lines += ["", ""]
        return "\r\n".join(req_lines).encode("utf-8") + (body or b"")

    @staticmethod
    def _validate_tunneled_response_headers(header_block: bytes) -> None:
        lines = header_block.split(b"\r\n")
        status_parts = lines[0].split() if lines else []
        try:
            status = int(status_parts[1])
        except (IndexError, ValueError) as exc:
            raise TransportError("ws: malformed HTTP status", reason="http_error") from exc
        if not 200 <= status < 300:
            st = None
            if status == 409:
                for line in lines[1:]:
                    name, colon, value = line.partition(b":")
                    if colon and name.strip().lower() == b"x-ryn-server-time":
                        st = _parse_server_time(value.decode("latin-1"))
                        break
            raise TransportError(f"http status {status}", reason="http_error", status=status, server_time=st)
        encodings: list[str] = []
        for line in lines[1:]:
            name, colon, value = line.partition(b":")
            if colon and name.strip().lower() == b"content-encoding":
                encodings.append(value.decode("latin-1"))
        _check_encoding_values(encodings)

    def _do_request(
        self, url: str, timeout_s: float, max_bytes: int,
        extra_headers: dict[str, str] | None, *, method: str = "GET",
        body: bytes | None = None,
    ) -> bytes:
        """Tunnel one bounded HTTP request through a WebSocket connection."""
        raw_request = self._build_tunneled_request(
            url, extra_headers, method=method, body=body,
        )

        sock = self._ws_connect(timeout_s)
        try:
            self._ws_send_frame(sock, raw_request)
            # Bound tunneled HTTP headers separately from the response body.
            raw_response = self._ws_recv_frame(sock, max_bytes + 64 * 1024)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        # Parse the HTTP response out of the frame.
        sep = raw_response.find(b"\r\n\r\n")
        if sep == -1:
            raise TransportError("ws: malformed HTTP response frame", reason="http_error")
        self._validate_tunneled_response_headers(raw_response[:sep])
        response_body = raw_response[sep + 4:]
        if len(response_body) > max_bytes:
            raise TransportError("response too large", reason="too_large")
        return response_body

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes:
        try:
            return self._do_request(url, timeout_s, max_bytes, headers)
        except TransportError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError(f"cdn-ws error: {exc}", reason="http_error") from exc

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        try:
            return self._do_request(
                url, timeout_s, max_bytes, headers, method="POST", body=body,
            )
        except TransportError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError(f"cdn-ws error: {exc}", reason="http_error") from exc

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path:
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw_request = self._build_tunneled_request(url, headers, method="GET", body=None)
        tmp_path: Path | None = None
        handle: Any | None = None
        sock: Any | None = None
        try:
            sock = self._ws_connect(timeout_s)
            self._ws_send_frame(sock, raw_request)
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(dest.parent),
                prefix=f".{dest.name}.",
                suffix=".part",
            )
            tmp_path = Path(tmp_name)
            handle = os.fdopen(tmp_fd, "wb")
            header_buf = bytearray()
            headers_parsed = False
            written = 0
            for chunk in self._ws_iter_message(sock, max_bytes + 64 * 1024):
                if not headers_parsed:
                    header_buf.extend(chunk)
                    sep = header_buf.find(b"\r\n\r\n")
                    if sep == -1:
                        if len(header_buf) > 64 * 1024:
                            raise TransportError(
                                "ws: malformed HTTP response frame", reason="http_error"
                            )
                        continue
                    self._validate_tunneled_response_headers(bytes(header_buf[:sep]))
                    headers_parsed = True
                    body_chunk = bytes(header_buf[sep + 4 :])
                    header_buf.clear()
                else:
                    body_chunk = chunk
                if body_chunk:
                    written += len(body_chunk)
                    if written > max_bytes:
                        raise TransportError("media too large", reason="too_large")
                    handle.write(body_chunk)
            if not headers_parsed:
                raise TransportError("ws: malformed HTTP response frame", reason="http_error")
            handle.flush()
            handle.close()
            handle = None
            os.replace(tmp_path, dest)
            tmp_path = None
            return dest
        except TransportError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError(f"cdn-ws error: {exc}", reason="http_error") from exc
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


# --- transport registry (plugin point) -----------------------------------

TransportFactory = Callable[[TransportProfile], Transport]
_REGISTRY: dict[str, TransportFactory] = {}
_CACHE: dict[str, Transport] = {}
_LAST_EFFECTIVE: dict[str, Any] | None = None
_LAST_PROFILE: TransportProfile | None = None


def register_transport(name: str, factory: TransportFactory) -> None:
    """Register an obfuscating transport (e.g. 'reality', 'cdn-ws', 'pt')."""
    _REGISTRY[name.lower()] = factory


# Built-in plugin registrations -------------------------------------------
register_transport("cdn-ws", lambda profile: CdnWebSocketTransport(profile))


def _record_effective(
    profile: TransportProfile,
    *,
    effective: str | None,
    degraded_reason: str | None,
) -> None:
    global _LAST_EFFECTIVE, _LAST_PROFILE
    _LAST_PROFILE = profile
    _LAST_EFFECTIVE = {
        "requested": profile.name,
        "effective": effective,
        "requested_unknown": profile.requested_unknown,
        "degraded_reason": degraded_reason,
        "via_proxy": bool(profile.proxies),
    }


def _inspect_effective(profile: TransportProfile, transport: Transport) -> None:
    if getattr(transport, "_ech_active", None) is False:
        if not hasattr(ssl.SSLContext, "set_ech_config_list"):
            reason = "ech_unavailable"
        elif not os.environ.get("RYNMESH_ECH_CONFIGS", "").strip():
            reason = "ech_configs_missing"
        else:
            reason = "ech_config_invalid"
        _record_effective(profile, effective="sni_fronting", degraded_reason=reason)
        return
    if profile.name in _REGISTRY:
        eff = profile.name
    elif isinstance(transport, FrontedHttpsTransport):
        eff = "fronted_https"
    else:
        eff = "stdlib_https"
    _record_effective(
        profile,
        effective=eff,
        degraded_reason="unknown_transport" if profile.requested_unknown else None,
    )


def last_effective() -> dict[str, Any] | None:
    """Return a copy of the last effective transport record, if any."""
    return dict(_LAST_EFFECTIVE) if _LAST_EFFECTIVE is not None else None


def transport_status() -> dict[str, Any]:
    """Resolve and return the effective transport status for node status reporting."""
    current = resolve_profile()
    if _LAST_EFFECTIVE is None or _LAST_PROFILE != current:
        try:
            get_transport(current)
        except TransportError:
            pass
    if _LAST_EFFECTIVE is not None:
        return dict(_LAST_EFFECTIVE)
    return {
        "requested": current.name,
        "effective": None,
        "requested_unknown": current.requested_unknown,
        "degraded_reason": "unknown_transport" if current.requested_unknown else None,
        "via_proxy": bool(current.proxies),
    }


def get_transport(profile: TransportProfile | None = None) -> Transport:
    """Return the active transport for the resolved/given profile (cached)."""
    active = profile or resolve_profile()
    cache_key = "|".join(
        [
            active.name,
            active.tls_min,
            ",".join(active.alpn),
            str(active.verify_tls),
            ";".join(f"{k}={v}" for k, v in sorted(active.proxies.items())),
            active.sni,
            active.connect_host,
            str(active.requested_unknown),
        ]
    )
    cached = _CACHE.get(cache_key)
    if cached is not None:
        _inspect_effective(active, cached)
        return cached
    factory = _REGISTRY.get(active.name)
    if factory:
        try:
            transport: Transport = factory(active)
        except TransportError as exc:
            _record_effective(
                active,
                effective=None,
                degraded_reason=exc.reason or "config_error",
            )
            raise
    elif active.sni or active.connect_host:
        # Real SNI/connect/Host split (defeats SNI filtering, substitutes for ECH).
        transport = FrontedHttpsTransport(active)
    else:
        transport = StdlibHttpsTransport(active)
    _CACHE[cache_key] = transport
    _inspect_effective(active, transport)
    return transport


def reset_transport_cache() -> None:
    """Drop cached transports (tests after env changes)."""
    global _LAST_EFFECTIVE, _LAST_PROFILE
    _CACHE.clear()
    _LAST_EFFECTIVE = None
    _LAST_PROFILE = None

def stamp_network_key(headers: dict[str, str]) -> dict[str, str]:
    """Return ``headers`` with the mesh credential enforced, case-insensitively.

    HTTP header names are case-insensitive; a plain ``dict.update`` with the
    canonical casing would let a caller-supplied ``x-ryn-auth`` ride along as a
    duplicate line, and whichever the receiving stack reads first would win.
    Every caller-supplied spelling is stripped before the real value is set.
    """
    cleaned = {k: v for k, v in headers.items() if k.lower() != NETWORK_KEY_HEADER.lower()}
    cleaned.update(network_key_header())
    return cleaned


def _check_encoding_values(values: list[str]) -> None:
    tokens = [
        tok.strip().lower()
        for raw in values
        for tok in str(raw).split(",")
        if tok.strip()
    ]
    if any(tok != "identity" for tok in tokens):
        raise TransportError("unsupported content encoding", reason="unsupported_encoding")


def _check_content_encoding(resp: Any) -> None:
    headers = getattr(resp, "headers", None)
    if headers is not None and hasattr(headers, "get_all"):
        values = [str(v) for v in (headers.get_all("Content-Encoding") or [])]
    elif headers is not None and hasattr(headers, "get"):
        val = headers.get("Content-Encoding") or headers.get("content-encoding")
        values = [str(val)] if val else []
    elif hasattr(resp, "getheader"):
        val = resp.getheader("Content-Encoding")
        values = [str(val)] if val else []
    else:
        values = []
    _check_encoding_values(values)


def read_bounded(resp, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from a response, refusing anything larger."""
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TransportError("response too large", reason="too_large")
    return data


def check_status(status: int, *, strict: bool) -> None:
    """Reject failure responses.

    POST paths are ``strict`` (2xx only — a redirect of a signed task is never
    right). GET/download keep the pre-existing ``>= 400`` contract so peers
    behind redirecting CDN edges do not flip from readable to hard-failed.
    """
    if (strict and not 200 <= status < 300) or (not strict and status >= 400):
        raise TransportError(f"http status {status}", reason="http_error", status=status)

