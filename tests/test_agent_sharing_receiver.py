"""Agent mutations -> signed receiver dispatch -> actual synthetic HTTP upstream.

All peers run in one process. This is neither physical-node nor real-model
acceptance; catalog exchange uses the existing authenticated in-memory mesh.
"""
import copy
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from test_ai_catalog import setup as friend_setup
from test_friends import _node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.ai_access.catalog import FriendAICatalog
from rynmesh.ai_access.routes import AIAccessState
from rynmesh.llm_package.peer_binding import expected
from rynmesh.llm_package.routes import _expires, install_llm_routes
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

_spec = importlib.util.spec_from_file_location("sharing_http_fixture", Path(__file__).parent / "fixtures/agent_acceptance/source_fixture.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.fixture
def sharing(tmp_path):
    mesh, owner, recipient, grants, publisher, consumer, _, _, post = friend_setup(tmp_path)
    other = _node(tmp_path, "Other", 18083, mesh)
    other.join(owner.create_invite()["invite_uri"])
    other.post_json = post
    other_catalog = FriendAICatalog(friends=lambda: other, grants=lambda: None, provider=lambda: None)
    store = RynmeshStore(home=owner.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    app.state.ai_access = AIAccessState(grants, lambda _: None)
    install_llm_routes(app, store=store, home=store.home, messaging_key=owner.messaging_private,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    services = app.state.llm_agent_services
    publisher.provider = lambda: [p.public_status() for p in services["active_source_managers"]().values()]
    sources = services["source_store"]
    with _module.FixtureSource(models=("model-a", "model-b")) as upstream:
        record = sources.prepare({"name": "Synthetic receiver upstream", "kind": "local", "base_url": upstream.url,
                                  "api_key": upstream.secret, "models": ["model-a", "model-b"]})
        record.update(status="ready", tool_support={"model-a": "supported", "model-b": "supported"})
        sources.save(record)
        _, tools, _ = build_agent_integrations(app, store, store.home)
        sid = sources.model_service_id(record, "model-b")
        rid = owner.store.relationship_for_peer(recipient.peer_id)["relationship_id"]
        try:
            yield SimpleNamespace(owner=owner, recipient=recipient, other=other, grants=grants, consumer=consumer,
                other_catalog=other_catalog, tools=tools, services=services, sources=sources, source_id=record["id"],
                sid=sid, model_id="local/" + sid, rid=rid, upstream=upstream)
        finally:
            app.state.llm_shutdown()


def mutate(p, name, args):
    return p.tools.execute(name, args, operation_id=uuid.uuid4().hex, cancelled=lambda: False,
                           expected_preview=p.tools.preview(name, args))


def dispatch(p, sender, permission, *, binding=None):
    task_id = uuid.uuid4().hex
    body = {"task_id": task_id, "service_id": p.sid,
            "chat": {"messages": [{"role": "user", "content": "Synthetic receiver probe"}], "max_tokens": 10},
            "max_tokens": 10, "max_amount": 0.01, "ai_permission": permission,
            "reply_messaging_pub": peer_box.public_key_b64(sender.messaging_private)}
    if binding is not None:
        body["expected_model_binding"] = binding
    wire = seal_task(body=body, task_id=task_id, kind="llm_request", sender_peer_id=sender.peer_id,
        recipient_peer_id=p.owner.peer_id, sender_signing_key=sender.identity_private,
        recipient_messaging_pub=peer_box.public_key_b64(p.owner.messaging_private), expires_at=_expires(300)).to_dict()
    response = p.services["provider_for"](p.sid).handle(wire)
    return open_task(response, recipient_peer_id=sender.peer_id, recipient_messaging_key=sender.messaging_private,
                     expected_kind="llm_response")[1]


def prepare(p):
    mutate(p, "ryn_set_model_sharing", {"model_id": p.model_id, "scope": "friends", "enabled": False})
    mutate(p, "ryn_set_friend_model_access", {"service_id": p.sid, "relationship_id": p.rid,
        "allowed": True, "expected_revision": 0})


def test_exact_recipient_dispatch_requires_sharing_and_grant_then_rejects_revoked_signature(sharing):
    p = sharing
    before = p.sources.read()[p.source_id]
    assert before["shared_models"] == []
    assert not p.consumer.refresh(p.owner.peer_id)["services"]
    prepare(p)
    permission = {"relationship_id": p.rid, "revision": 1}
    private = dispatch(p, p.recipient, permission)
    assert private["state"] == "rejected" and private["error_code"] == "service_paused"
    assert p.upstream.audit == []  # A saved grant alone must not reach HTTP inference.
    mutate(p, "ryn_set_model_sharing", {"model_id": p.model_id, "scope": "friends", "enabled": True})
    rows = p.consumer.refresh(p.owner.peer_id)["services"]
    assert len(rows) == 1 and rows[0]["service"]["package_id"] == p.sid and rows[0]["binding_verified"]
    assert not p.other_catalog.refresh(p.owner.peer_id)["services"]
    binding = expected(rows[0], p.recipient.peer_id)
    succeeded = dispatch(p, p.recipient, permission, binding=binding)
    assert succeeded["state"] == "succeeded" and succeeded["reported_usage"]["input_tokens"] == 8
    assert len(p.upstream.audit) == 1 and p.upstream.audit[0]["authenticated"]
    assert p.upstream.audit[0]["model"] == "model-b" and p.upstream.audit[0]["status"] == 200
    # Even an actual friend who knows the other peer's exact permission cannot use it.
    forged = dispatch(p, p.other, permission)
    assert forged["state"] == "rejected" and forged["error_code"] == "ai_permission_denied"
    mutate(p, "ryn_set_friend_model_access", {"service_id": p.sid, "relationship_id": p.rid,
        "allowed": False, "expected_revision": 1})
    revoked = dispatch(p, p.recipient, permission, binding=binding)
    assert revoked["state"] == "rejected" and revoked["error_code"] == "ai_permission_denied"
    assert not p.consumer.refresh(p.owner.peer_id)["services"] and len(p.upstream.audit) == 1
    after = p.sources.read()[p.source_id]
    assert after["model_settings"]["model-a"] == before["model_settings"]["model-a"]
    assert after["shared_models"] == ["model-b"]
    for field in ("base_url", "api_key", "models", "destination_revision"):
        assert after[field] == before[field]


def test_disable_model_blocks_current_signed_binding_without_rewriting_grants(sharing):
    p = sharing
    prepare(p)
    mutate(p, "ryn_set_model_sharing", {"model_id": p.model_id, "scope": "friends", "enabled": True})
    row = p.consumer.refresh(p.owner.peer_id)["services"][0]
    binding = expected(row, p.recipient.peer_id)
    grants_before = copy.deepcopy(p.grants.list())
    mutate(p, "ryn_set_model_sharing", {"model_id": p.model_id, "enabled": False})
    denied = dispatch(p, p.recipient, row["ai_permission"], binding=binding)
    assert denied["state"] == "rejected"
    assert denied["error_code"] in {"service_paused", "model_configuration_changed"}
    assert p.grants.list() == grants_before and not p.upstream.audit
