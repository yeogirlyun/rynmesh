"""In-memory authorization server; all real outbound sockets denied by default."""
import asyncio
import base64
import copy
import hashlib
import json
import socket
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.peer_providers import (
    PeerProviderError,
    PeerProviderService,
    install_peer_provider_routes,
)
from rynmesh.peer_providers.oauth import ProviderHttpError, request_json
from rynmesh.peer_providers.validation import endpoints, manifest, model_record

ORIGIN = "https://provider.test"
RESOURCE = ORIGIN + "/connect"
MANIFEST = {"url": RESOURCE, "oauth": {"clientId": "desktop", "scopes": ["peer.connect"]}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect, original_pair = socket.socket.connect, socket.socketpair
    context = threading.local()
    allowed = set()
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network access")
    def connect(sock, address):
        if (getattr(context, "socketpair", False) and address[0] in {"127.0.0.1", "::1"}) or address[:2] in allowed:
            return original_connect(sock, address)
        return denied()
    def socketpair(*args, **kwargs):
        context.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            context.socketpair = False
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    return allowed


def model():
    return {"service": {"package_id": "model-a", "model_alias": "Model A", "adapter": "openai_compatible",
            "context_window": 32768, "capabilities": ["client-tools"], "pricing": {"maximum_per_task": 10}},
            "capacity": {"available": 2}, "model_binding": {"version": "ryn.model-binding.v1", "service_id": "model-a", "model_generation": "a" * 32, "rule_revision": "b" * 32},
            "chat_protocol": "rynmesh.chat.v1", "tool_support": "supported", "delivery_protocols": ["complete-v1", "stream-v1"]}


class FakeServer:
    def __init__(self):
        self.clock = [1800000000.0]
        self.calls, self.tokens, self.flows, self.bindings, self.published, self.challenges = [], {}, {}, {}, {}, {}
        self.refreshes = 0
        self.revoked = set()
        self.response_filter = lambda target, value: value
        self.fail = None
        self.wait_request = None

    async def transport(self, method, target, headers=None, json=None, data=None):
        self.calls.append((method, target, copy.deepcopy(headers), copy.deepcopy(json), copy.deepcopy(data)))
        if self.wait_request:
            await self.wait_request(target)
        if self.fail and self.fail(target):
            raise ProviderHttpError(403)
        result = self.respond(method, target, headers or {}, json, data)
        return self.response_filter(target, copy.deepcopy(result))

    def mint(self, resource, subject):
        access = "private-access-" + str(len(self.tokens))
        refresh = "private-refresh-" + str(len(self.tokens))
        for token in (access, refresh):
            self.tokens[token] = (resource, subject)
        return {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "token_type": "Bearer", "scope": "peer.connect"}

    def respond(self, method, target, headers, body, data):
        if "/.well-known/oauth-protected-resource" in target:
            resource = ORIGIN + target.split("/.well-known/oauth-protected-resource", 1)[1]
            return {"resource": resource, "authorization_servers": [ORIGIN]}
        if target == ORIGIN + "/.well-known/oauth-authorization-server":
            return {"issuer": ORIGIN, "authorization_endpoint": ORIGIN + "/authorize", "token_endpoint": ORIGIN + "/token", "revocation_endpoint": ORIGIN + "/revoke", "code_challenge_methods_supported": ["S256"], "authorization_response_iss_parameter_supported": True, "token_endpoint_auth_methods_supported": ["none"], "response_types_supported": ["code"]}
        if target == ORIGIN + "/token":
            if data["grant_type"] == "authorization_code":
                flow = self.flows.pop(data["code"])
                challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).decode().rstrip("=")
                assert flow["code_challenge"] == challenge
                assert flow["redirect_uri"] == data["redirect_uri"]
                assert flow["resource"] == data["resource"]
                return self.mint(flow["resource"], flow["subject"])
            self.refreshes += 1
            token = data["refresh_token"]
            if token in self.revoked:
                raise ProviderHttpError(400)
            resource, subject = self.tokens[token]
            assert data["resource"] == resource
            self.revoked.add(token)
            return self.mint(resource, subject)
        if target == ORIGIN + "/revoke":
            self.revoked.add(data["token"])
            return {}
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.tokens or token in self.revoked:
            raise ProviderHttpError(401)
        resource, subject = self.tokens[token]
        assert target.startswith(resource + "/")
        operation = target[len(resource):]
        if operation == "/challenge":
            cid = "challenge-" + str(len(self.challenges))
            expiry = datetime.fromtimestamp(self.clock[0] + 120, timezone.utc).isoformat()
            value = {"version": "ryn.peer-bind.v1", "issuer": ORIGIN, "resource": resource, "grant_id": subject, "member_id": subject, "nonce": "nonce", "challenge_id": cid, **body, "expires_at": expiry}
            message = __import__("json").dumps(value, separators=(",", ":"))
            self.challenges[cid] = (subject, body, message)
            return {"challenge_id": cid, "message": message, "expires_at": expiry}
        if operation == "/bind":
            owner, fields, message = self.challenges.pop(body["challenge_id"])
            assert subject == owner
            Ed25519PublicKey.from_public_bytes(base64.b64decode(fields["peer_id"])).verify(base64.b64decode(body["signature"]), message.encode())
            self.bindings[subject] = fields
            return {"peer_id": fields["peer_id"], "member_id": subject, "revision": 1}
        if operation == "/models":
            assert subject in self.bindings
            self.published[subject] = body["models"]
            return {"revision": 2}
        if operation == "/directory":
            members = [{"id": s, "name": "Member " + s, "devices": [binding]} for s, binding in self.bindings.items()]
            models = [{**m, **{k: b[k] for k in ("peer_id", "messaging_pub", "endpoints")}} for s, b in self.bindings.items() for m in self.published.get(s, [])]
            return {"version": "ryn.peer-directory.v1", "issuer": ORIGIN, "resource": resource, "revision": 3, "members": members, "models": models}
        if operation == "/authorize":
            ids = {b["peer_id"] for b in self.bindings.values()}
            if body["from_peer_id"] not in ids or body["to_peer_id"] not in ids:
                raise ProviderHttpError(403)
            assert self.bindings[subject]["peer_id"] in (body["from_peer_id"], body["to_peer_id"])
            return {"allowed": True, "issuer": ORIGIN, "resource": resource, **body, "revision": 3}
        raise AssertionError("Unexpected fake endpoint")


