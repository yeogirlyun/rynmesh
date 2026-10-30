"""Real Windows ConPTY acceptance; no mocks for terminal semantics or cleanup."""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rynmesh.agent_runtime.local_processes import Processes, prepare_shell_command
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected

PROBE = Path(__file__).parent / "fixtures" / "pty_probe.py"
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="ConPTY requires Windows")


@pytest.fixture
def sessions():
    processes = Processes()
    yield processes
    processes.close()


def start(processes, tmp_path, scenario, **kwargs):
    return processes.start([sys.executable, "-u", str(PROBE), scenario], tmp_path,
                           "task", kwargs.pop("timeout", 20), kwargs.pop("allowed", lambda: True),
                           kwargs.pop("cancelled", lambda: False), tty=True, **kwargs)


def until(processes, ident, marker, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = processes.read(ident, "task", wait_seconds=0)
        if marker in result["output"]:
            return result
        if not result["running"]:
            pytest.fail(f"Exited before {marker}: {result}")
        time.sleep(0.03)
    pytest.fail(f"Missing {marker}: {result}")


def finished(processes, ident):
    result = processes.wait(ident, "task", lambda: False, yield_seconds=10)
    assert not result["running"], result
    assert processes.get(ident, "task")["cleanup_done"].wait(5)
    return result


def dead(pid):
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel.CloseHandle(handle)


@WINDOWS
@pytest.mark.parametrize("via_shell", [False, True])
def test_real_conpty_prompt_resize_stream_interrupt_and_survival(sessions, tmp_path, via_shell):
    if via_shell:
        command = f"& '{sys.executable}' -u '{PROBE}' interaction"
        args = prepare_shell_command({"command": command, "tty": True})
        ident = sessions.start(args, tmp_path, "task", 20, lambda: True, lambda: False,
                               tty=True, rows=30, cols=100)
    else:
        ident = start(sessions, tmp_path, "interaction", rows=30, cols=100)
    first = until(sessions, ident, "Name:")
    assert '"stdin": true, "stdout": true' in first["output"]
    assert first["terminal_backend"] == "conpty" and first["running"]
    with pytest.raises(AgentToolRejected, match="not_found"):
        sessions.write(ident, "other-task", "wrong\r")
    sessions.write(ident, "task", "", rows=40, cols=120)
    sessions.write(ident, "task", "测试🙂\r")
    second = until(sessions, ident, '"number": 1')
    assert '"value": "测试🙂"' in second["output"]
    assert '"columns": 120, "lines": 40' in second["output"]
    later = sessions.read(ident, "task", second["output_length"], wait_seconds=2)
    assert later["output"] and later["running"]
    until(sessions, ident, "interrupt_ready")
    sessions.write(ident, "task", "", control="interrupt")
    assert until(sessions, ident, "Continue:")["running"]
    sessions.write(ident, "task", "done\r")
    result = finished(sessions, ident)
    # PowerShell can set its own status to 1 when its pipeline is interrupted,
    # even when the Python program catches Ctrl+C and subsequently exits 0.
    assert result["exit_code"] in ({0, 1} if via_shell else {0})
    assert '"after_interrupt", "value": "done"' in result["output"]


@WINDOWS
def test_real_conpty_eof_is_not_terminal_close(sessions, tmp_path):
    ident = start(sessions, tmp_path, "eof")
    until(sessions, ident, "EOF:")
    with pytest.raises(AgentToolRejected, match="pty_eof_use_control"):
        sessions.write(ident, "task", "", eof=True)
    sessions.write(ident, "task", "", control="eof")
    assert until(sessions, ident, "Still open:")["running"]
    sessions.write(ident, "task", "yes\r")
    assert finished(sessions, ident)["exit_code"] == 0


@WINDOWS
@pytest.mark.parametrize("action", ["stop", "close_terminal", "timeout", "cancel", "revoke", "close", "orphan", "recover", "host_crash"])
def test_real_conpty_tree_cleanup(sessions, tmp_path, action):
    cancelled, revoked = threading.Event(), threading.Event()
    ident = start(sessions, tmp_path, "orphan" if action == "orphan" else "tree",
                  timeout=2 if action == "timeout" else 20,
                  cancelled=cancelled.is_set,
                  allowed=lambda: not cancelled.is_set() and not revoked.is_set())
    result = until(sessions, ident, '"event": "child"')
    child_pid = int(re.search(r'"event": "child", "pid": (\d+)', result["output"])[1])
    proc = sessions.get(ident, "task")["process"]
    if action == "stop":
        sessions.stop(ident, "task")
    elif action == "close_terminal":
        result = sessions.write(ident, "task", "", control="close_terminal")
        assert result["stop_reason"] == "terminal_closed"
    elif action == "cancel":
        cancelled.set()
    elif action == "revoke":
        revoked.set()
    elif action == "close":
        sessions.close()
    elif action == "recover":
        assert sessions.recover_operation("task")["ok"]
    elif action == "host_crash":
        proc._host.kill()
    result = finished(sessions, ident)
    assert dead(child_pid) and dead(proc.pid)
    assert proc._reader is None or not proc._reader.is_alive()
    assert proc._connection.closed
    if action == "timeout":
        assert result["stop_reason"] == "timeout"
    if action == "orphan":
        assert result["exit_code"] == 0
    if action == "host_crash":
        assert result["terminal_error"] == "agent_conpty_host_lost"


@WINDOWS
def test_real_conpty_exit_status_and_last_unicode_output(sessions, tmp_path):
    ident = start(sessions, tmp_path, "exit", cols=500)
    result = finished(sessions, ident)
    assert result["exit_code"] == 7
    assert "你好🙂" in result["output"] and "\ufffd" not in result["output"]
    assert result["stop_reason"] == "exited_nonzero"


@WINDOWS
def test_powershell_real_read_host(sessions, tmp_path):
    args = prepare_shell_command({"shell": "powershell", "command": "$x=Read-Host 'PTY_PROMPT'; Write-Output ('ANSWER_'+$x)", "tty": True})
    assert "-NonInteractive" not in args
    ident = sessions.start(args, tmp_path, "task", 20, lambda: True, lambda: False, tty=True)
    until(sessions, ident, "PTY_PROMPT:")
    sessions.write(ident, "task", "hello\r")
    result = finished(sessions, ident)
    assert result["exit_code"] == 0 and "ANSWER_hello" in result["output"]


def test_pipe_defaults_and_eof_remain_compatible(sessions, tmp_path):
    command = [sys.executable, "-u", "-c", "import sys; print(sys.stdin.isatty(), flush=True); print(sys.stdin.read(), flush=True)"]
    ident = sessions.start(command, tmp_path, "task", 10, lambda: True, lambda: False)
    until(sessions, ident, "False")
    with pytest.raises(AgentToolRejected, match="not_a_terminal"):
        sessions.write(ident, "task", "", control="interrupt")
    sessions.write(ident, "task", "piped", eof=True)
    result = finished(sessions, ident)
    assert result["exit_code"] == 0 and "piped" in result["output"]


def test_tty_arguments_remain_write_classified():
    from jsonschema import validate

    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.local_specs import SPECS
    args = {"command": "git status", "tty": True, "rows": 30, "cols": 100}
    validate(args, SPECS["ryn_shell_execute"][3])
    assert classify_operation("ryn_shell_execute", args, {"effect": "write"})["effect_class"] == "write"


@WINDOWS
def test_missing_dependency_rejected_without_fallback(sessions, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "winpty", None)
    with pytest.raises(AgentToolRejected, match="dependency_missing"):
        start(sessions, tmp_path, "tree")
    assert not sessions.items


def test_unsupported_platform_is_explicit(monkeypatch):
    from types import SimpleNamespace

    from rynmesh.agent_runtime import local_pty
    monkeypatch.setattr(local_pty, "os", SimpleNamespace(name="posix"))
    with pytest.raises(AgentToolRejected, match="platform_unsupported"):
        local_pty.check_pty_support()


@WINDOWS
def test_invalid_sizes_and_controls_have_no_input_side_effect(sessions, tmp_path):
    for size in (0, 501, True, 1.5):
        with pytest.raises(AgentToolRejected, match="invalid_terminal_size"):
            start(sessions, tmp_path, "tree", rows=size)
    assert not sessions.items
    ident = start(sessions, tmp_path, "eof")
    until(sessions, ident, "EOF:")
    for kwargs in ({"rows": 30}, {"cols": 90}, {"control": "nope"}, {"control": "interrupt", "rows": 30, "cols": 90}):
        with pytest.raises(AgentToolRejected):
            sessions.write(ident, "task", "", **kwargs)
    assert sessions.read(ident, "task", wait_seconds=0)["running"]


@WINDOWS
def test_large_output_is_drained_and_bounded(sessions, tmp_path, monkeypatch):
    from rynmesh.agent_runtime import local_processes
    # ConPTY emits screen updates and can coalesce one huge repetitive write.
    # Use spaced updates to exercise transport draining beyond the output cap.
    monkeypatch.setattr(local_processes, "MAX_OUTPUT", 4096)
    args = [sys.executable, "-u", "-c", "import time; [(print(str(i)*2000, flush=True), time.sleep(.03)) for i in range(30)]"]
    ident = sessions.start(args, tmp_path, "task", 20, lambda: True, lambda: False, tty=True)
    result = finished(sessions, ident)
    assert result["exit_code"] == 0 and result["truncated"]
    assert len(sessions.get(ident, "task")["output"]) == 4096


@WINDOWS
def test_secret_environment_is_filtered(sessions, tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_TEST_PTY_FAKE", "fake-marker-not-a-credential")
    args = [sys.executable, "-c", "import os; print('ENV='+str(os.getenv('RYNMESH_TEST_PTY_FAKE')))"]
    ident = sessions.start(args, tmp_path, "task", 20, lambda: True, lambda: False, tty=True)
    result = finished(sessions, ident)
    assert "ENV=None" in result["output"]
    assert "fake-marker" not in result["output"]


@WINDOWS
def test_owner_crash_kills_host_command_and_descendant(tmp_path):
    marker = tmp_path / "pids.json"
    script = f"""
import json, os, re, sys, time
from pathlib import Path
from rynmesh.agent_runtime.local_processes import Processes
p = Processes()
i = p.start([sys.executable, '-u', {str(PROBE)!r}, 'tree'], {str(tmp_path)!r},
            'task', 30, lambda: True, lambda: False, tty=True)
for _ in range(200):
    result = p.read(i, 'task', wait_seconds=0)
    match = re.search(r'"event": "child", "pid": (\\d+)', result['output'])
    if match:
        proc = p.get(i, 'task')['process']
        Path({str(marker)!r}).write_text(json.dumps([proc._host.pid, proc.pid, int(match[1])]))
        os._exit(0)
    time.sleep(.03)
os._exit(2)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert all(dead(pid) for pid in json.loads(marker.read_text()))


@WINDOWS
def test_repeated_sessions_release_os_handles(sessions, tmp_path):
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]

    def count():
        value = w.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value

    counts = []
    for _ in range(6):
        ident = sessions.start([sys.executable, "-c", "pass"], tmp_path, "task", 10,
                               lambda: True, lambda: False, tty=True)
        assert finished(sessions, ident)["exit_code"] == 0
        counts.append(count())
    assert counts[-1] <= counts[0] + 2, counts


@WINDOWS
def test_native_start_failure_releases_host_and_does_not_report_a_session(sessions, tmp_path):
    import multiprocessing
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(AgentError, match="conpty_start_failed"):
        sessions.start([str(tmp_path / "absent.exe")], tmp_path, "task", 10,
                       lambda: True, lambda: False, tty=True)
    assert not sessions.items
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_pty_does_not_use_read_authorization(local):  # noqa: F811
    from test_agent_local_tools import execute
    tools, context, _, _ = local
    with pytest.raises(AgentToolRejected, match="invalid_operation_classification"):
        execute(tools, {**context, "authorization": "read"}, "ryn_shell_execute",
                {"command": "git status", "tty": True}, "pty_read_denied")
    assert not tools.processes.items


@WINDOWS
def test_tool_interface_pty_session(local):  # noqa: F811
    from test_agent_local_tools import execute
    tools, context, _, _ = local
    result = execute(tools, context, "ryn_shell_execute", {"command": "$x=Read-Host 'NAME'; Write-Output ('OK_'+$x)", "tty": True, "background": True}, "pty_start")
    ident = result["process_id"]
    until(tools.processes, ident, "NAME:")
    execute(tools, context, "ryn_process_write", {"process_id": ident, "text": "", "rows": 25, "cols": 90}, "pty_resize")
    execute(tools, context, "ryn_process_write", {"process_id": ident, "text": "tool\r"}, "pty_input")
    assert "OK_tool" in finished(tools.processes, ident)["output"]


# Reuse the existing task/ledger fixture rather than bypassing LocalTools.
from test_agent_local_tools import local  # noqa: E402,F401
