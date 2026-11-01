"""Actual encrypted stores, friend pairing and app business boundaries."""

import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_friends import Mesh, _node

from rynmesh.crypto import canonical_json
from rynmesh.node_apps.engine import AppError
from rynmesh.node_apps.routes import install_node_apps
from rynmesh.node_apps.service import PATH, NodeApps
from rynmesh.node_apps.store import AppStore


class AppMesh(Mesh):
    def __init__(self):
        super().__init__()
        self.apps = {}
        self.wires = []

    def post(self, endpoint, path, payload, headers, **kwargs):
        if path != PATH:
            return super().post(endpoint, path, payload, headers, **kwargs)
        self.wires.append(copy.deepcopy(payload))
        friend = self.nodes[endpoint]
        relation = friend.verify_request(path=path, body=canonical_json(payload), headers=headers)
        return self.apps[endpoint].respond(payload, relation)


@pytest.fixture
def mesh(tmp_path):
    transport = AppMesh()
    friends = [
        _node(tmp_path, n, 18870 + i, transport)
        for i, n in enumerate(("provider", "buyer", "other"))
    ]
    for friend in friends:
        transport.apps[friend.endpoint] = NodeApps(
            AppStore(friend.home, friend.messaging_private), friend.peer_id, lambda f=friend: f
        )
    provider, buyer, other = friends
    for friend in (buyer, other):
        friend.join(provider.create_invite()["invite_uri"])
    return SimpleNamespace(
        transport=transport,
        provider=transport.apps[provider.endpoint],
        buyer=transport.apps[buyer.endpoint],
        other=transport.apps[other.endpoint],
        friends=friends,
    )


def allow(mesh, caller, app):
    preview = mesh.provider.grant_preview(app, caller.peer_id, True)
    mesh.provider.grant(app, caller.peer_id, True, preview["preview_hash"])


def invoke(service, peer, app, action, args, operation_id=None):
    preview = service.preview(peer, app, action, args)
    return service.invoke(
        peer,
        {
            "app": app,
            "action": action,
            "arguments": args,
            "operation_id": operation_id or uuid.uuid4().hex,
            "preview_hash": preview["preview_hash"],
        },
    )


def test_resume_full_draft_text_and_ciphertext_restart(mesh):
    allow(mesh, mesh.buyer, "resume")
    provider = mesh.provider.peer_id
    created = invoke(
        mesh.buyer,
        provider,
        "resume",
        "create_resume",
        {"name": "PRIVATE_RESUME_MARKER", "target_role": "Consultant"},
    )
    resume = created["result"]["record"]["id"]
    invoke(
        mesh.buyer,
        provider,
        "resume",
        "set_contact_info",
        {"resume_id": resume, "contact_info": {"email": "private@example.test"}},
    )
    invoke(
        mesh.buyer,
        provider,
        "resume",
        "add_experience",
        {
            "resume_id": resume,
            "title": "Engineer",
            "company": "Private Company",
            "highlights": ["Built reliable tools"],
        },
    )
    exported = invoke(mesh.buyer, provider, "resume", "export_resume_text", {"resume_id": resume})
    assert (
        "PRIVATE_RESUME_MARKER" in exported["result"]["text"]
        and "Engineer" in exported["result"]["text"]
    )
    assert "private@example.test" in exported["result"]["text"]
    cipher = mesh.provider.store.path.read_bytes()
    assert b"PRIVATE_RESUME_MARKER" not in cipher and b"private@example.test" not in cipher
    rebuilt = NodeApps(
        AppStore(mesh.friends[0].home, mesh.friends[0].messaging_private),
        mesh.provider.peer_id,
        lambda: mesh.friends[0],
    )
    assert rebuilt.call("", "view", {"app": "resume"})["records"] == {}
    assert (
        mesh.buyer.call(provider, "view", {"app": "resume"})["records"]["resumes"][0]["id"]
        == resume
    )
    assert all("PRIVATE_RESUME_MARKER" not in json.dumps(wire) for wire in mesh.transport.wires)


