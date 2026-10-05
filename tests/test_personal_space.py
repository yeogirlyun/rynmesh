import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.exceptions import InvalidTag

from rynmesh.crypto import SignedPayload, sign_payload
from rynmesh.jobs import verify_work_result
from rynmesh.personal_space import PersonalSpace, SpaceError, restore_backup
from rynmesh.registry import FilePeerRegistry
from rynmesh.store import RynmeshStore


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    monkeypatch.delenv("RYNMESH_REGISTRY_URL", raising=False)
    monkeypatch.setenv("RYNMESH_REGISTRY_DIR", str(tmp_path / "registry"))
    registry = FilePeerRegistry(tmp_path / "registry")
    spaces = []
    for name in ("Home PC", "Laptop", "Work PC", "Stranger"):
        store = RynmeshStore(home=tmp_path / name, network_dir=tmp_path / "network", node_name=name)
        store.registry = registry
        spaces.append(PersonalSpace(store))
    return spaces


def join(root, child, invitation=None):
    invitation = invitation or root.act("invite", {"hours": 24})["invitation"]
    child.join(invitation, child.store.node_name)
    child.tick()
    root.tick()
    child.tick()
    return invitation


def test_same_lan_join_completes_without_registry_mailbox_roundtrip(nodes, monkeypatch):
    root, laptop, *_ = nodes
    root.create("Home")
    laptop.join(root.act("invite", {})["invitation"], "Laptop")
    monkeypatch.setattr(laptop, "_direct_exchange",
                        lambda order, authority: root._process(order, publish=False))
    laptop.tick()
    assert laptop.status()["membership"] == "active"
    assert laptop.status()["pending"] == []
    assert root.status()["members"][1]["name"] == "Laptop"
    assert root.store.registry.list_work_orders(
        network_id="space-" + root.status()["space"]["id"],
        provider_peer_id=root.store.peer_id,
    ) == []


def test_direct_space_http_returns_signed_encrypted_reply(nodes):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from rynmesh.personal_space_routes import install_space_routes

    root, laptop, *_ = nodes
    root.create("Home")
    laptop.join(root.act("invite", {})["invitation"], "Laptop")
    request = next(iter(laptop.data["pending"].values()))["order"]
    app = FastAPI()
    install_space_routes(app, space=root, local_control=lambda _: None)
    with TestClient(app) as client:
        response = client.post("/api/peer/space/exchange", json=request)
    assert response.status_code == 200
    result = verify_work_result(SignedPayload.from_dict(response.json()))
    assert result.provider_peer_id == root.store.peer_id
    assert result.requester_peer_id == laptop.store.peer_id
    assert "envelope" in result.result_refs


def sync(root, child):
    child.last_poll = 0
    child.tick()
    root.tick()
    child.tick()


def test_three_devices_and_changed_address_keep_identity(nodes):
    root, laptop, work, stranger = nodes
    root.create("My home")
    join(root, laptop)
    join(root, work)
    sync(root, laptop)
    for node in (root, laptop, work):
        assert len(node.status()["members"]) == 3
        assert node.status()["membership"] == "active"
    assert not root.allows_ai(laptop.store.peer_id)
    root.set_policy("space")
    assert root.allows_ai(laptop.store.peer_id)
    assert not root.allows_ai(stranger.store.peer_id)
    laptop.store.node_name = "Renamed laptop"
    restarted = PersonalSpace(laptop.store)
    assert restarted.status()["space"]["id"] == root.status()["space"]["id"]
    assert root.allows_ai(restarted.store.peer_id)


def test_invitation_is_one_use_and_race_is_serialized(nodes):
    root, a, b, _ = nodes
    root.create("My home")
    invite = root.act("invite", {})["invitation"]
    a.join(invite, "Laptop")
    b.join(invite, "Work")
    a.tick()
    b.tick()
    orders = root.store.registry.list_work_orders(
        network_id="space-" + root.status()["space"]["id"], provider_peer_id=root.store.peer_id
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(root._process, orders))
    a.tick()
    b.tick()
    assert sorted([a.status()["membership"], b.status()["membership"]]) == ["active", "none"]
    assert len(root.status()["members"]) == 2


def test_invite_expiration_cancellation_and_tamper(nodes):
    root, child, *_ = nodes
    root.create("My home")
    invite = root.act("invite", {})["invitation"]
    root.act("cancel_invite", {"id": root.status()["invites"][0]["id"]})
    join(root, child, invite)
    assert child.status()["membership"] == "none"
    assert "cancelled" in child.status()["last_error"]
    root.act("invite", {"hours": 1})
    child.clock = lambda: time.time() + 3700
    with pytest.raises(SpaceError, match="expired"):
        child.join(root.status()["invitation"], "Laptop")


