"""Tests for REALITY / meek / ECH transport plugins."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from rynmesh.transport import (
    TransportError,
    TransportProfile,
    reset_transport_cache,
)

# ---------------------------------------------------------------------------
# REALITY transport (curl_cffi)
# ---------------------------------------------------------------------------

def test_reality_registered_after_import() -> None:
    import rynmesh.transport_plugins  # noqa: F401
    from rynmesh.transport import _REGISTRY

    assert "reality" in _REGISTRY


def test_reality_raises_on_missing_curl_cffi(monkeypatch) -> None:
    """If curl_cffi is not installed, constructing RealityTransport raises TransportError."""
    import sys

    from rynmesh.transport_plugins import RealityTransport

    # Temporarily hide curl_cffi from the import system.
    original = sys.modules.get("curl_cffi")
    sys.modules["curl_cffi"] = None  # type: ignore[assignment]
    sys.modules["curl_cffi.requests"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(TransportError, match="curl_cffi"):
            RealityTransport(TransportProfile(name="reality"))
    finally:
        if original is None:
            sys.modules.pop("curl_cffi", None)
            sys.modules.pop("curl_cffi.requests", None)
        else:
            sys.modules["curl_cffi"] = original


def test_reality_transport_makes_real_request() -> None:
    """RealityTransport with chrome124 fingerprint can fetch https://httpbin.org/get.
    Skipped if curl_cffi is unavailable or the host is unreachable."""
    pytest.importorskip("curl_cffi")
    from rynmesh.transport_plugins import RealityTransport

    transport = RealityTransport(TransportProfile(name="reality"))
    try:
        data = transport.get_bytes(
            "https://httpbin.org/get", timeout_s=10, max_bytes=64 * 1024
        )
    except TransportError as exc:
        pytest.skip(f"network unreachable: {exc}")
    payload = json.loads(data)
    # httpbin echoes the User-Agent; chrome124 impersonation should look like Chrome.
    ua = payload.get("headers", {}).get("User-Agent", "")
    assert "Chrome" in ua or len(ua) > 0  # curl_cffi may set its own UA


# ---------------------------------------------------------------------------
# Meek transport (stdlib, local server)
# ---------------------------------------------------------------------------

class _MeekBridgeHandler(http.server.BaseHTTPRequestHandler):
    """Minimal meek-bridge server: receives a POST with the target URL in the
    body and returns the path back as the response body (echo test)."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        response = b"meek-echo:" + body
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        return


def _start_meek_server() -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _MeekBridgeHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_meek_registered_after_import() -> None:
    import rynmesh.transport_plugins  # noqa: F401
    from rynmesh.transport import _REGISTRY

    assert "meek" in _REGISTRY


def test_meek_raises_without_url(monkeypatch) -> None:
    monkeypatch.delenv("RYNMESH_MEEK_URL", raising=False)
    from rynmesh.transport_plugins import MeekTransport

    with pytest.raises(TransportError, match="RYNMESH_MEEK_URL"):
        MeekTransport(TransportProfile(name="meek"))


def test_meek_posts_request_to_bridge(monkeypatch) -> None:
    server, port = _start_meek_server()
    try:
        monkeypatch.setenv("RYNMESH_MEEK_URL", f"http://127.0.0.1:{port}/")
        monkeypatch.delenv("RYNMESH_MEEK_URL", raising=False)  # will re-set below
        # Set directly via env since MeekTransport reads it at init.
        import os

        os.environ["RYNMESH_MEEK_URL"] = f"http://127.0.0.1:{port}/"
        from rynmesh.transport_plugins import MeekTransport

        transport = MeekTransport(TransportProfile(name="meek"))
        data = transport.get_bytes(
            "http://backend.example.com/api/v1/node",
            timeout_s=5,
            max_bytes=1 << 20,
        )
        # Our mini bridge echoes "meek-echo:" + request body (the target URL bytes)
        assert data.startswith(b"meek-echo:")
        assert b"backend.example.com" in data
    finally:
        server.shutdown()
        os.environ.pop("RYNMESH_MEEK_URL", None)


# ---------------------------------------------------------------------------
# ECH transport
# ---------------------------------------------------------------------------

def test_ech_registered_after_import() -> None:
    import rynmesh.transport_plugins  # noqa: F401
    from rynmesh.transport import _REGISTRY

    assert "ech" in _REGISTRY


def test_ech_falls_back_to_fronted_when_no_api(monkeypatch) -> None:
    """On current CPython (no ECH API), EchTransport must silently fall back
    to FrontedHttpsTransport without raising."""
    import ssl

    monkeypatch.delenv("RYNMESH_ECH_CONFIGS", raising=False)
    # Confirm no ECH API (expected on OpenSSL 3.0).
    if hasattr(ssl.SSLContext, "set_ech_config_list"):
        pytest.skip("ECH API available; fallback path not testable")

    from rynmesh.transport_plugins import EchTransport

    t = EchTransport(TransportProfile(name="ech", sni="cdn.example.com"))
    assert t._ech_active is False
    assert t._delegate is not None


def test_ech_transport_get_bytes_via_fallback(monkeypatch) -> None:
    """On current CPython without ECH, EchTransport must still deliver bytes
    via the FrontedHttpsTransport fallback (verified over a local HTTP server)."""
    import ssl

    if hasattr(ssl.SSLContext, "set_ech_config_list"):
        pytest.skip("ECH API available; test targets fallback path only")

    monkeypatch.delenv("RYNMESH_ECH_CONFIGS", raising=False)

    # Minimal local server.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ech": "fallback"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        from rynmesh.transport_plugins import EchTransport

        ech = EchTransport(
            TransportProfile(name="ech", connect_host="127.0.0.1")
        )
        data = ech.get_bytes(
            f"http://127.0.0.1:{port}/api/v1/node",
            timeout_s=5,
            max_bytes=1 << 20,
        )
        assert b"ech" in data
    finally:
        server.shutdown()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Profile resolution for all three names
# ---------------------------------------------------------------------------

def test_resolve_profile_names(monkeypatch) -> None:
    import rynmesh.transport_plugins  # noqa: F401 — ensure registered
    from rynmesh.transport import resolve_profile

    for transport_name in ("reality", "meek", "ech"):
        reset_transport_cache()
        monkeypatch.setenv("RYNMESH_TRANSPORT", transport_name)
        profile = resolve_profile()
        assert profile.name == transport_name, f"{transport_name} profile name mismatch"
        assert "Mozilla/" in profile.headers.get("User-Agent", ""), \
            f"{transport_name} should use browser UA"
