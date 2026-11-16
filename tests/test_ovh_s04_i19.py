"""Overhaul S04-I19: macOS/Linux POSIX PTY support (tty=True)."""
from __future__ import annotations

import errno
import os
import queue
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime import local_processes, local_pty
from rynmesh.agent_runtime.local_processes import Processes, prepare_shell_command, shell_command
from rynmesh.agent_runtime.local_specs import SPECS


@pytest.fixture
def sessions():
    procs = Processes()
    yield procs
    procs.close()


def _until_output(processes: Processes, ident: str, marker: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = processes.read(ident, "task", wait_seconds=0)
        if marker in last["output"]:
            return last
        if not last["running"] and last.get("output_complete"):
            break
        time.sleep(0.03)
    pytest.fail(f"Did not observe marker {marker!r} within {timeout}s: {last}")


def _posix_pid_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        stat_path = f"/proc/{pid}/stat"
        if os.path.exists(stat_path):
            try:
                with open(stat_path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                rparen = content.rfind(")")
                if rparen != -1 and content[rparen + 2 : rparen + 3] == "Z":
                    return True
            except OSError:
                return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Real POSIX kernel PTY tests (run on macOS/Linux; conditionally skipped on Windows)
# ---------------------------------------------------------------------------


def test_t1_posix_pty_python_input_echo(sessions: Processes, tmp_path):
    """T1 (real POSIX): python -c 'print(input())' 写入后回显并正常退出（覆盖改动 1、2、4、6）。"""
    if os.name == "nt":
        pytest.skip("Real POSIX PTY requires macOS or Linux")
    local_pty.check_pty_support()
    ident = sessions.start(
        [sys.executable, "-c", "print(input())"],
        tmp_path,
        "task",
        20,
        lambda: True,
        lambda: False,
        tty=True,
    )
    sessions.write(ident, "task", "hello_posix_pty\n")
    res = sessions.wait(ident, "task", lambda: False, yield_seconds=10)
    assert not res["running"]
    assert res["exit_code"] == 0
    assert res["terminal_backend"] == "posix_pty"
    assert "hello_posix_pty" in res["output"]

    # Verify POSIX EOF control sends Ctrl+D (\x04) without closing terminal transport prematurely
    eof_script = (
        "import sys; "
        "print('EOF_READY', flush=True); "
        "first = sys.stdin.readline(); "
        "print('FIRST=' + repr(first), flush=True)"
    )
    ident_eof = sessions.start(
        [sys.executable, "-u", "-c", eof_script],
        tmp_path,
        "task",
        20,
        lambda: True,
        lambda: False,
        tty=True,
    )
    _until_output(sessions, ident_eof, "EOF_READY")
    sessions.write(ident_eof, "task", "", control="eof")
    res_eof = sessions.wait(ident_eof, "task", lambda: False, yield_seconds=10)
    assert res_eof["exit_code"] == 0
    assert "FIRST=''" in res_eof["output"]


def test_t2_posix_pty_resize_updates_stty_size(sessions: Processes, tmp_path):
    """T2 (real POSIX): resize 后 stty size 正确返回新行列数（覆盖改动 1、4、5）。"""
    if os.name == "nt":
        pytest.skip("Real POSIX PTY requires macOS or Linux")
    args = prepare_shell_command(
        {"shell": "sh", "command": "echo READY_RESIZE; read _line; stty size", "tty": True}
    )
    assert args == ["/bin/sh", "-c", "echo READY_RESIZE; read _line; stty size"]
    ident = sessions.start(args, tmp_path, "task", 20, lambda: True, lambda: False, tty=True, rows=24, cols=80)
    _until_output(sessions, ident, "READY_RESIZE")
    write_res = sessions.write(ident, "task", "", rows=41, cols=116)
    assert write_res["rows"] == 41 and write_res["cols"] == 116
    sessions.write(ident, "task", "go\n")
    res = sessions.wait(ident, "task", lambda: False, yield_seconds=10)
    assert res["exit_code"] == 0
    assert res["rows"] == 41 and res["cols"] == 116
    assert "41 116" in res["output"]


def test_t3_posix_pty_stop_kills_process_group(sessions: Processes, tmp_path):
    """T3 (real POSIX): stop 通过 os.killpg(SIGKILL) 杀掉整个进程组（覆盖改动 1、3、4）。"""
    if os.name == "nt":
        pytest.skip("Real POSIX PTY requires macOS or Linux")
    tree_code = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print(f'CHILD_PID={child.pid}', flush=True); "
        "time.sleep(60)"
    )
    ident = sessions.start(
        [sys.executable, "-u", "-c", tree_code],
        tmp_path,
        "task",
        30,
        lambda: True,
        lambda: False,
        tty=True,
    )
    snap = _until_output(sessions, ident, "CHILD_PID=")
    m = re.search(r"CHILD_PID=(\d+)", snap["output"])
    assert m is not None
    child_pid = int(m.group(1))
    parent_pid = sessions.get(ident, "task")["process"].pid

    stop_res = sessions.stop(ident, "task")
    assert not stop_res["running"]
    assert stop_res["stop_reason"] == "owner_or_agent_stopped"
    assert _posix_pid_dead(parent_pid)
    assert _posix_pid_dead(child_pid)


def test_t4_posix_pty_reader_done_after_output_drained(sessions: Processes, tmp_path):
    """T4 (real POSIX): 输出读尽后 reader_done 置位且 output_complete 为 True（覆盖改动 1、4）。"""
    if os.name == "nt":
        pytest.skip("Real POSIX PTY requires macOS or Linux")
    ident = sessions.start(
        [sys.executable, "-u", "-c", "for i in range(4): print(f'DONE_LINE_{i}')"],
        tmp_path,
        "task",
        20,
        lambda: True,
        lambda: False,
        tty=True,
    )
    res = sessions.wait(ident, "task", lambda: False, yield_seconds=10)
    item = sessions.get(ident, "task")
    assert not res["running"]
    assert res["exit_code"] == 0
    for i in range(4):
        assert f"DONE_LINE_{i}" in res["output"]
    assert res["output_complete"] is True
    assert item["reader_done"].is_set()
    assert item["cleanup_done"].wait(5)


# ---------------------------------------------------------------------------
# Cross-platform POSIX PTY harness tests (run on Windows AND macOS/Linux)
# ---------------------------------------------------------------------------


class _SimulatedPosixPtyEnv:
    """In-memory POSIX PTY master/slave + ioctl/killpg harness to verify PosixPtyProcess on any OS."""

    TIOCSWINSZ = 0x5414

    def __init__(self) -> None:
        self._next_fd = 9000
        self.master_fd = 0
        self.slave_fd = 0
        self.open_fds: set[int] = set()
        self._sessions: dict[int, dict] = {}
        self.ioctls: list[tuple[int, int, tuple[int, int, int, int]]] = []
        self.killpg_calls: list[tuple[int, int]] = []
        self.popen_calls: list[dict] = []

    def openpty(self) -> tuple[int, int]:
        self._next_fd += 2
        m_fd, s_fd = self._next_fd - 1, self._next_fd
        self.master_fd, self.slave_fd = m_fd, s_fd
        self.open_fds.update({m_fd, s_fd})
        session = {
            "master_fd": m_fd,
            "slave_fd": s_fd,
            "to_child": queue.Queue(),
            "from_child": queue.Queue(),
            "proc": None,
        }
        self._sessions[m_fd] = session
        self._sessions[s_fd] = session
        return m_fd, s_fd

    def ioctl(self, fd: int, request: int, arg: bytes) -> bytes:
        if fd not in self.open_fds:
            raise OSError(errno.EBADF, "Bad file descriptor")
        unpacked = struct.unpack("HHHH", arg)
        self.ioctls.append((fd, request, unpacked))
        return arg

    def close(self, fd: int, orig_close) -> None:
        if fd in self._sessions:
            if fd not in self.open_fds:
                raise OSError(errno.EBADF, "Double close on simulated PTY fd")
            self.open_fds.discard(fd)
            session = self._sessions[fd]
            if fd == session["master_fd"]:
                session["to_child"].put(None)
                session["from_child"].put(None)
            return
        orig_close(fd)

    def read(self, fd: int, n: int, orig_read) -> bytes:
        session = self._sessions.get(fd)
        if session is not None and fd == session["master_fd"]:
            if fd not in self.open_fds:
                raise OSError(errno.EBADF, "Bad file descriptor")
            chunk = session["from_child"].get()
            if chunk is None:
                raise OSError(errno.EIO, "Input/output error")
            return chunk[:n]
        return orig_read(fd, n)

    def write(self, fd: int, data: bytes, orig_write) -> int:
        session = self._sessions.get(fd)
        if session is not None and fd == session["master_fd"]:
            if fd not in self.open_fds:
                raise OSError(errno.EBADF, "Bad file descriptor")
            buf = bytes(data)
            # Simulate canonical terminal echo + Ctrl+D (\x04) EOF to child stdin
            if buf == b"\x04":
                session["to_child"].put(b"__CTRL_D_EOF__")
            else:
                session["from_child"].put(buf.replace(b"\n", b"\r\n"))
                session["to_child"].put(buf)
            return len(buf)
        return orig_write(fd, data)

    def killpg(self, pgid: int, sig: int) -> None:
        self.killpg_calls.append((pgid, sig))
        if sig == getattr(signal, "SIGKILL", 9):
            seen: set[int] = set()
            for sess in list(self._sessions.values()):
                if id(sess) in seen:
                    continue
                seen.add(id(sess))
                proc = sess.get("proc")
                if proc is not None and proc.pid == pgid:
                    if proc.poll() is None:
                        proc.kill()
                    sess["from_child"].put(None)

    def spawn_real_python(self, orig_popen, args, **kwargs):
        s_fd = kwargs.get("stdin")
        session = self._sessions[s_fd]
        assert kwargs.get("stdout") == s_fd
        assert kwargs.get("stderr") == s_fd
        assert kwargs.get("start_new_session") is True
        self.popen_calls.append({"args": list(args), **kwargs})
        proc = orig_popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=kwargs.get("cwd"),
            env=kwargs.get("env"),
        )
        session["proc"] = proc

        def _feed_stdin():
            try:
                while True:
                    item = session["to_child"].get()
                    if item is None:
                        break
                    if item == b"__CTRL_D_EOF__":
                        proc.stdin.close()
                        break
                    proc.stdin.write(item)
                    proc.stdin.flush()
            except OSError:
                pass

        def _drain_stdout():
            try:
                while True:
                    chunk = proc.stdout.read1(1024)
                    if not chunk:
                        break
                    session["from_child"].put(chunk)
            except OSError:
                pass
            finally:
                proc.wait()
                session["from_child"].put(None)

        threading.Thread(target=_feed_stdin, daemon=True).start()
        threading.Thread(target=_drain_stdout, daemon=True).start()
        return proc