def test_delegated_manager_can_remove_but_regular_device_cannot(nodes):
    root, laptop, work, _ = nodes
    root.create("Home")
    join(root, laptop)
    join(root, work)
    with pytest.raises(SpaceError, match="management"):
        work.act("remove", {"peer_id": laptop.store.peer_id})
    root.act("role", {"peer_id": laptop.store.peer_id, "role": "manager"})
    sync(root, laptop)
    laptop.act("remove", {"peer_id": work.store.peer_id})
    laptop.tick()
    root.tick()
    laptop.tick()
    root.set_policy("space")
    assert not root.allows_ai(work.store.peer_id)
    sync(root, work)
    assert work.status()["membership"] == "removed"
    assert laptop.status()["can_manage"]
    with pytest.raises(SpaceError, match="coordinator"):
        root.act("remove", {"peer_id": root.store.peer_id})


def test_revocation_reaches_other_providers_and_stale_access_expires(nodes):
    root, laptop, work, _ = nodes
    root.create("Home")
    join(root, laptop)
    join(root, work)
    sync(root, laptop)
    laptop.set_policy("space")
    assert laptop.allows_ai(work.store.peer_id)
    root.act("remove", {"peer_id": work.store.peer_id})
    sync(root, laptop)
    assert not laptop.allows_ai(work.store.peer_id)
    assert laptop.allows_ai(root.store.peer_id)
    laptop.clock = lambda: time.time() + 25 * 3600
    assert not laptop.allows_ai(root.store.peer_id)
    assert laptop.status()["membership"] == "expired"


def test_untrusted_and_rollback_snapshots_are_rejected(nodes):
    root, child, _, stranger = nodes
    root.create("Home")
    join(root, child)
    old = copy.deepcopy(root.data["snapshot"])
    root.act("role", {"peer_id": child.store.peer_id, "role": "manager"})
    sync(root, child)
    with pytest.raises(SpaceError, match="outdated"):
        child._accept_snapshot(old, child._snapshot())
    forged = sign_payload(
        root._snapshot(), private_key_bytes=stranger.store.private_key_bytes
    ).to_dict()
    with pytest.raises(SpaceError):
        child._accept_snapshot(forged, child._snapshot())


def test_offline_queue_survives_restart_and_transport_contains_no_invite_secret(nodes):
    root, child, *_ = nodes
    root.create("Home")
    invitation = root.act("invite", {})["invitation"]
    child.join(invitation, "Secret Laptop Name")
    child.tick()  # coordinator is offline
    assert child.status()["pending"]
    restarted = PersonalSpace(child.store)
    root.tick()
    restarted.tick()
    assert restarted.status()["membership"] == "active"
    serialized = "".join(path.read_text() for path in root.store.registry.root.rglob("*.json"))
    assert serialized
    assert "Secret Laptop Name" not in serialized
    assert invitation not in serialized


