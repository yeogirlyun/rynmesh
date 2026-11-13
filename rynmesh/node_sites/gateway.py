"""Loopback gateway that gives every opened remote site its own browser origin.

``http://<key>.site.localhost:<port>/`` resolves to 127.0.0.1 in Chromium and
WebView2. ``<key>`` is a random 128-bit value known only to this node's Ryn,
so it is both the origin (storage and cookies are per site) and the
capability: another local page cannot guess it, and any Host that is not an
exact registered key is refused, which also stops DNS rebinding. The gateway
is separate from the node control port, so a remote page can never reach
``/api/local``.
"""

from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .client import SiteClient
from .protocol import CONTENT_SECURITY_POLICY, MAX_REQUEST_BODY, METHODS, SiteError, key_from_host

log = logging.getLogger("rynmesh.node_sites.gateway")
MAX_CONCURRENT = 16

_ERROR_TEXT = {
    "site_offline": "This computer is offline.",
    "site_denied": "You no longer have access to this page.",
    "site_rate_limited": "Too many requests. Try again in a moment.",
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class SiteGateway:
    def __init__(self, client: SiteClient, *, port: int | None = None) -> None:
        self.client = client
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._requested_port = port

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def url(self, key: str, entry: str = "/") -> str:
        return f"http://{key}.site.localhost:{self.port}{entry}"

    def start(self) -> "SiteGateway":
        if self._server is not None:
            return self
        handler = self._handler()
        candidates = [p for p in (self._requested_port, _env_port(), self.client.store.gateway_port()) if p]
        for port in [*candidates, 0]:
            try:
                self._server = _Server(("127.0.0.1", port), handler)
                break
            except OSError:
                continue
        assert self._server is not None
        self.client.store.set_gateway_port(self.port)
        self._thread = threading.Thread(target=self._server.serve_forever, name="node-site-gateway", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RynSite/1"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # no URLs or hosts in logs
                return

            def _send(self, status: int, headers: list[list[str]], body: bytes, *, head: bool = False) -> None:
                self.send_response(status)
                seen = set()
                for name, value in headers:
                    if name in {"content-length", "connection", "transfer-encoding", "set-cookie"}:
                        continue
                    seen.add(name)
                    self.send_header(name, value)
                if "content-type" not in seen:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), serial=()")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if not head and body:
                    self.wfile.write(body)

            def _error(self, code: str, status: int) -> None:
                text = _ERROR_TEXT.get(code, "This page is unavailable.")
                body = (f"<!doctype html><meta charset=utf-8><title>{status}</title>"
                        f"<p data-ryn-site-error=\"{code}\">{text}</p>").encode("utf-8")
                self._send(status, [["content-type", "text/html; charset=utf-8"], ["cache-control", "no-store"]], body)

            def _handle(self) -> None:
                self.close_connection = True
                key = key_from_host(self.headers.get("Host", ""), gateway.port)
                if key is None or gateway.client.store.opened(key) is None:
                    self._error("site_not_found", 404)
                    return
                method = self.command
                if method not in METHODS:
                    self._error("site_bad_request", 405)
                    return
                if self.headers.get("Transfer-Encoding"):
                    self._error("site_bad_request", 411)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._error("site_bad_request", 400)
                    return
                if length < 0 or length > MAX_REQUEST_BODY:
                    self._error("site_too_large", 413)
                    return
                body = self.rfile.read(length) if length else b""
                if not gateway._slots.acquire(timeout=30):
                    self._error("site_rate_limited", 429)
                    return
                try:
                    headers = [[k.lower(), v] for k, v in self.headers.items()]
                    status, out_headers, data = gateway.client.fetch(key, method, self.path, headers, body)
                except SiteError as exc:
                    self._error(exc.code, exc.status)
                    return
                except Exception:
                    log.warning("node site request failed", exc_info=True)
                    self._error("site_unavailable", 502)
                    return
                finally:
                    gateway._slots.release()
                self._send(status, out_headers, data, head=method == "HEAD")

            def do_GET(self) -> None:
                self._handle()

            do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

        return Handler


def _env_port() -> int | None:
    try:
        value = int(os.environ.get("RYNMESH_SITE_GATEWAY_PORT", "") or 0)
    except ValueError:
        return None
    return value if 1024 <= value <= 65535 else None