@pytest.fixture
def simulated_posix_pty(monkeypatch):
    sim = _SimulatedPosixPtyEnv()
    orig_close = os.close
    orig_read = os.read
    orig_write = os.write
    orig_popen = subprocess.Popen

    monkeypatch.setitem(sys.modules, "pty", SimpleNamespace(openpty=sim.openpty))
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=sim.ioctl))
    monkeypatch.setitem(sys.modules, "termios", SimpleNamespace(TIOCSWINSZ=sim.TIOCSWINSZ))
    posix_os_pty = SimpleNamespace(
        **{k: getattr(os, k) for k in dir(os) if not k.startswith("__")},
    )
    posix_os_pty.name = "posix"
    posix_os_pty.close = lambda fd: sim.close(fd, orig_close)
    posix_os_pty.read = lambda fd, n: sim.read(fd, n, orig_read)
    posix_os_pty.write = lambda fd, data: sim.write(fd, data, orig_write)
    posix_os_pty.killpg = sim.killpg
    monkeypatch.setattr(local_pty, "os", posix_os_pty)
    monkeypatch.setattr(
        local_pty,
        "subprocess",
        SimpleNamespace(
            **{k: getattr(subprocess, k) for k in dir(subprocess) if not k.startswith("__")},
        ),
    )
    local_pty.subprocess.Popen = lambda args, **kw: sim.spawn_real_python(orig_popen, args, **kw)
    monkeypatch.setattr(local_processes.login_environment, "start_warmup", lambda: None)
    monkeypatch.setattr(local_processes.login_environment, "captured_for_child", lambda: {})
    posix_os_proc = SimpleNamespace(
        **{k: getattr(os, k) for k in dir(os) if not k.startswith("__")},
    )
    posix_os_proc.name = "posix"
    monkeypatch.setattr(local_processes, "os", posix_os_proc)
    return sim