async def fake_callback(callback):
    return "http://127.0.0.1:17861/callback", lambda: None


def node(tmp_path, server, name="one", **kwargs):
    private = Ed25519PrivateKey.generate()
    store = SimpleNamespace(peer_id=base64.b64encode(private.public_key().public_bytes_raw()).decode(), private_key_bytes=private.private_bytes_raw(), node_name=name)
    messaging = X25519PrivateKey.generate()
    service = PeerProviderService(tmp_path / name, store, messaging, transport=server.transport, clock=lambda: server.clock[0], callback_factory=fake_callback, model_status=lambda: [model()], **kwargs)
    service.configure("test", MANIFEST)
    return service


async def begin(service, server, subject="one", plugin="test"):
    response = await service.start_login(plugin)
    parsed = {k: values[0] for k, values in parse_qs(urlsplit(response["browser_url"]).query).items()}
    code = "private-code-" + subject
    server.flows[code] = {**parsed, "subject": subject}
    return {"state": parsed["state"], "iss": ORIGIN, "code": code}, response


async def login(service, server, subject="one", plugin="test"):
    params, _ = await begin(service, server, subject, plugin)
    return await service.finish_login(plugin, params)


def test_two_nodes_bind_discover_publish_authorize_and_sealed_storage(tmp_path):
    server = FakeServer()
    a, b = node(tmp_path, server), node(tmp_path, server, "two")
    async def scenario():
        assert (await login(a, server))["bound"]
        await login(b, server, "two")
        await a.publish("test", ["model-a"])
        directory = await b.directory("test")
        assert len(directory["members"]) == 2
        record = b.cached_models()[0]
        assert record["provider_resource"] == RESOURCE and record["consumer_peer_id"] == b.store.peer_id
        assert record["external_permission"] == {"resource": RESOURCE}
        assert "online" not in record
        assert a.locally_published(RESOURCE, "model-a", model()["model_binding"])
        assert (await a.authorize(RESOURCE, b.store.peer_id, a.store.peer_id, "model.invoke", "model-a"))["allowed"]
        assert (await b.authorize(RESOURCE, a.store.peer_id, b.store.peer_id, "message.text"))["allowed"]
        assert "private-" not in json.dumps(a.status())
        assert "private-" not in a.storage.path.read_text()
        restored = PeerProviderService(tmp_path / "one", a.store, a.messaging_key, transport=server.transport, clock=lambda: server.clock[0])
        assert restored.status("test")["bound"]
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["http://provider.test/connect", "https://u:p@provider.test/connect", "https://provider.test/connect#x", "https://provider.test/connect?q=x", "https://provider.test\\@evil.test/connect", "https://provider.test/../connect", "https://provider.test/%2e/connect", "https://provider.test:0/connect"])
def test_invalid_manifest_url(bad):
    with pytest.raises(PeerProviderError):
        manifest({**MANIFEST, "url": bad})


