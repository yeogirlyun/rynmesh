"""Remove Ryn's 8000-character gate while retaining native shell semantics."""
import copy
import json
import os
from types import SimpleNamespace

import pytest
from test_agent_local_tools import local as local_fixture

from rynmesh.agent_runtime import local_processes
from rynmesh.agent_runtime.local_processes import Processes, shell_command
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.output_policy import MAX_TOOL_CALL_ARGUMENT_BYTES
from rynmesh.agent_runtime.tool_router import ToolCallProtocolError, normalize_tool_calls


@pytest.fixture
def local(tmp_path):
    yield from local_fixture.__wrapped__(tmp_path)


def powershell_command(length=16000, *, tail=None):
    text = '中文 "双引号" 与 \'单引号\'\n第二行 😀'
    tail = tail or ("$text = @'\n" + text + "\n'@\n"
                    "[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'long-result.txt'), $text, [System.Text.UTF8Encoding]::new($false))\n"
                    "[Console]::WriteLine($text)\n[Console]::WriteLine('ROOT=' + $PSScriptRoot)\nexit 7\n")
    return "#" + "x" * (length - len(tail) - 2) + "\n" + tail, text


def shell_spec():
    description, _, _, schema = SPECS["ryn_shell_execute"]
    return {"type": "function", "function": {"name": "ryn_shell_execute", "description": description,
                                              "parameters": copy.deepcopy(schema)}}


def proposal(command):
    return {"id": "long_command", "type": "function", "function": {"name": "ryn_shell_execute",
            "arguments": json.dumps({"command": command}, ensure_ascii=False)}}


def test_shell_schema_removes_only_command_length_gate_and_retains_shared_byte_budget():
    spec = shell_spec()
    assert spec["function"]["parameters"]["properties"]["command"] == {"type": "string", "minLength": 1}
    # Schema acceptance does not promise argv above the host OS launch limit.
    for length in (8585, 16000, 40000):
        command, _ = powershell_command(length)
        routed = normalize_tool_calls([proposal(command)], [spec])[0]
        assert routed["error_result"] is None
        assert routed["arguments"]["command"] == command
    with pytest.raises(ToolCallProtocolError) as rejected:
        normalize_tool_calls([proposal("x" * MAX_TOOL_CALL_ARGUMENT_BYTES)], [spec])
    assert rejected.value.reason == "arguments_too_large"
    assert normalize_tool_calls([proposal("")], [spec])[0]["error_result"]["error"] == "agent_tool_arguments_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell execution")
@pytest.mark.parametrize("length", [8585, 16000])
def test_real_long_powershell_cwd_unicode_quotes_newlines_and_nonzero_exit(local, length):
    tools, context, workspace, _ = local
    cwd = workspace / "工作 空间"
    cwd.mkdir()
    command, expected = powershell_command(length)
    assert len(command) == length
    routed = normalize_tool_calls([proposal(command)], [shell_spec()])[0]
    args = {**routed["arguments"], "cwd": str(cwd)}
    preview = tools.preview("ryn_shell_execute", args, context)
    result = tools.execute("ryn_shell_execute", args, operation_id="long-powershell", context=context,
                           expected_preview=preview, cancelled=lambda: False)
    assert result["exit_code"] == 7 and result["stop_reason"] == "exited_nonzero"
    assert expected in result["output"].replace("\r\n", "\n")
    assert "ROOT=\r\n" in result["output"] or result["output"].endswith("ROOT=\n")
    assert (cwd / "long-result.txt").read_text(encoding="utf-8") == expected
    assert tools.ledger_factory().get("long-powershell")["status"] == "confirmed"


@pytest.mark.skipif(os.name != "nt", reason="Windows task stdin")
@pytest.mark.parametrize("pipeline", [False, True])
def test_long_powershell_retains_native_task_stdin(tmp_path, pipeline):
    tail = ("$input | ForEach-Object { 'got:' + $_ }; exit 0\n" if pipeline else
            "$value=[Console]::ReadLine(); Write-Output $value; exit 0\n")
    command, _ = powershell_command(tail=tail)
    processes = Processes()
    try:
        ident = processes.start(shell_command("powershell", command), tmp_path, "task", 10, lambda: True, lambda: False)
        processes.write(ident, "task", "input-still-available\n", eof=pipeline)
        result = processes.wait(ident, "task", lambda: False)
        assert result["exit_code"] == 0 and "input-still-available" in result["output"]
    finally:
        processes.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native exit semantics")
@pytest.mark.parametrize("tail, expected", [
    ("& $env:COMSPEC /d /c 'exit 17'\n", 1),
    ("& $env:COMSPEC /d /c 'exit 17'\nWrite-Output 'success after native failure'\n", 0),
    ("throw 'fixture command failure'\n", 1),
])
def test_long_powershell_retains_native_and_terminating_exit_semantics(tmp_path, tail, expected):
    processes = Processes()
    try:
        outcomes = []
        for command in (tail, powershell_command(tail=tail)[0]):
            ident = processes.start(shell_command("powershell", command), tmp_path, "task", 10, lambda: True, lambda: False)
            outcomes.append(processes.wait(ident, "task", lambda: False)["exit_code"])
        assert outcomes == [expected, expected]
    finally:
        processes.close()


def test_posix_launch_plan_keeps_original_command(monkeypatch):
    monkeypatch.setattr(local_processes, "os", SimpleNamespace(name="posix"))
    command = "#" + "x" * 16000 + "\nprintf '中文 \"quote\"\\n'; exit 11"
    assert shell_command("sh", command) == ["/bin/sh", "-c", command]


@pytest.mark.skipif(os.name == "nt", reason="POSIX host execution")
def test_real_long_posix_command(tmp_path):
    command = "#" + "x" * 16000 + "\nprintf '中文 \"quote\"\\n'; pwd; exit 11"
    processes = Processes()
    try:
        ident = processes.start(shell_command("sh", command), tmp_path, "task", 10, lambda: True, lambda: False)
        result = processes.wait(ident, "task", lambda: False)
        assert result["exit_code"] == 11 and '中文 "quote"' in result["output"]
        assert str(tmp_path) in result["output"]
    finally:
        processes.close()
