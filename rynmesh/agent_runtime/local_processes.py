"""Task-owned shell processes with bounded output and process-tree cancellation."""
from __future__ import annotations

import atexit
import codecs
import collections
import locale
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from .. import __version__
from ..llm_package.process_guard import _windows_job
from . import login_environment
from .git_safety import (
    GIT_READONLY_DIFF_FLAGS,
    GIT_READONLY_DIFF_SUBCOMMANDS,
    SAFE_GIT_CONFIG,
    strip_rynmesh_env,
)
from .local_pty import COMPLETED, ConPtyProcess, PosixPtyProcess, check_pty_support, validate_size
from .local_pty import terminate_session as terminate
from .store import AgentToolRejected

_DEFAULT_CONPTY_CLS = ConPtyProcess

try:
    import winreg
except ImportError:
    winreg = None

_WINDOWS_SYSTEM_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_WINDOWS_WARMUP_LOCK = threading.Lock()
_WINDOWS_WARMUP_STARTED = False
_POWERSHELL_PROFILE_LOCK = threading.Lock()
_POWERSHELL_PROFILE_DISABLED = False
POWERSHELL_PROFILE_FALLBACK_NOTICE = (
    "PowerShell profile failed to load (PSSecurityException); "
    "switched to -NoProfile for subsequent commands in this process."
)

HEAD_KEEP_CHARS = 512 * 1024
TAIL_KEEP_CHARS = 512 * 1024
PAGE_CHARS = 6000
HEAD_CHARS = 2000
DEFAULT_READ_WAIT = 10
MAX_READ_WAIT = 20
DEFAULT_YIELD_SECONDS = 10
DEFAULT_READ_SHELL_TIMEOUT = 120
MAX_TASK_PROCESSES = 16
MAX_GLOBAL_PROCESSES = 64
_PROCESS_ID_RE = re.compile(r"^process_[A-Za-z0-9_-]{4,56}$")
LOST_PROCESS_CORRECTION = (
    "Ryn may have restarted since this process was started; background processes do not survive a restart. "
    "Re-run the command if it is still needed, and do not read this process_id again."
)


def _windows_fallback_encoding() -> str:
    preferred = (locale.getpreferredencoding(False) or "").strip().lower()
    if preferred in {"", "utf-8", "utf8", "cp65001"} and hasattr(locale, "getencoding"):
        preferred = (locale.getencoding() or "").strip().lower()
    if preferred in {"", "utf-8", "utf8", "cp65001"}:
        return "utf-8"
    try:
        codecs.lookup(preferred)
        return preferred
    except LookupError:
        return "utf-8"


class OutputDecoder:
    """Incremental strict UTF-8 decoder with sticky Windows code-page fallback."""

    def __init__(self) -> None:
        self.encoding = "utf-8"
        self._switched = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

    def decode(self, data: bytes, final: bool = False) -> str:
        if self._switched:
            return self._decoder.decode(data, final=final)
        pending = self._decoder.getstate()[0]
        try:
            return self._decoder.decode(data, final=final)
        except UnicodeDecodeError:
            raw = pending + data
            try:
                raw.decode("utf-8", errors="strict")
                split_at = len(raw)
            except UnicodeDecodeError as raw_exc:
                split_at = raw_exc.start
            prefix = raw[:split_at].decode("utf-8")
            remainder = raw[split_at:]
            target = _windows_fallback_encoding() if os.name == "nt" else "utf-8"
            self.encoding = target
            self._switched = True
            self._decoder = codecs.getincrementaldecoder(target)(errors="replace")
            return prefix + self._decoder.decode(remainder, final=final)


def _append_output(item: dict, text: str) -> None:
    if not text:
        return
    item["total_chars"] += len(text)
    head_keep = HEAD_KEEP_CHARS
    tail_keep = TAIL_KEEP_CHARS
    head_room = max(0, head_keep - len(item["head"]))
    if head_room > 0:
        take = min(head_room, len(text))
        item["head"] += text[:take]
        text = text[take:]
    if not text:
        return
    tail = item["tail"]
    tail_chars = item.get("tail_chars", 0)
    if tail_keep <= 0:
        if tail_chars:
            tail.clear()
            item["tail_chars"] = 0
            item["dropped_chars"] += tail_chars
        item["dropped_chars"] += len(text)
        return
    if len(text) >= tail_keep:
        item["dropped_chars"] += tail_chars + (len(text) - tail_keep)
        tail.clear()
        tail.append(text[-tail_keep:])
        item["tail_chars"] = tail_keep
        return
    tail.append(text)
    tail_chars += len(text)
    while tail_chars > tail_keep:
        excess = tail_chars - tail_keep
        oldest = tail[0]
        if len(oldest) <= excess:
            tail.popleft()
            tail_chars -= len(oldest)
            item["dropped_chars"] += len(oldest)
        else:
            tail[0] = oldest[excess:]
            tail_chars -= excess
            item["dropped_chars"] += excess
    item["tail_chars"] = tail_chars


