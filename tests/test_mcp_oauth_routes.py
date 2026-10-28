"""Real local routes, extension state and export/sync boundaries; no external IO."""
import io
import json
import socket
import zipfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.extensions import (
    ExtensionStore,
    ExtensionToolAdapter,
    compute_model_tool_name,
)
from rynmesh.agent_runtime.extensions.mcp_oauth import McpOAuthManager
from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager
from rynmesh.agent_runtime.extensions.routes import install_extension_routes
from rynmesh.agent_runtime.store import AgentError
from rynmesh.node_auth import NodeAuth

PREFIX = "/api/local/agent/extensions"
CANARY = "synthetic-private-oauth-canary"
AUTH = {"mode": "oauth", "client_id": "desktop", "scopes": ["tools.read"], "callback_port": None}


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    ports = set()
    bind, connect, connect_ex, resolve = socket.socket.bind, socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def denied(*args, **kwargs):
        raise AssertionError("unexpected test network")

    def owned_bind(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            return denied()
        result = bind(sock, address)
        ports.add(sock.getsockname()[1])
        return result

    def owned_connect(sock, address, original=connect):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] in ports:
            return original(sock, address)
        return denied()

    def owned_resolve(host, *args, **kwargs):
        if host in {"127.0.0.1", "::1"}:
            return resolve(host, *args, **kwargs)
        return denied()

    monkeypatch.setattr(socket.socket, "bind", owned_bind)
    monkeypatch.setattr(socket.socket, "connect", owned_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, address: owned_connect(sock, address, connect_ex))
    monkeypatch.setattr(socket.socket, "sendto", denied)
    monkeypatch.setattr(socket, "getaddrinfo", owned_resolve)


class FakeOAuth:
    """Only the OAuth protocol is fake; routes exercise its public interface."""
    def __init__(self, store):
        self.store, self.calls, self.states, self.pending = store, [], {}, {}
        self.private = {"access_token": CANARY, "refresh_token": CANARY, "client_secret": CANARY}

    def public_status(self, instance_id):
        return {"state": self.states.get(instance_id, "logged_out"), "issuer": "https://as.example.test", "scopes": ["tools.read"]}

    def start_login(self, server_id, revision, *, language):
        row = self.store.get_mcp_server_internal(server_id, expected_revision=revision)
        self.calls.append(("start", server_id, language))
        self.pending["synthetic-flow"] = server_id
        self.states[row["instance_id"]] = "pending"
        return {"auth_id": "synthetic-flow", "authorization_url": "https://as.example.test/authorize?state=synthetic", "expires_in": 300}

    def login_status(self, server_id, auth_id):
        self.calls.append(("status", server_id))
        if self.pending.get(auth_id) != server_id:
            raise AgentError("agent_mcp_oauth_flow_not_found")
        return {"state": "waiting"}

    def cancel_login(self, server_id, auth_id):
        self.login_status(server_id, auth_id)
        self.pending.pop(auth_id)
        self.calls.append(("cancel", server_id))
        return {"state": "cancelled"}

    def logout(self, server_id, revision):
        row = self.store.get_mcp_server_internal(server_id, expected_revision=revision)
        self.states[row["instance_id"]] = "logged_out"
        self.calls.append(("logout", server_id))
        return {"state": "logged_out"}

    def forget(self, instance_id):
        self.states.pop(instance_id, None)
        self.calls.append(("forget", instance_id))

    def clear_all(self):
        self.states.clear()
        self.pending.clear()
        self.calls.append(("clear",))

    def close(self):
        pass


@pytest.fixture
def host(tmp_path):
    store = ExtensionStore(tmp_path / "agent", X25519PrivateKey.generate())
    manager = McpSessionManager(store)
    manager.oauth = oauth = FakeOAuth(store)
    adapter = ExtensionToolAdapter(SimpleNamespace(), lambda: None, extension_store=store, session_manager=manager)
    app = FastAPI()
    auth = NodeAuth(tmp_path)

    def local_control(request):
        if not auth.authorize(client_host=request.client.host, headers=request.headers, cookie=request.cookies.get("ryn_session", "")).allowed:
            raise HTTPException(401, "local_control_unauthorized")

    runtime = SimpleNamespace(store=SimpleNamespace(inspect=lambda read: read({"settings": {}})))
    install_extension_routes(app, runtime=runtime, extensions=adapter, local_control=local_control)
    client = TestClient(app)
    value = SimpleNamespace(store=store, manager=manager, oauth=oauth, adapter=adapter, app=app, client=client)
    yield value
    client.close()
    manager.close()


