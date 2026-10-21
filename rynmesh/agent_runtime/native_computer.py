"""Shared host v1 facade, task-fenced window/observation registry, and native screenshot producer.

Coordinates between Ryn host tool entry points (`ExtensionToolAdapter` builtin `computer-use`
and `LocalTools` legacy `ryn_computer_*`) and the shared per-owner `NativeBridge` instance.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from .computer.binary import resolve_owner_id
from .computer.coordinator import (
    NativeBridge,
    bind_node_native_bridge,
)
from .computer.protocol import (
    NativeBridgeError,
    NativeBridgeRejected,
    compute_action_fingerprint,
    normalize_action_spec,
    validate_window_ref,
    window_refs_equal,
)
from .computer_overlay import ComputerOverlay
from .image_capabilities import (
    MAX_IMAGE_PIXELS,
    MAX_NATIVE_IMAGE_SOURCE_BYTES,
    _is_symlink_or_reparse,
    _issue_native_observation_descriptor,
    safe_segment,
    validate_native_observation_descriptor,
    verify_and_load_native_observation_artifact,
    verify_original_image_bytes,
)
from .native_computer_files import (
    VerifiedFileIdentity,
    cleanup_task_verified_artifacts,
    exclusive_write_verified_png,
    safe_unlink_verified_file,
)
from .store import AgentError, AgentToolRejected, encoded

MAX_TRACKED_TASKS = 64
MAX_TASK_WINDOWS = 64
MAX_TASK_OBSERVATIONS = 32
MAX_TASK_ARTIFACTS = 64

_SUPPORTED_KEY_NAMES: dict[str, str] = {
    "CTRL": "Control",
    "CONTROL": "Control",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "WIN": "Meta",
    "META": "Meta",
    "ENTER": "Enter",
    "RETURN": "Enter",
    "TAB": "Tab",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "BACKSPACE": "Backspace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "SPACE": "Space",
    "LEFT": "ArrowLeft",
    "ARROWLEFT": "ArrowLeft",
    "UP": "ArrowUp",
    "ARROWUP": "ArrowUp",
    "RIGHT": "ArrowRight",
    "ARROWRIGHT": "ArrowRight",
    "DOWN": "ArrowDown",
    "ARROWDOWN": "ArrowDown",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "PageUp",
    "PGUP": "PageUp",
    "PAGEDOWN": "PageDown",
    "PGDN": "PageDown",
}


def _core_window_ref_for_descriptor(wref: Mapping[str, Any]) -> dict[str, Any]:
    """Return the 6-key core WindowRef required by `image_capabilities._normalize_window_ref`."""
    return {
        "workerInstance": str(wref["workerInstance"]),
        "windowKey": str(wref["windowKey"]),
        "hwnd": str(wref["hwnd"]),
        "pid": int(wref["pid"]),
        "processStarted": str(wref["processStarted"]),
        "generation": int(wref["generation"]),
    }


def _compact_public_element(element: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize actionable UIA element fields without redundant default boolean/tree noise."""
    out: dict[str, Any] = {
        "id": element["id"],
        "name": element["name"],
        "type": element["type"],
        "value": element["value"],
        "x": element["x"],
        "y": element["y"],
        "bounds": copy.deepcopy(element["bounds"]),
    }
    if element.get("password"):
        out["password"] = True
    if not element.get("enabled", True):
        out["enabled"] = False
    if element.get("offscreen"):
        out["offscreen"] = True
    if element.get("focused"):
        out["focused"] = True
    return out


def normalize_key_token(raw_key: Any) -> str:
    """Validate and canonicalize a single key token for native `key_press` / `key_chord`."""
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise AgentToolRejected("agent_computer_key_unsupported")
    cleaned = raw_key.strip()
    upper = cleaned.upper()
    if upper in _SUPPORTED_KEY_NAMES:
        return _SUPPORTED_KEY_NAMES[upper]
    if len(cleaned) == 1 and cleaned.isascii() and cleaned.isalnum():
        return cleaned.upper()
    if upper.startswith("F") and upper[1:].isdigit():
        fn_num = int(upper[1:])
        if 1 <= fn_num <= 12:
            return f"F{fn_num}"
    raise AgentToolRejected("agent_computer_key_unsupported")


@dataclass
class HostObservationArtifactEntry:
    """Host-retained read-only record binding a produced native screenshot artifact to its observation."""

    artifact_id: str
    task_id: str
    operation_id: str
    observation_id: str
    state_token: str
    window_ref: dict[str, Any]
    descriptor_window_ref: dict[str, Any]
    worker_instance: str
    worker_generation: int
    workspace_root: str
    relative_artifact: dict[str, Any]
    sealed_descriptor: dict[str, Any]
    file_identity: VerifiedFileIdentity | None = None
    context_epoch: int = 0
    scope_revision: int = 0
    retired: bool = False


@dataclass(frozen=True)
class TrustedWriteAuthorityStamp:
    """Immutable host authority stamp captured before `action.prepare` and re-verified at `action.ready`."""

    task_id: str
    operation_id: str
    tool_name: str
    arguments_sha256: str
    workspace_root: str
    desktop_access: bool
    has_store_row: bool
    control_revision: int | None
    context_epoch: int | None
    scope_revision: int | None
    goal_revision: int | None
    has_in_flight: bool
    in_flight_operation_id: str | None
    in_flight_arguments_sha256: str | None
    extension_binding: dict[str, Any] | None
    frozen_window_ref: dict[str, Any]
    frozen_state_token: str
    frozen_action_fingerprint: str
    frozen_hwnd: int


@dataclass
class WindowObservationRecord:
    """Task-scoped validated observation of a single native window."""

    task_id: str
    hwnd: int
    sky_window_id: int
    window_ref: dict[str, Any]
    window_title: str
    class_name: str
    process_name: str
    observation_id: str
    state_token: str
    captured_at: str
    geometry: dict[str, Any]
    foreground: dict[str, Any]
    accessibility: dict[str, Any]
    elements: list[dict[str, Any]]
    elements_by_index: dict[int, dict[str, Any]]
    elements_by_sky_id: dict[str, dict[str, Any]]
    elements_by_runtime_id: dict[str, dict[str, Any]]
    worker_instance: str
    worker_generation: int
    context_epoch: int = 0
    scope_revision: int = 0
    artifact_ids: list[str] = field(default_factory=list)


