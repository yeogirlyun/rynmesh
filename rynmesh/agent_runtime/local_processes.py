"""Task-owned shell processes with bounded output and process-tree cancellation."""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import threading
import time
import uuid

from ..llm_package.process_guard import _windows_job, terminate
from .store import AgentToolRejected

MAX_OUTPUT = 512 * 1024
PAGE_CHARS = 6000
HEAD_CHARS = 2000


def child_environment():
    """The node's own configuration and secrets must not reach model-driven commands.

    RYNMESH_LOCAL_TOKEN/RYNMESH_NETWORK_KEY would hand node control to any command the model
    runs, and RYNMESH_HOME & co. change how a project's own tests behave (a run reported five
    false "pre-existing" failures because of them).
    """
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("RYNMESH_")}


class Processes:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        atexit.register(self.close)

    def close(self):
        with self.lock:
            for item in self.items.values():
                if item["process"].poll() is None:
                    terminate(item["process"])

    def start(self, args, cwd, task_id, timeout, allowed, cancelled):
        with self.lock:
            if sum(v["process"].poll() is None for v in self.items.values()) >= 8:
                raise AgentToolRejected("agent_process_capacity")
            # Evict only completed processes, never running ones.
            for key in list(self.items):
                if len(self.items) < 128:
                    break
                if self.items[key]["process"].poll() is not None:
                    self.items.pop(key)
            if cancelled():
                raise AgentToolRejected("agent_cancelled")
            flags = 0x4 | 0x08000000 if os.name == "nt" else 0
            process = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_environment(),
                                       creationflags=flags, start_new_session=os.name != "nt")
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
            item = {"process": process, "task_id": task_id, "output": bytearray(),
                    "truncated": False, "reason": None, "reader_done": threading.Event()}
            self.items[ident] = item

        def read():
            try:
                while chunk := os.read(process.stdout.fileno(), 8192):
                    with self.lock:
                        room = max(0, MAX_OUTPUT - len(item["output"]))
                        item["output"].extend(chunk[:room])
                        item["truncated"] |= len(chunk) > room
            finally:
                process.stdout.close()
                item["reader_done"].set()

        def watch():
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline or not allowed():
                        item["reason"] = "timeout" if time.monotonic() >= deadline else "permission_or_task_stopped"
                        terminate(process)
                        break
                    time.sleep(0.2)
            finally:
                # Closing a Windows job also cleans descendants after the shell exits.
                terminate(process)
                process.stdin.close()

        threading.Thread(target=read, daemon=True, name="ryn-shell-output").start()
        threading.Thread(target=watch, daemon=True, name="ryn-shell-lifetime").start()
        return ident

    def get(self, ident, task_id):
        with self.lock:
            item = self.items.get(ident)
            if item is None or item["task_id"] != task_id:
                raise AgentToolRejected("agent_process_not_found")
            return item

    def read(self, ident, task_id, offset=0):
        item = self.get(ident, task_id)
        with self.lock:
            output = bytes(item["output"]).decode("utf-8", errors="replace")
            if offset > len(output):
                raise AgentToolRejected("agent_invalid_process_offset")
            running = item["process"].poll() is None
            end = min(len(output), offset + PAGE_CHARS)
            text = output[offset:end]
            if offset == 0 and not running and len(output) > PAGE_CHARS:
                # Failures and summaries are usually at the end of a finished
                # command: show its head and tail; the middle stays pageable.
                end, tail = HEAD_CHARS, len(output) - (PAGE_CHARS - HEAD_CHARS)
                text = (output[:end] + f"\n... [{tail - end} characters omitted; ryn_process_read offset={end} reads them] ...\n"
                        + output[tail:])
            return {"process_id": ident, "running": running,
                    "exit_code": item["process"].poll(), "output": text,
                    "next_offset": end if end < len(output) else None, "output_length": len(output),
                    "truncated": item["truncated"], "stop_reason": item["reason"]}

    def stop(self, ident, task_id):
        item = self.get(ident, task_id)
        item["reason"] = "owner_or_agent_stopped"
        terminate(item["process"])
        item["reader_done"].wait(2)
        return self.read(ident, task_id)

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
        return self.read(ident, task_id)


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
        executable = shutil.which(program)
        if not executable:
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
