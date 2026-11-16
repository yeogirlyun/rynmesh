"""Tests for S01-I22: ModelUsageStore corrupt backup & quota separation, peer/daily pruning,
unstarted dispatch refund, pre-dispatch token quota clamping, and per-caller today split."""
from __future__ import annotations

import json
import logging
import threading

import pytest

from rynmesh.llm_package import model_settings
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.model_usage import (
    MAX_USAGE_BYTES,
    ModelUsageStore,
    usage_caller_scope,
)
from rynmesh.llm_package.sources import SourceStore


class _DummySources:
    def __init__(self, root, *, max_output_tokens: int = 4096, max_concurrent: int = 2):
        self.path = root / "llm" / "sources.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_store = ModelUsageStore(root / "llm" / "model-usage.json")
        self.lock = threading.RLock()
        self._active_models: dict[str, int] = {}
        record = {
            "id": "src-1",
            "kind": "api",
            "enabled": True,
            "base_url": "http://127.0.0.1:9999/v1",
            "models": ["m1"],
            "model_settings": {},
        }
        model_settings.reconcile(record, {})
        record["model_settings"]["m1"]["configuration"] = {
            "context_window": 32768,
            "max_output_tokens": max_output_tokens,
            "max_concurrent": max_concurrent,
        }
        self._record = record
        self.service_id = SourceStore.model_service_id(record, "m1")

    def model_service_id(self, record, model):
        return SourceStore.model_service_id(record, model)

    def read(self):
        return {"src-1": self._record}


