"""Real local identity/grant/envelope boundaries; fake in-memory network only."""
import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_integrations import add_source
from test_ai_catalog import setup as friend_setup
from test_friends import _node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.store import AgentError
from rynmesh.ai_access.catalog import FriendAICatalog
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package.peer_binding import expected, revision
from rynmesh.llm_package.routes import _expires, install_llm_routes
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.fixture
def paired(tmp_path):
    mesh, alice, bob, grants, publisher, consumer, clock, captured, post = friend_setup(tmp_path)
    pstore = RynmeshStore(home=alice.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=pstore, home=pstore.home, messaging_key=alice.messaging_private,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    for model in record["models"]:
        record = sources.read()[record["id"]]
        sources.configure_model(record["id"], {"model_id": model,
            "expected_rule_revision": record["model_settings"][model]["rule_revision"], "configuration": {"share_scope": "friends"}})
    record = sources.read()[record["id"]]
    record["shared_models"] = ["a", "b"]
    sources.save(record)
    services = app.state.llm_agent_services
    providers = {model: services["provider_for"](sources.model_service_id(record, model)) for model in record["models"]}
    for service in providers.values():
        service.access_check = grants.authorize
    publisher.provider = lambda: [p.public_status() for p in providers.values()]
    rid = alice.store.relationship_for_peer(bob.peer_id)["relationship_id"]
    grant = grants.set(providers["a"].manifest.package_id, rid, allowed=True, expected_revision=0)
    traces = []
    class Inner:
        def chat(self, body, **kwargs):
            traces.append(copy.deepcopy(body))
            return {"text": "only current", "message": {"role": "assistant", "content": "only current"}, "finish_reason": "stop",
                "input_tokens": 2, "output_tokens": 2, "duration_ms": 1, "reported_usage": {"input_tokens": 2, "output_tokens": 2}}
        def health(self):
            return {"ok": True}
    for service in providers.values():
        service.adapter.inner = Inner()
    yield SimpleNamespace(app=app, store=pstore, alice=alice, bob=bob, grants=grants, sources=sources, record=record,
        publisher=publisher, consumer=consumer, providers=providers, rid=rid, grant=grant, traces=traces, mesh=mesh, post=post)
    app.state.llm_shutdown()


def request(p, task_id, binding, content, *, permission=None):
    return seal_task(body={"task_id": task_id, "service_id": p.providers["a"].manifest.package_id,
        "chat": {"messages": [{"role": "user", "content": content}], "max_tokens": 10}, "max_tokens": 10,
        "expected_model_binding": binding, "ai_permission": permission or binding["ai_permission"], "max_amount": 0.01,
        "reply_messaging_pub": peer_box.public_key_b64(p.bob.messaging_private)}, task_id=task_id, kind="llm_request",
        sender_peer_id=p.bob.peer_id, recipient_peer_id=p.alice.peer_id, sender_signing_key=p.bob.identity_private,
        recipient_messaging_pub=peer_box.public_key_b64(p.alice.messaging_private), expires_at=_expires(300)).to_dict()


def response(p, wire):
    return open_task(wire, recipient_peer_id=p.bob.peer_id, recipient_messaging_key=p.bob.messaging_private, expected_kind="llm_response")[1]


def test_authenticated_catalog_matrix_and_receiver_bound_generations(paired):
    p = paired
    rows = p.consumer.refresh(p.alice.peer_id)["services"]
    assert len(rows) == 1 and rows[0]["binding_verified"]
    assert expected(rows[0], "unrelated-consumer") is None
    original = revision(rows[0], p.bob.peer_id)
    p.grants.set(p.providers["b"].manifest.package_id, p.rid, allowed=True, expected_revision=0)
    assert len(p.consumer.refresh(p.alice.peer_id)["services"]) == 2
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=False, expected_revision=1)
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=True, expected_revision=2)
    renewed = p.consumer.refresh(p.alice.peer_id)["services"][0]
    assert revision(renewed, p.bob.peer_id) != original


def test_actual_dispatch_revoke_regrant_old_generation_and_stateless_calls(paired):
    p = paired
    row = p.consumer.refresh(p.alice.peer_id)["services"][0]
    bound = expected(row, p.bob.peer_id)
    for index, secret in enumerate(("session-one-only", "session-two-only")):
        result = response(p, p.providers["a"].handle(request(p, "call" + str(index), bound, secret)))
        assert result["state"] == "succeeded" and result["reported_usage"]["input_tokens"] == 2
    assert p.traces[0]["messages"] == [{"role": "user", "content": "session-one-only"}]
    assert p.traces[1]["messages"] == [{"role": "user", "content": "session-two-only"}]
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=False, expected_revision=1)
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=True, expected_revision=2)
    assert response(p, p.providers["a"].handle(request(p, "old-permission", bound, "never")))["state"] == "rejected"
    newrow = p.consumer.refresh(p.alice.peer_id)["services"][0]
    current = expected(newrow, p.bob.peer_id)
    raw = p.sources.read()[p.record["id"]]
    p.sources.configure_model(raw["id"], {"model_id": "a", "expected_rule_revision": raw["model_settings"]["a"]["rule_revision"],
        "configuration": {"max_output_tokens": 999}})
    rejected = response(p, p.providers["a"].handle(request(p, "old-model", current, "never")))
    assert rejected["error_code"] == "model_configuration_changed" and len(p.traces) == 2