def server(host, server_id="fixture"):
    return host.store.upsert_mcp_server({"id": server_id, "transport": "streamable_http", "url": "https://mcp.example.test/tools",
        "auth": AUTH, "enabled": True})


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "", None), ("POST", "/mcp/servers/fixture/oauth/login", {"expected_revision": 1}),
    ("GET", "/mcp/servers/fixture/oauth/login/flow", None), ("POST", "/mcp/servers/fixture/oauth/login/flow/cancel", {}),
    ("POST", "/mcp/servers/fixture/oauth/logout", {"expected_revision": 1}),
    ("POST", "/mcp/oauth/clear-local-authorizations", {"confirm": True}),
    ("DELETE", "/mcp/servers/fixture?expected_revision=1", None), ("DELETE", "/plugins/fixture?expected_revision=1", None),
])
@pytest.mark.parametrize("headers", [{"x-forwarded-for": "203.0.113.10"}, {"origin": "https://untrusted.example.test"}])
def test_every_oauth_and_cleanup_route_requires_local_owner(host, method, path, payload, headers):
    response = host.client.request(method, PREFIX + path, headers=headers, **({"json": payload} if payload is not None else {}))
    assert response.status_code == 401 and host.oauth.calls == []


def test_public_lifecycle_routes_never_return_private_credentials(host):
    row = server(host)
    begin = host.client.post(PREFIX + "/mcp/servers/fixture/oauth/login", json={"expected_revision": row["revision"], "language": "en"})
    assert begin.status_code == 200 and begin.json()["auth_id"] == "synthetic-flow"
    assert host.oauth.calls[-1] == ("start", "fixture", "en")
    status = host.client.get(PREFIX + "/mcp/servers/fixture/oauth/login/synthetic-flow")
    cancelled = host.client.post(PREFIX + "/mcp/servers/fixture/oauth/login/synthetic-flow/cancel")
    logout = host.client.post(PREFIX + "/mcp/servers/fixture/oauth/logout", json={"expected_revision": row["revision"]})
    overview = host.client.get(PREFIX)
    assert status.json() == {"state": "waiting"} and cancelled.json() == {"state": "cancelled"}
    assert logout.status_code == 200 and host.manager._server_generations["fixture"] == 1
    for response in (begin, status, cancelled, logout, overview):
        assert CANARY not in response.text
        assert all(key not in response.text for key in ("access_token", "refresh_token", "client_secret"))


@pytest.mark.parametrize("action", ["login", "logout"])
def test_mutating_login_routes_validate_revision_before_oauth_work(host, action):
    row = server(host)
    host.store.upsert_mcp_server({"description": "new revision"}, server_id="fixture", expected_revision=row["revision"])
    path = PREFIX + f"/mcp/servers/fixture/oauth/{action}"
    assert host.client.post(path, json={"expected_revision": row["revision"]}).status_code == 409
    for value in (None, True, 0, "invalid"):
        assert host.client.post(path, json={"expected_revision": value}).status_code == 400
    assert host.oauth.calls == []


def test_unknown_oauth_failure_does_not_echo_private_error_in_response_or_log(host, monkeypatch, caplog):
    row = server(host)
    def fail(*args, **kwargs):
        raise RuntimeError(CANARY)
    monkeypatch.setattr(host.oauth, "start_login", fail)
    response = host.client.post(PREFIX + "/mcp/servers/fixture/oauth/login", json={"expected_revision": row["revision"]})
    assert response.status_code == 503 and response.json()["detail"] == "agent_unavailable"
    assert CANARY not in response.text + caplog.text


