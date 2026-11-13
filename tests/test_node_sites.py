"""Node sites between two real loopback nodes: one hosts a web page, the other opens it.

The opener's browser origin is ``<key>.site.localhost:<gateway>``; tests send
that Host header to 127.0.0.1 directly, which is what Chromium/WebView2 do.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest
import uvicorn
from fastapi import FastAPI
from test_drop_engine import Node, free_port

from rynmesh.drop import codec
from rynmesh.drop.p2p_link import _allowed_target
from rynmesh.drop.routes import install_drop
from rynmesh.drop.engine import DropEngine
from rynmesh.node_sites import host as host_mod
from rynmesh.node_sites import install_node_sites
from rynmesh.node_sites.protocol import REQUEST_INFO, REQUEST_PATH, key_from_host

RESOURCE = "https://provider.test/connect"


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError("external network is disabled")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


class SiteNode(Node):
    """``Node`` plus node sites installed on the same app."""

    def start(self) -> "SiteNode":
        engine = DropEngine(node=self.identity, home=self.home, messaging_key=self.msg_key,
                            network_id=lambda: "test", own_peers=dict, friend_peers=dict)
        app = FastAPI()
        install_drop(app, engine=engine, local_control=lambda request: None)
        self.sites = install_node_sites(app, engine=engine, home=self.home, local_control=lambda request: None)
        self.app = app
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started
        engine.start()
        self.engine = engine
        return self

    def stop(self) -> None:
        if getattr(self, "sites", None):
            self.sites.close()
        super().stop()


class Authority:
    """Stand-in for the provider: directory rows plus live ``site.open`` decisions."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.active = {node.identity.peer_id for node in nodes}
        self.calls = []
        # The directory outlives a device going offline.
        self._rows = [{"peer_id": node.identity.peer_id, "messaging_pub": node.e.msg_pub,
                       "name": f"{node.identity.node_name} PC", "endpoints": [node.endpoint],
                       "member_id": f"m-{index}", "member_name": f"成员 {index}",
                       "provider_resource": RESOURCE, "plugin_id": "test-plugin"}
                      for index, node in enumerate(nodes)]

    def rows(self):
        return [dict(row) for row in self._rows]

    def authorize(self, caller, resource, sender, receiver, capability, service_id=None):
        self.calls.append((caller, capability, service_id))
        if (resource != RESOURCE or caller not in (sender, receiver) or sender not in self.active
                or receiver not in self.active or capability != "site.open" or not service_id):
            raise RuntimeError("synthetic denial detail must never reach the caller")
        return {"allowed": True, "resource": resource, "from_peer_id": sender, "to_peer_id": receiver,
                "capability": capability, "service_id": service_id}


