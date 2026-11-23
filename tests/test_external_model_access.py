"""Real node envelopes and inference admission; all transport is test-local."""
import copy
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from test_agent_integrations import add_source

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.drop.peers import make_hello
from rynmesh.llm_package.peer_binding import expected
from rynmesh.llm_package.routes import _expires, install_llm_routes
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

RESOURCE = "https://provider.test/connect"


class Authority:
    def __init__(self):
        self.plugin_id = "other-workspace"
        self.allowed = True
        self.selected = {}
        self.models = []
        self.calls = []

    def locally_published(self, resource, service_id, binding):
        return resource == RESOURCE and self.selected.get(service_id) == binding

    async def authorize(self, resource, sender, recipient, capability, service_id=None):
        self.calls.append((resource, sender, recipient, capability, service_id))
        if not self.allowed or resource != RESOURCE or capability != "model.invoke":
            raise ValueError("denied")
        return {"allowed": True, "plugin_id": self.plugin_id}

    def cached_models(self):
        return copy.deepcopy(self.models)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    owned_ports = set()
    original_bind, original_connect, original_resolve = socket.socket.bind, socket.socket.connect, socket.getaddrinfo

    def local_bind(sock, address):
        result = original_bind(sock, address)
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            owned_ports.add(sock.getsockname()[1])
        return result

    def no_network(*args, **kwargs):
        raise AssertionError("unexpected network")

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] in owned_ports:
            return original_connect(sock, address)
        return no_network()

    def resolve(host, *args, **kwargs):
        if host in {"127.0.0.1", "::1"}:
            return original_resolve(host, *args, **kwargs)
        return no_network()
    monkeypatch.setattr(socket.socket, "bind", local_bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr("rynmesh.llm_package.routes.warm_stun_choice", lambda: None)
    nodes = []
    for name in ("provider", "consumer"):
        store = RynmeshStore(home=tmp_path / name, network_dir=tmp_path / "network")
        key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
        authority = Authority()
        store.peer_providers = authority
        app = FastAPI()
        app.state.peer_providers = authority
        install_llm_routes(app, store=store, home=store.home, messaging_key=key,
            resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nodes.append(SimpleNamespace(store=store, key=key, authority=authority, app=app,
                                     services=app.state.llm_agent_services))
    p, c = nodes
    record, _ = add_source(p.app)
    source = p.services["source_store"]
    sid = source.model_service_id(record, record["models"][0])
    manager = p.services["provider_for"](sid)
    traces = []

    class Inner:
        def health(self):
            return {"ok": True}

        def chat(self, body, **kwargs):
            traces.append(copy.deepcopy(body))
            return {"text": "shared reply", "message": {"role": "assistant", "content": "shared reply"},
                    "finish_reason": "stop", "input_tokens": 2, "output_tokens": 2, "duration_ms": 1}

    manager.adapter.inner = Inner()
    binding = manager.adapter.model_binding
    p.authority.selected[sid] = binding
    row = {**manager.public_status(), "model_binding": binding, "peer_id": p.store.peer_id,
           "messaging_pub": peer_box.public_key_b64(p.key), "endpoints": ["http://127.0.0.1:28111"],
           "provider_resource": RESOURCE, "external_permission": {"resource": RESOURCE},
           "consumer_peer_id": c.store.peer_id, "binding_verified": True}
    c.authority.models = [row]
    catalog = c.services["external_catalog"]

    def hello(endpoint, method, path, **kwargs):
        nonce = parse_qs(urlsplit(path).query)["nonce"][0]
        return 200, make_hello(peer_id=p.store.peer_id, node_name="Provider", messaging_pub=peer_box.public_key_b64(p.key),
                               nonce=nonce, private_key_bytes=p.store.private_key_bytes)
    catalog.request = hello
    catalog.run_once()
    monkeypatch.setattr("rynmesh.llm_package.routes._peer_post_json",
                        lambda endpoint, path, wire, **kwargs: manager.handle(wire))
    result = SimpleNamespace(p=p, c=c, manager=manager, row=row, sid=sid, traces=traces,
                             binding=expected(row, c.store.peer_id), record=record)
    yield result
    for node in nodes:
        node.app.state.llm_shutdown()


def wire(pair, task="test", **overrides):
    p, c = pair.p, pair.c
    body = {"task_id": task, "service_id": pair.sid, "chat": {"messages": [{"role": "user", "content": "hello"}]},
            "max_tokens": 10, "max_amount": .01, "expected_model_binding": pair.binding,
            "external_permission": {"resource": RESOURCE}, "reply_messaging_pub": peer_box.public_key_b64(c.key), **overrides}
    return seal_task(body=body, task_id=task, kind="llm_request", sender_peer_id=c.store.peer_id,
        recipient_peer_id=p.store.peer_id, sender_signing_key=c.store.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(p.key), expires_at=_expires(300)).to_dict()


def opened(pair, encrypted):
    return open_task(encrypted, recipient_peer_id=pair.c.store.peer_id,
        recipient_messaging_key=pair.c.key, expected_kind="llm_response")[1]


def test_external_models_use_existing_inference_without_personal_trust(pair):
    assert not pair.manager.accepting_orders
    result = opened(pair, pair.manager.handle(wire(pair)))
    assert result["state"] == "succeeded" and result["output"] == "shared reply"
    assert not pair.p.store.personal_space.allows_ai(pair.c.store.peer_id)
    assert all(call[3] == "model.invoke" for call in pair.p.authority.calls)


@pytest.mark.parametrize("change", ["revoke", "unpublish", "resource", "generation", "mixed", "unbound"])
def test_invalid_authority_cannot_dispatch(pair, change):
    overrides = {}
    if change == "revoke":
        pair.p.authority.allowed = False
    elif change == "unpublish":
        pair.p.authority.selected.clear()
    elif change == "resource":
        overrides["external_permission"] = {"resource": "https://unconfigured.test/connect"}
    elif change == "generation":
        bound = copy.deepcopy(pair.binding)
        bound["model_binding"]["model_generation"] = "f" * 32
        overrides["expected_model_binding"] = bound
    elif change == "mixed":
        overrides["ai_permission"] = {"relationship_id": "a" * 32, "revision": 1}
    else:
        overrides["expected_model_binding"] = None
    assert opened(pair, pair.manager.handle(wire(pair, **overrides)))["state"] == "rejected"
    assert pair.traces == []


def test_revoke_during_inference_withholds_result_and_cached_replay(pair):
    inner = pair.manager.adapter.inner
    original = inner.chat

    def revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        pair.p.authority.allowed = False
        return value
    inner.chat = revoke
    result = opened(pair, pair.manager.handle(wire(pair)))
    assert result["state"] != "succeeded" and "output" not in result
    assert opened(pair, pair.manager.handle(wire(pair)))["state"] == "rejected"


def test_consumer_bridge_model_selection_and_encrypted_direct_call(pair):
    bridge, _, catalog = build_agent_integrations(pair.c.app, pair.c.store, pair.c.store.home)
    model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
    assert model["id"].startswith("provider/") and model["recipient_access"]["kind"] == "provider"
    result = bridge.complete(model["id"], [{"role": "user", "content": "Private test"}], [], lambda: False,
                             expected_destination=catalog.destination(model))
    assert result["message"]["content"] == "shared reply"
    assert pair.traces[0]["messages"] == [{"role": "user", "content": "Private test"}]
    assert pair.c.authority.calls and pair.p.authority.calls


@pytest.mark.parametrize("entry", ["chat", "agent"])
@pytest.mark.parametrize("report_usage", [True, False])
def test_company_orders_do_not_access_task_balance(pair, monkeypatch, entry, report_usage):
    import asyncio
    from rynmesh.llm_package.task_balance import TaskBalanceLedger

    pair.c.authority.plugin_id = pair.p.authority.plugin_id = "company-workspace"
    if not report_usage:
        chat = pair.manager.adapter.inner.chat

        def without_usage(*args, **kwargs):
            result = chat(*args, **kwargs)
            return {k: v for k, v in result.items() if k not in {"input_tokens", "output_tokens"}}

        monkeypatch.setattr(pair.manager.adapter.inner, "chat", without_usage)

    def unexpected_balance(*args, **kwargs):
        pytest.fail("Company orders must not access the development balance")

    for operation in ("summary", "hold", "settle", "earn", "release"):
        monkeypatch.setattr(TaskBalanceLedger, operation, unexpected_balance)
    if entry == "chat":
        result = asyncio.run(pair.c.services["execute_order"]({
            "provider_peer_id": pair.p.store.peer_id, "service_id": pair.sid,
            "expected_model_binding": pair.binding, "transport": "direct",
            "chat": {"messages": [{"role": "user", "content": "hello"}]},
        }))
        assert result["message"]["content"] == "shared reply"
        assert result["amount"] == 0 and result["billing_basis"] == "company_free"
    else:
        bridge, _, catalog = build_agent_integrations(pair.c.app, pair.c.store, pair.c.store.home)
        model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
        result = bridge.complete(model["id"], [{"role": "user", "content": "hello"}], [],
                                 lambda: False, expected_destination=catalog.destination(model))
        assert result["message"]["content"] == "shared reply"
    assert len(pair.traces) == 1


def test_catalog_does_not_merge_identical_models_from_different_sources(pair):
    row = {**pair.row, "online": True}
    other = {**row, "provider_resource": "https://other.test/connect", "external_permission": {"resource": "https://other.test/connect"}}
    catalog = ModelCatalog({"source_store": pair.c.services["source_store"], "discover": lambda _: [row, other]}, pair.c.store)
    models = catalog.list_models()
    assert len(models) == 2 and models[0]["id"] != models[1]["id"]


def test_signed_probe_rejects_messaging_key_substitution(pair):
    catalog = pair.c.services["external_catalog"]
    bad = {**pair.row, "messaging_pub": peer_box.public_key_b64(pair.c.key)}
    assert catalog.refresh(bad) is False


def test_broken_peer_provider_preserves_local_service_catalog(pair, monkeypatch):
    from rynmesh.peer_providers.validation import PeerProviderError

    def unavailable():
        raise PeerProviderError("peer_provider_configuration_unavailable")

    monkeypatch.setattr(pair.p.authority, "cached_models", unavailable)
    endpoint = next(route.endpoint for route in pair.p.app.routes
                    if route.path == "/api/local/llm/services")
    rows = endpoint()["services"]
    assert any(row["service"]["package_id"] == pair.sid for row in rows)
    assert all(row.get("provider_resource") != RESOURCE for row in rows)

@pytest.mark.parametrize("stream", [False, True])
def test_company_relay_round_trip_without_public_registry(pair, monkeypatch, stream):
    import asyncio
    import threading

    client = pair.c.services["external_catalog"].company
    server = pair.p.services["external_catalog"].company
    pair.c.authority.plugin_id = pair.p.authority.plugin_id = "company-workspace"
    pair.row.update(company_transport=True, plugin_id="company-workspace")
    pair.c.authority.cached_peers = lambda: [pair.row]
    frames = {}
    workers = []
    closed = threading.Event()

    def channel(side):
        async def call(plugin, action, **fields):
            if action == "presence":
                return {"online": True}
            if action == "open":
                frames[fields["id"]] = {"c": [], "p": []}
                row = {"id": fields["id"], "source_peer": pair.c.store.peer_id,
                       "service_id": pair.sid, "mode": fields["mode"]}
                worker = threading.Thread(target=lambda: asyncio.run(server._serve(plugin, RESOURCE, row)))
                worker.start()
                workers.append(worker)
                return {}
            if action == "write":
                assert len(frames[fields["id"]][side]) == fields["seq"]
                frames[fields["id"]][side].append({"seq": fields["seq"], "bytes": fields["bytes"]})
                return {}
            if action == "read":
                return {"frames": frames[fields["id"]]["p" if side == "c" else "c"][fields["seq"] :]}
            if action == "close":
                closed.set()
                return {}
            raise AssertionError(action)
        return call

    monkeypatch.setattr(client, "call", channel("c"))
    monkeypatch.setattr(server, "call", channel("p"))
    monkeypatch.setenv("RYNMESH_LLM_TRANSPORT", "relay")
    registry_calls = []
    def no_registry(*args, **kwargs):
        registry_calls.append(kwargs.get("operation"))
        raise AssertionError("company transport used the public registry")
    monkeypatch.setattr(pair.c.store, "submit_work_order", no_registry)
    pair.row["service"]["capabilities"] = [*pair.row["service"]["capabilities"], "streaming"]
    events = []
    original_chat = pair.manager.adapter.inner.chat
    def reply(body, **options):
        if body.get("stream"):
            options["on_event"]({"choices": [{"index": 0, "delta": {"content": "shared reply"}}]})
        return original_chat(body, **options)
    monkeypatch.setattr(pair.manager.adapter.inner, "chat", reply)
    bridge, _, catalog = build_agent_integrations(pair.c.app, pair.c.store, pair.c.store.home)
    model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
    result = bridge.complete(model["id"], [{"role": "user", "content": "Private test"}],
                             [], lambda: False, on_event=events.append if stream else None, expected_destination=catalog.destination(model))
    assert result["message"]["content"] == "shared reply"
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    if stream:
        assert pair.traces[0]["stream"]
        assert any(e.get("type") == "text_delta" and e.get("text") == "shared reply" for e in events)
    assert not registry_calls
    assert len(frames) == 2 and all(session["p"] and session["c"] for session in frames.values())
    assert closed.is_set()
    assert len(pair.traces) == 1
