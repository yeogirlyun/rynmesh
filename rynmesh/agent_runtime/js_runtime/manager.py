"""Task-scoped QuickJS worker process manager for the Ryn JS runtime."""

from __future__ import annotations

import atexit
import os
import platform as py_platform
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...llm_package.process_guard import _windows_job, terminate
from .images import (
    HostImageIssuanceContext,
    HostImageRegistry,
    ImagePromotionResult,
    PromotedImageAttachment,
)
from .process_identity import (
    CREATE_SUSPENDED,
    MAX_ACTIVE_RESOURCE_LEASES,
    JsProcessIdentityError,
    VerifiedProcessIdentity,
    verify_suspended_process_identity_on_windows,
)
from .protocol import (
    DEFAULT_LIMITS,
    MAX_FRAME_BYTES,
    MAX_OUTPUT_BYTES,
    PROTOCOL_VERSION,
    JsExecutionSnapshot,
    JsProtocolError,
    JsRuntimeError,
    PendingHostCallEnvelope,
    build_cancel_frame,
    build_close_frame,
    build_end_turn_frame,
    build_evaluate_frame,
    build_host_result_frame,
    encode_parent_frame,
    parse_worker_frame,
    sanitize_image_metadata,
    sanitize_snapshot_text,
    sanitize_snapshot_value,
    validate_identifier,
    validate_sky_host_call,
)
from .resources import (
    MANIFEST_FILENAME,
    PINNED_QUICKJS_VARIANT,
    ExpectedJsRuntimeProfileSpec,
    JsResourceError,
    VerifiedJsResourceLease,
    active_resource_lease_count,
    is_verified_production_lease,
    open_development_js_runtime_lease,
    open_production_js_runtime_lease,
    sanitize_invocation_environment,
    verify_directory_lineage_no_reparse,
)

WORKER_PATH = Path(__file__).resolve().parents[3] / "native" / "ryn_js_runtime" / "src" / "worker.mjs"
PACKAGE_DIR = WORKER_PATH.parent.parent
MAX_EXECUTION_HISTORY = 32
MAX_MANAGER_ACTIVE_SESSIONS = MAX_ACTIVE_RESOURCE_LEASES

# Private host-embedded release commitment for packaged production builds.
# Remains None in unmounted/unpackaged repository builds until the packaging step
# embeds the build-generated embedded commitment; production resolution fails closed with 0 spawns when None.
_EMBEDDED_PRODUCTION_PROFILE_SPEC: ExpectedJsRuntimeProfileSpec | None = None

_HOST_INSTALLED_LEASES_LOCK = threading.Lock()
_HOST_INSTALLED_LEASE_ROOTS: dict[int, Path] = {}

_SAFE_ENV_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
)

_FORBIDDEN_ENV_PREFIXES = (
    "RYNMESH_",
    "RYN_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_",
    "CODEX_",
    "SKY_",
    "OAI_",
    "NODE_",
)


@dataclass(frozen=True)
class JsRuntimeDevelopmentConfig:
    """Explicit opt-in development/test resource profile configuration (`mode="source_dev"`).

    Never grants `packaged_release`, `release_verified`, or `installed_ready` status.
    """

    explicit_profile_root: Path
    expected_spec: ExpectedJsRuntimeProfileSpec | None = None
    expected_platform: str | None = None
    expected_arch: str | None = None

    def __post_init__(self) -> None:
        if not self.explicit_profile_root or not str(self.explicit_profile_root).strip():
            raise JsResourceError(
                "ryn_js_resource_invalid_path",
                "explicit_profile_root must be a non-empty path",
            )
        object.__setattr__(self, "explicit_profile_root", Path(self.explicit_profile_root))


def _current_platform_arch() -> tuple[str, str]:
    if sys.platform.startswith("win"):
        plat = "win32"
    elif sys.platform == "darwin":
        plat = "darwin"
    else:
        plat = "linux"
    mach = (py_platform.machine() or "").lower()
    if mach in {"amd64", "x86_64", "x64"}:
        arch = "x64"
    elif mach in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = "x64"
    return plat, arch


def _resolve_installed_production_profile_root(platform_name: str, arch_name: str) -> Path:
    """Derive the fixed installed sidecar profile root `computer-use/js-runtime/<platform>-<arch>`."""
    if getattr(sys, "frozen", False) is not True:
        raise JsResourceError(
            "ryn_js_runtime_not_packaged",
            "Production JS runtime requires a packaged frozen host build (sys.frozen is not True).",
        )
    exe_path = getattr(sys, "executable", None)
    if not isinstance(exe_path, str) or not exe_path.strip():
        raise JsResourceError(
            "ryn_js_runtime_not_packaged",
            "Packaged host executable path (sys.executable) is unavailable.",
        )
    install_dir = Path(exe_path).parent
    return install_dir / "computer-use" / "js-runtime" / f"{platform_name}-{arch_name}"


def _is_host_installed_production_lease(
    lease: VerifiedJsResourceLease,
    *,
    expected_spec: ExpectedJsRuntimeProfileSpec | None = None,
    expected_installed_root: Path | None = None,
) -> bool:
    """Return True only if `lease` was opened by `_open_production_lease_from_host` for the fixed installed root."""
    if not isinstance(lease, VerifiedJsResourceLease) or lease.closed:
        return False
    if getattr(sys, "frozen", False) is not True:
        return False
    spec = expected_spec if expected_spec is not None else _EMBEDDED_PRODUCTION_PROFILE_SPEC
    if not isinstance(spec, ExpectedJsRuntimeProfileSpec):
        return False
    try:
        installed_root = (
            expected_installed_root
            if expected_installed_root is not None
            else _resolve_installed_production_profile_root(spec.platform, spec.arch)
        )
        norm_installed_root = os.path.normcase(str(Path(os.path.abspath(installed_root))))
    except Exception:
        return False

    with _HOST_INSTALLED_LEASES_LOCK:
        recorded_root = _HOST_INSTALLED_LEASE_ROOTS.get(id(lease))
    if recorded_root is None or os.path.normcase(str(recorded_root)) != norm_installed_root:
        return False

    if not is_verified_production_lease(
        lease,
        expected_manifest_sha256=spec.expected_manifest_sha256,
        expected_profile_root=installed_root,
        expected_spec=spec,
    ):
        return False

    prof = lease.profile
    mf = prof.manifest
    expected_node = recorded_root / Path(mf.node_executable_relpath)
    expected_worker = recorded_root / Path(mf.worker_relpath)
    expected_shim = recorded_root / Path(mf.sky_shim_relpath)
    expected_pkg = recorded_root / "package"
    expected_wasm = recorded_root / Path(mf.wasm_relpath)
    expected_manifest = recorded_root / MANIFEST_FILENAME
    for actual_p, expected_p in (
        (prof.profile_root, recorded_root),
        (prof.node_executable, expected_node),
        (prof.worker_path, expected_worker),
        (prof.sky_shim_path, expected_shim),
        (prof.package_root, expected_pkg),
        (prof.wasm_path, expected_wasm),
        (prof.manifest_path, expected_manifest),
    ):
        if os.path.normcase(str(actual_p)) != os.path.normcase(str(expected_p)):
            return False
    return True


def _verify_host_installed_production_lease(lease: VerifiedJsResourceLease) -> VerifiedJsResourceLease:
    """Validate that `lease` is an authentic host-installed production lease or close and raise `JsResourceError`."""
    if not isinstance(lease, VerifiedJsResourceLease) or lease.closed:
        if isinstance(lease, VerifiedJsResourceLease):
            lease.close()
        raise JsResourceError(
            "ryn_js_resource_lease_closed",
            "Production loader did not return an open VerifiedJsResourceLease",
        )
    if lease.profile.mode != "packaged_release" or not lease.profile.is_production:
        lease.close()
        raise JsResourceError(
            "ryn_js_resource_invalid_mode",
            "Production mode requires a packaged_release profile lease",
        )
    spec = _EMBEDDED_PRODUCTION_PROFILE_SPEC
    if getattr(sys, "frozen", False) is not True or not isinstance(spec, ExpectedJsRuntimeProfileSpec):
        lease.close()
        raise JsResourceError(
            "ryn_js_resource_invalid_mode",
            "Production mode requires a frozen host and host-embedded ExpectedJsRuntimeProfileSpec commitment",
        )
    try:
        installed_root = _resolve_installed_production_profile_root(spec.platform, spec.arch)
    except BaseException:
        lease.close()
        raise
    if not _is_host_installed_production_lease(
        lease,
        expected_spec=spec,
        expected_installed_root=installed_root,
    ):
        lease.close()
        raise JsResourceError(
            "ryn_js_resource_invalid_mode",
            "Production mode requires an authentic host-installed packaged_release lease bound to the fixed installed root and embedded commitment",
        )
    return lease


def _open_production_lease_from_host() -> VerifiedJsResourceLease:
    """Private production lease loader using only the host-embedded release commitment and fixed installed root."""
    if getattr(sys, "frozen", False) is not True:
        raise JsResourceError(
            "ryn_js_runtime_not_packaged",
            "Production JS runtime requires a packaged frozen host build; no source_dev or PATH fallback is permitted.",
        )
    spec = _EMBEDDED_PRODUCTION_PROFILE_SPEC
    if not isinstance(spec, ExpectedJsRuntimeProfileSpec):
        raise JsResourceError(
            "ryn_js_resource_missing_commitment",
            "No host-embedded production ExpectedJsRuntimeProfileSpec commitment is configured.",
        )
    trusted_root = _resolve_installed_production_profile_root(spec.platform, spec.arch)
    resolved_installed_root = verify_directory_lineage_no_reparse(trusted_root)
    lease = open_production_js_runtime_lease(
        trusted_profile_root=resolved_installed_root,
        expected_spec=spec,
    )
    try:
        with _HOST_INSTALLED_LEASES_LOCK:
            _HOST_INSTALLED_LEASE_ROOTS[id(lease)] = resolved_installed_root
        return _verify_host_installed_production_lease(lease)
    except BaseException:
        with _HOST_INSTALLED_LEASES_LOCK:
            _HOST_INSTALLED_LEASE_ROOTS.pop(id(lease), None)
        lease.close()
        raise


def resolve_node_executable(explicit: str | Path | None = None) -> str:
    """Fail-closed legacy entry point: ambient PATH and RYN_JS_NODE_EXECUTABLE lookups are forbidden."""
    raise JsRuntimeError(
        "ryn_js_node_unavailable",
        "Ambient Node.js resolution via PATH or RYN_JS_NODE_EXECUTABLE is disabled; "
        "use a verified JsRuntimeResourceProfile lease.",
    )


def is_js_runtime_available(
    *,
    development_config: JsRuntimeDevelopmentConfig | None = None,
    development_profile_root: str | Path | None = None,
) -> bool:
    """Pure in-memory check whether the JS runtime has a configured profile source without touching disk or spawning."""
    if development_config is not None:
        return isinstance(development_config, JsRuntimeDevelopmentConfig)
    if development_profile_root is not None:
        return bool(str(development_profile_root).strip())
    return (
        getattr(sys, "frozen", False) is True
        and isinstance(_EMBEDDED_PRODUCTION_PROFILE_SPEC, ExpectedJsRuntimeProfileSpec)
    )


