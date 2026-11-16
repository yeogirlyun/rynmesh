"""Overhaul S04-I16 tests: non-TTY child stdin closed by default, pipe on demand or background/yield."""
from __future__ import annotations

import os
import sys
import time

import pytest
from test_agent_local_tools import execute
from test_agent_local_tools import local as local_fixture

from rynmesh.agent_runtime import execution_policy
from rynmesh.agent_runtime.local_processes import Processes
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import AgentToolRejected


@pytest.fixture
def local(tmp_path):
    yield from local_fixture.__wrapped__(tmp_path)


def _py_shell_cmd(code: str) -> str:
    if os.name == "nt":
        exe = sys.executable.replace("'", "''")
        return f"& '{exe}' -u -c \"{code}\""
    return f'"{sys.executable}" -u -c "{code}"'


def test_t1_foreground_python_input_hits_immediate_eof_and_closed_write_has_correction(local, tmp_path):
    """T1: 前台 python -c 'input()' 立即 EOF 结束而不是超时；向已关闭 stdin 写入报 agent_process_stdin_closed 并带 correction（覆盖改动 1, 2, 4, 5）。"""
    _, _, _, schema = SPECS["ryn_shell_execute"]
    assert schema["properties"]["stdin"] == {
        "type": "string",
        "enum": ["closed", "pipe"],
        "default": "closed",
    }

    # Processes.start with stdin_mode="closed" uses DEVNULL + stdin_closed=True -> immediate EOFError.
    processes = Processes()
    try:
        t0 = time.monotonic()
        ident = processes.start(
            [sys.executable, "-u", "-c", "input()"],
            tmp_path,
            "task_t1",
            3,
            lambda: True,
            lambda: False,
            stdin_mode="closed",
        )
        assert processes.get(ident, "task_t1")["stdin_closed"] is True
        res = processes.wait(ident, "task_t1", lambda: False)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert res["running"] is False
        assert res["stop_reason"] == "exited_nonzero"
        assert "EOFError" in res["output"]
    finally:
        processes.close()

    # Foreground ryn_shell_execute also defaults to closed stdin -> immediate EOFError, not timeout/yield.
    tools, context, _, _ = local
    try:
        t1 = time.monotonic()
        fg = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": _py_shell_cmd("input()"), "timeout_seconds": 4},
            "op_s04i16_t1_fg",
        )
        fg_elapsed = time.monotonic() - t1
        assert fg_elapsed < 3.5
        assert fg["running"] is False
        assert fg["stop_reason"] == "exited_nonzero"
        assert "EOFError" in fg["output"]

        # Running process started with explicit stdin="closed" rejects write with exact code + correction.
        sleep_cmd = _py_shell_cmd("import time; print('WAIT', flush=True); time.sleep(20)")
        closed_bg = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": sleep_cmd, "background": True, "stdin": "closed"},
            "op_s04i16_t1_closed_bg",
        )
        with pytest.raises(AgentToolRejected) as exc_info:
            tools.perform(
                "ryn_process_write",
                {"process_id": closed_bg["process_id"], "text": "hello\n"},
                context,
                lambda: False,
            )
        assert str(exc_info.value) == "agent_process_stdin_closed"
        assert exc_info.value.correction == 'Start the command with stdin="pipe" to write to it.'
    finally:
        tools.processes.close()


