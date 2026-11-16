"""Tests for S13-I3: per-task corrupt/migration isolation, .prev root fallback, delayed blob deletion, and quarantine."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import state as state_mod
from rynmesh.agent_runtime import store as storage
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded
from rynmesh.atomic_io import atomic_write_json
from rynmesh.services import peer_box


def _make_task_row(task_id: str, text: str = "hello") -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "status": "completed",
        "events": [],
        "messages": [{"role": "user", "content": text}],
    }
    state_mod.initialize(row)
    return row


def _seed_tasks(store: AgentStore, *task_ids: str) -> None:
    def apply(data: dict[str, Any]) -> None:
        for tid in task_ids:
            data["tasks"][tid] = _make_task_row(tid, f"payload-{tid}")

    store.transaction(apply)


def _write_custom_task_blob(store: AgentStore, task_id: str, raw_row: dict[str, Any]) -> dict[str, Any]:
    """Write a raw task dict into tasks/ and splice its entry into sessions.json's task_blobs."""
    raw_bytes = encoded(raw_row)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    fname = storage._blob_name(task_id, digest)
    tasks_dir = store.root / storage.TASKS_DIR
    tasks_dir.mkdir(parents=True, exist_ok=True)
    nonce, ciphertext = peer_box.seal(store.key, store.pub, raw_bytes, info=storage.TASK_CHANNEL)
    atomic_write_json(
        tasks_dir / fname,
        {"version": storage.TASK_VERSION, "nonce": nonce, "ciphertext": ciphertext},
        max_bytes=storage.MAX_BYTES,
    )

    envelope = json.loads(store.path.read_bytes())
    root_plain = json.loads(
        peer_box.open_sealed(store.key, store.pub, envelope["nonce"], envelope["ciphertext"], info=storage.CHANNEL)
    )
    entry = {"file": fname, "sha256": digest, "bytes": len(raw_bytes)}
    root_plain["task_blobs"][task_id] = entry
    r_nonce, r_cipher = peer_box.seal(store.key, store.pub, encoded(root_plain), info=storage.CHANNEL)
    atomic_write_json(
        store.path,
        {"version": storage.ROOT_VERSION, "nonce": r_nonce, "ciphertext": r_cipher},
        max_bytes=storage.MAX_BYTES,
    )
    return entry


def _legacy_migrate_check(data: dict[str, Any]) -> dict[str, Any]:
    """Reference pre-S13-I3 migrate check to verify old Ryn ignores unknown root keys like damaged_tasks."""
    data = copy.deepcopy(data)
    version = data.get("schema_version", 1)
    revision = data.setdefault("settings_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid Agent settings revision")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("unsupported Agent payload schema")
    for row in data["tasks"].values():
        row_version = row.get("schema_version", 1)
        if type(row_version) is not int or row_version not in {1, 2}:
            raise ValueError("unsupported Agent task schema")
        if row_version == 2:
            for required in (
                "goal",
                "criteria",
                "plan",
                "control_revision",
                "event_cursor",
                "pending_input",
                "outcome",
                "scope_revision",
                "scope",
            ):
                if required not in row:
                    raise ValueError("incomplete Agent task state")
    return data


