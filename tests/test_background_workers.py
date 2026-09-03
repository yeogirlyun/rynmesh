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


class _Death(BaseException):
    """A crash that is not an `Exception` (e.g. asyncio's own cancel-adjacent
    internals can raise `BaseException` subclasses). Never `KeyboardInterrupt`
    or `SystemExit` here — those would abort the pytest run itself.
    """


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


def test_registration_is_unique_and_deterministic() -> None:
    registry = BackgroundWorkerRegistry()
    second = BackgroundWorkerSpec("service.z", lambda: None, policy())
    first = BackgroundWorkerSpec("service.a", lambda: None, policy())
    registry.register(second)
    registry.register(first)
    assert [item.name for item in registry.specs()] == ["service.a", "service.z"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(first)


def test_late_registration_runs_and_replace_swaps_the_running_worker() -> None:
    async def scenario() -> None:
        registry = BackgroundWorkerRegistry()
        await registry.start()

        late_ran = asyncio.Event()
        registry.register(
            BackgroundWorkerSpec("late", lambda: late_ran.set(), policy()),
        )
        await asyncio.wait_for(late_ran.wait(), timeout=2)

        with pytest.raises(ValueError, match="already registered"):
            registry.register(BackgroundWorkerSpec("late", lambda: None, policy()))

        old_calls = 0
        old_ran = asyncio.Event()

        def old_body() -> bool:
            nonlocal old_calls
            old_calls += 1
            old_ran.set()
            return True

        registry.register(
            BackgroundWorkerSpec("late", old_body, policy(busy_delay_s=0.01)),
            replace=True,
        )
        await asyncio.wait_for(old_ran.wait(), timeout=2)

        new_ran = asyncio.Event()

        def new_body() -> bool:
            new_ran.set()
            return True

        registry.register(
            BackgroundWorkerSpec("late", new_body, policy()), replace=True,
        )
        await asyncio.wait_for(new_ran.wait(), timeout=2)
        calls_at_replace = old_calls
        # Give the (cancelled) old task every chance to sneak in one more
        # call before asserting it truly stopped running.
        await asyncio.sleep(0.05)

        status = registry.status()["late"]
        await registry.stop()
        # The old task was cancelled before the new one was spawned; at most
        # one already-dispatched call could have still been in flight.
        assert old_calls <= calls_at_replace + 1
        assert status["restarts"] == 0

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


def test_register_replaces_a_worker_only_when_asked() -> None:
    registry = BackgroundWorkerRegistry()
    first = BackgroundWorkerSpec("service.a", lambda: None, policy())
    second = BackgroundWorkerSpec("service.a", lambda: True, policy())
    registry.register(first)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(second)
    registry.register(second, replace=True)
    assert registry.specs() == (second,)


def test_create_app_registers_the_llm_and_mailbox_service_workers(tmp_path, monkeypatch) -> None:
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
        "mailbox.poll",
    ]
    specs = {spec.name: spec for spec in registry.specs()}
    assert specs["mailbox.poll"].initial_delay_s == 3
    assert specs["mailbox.poll"].policy.busy_delay_s == 2
    assert specs["mailbox.poll"].policy.idle_max_s == 60
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


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.005)


def test_crash_from_a_non_exception_baseexception_restarts_the_worker() -> None:
    async def scenario() -> tuple[int, dict, list[str]]:
        calls = 0

        def flaky() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _Death
            return True

        errors: list[str] = []
        spec = BackgroundWorkerSpec(
            "crashy",
            flaky,
            policy(busy_delay_s=0.01, idle_initial_s=0.01, idle_max_s=0.01, error_max_s=0.02),
            error_sink=errors.append,
        )
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        # Capture status right after the crash is recorded, before the
        # restart's later success has a chance to clear `error`.
        await _wait_until(lambda: registry.status()["crashy"]["crash_class"] != "")
        crash_status = registry.status()["crashy"]
        await _wait_until(lambda: calls >= 2)
        final_status = registry.status()["crashy"]
        await registry.stop()
        return calls, crash_status, final_status, errors

    calls, crash_status, final_status, errors = asyncio.run(scenario())
    assert calls >= 2
    assert final_status["restarts"] >= 1
    assert crash_status["crash_class"] == "_Death"
    assert crash_status["error"] == "_Death: background worker crashed"
    assert errors.count("_Death: background worker crashed") == 1


