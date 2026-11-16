"""Overhaul S13-I18: friend nonce replay vs rate_limited (4096), card auto-eviction, clock-skew retry, patch_invite 404, and forward-compatible invite permissions."""
from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rynmesh.crypto import canonical_json, sign_payload
from rynmesh.friends import card_cleanup
from rynmesh.friends.crypto import (
    PERMISSIONS,
    FriendCryptoError,
    auth_headers,
    invite_uri,
    parse_invite,
    secret_hash,
    verify_auth,
)
from rynmesh.friends.service import FriendError, FriendService
from rynmesh.friends.store import FriendStore
from rynmesh.peer_http import create_app
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _make_pair(tmp_path):
    nodes: dict[str, FriendService] = {}
    wire_attempts: list[tuple[str, dict, str | None]] = []

    def post(endpoint: str, path: str, payload: dict, headers: dict | None, **_kwargs) -> dict:
        node = nodes[endpoint]
        if path == "/api/peer/friends/accept":
            return node.accept(payload)
        assert headers is not None
        try:
            rel = node.verify_request(path=path, body=canonical_json(payload), headers=headers)
        except FriendError as exc:
            wire_attempts.append((path, dict(headers), str(exc)))
            raise
        wire_attempts.append((path, dict(headers), None))
        if path == "/api/peer/friends/message":
            node.receive_message(payload)
            return node.delivery_receipt(path, payload)
        if path == "/api/peer/friends/content-card":
            node.receive_content_card(payload)
            return node.delivery_receipt(path, payload)
        if path == "/api/peer/friends/content-card/fetch":
            return node.serve_content_card(payload, rel)
        raise AssertionError(f"Unexpected path: {path}")

    def build_node(name: str, port: int) -> FriendService:
        home = tmp_path / name
        rstore = RynmeshStore(home=home, network_dir=tmp_path / "network", node_name=name)
        msg_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        ep = f"http://127.0.0.1:{port}"
        svc = FriendService(
            home=home,
            peer_id=rstore.peer_id,
            node_name=name,
            endpoint=ep,
            identity_private=rstore.private_key_bytes,
            messaging_private=msg_key,
            post_json=post,
            allow_loopback=True,
        )
        nodes[ep] = svc
        return svc

    alice = build_node("Alice", 19101)
    bob = build_node("Bob", 19102)
    inv = alice.create_invite()
    bob.join(inv["invite_uri"])
    return alice, bob, wire_attempts


def test_t1_duplicate_nonce_returns_replay_enum(tmp_path):
    """T1: 同一 nonce 第二次 -> replay 枚举（含表满时重放既有 nonce 仍判 replay，覆盖改动 1）。"""
    from enum import Enum

    from rynmesh.friends.store import NonceStatus

    store = FriendStore(tmp_path)
    first = store.remember_nonce("rel_1", "nonce_a", now=1000, timestamp=1000, limit=2)
    assert isinstance(first, Enum)
    assert first == NonceStatus.ACCEPTED
    assert type(first) is not bool

    second = store.remember_nonce("rel_1", "nonce_a", now=1001, timestamp=1000, limit=2)
    assert second == NonceStatus.REPLAY

    # Fill to limit=2, then replay nonce_a again -> still REPLAY (not RATE_LIMITED)
    assert store.remember_nonce("rel_1", "nonce_b", now=1002, timestamp=1002, limit=2) == NonceStatus.ACCEPTED
    replay_when_full = store.remember_nonce("rel_1", "nonce_a", now=1003, timestamp=1000, limit=2)
    assert replay_when_full == NonceStatus.REPLAY


