"""Tests for S09-I15: extraction backoff retry, daily budget local-day deferral, watermarks, per-item validation, capture_status, and idle non-completed task capture."""
from __future__ import annotations

import calendar
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import (
    memory,
    memory_natural,
    memory_worker,
)
from rynmesh.agent_runtime.store import AgentError, AgentStore

DEST_A = {
    "catalog_model_id": "local/svc-a/model/gemini-4-argon-medium",
    "provider_peer_id": "peer-local-owner",
    "source_id": "src-local-a",
    "destination_revision": "rev-a-1",
}


class FakeModelAdapter:
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancelled: Any,
        on_event: Any,
        **options: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "model_id": model_id,
                "messages": messages,
                "tools": tools,
                "options": dict(options),
            }
        )
        if not self.responses:
            return {
                "message": {"role": "assistant", "content": json.dumps({"observations": []})},
                "usage": {"total_tokens": 32},
            }
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if "message" not in resp and "content" in resp:
            return {
                "message": {"role": "assistant", "content": resp["content"]},
                "usage": resp.get("usage", {"total_tokens": 48}),
            }
        return resp


class FakeRuntime:
    def __init__(self, tmp_path: Path, responses: list[Any] | None = None) -> None:
        self.store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
        self.store.transaction(lambda data: data["settings"].update(enabled=True))
        self.model = FakeModelAdapter(responses or [])
        self._models = {
            DEST_A["catalog_model_id"]: {
                "id": DEST_A["catalog_model_id"],
                "available": True,
                "agent_supported": True,
                "peer_id": DEST_A["provider_peer_id"],
                "source_id": DEST_A["source_id"],
                "destination_revision": DEST_A["destination_revision"],
            },
        }

    def _model_metadata(self, model_id: str) -> dict[str, Any] | None:
        return self._models.get(model_id)

    def _bound_destination(self, model: dict[str, Any]) -> dict[str, str]:
        return {
            "catalog_model_id": str(model["id"]),
            "provider_peer_id": str(model["peer_id"]),
            "source_id": str(model["source_id"]),
            "destination_revision": str(model["destination_revision"]),
        }

    def _current_destination(self, model_id: str, *, require_bound: bool = False) -> dict[str, str]:
        del require_bound
        model = self._model_metadata(model_id)
        if not model:
            raise AgentError("agent_model_unavailable")
        return self._bound_destination(model)