def test_jobs_pipeline_references_stage_guards_and_ryncoin_quotes(mesh):
    allow(mesh, mesh.buyer, "recruit")
    peer = mesh.provider.peer_id
    job = invoke(
        mesh.buyer,
        peer,
        "recruit",
        "create_job",
        {"name": "Part-time editor", "budget_ryncoin": "0.5"},
    )["result"]["record"]["id"]
    with pytest.raises(AppError):
        invoke(
            mesh.buyer,
            peer,
            "recruit",
            "create_candidate",
            {"name": "Applicant", "job_id": "foreign-job"},
        )
    candidate = invoke(
        mesh.buyer,
        peer,
        "recruit",
        "create_candidate",
        {"name": "Applicant", "job_id": job, "resume_text": "A writer"},
    )["result"]["record"]["id"]
    with pytest.raises(AppError, match="transition"):
        invoke(
            mesh.buyer,
            peer,
            "recruit",
            "advance_candidate",
            {"candidate_id": candidate, "stage": "hired"},
        )
    for stage in ("screening", "interview", "offer", "hired"):
        invoke(
            mesh.buyer,
            peer,
            "recruit",
            "advance_candidate",
            {"candidate_id": candidate, "stage": stage},
        )
    result = invoke(mesh.buyer, peer, "recruit", "get_pipeline", {"job_id": job})["result"]
    assert result["stages"]["hired"][0]["name"] == "Applicant"
    assert not (mesh.friends[0].home / "exchange" / "ledger.sqlite3").exists()
    with pytest.raises(AppError):
        mesh.buyer.preview(
            peer, "recruit", "create_job", {"name": "Job", "salary_range": {"currency": "USD"}}
        )


def test_empty_desk_real_comments_closed_filters_and_read_only_views(mesh):
    before = mesh.provider.store.read()
    assert mesh.provider.call("", "view", {"app": "desk"})["records"] == {}
    assert before == mesh.provider.store.read()
    ticket = invoke(
        mesh.provider,
        "",
        "desk",
        "create_ticket",
        {"name": "Fix a small website", "priority": "high"},
    )["result"]["record"]["id"]
    invoke(mesh.provider, "", "desk", "add_comment", {"ticket_id": ticket, "body": "Scope agreed"})
    invoke(
        mesh.provider,
        "",
        "desk",
        "close_ticket",
        {"ticket_id": ticket, "resolution_note": "Delivered"},
    )
    result = invoke(mesh.provider, "", "desk", "get_ticket", {"ticket_id": ticket})["result"][
        "record"
    ]
    assert result["status"] == "closed" and result["comments"][0]["body"] == "Scope agreed"
    assert (
        invoke(mesh.provider, "", "desk", "list_tickets", {"include_closed": False})["result"][
            "total"
        ]
        == 0
    )
    invoke(mesh.provider, "", "desk", "reopen_ticket", {"ticket_id": ticket})
    assert (
        invoke(mesh.provider, "", "desk", "list_tickets", {"priority": "high", "limit": 1})[
            "result"
        ]["total"]
        == 1
    )


def test_grant_revocation_regrant_invalidates_original_review(mesh):
    peer = mesh.provider.peer_id
    assert mesh.buyer.call(peer, "catalog", {})["apps"] == []
    with pytest.raises(AppError, match="denied"):
        mesh.buyer.call(peer, "view", {"app": "desk"})
    allow(mesh, mesh.buyer, "desk")
    args = {"name": "Reviewed task"}
    preview = mesh.buyer.preview(peer, "desk", "create_ticket", args)
    revoked = mesh.provider.grant_preview("desk", mesh.buyer.peer_id, False)
    mesh.provider.grant("desk", mesh.buyer.peer_id, False, revoked["preview_hash"])
    with pytest.raises(AppError, match="denied"):
        mesh.buyer.call(peer, "view", {"app": "desk"})
    allow(mesh, mesh.buyer, "desk")
    with pytest.raises(AppError, match="review_changed"):
        mesh.buyer.invoke(
            peer,
            {
                "app": "desk",
                "action": "create_ticket",
                "arguments": args,
                "operation_id": uuid.uuid4().hex,
                "preview_hash": preview["preview_hash"],
            },
        )