def _slice_tail(item: dict, rel_start: int, rel_end: int) -> str:
    if rel_start >= rel_end:
        return ""
    tail = item["tail"]
    tail_chars = item.get("tail_chars", 0)
    rel_start = max(0, rel_start)
    rel_end = min(tail_chars, rel_end)
    if rel_start >= rel_end:
        return ""
    if rel_start > tail_chars // 2:
        pos = tail_chars
        parts = []
        for chunk in reversed(tail):
            chunk_start = pos - len(chunk)
            if chunk_start < rel_end:
                s = max(0, rel_start - chunk_start)
                e = min(len(chunk), rel_end - chunk_start)
                parts.append(chunk[s:e])
                if chunk_start <= rel_start:
                    break
            pos = chunk_start
        parts.reverse()
        return "".join(parts)
    pos = 0
    parts = []
    for chunk in tail:
        chunk_end = pos + len(chunk)
        if chunk_end > rel_start:
            s = max(0, rel_start - pos)
            e = min(len(chunk), rel_end - pos)
            parts.append(chunk[s:e])
            if chunk_end >= rel_end:
                break
        pos = chunk_end
    return "".join(parts)


def _read_contiguous(item: dict, start: int, end: int) -> str:
    if start >= end:
        return ""
    head = item["head"]
    head_end = len(head)
    tail_start = head_end + item["dropped_chars"]
    parts = []
    if start < head_end:
        parts.append(head[start:min(head_end, end)])
    if end > tail_start:
        parts.append(_slice_tail(item, max(0, start - tail_start), end - tail_start))
    return "".join(parts)


class AgentProcessStartRejected(AgentToolRejected):
    """No child was created; callers may safely settle a dispatch as not executed."""