def _enable_memory(
    runtime: FakeRuntime,
    *,
    activation_policy: str = "automatic",
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def tx(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        patch: dict[str, Any] = {
            "capture_enabled": True,
            "use_enabled": True,
            "activation_policy": activation_policy,
            "extraction_destination": dict(DEST_A),
            "default_exposure": {
                "mode": "selected_models",
                "allowed_destinations": [dict(DEST_A)],
            },
        }
        if budgets is not None:
            patch["budgets"] = budgets
        return memory.update_memory_settings(data, patch, expected_generation=mem["generation"])

    return runtime.store.transaction(tx)


def _seed_task(
    runtime: FakeRuntime,
    task_id: str,
    user_messages: list[tuple[str, str]],
    *,
    status: str = "completed",
    updated_at: float | str | None = None,
    in_flight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def tx(data: dict[str, Any]) -> dict[str, Any]:
        memory.ensure_memory_state(data)
        msgs = []
        for mid, text in user_messages:
            msg = {
                "message_id": mid,
                "role": "user",
                "author_role": "owner",
                "content": text,
                "context_epoch": 0,
            }
            memory_natural.stamp_single_owner_message_in_tx(data, msg)
            msgs.append(msg)
        row: dict[str, Any] = {
            "id": task_id,
            "status": status,
            "archived": False,
            "revision": 1,
            "conversation_revision": 1,
            "context_epoch": 0,
            "model_id": DEST_A["catalog_model_id"],
            "model_destination": dict(DEST_A),
            "messages": msgs,
            "events": [],
            "in_flight": in_flight,
            "pending_control": None,
            "pending_user_messages": [],
            "pending_approval": None,
            "pending_input": None,
            "queue": [],
        }
        if updated_at is not None:
            row["updated_at"] = updated_at
        data.setdefault("tasks", {})[task_id] = row
        return row

    return runtime.store.transaction(tx)


def test_t1_timeout_backoff_retries_and_exhaustion_after_three_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1 (covers 1, 2, 4): Model timeout returns job to queued with increasing retry_at, fails on 3rd attempt with retry_exhausted, is not re-enqueued automatically, and force_relearn resets attempts to 0."""
    clock = [1_700_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    runtime = FakeRuntime(
        tmp_path,
        responses=[
            TimeoutError("agent_inference_timeout"),
            RuntimeError("agent_model_upstream_failed"),
            TimeoutError("agent_inference_timeout"),
        ],
    )
    _enable_memory(runtime, activation_policy="owner_review")
    _seed_task(runtime, "task_retry", [("m1", "我平时习惯用 Neovim 写代码。")])

    worker = memory_worker.MemoryExtractionWorker(runtime)

    # Attempt 1 -> back to queued with retry_at = now + 60 * 2^1 = now + 120
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 1
    job1 = next(iter(runtime.store.read()["memory"]["jobs"].values()))
    assert job1["state"] == "queued"
    assert job1["attempts"] == 1
    assert job1["last_error"] == "extraction_timeout"
    assert job1["lease_owner"] is None
    assert job1["lease_token"] is None
    retry_at_1 = float(job1["retry_at"])
    assert retry_at_1 == pytest.approx(clock[0] + 120.0, abs=1.0)

    # Before retry_at_1: both claim_extraction_job and collector skip it
    clock[0] += 60.0
    assert worker.run_once().activity is False
    assert len(runtime.model.calls) == 1
    assert runtime.store.transaction(lambda d: memory.claim_extraction_job(d, worker_id="manual")) is None

    # Attempt 2 (after retry_at_1) -> back to queued with retry_at = now + 60 * 2^2 = now + 240 (> retry_at_1 delay)
    clock[0] = retry_at_1 + 1.0
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 2
    job2 = next(iter(runtime.store.read()["memory"]["jobs"].values()))
    assert job2["state"] == "queued"
    assert job2["attempts"] == 2
    assert job2["last_error"] == "agent_model_upstream_failed"
    retry_at_2 = float(job2["retry_at"])
    assert (retry_at_2 - clock[0]) > (retry_at_1 - 1_700_000_000.0)
    assert retry_at_2 == pytest.approx(clock[0] + 240.0, abs=1.0)

    # Attempt 3 (after retry_at_2) -> fails permanently with error_code="retry_exhausted"
    clock[0] = retry_at_2 + 1.0
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 3
    jobs_after_3 = list(runtime.store.read()["memory"]["jobs"].values())
    assert len(jobs_after_3) == 1
    job3 = jobs_after_3[0]
    assert job3["state"] == "failed"
    assert job3["attempts"] == 3
    assert job3["error_code"] == "retry_exhausted"
    assert job3["last_error"] == "extraction_timeout"

    # Collector tick does NOT re-enqueue failed/retry_exhausted job
    clock[0] += 600.0
    assert worker.run_once().activity is False
    assert len(runtime.store.read()["memory"]["jobs"]) == 1

    # Direct claim_extraction_job also marks queued job with attempts >= 3 as failed/retry_exhausted
    def seed_exhausted_queued(data: dict[str, Any]) -> dict[str, Any] | None:
        mem = memory.ensure_memory_state(data)
        mem["jobs"]["mjob_direct3"] = {
            **job3,
            "id": "mjob_direct3",
            "idempotency_key": "custom_idem_3",
            "state": "queued",
            "attempts": 3,
            "retry_at": None,
            "error_code": None,
        }
        return memory.claim_extraction_job(data, worker_id="w_check", job_id="mjob_direct3")

    assert runtime.store.transaction(seed_exhausted_queued) is None
    direct_job = runtime.store.read()["memory"]["jobs"]["mjob_direct3"]
    assert direct_job["state"] == "failed"
    assert direct_job["error_code"] == "retry_exhausted"

    # Non-retryable error (e.g. destination drift / invalid request) does NOT return to queued
    _seed_task(runtime, "task_nonretry", [("m_nr", "我的常用终端是 Alacritty。")])
    runtime.model.responses.append(AgentError("agent_model_invalid_request"))
    assert worker.run_once().activity is True
    nr_jobs = [j for j in runtime.store.read()["memory"]["jobs"].values() if j.get("task_id") == "task_nonretry"]
    assert len(nr_jobs) == 1
    assert nr_jobs[0]["state"] == "failed"
    assert nr_jobs[0]["attempts"] == 1

    # Explicit owner force_relearn creates a fresh queued job with attempts == 0
    relearn_res = runtime.store.transaction(
        lambda d: memory.enqueue_extraction_job(d, task_id="task_retry", force_relearn=True)
    )
    assert relearn_res["queued"] is True
    assert relearn_res["deduplicated"] is False
    assert relearn_res["job"]["attempts"] == 0
    assert relearn_res["job"]["state"] == "queued"


def test_t2_daily_budget_exhausted_waits_for_local_midnight_and_clock_regression_or_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2 (covers 1, 3, 4 + G17-22/G18-15 + migration): Budget exhaustion keeps job queued until next local 00:00 (UTC+8 resets after UTC 16:00), does not increment attempts, clock regression resets usage without failing, and legacy budget-exhausted failed jobs migrate back to queued once."""
    # Simulate UTC+8 local timezone deterministically: 2026-05-01 15:30:00 UTC == 2026-05-01 23:30:00 UTC+8
    utc_2330_local = float(calendar.timegm((2026, 5, 1, 15, 30, 0, 0, 0, 0)))
    utc_next_local_midnight = float(calendar.timegm((2026, 5, 1, 16, 0, 0, 0, 0, 0)))
    clock = [utc_2330_local]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(
        time,
        "localtime",
        lambda secs=None: time.gmtime((clock[0] if secs is None else float(secs)) + 8 * 3600),
    )

    runtime = FakeRuntime(
        tmp_path,
        responses=[
            {"content": json.dumps({"candidates": []})},
            {"content": json.dumps({"candidates": []})},
        ],
    )
    _enable_memory(
        runtime,
        activation_policy="owner_review",
        budgets={"max_daily_extraction_calls": 1, "max_daily_extraction_tokens": 65536},
    )
    # Exhaust today's 1-call quota at 23:30 local (2026-05-01)
    _seed_task(runtime, "task_day1_ok", [("m_ok", "我喜欢喝手冲咖啡。")])
    worker = memory_worker.MemoryExtractionWorker(runtime)
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 1

    # Enqueue second task on the same local day -> budget exhausted
    _seed_task(runtime, "task_budget_wait", [("m_wait", "我平时用机械键盘打字。")])
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 1  # No extra model call dispatched

    snap = runtime.store.read()
    wait_jobs = [j for j in snap["memory"]["jobs"].values() if j.get("task_id") == "task_budget_wait"]
    assert len(wait_jobs) == 1
    wjob = wait_jobs[0]
    assert wjob["state"] == "queued"
    assert wjob["attempts"] == 0  # Budget exhaustion must NOT count attempts
    assert wjob["last_error"] == "daily_extraction_budget_exhausted"
    assert float(wjob["retry_at"]) == pytest.approx(utc_next_local_midnight, abs=1.0)

    # Another tick 10 minutes later (23:40 local): no duplicate job created, still queued
    clock[0] += 600.0
    assert worker.run_once().activity is False
    wait_jobs_again = [
        j for j in runtime.store.read()["memory"]["jobs"].values() if j.get("task_id") == "task_budget_wait"
    ]
    assert len(wait_jobs_again) == 1
    assert wait_jobs_again[0]["state"] == "queued"

    # Cross UTC 16:00 -> 2026-05-02 00:05:00 local (still 2026-05-01 in UTC!): quota resets and job runs!
    clock[0] = utc_next_local_midnight + 300.0
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 2
    snap_next_day = runtime.store.read()
    assert snap_next_day["memory"]["daily_usage"]["day"] == "2026-05-02"
    done_job = [j for j in snap_next_day["memory"]["jobs"].values() if j.get("task_id") == "task_budget_wait"][0]
    assert done_job["state"] == "no_op"

    # Clock regression (usage.day > today) must NOT mark job failed; resets usage and proceeds
    def inject_future_day_and_legacy_failed(data: dict[str, Any]) -> str:
        mem = data["memory"]
        mem["daily_usage"] = {"day": "2099-12-31", "extraction_calls": 99, "extraction_tokens": 99999}
        legacy_id = "mjob_legacy_budget"
        mem["jobs"][legacy_id] = {
            "id": legacy_id,
            "idempotency_key": "legacy_idem_budget",
            "state": "failed",
            "task_id": "task_clock_ok",
            "project_id": None,
            "project_generation": None,
            "conversation_revision": 1,
            "context_epoch": 0,
            "source_manifest": [],
            "expected_generation": 1,
            "reset_epoch": mem["reset_epoch"],
            "activation_policy": "owner_review",
            "destination_binding": dict(DEST_A),
            "lease_owner": None,
            "lease_token": None,
            "lease_until": 0.0,
            "attempts": 1,
            "output_hash": None,
            "error_code": "daily_extraction_budget_exhausted",
            "created_at": clock[0],
            "updated_at": clock[0],
        }
        return legacy_id

    _seed_task(runtime, "task_clock_ok", [("m_clk", "我的主力显示器是 4K IPS。")])
    legacy_jid = runtime.store.transaction(inject_future_day_and_legacy_failed)
    # Trigger ensure_memory_state migration first and inspect migrated legacy job
    migrated_state = runtime.store.transaction(
        lambda d: dict(memory.ensure_memory_state(d)["jobs"][legacy_jid])
    )
    assert migrated_state["state"] == "queued"
    assert migrated_state["last_error"] == "daily_extraction_budget_exhausted"

    # Remove empty-manifest legacy job so task_clock_ok runs normally under regressed usage.day
    runtime.store.transaction(lambda d: d["memory"]["jobs"].pop(legacy_jid, None))
    assert worker.run_once().activity is True
    snap_reg = runtime.store.read()
    assert snap_reg["memory"]["daily_usage"]["day"] == "2026-05-02"
    clk_job = [j for j in snap_reg["memory"]["jobs"].values() if j.get("task_id") == "task_clock_ok"][0]
    assert clk_job["state"] == "no_op"
    assert clk_job.get("error_code") != "extraction_clock_regressed"


def test_t3_missing_budgets_uses_65536_and_processed_sources_watermark_survives_job_eviction(
    tmp_path: Path,
) -> None:
    """T3 (covers 5, 6): Missing budgets fields fall back to 65536 tokens in worker; after old terminal jobs are evicted beyond MAX_JOBS=64, processed_sources watermarks prevent re-enqueuing old tasks and evict deleted/archived tasks first."""
    runtime = FakeRuntime(tmp_path)
    _enable_memory(runtime, activation_policy="owner_review")
    _seed_task(runtime, "task_old_0", [("m_old_0", "我的主力编辑器是 VS Code。")])

    # Remove budget token keys and pre-set daily_usage to 30000 (> old 24000 default, < 65536 default)
    def strip_budgets_and_seed_usage(data: dict[str, Any]) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime(time.time()))
        data["memory"]["settings"]["budgets"].pop("max_daily_extraction_tokens", None)
        data["memory"]["settings"]["budgets"].pop("max_daily_extraction_calls", None)
        data["memory"]["daily_usage"] = {
            "day": today,
            "extraction_calls": 5,
            "extraction_tokens": 30000,
        }

    runtime.store.transaction(strip_budgets_and_seed_usage)

    # Even if budgets dict is emptied directly before worker budget check, worker uses 65536
    worker = memory_worker.MemoryExtractionWorker(runtime)
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 1

    snap0 = runtime.store.read()
    wm0 = snap0["memory"]["processed_sources"].get("task_old_0")
    assert isinstance(wm0, dict)
    assert wm0["through_seq"] >= 1
    assert wm0["conversation_revision"] == 1
    assert wm0["reset_epoch"] == snap0["memory"]["reset_epoch"]

    # Now create 70 newer completed tasks so task_old_0's terminal job is evicted past MAX_JOBS=64
    def seed_and_commit_70_jobs(data: dict[str, Any]) -> None:
        for i in range(1, 71):
            tid = f"task_bulk_{i}"
            msg = {
                "message_id": f"m_{i}",
                "role": "user",
                "author_role": "owner",
                "content": f"记录测试条目 {i}",
                "context_epoch": 0,
            }
            memory_natural.stamp_single_owner_message_in_tx(data, msg)
            data.setdefault("tasks", {})[tid] = {
                "id": tid,
                "status": "completed",
                "archived": False,
                "revision": 1,
                "conversation_revision": 1,
                "context_epoch": 0,
                "model_id": DEST_A["catalog_model_id"],
                "model_destination": dict(DEST_A),
                "messages": [msg],
                "events": [],
                "in_flight": None,
                "pending_control": None,
                "pending_user_messages": [],
                "pending_approval": None,
                "pending_input": None,
                "queue": [],
            }
            enq = memory.enqueue_extraction_job(data, task_id=tid)
            claimed = memory.claim_extraction_job(data, worker_id="w_bulk", job_id=enq["job"]["id"])
            assert claimed is not None
            memory.commit_extraction_job(
                data,
                job_id=claimed["id"],
                worker_id="w_bulk",
                lease_token=claimed["lease_token"],
                proposals=[],
                tokens_used=10,
            )

    runtime.store.transaction(seed_and_commit_70_jobs)

    snap_after_evict = runtime.store.read()
    assert len(snap_after_evict["memory"]["jobs"]) <= memory.MAX_JOBS
    assert all(j.get("task_id") != "task_old_0" for j in snap_after_evict["memory"]["jobs"].values())
    assert "task_old_0" in snap_after_evict["memory"]["processed_sources"]

    # Running collector again MUST NOT re-enqueue task_old_0!
    calls_before = len(runtime.model.calls)
    assert worker.run_once().activity is False
    assert len(runtime.model.calls) == calls_before
    enq_again = runtime.store.transaction(
        lambda d: memory.enqueue_extraction_job(d, task_id="task_old_0")
    )
    assert enq_again["queued"] is False

    # Verify watermark eviction prioritizes deleted/archived tasks before live tasks
    def fill_watermarks_beyond_bound(data: dict[str, Any]) -> None:
        mem = memory.ensure_memory_state(data)
        mem["source_fences"]["deleted_tasks"]["task_deleted_early"] = {"at": 1.0, "reset_epoch": 0}
        mem["source_fences"]["archived_tasks"]["task_archived_early"] = {"at": 2.0}
        mem["processed_sources"]["task_deleted_early"] = {"through_seq": 1, "conversation_revision": 1, "reset_epoch": 0}
        mem["processed_sources"]["task_archived_early"] = {"through_seq": 2, "conversation_revision": 1, "reset_epoch": 0}
        idx = 0
        while len(mem["processed_sources"]) < memory.MAX_FENCED_SOURCES + 2:
            live_tid = f"task_live_wm_{idx}"
            idx += 1
            if live_tid not in mem["processed_sources"]:
                mem["processed_sources"][live_tid] = {"through_seq": idx + 10, "conversation_revision": 1, "reset_epoch": 0}
        memory._enforce_memory_bounds(mem)

    runtime.store.transaction(fill_watermarks_beyond_bound)
    ps_after = runtime.store.read()["memory"]["processed_sources"]
    assert len(ps_after) <= memory.MAX_FENCED_SOURCES
    assert "task_deleted_early" not in ps_after
    assert "task_archived_early" not in ps_after
    assert "task_old_0" in ps_after


def test_t4_partial_invalid_observation_keeps_valid_siblings_and_records_rejected(
    tmp_path: Path,
) -> None:
    """T4 (covers 8): One invalid observation in Stage 1 is recorded in job['rejected_observations'] without preventing two valid observations in the same batch from consolidating into active memories."""
    stage1_mixed = {
        "observations": [
            {
                "observation_id": "obs_good_1",
                "kind": "owner_fact",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "equipment",
                "fact_key": "personal.gear.camera",
                "text": "常用相机是尼康 Z8",
                "evidence_span": "我平时拍照用尼康 Z8",
                "source_message_ids": ["m1"],
            },
            {
                "observation_id": "obs_bad_ungrounded",
                "kind": "owner_fact",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "equipment",
                "fact_key": "personal.gear.drone",
                "text": "有一台无人机",
                "evidence_span": "这段文字在原消息里根本不存在",
                "source_message_ids": ["m1"],
            },
            {
                "observation_id": "obs_good_2",
                "kind": "owner_preference",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "workflow",
                "fact_key": "personal.workflow.backup",
                "text": "修图前习惯双盘备份 RAW",
                "evidence_span": "每次修图前我都会双盘备份 RAW",
                "source_message_ids": ["m1"],
            },
        ]
    }
    stage2_ops = {
        "operations": [
            {
                "action": "add",
                "observation_ids": ["obs_good_1"],
                "target_entry_id": None,
                "expected_entry_revision": None,
                "scope": "personal",
                "kind": "owner_fact",
                "category": "equipment",
                "fact_key": "personal.gear.camera",
                "text": "常用相机是尼康 Z8",
                "evidence_span": "我平时拍照用尼康 Z8",
                "source_message_ids": ["m1"],
            },
            {
                "action": "add",
                "observation_ids": ["obs_good_2"],
                "target_entry_id": None,
                "expected_entry_revision": None,
                "scope": "personal",
                "kind": "owner_preference",
                "category": "workflow",
                "fact_key": "personal.workflow.backup",
                "text": "修图前习惯双盘备份 RAW",
                "evidence_span": "每次修图前我都会双盘备份 RAW",
                "source_message_ids": ["m1"],
            },
        ]
    }

    runtime = FakeRuntime(
        tmp_path,
        responses=[
            {"content": json.dumps(stage1_mixed, ensure_ascii=False)},
            {"content": json.dumps(stage2_ops, ensure_ascii=False)},
        ],
    )
    _enable_memory(runtime, activation_policy="automatic")
    _seed_task(
        runtime,
        "task_partial_obs",
        [("m1", "我平时拍照用尼康 Z8，而且每次修图前我都会双盘备份 RAW。")],
    )

    worker = memory_worker.MemoryExtractionWorker(runtime)
    assert worker.run_once().activity is True

    snap = runtime.store.read()
    entries = [e for e in snap["memory"]["entries"].values() if e.get("status") == "active"]
    assert len(entries) == 2
    assert {e["fact_key"] for e in entries} == {"personal.gear.camera", "personal.workflow.backup"}

    job = next(iter(snap["memory"]["jobs"].values()))
    assert job["state"] == "committed"
    rejected = job.get("rejected_observations")
    assert isinstance(rejected, list) and len(rejected) == 1
    assert "evidence_span_not_in_source" in json.dumps(rejected, ensure_ascii=False)

    # All-invalid batch still raises ValueError
    all_bad_raw = json.dumps({"observations": [stage1_mixed["observations"][1]]}, ensure_ascii=False)
    with pytest.raises(ValueError, match="invalid_observation"):
        memory_natural.parse_phase1_source_analysis_response(all_bad_raw, dict(job))


def test_t5_truncated_json_retries_with_doubled_output_limit_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5 (covers 7): Default output token limit starts at min(4096, max(1024, est_input // 2)) = 1024, reserves it in daily budget, and after truncated JSON (invalid_extraction_json) retries with 2048 once."""
    clock = [1_700_100_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    runtime = FakeRuntime(
        tmp_path,
        responses=[
            {"content": '{"candidates": [{"category": "profile", "fact_key": "trunc'},
            {"content": '{"candidates": [{"category": "profile", "fact_key": "trunc2'},
            {"content": json.dumps({"candidates": []})},
        ],
    )
    _enable_memory(runtime, activation_policy="owner_review")
    _seed_task(runtime, "task_trunc", [("m_t", "我习惯使用双拼输入法。")])

    worker = memory_worker.MemoryExtractionWorker(runtime)

    # 1st attempt: max_output_tokens == 1024, truncated -> queued
    assert worker.run_once().activity is True
    assert runtime.model.calls[0]["options"]["max_output_tokens"] == 1024
    j1 = next(iter(runtime.store.read()["memory"]["jobs"].values()))
    assert j1["state"] == "queued"
    assert j1["last_error"] == "invalid_extraction_json"
    assert int(j1["reserved_tokens"]) > 1024

    # 2nd attempt: max_output_tokens doubled to 2048 -> truncated again -> queued
    clock[0] = float(j1["retry_at"]) + 1.0
    assert worker.run_once().activity is True
    assert runtime.model.calls[1]["options"]["max_output_tokens"] == 2048
    j2 = next(iter(runtime.store.read()["memory"]["jobs"].values()))
    assert j2["state"] == "queued"

    # 3rd attempt: max_output_tokens stays 2048 (doubled ONCE, not 4096) and succeeds
    clock[0] = float(j2["retry_at"]) + 1.0
    assert worker.run_once().activity is True
    assert runtime.model.calls[2]["options"]["max_output_tokens"] == 2048
    j3 = next(iter(runtime.store.read()["memory"]["jobs"].values()))
    assert j3["state"] == "no_op"


def test_t6_settings_snapshot_exposes_capture_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T6 (covers 9): get_memory_settings_snapshot (and list_memories_snapshot) includes capture_status with recent 5 jobs, queued/failed counts, and today's calls/tokens against budgets."""
    clock = [1_700_200_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    runtime = FakeRuntime(tmp_path, responses=[TimeoutError("agent_inference_timeout")])
    _enable_memory(runtime, activation_policy="owner_review")
    _seed_task(runtime, "task_status_1", [("m1", "我的常用浏览器是 Firefox。")])

    worker = memory_worker.MemoryExtractionWorker(runtime)
    assert worker.run_once().activity is True

    # Add 5 more synthetic terminal jobs to verify top-5 capping and failed count
    def seed_extra_jobs(data: dict[str, Any]) -> None:
        mem = data["memory"]
        for i in range(6):
            jid = f"mjob_hist_{i}"
            mem["jobs"][jid] = {
                "id": jid,
                "idempotency_key": f"idem_hist_{i}",
                "state": "failed" if i % 2 == 0 else "committed",
                "task_id": f"task_hist_{i}",
                "error_code": "retry_exhausted" if i % 2 == 0 else None,
                "last_error": "extraction_timeout" if i % 2 == 0 else None,
                "retry_at": None,
                "attempts": 3 if i % 2 == 0 else 1,
                "created_at": clock[0] - 100 + i,
                "updated_at": clock[0] - 50 + i,
            }

    runtime.store.transaction(seed_extra_jobs)

    settings_snap = runtime.store.inspect(memory.get_memory_settings_snapshot)
    list_snap = runtime.store.inspect(memory.list_memories_snapshot)
    for snap in (settings_snap, list_snap):
        assert "capture_status" in snap
        cs = snap["capture_status"]
        assert cs["queued_count"] == 1
        assert cs["failed_count"] == 3
        assert len(cs["recent_jobs"]) == 5
        for item in cs["recent_jobs"]:
            assert {"state", "last_error", "retry_at", "updated_at"} <= set(item.keys())
        assert cs["daily_calls_budget"] == memory.DEFAULT_DAILY_EXTRACTION_CALLS
        assert cs["daily_tokens_budget"] == memory.DEFAULT_DAILY_EXTRACTION_TOKENS
        assert cs["daily_calls_used"] >= 1


def test_t7_cancelled_and_paused_tasks_captured_after_ten_minutes_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7 (covers 10): Cancelled/paused/failed tasks with no in_flight work are skipped while < 10 minutes old, and captured once unchanged for >= 10 minutes."""
    base_dt = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
    clock = [base_dt.timestamp()]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    stage1_obs = {
        "observations": [
            {
                "observation_id": "obs_cancel",
                "kind": "owner_preference",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "workflow",
                "fact_key": "personal.workflow.package_manager",
                "text": "前端包管理器偏好使用 pnpm",
                "evidence_span": "我平时前端项目一律用 pnpm",
                "source_message_ids": ["m_cancel"],
            }
        ]
    }
    stage2_ops = {
        "operations": [
            {
                "action": "add",
                "observation_ids": ["obs_cancel"],
                "target_entry_id": None,
                "expected_entry_revision": None,
                "scope": "personal",
                "kind": "owner_preference",
                "category": "workflow",
                "fact_key": "personal.workflow.package_manager",
                "text": "前端包管理器偏好使用 pnpm",
                "evidence_span": "我平时前端项目一律用 pnpm",
                "source_message_ids": ["m_cancel"],
            }
        ]
    }

    runtime = FakeRuntime(
        tmp_path,
        responses=[
            {"content": json.dumps(stage1_obs, ensure_ascii=False)},
            {"content": json.dumps(stage2_ops, ensure_ascii=False)},
        ],
    )
    _enable_memory(runtime, activation_policy="automatic")

    # Cancelled 2 minutes ago (ISO-8601 timestamp like real AgentRuntime) -> NOT eligible yet
    recent_iso = (base_dt - timedelta(minutes=2)).isoformat()
    _seed_task(
        runtime,
        "task_cancelled",
        [("m_cancel", "我平时前端项目一律用 pnpm。")],
        status="cancelled",
        updated_at=recent_iso,
    )
    # Also seed a cancelled task that IS >10m old but still has in_flight -> NOT eligible
    old_iso = (base_dt - timedelta(minutes=15)).isoformat()
    _seed_task(
        runtime,
        "task_cancelled_inflight",
        [("m_inf", "我平时后端用 FastAPI。")],
        status="cancelled",
        updated_at=old_iso,
        in_flight={"kind": "tool", "call_id": "c1"},
    )

    worker = memory_worker.MemoryExtractionWorker(runtime)
    assert worker.run_once().activity is False
    assert len(runtime.model.calls) == 0

    # Advance clock by 9 more minutes (total 11 minutes unchanged) -> task_cancelled IS extracted!
    clock[0] = (base_dt + timedelta(minutes=9)).timestamp()
    assert worker.run_once().activity is True
    assert len(runtime.model.calls) == 2

    snap = runtime.store.read()
    entries = [e for e in snap["memory"]["entries"].values() if e.get("status") == "active"]
    assert len(entries) == 1
    assert entries[0]["fact_key"] == "personal.workflow.package_manager"
    # In-flight task was still not enqueued
    assert all(j.get("task_id") != "task_cancelled_inflight" for j in snap["memory"]["jobs"].values())