def test_consumer_isolation_idempotence_and_stale_views(mesh):
    peer = mesh.provider.peer_id
    for caller in (mesh.buyer, mesh.other):
        allow(mesh, caller, "desk")
    args = {"name": "Only buyer sees this"}
    preview = mesh.buyer.preview(peer, "desk", "create_ticket", args)
    command = {
        "app": "desk",
        "action": "create_ticket",
        "arguments": args,
        "operation_id": uuid.uuid4().hex,
        "preview_hash": preview["preview_hash"],
    }
    first = mesh.buyer.invoke(peer, command)
    assert mesh.buyer.invoke(peer, command) == first
    assert mesh.other.call(peer, "view", {"app": "desk"})["records"] == {}
    assert mesh.provider.call("", "view", {"app": "desk"})["records"] == {}
    with pytest.raises(AppError, match="operation_changed"):
        mesh.buyer.invoke(peer, {**command, "arguments": {"name": "Changed intent"}})
    with pytest.raises(AppError, match="review_changed"):
        mesh.buyer.invoke(peer, {**command, "operation_id": uuid.uuid4().hex})
    assert mesh.buyer.call(peer, "view", {"app": "desk"})["revision"] == 1


def test_preview_never_sends_draft_text_to_remote_provider(mesh, monkeypatch):
    allow(mesh, mesh.buyer, "desk")
    seen = []
    original = mesh.provider._handle

    def record(actor, operation, value, relationship_id=None):
        seen.append((operation, copy.deepcopy(value)))
        return original(actor, operation, value, relationship_id)

    monkeypatch.setattr(mesh.provider, "_handle", record)
    mesh.buyer.preview(
        mesh.provider.peer_id, "desk", "create_ticket", {"name": "NOT_YET_APPROVED_PRIVATE_TEXT"}
    )
    assert seen == [("context", {"app": "desk"})]


def test_current_friend_revocation_denies_even_old_receipts(mesh):
    allow(mesh, mesh.buyer, "desk")
    peer = mesh.provider.peer_id
    receipt = invoke(mesh.buyer, peer, "desk", "create_ticket", {"name": "Owned task"})
    rid = mesh.friends[0].store.relationship_for_peer(mesh.buyer.peer_id)["relationship_id"]
    mesh.friends[0].revoke(rid, notify=False)
    with pytest.raises((AppError, ValueError)):
        mesh.buyer.call(peer, "receipt", {"app": "desk", "operation_id": receipt["operation_id"]})


def test_routes_owner_auth_bounded_body_and_no_worker(mesh):
    app = FastAPI()

    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(403)

    install_node_apps(
        app,
        home=mesh.friends[0].home,
        messaging_key=mesh.friends[0].messaging_private,
        peer_id=mesh.provider.peer_id,
        friends=lambda: mesh.friends[0],
        local_control=guard,
    )
    with TestClient(app) as client:
        assert client.get("/api/local/node-apps").status_code == 403
        assert client.get("/api/local/node-apps", headers={"x-owner": "yes"}).status_code == 200
        assert (
            client.post(
                "/api/local/node-apps/preview", headers={"x-owner": "yes"}, content=b"x" * 65537
            ).status_code
            == 413
        )
        assert client.post(PATH, json={}).status_code == 403


def test_lost_response_original_intent_survives_reload_and_resumes_once(mesh, monkeypatch):
    allow(mesh, mesh.buyer, "desk")
    peer = mesh.provider.peer_id
    preview = mesh.buyer.preview(peer, "desk", "create_ticket", {"name": "Exactly once"})
    command = {
        "app": "desk",
        "action": "create_ticket",
        "arguments": preview["arguments"],
        "operation_id": uuid.uuid4().hex,
        "preview_hash": preview["preview_hash"],
    }
    original = mesh.transport.post
    lost = [False]

    def fail_after_commit(endpoint, path, payload, headers, **kwargs):
        result = original(endpoint, path, payload, headers, **kwargs)
        if path == PATH and not lost[0]:
            lost[0] = True
            raise OSError("lost response after remote commit")
        return result

    monkeypatch.setattr(mesh.transport, "post", fail_after_commit)
    # FriendService captured the bound original; replace its explicit transport.
    mesh.friends[1].post_json = fail_after_commit
    with pytest.raises(AppError, match="unconfirmed"):
        mesh.buyer.invoke(peer, command)
    assert len(mesh.buyer.pending()) == 1
    restored = NodeApps(
        AppStore(mesh.friends[1].home, mesh.friends[1].messaging_private),
        mesh.buyer.peer_id,
        lambda: mesh.friends[1],
    )
    with pytest.raises(AppError, match="pending_required"):
        invoke(restored, peer, "desk", "create_ticket", {"name": "Do not replace original"})
    receipt = restored.resume(command["operation_id"])
    assert receipt["operation_id"] == command["operation_id"] and restored.pending() == []
    assert len(restored.call(peer, "view", {"app": "desk"})["records"]["tickets"]) == 1