def sanitized_worker_env(
    session_id: str,
    generation: int,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct a minimal, secret-free child environment for the Node QuickJS worker."""
    source = os.environ if base_env is None else base_env
    if not isinstance(source, dict) and source is not os.environ:
        raise JsResourceError("ryn_js_resource_invalid_env", "base_env must be a dictionary")
    raw_env: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value:
            raw_env[key] = value
    if base_env is not None:
        # Preserve caller-supplied PATH only when explicitly unit-testing sanitized_worker_env(base_env=...)
        explicit_path = base_env.get("PATH")
        if isinstance(explicit_path, str) and explicit_path:
            raw_env["PATH"] = explicit_path
    else:
        # Never inherit ambient caller PATH when spawning; construct minimal OS system directory path only
        sys_root = raw_env.get("SYSTEMROOT") or raw_env.get("WINDIR") or (
            "C:\\Windows" if os.name == "nt" else "/usr/bin:/bin"
        )
        if os.name == "nt":
            raw_env["PATH"] = f"{sys_root}\\System32;{sys_root};{sys_root}\\System32\\Wbem"
        else:
            raw_env["PATH"] = "/usr/bin:/bin"

    for key in list(raw_env):
        upper_key = key.upper()
        if any(upper_key.startswith(prefix) for prefix in _FORBIDDEN_ENV_PREFIXES):
            raw_env.pop(key, None)

    env = sanitize_invocation_environment(raw_env)
    env["RYN_JS_SESSION_ID"] = validate_identifier(session_id, "session_id")
    env["RYN_JS_GENERATION"] = str(int(generation))
    env["NODE_NO_WARNINGS"] = "1"
    return env


def _wait_for_poll_exit(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    """Poll `proc.poll()` up to `timeout` seconds without forcing fake returncodes."""
    if proc.poll() is not None:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.005)
    return proc.poll() is not None


def _terminate_and_confirm_exit(
    proc: subprocess.Popen[bytes] | None,
    *,
    wait_timeout: float = 0.2,
) -> bool:
    """Invoke `terminate(proc)` and return True only when `proc is None` or `proc.poll() is not None`."""
    if proc is None:
        return True
    try:
        terminate(proc)
    except BaseException:
        pass
    return _wait_for_poll_exit(proc, wait_timeout)


def _cleanup_process(
    proc: subprocess.Popen[bytes] | None,
    stdout_thread: threading.Thread | None = None,
    stderr_thread: threading.Thread | None = None,
    resource_lease: VerifiedJsResourceLease | None = None,
    *,
    wait_timeout: float = 0.5,
) -> bool:
    """Terminate child and release pipes/lease ONLY when child exit (`proc.poll() is not None`) is confirmed.

    Returns True if child process exit is confirmed and `resource_lease` was closed;
    returns False if `proc.poll() is None`, leaving `resource_lease` open and pinned.
    """
    exited = _terminate_and_confirm_exit(proc, wait_timeout=wait_timeout)
    if not exited:
        return False
    try:
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        pass
        current = threading.current_thread()
        if stdout_thread is not None and stdout_thread.is_alive() and stdout_thread is not current:
            stdout_thread.join(timeout=1.5)
        if stderr_thread is not None and stderr_thread.is_alive() and stderr_thread is not current:
            stderr_thread.join(timeout=1.5)
    finally:
        if resource_lease is not None:
            try:
                resource_lease.close()
            except BaseException:
                pass
    return True


@dataclass
class _ActiveExecutionState:
    execution_id: str
    script_id: str
    generation: int
    status: str = "running"
    stdout_parts: list[str] = field(default_factory=list)
    stdout_bytes: int = 0
    stdout: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)
    image_validation_error: dict[str, Any] | None = None
    pending_host_calls: dict[str, PendingHostCallEnvelope] = field(default_factory=dict)
    unconsumed_host_calls: deque[str] = field(default_factory=deque)
    settled_callbacks: set[str] = field(default_factory=set)
    value: Any = None
    value_type: str = "undefined"
    error: dict[str, Any] | None = None
    cpu_elapsed_ms: int = 0


class JsRuntimeSession:
    """Isolated task-scoped QuickJS worker subprocess session backed by a handle-pinned `VerifiedJsResourceLease`."""

    def __init__(
        self,
        *,
        task_id: str,
        session_id: str,
        generation: int = 1,
        resource_lease: VerifiedJsResourceLease,
        startup_timeout: float = 10.0,
        image_registry: HostImageRegistry | None = None,
        context_epoch: str = "epoch_1",
        selection_scope: str = "default",
        before_resume_verifier: Callable[..., VerifiedProcessIdentity] | None = None,
        on_quarantine: Callable[[JsRuntimeSession], None] | None = None,
    ) -> None:
        self.task_id = validate_identifier(str(task_id).strip(), "task_id")
        self.session_id = validate_identifier(str(session_id).strip(), "session_id")
        if type(generation) is not int or generation < 1:
            if isinstance(resource_lease, VerifiedJsResourceLease):
                resource_lease.close()
            raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
        if not isinstance(resource_lease, VerifiedJsResourceLease) or resource_lease.closed:
            if isinstance(resource_lease, VerifiedJsResourceLease):
                resource_lease.close()
            raise JsResourceError(
                "ryn_js_resource_lease_closed",
                "JsRuntimeSession requires an open VerifiedJsResourceLease",
            )
        self.generation = generation
        self.turn = 1
        self.resource_lease: VerifiedJsResourceLease = resource_lease
        self.profile = resource_lease.profile
        self.node_executable = str(self.profile.node_executable)
        self.worker_path = self.profile.worker_path
        self.package_root = self.profile.package_root
        self.startup_timeout = max(0.5, float(startup_timeout))
        self.image_registry = image_registry
        self.context_epoch = validate_identifier(str(context_epoch or "epoch_1").strip(), "context_epoch")
        self.selection_scope = validate_identifier(str(selection_scope or "default").strip(), "selection_scope")
        self.used_custom_verifier: bool = before_resume_verifier is not None
        self._before_resume_verifier = (
            before_resume_verifier or verify_suspended_process_identity_on_windows
        )
        self._on_quarantine = on_quarantine

        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._quarantine_notify_pending: bool = False
        self.status = "starting"
        self.engine = ""
        self.release_variant = ""
        self.close_reason = ""
        self.cleanup_incomplete: bool = False
        self.last_protocol_error: dict[str, Any] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=50)
        self._seen_execution_ids: set[str] = set()
        self._active: _ActiveExecutionState | None = None
        self._history: dict[str, _ActiveExecutionState] = {}
        self._unverified_promotion_stubs: deque[dict[str, Any]] = deque(maxlen=64)
        self._unverified_ordinal = 0
        self._frame_wire_log: deque[dict[str, Any]] = deque(maxlen=256)
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        self.files_verified: bool = bool(self.profile.files_verified)
        self.actual_process_identity_verified: bool = False
        self.runtime_handshake_verified: bool = False
        self.verified_process_identity: VerifiedProcessIdentity | None = None

        self._start_process()

    def _invalidate_live_verification_unlocked(self) -> None:
        self.actual_process_identity_verified = False
        self.runtime_handshake_verified = False
        self.verified_process_identity = None

    def _record_quarantine_unlocked(self) -> None:
        self.cleanup_incomplete = True
        self._quarantine_notify_pending = True

    def _flush_pending_notifications(self) -> None:
        """Invoke any pending manager quarantine notification outside `self._cond` and `self._write_lock`."""
        callback: Callable[[JsRuntimeSession], None] | None = None
        with self._cond:
            if self._quarantine_notify_pending:
                self._quarantine_notify_pending = False
                callback = self._on_quarantine
        if callback is not None:
            try:
                callback(self)
            except Exception:
                pass

    def reap_if_exited(self) -> bool:
        """Check whether an unconfirmed/quarantined child process has now exited and release its lease if so."""
        with self._cond:
            proc = self.process
            if proc is not None and proc.poll() is None:
                return False
            self._invalidate_live_verification_unlocked()
            self.cleanup_incomplete = False
            self._quarantine_notify_pending = False
            self.status = "closed"
            stdout_thread = self._stdout_thread
            stderr_thread = self._stderr_thread
            lease = self.resource_lease
        _cleanup_process(proc, stdout_thread, stderr_thread, lease, wait_timeout=0.05)
        return True

    def _start_process(self) -> None:
        if os.name != "nt":
            self.resource_lease.close()
            raise JsRuntimeError(
                "ryn_js_platform_guard_unsupported",
                "Ryn QuickJS worker requires Windows Job + suspended-process verification.",
            )
        if self.resource_lease.closed:
            raise JsResourceError(
                "ryn_js_resource_lease_closed",
                "Cannot start QuickJS worker with a closed VerifiedJsResourceLease",
            )
        try:
            self.resource_lease.revalidate_critical_files()
        except BaseException:
            self.resource_lease.close()
            raise

        env = sanitized_worker_env(self.session_id, self.generation)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options: dict[str, Any] = {
            "cwd": str(self.package_root),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "creationflags": CREATE_SUSPENDED | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            "startupinfo": startup,
        }

        try:
            proc = subprocess.Popen(
                [str(self.profile.node_executable), str(self.profile.worker_path)],
                **options,
            )
        except OSError as exc:
            self.resource_lease.close()
            raise JsRuntimeError(
                "ryn_js_spawn_failed",
                f"Failed to start QuickJS worker process: {exc}",
            ) from exc

        self.process = proc

        def _verify_before_resume(child_proc: Any, _job_handle: Any) -> None:
            verified_id = self._before_resume_verifier(
                process=child_proc,
                lease=self.resource_lease,
                session_id=self.session_id,
                generation=self.generation,
            )
            if not isinstance(verified_id, VerifiedProcessIdentity):
                raise JsProcessIdentityError(
                    "ryn_js_process_identity_mismatch",
                    "before_resume verifier did not return a VerifiedProcessIdentity",
                )
            if (
                verified_id.pid != child_proc.pid
                or verified_id.creation_time_filetime <= 0
                or verified_id.session_id != self.session_id
                or verified_id.generation != self.generation
                or verified_id.profile_digest != self.profile.profile_digest
            ):
                raise JsProcessIdentityError(
                    "ryn_js_process_identity_mismatch",
                    "Suspended child process identity record failed session/profile invariants",
                )
            self.verified_process_identity = verified_id
            self.actual_process_identity_verified = True

        try:
            try:
                proc._ryn_job = _windows_job(proc, before_resume=_verify_before_resume)  # type: ignore[attr-defined]
            except (JsRuntimeError, JsResourceError, JsProcessIdentityError):
                raise
            except OSError as exc:
                raise JsRuntimeError(
                    "ryn_js_isolation_failed",
                    f"Failed to assign or verify QuickJS worker in Windows Job object: {exc}",
                ) from exc
            except Exception as exc:
                raise JsRuntimeError(
                    "ryn_js_process_identity_mismatch",
                    f"Suspended child process pre-resume verification failed: {exc}",
                ) from exc

            self._stdout_thread = threading.Thread(
                target=self._read_stdout_loop,
                name=f"ryn-js-stdout-{self.session_id}",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop,
                name=f"ryn-js-stderr-{self.session_id}",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()

            deadline = time.monotonic() + self.startup_timeout
            with self._cond:
                while self.status == "starting":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
                if self.status != "ready" or not self.runtime_handshake_verified:
                    stderr_text = "\n".join(self.stderr_tail).strip()
                    detail = f" ({stderr_text})" if stderr_text else ""
                    err_code = (
                        self.close_reason
                        if self.close_reason and self.close_reason != "ryn_js_startup_failed"
                        else "ryn_js_startup_failed"
                    )
                    raise JsRuntimeError(
                        err_code,
                        f"QuickJS worker did not complete verified ready handshake within {self.startup_timeout:.1f}s{detail}",
                    )
        except BaseException:
            with self._cond:
                self._invalidate_live_verification_unlocked()
                self.status = "closed"
                if not self.close_reason:
                    self.close_reason = "ryn_js_startup_failed"
                self._cond.notify_all()
            exited = _cleanup_process(proc, self._stdout_thread, self._stderr_thread, self.resource_lease)
            if not exited:
                with self._cond:
                    self.status = "lost"
                    self.close_reason = "ryn_js_cleanup_incomplete"
                    self._record_quarantine_unlocked()
                    self._cond.notify_all()
            self._flush_pending_notifications()
            raise

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def node_runtime_id(self) -> str:
        if self.verified_process_identity is not None:
            return (
                f"node_{self.session_id}_{self.verified_process_identity.pid}_"
                f"{self.verified_process_identity.creation_time_filetime}"
            )
        return f"node_{self.session_id}_{self.pid or 0}"

    @property
    def observed_wire_frames(self) -> list[dict[str, Any]]:
        with self._cond:
            return [dict(item) for item in self._frame_wire_log]

    @property
    def is_alive(self) -> bool:
        return (
            self.process is not None
            and self.process.poll() is None
            and self.status in {"starting", "ready"}
            and not self.resource_lease.closed
            and not self.cleanup_incomplete
        )

    def _record_history_unlocked(self, state: _ActiveExecutionState) -> None:
        if len(self._history) >= MAX_EXECUTION_HISTORY and state.execution_id not in self._history:
            oldest_key = next(iter(self._history))
            self._history.pop(oldest_key, None)
        self._history[state.execution_id] = state

    def _send_frame(self, frame: dict[str, Any]) -> None:
        raw = encode_parent_frame(frame)
        with self._cond:
            self._frame_wire_log.append({
                "direction": "parent_to_worker",
                "type": str(frame.get("type") or ""),
                "byte_length": len(raw),
            })
        lost_error: OSError | None = None
        with self._write_lock:
            proc = self.process
            if proc is None or proc.stdin is None or proc.poll() is not None:
                if self.image_registry is not None:
                    self.image_registry.retire_session(
                        self.session_id,
                        reason="ryn_js_worker_lost",
                        cancel_pinned=True,
                    )
                raise JsRuntimeError("ryn_js_worker_lost", "QuickJS worker stdin is closed.")
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except OSError as exc:
                lost_error = exc
        if lost_error is not None:
            self._mark_lost(f"QuickJS worker write failed: {lost_error}")
            raise JsRuntimeError(
                "ryn_js_worker_lost",
                f"QuickJS worker write failed: {lost_error}",
            ) from lost_error

    def _read_stderr_loop(self) -> None:
        proc = self.process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                raw_line = proc.stderr.readline(65536)
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    with self._cond:
                        self.stderr_tail.append(text)
        except Exception:
            return

    def _read_stdout_loop(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw_line = proc.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_FRAME_BYTES:
                    self._mark_lost(
                        "Worker frame exceeded MAX_FRAME_BYTES limit",
                        error_code="ryn_js_frame_too_large",
                    )
                    return
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    frame = parse_worker_frame(raw_line)
                except JsProtocolError as exc:
                    self._mark_lost(str(exc), error_code=exc.code)
                    return
                with self._cond:
                    self._frame_wire_log.append({
                        "direction": "worker_to_parent",
                        "type": str(frame.get("type") or ""),
                        "byte_length": len(raw_line),
                    })
                self._handle_worker_frame(frame)
        except Exception as exc:
            self._mark_lost(f"QuickJS worker stdout reader failed: {exc}")
            return
        self._on_stdout_eof()

    def _handle_worker_frame(self, frame: dict[str, Any]) -> None:
        try:
            self._handle_worker_frame_inner(frame)
        finally:
            self._flush_pending_notifications()

    def _handle_worker_frame_inner(self, frame: dict[str, Any]) -> None:
        frame_type = frame["type"]
        with self._cond:
            if frame.get("v") != PROTOCOL_VERSION:
                self._force_kill_unlocked(
                    "ryn_js_invalid_frame",
                    f"Worker emitted unsupported protocol version {frame.get('v')!r}",
                )
                return
            if frame.get("session_id") != self.session_id:
                self._force_kill_unlocked(
                    "ryn_js_session_mismatch",
                    f"Worker emitted mismatched session_id {frame.get('session_id')!r}",
                )
                return

            if frame_type == "ready":
                if self.status != "starting":
                    self._force_kill_unlocked(
                        "ryn_js_protocol_violation",
                        "Worker emitted duplicate or unexpected ready frame",
                    )
                    return
                if not self.actual_process_identity_verified or self.verified_process_identity is None:
                    self._force_kill_unlocked(
                        "ryn_js_process_identity_mismatch",
                        "Worker emitted ready frame before OS process identity was verified",
                    )
                    return
                frame_gen = frame.get("generation")
                if type(frame_gen) is not int or frame_gen != self.generation:
                    self._force_kill_unlocked(
                        "ryn_js_ready_generation_mismatch",
                        f"Worker ready generation {frame_gen!r} != expected session generation {self.generation}",
                    )
                    return
                frame_engine = frame.get("engine")
                if frame_engine != "quickjs-emscripten":
                    self._force_kill_unlocked(
                        "ryn_js_ready_engine_mismatch",
                        f"Worker ready engine {frame_engine!r} != 'quickjs-emscripten'",
                    )
                    return
                frame_variant = frame.get("release_variant")
                if frame_variant != PINNED_QUICKJS_VARIANT:
                    self._force_kill_unlocked(
                        "ryn_js_ready_variant_mismatch",
                        f"Worker ready release_variant {frame_variant!r} != {PINNED_QUICKJS_VARIANT!r}",
                    )
                    return
                frame_pid = frame.get("pid")
                if type(frame_pid) is not int or frame_pid != self.verified_process_identity.pid:
                    self._force_kill_unlocked(
                        "ryn_js_ready_pid_mismatch",
                        f"Worker ready pid {frame_pid!r} != OS-verified pid {self.verified_process_identity.pid}",
                    )
                    return

                self.engine = "quickjs-emscripten"
                self.release_variant = PINNED_QUICKJS_VARIANT
                self.runtime_handshake_verified = True
                self.status = "ready"
                self._cond.notify_all()
                return

            if not self.runtime_handshake_verified or self.status != "ready":
                self._force_kill_unlocked(
                    "ryn_js_protocol_violation",
                    f"Worker emitted {frame_type!r} frame before completing ready handshake",
                )
                return

            if frame_type == "turn_ended":
                frame_gen = frame.get("generation")
                if type(frame_gen) is not int or frame_gen < self.generation:
                    self._force_kill_unlocked(
                        "ryn_js_stale_generation",
                        f"Worker turn_ended generation {frame_gen!r} < session generation {self.generation}",
                    )
                    return
                self.generation = frame_gen
                if isinstance(frame.get("turn"), int) and type(frame.get("turn")) is int:
                    self.turn = int(frame["turn"])
                if self.image_registry is not None:
                    self.image_registry.retire_session(
                        self.session_id,
                        reason="ryn_js_turn_ended",
                        cancel_pinned=False,
                        reset_promotion_budget=True,
                    )
                self._cond.notify_all()
                return

            if frame_type == "protocol_error":
                self.last_protocol_error = dict(frame.get("error") or {})
                self._cond.notify_all()
                return

            active = self._active
            execution_id = frame.get("execution_id")
            if active is None or active.execution_id != execution_id:
                return

            if frame_type == "output":
                if int(frame["generation"]) != active.generation:
                    return
                chunk_text = sanitize_snapshot_text(str(frame.get("text") or ""))
                chunk_bytes = len(chunk_text.encode("utf-8", errors="replace"))
                if active.stdout_bytes + chunk_bytes <= MAX_OUTPUT_BYTES:
                    active.stdout_parts.append(chunk_text)
                    active.stdout_bytes += chunk_bytes
                    active.stdout = "".join(active.stdout_parts)
                self._cond.notify_all()
                return

            if frame_type == "image_request":
                if int(frame["generation"]) != active.generation:
                    return
                raw_img = frame.get("image")
                if isinstance(raw_img, dict):
                    if self.image_registry is not None:
                        try:
                            validated_meta = self.image_registry.validate_and_queue_request(
                                raw_img,
                                owner_id=self.image_registry.owner_id,
                                node_runtime_id=self.node_runtime_id,
                                task_id=self.task_id,
                                session_id=self.session_id,
                                generation=active.generation,
                                turn=self.turn,
                                emitting_execution_id=active.execution_id,
                                context_epoch=self.context_epoch,
                                selection_scope=self.selection_scope,
                            )
                        except JsProtocolError as exc:
                            active.image_validation_error = {
                                "code": exc.code,
                                "message": exc.message,
                            }
                            active.pending_host_calls.clear()
                            active.unconsumed_host_calls.clear()
                            self.image_registry.retire_session(
                                self.session_id,
                                reason=exc.code,
                                cancel_pinned=True,
                            )
                        else:
                            active.images.append(validated_meta)
                    else:
                        cleaned = sanitize_image_metadata(raw_img)
                        active.images.append(cleaned)
                        if str(raw_img.get("provenance") or "") != "guest_inline_data_url":
                            self._unverified_ordinal += 1
                            self._unverified_promotion_stubs.append({
                                "promotion_id": f"unverified_{active.generation}_{self.turn}_{self._unverified_ordinal}",
                                "emitting_execution_id": active.execution_id,
                                "generation": active.generation,
                                "turn": self.turn,
                                "ordinal": self._unverified_ordinal,
                                "image": cleaned,
                            })
                self._cond.notify_all()
                return

            if frame_type == "host_call":
                if int(frame["generation"]) != active.generation:
                    return
                callback_id = validate_identifier(frame.get("callback_id"), "callback_id")
                ordinal = int(frame.get("ordinal") or (len(active.settled_callbacks) + len(active.pending_host_calls) + 1))
                if callback_id in active.pending_host_calls or callback_id in active.settled_callbacks:
                    self._force_kill_unlocked(
                        "ryn_js_protocol_violation",
                        f"Duplicate callback_id {callback_id!r} emitted by worker",
                    )
                    return
                if active.image_validation_error is not None:
                    active.settled_callbacks.add(callback_id)
                    try:
                        self._send_frame(
                            build_host_result_frame(
                                session_id=self.session_id,
                                generation=active.generation,
                                execution_id=active.execution_id,
                                callback_id=callback_id,
                                ok=False,
                                error={
                                    "code": str(active.image_validation_error.get("code") or "ryn_js_image_untrusted"),
                                    "message": str(
                                        active.image_validation_error.get("message")
                                        or "Execution aborted due to host image validation error"
                                    ),
                                    "not_executed": True,
                                    "outcome": "not_executed",
                                },
                            )
                        )
                    except JsRuntimeError:
                        pass
                    self._cond.notify_all()
                    return
                try:
                    spec, normalized_args, args_sha256 = validate_sky_host_call(
                        str(frame.get("method") or ""),
                        frame.get("arguments"),
                    )
                except JsProtocolError as exc:
                    active.settled_callbacks.add(callback_id)
                    try:
                        self._send_frame(
                            build_host_result_frame(
                                session_id=self.session_id,
                                generation=active.generation,
                                execution_id=active.execution_id,
                                callback_id=callback_id,
                                ok=False,
                                error={
                                    "code": exc.code,
                                    "message": str(exc),
                                    "not_executed": True,
                                    "outcome": "not_executed",
                                },
                            )
                        )
                    except JsRuntimeError:
                        pass
                    self._cond.notify_all()
                    return

                envelope = PendingHostCallEnvelope(
                    task_id=self.task_id,
                    session_id=self.session_id,
                    generation=active.generation,
                    execution_id=active.execution_id,
                    callback_id=callback_id,
                    ordinal=ordinal,
                    method=str(frame["method"]),
                    effect=spec["effect"],
                    target_tool=spec["target_tool"],
                    arguments=normalized_args,
                    arguments_sha256=args_sha256,
                )
                active.pending_host_calls[callback_id] = envelope
                active.unconsumed_host_calls.append(callback_id)
                active.status = "awaiting_host"
                self._cond.notify_all()
                return

            if frame_type == "execution_result":
                if int(frame["generation"]) != active.generation:
                    return
                active.pending_host_calls.clear()
                active.unconsumed_host_calls.clear()
                if isinstance(frame.get("stdout"), str):
                    active.stdout = sanitize_snapshot_text(frame["stdout"])
                else:
                    active.stdout = sanitize_snapshot_text("".join(active.stdout_parts))
                if self.image_registry is None and isinstance(frame.get("images"), list):
                    active.images = [
                        sanitize_image_metadata(img)
                        for img in frame["images"]
                        if isinstance(img, dict)
                    ]
                active.cpu_elapsed_ms = int(frame.get("cpu_elapsed_ms") or 0)
                if active.image_validation_error is not None:
                    active.status = "failed"
                    active.value = None
                    active.value_type = "undefined"
                    active.error = dict(active.image_validation_error)
                else:
                    active.status = "completed"
                    active.value = sanitize_snapshot_value(frame.get("value"))
                    active.value_type = str(frame.get("value_type") or "undefined")
                    active.error = None
                self._record_history_unlocked(active)
                self._active = None
                self._cond.notify_all()
                return

            if frame_type == "execution_error":
                active_gen = frame.get("active_generation")
                if isinstance(active_gen, int) and type(active_gen) is int and active_gen >= self.generation:
                    self.generation = active_gen
                active.pending_host_calls.clear()
                active.unconsumed_host_calls.clear()
                err = sanitize_snapshot_value(dict(frame.get("error") or {}))
                code = str(err.get("code") or "ryn_js_guest_exception")
                if err.get("outcome") == "unknown" and self.image_registry is not None:
                    self.image_registry.retire_session(
                        self.session_id,
                        reason="ryn_js_unknown_outcome",
                        cancel_pinned=True,
                    )
                if active.image_validation_error is not None and code != "ryn_js_cancelled":
                    active.status = "failed"
                    active.error = dict(active.image_validation_error)
                else:
                    active.status = "cancelled" if code == "ryn_js_cancelled" else "failed"
                    active.error = err
                if isinstance(frame.get("stdout"), str):
                    active.stdout = sanitize_snapshot_text(frame["stdout"])
                else:
                    active.stdout = sanitize_snapshot_text("".join(active.stdout_parts))
                if self.image_registry is None and isinstance(frame.get("images"), list):
                    active.images = [
                        sanitize_image_metadata(img)
                        for img in frame["images"]
                        if isinstance(img, dict)
                    ]
                active.cpu_elapsed_ms = int(frame.get("cpu_elapsed_ms") or 0)
                self._record_history_unlocked(active)
                self._active = None
                self._cond.notify_all()
                return

    def _on_stdout_eof(self) -> None:
        with self._cond:
            self._invalidate_live_verification_unlocked()
            if self.status in {"closing", "closed"}:
                if self.image_registry is not None:
                    self.image_registry.retire_session(
                        self.session_id,
                        reason="ryn_js_session_closed",
                        cancel_pinned=True,
                    )
                if self._active is not None:
                    self._active.status = "cancelled"
                    self._active.error = {
                        "code": "ryn_js_session_closed",
                        "message": "QuickJS session was closed.",
                    }
                    self._record_history_unlocked(self._active)
                    self._active = None
                proc = self.process
                if proc is None or proc.poll() is not None:
                    self.cleanup_incomplete = False
                    self._quarantine_notify_pending = False
                    self.status = "closed"
                    try:
                        self.resource_lease.close()
                    except Exception:
                        pass
                self._cond.notify_all()
                return
            self._force_kill_unlocked(
                "ryn_js_worker_lost",
                "QuickJS worker exited unexpectedly before closing.",
            )
        self._flush_pending_notifications()

    def _mark_lost(self, message: str, *, error_code: str = "ryn_js_worker_lost") -> None:
        with self._cond:
            self._force_kill_unlocked(error_code, message)
        self._flush_pending_notifications()

    def _force_kill_unlocked(self, error_code: str, message: str = "") -> None:
        self._invalidate_live_verification_unlocked()
        if self.status not in {"closed", "lost"}:
            self.status = "lost"
            self.close_reason = error_code
        if self.image_registry is not None:
            self.image_registry.retire_session(
                self.session_id,
                reason=error_code,
                cancel_pinned=True,
            )
        if self._active is not None:
            has_pending = bool(self._active.pending_host_calls)
            self._active.status = "lost"
            self._active.error = {
                "code": error_code,
                "message": message or "QuickJS worker terminated unexpectedly.",
                "outcome": "unknown" if has_pending else "not_executed",
            }
            self._record_history_unlocked(self._active)
            self._active = None
        proc = self.process
        exited = _terminate_and_confirm_exit(proc, wait_timeout=0.1)
        if exited:
            self.cleanup_incomplete = False
            self._quarantine_notify_pending = False
            try:
                self.resource_lease.close()
            except Exception:
                pass
        else:
            self._record_quarantine_unlocked()
        self._cond.notify_all()

    def _sync_process_state_unlocked(self) -> None:
        proc = self.process
        if (
            proc is not None
            and proc.poll() is not None
            and self.status not in {"closing", "closed", "lost"}
        ):
            stdout_thread = self._stdout_thread
            if (
                stdout_thread is not None
                and stdout_thread.is_alive()
                and stdout_thread is not threading.current_thread()
            ):
                deadline = time.monotonic() + 0.05
                while (
                    self.status not in {"closing", "closed", "lost"}
                    and stdout_thread.is_alive()
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            if self.status not in {"closing", "closed", "lost"}:
                self._force_kill_unlocked(
                    "ryn_js_worker_lost",
                    "QuickJS worker exited unexpectedly before closing.",
                )

    def _sync_process_state(self) -> None:
        with self._cond:
            self._sync_process_state_unlocked()
        self._flush_pending_notifications()

    def _snapshot_for(self, state: _ActiveExecutionState) -> JsExecutionSnapshot:
        return JsExecutionSnapshot(
            task_id=self.task_id,
            session_id=self.session_id,
            generation=state.generation,
            execution_id=state.execution_id,
            status=state.status,
            value=state.value,
            value_type=state.value_type,
            stdout=state.stdout,
            images=[sanitize_image_metadata(item) for item in state.images],
            pending_host_calls=list(state.pending_host_calls.values()),
            error=dict(state.error) if state.error is not None else None,
            cpu_elapsed_ms=state.cpu_elapsed_ms,
        )

    def wait_for_protocol_error(self, *, timeout: float = 2.0) -> dict[str, Any] | None:
        """Wait up to timeout seconds for a worker protocol_error frame and return it."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while self.last_protocol_error is None and self.is_alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            return dict(self.last_protocol_error) if self.last_protocol_error is not None else None

    def evaluate(
        self,
        code: str,
        *,
        execution_id: str | None = None,
        script_id: str | None = None,
        limits: dict[str, int] | None = None,
    ) -> str:
        """Start an asynchronous guest JS evaluation on this session."""
        exec_id = validate_identifier(
            str(execution_id or f"exec_{uuid.uuid4().hex[:12]}").strip(),
            "execution_id",
        )
        scr_id = validate_identifier(
            str(script_id or f"cell_{uuid.uuid4().hex[:8]}").strip(),
            "script_id",
        )
        try:
            with self._cond:
                self._sync_process_state_unlocked()
                if self.status != "ready" or not self.is_alive or not self.runtime_handshake_verified:
                    raise JsRuntimeError(
                        "ryn_js_session_unavailable",
                        f"QuickJS session {self.session_id} is not ready (status={self.status}).",
                    )
                if self._active is not None:
                    raise JsRuntimeError(
                        "ryn_js_busy",
                        f"QuickJS session {self.session_id} already has an active evaluation ({self._active.execution_id}).",
                    )
                if exec_id in self._seen_execution_ids:
                    raise JsProtocolError(
                        "ryn_js_duplicate_execution_id",
                        f"Duplicate execution_id {exec_id!r} on session {self.session_id}",
                    )
                gen = self.generation
                frame = build_evaluate_frame(
                    session_id=self.session_id,
                    generation=gen,
                    execution_id=exec_id,
                    code=code,
                    script_id=scr_id,
                    limits=limits or DEFAULT_LIMITS,
                )
                self._seen_execution_ids.add(exec_id)
                self._active = _ActiveExecutionState(
                    execution_id=exec_id,
                    script_id=scr_id,
                    generation=gen,
                )
        finally:
            self._flush_pending_notifications()

        try:
            self._send_frame(frame)
        except Exception:
            with self._cond:
                if self._active is not None and self._active.execution_id == exec_id:
                    self._record_history_unlocked(self._active)
                    self._active = None
            raise
        return exec_id

    def poll(
        self,
        execution_id: str | None = None,
        *,
        timeout: float = 0.0,
    ) -> JsExecutionSnapshot:
        """Wait up to timeout seconds for a state transition or terminal result."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        try:
            with self._cond:
                while True:
                    self._sync_process_state_unlocked()
                    target = self._resolve_execution_unlocked(execution_id)
                    if target.status in {"completed", "failed", "cancelled", "lost"}:
                        return self._snapshot_for(target)
                    if target.pending_host_calls:
                        return self._snapshot_for(target)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return self._snapshot_for(target)
                    self._cond.wait(timeout=remaining)
        finally:
            self._flush_pending_notifications()

    def next_host_call(
        self,
        execution_id: str | None = None,
        *,
        timeout: float = 2.0,
    ) -> PendingHostCallEnvelope | None:
        """Return the next unconsumed PendingHostCallEnvelope or None if execution finishes/times out."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        try:
            with self._cond:
                while True:
                    self._sync_process_state_unlocked()
                    target = self._resolve_execution_unlocked(execution_id)
                    while target.unconsumed_host_calls:
                        cid = target.unconsumed_host_calls.popleft()
                        envelope = target.pending_host_calls.get(cid)
                        if envelope is not None:
                            return envelope
                    if target.status in {"completed", "failed", "cancelled", "lost"}:
                        return None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=remaining)
        finally:
            self._flush_pending_notifications()

    def _build_issuance_context_unlocked(
        self,
        envelope: PendingHostCallEnvelope,
        *,
        operation_id: str | None = None,
        request_id: str | None = None,
        native_worker_instance: str | None = None,
        native_window: dict[str, Any] | None = None,
    ) -> HostImageIssuanceContext:
        owner_id = self.image_registry.owner_id if self.image_registry is not None else "ryn_host_owner"
        win = envelope.arguments.get("window") if isinstance(envelope.arguments, dict) else None
        return HostImageIssuanceContext(
            owner_id=owner_id,
            node_runtime_id=self.node_runtime_id,
            task_id=self.task_id,
            session_id=self.session_id,
            generation=envelope.generation,
            turn=self.turn,
            execution_id=envelope.execution_id,
            callback_id=envelope.callback_id,
            request_id=str(request_id or envelope.arguments_sha256 or envelope.callback_id),
            window=win,
            operation_id=str(operation_id or "").strip(),
            context_epoch=self.context_epoch,
            selection_scope=self.selection_scope,
            native_worker_instance=str(native_worker_instance or "").strip(),
            native_window=native_window,
        )

    def bind_post_commit_window_state_result(
        self,
        durable_payload: dict[str, Any] | None = None,
        *,
        durable_outbox_payload: dict[str, Any] | None = None,
        execution_id: str,
        callback_id: str,
        generation: int | None = None,
        operation_id: str | None = None,
        request_id: str | None = None,
        native_worker_instance: str | None = None,
        native_worker_id: str | None = None,
        native_window: dict[str, Any] | None = None,
        outbox_committed: bool = False,
    ) -> dict[str, Any]:
        """Mint transient `ryn-image:cap:` descriptors AFTER durable outbox commit for a pending `get_window_state`."""
        if outbox_committed is not True:
            raise JsProtocolError(
                "ryn_js_image_precommit_forbidden",
                "Cannot mint or bind host screenshot capabilities before durable outbox commit",
            )
        if self.image_registry is None:
            raise JsProtocolError(
                "ryn_js_image_registry_unavailable",
                "Cannot bind post-commit window state result without an injected HostImageRegistry",
            )
        if (
            native_worker_instance is not None
            and native_worker_id is not None
            and str(native_worker_instance).strip() != str(native_worker_id).strip()
        ):
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                "Conflicting native_worker_instance and native_worker_id provided",
            )
        worker_inst = (
            native_worker_instance if native_worker_instance is not None else native_worker_id
        )
        payload = durable_outbox_payload if durable_outbox_payload is not None else durable_payload

        try:
            with self._cond:
                self._sync_process_state_unlocked()
                target_gen = self.generation if generation is None else generation
                if type(target_gen) is not int or target_gen != self.generation:
                    raise JsProtocolError(
                        "ryn_js_stale_generation",
                        f"Stale generation {target_gen} for session {self.session_id} (expected {self.generation})",
                    )
                active = self._active
                if active is None or active.execution_id != execution_id:
                    raise JsProtocolError(
                        "ryn_js_unknown_execution",
                        f"No active execution {execution_id!r} on session {self.session_id}",
                    )
                envelope = active.pending_host_calls.get(callback_id)
                if envelope is None:
                    raise JsProtocolError(
                        "ryn_js_unknown_callback",
                        f"Unknown pending callback {callback_id!r} on execution {execution_id!r}",
                    )
                if envelope.method != "get_window_state":
                    raise JsProtocolError(
                        "ryn_js_invalid_host_result",
                        f"Callback {callback_id!r} is {envelope.method!r}, not get_window_state",
                    )
                issuance_ctx = self._build_issuance_context_unlocked(
                    envelope,
                    operation_id=operation_id,
                    request_id=request_id,
                    native_worker_instance=worker_inst,
                    native_window=native_window,
                )
        finally:
            self._flush_pending_notifications()

        return self.image_registry.bind_post_commit_window_state_result(
            payload,
            issuance_context=issuance_ctx,
            outbox_committed=outbox_committed,
            native_window=native_window,
            native_worker_instance=worker_inst,
        )

    def update_scope(
        self,
        *,
        context_epoch: str | None = None,
        selection_scope: str | None = None,
    ) -> None:
        """Update context/selection scope and retire unfetched/pinned capabilities if scope changed."""
        changed = False
        with self._cond:
            if context_epoch is not None:
                norm_epoch = validate_identifier(str(context_epoch).strip(), "context_epoch")
                if norm_epoch != self.context_epoch:
                    self.context_epoch = norm_epoch
                    changed = True
            if selection_scope is not None:
                norm_scope = validate_identifier(str(selection_scope).strip(), "selection_scope")
                if norm_scope != self.selection_scope:
                    self.selection_scope = norm_scope
                    changed = True
            if changed and self.image_registry is not None:
                self.image_registry.retire_session(
                    self.session_id,
                    reason="ryn_js_image_scope_changed",
                    cancel_pinned=True,
                )

    def drain_image_promotions(
        self,
        *,
        generation: int | None = None,
        execution_id: str | None = None,
        carrier: Callable[[PromotedImageAttachment], Any] | None = None,
        parent_request_id: str | None = None,
        max_promotions: int | None = None,
        max_total_bytes: int | None = None,
    ) -> list[ImagePromotionResult]:
        """Drain queued transient image promotions for this session through `image_registry`."""
        if self.image_registry is not None:
            return self.image_registry.drain_promotions(
                task_id=self.task_id,
                session_id=self.session_id,
                generation=generation,
                execution_id=execution_id,
                carrier=carrier,
                parent_request_id=parent_request_id,
                max_promotions=max_promotions,
                max_total_bytes=max_total_bytes,
            )

        with self._cond:
            stubs = list(self._unverified_promotion_stubs)
            remaining_stubs: deque[dict[str, Any]] = deque(maxlen=64)
            matched: list[dict[str, Any]] = []
            for stub in stubs:
                if generation is not None and stub.get("generation") != generation:
                    remaining_stubs.append(stub)
                elif execution_id is not None and stub.get("emitting_execution_id") != execution_id:
                    remaining_stubs.append(stub)
                else:
                    matched.append(stub)
            self._unverified_promotion_stubs = remaining_stubs

        results: list[ImagePromotionResult] = []
        for stub in matched:
            img = stub.get("image") or {}
            results.append(
                ImagePromotionResult(
                    promotion_id=str(stub.get("promotion_id") or "unverified"),
                    status="unavailable",
                    disposition="unavailable",
                    stage="requested",
                    artifact_id=str(img.get("artifact_id") or ""),
                    screenshot_id=str(img.get("id") or "screenshot-0"),
                    observation_id=str(img.get("observation_id") or ""),
                    operation_id=str(img.get("operation_id") or ""),
                    receipt_id=str(img.get("receipt_id") or ""),
                    emitting_execution_id=str(stub.get("emitting_execution_id") or ""),
                    issued_execution_id=str(stub.get("emitting_execution_id") or ""),
                    issued_callback_id="",
                    provenance=str(img.get("provenance") or "host_window_state"),
                    provenance_kind="unverified",
                    sha256=str(img.get("sha256") or ""),
                    mime_type=str(img.get("mime_type") or img.get("mimeType") or "image/png"),
                    byte_length=int(img.get("byte_length") or img.get("byteLength") or 0),
                    width=int(img.get("width") or 0),
                    height=int(img.get("height") or 0),
                    origin_x=int(img.get("originX") or 0),
                    origin_y=int(img.get("originY") or 0),
                    z_index=int(img.get("zIndex") or 0),
                    window=dict(img.get("window") or {}),
                    state_token=str(img.get("stateToken") or ""),
                    emission_ordinal=int(stub.get("ordinal") or 1),
                    error_code="ryn_js_image_registry_unavailable",
                    reason="No HostImageRegistry configured for session; cannot promote unverified image",
                )
            )
        return results

    def respond(
        self,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        ok: bool,
        result: Any = None,
        error: dict[str, Any] | None = None,
        session_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        """Deliver a single host_result for a pending guest host_call."""
        target_sid = self.session_id if session_id is None else str(session_id)
        if target_sid != self.session_id:
            raise JsProtocolError(
                "ryn_js_session_mismatch",
                f"Session mismatch: expected {self.session_id}, got {target_sid}",
            )
        if type(generation) is not int or generation != self.generation:
            raise JsProtocolError(
                "ryn_js_stale_generation",
                f"Stale generation {generation} for session {self.session_id} (expected {self.generation})",
            )

        try:
            with self._cond:
                self._sync_process_state_unlocked()
                active = self._active
                if active is None or active.execution_id != execution_id:
                    raise JsProtocolError(
                        "ryn_js_unknown_execution",
                        f"No active execution {execution_id!r} on session {self.session_id}",
                    )
                if callback_id in active.settled_callbacks:
                    raise JsProtocolError(
                        "ryn_js_duplicate_host_result",
                        f"Callback {callback_id!r} was already settled on execution {execution_id!r}",
                    )
                envelope = active.pending_host_calls.get(callback_id)
                if envelope is None:
                    raise JsProtocolError(
                        "ryn_js_unknown_callback",
                        f"Unknown pending callback {callback_id!r} on execution {execution_id!r}",
                    )
                issuance_ctx: HostImageIssuanceContext | None = None
                if ok and envelope.method == "get_window_state" and self.image_registry is not None:
                    issuance_ctx = self._build_issuance_context_unlocked(
                        envelope,
                        operation_id=operation_id,
                    )
        finally:
            self._flush_pending_notifications()

        effective_result = result
        if issuance_ctx is not None and self.image_registry is not None:
            effective_result = self.image_registry.ensure_window_state_result_issued(
                result,
                issuance_context=issuance_ctx,
            )

        frame = build_host_result_frame(
            session_id=self.session_id,
            generation=generation,
            execution_id=execution_id,
            callback_id=callback_id,
            ok=ok,
            result=effective_result,
            error=error,
        )
        try:
            with self._cond:
                self._sync_process_state_unlocked()
                active = self._active
                if active is None or active.execution_id != execution_id:
                    raise JsProtocolError(
                        "ryn_js_unknown_execution",
                        f"No active execution {execution_id!r} on session {self.session_id}",
                    )
                if callback_id in active.settled_callbacks:
                    raise JsProtocolError(
                        "ryn_js_duplicate_host_result",
                        f"Callback {callback_id!r} was already settled on execution {execution_id!r}",
                    )
                if callback_id not in active.pending_host_calls:
                    raise JsProtocolError(
                        "ryn_js_unknown_callback",
                        f"Unknown pending callback {callback_id!r} on execution {execution_id!r}",
                    )
                active.pending_host_calls.pop(callback_id, None)
                active.settled_callbacks.add(callback_id)
                if not active.pending_host_calls:
                    active.status = "running"
        finally:
            self._flush_pending_notifications()

        self._send_frame(frame)

    def cancel(self, *, reason: str = "ryn_js_cancelled", timeout: float = 1.0) -> JsExecutionSnapshot | None:
        """Cancel the currently active evaluation; kill the worker if it does not stop within timeout."""
        if self.image_registry is not None:
            self.image_registry.retire_session(
                self.session_id,
                reason=reason,
                cancel_pinned=True,
            )
        try:
            with self._cond:
                self._sync_process_state_unlocked()
                active = self._active
                if active is None:
                    if self.status == "lost" and self._history:
                        last_key = next(reversed(self._history))
                        return self._snapshot_for(self._history[last_key])
                    return None
                exec_id = active.execution_id
                gen = self.generation
        finally:
            self._flush_pending_notifications()

        try:
            self._send_frame(
                build_cancel_frame(
                    session_id=self.session_id,
                    generation=gen,
                    reason=reason,
                    execution_id=exec_id,
                )
            )
        except JsRuntimeError:
            pass

        deadline = time.monotonic() + max(0.05, float(timeout))
        try:
            with self._cond:
                while self._active is not None and self._active.execution_id == exec_id:
                    self._sync_process_state_unlocked()
                    if self._active is None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
        finally:
            self._flush_pending_notifications()

        snapshot = self.poll(exec_id, timeout=0.0)
        if snapshot.status not in {"completed", "failed", "cancelled", "lost"}:
            with self._cond:
                self._force_kill_unlocked(
                    "ryn_js_cancel_forced_kill",
                    "QuickJS worker did not exit cleanly after cancel request.",
                )
            self._flush_pending_notifications()
            snapshot = self.poll(exec_id, timeout=0.0)
        return snapshot

    def end_turn(self, *, timeout: float = 2.0) -> None:
        """Clear turn-scoped screenshot/window-state allowances on the worker."""
        if self.image_registry is not None:
            self.image_registry.retire_session(
                self.session_id,
                reason="ryn_js_turn_ended",
                cancel_pinned=False,
                reset_promotion_budget=True,
            )
        if not self.is_alive:
            return
        with self._cond:
            prev_turn = self.turn
            gen = self.generation
        self._send_frame(build_end_turn_frame(session_id=self.session_id, generation=gen))
        deadline = time.monotonic() + max(0.05, float(timeout))
        with self._cond:
            while self.is_alive and self.turn == prev_turn:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)

    def close(self) -> None:
        """Terminate the QuickJS worker subprocess, release the Windows Job handle, close pipes, join readers, and close lease."""
        if self.image_registry is not None:
            self.image_registry.retire_session(
                self.session_id,
                reason="ryn_js_session_closed",
                cancel_pinned=True,
            )
        with self._cond:
            self._sync_process_state_unlocked()
            self._invalidate_live_verification_unlocked()
            if self.status == "closed" and not self.cleanup_incomplete and self.resource_lease.closed:
                return
            self.status = "closing"
            proc = self.process
            gen = self.generation
            stdout_thread = self._stdout_thread
            stderr_thread = self._stderr_thread
            lease = self.resource_lease
        self._flush_pending_notifications()

        if proc is not None and proc.poll() is None:
            try:
                self._send_frame(build_close_frame(session_id=self.session_id, generation=gen))
            except Exception:
                pass
            _wait_for_poll_exit(proc, 0.25)

        exited = _cleanup_process(proc, stdout_thread, stderr_thread, lease)

        with self._cond:
            self._invalidate_live_verification_unlocked()
            if exited:
                self.cleanup_incomplete = False
                self._quarantine_notify_pending = False
                self.status = "closed"
            else:
                self.status = "lost"
                self.close_reason = "ryn_js_cleanup_incomplete"
                self._record_quarantine_unlocked()
            if self._active is not None:
                self._active.status = "cancelled" if exited else "lost"
                self._active.error = {
                    "code": "ryn_js_session_closed" if exited else "ryn_js_cleanup_incomplete",
                    "message": "QuickJS session closed." if exited else "QuickJS worker termination could not be confirmed.",
                }
                self._record_history_unlocked(self._active)
                self._active = None
            self._cond.notify_all()
        self._flush_pending_notifications()

    def _resolve_execution_unlocked(self, execution_id: str | None) -> _ActiveExecutionState:
        if execution_id is None:
            if self._active is not None:
                return self._active
            if self._history:
                last_key = next(reversed(self._history))
                return self._history[last_key]
            raise JsProtocolError("ryn_js_no_execution", "No execution has been started on this session.")
        if self._active is not None and self._active.execution_id == execution_id:
            return self._active
        if execution_id in self._history:
            return self._history[execution_id]
        raise JsProtocolError("ryn_js_unknown_execution", f"Unknown execution_id {execution_id!r}")

    def to_status_snapshot(self) -> dict[str, Any]:
        """Return pure in-memory session status metadata without filesystem or OS calls."""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "turn": self.turn,
            "status": self.status,
            "mode": self.profile.mode,
            "profile_digest": self.profile.profile_digest,
            "files_verified": self.files_verified,
            "actual_process_identity_verified": self.actual_process_identity_verified,
            "runtime_handshake_verified": self.runtime_handshake_verified,
            "cleanup_incomplete": self.cleanup_incomplete,
            "lease_closed": self.resource_lease.closed,
            "pinned_handle_count": self.resource_lease.pinned_handle_count,
            "pinned_directory_count": self.resource_lease.pinned_directory_count,
            "process_identity": (
                self.verified_process_identity.to_public_metadata()
                if self.verified_process_identity is not None
                else None
            ),
        }


