"""Independent boundary checks for provider-backed inference, without network."""
import copy
import io
import json
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import test_external_model_access as shared_access
from fastapi.testclient import TestClient
from test_external_model_access import RESOURCE, opened, wire

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError
from rynmesh.drop.peers import http_json, make_hello
from rynmesh.llm_package.external_transport import ExternalPeerTransport
from rynmesh.llm_package.routes import _peer_post_json as real_peer_post_json
from rynmesh.llm_package.task_protocol import TaskOrderStore
from rynmesh.peer_http import HttpPeerClient
from rynmesh.services import peer_box

pair = shared_access.pair


@pytest.mark.parametrize("protocol", [None, "openai_responses", "anthropic_messages"])
def test_ordinary_agent_preserves_provider_binding_without_scope_destination(pair, protocol):
    if protocol:
        pair.c.authority.models[0]["service"]["capabilities"].append("protocol:" + protocol)
    bridge, _, catalog = build_agent_integrations(pair.c.app, pair.c.store, pair.c.store.home)
    model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
    result = bridge.complete(model["id"], [{"role": "user", "content": "Ordinary request"}], [], lambda: False)
    assert result["message"]["content"] == "shared reply"
    assert pair.c.authority.calls and pair.p.authority.calls


def test_scoped_runtime_accepts_only_exact_consumer_provider_binding(pair):
    bridge, _, catalog = build_agent_integrations(pair.c.app, pair.c.store, pair.c.store.home)
    model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
    runtime = object.__new__(AgentRuntime)
    runtime.catalog, runtime.model, runtime.consumer_node_id = catalog, bridge, pair.c.store.peer_id
    assert runtime._bound_destination(model) == catalog.destination(model)
    for changed in ("consumer", "permission", "revision"):
        bad = copy.deepcopy(model)
        if changed == "consumer":
            bad["consumer_peer_id"] = pair.p.store.peer_id
        elif changed == "permission":
            bad["expected_model_binding"]["ai_permission"] = {"relationship_id": "a" * 32, "revision": 1}
        else:
            bad["destination_revision"] = "changed"
        with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
            runtime._bound_destination(bad)


@pytest.mark.parametrize("stream", [False, True])
def test_external_transport_never_sends_ambient_mesh_headers(monkeypatch, stream):
    sent = []
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "synthetic-private-network-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")

    class Connection:
        def __init__(self, host, port=None, **kwargs):
            assert host == "node.example.test"

        def request(self, method, path, body=None, headers=None):
            sent.append((method, path, body, headers))

        def getresponse(self):
            value = io.BytesIO(b'{"ok":true}\n')
            value.status = 200
            return value

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    client = HttpPeerClient("https://node.example.test", transport=ExternalPeerTransport())
    if stream:
        assert list(client.iter_post_ndjson("/api/peer/llm/tasks/stream", {"ciphertext": "synthetic"})) == [{"ok": True}]
    else:
        assert client.post_json("/api/peer/llm/tasks", {"ciphertext": "synthetic"},
                                headers={"Authorization": "synthetic-ambient-credential"}) == {"ok": True}
    assert len(sent) == 1
    assert set(sent[0][3]) == {"Content-Type", "Accept"}
    assert "synthetic-private-network-key" not in repr(sent)
    assert "synthetic-ambient-credential" not in repr(sent)


def test_streaming_revocation_stops_new_chunks_and_terminal_result(pair, monkeypatch):
    import rynmesh.llm_package.routes as routes

    now = [routes.time.monotonic()]
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now[0], time=routes.time.time, sleep=routes.time.sleep))
    events = []

    def chat(body, **kwargs):
        kwargs["on_event"]({"text": "before revocation"})
        pair.p.authority.allowed = False
        now[0] += 2
        kwargs["on_event"]({"text": "must not leave provider"})
        pytest.fail("revoked stream continued")

    pair.manager.adapter.inner.chat = chat
    request = wire(pair, chat={"messages": [{"role": "user", "content": "hello"}], "stream": True})
    result = opened(pair, pair.manager.handle(request, on_event=events.append))
    assert len(events) == 1
    assert result["state"] != "succeeded" and "output" not in result


