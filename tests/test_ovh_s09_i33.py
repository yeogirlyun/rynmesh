"""Tests for S09-I33: memory ledger capacity governance, terminal job compaction, fence folding, and shrink-safe delete/reset."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import (
    lifecycle,
    memory,
    memory_integration,
    memory_natural,
    memory_worker,
    state,
)
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded


def _dest() -> dict[str, str]:
    return {
        "catalog_model_id": "local/gpt-4o",
        "provider_peer_id": "peer-local",
        "source_id": "src-local",
        "destination_revision": "rev-1",
    }


def _make_task(
    task_id: str,
    messages: list[dict],
    *,
    conv_rev: int = 1,
    dest: dict[str, str] | None = None,
) -> dict:
    d = dest or _dest()
    row = {
        "id": task_id,
        "title": f"Task {task_id}",
        "model_id": d["catalog_model_id"],
        "model_destination": copy.deepcopy(d),
        "status": "completed",
        "revision": 1,
        "conversation_revision": conv_rev,
        "context_epoch": 0,
        "messages": messages,
        "events": [],
        "receipts": [],
        "in_flight": False,
        "permission_mode": "automatic",
    }
    state.initialize(row)
    row["control_revision"] = 1
    row["in_flight"] = False
    return row


def test_t1_terminal_jobs_strip_manifest_text_and_drop_settled_dispatch_usage():
    data: dict = {"tasks": {}, "settings": {"enabled": True}}
    dest = _dest()
    memory.update_memory_settings(
        data,
        {
            "capture_enabled": True,
            "use_enabled": True,
            "activation_policy": "owner_review",
            "extraction_destination": dest,
        },
    )
    mem = memory.ensure_memory_state(data)

    # Seed 64 jobs each carrying 8 messages of 1000 chars (~512 KB raw text if uncompacted)
    long_chunk = "中" * 1000
    expected_keys = {"message_id", "conversation_revision", "content_sha256", "source_seq"}
    for j_idx in range(64):
        tid = f"task-job-{j_idx}"
        msgs = []
        manifest = []
        for m_idx in range(8):
            mid = f"{tid}:m:{m_idx}"
            txt = f"{long_chunk[:-4]}{j_idx:02d}{m_idx:02d}"
            msgs.append({"role": "user", "message_id": mid, "content": txt, "owner_source_seq": m_idx + 1})
            src = memory.build_source_ref(
                source_type="task_message",
                task_id=tid,
                message_id=mid,
                conversation_revision=1,
                context_epoch=0,
                content=txt,
            )
            src["source_seq"] = m_idx + 1
            manifest.append({**src, "text": txt})
        data["tasks"][tid] = _make_task(tid, msgs, conv_rev=1, dest=dest)
        jid = f"mjob_term_{j_idx:03d}"
        t_state = ("committed", "no_op", "stale", "failed")[j_idx % 4]
        mem["jobs"][jid] = {
            "id": jid,
            "idempotency_key": f"idem-{j_idx}",
            "client_request_id": None,
            "state": t_state,
            "task_id": tid,
            "project_id": None,
            "project_generation": None,
            "conversation_revision": 1,
            "context_epoch": 0,
            "source_manifest": manifest,
            "expected_generation": 1,
            "reset_epoch": 0,
            "activation_policy": "owner_review",
            "destination_binding": copy.deepcopy(dest),
            "lease_owner": None,
            "lease_token": None,
            "lease_until": 0.0,
            "attempts": 3 if t_state == "failed" else 1,
            "retry_at": None,
            "last_error": "extraction_timeout" if t_state == "failed" else None,
            "output_hash": "h",
            "error_code": "retry_exhausted" if t_state == "failed" else None,
            "dispatch_usage": {
                "lease_old": {"1": {"day": "2026-11-15", "reserved_tokens": 500, "dispatched": True, "settled": True}}
            },
            "created_at": float(100 + j_idx),
            "updated_at": float(100 + j_idx),
        }

    bytes_before = len(encoded(mem))
    assert bytes_before > 500_000

    # _enforce_memory_bounds compacts all 64 pre-existing terminal jobs so state drops well below 256 KiB
    memory._enforce_memory_bounds(mem)
    bytes_after = len(encoded(mem))
    assert bytes_after < bytes_before // 4
    assert bytes_after < memory.MAX_MEMORY_BYTES

    for j_idx in range(64):
        job = mem["jobs"][f"mjob_term_{j_idx:03d}"]
        assert "dispatch_usage" not in job
        assert len(job["source_manifest"]) == 8
        for item in job["source_manifest"]:
            assert "text" not in item
            assert set(item.keys()) <= expected_keys
            assert expected_keys <= set(item.keys())

    # Also verify live enqueue keeps `text` while queued/leased, and commit_extraction_job /
    # commit_natural_consolidation_job / _record_dispatch_failure strip `text` upon entering terminal state
    live_tid = "task-live-commit"
    live_txt = "我平时用 Pytest 跑回归测试" + ("甲" * 200)
    data["tasks"][live_tid] = _make_task(
        live_tid,
        [{"role": "user", "message_id": "m-live-1", "content": live_txt}],
        conv_rev=1,
        dest=dest,
    )
    enq = memory.enqueue_extraction_job(data, task_id=live_tid)
    live_jid = enq["job"]["id"]
    assert "text" in data["memory"]["jobs"][live_jid]["source_manifest"][0]

    claimed = memory.claim_extraction_job(data, worker_id="w-test")
    assert claimed is not None and claimed["id"] == live_jid
    assert "text" in data["memory"]["jobs"][live_jid]["source_manifest"][0]

    committed = memory.commit_extraction_job(
        data,
        job_id=live_jid,
        worker_id="w-test",
        proposals=[],
        lease_token=claimed["lease_token"],
        tokens_used=42,
    )
    assert committed["state"] == "no_op"
    assert "text" not in data["memory"]["jobs"][live_jid]["source_manifest"][0]

    # Verify natural consolidation commit & worker _record_dispatch_failure also compact terminal jobs
    nat_tid = "task-live-natural"
    data["tasks"][nat_tid] = _make_task(
        nat_tid,
        [{"role": "user", "message_id": "m-nat-1", "content": "我的主力编辑器是 Neovim"}],
        conv_rev=1,
        dest=dest,
    )
    memory.update_memory_settings(data, {"activation_policy": "automatic"})
    nat_enq = memory.enqueue_extraction_job(data, task_id=nat_tid)
    nat_jid = nat_enq["job"]["id"]
    nat_claimed = memory.claim_extraction_job(data, worker_id="w-nat")
    assert nat_claimed is not None
    nat_res = memory_natural.commit_natural_consolidation_job(
        data,
        job_id=nat_jid,
        worker_id="w-nat",
        lease_token=nat_claimed["lease_token"],
        phase1_mode="observations",
        phase1_items=[],
        phase2_operations=[],
    )
    assert nat_res["state"] == "no_op"
    assert "text" not in data["memory"]["jobs"][nat_jid]["source_manifest"][0]

    # Verify memory_worker._record_dispatch_failure compacts when retry_exhausted / stale
    fail_tid = "task-live-fail"
    data["tasks"][fail_tid] = _make_task(
        fail_tid,
        [{"role": "user", "message_id": "m-fail-1", "content": "失败重试耗尽测试文本" + ("乙" * 200)}],
        conv_rev=1,
        dest=dest,
    )
    fail_enq = memory.enqueue_extraction_job(data, task_id=fail_tid)
    fail_jid = fail_enq["job"]["id"]
    data["memory"]["jobs"][fail_jid]["attempts"] = memory.MAX_EXTRACTION_ATTEMPTS - 1
    fail_claimed = memory.claim_extraction_job(data, worker_id="w-fail")
    assert fail_claimed is not None

    class _MemBox:
        def transaction(self, fn):
            return fn(data)

    mw = memory_worker.MemoryExtractionWorker(SimpleNamespace(store=_MemBox()), worker_id="w-fail")
    mw._record_dispatch_failure(
        fail_claimed,
        error_code="extraction_timeout",
        actual_tokens=0,
        reserved_tokens=100,
        reserved_day="2026-11-16",
        state="failed",
    )
    assert data["memory"]["jobs"][fail_jid]["state"] == "failed"
    assert data["memory"]["jobs"][fail_jid]["error_code"] == "retry_exhausted"
    assert "text" not in data["memory"]["jobs"][fail_jid]["source_manifest"][0]

    snap = memory.get_memory_settings_snapshot(data)
    assert isinstance(snap.get("memory_bytes"), int) and snap["memory_bytes"] > 0
    assert snap.get("memory_bytes_limit") == memory.MAX_MEMORY_BYTES


def test_t2_three_thousand_deleted_tasks_fold_into_watermarks_without_blocking_deletes(tmp_path: Path):
    store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
    dest = _dest()

    def emit(row, kind, **extra):
        seq = int(row.get("event_cursor", 0)) + 1
        row["event_cursor"] = seq
        row.setdefault("events", []).append({"seq": seq, "kind": kind, **extra})

    def seed(data):
        memory.update_memory_settings(
            data,
            {
                "capture_enabled": True,
                "use_enabled": True,
                "activation_policy": "owner_review",
                "extraction_destination": dest,
            },
        )
        mem = memory.ensure_memory_state(data)
        # Seed 3000 already-deleted tasks (simulating long-lived installation)
        for i in range(3000):
            dtid = f"agent_{i:032x}"
            data.setdefault("deleted_tasks", {})[dtid] = {"id": dtid, "deleted_at": "2026-11-15T00:00:00Z"}
            mem["source_fences"]["deleted_tasks"][dtid] = {"at": float(1000 + i), "reset_epoch": 0}

        # Seed one live task to delete next, and one active memory entry to delete next
        live_row = _make_task(
            "task-3001",
            [{"role": "user", "message_id": "msg-3001", "content": "准备删除的第 3001 个任务"}],
            conv_rev=2,
            dest=dest,
        )
        data["tasks"]["task-3001"] = live_row

        up = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "preference",
                "fact_key": "personal.pref.theme",
                "text": "偏好深色界面主题",
                "exposure_policy": {"mode": "local_only", "allowed_destinations": []},
            },
            origin="manual",
        )
        return up["entry"]["id"]

    entry_id = store.transaction(seed)

    state_after_seed = store.read()
    fences = state_after_seed["memory"]["source_fences"]
    assert len(fences["deleted_tasks"]) <= memory.MAX_TASK_FENCES
    assert f"agent_{0:032x}" in fences["fenced_task_watermarks"]

    # Deleting the 3001st task succeeds cleanly
    del_task_res = lifecycle.change(
        store,
        "task-3001",
        {"expected_control_revision": 1, "client_request_id": "00000000-0000-4000-8000-000000003001"},
        "delete",
        emit,
        lambda: "2026-11-16T01:00:00Z",
        on_mutate=memory_integration.on_task_lifecycle_in_tx,
    )
    assert del_task_res["deleted"] is True

    # Deleting a memory entry also succeeds cleanly
    del_entry_res = store.transaction(lambda d: memory.delete_memory_entry(d, entry_id))
    assert del_entry_res["deleted"] is True

    # Old sources (both oldest folded task and newer deleted task) still fail source verification
    final_data = store.read()
    for check_tid in (f"agent_{0:032x}", f"agent_{2999:032x}", "task-3001"):
        old_src = memory.build_source_ref(
            source_type="task_message",
            task_id=check_tid,
            message_id="m-old",
            conversation_revision=2,
            context_epoch=0,
            content="旧消息原文",
        )
        ok, reason = memory.verify_source_ref_against_state(final_data, old_src, for_background_job=False)
        assert ok is False
        assert reason in {"source_task_deleted", "source_task_watermark_fenced", "source_task_missing"}


def test_t3_reset_all_across_50_tasks_3000_messages_blocks_old_and_allows_new():
    data: dict = {"tasks": {}, "settings": {"enabled": True}}
    dest = _dest()
    memory.update_memory_settings(
        data,
        {
            "capture_enabled": True,
            "use_enabled": True,
            "activation_policy": "owner_review",
            "extraction_destination": dest,
        },
    )

    pre_reset_sources: list[dict] = []
    for t_idx in range(50):
        tid = f"task-bulk-{t_idx:02d}"
        msgs = []
        for m_idx in range(60):
            mid = f"{tid}:msg:{m_idx:02d}"
            role = "user" if m_idx % 2 == 0 else "assistant"
            content = f"历史对话消息 {t_idx}-{m_idx}：旧偏好记录测试"
            msgs.append({"role": role, "message_id": mid, "content": content})
        data["tasks"][tid] = _make_task(tid, msgs, conv_rev=1, dest=dest)
        pre_reset_sources.append(
            memory.build_source_ref(
                source_type="task_message",
                task_id=tid,
                message_id=f"{tid}:msg:58",
                conversation_revision=1,
                context_epoch=0,
                content=f"历史对话消息 {t_idx}-58：旧偏好记录测试",
            )
        )

    memory.upsert_active_entry(
        data,
        {
            "scope": {"kind": "personal"},
            "category": "preference",
            "fact_key": "personal.pref.old",
            "text": "清空前的旧记忆",
            "sources": [pre_reset_sources[0]],
            "exposure_policy": {"mode": "local_only", "allowed_destinations": []},
        },
        origin="explicit_owner_chat",
    )

    reset_res = memory.reset_memory(data)
    assert reset_res["reset"] is True
    assert reset_res["cleared_entries"] == 1

    mem = data["memory"]
    assert len(encoded(mem)) < memory.MAX_MEMORY_BYTES
    boundaries = mem["source_fences"]["reset_task_boundaries"]
    assert len(boundaries) == 50
    for b_info in boundaries.values():
        assert "fenced_message_ids" not in b_info
        assert b_info["message_count"] == 60

    # All pre-reset sources across the 50 tasks are blocked
    for old_src in (pre_reset_sources[0], pre_reset_sources[25], pre_reset_sources[49]):
        ok, reason = memory.verify_source_ref_against_state(data, old_src, for_background_job=True)
        assert ok is False
        assert reason == "source_precedes_reset_watermark"

    # A new owner message appended after reset on task-bulk-00 IS valid and learnable
    row0 = data["tasks"]["task-bulk-00"]
    new_msg = {
        "role": "user",
        "message_id": "task-bulk-00:msg:60",
        "content": "清空之后的新消息：我现在改用 Rust 开发工具链",
    }
    row0["messages"].append(new_msg)
    state.owner_update(row0, new_msg)

    new_src = memory.build_source_ref(
        source_type="task_message",
        task_id="task-bulk-00",
        message_id="task-bulk-00:msg:60",
        conversation_revision=1,
        context_epoch=0,
        content=new_msg["content"],
    )
    ok_new, reason_new = memory.verify_source_ref_against_state(data, new_src, for_background_job=True)
    assert ok_new is True
    assert reason_new is None

    enq_after = memory.enqueue_extraction_job(data, task_id="task-bulk-00")
    assert enq_after["queued"] is True
    manifest_ids = [m["message_id"] for m in enq_after["job"]["source_manifest"]]
    assert manifest_ids == ["task-bulk-00:msg:60"]


def test_t4_over_byte_limit_allows_delete_and_reset_but_rejects_new_entry():
    data: dict = {"tasks": {}, "settings": {"enabled": True}}
    dest = _dest()
    memory.update_memory_settings(
        data,
        {
            "capture_enabled": True,
            "use_enabled": True,
            "activation_policy": "owner_review",
            "extraction_destination": dest,
            "budgets": {"max_memory_bytes": 16384},
        },
    )

    # Seed two real entries first while within budget
    e1 = memory.upsert_active_entry(
        data,
        {
            "scope": {"kind": "personal"},
            "category": "preference",
            "fact_key": "personal.pref.one",
            "text": "第一条待删除记忆",
            "exposure_policy": {"mode": "local_only", "allowed_destinations": []},
        },
        origin="manual",
    )["entry"]["id"]

    # Stuff active entries directly so even after _shrink_to_fit, serialized mem exceeds 16384 bytes
    mem = data["memory"]
    for idx in range(25):
        eid = f"mem_stuffed_{idx:03d}"
        mem["entries"][eid] = {
            "id": eid,
            "scope": {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None},
            "category": "general",
            "fact_key": f"personal.stuffed.fact_{idx}",
            "text": ("超限填充有效记忆文本_" * 75) + str(idx),
            "kind": "owner_fact",
            "status": "active",
            "revision": 1,
            "origin": "manual",
            "sources": [],
            "supersedes": [],
            "superseded_by": None,
            "exposure_policy": {"mode": "local_only", "allowed_destinations": [], "revision": 1},
            "created_at": 1.0,
            "updated_at": 1.0,
            "last_used_at": None,
            "reviewed_by_owner_at": 1.0,
        }
    assert len(encoded(mem)) > 16384

    # Growth operation (upsert new entry) MUST raise agent_memory_byte_limit
    with pytest.raises(AgentError, match="agent_memory_byte_limit"):
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "preference",
                "fact_key": "personal.pref.blocked_growth",
                "text": "超限时新增记忆应被拒绝",
                "exposure_policy": {"mode": "local_only", "allowed_destinations": []},
            },
            origin="manual",
        )

    # Shrink operation 1: delete_memory_entry succeeds despite remaining over 16384 bytes, marking ledger_degraded
    del_res = memory.delete_memory_entry(data, e1, client_request_id="req-del-over-limit")
    assert del_res["deleted"] is True
    assert e1 not in data["memory"]["entries"]
    assert isinstance(data["memory"].get("ledger_degraded"), dict)

    # Shrink operation 2: reset_memory succeeds and reclaims space below max_memory_bytes
    rst_res = memory.reset_memory(data, client_request_id="req-rst-over-limit")
    assert rst_res["reset"] is True
    assert data["memory"]["entries"] == {}
    snap = memory.get_memory_settings_snapshot(data)
    assert snap["memory_bytes"] < snap["memory_bytes_limit"] == 16384
