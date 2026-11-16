"""Tests for S13-I14: Windows ACL tightening and exclusive creation for local private keys."""
from __future__ import annotations

import ctypes as c
import logging
import os
import re
import threading
from ctypes import wintypes as w
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

import rynmesh.private_permissions as pp
import rynmesh.store as store_mod
from rynmesh.crypto import b64
from rynmesh.llm_package.api import InferenceKeys
from rynmesh.nas import NasPlugin
from rynmesh.peer_http import create_app
from rynmesh.personal_space import PersonalSpace, restore_backup
from rynmesh.private_permissions import (
    _DACL,
    _PROTECTED_DACL,
    _api,
    current_user_sid,
    read_windows_dacl,
)
from rynmesh.services import peer_box
from rynmesh.services.peer_box import load_or_create_messaging_key, public_key_b64
from rynmesh.store import RynmeshStore, load_or_create_private_key


def _assert_restricted_file_acl(path: Path) -> None:
    assert path.is_file(), f"Expected file at {path.name}"
    if os.name == "nt":
        dacl = read_windows_dacl(path)
        assert dacl.startswith("D:P"), f"DACL is not protected from inheritance: {dacl}"
        entries = re.findall(r"\(([^)]+)\)", dacl)
        allowed_sids = {"SY", "BA", current_user_sid()}
        found_sids = {entry.split(";")[-1] for entry in entries}
        assert found_sids == allowed_sids, f"Unexpected DACL trustees {found_sids} vs {allowed_sids} in {dacl}"
        assert all(entry.split(";")[0] == "A" and entry.split(";")[2] == "FA" for entry in entries), dacl
    else:
        assert (path.stat().st_mode & 0o777) == 0o600


def _loosen_windows_acl(path: Path) -> None:
    """Grant Everyone (WD) generic read so the DACL is demonstrably wider than SY/BA/owner."""
    if os.name != "nt":
        path.chmod(0o644)
        return
    adv, kernel = _api()
    wide_sddl = (
        f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{current_user_sid()})(A;;FR;;;WD)"
    )
    descriptor = c.c_void_p()
    assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(wide_sddl, 1, c.byref(descriptor), None)
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
        assert adv.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted))
        rc = adv.SetNamedSecurityInfoW(str(path), 1, _DACL | _PROTECTED_DACL, None, None, acl, None)
        assert rc == 0
    finally:
        kernel.LocalFree(descriptor)


def test_t1_new_keys_and_nas_files_have_expected_dacl(tmp_path: Path):
    id_path = tmp_path / "identity.ed25519"
    msg_path = tmp_path / "messaging.x25519"
    nas_home = tmp_path / "nas_home"

    priv_bytes = load_or_create_private_key(id_path)
    assert len(priv_bytes) == 32
    _assert_restricted_file_acl(id_path)

    msg_key = load_or_create_messaging_key(msg_path)
    assert len(msg_key.private_bytes_raw()) == 32
    _assert_restricted_file_acl(msg_path)

    nas = NasPlugin(nas_home)
    nas.save()
    _assert_restricted_file_acl(nas_home / "plugins" / "nas" / "key.protected")
    _assert_restricted_file_acl(nas_home / "plugins" / "nas" / "state.enc")


def test_t2_loosened_acl_is_retightened_on_reload(tmp_path: Path):
    id_path = tmp_path / "identity.ed25519"
    msg_path = tmp_path / "messaging.x25519"

    orig_id = load_or_create_private_key(id_path)
    orig_msg = load_or_create_messaging_key(msg_path).private_bytes_raw()

    _loosen_windows_acl(id_path)
    _loosen_windows_acl(msg_path)
    if os.name == "nt":
        assert "WD" in read_windows_dacl(id_path)
        assert "WD" in read_windows_dacl(msg_path)

    reloaded_id = load_or_create_private_key(id_path)
    reloaded_msg = load_or_create_messaging_key(msg_path).private_bytes_raw()

    assert reloaded_id == orig_id
    assert reloaded_msg == orig_msg
    _assert_restricted_file_acl(id_path)
    _assert_restricted_file_acl(msg_path)


def test_t3_restrict_windows_acl_failure_sets_weak_status_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    home = tmp_path / "node_home"
    net_dir = tmp_path / "net_dir"
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)

    def _always_fail_acl(*_args, **_kwargs):
        raise OSError("private_acl_write_failed")

    monkeypatch.setattr(pp, "restrict_windows_acl", _always_fail_acl)
    if hasattr(store_mod, "restrict_windows_acl"):
        monkeypatch.setattr(store_mod, "restrict_windows_acl", _always_fail_acl)
    if hasattr(peer_box, "restrict_windows_acl"):
        monkeypatch.setattr(peer_box, "restrict_windows_acl", _always_fail_acl)

    with caplog.at_level(logging.WARNING):
        store = RynmeshStore(home=home, network_dir=net_dir, node_name="weak-acl-node")
        InferenceKeys(home)
        app = create_app(store=store)
        with TestClient(app) as client:
            resp = client.get("/api/local/node/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("key_protection") == "weak"
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)

    # Also verify reload path when ACL was loosened and restrict_windows_acl raises OSError
    reload_dir = tmp_path / "reload_weak"
    reload_dir.mkdir()
    existing_msg = reload_dir / "messaging.x25519"
    existing_msg.write_bytes(os.urandom(32))
    load_or_create_messaging_key(existing_msg)
    assert pp.key_protection_status(reload_dir) == "weak"