def test_signed_presence_probe_does_not_send_private_network_key(pair, monkeypatch):
    sent = []
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "synthetic-private-network-key")

    class Connection:
        def __init__(self, host, port=None, **kwargs):
            assert host == "node.example.test"

        def request(self, method, path, body=None, headers=None):
            self.nonce = parse_qs(urlsplit(path).query)["nonce"][0]
            sent.append((method, path, headers))

        def getresponse(self):
            hello = make_hello(peer_id=pair.p.store.peer_id, node_name="Synthetic peer",
                messaging_pub=peer_box.public_key_b64(pair.p.key), nonce=self.nonce,
                private_key_bytes=pair.p.store.private_key_bytes)
            value = io.BytesIO(json.dumps(hello).encode())
            value.status = 200
            return value

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    catalog = pair.c.services["external_catalog"]
    catalog.request = http_json
    assert catalog.refresh({**pair.row, "endpoints": ["https://node.example.test"]}) is True
    assert len(sent) == 1 and sent[0][2] == {"Accept": "application/json"}


def capture_external_requests(pair, monkeypatch):
    """Keep real HTTP helper/transport; replace only the final socket owner."""
    sent = []
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "synthetic-private-network-key")
    monkeypatch.setattr("rynmesh.llm_package.routes._peer_post_json", real_peer_post_json)

    class Connection:
        def __init__(self, host, port=None, **kwargs):
            assert host == "node.example.test"

        def request(self, method, path, body=None, headers=None):
            self.path, self.body = path, json.loads(body)
            sent.append({"path": path, "headers": headers, "body": self.body})

        def getresponse(self):
            if self.path == "/api/peer/llm/tasks":
                response = pair.manager.handle(self.body)
            elif self.path == "/api/peer/llm/settlements":
                response = pair.manager.settle_earning(self.body)
            elif self.path == "/api/peer/llm/cancellations":
                response = {"cancelled": pair.manager.cancel_signed(self.body)}
            else:
                pytest.fail("unexpected peer request path")
            value = io.BytesIO(json.dumps(response).encode())
            value.status = 200
            return value

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    return sent


def test_live_external_settlement_keeps_credential_free_transport(pair, monkeypatch):
    sent = capture_external_requests(pair, monkeypatch)
    pair.c.authority.models[0]["endpoints"] = ["https://node.example.test"]
    pair.c.services["external_catalog"].run_once()
    bridge, _, catalog = build_agent_integrations(pair.c.app, pair.c.store, pair.c.store.home)
    model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
    result = bridge.complete(model["id"], [{"role": "user", "content": "Settlement test"}], [], lambda: False)
    assert result["message"]["content"] == "shared reply"
    settlement = [request for request in sent if request["path"] == "/api/peer/llm/settlements"]
    assert len(settlement) == 1
    assert set(settlement[0]["headers"]) == {"Content-Type", "Accept"}
    assert "synthetic-private-network-key" not in repr(sent)
    records = TaskOrderStore(pair.c.store.home / "llm" / "consumer-orders").list()
    assert len(records) == 1 and records[0]["bindings"]["provider_resource"] == RESOURCE


