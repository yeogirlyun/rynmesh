"""A bounded slow provider can finish, while actual capture revocation still cancels.

Clock injection is confined to worker/foundation memory modules. No process-wide
clock patch, sleep, real model, manufactured lease, or canonical candidate.
"""
import importlib
import time
from types import SimpleNamespace

import pytest
from test_memory_integration_contract import (
    finish,
    tick_memory_workers,
)
from test_memory_integration_contract import (
    integrated as integrated,
)
from test_memory_integration_contract import (
    prohibit_network as prohibit_network,
)


def prepare_delayed_provider(env, monkeypatch, elapsed, action=None):
    env.api.write("PUT", "/settings", {
        "capture_enabled": True,
        "extraction_destination": env.catalog.destination(env.catalog.row),
    })
    task_id = env.api.create_task("我的机身是富士 X-T5。")
    finish(env, task_id)
    worker = importlib.import_module("rynmesh.agent_runtime.memory_worker")
    memory = importlib.import_module("rynmesh.agent_runtime.memory")
    elapsed_clock = {"value": 0.0}
    wall_start, mono_start = time.time(), time.monotonic()
    fake_time = SimpleNamespace(
        time=lambda: wall_start + elapsed_clock["value"],
        monotonic=lambda: mono_start + elapsed_clock["value"],
        strftime=time.strftime, gmtime=time.gmtime,
    )
    monkeypatch.setattr(worker, "time", fake_time)
    monkeypatch.setattr(memory, "time", fake_time)
    env.bridge.supports_request_deadline = True
    env.bridge.supports_output_token_limit = True
    original = env.bridge.complete
    observations = []

    def delayed(model_id, messages, tools, cancelled, on_event=None, **options):
        assert not tools
        assert options["expected_destination"] == env.catalog.destination(env.catalog.row)
        response = original(model_id, messages, tools, cancelled, on_event=on_event, **options)
        elapsed_clock["value"] += elapsed
        if action == "capture_off":
            env.api.write("PUT", "/settings", {"capture_enabled": False})
        cancelled_now = cancelled()
        observations.append({"elapsed": elapsed, "cancelled": cancelled_now,
            "reason": getattr(cancelled, "reason", None),
            "deadline_expired": fake_time.monotonic() >= options["deadline"]})
        return response

    env.bridge.complete = delayed
    return observations


@pytest.mark.parametrize("elapsed", [25.0, 75.0])
def test_slow_bounded_provider_commits_candidate_with_a_live_real_lease(integrated, monkeypatch, elapsed):
    env = integrated
    observations = prepare_delayed_provider(env, monkeypatch, elapsed)
    tick_memory_workers(env)
    assert observations == [{"elapsed": elapsed, "cancelled": False, "reason": None, "deadline_expired": False}]
    snapshot = env.api.snapshot()
    assert not snapshot["entries"] and len(snapshot["candidates"]) == 1
    assert snapshot["candidates"][0]["status"] == "pending"
    state = env.runtime.store.read()["memory"]
    assert any(job["state"] == "committed" for job in state["jobs"].values())
    assert state["daily_usage"]["extraction_calls"] == 1


def test_capture_off_cancels_a_slow_actual_extraction_without_candidate(integrated, monkeypatch):
    env = integrated
    observations = prepare_delayed_provider(env, monkeypatch, 25.0, action="capture_off")
    tick_memory_workers(env)
    assert observations[0]["cancelled"] is True
    assert observations[0]["reason"] == "capture_disabled"
    snapshot = env.api.snapshot()
    assert snapshot["settings"]["capture_enabled"] is False
    assert not snapshot["entries"] and not snapshot["candidates"]
    assert all(job["state"] == "stale" for job in env.runtime.store.read()["memory"]["jobs"].values())


def test_a_provider_past_the_bounded_deadline_still_cannot_commit(integrated, monkeypatch):
    env = integrated
    worker = importlib.import_module("rynmesh.agent_runtime.memory_worker")
    elapsed = worker.DEFAULT_EXTRACTION_TIMEOUT_SECONDS + 1.0
    assert elapsed < worker.DEFAULT_WORKER_LEASE_SECONDS
    observations = prepare_delayed_provider(env, monkeypatch, elapsed)
    tick_memory_workers(env)
    assert observations[0]["cancelled"] is True and observations[0]["deadline_expired"] is True
    assert observations[0]["reason"] == "extraction_timeout"
    snapshot = env.api.snapshot()
    assert not snapshot["entries"] and not snapshot["candidates"]
    state = env.runtime.store.read()["memory"]
    assert any(job.get("error_code") == "extraction_timeout" for job in state["jobs"].values())
    assert state["daily_usage"]["extraction_calls"] == 1
