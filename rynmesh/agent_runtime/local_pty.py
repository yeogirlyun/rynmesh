"""ConPTY transport, isolated in a job-owned host before any command can run.

The host uses multiprocessing's existing frozen-entry support. Its start gate is
released only after job assignment; pywinpty cannot itself spawn suspended. All
terminal operations are serialized in that host. No target code runs in Ryn.
"""
from __future__ import annotations

import multiprocessing
import os
import queue
import re
import subprocess
import threading
import time
from types import SimpleNamespace

from ..llm_package.process_guard import _windows_job, terminate
from .diagnostic_log import failure
from .store import AgentError, AgentToolRejected

COMPLETED = threading.Event()
COMPLETED.set()
_RETIRED_LOCK = threading.RLock()


def input_unconfirmed(correction=None):
    return {"error": "agent_process_input_unconfirmed", "input_delivered": "unknown",
            "side_effects": "unknown", "correction": correction or (
                "Input may have been partly delivered; do not resend it. Read the process output first or stop the process.")}

TERMINAL_QUERY_REPLIES = (
    ("\x1b[5n", "\x1b[0n"),
    ("\x1b[6n", "\x1b[1;1R"),
    ("\x1b[18t", "\x1b[8;24;80t"),
)
_QUERY_PREFIXES = frozenset({
    "\x1b", "\x1b[", "\x1b[5", "\x1b[6", "\x1b[1", "\x1b[18",
})


def normalize_conpty_input(text: str, previous_was_cr: bool = False) -> tuple[str, bool]:
    """Normalize terminal input for Windows ConPTY (0x08->0x7f, lone LF->CR, drop LF after CR)."""
    if not text:
        return "", previous_was_cr
    out: list[str] = []
    prev_cr = bool(previous_was_cr)
    for ch in text:
        if ch == "\x08":
            out.append("\x7f")
            prev_cr = False
        elif ch == "\r":
            out.append("\r")
            prev_cr = True
        elif ch == "\n":
            if not prev_cr:
                out.append("\r")
            prev_cr = False
        else:
            out.append(ch)
            prev_cr = False
    return "".join(out), prev_cr


def _extract_terminal_query_replies(output: str, tail: str = "") -> tuple[list[str], str]:
    scan = tail + output
    replies: list[str] = []
    idx = 0
    n = len(scan)
    while idx < n:
        pos = scan.find("\x1b", idx)
        if pos < 0:
            return replies, ""
        matched = False
        for query, reply in TERMINAL_QUERY_REPLIES:
            if scan.startswith(query, pos):
                replies.append(reply)
                idx = pos + len(query)
                matched = True
                break
        if matched:
            continue
        rem = scan[pos:]
        if rem in _QUERY_PREFIXES:
            return replies, rem
        idx = pos + 1
    return replies, ""


def check_pty_support():
    if os.name == "nt":
        try:
            import winpty  # noqa: F401
        except ImportError:
            raise AgentToolRejected("agent_pty_dependency_missing_install_pywinpty") from None
        return
    if os.name == "posix":
        try:
            import pty  # noqa: F401
            import termios  # noqa: F401
        except ImportError:
            raise AgentToolRejected("agent_pty_platform_unsupported") from None
        return
    raise AgentToolRejected("agent_pty_platform_unsupported")


def validate_size(rows, cols):
    if any(type(n) is not int or not 1 <= n <= 500 for n in (rows, cols)):
        raise AgentToolRejected("agent_invalid_terminal_size")