def test_t2_nonce_table_full_returns_rate_limited_4096_and_backoff_retry(tmp_path, monkeypatch):
    """T2: 表满 -> rate_limited（上限 4096、不淘汰未过期项，service 抛 friend_rate_limited / HTTP 429，发送方按退避重试，覆盖改动 1、2）。"""
    from rynmesh.friends.store import NonceStatus

    store = FriendStore(tmp_path / "store_4096")
    # Verify default limit is 4096 and benchmark bulk persistence
    # Seed 4094 unexpired nonces, then add 2 more via remember_nonce to reach 4096
    seed_nonces = {f"n_{i}": 2120 for i in range(4094)}
    store._write_nonces({"rel_full": seed_nonces})
    t_start = time.perf_counter()
    assert store.remember_nonce("rel_full", "n_4094", now=2000, timestamp=2000) == NonceStatus.ACCEPTED
    assert store.remember_nonce("rel_full", "n_4095", now=2000, timestamp=2000) == NonceStatus.ACCEPTED
    elapsed_ms = (time.perf_counter() - t_start) / 2 * 1000
    assert elapsed_ms < 500, f"remember_nonce at 4096 entries too slow: {elapsed_ms:.2f} ms"

    # 4097th distinct unexpired nonce -> RATE_LIMITED without evicting n_0..n_4095
    over = store.remember_nonce("rel_full", "n_4096", now=2001, timestamp=2001)
    assert over == NonceStatus.RATE_LIMITED
    saved = store.state()["nonces"]["rel_full"]
    assert len(saved) == 4096
    assert "n_0" in saved and "n_4095" in saved and "n_4096" not in saved

    # Verify service.verify_request raises friend_rate_limited and sender retries with backoff
    alice, bob, _ = _make_pair(tmp_path / "pair_rate")
    rel = alice.store.relationship_for_peer(bob.peer_id)
    assert rel is not None
    secret = alice.store.secret(rel["relationship_id"])
    assert secret is not None

    now_ts = int(alice.clock().timestamp())
    alice.store._write_nonces({rel["relationship_id"]: {f"fill_{i}": now_ts + 5 for i in range(4096)}})
    body_bytes = canonical_json({"probe": 1})
    hdrs = auth_headers(
        secret,
        method="POST",
        path="/api/peer/friends/message",
        body=body_bytes,
        sender=bob.peer_id,
        receiver=alice.peer_id,
        relationship_id=rel["relationship_id"],
        timestamp=now_ts,
        nonce="fresh_over_limit",
    )
    with pytest.raises(FriendError, match="friend_rate_limited"):
        alice.verify_request(path="/api/peer/friends/message", body=body_bytes, headers=hdrs)

    # Sender backoff retry: first attempt hits 429 rate_limited, sleep advances/clears expired nonces, second attempt succeeds
    sleeps: list[float] = []

    def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        # Simulate window advancing past fill_* expiry during backoff
        alice.store._write_nonces({rel["relationship_id"]: {}})

    monkeypatch.setattr(bob, "_sleep", _fake_sleep)
    sent = bob.send_message(alice.peer_id, text="delivered after rate-limit backoff")
    assert sent["delivered"] is True
    assert sleeps and sleeps[0] > 0