def test_server_delete_forgets_only_removed_installation_after_revision_check(host):
    row, other = server(host), server(host, "other")
    assert host.client.delete(PREFIX + "/mcp/servers/fixture?expected_revision=999999").status_code == 409
    assert host.oauth.calls == []
    response = host.client.delete(PREFIX + f"/mcp/servers/fixture?expected_revision={row['revision']}")
    assert response.status_code == 200 and host.oauth.calls == [("forget", row["instance_id"])]
    assert host.store.get_mcp_server_internal("other")["instance_id"] == other["instance_id"]
    assert host.manager._server_generations["fixture"] == 1


def test_plugin_delete_forgets_all_its_servers_but_preserves_other_sources(host):
    other = server(host, "other")
    result = host.store.commit_imported_items(plugins=[{"id": "bundle", "mcp_server_ids": ["one", "two"]}], skills=[],
        mcp_servers=[{"id": name, "plugin_id": "bundle", "transport": "streamable_http", "url": "https://mcp.example.test/tools", "auth": AUTH} for name in ("one", "two")])
    plugin = next(row for row in result["overview"]["plugins"] if row["id"] == "bundle")
    identities = {host.store.get_mcp_server_internal(name)["instance_id"] for name in ("one", "two")}
    response = host.client.delete(PREFIX + f"/plugins/bundle?expected_revision={plugin['revision']}")
    assert response.status_code == 200
    assert {value for action, value in host.oauth.calls if action == "forget"} == identities
    assert host.store.get_mcp_server_internal("other")["instance_id"] == other["instance_id"]


def test_clear_requires_literal_confirmation_and_keeps_server_configuration(host):
    row = server(host)
    path = PREFIX + "/mcp/oauth/clear-local-authorizations"
    for value in (False, "true", 1, None):
        assert host.client.post(path, json={"confirm": value}).status_code == 400
    assert host.oauth.calls == []
    assert host.client.post(path, json={"confirm": True}).json() == {"cleared": True}
    assert host.oauth.calls == [("clear",)] and host.manager._manager_generation == 1
    assert host.store.get_mcp_server_internal("fixture")["instance_id"] == row["instance_id"]


@pytest.mark.parametrize("state,visible", [("logged_out", False), ("pending", False), ("error", False), ("logged_in", True)])
def test_tool_list_and_preview_exclude_unauthenticated_servers(host, state, visible):
    row = server(host)
    row = host.store.record_mcp_discovery("fixture", expected_revision=row["revision"], status="ready", discovered_tools=[
        {"name": "read", "input_schema": {"type": "object"}, "annotations": {"readOnlyHint": True}}])
    host.oauth.states[row["instance_id"]] = state
    name = compute_model_tool_name("fixture", "read")
    assert (name in {spec["function"]["name"] for spec in host.adapter.specs()}) is visible
    if not visible:
        with pytest.raises((AgentError, ValueError)):
            host.adapter.preview(name, {})
    current = next(value for value in host.client.get(PREFIX).json()["mcp_servers"] if value["id"] == "fixture")
    assert current["oauth"]["state"] == state
    if not visible:
        assert current["status"] == "NeedsAuth" and current["status_message"] == "agent_mcp_auth_required"


def test_nested_plugin_servers_report_the_same_login_status(host):
    result = host.store.commit_imported_items(plugins=[{"id": "bundle", "mcp_server_ids": ["nested"]}], skills=[],
        mcp_servers=[{"id": "nested", "plugin_id": "bundle", "transport": "streamable_http", "url": "https://mcp.example.test/tools", "auth": AUTH}])
    plugin = next(row for row in result["overview"]["plugins"] if row["id"] == "bundle")
    host.store.set_plugin_enabled("bundle", True, expected_revision=plugin["revision"])
    overview = host.client.get(PREFIX).json()
    flat = next(row for row in overview["mcp_servers"] if row["id"] == "nested")
    nested = next(row for row in overview["plugins"] if row["id"] == "bundle")["mcp_servers"][0]
    assert flat["status"] == nested["status"] == "NeedsAuth"
    assert flat["status_message"] == nested["status_message"] == "agent_mcp_auth_required"


def real_oauth(host):
    def denied_transport(*args, **kwargs):
        raise AssertionError("unexpected OAuth protocol request")

    host.manager.oauth = manager = McpOAuthManager(host.store, transport=denied_transport)
    return manager