@pytest.mark.parametrize(
    "app_id, action, arguments",
    [
        ("desk", "create_ticket", {"name": "Approved task"}),
        ("wordcraft", "editor_info", {}),
    ],
)
def test_app_agent_requires_approval_even_in_automatic_mode(tmp_path, app_id, action, arguments):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from test_agent_runtime import Catalog, Model, approve, assistant

    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.node_apps.agent import ACTION, SPECS, AppTools

    key = X25519PrivateKey.generate()
    service = NodeApps(AppStore(tmp_path / "business", key), "consumer-node", lambda: None)
    app = FastAPI()
    app.state.node_apps = service
    impl = AppTools(app)

    class Tools:
        def specs(self):
            return SPECS

        def metadata(self, name):
            return {
                "effect": "write",
                "scope_context_supported": True,
                "requires_explicit_approval": True,
                "supports_preview": True,
                "preview_precondition": True,
                "title": "App operation",
            }

        def preview(self, name, args, context=None):
            return impl.preview(args, context)

        def execute(
            self, name, args, operation_id=None, cancelled=None, expected_preview=None, context=None
        ):
            return impl.execute(name, args, operation_id, cancelled, expected_preview, context)

    model = Model(
        assistant(
            name=ACTION,
            args={
                "app": app_id,
                "action": action,
                "arguments": arguments,
                "reviewed_context": service.call("", "context", {"app": app_id}),
            },
        )
    )
    runtime = AgentRuntime(
        AgentStore(tmp_path / "agent", key),
        model,
        Tools(),
        Catalog(),
        consumer_node_id="consumer-node",
    )
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "permission_mode": "automatic",
            "allowed_write_tools": [ACTION],
        }
    )
    created = runtime.create({"message": "Create a ticket"})
    # The synthetic model calls this operation directly rather than discovering
    # it; expose its schema without granting execution or approval authority.
    from agent_tool_fixtures import expose
    expose(runtime, created["id"], [ACTION])
    runtime.run_once()
    runtime.run_once()
    task = runtime.get(created["id"])
    assert task["status"] == "waiting_approval"
    assert service.call("", "view", {"app": "desk"})["records"] == {}
    assert (
        task["pending_approval"]["preview"]["model_data_recipient"]["catalog_model_id"]
        == "local:test"
    )
    approve(runtime, created["id"])
    runtime.run_once()
    if app_id == "desk":
        assert (
            service.call("", "view", {"app": "desk"})["records"]["tickets"][0]["name"]
            == "Approved task"
        )
    else:
        assert service.call("", "view", {"app": app_id})["records"] == {}
        assert any(w["operations"] for w in service.store.read()["workspaces"].values())


def test_standalone_host_remote_auth_and_no_background_workers(tmp_path):
    from rynmesh.node_apps.host import create_host

    app = create_host(tmp_path / "host", "http://127.0.0.1:18990", allow_loopback=True)
    with TestClient(app, client=("198.51.100.10", 12345)) as client:
        assert client.get("/health").json()["background_workers"] is False
        assert client.get("/api/local/node-apps").status_code == 401
        token = app.state.owner_auth.token()
        assert (
            client.get(
                "/api/local/node-apps", headers={"authorization": "Bearer " + token}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/local/node-apps",
                headers={"authorization": "Bearer " + token, "sec-fetch-site": "cross-site"},
            ).status_code
            == 401
        )
    assert not hasattr(app.state, "background_workers")


def test_capacity_rejection_before_dispatch_keeps_original_business_state(mesh, monkeypatch):
    import rynmesh.node_apps.store as module

    before = mesh.provider.store.read()
    preview = mesh.provider.preview("", "desk", "create_ticket", {"name": "Too large to admit"})
    monkeypatch.setattr(module, "MAX_FILE", 512)
    with pytest.raises(AppError, match="capacity"):
        mesh.provider.invoke(
            "",
            {
                "app": "desk",
                "action": "create_ticket",
                "arguments": preview["arguments"],
                "operation_id": uuid.uuid4().hex,
                "preview_hash": preview["preview_hash"],
            },
        )
    assert mesh.provider.store.read() == before