class Backend:
    """A tiny site backend that records what Ryn forwards to it."""

    def __init__(self):
        self.requests = []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _reply(self, status, body, headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                backend.requests.append({"method": self.command, "path": self.path,
                                         "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if self.path == "/":
                    self._reply(200, "<!doctype html><title>做图</title><p>hello</p>".encode(),
                                [("Content-Type", "text/html; charset=utf-8"), ("Set-Cookie", "s=1"),
                                 ("X-Powered-By", "leak")])
                elif self.path.startswith("/echo"):
                    out = json.dumps({"len": len(body), "name": unquote(self.headers.get("X-Ryn-Caller-Name", ""))})
                    self._reply(200, out.encode(), [("Content-Type", "application/json")])
                elif self.path == "/big":
                    self._reply(200, b"\x89" * (3 * 1024 * 1024), [("Content-Type", "image/png")])
                elif self.path == "/go":
                    self._reply(302, b"", [("Location", f"http://127.0.0.1:{backend.port}/done?x=1")])
                elif self.path == "/away":
                    self._reply(302, b"", [("Location", "https://evil.example/steal")])
                else:
                    self._reply(404, b"missing", [("Content-Type", "text/plain")])

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def pair(tmp_path):
    opener, hosting = SiteNode(tmp_path, "opener").start(), SiteNode(tmp_path, "hosting").start()
    authority = Authority([opener, hosting])
    for node in (opener, hosting):
        node.e._provider_peers = authority.rows
        node.e._provider_authorize = (lambda resource, sender, receiver, capability, service_id=None, node=node:
                                      authority.authorize(node.identity.peer_id, resource, sender, receiver,
                                                          capability, service_id))
    backend = Backend()
    try:
        yield opener, hosting, authority, backend
    finally:
        backend.close()
        opener.stop()
        hosting.stop()


def publish(hosting, backend, *, enabled=True, audience=None):
    site, token = hosting.sites.store.register(sid="image-studio", title="做图", port=backend.port, entry="/")
    if enabled or audience:
        hosting.sites.store.update("image-studio", {"enabled": enabled,
                                                    **({"audience": audience} if audience else {})})
    return token


def open_site(opener, hosting):
    return opener.sites.client.open(RESOURCE, hosting.identity.peer_id, "image-studio")


def get(opener, key, path="/", method="GET", body=None, headers=None, host=None):
    port = opener.sites.gateway.port
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=body,
                     headers={"Host": host if host is not None else f"{key}.site.localhost:{port}",
                              **(headers or {})})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def test_closed_site_is_not_listed_or_openable(pair):
    opener, hosting, _authority, backend = pair
    publish(hosting, backend, enabled=False)
    devices = opener.sites.client.remote(refresh=True)
    row = next(d for d in devices if d["peer_id"] == hosting.identity.peer_id)
    assert row["online"] is True and row["sites"] == []
    with pytest.raises(Exception, match="site_not_found"):
        open_site(opener, hosting)
    assert backend.requests == []


def test_open_page_through_gateway_with_identity_from_ryn_only(pair):
    opener, hosting, authority, backend = pair
    token = publish(hosting, backend)
    devices = opener.sites.client.remote(refresh=True)
    row = next(d for d in devices if d["peer_id"] == hosting.identity.peer_id)
    assert row["sites"] == [{"site_id": "image-studio", "title": "做图", "entry": "/"}]
    opened = open_site(opener, hosting)
    assert opened["title"] == "做图" and opened["member_name"] == "成员 1"

    status, headers, body = get(opener, opened["key"], "/", headers={
        "Cookie": "session=steal", "X-Ryn-Caller-Peer": "forged", "X-Ryn-Site-Token": "forged",
        "Accept-Language": "zh-CN"})
    assert status == 200 and "hello" in body.decode()
    assert "set-cookie" not in headers and "x-powered-by" not in headers
    assert "connect-src 'self'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer" and headers["x-content-type-options"] == "nosniff"
    seen = backend.requests[-1]["headers"]
    assert seen["x-ryn-site-token"] == token
    assert seen["x-ryn-caller-peer"] == opener.identity.peer_id
    assert unquote(seen["x-ryn-caller-name"]) == "成员 0" and seen["x-ryn-caller-member"] == "m-0"
    assert "cookie" not in seen and seen["accept-language"] == "zh-CN"
    # Both sides asked the provider; repeated loads reuse the short authorization cache.
    for _ in range(5):
        assert get(opener, opened["key"], "/")[0] == 200
    site_calls = [c for c in authority.calls if c[1] == "site.open"]
    assert {c[2] for c in site_calls} == {"image-studio"}
    assert len(site_calls) <= 3


def test_bodies_head_and_redirects(pair):
    opener, hosting, _authority, backend = pair
    publish(hosting, backend)
    key = open_site(opener, hosting)["key"]
    payload = bytes(range(256)) * (2 * 1024 * 4)  # 2 MiB
    status, _, body = get(opener, key, "/echo", method="POST", body=payload,
                          headers={"Content-Type": "application/octet-stream"})
    assert status == 200 and json.loads(body) == {"len": len(payload), "name": "成员 0"}
    assert backend.requests[-1]["body"] == payload
    status, headers, body = get(opener, key, "/big")
    assert status == 200 and len(body) == 3 * 1024 * 1024 and headers["content-type"] == "image/png"
    status, headers, body = get(opener, key, "/big", method="HEAD")
    assert status == 200 and body == b""
    status, headers, _ = get(opener, key, "/go")
    assert status == 302 and headers["location"] == "/done?x=1"
    status, headers, _ = get(opener, key, "/away")
    assert status == 302 and "location" not in headers


def test_gateway_refuses_unknown_hosts_and_never_reaches_the_node(pair):
    opener, hosting, _authority, backend = pair
    publish(hosting, backend)
    key = open_site(opener, hosting)["key"]
    port = opener.sites.gateway.port
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"{'0' * 32}.site.localhost:{port}",
                 f"{key}.site.localhost:{port + 1}", f"{key}.site.localhost.evil.test:{port}", ""):
        assert get(opener, key, "/", host=host)[0] == 404
    assert backend.requests == []
    # Paths are the site's, never the node's control API.
    status, _, _ = get(opener, key, "/api/local/node-sites/mine")
    assert status == 404 and backend.requests[-1]["path"] == "/api/local/node-sites/mine"
    assert key_from_host(f"{key.upper()}.SITE.LOCALHOST:{port}", port) == key


def test_owner_switch_audience_and_live_revocation(pair):
    opener, hosting, authority, backend = pair
    publish(hosting, backend)
    key = open_site(opener, hosting)["key"]
    assert get(opener, key)[0] == 200
    hosting.sites.store.update("image-studio", {"audience": {"mode": "members", "members": ["m-7"]}})
    assert get(opener, key)[0] == 403
    hosting.sites.store.update("image-studio", {"audience": {"mode": "members", "members": ["m-0"]}})
    assert get(opener, key)[0] == 200
    hosting.sites.store.update("image-studio", {"enabled": False})
    assert get(opener, key)[0] == 403
    hosting.sites.store.update("image-studio", {"enabled": True})
    count = len(backend.requests)
    authority.active.discard(opener.identity.peer_id)
    hosting.sites.host.revoke_cache()  # stands in for the 60 s cache expiring
    opener.sites.client._auth.clear()
    status, _, body = get(opener, key)
    assert status == 403 and b"synthetic" not in body
    assert len(backend.requests) == count


def test_replay_forgery_and_rate_limit(pair, monkeypatch):
    opener, hosting, _authority, backend = pair
    publish(hosting, backend)
    open_site(opener, hosting)
    member = hosting.e.provider_member(RESOURCE, opener.identity.peer_id)
    secret = json.dumps({"method": "POST", "path": "/echo", "headers": [], "body": ""}).encode()

    def envelope(from_pub=None, resource=RESOURCE):
        payload = {"provider_resource": resource, "site_id": "image-studio",
                   "from_pub": from_pub or member["messaging_pub"],
                   "sealed": codec.seal_for(opener.msg_key, hosting.e.msg_pub, secret, REQUEST_INFO)}
        return codec.make_envelope("site_request", payload, sender=opener.identity.peer_id,
                                   to=hosting.identity.peer_id, private_key_bytes=opener.identity.private_key_bytes)

    def post(value):
        conn = http.client.HTTPConnection("127.0.0.1", hosting.port, timeout=30)
        try:
            conn.request("POST", REQUEST_PATH, body=json.dumps(value), headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    first = envelope()
    assert post(first)[0] == 200
    assert post(first)[0] == 409  # replayed envelope
    assert post(envelope(from_pub=hosting.e.msg_pub))[0] == 403  # key not bound to the sender
    assert post(envelope(resource="https://other.test/connect"))[0] == 403
    tampered = envelope()
    tampered["payload"]["site_id"] = "other-site"
    assert post(tampered)[0] == 400  # signature no longer matches
    monkeypatch.setattr(host_mod, "PER_CALLER_PER_MINUTE", 2)
    hosting.sites.host._recent.clear()
    assert [post(envelope())[0] for _ in range(3)] == [200, 200, 429]


def test_offline_host_reports_offline(pair):
    opener, hosting, _authority, backend = pair
    publish(hosting, backend)
    key = open_site(opener, hosting)["key"]
    assert opener.sites.client.status(key, fresh=True)["online"] is True
    hosting.stop()
    opener.e.provider_directory(RESOURCE).forget(hosting.identity.peer_id)
    status, _, body = get(opener, key)
    assert status == 502 and b"site_offline" in body
    state = opener.sites.client.status(key, fresh=True)
    assert state["online"] is False and state["member_name"] == "成员 1"


def test_p2p_link_carries_site_paths_only_alongside_drop():
    assert _allowed_target("POST", "/api/peer/site/request")
    assert _allowed_target("POST", "/api/peer/drop/message")
    assert not _allowed_target("POST", "/api/local/node-sites/mine")
    assert not _allowed_target("POST", "/api/peer/site/../local")


def test_store_validation(tmp_path):
    from rynmesh.node_sites.store import SiteStore
    store = SiteStore(tmp_path)
    site, token = store.register(sid="demo", title="Demo", port=18001)
    assert site["enabled"] is False and len(token) > 30 and "token" not in site
    for bad in ({"sid": "Bad_Id", "title": "x", "port": 18001}, {"sid": "ok", "title": "", "port": 18001},
                {"sid": "ok", "title": "x", "port": 80}, {"sid": "ok", "title": "x", "port": 18001, "entry": "http://x/"}):
        with pytest.raises(Exception):
            store.register(**bad)
    store.update("demo", {"enabled": True})
    _, token2 = store.register(sid="demo", title="Demo 2", port=18002)
    assert token2 != token and store.site("demo")["enabled"] is True
    key = store.key_for(RESOURCE, "peer", "demo")
    assert store.key_for(RESOURCE, "peer", "demo") == key and store.opened(key)["site_id"] == "demo"
    assert store.opened("../x") is None