def _rg_candidate_dirs() -> list[Path]:
    dirs = [Path(__file__).resolve().parents[1] / "bin"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        dirs.extend([base / "rynmesh" / "bin", base / "bin"])
    if getattr(sys, "executable", None):
        exe_dir = Path(sys.executable).resolve().parent
        dirs.extend([exe_dir / "bin", exe_dir])
    return dirs


def _bundled_rg_executable() -> str | None:
    names = ("rg.exe", "rg") if os.name == "nt" else ("rg",)
    for directory in _rg_candidate_dirs():
        for name in names:
            candidate = directory / name
            try:
                if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                    return str(candidate)
            except OSError:
                continue
    return None


def _which(name: str, path: str | None = None) -> str | None:
    return shutil.which(name, path=path) if path is not None else shutil.which(name)


_PS_PROFILE_PATH_RE = re.compile(r"(?:^|[\\/ _])profile\.ps1\b", re.IGNORECASE)


def _is_powershell_profile_security_error(head_text: str) -> bool:
    """Detect startup $PROFILE execution-policy failures without matching user script errors."""
    if not head_text or "PSSecurityException" not in head_text:
        return False
    leading = "\n".join(head_text[:4096].splitlines()[:20])
    exc_idx = leading.find("PSSecurityException")
    if exc_idx < 0:
        return False
    before_exc = re.sub(r"\r?\n\s*", "", leading[:exc_idx])
    return bool(_PS_PROFILE_PATH_RE.search(before_exc))


def resolve_rg_executable(path: str | None = None) -> str | None:
    """Resolve bundled ripgrep binary first, falling back to merged system PATH."""
    bundled = _bundled_rg_executable()
    if bundled:
        return bundled
    search_path = path if path is not None else child_environment().get("PATH")
    return _which("rg", path=search_path) or (_which("rg.exe", path=search_path) if os.name == "nt" else None)


class PreparedCommand(list):
    """Argv list carrying optional per-invocation environment overrides and launch flags."""

    def __init__(
        self,
        iterable=(),
        *,
        env_overrides: Mapping[str, str] | None = None,
        powershell_login_profile: bool = False,
    ):
        super().__init__(iterable)
        self.env_overrides = (
            {str(k): str(v) for k, v in env_overrides.items() if not str(k).upper().startswith("RYNMESH_")}
            if env_overrides
            else None
        )
        self.powershell_login_profile = bool(powershell_login_profile)


def _expand_windows_vars(raw: str, env: Mapping[str, str]) -> str:
    env_upper = {k.upper(): v for k, v in env.items()}
    expanded = re.sub(
        r"%([^%]+)%",
        lambda m: env_upper.get(m.group(1).upper(), m.group(0)),
        raw,
    )
    return os.path.expandvars(expanded)


def _read_windows_registry_paths() -> list[str]:
    reg = winreg
    if reg is None:
        return []
    values: list[str] = []
    for hive, subkey in (
        (reg.HKEY_CURRENT_USER, "Environment"),
        (reg.HKEY_LOCAL_MACHINE, _WINDOWS_SYSTEM_ENV_KEY),
    ):
        try:
            with reg.OpenKey(hive, subkey) as key_handle:
                raw_val, _ = reg.QueryValueEx(key_handle, "Path")
                if isinstance(raw_val, str) and raw_val.strip():
                    values.append(raw_val)
        except OSError:
            continue
    return values


def _merge_windows_registry_path(env: dict[str, str]) -> None:
    path_keys = [k for k in env if k.upper() == "PATH"]
    current_path = env.get("PATH")
    if current_path is None and path_keys:
        current_path = env[path_keys[0]]
    for key in path_keys:
        if key != "PATH":
            env.pop(key, None)
    sources = [current_path or ""]
    for raw_reg_path in _read_windows_registry_paths():
        sources.append(_expand_windows_vars(raw_reg_path, env))
    seen: set[str] = set()
    merged: list[str] = []
    for source in sources:
        for segment in source.split(";"):
            cleaned = segment.strip().strip('"')
            if not cleaned:
                continue
            norm = cleaned.rstrip("\\/").lower()
            if norm not in seen:
                seen.add(norm)
                merged.append(cleaned)
    if merged or current_path is not None:
        env["PATH"] = ";".join(merged)


def _start_environment_warmup() -> None:
    global _WINDOWS_WARMUP_STARTED
    if os.name != "nt":
        login_environment.start_warmup()
        return
    with _WINDOWS_WARMUP_LOCK:
        if _WINDOWS_WARMUP_STARTED:
            return
        _WINDOWS_WARMUP_STARTED = True
    threading.Thread(
        target=_read_windows_registry_paths,
        daemon=True,
        name="ryn-win-env-warmup",
    ).start()


def _pop_env_case_aware(env: dict[str, str], key: str) -> None:
    if os.name == "nt":
        target = key.upper()
        for existing in [k for k in env if k.upper() == target]:
            env.pop(existing, None)
    else:
        env.pop(key, None)


def _set_env_case_aware(env: dict[str, str], key: str, value: str) -> None:
    _pop_env_case_aware(env, key)
    env[key] = value


def child_environment(*, task_id: str | None = None, tty: bool = False) -> dict[str, str]:
    """The node's own configuration and secrets must not reach model-driven commands.

    RYNMESH_LOCAL_TOKEN/RYNMESH_NETWORK_KEY would hand node control to any command the model
    runs, and RYNMESH_HOME & co. change how a project's own tests behave (a run reported five
    false "pre-existing" failures because of them).
    """
    env = strip_rynmesh_env(os.environ)
    if os.name != "nt":
        for volatile_key in login_environment.IGNORED_KEYS:
            env.pop(volatile_key, None)
        captured = login_environment.captured_for_child()
        for key, value in captured.items():
            if key not in login_environment.IGNORED_KEYS and not key.upper().startswith("RYNMESH_"):
                env[key] = value
    else:
        _merge_windows_registry_path(env)

    # Fixed child overrides (applied after login/registry merge, before bundled rg PATH prepend).
    for fixed_key, fixed_val in (
        ("PAGER", "cat"),
        ("GIT_PAGER", "cat"),
        ("GH_PAGER", "cat"),
        ("NO_COLOR", "1"),
        ("PYTHONUTF8", "1"),
        ("PYTHONIOENCODING", "utf-8"),
        ("RYN_VERSION", __version__),
    ):
        _set_env_case_aware(env, fixed_key, fixed_val)
    if not tty:
        _set_env_case_aware(env, "TERM", "dumb")
    else:
        existing_term = next((env[k] for k in env if (k.upper() == "TERM" if os.name == "nt" else k == "TERM")), None)
        if existing_term == "dumb":
            _pop_env_case_aware(env, "TERM")
    if task_id is not None:
        _set_env_case_aware(env, "RYN_TASK_ID", str(task_id))
    else:
        _pop_env_case_aware(env, "RYN_TASK_ID")

    bundled = _bundled_rg_executable()
    if bundled:
        sep = ";" if os.name == "nt" else ":"
        bundled_dir = os.path.dirname(bundled)
        norm_bundled = bundled_dir.rstrip("\\/").lower() if os.name == "nt" else bundled_dir.rstrip("/")
        current_path = env.get("PATH", "")
        parts = [
            p
            for p in (current_path.split(sep) if current_path else [])
            if p and ((p.rstrip("\\/").lower() if os.name == "nt" else p.rstrip("/")) != norm_bundled)
        ]
        env["PATH"] = sep.join([bundled_dir, *parts]) if parts else bundled_dir
    return env


class Processes:
    def __init__(self):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.items = {}
        self.boot_id = "boot_" + uuid.uuid4().hex
        atexit.register(self.close)
        _start_environment_warmup()

    def close(self):
        with self.lock:
            items = list(self.items.values())
        for item in items:
            if item["process"].poll() is None or item.get("tty"):
                terminate(item["process"])
            if item.get("cleanup_done"):
                item["cleanup_done"].wait(5)

    def _evict_completed_locked(self, task_id):
        now_mono = time.monotonic()
        for val in self.items.values():
            if val["process"].poll() is not None and val.get("ended_at") is None:
                val["ended_at"] = now_mono

        def _evict_key(pair):
            _k, val = pair
            return (
                val.get("ended_at") if val.get("ended_at") is not None else now_mono,
                val.get("last_read_at") if val.get("last_read_at") is not None else 0.0,
                val.get("started_at") if val.get("started_at") is not None else 0.0,
            )

        while sum(v.get("task_id") == task_id for v in self.items.values()) >= MAX_TASK_PROCESSES:
            completed_task = [
                (k, v)
                for k, v in self.items.items()
                if v.get("task_id") == task_id and v["process"].poll() is not None
            ]
            if not completed_task:
                break
            victim_id, _ = min(completed_task, key=_evict_key)
            self.items.pop(victim_id, None)

        while len(self.items) >= MAX_GLOBAL_PROCESSES:
            completed_all = [
                (k, v)
                for k, v in self.items.items()
                if v["process"].poll() is not None
            ]
            if not completed_all:
                break
            victim_id, _ = min(completed_all, key=_evict_key)
            self.items.pop(victim_id, None)

    def start(self, args, cwd, task_id, timeout=None, allowed=lambda: True, cancelled=lambda: False, *, operation_id=None, tty=False, rows=24, cols=80, env_overrides=None):
        # Task-state callbacks can acquire the Agent store lock; never call one
        # while holding the process-table lock (preview takes them in reverse).
        if type(tty) is not bool:
            raise AgentProcessStartRejected("agent_invalid_terminal_mode")
        if tty:
            validate_size(rows, cols)
            check_pty_support()
        if cancelled():
            raise AgentProcessStartRejected("agent_cancelled")
        with self.lock:
            if operation_id is not None and any(
                v.get("task_id") == task_id and v.get("operation_id") == operation_id and v["process"].poll() is None
                for v in self.items.values()
            ):
                raise AgentProcessStartRejected("agent_process_already_running")
            # Evict earliest-finished, least-recently-read completed entries before checking capacity.
            self._evict_completed_locked(task_id)
            task_running_ids = [
                k for k, v in self.items.items()
                if v.get("task_id") == task_id and v["process"].poll() is None
            ]
            global_running_count = sum(v["process"].poll() is None for v in self.items.values())
            if len(task_running_ids) >= MAX_TASK_PROCESSES or global_running_count >= MAX_GLOBAL_PROCESSES:
                raise AgentProcessStartRejected(
                    "agent_process_capacity",
                    correction=(
                        "Too many background processes are running. Stop unused processes with "
                        "ryn_process_stop before starting another command."
                    ),
                    details={
                        "running_process_ids": task_running_ids,
                        "task_limit": MAX_TASK_PROCESSES,
                        "global_limit": MAX_GLOBAL_PROCESSES,
                    },
                )
            proc_env = child_environment(task_id=task_id, tty=tty)
            for extra_env in (getattr(args, "env_overrides", None), env_overrides):
                if isinstance(extra_env, Mapping):
                    for key, value in extra_env.items():
                        if not str(key).upper().startswith("RYNMESH_"):
                            proc_env[str(key)] = str(value)
            cmd_args = list(args)
            flags = 0x4 | 0x08000000 if os.name == "nt" else 0
            try:
                pty_cls = (
                    ConPtyProcess
                    if (os.name == "nt" or ConPtyProcess is not _DEFAULT_CONPTY_CLS)
                    else PosixPtyProcess
                )
                process = pty_cls(cmd_args, cwd, proc_env, rows, cols) if tty else subprocess.Popen(
                    cmd_args,
                    cwd=str(cwd),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=proc_env,
                    creationflags=flags,
                    start_new_session=os.name != "nt",
                )
            except OSError:
                if tty:
                    # The host may have started the command before IPC failed.
                    from .store import AgentError
                    raise AgentError("agent_conpty_start_unconfirmed") from None
                # Popen did not return a child: there is no operation to replay.
                # Failures after this boundary must retain uncertain-write handling.
                raise AgentProcessStartRejected("agent_process_start_failed") from None
            if os.name == "nt" and not tty:
                try:
                    # Workstation builds/tests need normal OS resources. Retain
                    # kill-on-close ownership without inheriting model CLI limits.
                    process._ryn_job = _windows_job(process, memory_limit=0, cpu_rate=0, process_limit=0)
                except BaseException:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            ident = "process_" + uuid.uuid4().hex
            decoder = OutputDecoder()
            started_mono = time.monotonic()
            item = {"process": process, "task_id": task_id, "operation_id": operation_id,
                    "timeout": timeout, "started_at": started_mono, "last_read_at": started_mono, "ended_at": None,
                    "head": "", "tail": collections.deque(), "tail_chars": 0,
                    "total_chars": 0, "dropped_chars": 0, "decoder": decoder,
                    "reason": None, "reader_done": threading.Event(),
                    "last_empty_offset": None, "empty_poll_count": 0, "tty": tty,
                    "powershell_login_profile": bool(getattr(args, "powershell_login_profile", False)),
                    "input_lock": None if tty else threading.Lock(), "stdin_closed": False, "cleanup_done": threading.Event()}
            self.items[ident] = item

        def read():
            flushed_done = False
            try:
                while chunk := (process.read_output() if tty else os.read(process.stdout.fileno(), 8192)):
                    text = item["decoder"].decode(chunk, final=False)
                    if text:
                        with self.changed:
                            _append_output(item, text)
                            self.changed.notify_all()
                flushed = item["decoder"].decode(b"", final=True)
                flushed_done = True
                if flushed:
                    with self.changed:
                        _append_output(item, flushed)
                        self.changed.notify_all()
            finally:
                if not flushed_done:
                    try:
                        flushed = item["decoder"].decode(b"", final=True)
                    except Exception:
                        flushed = ""
                    if flushed:
                        with self.changed:
                            _append_output(item, flushed)
                if not tty:
                    process.stdout.close()
                elif item["cleanup_done"].wait(5):
                    process.release_buffers()
                with self.changed:
                    item["reader_done"].set()
                    item["reader_done"] = COMPLETED
                    self.changed.notify_all()

        def watch():
            deadline = (time.monotonic() + float(timeout)) if timeout is not None else None
            try:
                while process.poll() is None:
                    now_mono = time.monotonic()
                    timed_out = deadline is not None and now_mono >= deadline
                    # cancelled() belongs to the launch dispatch and becomes
                    # true at an ordinary running -> queued scheduler handoff.
                    # Session lifetime follows durable task permissions instead;
                    # allowed() also observes owner cancellation and revocation.
                    if timed_out or not allowed():
                        item["reason"] = "timeout" if timed_out else "permission_or_task_stopped"
                        terminate(process)
                        break
                    sleep_for = 0.05 if deadline is None else min(0.05, max(0.005, deadline - now_mono))
                    time.sleep(sleep_for)
            finally:
                # Closing a Windows job also cleans descendants after the shell exits.
                terminate(process)
                if not tty:
                    process.stdin.close()
                if item.get("ended_at") is None:
                    item["ended_at"] = time.monotonic()
                item["cleanup_done"].set()
                item["cleanup_done"] = COMPLETED
                with self.changed:
                    self.changed.notify_all()

        threading.Thread(target=read, daemon=True, name="ryn-shell-output").start()
        threading.Thread(target=watch, daemon=True, name="ryn-shell-lifetime").start()
        return ident

    def running_for_operation(self, task_id, operation_id=None):
        with self.lock:
            return [
                ident for ident, item in self.items.items()
                if item.get("task_id") == task_id
                and (operation_id is None or item.get("operation_id") == operation_id)
                and item["process"].poll() is None
            ]

    def recover_operation(self, task_id, operation_id=None):
        with self.lock:
            candidates = [
                (ident, item) for ident, item in self.items.items()
                if item.get("task_id") == task_id
                and (operation_id is None or item.get("operation_id") == operation_id)
            ]
        had_running = False
        stopped_ids = []
        still = []
        cleanup_error = False
        for ident, item in candidates:
            proc = item["process"]
            try:
                running_before = proc.poll() is None
            except Exception:
                cleanup_error = True
                still.append(ident)
                continue
            if not running_before:
                continue
            had_running = True
            item["reason"] = item.get("reason") or "interrupted_by_recovery"
            try:
                terminate(proc)
                reader_done = item.get("reader_done")
                if reader_done is not None:
                    reader_done.wait(0.2)
                exit_code = proc.poll()
            except Exception:
                cleanup_error = True
                exit_code = None
            if exit_code is not None:
                stopped_ids.append(ident)
            else:
                still.append(ident)
        try:
            for ident in self.running_for_operation(task_id, operation_id):
                if ident not in still:
                    still.append(ident)
        except Exception:
            cleanup_error = True
        still_running = bool(still) or cleanup_error
        return {
            "ok": not still_running and not cleanup_error,
            "had_running": had_running,
            "still_running": still_running,
            "cleanup_error": cleanup_error,
            "process_ids": still,
            "stopped_ids": stopped_ids,
        }

    def get(self, ident, task_id):
        with self.lock:
            item = self.items.get(ident)
            if item is None:
                if isinstance(ident, str) and _PROCESS_ID_RE.match(ident):
                    raise AgentToolRejected(
                        "agent_process_not_found",
                        correction=LOST_PROCESS_CORRECTION,
                        details={
                            "reason": "process_table_is_in_memory",
                            "may_have_been_lost_on_restart": True,
                            "restart_note_in_history": True,
                        },
                    )
                raise AgentToolRejected("agent_process_not_found")
            if item["task_id"] != task_id:
                raise AgentToolRejected("agent_process_not_found")
            return item

    def read(self, ident, task_id, offset=0, *, wait_seconds=DEFAULT_READ_WAIT, cancelled=None,
             coalesce_empty_poll=False, tail=False):
        if (type(wait_seconds) not in {int, float} or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= MAX_READ_WAIT):
            raise AgentToolRejected("agent_invalid_process_wait")
        if type(tail) is not bool or type(offset) is not int:
            raise AgentToolRejected("agent_invalid_process_offset")
        if tail:
            if offset not in (0, -1):
                raise AgentToolRejected("agent_invalid_process_offset")
            offset = -1
        item = self.get(ident, task_id)
        effective_wait = float(wait_seconds)
        if coalesce_empty_poll and item.get("last_empty_offset") == offset and effective_wait < 1.0:
            effective_wait = max(effective_wait, float(item.get("coalesce_wait_seconds", 1.5)))
        deadline = time.monotonic() + effective_wait
        while True:
            # A cancellation callback may read task state. Keep it outside the
            # process lock, as well as releasing that lock throughout the wait.
            if cancelled is not None and cancelled():
                raise AgentToolRejected("agent_cancelled")
            with self.changed:
                total_chars = item["total_chars"]
                if offset < -1 or offset > total_chars:
                    raise AgentToolRejected("agent_invalid_process_offset")
                running = item["process"].poll() is None
                remaining = deadline - time.monotonic()
                has_unread = (total_chars > 0) if offset == -1 else (offset < total_chars)
                if (has_unread and running) or (not running and item["reader_done"].is_set()) or remaining <= 0:
                    head = item["head"]
                    head_end = len(head)
                    dropped = item["dropped_chars"]
                    tail_start = head_end + dropped
                    if offset == -1:
                        min_start = tail_start if dropped > 0 else 0
                        start = max(min_start, total_chars - PAGE_CHARS)
                        end = total_chars
                        text = _read_contiguous(item, start, end)
                    elif offset == 0 and not running and total_chars > PAGE_CHARS and (dropped == 0 or head_end > 0):
                        # Failures and summaries are usually at the end of a finished
                        # command: show its head and tail; the middle stays pageable.
                        if dropped == 0:
                            end = min(HEAD_CHARS, total_chars)
                            head_part = _read_contiguous(item, 0, end)
                            tail_pos = max(end, total_chars - (PAGE_CHARS - len(head_part)))
                        else:
                            end = min(HEAD_CHARS, head_end)
                            head_part = head[:end]
                            tail_pos = max(tail_start, total_chars - (PAGE_CHARS - len(head_part)))
                        tail_part = _read_contiguous(item, tail_pos, total_chars)
                        text = (head_part
                                + f"\n... [{tail_pos - end} characters omitted; ryn_process_read offset={end} reads them] ...\n"
                                + tail_part)
                    elif dropped > 0 and offset < head_end:
                        end = min(head_end, offset + PAGE_CHARS)
                        text = head[offset:end]
                    elif dropped > 0 and offset < tail_start:
                        end = tail_start
                        text = f"[{tail_start - offset} characters dropped]\n"
                    else:
                        end = min(total_chars, offset + PAGE_CHARS)
                        text = _read_contiguous(item, offset, end)
                    exit_code = item["process"].poll()
                    stop_reason = item["reason"] or (
                        None if running else ("exited" if exit_code == 0 else "exited_nonzero" if exit_code is not None else "terminated")
                    )
                    if coalesce_empty_poll:
                        if text == "" and running:
                            if item.get("last_empty_offset") == offset:
                                item["empty_poll_count"] = int(item.get("empty_poll_count", 0)) + 1
                            else:
                                item["last_empty_offset"] = offset
                                item["empty_poll_count"] = 1
                        else:
                            item["last_empty_offset"] = None
                            item["empty_poll_count"] = 0
                    output_encoding = getattr(item.get("decoder"), "encoding", None) or "utf-8"
                    payload = {"process_id": ident, "running": running,
                               "exit_code": exit_code, "output": text,
                               "output_encoding": output_encoding,
                               "next_offset": end if end < total_chars else None, "output_length": total_chars,
                               "dropped_chars": dropped, "truncated": dropped > 0, "stop_reason": stop_reason,
                               "empty_poll_count": int(item.get("empty_poll_count", 0))}
                    if item.get("tty"):
                        payload.update(tty=True, terminal_backend=item["process"].backend,
                                       rows=item["process"].rows, cols=item["process"].cols,
                                       terminal_closed=item["process"]._closed.is_set(),
                                       cleanup_complete=item["cleanup_done"].is_set(),
                                       output_complete=item["reader_done"].is_set())
                        if item["process"].error:
                            payload["terminal_error"] = item["process"].error
                            payload["stop_reason"] = item["reason"] or "terminal_transport_error"
                    if payload["empty_poll_count"] >= 2 and running:
                        payload["poll_notice"] = (
                            "No new output at this offset yet; pass a larger wait_seconds (e.g. 10-20) "
                            "or stop the process instead of rapid polling at the same offset."
                        )
                    if item.get("powershell_login_profile") and _is_powershell_profile_security_error(head):
                        global _POWERSHELL_PROFILE_DISABLED
                        with _POWERSHELL_PROFILE_LOCK:
                            if not _POWERSHELL_PROFILE_DISABLED:
                                _POWERSHELL_PROFILE_DISABLED = True
                                item["profile_fallback_triggered"] = True
                        if item.get("profile_fallback_triggered"):
                            payload["profile_notice"] = POWERSHELL_PROFILE_FALLBACK_NOTICE
                    now_read = time.monotonic()
                    item["last_read_at"] = now_read
                    if not running and item.get("ended_at") is None:
                        item["ended_at"] = now_read
                    return payload
                self.changed.wait(min(0.1, remaining))

    def stop(self, ident, task_id):
        item = self.get(ident, task_id)
        item["reason"] = "owner_or_agent_stopped"
        terminate(item["process"])
        item["reader_done"].wait(2)
        if item.get("tty"):
            item["cleanup_done"].wait(5)
        return self.read(ident, task_id, wait_seconds=0)

    def write(self, ident, task_id, text, eof=False, *, control=None, rows=None, cols=None):
        item = self.get(ident, task_id)
        process = item["process"]
        if control not in {None, "interrupt", "eof", "close_terminal"}:
            raise AgentToolRejected("agent_invalid_terminal_control")
        terminal_action = control is not None or rows is not None or cols is not None
        if terminal_action and not item.get("tty"):
            raise AgentToolRejected("agent_process_not_a_terminal")
        if item.get("tty"):
            # A PTY has no independent write half to close. EOF is a terminal
            # keystroke interpreted by the program, never a transport close.
            if eof:
                raise AgentToolRejected("agent_pty_eof_use_control")
            if rows is not None or cols is not None:
                validate_size(rows, cols)
            if control is not None and (text or rows is not None or cols is not None):
                raise AgentToolRejected("agent_terminal_control_requires_empty_text")
            if control == "close_terminal":
                item["reason"] = "terminal_closed"
                terminate(process)
                item["reader_done"].wait(2)
                return self.read(ident, task_id, wait_seconds=0)
            if process.poll() is not None:
                raise AgentToolRejected("agent_process_not_running")
            if rows is not None:
                if text:
                    raise AgentToolRejected("agent_terminal_resize_requires_empty_text")
                process.resize(rows, cols)
            else:
                # Windows console EOF is Ctrl+Z followed by Enter; POSIX PTY EOF
                # is Ctrl+D (\x04). Both are program-dependent and keep the PTY open.
                eof_seq = "\x1a\r" if os.name == "nt" and getattr(process, "backend", None) != "posix_pty" else "\x04"
                process.write_input({"interrupt": "\x03", "eof": eof_seq}.get(control, text))
            return {"process_id": ident, "input_delivered": True, "eof": False,
                    "control": control, "rows": process.rows, "cols": process.cols}
        if process.poll() is not None:
            raise AgentToolRejected("agent_process_not_running")
        if item.get("stdin_closed"):
            raise AgentToolRejected("agent_process_stdin_closed")
        errors = []

        def send():
            try:
                with item["input_lock"]:
                    process.stdin.write(text.encode("utf-8"))
                    process.stdin.flush()
                    if eof:
                        process.stdin.close()
                        item["stdin_closed"] = True
            except (OSError, ValueError):
                errors.append(True)
        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        sender.join(2)
        if sender.is_alive() or errors:
            # Input may have been partially delivered; it must not be replayed.
            from .store import AgentError
            raise AgentError("agent_process_input_unconfirmed")
        return {"process_id": ident, "input_delivered": True, "eof": eof}

    def wait(self, ident, task_id, cancelled, *, yield_seconds=None):
        item = self.get(ident, task_id)
        deadline = (time.monotonic() + float(yield_seconds)) if yield_seconds is not None else None
        while item["process"].poll() is None:
            if cancelled():
                self.stop(ident, task_id)
                break
            if deadline is not None and time.monotonic() >= deadline:
                return self.read(ident, task_id, wait_seconds=0)
            time.sleep(0.05)
        item["reader_done"].wait(2)
        if item.get("tty"):
            item["cleanup_done"].wait(5)
        return self.read(ident, task_id, wait_seconds=0)


def shell_command(shell, command, *, tty=False, login=True, env: Mapping[str, str] | None = None):
    if os.name == "nt":
        if shell == "powershell":
            search_path = (env if env is not None else child_environment()).get("PATH")
            executable = _which("pwsh", path=search_path) or _which("powershell", path=search_path)
            if executable:
                use_profile = bool(login) and not _POWERSHELL_PROFILE_DISABLED
                prefix = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); $OutputEncoding=[Console]::OutputEncoding; "
                argv = [
                    executable,
                    "-NoLogo",
                    *([] if use_profile else ["-NoProfile"]),
                    *([] if tty else ["-NonInteractive"]),
                    "-Command",
                    prefix + command,
                ]
                return PreparedCommand(argv, powershell_login_profile=True) if use_profile else argv
        elif shell == "cmd":
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "chcp 65001 >nul & " + command]
    elif shell == "sh":
        return ["/bin/sh", "-c", command]
    raise AgentToolRejected("agent_shell_unavailable")


