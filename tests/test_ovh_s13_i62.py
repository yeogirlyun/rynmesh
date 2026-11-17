"""S13-I62: personal-space lifecycle and recovery regressions."""
import base64
import copy
import hashlib
import json
import sys

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from rynmesh.personal_space import (
    INVITE_PREFIX, LEASE_SECONDS, PersonalSpace, SpaceError, _decode, _name,
    main, restore_backup,
)
from test_personal_space import nodes, join


def direct(root, child, monkeypatch):
    monkeypatch.setattr(child, "_direct_exchange",
                        lambda order, authority: root._process(order, publish=False))


def invitation_key(invitation):
    return hashlib.sha256(_decode(invitation[len(INVITE_PREFIX):])["payload"]["token"].encode()).hexdigest()


def test_T1_invalid_sync_name_renews_without_renaming(nodes):
    root, child, *_ = nodes
    root.create("Home")
    join(root, child)
    previous = copy.deepcopy(root._snapshot())
    now = previous["issued_at"] + 3700
    root.clock = lambda: now
    result = root._apply("sync", {"name": "x" * 40}, child.store.peer_id)
    assert result["name_rejected"] is True
    assert root._snapshot()["members"][child.store.peer_id]["name"] == child.store.node_name
    assert root._snapshot()["valid_until"] == now + LEASE_SECONDS
    assert root._snapshot()["access_revision"] == previous["access_revision"]


def test_T1_wire_reply_persists_warning_and_outbound_name_is_clipped(nodes, monkeypatch):
    root, child, *_ = nodes
    root.create("Home")
    direct(root, child, monkeypatch)
    join(root, child)
    child._queue("sync", {"name": "x" * 40}, child._snapshot())
    child.tick()
    assert child.status()["name_rejected"] is True
    assert PersonalSpace(child.store).status()["name_rejected"] is True
    child.store.node_name = "z" * 40
    child.last_poll = 0
    child.tick()
    assert root._snapshot()["members"][child.store.peer_id]["name"] == "z" * 32
    assert child.status()["name_rejected"] is False


def test_T2_sixty_four_removed_devices_do_not_fill_space(nodes):
    root, child, *_ = nodes
    root.create("Home")
    for index in range(64):
        token = f"token-{index}"
        root.data.setdefault("invites", {})[hashlib.sha256(token.encode()).hexdigest()] = {
            "expires_at": root.clock() + 3600,
        }
        peer = f"removed-{index}"
        root._apply("join", {"token": token, "name": "Old"}, peer)
        root.act("remove", {"peer_id": peer})
        assert root._snapshot()["members"][peer]["removed_at"] > 0
    join(root, child)
    assert child.status()["membership"] == "active"
    assert len(root._verify(root.data["snapshot"])["members"]) == 66


def test_T2_removed_rejoin_cannot_bypass_active_limit_and_old_tombstones_prune(nodes):
    root, *_ = nodes
    root.create("Home")
    p = copy.deepcopy(root._snapshot())
    now = root.clock()
    p["members"].update({f"active-{n}": {"name": "Active", "role": "device", "removed": False}
                         for n in range(63)})
    p["members"]["removed"] = {"name": "Old", "role": "device", "removed": True,
                                 "removed_at": now - LEASE_SECONDS - 1}
    p["members"]["recent"] = {"name": "Recent", "role": "device", "removed": True,
                                "removed_at": now - 60}
    # A previously signed snapshot can contain a tombstone that has just aged out.
    from rynmesh.crypto import sign_payload
    root.data["snapshot"] = sign_payload(p, private_key_bytes=root.store.private_key_bytes).to_dict()
    access = p["access_revision"]
    inv = root.act("invite", {})["invitation"]
    token = _decode(inv[len(INVITE_PREFIX):])["payload"]["token"]
    with pytest.raises(SpaceError, match="device limit"):
        root._apply("join", {"token": token, "name": "Again"}, "recent")
    root._sign(root._snapshot())
    assert "removed" not in root._snapshot()["members"]
    assert "recent" in root._snapshot()["members"]
    assert root._snapshot()["access_revision"] == access


def test_T3_tick_prunes_invites_and_replies_and_only_saves_changes(nodes, monkeypatch):
    root, *_ = nodes
    root.create("Home")
    now = root.clock()
    root.clock = lambda: now
    root.data["invites"] = {"old": {"expires_at": now}, "live": {"expires_at": now + 50}}
    root.data["replies"] = {"old": {"expires": now}, "live": {"expires": now + 50}}
    saved = []
    monkeypatch.setattr(root, "_save", lambda: saved.append(copy.deepcopy(root.data)))
    root.tick()
    assert set(root.data["invites"]) == {"live"}
    assert set(root.data["replies"]) == {"live"}
    assert len(saved) == 1
    root.tick()
    assert len(saved) == 1


