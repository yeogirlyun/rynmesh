"""Supervised, in-process background workers for service packages.

Workers normally register during application construction; a worker
registered after ``start()`` is spawned immediately, so late installers are
picked up rather than rejected. The node lifespan starts and stops the
registry as one unit; service packages do not own detached asyncio tasks or
need service-specific lifecycle wiring.

Supervision contract:

- An invocation that raises is backed off and retried. The message is recorded
  (truncated) and forwarded to the worker's error sink, then cleared on the
  next success, so operators can tell a registry outage from an unhealthy
  runtime.
- A worker task that dies for any other reason (a ``BaseException`` escaping,
  or a supervisor bug) is logged, its error is recorded and forwarded, and it
  is restarted after the policy's ``error_max_s``. A dead worker never
  silently looks healthy.
- ``stop()`` cancels every task and waits a bounded time. A sync worker
  running in the default executor cannot be interrupted mid-tick: its task is
  unwound at the ``await`` while the thread finishes its in-flight call.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 512
_REDACTED_ERROR = "background worker invocation failed"


@dataclass(frozen=True)
class WorkerRunResult:
    """Explicit activity signal for workers that want to be unambiguous.

    Any other return value is interpreted as ``bool(result)``: an int count
    of processed items, a non-empty dict, or ``True`` all mean "busy".
    """

    activity: bool = False


@dataclass(frozen=True)
class BackoffPolicy:
    """Busy, idle, and failure scheduling policy for one worker."""

    busy_delay_s: float
    idle_initial_s: float
    idle_multiplier: float
    idle_max_s: float
    error_multiplier: float
    error_max_s: float

    def __post_init__(self) -> None:
        delays = {
            "busy_delay_s": self.busy_delay_s,
            "idle_initial_s": self.idle_initial_s,
            "idle_max_s": self.idle_max_s,
            "error_max_s": self.error_max_s,
        }
        for name, value in delays.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name, value in {
            "idle_multiplier": self.idle_multiplier,
            "error_multiplier": self.error_multiplier,
        }.items():
            if not math.isfinite(value) or value < 1:
                raise ValueError(f"{name} must be finite and at least 1")
        if self.idle_max_s < self.idle_initial_s:
            raise ValueError("idle_max_s must be at least idle_initial_s")
        # A failing worker must never retry faster than a healthy one.
        if self.error_max_s < self.busy_delay_s:
            raise ValueError("error_max_s must be at least busy_delay_s")

    @classmethod
    def fixed(cls, interval_s: float) -> "BackoffPolicy":
        """Run at one cadence regardless of activity or failure."""
        return cls(
            busy_delay_s=interval_s, idle_initial_s=interval_s, idle_multiplier=1.0,
            idle_max_s=interval_s, error_multiplier=1.0, error_max_s=interval_s,
        )


@dataclass(frozen=True)
class BackgroundWorkerSpec:
    """One service-owned unit of repeatable background work."""

    name: str
    run_once: Callable[[], object]
    policy: BackoffPolicy
    initial_delay_s: float = 0.0
    error_sink: Callable[[str], None] | None = None
    # Opt in for workers whose exceptions could carry a prompt, key, or
    # private path. The default keeps the (truncated) message: operators
    # need "registry unreachable" vs "unhealthy service" to act.
    redact_errors: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("worker name must not be empty")
        if len(self.name) > 128:
            raise ValueError("worker name is too long")
        if not callable(self.run_once):
            raise TypeError("run_once must be callable")
        if not math.isfinite(self.initial_delay_s) or self.initial_delay_s < 0:
            raise ValueError("initial_delay_s must be finite and non-negative")
        if self.error_sink is not None and not callable(self.error_sink):
            raise TypeError("error_sink must be callable")


@dataclass
class _WorkerState:
    last_success_at: str = ""
    last_failure_at: str = ""
    last_started_monotonic: float | None = None
    next_run_monotonic: float | None = None
    consecutive_failures: int = 0
    restarts: int = 0
    error: str = ""
    # Last value forwarded to the sink, so a healthy 1s worker doesn't
    # re-send "" every tick.
    sent_error: str | None = None


class BackgroundWorkerRegistry:
    """Own, supervise, and shut down registered service workers."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        stop_timeout_s: float = 5.0,
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._stop_timeout_s = stop_timeout_s
        self._specs: dict[str, BackgroundWorkerSpec] = {}
        self._states: dict[str, _WorkerState] = {}
        self._invokers: dict[str, Callable[[], Awaitable[object]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False

    # ---- registration ---------------------------------------------------
    def register(self, spec: BackgroundWorkerSpec, *, replace: bool = False) -> None:
        """Register one worker; spawn it immediately if the registry is running.

        ``replace=True`` makes re-installation idempotent: an existing worker
        of the same name is cancelled and superseded.
        """
        existing = self._specs.get(spec.name)
        if existing is not None and not replace:
            raise ValueError(f"background worker already registered: {spec.name}")
        if existing is not None:
            task = self._tasks.pop(spec.name, None)
            if task is not None:
                task.cancel()
        self._specs[spec.name] = spec
        self._states[spec.name] = _WorkerState()
        self._invokers[spec.name] = self._make_invoker(spec.run_once)
        if self._started:
            self._spawn(spec, delay_s=spec.initial_delay_s)

    def specs(self) -> tuple[BackgroundWorkerSpec, ...]:
        """Return a deterministic, immutable view of registered workers."""
        return tuple(self._specs[name] for name in sorted(self._specs))

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        """Create exactly one supervised task for every registered worker."""
        if self._started:
            raise RuntimeError("background worker registry is already started")
        self._started = True
        for spec in self.specs():
            self._spawn(spec, delay_s=spec.initial_delay_s)

    async def stop(self) -> None:
        """Cancel every worker task and wait a bounded time for them.

        ``_started`` flips first so a task finishing during cancellation is
        not restarted by the done-callback. Sync workers in the executor
        finish their in-flight tick; the wait is bounded so shutdown is not
        held hostage by a hung network call.
        """
        self._started = False
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self._stop_timeout_s)
            for task in pending:
                _log.warning("background worker %s did not stop within %ss",
                             task.get_name(), self._stop_timeout_s)
        self._tasks.clear()
        for state in self._states.values():
            state.next_run_monotonic = None

    def status(self) -> dict[str, dict[str, object]]:
        """Return bounded scheduling metadata; never worker arguments/results."""
        values: dict[str, dict[str, object]] = {}
        for name in sorted(self._specs):
            state = self._states[name]
            task = self._tasks.get(name)
            values[name] = {
                "name": name,
                "running": bool(self._started and task is not None and not task.done()),
                "last_success_at": state.last_success_at,
                "last_failure_at": state.last_failure_at,
                "last_started_monotonic": state.last_started_monotonic,
                "next_run_monotonic": state.next_run_monotonic,
                "consecutive_failures": state.consecutive_failures,
                "restarts": state.restarts,
                "error": state.error[:_MAX_ERROR_CHARS],
            }
        return values

    # ---- supervision ----------------------------------------------------
    def _spawn(self, spec: BackgroundWorkerSpec, *, delay_s: float) -> None:
        task = asyncio.create_task(
            self._run(spec, initial_delay_s=delay_s), name=f"rynmesh-worker:{spec.name}",
        )
        self._tasks[spec.name] = task
        task.add_done_callback(lambda done, spec=spec: self._on_task_done(spec, done))

    def _on_task_done(self, spec: BackgroundWorkerSpec, task: asyncio.Task[None]) -> None:
        """A worker task ended. Cancellation is normal; anything else is a crash."""
        if task.cancelled() or not self._started or self._tasks.get(spec.name) is not task:
            return
        state = self._states[spec.name]
        exc = task.exception()
        state.consecutive_failures += 1
        state.restarts += 1
        state.last_failure_at = self._timestamp()
        state.error = (
            "worker crashed: " + self._describe(exc, spec) if exc is not None
            else "worker exited unexpectedly"
        )
        self._send_error(spec, state, state.error)
        delay = spec.policy.error_max_s
        _log.error("background worker %s died (%s); restarting in %.1fs",
                   spec.name, state.error, delay, exc_info=exc)
        self._spawn(spec, delay_s=delay)

    async def _run(self, spec: BackgroundWorkerSpec, *,
                   initial_delay_s: float | None = None) -> None:
        state = self._states[spec.name]
        invoke = self._invokers[spec.name]
        idle_delay = spec.policy.idle_initial_s
        error_delay = spec.policy.busy_delay_s
        first_delay = spec.initial_delay_s if initial_delay_s is None else initial_delay_s
        if first_delay:
            await self._wait(state, first_delay)
        while True:
            state.last_started_monotonic = self._monotonic()
            try:
                result = await invoke()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.consecutive_failures += 1
                state.last_failure_at = self._timestamp()
                state.error = self._describe(exc, spec)
                self._send_error(spec, state, state.error)
                error_delay = min(
                    max(error_delay, spec.policy.busy_delay_s)
                    * spec.policy.error_multiplier,
                    spec.policy.error_max_s,
                )
                await self._wait(state, error_delay)
                continue

            activity = self._activity(result)
            state.last_success_at = self._timestamp()
            state.consecutive_failures = 0
            state.error = ""
            self._send_error(spec, state, "")
            error_delay = spec.policy.busy_delay_s
            if activity:
                idle_delay = spec.policy.idle_initial_s
                delay = spec.policy.busy_delay_s
            else:
                idle_delay = min(
                    idle_delay * spec.policy.idle_multiplier,
                    spec.policy.idle_max_s,
                )
                delay = idle_delay
            await self._wait(state, delay)

    @staticmethod
    def _make_invoker(run_once: Callable[[], object]) -> Callable[[], Awaitable[object]]:
        """Classify the callable once at registration, not on every tick."""
        if inspect.iscoroutinefunction(run_once):
            return run_once  # type: ignore[return-value]

        async def invoke() -> object:
            result = await asyncio.to_thread(run_once)
            if inspect.isawaitable(result):
                return await result
            return result

        return invoke

    async def _wait(self, state: _WorkerState, delay_s: float) -> None:
        state.next_run_monotonic = self._monotonic() + delay_s
        try:
            await self._sleep(delay_s)
        finally:
            state.next_run_monotonic = None

    def _timestamp(self) -> str:
        return datetime.fromtimestamp(self._wall_time(), timezone.utc).isoformat()

    @staticmethod
    def _activity(result: object) -> bool:
        if isinstance(result, WorkerRunResult):
            return result.activity
        return bool(result)

    @staticmethod
    def _describe(exc: BaseException, spec: BackgroundWorkerSpec) -> str:
        kind = type(exc).__name__
        if spec.redact_errors:
            return f"{kind}: {_REDACTED_ERROR}"
        message = str(exc).strip() or _REDACTED_ERROR
        return f"{kind}: {message}"[:_MAX_ERROR_CHARS]

    @staticmethod
    def _send_error(spec: BackgroundWorkerSpec, state: _WorkerState, value: str) -> None:
        if spec.error_sink is None or state.sent_error == value:
            return
        state.sent_error = value
        try:
            spec.error_sink(value)
        except Exception:
            # Diagnostics must not be able to terminate the supervised worker.
            pass
