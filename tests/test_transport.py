"""Tests for the pluggable transport seam + active-probe resistance."""

from __future__ import annotations

import hashlib

import pytest

from rynmesh.transport import (
    NETWORK_KEY_HEADER,
    StdlibHttpsTransport,
    TransportProfile,
    get_transport,
    register_transport,
    reset_transport_cache,
    resolve_profile,
)


def test_default_profile_is_camouflage_with_browser_ua(monkeypatch) -> None:
    monkeypatch.delenv("RYNMESH_TRANSPORT", raising=False)
    profile = resolve_profile()
    assert profile.name == "camouflage"
    assert "Mozilla/" in profile.headers["User-Agent"]
    assert "h2" in profile.alpn  # browser-like ALPN


def test_direct_profile_keeps_legacy_identity(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_TRANSPORT", "direct")
    profile = resolve_profile()
    assert profile.name == "direct"
    assert profile.headers["User-Agent"] == "Rynmesh/0.1"


def test_hardened_profile_requires_tls13(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_TRANSPORT", "hardened")
    profile = resolve_profile()
    assert profile.name == "hardened"
    assert profile.tls_min == "1.3"


def test_proxy_env_routes_through_proxy(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_HTTPS_PROXY", "http://127.0.0.1:9999")
    profile = resolve_profile()
    assert profile.proxies.get("https") == "http://127.0.0.1:9999"


def test_network_key_is_sent_as_salted_hash(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "swordfish")
    transport = StdlibHttpsTransport(TransportProfile())
    headers = transport._headers(None)
    expected = hashlib.sha256(b"rynmesh-net-key:swordfish").hexdigest()
    assert headers[NETWORK_KEY_HEADER] == expected
    assert "swordfish" not in headers[NETWORK_KEY_HEADER]  # raw key never sent


def test_transport_plugin_registry(monkeypatch) -> None:
    reset_transport_cache()
    sentinel = object()
    monkeypatch.setenv("RYNMESH_TRANSPORT", "fakeobfs")
    register_transport("fakeobfs", lambda profile: sentinel)  # type: ignore[arg-type,return-value]
    try:
        assert get_transport() is sentinel
    finally:
        reset_transport_cache()


def test_active_probe_resistance_blocks_anonymous_peer_requests(monkeypatch) -> None:
    """With a network key set, an unauthenticated probe gets a generic 404 on
    the peer surface (no Rynmesh banner); a correctly-keyed request gets 200."""
    pytest.importorskip("fastapi")
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "swordfish")

    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    node = RynmeshStore(home=d / "node", network_dir=d / "mesh", node_name="probe")
    client = TestClient(create_app(node))

    # Anonymous probe: peer surface + health look like an empty server.
    assert client.get("/health").status_code == 404
    assert client.get("/api/v1/node").status_code == 404

    # Correctly-keyed peer request succeeds.
    auth = hashlib.sha256(b"rynmesh-net-key:swordfish").hexdigest()
    assert client.get("/health", headers={"X-Ryn-Auth": auth}).status_code == 200
    assert client.get("/api/v1/node", headers={"X-Ryn-Auth": auth}).status_code == 200


def test_active_probe_resistance_guards_registry_and_relay(monkeypatch, tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.registry import FilePeerRegistry
    from rynmesh.registry_http import create_app
    from rynmesh.relay import FileRelayStore

    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "registry-secret")
    client = TestClient(create_app(
        FilePeerRegistry(tmp_path / "registry"),
        relay_store=FileRelayStore(tmp_path / "relay"),
    ))
    auth = hashlib.sha256(b"rynmesh-net-key:registry-secret").hexdigest()

    assert client.get("/health").status_code == 404
    assert client.get("/api/v1/jobs/capacity").status_code == 404
    assert client.post("/api/v1/relay/blobs", content=b"probe").status_code == 404
    assert client.get("/health", headers={"X-Ryn-Auth": auth}).status_code == 200
    assert client.get(
        "/api/v1/jobs/capacity", headers={"X-Ryn-Auth": auth}
    ).status_code == 200


def test_get_transport_selects_fronted_when_sni_or_connect_set(monkeypatch) -> None:
    from rynmesh.transport import FrontedHttpsTransport, StdlibHttpsTransport

    reset_transport_cache()
    monkeypatch.delenv("RYNMESH_TRANSPORT", raising=False)
    monkeypatch.delenv("RYNMESH_TLS_SNI", raising=False)
    monkeypatch.delenv("RYNMESH_CONNECT_HOST", raising=False)
    assert isinstance(get_transport(), StdlibHttpsTransport)

    reset_transport_cache()
    monkeypatch.setenv("RYNMESH_TLS_SNI", "cdn.example.com")
    assert isinstance(get_transport(), FrontedHttpsTransport)
    reset_transport_cache()


def test_fronted_transport_splits_connect_host_and_host_header() -> None:
    """Over plain HTTP, the fronted transport must dial connect_host while
    sending the original Host header (the connect/Host split mechanism that, on
    TLS, also carries an overridden SNI)."""
    import http.server
    import threading

    from rynmesh.transport import FrontedHttpsTransport, TransportProfile

    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["host"] = self.headers.get("Host", "")
            seen["ua"] = self.headers.get("User-Agent", "")
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # connect_host = 127.0.0.1 (where the server really is); the URL names a
        # different backend host that becomes the Host header.
        profile = TransportProfile(
            name="fronted",
            headers={"User-Agent": "Mozilla/5.0 test"},
            connect_host="127.0.0.1",
        )
        transport = FrontedHttpsTransport(profile)
        url = f"http://backend.internal:{port}/api/v1/node"
        data = transport.get_bytes(url, timeout_s=5, max_bytes=1 << 20)
        assert data == b'{"ok": true}'
        assert seen["host"] == f"backend.internal:{port}"  # Host = backend, not connect_host
        assert seen["ua"] == "Mozilla/5.0 test"
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_cdn_ws_profile_is_returned_for_cdn_ws_name(monkeypatch) -> None:
    reset_transport_cache()
    monkeypatch.setenv("RYNMESH_TRANSPORT", "cdn-ws")
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", "wss://cdn.example.com/ryn-ws")
    profile = resolve_profile()
    assert profile.name == "cdn-ws"
    assert "Mozilla/" in profile.headers["User-Agent"]


def test_cdn_ws_transport_tunnels_request_over_websocket(monkeypatch) -> None:
    """End-to-end: CdnWebSocketTransport opens a WebSocket, sends the HTTP
    request as a binary frame, and returns the body from the response frame."""
    import base64
    import hashlib
    import http.server
    import threading

    from rynmesh.transport import CdnWebSocketTransport, TransportProfile

    class MinimalWsHandler(http.server.BaseHTTPRequestHandler):
        """Implement just enough of RFC 6455 to accept an upgrade, receive one
        binary request frame, and send back a binary response frame."""

        def do_GET(self):  # noqa: N802
            # WebSocket upgrade
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()

            # Receive one masked binary frame (client→server)
            def recv_frame():
                raw = self.rfile
                hdr = raw.read(2)
                masked = bool(hdr[1] & 0x80)
                length = hdr[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(raw.read(2), "big")
                mask_bytes = raw.read(4) if masked else b"\x00\x00\x00\x00"
                payload = bytearray(raw.read(length))
                return bytes(b ^ mask_bytes[i % 4] for i, b in enumerate(payload))

            _request = recv_frame()  # discard; just need to drain it

            # Send one binary frame back (server→client, never masked)
            response_body = b'{"data": "hello from ws"}'
            http_response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
                b"\r\n" + response_body
            )
            payload = http_response
            length = len(payload)
            if length < 126:
                frame_header = bytes([0x82, length])
            elif length < 65536:
                frame_header = bytes([0x82, 126]) + length.to_bytes(2, "big")
            else:
                frame_header = bytes([0x82, 127]) + length.to_bytes(8, "big")
            self.wfile.write(frame_header + payload)
            self.wfile.flush()

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), MinimalWsHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port}/ryn-ws")
        profile = TransportProfile(name="cdn-ws", headers={"User-Agent": "Mozilla/5.0 test"})
        transport = CdnWebSocketTransport(profile)
        data = transport.get_bytes(
            f"http://backend.internal:{port}/api/v1/node",
            timeout_s=5,
            max_bytes=1 << 20,
        )
        assert b"hello from ws" in data
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_no_network_key_keeps_peer_surface_open(monkeypatch) -> None:
    """Default (no key): peer surface stays open for plain P2P / dev."""
    pytest.importorskip("fastapi")
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.delenv("RYNMESH_NETWORK_KEY", raising=False)
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    node = RynmeshStore(home=d / "node", network_dir=d / "mesh", node_name="open")
    client = TestClient(create_app(node))
    assert client.get("/health").status_code == 200