@pytest.mark.parametrize("kind", ["password", "missing", "base64", "json", "box"])
def test_T4_restore_errors_are_friendly_and_do_not_write_partial_identity(nodes, tmp_path, kind):
    root, *_ = nodes
    root.create("Home")
    password = "a long recovery password"
    bundle = root.backup(password)
    if kind == "password":
        password = "wrong password"
    elif kind == "missing":
        del bundle["salt"]
    elif kind == "base64":
        bundle["nonce"] = "?"
    else:
        salt = base64.b64decode(bundle["salt"])
        nonce = base64.b64decode(bundle["nonce"])
        aes = AESGCM(Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode()))
        payload = json.loads(aes.decrypt(nonce, base64.b64decode(bundle["ciphertext"]), b"ryn.space.recovery.v1"))
        payload["box"] = "?"
        raw = b"not json" if kind == "json" else json.dumps(payload).encode()
        bundle["ciphertext"] = base64.b64encode(aes.encrypt(nonce, raw, b"ryn.space.recovery.v1")).decode()
    destination = tmp_path / "restore"
    with pytest.raises(SpaceError, match="^恢复文件无效或密码错误$"):
        restore_backup(bundle, password, destination)
    assert not destination.exists() or list(destination.iterdir()) == []


@pytest.mark.parametrize("kind", ["password", "missing"])
def test_T4_cli_prints_friendly_error_and_exits_two(nodes, tmp_path, monkeypatch, capsys, kind):
    root, *_ = nodes
    root.create("Home")
    bundle = root.backup("a long recovery password")
    if kind == "missing":
        del bundle["salt"]
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps(bundle))
    monkeypatch.setattr(sys, "argv", ["restore", str(backup), str(tmp_path / "restored")])
    monkeypatch.setattr("getpass.getpass", lambda _: "wrong password")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "恢复文件无效或密码错误" in output.err + output.out
    assert "Traceback" not in output.err + output.out


def test_T5_created_invitation_has_persisted_record_id(nodes):
    root, *_ = nodes
    root.create("Home")
    invitation = root.act("invite", {})["invitation"]
    key = invitation_key(invitation)
    assert root.data["last_invitation_id"] == key
    assert key in root.data["invites"]
    assert PersonalSpace(root.store).status()["invitation"] == invitation


@pytest.mark.parametrize("reason", ["cancel", "expire", "use"])
def test_T6_invalidated_invitation_clears_both_fields(nodes, reason):
    root, child, *_ = nodes
    root.create("Home")
    inv = root.act("invite", {})["invitation"]
    key = invitation_key(inv)
    if reason == "cancel":
        root.act("cancel_invite", {"id": key})
    elif reason == "expire":
        expires = root.data["invites"][key]["expires_at"]
        root.clock = lambda: expires
        root.tick()
    else:
        join(root, child, inv)
    assert root.status()["invitation"] == ""
    assert "last_invitation" not in root.data
    assert "last_invitation_id" not in root.data
    assert PersonalSpace(root.store).status()["invitation"] == ""


def test_T5_T6_remote_manager_tracks_invitation_until_used(nodes, monkeypatch):
    root, manager, child, *_ = nodes
    root.create("Home")
    direct(root, manager, monkeypatch)
    direct(root, child, monkeypatch)
    join(root, manager)
    root.act("role", {"peer_id": manager.store.peer_id, "role": "manager"})
    manager.last_poll = 0
    manager.tick()
    manager.act("invite", {})
    manager.tick()
    inv = manager.status()["invitation"]
    assert inv.startswith(INVITE_PREFIX)
    key = invitation_key(inv)
    assert manager.data["last_invitation_id"] == key
    assert key in manager.data["invites"]
    join(root, child, inv)
    manager.last_poll = 0
    manager.tick()
    assert manager.status()["invitation"] == ""
    assert "last_invitation_id" not in manager.data


def test_T7_legacy_invitation_without_id_is_cleared_and_saved(nodes):
    root, *_ = nodes
    root.create("Home")
    root.data["last_invitation"] = "ryn-invite-v1:legacy"
    root._save()
    restored = PersonalSpace(root.store)
    assert restored.status()["invitation"] == ""
    assert "last_invitation" not in restored.data
    assert "last_invitation" not in json.loads(restored.path.read_text())


def test_T8_create_clips_local_device_name(nodes):
    root, *_ = nodes
    root.store.node_name = "长" * 40
    assert root.create("Home")["members"][0]["name"] == "长" * 32


@pytest.mark.parametrize("name", ["", "x" * 40])
def test_T9_name_errors_are_stable_codes(nodes, name):
    root, *_ = nodes
    with pytest.raises(SpaceError, match="^space_name_invalid$"):
        _name(name)
    with pytest.raises(SpaceError, match="^space_name_invalid$"):
        root.create(name)