def test_t4_concurrent_first_create_generates_single_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 1. Messaging key concurrent first-create race (deterministic barrier after existence check)
    msg_path = tmp_path / "race_messaging.x25519"
    orig_x25519_gen = X25519PrivateKey.generate
    msg_barrier = threading.Barrier(2)

    def _synced_x25519_gen():
        key = orig_x25519_gen()
        msg_barrier.wait(timeout=5.0)
        return key

    monkeypatch.setattr(X25519PrivateKey, "generate", staticmethod(_synced_x25519_gen))
    msg_results: list[str] = []
    msg_errors: list[BaseException] = []

    def _msg_worker():
        try:
            k = load_or_create_messaging_key(msg_path)
            msg_results.append(public_key_b64(k))
        except BaseException as exc:
            msg_errors.append(exc)

    t1 = threading.Thread(target=_msg_worker)
    t2 = threading.Thread(target=_msg_worker)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not msg_errors, f"Unexpected errors: {msg_errors}"
    assert len(msg_results) == 2
    assert msg_results[0] == msg_results[1], "Racing first-create overwrote messaging key!"
    monkeypatch.setattr(X25519PrivateKey, "generate", orig_x25519_gen)
    assert public_key_b64(load_or_create_messaging_key(msg_path)) == msg_results[0]

    # 2. Identity key concurrent first-create race
    id_path = tmp_path / "race_identity.ed25519"
    orig_id_gen = store_mod._generate_private_key
    id_barrier = threading.Barrier(2)

    def _synced_id_gen():
        raw = orig_id_gen()
        id_barrier.wait(timeout=5.0)
        return raw

    monkeypatch.setattr(store_mod, "_generate_private_key", _synced_id_gen)
    id_results: list[bytes] = []
    id_errors: list[BaseException] = []

    def _id_worker():
        try:
            id_results.append(load_or_create_private_key(id_path))
        except BaseException as exc:
            id_errors.append(exc)

    t3 = threading.Thread(target=_id_worker)
    t4 = threading.Thread(target=_id_worker)
    t3.start()
    t4.start()
    t3.join(timeout=5.0)
    t4.join(timeout=5.0)
    assert not id_errors, f"Unexpected errors: {id_errors}"
    assert len(id_results) == 2
    assert id_results[0] == id_results[1], "Racing first-create overwrote identity key!"
    monkeypatch.setattr(store_mod, "_generate_private_key", orig_id_gen)
    assert load_or_create_private_key(id_path) == id_results[0]


def test_t5_31_byte_key_rejected_unmodified_and_personal_space_and_inference_db_acl(tmp_path: Path):
    # Part A: 31-byte messaging key raises messaging_key_invalid and is NOT overwritten
    bad_msg = tmp_path / "bad_messaging.x25519"
    raw_31 = b"m" * 31
    bad_msg.write_bytes(raw_31)
    with pytest.raises(ValueError, match="messaging_key_invalid"):
        load_or_create_messaging_key(bad_msg)
    assert bad_msg.read_bytes() == raw_31

    # 31-byte identity key (encoded as base64 of 31 bytes) raises ValueError and is NOT overwritten
    bad_id = tmp_path / "bad_identity.ed25519"
    bad_id_text = b64(b"i" * 31) + "\n"
    bad_id.write_text(bad_id_text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_or_create_private_key(bad_id)
    assert bad_id.read_text(encoding="utf-8") == bad_id_text

    # Part B (F2 supplement): personal-space.json, restored identity.ed25519 + space-messaging.key, and inference-keys.sqlite3 DACL
    coord_home = tmp_path / "coord_home"
    coord_net = tmp_path / "coord_net"
    store = RynmeshStore(home=coord_home, network_dir=coord_net, node_name="CoordinatorNode")
    space = PersonalSpace(store)
    space.create("TeamSpace")
    _assert_restricted_file_acl(coord_home / "personal-space.json")
    _assert_restricted_file_acl(coord_home / "space-messaging.key")

    recovery_bundle = space.backup("correct-horse-battery-staple")
    restored_home = tmp_path / "restored_home"
    restore_backup(recovery_bundle, "correct-horse-battery-staple", restored_home)
    _assert_restricted_file_acl(restored_home / "identity.ed25519")
    _assert_restricted_file_acl(restored_home / "space-messaging.key")
    _assert_restricted_file_acl(restored_home / "personal-space.json")
    assert load_or_create_private_key(restored_home / "identity.ed25519") == store.private_key_bytes

    inf_home = tmp_path / "inf_home"
    InferenceKeys(inf_home)
    _assert_restricted_file_acl(inf_home / "llm" / "inference-keys.sqlite3")
