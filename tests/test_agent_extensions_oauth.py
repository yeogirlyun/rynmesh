"""Real, owned loopback AS/MCP fixtures; all other network access is denied."""
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions.mcp_oauth import (
    USER_AGENT,
    McpOAuthError,
    McpOAuthManager,
    McpOAuthStore,
    OAuthHttpError,
    canonical_url,
    parse_auth_challenge,
    request_json,
)
from rynmesh.agent_runtime.extensions.mcp_transport import (
    McpSessionManager,
    McpTransportError,
    StreamableHttpMcpSession,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    ports, rejected = set(), []
    connect, dns, pair, init = socket.socket.connect, socket.getaddrinfo, socket.socketpair, ThreadingHTTPServer.__init__
    context = threading.local()
    def denied():
        rejected.append(True)
        raise AssertionError("test_network_denied")
    def allowed(address):
        return isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and (address[1] in ports or getattr(context, "pair", False))
    def guarded(sock, address):
        if not allowed(address):
            return denied()
        return connect(sock, address)
    def guarded_dns(host, port, *args, **kwargs):
        if not allowed((host, int(port))):
            return denied()
        return dns(host, port, *args, **kwargs)
    def guarded_pair(*args, **kwargs):
        context.pair = True
        try:
            return pair(*args, **kwargs)
        finally:
            context.pair = False
    def owned(server, address, *args, **kwargs):
        assert address[0] == "127.0.0.1"
        init(server, address, *args, **kwargs)
        ports.add(server.server_port)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_args: denied())
    monkeypatch.setattr(socket.socket, "sendto", lambda *_args: denied())
    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket, "getfqdn", lambda _host="": "localhost")
    monkeypatch.setattr(socket, "socketpair", guarded_pair)
    monkeypatch.setattr(ThreadingHTTPServer, "__init__", owned)
    yield ports
    assert not rejected, "A test attempted an undeclared network connection"


