"""Tests for S13-I45: Fronted and CdnWS outbound CONNECT proxy, 407 auth error, system/manual settings, and pinned friend invite proxy tunneling."""
from __future__ import annotations

import base64
import datetime
import hashlib
import select
import socket
import ssl
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from rynmesh import transport
from rynmesh.peer_http import create_app
from rynmesh.settings_store import SettingsStore
from rynmesh.store import RynmeshStore
from rynmesh.transport import (
    CdnWebSocketTransport,
    FrontedHttpsTransport,
    TransportError,
    TransportProfile,
    get_pinned_transport,
    reset_transport_cache,
    resolve_profile,
    transport_status,
)

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "ryn_home"))
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "s13-i45-token")
    monkeypatch.delenv("RYNMESH_HTTPS_PROXY", raising=False)
    monkeypatch.delenv("RYNMESH_HTTP_PROXY", raising=False)
    monkeypatch.delenv("RYNMESH_USE_SYSTEM_PROXY", raising=False)
    monkeypatch.delenv("RYNMESH_TRANSPORT", raising=False)
    monkeypatch.delenv("RYNMESH_TLS_SNI", raising=False)
    monkeypatch.delenv("RYNMESH_CONNECT_HOST", raising=False)
    monkeypatch.delenv("RYNMESH_CDN_WS_URL", raising=False)
    reset_transport_cache()
    yield
    reset_transport_cache()


def _make_tls_pair(tmp_path: Path, dns_names: list[str]) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    return server_ctx, client_ctx


class _FakeConnectProxy:
    """Minimal real TCP HTTP CONNECT proxy supporting optional Basic auth or forced 407."""

    def __init__(self, *, expected_auth: tuple[str, str] | None = None, force_status: int | None = None) -> None:
        self.expected_auth = expected_auth
        self.force_status = force_status
        self.requests: list[dict[str, Any]] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                client_sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        client_sock.settimeout(5.0)
        upstream: socket.socket | None = None
        try:
            buf = bytearray()
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                buf.extend(chunk)
            header_text = bytes(buf).split(b"\r\n\r\n", 1)[0].decode("latin-1")
            lines = header_text.split("\r\n")
            req_line = lines[0]
            parts = req_line.split()
            method = parts[0] if parts else ""
            target = parts[1] if len(parts) > 1 else ""
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            self.requests.append({"method": method, "target": target, "headers": headers})

            if self.force_status == 407:
                client_sock.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="test"\r\n'
                    b"Content-Length: 0\r\n\r\n"
                )
                return

            if self.expected_auth is not None:
                user, pw = self.expected_auth
                expected = "Basic " + base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
                if headers.get("proxy-authorization") != expected:
                    client_sock.sendall(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b'Proxy-Authenticate: Basic realm="test"\r\n'
                        b"Content-Length: 0\r\n\r\n"
                    )
                    return

            if method == "GET":
                client_sock.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\n{"registry":true}')
                return

            if method != "CONNECT":
                client_sock.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                return

            host, port_s = target.rsplit(":", 1)
            host = host.strip("[]")
            upstream = socket.create_connection((host, int(port_s)), timeout=5.0)
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            sockets = [client_sock, upstream]
            while not self._stop.is_set():
                readable, _, _ = select.select(sockets, [], [], 2.0)
                if not readable:
                    break
                done = False
                for ready in readable:
                    data = ready.recv(16384)
                    if not data:
                        done = True
                        break
                    other = upstream if ready is client_sock else client_sock
                    other.sendall(data)
                if done:
                    break
        except OSError:
            return
        finally:
            if upstream is not None:
                upstream.close()
            client_sock.close()


def _start_tls_http_origin(
    server_ctx: ssl.SSLContext, response_body: bytes
) -> tuple[int, list[dict[str, str]], Callable[[], None]]:
    observed: list[dict[str, str]] = []

    def sni_cb(ssl_sock: ssl.SSLSocket, server_name: str | None, _ctx: ssl.SSLContext) -> None:
        observed.append({"sni": server_name or ""})

    server_ctx.sni_callback = sni_cb
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                conn.settimeout(5.0)
                with server_ctx.wrap_socket(conn, server_side=True) as tls_conn:
                    req = bytearray()
                    while b"\r\n\r\n" not in req:
                        chunk = tls_conn.recv(4096)
                        if not chunk:
                            break
                        req.extend(chunk)
                    hdr_block = bytes(req).split(b"\r\n\r\n", 1)[0].decode("latin-1")
                    host_hdr = ""
                    for line in hdr_block.split("\r\n")[1:]:
                        if line.lower().startswith("host:"):
                            host_hdr = line.split(":", 1)[1].strip()
                    if observed:
                        observed[-1]["host"] = host_hdr
                    resp = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(response_body)}\r\n".encode("ascii")
                        + b"Connection: close\r\n\r\n"
                        + response_body
                    )
                    tls_conn.sendall(resp)
            except OSError:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def close() -> None:
        stop.set()
        listener.close()
        thread.join(timeout=2)

    return port, observed, close