def _host(connection):
    """Trusted bootstrap: wait for ownership before importing/starting ConPTY."""
    terminal = None
    try:
        args, cwd, env, rows, cols = connection.recv()
        os.environ.clear()
        os.environ.update(env)
        from winpty import PTY, Backend

        terminal = PTY(cols, rows, backend=Backend.ConPTY)
        environment = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"
        terminal.spawn(args[0], cmdline=" " + subprocess.list2cmdline(args[1:]),
                       cwd=cwd, env=environment)
        connection.send(("started", terminal.pid))
        ended_at = None
        query_tail = ""
        while True:
            # One host thread owns the native object, including resize/write.
            if connection.poll():
                request_id, action, value = connection.recv()
                try:
                    if action == "write":
                        terminal.write(value)
                    elif action == "resize":
                        terminal.set_size(value[1], value[0])
                    connection.send(("ack", request_id, True))
                except Exception:
                    connection.send(("ack", request_id, False))
            try:
                output = terminal.read(blocking=False)
            except Exception:
                if terminal.isalive():
                    raise
                output = ""
            if output:
                replies, query_tail = _extract_terminal_query_replies(output, query_tail)
                for reply in replies:
                    try:
                        terminal.write(reply)
                    except Exception:
                        break
                connection.send(("output", output.encode("utf-8")))
                ended_at = None
            if not terminal.isalive():
                # ConPTY can deliver the last screen update after process exit.
                ended_at = ended_at or time.monotonic()
                if time.monotonic() - ended_at >= 0.2:
                    connection.send(("exit", terminal.get_exitstatus()))
                    break
            time.sleep(0.005)
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        # Never forward OS error strings containing commands or environment.
        try:
            connection.send(("error", "agent_conpty_failed", type(exc).__name__))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
        # Parent owns the job and destroys this host plus all descendants, also
        # if ConPTY's native destructor blocks. Do not depend on that destructor.
        os._exit(0)