class LocalAuthority:
    def __init__(self):
        self.calls, self.codes, self.access, self.refresh = [], {}, {}, {}
        self.refresh_count, self.call_count, self.list_count, self.registrations = 0, 0, 0, []
        self.refresh_error, self.pause, self.reject = None, None, None
        self.prm_missing, self.oidc_only, self.issuer_path = False, False, ""
        self.metadata_overrides, self.prm_overrides = {}, {}
        self.require_auth, self.challenge_scope = True, "tools.read"
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                self.serve()
            def do_POST(self):
                self.serve()
            def serve(self):
                size = int(self.headers.get("Content-Length", 0))
                assert size <= 65536
                raw = self.rfile.read(size)
                body = json.loads(raw) if "application/json" in self.headers.get("Content-Type", "") else {k: vals[0] for k, vals in parse_qs(raw.decode()).items()}
                status, headers, value = fixture.handle(self.command, self.path, dict(self.headers), body)
                payload = json.dumps(value).encode() if value is not None else b""
                self.send_response(status)
                for key, val in {"Content-Type": "application/json", "Content-Length": str(len(payload)), **headers}.items():
                    self.send_header(key, val)
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def issuer(self):
        return self.origin + self.issuer_path

    @property
    def resource(self):
        return self.origin + "/mcp"

    def handle(self, method, path, headers, body):
        self.calls.append((method, path, headers, body))
        if method == "GET":
            if path in {"/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource", "/prm"}:
                if self.prm_missing and path.endswith("/mcp"):
                    return 404, {}, {}
                return 200, {}, {"resource": self.resource, "authorization_servers": [self.issuer], "scopes_supported": ["tools.read"], **self.prm_overrides}
            if "oauth-authorization-server" in path or "openid-configuration" in path:
                if self.oidc_only and "oauth-authorization-server" in path:
                    return 404, {}, {}
                return 200, {}, {"issuer": self.issuer, "authorization_endpoint": self.origin + "/authorize", "token_endpoint": self.origin + "/token",
                    "registration_endpoint": self.origin + "/register", "revocation_endpoint": self.origin + "/revoke",
                    "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
                    "authorization_response_iss_parameter_supported": True, **self.metadata_overrides}
        if path == "/register":
            self.registrations.append(body)
            return 201, {}, {"client_id": "dynamic-" + str(len(self.registrations)), "redirect_uris": body["redirect_uris"], "token_endpoint_auth_method": "none"}
        if path == "/token":
            if self.pause:
                ready, release = self.pause
                ready.set()
                assert release.wait(5)
            if body.get("grant_type") == "authorization_code":
                params = self.codes.pop(body.get("code"), None)
                digest = base64.urlsafe_b64encode(hashlib.sha256(body.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
                if not params or digest != params["code_challenge"] or any(body.get(k) != params[k] for k in ("resource", "redirect_uri", "client_id")):
                    return 400, {}, {"error": "invalid_grant"}
            else:
                self.refresh_count += 1
                if self.refresh_error:
                    return (400 if self.refresh_error == "invalid_grant" else 503), {}, {"error": self.refresh_error, "error_description": "synthetic-secret-do-not-echo"}
                params = self.refresh.pop(body.get("refresh_token"), None)
                if not params or body.get("resource") != params["resource"]:
                    return 400, {}, {"error": "invalid_grant"}
            access, refresh = "synthetic-access-" + secrets.token_hex(8), "synthetic-refresh-" + secrets.token_hex(8)
            self.access[access], self.refresh[refresh] = params, params
            return 200, {}, {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "token_type": "Bearer", "scope": params.get("scope", ""), "resource": params["resource"]}
        if path == "/revoke":
            self.refresh.pop(body.get("token"), None)
            return 200, {}, {}
        if path == "/mcp":
            method = body.get("method")
            if method == "tools/call":
                self.call_count += 1
            if method == "tools/list":
                self.list_count += 1
            auth = headers.get("Authorization", "").removeprefix("Bearer ")
            rejection = self.reject(method) if self.reject else 0
            if rejection or (self.require_auth and auth not in self.access):
                error = "insufficient_scope" if rejection == 403 else "invalid_token"
                return rejection or 401, {"WWW-Authenticate": f'Bearer resource_metadata="{self.origin}/prm", scope="{self.challenge_scope}", error="{error}"'}, {}
            if method == "notifications/initialized":
                return 202, {}, None
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "local"}} if method == "initialize" else {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]} if method == "tools/list" else {"content": [{"type": "text", "text": "synthetic result"}]}
            return 200, {}, {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        return 404, {}, {}

    def callback(self, authorization_url, **overrides):
        params = {k: vals[0] for k, vals in parse_qs(urlsplit(authorization_url).query).items()}
        assert params["code_challenge_method"] == "S256" and params["resource"] == self.resource
        code = secrets.token_urlsafe(24)
        self.codes[code] = params
        values = {"code": code, "state": params["state"], "iss": self.issuer, **overrides}
        values = {k: v for k, v in values.items() if v is not None}
        return params["redirect_uri"] + "?" + urlencode(values)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def authority():
    value = LocalAuthority()
    yield value
    value.close()


def setup(tmp_path, authority, **auth):
    store = ExtensionStore(tmp_path)
    store.upsert_mcp_server({"id": "remote", "name": "Local fixture", "transport": "streamable_http", "url": authority.resource,
                             "enabled": True, "auth": {"mode": "oauth", "client_id": "desktop", **auth}})
    manager = McpOAuthManager(store)
    return store, manager


def server(store):
    return store.get_mcp_server_internal("remote")


def begin(store, manager):
    return manager.start_login("remote", server(store)["revision"], "en")


def login(store, manager, authority):
    started = begin(store, manager)
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        response = client.get(authority.callback(started["authorization_url"]))
    assert response.status_code == 200
    assert manager.login_status("remote", started["auth_id"])["state"] == "connected"
    return started


def test_real_login_pkce_sealed_credentials_and_protocol_headers(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    try:
        login(store, manager, authority)
        with_session = StreamableHttpMcpSession(server(store), oauth=manager)
        try:
            assert with_session.list_all_tools()[0]["name"] == "read"
        finally:
            with_session.close()
        assert not authority.registrations
        calls = [item for item in authority.calls if item[1] == "/mcp"]
        assert calls[0][2].get("MCP-Protocol-Version") is None
        assert all(row[2]["MCP-Protocol-Version"] == "2025-06-18" for row in calls[1:])
        assert all(row[2]["User-Agent"] == USER_AGENT for row in authority.calls)
        saved = manager.credentials.path.read_text()
        status = manager.public_status(server(store)["instance_id"])
        assert status["state"] == "logged_in"
        assert "synthetic-access" not in saved + json.dumps(status) + json.dumps(store.overview())
        wrong = ExtensionStore(tmp_path / "wrong", X25519PrivateKey.generate())
        sealed = McpOAuthStore(wrong)
        sealed.path = manager.credentials.path
        with pytest.raises(McpOAuthError, match="storage_unavailable"):
            sealed.read()
    finally:
        manager.close()


def test_challenge_parser_and_well_known_fallback(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    authority.prm_missing, authority.oidc_only, authority.issuer_path = True, True, "/tenant"
    try:
        login(store, manager, authority)
        paths = [row[1] for row in authority.calls]
        assert "/.well-known/oauth-protected-resource" in paths
        assert "/.well-known/oauth-authorization-server/tenant" in paths
        assert "/.well-known/openid-configuration/tenant" in paths
        assert parse_auth_challenge('Basic realm="x", Bearer scope="one two", error="insufficient_scope", error_description="secret"') == {"scope": "one two", "error": "insufficient_scope"}
        assert parse_auth_challenge('Bearer scope="one", scope="two"') == {}
    finally:
        manager.close()


@pytest.mark.parametrize("mutation,error", [
    ("resource", "resource_mismatch"), ("issuer", "issuer_mismatch"), ("pkce", "pkce_unsupported"), ("endpoint", "untrusted_endpoint"),
])
def test_discovery_rejects_mismatch_missing_pkce_and_cross_origin(tmp_path, authority, mutation, error):
    store, manager = setup(tmp_path, authority)
    if mutation == "resource":
        authority.prm_overrides["resource"] = authority.resource + "/other"
    elif mutation == "issuer":
        authority.metadata_overrides["issuer"] = authority.origin + "/other"
    elif mutation == "pkce":
        authority.metadata_overrides["code_challenge_methods_supported"] = []
    else:
        authority.metadata_overrides["token_endpoint"] = "https://169.254.169.254/token"
    with pytest.raises(McpOAuthError, match=error):
        begin(store, manager)
    manager.close()


def test_issuer_pin_never_reuses_preregistered_client_on_new_issuer(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    try:
        login(store, manager, authority)
        authority.issuer_path = "/new"
        for _ in range(2):
            with pytest.raises(McpOAuthError, match="issuer_changed"):
                begin(store, manager)
        row = manager.credentials.read()["servers"][server(store)["instance_id"]]
        assert "tokens" not in row and "client" not in row and row["issuer"] == authority.origin
    finally:
        manager.close()


@pytest.mark.parametrize("override", [{"state": "wrong"}, {"iss": "https://wrong.test"}, {"iss": None}])
def test_callback_state_issuer_rejected_without_token_request(tmp_path, authority, override):
    store, manager = setup(tmp_path, authority)
    try:
        started = begin(store, manager)
        response = httpx.get(authority.callback(started["authorization_url"], **override), trust_env=False)
        assert response.status_code == 400 and "synthetic" not in response.text
        assert not any(row[1] == "/token" for row in authority.calls)
        assert manager.login_status("remote", started["auth_id"])["state"] == "waiting"
    finally:
        manager.close()


def test_optional_issuer_and_upstream_errors_are_fixed(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    authority.metadata_overrides["authorization_response_iss_parameter_supported"] = False
    try:
        started = begin(store, manager)
        response = httpx.get(authority.callback(started["authorization_url"], iss=None, error="bad", error_description="token=synthetic-secret"), trust_env=False)
        assert response.status_code == 400 and "synthetic-secret" not in response.text
        status = manager.login_status("remote", started["auth_id"])
        assert status == {"state": "failed", "error": "agent_mcp_oauth_login_denied"}
        started = begin(store, manager)
        assert httpx.get(authority.callback(started["authorization_url"], iss=None), trust_env=False).status_code == 200
    finally:
        manager.close()


def test_dcr_fixed_port_and_preregistered_priority(tmp_path, authority):
    store, manager = setup(tmp_path, authority, client_id=None)
    try:
        login(store, manager, authority)
        registered = authority.registrations[0]
        assert registered["application_type"] == "native" and registered["token_endpoint_auth_method"] == "none"
        manager.logout("remote")
        started = begin(store, manager)
        params = parse_qs(urlsplit(started["authorization_url"]).query)
        assert params["redirect_uri"] == registered["redirect_uris"] and len(authority.registrations) == 1
        manager.cancel_login("remote", started["auth_id"])
        store.upsert_mcp_server({"auth": {"mode": "oauth", "client_id": "fixed"}, "oauth_client_registration": "dcr"}, server_id="remote")
        login(store, manager, authority)
        assert len(authority.registrations) == 1
    finally:
        manager.close()


def test_auto_public_and_static_authorization_do_not_require_oauth(tmp_path, authority):
    store, manager = setup(tmp_path, authority, mode="auto")
    authority.require_auth = False
    try:
        assert manager.public_status(server(store)["instance_id"])["state"] == "not_required"
        session = StreamableHttpMcpSession(server(store), oauth=manager)
        assert session.list_all_tools()
        session.close()
        for field in ({"secret_headers": {"AUTHORIZATION": "Bearer synthetic-static"}}, {"bearer_token_env": "MISSING_TEST_TOKEN"}, {"env_http_headers": {"Authorization": "MISSING_TEST_TOKEN"}}):
            configured = {**server(store), **field}
            assert manager.access_token(configured) is None
            if not field.get("bearer_token_env"):
                session = StreamableHttpMcpSession(configured, oauth=manager)
                assert session.oauth is None
                session.close()
    finally:
        manager.close()


@pytest.mark.parametrize("method", ["initialize", "tools/list", "notifications/initialized", "tools/call"])
def test_only_safe_methods_refresh_and_retry_once(tmp_path, authority, method):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    attempts = []
    def reject(name):
        if name == method:
            attempts.append(name)
            return 401 if len(attempts) == 1 else 0
        return 0
    authority.reject = reject
    session = StreamableHttpMcpSession(server(store), oauth=manager)
    try:
        if method == "tools/call":
            with pytest.raises(McpTransportError, match="auth_required") as caught:
                session.call_tool("read", {})
            assert caught.value.dispatched and caught.value.auth_challenge["scope"] == "tools.read"
            assert len(attempts) == 1 and authority.refresh_count == 0
        else:
            assert session.list_all_tools()
            assert len(attempts) == 2 and authority.refresh_count == 1
    finally:
        session.close()
        manager.close()


def test_repeated_401_stops_after_one_refresh_and_step_up_is_scope_union(tmp_path, authority):
    store, manager = setup(tmp_path, authority, scopes=["tools.extra"])
    login(store, manager, authority)
    authority.reject = lambda name: 401 if name == "tools/list" else 0
    session = StreamableHttpMcpSession(server(store), oauth=manager)
    try:
        with pytest.raises(McpTransportError, match="auth_required"):
            session.list_all_tools()
        assert authority.list_count == 2 and authority.refresh_count == 1
        manager.record_challenge(server(store), {"error": "insufficient_scope", "scope": "tools.new", "resource_metadata": authority.origin + "/prm"})
        started = begin(store, manager)
        assert set(parse_qs(urlsplit(started["authorization_url"]).query)["scope"][0].split()) == {"tools.read", "tools.extra", "tools.new"}
        manager.cancel_login("remote", started["auth_id"])
    finally:
        session.close()
        manager.close()


def test_refresh_serialization_invalid_grant_and_network_failure(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    row = server(store)
    now = manager.clock()
    manager.clock = lambda: now + 3550
    values, errors = [], []
    def refresh():
        try:
            values.append(manager.access_token(row))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=refresh) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors and len(set(values)) == 1 and authority.refresh_count == 1
    authority.refresh_error = "server_error"
    with pytest.raises(McpOAuthError, match="refresh_unavailable"):
        manager.access_token(row, force_refresh=True)
    assert manager.credentials.read()["servers"][row["instance_id"]]["tokens"]["access_token"] == values[0]
    authority.refresh_error = "invalid_grant"
    with pytest.raises(McpOAuthError, match="auth_required"):
        manager.access_token(row, force_refresh=True)
    assert manager.public_status(row["instance_id"])["state"] == "error"
    manager.close()


@pytest.mark.parametrize("change", ["logout", "cancel", "delete", "config", "disable_enable"])
def test_late_exchange_cannot_resurrect_after_lifecycle_change(tmp_path, authority, change):
    store, manager = setup(tmp_path, authority)
    started = begin(store, manager)
    callback = authority.callback(started["authorization_url"])
    ready, release = threading.Event(), threading.Event()
    authority.pause = (ready, release)
    params = {k: vals[0] for k, vals in parse_qs(urlsplit(callback).query).items()}
    results = []
    worker = threading.Thread(target=lambda: results.append(manager.complete_login(started["auth_id"], params)))
    worker.start()
    assert ready.wait(5)
    try:
        if change == "logout":
            manager.logout("remote")
        elif change == "cancel":
            manager.cancel_login("remote", started["auth_id"])
        elif change == "delete":
            ident = server(store)["instance_id"]
            store.delete_mcp_server("remote")
            manager.forget(ident)
        elif change == "config":
            store.upsert_mcp_server({"auth": {"mode": "oauth", "client_id": "changed"}}, server_id="remote")
        else:
            store.upsert_mcp_server({"enabled": False}, server_id="remote")
            store.upsert_mcp_server({"enabled": True}, server_id="remote")
    finally:
        release.set()
        worker.join(5)
        manager.close()
    assert results == [False]
    assert all("tokens" not in row for row in manager.credentials.read()["servers"].values())
    assert any(row[1] == "/revoke" for row in authority.calls)


def test_logout_revoke_forget_and_recreated_identity_do_not_inherit(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    old = server(store)["instance_id"]
    manager.logout("remote", server(store)["revision"])
    assert manager.public_status(old)["state"] == "logged_out"
    assert any(row[1] == "/revoke" for row in authority.calls)
    store.delete_mcp_server("remote")
    manager.forget(old)
    assert old not in manager.credentials.read()["servers"]
    store.upsert_mcp_server({"id": "remote", "transport": "streamable_http", "url": authority.resource, "auth": {"mode": "oauth", "client_id": "desktop"}})
    assert server(store)["instance_id"] != old and manager.public_status(server(store)["instance_id"])["state"] == "logged_out"
    manager.close()


def test_transport_auth_before_dispatch_and_manager_needs_auth(tmp_path, authority):
    store, _ = setup(tmp_path, authority)
    manager = McpSessionManager(store)
    try:
        result = manager.test_server("remote", expected_revision=server(store)["revision"])
        assert result["test_result"]["error"] == "agent_mcp_auth_required"
        assert not authority.calls
        session = StreamableHttpMcpSession(server(store), oauth=manager.oauth)
        with pytest.raises(McpTransportError) as caught:
            session.call_tool("read", {})
        assert not caught.value.dispatched
        session.close()
    finally:
        manager.close()


def test_request_bounds_redirects_and_fixed_exception_text(monkeypatch):
    real = httpx.Client
    for status, body in [(302, {}), (200, {"value": "x" * 65537})]:
        monkeypatch.setattr(httpx, "Client", lambda _status=status, _body=body, **kwargs: real(transport=httpx.MockTransport(lambda req: httpx.Response(_status, json=_body)), **kwargs))
        with pytest.raises(McpOAuthError):
            request_json("GET", "https://public.example.test/doc")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: (_ for _ in ()).throw(ValueError("code=secret-token")))
    with pytest.raises(OAuthHttpError) as error:
        request_json("GET", "https://public.example.test/doc")
    assert str(error.value) == "agent_mcp_oauth_request_failed" and error.value.__suppress_context__


@pytest.mark.parametrize("url", ["http://public.test/token", "https://u@public.test/token", "https://public.test/token#fragment", "https://public.test\\@evil.test", "file:///secret"])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(McpOAuthError):
        canonical_url(url)


def test_canonical_url_preserves_resource_trailing_slash():
    assert canonical_url("https://PUBLIC.test:443/mcp/") == "https://public.test/mcp/"
    assert canonical_url("https://public.test/mcp") != canonical_url("https://public.test/mcp/")


def test_callback_duplicate_query_host_origin_and_expiry(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    try:
        started = begin(store, manager)
        target = authority.callback(started["authorization_url"])
        assert httpx.get(target + "&state=duplicate", trust_env=False).status_code == 400
        assert httpx.get(target, headers={"Host": "evil.test"}, trust_env=False).status_code == 400
        assert httpx.get(target, headers={"Origin": authority.origin}, trust_env=False).status_code == 400
        assert not any(row[1] == "/token" for row in authority.calls)
        now = manager.clock()
        manager.clock = lambda: now + 601
        assert manager.login_status("remote", started["auth_id"])["state"] == "expired"
        assert manager.public_status(server(store)["instance_id"])["state"] == "logged_out"
    finally:
        manager.close()


def test_real_403_requires_step_up_without_refresh_or_tool_retry(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    session = StreamableHttpMcpSession(server(store), oauth=manager)
    try:
        session.initialize()
        authority.challenge_scope = "tools.read tools.more"
        authority.reject = lambda name: 403 if name == "tools/call" else 0
        with pytest.raises(McpTransportError, match="auth_required") as caught:
            session.call_tool("read", {})
        assert caught.value.auth_challenge["error"] == "insufficient_scope"
        assert authority.call_count == 1 and authority.refresh_count == 0
        assert manager.public_status(server(store)["instance_id"])["state"] == "error"
        started = begin(store, manager)
        assert parse_qs(urlsplit(started["authorization_url"]).query)["scope"] == ["tools.more tools.read"]
    finally:
        session.close()
        manager.close()


def test_new_pending_survives_cancelled_exchange_response(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    started = begin(store, manager)
    target = authority.callback(started["authorization_url"])
    ready, release = threading.Event(), threading.Event()
    actual = manager.transport
    def delayed(method, url, **kwargs):
        value = actual(method, url, **kwargs)
        if url.endswith("/token") and not ready.is_set():
            ready.set()
            assert release.wait(5)
        return value
    manager.transport = delayed
    params = {k: vals[0] for k, vals in parse_qs(urlsplit(target).query).items()}
    result = []
    thread = threading.Thread(target=lambda: result.append(manager.complete_login(started["auth_id"], params)))
    thread.start()
    assert ready.wait(5)
    try:
        manager.cancel_login("remote", started["auth_id"])
        newer = begin(store, manager)
    finally:
        release.set()
        thread.join(5)
    try:
        assert result == [False] and manager.public_status(server(store)["instance_id"])["state"] == "pending"
        assert httpx.get(authority.callback(newer["authorization_url"]), trust_env=False).status_code == 200
    finally:
        manager.close()


def test_late_refresh_after_logout_is_discarded_and_revoked(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    ready, release = threading.Event(), threading.Event()
    actual = manager.transport
    def delayed(method, url, **kwargs):
        value = actual(method, url, **kwargs)
        if url.endswith("/token"):
            ready.set()
            assert release.wait(5)
        return value
    manager.transport = delayed
    errors = []
    def refresh():
        try:
            manager.access_token(server(store), force_refresh=True)
        except McpOAuthError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=refresh)
    thread.start()
    assert ready.wait(5)
    try:
        manager.logout("remote")
    finally:
        release.set()
        thread.join(5)
    assert errors == ["agent_mcp_oauth_configuration_changed"]
    assert manager.public_status(server(store)["instance_id"])["state"] == "logged_out" and not authority.refresh
    manager.close()


def test_disabled_login_refused_and_config_change_discards_tokens(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    original = server(store)["instance_id"]
    store.upsert_mcp_server({"enabled": False}, server_id="remote")
    with pytest.raises(McpOAuthError, match="server_disabled"):
        begin(store, manager)
    # Logging out a disabled source still revokes its prior credential.
    manager.logout("remote")
    assert not authority.refresh
    store.upsert_mcp_server({"enabled": True}, server_id="remote")
    assert manager.public_status(original)["state"] == "logged_out"
    login(store, manager, authority)
    store.upsert_mcp_server({"url": authority.origin + "/other"}, server_id="remote")
    assert manager.public_status(original)["state"] == "logged_out"
    assert "client" not in manager.credentials.read()["servers"][original]
    manager.close()


def test_login_poll_waits_for_discovery_and_stays_authorized_after_revision_change(tmp_path, authority):
    store, _ = setup(tmp_path, authority)
    sessions = McpSessionManager(store)
    ready, release = threading.Event(), threading.Event()
    actual = sessions.oauth.on_connected
    def discover(ident):
        ready.set()
        assert release.wait(5)
        actual(ident)
    sessions.oauth.on_connected = discover
    try:
        started = begin(store, sessions.oauth)
        assert httpx.get(authority.callback(started["authorization_url"]), trust_env=False).status_code == 200
        assert ready.wait(5)
        assert sessions.oauth.login_status("remote", started["auth_id"])["state"] == "exchanging"
        release.set()
        deadline = time.monotonic() + 5
        while sessions.oauth.login_status("remote", started["auth_id"])["state"] != "connected" and time.monotonic() < deadline:
            time.sleep(.01)
        assert sessions.oauth.login_status("remote", started["auth_id"])["state"] == "connected"
        assert len(server(store)["discovered_tools"]) == 1
        assert sessions.oauth.public_status(server(store)["instance_id"])["state"] == "logged_in"
    finally:
        release.set()
        sessions.close()


def test_registration_restriction_and_busy_saved_port_reregisters(tmp_path, authority):
    store, manager = setup(tmp_path, authority, client_id=None)
    try:
        store.upsert_mcp_server({"oauth_client_registration": "cimd"}, server_id="remote")
        with pytest.raises(McpOAuthError, match="client_required"):
            begin(store, manager)
        store.upsert_mcp_server({"oauth_client_registration": "dcr"}, server_id="remote")
        started = begin(store, manager)
        port = urlsplit(parse_qs(urlsplit(started["authorization_url"]).query)["redirect_uri"][0]).port
        manager.cancel_login("remote", started["auth_id"])
        occupied = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        try:
            restarted = begin(store, manager)
            new_port = urlsplit(parse_qs(urlsplit(restarted["authorization_url"]).query)["redirect_uri"][0]).port
            assert new_port != port and len(authority.registrations) == 2
        finally:
            occupied.server_close()
    finally:
        manager.close()


def test_initialized_unauthenticated_call_never_marks_dispatched(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    session = StreamableHttpMcpSession(server(store), oauth=manager)
    session.initialized = True
    dispatched = []
    try:
        with pytest.raises(McpTransportError, match="auth_required") as error:
            session.call_tool("read", {}, before_dispatch=lambda: dispatched.append(True))
        assert not error.value.dispatched and not dispatched and not authority.calls
    finally:
        session.close()
        manager.close()


def test_safe_discovery_recovers_after_tool_call_401_without_replaying_call(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    session = StreamableHttpMcpSession(server(store), oauth=manager)
    try:
        session.initialize()
        authority.reject = lambda name: 401 if name == "tools/call" else 0
        with pytest.raises(McpTransportError, match="auth_required"):
            session.call_tool("read", {})
        assert authority.call_count == 1 and authority.refresh_count == 0
        assert manager.public_status(server(store)["instance_id"])["state"] == "error"
        session.close()
        session = StreamableHttpMcpSession(server(store), oauth=manager)
        assert session.list_all_tools()
        assert authority.call_count == 1 and authority.refresh_count == 1
        assert manager.public_status(server(store)["instance_id"])["state"] == "logged_in"
    finally:
        session.close()
        manager.close()


def test_preemptive_refresh_and_following_401_share_one_refresh_budget(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    login(store, manager, authority)
    now = manager.clock()
    manager.clock = lambda: now + 3550
    authority.reject = lambda name: 401 if name == "initialize" else 0
    session = StreamableHttpMcpSession(server(store), oauth=manager)
    try:
        with pytest.raises(McpTransportError, match="auth_required"):
            session.initialize()
        assert authority.refresh_count == 1
        assert len([row for row in authority.calls if row[1] == "/mcp"]) == 1
    finally:
        session.close()
        manager.close()


def test_expired_unrefreshable_tokens_are_not_exposed_as_logged_in(tmp_path, authority):
    store, manager = setup(tmp_path, authority)
    actual = manager.transport
    def without_refresh(method, url, **kwargs):
        result = actual(method, url, **kwargs)
        if url.endswith("/token"):
            result.pop("refresh_token", None)
        return result
    manager.transport = without_refresh
    try:
        login(store, manager, authority)
        now = manager.clock()
        manager.clock = lambda: now + 3601
        assert manager.public_status(server(store)["instance_id"])["state"] == "logged_out"
        with pytest.raises(McpOAuthError, match="auth_required"):
            manager.access_token(server(store))
        assert authority.refresh_count == 0
    finally:
        manager.close()