@dataclass
class _InFlightSessionStart:
    token_id: int
    task_id: str
    generation: int
    session_id: str
    context_epoch: str
    selection_scope: str
    task_close_seq: int
    manager_close_seq: int
    done_event: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    session: JsRuntimeSession | None = None
    error: BaseException | None = None


class JsRuntimeManager:
    """Task-scoped manager for isolated QuickJS WASM worker sessions.

    Construction and `get_status()` are strictly pure in-memory operations (zero filesystem
    stats/opens, zero environment/PATH lookups, zero subprocess spawns).
    """

    def __init__(
        self,
        *,
        development_config: JsRuntimeDevelopmentConfig | None = None,
        development_profile_root: str | Path | None = None,
        startup_timeout: float = 10.0,
        image_registry: HostImageRegistry | None = None,
        context_epoch: str = "epoch_1",
        selection_scope: str = "default",
        _production_loader: Callable[[], VerifiedJsResourceLease] | None = None,
        _before_resume_verifier: Callable[..., VerifiedProcessIdentity] | None = None,
    ) -> None:
        if development_config is not None and development_profile_root is not None:
            raise JsResourceError(
                "ryn_js_resource_invalid_mode",
                "Cannot specify both development_config and development_profile_root",
            )
        if development_profile_root is not None:
            development_config = JsRuntimeDevelopmentConfig(
                explicit_profile_root=Path(development_profile_root),
            )
        if development_config is not None and not isinstance(development_config, JsRuntimeDevelopmentConfig):
            raise JsResourceError(
                "ryn_js_resource_invalid_mode",
                "development_config must be a JsRuntimeDevelopmentConfig instance",
            )

        self._development_config: JsRuntimeDevelopmentConfig | None = development_config
        self._custom_production_loader: Callable[[], VerifiedJsResourceLease] | None = _production_loader
        self._used_custom_hooks: bool = (
            _production_loader is not None or _before_resume_verifier is not None
        )
        self._mode: str = "source_dev" if development_config is not None else "packaged_release"
        self._production_loader = _open_production_lease_from_host
        self._before_resume_verifier = _before_resume_verifier
        self._startup_timeout = max(0.5, float(startup_timeout))
        self.image_registry = image_registry
        self._default_context_epoch = validate_identifier(str(context_epoch or "epoch_1").strip(), "context_epoch")
        self._default_selection_scope = validate_identifier(str(selection_scope or "default").strip(), "selection_scope")
        self._lock = threading.RLock()
        self._sessions: dict[str, JsRuntimeSession] = {}
        self._inflight_starts: dict[str, _InFlightSessionStart] = {}
        self._next_start_token_id: int = 0
        self._task_close_seq: dict[str, int] = {}
        self._manager_close_seq: int = 0
        self._quarantined_sessions: dict[str, JsRuntimeSession] = {}
        self._quarantined_by_task: dict[str, list[JsRuntimeSession]] = {}
        self._generations: dict[str, int] = {}
        self._task_scopes: dict[str, tuple[str, str]] = {}

        # Pure in-memory cached static profile verification state (live runtime flags are derived from active sessions)
        self._files_verified: bool = False
        self._last_verified_profile_metadata: dict[str, Any] | None = None
        self._last_error_code: str | None = None
        self._last_error_message: str | None = None

        atexit.register(self.close)

    @property
    def mode(self) -> str:
        return self._mode

    def _quarantine_session(self, session: JsRuntimeSession) -> None:
        with self._lock:
            self._quarantined_sessions[session.session_id] = session
            task_list = self._quarantined_by_task.setdefault(session.task_id, [])
            if session not in task_list:
                task_list.append(session)
            self._last_error_code = "ryn_js_cleanup_incomplete"
            self._last_error_message = (
                f"QuickJS worker session {session.session_id} for task {session.task_id!r} "
                "has unconfirmed process exit; resource lease remains pinned in quarantine."
            )

    def _reap_quarantined(self, task_id: str | None = None) -> bool:
        """Attempt to reap quarantined sessions that have since exited outside `self._lock`; return True if none remain unexited."""
        with self._lock:
            if task_id is not None:
                candidates = list(self._quarantined_by_task.get(task_id, ()))
            else:
                candidates = list(self._quarantined_sessions.values())
        if not candidates:
            return True

        reaped: list[JsRuntimeSession] = []
        for q_sess in candidates:
            if q_sess.reap_if_exited():
                reaped.append(q_sess)

        with self._lock:
            for q_sess in reaped:
                self._quarantined_sessions.pop(q_sess.session_id, None)
                t_list = self._quarantined_by_task.get(q_sess.task_id)
                if t_list and q_sess in t_list:
                    t_list.remove(q_sess)
                    if not t_list:
                        self._quarantined_by_task.pop(q_sess.task_id, None)
            if task_id is not None:
                remaining = [
                    q
                    for q in self._quarantined_by_task.get(task_id, ())
                    if q.cleanup_incomplete or not q.resource_lease.closed
                ]
            else:
                remaining = [
                    q
                    for q in self._quarantined_sessions.values()
                    if q.cleanup_incomplete or not q.resource_lease.closed
                ]
            return len(remaining) == 0

    def get_status(self) -> dict[str, Any]:
        """Return pure in-memory status derived from current owned live sessions without FS, env, or OS calls."""
        with self._lock:
            live_ready_sessions = [
                s
                for s in self._sessions.values()
                if s.status == "ready"
                and not s.resource_lease.closed
                and not s.cleanup_incomplete
            ]
            active_count = sum(
                1
                for s in self._sessions.values()
                if s.status in {"starting", "ready"}
                and not s.resource_lease.closed
                and not s.cleanup_incomplete
            )
            quarantined_ids = {
                s.session_id
                for s in self._quarantined_sessions.values()
                if s.cleanup_incomplete or not s.resource_lease.closed
            } | {
                s.session_id
                for s in self._sessions.values()
                if s.cleanup_incomplete
            }
            quarantined_count = len(quarantined_ids)
            cleanup_incomplete = quarantined_count > 0

            live_identity_verified = any(
                s.actual_process_identity_verified for s in live_ready_sessions
            )
            live_handshake_verified = any(
                s.runtime_handshake_verified for s in live_ready_sessions
            )
            live_prod_authentic = (
                bool(live_ready_sessions)
                and all(
                    not s.used_custom_verifier
                    and _verify_host_installed_production_lease(s.resource_lease)
                    for s in live_ready_sessions
                )
            )
            profile_meta: dict[str, Any] | None = None
            if self._last_verified_profile_metadata is not None:
                profile_meta = dict(self._last_verified_profile_metadata)
                profile_meta["handshake_verified"] = live_handshake_verified

            return {
                "mode": self._mode,
                "is_production": self._mode == "packaged_release",
                "is_development": self._mode == "source_dev",
                "release_verified": (
                    self._mode == "packaged_release"
                    and not self._used_custom_hooks
                    and self._files_verified
                    and live_identity_verified
                    and live_handshake_verified
                    and live_prod_authentic
                    and not cleanup_incomplete
                ),
                "installed_ready": False,
                "files_verified": self._files_verified,
                "actual_process_identity_verified": live_identity_verified,
                "runtime_handshake_verified": live_handshake_verified,
                "active_session_count": active_count,
                "quarantined_session_count": quarantined_count,
                "cleanup_incomplete": cleanup_incomplete,
                "active_lease_count": active_resource_lease_count(),
                "profile": profile_meta,
                "last_error_code": self._last_error_code,
                "last_error_message": self._last_error_message,
            }

    def _acquire_session_lease(self) -> VerifiedJsResourceLease:
        """Open a fresh, handle-pinned `VerifiedJsResourceLease` for a new worker session."""
        if self._development_config is not None:
            if self._custom_production_loader is not None:
                raise JsResourceError(
                    "ryn_js_resource_invalid_mode",
                    "Development mode does not accept a custom _production_loader hook",
                )
            lease = open_development_js_runtime_lease(
                explicit_profile_root=self._development_config.explicit_profile_root,
                expected_spec=self._development_config.expected_spec,
                expected_platform=self._development_config.expected_platform,
                expected_arch=self._development_config.expected_arch,
            )
            if lease.profile.mode != "source_dev" or lease.profile.is_production:
                lease.close()
                raise JsResourceError(
                    "ryn_js_resource_invalid_mode",
                    "Development lease resolver returned non-source_dev profile mode",
                )
            return lease

        if self._used_custom_hooks:
            raise JsResourceError(
                "ryn_js_resource_invalid_mode",
                "Production mode does not accept custom loader or pre-resume verifier hooks without explicit source_dev configuration",
            )

        lease = _open_production_lease_from_host()
        if not isinstance(lease, VerifiedJsResourceLease) or lease.closed:
            if isinstance(lease, VerifiedJsResourceLease):
                lease.close()
            raise JsResourceError(
                "ryn_js_resource_lease_closed",
                "Production loader did not return an open VerifiedJsResourceLease",
            )
        if not _verify_host_installed_production_lease(lease):
            lease.close()
            raise JsResourceError(
                "ryn_js_resource_invalid_mode",
                "Production mode requires an authentic packaged_release lease verified against the host-installed root and embedded commitment",
            )
        return lease

    def get_session(
        self,
        task_id: str,
        *,
        include_inactive: bool = True,
    ) -> JsRuntimeSession | None:
        key = str(task_id or "").strip()
        with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return None
        session._sync_process_state()
        with self._lock:
            current = self._sessions.get(key)
            if current is not session:
                return None
            if session.status == "closed":
                return None
            if include_inactive or session.is_alive:
                return session
            return None

    def get_or_create_session(self, task_id: str) -> JsRuntimeSession:
        key = validate_identifier(str(task_id or "").strip(), "task_id")
        while True:
            if not self._reap_quarantined(key):
                msg = (
                    f"Previous QuickJS worker for task {key!r} has unconfirmed process exit; "
                    "resource lease remains pinned in quarantine."
                )
                with self._lock:
                    self._last_error_code = "ryn_js_cleanup_incomplete"
                    self._last_error_message = msg
                raise JsRuntimeError("ryn_js_cleanup_incomplete", msg)

            self._reap_quarantined()

            with self._lock:
                existing = self._sessions.get(key)
                inflight = self._inflight_starts.get(key)

            if existing is not None:
                existing._sync_process_state()
                if existing.is_alive:
                    with self._lock:
                        if self._sessions.get(key) is existing:
                            return existing
                    continue

                stale_to_close: JsRuntimeSession | None = None
                with self._lock:
                    if self._sessions.get(key) is existing:
                        if existing.generation > self._generations.get(key, 0):
                            self._generations[key] = existing.generation
                        self._sessions.pop(key, None)
                        stale_to_close = existing
                if stale_to_close is not None:
                    stale_to_close.close()
                    if stale_to_close.cleanup_incomplete or not stale_to_close.resource_lease.closed:
                        self._quarantine_session(stale_to_close)
                        msg = (
                            f"Previous QuickJS worker for task {key!r} failed termination confirmation; "
                            "resource lease remains pinned in quarantine."
                        )
                        with self._lock:
                            self._last_error_code = "ryn_js_cleanup_incomplete"
                            self._last_error_message = msg
                        raise JsRuntimeError("ryn_js_cleanup_incomplete", msg)
                continue

            if inflight is not None and not inflight.cancelled:
                inflight.done_event.wait()
                if inflight.cancelled:
                    continue
                if inflight.error is not None:
                    raise inflight.error
                if inflight.session is not None:
                    with self._lock:
                        if self._sessions.get(key) is inflight.session:
                            return inflight.session
                continue

            reservation: _InFlightSessionStart | None = None
            with self._lock:
                if (
                    key in self._quarantined_by_task
                    and any(
                        q.cleanup_incomplete or not q.resource_lease.closed
                        for q in self._quarantined_by_task.get(key, ())
                    )
                ):
                    msg = (
                        f"Previous QuickJS worker for task {key!r} has unconfirmed process exit; "
                        "resource lease remains pinned in quarantine."
                    )
                    self._last_error_code = "ryn_js_cleanup_incomplete"
                    self._last_error_message = msg
                    raise JsRuntimeError("ryn_js_cleanup_incomplete", msg)

                if key in self._sessions:
                    continue
                current_inflight = self._inflight_starts.get(key)
                if current_inflight is not None and not current_inflight.cancelled:
                    continue

                active_sessions = (
                    sum(
                        1
                        for s in self._sessions.values()
                        if s.status in {"starting", "ready"}
                        and not s.resource_lease.closed
                        and not s.cleanup_incomplete
                    )
                    + sum(
                        1
                        for f in self._inflight_starts.values()
                        if not f.cancelled
                    )
                    + len(self._quarantined_sessions)
                )
                if active_sessions >= MAX_MANAGER_ACTIVE_SESSIONS:
                    raise JsResourceError(
                        "ryn_js_resource_lease_limit_exceeded",
                        f"Active QuickJS session count exceeds limit {MAX_MANAGER_ACTIVE_SESSIONS}",
                    )

                if self._development_config is None and self._used_custom_hooks:
                    err = JsResourceError(
                        "ryn_js_resource_invalid_mode",
                        "Production mode does not accept custom loader or pre-resume verifier hooks without explicit source_dev configuration",
                    )
                    self._last_error_code = err.code
                    self._last_error_message = err.message
                    raise err
                if self._development_config is not None and self._custom_production_loader is not None:
                    err = JsResourceError(
                        "ryn_js_resource_invalid_mode",
                        "Development mode does not accept a custom _production_loader hook",
                    )
                    self._last_error_code = err.code
                    self._last_error_message = err.message
                    raise err

                self._next_start_token_id += 1
                generation = self._generations.get(key, 0) + 1
                self._generations[key] = generation
                epoch, scope = self._task_scopes.get(
                    key,
                    (self._default_context_epoch, self._default_selection_scope),
                )
                session_id = f"ryn_js_{key}_{generation}_{uuid.uuid4().hex[:8]}"
                reservation = _InFlightSessionStart(
                    token_id=self._next_start_token_id,
                    task_id=key,
                    generation=generation,
                    session_id=session_id,
                    context_epoch=epoch,
                    selection_scope=scope,
                    task_close_seq=self._task_close_seq.get(key, 0),
                    manager_close_seq=self._manager_close_seq,
                )
                self._inflight_starts[key] = reservation

            lease: VerifiedJsResourceLease | None = None
            session: JsRuntimeSession | None = None
            try:
                try:
                    lease = self._acquire_session_lease()
                except JsRuntimeError as exc:
                    with self._lock:
                        self._last_error_code = exc.code
                        self._last_error_message = exc.message
                    raise
                except Exception as exc:
                    with self._lock:
                        self._last_error_code = "ryn_js_runtime_unavailable"
                        self._last_error_message = str(exc)
                    raise JsRuntimeError("ryn_js_runtime_unavailable", str(exc)) from exc

                with self._lock:
                    self._files_verified = True
                    self._last_verified_profile_metadata = lease.profile.to_public_metadata()
                    superseded_before_spawn = (
                        reservation.cancelled
                        or self._inflight_starts.get(key) is not reservation
                        or self._task_close_seq.get(key, 0) != reservation.task_close_seq
                        or self._manager_close_seq != reservation.manager_close_seq
                    )

                if superseded_before_spawn:
                    lease.close()
                    raise JsRuntimeError(
                        "ryn_js_session_closed",
                        f"QuickJS session startup for task {key!r} was cancelled before spawn.",
                    )

                try:
                    session = JsRuntimeSession(
                        task_id=key,
                        session_id=reservation.session_id,
                        generation=reservation.generation,
                        resource_lease=lease,
                        startup_timeout=self._startup_timeout,
                        image_registry=self.image_registry,
                        context_epoch=reservation.context_epoch,
                        selection_scope=reservation.selection_scope,
                        before_resume_verifier=self._before_resume_verifier,
                        on_quarantine=self._quarantine_session,
                    )
                except JsRuntimeError as exc:
                    with self._lock:
                        is_quarantined = any(
                            q.resource_lease is lease for q in self._quarantined_sessions.values()
                        )
                        if not is_quarantined or self._last_error_code != "ryn_js_cleanup_incomplete":
                            self._last_error_code = exc.code
                            self._last_error_message = exc.message
                    if not is_quarantined:
                        lease.close()
                    raise
                except Exception as exc:
                    with self._lock:
                        is_quarantined = any(
                            q.resource_lease is lease for q in self._quarantined_sessions.values()
                        )
                        if not is_quarantined or self._last_error_code != "ryn_js_cleanup_incomplete":
                            self._last_error_code = "ryn_js_startup_failed"
                            self._last_error_message = str(exc)
                    if not is_quarantined:
                        lease.close()
                    raise

                scope_to_sync: tuple[str, str] | None = None
                with self._lock:
                    superseded_after_spawn = (
                        reservation.cancelled
                        or self._inflight_starts.get(key) is not reservation
                        or self._task_close_seq.get(key, 0) != reservation.task_close_seq
                        or self._manager_close_seq != reservation.manager_close_seq
                    )
                    if not superseded_after_spawn:
                        if session.generation > self._generations.get(key, 0):
                            self._generations[key] = session.generation
                        self._last_error_code = None
                        self._last_error_message = None
                        if self._last_verified_profile_metadata is not None:
                            self._last_verified_profile_metadata["handshake_verified"] = bool(
                                session.runtime_handshake_verified
                            )
                        current_scope = self._task_scopes.get(key)
                        if current_scope is not None and current_scope != (
                            reservation.context_epoch,
                            reservation.selection_scope,
                        ):
                            scope_to_sync = current_scope
                        self._sessions[key] = session
                        reservation.session = session

                if superseded_after_spawn:
                    session.close()
                    if session.cleanup_incomplete or not session.resource_lease.closed:
                        self._quarantine_session(session)
                    raise JsRuntimeError(
                        "ryn_js_session_closed",
                        f"QuickJS session startup for task {key!r} was cancelled concurrently.",
                    )

                if scope_to_sync is not None:
                    session.update_scope(
                        context_epoch=scope_to_sync[0],
                        selection_scope=scope_to_sync[1],
                    )
                return session
            except BaseException as exc:
                reservation.error = exc
                raise
            finally:
                with self._lock:
                    if self._inflight_starts.get(key) is reservation:
                        self._inflight_starts.pop(key, None)
                reservation.done_event.set()

    def update_task_scope(
        self,
        task_id: str,
        *,
        context_epoch: str | None = None,
        selection_scope: str | None = None,
    ) -> None:
        key = validate_identifier(str(task_id or "").strip(), "task_id")
        with self._lock:
            cur_epoch, cur_scope = self._task_scopes.get(
                key,
                (self._default_context_epoch, self._default_selection_scope),
            )
            new_epoch = (
                validate_identifier(str(context_epoch).strip(), "context_epoch")
                if context_epoch is not None
                else cur_epoch
            )
            new_scope = (
                validate_identifier(str(selection_scope).strip(), "selection_scope")
                if selection_scope is not None
                else cur_scope
            )
            self._task_scopes[key] = (new_epoch, new_scope)
            session = self._sessions.get(key)
        if session is not None:
            session.update_scope(context_epoch=new_epoch, selection_scope=new_scope)
        elif self.image_registry is not None and (new_epoch != cur_epoch or new_scope != cur_scope):
            self.image_registry.retire_task(
                key,
                reason="ryn_js_image_scope_changed",
                cancel_pinned=True,
            )

    def bind_post_commit_window_state_result(
        self,
        task_id: str,
        durable_payload: dict[str, Any] | None = None,
        *,
        durable_outbox_payload: dict[str, Any] | None = None,
        execution_id: str,
        callback_id: str,
        generation: int | None = None,
        operation_id: str | None = None,
        request_id: str | None = None,
        native_worker_instance: str | None = None,
        native_worker_id: str | None = None,
        native_window: dict[str, Any] | None = None,
        outbox_committed: bool = False,
    ) -> dict[str, Any]:
        if outbox_committed is not True:
            raise JsProtocolError(
                "ryn_js_image_precommit_forbidden",
                "Cannot mint or bind host screenshot capabilities before durable outbox commit",
            )
        session = self.get_session(task_id, include_inactive=False)
        if session is None:
            raise JsProtocolError("ryn_js_no_session", f"No active QuickJS session for task {task_id!r}")
        return session.bind_post_commit_window_state_result(
            durable_payload,
            durable_outbox_payload=durable_outbox_payload,
            execution_id=execution_id,
            callback_id=callback_id,
            generation=generation,
            operation_id=operation_id,
            request_id=request_id,
            native_worker_instance=native_worker_instance,
            native_worker_id=native_worker_id,
            native_window=native_window,
            outbox_committed=outbox_committed,
        )

    def drain_image_promotions(
        self,
        task_id: str,
        *,
        generation: int | None = None,
        execution_id: str | None = None,
        carrier: Callable[[PromotedImageAttachment], Any] | None = None,
        parent_request_id: str | None = None,
        max_promotions: int | None = None,
        max_total_bytes: int | None = None,
    ) -> list[ImagePromotionResult]:
        session = self.get_session(task_id, include_inactive=True)
        if session is None:
            return []
        return session.drain_image_promotions(
            generation=generation,
            execution_id=execution_id,
            carrier=carrier,
            parent_request_id=parent_request_id,
            max_promotions=max_promotions,
            max_total_bytes=max_total_bytes,
        )

    def evaluate(
        self,
        task_id: str,
        code: str,
        *,
        execution_id: str | None = None,
        script_id: str | None = None,
        limits: dict[str, int] | None = None,
    ) -> tuple[JsRuntimeSession, str]:
        session = self.get_or_create_session(task_id)
        exec_id = session.evaluate(
            code,
            execution_id=execution_id,
            script_id=script_id,
            limits=limits,
        )
        return session, exec_id

    def run_to_completion(
        self,
        task_id: str,
        code: str,
        *,
        host_handler: Callable[[PendingHostCallEnvelope], Any] | None = None,
        limits: dict[str, int] | None = None,
        timeout: float = 5.0,
    ) -> JsExecutionSnapshot:
        """Run a guest JS cell to completion, dispatching host_calls through host_handler."""
        session, exec_id = self.evaluate(task_id, code, limits=limits)
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                session.cancel(reason="ryn_js_wall_timeout", timeout=0.5)
                return session.poll(exec_id, timeout=0.0)
            envelope = session.next_host_call(exec_id, timeout=min(0.25, remaining))
            if envelope is not None:
                try:
                    if host_handler is None:
                        session.respond(
                            execution_id=exec_id,
                            generation=envelope.generation,
                            callback_id=envelope.callback_id,
                            ok=False,
                            error={
                                "code": "ryn_sky_host_unavailable",
                                "message": f"No host handler configured for sky.{envelope.method}",
                                "not_executed": True,
                                "outcome": "not_executed",
                            },
                        )
                    else:
                        try:
                            result = host_handler(envelope)
                        except JsRuntimeError as exc:
                            session.respond(
                                execution_id=exec_id,
                                generation=envelope.generation,
                                callback_id=envelope.callback_id,
                                ok=False,
                                error={
                                    "code": exc.code,
                                    "message": str(exc),
                                    "not_executed": True,
                                    "outcome": "not_executed",
                                },
                            )
                        except Exception as exc:
                            session.respond(
                                execution_id=exec_id,
                                generation=envelope.generation,
                                callback_id=envelope.callback_id,
                                ok=False,
                                error={
                                    "code": "ryn_sky_host_error",
                                    "message": str(exc),
                                    "not_executed": False,
                                    "outcome": "failed",
                                },
                            )
                        else:
                            session.respond(
                                execution_id=exec_id,
                                generation=envelope.generation,
                                callback_id=envelope.callback_id,
                                ok=True,
                                result=result,
                            )
                except JsProtocolError as exc:
                    if exc.code in {"ryn_js_unknown_execution", "ryn_js_stale_generation"}:
                        snap = session.poll(exec_id, timeout=0.0)
                        if snap.status in {"completed", "failed", "cancelled", "lost"}:
                            return snap
                    raise
                continue

            snapshot = session.poll(exec_id, timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            if snapshot.status in {"completed", "failed", "cancelled", "lost"}:
                return snapshot

    def cancel_task(self, task_id: str, *, reason: str = "ryn_js_cancelled") -> JsExecutionSnapshot | None:
        key = str(task_id or "").strip()
        if self.image_registry is not None and key:
            self.image_registry.retire_task(key, reason=reason, cancel_pinned=True)
        session = self.get_session(task_id)
        if session is None:
            return None
        snap = session.cancel(reason=reason)
        with self._lock:
            if session.generation > self._generations.get(key, 0):
                self._generations[key] = session.generation
        return snap

    def end_turn(self, task_id: str) -> None:
        key = str(task_id or "").strip()
        session = self.get_session(task_id)
        if session is not None:
            session.end_turn()
            with self._lock:
                if session.generation > self._generations.get(key, 0):
                    self._generations[key] = session.generation

    def close_task(self, task_id: str) -> None:
        key = str(task_id or "").strip()
        if self.image_registry is not None and key:
            self.image_registry.retire_task(key, reason="ryn_js_session_closed", cancel_pinned=True)
        with self._lock:
            self._task_close_seq[key] = self._task_close_seq.get(key, 0) + 1
            inflight = self._inflight_starts.pop(key, None)
            if inflight is not None:
                inflight.cancelled = True
                if inflight.generation > self._generations.get(key, 0):
                    self._generations[key] = inflight.generation
            session = self._sessions.pop(key, None)
            if session is not None and session.generation > self._generations.get(key, 0):
                self._generations[key] = session.generation
        if session is not None:
            session.close()
            if session.cleanup_incomplete or not session.resource_lease.closed:
                self._quarantine_session(session)
        self._reap_quarantined(key if key else None)

    def close(self) -> None:
        with self._lock:
            self._manager_close_seq += 1
            for inflight_key, inflight in list(self._inflight_starts.items()):
                inflight.cancelled = True
                if inflight.generation > self._generations.get(inflight_key, 0):
                    self._generations[inflight_key] = inflight.generation
            self._inflight_starts.clear()
            sessions = list(self._sessions.values())
            for key, session in self._sessions.items():
                if session.generation > self._generations.get(key, 0):
                    self._generations[key] = session.generation
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
                if session.cleanup_incomplete or not session.resource_lease.closed:
                    self._quarantine_session(session)
            except Exception:
                pass
        self._reap_quarantined()
