"""Owned native worker lifecycle and bounded JSONL request/event transport for Ryn computer control."""

from __future__ import annotations

import copy
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from ...llm_package import process_guard
from .binary import (
    ArtifactLease,
    BridgeConfigIdentity,
    CachedBinding,
    ProductionBinaryResolver,
    VerifiedProcessIdentity,
    build_bridge_config_identity,
    open_dev_executable_lease,
    resolve_owner_id,
    verify_lease_release_authority,
    verify_suspended_process_handle,
)
from .protocol import (
    EXPECTED_WORKER_NAME,
    EXPECTED_WORKER_VERSION,
    MAX_EVENT_QUEUE,
    MAX_PENDING_REQUESTS,
    MAX_RESPONSE_FRAME_BYTES,
    MAX_STDERR_BYTES,
    PROTOCOL_SEMVER,
    BoundedJsonlReader,
    NativeBridgeError,
    NativeBridgeRejected,
    bridge_error_from_native,
    encode_request_frame,
    parse_wire_envelope,
    request_id_correlation_key,
    validate_action_cancel_result,
    validate_initialize_result,
    validate_session_end_result,
    validate_shutdown_result,
)

CLIENT_NAME = "ryn_python_native_bridge"
CLIENT_VERSION = "0.1.0"

DEFAULT_HANDSHAKE_TIMEOUT_S = 5.0
DEFAULT_REQUEST_TIMEOUT_S = 15.0
DEFAULT_CONTROL_TIMEOUT_S = 2.0
MAX_UNCORRELATED_EVENTS = 32


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _worker_environment(private_dir: str) -> dict[str, str]:
    keep = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(
        HOME=private_dir,
        USERPROFILE=private_dir,
        APPDATA=private_dir,
        LOCALAPPDATA=private_dir,
        TMP=private_dir,
        TEMP=private_dir,
        TMPDIR=private_dir,
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        RUST_BACKTRACE="0",
    )
    return env


def default_bundled_executable_candidates() -> list[Path]:
    """Return fixed frozen bundle executable path without searching PATH, target/debug, or source sidecars."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return [exe_dir / "ryn_computer_use.exe"]
    return []


def acquire_worker_artifact_lease(
    *,
    bundled_executable: str | Path | None = None,
    dev_executable: str | Path | list[str] | tuple[str, ...] | None = None,
    artifact_resolver: Any = None,
    manifest_path: str | Path | None = None,
    bundle_root: str | Path | None = None,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
) -> ArtifactLease:
    """Acquire a verified `ArtifactLease` (or explicit development lease) before spawning a worker."""
    if artifact_resolver is not None:
        lease = artifact_resolver.acquire_lease(phase="pre_dispatch")
        if not isinstance(lease, ArtifactLease):
            raise NativeBridgeRejected(
                "agent_computer_binary_invalid",
                phase="pre_dispatch",
            )
        if getattr(lease, "_release_proof", None) is not None and not verify_lease_release_authority(lease):
            lease.close()
            raise NativeBridgeRejected(
                "agent_computer_binary_tampered",
                phase="pre_dispatch",
            )
        return lease
    if dev_executable is not None:
        return open_dev_executable_lease(
            dev_executable,
            expected_worker_name=expected_worker_name,
            expected_worker_version=expected_worker_version,
            expected_protocol_version=expected_protocol_version,
            phase="pre_dispatch",
        )
    resolver = ProductionBinaryResolver(
        manifest_path=manifest_path,
        bundle_root=bundle_root,
        bundled_executable=bundled_executable,
        expected_worker_name=expected_worker_name,
        expected_worker_version=expected_worker_version,
        expected_protocol_version=expected_protocol_version,
    )
    return resolver.acquire_lease(phase="pre_dispatch")


def resolve_verified_worker_command(
    *,
    bundled_executable: str | Path | None = None,
    dev_executable: str | Path | list[str] | tuple[str, ...] | None = None,
    artifact_resolver: Any = None,
    manifest_path: str | Path | None = None,
    bundle_root: str | Path | None = None,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
) -> list[str]:
    """Resolve the exact native worker command via the authoritative manifest or explicit dev command."""
    lease = acquire_worker_artifact_lease(
        bundled_executable=bundled_executable,
        dev_executable=dev_executable,
        artifact_resolver=artifact_resolver,
        manifest_path=manifest_path,
        bundle_root=bundle_root,
        expected_worker_name=expected_worker_name,
        expected_worker_version=expected_worker_version,
        expected_protocol_version=expected_protocol_version,
    )
    try:
        return list(lease.command)
    finally:
        lease.close()


resolve_verified_worker_artifact = acquire_worker_artifact_lease


def _spawn_guarded_worker(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    before_resume: Callable[..., Any] | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn the native worker suspended with CREATE_NO_WINDOW, verify identity in Job before resume."""
    if os.name != "nt":
        raise NativeBridgeRejected(
            "agent_computer_windows_required",
            phase="pre_dispatch",
        )
    if not args or not isinstance(args[0], str) or not args[0].strip():
        raise NativeBridgeRejected(
            "agent_computer_executable_not_found",
            phase="pre_dispatch",
        )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
            startupinfo=startup,
        )
    except (OSError, ValueError):
        raise NativeBridgeRejected(
            "agent_computer_spawn_failed",
            phase="pre_dispatch",
        ) from None

    def _default_pre_resume_check(proc: Any, _job: Any = None) -> None:
        ident = verify_suspended_process_handle(
            proc,
            expected_executable_path=args[0],
            phase="pre_dispatch",
        )
        try:
            proc._ryn_verified_identity = ident
        except Exception:
            pass

    hook = before_resume if before_resume is not None else _default_pre_resume_check

    try:
        process._ryn_job = process_guard._windows_job(  # type: ignore[attr-defined]
            process,
            before_resume=hook,
        )
    except BaseException as exc:
        for stream_attr in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_attr, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass
        if isinstance(exc, NativeBridgeError):
            raise
        raise NativeBridgeRejected(
            "agent_computer_spawn_failed",
            phase="pre_dispatch",
        ) from None

    return process


def _coerce_test_process_identity(raw: Any) -> VerifiedProcessIdentity:
    if isinstance(raw, VerifiedProcessIdentity):
        return raw
    if isinstance(raw, dict):
        pid = raw.get("pid")
        creation_time = raw.get("creation_time_ft", raw.get("creationTime", 1))
        if type(pid) is int and pid > 0:
            try:
                ct_int = int(creation_time)
            except (ValueError, TypeError):
                ct_int = 1
            return VerifiedProcessIdentity(
                pid=pid,
                creation_time_ft=max(1, ct_int),
                image_path=raw.get("image_path") if isinstance(raw.get("image_path"), str) else None,
                volume_serial=raw.get("volume_serial") if type(raw.get("volume_serial")) is int else None,
                file_id_high=raw.get("file_id_high") if type(raw.get("file_id_high")) is int else None,
                file_id_low=raw.get("file_id_low") if type(raw.get("file_id_low")) is int else None,
                verified_by="test_identity_provider",
            )
    raise NativeBridgeRejected(
        "agent_computer_process_identity_mismatch",
        phase="initialize",
    )


