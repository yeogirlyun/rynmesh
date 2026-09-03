"""Supervised, in-process background workers for service packages.

Workers register during application construction. The node lifespan starts and
stops the registry as one unit; service packages do not own detached asyncio
tasks or need service-specific lifecycle wiring.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

_MAX_ERROR_CHARS = 512


@dataclass(frozen=True)
class WorkerRunResult:
    """Metadata-only result used to select the next scheduling delay."""

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


@dataclass(frozen=True)
class BackgroundWorkerSpec:
    """One service-owned unit of repeatable background work."""

    name: str
    run_once: Callable[[], object]
    policy: BackoffPolicy
    initial_delay_s: float = 0.0
    error_sink: Callable[[str], None] | None = None

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
    error: str = ""


class BackgroundWorkerRegistry:
    """Own, supervise, and shut down registered service workers."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._specs: dict[str, BackgroundWorkerSpec] = {}
        self._states: dict[str, _WorkerState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._sealed = False

    def register(self, spec: BackgroundWorkerSpec, *, replace: bool = False) -> None:
        """Register one worker before the registry is first started.

        A name is claimed exactly once unless ``replace`` is set, which lets an
        installer be re-run over the same app (re-installed routes, a rebuilt
        client) without the second pass colliding with its own first one.
        """
        if self._sealed:
            raise RuntimeError("background worker registration is closed after start")
        if spec.name in self._specs and not replace:
            raise ValueError(f"background worker already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._states[spec.name] = _WorkerState()

    def specs(self) -> tuple[BackgroundWorkerSpec, ...]:
        """Return a deterministic, immutable view of registered workers."""
        return tuple(self._specs[name] for name in sorted(self._specs))

    async def start(self) -> None:
        """Create exactly one supervised task for every registered worker."""
        if self._started:
            raise RuntimeError("background worker registry is already started")
        self._sealed = True
        self._started = True
        for spec in self.specs():
            self._tasks[spec.name] = asyncio.create_task(
                self._run(spec), name=f"rynmesh-worker:{spec.name}",
            )

    async def stop(self) -> None:
        """Cancel and await every worker task before application shutdown."""
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False
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
                "error": state.error[:_MAX_ERROR_CHARS],
            }
        return values

    async def _run(self, spec: BackgroundWorkerSpec) -> None:
        state = self._states[spec.name]
        idle_delay = spec.policy.idle_initial_s
        error_delay = spec.policy.busy_delay_s
        if spec.initial_delay_s:
            await self._wait(state, spec.initial_delay_s)
        while True:
            state.last_started_monotonic = self._monotonic()
            try:
                result = await self._invoke(spec.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.consecutive_failures += 1
                state.last_failure_at = self._timestamp()
                state.error = self._sanitize_error(exc)
                self._send_error(spec.error_sink, state.error)
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
            self._send_error(spec.error_sink, "")
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

    async def _invoke(self, run_once: Callable[[], object]) -> object:
        if inspect.iscoroutinefunction(run_once):
            return await run_once()
        result = await asyncio.to_thread(run_once)
        if inspect.isawaitable(result):
            return await result
        return result

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
        if isinstance(result, bool):
            return result
        return False

    @staticmethod
    def _sanitize_error(exc: Exception) -> str:
        # Exception messages from service code can accidentally contain a
        # prompt, model output, key, URL, or private path. Retain the failure
        # class for diagnosis without copying arbitrary service data.
        value = f"{type(exc).__name__}: background worker invocation failed"
        return value[:_MAX_ERROR_CHARS]

    @staticmethod
    def _send_error(sink: Callable[[str], None] | None, value: str) -> None:
        if sink is None:
            return
        try:
            sink(value)
        except Exception:
            # Diagnostics must not be able to terminate the supervised worker.
            pass