def test_explicit_identity_group_sso_is_resource_client_and_scope_bound(host, tmp_path):
    from rynmesh.agent_runtime.extensions import ExtensionImportManager
    manager=real_oauth(host)
    def install(pid, client_id='desktop', scopes=None):
        package=tmp_path/pid;package.mkdir()
        (package/'plugin.json').write_text(json.dumps({'id':pid,'name':pid,'ryn':{'identity_group':{'id':'example-suite','name':'Example'}}}))
        (package/'mcp.json').write_text(json.dumps({'mcpServers':{'reader':{'type':'http','url':'https://mcp.example.test/tools','oauth':{'clientId':client_id,'scopes':scopes or ['tools.read']}}}}))
        importer=ExtensionImportManager(host.store);preview=importer.preview({'kind':'plugin','path':str(package)})
        assert preview['can_import'];importer.commit(preview['preview_id'],expected_revision=preview['expected_revision'])
        plugin=next(p for p in host.store.overview()['plugins'] if p['id']==pid)
        host.store.set_plugin_enabled(pid,True,expected_revision=plugin['revision'])
        return host.store.get_mcp_server_internal(pid+'-reader')
    source=install('example-one');same=install('example-two')
    other_client=install('example-three','another-client');other_scope=install('example-four',scopes=['admin.write'])
    manager.public_status(source['instance_id'])
    with manager.credentials.transaction() as rows:
        rows[source['instance_id']].update(tokens={'access_token':CANARY,'refresh_token':CANARY,'expires_at':9999999999},issuer='https://mcp.example.test',granted_scopes=['tools.read'])
    assert manager._propagate_identity_group_tokens(source)==[same['id']]
    assert manager.public_status(same['instance_id'])['state']=='logged_in'
    assert manager.public_status(other_client['instance_id'])['state']=='logged_out'
    assert manager.public_status(other_scope['instance_id'])['state']=='logged_out'
    manager.logout(same['id'],same['revision'])
    assert manager.public_status(source['instance_id'])['state']=='logged_out'


@pytest.mark.parametrize("mode,expected", [("auto", "not_required"), ("oauth", "logged_out"), ("none", "not_required")])
def test_real_manager_public_status_respects_auth_mode(host, mode, expected):
    manager = real_oauth(host)
    row = host.store.upsert_mcp_server({"id": "fixture", "transport": "streamable_http", "url": "https://mcp.example.test/tools",
        "auth": {"mode": mode}, "enabled": True})
    overview = host.client.get(PREFIX)
    current = next(value for value in overview.json()["mcp_servers"] if value["id"] == "fixture")
    assert current["oauth"]["state"] == manager.public_status(row["instance_id"])["state"] == expected
    if mode == "oauth":
        assert current["status"] == "NeedsAuth"
    assert CANARY not in overview.text


@pytest.mark.parametrize("action", ["logout", "delete", "clear"])
def test_real_manager_cleanup_removes_persisted_credentials_through_routes(host, action):
    manager = real_oauth(host)
    row, other = server(host), server(host, "other")
    for current in (row, other):
        manager.public_status(current["instance_id"])
        with manager.credentials.transaction() as rows:
            rows[current["instance_id"]].update(tokens={"access_token": CANARY, "refresh_token": CANARY, "expires_at": 9999999999},
                issuer="https://as.example.test", granted_scopes=["tools.read"])
    assert CANARY not in manager.credentials.path.read_text(encoding="utf-8")
    overview = host.client.get(PREFIX)
    assert overview.status_code == 200 and CANARY not in overview.text
    assert next(value for value in overview.json()["mcp_servers"] if value["id"] == "fixture")["oauth"]["state"] == "logged_in"
    if action == "logout":
        response = host.client.post(PREFIX + "/mcp/servers/fixture/oauth/logout", json={"expected_revision": row["revision"]})
    elif action == "delete":
        response = host.client.delete(PREFIX + f"/mcp/servers/fixture?expected_revision={row['revision']}")
    else:
        response = host.client.post(PREFIX + "/mcp/oauth/clear-local-authorizations", json={"confirm": True})
    assert response.status_code == 200 and CANARY not in response.text
    # Reopen encrypted state so an in-memory-only cleanup cannot pass.
    reopened = McpOAuthManager(host.store, transport=manager.transport)
    saved = reopened.credentials.read()["servers"]
    assert not (saved.get(row["instance_id"], {}).get("tokens"))
    if action == "clear":
        assert not saved
        assert host.store.get_mcp_server_internal("fixture")["instance_id"] == row["instance_id"]
    else:
        assert saved[other["instance_id"]]["tokens"]["access_token"] == CANARY
    if action == "logout":
        assert reopened.public_status(row["instance_id"])["state"] == "logged_out"
    reopened.close()


