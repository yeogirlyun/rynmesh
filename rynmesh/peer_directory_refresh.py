"""Keep device-list reads off the network.

The desktop polls the device list and the device directory in the background.
Those requests used to ask the public registry (10 s per call when it cannot be
reached) and probe every device before answering, so one request could take
40 s while the next one was already on its way. Here the requests read caches,
and a refresher updates those caches on its own small thread pool.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Iterator

_log = logging.getLogger(__name__)

REFRESH_MIN_INTERVAL_SECONDS = 15.0
COLD_START_WAIT_SECONDS = 8.0
SLOW_PHASE_LOG_SECONDS = 1.0


class PhaseTimer:
    """Phase durations of one request or refresh; logged only when slow.

    The log carries phase names and seconds only, never peer ids, addresses or
    names.
    """

    def __init__(self, stage: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.stage = stage
        self._clock = clock
        self._started = clock()
        self.phases: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = self._clock()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + (self._clock() - started)

    def finish(self) -> float:
        total = self._clock() - self._started
        if total >= SLOW_PHASE_LOG_SECONDS:
            _log.warning("peer_directory_slow stage=%s total_seconds=%.3f phases=%s", self.stage, total,
                         {name: round(seconds, 3) for name, seconds in self.phases.items()})
        return total


class _NetworkState:
    __slots__ = ("future", "last_started", "completed")

    def __init__(self) -> None:
        self.future: Future[None] | None = None
        self.last_started = float("-inf")
        self.completed = False


class DirectoryRefresher:
    """Refreshes registry-backed device data in the background, per network.

    - Its own two threads (never the event loop's default pool, which the Agent
      and every background worker share).
    - At most one refresh in flight per network, started at least
      ``min_interval`` seconds apart.
    - Callers never wait, except the very first full read after startup, which
      waits up to ``cold_wait`` seconds so the first list is not empty.
    """

    def __init__(
        self,
        refresh: Callable[[str], None],
        *,
        min_interval: float = REFRESH_MIN_INTERVAL_SECONDS,
        cold_wait: float = COLD_START_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._refresh = refresh
        self._min_interval = min_interval
        self._cold_wait = cold_wait
        self._clock = clock
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._networks: dict[str, _NetworkState] = {}

    def _run(self, network_id: str) -> None:
        try:
            self._refresh(network_id)
        finally:
            with self._lock:
                self._networks[network_id].completed = True

    def _start_locked(self, network_id: str, state: _NetworkState) -> Future[None] | None:
        if state.future is not None and not state.future.done():
            return state.future
        if self._clock() - state.last_started < self._min_interval:
            return None
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ryn-directory-refresh")
        state.last_started = self._clock()
        state.future = self._executor.submit(self._run, network_id)
        state.future.add_done_callback(_log_refresh_failure)
        return state.future

    def request(self, network_id: str) -> None:
        """Note a full read: refresh in the background when due, never waiting.

        On the first full read for a network, wait up to ``cold_wait`` seconds
        for the first refresh to finish.
        """
        with self._lock:
            state = self._networks.setdefault(network_id, _NetworkState())
            future = self._start_locked(network_id, state)
            cold = not state.completed
        if cold and future is not None:
            try:
                future.result(timeout=self._cold_wait)
            except Exception:  # noqa: BLE001 - a slow or failed refresh still answers from cache
                pass

    def close(self) -> None:
        """Stop accepting work; a running refresh finishes on its own."""
        with self._lock:
            executor, self._executor = self._executor, None
            for state in self._networks.values():
                state.future = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


def _log_refresh_failure(future: Future[None]) -> None:
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        # Class name only: registry and peer errors can carry addresses.
        _log.warning("peer_directory_refresh_failed error=%s", type(error).__name__)


class SingleFlight:
    """Concurrent callers of the same key share one call and its result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, Future[Any]] = {}

    def run(self, key: str, call: Callable[[], Any]) -> Any:
        with self._lock:
            pending = self._calls.get(key)
            owner = pending is None
            if owner:
                pending = Future()
                self._calls[key] = pending
        if not owner:
            return pending.result()
        try:
            result = call()
        except BaseException as error:
            pending.set_exception(error)
            raise
        else:
            pending.set_result(result)
            return result
        finally:
            with self._lock:
                self._calls.pop(key, None)