def literal_read_command(arguments, *, env: Mapping[str, str] | None = None):
    """Execute recognized reads without shell expansion or ripgrep preprocessors."""
    from .execution_policy import POWERSHELL_NON_ASCII_SYNTAX_CHARS, literal_command, shell_read
    if not shell_read(arguments):
        return None
    shell = arguments.get("shell", "powershell" if os.name == "nt" else "sh")
    words = literal_command(arguments["command"], shell)
    program = words[0].lower()
    resolved_env = env if env is not None else child_environment()
    search_path = resolved_env.get("PATH")
    if program in {"git", "git.exe"}:
        executable = _which("git", path=search_path) or (_which("git.exe", path=search_path) if os.name == "nt" else None)
        if not executable:
            raise AgentToolRejected("agent_shell_unavailable")
        subcommand = words[1].lower()
        tail_args = (
            [words[1], *GIT_READONLY_DIFF_FLAGS, *words[2:]]
            if subcommand in GIT_READONLY_DIFF_SUBCOMMANDS
            else list(words[1:])
        )
        return PreparedCommand(
            [executable, "--no-pager", *SAFE_GIT_CONFIG, *tail_args],
            env_overrides={"GIT_OPTIONAL_LOCKS": "0"},
        )
    if program in {"rg", "rg.exe"} or shell == "sh":
        executable = resolve_rg_executable() if program in {"rg", "rg.exe"} else _which(program, path=search_path)
        if not executable:
            if program in {"rg", "rg.exe"}:
                raise AgentToolRejected("agent_ripgrep_unavailable_use_file_search")
            raise AgentToolRejected("agent_shell_unavailable")
        return [executable, *(["--no-config"] if program in {"rg", "rg.exe"} else []), *words[1:]]
    # Resolve built-ins explicitly; no user profile, aliases or module with the
    # same command name can replace the intended cmdlet. Quote argument values
    # again so the permission check and actual interpretation agree.
    module = "Microsoft.PowerShell.Utility" if program == "select-string" else "Microsoft.PowerShell.Management"
    if any(ch in POWERSHELL_NON_ASCII_SYNTAX_CHARS for word in words for ch in word):
        raise AgentToolRejected("agent_invalid_operation_classification")
    args = [word if re.fullmatch(r"-[A-Za-z]+", word) else "'" + word.replace("'", "''") + "'"
            for word in words[1:]]
    return shell_command(shell, module + "\\" + program + " " + " ".join(args), login=False, env=resolved_env)