def test_actual_device_transfer_excludes_local_oauth_file(tmp_path):
    from test_ask_history import sample
    from test_device_sync_reading import ITEM
    from test_device_sync_transfer import Mesh

    from rynmesh.device_sync.records import SyncError

    mesh = Mesh(tmp_path)
    path = tmp_path / "A" / "agent" / "mcp_oauth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"synthetic_credential_file": CANARY}), encoding="utf-8")
    mesh.reader(mesh.a).record(ITEM, "bookmark")
    mesh.reader(mesh.a).record(ITEM, "progress", progress=.6)
    mesh.history(mesh.a).save(sample(), expected_revision=0)
    for scope in ("bookmarks", "reading", "conversations"):
        wire = mesh.left.prepare(mesh.pair_id, scope)
        plaintext = mesh.left._outgoing(mesh.left._pair(mesh.pair_id), wire)
        assert CANARY not in json.dumps(plaintext) and "mcp_oauth.json" not in json.dumps(plaintext)
        mesh.left.acknowledge(wire, mesh.right.receive(wire))
    assert mesh.reader(mesh.b).sync_read("reading", ITEM["item_id"])["value"]["progress"] == .6
    assert mesh.history(mesh.b).get(sample()["id"])
    assert not list((tmp_path / "B").rglob("mcp_oauth.json"))
    assert CANARY in path.read_text(encoding="utf-8")
    with pytest.raises(SyncError):
        mesh.left.prepare(mesh.pair_id, "mcp_oauth")


def test_real_privacy_json_and_all_scope_zip_exports_exclude_local_oauth_file(tmp_path, monkeypatch):
    from rynmesh.peer_http import create_app
    from rynmesh.privacy_export.archive import SCOPES
    from rynmesh.store import RynmeshStore

    home = tmp_path / "node"
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("RYNMESH_ALLOW_REMOTE_CONTROL", raising=False)
    monkeypatch.delenv("RYNMESH_NETWORK_KEY", raising=False)
    monkeypatch.setattr("rynmesh.store._local_ip_addresses", lambda: ["192.0.2.10"])
    monkeypatch.setattr("rynmesh.llm_package.routes.warm_stun_choice", lambda: None)
    app = create_app(RynmeshStore(home=home, network_dir=tmp_path / "network"))
    path = home / "agent" / "mcp_oauth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"synthetic_credential_file": CANARY}), encoding="utf-8")
    app.state.consumption_store.record({"item_id": "export-fixture", "title": "Public fixture", "link": "https://example.test/article"}, "bookmark")
    client = TestClient(app)  # No lifespan: no updater/discovery/background jobs.
    try:
        summary = client.get("/api/local/privacy/export")
        archive = client.post("/api/local/privacy/export/archive", json={"scopes": list(SCOPES)})
        assert summary.status_code == archive.status_code == 200
        assert "export-fixture" in summary.text and CANARY not in summary.text and "mcp_oauth.json" not in summary.text
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            assert not any("mcp_oauth" in name for name in zipped.namelist())
            assert all(CANARY.encode() not in zipped.read(name) for name in zipped.namelist())
            assert b"export-fixture" in zipped.read("reading/history.json")
        assert CANARY in path.read_text(encoding="utf-8")
    finally:
        client.close()
        app.state.agent_runtime.tools.extensions.close()
        app.state.peer_providers.close()
        app.state.llm_shutdown()