@dataclass
class _PendingRequest:
    request_id: str | int
    correlation_key: str
    method: str
    generation: int
    operation_id: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: NativeBridgeError | None = None
    ready_event: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    unconsumed_events: int = 0
    events_consumed: bool = False


@dataclass
class _WriteItem:
    raw_frame: bytes
    pending: _PendingRequest
    commit_send: bool
    pre_dispatch_on_send_error: bool
    phase: str
    deadline: float
    cancelled: Callable[[], bool] | None
    on_before_write: Callable[[], None] | None
    started_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    write_started: bool = False
    error: NativeBridgeError | None = None


class _WorkerGenerationSession:
    """Single generation of a native worker process and its session-fenced reader/writer."""

    def __init__(
        self,
        *,
        generation: int,
        command: list[str] | None,
        transport_factory: Callable[[], Any] | None,
        expected_worker_name: str,
        expected_worker_version: str | None,
        handshake_timeout: float,
        artifact_lease: ArtifactLease | None = None,
        process_identity_provider: Callable[[Any], Any] | None = None,
        expected_protocol_version: str = PROTOCOL_SEMVER,
        abort_check: Callable[[], bool] | None = None,
        on_liveness_lost: Callable[[int], None] | None = None,
    ) -> None:
        self.generation = int(generation)
        self.expected_worker_name = expected_worker_name
        self.expected_worker_version = expected_worker_version
        self.expected_protocol_version = expected_protocol_version
        self._artifact_lease = artifact_lease
        self._process_identity_provider = process_identity_provider
        self._on_liveness_lost = on_liveness_lost
        self._state_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._terminate_lock = threading.Lock()
        self._closed = threading.Event()
        self._live_in_memory = False
        self._liveness_checked_at: str | None = None
        self._broken_error: NativeBridgeError | None = None
        self._seq = 0
        self._pending: dict[str, _PendingRequest] = {}
        self._completed_ids: deque[str] = deque(maxlen=256)
        self._event_history: deque[dict[str, Any]] = deque(maxlen=MAX_EVENT_QUEUE)
        self._unconsumed_event_count = 0
        self._uncorrelated_event_count = 0
        self._stderr_bytes = bytearray()
        self._write_queue: queue.Queue[_WriteItem | None] = queue.Queue()

        self.process: Any = None
        self.process_identity: VerifiedProcessIdentity | None = None
        self._stdin: BinaryIO | None = None
        self._stdout: BinaryIO | None = None
        self._stderr: BinaryIO | None = None
        self._private_dir: tempfile.TemporaryDirectory[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None

        self.handshake: dict[str, Any] | None = None
        self.handshake_at: str | None = None
        self.worker_instance: str | None = None
        self.worker_pid: int | None = None

        try:
            if abort_check is not None and abort_check():
                raise NativeBridgeRejected(
                    "agent_computer_closed",
                    phase="pre_dispatch",
                    worker_generation=self.generation,
                )

            if transport_factory is not None:
                transport = transport_factory()
                self.process = getattr(transport, "process", transport)
                self._stdin = getattr(transport, "stdin", None) or getattr(
                    self.process, "stdin", None
                )
                self._stdout = getattr(transport, "stdout", None) or getattr(
                    self.process, "stdout", None
                )
                self._stderr = getattr(transport, "stderr", None) or getattr(
                    self.process, "stderr", None
                )
                if self._stdin is None or self._stdout is None:
                    raise NativeBridgeRejected(
                        "agent_computer_spawn_failed",
                        phase="pre_dispatch",
                        worker_generation=self.generation,
                    )
                if self._process_identity_provider is not None:
                    self.process_identity = _coerce_test_process_identity(
                        self._process_identity_provider(transport)
                    )
                else:
                    existing_ident = getattr(
                        self.process, "_ryn_verified_identity", None
                    )
                    if isinstance(existing_ident, VerifiedProcessIdentity):
                        self.process_identity = existing_ident
                    elif getattr(self.process, "_handle", None) is not None:
                        self.process_identity = verify_suspended_process_handle(
                            self.process,
                            phase="initialize",
                        )
                    else:
                        raise NativeBridgeRejected(
                            "agent_computer_process_identity_mismatch",
                            phase="initialize",
                            worker_generation=self.generation,
                        )
            else:
                if os.name != "nt":
                    raise NativeBridgeRejected(
                        "agent_computer_windows_required",
                        phase="pre_dispatch",
                        worker_generation=self.generation,
                    )
                spawn_cmd = (
                    list(self._artifact_lease.command)
                    if self._artifact_lease is not None
                    else command
                )
                if not spawn_cmd:
                    raise NativeBridgeRejected(
                        "agent_computer_executable_not_found",
                        phase="pre_dispatch",
                        worker_generation=self.generation,
                    )
                self._private_dir = tempfile.TemporaryDirectory(
                    prefix=".ryn-computer-worker-",
                    ignore_cleanup_errors=True,
                )
                env = _worker_environment(self._private_dir.name)
                before_resume_hook = (
                    self._artifact_lease.verify_suspended_process
                    if self._artifact_lease is not None
                    else None
                )
                self.process = _spawn_guarded_worker(
                    spawn_cmd,
                    cwd=self._private_dir.name,
                    env=env,
                    before_resume=before_resume_hook,
                )
                self.process_identity = (
                    self._artifact_lease.process_identity
                    if self._artifact_lease is not None
                    else getattr(self.process, "_ryn_verified_identity", None)
                )
                if self.process_identity is None:
                    raise NativeBridgeRejected(
                        "agent_computer_process_identity_mismatch",
                        phase="pre_dispatch",
                        worker_generation=self.generation,
                    )
                self._stdin = self.process.stdin
                self._stdout = self.process.stdout
                self._stderr = self.process.stderr

            if abort_check is not None and abort_check():
                raise NativeBridgeRejected(
                    "agent_computer_closed",
                    phase="pre_dispatch",
                    worker_generation=self.generation,
                )

            self._stdout_reader = BoundedJsonlReader(
                self._stdout,
                max_line_bytes=MAX_RESPONSE_FRAME_BYTES,
            )
            self._reader_thread = threading.Thread(
                target=self._read_stdout_loop,
                daemon=True,
                name=f"ryn-computer-stdout-g{self.generation}",
            )
            self._reader_thread.start()

            self._writer_thread = threading.Thread(
                target=self._write_stdin_loop,
                daemon=True,
                name=f"ryn-computer-stdin-g{self.generation}",
            )
            self._writer_thread.start()

            if self._stderr is not None:
                self._stderr_thread = threading.Thread(
                    target=self._read_stderr_loop,
                    daemon=True,
                    name=f"ryn-computer-stderr-g{self.generation}",
                )
                self._stderr_thread.start()

            self._perform_handshake(
                timeout=handshake_timeout,
                abort_check=abort_check,
            )
        except BaseException:
            self.close()
            raise

    def is_live_cached(self) -> bool:
        """Strictly pure-memory liveness check (never calls process.poll() or touches OS handles)."""
        with self._state_lock:
            return (
                self._live_in_memory
                and not self._closed.is_set()
                and self._broken_error is None
                and self.handshake is not None
            )

    def is_usable(self) -> bool:
        with self._state_lock:
            if (
                not self._live_in_memory
                or self._closed.is_set()
                or self._broken_error is not None
                or self.handshake is None
            ):
                return False
            if self.process is not None and hasattr(self.process, "poll"):
                if self.process.poll() is not None:
                    self._live_in_memory = False
                    self._liveness_checked_at = _utc_now_iso()
                    return False
            return True

    def _perform_handshake(
        self,
        *,
        timeout: float,
        abort_check: Callable[[], bool] | None = None,
    ) -> None:
        if self.process_identity is None:
            raise NativeBridgeRejected(
                "agent_computer_process_identity_mismatch",
                phase="initialize",
                method="initialize",
                worker_generation=self.generation,
            )
        params = {
            "protocolVersion": self.expected_protocol_version,
            "client": {
                "name": CLIENT_NAME,
                "version": CLIENT_VERSION,
            },
            "capabilities": {
                "highDpi": True,
                "uia": True,
                "windowCapture": True,
            },
        }
        res, _ = self.request(
            "initialize",
            params,
            timeout=timeout,
            phase="initialize",
            pre_dispatch_on_send_error=True,
            allow_uninitialized=True,
            cancelled=abort_check,
        )
        validated = validate_initialize_result(
            res,
            expected_worker_name=self.expected_worker_name,
            expected_worker_version=self.expected_worker_version,
            expected_worker_pid=self.process_identity.pid,
            expected_protocol_version=self.expected_protocol_version,
        )
        if self._artifact_lease is not None:
            self._artifact_lease.verify_still_pinned(phase="initialize")

        now_iso = _utc_now_iso()
        with self._state_lock:
            if (
                self._closed.is_set()
                or self._broken_error is not None
                or (abort_check is not None and abort_check())
            ):
                raise self._broken_error or NativeBridgeRejected(
                    "agent_computer_closed",
                    phase="initialize",
                    method="initialize",
                    worker_generation=self.generation,
                )
            self.handshake = validated
            self.handshake_at = now_iso
            self.worker_instance = validated["workerInstance"]
            self.worker_pid = validated["worker"]["pid"]
            self._live_in_memory = True
            self._liveness_checked_at = now_iso

    def _terminate_owned_process(self) -> None:
        """Terminate the owned worker process/Job immediately without waiting on any I/O lock."""
        with self._terminate_lock:
            proc = self.process
            if proc is not None:
                try:
                    process_guard.terminate(proc)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _fail_session(self, err: NativeBridgeError) -> None:
        with self._state_lock:
            if self._broken_error is None:
                self._broken_error = err
            self._live_in_memory = False
            self._liveness_checked_at = _utc_now_iso()
            pending_items = list(self._pending.values())
            self._pending.clear()
            for item in pending_items:
                self._consume_pending_events_locked(item)
                if item.error is None and item.response is None:
                    item.error = self._broken_error
                item.done.set()
            self._unconsumed_event_count = 0
        if self._on_liveness_lost is not None:
            try:
                self._on_liveness_lost(self.generation)
            except Exception:
                pass
        try:
            self._write_queue.put_nowait(None)
        except Exception:
            pass
        threading.Thread(
            target=self.close,
            daemon=True,
            name=f"ryn-computer-cleanup-g{self.generation}",
        ).start()

    def _write_stdin_loop(self) -> None:
        """Dedicated writer thread that executes the final pre-write gate immediately before pipe write."""
        while not self._closed.is_set():
            try:
                item = self._write_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                return

            try:
                with self._state_lock:
                    session_dead = (
                        self._closed.is_set()
                        or self._broken_error is not None
                        or self._stdin is None
                    )
                    still_pending = (
                        self._pending.get(item.pending.correlation_key) is item.pending
                    )

                if session_dead or not still_pending:
                    with self._state_lock:
                        self._pending.pop(item.pending.correlation_key, None)
                        self._consume_pending_events_locked(item.pending)
                    item.error = self._make_pre_write_abort_error(
                        item,
                        code="agent_computer_process_disconnected",
                    )
                    item.done_event.set()
                    continue

                if time.monotonic() >= item.deadline:
                    with self._state_lock:
                        self._pending.pop(item.pending.correlation_key, None)
                        self._consume_pending_events_locked(item.pending)
                    item.error = self._make_pre_write_abort_error(
                        item,
                        code="agent_computer_timeout",
                    )
                    item.done_event.set()
                    continue

                if item.cancelled is not None and item.cancelled():
                    with self._state_lock:
                        self._pending.pop(item.pending.correlation_key, None)
                        self._consume_pending_events_locked(item.pending)
                    reason_code = getattr(item.cancelled, "reason", None)
                    cancel_code = (
                        reason_code
                        if isinstance(reason_code, str)
                        and reason_code
                        in {"agent_time_limit", "agent_time_budget_exhausted"}
                        else "agent_cancelled"
                    )
                    item.error = self._make_pre_write_abort_error(
                        item,
                        code=cancel_code,
                    )
                    item.done_event.set()
                    continue

                if item.on_before_write is not None:
                    try:
                        item.on_before_write()
                    except NativeBridgeError as gate_err:
                        with self._state_lock:
                            self._pending.pop(item.pending.correlation_key, None)
                            self._consume_pending_events_locked(item.pending)
                        item.error = gate_err
                        item.done_event.set()
                        continue
                    except BaseException:
                        with self._state_lock:
                            self._pending.pop(item.pending.correlation_key, None)
                            self._consume_pending_events_locked(item.pending)
                        item.error = self._make_pre_write_abort_error(
                            item,
                            code="agent_computer_native_error",
                        )
                        item.done_event.set()
                        continue

                stdin_stream = self._stdin
                if stdin_stream is None or self._closed.is_set():
                    with self._state_lock:
                        self._pending.pop(item.pending.correlation_key, None)
                        self._consume_pending_events_locked(item.pending)
                    item.error = self._make_pre_write_abort_error(
                        item,
                        code="agent_computer_process_disconnected",
                    )
                    item.done_event.set()
                    continue

                item.write_started = True
                item.started_event.set()

                stdin_stream.write(item.raw_frame)
                stdin_stream.flush()
                item.done_event.set()
            except BaseException:
                with self._state_lock:
                    self._pending.pop(item.pending.correlation_key, None)
                    self._consume_pending_events_locked(item.pending)
                write_err = NativeBridgeError(
                    "agent_computer_write_failed",
                    phase="commit_send" if item.commit_send else item.phase,
                    method=item.pending.method,
                    commit_attempted=item.commit_send and item.write_started,
                    dispatched=item.commit_send or item.write_started,
                    pre_dispatch=False,
                    request_id=item.pending.request_id,
                    operation_id=item.pending.operation_id,
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
                item.error = write_err
                item.done_event.set()
                self._fail_session(write_err)
                return

    def _make_pre_write_abort_error(
        self,
        item: _WriteItem,
        *,
        code: str,
    ) -> NativeBridgeError:
        """Construct an error when a request is aborted in the writer critical section BEFORE any byte is written."""
        if item.commit_send:
            # Host on_ready already ran and may have marked dispatched in its durable ledger,
            # but zero bytes of `action.commit` were written to the pipe!
            return NativeBridgeError(
                code,
                phase="commit_send",
                stage="ready",
                method=item.pending.method,
                commit_attempted=False,
                dispatched=True,
                pre_dispatch=False,
                request_id=item.pending.request_id,
                operation_id=item.pending.operation_id,
                worker_instance=self.worker_instance,
                worker_generation=self.generation,
                worker_pid=self.worker_pid,
            )
        if item.pre_dispatch_on_send_error:
            return NativeBridgeRejected(
                code,
                phase="pre_dispatch",
                method=item.pending.method,
                request_id=item.pending.request_id,
                operation_id=item.pending.operation_id,
                worker_instance=self.worker_instance,
                worker_generation=self.generation,
                worker_pid=self.worker_pid,
            )
        return NativeBridgeError(
            code,
            phase=item.phase,
            method=item.pending.method,
            commit_attempted=False,
            dispatched=False,
            pre_dispatch=False,
            request_id=item.pending.request_id,
            operation_id=item.pending.operation_id,
            worker_instance=self.worker_instance,
            worker_generation=self.generation,
            worker_pid=self.worker_pid,
        )

    def _read_stderr_loop(self) -> None:
        stream = self._stderr
        if stream is None:
            return
        try:
            while not self._closed.is_set():
                chunk = stream.read(4096)
                if not chunk:
                    break
                with self._state_lock:
                    remaining = MAX_STDERR_BYTES - len(self._stderr_bytes)
                    if remaining > 0:
                        self._stderr_bytes.extend(chunk[:remaining])
        except Exception:
            pass

    def _read_stdout_loop(self) -> None:
        try:
            while not self._closed.is_set():
                frame = self._stdout_reader.read_frame()
                if frame.kind == "eof":
                    self._fail_session(
                        NativeBridgeError(
                            "agent_computer_process_disconnected",
                            phase="read",
                            worker_instance=self.worker_instance,
                            worker_generation=self.generation,
                            worker_pid=self.worker_pid,
                        )
                    )
                    return
                if frame.kind == "oversized":
                    self._fail_session(
                        NativeBridgeError(
                            "agent_computer_frame_too_large",
                            phase="read",
                            reason="frame_too_large",
                            worker_instance=self.worker_instance,
                            worker_generation=self.generation,
                            worker_pid=self.worker_pid,
                        )
                    )
                    return
                if frame.kind in {"unterminated_eof", "invalid_utf8"}:
                    self._fail_session(
                        NativeBridgeError(
                            "agent_computer_protocol_invalid",
                            phase="read",
                            reason="malformed_json",
                            worker_instance=self.worker_instance,
                            worker_generation=self.generation,
                            worker_pid=self.worker_pid,
                        )
                    )
                    return

                line = frame.line.strip()
                if not line:
                    continue

                try:
                    env_kind, env = parse_wire_envelope(line, phase="read")
                except NativeBridgeError as exc:
                    self._fail_session(exc)
                    return

                if env_kind == "event":
                    self._handle_incoming_event(env)
                else:
                    self._handle_incoming_response(env)
        except Exception:
            self._fail_session(
                NativeBridgeError(
                    "agent_computer_process_disconnected",
                    phase="read",
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
            )

    def _handle_incoming_event(self, env: dict[str, Any]) -> None:
        req_key = request_id_correlation_key(env["requestId"])
        with self._state_lock:
            pending = self._pending.get(req_key)
            if pending is None or pending.generation != self.generation:
                self._uncorrelated_event_count += 1
                if self._uncorrelated_event_count > MAX_UNCORRELATED_EVENTS:
                    self._fail_session(
                        NativeBridgeError(
                            "agent_computer_event_limit",
                            phase="read",
                            worker_instance=self.worker_instance,
                            worker_generation=self.generation,
                            worker_pid=self.worker_pid,
                        )
                    )
                    return
                self._event_history.append(env)
                return

            if (
                len(pending.events) >= MAX_EVENT_QUEUE
                or self._unconsumed_event_count >= MAX_EVENT_QUEUE
            ):
                self._fail_session(
                    NativeBridgeError(
                        "agent_computer_event_limit",
                        phase="read",
                        worker_instance=self.worker_instance,
                        worker_generation=self.generation,
                        worker_pid=self.worker_pid,
                    )
                )
                return

            self._unconsumed_event_count += 1
            pending.unconsumed_events += 1
            pending.events_consumed = False
            self._event_history.append(env)
            pending.events.append(env)
            if env["event"] == "action.ready":
                pending.ready_event = env["data"]

    def _consume_pending_events_locked(self, pending: _PendingRequest) -> None:
        if pending.events_consumed or pending.unconsumed_events <= 0:
            pending.events_consumed = True
            pending.unconsumed_events = 0
            return
        consumed = pending.unconsumed_events
        pending.unconsumed_events = 0
        pending.events_consumed = True
        self._unconsumed_event_count = max(
            0, self._unconsumed_event_count - consumed
        )

    def _handle_incoming_response(self, env: dict[str, Any]) -> None:
        req_key = request_id_correlation_key(env["id"])
        with self._state_lock:
            if req_key in self._completed_ids:
                self._fail_session(
                    NativeBridgeError(
                        "agent_computer_duplicate_response_id",
                        phase="read",
                        request_id=env["id"],
                        worker_instance=self.worker_instance,
                        worker_generation=self.generation,
                        worker_pid=self.worker_pid,
                    )
                )
                return
            pending = self._pending.pop(req_key, None)
            if pending is None or pending.generation != self.generation:
                if env["id"] == 0 and env.get("error") is not None:
                    err = bridge_error_from_native(
                        env["error"],
                        phase="read",
                        method="initialize",
                        worker_instance=self.worker_instance,
                        worker_generation=self.generation,
                        worker_pid=self.worker_pid,
                    )
                    self._fail_session(err)
                    return
                self._fail_session(
                    NativeBridgeError(
                        "agent_computer_foreign_response_id",
                        phase="read",
                        request_id=env["id"],
                        worker_instance=self.worker_instance,
                        worker_generation=self.generation,
                        worker_pid=self.worker_pid,
                    )
                )
                return
            self._consume_pending_events_locked(pending)
            self._completed_ids.append(req_key)
            pending.response = env
            pending.done.set()

    def _allocate_pending(
        self,
        method: str,
        *,
        operation_id: str | None = None,
        allow_uninitialized: bool = False,
        is_control: bool = False,
        phase: str = "pre_dispatch",
    ) -> _PendingRequest:
        with self._state_lock:
            if self._closed.is_set() or self._broken_error is not None:
                if self._broken_error is not None and not is_control:
                    raise NativeBridgeRejected(
                        self._broken_error.code,
                        phase="pre_dispatch",
                        method=method,
                        operation_id=operation_id,
                        reason=self._broken_error.reason,
                        worker_instance=self.worker_instance,
                        worker_generation=self.generation,
                        worker_pid=self.worker_pid,
                    )
                raise NativeBridgeRejected(
                    "agent_computer_closed",
                    phase="pre_dispatch",
                    method=method,
                    operation_id=operation_id,
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
            if not allow_uninitialized and self.handshake is None:
                raise NativeBridgeRejected(
                    "agent_computer_handshake_failed",
                    phase="pre_dispatch",
                    method=method,
                    operation_id=operation_id,
                    worker_generation=self.generation,
                )
            if self.process is not None and hasattr(self.process, "poll"):
                if self.process.poll() is not None:
                    raise NativeBridgeRejected(
                        "agent_computer_process_disconnected",
                        phase="pre_dispatch",
                        method=method,
                        operation_id=operation_id,
                        worker_instance=self.worker_instance,
                        worker_generation=self.generation,
                        worker_pid=self.worker_pid,
                    )
            limit = MAX_PENDING_REQUESTS + (8 if is_control else 0)
            if len(self._pending) >= limit:
                raise NativeBridgeRejected(
                    "agent_computer_pending_limit",
                    phase=phase,
                    method=method,
                    operation_id=operation_id,
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
            self._seq += 1
            req_id = f"req_{self.generation}_{self._seq}"
            corr_key = request_id_correlation_key(req_id)
            pending = _PendingRequest(
                request_id=req_id,
                correlation_key=corr_key,
                method=method,
                generation=self.generation,
                operation_id=operation_id,
            )
            self._pending[corr_key] = pending
            return pending

    def _enqueue_and_wait_write(
        self,
        raw_frame: bytes,
        *,
        pending: _PendingRequest,
        deadline: float,
        phase: str,
        commit_send: bool,
        pre_dispatch_on_send_error: bool,
        cancelled: Callable[[], bool] | None = None,
        on_before_write: Callable[[], None] | None = None,
    ) -> None:
        item = _WriteItem(
            raw_frame=raw_frame,
            pending=pending,
            commit_send=commit_send,
            pre_dispatch_on_send_error=pre_dispatch_on_send_error,
            phase=phase,
            deadline=deadline,
            cancelled=cancelled,
            on_before_write=on_before_write,
        )
        self._write_queue.put(item)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if item.done_event.is_set():
                    break
                # Write or writer queue timed out! Terminate owned worker/Job so blocked pipe write unblocks.
                with self._state_lock:
                    self._pending.pop(pending.correlation_key, None)
                    self._consume_pending_events_locked(pending)
                started = item.write_started or item.started_event.is_set()
                timeout_err = NativeBridgeError(
                    "agent_computer_timeout",
                    phase="commit_send" if commit_send else phase,
                    method=pending.method,
                    commit_attempted=commit_send and started,
                    dispatched=commit_send or started,
                    pre_dispatch=False,
                    request_id=pending.request_id,
                    operation_id=pending.operation_id,
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
                self._terminate_owned_process()
                self._fail_session(timeout_err)
                if self._writer_thread is not None and self._writer_thread is not threading.current_thread():
                    self._writer_thread.join(timeout=0.5)
                raise timeout_err

            if self._closed.is_set():
                if item.done_event.is_set():
                    break
                with self._state_lock:
                    self._pending.pop(pending.correlation_key, None)
                    self._consume_pending_events_locked(pending)
                started = item.write_started or item.started_event.is_set()
                closed_err = NativeBridgeError(
                    "agent_computer_closed",
                    phase="commit_send" if commit_send else phase,
                    method=pending.method,
                    commit_attempted=commit_send and started,
                    dispatched=commit_send or started,
                    pre_dispatch=False,
                    request_id=pending.request_id,
                    operation_id=pending.operation_id,
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
                raise closed_err

            wait_slice = min(0.01, max(0.001, remaining))
            if item.done_event.wait(timeout=wait_slice):
                break

        if item.error is not None:
            raise item.error

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        operation_id: str | None = None,
        task_session_id: str | None = None,
        phase: str = "read",
        commit_send: bool = False,
        pre_dispatch_allowed: bool = False,
        pre_dispatch_on_send_error: bool = False,
        allow_uninitialized: bool = False,
        is_control: bool = False,
        cancelled: Callable[[], bool] | None = None,
        on_cancel_triggered: Callable[[], None] | None = None,
        on_before_write: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], _PendingRequest]:
        """Send a request frame and wait for its correlated response under a single end-to-end deadline."""
        deadline = time.monotonic() + max(0.02, float(timeout))
        pending = self._allocate_pending(
            method,
            operation_id=operation_id,
            allow_uninitialized=allow_uninitialized,
            is_control=is_control,
            phase="pre_dispatch" if pre_dispatch_on_send_error else phase,
        )
        try:
            raw_frame = encode_request_frame(pending.request_id, method, params)
        except BaseException:
            with self._state_lock:
                self._pending.pop(pending.correlation_key, None)
                self._consume_pending_events_locked(pending)
            raise

        try:
            self._enqueue_and_wait_write(
                raw_frame,
                pending=pending,
                deadline=deadline,
                phase=phase,
                commit_send=commit_send,
                pre_dispatch_on_send_error=pre_dispatch_on_send_error,
                cancelled=cancelled,
                on_before_write=on_before_write,
            )
        except NativeBridgeError as exc:
            if task_session_id and not exc.task_session_id:
                exc.task_session_id = task_session_id
                exc.diagnostics["task_session_id"] = task_session_id
            if operation_id and not exc.operation_id:
                exc.operation_id = operation_id
                exc.diagnostics["operation_id"] = operation_id
            raise

        cancel_signaled = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._state_lock:
                    self._pending.pop(pending.correlation_key, None)
                    self._consume_pending_events_locked(pending)
                timeout_err = NativeBridgeError(
                    "agent_computer_timeout",
                    phase="commit_wait" if commit_send else phase,
                    method=method,
                    commit_attempted=commit_send,
                    dispatched=True,
                    pre_dispatch=False,
                    request_id=pending.request_id,
                    operation_id=operation_id,
                    task_session_id=task_session_id,
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
                if commit_send or method == "initialize":
                    self._fail_session(timeout_err)
                raise timeout_err

            if cancelled is not None and not cancel_signaled and cancelled():
                cancel_signaled = True
                if on_cancel_triggered is not None:
                    try:
                        on_cancel_triggered()
                    except Exception:
                        pass
                if commit_send:
                    deadline = min(deadline, time.monotonic() + 0.3)
                elif method == "initialize":
                    with self._state_lock:
                        self._pending.pop(pending.correlation_key, None)
                        self._consume_pending_events_locked(pending)
                    cancel_err = NativeBridgeRejected(
                        "agent_computer_closed",
                        phase="initialize",
                        method="initialize",
                        request_id=pending.request_id,
                        worker_generation=self.generation,
                    )
                    self._fail_session(cancel_err)
                    raise cancel_err

            wait_slice = min(0.02, max(0.001, remaining))
            if pending.done.wait(timeout=wait_slice):
                break

        with self._state_lock:
            self._consume_pending_events_locked(pending)

        if pending.error is not None:
            err = pending.error
            raise NativeBridgeError(
                err.code,
                phase="commit_wait" if commit_send else phase,
                stage=err.stage,
                method=method,
                commit_attempted=commit_send,
                dispatched=True,
                pre_dispatch=False,
                native_code=err.native_code,
                native_stage=err.native_stage,
                native_mutation=err.native_mutation,
                internal_mutation=err.internal_mutation,
                reason=err.reason,
                request_id=pending.request_id,
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=self.worker_instance,
                worker_generation=self.generation,
                worker_pid=self.worker_pid,
            )

        resp = pending.response
        if resp is None:
            raise NativeBridgeError(
                "agent_computer_process_disconnected",
                phase="commit_wait" if commit_send else phase,
                method=method,
                commit_attempted=commit_send,
                dispatched=True,
                pre_dispatch=False,
                request_id=pending.request_id,
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=self.worker_instance,
                worker_generation=self.generation,
                worker_pid=self.worker_pid,
            )

        if resp.get("error") is not None:
            native_err = bridge_error_from_native(
                resp["error"],
                phase="commit_wait" if commit_send else phase,
                method=method,
                commit_attempted=commit_send,
                dispatched=commit_send,
                pre_dispatch_allowed=pre_dispatch_allowed and not commit_send,
                worker_instance=self.worker_instance,
                worker_generation=self.generation,
                worker_pid=self.worker_pid,
            )
            if task_session_id and not native_err.task_session_id:
                native_err.task_session_id = task_session_id
                native_err.diagnostics["task_session_id"] = task_session_id
            if operation_id and not native_err.operation_id:
                native_err.operation_id = operation_id
                native_err.diagnostics["operation_id"] = operation_id
            raise native_err

        return resp["result"], pending

    def send_cancel_control(
        self,
        *,
        task_session_id: str,
        operation_id: str,
        prepare_ticket: str | None = None,
        timeout: float = DEFAULT_CONTROL_TIMEOUT_S,
    ) -> dict[str, Any] | None:
        """Send an `action.cancel` request on the independent control path even while another request waits."""
        params: dict[str, Any] = {
            "taskSessionId": task_session_id,
            "operationId": operation_id,
        }
        if prepare_ticket is not None:
            params["prepareTicket"] = prepare_ticket
        try:
            res, _ = self.request(
                "action.cancel",
                params,
                timeout=timeout,
                operation_id=operation_id,
                task_session_id=task_session_id,
                phase="cancel",
                pre_dispatch_allowed=True,
                pre_dispatch_on_send_error=True,
                is_control=True,
            )
            return validate_action_cancel_result(
                res,
                expected_operation_id=operation_id,
                phase="cancel",
            )
        except Exception:
            return None

    def close(self) -> None:
        """Deterministically terminate the owned Job/process first, then close pipes, reap threads, and release file leases."""
        with self._close_lock:
            if self._closed.is_set():
                if self._artifact_lease is not None:
                    self._artifact_lease.close()
                return
            self._closed.set()

            with self._state_lock:
                self._live_in_memory = False
                self._liveness_checked_at = _utc_now_iso()
                pending_items = list(self._pending.values())
                self._pending.clear()
                close_err = self._broken_error or NativeBridgeError(
                    "agent_computer_closed",
                    phase="read",
                    worker_instance=self.worker_instance,
                    worker_generation=self.generation,
                    worker_pid=self.worker_pid,
                )
                for item in pending_items:
                    self._consume_pending_events_locked(item)
                    if item.error is None and item.response is None:
                        item.error = close_err
                    item.done.set()
                self._unconsumed_event_count = 0

            if self._on_liveness_lost is not None:
                try:
                    self._on_liveness_lost(self.generation)
                except Exception:
                    pass

            try:
                self._write_queue.put_nowait(None)
            except Exception:
                pass

            # Terminate the owned Job/process BEFORE attempting to close stdin or join the writer thread,
            # so any blocked OS pipe write/flush immediately unblocks with EPIPE/EOF.
            self._terminate_owned_process()

            for stream_attr in ("_stdin", "_stdout", "_stderr"):
                stream = getattr(self, stream_attr, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    setattr(self, stream_attr, None)

            cur_thread = threading.current_thread()
            if self._writer_thread is not None and self._writer_thread is not cur_thread:
                self._writer_thread.join(timeout=1.5)
            if self._reader_thread is not None and self._reader_thread is not cur_thread:
                self._reader_thread.join(timeout=1.5)
            if self._stderr_thread is not None and self._stderr_thread is not cur_thread:
                self._stderr_thread.join(timeout=1.0)

            if self._private_dir is not None:
                try:
                    self._private_dir.cleanup()
                except Exception:
                    pass
                self._private_dir = None

            if self._artifact_lease is not None:
                self._artifact_lease.close()


class NativeWorkerClient:
    """Manages native worker generations, single-flight initialization, pure-memory cache status, and RPCs."""

    def __init__(
        self,
        *,
        owner_id: str | None = None,
        config_identity: BridgeConfigIdentity | None = None,
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
        on_generation_broken: Callable[[int, str | None], None] | None = None,
    ) -> None:
        self._owner_id = owner_id if isinstance(owner_id, str) and owner_id else resolve_owner_id(None)
        self._bundled_executable = bundled_executable
        self._dev_executable = dev_executable
        self._initial_dev_executable = dev_executable
        self._transport_factory = transport_factory
        self._artifact_resolver = artifact_resolver
        self._manifest_path = manifest_path
        self._bundle_root = bundle_root
        self._process_identity_provider = process_identity_provider
        self._expected_worker_name = expected_worker_name
        self._expected_worker_version = expected_worker_version
        self._expected_protocol_version = expected_protocol_version
        self._handshake_timeout = max(0.2, float(handshake_timeout))
        self._on_generation_broken = on_generation_broken

        self._config_identity = config_identity or build_bridge_config_identity(
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

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._connecting = False
        self._active_connect_attempt: object | None = None
        self._ever_used = False
        self._connect_epoch = 0
        self._generation_counter = 0
        self._session: _WorkerGenerationSession | None = None
        self._cached_binding: CachedBinding | None = None
        self._cached_live = False
        self._cached_checked_at: str | None = None
        self._cached_handshake: dict[str, Any] | None = None
        self._cached_handshake_at: str | None = None
        self._cached_generation: int | None = None

    @property
    def owner_id(self) -> str:
        with self._lock:
            return self._owner_id

    @property
    def config_identity(self) -> BridgeConfigIdentity:
        with self._lock:
            return self._config_identity

    def is_pristine_locked(self) -> bool:
        """Return True iff this client has never started, connected, retired, reconfigured, or closed.

        Caller must hold `self._lock`.
        """
        return (
            not self._ever_used
            and not self._closed
            and not self._connecting
            and self._active_connect_attempt is None
            and self._connect_epoch == 0
            and self._generation_counter == 0
            and self._session is None
            and self._cached_binding is None
            and self._cached_handshake is None
            and self._cached_checked_at is None
        )

    def _on_session_liveness_lost(self, generation: int) -> None:
        now_iso = _utc_now_iso()
        with self._lock:
            if (
                self._cached_binding is not None
                and self._cached_binding.worker_generation == generation
            ):
                self._cached_live = False
                self._cached_checked_at = now_iso

    def mark_stale_for_reconfigure(
        self, new_config_identity: BridgeConfigIdentity
    ) -> None:
        """Mark any cached binding stale when the bridge is reconfigured."""
        now_iso = _utc_now_iso()
        with self._lock:
            self._ever_used = True
            self._config_identity = new_config_identity
            self._cached_live = False
            self._cached_checked_at = now_iso

    def cache_status(self) -> dict[str, Any]:
        """Strictly PURE-MEMORY inspection of cached handshake and binary binding metadata.

        Never calls `stat()`, `open()`, `resolve()`, `hashlib`, `process.poll()`, `Popen`, or `handshake`.
        When no validated handshake has been cached, returns `status='unknown'`.
        """
        with self._lock:
            cfg = self._config_identity
            binding = self._cached_binding
            closed = bool(self._closed)
            session = self._session
            live = bool(
                binding is not None
                and self._cached_live
                and not closed
                and session is not None
                and session.generation == binding.worker_generation
                and session.is_live_cached()
            )
            if binding is None:
                return {
                    "status": "unknown",
                    "cached": False,
                    "live": False,
                    "closed": closed,
                    "mode": cfg.mode,
                    "verified": False,
                    "releaseVerified": False,
                    "ownerId": self._owner_id,
                    "configFingerprint": cfg.config_fingerprint,
                    "checkedAt": self._cached_checked_at,
                    "handshakeTimestamp": None,
                    "workerGeneration": None,
                    "workerInstance": None,
                    "worker": None,
                    "protocolVersion": None,
                    "capabilities": None,
                    "limits": None,
                    "binding": None,
                }

            binding_copy = copy.deepcopy(binding.to_dict())
            hs = binding_copy["validatedHandshake"]
            verified = bool(binding.verified and live)
            release_verified = bool(binding.release_verified and live)
            return {
                "status": "ready" if live else "stale",
                "cached": True,
                "live": live,
                "closed": closed,
                "mode": binding.mode,
                "verified": verified,
                "releaseVerified": release_verified,
                "ownerId": binding.owner_id,
                "configFingerprint": binding.config_fingerprint,
                "checkedAt": self._cached_checked_at,
                "handshakeTimestamp": binding.handshake_timestamp,
                "workerGeneration": binding.worker_generation,
                "workerInstance": hs["workerInstance"],
                "worker": hs["worker"],
                "protocolVersion": hs["protocolVersion"],
                "capabilities": hs["capabilities"],
                "limits": hs["limits"],
                "binding": binding_copy,
            }

    def active_session_info(self) -> tuple[int | None, str | None, int | None]:
        with self._lock:
            if self._session is not None and self._session.is_usable():
                return (
                    self._session.generation,
                    self._session.worker_instance,
                    self._session.worker_pid,
                )
            if self._session is not None and self._cached_live:
                self._cached_live = False
                self._cached_checked_at = _utc_now_iso()
            return None, None, None

    def ensure_session(self) -> _WorkerGenerationSession:
        """Single-flight session initializer: concurrent callers wait and share the single healthy winner."""
        while True:
            broken_to_notify: tuple[int, str | None] | None = None
            old_to_close: _WorkerGenerationSession | None = None

            with self._cond:
                self._ever_used = True
                while self._active_connect_attempt is not None and not self._closed:
                    self._cond.wait(timeout=0.05)

                if self._closed:
                    raise NativeBridgeRejected(
                        "agent_computer_closed",
                        phase="pre_dispatch",
                    )

                if self._session is not None:
                    if self._session.is_usable():
                        return self._session
                    old_to_close = self._session
                    broken_to_notify = (
                        self._session.generation,
                        self._session.worker_instance,
                    )
                    self._session = None
                    self._cached_live = False
                    self._cached_checked_at = _utc_now_iso()

                if self._dev_executable is not self._initial_dev_executable:
                    self._initial_dev_executable = self._dev_executable
                    self._config_identity = build_bridge_config_identity(
                        bundled_executable=self._bundled_executable,
                        dev_executable=self._dev_executable,
                        transport_factory=self._transport_factory,
                        artifact_resolver=self._artifact_resolver,
                        manifest_path=self._manifest_path,
                        bundle_root=self._bundle_root,
                        process_identity_provider=self._process_identity_provider,
                        expected_worker_name=self._expected_worker_name,
                        expected_worker_version=self._expected_worker_version,
                        expected_protocol_version=self._expected_protocol_version,
                    )

                # Claim attempt-owned single-flight leader role before unlocking _cond
                attempt_token = object()
                self._active_connect_attempt = attempt_token
                self._connecting = True
                self._connect_epoch += 1
                my_epoch = self._connect_epoch
                self._generation_counter += 1
                gen = self._generation_counter
                cfg_snapshot = self._config_identity
                owner_id_snapshot = self._owner_id
                bundled_executable = self._bundled_executable
                dev_executable = self._dev_executable
                transport_factory = self._transport_factory
                artifact_resolver = self._artifact_resolver
                manifest_path = self._manifest_path
                bundle_root = self._bundle_root
                process_identity_provider = self._process_identity_provider
                expected_worker_name = self._expected_worker_name
                expected_worker_version = self._expected_worker_version
                expected_protocol_version = self._expected_protocol_version
                handshake_timeout = self._handshake_timeout

            if old_to_close is not None:
                old_to_close.close()
            if broken_to_notify is not None and self._on_generation_broken is not None:
                try:
                    self._on_generation_broken(
                        broken_to_notify[0], broken_to_notify[1]
                    )
                except Exception:
                    pass

            def _abort_check(*, my_epoch=my_epoch, attempt_token=attempt_token) -> bool:
                with self._lock:
                    return (
                        self._closed
                        or self._connect_epoch != my_epoch
                        or self._active_connect_attempt is not attempt_token
                    )

            new_session: _WorkerGenerationSession | None = None
            lease: ArtifactLease | None = None
            try:
                if transport_factory is None:
                    if os.name != "nt":
                        raise NativeBridgeRejected(
                            "agent_computer_windows_required",
                            phase="pre_dispatch",
                        )
                    lease = acquire_worker_artifact_lease(
                        bundled_executable=bundled_executable,
                        dev_executable=dev_executable,
                        artifact_resolver=artifact_resolver,
                        manifest_path=manifest_path,
                        bundle_root=bundle_root,
                        expected_worker_name=expected_worker_name,
                        expected_worker_version=expected_worker_version,
                        expected_protocol_version=expected_protocol_version,
                    )

                exp_name = (
                    lease.verified_artifact.worker_name
                    if lease is not None
                    else expected_worker_name
                )
                exp_ver = (
                    lease.verified_artifact.worker_version
                    if lease is not None and lease.verified_artifact.verified
                    else expected_worker_version
                )
                exp_proto = (
                    lease.verified_artifact.protocol_version
                    if lease is not None
                    else expected_protocol_version
                )

                new_session = _WorkerGenerationSession(
                    generation=gen,
                    command=list(lease.command) if lease is not None else None,
                    transport_factory=transport_factory,
                    expected_worker_name=exp_name,
                    expected_worker_version=exp_ver,
                    expected_protocol_version=exp_proto,
                    handshake_timeout=handshake_timeout,
                    artifact_lease=lease,
                    process_identity_provider=process_identity_provider,
                    abort_check=_abort_check,
                    on_liveness_lost=self._on_session_liveness_lost,
                )
            except BaseException:
                if lease is not None and new_session is None:
                    lease.close()
                with self._cond:
                    if self._active_connect_attempt is attempt_token:
                        self._active_connect_attempt = None
                        self._connecting = False
                        self._cond.notify_all()
                raise

            discard_candidate = False
            abort_invalidated = False
            with self._cond:
                if (
                    self._closed
                    or self._connect_epoch != my_epoch
                    or self._active_connect_attempt is not attempt_token
                ):
                    if self._active_connect_attempt is attempt_token:
                        self._active_connect_attempt = None
                        self._connecting = False
                        self._cond.notify_all()
                    discard_candidate = True
                    abort_invalidated = True
                elif not new_session.is_usable():
                    self._active_connect_attempt = None
                    self._connecting = False
                    self._cond.notify_all()
                    discard_candidate = True
                else:
                    assert new_session.handshake is not None
                    assert new_session.handshake_at is not None
                    assert new_session.worker_instance is not None
                    assert new_session.process_identity is not None

                    art = lease.verified_artifact if lease is not None else None
                    release_verified = (
                        verify_lease_release_authority(
                            lease,
                            process_identity=new_session.process_identity,
                            handshake=new_session.handshake,
                        )
                        if lease is not None
                        else False
                    )
                    verified = bool(
                        release_verified
                        or (
                            lease is not None
                            and lease.has_pinned_verified_artifact(
                                process_identity=new_session.process_identity
                            )
                        )
                    )
                    if release_verified:
                        mode = "production"
                    elif lease is None or (
                        art is not None and art.mode == "test" and not verified
                    ):
                        mode = "test"
                    else:
                        mode = "development"
                    hs_copy = copy.deepcopy(new_session.handshake)
                    binding = CachedBinding(
                        owner_id=owner_id_snapshot,
                        config_fingerprint=cfg_snapshot.config_fingerprint,
                        mode=mode,
                        verified=verified,
                        release_verified=release_verified,
                        manifest_digest=(
                            art.manifest_sha256
                            if art is not None and verified
                            else None
                        ),
                        release_id=(
                            art.release_id
                            if art is not None and verified
                            else None
                        ),
                        artifact=(
                            art.to_cache_dict()
                            if art is not None and verified
                            else None
                        ),
                        process=new_session.process_identity.to_cache_dict(),
                        worker_generation=new_session.generation,
                        worker_instance=new_session.worker_instance,
                        handshake_timestamp=new_session.handshake_at,
                        validated_handshake=hs_copy,
                    )
                    self._session = new_session
                    self._cached_binding = binding
                    self._cached_live = True
                    self._cached_checked_at = new_session.handshake_at
                    self._cached_handshake = hs_copy
                    self._cached_handshake_at = new_session.handshake_at
                    self._cached_generation = new_session.generation
                    self._active_connect_attempt = None
                    self._connecting = False
                    self._cond.notify_all()
                    return new_session

            if discard_candidate and new_session is not None:
                new_session.close()
            if abort_invalidated:
                raise NativeBridgeRejected(
                    "agent_computer_closed",
                    phase="pre_dispatch",
                    worker_generation=gen,
                )

    def retire_generation(self, generation: int | None = None) -> None:
        """Retire and close the active worker generation so all tokens/tickets are invalidated."""
        to_close: _WorkerGenerationSession | None = None
        broken_info: tuple[int, str | None] | None = None
        with self._cond:
            self._ever_used = True
            if generation is None:
                # Invalidate any in-flight candidate creation and release its connect claim
                self._connect_epoch += 1
                self._active_connect_attempt = None
                self._connecting = False
                self._cached_live = False
                self._cached_checked_at = _utc_now_iso()
                self._cond.notify_all()
            if self._session is not None and (
                generation is None or self._session.generation == generation
            ):
                to_close = self._session
                broken_info = (
                    self._session.generation,
                    self._session.worker_instance,
                )
                self._session = None
                self._cached_live = False
                self._cached_checked_at = _utc_now_iso()
        if to_close is not None:
            to_close.close()
        if broken_info is not None and self._on_generation_broken is not None:
            try:
                self._on_generation_broken(broken_info[0], broken_info[1])
            except Exception:
                pass

    def cancel_operation(
        self,
        *,
        task_session_id: str,
        operation_id: str,
        prepare_ticket: str | None = None,
        generation: int | None = None,
        timeout: float = DEFAULT_CONTROL_TIMEOUT_S,
    ) -> dict[str, Any] | None:
        with self._lock:
            self._ever_used = True
            session = self._session
            if session is None or not session.is_usable():
                return None
            if generation is not None and session.generation != generation:
                return None
        return session.send_cancel_control(
            task_session_id=task_session_id,
            operation_id=operation_id,
            prepare_ticket=prepare_ticket,
            timeout=timeout,
        )

    def end_task_session(
        self,
        task_session_id: str,
        *,
        timeout: float = DEFAULT_CONTROL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Retire a task namespace in the active worker without spawning a worker if none is running."""
        with self._lock:
            self._ever_used = True
            session = self._session
            if session is None or not session.is_usable():
                return {
                    "ended": True,
                    "cancelledOperations": 0,
                    "invalidatedStateTokens": 0,
                }
        try:
            res, _ = session.request(
                "session.end",
                {"taskSessionId": task_session_id},
                timeout=timeout,
                task_session_id=task_session_id,
                phase="session",
                pre_dispatch_allowed=True,
                pre_dispatch_on_send_error=True,
                is_control=True,
            )
            return validate_session_end_result(res, phase="session")
        except NativeBridgeError:
            self.retire_generation(session.generation)
            raise

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
        """Retire the active worker generation, mark cached binding stale, and apply new configuration."""
        new_cfg = build_bridge_config_identity(
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
        broken_info: tuple[int, str | None] | None = None
        with self._cond:
            self._ever_used = True
            self._bundled_executable = bundled_executable
            self._dev_executable = dev_executable
            self._initial_dev_executable = dev_executable
            self._transport_factory = transport_factory
            self._artifact_resolver = artifact_resolver
            self._manifest_path = manifest_path
            self._bundle_root = bundle_root
            self._process_identity_provider = process_identity_provider
            self._expected_worker_name = expected_worker_name
            self._expected_worker_version = expected_worker_version
            self._expected_protocol_version = expected_protocol_version
            if handshake_timeout is not None:
                self._handshake_timeout = max(0.2, float(handshake_timeout))
            self._config_identity = new_cfg
            self._connect_epoch += 1
            self._active_connect_attempt = None
            self._connecting = False
            self._cached_live = False
            self._cached_checked_at = _utc_now_iso()
            self._cond.notify_all()
            session = self._session
            if session is not None:
                broken_info = (session.generation, session.worker_instance)
            self._session = None

        if session is not None:
            session.close()
        if broken_info is not None and self._on_generation_broken is not None:
            try:
                self._on_generation_broken(broken_info[0], broken_info[1])
            except Exception:
                pass
        return new_cfg

    def shutdown(self, *, timeout: float = DEFAULT_CONTROL_TIMEOUT_S) -> None:
        """Gracefully request worker shutdown and deterministically close all client resources."""
        with self._cond:
            self._ever_used = True
            self._connect_epoch += 1
            self._active_connect_attempt = None
            self._connecting = False
            self._cached_live = False
            self._cached_checked_at = _utc_now_iso()
            self._cond.notify_all()
            session = self._session
            self._session = None
        if session is not None:
            if session.is_usable():
                try:
                    res, _ = session.request(
                        "shutdown",
                        {},
                        timeout=min(1.0, max(0.1, timeout)),
                        phase="shutdown",
                        pre_dispatch_allowed=True,
                        pre_dispatch_on_send_error=True,
                        is_control=True,
                    )
                    validate_shutdown_result(res, phase="shutdown")
                except Exception:
                    pass
            session.close()

    def close(self) -> None:
        with self._cond:
            self._ever_used = True
            self._closed = True
            self._cached_live = False
            self._cached_checked_at = _utc_now_iso()
            self._connect_epoch += 1
            self._active_connect_attempt = None
            self._connecting = False
            self._cond.notify_all()
        self.shutdown()