def _contains_unavailable_rg_segment(command: str) -> bool:
    """Detect unquoted rg/rg.exe command heads in compound shell invocations."""
    if not isinstance(command, str) or "rg" not in command.lower():
        return False
    import re
    segments = []
    current = []
    quote = None
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            current.append(ch)
            escaped = False
        elif quote == "'":
            if ch == "'":
                if i + 1 < len(command) and command[i + 1] == "'":
                    current.append("''")
                    i += 1
                else:
                    quote = None
            else:
                current.append(ch)
        elif quote == '"':
            if ch in {"\\", "`"}:
                escaped = True
                current.append(ch)
            elif ch == '"':
                quote = None
            else:
                current.append(ch)
        else:
            if ch in {"'", '"'}:
                quote = ch
                current.append(ch)
            elif ch in {";", "|", "&", "\n", "\r"}:
                segments.append("".join(current))
                current = []
            else:
                current.append(ch)
        i += 1
    segments.append("".join(current))
    for seg in segments:
        stripped = seg.strip()
        if stripped.startswith("&") and len(stripped) > 1 and stripped[1].isspace():
            stripped = stripped[1:].strip()
        if re.match(r"^(?:rg|rg\.exe)(?:\s|$)", stripped, re.IGNORECASE):
            return True
    return False


def prepare_shell_command(arguments):
    """Resolve the actual executable before recording a durable dispatch intent."""
    login = arguments.get("login", True)
    if type(login) is not bool:
        raise AgentToolRejected("agent_invalid_login_mode")
    if arguments.get("tty"):
        check_pty_support()
        validate_size(arguments.get("rows", 24), arguments.get("cols", 80))
    elif "rows" in arguments or "cols" in arguments:
        raise AgentToolRejected("agent_terminal_size_requires_tty")
    env = child_environment()
    literal = literal_read_command(arguments, env=env)
    if literal is not None:
        return literal
    command = arguments["command"]
    if not resolve_rg_executable() and _contains_unavailable_rg_segment(command):
        raise AgentToolRejected("agent_ripgrep_unavailable_use_file_search")
    return shell_command(
        arguments.get("shell", "powershell" if os.name == "nt" else "sh"),
        command,
        tty=arguments.get("tty", False),
        login=login,
        env=env,
    )