def test_t1_t2_corrupt_usage_file_is_renamed_and_quota_config_survives(tmp_path, caplog):
    usage_path = tmp_path / "llm" / "model-usage.json"
    quota_path = tmp_path / "llm" / "model-quota.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)

    # Verify one-time migration from legacy model-usage.json limits without deleting legacy field.
    legacy_payload = {
        "local/migrated-model": {
            "today": {"day": ModelUsageStore.utc_day(), "requests": 2, "total_tokens": 50},
            "total": {"requests": 2, "total_tokens": 50},
            "limits": {"requests_per_day": 25, "tokens_per_day": 8000, "shared_tokens_per_day": None},
        }
    }
    usage_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    store = ModelUsageStore(usage_path)
    assert quota_path.exists()
    migrated_quota = json.loads(quota_path.read_text(encoding="utf-8"))
    assert migrated_quota["local/migrated-model"]["requests_per_day"] == 25
    assert migrated_quota["local/migrated-model"]["tokens_per_day"] == 8000
    assert json.loads(usage_path.read_text(encoding="utf-8"))["local/migrated-model"]["limits"]["requests_per_day"] == 25

    # Configure quota via configure_quota -> stored in model-quota.json.
    store.configure_quota(
        "local/m1",
        {"requests_per_day": 10, "tokens_per_day": 5000, "shared_tokens_per_day": 1200},
    )
    saved_quota = json.loads(quota_path.read_text(encoding="utf-8"))
    assert saved_quota["local/m1"] == {
        "requests_per_day": 10,
        "tokens_per_day": 5000,
        "shared_tokens_per_day": 1200,
    }

    # Corrupt model-usage.json and verify backup rename + warning log + surviving quota.
    usage_path.write_text("{corrupt-json-payload", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rynmesh.llm_package.model_usage"):
        pub = store.public_usage("local/m1")

    backups = list(usage_path.parent.glob("model-usage.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{corrupt-json-payload"
    assert any("model-usage.json" in rec.message for rec in caplog.records)

    assert pub["quota"]["requests_per_day"] == 10
    assert pub["quota"]["tokens_per_day"] == 5000
    assert pub["quota"]["shared_tokens_per_day"] == 1200
    assert store.public_usage("local/migrated-model")["quota"]["requests_per_day"] == 25


def test_t3_oversized_peer_and_daily_entries_pruned_below_4mib_without_corrupt_backup(tmp_path):
    usage_path = tmp_path / "llm" / "model-usage.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    today = ModelUsageStore.utc_day()

    # Build a valid JSON > 4 MiB with >64 peers and >90 daily buckets per model.
    many_days = [
        {
            "day": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "requests": 3,
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 10,
            "total_tokens": 150,
            "tokens": 150,
        }
        for i in range(140)
    ]
    peers = {
        f"peer-device-{p:04d}": {
            "channel": "company",
            "name": f"Member-{p:04d}-" + ("x" * 40),
            "last_used_at": f"2026-11-{(p % 28) + 1:02d}T10:00:00+00:00",
            "daily": many_days,
        }
        for p in range(260)
    }
    big_data = {
        f"local/model-{m}": {
            "today": {"day": today, "requests": 1, "total_tokens": 10},
            "total": {"requests": 10, "total_tokens": 100, "last_used_at": "2026-11-16T00:00:00+00:00"},
            "daily": many_days,
            "by_peer": peers if m < 3 else {},
        }
        for m in range(4)
    }
    usage_path.write_text(json.dumps(big_data), encoding="utf-8")
    assert usage_path.stat().st_size > MAX_USAGE_BYTES

    store_a = ModelUsageStore(usage_path)
    store_b = ModelUsageStore(usage_path)
    data = store_a.read_cached()

    # Must NOT treat >4 MiB valid JSON as corrupt.
    assert list(usage_path.parent.glob("model-usage.json.corrupt-*")) == []
    assert len(data["local/model-0"]["by_peer"]) == 64
    assert len(data["local/model-0"]["daily"]) == 90
    for peer_bucket in data["local/model-0"]["by_peer"].values():
        assert len(peer_bucket["daily"]) == 90

    # Concurrent/separate store instances share disk state without in-memory write buffering loss.
    store_a.admit_and_record_dispatch("local/model-0", caller="self", task_id="t-a")
    store_b.admit_and_record_dispatch("local/model-0", caller="self", task_id="t-b")
    assert usage_path.stat().st_size < MAX_USAGE_BYTES
    assert store_a.public_usage("local/model-0")["today"]["requests"] == 3


def test_t4_unstarted_connection_or_busy_dispatch_is_refunded_while_started_or_timeout_is_not(tmp_path):
    sources = _DummySources(tmp_path)
    usage_key = f"local/{sources.service_id}"
    outcome: list[Exception | dict] = []

    class _Inner:
        def chat(self, body, **kwargs):
            item = outcome.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    adapter = ModelDispatchAdapter(sources, sources.read()["src-1"], "m1", _Inner())

    # 1. runtime_connection_failed in request phase -> refunded.
    outcome.append(
        AdapterError(
            "conn refused",
            code="runtime_connection_failed",
            diagnostics={"network_reason": "refused", "network_phase": "request"},
        )
    )
    with pytest.raises(AdapterError):
        adapter.chat({"messages": [{"role": "user", "content": "hi"}]}, task_id="req-conn-fail")
    u = sources.usage_store.public_usage(usage_key)
    assert u["today"]["requests"] == 0
    assert u["total"]["requests"] == 0
    assert u["by_caller"]["self"]["today"]["requests"] == 0

    # 2. runtime_busy in request phase -> refunded (including shared caller counters).
    outcome.append(
        AdapterError(
            "upstream busy",
            code="runtime_busy",
            diagnostics={"network_reason": "http", "network_phase": "request"},
        )
    )
    with usage_caller_scope("shared", peer={"channel": "company", "peer_id": "p1", "name": "Colleague"}):
        with pytest.raises(AdapterError):
            adapter.chat({"messages": [{"role": "user", "content": "hi"}]}, task_id="req-busy")
    u = sources.usage_store.public_usage(usage_key)
    assert u["today"]["requests"] == 0
    assert u["today"]["shared_requests"] == 0
    assert u["by_caller"]["shared"]["today"]["requests"] == 0

    # 3. runtime_connection_failed in read phase (response already started) -> NOT refunded.
    outcome.append(
        AdapterError(
            "mid-stream drop",
            code="runtime_connection_failed",
            diagnostics={"network_reason": "reset", "network_phase": "read"},
        )
    )
    with pytest.raises(AdapterError):
        adapter.chat({"messages": [{"role": "user", "content": "hi"}]}, task_id="read-drop")
    u = sources.usage_store.public_usage(usage_key)
    assert u["today"]["requests"] == 1
    assert u["today"]["failed_requests"] == 1

    # 4. inference_timeout (even in request phase) -> NOT refunded.
    outcome.append(
        AdapterError(
            "timed out",
            code="inference_timeout",
            diagnostics={"network_reason": "idle_timeout", "network_phase": "request"},
        )
    )
    with pytest.raises(AdapterError):
        adapter.chat({"messages": [{"role": "user", "content": "hi"}]}, task_id="req-timeout")
    u = sources.usage_store.public_usage(usage_key)
    assert u["today"]["requests"] == 2
    assert u["today"]["failed_requests"] == 2


def test_t5_daily_token_quota_clamps_max_tokens_when_at_least_256_and_rejects_when_below_256(tmp_path):
    sources = _DummySources(tmp_path, max_output_tokens=4096)
    usage_key = f"local/{sources.service_id}"
    sources.usage_store.configure_quota(
        usage_key,
        {"tokens_per_day": 2000, "shared_tokens_per_day": 1500},
    )

    seen_bodies: list[dict] = []

    class _Inner:
        def chat(self, body, **kwargs):
            seen_bodies.append(dict(body))
            return {"text": "ok", "input_tokens": 500, "output_tokens": 500}

    adapter = ModelDispatchAdapter(sources, sources.read()["src-1"], "m1", _Inner())

    # Consume 1000 tokens first -> remaining tokens_per_day is 1000.
    adapter.chat({"messages": [{"role": "user", "content": "1"}], "max_tokens": 600}, task_id="step-1")
    assert sources.usage_store.public_usage(usage_key)["quota"]["tokens_remaining"] == 1000

    # Remaining 1000 >= 256: request with max_tokens=4096 must be clamped to 1000!
    class _SmallUsageInner:
        def chat(self, body, **kwargs):
            seen_bodies.append(dict(body))
            return {"text": "ok", "input_tokens": 450, "output_tokens": 450}

    adapter_clamp = ModelDispatchAdapter(sources, sources.read()["src-1"], "m1", _SmallUsageInner())
    adapter_clamp.chat({"messages": [{"role": "user", "content": "2"}], "max_tokens": 4096}, task_id="step-2")
    assert seen_bodies[-1]["max_tokens"] == 1000

    # Now total_tokens is 1900 -> remaining is 100 (< 256): max_tokens=4096 must be rejected!
    assert sources.usage_store.public_usage(usage_key)["quota"]["tokens_remaining"] == 100
    with pytest.raises(AdapterError) as exc_info:
        adapter_clamp.chat(
            {"messages": [{"role": "user", "content": "3"}], "max_tokens": 4096},
            task_id="step-3-rejected",
        )
    assert exc_info.value.code == "model_daily_quota_exhausted"

    # Also verify shared_tokens_per_day clamping & rejection directly.
    sources.usage_store.configure_quota(usage_key, {"tokens_per_day": 10000, "shared_tokens_per_day": 1000})
    admitted = sources.usage_store.admit_and_record_dispatch(
        usage_key, caller="shared", task_id="shared-clamp", max_tokens=4096
    )
    assert admitted["granted_max_tokens"] == 1000
    sources.usage_store.record_completion(
        usage_key, caller="shared", task_id="shared-clamp", input_tokens=450, output_tokens=450
    )
    with pytest.raises(AdapterError) as shared_exc:
        sources.usage_store.admit_and_record_dispatch(
            usage_key, caller="shared", task_id="shared-reject", max_tokens=4096
        )
    assert shared_exc.value.code == "model_daily_quota_exhausted"


def test_t6_by_caller_today_tracks_input_output_cached_and_hit_rate_for_self_and_shared(tmp_path):
    store = ModelUsageStore(tmp_path / "llm" / "model-usage.json")
    key = "local/caller-split"

    store.admit_and_record_dispatch(key, caller="self", task_id="self-1")
    store.record_completion(
        key,
        caller="self",
        task_id="self-1",
        input_tokens=200,
        output_tokens=80,
        cached_tokens=50,
    )

    store.admit_and_record_dispatch(key, caller="shared", task_id="shared-1")
    store.record_completion(
        key,
        caller="shared",
        task_id="shared-1",
        input_tokens=400,
        output_tokens=120,
        cached_tokens=300,
    )

    pub = store.public_usage(key)
    self_today = pub["by_caller"]["self"]["today"]
    shared_today = pub["by_caller"]["shared"]["today"]

    assert self_today["requests"] == 1
    assert self_today["input_tokens"] == 200
    assert self_today["output_tokens"] == 80
    assert self_today["cached_tokens"] == 50
    assert self_today["total_tokens"] == 280
    assert self_today["cache_hit_rate"] == 0.25

    assert shared_today["requests"] == 1
    assert shared_today["input_tokens"] == 400
    assert shared_today["output_tokens"] == 120
    assert shared_today["cached_tokens"] == 300
    assert shared_today["total_tokens"] == 520
    assert shared_today["cache_hit_rate"] == 0.75

    # Zero input tokens yields 0.0 cache_hit_rate without ZeroDivisionError.
    empty_pub = store.public_usage("local/unused-model")
    assert empty_pub["by_caller"]["self"]["today"]["cache_hit_rate"] == 0.0
    assert empty_pub["by_caller"]["shared"]["today"]["cache_hit_rate"] == 0.0


def test_t7_by_caller_today_counters_reset_across_utc_day_rollover(tmp_path, monkeypatch):
    current_day = ["2026-11-16"]
    monkeypatch.setattr("rynmesh.llm_package.model_usage._utc_today", lambda: current_day[0])

    store = ModelUsageStore(tmp_path / "llm" / "model-usage.json")
    key = "local/rollover-model"

    store.admit_and_record_dispatch(key, caller="self", task_id="d1-self")
    store.record_completion(
        key, caller="self", task_id="d1-self", input_tokens=100, output_tokens=40, cached_tokens=20
    )
    store.admit_and_record_dispatch(key, caller="shared", task_id="d1-shared")
    store.record_completion(
        key, caller="shared", task_id="d1-shared", input_tokens=300, output_tokens=60, cached_tokens=150
    )
    assert store.public_usage(key)["by_caller"]["self"]["today"]["input_tokens"] == 100
    assert store.public_usage(key)["by_caller"]["shared"]["today"]["cached_tokens"] == 150

    # Roll over to next UTC day: today split resets to 0 while total counters remain intact.
    current_day[0] = "2026-11-17"
    after = store.public_usage(key)
    for caller in ("self", "shared"):
        today_bucket = after["by_caller"][caller]["today"]
        assert today_bucket["day"] == "2026-11-17"
        assert today_bucket["requests"] == 0
        assert today_bucket["input_tokens"] == 0
        assert today_bucket["output_tokens"] == 0
        assert today_bucket["cached_tokens"] == 0
        assert today_bucket["total_tokens"] == 0
        assert today_bucket["cache_hit_rate"] == 0.0

    assert after["by_caller"]["self"]["total"]["input_tokens"] == 100
    assert after["by_caller"]["shared"]["total"]["cached_tokens"] == 150


def test_t8_legacy_usage_files_without_caller_today_split_load_without_error(tmp_path):
    usage_path = tmp_path / "llm" / "model-usage.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    today = ModelUsageStore.utc_day()

    # Old schema missing today_input_tokens / today_output_tokens / today_cached_tokens, plus v0 entry.
    old_data = {
        "local/old-v2": {
            "today": {
                "day": today,
                "requests": 2,
                "succeeded_requests": 2,
                "input_tokens": 120,
                "output_tokens": 80,
                "cached_tokens": 40,
                "total_tokens": 200,
            },
            "total": {
                "requests": 2,
                "succeeded_requests": 2,
                "input_tokens": 120,
                "output_tokens": 80,
                "cached_tokens": 40,
                "total_tokens": 200,
            },
            "by_caller": {
                "self": {
                    "day": today,
                    "today_requests": 2,
                    "today_tokens": 200,
                    "requests": 2,
                    "input_tokens": 120,
                    "output_tokens": 80,
                    "cached_tokens": 40,
                    "total_tokens": 200,
                }
            },
        },
        "v0-only": {"day": today, "requests": 4},
    }
    usage_path.write_text(json.dumps(old_data), encoding="utf-8")

    store = ModelUsageStore(usage_path)
    pub = store.public_usage("local/old-v2")
    assert pub["by_caller"]["self"]["today"]["requests"] == 2
    assert pub["by_caller"]["self"]["today"]["total_tokens"] == 200
    assert pub["by_caller"]["self"]["today"]["input_tokens"] == 0
    assert pub["by_caller"]["self"]["today"]["output_tokens"] == 0
    assert pub["by_caller"]["self"]["today"]["cached_tokens"] == 0
    assert pub["by_caller"]["self"]["today"]["cache_hit_rate"] == 0.0

    # Subsequent completion on the same day accumulates normally from 0.
    store.admit_and_record_dispatch("local/old-v2", caller="self", task_id="after-upgrade")
    store.record_completion(
        "local/old-v2",
        caller="self",
        task_id="after-upgrade",
        input_tokens=50,
        output_tokens=25,
        cached_tokens=25,
    )
    updated = store.public_usage("local/old-v2")
    assert updated["by_caller"]["self"]["today"]["input_tokens"] == 50
    assert updated["by_caller"]["self"]["today"]["output_tokens"] == 25
    assert updated["by_caller"]["self"]["today"]["cached_tokens"] == 25
    assert updated["by_caller"]["self"]["today"]["cache_hit_rate"] == 0.5
    assert store.public_usage("local/v0-only")["today"]["requests"] == 4