def test_t3_card_eviction_and_five_minute_clock_skew_calibration(tmp_path):
    """T3: 卡片满且有已读旧卡（或已过期卡）-> 淘汰最旧并记擦除标记后写入成功；对方时钟快 5 分钟时首个请求得 friend_clock_skew，校正重试后成功（覆盖改动 3、4）。"""
    # Part A: Card capacity auto-eviction
    cstore = FriendStore(tmp_path / "cards_home")
    # Fill to limit=3:
    # - card 1: unread & not downloaded (must NOT be evicted even though oldest overall!)
    # - card 2: read & downloaded (oldest eligible -> MUST be evicted first!)
    # - card 3: read & downloaded (newer than card 2 -> kept when only 1 slot needed)
    c1, c2, c3, c4, c5 = ("1" * 32, "2" * 32, "3" * 32, "4" * 32, "5" * 32)
    cstore.put_card(
        {"card_id": c1, "created_at": "2026-01-01T00:00:00+00:00", "fetch_state": "available", "card": {"title": "Unread"}},
        limit=3,
    )
    cstore.put_card(
        {"card_id": c2, "created_at": "2026-01-02T00:00:00+00:00", "read": True, "fetch_state": "fetched", "card": {"title": "Old Read"}},
        limit=3,
    )
    cstore.put_card(
        {"card_id": c3, "created_at": "2026-01-03T00:00:00+00:00", "read": True, "downloaded": True, "card": {"title": "Newer Read"}},
        limit=3,
    )

    evicted = cstore.put_card(
        {"card_id": c4, "created_at": "2026-01-04T00:00:00+00:00", "fetch_state": "available", "card": {"title": "Incoming 4"}},
        limit=3,
    )
    assert evicted == 1
    remaining_ids = {row["card_id"] for row in cstore.list_cards()}
    assert remaining_ids == {c1, c3, c4}
    assert cstore.card_erased(c2) is True
    assert card_cleanup.erased(cstore.state(), c2) is True
    assert cstore.auto_cleaned_cards_count() == 1

    # Evict c3 next when adding c5, then all 3 remaining (c1, c4, c5) are unread & unexpired -> 6th raises friend_card_capacity_exhausted
    evicted2 = cstore.put_card(
        {"card_id": c5, "created_at": "2026-01-05T00:00:00+00:00", "fetch_state": "available", "card": {"title": "Incoming 5"}},
        limit=3,
    )
    assert evicted2 == 1
    assert cstore.card_erased(c3) is True
    assert cstore.auto_cleaned_cards_count() == 2
    with pytest.raises(ValueError, match="friend_card_capacity_exhausted"):
        cstore.put_card(
            {"card_id": "6" * 32, "created_at": "2026-01-06T00:00:00+00:00", "fetch_state": "available", "card": {"title": "Overflow"}},
            limit=3,
        )

    # Part B: Peer clock is +5 minutes (300s) ahead -> first request gets friend_clock_skew with server_time, sender calibrates and retries once -> succeeds!
    alice, bob, wire_attempts = _make_pair(tmp_path / "skew_pair")
    base_time = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    bob.clock = lambda: base_time
    alice.clock = lambda: base_time + timedelta(minutes=5)

    # Direct verify_auth check: >120s window raises friend_clock_skew carrying server_time
    rel = alice.store.relationship_for_peer(bob.peer_id)
    assert rel is not None
    sec = alice.store.secret(rel["relationship_id"])
    assert sec is not None
    raw_body = canonical_json({"hello": 1})
    skew_hdrs = auth_headers(
        sec,
        method="POST",
        path="/api/peer/friends/message",
        body=raw_body,
        sender=bob.peer_id,
        receiver=alice.peer_id,
        relationship_id=rel["relationship_id"],
        timestamp=int(base_time.timestamp()),
        nonce="skew_probe_1",
    )
    with pytest.raises(FriendCryptoError, match="friend_clock_skew") as crypto_exc:
        verify_auth(
            sec,
            method="POST",
            path="/api/peer/friends/message",
            body=raw_body,
            sender=bob.peer_id,
            receiver=alice.peer_id,
            relationship_id=rel["relationship_id"],
            timestamp=int(base_time.timestamp()),
            nonce="skew_probe_1",
            signature=skew_hdrs["X-Ryn-Friend-MAC"],
            now=int((base_time + timedelta(minutes=5)).timestamp()),
        )
    assert getattr(crypto_exc.value, "server_time", None) == int((base_time + timedelta(minutes=5)).timestamp())

    # End-to-end send_message: first wire attempt fails with friend_clock_skew, calibrated retry succeeds!
    wire_attempts.clear()
    msg = bob.send_message(alice.peer_id, text="hello across 5min skew")
    assert msg["delivered"] is True
    assert len(wire_attempts) == 2
    assert wire_attempts[0][2] == "friend_clock_skew"
    assert wire_attempts[1][2] is None
    assert alice.history(bob.peer_id)[0]["text"] == "hello across 5min skew"

    # Subsequent send uses saved offset immediately in 1 attempt!
    wire_attempts.clear()
    msg2 = bob.send_message(alice.peer_id, text="second message uses cached offset")
    assert msg2["delivered"] is True
    assert len(wire_attempts) == 1
    assert wire_attempts[0][2] is None