def test_t1_corrupt_single_blob_byte_isolates_task_and_quarantines_on_next_transaction(
    tmp_path: Path,
) -> None:
    """T1 (covers 1, 2): Corrupting one byte of one task blob keeps other tasks readable, records damaged_tasks + _index damaged marker without writing on read, and moves the corrupt blob to quarantine/<ts>-<seq>/ on the next transaction."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _seed_tasks(store, "t1", "t2", "t3")

    bad_file = store._index["t2"]["file"]
    bad_path = tmp_path / storage.TASKS_DIR / bad_file
    blob_env = json.loads(bad_path.read_text(encoding="utf-8"))
    ct = bytearray(blob_env["ciphertext"].encode("ascii"))
    ct[10] = ord("A") if ct[10] != ord("A") else ord("B")
    blob_env["ciphertext"] = ct.decode("ascii")
    bad_path.write_text(json.dumps(blob_env), encoding="utf-8")

    reader = AgentStore(tmp_path, key)
    loaded = reader.read()
    assert set(loaded["tasks"]) == {"t1", "t3"}
    assert loaded["tasks"]["t1"]["messages"][0]["content"] == "payload-t1"
    assert loaded["tasks"]["t3"]["messages"][0]["content"] == "payload-t3"
    assert "t2" in loaded["damaged_tasks"]
    dmg = loaded["damaged_tasks"]["t2"]
    assert dmg["file"] == bad_file
    assert isinstance(dmg["reason"], str) and dmg["reason"]
    assert isinstance(dmg["at"], int) and dmg["at"] > 0
    assert reader._index["t2"]["damaged"] is True
    # Read path must not write or move files yet
    assert bad_path.exists()
    assert not (tmp_path / "quarantine").exists()

    # Next transaction moves corrupt blob to quarantine/<unix_sec>-<seq>/ via os.replace and drops it from index
    reader.transaction(lambda _data: None)
    assert not bad_path.exists()
    assert "t2" not in reader._index

    q_root = tmp_path / "quarantine"
    assert q_root.is_dir()
    q_batches = [p for p in q_root.iterdir() if p.is_dir()]
    assert len(q_batches) == 1
    assert "-" in q_batches[0].name
    quarantined_blob = q_batches[0] / bad_file
    assert quarantined_blob.is_file()

    # Persisted root retains damaged_tasks and remains readable by fresh store and legacy migrate
    reopened = AgentStore(tmp_path, key).read()
    assert set(reopened["tasks"]) == {"t1", "t3"}
    assert reopened["damaged_tasks"]["t2"]["file"] == bad_file
    checked_by_old = _legacy_migrate_check(reopened)
    assert set(checked_by_old["tasks"]) == {"t1", "t3"}


def test_t2_truncated_root_recovers_from_prev_and_records_lost_post_prev_tasks(
    tmp_path: Path,
) -> None:
    """T2 (covers 2, 3): Truncating sessions.json falls back to sessions.json.prev, sets recovered_from_prev=True, and records task IDs created after .prev in damaged_tasks."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _seed_tasks(store, "t1")
    # Second commit writes sessions.json.prev (containing t1) and new sessions.json (containing t1 + t2)
    store.transaction(lambda data: data["tasks"].update(t2=_make_task_row("t2", "payload-t2")))
    assert (tmp_path / "sessions.json.prev").is_file()

    raw_root = store.path.read_bytes()
    store.path.write_bytes(raw_root[:24])

    recovered_store = AgentStore(tmp_path, key)
    recovered = recovered_store.read()
    assert recovered.get("recovered_from_prev") is True
    assert set(recovered["tasks"]) == {"t1"}
    assert recovered["tasks"]["t1"]["messages"][0]["content"] == "payload-t1"
    assert "t2" in recovered["damaged_tasks"]
    assert recovered["damaged_tasks"]["t2"]["file"] is not None