def test_t2_explicit_stdin_pipe_allows_writing(local):
    """T2: stdin='pipe' 后可写（覆盖改动 1, 2, 5）。"""
    import copy
    import json
    from rynmesh.agent_runtime.tool_router import normalize_tool_calls

    desc, _, _, schema = SPECS["ryn_shell_execute"]
    spec = {"type": "function", "function": {"name": "ryn_shell_execute", "description": desc, "parameters": copy.deepcopy(schema)}}
    tools, context, _, _ = local
    cmd = _py_shell_cmd("import sys; print('GOT:' + sys.stdin.readline().strip(), flush=True)")
    routed = normalize_tool_calls(
        [{"id": "c_t2", "type": "function", "function": {"name": "ryn_shell_execute", "arguments": json.dumps({"command": cmd, "stdin": "pipe", "yield_seconds": 0.2})}}],
        [spec],
    )[0]
    assert routed["error_result"] is None
    try:
        started = execute(
            tools,
            context,
            "ryn_shell_execute",
            routed["arguments"],
            "op_s04i16_t2_pipe",
        )
        pid = started["process_id"]
        wrote = execute(
            tools,
            context,
            "ryn_process_write",
            {"process_id": pid, "text": "hello-pipe\n", "eof": True},
            "op_s04i16_t2_write",
        )
        assert wrote["input_delivered"] is True
        done = tools.processes.wait(pid, context["task_id"], lambda: False)
        assert done["running"] is False
        assert done["exit_code"] == 0
        assert "GOT:hello-pipe" in done["output"]
    finally:
        tools.processes.close()


@pytest.mark.parametrize("launch_extra", [{"background": True}, {"yield_seconds": 0.2}])
def test_t3_background_and_yield_default_to_writable_pipe(local, launch_extra):
    """T3: 后台或显式 yield_seconds 未写 stdin 时默认仍可写，区别于前台默认关闭（覆盖改动 2, 3, 5）。"""
    tools, context, _, _ = local
    fast_fg = _py_shell_cmd("print('FG_OK')")
    cmd = _py_shell_cmd("import sys; print('BG:' + sys.stdin.readline().strip(), flush=True)")
    tag = "bg" if "background" in launch_extra else "yield"
    try:
        fg_res = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": fast_fg},
            f"op_s04i16_t3_fg_{tag}",
        )
        assert tools.processes.get(fg_res["process_id"], context["task_id"])["stdin_closed"] is True

        started = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd, **launch_extra},
            f"op_s04i16_t3_{tag}",
        )
        pid = started["process_id"]
        assert started["running"] is True
        assert tools.processes.get(pid, context["task_id"])["stdin_closed"] is False
        wrote = execute(
            tools,
            context,
            "ryn_process_write",
            {"process_id": pid, "text": f"from-{tag}\n", "eof": True},
            f"op_s04i16_t3_write_{tag}",
        )
        assert wrote["input_delivered"] is True
        done = tools.processes.wait(pid, context["task_id"], lambda: False)
        assert done["running"] is False
        assert done["exit_code"] == 0
        assert f"BG:from-{tag}" in done["output"]
    finally:
        tools.processes.close()


def test_t4_literal_read_accepts_only_closed_stdin():
    """T4: 字面读带 stdin='pipe' 不判只读，省略或 stdin='closed' 仍判只读（覆盖改动 1）。"""
    assert "stdin" in execution_policy.SHELL_ARGUMENT_KEYS

    read_cmd = "Get-Content README.md" if os.name == "nt" else "cat README.md"
    assert execution_policy.shell_read({"command": read_cmd}) is True
    assert execution_policy.shell_read({"command": read_cmd, "stdin": "closed"}) is True
    assert execution_policy.shell_read({"command": read_cmd, "stdin": "pipe"}) is False
    assert execution_policy.shell_read({"command": "git status", "stdin": "pipe"}) is False
    assert execution_policy.shell_read({"command": "rg pattern src", "stdin": "pipe"}) is False

    pipe_cls = execution_policy.classify_operation(
        "ryn_shell_execute",
        {"command": read_cmd, "stdin": "pipe"},
        {"effect": "write"},
    )
    closed_cls = execution_policy.classify_operation(
        "ryn_shell_execute",
        {"command": read_cmd, "stdin": "closed"},
        {"effect": "write"},
    )
    assert pipe_cls["effect_class"] == "write"
    assert closed_cls["effect_class"] == "read"
    assert closed_cls["basis"] == "literal_shell_read"