def _start_tls_ws_origin(
    server_ctx: ssl.SSLContext, inner_http_response: bytes
) -> tuple[int, list[dict[str, str]], Callable[[], None]]:
    observed: list[dict[str, str]] = []

    def sni_cb(ssl_sock: ssl.SSLSocket, server_name: str | None, _ctx: ssl.SSLContext) -> None:
        observed.append({"sni": server_name or ""})

    server_ctx.sni_callback = sni_cb
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                conn.settimeout(5.0)
                with server_ctx.wrap_socket(conn, server_side=True) as tls_conn:
                    buf = bytearray()
                    while b"\r\n\r\n" not in buf:
                        chunk = tls_conn.recv(4096)
                        if not chunk:
                            break
                        buf.extend(chunk)
                    hdrs: dict[str, str] = {}
                    for line in bytes(buf).split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")[1:]:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            hdrs[k.strip().lower()] = v.strip()
                    if observed:
                        observed[-1]["ws_host"] = hdrs.get("host", "")
                    ws_key = hdrs["sec-websocket-key"]
                    accept = base64.b64encode(
                        hashlib.sha1((ws_key + WS_MAGIC).encode("ascii")).digest()
                    ).decode("ascii")
                    tls_conn.sendall(
                        f"HTTP/1.1 101 Switching Protocols\r\n"
                        f"Upgrade: websocket\r\n"
                        f"Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
                    )
                    # Read client masked frame header
                    fhdr = tls_conn.recv(2)
                    plen = fhdr[1] & 0x7F
                    if plen == 126:
                        plen = int.from_bytes(tls_conn.recv(2), "big")
                    elif plen == 127:
                        plen = int.from_bytes(tls_conn.recv(8), "big")
                    mask = tls_conn.recv(4)
                    raw_payload = bytearray()
                    while len(raw_payload) < plen:
                        raw_payload.extend(tls_conn.recv(plen - len(raw_payload)))
                    unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(raw_payload))
                    if observed:
                        observed[-1]["inner_req"] = unmasked.decode("latin-1", errors="replace")
                    # Send unmasked server binary frame
                    n = len(inner_http_response)
                    if n < 126:
                        out_hdr = bytes([0x82, n])
                    else:
                        out_hdr = bytes([0x82, 126]) + n.to_bytes(2, "big")
                    tls_conn.sendall(out_hdr + inner_http_response)
            except OSError:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def close() -> None:
        stop.set()
        listener.close()
        thread.join(timeout=2)

    return port, observed, close