def test_t4_patch_invite_missing_id_raises_invite_not_found_and_route_404(tmp_path, monkeypatch):
    """T4: patch_invite 不存在的 id 抛 ValueError('invite_not_found')，且路由映射 404（覆盖改动 5）。"""
    store = FriendStore(tmp_path / "inv_store")
    with pytest.raises(ValueError, match="^invite_not_found$"):
        store.patch_invite("nonexistent_invite_id", {"ai_stage": "authorized"})

    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "owner-tok")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", "http://127.0.0.1:19199")
    app = create_app(RynmeshStore(home=tmp_path / "route_node", network_dir=tmp_path / "net"))
    with TestClient(app) as client:
        resp = client.delete(
            "/api/local/friends/invites/missing_invite_999",
            headers={"x-ryn-local-token": "owner-tok"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "invite_not_found"


def test_t5_invite_with_unknown_extra_permission_is_accepted_and_records_ignored_permissions(tmp_path):
    """T5: 带一项未知权限的邀请可接受且 ignored_permissions 有它；缺少基础权限仍判 invalid_invite（覆盖改动 6）。"""
    alice, bob, _ = _make_pair(tmp_path / "perm_base")
    # Create a second pair: Alice invites Carol with an extra future permission
    carol_home = tmp_path / "Carol"
    carol_rstore = RynmeshStore(home=carol_home, network_dir=tmp_path / "perm_base" / "network", node_name="Carol")
    carol_msg = peer_box.load_or_create_messaging_key(carol_home / "messaging.x25519")
    carol = FriendService(
        home=carol_home,
        peer_id=carol_rstore.peer_id,
        node_name="Carol",
        endpoint="http://127.0.0.1:19103",
        identity_private=carol_rstore.private_key_bytes,
        messaging_private=carol_msg,
        post_json=bob.post_json,
        allow_loopback=True,
    )

    now = alice.clock()
    invite_id = uuid.uuid4().hex
    secret = os.urandom(32)
    payload_with_extra = {
        "kind": "ryn.friend-invite.v1",
        "invite_id": invite_id,
        "peer_id": alice.peer_id,
        "node_name": alice.node_name,
        "endpoint": alice.endpoint,
        "endpoints": [alice.endpoint],
        "network_id": "rynmesh-main",
        "messaging_pub": peer_box.public_key_b64(alice.messaging_private),
        "permissions": [*PERMISSIONS, "friend.voice-call.v2"],
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
        "secret_hash": secret_hash(secret),
    }
    signed = sign_payload(payload_with_extra, private_key_bytes=alice.identity_private)
    alice.store.put_invite(
        invite_id,
        {**{k: v for k, v in payload_with_extra.items() if k != "messaging_pub"}, "status": "active"},
    )
    uri = invite_uri(signed, secret)

    parsed_signed, _ = parse_invite(uri, now=now, allow_loopback=True)
    assert parsed_signed.ignored_permissions == ["friend.voice-call.v2"]

    preview = carol.inspect_invite(uri)
    assert preview["ignored_permissions"] == ["friend.voice-call.v2"]
    assert tuple(preview["permissions"]) == PERMISSIONS

    joined = carol.join(uri)
    assert joined["status"] == "active"
    assert joined["ignored_permissions"] == ["friend.voice-call.v2"]
    assert tuple(joined["permissions"]) == PERMISSIONS

    # Missing a base permission must still raise invalid_invite
    bad_payload = {
        **payload_with_extra,
        "invite_id": uuid.uuid4().hex,
        "permissions": ["friend.message", "friend.voice-call.v2"],
    }
    bad_uri = invite_uri(sign_payload(bad_payload, private_key_bytes=alice.identity_private), secret)
    with pytest.raises(FriendCryptoError, match="invalid_invite"):
        parse_invite(bad_uri, now=now, allow_loopback=True)


def test_t6_clock_skew_over_real_post_json_and_transport_error(tmp_path, monkeypatch):
    """返工补充：走真实 HttpPeerClient.post_json（假 Transport 对 409 抛 TransportError，丢弃响应体仅带响应头整数 server_time），发送方记下偏移并重试成功。"""
    import email.message
    from urllib.error import HTTPError
    from urllib.parse import urlparse

    from rynmesh.peer_http import HttpPeerClient
    from rynmesh.transport import StdlibHttpsTransport, TransportError, TransportProfile

    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "owner-tok")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", "http://127.0.0.1:19201")

    alice_app = create_app(RynmeshStore(home=tmp_path / "alice_real", network_dir=tmp_path / "net_real", node_name="AliceReal"))
    with TestClient(alice_app) as alice_http:
        alice_svc: FriendService = alice_app.state.friends.service
        calls: list[int] = []

        class _Fake409Transport:
            def post_bytes(self, url: str, body: bytes, *, timeout_s: float, max_bytes: int, headers=None) -> bytes:
                parsed = urlparse(url)
                resp = alice_http.post(parsed.path, content=body, headers=headers or {})
                calls.append(resp.status_code)
                if resp.status_code >= 400:
                    hdr_st = resp.headers.get("x-ryn-server-time")
                    raise TransportError(
                        f"http status {resp.status_code}",
                        reason="http_error",
                        status=resp.status_code,
                        server_time=int(hdr_st) if hdr_st and hdr_st.isdigit() else None,
                    )
                return resp.content

        fake_transport = _Fake409Transport()

        def real_post_json(endpoint, path, payload, headers, *, max_response_bytes=2 * 1024 * 1024):
            return HttpPeerClient(endpoint, timeout_s=5, transport=fake_transport).post_json(
                path, payload, headers=headers, max_bytes=max_response_bytes
            )

        bob_home = tmp_path / "bob_real"
        bob_rstore = RynmeshStore(home=bob_home, network_dir=tmp_path / "net_real", node_name="BobReal")
        bob_msg = peer_box.load_or_create_messaging_key(bob_home / "messaging.x25519")
        bob_svc = FriendService(
            home=bob_home,
            peer_id=bob_rstore.peer_id,
            node_name="BobReal",
            endpoint="http://127.0.0.1:19202",
            identity_private=bob_rstore.private_key_bytes,
            messaging_private=bob_msg,
            post_json=real_post_json,
            allow_loopback=True,
        )

        created = alice_svc.create_invite(reviewed_endpoint="http://127.0.0.1:19201")
        bob_svc.join(created["invite_uri"])

        base_time = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
        bob_svc.clock = lambda: base_time
        alice_svc.clock = lambda: base_time + timedelta(minutes=5)

        calls.clear()
        sent = bob_svc.send_message(alice_svc.peer_id, text="real post_json skew calibration")
        assert sent["delivered"] is True
        assert calls == [409, 200]
        assert bob_svc._clock_offsets.get(alice_svc.peer_id) == 300
        assert alice_svc.history(bob_svc.peer_id)[0]["text"] == "real post_json skew calibration"

    # Also verify StdlibHttpsTransport extracts X-Ryn-Server-Time from urllib HTTPError(409)
    hdrs = email.message.Message()
    hdrs["X-Ryn-Server-Time"] = "1700000300"
    stdlib_transport = StdlibHttpsTransport(TransportProfile())

    def _raise_409(req, timeout=None):
        raise HTTPError(req.full_url, 409, "Conflict", hdrs, None)

    monkeypatch.setattr(stdlib_transport._opener, "open", _raise_409)
    with pytest.raises(TransportError) as t_exc:
        stdlib_transport.post_bytes("http://127.0.0.1:19201/api/peer/friends/message", b"{}", timeout_s=5, max_bytes=1024)
    assert t_exc.value.status == 409
    assert t_exc.value.server_time == 1700000300