def test_no_restart_scheduled_after_stop() -> None:
    async def scenario() -> tuple[int, dict]:
        calls = 0

        def always_dies() -> None:
            nonlocal calls
            calls += 1
            raise _Death

        spec = BackgroundWorkerSpec(
            "crashy2", always_dies, policy(error_max_s=5.0),
        )
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        await _wait_until(lambda: registry.status()["crashy2"]["crash_class"] != "")
        await registry.stop()
        # The restart timer (error_max_s=5.0) would fire long after this
        # window; if stop() failed to cancel it, calls would grow past 1.
        await asyncio.sleep(0.05)
        return calls, registry.status()["crashy2"]

    calls, status = asyncio.run(scenario())
    assert calls == 1
    assert status["running"] is False


def test_stop_is_bounded_and_reports_abandoned_workers() -> None:
    # Note: a sync `run_once` blocked on a `threading.Event` (as sketched in
    # the task brief) turns out not to reproduce a stuck `stop()` here --
    # cancelling a task suspended on `asyncio.to_thread` resolves the *task*
    # immediately (it just leaks the underlying OS thread), which is a real
    # but different hazard than a hung `stop()`. To exercise the bounded-wait
    # and `abandoned` bookkeeping itself, this uses a worker that -- like a
    # shielded critical section -- does not honor the first cancellation
    # request immediately.
    async def scenario() -> tuple[float, dict]:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        async def stubborn() -> bool:
            entered.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                await asyncio.sleep(0.4)
                raise
            return True

        spec = BackgroundWorkerSpec("stuck", stubborn, policy())
        registry = BackgroundWorkerRegistry(stop_timeout_s=0.2)
        registry.register(spec)
        await registry.start()
        await asyncio.wait_for(entered.wait(), timeout=2)

        start = loop.time()
        result = await registry.stop()
        elapsed = loop.time() - start

        # Let the stubborn worker actually finish so it doesn't linger past
        # the test.
        await asyncio.sleep(0.5)
        return elapsed, result

    elapsed, result = asyncio.run(scenario())
    assert elapsed < 1.0
    assert result == {"stopped": [], "abandoned": ["stuck"]}


def test_error_max_s_below_busy_delay_s_is_rejected() -> None:
    with pytest.raises(ValueError, match="error_max_s must be at least busy_delay_s"):
        policy(busy_delay_s=1.0, error_max_s=0.5)


def test_backoff_policy_fixed_uses_one_flat_interval() -> None:
    fixed = BackoffPolicy.fixed(5.0)
    assert fixed.busy_delay_s == 5.0
    assert fixed.idle_initial_s == 5.0
    assert fixed.idle_multiplier == 1.0
    assert fixed.idle_max_s == 5.0
    assert fixed.error_multiplier == 1.0
    assert fixed.error_max_s == 5.0

    results = iter([False, False, True])
    spec = BackgroundWorkerSpec("clockwork", lambda: next(results), BackoffPolicy.fixed(5.0))
    delays, _ = run_worker(spec, stop_after=3)
    assert delays == [5.0, 5.0, 5.0]


def test_sink_notified_once_for_repeated_identical_failure_then_on_recovery() -> None:
    calls = 0
    errors: list[str] = []

    def worker() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("boom")
        return True

    spec = BackgroundWorkerSpec(
        "dedup", worker, policy(error_max_s=8), error_sink=errors.append,
    )
    run_worker(spec, stop_after=3)
    assert errors == ["RuntimeError: background worker invocation failed", ""]


def test_crash_message_is_redacted_from_status() -> None:
    marker = "PRIVATE_CRASH_MARKER"

    class _DeathWithMarker(_Death):
        def __str__(self) -> str:
            return marker

    async def scenario() -> dict:
        def die() -> None:
            raise _DeathWithMarker

        spec = BackgroundWorkerSpec("redact", die, policy(error_max_s=5.0))
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        await _wait_until(lambda: registry.status()["redact"]["crash_class"] != "")
        status = registry.status()
        await registry.stop()
        return status

    status = asyncio.run(scenario())
    assert marker not in json.dumps(status)