def test_s13_i45_t1_fronted_and_cdn_ws_tunnel_via_connect_proxy_with_basic_auth_and_status(
    tmp_path: Path, monkeypatch
) -> None:
    """T1 (covers Steps 1, 2, 5): FrontedHttpsTransport and CdnWebSocketTransport tunnel over HTTP CONNECT with Basic auth and TLS SNI; S13-I16 status shows via_proxy."""
    server_ctx, client_ctx = _make_tls_pair(
        tmp_path, ["cdn.front.example", "ws.front.example", "friend.example"]
    )
    monkeypatch.setattr(transport, "_ssl_context", lambda _profile: client_ctx)

    proxy = _FakeConnectProxy(expected_auth=("alice", "s3cr3t"))
    origin_port, origin_obs, close_origin = _start_tls_http_origin(
        server_ctx, b'{"fronted_via_proxy":true}'
    )
    (tmp_path / "ws_cert").mkdir()
    ws_server_ctx, _ = _make_tls_pair(tmp_path / "ws_cert", ["cdn.front.example"])
    monkeypatch.setattr(
        transport,
        "_ssl_context",
        lambda _profile: ssl.create_default_context(cafile=str(tmp_path / "server.pem"))
        if _profile.name != "cdn-ws"
        else ssl.create_default_context(cafile=str(tmp_path / "ws_cert" / "server.pem")),
    )
    ws_port, ws_obs, close_ws = _start_tls_ws_origin(
        ws_server_ctx,
        b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 19\r\n\r\n{"ws_proxied":true}',
    )
    try:
        proxy_url = f"http://alice:s3cr3t@127.0.0.1:{proxy.port}"

        # 1. FrontedHttpsTransport via CONNECT + Basic Auth + SNI split
        fronted_profile = TransportProfile(
            name="hardened",
            proxies={"https": proxy_url},
            sni="cdn.front.example",
            connect_host="127.0.0.1",
        )
        fronted = FrontedHttpsTransport(fronted_profile)
        data = fronted.get_bytes(
            f"https://origin.peer.example:{origin_port}/api/v1/node",
            timeout_s=5.0,
            max_bytes=4096,
        )
        assert data == b'{"fronted_via_proxy":true}'
        assert proxy.requests[0]["method"] == "CONNECT"
        assert proxy.requests[0]["target"] == f"127.0.0.1:{origin_port}"
        assert origin_obs[0]["sni"] == "cdn.front.example"
        assert origin_obs[0]["host"] == f"origin.peer.example:{origin_port}"

        # 2. CdnWebSocketTransport via same CONNECT + Basic Auth + SNI split
        monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"wss://relay.cdn.example:{ws_port}/ryn-ws")
        ws_profile = TransportProfile(
            name="cdn-ws",
            proxies={"https": proxy_url},
            sni="cdn.front.example",
            connect_host="127.0.0.1",
        )
        ws_tx = CdnWebSocketTransport(ws_profile)
        ws_data = ws_tx.get_bytes(
            "https://peer-behind-cdn.example/api/v1/node",
            timeout_s=5.0,
            max_bytes=4096,
        )
        assert ws_data == b'{"ws_proxied":true}'
        assert proxy.requests[1]["method"] == "CONNECT"
        assert proxy.requests[1]["target"] == f"127.0.0.1:{ws_port}"
        assert ws_obs[0]["sni"] == "cdn.front.example"
        assert ws_obs[0]["ws_host"] == f"relay.cdn.example:{ws_port}"

        # 5. S13-I16 transport status includes via_proxy boolean
        reset_transport_cache()
        assert transport_status()["via_proxy"] is False
        monkeypatch.setenv("RYNMESH_HTTPS_PROXY", proxy_url)
        reset_transport_cache()
        assert transport_status()["via_proxy"] is True

        app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net"))
        with TestClient(app) as client:
            resp = client.get(
                "/api/local/node/status",
                headers={"x-ryn-local-token": "s13-i45-token"},
            )
            assert resp.status_code == 200
            assert resp.json()["transport"]["via_proxy"] is True
    finally:
        close_ws()
        close_origin()
        proxy.close()


def test_s13_i45_t2_proxy_407_raises_proxy_auth_required(monkeypatch) -> None:
    """T2 (covers Steps 1, 2): Proxy returning 407 raises TransportError with reason='proxy_auth_required'."""
    proxy = _FakeConnectProxy(force_status=407)
    try:
        proxy_url = f"http://127.0.0.1:{proxy.port}"
        fronted = FrontedHttpsTransport(
            TransportProfile(
                name="hardened",
                proxies={"https": proxy_url},
                sni="cdn.front.example",
                connect_host="127.0.0.1",
            )
        )
        with pytest.raises(TransportError) as fronted_exc:
            fronted.get_bytes("https://origin.peer.example:8443/health", timeout_s=5.0, max_bytes=1024)
        assert fronted_exc.value.reason == "proxy_auth_required"
        assert fronted_exc.value.status == 407

        monkeypatch.setenv("RYNMESH_CDN_WS_URL", "wss://relay.cdn.example:8443/ryn-ws")
        ws_tx = CdnWebSocketTransport(
            TransportProfile(
                name="cdn-ws",
                proxies={"https": proxy_url},
                sni="cdn.front.example",
                connect_host="127.0.0.1",
            )
        )
        with pytest.raises(TransportError) as ws_exc:
            ws_tx.get_bytes("https://origin.peer.example/health", timeout_s=5.0, max_bytes=1024)
        assert ws_exc.value.reason == "proxy_auth_required"
        assert ws_exc.value.status == 407
    finally:
        proxy.close()