def test_cross_platform_posix_pty_echo_eof_resize_stop_and_reader_done(simulated_posix_pty, tmp_path):
    """Cross-platform verification of items 1..6 and T1..T4 through PosixPtyProcess + Processes."""
    # Item 6: SPECS updated (no 'Other platforms currently reject tty=true'; POSIX EOF mentions Ctrl+D)
    shell_desc = SPECS["ryn_shell_execute"][0]
    write_desc = SPECS["ryn_process_write"][0]
    assert "Other platforms currently reject tty=true" not in shell_desc
    assert "Ctrl+D" in write_desc

    # Item 2 & 5: check_pty_support passes on POSIX; shell_command keeps ['/bin/sh', '-c', command] for tty=True
    local_pty.check_pty_support()
    assert shell_command("sh", "stty size", tty=True) == ["/bin/sh", "-c", "stty size"]
    assert shell_command("sh", "stty size", tty=False) == ["/bin/sh", "-c", "stty size"]

    procs = Processes()
    try:
        # T1 + T4: python -c "print(input())" echo & reader_done on PosixPtyProcess
        ident = procs.start(
            [sys.executable, "-u", "-c", "print('ECHO:' + input())"],
            tmp_path,
            "task",
            20,
            lambda: True,
            lambda: False,
            tty=True,
            rows=28,
            cols=96,
        )
        proc_obj = procs.get(ident, "task")["process"]
        assert isinstance(proc_obj, local_pty.PosixPtyProcess)
        assert proc_obj.backend == "posix_pty"
        assert simulated_posix_pty.slave_fd not in simulated_posix_pty.open_fds
        assert simulated_posix_pty.ioctls[0] == (
            simulated_posix_pty.slave_fd,
            simulated_posix_pty.TIOCSWINSZ,
            (28, 96, 0, 0),
        )

        # T2: resize invokes fcntl.ioctl(TIOCSWINSZ) and updates rows/cols
        resize_res = procs.write(ident, "task", "", rows=45, cols=132)
        assert resize_res["rows"] == 45 and resize_res["cols"] == 132
        assert simulated_posix_pty.ioctls[-1] == (
            simulated_posix_pty.master_fd,
            simulated_posix_pty.TIOCSWINSZ,
            (45, 132, 0, 0),
        )

        procs.write(ident, "task", "hello_world\n")
        done_res = procs.wait(ident, "task", lambda: False, yield_seconds=10)
        assert done_res["exit_code"] == 0
        assert done_res["terminal_backend"] == "posix_pty"
        assert "hello_world" in done_res["output"]
        assert "ECHO:hello_world" in done_res["output"]
        assert done_res["output_complete"] is True
        assert procs.get(ident, "task")["reader_done"].is_set()
        assert simulated_posix_pty.master_fd not in simulated_posix_pty.open_fds

        # Item 4: POSIX control="eof" sends \x04 (Ctrl+D)
        ident_eof = procs.start(
            [sys.executable, "-u", "-c", "import sys; print('READY_EOF', flush=True); print('READ=' + repr(sys.stdin.read()))"],
            tmp_path,
            "task",
            20,
            lambda: True,
            lambda: False,
            tty=True,
        )
        _until_output(procs, ident_eof, "READY_EOF")
        procs.write(ident_eof, "task", "", control="eof")
        eof_res = procs.wait(ident_eof, "task", lambda: False, yield_seconds=10)
        assert eof_res["exit_code"] == 0
        assert "READ=''" in eof_res["output"]

        # T3 + Item 3: stop -> terminate_session -> PosixPtyProcess.dispose -> os.killpg(pid, SIGKILL)
        ident_stop = procs.start(
            [sys.executable, "-u", "-c", "import time; print('SLEEPING', flush=True); time.sleep(60)"],
            tmp_path,
            "task",
            20,
            lambda: True,
            lambda: False,
            tty=True,
        )
        _until_output(procs, ident_stop, "SLEEPING")
        stop_proc = procs.get(ident_stop, "task")["process"]
        target_pid = stop_proc.pid
        simulated_posix_pty.killpg_calls.clear()
        stop_res = procs.stop(ident_stop, "task")
        assert not stop_res["running"]
        assert stop_res["stop_reason"] == "owner_or_agent_stopped"
        sigkill = getattr(signal, "SIGKILL", 9)
        assert (target_pid, sigkill) in simulated_posix_pty.killpg_calls
    finally:
        procs.close()