def test_t3_both_root_and_prev_corrupt_raises_agent_state_corrupt(
    tmp_path: Path,
) -> None:
    """T3 (covers 1, 3): When both sessions.json and sessions.json.prev are corrupt (or wrong key / bad version), read raises agent_state_corrupt."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _seed_tasks(store, "t1")
    store.transaction(lambda data: data["tasks"].update(t2=_make_task_row("t2")))

    # Wrong key fails decryption on both root and .prev -> agent_state_corrupt
    with pytest.raises(AgentError, match="^agent_state_corrupt$"):
        AgentStore(tmp_path, X25519PrivateKey.generate()).read()

    # Truncate both sessions.json and sessions.json.prev -> agent_state_corrupt
    (tmp_path / "sessions.json").write_bytes(b"{truncated")
    (tmp_path / "sessions.json.prev").write_bytes(b"{also-corrupt")
    with pytest.raises(AgentError, match="^agent_state_corrupt$"):
        AgentStore(tmp_path, key).read()


def test_t4_permission_error_raises_agent_state_unavailable_without_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4 (covers 1): PermissionError / transient OSError raises agent_state_unavailable and does NOT isolate tasks or move blobs."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _seed_tasks(store, "t1", "t2")
    t2_file = store._index["t2"]["file"]

    orig_read_json = storage.read_json

    # 1. PermissionError on task blob
    def deny_blob(path: str | Path, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == t2_file:
            raise PermissionError("transient lock on blob")
        return orig_read_json(path, *args, **kwargs)

    monkeypatch.setattr(storage, "read_json", deny_blob)
    fresh = AgentStore(tmp_path, key)
    with pytest.raises(AgentError, match="^agent_state_unavailable$"):
        fresh.read()
    assert not (tmp_path / "quarantine").exists()

    # 2. PermissionError on root sessions.json (must NOT fall back to .prev or quarantine)
    def deny_root(path: str | Path, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "sessions.json":
            raise PermissionError("transient lock on root")
        return orig_read_json(path, *args, **kwargs)

    monkeypatch.setattr(storage, "read_json", deny_root)
    with pytest.raises(AgentError, match="^agent_state_unavailable$"):
        AgentStore(tmp_path, key).read()

    # 3. Once PermissionError clears, all tasks load cleanly with zero damaged_tasks
    monkeypatch.setattr(storage, "read_json", orig_read_json)
    restored = AgentStore(tmp_path, key).read()
    assert set(restored["tasks"]) == {"t1", "t2"}
    assert not restored.get("damaged_tasks")


def test_t5_purge_keeps_prev_blobs_leaves_quarantine_and_isolates_task_migration_errors(
    tmp_path: Path,
) -> None:
    """T5 (covers 2, 4, 5): purge_orphan_snapshots treats .prev blobs as live and never touches quarantine/; missing 'plan' or schema_version=99 tasks are isolated into damaged_tasks while healthy tasks load."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _seed_tasks(store, "t1", "t2")
    t1_blob = store._index["t1"]["file"]
    t2_blob = store._index["t2"]["file"]

    # Pop t1: t1_blob is now only in sessions.json.prev (delayed 1 commit)
    store.transaction(lambda data: data["tasks"].pop("t1"))
    tasks_dir = tmp_path / storage.TASKS_DIR
    assert (tasks_dir / t1_blob).is_file()

    # Plant an unreferenced orphan blob, a temp file, and a quarantine artifact
    orphan_blob = tasks_dir / ("f" * 24 + ".json")
    orphan_blob.write_text("{}", encoding="utf-8")
    orphan_tmp = tasks_dir / (".orphan." + "a" * 32 + ".tmp")
    orphan_tmp.write_text("{}", encoding="utf-8")
    q_sample = tmp_path / "quarantine" / "1700000000-1" / ("e" * 24 + ".json")
    q_sample.parent.mkdir(parents=True, exist_ok=True)
    q_sample.write_text('{"kept":true}', encoding="utf-8")

    store.purge_orphan_snapshots()
    assert not orphan_blob.exists()
    assert not orphan_tmp.exists()
    assert (tasks_dir / t1_blob).is_file(), ".prev-referenced blob must be kept alive"
    assert (tasks_dir / t2_blob).is_file()
    assert q_sample.is_file(), "quarantine/ must not be touched by purge_orphan_snapshots"

    # Next commit advances .prev past t1 -> t1_blob is now retired and unlinked
    store.transaction(lambda data: data["settings"].update(enabled=True))
    assert not (tasks_dir / t1_blob).exists()

    # F2: per-task migration errors (missing 'plan', schema_version=99, schema_version=0) isolated
    missing_plan = _make_task_row("t_no_plan")
    missing_plan.pop("plan")
    _write_custom_task_blob(store, "t_no_plan", missing_plan)

    newer_schema = _make_task_row("t_v99")
    newer_schema["schema_version"] = 99
    _write_custom_task_blob(store, "t_v99", newer_schema)

    bad_schema = _make_task_row("t_v0")
    bad_schema["schema_version"] = 0
    _write_custom_task_blob(store, "t_v0", bad_schema)

    reloaded = AgentStore(tmp_path, key).read()
    assert set(reloaded["tasks"]) == {"t2"}
    assert reloaded["damaged_tasks"]["t_no_plan"]["reason"] == "incomplete"
    assert reloaded["damaged_tasks"]["t_v99"]["reason"] == "schema_newer"
    assert reloaded["damaged_tasks"]["t_v0"]["reason"] == "schema_unsupported"


def test_t6_schema_newer_blob_is_not_quarantined_or_deleted_across_writes(
    tmp_path: Path,
) -> None:
    """T6 (covers 2, 4, 5): A task with schema_version > SCHEMA_VERSION (schema_newer) is skipped during load but its blob stays in tasks/ (never moved to quarantine or unlinked) across multiple transactions and purges."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _seed_tasks(store, "t_ok")

    future_row = _make_task_row("t_future", "from-newer-ryn")
    future_row["schema_version"] = 99
    future_entry = _write_custom_task_blob(store, "t_future", future_row)
    future_blob_path = tmp_path / storage.TASKS_DIR / future_entry["file"]

    s2 = AgentStore(tmp_path, key)
    first_read = s2.read()
    assert set(first_read["tasks"]) == {"t_ok"}
    assert first_read["damaged_tasks"]["t_future"]["reason"] == "schema_newer"

    # Perform two transactions (so .prev also rotates) + purge_orphan_snapshots
    s2.transaction(lambda data: data["tasks"]["t_ok"]["messages"].append({"role": "assistant", "content": "step 1"}))
    s2.transaction(lambda data: data["tasks"]["t_ok"]["messages"].append({"role": "assistant", "content": "step 2"}))
    s2.purge_orphan_snapshots()

    assert future_blob_path.is_file(), "schema_newer blob must stay in tasks/ for future upgrade"
    q_dir = tmp_path / "quarantine"
    if q_dir.exists():
        quarantined_names = [p.name for p in q_dir.rglob("*.json")]
        assert future_entry["file"] not in quarantined_names

    # Verify root task_blobs still preserves t_future so upgrading Ryn can load it back
    env = json.loads(s2.path.read_bytes())
    root_plain = json.loads(peer_box.open_sealed(key, s2.pub, env["nonce"], env["ciphertext"], info=storage.CHANNEL))
    assert root_plain["task_blobs"].get("t_future") == {
        "file": future_entry["file"],
        "sha256": future_entry["sha256"],
        "bytes": future_entry["bytes"],
    }
