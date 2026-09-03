"""Tests for the service background-worker registry and LLM migration."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from rynmesh.background_workers import (
    BackgroundWorkerRegistry,
    BackgroundWorkerSpec,
    BackoffPolicy,
    WorkerRunResult,
)


def policy(**overrides) -> BackoffPolicy:
    values = {
        "busy_delay_s": 1.0,
        "idle_initial_s": 1.0,
        "idle_multiplier": 2.0,
        "idle_max_s": 4.0,
        "error_multiplier": 2.0,
        "error_max_s": 8.0,
    }
    values.update(overrides)
    return BackoffPolicy(**values)


class RecordingSleep:
    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            raise asyncio.CancelledError


def run_worker(spec: BackgroundWorkerSpec, *, stop_after: int) -> tuple[list[float], dict]:
    sleeper = RecordingSleep(stop_after)
    registry = BackgroundWorkerRegistry(sleep=sleeper)
    registry.register(spec)

    async def scenario() -> dict:
        with pytest.raises(asyncio.CancelledError):
            await registry._run(spec)
        return registry.status()[spec.name]

    return sleeper.delays, asyncio.run(scenario())


def test_registration_is_unique_deterministic_and_sealed_after_start() -> None:
    registry = BackgroundWorkerRegistry()
    second = BackgroundWorkerSpec("service.z", lambda: None, policy())
    first = BackgroundWorkerSpec("service.a", lambda: None, policy())
    registry.register(second)
    registry.register(first)
    assert [item.name for item in registry.specs()] == ["service.a", "service.z"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(first)

    async def scenario() -> None:
        await registry.start()
        with pytest.raises(RuntimeError, match="registration is closed"):
            registry.register(BackgroundWorkerSpec("late", lambda: None, policy()))
        await registry.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("busy_delay_s", 0.0),
        ("idle_initial_s", -1.0),
        ("idle_multiplier", 0.5),
        ("idle_max_s", float("inf")),
        ("error_multiplier", float("nan")),
        ("error_max_s", 0.0),
    ],
)
def test_policy_rejects_invalid_backoff(field: str, value: float) -> None:
    values = policy().__dict__
    values[field] = value
    with pytest.raises(ValueError):
        BackoffPolicy(**values)


def test_sync_worker_runs_off_event_loop_and_async_worker_is_awaited() -> None:
    async def scenario() -> None:
        main_thread = threading.get_ident()
        sync_threads: list[int] = []
        async_called = asyncio.Event()

        def sync_once() -> bool:
            sync_threads.append(threading.get_ident())
            return True

        async def async_once() -> WorkerRunResult:
            async_called.set()
            return WorkerRunResult(activity=True)

        registry = BackgroundWorkerRegistry()
        registry.register(BackgroundWorkerSpec("sync", sync_once, policy()))
        registry.register(BackgroundWorkerSpec("async", async_once, policy()))
        await registry.start()
        await asyncio.wait_for(async_called.wait(), timeout=2)
        deadline = asyncio.get_running_loop().time() + 2
        while not sync_threads and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        await registry.stop()
        assert sync_threads and sync_threads[0] != main_thread

    asyncio.run(scenario())


def test_idle_backoff_caps_and_activity_resets_to_busy_delay() -> None:
    results = iter([False, False, True])
    spec = BackgroundWorkerSpec(
        "poll", lambda: next(results),
        policy(busy_delay_s=0.25, idle_initial_s=1, idle_multiplier=2, idle_max_s=3),
    )
    delays, status = run_worker(spec, stop_after=3)
    assert delays == [2, 3, 0.25]
    assert status["consecutive_failures"] == 0
    assert status["error"] == ""


def test_failure_backoff_is_bounded_and_success_clears_error_sink() -> None:
    calls = 0
    errors: list[str] = []

    def worker() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("registry unavailable")
        return True

    spec = BackgroundWorkerSpec(
        "publish", worker,
        policy(busy_delay_s=1, error_multiplier=3, error_max_s=5),
        error_sink=errors.append,
    )
    delays, status = run_worker(spec, stop_after=3)
    assert delays == [3, 5, 1]
    assert errors[0] == "RuntimeError: background worker invocation failed"
    assert errors[-1] == ""
    assert status["consecutive_failures"] == 0
    assert status["last_failure_at"]
    assert status["last_success_at"]


def test_failing_worker_isolated_from_other_worker() -> None:
    async def scenario() -> None:
        healthy_ran = asyncio.Event()

        def failing() -> None:
            raise RuntimeError("temporary outage")

        async def healthy() -> bool:
            healthy_ran.set()
            return True

        registry = BackgroundWorkerRegistry()
        registry.register(BackgroundWorkerSpec("failing", failing, policy()))
        registry.register(BackgroundWorkerSpec("healthy", healthy, policy()))
        await registry.start()
        await asyncio.wait_for(healthy_ran.wait(), timeout=2)
        deadline = asyncio.get_running_loop().time() + 2
        while (
            registry.status()["failing"]["consecutive_failures"] == 0
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.001)
        snapshot = registry.status()
        await registry.stop()
        assert snapshot["failing"]["consecutive_failures"] == 1
        assert snapshot["healthy"]["last_success_at"]

    asyncio.run(scenario())


def test_stop_cancels_and_awaits_workers_without_recording_cancellation() -> None:
    async def scenario() -> tuple[bool, dict]:
        entered = asyncio.Event()
        finalized = asyncio.Event()

        async def worker() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        registry = BackgroundWorkerRegistry()
        registry.register(BackgroundWorkerSpec("long", worker, policy()))
        await registry.start()
        await asyncio.wait_for(entered.wait(), timeout=2)
        await registry.stop()
        return finalized.is_set(), registry.status()["long"]

    finalized, status = asyncio.run(scenario())
    assert finalized is True
    assert status["running"] is False
    assert status["consecutive_failures"] == 0
    assert status["error"] == ""


def test_status_is_bounded_metadata_and_uses_monotonic_schedule() -> None:
    ticks = iter([10.0, 11.0, 12.0])
    sleeper = RecordingSleep(stop_after=1)
    registry = BackgroundWorkerRegistry(sleep=sleeper, monotonic=lambda: next(ticks))
    private_marker = "PRIVATE_PROMPT_MARKER"
    spec = BackgroundWorkerSpec(
        "safe", lambda: {"prompt": private_marker}, policy(idle_multiplier=1),
    )
    registry.register(spec)

    async def scenario() -> dict:
        with pytest.raises(asyncio.CancelledError):
            await registry._run(spec)
        return registry.status()["safe"]

    status = asyncio.run(scenario())
    assert status["last_started_monotonic"] == 10.0
    assert status["next_run_monotonic"] is None
    assert private_marker not in json.dumps(status)

    private_error = "PRIVATE_ERROR_BODY_MARKER" * 100
    failing = BackgroundWorkerSpec(
        "bounded", lambda: (_ for _ in ()).throw(ValueError(private_error)), policy(),
    )
    _, failed_status = run_worker(failing, stop_after=1)
    assert private_error not in failed_status["error"]
    assert len(failed_status["error"]) <= 512


def test_create_app_registers_only_the_two_llm_service_workers(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)
    registry = app.state.background_workers
    assert [spec.name for spec in registry.specs()] == [
        "llm.publish-refresh",
        "llm.relay-poll",
    ]
    specs = {spec.name: spec for spec in registry.specs()}
    assert specs["llm.relay-poll"].initial_delay_s == 1
    assert specs["llm.relay-poll"].policy.busy_delay_s == 1
    assert specs["llm.relay-poll"].policy.idle_multiplier == 1.5
    assert specs["llm.relay-poll"].policy.idle_max_s == 10
    assert specs["llm.relay-poll"].policy.error_max_s == 30
    assert specs["llm.publish-refresh"].initial_delay_s == 1
    assert specs["llm.publish-refresh"].policy.busy_delay_s == 30
    assert not hasattr(app.state, "llm_publish_once")
    assert not hasattr(app.state, "llm_relay_once")

    with TestClient(app):
        running = registry.status()
        assert all(item["running"] for item in running.values())
    stopped = registry.status()
    assert all(not item["running"] for item in stopped.values())
