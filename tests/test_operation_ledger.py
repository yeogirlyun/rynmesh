"""Tests for S13-I32: per-record encrypted OperationLedger, retired tombstone exclusion & 30-day pruning,
single-record quarantine vs whole-ledger reset on key change, legacy migration, and FriendSharingTool parity."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import operation_ledger
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import CHANNEL, OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.sharing import FriendSharingTool
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.atomic_io import atomic_write_json
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box


def _write_legacy_single_file(home: Path, records: dict) -> Path:
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    pub = peer_box.public_key_b64(key)
    raw = encoded(records)
    nonce, ciphertext = peer_box.seal(key, pub, raw, info=CHANNEL)
    legacy_file = home / "agent" / "business-operations.json"
    atomic_write_json(legacy_file, {"nonce": nonce, "ciphertext": ciphertext}, max_bytes=32 * 1024 * 1024)
    return legacy_file


class _FakeSourcesStore:
    def __init__(self, home: Path, records: dict):
        import threading

        self.lock = threading.RLock()
        home.mkdir(parents=True, exist_ok=True)
        self.path = home / "sources.enc"
        self.path.write_bytes(b"placeholder-sources-enc")
        self._records = records

    def read(self):
        import copy

        return copy.deepcopy(self._records)

    def _write(self, records):
        import copy

        self._records = copy.deepcopy(records)

    @staticmethod
    def model_service_id(record, model):
        return SourceStore.model_service_id(record, model)

    @staticmethod
    def shared_models(record):
        return SourceStore.shared_models(record)


class _FakeAccessStore:
    def __init__(self, rel_id: str):
        self.rel_id = rel_id
        self.sources = None

    def relationship(self, rid: str):
        if rid != self.rel_id:
            return None
        return {
            "relationship_id": rid,
            "peer_id": "peer_bob",
            "node_name": "Bob",
            "status": "active",
            "revision": 1,
            "signing_pub": "sig_bob",
            "messaging_pub": "msg_bob",
        }

    def list(self):
        return []

    @staticmethod
    def _public(row):
        return dict(row)


def _make_friend_sharing_tool(tmp_path: Path, initial_ops: dict | None = None):
    rel_id = "a" * 32
    source_record = {
        "id": "src1",
        "name": "LocalOpenAI",
        "kind": "openai_compatible",
        "provider": "openai",
        "base_url": "https://api.example.test/v1",
        "models": ["gpt-test"],
        "enabled": True,
        "status": "ready",
        "source_revision": "rev_init",
        "shared_models": [],
        "_sharing_operations": dict(initial_ops or {}),
    }
    model_settings.reconcile(source_record, {})
    sources = _FakeSourcesStore(tmp_path, {"src1": source_record})
    access_store = _FakeAccessStore(rel_id)
    app = SimpleNamespace(state=SimpleNamespace(ai_access=SimpleNamespace(store=access_store, recheck=None)))
    tool = FriendSharingTool(app, {"source_store": sources})
    return tool, sources, rel_id


def _seed_per_record_tombstones(home: Path, count: int) -> Path:
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    pub = peer_box.public_key_b64(key)
    ops_dir = home / "agent" / "business-operations"
    ops_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        op_id = f"op_ret_{i}"
        row = {
            "binding": {"operation_id": op_id, "task_id": "deleted_task"},
            "status": "retired",
            "prior_status": "confirmed",
            "retired_at": datetime.now(timezone.utc).isoformat(),
            "result_sha256": "0" * 64,
        }
        nonce, ciphertext = peer_box.seal(key, pub, encoded(row), info=CHANNEL)
        fname = f"{hashlib.sha256(op_id.encode('utf-8')).hexdigest()[:24]}.json"
        (ops_dir / fname).write_text(json.dumps({"nonce": nonce, "ciphertext": ciphertext}), encoding="utf-8")
    return ops_dir


def test_t1_5000_retired_tombstones_do_not_block_new_operations(tmp_path):
    """T1: 5000 个 retired 占位不阻止新操作（同时验证 retired 仍防重放，且 begin / begin_dispatched 薄包装均正常）。"""
    _seed_per_record_tombstones(tmp_path, 5000)

    ledger = OperationLedger(tmp_path)
    # Replaying an already-retired operation_id is still rejected.
    with pytest.raises(AgentToolRejected, match="agent_operation_retired"):
        ledger.begin({"operation_id": "op_ret_0", "task_id": "deleted_task"}, {})

    # A new operation succeeds despite 5000 retired tombstones on disk.
    row, created = ledger.begin({"operation_id": "op_fresh_1", "task_id": "live_task"}, {"title": "Create"})
    assert created is True
    assert row["status"] == "intent"

    disp_row, disp_created = ledger.begin_dispatched(
        {"operation_id": "op_fresh_2", "task_id": "live_task"},
        {"title": "Dispatch"},
        operation_classification={"version": 1, "effect_class": "write"},
        host_pid=12345,
    )
    assert disp_created is True
    assert disp_row["status"] == "dispatched"
    assert disp_row["host_pid"] == 12345


def test_t2_retired_tombstones_pruned_after_30_days(tmp_path):
    """T2: forget_task 记录 retired_at，30 天后占位被 prune_retired 清理，未满 30 天和活跃记录保留。"""
    ledger = OperationLedger(tmp_path)
    ledger.begin({"operation_id": "op_old", "task_id": "task_old"}, {})
    ledger.update("op_old", "confirmed", result={"ok": 1})
    ledger.begin({"operation_id": "op_recent", "task_id": "task_recent"}, {})
    ledger.update("op_recent", "confirmed", result={"ok": 2})
    ledger.begin({"operation_id": "op_active", "task_id": "task_active"}, {})

    ledger.forget_task("task_old")
    ledger.forget_task("task_recent")

    old_tomb = ledger.get("op_old")
    recent_tomb = ledger.get("op_recent")
    assert old_tomb["status"] == "retired" and old_tomb.get("retired_at")
    assert recent_tomb["status"] == "retired" and recent_tomb.get("retired_at")

    # Backdate only op_old to 31 days ago.
    expired_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    ledger._write_one("op_old", {**old_tomb, "retired_at": expired_ts})

    removed = ledger.prune_retired(older_than_days=30)
    assert removed == 1
    assert ledger.get("op_old") is None
    assert ledger.get("op_recent")["status"] == "retired"
    assert ledger.get("op_active")["status"] == "intent"


def test_t3_single_corrupt_record_isolated_to_quarantine_and_key_mismatch_reset(tmp_path):
    """T3: 一条记录损坏只隔离那一条（移到 quarantine/，任务对账为 unknown），其余正常；整目录钥匙换了可重置账本并将未决写标为 unknown。"""
    ledger = OperationLedger(tmp_path)
    b_good = {"operation_id": "op_good", "task_id": "t1", "tool": "ryn_file_write", "arguments_sha256": "a" * 64}
    b_bad_json = {"operation_id": "op_bad_json", "task_id": "t1", "tool": "ryn_file_write", "arguments_sha256": "b" * 64}
    b_bad_tag = {"operation_id": "op_bad_tag", "task_id": "t1", "tool": "ryn_file_write", "arguments_sha256": "c" * 64}

    ledger.begin(b_good, {"p": 1})
    ledger.update("op_good", "confirmed", result={"written": True})
    ledger.begin(b_bad_json, {"p": 2})
    ledger.begin(b_bad_tag, {"p": 3})

    bad_json_file = tmp_path / "agent" / "business-operations" / f"{hashlib.sha256(b'op_bad_json').hexdigest()[:24]}.json"
    bad_tag_file = tmp_path / "agent" / "business-operations" / f"{hashlib.sha256(b'op_bad_tag').hexdigest()[:24]}.json"
    assert bad_json_file.is_file() and bad_tag_file.is_file()

    # Corrupt op_bad_json with truncated JSON, and corrupt op_bad_tag ciphertext (AEAD InvalidTag).
    bad_json_file.write_text("{truncated", encoding="utf-8")
    env_tag = json.loads(bad_tag_file.read_text(encoding="utf-8"))
    ct = env_tag["ciphertext"]
    flipped = ("A" if ct[0] != "A" else "B") + ct[1:]
    bad_tag_file.write_text(json.dumps({"nonce": env_tag["nonce"], "ciphertext": flipped}), encoding="utf-8")

    # Good record and new operations continue unaffected.
    assert ledger.get("op_good")["status"] == "confirmed"
    _, created = ledger.begin({"operation_id": "op_still_works", "task_id": "t1"}, {})
    assert created is True

    # Reading the corrupt records isolates only those files to agent/quarantine/ and returns None -> reconcile unknown.
    assert ledger.get("op_bad_json") is None
    assert ledger.get("op_bad_tag") is None
    assert not bad_json_file.exists()
    assert not bad_tag_file.exists()
    quarantined_files = list((tmp_path / "agent" / "quarantine").rglob("*.json"))
    quarantined_names = {p.name for p in quarantined_files}
    assert bad_json_file.name in quarantined_names
    assert bad_tag_file.name in quarantined_names

    local_tools = LocalTools(SimpleNamespace(home=tmp_path), lambda: ledger)
    rec = local_tools.reconcile(
        "op_bad_tag",
        {
            "task_id": "t1",
            "operation": {"operation_id": "op_bad_tag", "tool": "ryn_file_write", "arguments_sha256": "c" * 64},
        },
    )
    assert rec["outcome"] == "unknown"

    # Whole-directory unreadable when messaging.x25519 is replaced: raises unreadable and supports owner reset.
    key_path = tmp_path / "messaging.x25519"
    new_key = X25519PrivateKey.generate()
    key_path.write_bytes(new_key.private_bytes_raw())

    rekeyed_ledger = OperationLedger(tmp_path)
    with pytest.raises(AgentError, match="agent_operation_ledger_unreadable"):
        rekeyed_ledger.begin({"operation_id": "op_blocked_until_reset", "task_id": "t2"}, {})

    store = AgentStore(tmp_path / "agent", new_key)
    fake_tools = SimpleNamespace(
        resource_writes=SimpleNamespace(ledger=rekeyed_ledger),
        tool_catalog=lambda: [],
    )
    model_meta = {"id": "m1", "source": "local_api", "available": True, "agent_supported": True}
    fake_catalog = SimpleNamespace(
        list_models=lambda: [model_meta],
        display_models=lambda: [model_meta],
        display_model=lambda mid: model_meta if mid == "m1" else None,
        get_model=lambda mid: model_meta if mid == "m1" else None,
    )
    runtime = AgentRuntime(store, SimpleNamespace(), fake_tools, fake_catalog)
    runtime.update_settings({"enabled": True, "model_id": "m1"})
    created_task = runtime.create({"message": "Write report"})
    tid = created_task["id"]

    # Seed an in-flight dispatched write on that task before resetting the ledger.
    def _seed_inflight(data):
        row = data["tasks"][tid]
        row["status"] = "running"
        row["in_flight"] = {
            "kind": "tool",
            "name": "ryn_create_text_file",
            "call_id": "call_1",
            "operation_id": "op_good",
            "effect": "write",
            "dispatched": True,
            "goal_revision": row["goal"]["revision"],
            "scope_revision": row["scope_revision"],
            "control_revision": row["control_revision"],
        }
        return True

    store.transaction(_seed_inflight)
    assert runtime.settings()["operation_ledger"]["state"] == "unreadable"

    reset_res = runtime.update_settings({"reset_operation_ledger": True})["operation_ledger_reset"]
    assert reset_res["ok"] is True
    assert tid in reset_res["affected_tasks"]
    assert runtime.settings()["operation_ledger"]["state"] == "ok"

    updated_task = runtime.get(tid)
    assert updated_task["status"] == "interrupted"
    assert updated_task["error"] == "agent_write_outcome_unknown"
    assert updated_task["reconciliation"]["state"] == "unknown"

    # Fresh empty ledger accepts new operations under the new key.
    row_after_reset, ok_after = rekeyed_ledger.begin({"operation_id": "op_after_reset", "task_id": "t2"}, {})
    assert ok_after is True and row_after_reset["status"] == "intent"


def test_t4_legacy_single_file_migrates_and_preserves_migrated_backup(tmp_path):
    """T4: 旧单文件 business-operations.json 自动拆分迁移到 business-operations/<hash[:24]>.json 并改名为 .migrated。"""
    legacy_records = {
        "op_alpha": {
            "binding": {"operation_id": "op_alpha", "task_id": "t_a"},
            "preview": {"title": "Alpha"},
            "status": "confirmed",
            "result": {"id": "123"},
        },
        "op_beta": {
            "binding": {"operation_id": "op_beta", "task_id": "t_b"},
            "status": "retired",
            "prior_status": "not_executed",
            "result_sha256": "f" * 64,
        },
    }
    legacy_file = _write_legacy_single_file(tmp_path, legacy_records)
    original_cipher_bytes = legacy_file.read_bytes()

    ledger = OperationLedger(tmp_path)
    migrated_backup = tmp_path / "agent" / "business-operations.json.migrated"
    ops_dir = tmp_path / "agent" / "business-operations"

    assert not legacy_file.exists()
    assert migrated_backup.is_file()
    assert migrated_backup.read_bytes() == original_cipher_bytes
    assert (ops_dir / f"{hashlib.sha256(b'op_alpha').hexdigest()[:24]}.json").is_file()
    assert (ops_dir / f"{hashlib.sha256(b'op_beta').hexdigest()[:24]}.json").is_file()

    alpha = ledger.get("op_alpha")
    beta = ledger.get("op_beta")
    assert alpha["status"] == "confirmed" and alpha["result"] == {"id": "123"}
    assert beta["status"] == "retired"


def test_t5_update_rewrites_only_one_record_and_2500_retired_sharing_ops_do_not_block(tmp_path, monkeypatch):
    """T5: update 只重写一条记录（统计写入次数 == 1）；2500 条 retired 分享回执不阻止新分享。"""
    ledger = OperationLedger(tmp_path)
    for idx in range(25):
        ledger.begin({"operation_id": f"op_{idx}", "task_id": "task_u"}, {"idx": idx})

    written_paths: list[Path] = []
    real_atomic_write_json = operation_ledger.atomic_write_json

    def counting_write_json(path, data, **kwargs):
        written_paths.append(Path(path))
        return real_atomic_write_json(path, data, **kwargs)

    monkeypatch.setattr(operation_ledger, "atomic_write_json", counting_write_json)
    ledger.update("op_12", "confirmed", result={"done": True})

    assert len(written_paths) == 1
    assert written_paths[0].name == f"{hashlib.sha256(b'op_12').hexdigest()[:24]}.json"
    assert ledger.get("op_12")["status"] == "confirmed"
    assert "receipt_reserve" not in ledger.get("op_12")

    # Part 2 (F2): 2500 retired _sharing_operations in sources.enc do not block a new share.
    retired_sharing = {
        f"share_ret_{i}": {
            "operation_id": f"share_ret_{i}",
            "tool": "ryn_share_model_with_friend",
            "arguments_sha256": "0" * 64,
            "binding": {
                "task_id": "old_share_task",
                "owner_id": "owner_1",
                "consumer_node_id": "node_1",
                "goal_revision": 1,
                "scope_revision": 1,
            },
            "retired": True,
        }
        for i in range(2500)
    }
    tool, sources, rel_id = _make_friend_sharing_tool(tmp_path / "sharing_case", retired_sharing)
    ctx = {
        "task_id": "live_share_task",
        "owner_id": "owner_1",
        "consumer_node_id": "node_1",
        "goal_revision": 1,
        "scope_revision": 1,
    }
    sid = SourceStore.model_service_id(sources.read()["src1"], "gpt-test")
    args = {"model_id": f"local/{sid}", "relationship_id": rel_id}
    preview = tool.preview(args, ctx)
    res = tool.execute(
        args,
        operation_id="share_live_2501",
        context=ctx,
        expected_preview=preview,
        cancelled=lambda: False,
    )
    assert res["ok"] is True
    assert "share_live_2501" in sources.read()["src1"]["_sharing_operations"]


def test_t6_retired_sharing_receipts_pruned_after_30_days_via_unified_prune(tmp_path):
    """T6: FriendSharingTool.forget_task 写入 retired_at，30 天后由同一次 prune_retired 清理。"""
    tool, sources, rel_id = _make_friend_sharing_tool(tmp_path)
    ctx_old = {
        "task_id": "task_share_old",
        "owner_id": "owner_1",
        "consumer_node_id": "node_1",
        "goal_revision": 1,
        "scope_revision": 1,
    }
    ctx_recent = {**ctx_old, "task_id": "task_share_recent"}
    sid = SourceStore.model_service_id(sources.read()["src1"], "gpt-test")
    args = {"model_id": f"local/{sid}", "relationship_id": rel_id}

    p1 = tool.preview(args, ctx_old)
    tool.execute(args, operation_id="share_old", context=ctx_old, expected_preview=p1, cancelled=lambda: False)
    p2 = tool.preview(args, ctx_recent)
    tool.execute(args, operation_id="share_recent", context=ctx_recent, expected_preview=p2, cancelled=lambda: False)

    tool.forget_task("task_share_old")
    tool.forget_task("task_share_recent")

    ops_after_forget = sources.read()["src1"]["_sharing_operations"]
    assert ops_after_forget["share_old"]["retired"] is True
    assert ops_after_forget["share_old"].get("retired_at")
    assert "result" not in ops_after_forget["share_old"]
    assert ops_after_forget["share_recent"]["retired"] is True

    # Backdate share_old past 30 days.
    records = sources.read()
    records["src1"]["_sharing_operations"]["share_old"]["retired_at"] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).isoformat()
    sources._write(records)

    # Trigger unified prune through AgentRuntime (same hook called on idle storage relief).
    ledger = OperationLedger(tmp_path)
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    store = AgentStore(tmp_path / "agent", key)
    fake_tools = SimpleNamespace(
        resource_writes=SimpleNamespace(ledger=ledger),
        friend_sharing=tool,
        tool_catalog=lambda: [],
    )
    fake_catalog = SimpleNamespace(list_models=lambda: [], display_models=lambda: [], display_model=lambda mid: None)
    runtime = AgentRuntime(store, SimpleNamespace(), fake_tools, fake_catalog)

    pruned = runtime.prune_retired_operations(older_than_days=30)
    assert pruned["sharing_pruned"] == 1

    remaining_ops = sources.read()["src1"]["_sharing_operations"]
    assert "share_old" not in remaining_ops
    assert "share_recent" in remaining_ops