def test_revoke_between_outer_auth_and_actual_adapter_admission(paired):
    p = paired
    row = p.consumer.refresh(p.alice.peer_id)["services"][0]
    bound = expected(row, p.bob.peer_id)
    service = p.providers["a"]
    original = service.adapter.call_bound
    def race(*args, **kwargs):
        p.grants.set(service.manifest.package_id, p.rid, allowed=False, expected_revision=1)
        return original(*args, **kwargs)
    service.adapter.call_bound = race
    result = response(p, service.handle(request(p, "race", bound, "never")))
    assert result["error_code"] == "ai_permission_denied" and not p.traces


def test_catalog_flag_is_required_for_peer_resource_binding(paired):
    p = paired
    row = p.consumer.refresh(p.alice.peer_id)["services"][0]
    services = {"source_store": p.sources, "discover": lambda _: [row]}
    catalog = ModelCatalog(services, SimpleNamespace(peer_id=p.bob.peer_id))
    peer = next(m for m in catalog.list_models() if m["source"] == "peer")
    assert peer["destination_binding_supported"] and peer["destination_revision"]
    row["binding_verified"] = False
    assert not next(m for m in catalog.list_models() if m["source"] == "peer")["destination_revision"]


def test_real_consumer_bridge_seals_exact_binding_and_rejects_refresh_change(paired, monkeypatch, tmp_path):
    p = paired
    p.consumer.refresh(p.alice.peer_id)
    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    install_llm_routes(app, store=store, home=store.home, messaging_key=p.bob.messaging_private,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    envelopes = []
    async def exchange(*, signed_request, **kwargs):
        _, body = open_task(signed_request, recipient_peer_id=p.alice.peer_id, recipient_messaging_key=p.alice.messaging_private, expected_kind="llm_request")
        envelopes.append(copy.deepcopy(body))
        return p.providers["a"].handle(signed_request), {"transport": "local_test_signed_p2p", "relay_used": False}
    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", exchange)
    try:
        bridge, _, catalog = build_agent_integrations(app, store, store.home)
        model = next(m for m in catalog.list_models() if m["source"] == "peer")
        destination = catalog.destination(model)
        result = bridge.complete(model["id"], [{"role": "user", "content": "Private authorized local fixture"}], [],
                                 lambda: False, expected_destination=destination)
        assert result["message"]["content"] == "only current"
        assert envelopes[0]["expected_model_binding"]["consumer_peer_id"] == p.bob.peer_id
        assert envelopes[0]["expected_model_binding"]["model_binding"] == p.providers["a"].adapter.model_binding
        assert result["usage"] == {"input_tokens": 2, "output_tokens": 2}
        p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=False, expected_revision=1)
        p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=True, expected_revision=2)
        p.consumer.refresh(p.alice.peer_id)
        with pytest.raises(AgentError, match="agent_model_destination_changed"):
            bridge.complete(model["id"], [{"role": "user", "content": "Never sent"}], [], lambda: False, expected_destination=destination)
        assert len(envelopes) == 1
    finally:
        app.state.llm_shutdown()


def test_owner_api_grants_are_per_model_and_per_receiver(paired, tmp_path):
    p = paired
    carol = _node(tmp_path, "Carol", 18083, p.mesh)
    carol.join(p.alice.create_invite()["invite_uri"])
    carol.post_json = p.post
    crid = p.alice.store.relationship_for_peer(carol.peer_id)["relationship_id"]
    other = FriendAICatalog(friends=lambda: carol, grants=lambda: None, provider=lambda: None)
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    install_ai_access(p.app, home=p.store.home, local_control=guard, relationship=p.alice.store.relationship)
    with TestClient(p.app) as client:
        path = f"/api/local/ai-access/{p.providers['b'].manifest.package_id}/{crid}"
        assert client.put(path, json={"allowed": True, "expected_revision": 0}).status_code == 401
        assert client.put(path, headers={"x-owner": "yes"}, json={"allowed": True, "expected_revision": 0}).status_code == 200
        bob_models = p.consumer.refresh(p.alice.peer_id)["services"]
        carol_models = other.refresh(p.alice.peer_id)["services"]
        assert [r["service"]["package_id"] for r in bob_models] == [p.providers["a"].manifest.package_id]
        assert [r["service"]["package_id"] for r in carol_models] == [p.providers["b"].manifest.package_id]
        assert expected(bob_models[0], carol.peer_id) is None
        assert expected(carol_models[0], p.bob.peer_id) is None