class ConPtyProcess:
    """Small process-like adapter; output and exit status refer to the command."""

    backend = "conpty"

    def __init__(self, args, cwd, env, rows=24, cols=80):
        check_pty_support()
        validate_size(rows, cols)
        self._lock = threading.RLock()
        self._dispose_lock = threading.Lock()
        self._send_lock = threading.RLock()
        self._pending = {}
        self._sequence = 0
        self._output = queue.Queue(maxsize=64)
        self.dropped_chars = 0
        self.last_read_dropped_chars = 0
        self._reported_dropped_chars = 0
        self._closed = threading.Event()
        self._events_done = threading.Event()
        self._returncode = None
        self._command_handle = None
        self.error = None
        self.error_detail = None
        self._input_previous_was_cr = False
        self.rows, self.cols = rows, cols
        self._job = None
        ctx = multiprocessing.get_context("spawn")
        self._connection, child = ctx.Pipe()
        self._host = ctx.Process(target=_host, args=(child,), daemon=True, name="ryn-conpty-host")
        try:
            self._host.start()
            child.close()
            # The trusted host is blocked at recv(), with no command or ConPTY
            # descendants yet. NtResumeProcess is harmless for this waiting host.
            owner = SimpleNamespace(_handle=self._host._popen._handle)
            self._job = _windows_job(owner, memory_limit=0, cpu_rate=0, process_limit=0)
            self._connection.send((args, str(cwd), env, rows, cols))
            if not self._connection.poll(15):
                timeout_err = AgentError("agent_conpty_start_timeout")
                failure("conpty_start", timeout_err)
                raise timeout_err
            event = self._connection.recv()
            if event[0] != "started":
                raw_detail = event[2] if len(event) > 2 and isinstance(event[2], str) else ""
                detail = re.sub(r"[^A-Za-z0-9_]", "", raw_detail)[:80] or None
                self.error_detail = detail
                start_err = AgentError("agent_conpty_start_failed")
                if detail:
                    start_err.error_detail = detail
                    start_err.__cause__ = type(detail, (Exception,), {})()
                failure("conpty_start", start_err)
                raise start_err
            self.pid = event[1]
            import _winapi
            # Keep the OS exit status available even if the host/terminal is
            # forcibly killed. The handle also prevents PID-reuse ambiguity.
            self._command_handle = _winapi.OpenProcess(0x100000 | 0x1000, False, self.pid)
        except BaseException as exc:
            if not isinstance(exc, (AgentError, KeyboardInterrupt, SystemExit)):
                self.error_detail = type(exc).__name__
                failure("conpty_start", exc)
            child.close()
            self.dispose()
            raise
        self._reader = threading.Thread(target=self._receive, daemon=True, name="ryn-conpty-events")
        self._reader.start()

    def _receive(self):
        try:
            while not self._closed.is_set():
                event = self._connection.recv()
                if event[0] == "output":
                    with self._lock:
                        try:
                            self._output.put_nowait(event[1])
                        except queue.Full:
                            dropped = self._output.get_nowait()
                            self.dropped_chars += len(dropped.decode("utf-8"))
                            self._output.put_nowait(event[1])
                elif event[0] == "ack":
                    with self._lock:
                        waiter = self._pending.get(event[1])
                        if waiter:
                            waiter[1].append(event[2])
                            waiter[0].set()
                elif event[0] == "exit":
                    self._returncode = event[1]
                    break
                elif event[0] == "error":
                    self.error = event[1]
                    if len(event) > 2 and isinstance(event[2], str):
                        clean = re.sub(r"[^A-Za-z0-9_]", "", event[2])[:80]
                        if clean:
                            self.error_detail = clean
                    break
        except (OSError, EOFError, TypeError):
            pass
        finally:
            if self._returncode is None:
                self._returncode = 1
                if not self._closed.is_set():
                    self.error = self.error or "agent_conpty_host_lost"
            self._events_done.set()

    def read_output(self):
        while True:
            with self._lock:
                try:
                    chunk = self._output.get_nowait()
                except queue.Empty:
                    chunk = None
                if chunk is not None:
                    self.last_read_dropped_chars = self.dropped_chars - self._reported_dropped_chars
                    self._reported_dropped_chars = self.dropped_chars
                    return chunk
                if self._events_done.is_set():
                    return b""
            self._events_done.wait(0.1)

    def release_buffers(self):
        """Retain status, not transport locks/queues, in the session history.

        Python locks/events themselves own Windows handles. Call only after the
        output consumer has drained the queue and dispose has joined the host.
        """
        self._output = None
        self._reader = None
        self._closed = self._events_done = COMPLETED
        self._lock = self._send_lock = _RETIRED_LOCK
        self._dispose_lock = _RETIRED_LOCK

    def poll(self):
        return self._returncode

    def _request(self, action, value):
        with self._send_lock:
            if self.poll() is not None or self._closed.is_set():
                raise AgentToolRejected("agent_process_not_running")
            event, result = threading.Event(), []
            with self._lock:
                self._sequence += 1
                ident = self._sequence
                self._pending[ident] = (event, result)
            try:
                self._connection.send((ident, action, value))
                if not event.wait(2) or not result or not result[0]:
                    return input_unconfirmed()
            except (OSError, EOFError):
                self.dispose()
                return input_unconfirmed()
            finally:
                with self._lock:
                    self._pending.pop(ident, None)

    def write_input(self, text):
        with self._send_lock:
            if self.poll() is not None or self._closed.is_set():
                raise AgentToolRejected("agent_process_not_running")
            normalized, self._input_previous_was_cr = normalize_conpty_input(
                text, self._input_previous_was_cr
            )
            if not normalized:
                return
            return self._request("write", normalized)

    def resize(self, rows, cols):
        validate_size(rows, cols)
        result = self._request("resize", (rows, cols))
        if result:
            return result
        self.rows, self.cols = rows, cols

    def dispose(self):
        with self._dispose_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            if self._job:
                # Reuse the existing typed job cleanup without owning Popen.
                owner = SimpleNamespace(_ryn_job=self._job, wait=lambda **kw: None)
                self._job = None
                terminate(owner)
            if self._host.pid is not None:
                if self._host.is_alive():
                    self._host.kill()
                self._host.join(5)
                self._host.close()
            if self._command_handle is not None:
                import _winapi
                if _winapi.WaitForSingleObject(self._command_handle, 5000) == 0:
                    self._returncode = _winapi.GetExitCodeProcess(self._command_handle)
                _winapi.CloseHandle(self._command_handle)
                self._command_handle = None
            self._connection.close()
            if self._returncode is None:
                self._returncode = 1
            self._events_done.set()
            reader = getattr(self, "_reader", None)
            if reader is not None and reader is not threading.current_thread():
                reader.join(2)