class _TaskComputerState:
    """Bounded per-task registry for observed windows, Sky IDs, state tokens, and screenshot artifacts."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.worker_instance: str | None = None
        self.worker_generation: int | None = None
        self.context_epoch: int | None = None
        self.scope_revision: int | None = None
        self.next_sky_id: int = 1
        self.sky_id_by_window_key: OrderedDict[str, int] = OrderedDict()
        self.window_ref_by_sky_id: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.windows_by_hwnd: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.windows_by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.observations_by_hwnd: OrderedDict[int, WindowObservationRecord] = OrderedDict()
        self.observations_by_token: OrderedDict[str, WindowObservationRecord] = OrderedDict()
        self.artifacts_by_id: OrderedDict[str, HostObservationArtifactEntry] = OrderedDict()
        self.tracked_files: OrderedDict[str, tuple[Path, VerifiedFileIdentity | None, Path]] = OrderedDict()

    def retire_all_observations(self) -> None:
        for art in self.artifacts_by_id.values():
            art.retired = True
        self.windows_by_hwnd.clear()
        self.windows_by_key.clear()
        self.window_ref_by_sky_id.clear()
        self.sky_id_by_window_key.clear()
        self.observations_by_hwnd.clear()
        self.observations_by_token.clear()

    def sync_generation_and_context(
        self,
        *,
        worker_instance: str | None,
        worker_generation: int | None,
        context_epoch: int | None = None,
        scope_revision: int | None = None,
    ) -> None:
        changed = False
        if (
            self.worker_instance is not None
            and worker_instance is not None
            and self.worker_instance != worker_instance
        ):
            changed = True
        if (
            self.worker_generation is not None
            and worker_generation is not None
            and self.worker_generation != worker_generation
        ):
            changed = True
        if (
            self.context_epoch is not None
            and context_epoch is not None
            and self.context_epoch != context_epoch
        ):
            changed = True
        if (
            self.scope_revision is not None
            and scope_revision is not None
            and self.scope_revision != scope_revision
        ):
            changed = True
        if changed:
            self.retire_all_observations()
        if worker_instance is not None:
            self.worker_instance = worker_instance
        if worker_generation is not None:
            self.worker_generation = worker_generation
        if context_epoch is not None:
            self.context_epoch = context_epoch
        if scope_revision is not None:
            self.scope_revision = scope_revision

    def assign_sky_id(self, wref: Mapping[str, Any]) -> int:
        wkey = str(wref["windowKey"])
        existing_id = self.sky_id_by_window_key.get(wkey)
        if existing_id is not None:
            prev_ref = self.window_ref_by_sky_id.get(existing_id)
            if prev_ref is not None and window_refs_equal(prev_ref, dict(wref)):
                self.sky_id_by_window_key.move_to_end(wkey)
                self.window_ref_by_sky_id.move_to_end(existing_id)
                self.window_ref_by_sky_id[existing_id] = copy.deepcopy(dict(wref))
                return existing_id
            # Different generation/PID for same windowKey gets a fresh non-reusable Sky ID
            self.window_ref_by_sky_id.pop(existing_id, None)
        sky_id = self.next_sky_id
        self.next_sky_id += 1
        self.sky_id_by_window_key[wkey] = sky_id
        self.window_ref_by_sky_id[sky_id] = copy.deepcopy(dict(wref))
        while len(self.sky_id_by_window_key) > MAX_TASK_WINDOWS:
            _, old_id = self.sky_id_by_window_key.popitem(last=False)
            self.window_ref_by_sky_id.pop(old_id, None)
        return sky_id

    def record_window_entry(self, entry: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        wref = validate_window_ref(dict(entry["window"]), phase="read")
        hwnd_int = int(wref["hwnd"], 10)
        sky_id = self.assign_sky_id(wref)
        prev_entry = self.windows_by_hwnd.get(hwnd_int)
        if prev_entry is not None and not window_refs_equal(prev_entry["window"], wref):
            old_obs = self.observations_by_hwnd.pop(hwnd_int, None)
            if old_obs is not None:
                self.observations_by_token.pop(old_obs.state_token, None)
                for aid in old_obs.artifact_ids:
                    if aid in self.artifacts_by_id:
                        self.artifacts_by_id[aid].retired = True

        stored = copy.deepcopy(dict(entry))
        stored["window"] = wref
        stored["sky_window_id"] = sky_id
        self.windows_by_hwnd[hwnd_int] = stored
        self.windows_by_hwnd.move_to_end(hwnd_int)
        while len(self.windows_by_hwnd) > MAX_TASK_WINDOWS:
            self.windows_by_hwnd.popitem(last=False)

        self.windows_by_key[wref["windowKey"]] = stored
        self.windows_by_key.move_to_end(wref["windowKey"])
        while len(self.windows_by_key) > MAX_TASK_WINDOWS:
            self.windows_by_key.popitem(last=False)
        return sky_id, stored

    def record_observation(
        self,
        obs_record: WindowObservationRecord,
        artifacts: list[HostObservationArtifactEntry],
    ) -> list[tuple[Path, VerifiedFileIdentity | None, Path]]:
        evicted_files: list[tuple[Path, VerifiedFileIdentity | None, Path]] = []
        prev = self.observations_by_hwnd.get(obs_record.hwnd)
        if prev is not None and prev.state_token != obs_record.state_token:
            self.observations_by_token.pop(prev.state_token, None)
            for aid in prev.artifact_ids:
                if aid in self.artifacts_by_id:
                    self.artifacts_by_id[aid].retired = True

        self.observations_by_hwnd[obs_record.hwnd] = obs_record
        self.observations_by_hwnd.move_to_end(obs_record.hwnd)
        while len(self.observations_by_hwnd) > MAX_TASK_OBSERVATIONS:
            _, evicted = self.observations_by_hwnd.popitem(last=False)
            self.observations_by_token.pop(evicted.state_token, None)
            for aid in evicted.artifact_ids:
                if aid in self.artifacts_by_id:
                    self.artifacts_by_id[aid].retired = True

        self.observations_by_token[obs_record.state_token] = obs_record
        self.observations_by_token.move_to_end(obs_record.state_token)
        while len(self.observations_by_token) > MAX_TASK_OBSERVATIONS:
            _, evicted = self.observations_by_token.popitem(last=False)
            for aid in evicted.artifact_ids:
                if aid in self.artifacts_by_id:
                    self.artifacts_by_id[aid].retired = True

        for art in artifacts:
            self.artifacts_by_id[art.artifact_id] = art
            self.artifacts_by_id.move_to_end(art.artifact_id)
            ws_path = Path(art.workspace_root)
            rel_p = str(art.relative_artifact.get("path") or "")
            if rel_p:
                target_p = ws_path / Path(rel_p)
                self.tracked_files[art.artifact_id] = (target_p, art.file_identity, ws_path)
                self.tracked_files.move_to_end(art.artifact_id)
            while len(self.artifacts_by_id) > MAX_TASK_ARTIFACTS:
                ev_id, evicted_art = self.artifacts_by_id.popitem(last=False)
                evicted_art.retired = True
                f_info = self.tracked_files.pop(ev_id, None)
                if f_info is not None:
                    evicted_files.append(f_info)
        return evicted_files


class NativeComputerFacade:
    """Shared host facade routing builtin `computer-use` and `ryn_computer_*` through one `NativeBridge`."""

    def __init__(
        self,
        owner: Any,
        *,
        local_tools: Any = None,
        bridge: NativeBridge | None = None,
    ) -> None:
        if owner is None:
            raise ValueError("owner is required for NativeComputerFacade")
        self._lock = threading.RLock()
        self._owner = owner
        self._owner_id = resolve_owner_id(owner)
        self._local_tools = local_tools
        self._explicit_bridge = bridge
        self._tasks: OrderedDict[str, _TaskComputerState] = OrderedDict()
        self._workspace_roots_by_task: OrderedDict[str, set[Path]] = OrderedDict()
        self.overlay = ComputerOverlay()

    @property
    def owner_id(self) -> str:
        with self._lock:
            return self._owner_id

    def register_owner_alias(self, alias_owner: Any) -> None:
        if alias_owner is None:
            return
        with self._lock:
            if alias_owner is self._local_tools or self._local_tools is None:
                self._local_tools = alias_owner
            try:
                alias_owner._ryn_native_computer = self
            except AttributeError:
                pass
            if hasattr(alias_owner, "native_computer"):
                try:
                    alias_owner.native_computer = self
                except AttributeError:
                    pass

    def _peek_bridge_locked(self) -> NativeBridge | None:
        if isinstance(self._explicit_bridge, NativeBridge):
            return self._explicit_bridge
        bound = getattr(self._owner, "_ryn_native_bridge", None)
        if isinstance(bound, NativeBridge):
            return bound
        return None

    @property
    def bridge(self) -> NativeBridge:
        with self._lock:
            if self._explicit_bridge is not None and not isinstance(self._explicit_bridge, NativeBridge):
                return self._explicit_bridge
            return bind_node_native_bridge(self._owner, bridge=self._explicit_bridge)

    def _get_task_state_locked(
        self,
        task_id: str,
        *,
        worker_instance: str | None = None,
        worker_generation: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> _TaskComputerState:
        tstate = self._tasks.get(task_id)
        if tstate is None:
            tstate = _TaskComputerState(task_id)
            self._tasks[task_id] = tstate
            while len(self._tasks) > MAX_TRACKED_TASKS:
                _, evicted = self._tasks.popitem(last=False)
                evicted.retire_all_observations()
        else:
            self._tasks.move_to_end(task_id)

        ctx_epoch = (
            int(context["context_epoch"])
            if isinstance(context, Mapping) and type(context.get("context_epoch")) is int
            else None
        )
        scope_rev = (
            int(context["scope_revision"])
            if isinstance(context, Mapping) and type(context.get("scope_revision")) is int
            else None
        )
        tstate.sync_generation_and_context(
            worker_instance=worker_instance,
            worker_generation=worker_generation,
            context_epoch=ctx_epoch,
            scope_revision=scope_rev,
        )
        return tstate

    def _resolve_workspace_root(self, context: Mapping[str, Any] | None) -> Path:
        raw_ws = ""
        if isinstance(context, Mapping):
            loc = context.get("local_execution")
            if isinstance(loc, Mapping) and isinstance(loc.get("workspace_root"), str):
                raw_ws = loc["workspace_root"].strip()
            if not raw_ws and isinstance(context.get("workspace_root"), str):
                raw_ws = context["workspace_root"].strip()
        if not raw_ws and self._local_tools is not None and isinstance(context, Mapping):
            try:
                pol = self._local_tools.policy(dict(context))
                if isinstance(pol, Mapping) and isinstance(pol.get("workspace_root"), str):
                    raw_ws = pol["workspace_root"].strip()
            except Exception:
                pass
        if raw_ws:
            ws_path = Path(raw_ws)
            if not ws_path.exists() or _is_symlink_or_reparse(ws_path):
                raise AgentToolRejected("agent_invalid_workspace")
            resolved = ws_path.resolve(strict=True)
            if not resolved.is_dir() or _is_symlink_or_reparse(resolved):
                raise AgentToolRejected("agent_invalid_workspace")
            return resolved

        # Fallback internal artifact root when task has no workspace_root configured
        for cand in (
            self._owner,
            getattr(self._local_tools, "store", None) if self._local_tools is not None else None,
            self._local_tools,
        ):
            if cand is None:
                continue
            root_attr = getattr(cand, "root", None) or getattr(cand, "home", None)
            if root_attr is not None:
                fallback = Path(root_attr).resolve() / "native_workspace"
                fallback.mkdir(parents=True, exist_ok=True)
                return fallback.resolve(strict=True)
        raise AgentToolRejected("agent_workspace_required")

    def _read_fresh_task_authority(
        self, task_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Read fresh `(settings, task_row)` from the authoritative owner store when available."""
        store_obj = None
        for cand in (
            self._owner,
            getattr(self._local_tools, "store", None) if self._local_tools is not None else None,
        ):
            if cand is not None and (hasattr(cand, "inspect") or hasattr(cand, "read_control")):
                store_obj = cand
                break
        if store_obj is None:
            return None, None
        if hasattr(store_obj, "inspect"):
            def _proj(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
                settings = copy.deepcopy(dict(data.get("settings") or {}))
                raw_row = (data.get("tasks") or {}).get(task_id)
                if not isinstance(raw_row, Mapping):
                    return settings, None
                row_copy = {
                    k: copy.deepcopy(raw_row[k])
                    for k in (
                        "id",
                        "status",
                        "pending_control",
                        "control_revision",
                        "context_epoch",
                        "scope_revision",
                        "goal",
                        "local_execution",
                        "permission_mode",
                        "allowed_write_tools",
                        "approved",
                        "in_flight",
                    )
                    if k in raw_row
                }
                return settings, row_copy
            try:
                return store_obj.inspect(_proj)
            except Exception:
                pass
        if hasattr(store_obj, "read_control"):
            try:
                ctrl = store_obj.read_control(task_id)
                settings = copy.deepcopy(dict(ctrl.get("settings") or {}))
                raw_row = (ctrl.get("tasks") or {}).get(task_id)
                return settings, (copy.deepcopy(dict(raw_row)) if isinstance(raw_row, Mapping) else None)
            except Exception:
                pass
        return None, None

    def capture_trusted_write_authority(
        self,
        *,
        task_id: str,
        operation_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        approved_preview: Mapping[str, Any],
        binding: Mapping[str, Any] | None = None,
        verify_binding_fn: Callable[[dict[str, Any]], Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> TrustedWriteAuthorityStamp:
        """Capture trusted host write authority before `action.prepare`."""
        if cancelled is not None and cancelled():
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
        if binding is not None and verify_binding_fn is not None:
            verify_binding_fn(copy.deepcopy(dict(binding)))

        ctx: Mapping[str, Any] = context if isinstance(context, Mapping) else {}
        if self._local_tools is not None and ctx and hasattr(self._local_tools, "policy"):
            pol = self._local_tools.policy(dict(ctx))
            if not pol.get("desktop_access"):
                raise AgentToolRejected("agent_desktop_access_required")

        settings, row = self._read_fresh_task_authority(task_id)
        if settings is not None:
            if not settings.get("enabled", True):
                raise AgentToolRejected("agent_disabled")
            if not settings.get("desktop_access"):
                raise AgentToolRejected("agent_desktop_access_required")
            if settings.get("permission_mode") == "read_only":
                raise AgentToolRejected("agent_permission_denied")

        has_store_row = row is not None
        if row is not None:
            status = row.get("status")
            if status is not None and status != "running":
                if status in {"paused", "cancelled", "interrupted"}:
                    raise AgentToolRejected("agent_cancelled")
                raise AgentToolRejected("agent_desktop_access_required")
            if row.get("pending_control"):
                raise AgentToolRejected("agent_cancelled")
            row_exec = row.get("local_execution") or {}
            if "desktop_access" in row_exec and not row_exec.get("desktop_access"):
                raise AgentToolRejected("agent_desktop_access_required")

        ctx_exec = ctx.get("local_execution") if isinstance(ctx.get("local_execution"), Mapping) else {}
        if "desktop_access" in ctx_exec and not ctx_exec.get("desktop_access"):
            raise AgentToolRejected("agent_desktop_access_required")
        ws_root = str(
            ctx_exec.get("workspace_root")
            or ((row or {}).get("local_execution") or {}).get("workspace_root")
            or ""
        )

        def _pick_rev(ctx_key: str, row_val: Any) -> int | None:
            ctx_val = ctx.get(ctx_key)
            if type(row_val) is int and type(ctx_val) is int and row_val != ctx_val:
                raise AgentToolRejected("agent_cancelled")
            if type(row_val) is int:
                return int(row_val)
            if type(ctx_val) is int:
                return int(ctx_val)
            return None

        ctrl_rev = _pick_rev("control_revision", (row or {}).get("control_revision"))
        ctx_epoch = _pick_rev("context_epoch", (row or {}).get("context_epoch"))
        scope_rev = _pick_rev("scope_revision", (row or {}).get("scope_revision"))
        row_goal_rev = ((row or {}).get("goal") or {}).get("revision") if isinstance((row or {}).get("goal"), Mapping) else None
        goal_rev = _pick_rev("goal_revision", row_goal_rev)

        in_flight = (row or {}).get("in_flight") if isinstance((row or {}).get("in_flight"), Mapping) else None
        has_in_flight = bool(in_flight and in_flight.get("operation_id"))
        in_flight_op_id = str(in_flight["operation_id"]) if has_in_flight and in_flight is not None else None
        in_flight_args_sha = (
            str(in_flight.get("arguments_sha256"))
            if has_in_flight and in_flight is not None and in_flight.get("arguments_sha256")
            else None
        )

        frozen_wref = copy.deepcopy(dict(approved_preview["window_ref"]))
        frozen_token = str(approved_preview["expected_state_token"])
        frozen_fp = str(approved_preview["action_fingerprint"])
        hwnd = int(frozen_wref["hwnd"], 10)
        args_sha = hashlib.sha256(encoded(dict(arguments))).hexdigest()

        return TrustedWriteAuthorityStamp(
            task_id=task_id,
            operation_id=operation_id,
            tool_name=tool_name,
            arguments_sha256=args_sha,
            workspace_root=ws_root,
            desktop_access=True,
            has_store_row=has_store_row,
            control_revision=ctrl_rev,
            context_epoch=ctx_epoch,
            scope_revision=scope_rev,
            goal_revision=goal_rev,
            has_in_flight=has_in_flight,
            in_flight_operation_id=in_flight_op_id,
            in_flight_arguments_sha256=in_flight_args_sha,
            extension_binding=copy.deepcopy(dict(binding)) if isinstance(binding, Mapping) else None,
            frozen_window_ref=frozen_wref,
            frozen_state_token=frozen_token,
            frozen_action_fingerprint=frozen_fp,
            frozen_hwnd=hwnd,
        )

    def verify_ready_write_authority(
        self,
        stamp: TrustedWriteAuthorityStamp,
        *,
        validated_ready: Mapping[str, Any],
        context: Mapping[str, Any] | None,
        cancelled: Callable[[], bool],
        verify_binding_fn: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Re-verify fresh task/settings/control/binding/preview authority at `action.ready` before ledger dispatch."""
        if cancelled():
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")

        if (
            validated_ready.get("status") != "ready"
            or validated_ready.get("mutation") != "none"
            or validated_ready.get("inputLockHeld") is not True
            or validated_ready.get("taskSessionId") != stamp.task_id
            or validated_ready.get("operationId") != stamp.operation_id
            or validated_ready.get("expectedStateToken") != stamp.frozen_state_token
            or validated_ready.get("actionFingerprint") != stamp.frozen_action_fingerprint
            or not window_refs_equal(dict(validated_ready.get("window") or {}), stamp.frozen_window_ref)
        ):
            raise AgentToolRejected("agent_computer_ready_tampered")

        if stamp.extension_binding is not None and verify_binding_fn is not None:
            verify_binding_fn(copy.deepcopy(stamp.extension_binding))

        if self._local_tools is not None and isinstance(context, Mapping) and hasattr(self._local_tools, "policy"):
            pol = self._local_tools.policy(dict(context))
            if not pol.get("desktop_access"):
                raise AgentToolRejected("agent_desktop_access_required")
            if stamp.workspace_root and str(pol.get("workspace_root") or "") != stamp.workspace_root:
                raise AgentToolRejected("agent_workspace_changed_new_task_required")

        settings, row = self._read_fresh_task_authority(stamp.task_id)
        if settings is not None:
            if not settings.get("enabled", True):
                raise AgentToolRejected("agent_disabled")
            if not settings.get("desktop_access"):
                raise AgentToolRejected("agent_desktop_access_required")
            if settings.get("permission_mode") == "read_only":
                raise AgentToolRejected("agent_permission_denied")

        if stamp.has_store_row and row is None:
            raise AgentToolRejected("agent_cancelled")

        if row is not None:
            status = row.get("status")
            if status is not None and status != "running":
                if status in {"paused", "cancelled", "interrupted"}:
                    raise AgentToolRejected("agent_cancelled")
                raise AgentToolRejected("agent_desktop_access_required")
            if row.get("pending_control"):
                raise AgentToolRejected("agent_cancelled")
            row_exec = row.get("local_execution") or {}
            if "desktop_access" in row_exec and not row_exec.get("desktop_access"):
                raise AgentToolRejected("agent_desktop_access_required")
            if stamp.workspace_root and str(row_exec.get("workspace_root") or "") != stamp.workspace_root:
                raise AgentToolRejected("agent_workspace_changed_new_task_required")

            if (
                stamp.control_revision is not None
                and type(row.get("control_revision")) is int
                and int(row["control_revision"]) != stamp.control_revision
            ):
                raise AgentToolRejected("agent_cancelled")
            if (
                stamp.context_epoch is not None
                and type(row.get("context_epoch")) is int
                and int(row["context_epoch"]) != stamp.context_epoch
            ):
                raise AgentToolRejected("agent_cancelled")
            if (
                stamp.scope_revision is not None
                and type(row.get("scope_revision")) is int
                and int(row["scope_revision"]) != stamp.scope_revision
            ):
                raise AgentToolRejected("agent_cancelled")
            row_goal_rev = (row.get("goal") or {}).get("revision") if isinstance(row.get("goal"), Mapping) else None
            if (
                stamp.goal_revision is not None
                and type(row_goal_rev) is int
                and int(row_goal_rev) != stamp.goal_revision
            ):
                raise AgentToolRejected("agent_cancelled")

            if stamp.has_in_flight:
                cur_flight = row.get("in_flight") if isinstance(row.get("in_flight"), Mapping) else {}
                if (
                    cur_flight.get("operation_id") != stamp.in_flight_operation_id
                    or (
                        stamp.in_flight_arguments_sha256 is not None
                        and cur_flight.get("arguments_sha256") != stamp.in_flight_arguments_sha256
                    )
                ):
                    raise AgentToolRejected("agent_cancelled")

        if (
            self._local_tools is not None
            and isinstance(context, Mapping)
            and hasattr(self._local_tools, "allowed")
            and hasattr(getattr(self._local_tools, "store", None), "inspect")
        ):
            if not self._local_tools.allowed(dict(context))():
                raise AgentToolRejected("agent_cancelled")

        latest_obs = self._require_observed_window_state(
            stamp.task_id, stamp.frozen_hwnd, context=context
        )
        if (
            latest_obs.state_token != stamp.frozen_state_token
            or not window_refs_equal(latest_obs.window_ref, stamp.frozen_window_ref)
        ):
            raise AgentToolRejected("agent_approval_preview_changed")

    def _translate_bridge_exception(self, exc: NativeBridgeError, *, is_write: bool) -> NoReturn:
        # The bundled worker distinguishes activation from delivery of the
        # requested input. A validated hit-test failure occurs before SendInput;
        # it must not strand a task as an ambiguous send merely because focus
        # changed. Never infer this from an arbitrary MCP error or timeout.
        no_input_failure = (
            (exc.native_code == "window_occluded" and exc.native_stage == "validating"
             and exc.reason == "hit_test_unrelated_window")
            or (exc.native_code == "focus_failed" and exc.native_stage == "activating"
                and exc.reason == "foreground_verification_failed")
        )
        if (is_write and exc.method == "action.commit" and no_input_failure
                and exc.internal_mutation == "focus_only"
                and not exc.callback_uncertain):
            rej = AgentToolRejected(exc.code, details=copy.deepcopy(exc.diagnostics),
                correction="Window activation or target hit testing failed. Only window activation was attempted; "
                           "no click or text input was delivered. Because activation mutated window state, your "
                           "previous state_token is now stale. Call computer_read_window(hwnd=..., include_screenshot=true) "
                           "first to read a fresh screenshot and fresh state_token before calling computer_act_window "
                           "(including action='focus'). Then choose the correct current target and do not reuse stale coordinates.")
            rej.diagnostics = copy.deepcopy(exc.diagnostics)
            rej.input_not_delivered = True
            rej.pre_dispatch = False
            rej.dispatched = True
            rej.commit_attempted = True
            raise rej from None
        if isinstance(exc, NativeBridgeRejected) or (
            exc.pre_dispatch
            and not exc.commit_attempted
            and not exc.dispatched
            and not exc.callback_uncertain
        ):
            cause = exc.__cause__
            rej_code = (
                str(cause)
                if isinstance(cause, AgentToolRejected) and str(cause).strip()
                else exc.code
            )
            rej_correction = None
            if rej_code == "agent_computer_state_token_stale":
                rej_correction = (
                    "The window state_token is stale because window or desktop state changed. "
                    "Call computer_read_window(hwnd=..., include_screenshot=true) on the target window "
                    "to obtain a fresh state_token and fresh screenshot before calling computer_act_window again. "
                    "Note that computer_observe_windows only lists windows and does not issue a state_token."
                )
            rej = AgentToolRejected(rej_code, details=copy.deepcopy(exc.diagnostics), correction=rej_correction)
            rej.diagnostics = copy.deepcopy(exc.diagnostics)
            rej.pre_dispatch = True
            rej.dispatched = False
            rej.commit_attempted = False
            raise rej from None

        if is_write:
            err = AgentError("agent_write_outcome_unknown")
            err.diagnostics = copy.deepcopy(exc.diagnostics)
            err.cause_code = exc.code
            err.dispatched = True
            err.commit_attempted = exc.commit_attempted
            err.callback_uncertain = exc.callback_uncertain
            raise err from None

        read_err = AgentError(exc.code)
        read_err.diagnostics = copy.deepcopy(exc.diagnostics)
        raise read_err from None

    def _produce_observation_screenshots(
        self,
        *,
        task_id: str,
        operation_id: str,
        obs: Mapping[str, Any],
        worker_instance: str,
        worker_generation: int,
        context: Mapping[str, Any] | None,
    ) -> tuple[list[HostObservationArtifactEntry], list[dict[str, Any]]]:
        raw_shots = obs.get("screenshots") or []
        if not isinstance(raw_shots, list) or not raw_shots:
            return [], []

        ok_shots = [s for s in raw_shots if isinstance(s, Mapping) and s.get("status") == "ok"]
        if not ok_shots:
            return [], []

        ws_resolved = self._resolve_workspace_root(context)
        with self._lock:
            roots = self._workspace_roots_by_task.setdefault(task_id, set())
            if len(roots) < 8:
                roots.add(ws_resolved)
            self._workspace_roots_by_task.move_to_end(task_id)
            while len(self._workspace_roots_by_task) > MAX_TRACKED_TASKS:
                self._workspace_roots_by_task.popitem(last=False)

        safe_task = safe_segment(task_id, "task")
        safe_op = safe_segment(operation_id, "op")
        wref = copy.deepcopy(dict(obs["window"]))
        desc_wref = _core_window_ref_for_descriptor(wref)
        geom = obs["geometry"]
        win_rect = geom["windowRect"]
        cli_rect = geom["clientRect"]
        dpi = int(geom["dpi"])
        geom_scale = float(geom["scaleFactor"])
        expected_scale = dpi / 96.0
        if dpi <= 0 or abs(geom_scale - expected_scale) > 1e-2:
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase="read",
                reason="malformed_json",
            )

        win_left = int(win_rect["left"])
        win_top = int(win_rect["top"])
        win_width = int(win_rect["width"])
        win_height = int(win_rect["height"])
        cli_left = int(cli_rect["left"])
        cli_top = int(cli_rect["top"])
        cli_width = int(cli_rect["width"])
        cli_height = int(cli_rect["height"])
        if (
            win_width <= 0
            or win_height <= 0
            or cli_width < 0
            or cli_height < 0
            or cli_left < win_left
            or cli_top < win_top
            or (cli_left + cli_width) > (win_left + win_width)
            or (cli_top + cli_height) > (win_top + win_height)
        ):
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase="read",
                reason="malformed_json",
            )

        ctx_epoch = (
            int(context["context_epoch"])
            if isinstance(context, Mapping) and type(context.get("context_epoch")) is int
            else 0
        )
        scope_rev = (
            int(context["scope_revision"])
            if isinstance(context, Mapping) and type(context.get("scope_revision")) is int
            else 0
        )

        entries: list[HostObservationArtifactEntry] = []
        public_items: list[dict[str, Any]] = []

        for idx, shot in enumerate(ok_shots, start=1):
            data_b64 = shot.get("dataBase64")
            if not isinstance(data_b64, str) or not data_b64.strip():
                raise NativeBridgeError(
                    "agent_computer_screenshot_invalid",
                    phase="read",
                    reason="malformed_json",
                )
            try:
                raw_bytes = base64.b64decode(data_b64.strip(), validate=True)
            except (binascii.Error, ValueError):
                raise NativeBridgeError(
                    "agent_computer_screenshot_invalid",
                    phase="read",
                    reason="malformed_json",
                ) from None

            try:
                verified = verify_original_image_bytes(
                    raw_bytes,
                    expected_mime="image/png",
                    max_bytes=MAX_NATIVE_IMAGE_SOURCE_BYTES,
                    max_pixels=MAX_IMAGE_PIXELS,
                    expected_width=int(shot["width"]),
                    expected_height=int(shot["height"]),
                    expected_sha256=str(shot["sha256"]).lower(),
                )
            except ValueError:
                raise NativeBridgeError(
                    "agent_computer_screenshot_invalid",
                    phase="read",
                    reason="malformed_json",
                ) from None

            shot_dpi = int(shot.get("dpi", dpi))
            shot_scale = float(shot.get("scaleFactor", geom_scale))
            if shot_dpi != dpi or abs(shot_scale - geom_scale) > 1e-3:
                raise NativeBridgeError(
                    "agent_computer_screenshot_invalid",
                    phase="read",
                    reason="malformed_json",
                )

            # Validate screenshot origin and bounds inside windowRect BEFORE writing any file
            raw_ox = int(shot["originX"]) - win_left
            raw_oy = int(shot["originY"]) - win_top
            if (
                raw_ox < 0
                or raw_oy < 0
                or raw_ox + verified["width"] > win_width
                or raw_oy + verified["height"] > win_height
            ):
                raise NativeBridgeError(
                    "agent_computer_screenshot_invalid",
                    phase="read",
                    reason="malformed_json",
                )

            phys_origin = [raw_ox, raw_oy]
            screen_origin = [
                win_left + phys_origin[0],
                win_top + phys_origin[1],
            ]

            try:
                sealed_desc = _issue_native_observation_descriptor(
                    task_id=task_id,
                    operation_id=operation_id,
                    observation_id=str(obs["observationId"]),
                    state_token=str(obs["stateToken"]),
                    window_ref=desc_wref,
                    source_sha256=verified["sha256"],
                    source_width=verified["width"],
                    source_height=verified["height"],
                    source_bytes=len(raw_bytes),
                    dpi=dpi,
                    dpi_scale=round(dpi / 96.0, 6),
                    window_rect={
                        "x": win_left,
                        "y": win_top,
                        "width": win_width,
                        "height": win_height,
                    },
                    client_rect={
                        "x": cli_left,
                        "y": cli_top,
                        "width": cli_width,
                        "height": cli_height,
                    },
                    physical_origin=phys_origin,
                    screen_origin=screen_origin,
                    representation={
                        "kind": "original",
                        "image_width": verified["width"],
                        "image_height": verified["height"],
                        "source_rect": {
                            "x": 0,
                            "y": 0,
                            "width": verified["width"],
                            "height": verified["height"],
                        },
                        "origin": [0, 0],
                        "scale": [1.0, 1.0],
                        "padding": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                        "payload_sha256": verified["sha256"],
                        "payload_bytes": len(raw_bytes),
                    },
                )
            except ValueError:
                raise NativeBridgeError(
                    "agent_computer_screenshot_invalid",
                    phase="read",
                    reason="malformed_json",
                ) from None

            rel_path = f".ryn/computer/{safe_task}/{safe_op}/image_{idx}.png"
            target_path = ws_resolved / ".ryn" / "computer" / safe_task / safe_op / f"image_{idx}.png"
            file_ident = exclusive_write_verified_png(
                target_path, raw_bytes, ws_resolved=ws_resolved
            )

            if self._local_tools is not None and hasattr(self._local_tools, "_record_observed_files") and isinstance(context, Mapping):
                try:
                    self._local_tools._record_observed_files(dict(context), {target_path: verified["sha256"]})
                except Exception:
                    pass

            rel_art = {
                "id": str(shot["id"]),
                "path": rel_path,
                "sha256": verified["sha256"],
                "bytes": len(raw_bytes),
                "mime_type": "image/png",
                "width": verified["width"],
                "height": verified["height"],
                "origin_x": int(shot["originX"]),
                "origin_y": int(shot["originY"]),
                "dpi": dpi,
                "scale_factor": geom_scale,
                "capture_method": str(shot["captureMethod"]),
                "status": "ok",
                "observation_id": str(obs["observationId"]),
                "state_token": str(obs["stateToken"]),
                "provenance": "native_computer_observation",
                "coordinate_authority": True,
            }
            entry = HostObservationArtifactEntry(
                artifact_id=str(shot["id"]),
                task_id=task_id,
                operation_id=operation_id,
                observation_id=str(obs["observationId"]),
                state_token=str(obs["stateToken"]),
                window_ref=wref,
                descriptor_window_ref=desc_wref,
                worker_instance=worker_instance,
                worker_generation=worker_generation,
                workspace_root=str(ws_resolved),
                relative_artifact=rel_art,
                sealed_descriptor=sealed_desc,
                file_identity=file_ident,
                context_epoch=ctx_epoch,
                scope_revision=scope_rev,
            )
            entries.append(entry)
            public_items.append(
                {
                    **copy.deepcopy(rel_art),
                    "window_ref": copy.deepcopy(wref),
                    "native_descriptor": copy.deepcopy(sealed_desc),
                }
            )
        return entries, public_items

    def _ingest_observation(
        self,
        *,
        task_id: str,
        operation_id: str,
        obs: Mapping[str, Any],
        window_meta: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None,
    ) -> tuple[WindowObservationRecord, list[dict[str, Any]]]:
        gen, worker_inst, _ = self.bridge._client.active_session_info()
        wref = validate_window_ref(dict(obs["window"]), phase="read")
        eff_worker = worker_inst or str(wref["workerInstance"])
        eff_gen = gen if gen is not None else int(wref["generation"])

        artifact_entries, public_artifacts = self._produce_observation_screenshots(
            task_id=task_id,
            operation_id=operation_id,
            obs=obs,
            worker_instance=eff_worker,
            worker_generation=eff_gen,
            context=context,
        )

        hwnd_int = int(wref["hwnd"], 10)
        elements_out: list[dict[str, Any]] = []
        by_index: dict[int, dict[str, Any]] = {}
        by_sky_id: dict[str, dict[str, Any]] = {}
        by_runtime_id: dict[str, dict[str, Any]] = {}

        for raw_el in obs["accessibility"]["elements"]:
            idx = int(raw_el["index"])
            sky_el_id = f"el-{idx}"
            b = raw_el["bounds"]
            cx = int(b["left"]) + int(b["width"]) // 2
            cy = int(b["top"]) + int(b["height"]) // 2
            is_pwd = bool(raw_el["password"])
            el_item: dict[str, Any] = {
                "id": sky_el_id,
                "element_id": idx,
                "sky_element_id": sky_el_id,
                "index": idx,
                "runtime_id": str(raw_el["runtimeId"]),
                "name": str(raw_el["name"]),
                "type": str(raw_el["controlType"]),
                "control_type": str(raw_el["controlType"]),
                "automation_id": str(raw_el.get("automationId") or ""),
                "class_name": str(raw_el.get("className") or ""),
                "password": is_pwd,
                "enabled": bool(raw_el["enabled"]),
                "offscreen": bool(raw_el["offscreen"]),
                "focusable": bool(raw_el["focusable"]),
                "focused": bool(raw_el["focused"]),
                "value": "" if is_pwd else str(raw_el.get("value") or ""),
                "x": cx,
                "y": cy,
                "bounds": copy.deepcopy(dict(b)),
                "depth": int(raw_el["depth"]),
                "patterns": list(raw_el.get("patterns") or []),
            }
            elements_out.append(el_item)
            by_index[idx] = el_item
            by_sky_id[sky_el_id] = el_item
            by_runtime_id[str(raw_el["runtimeId"])] = el_item

        ctx_epoch = (
            int(context["context_epoch"])
            if isinstance(context, Mapping) and type(context.get("context_epoch")) is int
            else 0
        )
        scope_rev = (
            int(context["scope_revision"])
            if isinstance(context, Mapping) and type(context.get("scope_revision")) is int
            else 0
        )

        evicted_files: list[tuple[Path, VerifiedFileIdentity | None, Path]] = []
        with self._lock:
            tstate = self._get_task_state_locked(
                task_id,
                worker_instance=eff_worker,
                worker_generation=eff_gen,
                context=context,
            )
            sky_win_id = tstate.assign_sky_id(wref)
            saved_win = tstate.windows_by_hwnd.get(hwnd_int) or {}
            title = str(
                (window_meta or {}).get("title")
                if window_meta is not None and "title" in window_meta
                else saved_win.get("title", "")
            )
            class_name = str(
                (window_meta or {}).get("className")
                if window_meta is not None and "className" in window_meta
                else saved_win.get("className", "")
            )
            process_name = str(
                (window_meta or {}).get("processName")
                if window_meta is not None and "processName" in window_meta
                else saved_win.get("processName", "")
            )
            win_entry = {
                "window": copy.deepcopy(wref),
                "sky_window_id": sky_win_id,
                "title": title,
                "className": class_name,
                "processName": process_name,
                "visible": bool(saved_win.get("visible", True)),
                "enabled": bool(saved_win.get("enabled", True)),
                "minimized": bool(saved_win.get("minimized", False)),
                "maximized": bool(saved_win.get("maximized", False)),
                "foreground": bool(obs["foreground"]["isTargetWindow"]),
                "cloaked": bool(saved_win.get("cloaked", False)),
                "captureProtected": bool(saved_win.get("captureProtected", False)),
                "bounds": copy.deepcopy(dict(obs["geometry"]["windowRect"])),
                "clientBounds": copy.deepcopy(dict(obs["geometry"]["clientRect"])),
                "dpi": int(obs["geometry"]["dpi"]),
            }
            tstate.windows_by_hwnd[hwnd_int] = win_entry
            tstate.windows_by_hwnd.move_to_end(hwnd_int)
            tstate.windows_by_key[wref["windowKey"]] = win_entry
            tstate.windows_by_key.move_to_end(wref["windowKey"])

            obs_record = WindowObservationRecord(
                task_id=task_id,
                hwnd=hwnd_int,
                sky_window_id=sky_win_id,
                window_ref=copy.deepcopy(wref),
                window_title=title,
                class_name=class_name,
                process_name=process_name,
                observation_id=str(obs["observationId"]),
                state_token=str(obs["stateToken"]),
                captured_at=str(obs["capturedAt"]),
                geometry=copy.deepcopy(dict(obs["geometry"])),
                foreground=copy.deepcopy(dict(obs["foreground"])),
                accessibility={
                    "status": str(obs["accessibility"]["status"]),
                    "treeHash": str(obs["accessibility"]["treeHash"]),
                    "truncated": bool(obs["accessibility"]["truncated"]),
                    "selectedElementIndices": list(obs["accessibility"]["selectedElementIndices"]),
                    **(
                        {"focusedElementIndex": int(obs["accessibility"]["focusedElementIndex"])}
                        if obs["accessibility"].get("focusedElementIndex") is not None
                        else {}
                    ),
                    **(
                        {"focusedRuntimeId": str(obs["accessibility"]["focusedRuntimeId"])}
                        if obs["accessibility"].get("focusedRuntimeId") is not None
                        else {}
                    ),
                    **(
                        {"selectedText": str(obs["accessibility"]["selectedText"])}
                        if obs["accessibility"].get("selectedText") is not None
                        else {}
                    ),
                },
                elements=elements_out,
                elements_by_index=by_index,
                elements_by_sky_id=by_sky_id,
                elements_by_runtime_id=by_runtime_id,
                worker_instance=eff_worker,
                worker_generation=eff_gen,
                context_epoch=ctx_epoch,
                scope_revision=scope_rev,
                artifact_ids=[a.artifact_id for a in artifact_entries],
            )
            evicted_files = tstate.record_observation(obs_record, artifact_entries)

        for target_p, f_ident, ws_p in evicted_files:
            safe_unlink_verified_file(target_p, f_ident, ws_resolved=ws_p)

        return obs_record, public_artifacts

    def verify_and_load_registered_artifact(
        self,
        *,
        task_id: str,
        artifact_id: str,
        expected_operation_id: str | None = None,
        expected_state_token: str | None = None,
        expected_observation_id: str | None = None,
        expected_window_ref: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Host-only artifact verifier: resolve from host registry and verify sealed descriptor + file bytes."""
        with self._lock:
            existing_bridge = self._peek_bridge_locked()
            if existing_bridge is None:
                raise ValueError("native_descriptor_worker_mismatch")
            gen, worker_inst, _ = existing_bridge._client.active_session_info()
            if gen is None or worker_inst is None:
                raise ValueError("native_descriptor_worker_mismatch")
            tstate = self._tasks.get(task_id)
            if tstate is None:
                raise ValueError("native_artifact_task_not_found")
            entry = tstate.artifacts_by_id.get(artifact_id)
            if entry is None:
                raise ValueError("native_artifact_not_registered")
            if entry.retired:
                raise ValueError("native_descriptor_stale_state_token")
            if entry.worker_instance != worker_inst or entry.worker_generation != gen:
                raise ValueError("native_descriptor_worker_mismatch")
            if (
                tstate.worker_instance is not None
                and tstate.worker_instance != worker_inst
            ) or (
                tstate.worker_generation is not None
                and tstate.worker_generation != gen
            ):
                raise ValueError("native_descriptor_worker_mismatch")
            if (
                tstate.context_epoch is not None
                and entry.context_epoch != tstate.context_epoch
            ) or (
                tstate.scope_revision is not None
                and entry.scope_revision != tstate.scope_revision
            ):
                raise ValueError("native_descriptor_stale_state_token")
            hwnd_int = int(entry.window_ref["hwnd"], 10)
            current_obs = tstate.observations_by_hwnd.get(hwnd_int)
            if (
                current_obs is None
                or current_obs.state_token != entry.state_token
                or current_obs.observation_id != entry.observation_id
                or not window_refs_equal(current_obs.window_ref, entry.window_ref)
            ):
                raise ValueError("native_descriptor_stale_state_token")
            if expected_operation_id is not None and entry.operation_id != expected_operation_id:
                raise ValueError("native_descriptor_operation_mismatch")
            if expected_state_token is not None and entry.state_token != expected_state_token:
                raise ValueError("native_descriptor_stale_state_token")
            if expected_observation_id is not None and entry.observation_id != expected_observation_id:
                raise ValueError("native_descriptor_observation_mismatch")
            if expected_window_ref is not None and not window_refs_equal(
                entry.window_ref, dict(expected_window_ref)
            ):
                raise ValueError("native_descriptor_window_mismatch")

            rel_art = copy.deepcopy(entry.relative_artifact)
            sealed_desc = copy.deepcopy(entry.sealed_descriptor)
            ws_root = entry.workspace_root
            op_id = entry.operation_id
            obs_id = entry.observation_id
            tok = entry.state_token
            desc_wref = copy.deepcopy(entry.descriptor_window_ref)
            w_inst = entry.worker_instance
            w_gen = entry.worker_generation

        loaded = verify_and_load_native_observation_artifact(
            rel_art,
            descriptor=sealed_desc,
            workspace_root=ws_root,
            task_id=task_id,
            operation_id=op_id,
            current_state_token=tok,
            expected_window_ref=desc_wref,
            expected_observation_id=obs_id,
            expected_worker_instance_id=w_inst,
        )

        # Re-verify under lock after unlocked file IO that the artifact and worker session were not retired concurrently
        with self._lock:
            bridge_after = self._peek_bridge_locked()
            if bridge_after is not existing_bridge or bridge_after is None:
                raise ValueError("native_descriptor_worker_mismatch")
            gen_after, inst_after, _ = bridge_after._client.active_session_info()
            if gen_after != w_gen or inst_after != w_inst:
                raise ValueError("native_descriptor_worker_mismatch")
            tstate_after = self._tasks.get(task_id)
            if tstate_after is not tstate:
                raise ValueError("native_descriptor_stale_state_token")
            entry_after = tstate_after.artifacts_by_id.get(artifact_id)
            if entry_after is not entry or entry.retired:
                raise ValueError("native_descriptor_stale_state_token")
            obs_after = tstate_after.observations_by_hwnd.get(hwnd_int)
            if (
                obs_after is not current_obs
                or obs_after.state_token != tok
                or obs_after.observation_id != obs_id
                or not window_refs_equal(obs_after.window_ref, entry.window_ref)
            ):
                raise ValueError("native_descriptor_stale_state_token")
            if (
                tstate_after.context_epoch is not None
                and entry.context_epoch != tstate_after.context_epoch
            ) or (
                tstate_after.scope_revision is not None
                and entry.scope_revision != tstate_after.scope_revision
            ):
                raise ValueError("native_descriptor_stale_state_token")

        loaded["artifact_id"] = artifact_id
        loaded["full_window_ref"] = copy.deepcopy(entry.window_ref)
        return loaded

    def _require_observed_window_ref(
        self,
        task_id: str,
        hwnd: int,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve a bare integer HWND strictly from this task's previously observed WindowRefs."""
        if type(hwnd) is not int or hwnd < 1:
            raise AgentToolRejected("agent_tool_arguments_invalid")
        gen, worker_inst, _ = self.bridge._client.active_session_info()
        with self._lock:
            tstate = self._get_task_state_locked(
                task_id,
                worker_instance=worker_inst,
                worker_generation=gen,
                context=context,
            )
            win_entry = tstate.windows_by_hwnd.get(hwnd)
            if win_entry is None:
                raise AgentToolRejected("agent_computer_window_changed_read_again")
            wref = copy.deepcopy(win_entry["window"])
            if worker_inst is not None and wref.get("workerInstance") != worker_inst:
                raise AgentToolRejected("agent_computer_window_changed_read_again")
            return copy.deepcopy(win_entry)

    def _require_observed_window_state(
        self,
        task_id: str,
        hwnd: int,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> WindowObservationRecord:
        """Require a current-generation `state.get` observation for `(task_id, hwnd)`."""
        if type(hwnd) is not int or hwnd < 1:
            raise AgentToolRejected("agent_tool_arguments_invalid")
        gen, worker_inst, _ = self.bridge._client.active_session_info()
        with self._lock:
            tstate = self._get_task_state_locked(
                task_id,
                worker_instance=worker_inst,
                worker_generation=gen,
                context=context,
            )
            obs_rec = tstate.observations_by_hwnd.get(hwnd)
            if obs_rec is None:
                raise AgentToolRejected("agent_computer_window_changed_read_again")
            if worker_inst is not None and obs_rec.window_ref.get("workerInstance") != worker_inst:
                raise AgentToolRejected("agent_computer_window_changed_read_again")
            if gen is not None and obs_rec.worker_generation != gen:
                raise AgentToolRejected("agent_computer_window_changed_read_again")
            return obs_rec

    def _resolve_element_in_observation(
        self,
        obs_rec: WindowObservationRecord,
        raw_element_id: Any,
        *,
        forbid_password: bool = True,
        legacy_local: bool = False,
    ) -> dict[str, Any]:
        if raw_element_id is None:
            raise AgentToolRejected("agent_computer_element_required")
        el: dict[str, Any] | None = None
        if type(raw_element_id) is int:
            if raw_element_id < 0:
                raise AgentToolRejected("agent_computer_element_required")
            el = obs_rec.elements_by_index.get(raw_element_id)
        elif isinstance(raw_element_id, str):
            cleaned = raw_element_id.strip()
            if not cleaned or cleaned.lower().startswith("hwnd:"):
                raise AgentToolRejected("agent_computer_element_required")
            el = obs_rec.elements_by_sky_id.get(cleaned)
            if el is None and cleaned.isdigit():
                el = obs_rec.elements_by_index.get(int(cleaned, 10))
            if el is None:
                el = obs_rec.elements_by_runtime_id.get(cleaned)
        else:
            raise AgentToolRejected("agent_computer_element_required")

        if el is None:
            raise AgentToolRejected("agent_computer_element_required")
        if forbid_password and el.get("password"):
            raise AgentToolRejected(
                "agent_computer_element_required"
                if legacy_local
                else "agent_computer_password_field"
            )
        return copy.deepcopy(el)

    def _screen_point_to_window_physical(
        self,
        obs_rec: WindowObservationRecord,
        screen_x: Any,
        screen_y: Any,
    ) -> dict[str, Any]:
        """Convert absolute screen physical coordinates ONCE to `window_physical_px` without double DPI scaling."""
        if type(screen_x) is not int or type(screen_y) is not int:
            raise AgentToolRejected("agent_computer_click_outside_window")
        win_rect = obs_rec.geometry["windowRect"]
        left = int(win_rect["left"])
        top = int(win_rect["top"])
        right = int(win_rect["right"])
        bottom = int(win_rect["bottom"])
        if not (left <= screen_x < right and top <= screen_y < bottom):
            raise AgentToolRejected("agent_computer_click_outside_window")
        return {
            "coordinateSpace": "window_physical_px",
            "x": screen_x - left,
            "y": screen_y - top,
        }

    def _build_action_plan(
        self,
        *,
        task_id: str,
        hwnd: int,
        action_name: str,
        arguments: Mapping[str, Any],
        context: Mapping[str, Any] | None,
        legacy_local: bool,
    ) -> dict[str, Any]:
        if action_name == "close":
            raise AgentToolRejected("agent_computer_action_unsupported")
        if arguments.get("clear") is True:
            raise AgentToolRejected("agent_computer_action_unsupported")

        try:
            obs_rec = self._require_observed_window_state(task_id, hwnd, context=context)
        except AgentToolRejected:
            if legacy_local and action_name != "focus":
                raise
            # Any window already enumerated in this task via computer_observe_windows
            # can acquire its initial state token on demand before preview; activation
            # still goes through the normal authorized prepare/commit path.
            entry = self._require_observed_window_ref(task_id, hwnd, context=context)
            try:
                need_elements = bool(arguments.get("element_id") is not None)
                observation = self.bridge.get_state(
                    task_session_id=task_id,
                    window=entry["window"],
                    include_screenshot=False,
                    include_text=need_elements,
                )
                obs_rec, _ = self._ingest_observation(
                    task_id=task_id,
                    operation_id="focus_preview",
                    obs=observation,
                    window_meta=entry,
                    context=context,
                )
            except NativeBridgeError as exc:
                self._translate_bridge_exception(exc, is_write=False)
        win_rect = obs_rec.geometry["windowRect"]
        rect_list = [
            int(win_rect["left"]),
            int(win_rect["top"]),
            int(win_rect["right"]),
            int(win_rect["bottom"]),
        ]

        element_info: dict[str, Any] | None = None
        raw_action: dict[str, Any]
        effective_action = (
            "type"
            if (
                action_name == "click"
                and not legacy_local
                and isinstance(arguments.get("text"), str)
                and bool(arguments.get("text"))
            )
            else action_name
        )

        if effective_action == "focus":
            raw_action = {
                "type": "focus_window",
                "restoreIfMinimized": True,
            }
        elif effective_action == "click":
            has_xy = "x" in arguments or "y" in arguments
            has_el = arguments.get("element_id") is not None
            if has_xy and has_el:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            if has_el and not legacy_local:
                element_info = self._resolve_element_in_observation(
                    obs_rec,
                    arguments.get("element_id"),
                    forbid_password=False,
                    legacy_local=legacy_local,
                )
                raw_action = {
                    "type": "click",
                    "button": "left",
                    "target": None,
                    "element": {
                        "index": int(element_info["index"]),
                        "runtimeId": str(element_info["runtime_id"]),
                    },
                }
            else:
                if "x" not in arguments or "y" not in arguments:
                    raise AgentToolRejected("agent_computer_click_outside_window")
                pt = self._screen_point_to_window_physical(
                    obs_rec, arguments.get("x"), arguments.get("y")
                )
                raw_action = {
                    "type": "click",
                    "button": "left",
                    "target": pt,
                    "element": None,
                }
        elif effective_action == "type":
            text = arguments.get("text")
            if not isinstance(text, str) or not text:
                raise AgentToolRejected("agent_computer_text_required")
            has_xy = "x" in arguments or "y" in arguments
            has_el = arguments.get("element_id") is not None
            if has_xy and has_el and not legacy_local:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            target_pt: dict[str, Any] | None = None
            target_el: dict[str, Any] | None = None
            if has_el and not legacy_local:
                element_info = self._resolve_element_in_observation(
                    obs_rec,
                    arguments.get("element_id"),
                    forbid_password=True,
                    legacy_local=legacy_local,
                )
                target_el = {
                    "index": int(element_info["index"]),
                    "runtimeId": str(element_info["runtime_id"]),
                }
            elif has_xy and not legacy_local:
                target_pt = self._screen_point_to_window_physical(
                    obs_rec, arguments.get("x"), arguments.get("y")
                )
            raw_action = {
                "type": "type_text",
                "text": text,
                "target": target_pt,
                "element": target_el,
            }
        elif action_name == "key":
            if legacy_local:
                raw_keys = arguments.get("keys")
                if not isinstance(raw_keys, list) or not raw_keys:
                    raise AgentToolRejected("agent_computer_keys_required")
                norm_keys = [normalize_key_token(k) for k in raw_keys]
            else:
                raw_key_str = arguments.get("key")
                if not isinstance(raw_key_str, str) or not raw_key_str.strip():
                    raise AgentToolRejected("agent_computer_keys_required")
                parts = [p.strip() for p in raw_key_str.split("+") if p.strip()]
                if not parts:
                    raise AgentToolRejected("agent_computer_keys_required")
                norm_keys = [normalize_key_token(p) for p in parts]
            if len(norm_keys) == 1:
                raw_action = {
                    "type": "key_press",
                    "key": norm_keys[0],
                    "repeat": 1,
                }
            else:
                raw_action = {
                    "type": "key_chord",
                    "keys": norm_keys,
                }
        elif action_name == "invoke":
            element_info = self._resolve_element_in_observation(
                obs_rec,
                arguments.get("element_id"),
                forbid_password=True,
                legacy_local=legacy_local,
            )
            raw_action = {
                "type": "uia_invoke",
                "element": {
                    "index": int(element_info["index"]),
                    "runtimeId": str(element_info["runtime_id"]),
                },
            }
        elif action_name == "set_value":
            text = arguments.get("text")
            if not isinstance(text, str):
                raise AgentToolRejected("agent_computer_text_required")
            element_info = self._resolve_element_in_observation(
                obs_rec,
                arguments.get("element_id"),
                forbid_password=True,
                legacy_local=legacy_local,
            )
            raw_action = {
                "type": "uia_set_value",
                "element": {
                    "index": int(element_info["index"]),
                    "runtimeId": str(element_info["runtime_id"]),
                },
                "value": text,
            }
        elif action_name == "scroll":
            delta_y = arguments.get("delta_y", -3)
            if type(delta_y) is not int or delta_y == 0:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            if "x" in arguments or "y" in arguments:
                pt = self._screen_point_to_window_physical(
                    obs_rec, arguments.get("x"), arguments.get("y")
                )
            else:
                pt = {
                    "coordinateSpace": "window_physical_px",
                    "x": max(0, int(win_rect["width"]) // 2),
                    "y": max(0, int(win_rect["height"]) // 2),
                }
            raw_action = {
                "type": "scroll",
                "target": pt,
                "deltaX": 0,
                "deltaY": int(delta_y) * 120,
            }
        else:
            raise AgentToolRejected("agent_computer_action_unsupported")

        follow_up_action: dict[str, Any] | None = None
        if (
            not legacy_local
            and action_name in {"type", "click", "set_value"}
            and arguments.get("key") is not None
        ):
            raw_key_str = arguments.get("key")
            if not isinstance(raw_key_str, str) or not raw_key_str.strip():
                raise AgentToolRejected("agent_computer_keys_required")
            parts = [p.strip() for p in raw_key_str.split("+") if p.strip()]
            if not parts:
                raise AgentToolRejected("agent_computer_keys_required")
            norm_keys = [normalize_key_token(p) for p in parts]
            follow_up_raw = (
                {"type": "key_press", "key": norm_keys[0], "repeat": 1}
                if len(norm_keys) == 1
                else {"type": "key_chord", "keys": norm_keys}
            )
            follow_up_action = normalize_action_spec(follow_up_raw, phase="pre_dispatch")

        normalized_action = normalize_action_spec(raw_action, phase="pre_dispatch")
        fp = compute_action_fingerprint(
            task_id,
            obs_rec.window_ref,
            obs_rec.state_token,
            normalized_action,
        )
        plan_out = {
            "task_id": task_id,
            "hwnd": hwnd,
            "sky_window_id": obs_rec.sky_window_id,
            "window_ref": copy.deepcopy(obs_rec.window_ref),
            "window_title": obs_rec.window_title,
            "window_pid": int(obs_rec.window_ref["pid"]),
            "window_rect": rect_list,
            "expected_state_token": obs_rec.state_token,
            "observation_id": obs_rec.observation_id,
            "native_action": normalized_action,
            "action_fingerprint": fp,
            "element": copy.deepcopy(element_info) if element_info is not None else None,
        }
        if follow_up_action is not None:
            plan_out["follow_up_action"] = follow_up_action
        return plan_out

    # =========================================================================
    # Builtin MCP `computer-use` Entry Points
    # =========================================================================

    def execute_mcp_read(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        binding: Mapping[str, Any],
        operation_id: str | None,
        context: Mapping[str, Any],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        if cancelled():
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
        task_id = str(context.get("task_id") or "").strip()
        if not task_id:
            raise AgentError("agent_scope_context_required")
        eff_op_id = str(operation_id or context.get("operation_id") or "op_read").strip() or "op_read"
        self.overlay.touch(task_id, "正在查看屏幕")

        if tool_name == "computer_observe_windows":
            if set(arguments.keys()) - {"query", "limit"}:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            query = arguments.get("query")
            if query is not None and (not isinstance(query, str) or len(query) > 160):
                raise AgentToolRejected("agent_tool_arguments_invalid")
            limit = arguments.get("limit", 50)
            if type(limit) is not int or not (1 <= limit <= 50):
                raise AgentToolRejected("agent_tool_arguments_invalid")

            try:
                raw_list = self.bridge.list_windows(
                    task_session_id=task_id,
                    include_minimized=True,
                    max_windows=50,
                    cancelled=cancelled,
                )
            except NativeBridgeError as exc:
                self._translate_bridge_exception(exc, is_write=False)

            gen, worker_inst, _ = self.bridge._client.active_session_info()
            q_lower = query.strip().lower() if isinstance(query, str) and query.strip() else ""
            windows_out: list[dict[str, Any]] = []
            with self._lock:
                tstate = self._get_task_state_locked(
                    task_id,
                    worker_instance=worker_inst,
                    worker_generation=gen,
                    context=context,
                )
                for entry in raw_list["windows"]:
                    sky_id, stored = tstate.record_window_entry(entry)
                    if q_lower:
                        haystack = f"{stored['title']} {stored['processName']} {stored['className']}".lower()
                        if q_lower not in haystack:
                            continue
                    if len(windows_out) >= limit:
                        continue
                    wref = stored["window"]
                    b = stored["bounds"]
                    windows_out.append(
                        {
                            "hwnd": int(wref["hwnd"], 10),
                            "window_id": int(wref["hwnd"], 10),
                            "sky_window_id": sky_id,
                            "window_ref": copy.deepcopy(wref),
                            "title": stored["title"],
                            "class_name": stored["className"],
                            "process_name": stored["processName"],
                            "pid": int(wref["pid"]),
                            "visible": bool(stored["visible"]),
                            "enabled": bool(stored["enabled"]),
                            "minimized": bool(stored["minimized"]),
                            "maximized": bool(stored["maximized"]),
                            "foreground": bool(stored["foreground"]),
                            "rect": [int(b["left"]), int(b["top"]), int(b["right"]), int(b["bottom"])],
                            "bounds": copy.deepcopy(b),
                            "client_bounds": copy.deepcopy(stored["clientBounds"]),
                            "dpi": int(stored["dpi"]),
                        }
                    )

            windows_out.sort(
                key=lambda w: (
                    not w.get("foreground", False),
                    bool(w.get("minimized", False)),
                    not bool(str(w.get("title") or "").strip()),
                )
            )
            fg_wref = raw_list.get("foregroundWindow")
            structured = {
                "captured_at": raw_list["capturedAt"],
                "windows": windows_out,
                "count": len(windows_out),
                "foreground_hwnd": int(fg_wref["hwnd"], 10) if isinstance(fg_wref, Mapping) else None,
            }
            summary_lines = [
                f"- hwnd={w['hwnd']} title={w['title']!r} proc={w['process_name']} rect={w['rect']}"
                + (" [foreground]" if w.get("foreground") else "")
                + (" [minimized]" if w.get("minimized") else "")
                for w in windows_out[:25]
            ]
            summary_block = ("\n".join(summary_lines) + "\n") if summary_lines else ""
            txt = (
                f"Observed {len(windows_out)} windows.\n{summary_block}"
                "Next step: call computer_read_window(hwnd=..., include_screenshot=true) on the target hwnd to inspect "
                "its controls and screenshot (works even on background windows; do NOT call action='focus' first). "
                "When acting with computer_act_window, every click/type/key action automatically activates the window, "
                "and you can combine clicking a field, typing text, and pressing a key (e.g. key='Enter') in ONE "
                "computer_act_window call by passing action='type' with x, y (or element_id), text, and optional key. "
                "Successful actions return a fresh observation and screenshot; use that state directly without a redundant read."
            )
            return {
                "server_id": str(binding["server_id"]),
                "tool": str(binding["original_tool_name"]),
                "model_tool_name": str(binding["model_tool_name"]),
                "is_error": False,
                "content": [{"type": "text", "text": txt}],
                "text": txt,
                "structuredContent": structured,
            }

        if tool_name == "computer_read_window":
            if set(arguments.keys()) - {"hwnd", "include_screenshot"} or "hwnd" not in arguments:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            hwnd = arguments.get("hwnd")
            if type(hwnd) is not int or hwnd < 1:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            inc_shot = arguments.get("include_screenshot", True)
            if type(inc_shot) is not bool:
                raise AgentToolRejected("agent_tool_arguments_invalid")

            win_entry = self._require_observed_window_ref(task_id, hwnd, context=context)
            wref = win_entry["window"]

            try:
                obs = self.bridge.get_state(
                    task_session_id=task_id,
                    window=wref,
                    include_screenshot=inc_shot,
                    include_text=True,
                    cancelled=cancelled,
                )
                obs_record, public_artifacts = self._ingest_observation(
                    task_id=task_id,
                    operation_id=eff_op_id,
                    obs=obs,
                    window_meta=win_entry,
                    context=context,
                )
            except NativeBridgeError as exc:
                self._translate_bridge_exception(exc, is_write=False)

            win_rect = obs_record.geometry["windowRect"]
            structured = {
                "hwnd": obs_record.hwnd,
                "window_id": obs_record.hwnd,
                "sky_window_id": obs_record.sky_window_id,
                "window_ref": copy.deepcopy(obs_record.window_ref),
                "title": obs_record.window_title,
                "class_name": obs_record.class_name,
                "process_name": obs_record.process_name,
                "pid": int(obs_record.window_ref["pid"]),
                "observation_id": obs_record.observation_id,
                "state_token": obs_record.state_token,
                "captured_at": obs_record.captured_at,
                "rect": [
                    int(win_rect["left"]),
                    int(win_rect["top"]),
                    int(win_rect["right"]),
                    int(win_rect["bottom"]),
                ],
                "geometry": copy.deepcopy(obs_record.geometry),
                "foreground": copy.deepcopy(obs_record.foreground),
                "accessibility": copy.deepcopy(obs_record.accessibility),
                # Full runtime IDs and UIA metadata stay in the trusted observation.
                # The model needs actionable IDs, labels, state and coordinates.
                "elements": [_compact_public_element(element) for element in obs_record.elements],
                "element_count": len(obs_record.elements),
                "incomplete": bool(obs_record.accessibility.get("truncated", False)),
            }
            if public_artifacts:
                structured["screenshots"] = [
                    {k: v for k, v in item.items() if k != "native_descriptor"}
                    for item in public_artifacts
                ]

            txt = f"Read window {obs_record.hwnd}: {obs_record.window_title[:48]}. Controls are in structuredContent."
            content_blocks: list[dict[str, Any]] = [{"type": "text", "text": txt}]
            for art in public_artifacts:
                content_blocks.append(
                    {
                        "type": "image",
                        "id": art["id"],
                        "path": art["path"],
                        "sha256": art["sha256"],
                        "bytes": art["bytes"],
                        "mime_type": art["mime_type"],
                        "width": art["width"],
                        "height": art["height"],
                        "provenance": "native_computer_observation",
                        "coordinate_authority": True,
                    }
                )
            out: dict[str, Any] = {
                "server_id": str(binding["server_id"]),
                "tool": str(binding["original_tool_name"]),
                "model_tool_name": str(binding["model_tool_name"]),
                "is_error": False,
                "content": content_blocks,
                "text": txt,
                "structuredContent": structured,
            }
            if public_artifacts:
                out["native_observation_artifacts"] = copy.deepcopy(public_artifacts)
                out["images"] = [
                    {k: v for k, v in item.items() if k != "native_descriptor"}
                    for item in public_artifacts
                ]
                out["image_path"] = public_artifacts[0]["path"]
            return out

        raise AgentToolRejected("agent_unknown_tool")

    def preview_mcp_write(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        binding: Mapping[str, Any],
        policy: Mapping[str, Any],
        server: Mapping[str, Any],
        tool: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        if tool_name != "computer_act_window":
            raise AgentToolRejected("agent_unknown_tool")
        task_id = str(context.get("task_id") or "").strip()
        if not task_id:
            raise AgentError("agent_scope_context_required")
        hwnd = arguments.get("hwnd")
        action_name = arguments.get("action")
        if type(hwnd) is not int or hwnd < 1 or not isinstance(action_name, str):
            raise AgentToolRejected("agent_tool_arguments_invalid")

        plan = self._build_action_plan(
            task_id=task_id,
            hwnd=hwnd,
            action_name=action_name,
            arguments=arguments,
            context=context,
            legacy_local=False,
        )
        preview_out: dict[str, Any] = {
            "title": f"{server.get('name') or server['id']}: {tool.get('title') or tool.get('name')}",
            "server_id": str(binding["server_id"]),
            "server_name": str(binding["server_name"]),
            "tool_name": str(binding["original_tool_name"]),
            "model_tool_name": str(binding["model_tool_name"]),
            "workspace_root": str(policy.get("workspace_root", "")),
            "desktop_access": bool(policy.get("desktop_access", False)),
            "computer_access": bool(binding["computer_access"]),
            "operation": copy.deepcopy(dict(arguments)),
            "window_title": plan["window_title"],
            "window_pid": plan["window_pid"],
            "window_rect": plan["window_rect"],
            "window_ref": plan["window_ref"],
            "expected_state_token": plan["expected_state_token"],
            "observation_id": plan["observation_id"],
            "native_action": plan["native_action"],
            "action_fingerprint": plan["action_fingerprint"],
            "extension_binding": copy.deepcopy(dict(binding)),
        }
        if plan["element"] is not None:
            preview_out["element"] = {("is_password" if k == "password" else k): v
                                      for k, v in plan["element"].items()}
        if plan.get("follow_up_action") is not None:
            preview_out["follow_up_action"] = copy.deepcopy(plan["follow_up_action"])
        return preview_out

    def execute_mcp_write(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        binding: Mapping[str, Any],
        operation_id: str,
        approved_preview: Mapping[str, Any],
        context: Mapping[str, Any],
        cancelled: Callable[[], bool],
        on_ready_host_gate: Callable[[dict[str, Any]], None],
        verify_binding_fn: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        if tool_name != "computer_act_window":
            raise AgentToolRejected("agent_unknown_tool")
        task_id = str(context.get("task_id") or "").strip()
        if not task_id:
            raise AgentError("agent_scope_context_required")

        stamp = self.capture_trusted_write_authority(
            task_id=task_id,
            operation_id=operation_id,
            tool_name=tool_name,
            arguments=arguments,
            context=context,
            approved_preview=approved_preview,
            binding=binding,
            verify_binding_fn=verify_binding_fn,
            cancelled=cancelled,
        )
        frozen_wref = copy.deepcopy(stamp.frozen_window_ref)
        self.overlay.touch(task_id, "正在操作窗口")
        frozen_token = stamp.frozen_state_token
        frozen_action = copy.deepcopy(dict(approved_preview["native_action"]))
        hwnd = stamp.frozen_hwnd

        def _on_ready(validated_ready: dict[str, Any]) -> bool:
            self.verify_ready_write_authority(
                stamp,
                validated_ready=validated_ready,
                context=context,
                cancelled=cancelled,
                verify_binding_fn=verify_binding_fn,
            )
            on_ready_host_gate(validated_ready)
            return True

        try:
            commit_res = self.bridge.execute_action(
                task_session_id=task_id,
                operation_id=operation_id,
                window=frozen_wref,
                expected_state_token=frozen_token,
                action=frozen_action,
                on_ready=_on_ready,
                cancelled=cancelled,
                post_observation={"enabled": True, "settleDelayMs": 40},
            )
        except NativeBridgeError as exc:
            self._translate_bridge_exception(exc, is_write=True)

        follow_up_spec = approved_preview.get("follow_up_action")
        if (
            isinstance(follow_up_spec, Mapping)
            and isinstance(commit_res.get("observation"), Mapping)
            and commit_res["observation"].get("stateToken")
            and not cancelled()
        ):
            next_token = str(commit_res["observation"]["stateToken"])
            try:
                follow_res = self.bridge.execute_action(
                    task_session_id=task_id,
                    operation_id=f"{operation_id}_followup",
                    window=frozen_wref,
                    expected_state_token=next_token,
                    action=copy.deepcopy(dict(follow_up_spec)),
                    on_ready=lambda _r: not cancelled(),
                    cancelled=cancelled,
                    post_observation={"enabled": True, "settleDelayMs": 40},
                )
                if isinstance(follow_res.get("observation"), Mapping):
                    commit_res["observation"] = follow_res["observation"]
            except NativeBridgeError:
                pass

        with self._lock:
            tstate = self._tasks.get(task_id)
            if tstate is not None:
                prev_obs = tstate.observations_by_hwnd.pop(hwnd, None)
                if prev_obs is not None:
                    tstate.observations_by_token.pop(prev_obs.state_token, None)
                    for aid in prev_obs.artifact_ids:
                        if aid in tstate.artifacts_by_id:
                            tstate.artifacts_by_id[aid].retired = True

        post_artifacts: list[dict[str, Any]] = []
        post_record: WindowObservationRecord | None = None
        if isinstance(commit_res.get("observation"), Mapping):
            try:
                post_record, post_artifacts = self._ingest_observation(
                    task_id=task_id,
                    operation_id=operation_id,
                    obs=commit_res["observation"],
                    window_meta={"title": approved_preview.get("window_title", "")},
                    context=context,
                )
            except (NativeBridgeError, OSError):
                # The native receipt already proves input delivery. A failed
                # follow-up capture/file write must not turn it into a retry.
                post_artifacts = []

        structured = {
            "status": str(commit_res["status"]),
            "hwnd": hwnd,
            "window_id": hwnd,
            "action": str(arguments.get("action")),
            "action_delivered": str(arguments.get("action")),
            "operation_id": str(commit_res["operationId"]),
            "action_fingerprint": str(commit_res["actionFingerprint"]),
            "mutation": str(commit_res["mutation"]),
            "focus_recovery": copy.deepcopy(dict(commit_res["focusRecovery"])),
            "next_step": (
                "The fresh post-action observation and screenshot are ALREADY attached to this result. "
                "If the screenshot shows the user's goal is complete (e.g. message sent, UI updated), finish and reply "
                "to the user IMMEDIATELY—do NOT call computer_read_window again to re-verify. "
                "Otherwise use this observation directly for the next action (you can pass action='type' with x, y, "
                "text, and optional key='Enter' in a single call). Never replay an uncertain send."
            ) if post_record else
            "The action committed but its new observation is unavailable. Read the window to verify; do not replay the action.",
        }
        if post_record:
            bounds = post_record.geometry["windowRect"]
            structured["observation"] = {
                "hwnd": hwnd, "title": post_record.window_title,
                "state_token": post_record.state_token, "observation_id": post_record.observation_id,
                "captured_at": post_record.captured_at,
                "rect": [bounds["left"], bounds["top"], bounds["right"], bounds["bottom"]],
                "accessibility": copy.deepcopy(post_record.accessibility),
                "elements": [_compact_public_element(el) for el in post_record.elements[:45]],
            }
        txt = f"{arguments.get('action')} committed. " + structured["next_step"]
        out: dict[str, Any] = {
            "server_id": str(binding["server_id"]),
            "tool": str(binding["original_tool_name"]),
            "model_tool_name": str(binding["model_tool_name"]),
            "is_error": False,
            "content": [{"type": "text", "text": txt}],
            "text": txt,
            "structuredContent": structured,
        }
        if post_artifacts:
            out["native_observation_artifacts"] = copy.deepcopy(post_artifacts)
            out["images"] = [
                {k: v for k, v in item.items() if k != "native_descriptor"}
                for item in post_artifacts
            ]
            out["image_path"] = post_artifacts[0]["path"]
        return out

    # =========================================================================
    # Legacy `LocalTools` (`ryn_computer_read` / `ryn_computer_action`) Entry Points
    # =========================================================================

    def preview_local_action(
        self,
        args: Mapping[str, Any],
        task_id: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        hwnd = args.get("window_id")
        action_name = args.get("action")
        if type(hwnd) is not int or hwnd < 1 or not isinstance(action_name, str):
            raise AgentToolRejected("agent_tool_arguments_invalid")
        plan = self._build_action_plan(
            task_id=task_id,
            hwnd=hwnd,
            action_name=action_name,
            arguments=args,
            context=context,
            legacy_local=True,
        )
        result: dict[str, Any] = {
            "window_title": plan["window_title"],
            "window_pid": plan["window_pid"],
            "window_rect": plan["window_rect"],
            "window_ref": plan["window_ref"],
            "expected_state_token": plan["expected_state_token"],
            "observation_id": plan["observation_id"],
            "native_action": plan["native_action"],
            "action_fingerprint": plan["action_fingerprint"],
        }
        if plan["element"] is not None:
            result["element"] = {("is_password" if k == "password" else k): v
                                 for k, v in plan["element"].items()}
        return result

    def execute_local_read(
        self,
        args: Mapping[str, Any],
        task_id: str,
        *,
        operation_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        if cancelled():
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
        if args.get("screenshot_path"):
            # Whole-screen screenshot_path has no v1 native equivalent and must never
            # silently become a single-window screenshot or fall back to ImageGrab.
            raise AgentToolRejected("agent_computer_action_unsupported")

        self.overlay.touch(task_id, "正在查看屏幕")

        try:
            raw_list = self.bridge.list_windows(
                task_session_id=task_id,
                include_minimized=False,
                max_windows=60,
                cancelled=cancelled,
            )
        except NativeBridgeError as exc:
            self._translate_bridge_exception(exc, is_write=False)

        gen, worker_inst, _ = self.bridge._client.active_session_info()
        windows_out: list[dict[str, Any]] = []
        with self._lock:
            tstate = self._get_task_state_locked(
                task_id,
                worker_instance=worker_inst,
                worker_generation=gen,
                context=context,
            )
            for entry in raw_list["windows"]:
                sky_id, stored = tstate.record_window_entry(entry)
                wref = stored["window"]
                b = stored["bounds"]
                windows_out.append(
                    {
                        "window_id": int(wref["hwnd"], 10),
                        "hwnd": int(wref["hwnd"], 10),
                        "sky_window_id": sky_id,
                        "window_ref": copy.deepcopy(wref),
                        "title": stored["title"],
                        "pid": int(wref["pid"]),
                        "rect": [int(b["left"]), int(b["top"]), int(b["right"]), int(b["bottom"])],
                        "foreground": bool(stored["foreground"]),
                    }
                )

        result: dict[str, Any] = {"windows": windows_out}
        ident = args.get("window_id")
        if ident is not None:
            win_entry = self._require_observed_window_ref(task_id, int(ident), context=context)
            eff_op_id = str(operation_id or (context or {}).get("operation_id") or "op_local_read").strip() or "op_local_read"
            try:
                obs = self.bridge.get_state(
                    task_session_id=task_id,
                    window=win_entry["window"],
                    include_screenshot=False,
                    include_text=True,
                    cancelled=cancelled,
                )
                obs_record, _ = self._ingest_observation(
                    task_id=task_id,
                    operation_id=eff_op_id,
                    obs=obs,
                    window_meta=win_entry,
                    context=context,
                )
                result.update(
                    elements=copy.deepcopy(obs_record.elements),
                    incomplete=bool(obs_record.accessibility.get("truncated", False)),
                    state_token=obs_record.state_token,
                    observation_id=obs_record.observation_id,
                    window_ref=copy.deepcopy(obs_record.window_ref),
                )
            except NativeBridgeError as exc:
                self._translate_bridge_exception(exc, is_write=False)
        return result

    def execute_local_action(
        self,
        args: Mapping[str, Any],
        task_id: str,
        *,
        operation_id: str,
        approved_preview: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        on_ready_host_gate: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if cancelled():
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
        current_preview = self.preview_local_action(args, task_id, context=context)
        self.overlay.touch(task_id, "正在操作窗口")
        if approved_preview is not None:
            for k in (
                "window_title",
                "window_pid",
                "window_rect",
                "window_ref",
                "expected_state_token",
                "observation_id",
                "native_action",
                "action_fingerprint",
                "element",
            ):
                if k in approved_preview and current_preview.get(k) != approved_preview.get(k):
                    raise AgentToolRejected("agent_approval_preview_changed")

        stamp = self.capture_trusted_write_authority(
            task_id=task_id,
            operation_id=operation_id,
            tool_name="ryn_computer_action",
            arguments=args,
            context=context,
            approved_preview=current_preview,
            binding=None,
            verify_binding_fn=None,
            cancelled=cancelled,
        )
        frozen_wref = copy.deepcopy(stamp.frozen_window_ref)
        frozen_token = stamp.frozen_state_token
        frozen_action = copy.deepcopy(dict(current_preview["native_action"]))
        hwnd = stamp.frozen_hwnd

        def _on_ready(validated_ready: dict[str, Any]) -> bool:
            self.verify_ready_write_authority(
                stamp,
                validated_ready=validated_ready,
                context=context,
                cancelled=cancelled,
                verify_binding_fn=None,
            )
            if on_ready_host_gate is not None:
                on_ready_host_gate(validated_ready)
            return True

        try:
            commit_res = self.bridge.execute_action(
                task_session_id=task_id,
                operation_id=operation_id,
                window=frozen_wref,
                expected_state_token=frozen_token,
                action=frozen_action,
                on_ready=_on_ready,
                cancelled=cancelled,
            )
        except NativeBridgeError as exc:
            self._translate_bridge_exception(exc, is_write=True)

        with self._lock:
            tstate = self._tasks.get(task_id)
            if tstate is not None:
                prev_obs = tstate.observations_by_hwnd.pop(hwnd, None)
                if prev_obs is not None:
                    tstate.observations_by_token.pop(prev_obs.state_token, None)
                    for aid in prev_obs.artifact_ids:
                        if aid in tstate.artifacts_by_id:
                            tstate.artifacts_by_id[aid].retired = True

        return {
            "window_id": hwnd,
            "action_delivered": str(args["action"]),
            "operation_id": str(commit_res["operationId"]),
            "action_fingerprint": str(commit_res["actionFingerprint"]),
            "mutation": str(commit_res["mutation"]),
            "next_step": "Read the actual window state again to verify the effect.",
        }

    # =========================================================================
    # Task Retirement & Owner Cleanup
    # =========================================================================

    def forget_task(self, task_id: str) -> None:
        if not isinstance(task_id, str) or not task_id.strip():
            return
        clean_task = task_id.strip()
        self.overlay.forget(clean_task)
        tracked_files: list[tuple[Path, VerifiedFileIdentity | None, Path]] = []
        with self._lock:
            tstate = self._tasks.pop(clean_task, None)
            ws_roots = self._workspace_roots_by_task.pop(clean_task, set())
            if tstate is not None:
                tracked_files = list(tstate.tracked_files.values())
                tstate.tracked_files.clear()
                tstate.retire_all_observations()
            existing_bridge = self._peek_bridge_locked()

        safe_task = safe_segment(clean_task, "task")
        cleanup_task_verified_artifacts(
            tracked_files,
            safe_task=safe_task,
            ws_roots=ws_roots,
        )

        if existing_bridge is not None:
            has_bridge_task = False
            with existing_bridge._lock:
                has_bridge_task = clean_task in existing_bridge._tasks
            has_live_session = getattr(existing_bridge._client, "_session", None) is not None
            if tstate is not None or has_bridge_task or has_live_session:
                try:
                    existing_bridge.end_task_session(clean_task)
                except Exception:
                    pass

    def close(self) -> None:
        self.overlay.close()
        with self._lock:
            for tstate in self._tasks.values():
                tstate.tracked_files.clear()
                tstate.retire_all_observations()
            self._tasks.clear()
            self._workspace_roots_by_task.clear()
            existing_bridge = self._peek_bridge_locked()
        if existing_bridge is not None:
            try:
                existing_bridge.close()
            except Exception:
                pass


_FACADE_LOCK = threading.Lock()
_OWNER_FACADES: weakref.WeakKeyDictionary[Any, NativeComputerFacade] = weakref.WeakKeyDictionary()


def bind_owner_native_computer(
    owner: Any,
    *,
    local_tools: Any = None,
    bridge: NativeBridge | None = None,
) -> NativeComputerFacade:
    """Attach or retrieve the `NativeComputerFacade` bound exclusively to `owner`.

    Never reuses a facade or `NativeBridge` from a distinct owner via `local_tools`.
    """
    if owner is None:
        raise ValueError("owner is required for NativeComputerFacade")
    target_owner_id = resolve_owner_id(owner)
    with _FACADE_LOCK:
        if bridge is not None:
            bind_node_native_bridge(owner, bridge=bridge)

        existing = getattr(owner, "_ryn_native_computer", None)
        if not isinstance(existing, NativeComputerFacade):
            try:
                existing = _OWNER_FACADES.get(owner)
            except TypeError:
                existing = None

        if (
            isinstance(existing, NativeComputerFacade)
            and existing._owner is owner
            and existing.owner_id == target_owner_id
        ):
            if bridge is not None:
                existing._explicit_bridge = bridge
            if local_tools is not None:
                existing.register_owner_alias(local_tools)
            return existing

        facade = NativeComputerFacade(owner, local_tools=local_tools, bridge=bridge)
        try:
            owner._ryn_native_computer = facade
        except AttributeError:
            try:
                _OWNER_FACADES[owner] = facade
            except TypeError:
                pass
        if local_tools is not None:
            facade.register_owner_alias(local_tools)
        return facade


__all__ = [
    "HostObservationArtifactEntry",
    "NativeComputerFacade",
    "TrustedWriteAuthorityStamp",
    "WindowObservationRecord",
    "bind_owner_native_computer",
    "normalize_key_token",
    "validate_native_observation_descriptor",
]
