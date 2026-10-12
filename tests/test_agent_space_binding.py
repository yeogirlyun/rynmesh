"""Real local space membership, signed catalogs and sealed inference; no network."""
import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_integrations import add_source
from test_personal_space import join, nodes  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.peer_binding import expected, revision
from rynmesh.llm_package.routes import _expires, install_llm_routes
from rynmesh.llm_package.task_protocol import TaskProtocolError, open_task, seal_task
from rynmesh.services import peer_box


@pytest.fixture
def space_pair(nodes):  # noqa: F811
    root, child, work, stranger = nodes
    root.create("Isolated test space")
    join(root, child)
    apps, keys = [], []
    for space in (root, child):
        space.store.personal_space = space
        space.set_policy("space")
        key = peer_box.load_or_create_messaging_key(space.store.home / "messaging.x25519")
        app = FastAPI()
        install_llm_routes(app, store=space.store, home=space.store.home, messaging_key=key,
                           resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        apps.append(app)
        keys.append(key)
    services = apps[0].state.llm_agent_services
    record, _ = add_source(apps[0], models=["a", "b"])
    sources = services["source_store"]
    record["shared_models"] = ["a"]
    sources.save(record)
    provider = services["provider_for"](sources.model_service_id(record, "a"))
    traces = []
    class Inner:
        def health(self):
            return {"ok": True}
        def chat(self, body, **kwargs):
            traces.append(copy.deepcopy(body))
            return {"text": "current only", "message": {"role": "assistant", "content": "current only"},
                    "input_tokens": 2, "output_tokens": 2, "duration_ms": 1, "finish_reason": "stop"}
    provider.adapter.inner = Inner()
    publisher = services["space_catalog"]
    consumer = apps[1].state.llm_agent_services["space_catalog"]
    tick = [0]
    def exchange(peer, wire):
        # Real endpoint validation and real signed/encrypted body.
        tick[0] += 2
        publisher.clock = lambda: __import__("time").time() + tick[0]
        with TestClient(apps[0]) as client:
            response = client.post("/api/peer/llm/space-catalog", json=wire)
        response.raise_for_status()
        return response.json()
    consumer.exchange = exchange
    locator = {"peer_id": root.store.peer_id, "node_messaging_pub": peer_box.public_key_b64(keys[0])}
    yield SimpleNamespace(root=root, child=child, work=work, stranger=stranger, apps=apps, keys=keys,
        sources=sources, record=record, provider=provider, traces=traces, publisher=publisher, consumer=consumer, locator=locator)
    for app in apps:
        app.state.llm_shutdown()


def request(p, bound, task="space-call", content="isolated"):
    return seal_task(body={"task_id": task, "service_id": p.provider.manifest.package_id,
        "chat": {"messages": [{"role": "user", "content": content}], "max_tokens": 10}, "max_tokens": 10,
        "expected_model_binding": bound, "max_amount": 0.01,
        "reply_messaging_pub": peer_box.public_key_b64(p.keys[1])}, task_id=task, kind="llm_request",
        sender_peer_id=p.child.store.peer_id, recipient_peer_id=p.root.store.peer_id,
        sender_signing_key=p.child.store.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(p.keys[0]), expires_at=_expires(300)).to_dict()


def response(p, wire):
    return open_task(wire, recipient_peer_id=p.child.store.peer_id,
                     recipient_messaging_key=p.keys[1], expected_kind="llm_response")[1]


def test_space_catalog_signed_metadata_only_and_stateless_actual_dispatch(space_pair):
    p = space_pair
    rows = p.consumer.refresh(p.locator)
    assert len(rows) == 1  # The unshared sibling is absent.
    bound = expected(rows[0], p.child.store.peer_id)
    assert bound and "ai_permission" not in bound and bound["space_permission"]["space_id"]
    assert "secret-do-not-export" not in str(rows)
    for n, content in enumerate(("first history", "second history")):
        result = response(p, p.provider.handle(request(p, bound, "space-call-" + str(n), content)))
        assert result["state"] == "succeeded", result
    assert [trace["messages"] for trace in p.traces] == [[{"role": "user", "content": "first history"}], [{"role": "user", "content": "second history"}]]


def test_both_nodes_policy_aba_invalidates_old_destination(space_pair):
    p = space_pair
    row = p.consumer.refresh(p.locator)[0]
    original = revision(row, p.child.store.peer_id)
    bound = expected(row, p.child.store.peer_id)
    p.child.set_policy("local")
    assert not p.consumer.records()
    p.child.set_policy("space")
    assert not p.consumer.records()
    fresh = p.consumer.refresh(p.locator)[0]
    assert revision(fresh, p.child.store.peer_id) != original
    p.root.set_policy("local")
    p.root.set_policy("space")
    rejected = response(p, p.provider.handle(request(p, bound)))
    assert rejected["error_code"] == "ai_permission_denied" and not p.traces


def test_space_revoke_between_outer_check_and_actual_admission(space_pair):
    p = space_pair
    bound = expected(p.consumer.refresh(p.locator)[0], p.child.store.peer_id)
    original = p.provider.adapter.call_bound
    def race(*args, **kwargs):
        p.root.set_policy("local")
        p.root.set_policy("space")
        return original(*args, **kwargs)
    p.provider.adapter.call_bound = race
    result = response(p, p.provider.handle(request(p, bound)))
    assert result["error_code"] == "ai_permission_denied" and not p.traces


def test_space_member_snapshot_change_invalidates_both_catalog_and_provider(space_pair):
    p = space_pair
    bound = expected(p.consumer.refresh(p.locator)[0], p.child.store.peer_id)
    join(p.root, p.work)
    p.child._accept_snapshot(p.root.data["snapshot"], p.root.status()["space"])
    assert not p.consumer.records()
    assert response(p, p.provider.handle(request(p, bound)))["error_code"] == "ai_permission_denied"
    assert not p.traces


def test_space_catalog_rejects_replay_tampering_and_mixed_permission(space_pair):
    p = space_pair
    real = p.consumer.exchange
    captured = []
    def capture(peer, wire):
        result = real(peer, wire)
        captured.append(result)
        return result
    p.consumer.exchange = capture
    row = p.consumer.refresh(p.locator)[0]
    p.consumer.exchange = lambda *_: captured[0]
    with pytest.raises(TaskProtocolError, match="space_catalog_refresh_failed"):
        p.consumer.refresh(p.locator)
    assert not p.consumer.records()
    row["ai_permission"] = {"relationship_id": "a" * 32, "revision": 1}
    assert expected(row, p.child.store.peer_id) is None
    with TestClient(p.apps[0]) as client:
        invalid = copy.deepcopy(captured[0])
        assert client.post("/api/peer/llm/space-catalog", json=invalid).status_code == 403
        assert client.post("/api/peer/llm/space-catalog", content="x" * 9000).status_code == 413


def test_space_real_consumer_bridge_uses_authenticated_key_and_rejects_local_aba(space_pair, monkeypatch):
    p = space_pair
    p.consumer.refresh(p.locator)
    envelopes = []
    async def exchange(*, signed_request, **kwargs):
        envelopes.append(copy.deepcopy(signed_request))
        return p.provider.handle(signed_request), {"transport": "local_signed_fixture", "relay_used": False}
    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", exchange)
    bridge, _, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    model = next(m for m in catalog.list_models() if m["source"] == "peer")
    destination = catalog.destination(model)
    assert bridge.complete(model["id"], [{"role": "user", "content": "This task only"}], [], lambda: False,
                           expected_destination=destination)["message"]["content"] == "current only"
    p.child.set_policy("local")
    p.child.set_policy("space")
    p.consumer.refresh(p.locator)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        bridge.complete(model["id"], [{"role": "user", "content": "Must not leave"}], [], lambda: False,
                        expected_destination=destination)
    assert len(envelopes) == len(p.traces) == 1


def test_space_catalog_limits_and_stranger_cannot_read_metadata(space_pair):
    p = space_pair
    captured = []
    def capture(peer, wire):
        captured.append(wire)
        return p.publisher.respond(wire)
    p.consumer.exchange = capture
    p.consumer.refresh(p.locator)
    with pytest.raises(TaskProtocolError, match="rate_limited"):
        p.publisher.respond(captured[0])
    stranger_key = peer_box.load_or_create_messaging_key(p.stranger.store.home / "messaging.x25519")
    task = "stranger-metadata"
    wire = seal_task(body={"task_id": task, "reply_messaging_pub": peer_box.public_key_b64(stranger_key)}, task_id=task,
        kind="space_model_catalog_request", sender_peer_id=p.stranger.store.peer_id, recipient_peer_id=p.root.store.peer_id,
        sender_signing_key=p.stranger.store.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(p.keys[0]),
        expires_at=_expires(300)).to_dict()
    with TestClient(p.apps[0]) as client:
        assert client.post("/api/peer/llm/space-catalog", json=wire).status_code == 403
    p.publisher.last_requests.clear()
    p.publisher.providers = lambda: [p.provider] * 101
    with pytest.raises(TaskProtocolError, match="capacity_exhausted"):
        p.publisher.respond(captured[0])
    assert not p.traces


def test_space_leave_rejoin_cannot_restore_prior_binding(space_pair):
    p = space_pair
    old = p.consumer.refresh(p.locator)[0]
    old_revision = revision(old, p.child.store.peer_id)
    p.child.leave()
    assert not p.consumer.records()
    p.root.act("remove", {"peer_id": p.child.store.peer_id})
    p.child.last_poll = p.root.last_poll = 0
    join(p.root, p.child)
    assert p.child.status()["membership"] == "active", p.child.status()
    p.child.set_policy("space")
    fresh = p.consumer.refresh(p.locator)[0]
    assert revision(fresh, p.child.store.peer_id) != old_revision
    assert response(p, p.provider.handle(request(p, expected(old, p.child.store.peer_id))))["error_code"] == "ai_permission_denied"
    assert not p.traces


def test_native_model_uses_same_signed_space_chain_and_dispatch_rule_gate(space_pair, tmp_path):
    from test_native_model import tiny_model

    from rynmesh.llm_package.native_model import validate_model
    p = space_pair
    model = p.apps[0].state.native_model
    original = tiny_model(tmp_path)
    model.model_path.parent.mkdir(parents=True)
    model.model_path.write_bytes(original.read_bytes())
    model.metadata = validate_model(original)
    model.config.update(installed=True, sharing=True)
    model._save()
    model._update_manifest()
    model.state, model.process = "running", SimpleNamespace(poll=lambda: None)
    model.upstream = p.provider.adapter.inner
    p.root.store.llm_tool_capabilities.save(model.manifest, "supported")
    p.provider = p.apps[0].state.llm_agent_services["provider_for"](model.manifest.package_id)
    row = next(row for row in p.consumer.refresh(p.locator) if row["service"]["package_id"] == model.manifest.package_id)
    bound = expected(row, p.child.store.peer_id)
    assert response(p, p.provider.handle(request(p, bound, "native-space")))["state"] == "succeeded"
    old = model.adapter.call_bound
    def race(*args, **kwargs):
        model.configure_model({"expected_rule_revision": model.status()["model_settings"]["rule_revision"],
                               "configuration": {"requests_per_day": 1}})
        return old(*args, **kwargs)
    model.adapter.call_bound = race
    assert response(p, p.provider.handle(request(p, bound, "native-stale")))["error_code"] == "model_configuration_changed"
    assert len(p.traces) == 1
