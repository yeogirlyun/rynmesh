"""Overhaul S04-I20 tests: ConPTY input normalization, terminal query auto-replies, and body-free host error_detail."""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime import local_pty
from rynmesh.agent_runtime.local_processes import Processes, prepare_shell_command
from rynmesh.agent_runtime.store import AgentError

PROBE_SCRIPT = Path(__file__).resolve().parents[1] / "docs" / "acceptance" / "conpty_terminal_query_probe.py"


def test_t1_conpty_input_normalization_stateful_rules_and_controls():
    """T1: 规范化函数单测（a\\nb→a\\rb，a\\r\\nb→a\\rb，跨两次写入的 \\r + \\n，0x08→0x7f，以及控制串不变）。"""
    normalize = local_pty.normalize_conpty_input

    out, prev_cr = normalize("a\nb", False)
    assert out == "a\rb" and prev_cr is False

    out, prev_cr = normalize("a\r\nb", False)
    assert out == "a\rb" and prev_cr is False

    # Split CR + LF across two consecutive write_input calls.
    out1, prev_cr = normalize("line1\r", False)
    assert out1 == "line1\r" and prev_cr is True
    out2, prev_cr = normalize("\n", prev_cr)
    assert out2 == "" and prev_cr is False
    out3, prev_cr = normalize("\n", prev_cr)
    assert out3 == "\r" and prev_cr is False

    # Backspace 0x08 -> DEL 0x7f.
    out_bs, prev_cr = normalize("ab\x08c", False)
    assert out_bs == "ab\x7fc" and prev_cr is False

    # Control sequences from local_processes.py (\x03, \x1a\r) stay intact.
    ctrl_c, prev_cr = normalize("\x03", False)
    assert ctrl_c == "\x03" and prev_cr is False
    ctrl_z, prev_cr = normalize("\x1a\r", False)
    assert ctrl_z == "\x1a\r" and prev_cr is True

    # Verify ConPtyProcess.write_input persists _input_previous_was_cr across calls.
    sent: list[str] = []
    fake_self = SimpleNamespace(
        _send_lock=threading.Lock(),
        _closed=threading.Event(),
        _input_previous_was_cr=False,
        poll=lambda: None,
        _request=lambda action, val: sent.append((action, val)),
    )
    local_pty.ConPtyProcess.write_input(fake_self, "hi\r")
    local_pty.ConPtyProcess.write_input(fake_self, "\nthere\x08\n")
    assert sent == [("write", "hi\r"), ("write", "there\x7f\r")]


def test_t2_real_windows_read_host_accepts_lf_and_conpty_answers_terminal_queries(tmp_path):
    """T2: Windows 实机 Read-Host 收到以 \\n 结尾的输入后返回；终端查询实测脚本验证 ESC[6n/5n/18t 收到应答不卡住。"""
    assert PROBE_SCRIPT.is_file()
    # Cross-platform query parser check (including split escape sequence across chunks).
    r1, tail = local_pty._extract_terminal_query_replies("hello\x1b[6nworld\x1b[18", "")
    assert r1 == ["\x1b[1;1R"] and tail == "\x1b[18"
    r2, tail2 = local_pty._extract_terminal_query_replies("t\x1b[5n!", tail)
    assert r2 == ["\x1b[8;24;80t", "\x1b[0n"] and tail2 == ""

    if os.name != "nt":
        return

    processes = Processes()
    try:
        args = prepare_shell_command(
            {
                "shell": "powershell",
                "command": "$v = Read-Host 'LF_PROMPT'; Write-Output ('LF_ANS=' + $v)",
                "tty": True,
            }
        )
        ident = processes.start(args, tmp_path, "task_lf", 20, lambda: True, lambda: False, tty=True)
        deadline = __import__("time").monotonic() + 10.0
        deadline_out = ""
        while __import__("time").monotonic() < deadline:
            snap = processes.read(ident, "task_lf", wait_seconds=0)
            deadline_out = snap["output"]
            if "LF_PROMPT:" in deadline_out:
                break
            __import__("time").sleep(0.03)
        assert "LF_PROMPT:" in deadline_out

        # Send LF-terminated input (no CR); normalization converts LF -> CR so Read-Host returns.
        processes.write(ident, "task_lf", "hello_from_lf\n")
        done = processes.wait(ident, "task_lf", lambda: False, yield_seconds=8)
        assert done["running"] is False
        assert done["exit_code"] == 0
        assert "LF_ANS=hello_from_lf" in done["output"]
    finally:
        processes.close()

    # Run empirical ConPTY query probe from docs/acceptance/ and verify all 3 queries receive replies.
    import importlib.util

    spec = importlib.util.spec_from_file_location("conpty_terminal_query_probe", PROBE_SCRIPT)
    probe_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe_mod)
    probe_res = probe_mod.run_host_probe()
    assert probe_res["DSR_6n"]["timed_out"] is False
    assert "\\x1b[1;1R" in probe_res["DSR_6n"]["received"]
    assert probe_res["DSR_5n"]["timed_out"] is False
    assert "\\x1b[0n" in probe_res["DSR_5n"]["received"]
    assert probe_res["XTWINOPS_18t"]["timed_out"] is False
    assert "\\x1b[8;24;80t" in probe_res["XTWINOPS_18t"]["received"]


