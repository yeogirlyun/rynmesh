"""Shared node-scoped NativeBridge coordinator for Ryn computer reads and two-phase writes."""

from __future__ import annotations

import copy
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..store import AgentError, AgentToolRejected
from .binary import (
    BridgeConfigIdentity,
    build_bridge_config_identity,
    resolve_owner_id,
)
from .client import (
    DEFAULT_CONTROL_TIMEOUT_S,
    DEFAULT_HANDSHAKE_TIMEOUT_S,
    DEFAULT_REQUEST_TIMEOUT_S,
    NativeWorkerClient,
)
from .protocol import (
    ALLOWLISTED_BRIDGE_ERROR_CODES,
    EXPECTED_WORKER_NAME,
    EXPECTED_WORKER_VERSION,
    MAX_ID_CHARS,
    PROTOCOL_SEMVER,
    NativeBridgeError,
    NativeBridgeRejected,
    compute_action_fingerprint,
    normalize_action_spec,
    validate_action_commit_result,
    validate_apps_list_result,
    validate_observation,
    validate_prepare_constraints,
    validate_window_ref,
    validate_windows_get_result,
    validate_windows_list_result,
    verify_ready_integrity,
    window_refs_equal,
)

MAX_TRACKED_TASKS = 128
MAX_TRACKED_WINDOWS_PER_TASK = 256
MAX_TRACKED_TOKENS_PER_TASK = 256
MAX_TRACKED_OPERATIONS = 512


@dataclass(frozen=True)
class _OperationRecord:
    task_session_id: str
    operation_id: str
    action_fingerprint: str
    window: dict[str, Any]
    expected_state_token: str
    status: str  # "in_progress" | "commit_attempted" | "confirmed" | "pre_dispatch_rejected"
    commit_attempted: bool = False
    dispatched: bool = False
    callback_uncertain: bool = False
    result: dict[str, Any] | None = None
    error: NativeBridgeError | None = None


class _TaskNamespaceState:
    def __init__(self, task_session_id: str, worker_instance: str, generation: int) -> None:
        self.task_session_id = task_session_id
        self.worker_instance = worker_instance
        self.generation = generation
        self.windows_by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.windows_by_hwnd: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.tokens: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def record_window(self, wref: dict[str, Any]) -> None:
        wcopy = copy.deepcopy(wref)
        wkey = wcopy["windowKey"]
        hwnd = wcopy["hwnd"]
        self.windows_by_key.pop(wkey, None)
        self.windows_by_key[wkey] = wcopy
        while len(self.windows_by_key) > MAX_TRACKED_WINDOWS_PER_TASK:
            self.windows_by_key.popitem(last=False)

        self.windows_by_hwnd.pop(hwnd, None)
        self.windows_by_hwnd[hwnd] = wcopy
        while len(self.windows_by_hwnd) > MAX_TRACKED_WINDOWS_PER_TASK:
            self.windows_by_hwnd.popitem(last=False)

    def record_observation_meta(self, obs: dict[str, Any]) -> None:
        """Record lightweight token/window metadata without storing base64 screenshots."""
        self.record_window(obs["window"])
        st = obs["stateToken"]
        meta = {
            "stateToken": st,
            "observationId": obs["observationId"],
            "taskSessionId": obs["taskSessionId"],
            "window": copy.deepcopy(obs["window"]),
            "capturedAt": obs["capturedAt"],
            "geometry": copy.deepcopy(obs["geometry"]),
        }
        self.tokens.pop(st, None)
        self.tokens[st] = meta
        while len(self.tokens) > MAX_TRACKED_TOKENS_PER_TASK:
            self.tokens.popitem(last=False)


def _strip_observation_screenshots_for_cache(obs: dict[str, Any]) -> dict[str, Any]:
    """Strip `dataBase64` from cached commit records so base64 is never durably retained in memory."""
    cleaned = copy.deepcopy(obs)
    shots = cleaned.get("screenshots")
    if isinstance(shots, list):
        for s in shots:
            if isinstance(s, dict):
                s.pop("dataBase64", None)
    return cleaned