class PosixPtyProcess:
    """POSIX PTY process adapter matching the ConPtyProcess interface."""

    backend = "posix_pty"

    def __init__(self, args, cwd, env, rows=24, cols=80):
        check_pty_support()
        validate_size(rows, cols)
        import fcntl
        import pty
        import struct
        import termios

        self._lock = threading.RLock()
        self._dispose_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._output = queue.Queue(maxsize=64)
        self._closed = threading.Event()
        self._disposing = threading.Event()
        self._events_done = threading.Event()
        self._returncode = None
        self._master_fd = None
        self._proc = None
        self.pid = None
        self.error = None
        self.error_detail = None
        self.rows, self.cols = rows, cols

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            self._proc = subprocess.Popen(
                args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                cwd=str(cwd),
                env=env,
            )
            self.pid = self._proc.pid
        except BaseException as exc:
            try:
                os.close(slave_fd)
            except OSError:
                pass
            self.dispose()
            if isinstance(exc, OSError):
                raise AgentError("agent_pty_start_failed") from None
            raise
        else:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        self._reader = threading.Thread(target=self._receive, daemon=True, name="ryn-posix-pty-events")
        self._reader.start()

    def _close_master(self):
        with self._lock:
            fd = self._master_fd
            self._master_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _receive(self):
        import errno

        try:
            while not self._closed.is_set():
                fd = self._master_fd
                if fd is None:
                    break
                try:
                    chunk = os.read(fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    if exc.errno not in (errno.EIO, errno.EBADF) and not self._disposing.is_set() and not self._closed.is_set():
                        self.error = "agent_posix_pty_failed"
                    break
                if not chunk:
                    break
                while not self._closed.is_set():
                    q = self._output
                    if q is None:
                        break
                    try:
                        q.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        if self._disposing.is_set():
                            break
                        continue
        finally:
            if self._proc is not None and self._returncode is None and not self._disposing.is_set():
                try:
                    self._returncode = self._proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            self._events_done.set()

    def read_output(self):
        while True:
            q = self._output
            if q is None:
                return b""
            try:
                return q.get(timeout=0.1)
            except queue.Empty:
                if self._events_done.is_set():
                    return b""

    def release_buffers(self):
        self._output = None
        self._reader = None
        self._closed = self._disposing = self._events_done = COMPLETED
        self._lock = self._send_lock = _RETIRED_LOCK
        self._dispose_lock = _RETIRED_LOCK

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        proc = self._proc
        if proc is None:
            return 1
        rc = proc.poll()
        if rc is not None:
            self._returncode = rc
        return rc

    def write_input(self, text):
        import errno
        import select

        with self._send_lock:
            if self.poll() is not None or self._closed.is_set() or self._disposing.is_set():
                raise AgentToolRejected("agent_process_not_running")
            fd = self._master_fd
            if fd is None:
                raise AgentToolRejected("agent_process_not_running")
            raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
            if not raw:
                return
            view = memoryview(raw)
            deadline = time.monotonic() + 2.0
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return input_unconfirmed()
                try:
                    _, ready, _ = select.select([], [fd], [], remaining)
                    if not ready:
                        return input_unconfirmed()
                except (OSError, ValueError, TypeError):
                    pass
                try:
                    written = os.write(fd, view)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    self.dispose()
                    return input_unconfirmed()
                if written <= 0:
                    self.dispose()
                    return input_unconfirmed()
                view = view[written:]

    def resize(self, rows, cols):
        validate_size(rows, cols)
        import fcntl
        import struct
        import termios

        with self._send_lock:
            if self.poll() is not None or self._closed.is_set() or self._disposing.is_set():
                raise AgentToolRejected("agent_process_not_running")
            fd = self._master_fd
            if fd is None:
                raise AgentToolRejected("agent_process_not_running")
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                raise AgentError("agent_process_input_unconfirmed") from None
            self.rows, self.cols = rows, cols

    def dispose(self):
        import signal

        with self._dispose_lock:
            if self._closed.is_set():
                return
            self._disposing.set()
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", 9)
            if killpg is not None and self.pid is not None:
                try:
                    killpg(self.pid, sigkill)
                except OSError:
                    pass
            if self._proc is not None:
                if self._returncode is None:
                    rc = self._proc.poll()
                    if rc is not None:
                        self._returncode = rc
                try:
                    rc = self._proc.wait(timeout=5)
                    if self._returncode is None:
                        self._returncode = rc
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                        rc = self._proc.wait(timeout=2)
                        if self._returncode is None:
                            self._returncode = rc
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            reader = getattr(self, "_reader", None)
            if reader is not None and reader is not threading.current_thread():
                reader.join(2)
            self._closed.set()
            self._close_master()
            if reader is not None and reader is not threading.current_thread() and reader.is_alive():
                reader.join(1)
            if self._returncode is None:
                self._returncode = 1
            self._events_done.set()


def terminate_session(process):
    if isinstance(process, (ConPtyProcess, PosixPtyProcess)):
        process.dispose()
    else:
        terminate(process)
