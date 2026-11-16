"""S13-I19 acceptance tests: unified peer presence across drop_probe, PeerPresenceCache, and Agent device tools."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

from rynmesh.drop.peers import PeerDirectory, make_hello
from rynmesh.llm_package.service_view import presence_from_probe
from rynmesh.peer_http import create_app
from rynmesh.services.peer_health import PeerHealthProbe, PeerPresenceCache, _default_probe
from rynmesh.store import RynmeshStore


def _start_http_server(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_s13_i19_t1_drop_probe_populates_peer_presence_and_agent_devices(tmp_path, monkeypatch):
    """T1: Successful drop probe immediately populates PeerPresenceCache (online/drop_probe) and Agent device tool."""
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "self_node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")

    self_store = RynmeshStore(home=tmp_path / "self_node", network_dir=tmp_path / "net")
    remote_store = RynmeshStore(home=tmp_path / "remote_node", network_dir=tmp_path / "net")
    remote_msg_priv = X25519PrivateKey.generate()
    import base64

    remote_msg_pub = base64.b64encode(remote_msg_priv.public_key().public_bytes_raw()).decode("ascii")

    class HelloHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(self.path).query)
            nonce = (qs.get("nonce") or [""])[0]
            body = make_hello(
                peer_id=remote_store.peer_id,
                node_name="Remote-Laptop",
                messaging_pub=remote_msg_pub,
                nonce=nonce,
                private_key_bytes=remote_store.private_key_bytes,
            )
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = _start_http_server(HelloHandler)
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        app = create_app(self_store)
        with TestClient(app) as _client:
            presence_cache: PeerPresenceCache = app.state.peer_presence
            drop_engine = app.state.drop_engine
            # Inject candidate endpoint for remote peer and run drop probe.
            drop_engine.dir._extra_endpoints = lambda pid: [endpoint] if pid == remote_store.peer_id else []
            probe_res = drop_engine.dir.probe(remote_store.peer_id, fresh=True)
            assert probe_res.online is True

            cached = presence_cache.get(remote_store.peer_id)
            assert cached is not None
            assert cached["online"] is True
            assert cached["source"] == "drop_probe"
            assert cached["observed_at"]

            # Out-of-order older observation must NOT overwrite newer drop_probe observation.
            older_ts = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
            presence_cache.update([
                {
                    "peerId": remote_store.peer_id,
                    "online": None,
                    "checkedAt": older_ts,
                    "presenceSource": "unknown",
                }
            ])
            still_latest = presence_cache.get(remote_store.peer_id)
            assert still_latest is not None
            assert still_latest["online"] is True
            assert still_latest["source"] == "drop_probe"

            # Agent device tool (ryn_list_devices) also exposes online, observed_at, source='drop_probe'.
            from rynmesh.agent_runtime.bridge import build_agent_integrations

            owner = SimpleNamespace(peer_id=self_store.peer_id, node_name=self_store.node_name, home=self_store.home)
            _, agent_tools, _ = build_agent_integrations(app, owner, self_store.home)
            try:
                # Register remote peer in space/discovered or via personal_space mock so ryn_list_devices lists it.
                monkeypatch.setattr(
                    agent_tools,
                    "_space",
                    lambda: {
                        "membership": "active",
                        "ai_access": True,
                        "members": [{"peer_id": remote_store.peer_id, "name": "Remote-Laptop", "role": "member", "status": "active"}],
                    },
                )
                listed = agent_tools.execute("ryn_list_devices", {}, operation_id="op_dev_1", cancelled=lambda: False)
                remote_row = next(d for d in listed["devices"] if d["peer_id"] == remote_store.peer_id)
                assert remote_row["online"] is True
                assert remote_row["source"] == "drop_probe"
                assert remote_row["observed_at"] == still_latest["observed_at"]
            finally:
                agent_tools.close()
    finally:
        server.shutdown()
        server.server_close()


def test_s13_i19_t2_expired_presence_returns_unconfirmed_not_offline(tmp_path, monkeypatch):
    """T2: Once presence observation expires, both PeerPresenceCache/service_view and Agent device tool report unconfirmed rather than offline."""
    now_mono = [100.0]
    cache = PeerPresenceCache(ttl_s=180.0, clock=lambda: now_mono[0])
    t0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    cache.update([
        {
            "peerId": "peer-offline-earlier",
            "online": False,
            "checkedAt": t0.isoformat(),
            "presenceSource": "heartbeat_expired",
        }
    ])
    fresh_obs = cache.get("peer-offline-earlier")
    assert fresh_obs is not None
    assert fresh_obs["online"] is False
    assert fresh_obs["observed_at"] == t0.isoformat()
    assert fresh_obs["source"] == "heartbeat_expired"

    # Advance past 180s TTL -> observation expires and becomes unconfirmed (not offline).
    now_mono[0] = 300.0
    assert cache.get("peer-offline-earlier") is None
    assert presence_from_probe("peer-offline-earlier", self_id="self", probe=cache.get("peer-offline-earlier")) == {
        "state": "unconfirmed",
        "basis": "none",
    }

    # Agent tool: if an observation's observed_at is stale (>180s old), offline must NOT be concluded; online is None (unconfirmed).
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "agent_node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "agent_node", network_dir=tmp_path / "net")
    app = create_app(store)
    from rynmesh.agent_runtime.bridge import build_agent_integrations

    owner = SimpleNamespace(peer_id=store.peer_id, node_name=store.node_name, home=store.home)
    _, agent_tools, _ = build_agent_integrations(app, owner, store.home)
    try:
        stale_checked = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        monkeypatch.setattr(
            agent_tools,
            "_space",
            lambda: {
                "membership": "active",
                "ai_access": True,
                "members": [{"peer_id": "stale-peer", "name": "StalePeer", "role": "member", "status": "active"}],
            },
        )
        monkeypatch.setattr(
            agent_tools.peer_health,
            "check",
            lambda _peers: [
                {
                    "peerId": "stale-peer",
                    "online": False,
                    "checkedAt": stale_checked,
                    "presenceSource": "heartbeat_expired",
                    "httpReachable": False,
                    "lanReachable": False,
                    "lastSeen": stale_checked,
                }
            ],
        )
        listed = agent_tools.execute("ryn_list_devices", {}, operation_id="op_dev_stale", cancelled=lambda: False)
        stale_row = next(d for d in listed["devices"] if d["peer_id"] == "stale-peer")
        assert stale_row["online"] is None
        assert stale_row["presence_state"] == "unconfirmed"
        assert stale_row["observed_at"] == stale_checked
        assert stale_row["source"] == "heartbeat"
    finally:
        agent_tools.close()


def test_s13_i19_t3_redirect_302_is_treated_as_unreachable_without_following(monkeypatch):
    """T3: Health probe returning 302 redirect is treated as unreachable and never follows Location."""
    target_hits = []

    class RedirectTargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            target_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *_args):
            return

    target_srv = _start_http_server(RedirectTargetHandler)
    target_url = f"http://127.0.0.1:{target_srv.server_port}/health"

    class RedirectorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, *_args):
            return

    redirect_srv = _start_http_server(RedirectorHandler)
    try:
        reachable = _default_probe(f"http://127.0.0.1:{redirect_srv.server_port}/health", {}, 2.0)
        assert reachable is False
        assert target_hits == []

        # Also verify PeerHealthProbe.auth_header delegates to transport.network_key_header.
        monkeypatch.setenv("RYNMESH_NETWORK_KEY", "unified-secret")
        from rynmesh import transport

        probe = PeerHealthProbe()
        expected_val = next(iter(transport.network_key_header().values()))
        assert probe.auth_header() == {"x-ryn-auth": expected_val}
    finally:
        redirect_srv.shutdown()
        redirect_srv.server_close()
        target_srv.shutdown()
        target_srv.server_close()