class NativeBridge:
    """Shared per-node coordinator for Ryn native computer control.

    - Owns a single `NativeWorkerClient` per node/owner rather than spawning per call or sharing
      an unconditional global singleton across unrelated `AgentStore`s.
    - Enforces task-scoped `WindowRef` and `Observation` validation without `windows[0]` assumptions.
    - Implements the strict `action.prepare` -> `action.ready` -> `on_ready` -> `action.commit`
      write state machine with deterministic cancellation and no post-dispatch replay.
    """

    def __init__(
        self,
        *,
        owner: Any = None,
        owner_id: str | None = None,
        bundled_executable: str | Path | None = None,
        dev_executable: str | Path | list[str] | tuple[str, ...] | None = None,
        transport_factory: Callable[[], Any] | None = None,
        artifact_resolver: Any = None,
        manifest_path: str | Path | None = None,
        bundle_root: str | Path | None = None,
        process_identity_provider: Callable[[Any], Any] | None = None,
        expected_worker_name: str = EXPECTED_WORKER_NAME,
        expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
        expected_protocol_version: str = PROTOCOL_SEMVER,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
    ) -> None:
        try:
            self._owner_ref = weakref.ref(owner) if owner is not None else None
        except TypeError:
            self._owner_ref = None
        self._owner_id = (
            owner_id
            if isinstance(owner_id, str) and owner_id
            else resolve_owner_id(owner)
        )
        self._bound_owner_token: int | None = id(owner) if owner is not None else None
        self._lock = threading.RLock()
        self._write_mutex = threading.Lock()
        self._ever_used = False
        self._tasks: OrderedDict[str, _TaskNamespaceState] = OrderedDict()
        self._operations: OrderedDict[str, _OperationRecord] = OrderedDict()

        config_identity = build_bridge_config_identity(
            bundled_executable=bundled_executable,
            dev_executable=dev_executable,
            transport_factory=transport_factory,
            artifact_resolver=artifact_resolver,
            manifest_path=manifest_path,
            bundle_root=bundle_root,
            process_identity_provider=process_identity_provider,
            expected_worker_name=expected_worker_name,
            expected_worker_version=expected_worker_version,
            expected_protocol_version=expected_protocol_version,
        )
        self._client = NativeWorkerClient(
            owner_id=self._owner_id,
            config_identity=config_identity,
            bundled_executable=bundled_executable,
            dev_executable=dev_executable,
            transport_factory=transport_factory,
            artifact_resolver=artifact_resolver,
            manifest_path=manifest_path,
            bundle_root=bundle_root,
            process_identity_provider=process_identity_provider,
            expected_worker_name=expected_worker_name,
            expected_worker_version=expected_worker_version,
            expected_protocol_version=expected_protocol_version,
            handshake_timeout=handshake_timeout,
            on_generation_broken=self._on_worker_generation_broken,
        )

    @property
    def owner_id(self) -> str:
        with self._lock:
            return self._owner_id

    @property
    def config_identity(self) -> BridgeConfigIdentity:
        return self._client.config_identity

    @property
    def client(self) -> NativeWorkerClient:
        return self._client

    def _is_pristine_locked(self) -> bool:
        """Return True iff this bridge and its client have never been used, connected, reconfigured, or closed.

        Caller must hold `self._lock` and `self._client._lock`.
        """
        return (
            not self._ever_used
            and len(self._tasks) == 0
            and len(self._operations) == 0
            and self._client.is_pristine_locked()
        )

    def cache_status(self) -> dict[str, Any]:
        """Read-only inspection of cached handshake metadata.

        Never spawns a worker, initiates a handshake, probes the filesystem, or infers readiness.
        """
        return self._client.cache_status()

    def _on_worker_generation_broken(
        self, generation: int, worker_instance: str | None
    ) -> None:
        """Retire all task tokens and window references bound to a broken worker generation."""
        with self._lock:
            stale_tasks = [
                tid
                for tid, tstate in self._tasks.items()
                if tstate.generation == generation
                or (
                    worker_instance is not None
                    and tstate.worker_instance == worker_instance
                )
            ]
            for tid in stale_tasks:
                self._tasks.pop(tid, None)

    def _validate_task_session_id(
        self, task_session_id: Any, *, phase: str = "pre_dispatch"
    ) -> str:
        if (
            not isinstance(task_session_id, str)
            or not task_session_id.strip()
            or len(task_session_id) > MAX_ID_CHARS
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in task_session_id)
        ):
            raise NativeBridgeRejected(
                "agent_computer_invalid_params",
                phase=phase,
                reason="malformed_json",
            )
        return task_session_id

    def _validate_operation_id(
        self, operation_id: Any, *, phase: str = "pre_dispatch"
    ) -> str:
        if (
            not isinstance(operation_id, str)
            or not operation_id.strip()
            or len(operation_id) > MAX_ID_CHARS
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in operation_id)
        ):
            raise NativeBridgeRejected(
                "agent_operation_id_required",
                phase=phase,
                reason="malformed_json",
            )
        return operation_id

    def _get_or_create_task_state(
        self,
        task_session_id: str,
        *,
        worker_instance: str,
        generation: int,
    ) -> _TaskNamespaceState:
        with self._lock:
            existing = self._tasks.get(task_session_id)
            if (
                existing is not None
                and existing.worker_instance == worker_instance
                and existing.generation == generation
            ):
                self._tasks.move_to_end(task_session_id)
                return existing
            state = _TaskNamespaceState(task_session_id, worker_instance, generation)
            self._tasks[task_session_id] = state
            while len(self._tasks) > MAX_TRACKED_TASKS:
                self._tasks.popitem(last=False)
            return state

    def _record_operation(self, rec: _OperationRecord) -> None:
        with self._lock:
            self._operations.pop(rec.operation_id, None)
            self._operations[rec.operation_id] = rec
            while len(self._operations) > MAX_TRACKED_OPERATIONS:
                # Prefer evicting non-commit-attempted records first if full
                for k, v in list(self._operations.items()):
                    if not v.commit_attempted:
                        self._operations.pop(k, None)
                        break
                else:
                    self._operations.popitem(last=False)

    # =========================================================================
    # Read Operations
    # =========================================================================

    def list_windows(
        self,
        *,
        task_session_id: str | None = None,
        include_minimized: bool | None = None,
        include_invisible: bool | None = None,
        include_cloaked: bool | None = None,
        app_query: str | None = None,
        title_query: str | None = None,
        pid: int | None = None,
        max_windows: int | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """List desktop windows via `windows.list` without assuming `windows[0]` is a target."""
        with self._lock:
            self._ever_used = True
        valid_task: str | None = None
        if task_session_id is not None:
            valid_task = self._validate_task_session_id(
                task_session_id, phase="pre_dispatch"
            )
        params: dict[str, Any] = {}
        if valid_task is not None:
            params["taskSessionId"] = valid_task
        if include_minimized is not None:
            params["includeMinimized"] = bool(include_minimized)
        if include_invisible is not None:
            params["includeInvisible"] = bool(include_invisible)
        if include_cloaked is not None:
            params["includeCloaked"] = bool(include_cloaked)
        if app_query is not None:
            params["appQuery"] = str(app_query)
        if title_query is not None:
            params["titleQuery"] = str(title_query)
        if pid is not None:
            if type(pid) is not int or pid <= 0:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="windows.list",
                )
            params["pid"] = pid
        if max_windows is not None:
            if type(max_windows) is not int or max_windows <= 0:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="windows.list",
                )
            params["maxWindows"] = max_windows

        for attempt in range(2):
            session = self._client.ensure_session()
            assert session.worker_instance is not None
            try:
                res, _ = session.request(
                    "windows.list",
                    params,
                    timeout=timeout,
                    task_session_id=valid_task,
                    phase="read",
                    pre_dispatch_allowed=True,
                    pre_dispatch_on_send_error=True,
                    cancelled=cancelled,
                )
                break
            except NativeBridgeError as exc:
                # A lost lifecycle hook is permanently unhealthy in this worker.
                # Only this read can be retried, once, under a fresh generation.
                if (attempt or exc.native_code != "observation_unavailable"
                        or exc.reason != "generation_mismatch"
                        or exc.native_stage != "observe"
                        or exc.commit_attempted or exc.dispatched
                        or exc.callback_uncertain or (cancelled and cancelled())):
                    raise
                # Never tear down a worker while another task is preparing or
                # committing input. Leave all operation receipts intact.
                if not self._write_mutex.acquire(blocking=False):
                    raise
                try:
                    self._client.retire_generation(session.generation)
                finally:
                    self._write_mutex.release()
        validated = validate_windows_list_result(
            res,
            expected_worker_instance=session.worker_instance,
            phase="read",
        )
        if valid_task is not None:
            tstate = self._get_or_create_task_state(
                valid_task,
                worker_instance=session.worker_instance,
                generation=session.generation,
            )
            with self._lock:
                for entry in validated["windows"]:
                    tstate.record_window(entry["window"])
        return validated

    def get_window(
        self,
        *,
        task_session_id: str | None = None,
        window: dict[str, Any] | None = None,
        hwnd: str | int | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Query a specific window via `windows.get` by `WindowRef` or decimal `hwnd`."""
        with self._lock:
            self._ever_used = True
        valid_task: str | None = None
        if task_session_id is not None:
            valid_task = self._validate_task_session_id(
                task_session_id, phase="pre_dispatch"
            )
        if window is None and hwnd is None:
            raise NativeBridgeRejected(
                "agent_computer_invalid_params",
                phase="pre_dispatch",
                method="windows.get",
            )

        session = self._client.ensure_session()
        assert session.worker_instance is not None

        params: dict[str, Any] = {}
        if valid_task is not None:
            params["taskSessionId"] = valid_task

        expected_wref: dict[str, Any] | None = None
        if window is not None:
            try:
                expected_wref = validate_window_ref(
                    window,
                    expected_worker_instance=session.worker_instance,
                    phase="pre_dispatch",
                )
            except NativeBridgeError as exc:
                raise NativeBridgeRejected(
                    exc.code,
                    phase="pre_dispatch",
                    method="windows.get",
                    reason=exc.reason,
                    task_session_id=valid_task,
                    worker_instance=session.worker_instance,
                    worker_generation=session.generation,
                    worker_pid=session.worker_pid,
                ) from None
            params["window"] = expected_wref

        if hwnd is not None:
            hwnd_str = str(hwnd) if type(hwnd) is int and hwnd > 0 else hwnd
            if not isinstance(hwnd_str, str) or not hwnd_str.isdigit() or int(hwnd_str) <= 0:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="windows.get",
                    reason="hwnd_parse_failed",
                )
            params["hwnd"] = hwnd_str

        res, _ = session.request(
            "windows.get",
            params,
            timeout=timeout,
            task_session_id=valid_task,
            phase="read",
            pre_dispatch_allowed=True,
            pre_dispatch_on_send_error=True,
            cancelled=cancelled,
        )
        validated = validate_windows_get_result(
            res,
            expected_worker_instance=session.worker_instance,
            expected_window=expected_wref,
            phase="read",
        )
        if valid_task is not None:
            tstate = self._get_or_create_task_state(
                valid_task,
                worker_instance=session.worker_instance,
                generation=session.generation,
            )
            with self._lock:
                tstate.record_window(validated["entry"]["window"])
        return validated

    def list_apps(
        self,
        *,
        query: str | None = None,
        max_apps: int | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """List discoverable applications via `apps.list`."""
        with self._lock:
            self._ever_used = True
        params: dict[str, Any] = {}
        if query is not None:
            if not isinstance(query, str) or len(query) > 256:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="apps.list",
                )
            params["query"] = query
        if max_apps is not None:
            if type(max_apps) is not int or max_apps <= 0:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="apps.list",
                )
            params["maxApps"] = max_apps

        session = self._client.ensure_session()
        res, _ = session.request(
            "apps.list",
            params,
            timeout=timeout,
            phase="read",
            pre_dispatch_allowed=True,
            pre_dispatch_on_send_error=True,
            cancelled=cancelled,
        )
        return validate_apps_list_result(res, phase="read")

    def get_state(
        self,
        *,
        task_session_id: str,
        window: dict[str, Any],
        include_screenshot: bool | None = None,
        include_text: bool | None = None,
        screenshot: dict[str, Any] | None = None,
        accessibility: dict[str, Any] | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Observe a target window (`state.get`) and return the validated `Observation` to the caller.

        Screenshots are validated (size, SHA-256, PNG dimensions) and returned directly to the caller
        for artifact routing; `NativeBridge` never stores `dataBase64` in memory or on disk.
        """
        with self._lock:
            self._ever_used = True
        valid_task = self._validate_task_session_id(
            task_session_id, phase="pre_dispatch"
        )
        session = self._client.ensure_session()
        assert session.worker_instance is not None

        try:
            valid_wref = validate_window_ref(
                window,
                expected_worker_instance=session.worker_instance,
                phase="pre_dispatch",
            )
        except NativeBridgeError as exc:
            raise NativeBridgeRejected(
                exc.code,
                phase="pre_dispatch",
                method="state.get",
                reason=exc.reason,
                task_session_id=valid_task,
                worker_instance=session.worker_instance,
                worker_generation=session.generation,
                worker_pid=session.worker_pid,
            ) from None

        params: dict[str, Any] = {
            "taskSessionId": valid_task,
            "window": valid_wref,
        }
        if include_screenshot is not None:
            params["includeScreenshot"] = bool(include_screenshot)
        if include_text is not None:
            params["includeText"] = bool(include_text)
        if screenshot is not None:
            if not isinstance(screenshot, dict):
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="state.get",
                )
            params["screenshot"] = copy.deepcopy(screenshot)
        if accessibility is not None:
            if not isinstance(accessibility, dict):
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase="pre_dispatch",
                    method="state.get",
                )
            params["accessibility"] = copy.deepcopy(accessibility)

        res, _ = session.request(
            "state.get",
            params,
            timeout=timeout,
            task_session_id=valid_task,
            phase="read",
            pre_dispatch_allowed=True,
            pre_dispatch_on_send_error=True,
            cancelled=cancelled,
        )
        validated = validate_observation(
            res,
            expected_worker_instance=session.worker_instance,
            expected_task_session_id=valid_task,
            expected_window=valid_wref,
            phase="read",
        )
        tstate = self._get_or_create_task_state(
            valid_task,
            worker_instance=session.worker_instance,
            generation=session.generation,
        )
        with self._lock:
            tstate.record_observation_meta(validated)
        return validated

    def resolve_task_window(
        self,
        task_session_id: str,
        *,
        window_key: str | None = None,
        hwnd: str | int | None = None,
    ) -> dict[str, Any] | None:
        """Look up a previously observed `WindowRef` within the task's namespace without assuming `windows[0]`."""
        with self._lock:
            self._ever_used = True
        valid_task = self._validate_task_session_id(
            task_session_id, phase="pre_dispatch"
        )
        gen, worker_instance, _ = self._client.active_session_info()
        if gen is None or worker_instance is None:
            return None
        with self._lock:
            tstate = self._tasks.get(valid_task)
            if (
                tstate is None
                or tstate.generation != gen
                or tstate.worker_instance != worker_instance
            ):
                return None
            if window_key is not None:
                found = tstate.windows_by_key.get(window_key)
                return copy.deepcopy(found) if found is not None else None
            if hwnd is not None:
                hwnd_str = str(hwnd)
                found = tstate.windows_by_hwnd.get(hwnd_str)
                return copy.deepcopy(found) if found is not None else None
            return None

    # =========================================================================
    # Write Operations (Prepare -> Ready -> on_ready -> Commit)
    # =========================================================================

    def execute_action(
        self,
        *,
        task_session_id: str,
        operation_id: str,
        window: dict[str, Any],
        expected_state_token: str,
        action: dict[str, Any],
        on_ready: Callable[[dict[str, Any]], Any],
        timeout_ms: int | None = None,
        authority_epoch: int | None = None,
        constraints: dict[str, Any] | None = None,
        post_observation: dict[str, Any] | None = None,
        commit_deadline_ms: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Execute a two-phase physical desktop action (`action.prepare` -> `on_ready` -> `action.commit`).

        Invariants:
        1. Freezes `task_session_id`, `operation_id`, `window`, `expected_state_token`, `action`, and
           `constraints` before sending `action.prepare`.
        2. Rejects duplicate concurrent calls for the same `operation_id`, and never replays an
           operation once `action.commit` has been attempted (even across worker generations).
        3. Validates `action.prepare` response and `action.ready` event against the locally recomputed
           Rust `ActionSpec` SHA-256 fingerprint before calling `on_ready`.
        4. Calls `on_ready` synchronously only while the native input lock is held and zero mutation
           has occurred.
        5. If `on_ready` refuses (`False` or `AgentToolRejected`), cancels the prepared ticket and raises
           a `NativeBridgeRejected` (`pre_dispatch=True`) without issuing `action.commit`.
        6. If `on_ready` raises an unexpected exception (e.g., host ledger failure), cancels the ticket
           and raises `NativeBridgeError` with `callback_uncertain=True` and `pre_dispatch=False` so the
           caller never blindly treats it as a safe retry.
        7. Issues `action.commit` at most once using only fixed `(taskSessionId, operationId, prepareTicket)`.
        8. Any error, timeout, disconnect, or cancellation once `action.commit` write begins is classified
           as `commit_attempted=True`, `dispatched=True`, `pre_dispatch=False` (`outcome='unknown'`),
           even if native reports `mutation='none'`.
        """
        with self._lock:
            self._ever_used = True
        valid_task = self._validate_task_session_id(
            task_session_id, phase="pre_dispatch"
        )
        valid_op = self._validate_operation_id(operation_id, phase="pre_dispatch")
        if not callable(on_ready):
            raise NativeBridgeRejected(
                "agent_computer_invalid_params",
                phase="pre_dispatch",
                method="action.prepare",
                operation_id=valid_op,
                task_session_id=valid_task,
            )
        if (
            not isinstance(expected_state_token, str)
            or not expected_state_token.strip()
            or len(expected_state_token) > MAX_ID_CHARS
        ):
            raise NativeBridgeRejected(
                "agent_computer_state_token_invalid",
                phase="pre_dispatch",
                method="action.prepare",
                operation_id=valid_op,
                task_session_id=valid_task,
                reason="token_window_mismatch",
            )
        frozen_state_token = expected_state_token.strip()

        try:
            frozen_window = validate_window_ref(
                 copy.deepcopy(window), phase="pre_dispatch"
            )
        except NativeBridgeError as exc:
            raise NativeBridgeRejected(
                exc.code,
                phase="pre_dispatch",
                method="action.prepare",
                operation_id=valid_op,
                task_session_id=valid_task,
                reason=exc.reason,
            ) from None

        frozen_action = normalize_action_spec(
            copy.deepcopy(action), phase="pre_dispatch"
        )
        frozen_constraints = validate_prepare_constraints(
            copy.deepcopy(constraints), phase="pre_dispatch"
        )
        if timeout_ms is not None and (
            type(timeout_ms) is not int or not (1 <= timeout_ms <= 120_000)
        ):
            raise NativeBridgeRejected(
                "agent_computer_invalid_params",
                phase="pre_dispatch",
                method="action.prepare",
                operation_id=valid_op,
                task_session_id=valid_task,
            )
        if authority_epoch is not None and (
            type(authority_epoch) is not int or authority_epoch < 0
        ):
            raise NativeBridgeRejected(
                "agent_computer_invalid_params",
                phase="pre_dispatch",
                method="action.prepare",
                operation_id=valid_op,
                task_session_id=valid_task,
            )

        expected_fingerprint = compute_action_fingerprint(
            valid_task,
            frozen_window,
            frozen_state_token,
            frozen_action,
        )

        # Check duplicate / replay table before touching the worker
        with self._lock:
            existing = self._operations.get(valid_op)
            if existing is not None:
                if (
                    existing.task_session_id != valid_task
                    or existing.action_fingerprint != expected_fingerprint
                    or not window_refs_equal(existing.window, frozen_window)
                    or existing.expected_state_token != frozen_state_token
                ):
                    raise NativeBridgeRejected(
                        "agent_operation_id_conflict",
                        phase="pre_dispatch",
                        method="action.prepare",
                        operation_id=valid_op,
                        task_session_id=valid_task,
                    )
                if existing.status == "in_progress":
                    raise NativeBridgeRejected(
                        "agent_computer_operation_in_progress",
                        phase="pre_dispatch",
                        method="action.prepare",
                        operation_id=valid_op,
                        task_session_id=valid_task,
                    )
                if existing.commit_attempted or existing.dispatched or existing.callback_uncertain:
                    # Never replay an operation once commit was attempted or callback was uncertain,
                    # even if the original call succeeded or failed.
                    raise NativeBridgeError(
                        "agent_computer_commit_already_attempted",
                        phase="post_dispatch",
                        method="action.commit",
                        commit_attempted=existing.commit_attempted,
                        dispatched=existing.dispatched,
                        pre_dispatch=False,
                        callback_uncertain=existing.callback_uncertain,
                        operation_id=valid_op,
                        task_session_id=valid_task,
                    )

            self._record_operation(
                _OperationRecord(
                    task_session_id=valid_task,
                    operation_id=valid_op,
                    action_fingerprint=expected_fingerprint,
                    window=copy.deepcopy(frozen_window),
                    expected_state_token=frozen_state_token,
                    status="in_progress",
                )
            )

        try:
            with self._write_mutex:
                return self._execute_action_locked(
                    task_session_id=valid_task,
                    operation_id=valid_op,
                    frozen_window=frozen_window,
                    frozen_state_token=frozen_state_token,
                    frozen_action=frozen_action,
                    frozen_constraints=frozen_constraints,
                    expected_fingerprint=expected_fingerprint,
                    on_ready=on_ready,
                    timeout_ms=timeout_ms,
                    authority_epoch=authority_epoch,
                    post_observation=copy.deepcopy(post_observation),
                    commit_deadline_ms=commit_deadline_ms,
                    cancelled=cancelled,
                )
        except BaseException as exc:
            with self._lock:
                cur = self._operations.get(valid_op)
                if cur is not None and cur.status == "in_progress":
                    if isinstance(exc, NativeBridgeRejected) or (
                        isinstance(exc, NativeBridgeError)
                        and exc.pre_dispatch
                        and not exc.commit_attempted
                        and not exc.dispatched
                        and not exc.callback_uncertain
                    ):
                        # Pre-dispatch failure before commit: clear in_progress record
                        self._operations.pop(valid_op, None)
                    else:
                        err_obj = (
                            exc
                            if isinstance(exc, NativeBridgeError)
                            else NativeBridgeError(
                                "agent_computer_native_error",
                                phase="post_dispatch",
                                commit_attempted=cur.commit_attempted,
                                dispatched=cur.dispatched,
                                pre_dispatch=False,
                                operation_id=valid_op,
                                task_session_id=valid_task,
                            )
                        )
                        self._record_operation(
                            _OperationRecord(
                                task_session_id=valid_task,
                                operation_id=valid_op,
                                action_fingerprint=expected_fingerprint,
                                window=frozen_window,
                                expected_state_token=frozen_state_token,
                                status="commit_attempted",
                                commit_attempted=err_obj.commit_attempted,
                                dispatched=err_obj.dispatched,
                                callback_uncertain=err_obj.callback_uncertain,
                                error=err_obj,
                            )
                        )
            raise

    def _execute_action_locked(
        self,
        *,
        task_session_id: str,
        operation_id: str,
        frozen_window: dict[str, Any],
        frozen_state_token: str,
        frozen_action: dict[str, Any],
        frozen_constraints: dict[str, Any] | None,
        expected_fingerprint: str,
        on_ready: Callable[[dict[str, Any]], Any],
        timeout_ms: int | None,
        authority_epoch: int | None,
        post_observation: dict[str, Any] | None,
        commit_deadline_ms: int | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        session = self._client.ensure_session()
        assert session.worker_instance is not None
        gen = session.generation
        worker_instance = session.worker_instance
        worker_pid = session.worker_pid

        if frozen_window["workerInstance"] != worker_instance:
            raise NativeBridgeRejected(
                "agent_computer_window_stale",
                phase="pre_dispatch",
                method="action.prepare",
                operation_id=operation_id,
                task_session_id=task_session_id,
                reason="worker_instance_mismatch",
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            )

        with self._lock:
            tstate = self._tasks.get(task_session_id)
            if tstate is not None:
                if (
                    tstate.generation != gen
                    or tstate.worker_instance != worker_instance
                ):
                    raise NativeBridgeRejected(
                        "agent_computer_state_token_stale",
                        phase="pre_dispatch",
                        method="action.prepare",
                        operation_id=operation_id,
                        task_session_id=task_session_id,
                        reason="worker_instance_mismatch",
                        worker_instance=worker_instance,
                        worker_generation=gen,
                        worker_pid=worker_pid,
                    )
                tok_meta = tstate.tokens.get(frozen_state_token)
                if tok_meta is not None and not window_refs_equal(
                    tok_meta["window"], frozen_window
                ):
                    raise NativeBridgeRejected(
                        "agent_computer_state_token_stale",
                        phase="pre_dispatch",
                        method="action.prepare",
                        operation_id=operation_id,
                        task_session_id=task_session_id,
                        reason="token_window_mismatch",
                        worker_instance=worker_instance,
                        worker_generation=gen,
                        worker_pid=worker_pid,
                    )

        # Check advertised unsupported actions from handshake
        hs = session.handshake or {}
        caps = hs.get("capabilities") or {}
        unsupported = caps.get("unsupportedActions") or []
        if frozen_action["type"] in unsupported:
            raise NativeBridgeRejected(
                "agent_computer_action_unsupported",
                phase="pre_dispatch",
                stage="prepare",
                method="action.prepare",
                native_code="unsupported",
                native_stage="prepare",
                native_mutation="none",
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            )

        prepare_params: dict[str, Any] = {
            "taskSessionId": task_session_id,
            "operationId": operation_id,
            "window": copy.deepcopy(frozen_window),
            "expectedStateToken": frozen_state_token,
            "action": copy.deepcopy(frozen_action),
        }
        if timeout_ms is not None:
            prepare_params["timeoutMs"] = timeout_ms
        if authority_epoch is not None:
            prepare_params["authorityEpoch"] = authority_epoch
        if frozen_constraints is not None:
            prepare_params["constraints"] = copy.deepcopy(frozen_constraints)

        prep_timeout_s = (
            max(1.0, (timeout_ms / 1000.0) + 2.0)
            if timeout_ms is not None
            else DEFAULT_REQUEST_TIMEOUT_S
        )

        prepare_started_at = time.monotonic()
        try:
            prepare_res_raw, pending_prep = session.request(
                "action.prepare",
                prepare_params,
                timeout=prep_timeout_s,
                operation_id=operation_id,
                task_session_id=task_session_id,
                phase="prepare",
                commit_send=False,
                pre_dispatch_allowed=True,
                pre_dispatch_on_send_error=True,
                cancelled=cancelled,
                on_cancel_triggered=lambda: self._client.cancel_operation(
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    generation=gen,
                ),
            )
        except NativeBridgeRejected:
            self._client.cancel_operation(
                task_session_id=task_session_id,
                operation_id=operation_id,
                generation=gen,
            )
            raise
        except NativeBridgeError as exc:
            # `action.prepare` cannot execute focus or input without `action.commit`,
            # provided native did not report a non-none mutation. Always cancel/retire
            # so the global desktop input lock is never left held.
            cancel_info = self._client.cancel_operation(
                task_session_id=task_session_id,
                operation_id=operation_id,
                generation=gen,
            )
            if cancel_info is None or not cancel_info.get("inputLockReleased", False):
                self._client.retire_generation(gen)

            if (
                not exc.commit_attempted
                and exc.native_mutation in (None, "none")
                and exc.internal_mutation in (None, "none")
            ):
                raise NativeBridgeRejected(
                    exc.code,
                    phase="pre_dispatch",
                    stage=exc.stage or "prepare",
                    method="action.prepare",
                    native_code=exc.native_code,
                    native_stage=exc.native_stage,
                    native_mutation=exc.native_mutation or "none",
                    internal_mutation=exc.internal_mutation,
                    reason=exc.reason,
                    request_id=exc.request_id,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                ) from None
            raise

        # Validate ready response + ready event against immutable original payload & recomputed fingerprint
        try:
            validated_ready = verify_ready_integrity(
                prepare_result=prepare_res_raw,
                ready_event=pending_prep.ready_event,
                expected_worker_instance=worker_instance,
                task_session_id=task_session_id,
                operation_id=operation_id,
                window=frozen_window,
                expected_state_token=frozen_state_token,
                action=frozen_action,
                authority_epoch=authority_epoch,
            )
            elapsed_ms = (time.monotonic() - prepare_started_at) * 1000.0
            if elapsed_ms >= validated_ready["expiresInMs"]:
                raise NativeBridgeError(
                    "agent_computer_ready_expired",
                    phase="ready",
                    method="action.prepare",
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                )
        except NativeBridgeError as exc:
            prep_ticket = (
                prepare_res_raw.get("prepareTicket")
                if isinstance(prepare_res_raw, dict)
                and isinstance(prepare_res_raw.get("prepareTicket"), str)
                else None
            )
            cancel_info = self._client.cancel_operation(
                task_session_id=task_session_id,
                operation_id=operation_id,
                prepare_ticket=prep_ticket,
                generation=gen,
            )
            self._client.retire_generation(gen)
            if (
                exc.code == "agent_computer_ready_expired"
                and cancel_info is not None
                and cancel_info.get("mutation") == "none"
            ):
                raise NativeBridgeRejected(
                    exc.code,
                    phase="pre_dispatch",
                    stage="ready",
                    method="action.prepare",
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                ) from None
            # Tampered or invalid ready payload cannot be trusted to have zero mutation unless
            # strict protocol verified it; retire generation and raise pre_dispatch only if
            # we know commit was never sent and we retired the worker before commit.
            raise NativeBridgeRejected(
                exc.code,
                phase="pre_dispatch",
                stage="ready",
                method="action.prepare",
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            ) from None

        prepare_ticket = validated_ready["prepareTicket"]

        if cancelled is not None and cancelled():
            self._cancel_ready_ticket_or_retire(
                session=session,
                task_session_id=task_session_id,
                operation_id=operation_id,
                prepare_ticket=prepare_ticket,
            )
            reason_code = getattr(cancelled, "reason", None)
            code = (
                reason_code
                if isinstance(reason_code, str)
                and reason_code in {"agent_time_limit", "agent_time_budget_exhausted"}
                else "agent_cancelled"
            )
            raise NativeBridgeRejected(
                code,
                phase="pre_dispatch",
                stage="ready",
                method="action.prepare",
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            )

        # Call host on_ready synchronously with a detached copy so host callback cannot mutate internal state
        try:
            callback_outcome = on_ready(copy.deepcopy(validated_ready))
        except AgentToolRejected as exc:
            self._cancel_ready_ticket_or_retire(
                session=session,
                task_session_id=task_session_id,
                operation_id=operation_id,
                prepare_ticket=prepare_ticket,
            )
            raw_code = str(exc)
            safe_code = (
                raw_code
                if raw_code in ALLOWLISTED_BRIDGE_ERROR_CODES
                else "agent_computer_callback_rejected"
            )
            raise NativeBridgeRejected(
                safe_code,
                phase="on_ready",
                stage="ready",
                method="action.prepare",
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            ) from exc
        except BaseException as exc:
            # Host callback raised a non-AgentToolRejected exception (e.g. ledger I/O error or
            # post-update failure). Cancel the native ticket so no physical action runs,
            # but mark callback_uncertain=True / pre_dispatch=False so caller never blindly
            # labels it a safe retry!
            self._cancel_ready_ticket_or_retire(
                session=session,
                task_session_id=task_session_id,
                operation_id=operation_id,
                prepare_ticket=prepare_ticket,
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raw_code = str(exc) if isinstance(exc, AgentError) else ""
            safe_code = (
                raw_code
                if raw_code in ALLOWLISTED_BRIDGE_ERROR_CODES
                else "agent_computer_callback_uncertain"
            )
            uncertain_err = NativeBridgeError(
                safe_code,
                phase="on_ready",
                stage="ready",
                method="action.prepare",
                commit_attempted=False,
                dispatched=False,
                pre_dispatch=False,
                callback_uncertain=True,
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            )
            self._record_operation(
                _OperationRecord(
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    action_fingerprint=expected_fingerprint,
                    window=frozen_window,
                    expected_state_token=frozen_state_token,
                    status="commit_attempted",
                    commit_attempted=False,
                    dispatched=False,
                    callback_uncertain=True,
                    error=uncertain_err,
                )
            )
            raise uncertain_err from exc

        if callback_outcome is False:
            self._cancel_ready_ticket_or_retire(
                session=session,
                task_session_id=task_session_id,
                operation_id=operation_id,
                prepare_ticket=prepare_ticket,
            )
            raise NativeBridgeRejected(
                "agent_computer_callback_rejected",
                phase="on_ready",
                stage="ready",
                method="action.prepare",
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=worker_instance,
                worker_generation=gen,
                worker_pid=worker_pid,
            )

        # Build fixed commit payload: ONLY taskSessionId, operationId, prepareTicket (+ optional observation/deadline)
        commit_params: dict[str, Any] = {
            "taskSessionId": task_session_id,
            "operationId": operation_id,
            "prepareTicket": prepare_ticket,
        }
        if post_observation is not None:
            if not isinstance(post_observation, dict):
                self._cancel_ready_ticket_or_retire(
                    session=session,
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    prepare_ticket=prepare_ticket,
                )
                raise NativeBridgeError(
                    "agent_computer_invalid_params",
                    phase="commit_send",
                    stage="ready",
                    method="action.commit",
                    commit_attempted=False,
                    dispatched=True,
                    pre_dispatch=False,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                )
            commit_params["postObservation"] = post_observation
        if commit_deadline_ms is not None:
            if type(commit_deadline_ms) is not int or not (
                50 <= commit_deadline_ms <= 120_000
            ):
                self._cancel_ready_ticket_or_retire(
                    session=session,
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    prepare_ticket=prepare_ticket,
                )
                raise NativeBridgeError(
                    "agent_computer_invalid_params",
                    phase="commit_send",
                    stage="ready",
                    method="action.commit",
                    commit_attempted=False,
                    dispatched=True,
                    pre_dispatch=False,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                )
            commit_params["deadlineMs"] = commit_deadline_ms

        commit_timeout_s = (
            max(1.0, (commit_deadline_ms / 1000.0) + 2.0)
            if commit_deadline_ms is not None
            else DEFAULT_REQUEST_TIMEOUT_S
        )

        commit_wire_started = False

        def _commit_writer_critical_gate() -> None:
            """Final pre-write gate executed inside the writer thread immediately before the first commit byte."""
            nonlocal commit_wire_started
            if cancelled is not None and cancelled():
                reason_code = getattr(cancelled, "reason", None)
                code = (
                    reason_code
                    if isinstance(reason_code, str)
                    and reason_code in {"agent_time_limit", "agent_time_budget_exhausted"}
                    else "agent_cancelled"
                )
                raise NativeBridgeError(
                    code,
                    phase="commit_send",
                    stage="ready",
                    method="action.commit",
                    commit_attempted=False,
                    dispatched=True,
                    pre_dispatch=False,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                )

            cur_gen, cur_inst, _ = self._client.active_session_info()
            if (
                not session.is_usable()
                or cur_gen != gen
                or cur_inst != worker_instance
            ):
                raise NativeBridgeError(
                    "agent_computer_process_disconnected",
                    phase="commit_send",
                    stage="ready",
                    method="action.commit",
                    commit_attempted=False,
                    dispatched=True,
                    pre_dispatch=False,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                )

            elapsed_before_write_ms = (time.monotonic() - prepare_started_at) * 1000.0
            if elapsed_before_write_ms >= validated_ready["expiresInMs"]:
                raise NativeBridgeError(
                    "agent_computer_ready_expired",
                    phase="commit_send",
                    stage="ready",
                    method="action.commit",
                    commit_attempted=False,
                    dispatched=True,
                    pre_dispatch=False,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=worker_instance,
                    worker_generation=gen,
                    worker_pid=worker_pid,
                )

            commit_wire_started = True
            self._record_operation(
                _OperationRecord(
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    action_fingerprint=expected_fingerprint,
                    window=frozen_window,
                    expected_state_token=frozen_state_token,
                    status="commit_attempted",
                    commit_attempted=True,
                    dispatched=True,
                )
            )

        # Issue action.commit EXACTLY ONCE
        try:
            commit_res_raw, _ = session.request(
                "action.commit",
                commit_params,
                timeout=commit_timeout_s,
                operation_id=operation_id,
                task_session_id=task_session_id,
                phase="commit_wait",
                commit_send=True,
                pre_dispatch_allowed=False,
                pre_dispatch_on_send_error=False,
                cancelled=cancelled,
                on_cancel_triggered=lambda: self._client.cancel_operation(
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    prepare_ticket=prepare_ticket,
                    generation=gen,
                ),
                on_before_write=_commit_writer_critical_gate,
            )
            validated_commit = validate_action_commit_result(
                commit_res_raw,
                expected_worker_instance=worker_instance,
                expected_task_session_id=task_session_id,
                expected_operation_id=operation_id,
                expected_action_fingerprint=expected_fingerprint,
                expected_window=frozen_window,
                phase="commit_wait",
            )
        except NativeBridgeError as exc:
            if not commit_wire_started and not exc.commit_attempted:
                # Aborted in the writer critical section BEFORE any commit byte was written!
                # Cancel the prepared ticket so the OS lock is released and zero commit runs,
                # while preserving dispatched=True, commit_attempted=False, pre_dispatch=False
                # because host on_ready already completed.
                self._cancel_ready_ticket_or_retire(
                    session=session,
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    prepare_ticket=prepare_ticket,
                )
                exc.commit_attempted = False
                exc.dispatched = True
                exc.pre_dispatch = False
                exc.outcome = "unknown"
                exc.diagnostics["commit_attempted"] = False
                exc.diagnostics["dispatched"] = True
                self._record_operation(
                    _OperationRecord(
                        task_session_id=task_session_id,
                        operation_id=operation_id,
                        action_fingerprint=expected_fingerprint,
                        window=frozen_window,
                        expected_state_token=frozen_state_token,
                        status="commit_attempted",
                        commit_attempted=False,
                        dispatched=True,
                        error=exc,
                    )
                )
                raise

            # Once commit write has started, ALL errors/timeouts/disconnects stay commit_attempted=True,
            # dispatched=True, pre_dispatch=False (outcome='unknown'), even if native reports mutation='none'.
            exc.commit_attempted = True
            exc.dispatched = True
            exc.pre_dispatch = False
            exc.outcome = "unknown"
            exc.diagnostics["commit_attempted"] = True
            exc.diagnostics["dispatched"] = True
            if exc.phase == "pre_dispatch":
                exc.phase = "post_dispatch"
                exc.diagnostics["phase"] = "post_dispatch"

            self._record_operation(
                _OperationRecord(
                    task_session_id=task_session_id,
                    operation_id=operation_id,
                    action_fingerprint=expected_fingerprint,
                    window=frozen_window,
                    expected_state_token=frozen_state_token,
                    status="commit_attempted",
                    commit_attempted=True,
                    dispatched=True,
                    error=exc,
                )
            )
            raise

        # Update task namespace tokens if post-observation was returned
        tstate = self._get_or_create_task_state(
            task_session_id,
            worker_instance=worker_instance,
            generation=gen,
        )
        with self._lock:
            # Old state token on this window is now superseded by commit's epoch advance
            tstate.tokens.pop(frozen_state_token, None)
            if validated_commit.get("observation") is not None:
                tstate.record_observation_meta(validated_commit["observation"])

        cached_commit = copy.deepcopy(validated_commit)
        if cached_commit.get("observation") is not None:
            cached_commit["observation"] = _strip_observation_screenshots_for_cache(
                cached_commit["observation"]
            )

        self._record_operation(
            _OperationRecord(
                task_session_id=task_session_id,
                operation_id=operation_id,
                action_fingerprint=expected_fingerprint,
                window=frozen_window,
                expected_state_token=frozen_state_token,
                status="confirmed",
                commit_attempted=True,
                dispatched=True,
                result=cached_commit,
            )
        )
        return validated_commit

    def _cancel_ready_ticket_or_retire(
        self,
        *,
        session: Any,
        task_session_id: str,
        operation_id: str,
        prepare_ticket: str,
    ) -> None:
        cancel_res = self._client.cancel_operation(
            task_session_id=task_session_id,
            operation_id=operation_id,
            prepare_ticket=prepare_ticket,
            generation=session.generation,
        )
        if (
            cancel_res is None
            or not cancel_res.get("inputLockReleased", False)
            or cancel_res.get("mutation") != "none"
        ):
            self._client.retire_generation(session.generation)

    def cancel_operation(
        self,
        *,
        task_session_id: str,
        operation_id: str,
        prepare_ticket: str | None = None,
        timeout: float = DEFAULT_CONTROL_TIMEOUT_S,
    ) -> dict[str, Any] | None:
        with self._lock:
            self._ever_used = True
        valid_task = self._validate_task_session_id(
            task_session_id, phase="cancel"
        )
        valid_op = self._validate_operation_id(operation_id, phase="cancel")
        return self._client.cancel_operation(
            task_session_id=valid_task,
            operation_id=valid_op,
            prepare_ticket=prepare_ticket,
            timeout=timeout,
        )

    def end_task_session(
        self,
        task_session_id: str,
        *,
        timeout: float = DEFAULT_CONTROL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Retire a task's snapshots, tokens, and pending operations without closing user apps."""
        with self._lock:
            self._ever_used = True
        valid_task = self._validate_task_session_id(
            task_session_id, phase="session"
        )
        with self._lock:
            self._tasks.pop(valid_task, None)
        return self._client.end_task_session(valid_task, timeout=timeout)

    def reconfigure(
        self,
        *,
        bundled_executable: str | Path | None = None,
        dev_executable: str | Path | list[str] | tuple[str, ...] | None = None,
        transport_factory: Callable[[], Any] | None = None,
        artifact_resolver: Any = None,
        manifest_path: str | Path | None = None,
        bundle_root: str | Path | None = None,
        process_identity_provider: Callable[[Any], Any] | None = None,
        expected_worker_name: str = EXPECTED_WORKER_NAME,
        expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
        expected_protocol_version: str = PROTOCOL_SEMVER,
        handshake_timeout: float | None = None,
    ) -> BridgeConfigIdentity:
        """Explicitly reconfigure this bridge: retire active worker, clear task tokens, preserve post-dispatch no-replay records."""
        with self._lock:
            self._ever_used = True
            self._tasks.clear()
            preserved_ops: OrderedDict[str, _OperationRecord] = OrderedDict()
            for op_key, rec in self._operations.items():
                if (
                    rec.commit_attempted
                    or rec.dispatched
                    or rec.callback_uncertain
                    or rec.status in ("commit_attempted", "confirmed", "in_progress")
                ):
                    preserved_ops[op_key] = rec
            self._operations = preserved_ops

        return self._client.reconfigure(
            bundled_executable=bundled_executable,
            dev_executable=dev_executable,
            transport_factory=transport_factory,
            artifact_resolver=artifact_resolver,
            manifest_path=manifest_path,
            bundle_root=bundle_root,
            process_identity_provider=process_identity_provider,
            expected_worker_name=expected_worker_name,
            expected_worker_version=expected_worker_version,
            expected_protocol_version=expected_protocol_version,
            handshake_timeout=handshake_timeout,
        )

    def _verify_and_claim_owner_locked(
        self,
        owner: Any,
        owner_id: str,
        *,
        existing: NativeBridge | None,
        allow_replace_existing: bool,
    ) -> None:
        """Atomically validate owner/config conflicts and claim ownership only if `self` is pristine.

        Lock order: `_OWNER_BRIDGES_LOCK` -> `self._lock` -> `self._client._lock`.
        Never mutates `self` or `self._client` if any conflict or non-pristine rejection occurs.
        """
        with self._lock:
            with self._client._lock:
                if existing is not None and self is existing:
                    if (
                        self._bound_owner_token != id(owner)
                        or (
                            self._owner_ref is not None
                            and self._owner_ref() is not None
                            and self._owner_ref() is not owner
                        )
                        or self._owner_id != owner_id
                        or self._client._owner_id != owner_id
                    ):
                        raise NativeBridgeRejected(
                            "agent_computer_owner_conflict",
                            phase="pre_dispatch",
                        )
                    return

                owner_matches = (
                    self._bound_owner_token == id(owner)
                    and (
                        self._owner_ref is None
                        or self._owner_ref() is None
                        or self._owner_ref() is owner
                    )
                    and self._owner_id == owner_id
                    and self._client._owner_id == owner_id
                )
                is_unowned = (
                    self._bound_owner_token is None and self._owner_ref is None
                )
                if not (is_unowned or owner_matches) or not self._is_pristine_locked():
                    raise NativeBridgeRejected(
                        "agent_computer_owner_conflict",
                        phase="pre_dispatch",
                    )

                if existing is not None and not allow_replace_existing:
                    raise NativeBridgeRejected(
                        "agent_computer_config_conflict",
                        phase="pre_dispatch",
                    )

                try:
                    self._owner_ref = weakref.ref(owner)
                except TypeError:
                    self._owner_ref = None
                self._bound_owner_token = id(owner)
                self._owner_id = owner_id
                self._client._owner_id = owner_id

    def _adopt_post_dispatch_operations_from(self, other: NativeBridge) -> None:
        with other._lock:
            other_ops = list(other._operations.items())
        with self._lock:
            for op_key, rec in other_ops:
                if (
                    rec.commit_attempted
                    or rec.dispatched
                    or rec.callback_uncertain
                    or rec.status in ("commit_attempted", "confirmed", "in_progress")
                ):
                    if op_key not in self._operations:
                        self._operations[op_key] = rec
                        while len(self._operations) > MAX_TRACKED_OPERATIONS:
                            self._operations.popitem(last=False)

    def close(self) -> None:
        with self._lock:
            self._ever_used = True
            self._tasks.clear()
        self._client.close()


_OWNER_BRIDGES_LOCK = threading.Lock()
_OWNER_BRIDGES: weakref.WeakKeyDictionary[Any, NativeBridge] = (
    weakref.WeakKeyDictionary()
)


def _lookup_bound_bridge_locked(owner: Any) -> NativeBridge | None:
    existing = getattr(owner, "_ryn_native_bridge", None)
    if isinstance(existing, NativeBridge):
        return existing
    try:
        weak_existing = _OWNER_BRIDGES.get(owner)
    except TypeError:
        weak_existing = None
    if isinstance(weak_existing, NativeBridge):
        return weak_existing
    return None


def _store_bound_bridge_locked(owner: Any, instance: NativeBridge) -> None:
    try:
        setattr(owner, "_ryn_native_bridge", instance)
    except AttributeError:
        _OWNER_BRIDGES[owner] = instance


def bind_node_native_bridge(
    owner: Any,
    *,
    bridge: NativeBridge | None = None,
    bundled_executable: str | Path | None = None,
    dev_executable: str | Path | list[str] | tuple[str, ...] | None = None,
    transport_factory: Callable[[], Any] | None = None,
    artifact_resolver: Any = None,
    manifest_path: str | Path | None = None,
    bundle_root: str | Path | None = None,
    process_identity_provider: Callable[[Any], Any] | None = None,
    expected_worker_name: str | None = None,
    expected_worker_version: str | None = None,
    expected_protocol_version: str | None = None,
    handshake_timeout: float | None = None,
) -> NativeBridge:
    """Attach or retrieve the shared `NativeBridge` instance owned by `owner` (e.g. `AgentStore` or node runtime).

    - Retrieve-only (`bind_node_native_bridge(owner)`): returns the bound bridge or initializes a default one.
    - Idempotent same-config: returns the existing bound bridge without restarting the worker.
    - Conflicting explicit config or different `bridge`: raises `agent_computer_config_conflict`.
    - Cross-owner or already-used standalone `bridge` reuse: raises `agent_computer_owner_conflict`.
    """
    if owner is None:
        raise ValueError("owner is required to scope NativeBridge ownership")

    owner_id = resolve_owner_id(owner)
    has_explicit_config = any(
        val is not None
        for val in (
            bundled_executable,
            dev_executable,
            transport_factory,
            artifact_resolver,
            manifest_path,
            bundle_root,
            process_identity_provider,
            expected_worker_name,
            expected_worker_version,
            expected_protocol_version,
            handshake_timeout,
        )
    )

    if bridge is not None and has_explicit_config:
        raise NativeBridgeRejected(
            "agent_computer_config_conflict",
            phase="pre_dispatch",
        )

    eff_worker_name = (
        expected_worker_name
        if expected_worker_name is not None
        else EXPECTED_WORKER_NAME
    )
    eff_worker_version = (
        expected_worker_version
        if expected_worker_version is not None
        else EXPECTED_WORKER_VERSION
    )
    eff_protocol_version = (
        expected_protocol_version
        if expected_protocol_version is not None
        else PROTOCOL_SEMVER
    )
    eff_timeout = (
        handshake_timeout
        if handshake_timeout is not None
        else DEFAULT_HANDSHAKE_TIMEOUT_S
    )

    requested_identity = (
        build_bridge_config_identity(
            bundled_executable=bundled_executable,
            dev_executable=dev_executable,
            transport_factory=transport_factory,
            artifact_resolver=artifact_resolver,
            manifest_path=manifest_path,
            bundle_root=bundle_root,
            process_identity_provider=process_identity_provider,
            expected_worker_name=eff_worker_name,
            expected_worker_version=eff_worker_version,
            expected_protocol_version=eff_protocol_version,
        )
        if has_explicit_config
        else None
    )

    with _OWNER_BRIDGES_LOCK:
        existing = _lookup_bound_bridge_locked(owner)
        if bridge is not None:
            bridge._verify_and_claim_owner_locked(
                owner,
                owner_id,
                existing=existing,
                allow_replace_existing=False,
            )
            if existing is not None:
                return existing
            _store_bound_bridge_locked(owner, bridge)
            return bridge

        if existing is not None:
            if requested_identity is not None:
                if (
                    existing.config_identity.config_fingerprint
                    != requested_identity.config_fingerprint
                ):
                    raise NativeBridgeRejected(
                        "agent_computer_config_conflict",
                        phase="pre_dispatch",
                    )
            return existing

        instance = NativeBridge(
            owner=owner,
            owner_id=owner_id,
            bundled_executable=bundled_executable,
            dev_executable=dev_executable,
            transport_factory=transport_factory,
            artifact_resolver=artifact_resolver,
            manifest_path=manifest_path,
            bundle_root=bundle_root,
            process_identity_provider=process_identity_provider,
            expected_worker_name=eff_worker_name,
            expected_worker_version=eff_worker_version,
            expected_protocol_version=eff_protocol_version,
            handshake_timeout=eff_timeout,
        )
        _store_bound_bridge_locked(owner, instance)
        return instance


def reconfigure_node_native_bridge(
    owner: Any,
    *,
    bridge: NativeBridge | None = None,
    bundled_executable: str | Path | None = None,
    dev_executable: str | Path | list[str] | tuple[str, ...] | None = None,
    transport_factory: Callable[[], Any] | None = None,
    artifact_resolver: Any = None,
    manifest_path: str | Path | None = None,
    bundle_root: str | Path | None = None,
    process_identity_provider: Callable[[Any], Any] | None = None,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
) -> NativeBridge:
    """Explicitly replace or reconfigure the `NativeBridge` bound to `owner`.

    Retires the old worker generation, marks old cached binding stale, invalidates old task tokens,
    and preserves post-dispatch operation records so no-replay authority is never lost.
    """
    if owner is None:
        raise ValueError("owner is required to scope NativeBridge ownership")

    owner_id = resolve_owner_id(owner)
    old_to_close: NativeBridge | None = None
    existing_to_reconfigure: NativeBridge | None = None
    result_bridge: NativeBridge | None = None

    with _OWNER_BRIDGES_LOCK:
        existing = _lookup_bound_bridge_locked(owner)
        if bridge is not None:
            bridge._verify_and_claim_owner_locked(
                owner,
                owner_id,
                existing=existing,
                allow_replace_existing=True,
            )
            if existing is None:
                _store_bound_bridge_locked(owner, bridge)
                return bridge
            if bridge is existing:
                return existing
            bridge._adopt_post_dispatch_operations_from(existing)
            _store_bound_bridge_locked(owner, bridge)
            old_to_close = existing
            result_bridge = bridge
        elif existing is None:
            instance = NativeBridge(
                owner=owner,
                owner_id=owner_id,
                bundled_executable=bundled_executable,
                dev_executable=dev_executable,
                transport_factory=transport_factory,
                artifact_resolver=artifact_resolver,
                manifest_path=manifest_path,
                bundle_root=bundle_root,
                process_identity_provider=process_identity_provider,
                expected_worker_name=expected_worker_name,
                expected_worker_version=expected_worker_version,
                expected_protocol_version=expected_protocol_version,
                handshake_timeout=handshake_timeout,
            )
            _store_bound_bridge_locked(owner, instance)
            return instance
        else:
            existing_to_reconfigure = existing
            result_bridge = existing

    if old_to_close is not None:
        old_to_close.close()
        assert result_bridge is not None
        return result_bridge

    assert existing_to_reconfigure is not None
    existing_to_reconfigure.reconfigure(
        bundled_executable=bundled_executable,
        dev_executable=dev_executable,
        transport_factory=transport_factory,
        artifact_resolver=artifact_resolver,
        manifest_path=manifest_path,
        bundle_root=bundle_root,
        process_identity_provider=process_identity_provider,
        expected_worker_name=expected_worker_name,
        expected_worker_version=expected_worker_version,
        expected_protocol_version=expected_protocol_version,
        handshake_timeout=handshake_timeout,
    )
    return existing_to_reconfigure