def test_repairing_friend_never_revives_an_old_app_grant(mesh):
    peer = mesh.provider.peer_id
    allow(mesh, mesh.buyer, "desk")
    relationship = mesh.friends[0].store.relationship_for_peer(mesh.buyer.peer_id)[
        "relationship_id"
    ]
    mesh.friends[0].revoke(relationship)
    mesh.friends[1].join(mesh.friends[0].create_invite()["invite_uri"])
    assert mesh.buyer.call(peer, "catalog", {})["apps"] == []
    with pytest.raises(AppError, match="denied"):
        mesh.buyer.preview(peer, "desk", "create_ticket", {"name": "No silent revival"})
    allow(mesh, mesh.buyer, "desk")
    assert (
        invoke(mesh.buyer, peer, "desk", "create_ticket", {"name": "New reviewed grant"})["result"][
            "record"
        ]["name"]
        == "New reviewed grant"
    )


def test_pending_unknown_workspace_does_not_block_unrelated_app(mesh):
    allow(mesh, mesh.buyer, "desk")
    peer = mesh.provider.peer_id
    preview = mesh.buyer.preview(peer, "desk", "create_ticket", {"name": "Unavailable service"})
    operation_id = uuid.uuid4().hex

    def offline(*args, **kwargs):
        raise OSError("synthetic unreachable provider")

    mesh.friends[1].post_json = offline
    with pytest.raises(AppError, match="unconfirmed"):
        mesh.buyer.invoke(
            peer,
            {
                "app": "desk",
                "action": "create_ticket",
                "arguments": preview["arguments"],
                "operation_id": operation_id,
                "preview_hash": preview["preview_hash"],
            },
        )
    assert (
        invoke(mesh.buyer, "", "resume", "create_resume", {"name": "Still available"})["result"][
            "record"
        ]["name"]
        == "Still available"
    )
    assert mesh.buyer.pending()[0]["operation_id"] == operation_id


def test_stale_client_view_cannot_be_rebased_during_preview(mesh):
    peer = mesh.provider.peer_id
    allow(mesh, mesh.buyer, "desk")
    original = mesh.buyer.call(peer, "context", {"app": "desk"})
    invoke(mesh.buyer, peer, "desk", "create_ticket", {"name": "Another device changed state"})
    with pytest.raises(AppError, match="review_changed"):
        mesh.buyer.preview(peer, "desk", "create_ticket", {"name": "Draft stays local"}, original)
    assert mesh.buyer.call(peer, "view", {"app": "desk"})["revision"] == 1


@pytest.mark.parametrize(
    "slug",
    [
        "cadcraft",
        "deckcraft",
        "designcraft",
        "effectcraft",
        "figcraft",
        "filmcraft",
        "gridcraft",
        "lightcraft",
        "pdfcraft",
        "photocraft",
        "soundcraft",
        "vectorcraft",
        "wordcraft",
    ],
)
def test_craft_catalog_honest_runtime_and_existing_peer_contract(mesh, slug):
    from rynmesh.node_apps.craft_catalog import CRAFT

    peer = mesh.provider.peer_id
    assert slug not in {a["id"] for a in mesh.buyer.call(peer, "catalog", {})["apps"]}
    allow(mesh, mesh.buyer, slug)
    catalog = mesh.buyer.call(peer, "catalog", {})
    item = next(a for a in catalog["apps"] if a["id"] == slug)
    assert list(item["operations"]) == ["editor_info"]
    assert item["editor"]["web"] is CRAFT[slug]["web"]
    first = invoke(mesh.buyer, peer, slug, "editor_info", {}, operation_id="craft-information")
    info = first["result"]
    assert info["web_sha256"] == CRAFT[slug]["web_sha256"]
    assert info["editing_api"] == "not_exposed"
    assert info["network_workspace_sync"] is False
    assert first["price_ryncoin"] == "0" and first["revision"] == 0
    assert mesh.buyer.call(peer, "view", {"app": slug})["records"] == {}
    with pytest.raises(AppError):
        mesh.buyer.preview(peer, slug, "install", {})
    with pytest.raises(AppError, match="denied"):
        mesh.other.call(peer, "view", {"app": slug})
    grant = mesh.provider.grant_preview(slug, mesh.buyer.peer_id, False)
    mesh.provider.grant(slug, mesh.buyer.peer_id, False, grant["preview_hash"])
    with pytest.raises(AppError, match="denied"):
        mesh.buyer.call(peer, "view", {"app": slug})