def test_s13_i45_t3_system_proxy_env_and_manual_settings_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    """T3 (covers Steps 3, 6): RYNMESH_USE_SYSTEM_PROXY=1 reads urllib.request.getproxies(); manual network_proxy setting takes precedence over env."""
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890", "http": "http://127.0.0.1:7891"},
    )

    # Default ignores ambient system proxy
    assert resolve_profile().proxies == {}

    # RYNMESH_USE_SYSTEM_PROXY=1 enables reading system proxies
    monkeypatch.setenv("RYNMESH_USE_SYSTEM_PROXY", "1")
    sys_prof = resolve_profile()
    assert sys_prof.proxies == {"https": "http://127.0.0.1:7890", "http": "http://127.0.0.1:7891"}

    # Node settings manual mode overrides env vars (settings read before env)
    monkeypatch.setenv("RYNMESH_HTTPS_PROXY", "http://127.0.0.1:9999")
    node_home = tmp_path / "ryn_home"
    app = create_app(RynmeshStore(home=node_home, network_dir=tmp_path / "net"))
    headers = {"x-ryn-local-token": "s13-i45-token"}
    with TestClient(app) as client:
        patch_resp = client.patch(
            "/api/local/settings",
            headers=headers,
            json={"network_proxy": {"mode": "manual", "url": "http://127.0.0.1:8888"}},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["network_proxy"] == {
            "mode": "manual",
            "url": "http://127.0.0.1:8888",
        }

        manual_prof = resolve_profile()
        assert manual_prof.proxies["https"] == "http://127.0.0.1:8888"
        assert manual_prof.proxies["http"] == "http://127.0.0.1:8888"

        # Switching settings to system mode also works without env var
        monkeypatch.delenv("RYNMESH_USE_SYSTEM_PROXY", raising=False)
        monkeypatch.delenv("RYNMESH_HTTPS_PROXY", raising=False)
        sys_resp = client.patch(
            "/api/local/settings",
            headers=headers,
            json={"network_proxy": {"mode": "system", "url": ""}},
        )
        assert sys_resp.status_code == 200
        assert resolve_profile().proxies == {
            "https": "http://127.0.0.1:7890",
            "http": "http://127.0.0.1:7891",
        }


def test_s13_i45_t4_friend_invite_and_pinned_transport_via_connect_proxy(
    tmp_path: Path, monkeypatch
) -> None:
    """T4 (covers Steps 4, 6): Non-CONNECT proxy schemes are still rejected with clear reason, while HTTP CONNECT proxy tunnels pinned transport and full FriendService invitation to the pinned IP."""
    # Step 4: non-CONNECT proxy scheme (e.g. socks5) is rejected with clear reason
    monkeypatch.setenv("RYNMESH_HTTPS_PROXY", "socks5://127.0.0.1:1080")
    with pytest.raises(TransportError) as bad_proxy:
        get_pinned_transport("https://friend.example:8791", "127.0.0.1")
    assert bad_proxy.value.reason == "pinned_proxy_unsupported"
    assert "CONNECT" in str(bad_proxy.value) or "pinned" in str(bad_proxy.value)

    # Step 6: HTTP CONNECT proxy tunnels get_pinned_transport to pinned IP while keeping original SNI
    monkeypatch.delenv("RYNMESH_HTTPS_PROXY", raising=False)
    server_ctx, client_ctx = _make_tls_pair(tmp_path, ["friend.example"])
    monkeypatch.setattr(transport, "_ssl_context", lambda _profile: client_ctx)

    proxy = _FakeConnectProxy()
    tls_port, tls_obs, close_tls = _start_tls_http_origin(server_ctx, b'{"pinned_ok":true}')

    # Also start a real HTTP server hosting inviter's /api/peer/friends/accept to test real FriendService.join
    import http.server
    import json
    from rynmesh.friends.routes import install_friends
    from rynmesh.services import peer_box

    inviter_store = RynmeshStore(home=tmp_path / "inviter", network_dir=tmp_path / "net", node_name="Inviter")
    inviter_msg_key = peer_box.load_or_create_messaging_key(inviter_store.home / "messaging.x25519")

    class _DummyWorkers:
        def register(self, *_a, **_kw):
            return None

    from fastapi import FastAPI

    inviter_holder: dict[str, Any] = {}

    class _AcceptHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if self.path == "/api/peer/friends/accept":
                result = inviter_holder["service"].accept(json.loads(raw.decode("utf-8")))
                out = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            return

    inviter_http = http.server.HTTPServer(("127.0.0.1", 0), _AcceptHandler)
    inviter_port = inviter_http.server_address[1]
    inviter_thread = threading.Thread(target=inviter_http.serve_forever, daemon=True)
    inviter_thread.start()

    try:
        settings = SettingsStore(tmp_path / "ryn_home" / "settings.json")
        settings.patch({"network_proxy": {"mode": "manual", "url": f"http://127.0.0.1:{proxy.port}"}})

        # 1) Direct get_pinned_transport over TLS + CONNECT proxy
        pinned_tx = get_pinned_transport(f"https://friend.example:{tls_port}", "127.0.0.1")
        pinned_resp = pinned_tx.get_bytes(
            f"https://friend.example:{tls_port}/api/v1/node",
            timeout_s=5.0,
            max_bytes=2048,
        )
        assert pinned_resp == b'{"pinned_ok":true}'
        assert proxy.requests[0]["method"] == "CONNECT"
        assert proxy.requests[0]["target"] == f"127.0.0.1:{tls_port}"
        assert tls_obs[0]["sni"] == "friend.example"
        assert tls_obs[0]["host"] == f"friend.example:{tls_port}"

        # 2) Real FriendService invite + join wired through install_friends over the CONNECT proxy
        monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
        monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", f"http://127.0.0.1:{inviter_port}")
        inviter_app = FastAPI()
        inviter_service = install_friends(
            inviter_app,
            store=inviter_store,
            home=inviter_store.home,
            workers=_DummyWorkers(),
            local_control=lambda _req: None,
            messaging_key=inviter_msg_key,
        )
        inviter_holder["service"] = inviter_service
        invite_bundle = inviter_service.create_invite()

        joiner_store = RynmeshStore(home=tmp_path / "joiner", network_dir=tmp_path / "net", node_name="Joiner")
        joiner_msg_key = peer_box.load_or_create_messaging_key(joiner_store.home / "messaging.x25519")
        monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", "http://127.0.0.1:18799")
        joiner_app = FastAPI()
        joiner_service = install_friends(
            joiner_app,
            store=joiner_store,
            home=joiner_store.home,
            workers=_DummyWorkers(),
            local_control=lambda _req: None,
            messaging_key=joiner_msg_key,
        )

        joined = joiner_service.join(invite_bundle["invite_uri"])
        assert joined["status"] == "active"
        assert joined["peer_id"] == inviter_store.peer_id
        assert any(
            req["method"] == "CONNECT" and req["target"] == f"127.0.0.1:{inviter_port}"
            for req in proxy.requests
        )
        assert len(inviter_service.list_friends()) == 1
    finally:
        inviter_http.shutdown()
        inviter_thread.join(timeout=2)
        close_tls()
        proxy.close()


def test_s13_i45_t3_persisted_off_overrides_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HTTPS_PROXY", "http://proxy.example:8080")
    settings = SettingsStore(tmp_path / "ryn_home" / "settings.json")
    settings.patch({"network_proxy": {"mode": "off", "url": ""}})
    reset_transport_cache()
    assert resolve_profile().proxies == {}


def test_s13_i45_t3_persisted_manual_survives_restart(tmp_path, monkeypatch):
    settings = SettingsStore(tmp_path / "ryn_home" / "settings.json")
    settings.patch({"network_proxy": {"mode": "manual", "url": "http://proxy.example:8080"}})
    reset_transport_cache()
    assert resolve_profile().proxies == {
        "https": "http://proxy.example:8080", "http": "http://proxy.example:8080"
    }


def test_s13_i45_t3_registry_uses_saved_manual_proxy(tmp_path, monkeypatch):
    from urllib.request import Request
    from rynmesh.registry import _open_registry_request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    proxy = _FakeConnectProxy()
    try:
        SettingsStore(tmp_path / "ryn_home" / "settings.json").patch({
            "network_proxy": {"mode": "manual", "url": f"http://127.0.0.1:{proxy.port}"}
        })
        with _open_registry_request(Request("http://registry.invalid/status"), timeout_s=2) as response:
            assert response.read() == b'{"registry":true}'
        assert proxy.requests[0]["target"] == "http://registry.invalid/status"
    finally:
        proxy.close()
