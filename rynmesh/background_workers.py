"""Supervised, in-process background workers for service packages.

Workers register during application construction. The node lifespan starts and
stops the registry as one unit; service packages do not own detached asyncio
tasks or need service-specific lifecycle wiring.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

_MAX_ERROR_CHARS = 512

_log = logging.getLogger(__name__)
# Six Agent workers can sit in long model calls; keep room for friends, Ask and discovery.
_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="ryn-bg")


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
        if self.error_max_s < self.busy_delay_s:
            raise ValueError("error_max_s must be at least busy_delay_s")

    @classmethod
    def fixed(cls, interval_s: float) -> "BackoffPolicy":
        """A flat retry/poll interval with no busy/idle distinction.

        For workers that poll a clock or a remote on a flat interval and have
        no meaningful busy/idle distinction: every delay is ``interval_s`` and
        both multipliers are ``1.0``.
        """
        return cls(
            busy_delay_s=interval_s,
            idle_initial_s=interval_s,
            idle_multiplier=1.0,
            idle_max_s=interval_s,
            error_multiplier=1.0,
            error_max_s=interval_s,
        )


@dataclass(frozen=True)
class BackgroundWorkerSpec:
    """One service-owned unit of repeatable background work."""

    name: str
    run_once: Callable[[], object]
    policy: BackoffPolicy
    initial_delay_s: float = 0.0
    error_sink: Callable[[str], None] | None = None
    run_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("worker name must not be empty")
        if len(self.name) > 128:
            raise ValueError("worker name is too long")
        if not callable(self.run_once):
            raise TypeError("run_once must be callable")
        if not math.isfinite(self.initial_delay_s) or self.initial_delay_s < 0:
            raise ValueError("initial_delay_s must be finite and non-negative")
        if self.run_timeout_s is not None and (not math.isfinite(self.run_timeout_s) or self.run_timeout_s <= 0):
            raise ValueError("run_timeout_s must be finite and positive")
        if self.error_sink is not None and not callable(self.error_sink):
            raise TypeError("error_sink must be callable")


@dataclass
class _WorkerState:
    in_run: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    invocation: asyncio.Future | None = field(default=None, repr=False)
    abandoned: bool = False
    last_warning_monotonic: float | None = None
    logged_error: str = ""
    last_success_at: str = ""
    last_failure_at: str = ""
    last_started_monotonic: float | None = None
    next_run_monotonic: float | None = None
    consecutive_failures: int = 0
    error: str = ""
    restarts: int = 0
    crash_class: str = ""
    # `None` is a sentinel that no real `error` value can ever equal (`error`
    # is always `str`), so the first `_send_error` call after a fresh state
    # (initial registration, or `register(..., replace=True)`) always fires
    # even when the first outcome happens to be a success (`error == ""`).
    # Defaulting this to `""` would make that first success a no-op compare
    # (`"" == ""`), silently skipping the sink and leaving a stale error from
    # the previous incarnation on `app.state`.
    sent_error: str | None = field(default=None, repr=False)


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
        if not math.isfinite(stop_timeout_s) or stop_timeout_s <= 0:
            raise ValueError("stop_timeout_s must be finite and positive")
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._stop_timeout_s = stop_timeout_s
        self._specs: dict[str, BackgroundWorkerSpec] = {}
        self._states: dict[str, _WorkerState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._restart_timers: dict[str, asyncio.Task[None]] = {}
        self._started = False

    def register(self, spec: BackgroundWorkerSpec, *, replace: bool = False) -> None:
        """Register a worker; replacements wait for the previous invocation to exit."""
        existing = self._specs.get(spec.name)
        if existing is not None and not replace:
            raise ValueError(f"background worker already registered: {spec.name}")
        if existing is not None:
            self._states[spec.name].cancelled.set()
            old_task = self._tasks.get(spec.name)
            if old_task is not None:
                old_task.cancel()
            old_timer = self._restart_timers.pop(spec.name, None)
            if old_timer is not None:
                old_timer.cancel()
        self._specs[spec.name] = spec
        self._states.setdefault(spec.name, _WorkerState())
        if self._started:
            self._start_or_wait(spec)

    def _start_or_wait(self, spec):
        state = self._states[spec.name]
        previous = self._tasks.get(spec.name)
        pending = {future for future in (previous, state.invocation)
                   if future is not None and not future.done()}
        if pending:
            self._restart_timers[spec.name] = asyncio.create_task(self._replace_when_stopped(spec, state, pending))
        else:
            self._states[spec.name] = _WorkerState()
            self._spawn(spec)

    async def _replace_when_stopped(self, spec, state, pending):
        try:
            _, pending = await asyncio.wait(pending, timeout=self._stop_timeout_s)
            if pending:
                state.abandoned = True
                _log.warning("background worker %s replacement waiting for abandoned invocation", spec.name)
                await asyncio.wait(pending)
            if self._started and self._specs.get(spec.name) is spec:
                self._states[spec.name] = _WorkerState()
                self._spawn(spec)
        finally:
            if self._restart_timers.get(spec.name) is asyncio.current_task():
                self._restart_timers.pop(spec.name, None)

    def specs(self) -> tuple[BackgroundWorkerSpec, ...]:
        """Return a deterministic, immutable view of registered workers."""
        return tuple(self._specs[name] for name in sorted(self._specs))

    async def start(self) -> None:
        """Create exactly one supervised task for every registered worker."""
        if self._started:
            raise RuntimeError("background worker registry is already started")
        self._started = True
        for spec in self.specs():
            self._start_or_wait(spec)

    async def stop(self) -> dict[str, list[str]]:
        """Cancel every worker and restart timer, bounded by `stop_timeout_s`."""
        # Set this first so a done callback firing during shutdown cannot
        # schedule a restart.
        self._started = False
        for timer in self._restart_timers.values():
            timer.cancel()
        # Cleared without awaiting the cancellation: a pending timer's crashed
        # task is always still sitting in `self._tasks` (nothing pops it until
        # `_spawn` replaces it), so `asyncio.wait` below still has that task to
        # wait on and gives the event loop the turn the cancellation needs.
        self._restart_timers.clear()
        for state in self._states.values():
            state.cancelled.set()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        waits = {task: name for name, task in self._tasks.items()}
        for name, state in self._states.items():
            if state.invocation is not None:
                waits[state.invocation] = name
        pending = set()
        if waits:
            _, pending = await asyncio.wait(waits, timeout=self._stop_timeout_s)
        abandoned = sorted({waits[future] for future in pending})
        stopped = sorted(set(waits.values()) - set(abandoned))
        for name in abandoned:
            self._states[name].abandoned = True
        if abandoned:
            _log.warning(
                "background workers did not stop within %.1fs: %s",
                self._stop_timeout_s,
                sorted(abandoned),
            )
        self._tasks = {name: task for name, task in self._tasks.items() if not task.done()}
        for state in self._states.values():
            state.next_run_monotonic = None
        return {"stopped": sorted(stopped), "abandoned": sorted(abandoned)}

    def status(self) -> dict[str, dict[str, object]]:
        """Return bounded scheduling metadata; never worker arguments/results.

        Best-effort diagnostics snapshot: safe to call from any thread (the
        `GET /api/local/node/status` route is a sync handler, so Starlette
        runs this in its threadpool while the event loop thread concurrently
        mutates worker state), but with no lock protecting the async paths.
        Rows are not guaranteed to be mutually consistent with each other,
        and a single row is not guaranteed to reflect one single instant —
        see the Background Workers section of docs/ARCHITECTURE.md for the
        full contract.
        """
        values: dict[str, dict[str, object]] = {}
        started = self._started
        for name in sorted(self._specs):
            state = self._states[name]
            task = self._tasks.get(name)
            # Capture every field into a local before assembling the row's
            # dict literal below, so construction reads `state`/`task` once
            # each rather than interleaving attribute reads with the literal
            # build (the GIL can switch to the mutating event-loop thread
            # between any two bytecode ops while this runs off-loop).
            running = bool(started and task is not None and not task.done())
            last_success_at = state.last_success_at
            last_failure_at = state.last_failure_at
            last_started_monotonic = state.last_started_monotonic
            next_run_monotonic = state.next_run_monotonic
            consecutive_failures = state.consecutive_failures
            error = state.error[:_MAX_ERROR_CHARS]
            restarts = state.restarts
            crash_class = state.crash_class
            in_run = state.in_run
            timeout = self._specs[name].run_timeout_s
            running_for_s = max(0.0, self._monotonic() - last_started_monotonic) if in_run and last_started_monotonic is not None else 0.0
            hung = bool(in_run and timeout is not None and running_for_s > timeout)
            abandoned = state.abandoned
            values[name] = {
                "name": name,
                "running": running,
                "in_run": in_run,
                "hung": hung,
                "running_for_s": running_for_s,
                "abandoned": abandoned,
                "last_success_at": last_success_at,
                "last_failure_at": last_failure_at,
                "last_started_monotonic": last_started_monotonic,
                "next_run_monotonic": next_run_monotonic,
                "consecutive_failures": consecutive_failures,
                "error": error,
                "restarts": restarts,
                "crash_class": crash_class,
            }
        return values

    def _spawn(
        self, spec: BackgroundWorkerSpec, *, initial_delay_s: float | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._run(spec, initial_delay_s=initial_delay_s),
            name=f"rynmesh-worker:{spec.name}",
        )
        self._tasks[spec.name] = task
        task.add_done_callback(functools.partial(self._on_task_done, spec))

    def _on_task_done(self, spec: BackgroundWorkerSpec, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._specs.get(spec.name) is not spec or self._states[spec.name].cancelled.is_set():
            # Either stop() or a register(replace=True) cancelled this task
            # deliberately; neither is a crash.
            return
        exc = task.exception()
        if exc is None:
            # _run() never returns normally; treat it as a crash so it is
            # never silently forgotten.
            exc = RuntimeError()
        state = self._states.get(spec.name)
        if state is None:
            return
        crash_class = type(exc).__name__
        state.crash_class = crash_class
        state.last_failure_at = self._timestamp()
        state.error = self._sanitize_crash(exc)
        self._send_error(spec.error_sink, state)
        _log.error("background worker %s crashed: %s", spec.name, crash_class)
        if not self._started:
            return
        timer = asyncio.create_task(self._restart_after_delay(spec))
        self._restart_timers[spec.name] = timer

    async def _restart_after_delay(self, spec: BackgroundWorkerSpec) -> None:
        try:
            await self._sleep(spec.policy.error_max_s)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # An injected `sleep` (or, in principle, a bug in this method's
            # own bookkeeping below) is otherwise the one unsupervised
            # failure mode left in this module: it would kill this timer
            # task silently and abandon the worker forever with no restart
            # and no log. Make it visible instead. Class name only — never
            # the exception's own message.
            _log.error(
                "background worker %s restart timer failed: %s",
                spec.name,
                type(exc).__name__,
            )
            return
        try:
            self._restart_timers.pop(spec.name, None)
            if not self._started:
                return
            if self._specs.get(spec.name) is not spec:
                # The spec was replaced or removed while the timer was pending.
                return
            state = self._states.get(spec.name)
            if state is not None:
                state.restarts += 1
            self._spawn(spec, initial_delay_s=0.0)
        except Exception as exc:
            _log.error(
                "background worker %s failed to restart: %s",
                spec.name,
                type(exc).__name__,
            )

    async def _run(
        self, spec: BackgroundWorkerSpec, *, initial_delay_s: float | None = None,
    ) -> None:
        state = self._states[spec.name]
        idle_delay = spec.policy.idle_initial_s
        error_delay = spec.policy.busy_delay_s
        delay_before_first_run = (
            spec.initial_delay_s if initial_delay_s is None else initial_delay_s
        )
        if delay_before_first_run:
            await self._wait(state, delay_before_first_run)
        while not state.cancelled.is_set():
            state.last_started_monotonic = self._monotonic()
            state.in_run = True
            try:
                try:
                    result = await self._invoke(spec, state)
                finally:
                    if state.invocation is None or state.invocation.done():
                        state.in_run = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.consecutive_failures += 1
                state.last_failure_at = self._timestamp()
                state.error = self._sanitize_error(exc)
                now = self._monotonic()
                if (state.consecutive_failures == 1 or state.logged_error != state.error
                        or state.last_warning_monotonic is None or now - state.last_warning_monotonic >= 300):
                    _log.warning("background worker %s failed: %s", spec.name, type(exc).__name__)
                    state.last_warning_monotonic, state.logged_error = now, state.error
                self._send_error(spec.error_sink, state)
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
            self._send_error(spec.error_sink, state)
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

    async def _invoke(self, spec, state) -> object:
        run_once = spec.run_once
        try:
            accepts_cancelled = "cancelled" in inspect.signature(run_once).parameters
        except (ValueError, TypeError):
            accepts_cancelled = False
        call = functools.partial(run_once, cancelled=state.cancelled) if accepts_cancelled else run_once
        is_async = inspect.iscoroutinefunction(run_once)
        if is_async:
            future = asyncio.ensure_future(call())
        else:
            def invoke_sync():
                if state.cancelled.is_set():
                    return WorkerRunResult()
                return call()
            future = asyncio.get_running_loop().run_in_executor(_executor, invoke_sync)
        state.invocation = future
        try:
            if spec.run_timeout_s is not None:
                done, _ = await asyncio.wait({future}, timeout=spec.run_timeout_s)
                if not done:
                    from .agent_runtime.diagnostic_log import failure
                    failure(f"background_worker_hung:{spec.name}", "agent_background_worker_hung",
                            elapsed_seconds=self._monotonic() - state.last_started_monotonic)
            result = await asyncio.shield(future)
            if inspect.isawaitable(result):
                is_async = True
                future = state.invocation = asyncio.ensure_future(result)
                result = await asyncio.shield(future)
            return result
        except asyncio.CancelledError:
            if is_async:
                future.cancel()
            raise
        finally:
            def completed(done):
                state.in_run = False
                if not done.cancelled():
                    done.exception()  # Retrieve failures if shutdown detached the awaiting task.
            if future.done():
                completed(future)
            else:
                future.add_done_callback(completed)

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
        if type(exc).__module__.startswith("rynmesh.") and re.fullmatch(r"[a-z0-9_]{1,64}", str(exc)):
            return f"{type(exc).__name__}: {exc}"
        value = f"{type(exc).__name__}: background worker invocation failed"
        return value[:_MAX_ERROR_CHARS]

    @staticmethod
    def _sanitize_crash(exc: BaseException) -> str:
        # Same rationale as `_sanitize_error`: class name only, never the
        # exception's own message.
        value = f"{type(exc).__name__}: background worker crashed"
        return value[:_MAX_ERROR_CHARS]

    @staticmethod
    def _send_error(sink: Callable[[str], None] | None, state: _WorkerState) -> None:
        if sink is None:
            return
        value = state.error
        if value == state.sent_error:
            return
        state.sent_error = value
        try:
            sink(value)
        except Exception:
            # Diagnostics must not be able to terminate the supervised worker.
            pass