def test_external_cancel_resolves_its_source_and_omits_network_key(pair, monkeypatch):
    sent = capture_external_requests(pair, monkeypatch)
    monkeypatch.setattr(pair.c.authority, "cached_peers", lambda: [
        {"provider_resource": "https://other.example/connect", "peer_id": pair.p.store.peer_id,
         "endpoints": ["https://wrong-source.invalid"]},
        {"provider_resource": RESOURCE, "peer_id": pair.p.store.peer_id,
         "endpoints": ["https://node.example.test"]},
    ], raising=False)
    orders = TaskOrderStore(pair.c.store.home / "llm" / "consumer-orders")
    orders.claim(task_id="external-cancel-review", bindings={"provider_peer_id": pair.p.store.peer_id,
        "service_id": pair.sid, "provider_resource": RESOURCE})
    result = pair.c.services["cancel_order"]("external-cancel-review")
    assert result["state"] == "cancelled"
    assert len(sent) == 1 and sent[0]["path"] == "/api/peer/llm/cancellations"
    assert set(sent[0]["headers"]) == {"Content-Type", "Accept"}
    assert "synthetic-private-network-key" not in repr(sent)


def test_full_app_mounts_provider_controls_and_extension_sync(tmp_path, monkeypatch):
    """Construct the real app, without starting its network/background lifespan."""
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    def denied(*args, **kwargs):
        raise AssertionError("unexpected network during app installation")

    owned_ports = set()
    real_bind, real_connect, real_connect_ex, real_resolve = socket.socket.bind, socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def bind(sock, address):
        result = real_bind(sock, address)
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            owned_ports.add(sock.getsockname()[1])
        return result

    def connect(sock, address, *, original=real_connect):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] in owned_ports:
            return original(sock, address)
        return denied()

    def resolve(host, *args, **kwargs):
        if host in {"127.0.0.1", "::1"}:
            return real_resolve(host, *args, **kwargs)
        return denied()

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, address: connect(sock, address, original=real_connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr("rynmesh.store._local_ip_addresses", lambda: ["192.0.2.10"])
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("RYNMESH_ALLOW_REMOTE_CONTROL", raising=False)
    monkeypatch.delenv("RYNMESH_NETWORK_KEY", raising=False)
    monkeypatch.setattr("rynmesh.llm_package.routes.warm_stun_choice", lambda: None)
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = create_app(store)
    try:
        assert app.state.peer_providers.extension_source() == []
        assert store.peer_providers is app.state.peer_providers
        paths = {getattr(r, "path", "") for r in app.routes}
        assert {"/api/local/peer-providers", "/api/local/peer-providers/{plugin_id}/login/start",
                "/api/local/peer-providers/{plugin_id}/publish"} <= paths
        # An ASGI request without TestClient context deliberately does not run
        # lifespan, the updater, LAN discovery, or a background network worker.
        client = TestClient(app)
        try:
            response = client.get("/api/local/peer-providers")
            assert response.status_code == 200 and response.json() == {"providers": []}
            extensions = app.state.agent_runtime.tools.extensions.store
            extensions.commit_imported_items(plugins=[{"id": "provider-smoke", "peer_provider": {
                "url": RESOURCE, "oauth": {"clientId": "desktop", "scopes": ["peer.connect"]}}}], skills=[], mcp_servers=[])
            disabled = client.get("/api/local/peer-providers/provider-smoke/status").json()
            assert disabled["status"] == "signed_out" and disabled["enabled"] is False
            installed = extensions.list_peer_providers_internal()[0]
            extensions.set_plugin_enabled("provider-smoke", True, expected_revision=installed["revision"])
            enabled = client.get("/api/local/peer-providers/provider-smoke/status").json()
            assert enabled["status"] == "signed_out" and enabled["enabled"] is True
            assert client.post("/api/local/peer-providers/provider-smoke/publish", json={"service_ids": []}).status_code == 409
            installed = extensions.list_peer_providers_internal()[0]
            extensions.delete_plugin("provider-smoke", expected_revision=installed["revision"])
            assert client.get("/api/local/peer-providers").json() == {"providers": []}
            denied_response = client.get("/api/local/peer-providers", headers={"x-forwarded-for": "203.0.113.9"})
            assert denied_response.status_code == 401
        finally:
            client.close()
    finally:
        app.state.peer_providers.close()
        app.state.llm_shutdown()