def test_crash_after_join_before_reply_does_not_consume_twice(nodes, monkeypatch):
    root, child, *_ = nodes
    root.create("Home")
    child.join(root.act("invite", {})["invitation"], "Laptop")
    child.tick()
    publish = root.store.publish_work_result
    monkeypatch.setattr(
        root.store,
        "publish_work_result",
        lambda **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    root.tick()
    assert len(root.status()["members"]) == 2
    monkeypatch.setattr(root.store, "publish_work_result", publish)
    PersonalSpace(root.store).tick()
    child.tick()
    assert child.status()["membership"] == "active"
    assert len(root.status()["members"]) == 2


def test_encrypted_recovery_preserves_identity_and_refuses_overwrite(nodes, tmp_path):
    root, child, *_ = nodes
    root.create("Home")
    join(root, child)
    backup = root.backup("a long recovery password")
    assert root.store.peer_id not in json.dumps(backup)
    with pytest.raises(InvalidTag):
        restore_backup(backup, "wrong password", tmp_path / "bad")
    restore_backup(backup, "a long recovery password", tmp_path / "recovered")
    store = RynmeshStore(home=tmp_path / "recovered", network_dir=tmp_path / "network")
    restored = PersonalSpace(store)
    assert store.peer_id == root.store.peer_id
    assert len(restored.status()["members"]) == 2
    assert restored.status()["ai_access"] == "local"
    with pytest.raises(SpaceError, match="empty"):
        restore_backup(backup, "a long recovery password", tmp_path / "recovered")


def test_space_api_is_local_only(nodes, monkeypatch):
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app

    root = nodes[0]
    monkeypatch.setenv("RYNMESH_HOME", str(root.store.home))
    with TestClient(create_app(root.store)) as client:
        assert client.post("/api/local/space/create", json={"name": "Home"}).status_code == 200
        assert (
            client.get("/api/local/space", headers={"x-forwarded-for": "203.0.113.2"}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/local/space/invite", json={}, headers={"x-forwarded-for": "203.0.113.2"}
            ).status_code
            == 401
        )
        assert client.post("/api/local/space/create", json={"name": "Again"}).status_code == 400
        assert (
            client.post("/api/local/space/policy", json={"access": "everyone"}).status_code == 400
        )


def test_remote_member_cannot_forge_management_and_removed_manager_invites_die(nodes):
    root, member, target, newcomer = nodes
    root.create("Home")
    join(root, member)
    join(root, target)
    member._queue("remove", {"peer_id": target.store.peer_id}, member._snapshot())
    member.tick()
    root.tick()
    member.tick()
    assert "management" in member.status()["last_error"]
    assert not next(m for m in root.status()["members"] if m["peer_id"] == target.store.peer_id)[
        "removed"
    ]
    root.act("role", {"peer_id": member.store.peer_id, "role": "manager"})
    sync(root, member)
    member.act("invite", {})
    member.tick()
    root.tick()
    member.tick()
    invitation = member.status()["invitation"]
    root.act("remove", {"peer_id": member.store.peer_id})
    join(root, newcomer, invitation)
    assert newcomer.status()["membership"] == "none"


def test_provider_checks_space_permission_before_inference(nodes, tmp_path):
    from rynmesh.jobs import default_expires_at
    from rynmesh.llm_package.manifest import LLMPackageManifest
    from rynmesh.llm_package.routes import ProviderService
    from rynmesh.llm_package.task_balance import TaskBalanceLedger
    from rynmesh.llm_package.task_protocol import TaskOrderStore, TaskProtocolError, seal_task
    from rynmesh.services.peer_box import public_key_b64

    root, member, _, stranger = nodes
    root.create("Home")
    join(root, member)
    root.store.personal_space = root

    class Adapter:
        calls = 0

        def health(self):
            return {"ok": True}

        def infer(self, **kwargs):
            self.calls += 1
            return {
                "text": "ok",
                "model": "test",
                "input_tokens": 1,
                "output_tokens": 1,
                "duration_ms": 1,
            }

    adapter = Adapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc",
            mode="openai_compatible",
            public_model_alias="test",
            base_url="http://127.0.0.1:1",
        ),
        adapter=adapter,
        store=root.store,
        task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=root.box,
    )

    def request(sender, task):
        return seal_task(
            body={
                "task_id": task,
                "service_id": "svc",
                "prompt": "test",
                "max_tokens": 8,
                "max_amount": 1,
                "reply_messaging_pub": public_key_b64(sender.box),
            },
            task_id=task,
            kind="llm_request",
            sender_peer_id=sender.store.peer_id,
            recipient_peer_id=root.store.peer_id,
            sender_signing_key=sender.store.private_key_bytes,
            recipient_messaging_pub=public_key_b64(root.box),
            expires_at=default_expires_at(1),
        ).to_dict()

    with pytest.raises(TaskProtocolError, match="not allowed"):
        service.handle(request(member, "local-only"))
    root.set_policy("space")
    with pytest.raises(TaskProtocolError, match="not allowed"):
        service.handle(request(stranger, "stranger"))
    service.handle(request(member, "member"))
    assert adapter.calls == 1
    root.act("remove", {"peer_id": member.store.peer_id})
    with pytest.raises(TaskProtocolError, match="not allowed"):
        service.handle(request(member, "revoked"))
    assert adapter.calls == 1


def test_existing_registry_http_endpoints_support_space_exchange(nodes):
    from fastapi.testclient import TestClient

    from rynmesh.registry import HttpPeerRegistry
    from rynmesh.registry_http import create_app

    root, member, *_ = nodes
    with TestClient(create_app(registry=root.store.registry)) as http:

        class Registry(HttpPeerRegistry):
            def _json(self, req):
                response = http.request(
                    req.get_method(),
                    req.full_url.replace("https://registry.example", ""),
                    content=req.data,
                    headers=dict(req.header_items()),
                )
                response.raise_for_status()
                return response.json()

        root.store.registry = Registry("https://registry.example")
        member.store.registry = Registry("https://registry.example")
        root.create("Home")
        join(root, member)
        assert member.status()["membership"] == "active"
        assert len(root.status()["members"]) == 2