@pytest.mark.parametrize("bad", ["http://127.0.0.1:42", "http://[::1]:42", "http://169.254.169.254", "http://0.0.0.0", "http://localhost", "http://example.test/path", "http://example.test?secret=x"])
def test_invalid_peer_endpoints(bad):
    with pytest.raises(PeerProviderError):
        endpoints([bad])


def test_unknown_resource_never_connects(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    with pytest.raises(PeerProviderError, match="access_denied"):
        asyncio.run(service.authorize("https://evil.test/connect", service.store.peer_id, service.store.peer_id, "message.text"))
    assert not server.calls


@pytest.mark.parametrize("change", [{"state": "wrong"}, {"iss": "https://evil.test"}, {"code": ["duplicate"]}, {"error": "secret-from-upstream"}, {"extra": "private-code"}])
def test_callback_rejects_invalid_bindings_without_exchange(tmp_path, change):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        params, _ = await begin(service, server)
        before = len(server.calls)
        with pytest.raises(PeerProviderError) as error:
            await service.finish_login("test", {**params, **change})
        assert "secret" not in str(error.value) and "private-code" not in str(error.value)
        assert len(server.calls) == before
    asyncio.run(scenario())


def test_callback_expiry_replay_and_pkce(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        params, started = await begin(service, server)
        assert "code_challenge_method=S256" in started["browser_url"]
        assert "code_verifier" not in started["browser_url"]
        await service.finish_login("test", params)
        with pytest.raises(PeerProviderError):
            await service.finish_login("test", params)
        params, _ = await begin(service, server)
        server.clock[0] += 601
        with pytest.raises(PeerProviderError):
            await service.finish_login("test", params)
        assert service.status("test")["status"] == "signed_out"
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", [lambda d: {**d, "issuer": "https://other.test"}, lambda d: {**d, "token_endpoint": "https://other.test/token"}, lambda d: {**d, "authorization_response_iss_parameter_supported": False}, lambda d: {**d, "code_challenge_methods_supported": ["plain"]}])
def test_discovery_rejects_unsafe_metadata(tmp_path, mutation):
    server = FakeServer()
    service = node(tmp_path, server)
    server.response_filter = lambda target, value: mutation(value) if target.endswith("oauth-authorization-server") else value
    with pytest.raises(PeerProviderError):
        asyncio.run(service.start_login("test"))
    assert not any(target.startswith("https://other.test") for _, target, *_ in server.calls)


@pytest.mark.parametrize("mutation", [{"resource": ORIGIN + "/mcp"}, {"scope": "other.read"}, {"token_type": "MAC"}, {"expires_in": -1}, {"access_token": "bad\nsecret"}])
def test_token_resource_scope_and_format_are_bound(tmp_path, mutation):
    server = FakeServer()
    service = node(tmp_path, server)
    server.response_filter = lambda target, value: {**value, **mutation} if target.endswith("/token") else value
    with pytest.raises(PeerProviderError):
        asyncio.run(login(service, server))
    assert service.status("test")["status"] == "signed_out"


def test_resource_isolation(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    second = ORIGIN + "/second"
    service.configure("second", {**MANIFEST, "url": second})
    async def scenario():
        await login(service, server)
        await login(service, server, "second", "second")
        await service.authorize(second, service.store.peer_id, service.store.peer_id, "message.text")
        call = server.calls[-1]
        token = call[2]["Authorization"].removeprefix("Bearer ")
        assert server.tokens[token][0] == second
    asyncio.run(scenario())


def test_concurrent_refresh_rotates_once_and_revoke_clears_local_state(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        server.clock[0] += 3571
        await asyncio.gather(service.directory("test"), service.directory("test"))
        assert server.refreshes == 1
        assert service.cached_peers()
        result = await service.logout("test")
        assert result["revocation_confirmed"] and not result["bound"]
        assert not service.cached_peers()
        with pytest.raises(PeerProviderError):
            await service.authorize(RESOURCE, service.store.peer_id, service.store.peer_id, "message.text")
    asyncio.run(scenario())


def test_401_refreshes_once_and_repeated_failure_fails_closed(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        original = server.respond
        def response(method, target, headers, body, data):
            if target.endswith("/directory"):
                raise ProviderHttpError(401)
            return original(method, target, headers, body, data)
        server.respond = response
        with pytest.raises(PeerProviderError):
            await service.directory("test")
        assert server.refreshes == 1
        assert service.status("test")["status"] == "signed_out"
    asyncio.run(scenario())


def test_directory_whitelist_and_bad_directory_evicts_cache(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        await service.publish("test", ["model-a"])
        def extra(target, value):
            if target.endswith("/directory"):
                value["access_token"] = "private-access"
                value["members"][0]["email"] = "private-email"
                value["models"][0]["service"]["api_key"] = "private-key"
                value["models"][0]["capacity"]["secret"] = "private-key"
            return value
        server.response_filter = extra
        safe = await service.directory("test")
        assert "private-" not in json.dumps(safe)
        server.response_filter = lambda target, value: {**value, "resource": "https://evil.test"} if target.endswith("/directory") else value
        with pytest.raises(PeerProviderError):
            await service.directory("test")
        assert not service.cached_peers()
    asyncio.run(scenario())


def test_publish_requires_local_selection_generation_and_fails_closed_on_deselect(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        before = len(server.calls)
        with pytest.raises(PeerProviderError):
            await service.publish("test", ["invented"])
        assert len(server.calls) == before
        await service.publish("test", ["model-a"])
        old = model()["model_binding"]
        assert service.locally_published(RESOURCE, "model-a", old)
        assert not service.locally_published(RESOURCE, "model-a", {**old, "model_generation": "c" * 32})
        server.fail = lambda target: target.endswith("/models")
        with pytest.raises(PeerProviderError):
            await service.publish("test", [])
        assert not service.locally_published(RESOURCE, "model-a", old)
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["disable", "instance", "remove", "url"])
def test_extension_lifecycle_invalidates_tokens_and_cache(tmp_path, mutation):
    server = FakeServer()
    records = [{"plugin_id": "test", "instance_id": "first", "enabled": True, "peer_provider": MANIFEST}]
    service = node(tmp_path, server, extension_source=lambda: records)
    async def scenario():
        await login(service, server)
        await service.directory("test")
        if mutation == "remove":
            records.clear()
        elif mutation == "disable":
            records[0]["enabled"] = False
        elif mutation == "instance":
            records[0]["instance_id"] = "new"
        else:
            records[0]["peer_provider"] = {**MANIFEST, "url": ORIGIN + "/new"}
        assert not service.cached_peers()
        with pytest.raises(PeerProviderError):
            await service.authorize(RESOURCE, service.store.peer_id, service.store.peer_id, "message.text")
    asyncio.run(scenario())


def test_late_result_cannot_restore_logged_out_cache(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait(target):
            if target.endswith("/directory"):
                entered.set()
                await release.wait()
        server.wait_request = wait
        task = asyncio.create_task(service.directory("test"))
        await entered.wait()
        await service.logout("test")
        release.set()
        with pytest.raises(PeerProviderError):
            await task
        assert not service.cached_peers()
    asyncio.run(scenario())


def test_upstream_exception_never_leaks_and_routes_are_owner_only(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    app = FastAPI()
    def owner(request):
        if request.headers.get("X-Test-Owner") != "yes":
            raise HTTPException(403, "owner_required")
    install_peer_provider_routes(app, service, owner)
    with TestClient(app) as client:
        assert client.get("/api/local/peer-providers/test/status").status_code == 403
        response = client.get("/api/local/peer-providers/test/status", headers={"X-Test-Owner": "yes"})
        assert response.json()["plugin_id"] == "test"
        async def bad(*args, **kwargs):
            raise ValueError("private-token code=private-code secret=private-secret")
        service.oauth.transport = bad
        response = client.post("/api/local/peer-providers/test/login/start", headers={"X-Test-Owner": "yes"})
        assert response.status_code == 409 and "private-" not in response.text


def test_default_transport_rejects_redirect_and_oversize_without_network(monkeypatch):
    original = httpx.AsyncClient
    for response in (httpx.Response(302, headers={"Location": "https://evil.test"}), httpx.Response(200, content=b"x" * 1048577)):
        monkeypatch.setattr(httpx, "AsyncClient", lambda value=response, **kwargs: original(transport=httpx.MockTransport(lambda request: value), **kwargs))
        with pytest.raises(PeerProviderError):
            asyncio.run(request_json("GET", RESOURCE))


def test_public_model_whitelist_drops_nested_credentials():
    record = model()
    record["secret"] = "private-key"
    record["service"]["pricing"]["secret"] = "private-key"
    assert "private-key" not in json.dumps(model_record(record))


def test_real_public_status_and_float_timeout_survive_projection():
    from rynmesh.llm_package.manifest import LLMPackageManifest
    from rynmesh.llm_package.routes import ProviderService

    provider = object.__new__(ProviderService)
    provider.manifest = LLMPackageManifest(package_id="model-a", mode="openai_compatible", public_model_alias="Local A", base_url="http://127.0.0.1:1234", runtime_api_key="private-runtime", timeout_seconds=120.0)
    provider.adapter = SimpleNamespace(health=lambda: {"ok": True}, chat=lambda: None, model_binding=model()["model_binding"])
    provider.store = SimpleNamespace()
    provider.messaging_key = X25519PrivateKey.generate()
    provider.accepting_orders, provider._running = False, 0
    projected = model_record(provider.public_status())
    assert projected["service"]["timeout_seconds"] == 120.0
    assert projected["service"]["pricing"]["currency"] == "DEV_TASK_BALANCE"
    assert "private-runtime" not in json.dumps(projected)
    assert "base_url" not in projected["service"]


def test_real_loopback_callback_rejects_duplicates_and_never_echoes_code(tmp_path, no_network):
    server = FakeServer()
    service = node(tmp_path, server)
    service.oauth.callback_factory = service.oauth._listen
    async def scenario():
        params, response = await begin(service, server)
        redirect = parse_qs(urlsplit(response["browser_url"]).query)["redirect_uri"][0]
        port = urlsplit(redirect).port
        no_network.add(("127.0.0.1", port))
        async def get(query, host=None):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((f"GET /callback?{query} HTTP/1.1\r\nHost: {host or '127.0.0.1:' + str(port)}\r\n\r\n").encode())
            await writer.drain()
            value = await reader.read()
            writer.close()
            await writer.wait_closed()
            return value
        rejected = await get(urlencode(params) + "&state=duplicate")
        assert b"400 Bad Request" in rejected and b"private-code" not in rejected
        wrong_host = await get(urlencode(params), "attacker.test")
        assert b"400 Bad Request" in wrong_host
        accepted = await get(urlencode(params))
        assert b"200 OK" in accepted and b"private-code" not in accepted
        assert service.status("test")["bound"]
        service.close()
    asyncio.run(scenario())


def test_cancelled_refresh_waiter_does_not_leak_serialization_lock(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        held = threading.Lock()
        held.acquire()
        service._requests["test"] = held
        task = asyncio.create_task(service.directory("test"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        held.release()
        await asyncio.wait_for(service.directory("test"), 1)
    asyncio.run(scenario())


def test_relogin_revokes_previous_grant_and_denied_callback_ends_pending(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    async def scenario():
        await login(service, server)
        old = copy.deepcopy(service._rows["test"]["session"])
        params, _ = await begin(service, server)
        assert old["refresh_token"] in server.revoked and old["access_token"] in server.revoked
        with pytest.raises(PeerProviderError):
            await service.finish_login("test", {"state": params["state"], "iss": ORIGIN, "error": "access_denied"})
        assert service.status("test")["status"] == "signed_out"
    asyncio.run(scenario())
