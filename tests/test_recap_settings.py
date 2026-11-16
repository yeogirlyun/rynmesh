"""Tests for S13-I15 Steps 4 & 5 (T3, T4, T5): SMTP password protection/migration and FriendStore shared secret protection/migration."""
from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

from rynmesh.friends.crypto import auth_headers, verify_auth
from rynmesh.friends.store import FriendStore
from rynmesh.peer_http import create_app
from rynmesh.services import recap as recap_service
from rynmesh.store import RynmeshStore


def _make_app_client(home: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    return TestClient(create_app(RynmeshStore()))


def test_t3_recap_smtp_password_saved_protected_never_plaintext_and_unprotected_on_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3 (covers 4): Saving recap settings stores recap.smtp_password_protected (no plaintext in settings.json), returns has_smtp_password without echoing password, and unprotects when sending."""
    home = tmp_path / "node-t3"
    client = _make_app_client(home, monkeypatch)
    secret_pw = "MySmtpSecret#2026!"

    resp = client.patch(
        "/api/local/recap/settings",
        json={
            "to_address": "owner@example.com",
            "from_address": "bot@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "bot@example.com",
            "smtp_password": secret_pw,
            "enabled": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_smtp_password"] is True
    assert "smtp_password" not in body
    assert "smtp_password_protected" not in body
    assert secret_pw not in json.dumps(body, ensure_ascii=False)

    settings_path = home / "settings.json"
    raw_text = settings_path.read_text(encoding="utf-8")
    assert secret_pw not in raw_text
    assert base64.b64encode(secret_pw.encode("utf-8")).decode("ascii") not in raw_text
    parsed = json.loads(raw_text)
    assert "smtp_password" not in parsed["recap"]
    assert isinstance(parsed["recap"].get("smtp_password_protected"), str)
    assert parsed["recap"]["smtp_password_protected"]

    # Verify send path unprotects and passes exact password to send_email
    captured: list[str] = []

    def fake_send_email(message: Any, *, config: recap_service.RecapConfig) -> dict[str, Any]:
        captured.append(config.smtp_password)
        return {"sent": True, "to": config.to_address}

    monkeypatch.setattr(recap_service, "send_email", fake_send_email)
    monkeypatch.setattr(recap_service, "render_pdf", lambda *a, **kw: b"%PDF-stub")
    send_resp = client.post("/api/local/recap/send")
    assert send_resp.status_code == 200
    assert captured == [secret_pw]


def test_t4_legacy_plaintext_smtp_migrates_and_new_friend_relationship_stores_no_plaintext_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4 (covers 4, 5): Legacy plaintext recap.smtp_password in settings.json is migrated and deleted on startup; newly established friend relationship writes secrets_protected with no plaintext secret in state.json (while protection='file' keeps legacy format and reports secret_protection='file')."""
    home = tmp_path / "node-t4"
    home.mkdir(parents=True, exist_ok=True)
    legacy_pw = "LegacyPlaintextSmtpPw!99"
    (home / "settings.json").write_text(
        json.dumps(
            {
                "recap": {
                    "to_address": "owner@example.com",
                    "smtp_host": "smtp.example.com",
                    "smtp_user": "owner@example.com",
                    "smtp_password": legacy_pw,
                }
            }
        ),
        encoding="utf-8",
    )

    client = _make_app_client(home, monkeypatch)

    # 1. Startup / first read migrated settings.json and removed plaintext smtp_password
    disk_after_start = (home / "settings.json").read_text(encoding="utf-8")
    assert legacy_pw not in disk_after_start
    parsed_after = json.loads(disk_after_start)
    assert "smtp_password" not in parsed_after["recap"]
    assert parsed_after["recap"].get("smtp_password_protected")

    get_body = client.get("/api/local/recap/settings").json()
    assert get_body["has_smtp_password"] is True
    assert legacy_pw not in json.dumps(get_body)

    cfg = recap_service.RecapConfig.from_settings(parsed_after, port=8791)
    assert cfg.smtp_password == legacy_pw

    # 2. F2: Creating a friend relationship stores secrets_protected with zero plaintext secret in state.json
    fstore = FriendStore(home)
    shared_secret = b"0123456789abcdef0123456789abcdef"
    plain_b64 = base64.b64encode(shared_secret).decode("ascii")
    rel_record = {
        "relationship_id": "rel_alice_1",
        "peer_id": "peer_alice",
        "node_name": "Alice",
        "endpoint": "https://alice.example.com",
        "messaging_pub": base64.b64encode(
            X25519PrivateKey.generate().public_key().public_bytes_raw()
        ).decode("ascii"),
        "permissions": ["friend.message"],
        "status": "active",
        "created_at": "2026-11-16T00:00:00Z",
    }
    fstore.put_relationship(rel_record, shared_secret)

    state_raw = (home / "friends" / "state.json").read_text(encoding="utf-8")
    state_json = json.loads(state_raw)
    os_secret = importlib.import_module("rynmesh.os_secret")
    if os_secret.protection_mode() != "file":
        assert plain_b64 not in state_raw
        assert "secrets" not in state_json
        assert "rel_alice_1" in state_json.get("secrets_protected", {})
    assert fstore.secret("rel_alice_1") == shared_secret

    # 3. On protection='file' platform, keeps plaintext 'secrets' format and /api/local/friends reports secret_protection='file'
    monkeypatch.setattr(os_secret.sys, "platform", "linux")
    file_home = tmp_path / "node-file-mode"
    file_client = _make_app_client(file_home, monkeypatch)
    fstore_file = FriendStore(file_home)
    fstore_file.put_relationship(rel_record, shared_secret)
    file_state_json = json.loads((file_home / "friends" / "state.json").read_text(encoding="utf-8"))
    assert file_state_json.get("secrets", {}).get("rel_alice_1") == plain_b64
    assert "secrets_protected" not in file_state_json

    friends_resp = file_client.get("/api/local/friends")
    assert friends_resp.status_code == 200
    assert friends_resp.json().get("secret_protection") == "file"


def test_t5_legacy_friend_state_migrates_on_startup_and_friend_auth_succeeds(
    tmp_path: Path,
) -> None:
    """T5 (covers 5): Legacy friends/state.json with base64 plaintext 'secrets' is migrated to 'secrets_protected' on startup, and friend HMAC authentication continues to verify cleanly."""
    home = tmp_path / "node-t5"
    friends_dir = home / "friends"
    friends_dir.mkdir(parents=True, exist_ok=True)

    shared_secret = b"fedcba9876543210fedcba9876543210"
    plain_b64 = base64.b64encode(shared_secret).decode("ascii")
    rid = "rel_bob_legacy"
    legacy_state = {
        "version": "ryn.friends.v2",
        "invites": {},
        "relationships": {
            rid: {
                "relationship_id": rid,
                "peer_id": "peer_bob",
                "node_name": "Bob",
                "endpoint": "https://bob.example.com",
                "messaging_pub": base64.b64encode(
                    X25519PrivateKey.generate().public_key().public_bytes_raw()
                ).decode("ascii"),
                "permissions": ["friend.message"],
                "status": "active",
                "created_at": "2026-11-15T00:00:00Z",
            }
        },
        "nonces": {},
        "cards": {},
        "secrets": {rid: plain_b64},
    }
    state_path = friends_dir / "state.json"
    state_path.write_text(json.dumps(legacy_state, indent=2), encoding="utf-8")

    # Startup instantiation migrates legacy state.json in place
    store = FriendStore(home)
    migrated_raw = state_path.read_text(encoding="utf-8")
    migrated_json = json.loads(migrated_raw)
    os_secret = importlib.import_module("rynmesh.os_secret")
    if os_secret.protection_mode() != "file":
        assert plain_b64 not in migrated_raw
        assert "secrets" not in migrated_json
        assert rid in migrated_json.get("secrets_protected", {})

    loaded_secret = store.secret(rid)
    assert loaded_secret == shared_secret

    # Verify authentic friend request HMAC signature verification works end-to-end with the migrated secret
    payload_bytes = b'{"msg_id":"0123456789abcdef0123456789abcdef","text":"hello"}'
    now_ts = 1_794_800_000
    headers = auth_headers(
        shared_secret,
        method="POST",
        path="/api/peer/friends/messages",
        body=payload_bytes,
        sender="peer_bob",
        receiver="peer_local",
        relationship_id=rid,
        timestamp=now_ts,
        nonce="nonce-s13-i15-check",
    )
    verify_auth(
        loaded_secret,
        method="POST",
        path="/api/peer/friends/messages",
        body=payload_bytes,
        sender=headers["X-Ryn-Friend-Peer"],
        receiver="peer_local",
        relationship_id=headers["X-Ryn-Friend-Id"],
        timestamp=int(headers["X-Ryn-Friend-Time"]),
        nonce=headers["X-Ryn-Friend-Nonce"],
        signature=headers["X-Ryn-Friend-MAC"],
        now=now_ts,
    )
