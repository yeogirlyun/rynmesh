"""Tests for S01-I28: SpaceModelCatalog error codes, row-level fault tolerance, queueing/stale cache, and clock skew tolerance."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.llm_package.peer_binding import VERSION as BINDING_VERSION, expected
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.space_catalog import FRESH_SECONDS, MAX_BYTES, SpaceModelCatalog
from rynmesh.llm_package.task_protocol import TaskProtocolError, open_task, seal_task
from rynmesh.peer_http import PeerTransportError
from rynmesh.services import peer_box
from test_agent_integrations import add_source
from test_personal_space import join, nodes  # noqa: F401


@pytest.fixture
def space_env(nodes):  # noqa: F811
    root, child, work, stranger = nodes
    root.create("S01-I28 test space")
    join(root, child)
    join(root, work)
    join(root, stranger)
    members = (root, child, work, stranger)
    apps, keys = [], []
    for space in members:
        if space is not root:
            space._accept_snapshot(root.data["snapshot"], root.status()["space"])
        space.store.personal_space = space
        space.set_policy("space")
        key = peer_box.load_or_create_messaging_key(space.store.home / "messaging.x25519")
        app = FastAPI()
        install_llm_routes(
            app,
            store=space.store,
            home=space.store.home,
            messaging_key=key,
            resolve_endpoint=lambda _: "",
            resolve_pubkey=lambda _: "",
        )
        apps.append(app)
        keys.append(key)

    services = apps[0].state.llm_agent_services
    record, _ = add_source(apps[0], models=["alpha", "beta"])
    sources = services["source_store"]
    record["shared_models"] = ["alpha", "beta"]
    sources.save(record)
    provider_alpha = services["provider_for"](sources.model_service_id(record, "alpha"))
    provider_beta = services["provider_for"](sources.model_service_id(record, "beta"))

    class HealthyInner:
        def health(self):
            return {"ok": True}

        def chat(self, body, **kwargs):
            return {
                "text": "ok",
                "message": {"role": "assistant", "content": "ok"},
                "input_tokens": 1,
                "output_tokens": 1,
                "duration_ms": 1,
                "finish_reason": "stop",
            }

    provider_alpha.adapter.inner = HealthyInner()
    provider_beta.adapter.inner = HealthyInner()

    publisher = services["space_catalog"]
    consumer: SpaceModelCatalog = apps[1].state.llm_agent_services["space_catalog"]
    tick = [0.0]

    def exchange(peer, wire):
        tick[0] += 2.0
        publisher.clock = lambda: time.time() + tick[0]
        with TestClient(apps[0]) as client:
            resp = client.post("/api/peer/llm/space-catalog", json=wire)
        resp.raise_for_status()
        return resp.json()

    consumer.exchange = exchange
    locator = {
        "peer_id": root.store.peer_id,
        "node_messaging_pub": peer_box.public_key_b64(keys[0]),
        "access_policy": "personal_space",
    }
    yield SimpleNamespace(
        root=root,
        child=child,
        work=work,
        stranger=stranger,
        apps=apps,
        keys=keys,
        sources=sources,
        record=record,
        provider_alpha=provider_alpha,
        provider_beta=provider_beta,
        publisher=publisher,
        consumer=consumer,
        locator=locator,
        real_exchange=exchange,
    )
    for app in apps:
        app.state.llm_shutdown()


def _make_fake_provider(package_id: str, alias: str, extra_pad: str = ""):
    digest = f"{abs(hash(package_id)):032x}"[:32]
    status = {
        "online": True,
        "ready": True,
        "access_policy": "personal_space",
        "chat_protocol": "rynmesh.chat.v1",
        "service": {
            "package_id": package_id,
            "model_alias": alias,
            "adapter": "openai_compatible",
            "capabilities": ["chat", "client-tools", "personal-space-only"],
            **({"notes": extra_pad} if extra_pad else {}),
        },
        "model_binding": {
            "version": BINDING_VERSION,
            "service_id": package_id,
            "model_generation": digest,
            "rule_revision": digest,
        },
    }
    return SimpleNamespace(
        accepting_orders=True,
        manifest=SimpleNamespace(package_id=package_id),
        public_status=lambda cached_cli_models=True, s=status: copy.deepcopy(s),
    )


def test_t1_refresh_error_codes_last_errors_and_model_description(space_env) -> None:
    """T1: Peer rate-limit, unreachable, clock skew, and invalid response produce distinct codes and model descriptions."""
    p = space_env
    peer_id = p.locator["peer_id"]

    # Seed an unverified locator advertisement so the peer model row appears in ModelCatalog even before/without valid cache
    _, _, model_catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    advertised_service = {
        **p.provider_alpha.public_status(cached_cli_models=True),
        "peer_id": peer_id,
        "node_name": "RootDevice",
        "node_messaging_pub": p.locator["node_messaging_pub"],
        "access_policy": "personal_space",
        "binding_verified": False,
        "model_binding": None,
        "online": False,
    }
    model_catalog.services["discover"] = lambda _net: [copy.deepcopy(advertised_service)]
    model_catalog.services["discover_agent"] = lambda _net: [copy.deepcopy(advertised_service)]

    # 1. Rate-limited closed code from peer transport
    def raise_rate_limited(_peer, _wire):
        raise PeerTransportError("peer_http_error:http_429:space_catalog_rate_limited")

    p.consumer.exchange = raise_rate_limited
    with pytest.raises(TaskProtocolError, match="^space_catalog_rate_limited$"):
        p.consumer.refresh(p.locator)
    assert p.consumer.last_errors[peer_id][0] == "space_catalog_rate_limited"

    peer_rows = [m for m in model_catalog.list_models() if m["peer_id"] == peer_id]
    assert peer_rows
    model_id = peer_rows[0]["id"]
    refreshed_row = model_catalog.refresh_destination(model_id)
    assert refreshed_row is not None
    assert refreshed_row["description"].startswith("目录刷新失败：")
    assert "rate_limited" in refreshed_row["description"] or "频繁" in refreshed_row["description"]

    # 2. Unreachable transport error
    def raise_unreachable(_peer, _wire):
        raise OSError("network unreachable")

    p.consumer.exchange = raise_unreachable
    with pytest.raises(TaskProtocolError, match="^space_catalog_unreachable$"):
        p.consumer.refresh(p.locator)
    assert p.consumer.last_errors[peer_id][0] == "space_catalog_unreachable"
    refreshed_row = model_catalog.refresh_destination(model_id)
    assert refreshed_row is not None
    assert refreshed_row["description"].startswith("目录刷新失败：")
    assert "unreachable" in refreshed_row["description"] or "连接" in refreshed_row["description"]

    # 3. Clock skew (response envelope expired beyond 300s skew window)
    def expired_response(_peer, wire):
        p.publisher.last_requests.clear()
        p.publisher.clock = lambda: time.time() - 700.0
        return p.publisher.respond(wire)

    p.consumer.exchange = expired_response
    with pytest.raises(TaskProtocolError, match="^space_catalog_clock_skew$"):
        p.consumer.refresh(p.locator)
    assert p.consumer.last_errors[peer_id][0] == "space_catalog_clock_skew"
    refreshed_row = model_catalog.refresh_destination(model_id)
    assert refreshed_row is not None
    assert refreshed_row["description"].startswith("目录刷新失败：")
    assert "clock_skew" in refreshed_row["description"] or "时钟" in refreshed_row["description"]

    # 4. Tampered/invalid response body -> space_catalog_response_invalid
    p.consumer.exchange = lambda _peer, _wire: {"payload": {}, "public_key": "x", "signature": "y"}
    with pytest.raises(TaskProtocolError, match="^space_catalog_response_invalid$"):
        p.consumer.refresh(p.locator)
    assert p.consumer.last_errors[peer_id][0] == "space_catalog_response_invalid"


def test_t2_consumer_skips_malformed_and_duplicate_rows_without_discarding_catalog(space_env) -> None:
    """T2: A single bad or duplicate row is skipped and counted in skipped_rows while valid rows succeed."""
    p = space_env
    good_a = _make_fake_provider("svc-good-a", "Model A").public_status()
    good_b = _make_fake_provider("svc-good-b", "Model B").public_status()
    missing_pkg_id = {"service": {"name": "Broken"}, "online": True}
    bad_binding = {
        **copy.deepcopy(good_a),
        "service": {**good_a["service"], "package_id": "svc-bad-binding"},
        "model_binding": {"version": BINDING_VERSION, "service_id": "wrong-id", "model_generation": "0" * 32, "rule_revision": "0" * 32},
    }
    duplicate_good_a = copy.deepcopy(good_a)

    def mixed_rows_exchange(_peer, wire):
        outer, req = open_task(
            wire,
            recipient_peer_id=p.root.store.peer_id,
            recipient_messaging_key=p.keys[0],
            expected_kind="space_model_catalog_request",
        )
        permission = p.root.model_permission(outer["from_peer_id"])
        body = {
            "task_id": req["task_id"],
            "services": [good_a, missing_pkg_id, bad_binding, duplicate_good_a, good_b],
            "space_permission": permission,
            "provider_messaging_pub": peer_box.public_key_b64(p.keys[0]),
        }
        return p.publisher._seal(
            body,
            req["task_id"],
            "space_model_catalog_response",
            outer["from_peer_id"],
            req["reply_messaging_pub"],
        )

    p.consumer.exchange = mixed_rows_exchange
    rows = p.consumer.refresh(p.locator)
    assert [r["service"]["package_id"] for r in rows] == ["svc-good-a", "svc-good-b"]
    assert p.consumer.skipped_rows == 3


def test_t3_provider_truncates_101_services_and_oversized_payload_by_name(space_env) -> None:
    """T3: Provider with 101 services or >64 KiB payload truncates sorted by name instead of raising capacity_exhausted."""
    p = space_env
    providers_101 = [
        _make_fake_provider(f"svc-{idx:03d}", f"Model-{idx:03d}")
        for idx in reversed(range(101))
    ]
    p.publisher.providers = lambda: providers_101
    p.publisher.last_requests.clear()

    rows = p.consumer.refresh(p.locator)
    assert len(rows) == 100
    names = [r["service"]["model_alias"] for r in rows]
    assert names == [f"Model-{idx:03d}" for idx in range(100)]
    assert "Model-100" not in names

    # Also verify >64 KiB payload truncation (e.g. 40 services * ~2.4 KiB > 64 KiB)
    large_providers = [
        _make_fake_provider(f"big-{idx:02d}", f"BigModel-{idx:02d}", extra_pad="x" * 2200)
        for idx in reversed(range(40))
    ]
    p.publisher.providers = lambda: large_providers
    p.publisher.last_requests.clear()

    large_rows = p.consumer.refresh(p.locator)
    assert 0 < len(large_rows) < 40
    large_names = [r["service"]["model_alias"] for r in large_rows]
    assert large_names == [f"BigModel-{idx:02d}" for idx in range(len(large_rows))]
    assert len(str(large_rows).encode("utf-8")) > 0


def test_t4_three_peers_concurrent_refresh_queues_and_all_succeed(space_env) -> None:
    """T4: Refreshing 3 distinct peers concurrently queues on outgoing_slots(timeout=10) so all 3 succeed."""
    p = space_env
    # Configure providers on root (apps[0]), work (apps[2]), and stranger (apps[3])
    peer_defs = [
        (p.root, p.apps[0], p.keys[0], "root-svc", "RootModel"),
        (p.work, p.apps[2], p.keys[2], "work-svc", "WorkModel"),
        (p.stranger, p.apps[3], p.keys[3], "stranger-svc", "StrangerModel"),
    ]
    publishers = {}
    locators = []
    for member, app, key, sid, alias in peer_defs:
        pub = app.state.llm_agent_services["space_catalog"]
        pub.providers = lambda s=sid, a=alias: [_make_fake_provider(s, a)]
        publishers[member.store.peer_id] = pub
        locators.append({
            "peer_id": member.store.peer_id,
            "node_messaging_pub": peer_box.public_key_b64(key),
            "access_policy": "personal_space",
        })

    in_flight = 0
    two_active = threading.Event()
    counter_lock = threading.Lock()

    def slow_multi_peer_exchange(peer, wire):
        nonlocal in_flight
        with counter_lock:
            in_flight += 1
            if in_flight >= 2:
                two_active.set()
        two_active.wait(timeout=0.3)
        time.sleep(0.15)
        try:
            return publishers[peer].respond(wire)
        finally:
            with counter_lock:
                in_flight -= 1

    p.consumer.exchange = slow_multi_peer_exchange
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(p.consumer.refresh, loc) for loc in locators]
        results = [f.result(timeout=10) for f in futures]

    assert all(len(r) == 1 for r in results)
    cached_ids = {r["service"]["package_id"] for r in p.consumer.records()}
    assert cached_ids == {"root-svc", "work-svc", "stranger-svc"}


def test_t5_expired_cache_returns_stale_rows_and_resolve_refreshes(space_env) -> None:
    """T5: Cache older than FRESH_SECONDS returns catalog_state='stale', available=False, UI shows '待确认', and resolve refreshes."""
    p = space_env
    now = [time.time()]
    p.consumer.clock = lambda: now[0]
    p.publisher.clock = lambda: now[0]

    def clock_exchange(_peer, wire):
        p.publisher.last_requests.clear()
        return p.publisher.respond(wire)

    p.consumer.exchange = clock_exchange
    fresh = p.consumer.refresh(p.locator)
    assert len(fresh) == 2

    # Advance past FRESH_SECONDS
    now[0] += FRESH_SECONDS + 15
    stale = p.consumer.records()
    assert len(stale) == 2
    for row in stale:
        assert row["catalog_state"] == "stale"
        assert row["available"] is False
        assert expected(row, p.child.store.peer_id) is None

    _, _, model_catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    peer_models = [m for m in model_catalog.list_models() if m["source"] == "peer"]
    assert len(peer_models) == 2
    target_id = peer_models[0]["id"]
    assert peer_models[0]["available"] is False
    assert peer_models[0]["catalog_state"] == "stale"
    assert "待确认" in peer_models[0]["description"]

    # Calling resolve() refreshes the stale peer destination first and returns an available bound model
    refresh_calls = []

    def tracked_exchange(peer, wire):
        refresh_calls.append(peer)
        return clock_exchange(peer, wire)

    p.consumer.exchange = tracked_exchange
    resolved = model_catalog.resolve(target_id)
    assert len(refresh_calls) == 1
    assert resolved["available"] is True
    assert resolved["destination_binding_supported"] is True
    assert resolved.get("catalog_state") != "stale"


def test_t6_clock_skew_200s_opens_and_400s_reports_clock_hint(space_env) -> None:
    """T6: Envelope expired by 200s still opens within 300s tolerance; 400s raises 'task envelope expired; check device clock'."""
    p = space_env
    sender_id = p.root.store.peer_id
    recipient_id = p.child.store.peer_id
    recipient_key = p.keys[1]
    recipient_pub = peer_box.public_key_b64(recipient_key)

    # 1. 200s in the past -> within 300s clock skew tolerance -> opens cleanly
    exp_200 = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
    env_200 = seal_task(
        body={"task_id": "skew-200", "hello": "world"},
        task_id="skew-200",
        kind="llm_request",
        sender_peer_id=sender_id,
        recipient_peer_id=recipient_id,
        sender_signing_key=p.root.store.private_key_bytes,
        recipient_messaging_pub=recipient_pub,
        expires_at=exp_200,
    )
    outer, body = open_task(
        env_200,
        recipient_peer_id=recipient_id,
        recipient_messaging_key=recipient_key,
        expected_kind="llm_request",
    )
    assert outer["task_id"] == "skew-200" and body["hello"] == "world"

    # 2. 400s in the past -> exceeds 300s clock skew tolerance -> raises clock hint
    exp_400 = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    env_400 = seal_task(
        body={"task_id": "skew-400", "hello": "world"},
        task_id="skew-400",
        kind="llm_request",
        sender_peer_id=sender_id,
        recipient_peer_id=recipient_id,
        sender_signing_key=p.root.store.private_key_bytes,
        recipient_messaging_pub=recipient_pub,
        expires_at=exp_400,
    )
    with pytest.raises(TaskProtocolError, match=r"^task envelope expired; check device clock$"):
        open_task(
            env_400,
            recipient_peer_id=recipient_id,
            recipient_messaging_key=recipient_key,
            expected_kind="llm_request",
        )

    # 3. SpaceModelCatalog._seal uses 300s TTL (instead of 60s), so 200s clock skew between peers succeeds end-to-end
    base_now = time.time()
    p.consumer.clock = lambda: base_now
    p.publisher.clock = lambda: base_now - 200.0
    p.publisher.last_requests.clear()
    p.consumer.exchange = lambda _peer, wire: p.publisher.respond(wire)
    rows = p.consumer.refresh(p.locator)
    assert len(rows) == 2
