"""ConPTY transport, isolated in a job-owned host before any command can run.

The host uses multiprocessing's existing frozen-entry support. Its start gate is
released only after job assignment; pywinpty cannot itself spawn suspended. All
terminal operations are serialized in that host. No target code runs in Ryn.
"""
from __future__ import annotations

import multiprocessing
import os
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

from ..llm_package.process_guard import _windows_job, terminate
from .store import AgentError, AgentToolRejected

COMPLETED = threading.Event()
COMPLETED.set()
_RETIRED_LOCK = threading.RLock()


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
    except Exception:
        # Never forward OS error strings containing commands or environment.
        try:
            connection.send(("error", "agent_conpty_failed"))
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
        self._send_lock = threading.Lock()
        self._pending = {}
        self._sequence = 0
        self._output = queue.Queue(maxsize=64)
        self._closed = threading.Event()
        self._events_done = threading.Event()
        self._returncode = None
        self._command_handle = None
        self.error = None
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
                raise AgentError("agent_conpty_start_timeout")
            event = self._connection.recv()
            if event[0] != "started":
                raise AgentError("agent_conpty_start_failed")
            self.pid = event[1]
            import _winapi
            # Keep the OS exit status available even if the host/terminal is
            # forcibly killed. The handle also prevents PID-reuse ambiguity.
            self._command_handle = _winapi.OpenProcess(0x100000 | 0x1000, False, self.pid)
        except BaseException:
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
                    while not self._closed.is_set():
                        try:
                            self._output.put(event[1], timeout=0.1)
                            break
                        except queue.Full:
                            continue
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
            try:
                return self._output.get(timeout=0.1)
            except queue.Empty:
                if self._events_done.is_set():
                    return b""

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
                    raise AgentError("agent_process_input_unconfirmed")
            except (OSError, EOFError):
                self.dispose()
                raise AgentError("agent_process_input_unconfirmed") from None
            except AgentError:
                # Do not leave an unacknowledged input queued for later delivery.
                self.dispose()
                raise
            finally:
                with self._lock:
                    self._pending.pop(ident, None)

    def write_input(self, text):
        self._request("write", text)

    def resize(self, rows, cols):
        validate_size(rows, cols)
        self._request("resize", (rows, cols))
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
                    self.dispose()
                    raise AgentError("agent_process_input_unconfirmed")
                try:
                    _, ready, _ = select.select([], [fd], [], remaining)
                    if not ready:
                        self.dispose()
                        raise AgentError("agent_process_input_unconfirmed")
                except (OSError, ValueError, TypeError):
                    pass
                try:
                    written = os.write(fd, view)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    self.dispose()
                    raise AgentError("agent_process_input_unconfirmed") from None
                if written <= 0:
                    self.dispose()
                    raise AgentError("agent_process_input_unconfirmed")
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
