"""Task-owned shell processes with bounded output and process-tree cancellation."""
from __future__ import annotations

import atexit
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..llm_package.process_guard import _windows_job, terminate
from .store import AgentToolRejected

MAX_OUTPUT = 512 * 1024
PAGE_CHARS = 6000
HEAD_CHARS = 2000
DEFAULT_READ_WAIT = 10
MAX_READ_WAIT = 20


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


def resolve_rg_executable() -> str | None:
    """Resolve bundled ripgrep binary first, falling back to system PATH."""
    bundled = _bundled_rg_executable()
    if bundled:
        return bundled
    return shutil.which("rg") or (shutil.which("rg.exe") if os.name == "nt" else None)


def child_environment():
    """The node's own configuration and secrets must not reach model-driven commands.

    RYNMESH_LOCAL_TOKEN/RYNMESH_NETWORK_KEY would hand node control to any command the model
    runs, and RYNMESH_HOME & co. change how a project's own tests behave (a run reported five
    false "pre-existing" failures because of them).
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("RYNMESH_")}
    bundled = _bundled_rg_executable()
    if bundled:
        bundled_dir = str(Path(bundled).parent)
        current_path = env.get("PATH", "")
        parts = current_path.split(os.pathsep) if current_path else []
        if bundled_dir not in parts:
            env["PATH"] = bundled_dir + (os.pathsep + current_path if current_path else "")
    return env


class Processes:
    def __init__(self):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.items = {}
        atexit.register(self.close)

    def close(self):
        with self.lock:
            for item in self.items.values():
                if item["process"].poll() is None:
                    terminate(item["process"])

    def start(self, args, cwd, task_id, timeout, allowed, cancelled, *, operation_id=None):
        # Task-state callbacks can acquire the Agent store lock; never call one
        # while holding the process-table lock (preview takes them in reverse).
        if cancelled():
            raise AgentProcessStartRejected("agent_cancelled")
        with self.lock:
            if operation_id is not None and any(
                v.get("task_id") == task_id and v.get("operation_id") == operation_id and v["process"].poll() is None
                for v in self.items.values()
            ):
                raise AgentProcessStartRejected("agent_process_already_running")
            if sum(v["process"].poll() is None for v in self.items.values()) >= 8:
                raise AgentProcessStartRejected("agent_process_capacity")
            # Evict only completed processes, never running ones.
            for key in list(self.items):
                if len(self.items) < 128:
                    break
                if self.items[key]["process"].poll() is not None:
                    self.items.pop(key)
            flags = 0x4 | 0x08000000 if os.name == "nt" else 0
            try:
                process = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_environment(),
                                           creationflags=flags, start_new_session=os.name != "nt")
            except OSError:
                # Popen did not return a child: there is no operation to replay.
                # Failures after this boundary must retain uncertain-write handling.
                raise AgentProcessStartRejected("agent_process_start_failed") from None
            if os.name == "nt":
                try:
                    # Workstation builds/tests need normal OS resources. Retain
                    # kill-on-close ownership without inheriting model CLI limits.
                    process._ryn_job = _windows_job(process, memory_limit=0, cpu_rate=0, process_limit=0)
                except BaseException:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            ident = "process_" + uuid.uuid4().hex
            item = {"process": process, "task_id": task_id, "operation_id": operation_id, "output": bytearray(),
                    "truncated": False, "reason": None, "reader_done": threading.Event(),
                    "last_empty_offset": None, "empty_poll_count": 0}
            self.items[ident] = item

        def read():
            try:
                while chunk := os.read(process.stdout.fileno(), 8192):
                    with self.changed:
                        room = max(0, MAX_OUTPUT - len(item["output"]))
                        item["output"].extend(chunk[:room])
                        item["truncated"] |= len(chunk) > room
                        self.changed.notify_all()
            finally:
                process.stdout.close()
                with self.changed:
                    item["reader_done"].set()
                    self.changed.notify_all()

        def watch():
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    now_mono = time.monotonic()
                    if now_mono >= deadline or not allowed():
                        item["reason"] = "timeout" if now_mono >= deadline else "permission_or_task_stopped"
                        terminate(process)
                        break
                    time.sleep(min(0.05, max(0.005, deadline - now_mono)))
            finally:
                # Closing a Windows job also cleans descendants after the shell exits.
                terminate(process)
                process.stdin.close()

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
            if item is None or item["task_id"] != task_id:
                raise AgentToolRejected("agent_process_not_found")
            return item

    def read(self, ident, task_id, offset=0, *, wait_seconds=DEFAULT_READ_WAIT, cancelled=None,
             coalesce_empty_poll=False):
        if (type(wait_seconds) not in {int, float} or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= MAX_READ_WAIT):
            raise AgentToolRejected("agent_invalid_process_wait")
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
                output = bytes(item["output"]).decode("utf-8", errors="replace")
                if type(offset) is not int or not 0 <= offset <= len(output):
                    raise AgentToolRejected("agent_invalid_process_offset")
                running = item["process"].poll() is None
                remaining = deadline - time.monotonic()
                if (offset < len(output) and running) or (not running and item["reader_done"].is_set()) or remaining <= 0:
                    end = min(len(output), offset + PAGE_CHARS)
                    text = output[offset:end]
                    if offset == 0 and not running and len(output) > PAGE_CHARS:
                        # Failures and summaries are usually at the end of a finished
                        # command: show its head and tail; the middle stays pageable.
                        end, tail = HEAD_CHARS, len(output) - (PAGE_CHARS - HEAD_CHARS)
                        text = (output[:end] + f"\n... [{tail - end} characters omitted; ryn_process_read offset={end} reads them] ...\n"
                                + output[tail:])
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
                    payload = {"process_id": ident, "running": running,
                               "exit_code": exit_code, "output": text,
                               "next_offset": end if end < len(output) else None, "output_length": len(output),
                               "truncated": item["truncated"], "stop_reason": stop_reason,
                               "empty_poll_count": int(item.get("empty_poll_count", 0))}
                    if payload["empty_poll_count"] >= 2 and running:
                        payload["poll_notice"] = (
                            "No new output at this offset yet; pass a larger wait_seconds (e.g. 10-20) "
                            "or stop the process instead of rapid polling at the same offset."
                        )
                    return payload
                self.changed.wait(min(0.1, remaining))

    def stop(self, ident, task_id):
        item = self.get(ident, task_id)
        item["reason"] = "owner_or_agent_stopped"
        terminate(item["process"])
        item["reader_done"].wait(2)
        return self.read(ident, task_id, wait_seconds=0)

    def write(self, ident, task_id, text, eof=False):
        item = self.get(ident, task_id)
        if item["process"].poll() is not None:
            raise AgentToolRejected("agent_process_not_running")
        errors = []
        def send():
            try:
                item["process"].stdin.write(text.encode("utf-8"))
                item["process"].stdin.flush()
                if eof:
                    item["process"].stdin.close()
            except OSError:
                errors.append(True)
        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        sender.join(2)
        if sender.is_alive() or errors:
            # Input may have been partially delivered; it must not be replayed.
            from .store import AgentError
            raise AgentError("agent_process_input_unconfirmed")
        return {"process_id": ident, "input_delivered": True, "eof": eof}

    def wait(self, ident, task_id, cancelled):
        item = self.get(ident, task_id)
        while item["process"].poll() is None:
            if cancelled():
                self.stop(ident, task_id)
                break
            time.sleep(0.05)
        item["reader_done"].wait(2)
        return self.read(ident, task_id, wait_seconds=0)


def shell_command(shell, command):
    if os.name == "nt":
        if shell == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if executable:
                prefix = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); $OutputEncoding=[Console]::OutputEncoding; "
                return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
        elif shell == "cmd":
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "chcp 65001 >nul & " + command]
    elif shell == "sh":
        return ["/bin/sh", "-c", command]
    raise AgentToolRejected("agent_shell_unavailable")


def literal_read_command(arguments):
    """Execute recognized reads without shell expansion or ripgrep preprocessors."""
    from .execution_policy import literal_command, shell_read
    if not shell_read(arguments):
        return None
    shell = arguments.get("shell", "powershell" if os.name == "nt" else "sh")
    words = literal_command(arguments["command"], shell)
    program = words[0].lower()
    if program in {"rg", "rg.exe"} or shell == "sh":
        executable = resolve_rg_executable() if program in {"rg", "rg.exe"} else shutil.which(program)
        if not executable:
            if program in {"rg", "rg.exe"}:
                raise AgentToolRejected("agent_ripgrep_unavailable_use_file_search")
            raise AgentToolRejected("agent_shell_unavailable")
        return [executable, *(["--no-config"] if program in {"rg", "rg.exe"} else []), *words[1:]]
    # Resolve built-ins explicitly; no user profile, aliases or module with the
    # same command name can replace the intended cmdlet. Quote argument values
    # again so the permission check and actual interpretation agree.
    import re
    module = "Microsoft.PowerShell.Utility" if program == "select-string" else "Microsoft.PowerShell.Management"
    args = [word if re.fullmatch(r"-[A-Za-z]+", word) else "'" + word.replace("'", "''") + "'"
            for word in words[1:]]
    return shell_command(shell, module + "\\" + program + " " + " ".join(args))


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
    literal = literal_read_command(arguments)
    if literal is not None:
        return literal
    command = arguments["command"]
    if not resolve_rg_executable() and _contains_unavailable_rg_segment(command):
        raise AgentToolRejected("agent_ripgrep_unavailable_use_file_search")
    return shell_command(
        arguments.get("shell", "powershell" if os.name == "nt" else "sh"), command)