def test_t3_host_oserror_reports_class_name_only_and_logs_start_failure(monkeypatch, tmp_path, caplog):
    """T3: 宿主抛 OSError 时读取结果的 terminal_error_detail 为 OSError 且不含异常消息文本；启动失败同样带类名并记诊断日志。"""
    secret_runtime_msg = "SENSITIVE_RUNTIME_CMD_SECRET_9912"
    secret_startup_msg = "SENSITIVE_STARTUP_ENV_SECRET_8841"

    # 1. Exercise _host + ConPtyProcess._receive + Processes.read when terminal.read raises OSError.
    parent_conn, child_conn = multiprocessing.Pipe()

    class BrokenRuntimePTY:
        pid = 42424

        def __init__(self, cols, rows, backend=None):
            self.size = (cols, rows)

        def spawn(self, appname, cmdline="", cwd=None, env=None):
            self.appname = appname

        def isalive(self):
            return True

        def read(self, blocking=False):
            raise OSError(secret_runtime_msg)

    fake_winpty = SimpleNamespace(PTY=BrokenRuntimePTY, Backend=SimpleNamespace(ConPTY=1))
    monkeypatch.setitem(sys.modules, "winpty", fake_winpty)
    monkeypatch.setattr(local_pty.os, "_exit", lambda code: None)

    saved_env = os.environ.copy()
    try:
        parent_conn.send((["cmd.exe"], str(tmp_path), {"PATH": ""}, 24, 80))
        local_pty._host(child_conn)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)

    started_evt = parent_conn.recv()
    assert started_evt == ("started", 42424)

    # Feed remaining host pipe events through real ConPtyProcess._receive into Processes.read.
    adapter = object.__new__(local_pty.ConPtyProcess)
    adapter.backend = "conpty"
    adapter.rows, adapter.cols = 24, 80
    adapter._lock = threading.RLock()
    adapter._pending = {}
    adapter._output = queue.Queue(maxsize=64)
    adapter._closed = threading.Event()
    adapter._events_done = threading.Event()
    adapter._returncode = None
    adapter.error = None
    adapter.error_detail = None
    adapter._connection = parent_conn
    adapter._receive()
    parent_conn.close()

    processes = Processes()
    try:
        done_evt = threading.Event()
        done_evt.set()
        processes.items["process_pty_err"] = {
            "process": adapter,
            "task_id": "task_err",
            "head": "",
            "tail": local_pty.queue.deque() if hasattr(local_pty.queue, "deque") else __import__("collections").deque(),
            "tail_chars": 0,
            "total_chars": 0,
            "dropped_chars": 0,
            "decoder": None,
            "reason": None,
            "reader_done": done_evt,
            "cleanup_done": done_evt,
            "tty": True,
        }
        payload = processes.read("process_pty_err", "task_err", wait_seconds=0)
        serialized = json.dumps(payload)
        assert payload["terminal_error"] == "agent_conpty_failed"
        assert payload["terminal_error_detail"] == "OSError"
        assert secret_runtime_msg not in serialized
    finally:
        processes.items.clear()
        processes.close()

    # 2. Startup failure path in ConPtyProcess.__init__: preserves class name and writes body-free diagnostic log.
    p2, c2 = multiprocessing.Pipe()
    c2.send(("error", "agent_conpty_failed", "FileNotFoundError"))
    dummy_host = SimpleNamespace(
        pid=None,
        _popen=SimpleNamespace(_handle=1),
        start=lambda: None,
        is_alive=lambda: False,
        close=lambda: None,
    )
    dummy_ctx = SimpleNamespace(
        Pipe=lambda: (p2, SimpleNamespace(close=lambda: None)),
        Process=lambda **kw: dummy_host,
    )
    monkeypatch.setattr(local_pty, "check_pty_support", lambda: None)
    monkeypatch.setattr(local_pty.multiprocessing, "get_context", lambda mode: dummy_ctx)
    monkeypatch.setattr(local_pty, "_windows_job", lambda *a, **kw: None)

    try:
        with caplog.at_level(logging.WARNING, logger="rynmesh.agent_runtime.diagnostic_log"):
            with pytest.raises(AgentError) as start_exc:
                local_pty.ConPtyProcess([str(tmp_path / secret_startup_msg)], tmp_path, {}, 24, 80)
    finally:
        c2.close()

    assert str(start_exc.value) == "agent_conpty_start_failed"
    assert getattr(start_exc.value, "error_detail", None) == "FileNotFoundError"
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent_failure" in log_text
    assert "FileNotFoundError" in log_text
    assert secret_startup_msg not in log_text
