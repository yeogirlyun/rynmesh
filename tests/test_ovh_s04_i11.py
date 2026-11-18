"""S04-I11: static command recognition and real encrypted runtime approval flow.

Only the OS execution boundary is a fixture; dangerous commands never run.
"""
import importlib
import json
import os
import shlex
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime import execution_policy
from test_agent_runtime import advance, approve, assistant
from test_agent_shipped_permission_mode import runtime_at
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def scan(command, monkeypatch, *, windows=True, shell=None, depth=0):
    module = importlib.import_module("rynmesh.agent_runtime.dangerous_commands")
    platform = SimpleNamespace(name="nt" if windows else "posix", path=os.path)
    monkeypatch.setattr(module, "os", platform)
    monkeypatch.setattr(execution_policy, "os", platform)
    return module.dangerous_shell({"command": command, "shell": shell or ("powershell" if windows else "sh")}, depth=depth)


@pytest.mark.parametrize("command,reason", [
    ("rm -rf x", "forced_rm"),
    ("sudo rm -fr x", "forced_rm"),
    ("sudo -u root -- rm -f x", "forced_rm"),
    ("env -i A=1 rm -f x", "forced_rm"),
    ("env A=$VALUE rm --force x", "forced_rm"),
    ('bash -c "rm -rf x"', "forced_rm"),
    ('sh -lc "rm --force x"', "forced_rm"),
    ("trap 'rm -rf x' EXIT", "forced_rm"),
    ('powershell -NoProfile -Command "Remove-Item x -Recurse -Force"', "force_delete"),
    ('cmd /d /c "del /f /s /q x"', "force_delete"),
    ("Remove-Item x -Recurse -Force", "force_delete"),
    ("ri x -FoRcE", "force_delete"),
    ("rm x -Force", "force_delete"),
    ("del /f /s /q x", "force_delete"),
    ("erase /f x", "force_delete"),
    ("rd /s /q x", "force_delete"),
    ("rmdir /q /s x", "force_delete"),
    ("Start-Process https://a.b", "open_url"),
    ("start https://a.b", "open_url"),
    ("ii 'https://a.b'", "open_url"),
    ("explorer https://a.b", "open_url"),
    ("mshta https://a.b", "open_url"),
    ("rundll32 url.dll,FileProtocolHandler https://a.b", "open_url"),
    ('& "C:\\Program Files\\Browser\\CHROME.EXE" https://a.b', "open_url"),
    ("Start-Process ('https://a.b');", "open_url"),
    ("if ($ok) { Remove-Item x -Force }", "force_delete"),
    ("Write-Output ready && rm -rf x || echo failed", "forced_rm"),
    ("echo ready\trm -rf x", "forced_rm"),
    ("echo ready\nrm -rf x", "forced_rm"),
    ("echo ready | Remove-Item x -Force", "force_delete"),
])
def test_t2_required_static_dangers(command, reason, monkeypatch):
    assert scan(command, monkeypatch) == reason


@pytest.mark.parametrize("command", [
    "Get-ChildItem -Force; Remove-Item x",
    "Get-ChildItem -Force | Remove-Item x",
    "echo rm -rf",
    'echo "rm -rf x; Remove-Item x -Force"',
    "Write-Output 'Start-Process https://a.b'",
    "rm $X",
    "rm x",
    "rm -- -f x",
    "rm -- --force x",
    "Remove-Item x -Recurse",
    "rmdir /s x",
    "Start-Process notes.txt",
    "$PROGRAM -rf x",
    'bash -c "echo rm -rf" rm -rf x',
    "trap -p",
    "trap - EXIT",
])
def test_t3_static_boundaries_do_not_trigger(command, monkeypatch):
    assert scan(command, monkeypatch) is None


def test_windows_rules_are_disabled_on_posix(monkeypatch):
    assert scan("Remove-Item x -Force", monkeypatch, windows=False) is None
    assert scan("Start-Process https://a.b", monkeypatch, windows=False) is None
    assert scan("/bin/rm --force x", monkeypatch, windows=False) == "forced_rm"


@pytest.mark.parametrize("command,reason", [
    ("env -i -- A=1 /bin/rm -rf x", "forced_rm"),
    (r"rundll32 C:\Windows\System32\url.dll,FileProtocolHandler https://a.b", "open_url"),
])
def test_normalized_wrappers_keep_the_visible_target(command, reason, monkeypatch):
    assert scan(command, monkeypatch) == reason


def test_t4_nine_shell_wrappers_are_a_danger_even_with_safe_body(monkeypatch):
    command = "echo safe"
    for _ in range(8):
        command = "sh -c " + shlex.quote(command)
    assert scan(command, monkeypatch, windows=False, shell="sh") is None
    command = "sh -c " + shlex.quote(command)
    assert scan(command, monkeypatch, windows=False, shell="sh") == "wrapper_depth"


@pytest.mark.parametrize("value,expected", [
    ('"(https://a.b);"', True), ("HTTP://a.b/path", True),
    ("https:example", True), ("file:///tmp/a", False),
    ("ftp://a.b", False), ("notes.txt", False),
])
def test_url_detection_uses_parsed_scheme(value, expected):
    module = importlib.import_module("rynmesh.agent_runtime.dangerous_commands")
    assert module._is_http_url(value) is expected


@pytest.mark.parametrize("mode,command,action,basis", [
    ("workspace", "rm -rf x", "ask", "dangerous_command"),
    ("workspace", "rm x", "execute", "automatic"),
    ("automatic", "rm -rf x", "execute", "automatic"),
    ("read_only", "rm -rf x", "deny", "read_only"),
    ("confirm_changes", "rm -rf x", "ask", "confirm_changes"),
])
def test_t1_policy_mode_boundary(mode, command, action, basis):
    decision = execution_policy.decide({"name": "ryn_shell_execute", "arguments": {"command": command}},
        {"effect": "write"}, {"permission_mode": mode, "approved": None}, {})
    assert (decision.action, decision.basis) == (action, basis)


class ShellBoundary:
    def __init__(self):
        self.calls = []

    def specs(self):
        return [{"type": "function", "function": {"name": "ryn_shell_execute", "description": "Shell",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "shell": {"type": "string"},
                "cwd": {"type": "string"}, "timeout_seconds": {"type": "number"}}, "required": ["command"], "additionalProperties": False}}}]

    def metadata(self, name):
        return {"effect": "write", "title": "Shell"}

    def preview(self, name, arguments):
        return {"command": arguments["command"], "cwd": arguments.get("cwd", ".")}

    def execute(self, name, arguments, *, operation_id, cancelled):
        self.calls.append(arguments)
        return {"exit_code": 0, "output": "Synthetic OS boundary; no command executed."}


def shell_runtime(tmp_path, *, mode=None):
    runtime, _, _ = runtime_at(tmp_path, X25519PrivateKey.generate(), assistant(name="ryn_shell_execute", args={"command": "rm -rf old", "cwd": ".", "shell": "sh", "timeout_seconds": 12}), assistant("Done."))
    boundary = ShellBoundary()
    runtime.tools = boundary
    runtime.update_settings({"enabled": True, "model_id": "local:test", **({"permission_mode": mode} if mode is not None else {})})
    task = runtime.create({"message": "Clean the specified old directory."})
    return runtime, boundary, task


def test_t1_workspace_kernel_stages_exact_approval_and_reason_before_dispatch(tmp_path):
    runtime, boundary, task = shell_runtime(tmp_path, mode="workspace")
    advance(runtime, 2)
    row = runtime.get(task["id"])
    assert row["status"] == "waiting_approval"
    approval = row["pending_approval"]
    assert approval["reason"] == "dangerous_command"
    assert approval["dangerous_reason"] == "forced_rm"
    assert approval["arguments"]["command"] == "rm -rf old"
    assert boundary.calls == []
    approve(runtime, task["id"])
    advance(runtime)
    assert len(boundary.calls) == 1
    assert runtime.get(task["id"])["pending_approval"] is None


def test_subagent_danger_is_refused_with_non_forced_correction(tmp_path):
    runtime, boundary, task = shell_runtime(tmp_path, mode="workspace")
    parent = runtime.create({"message": "Coordinate child work."})
    def child(data):
        data["tasks"][parent["id"]]["status"] = "paused"
        data["tasks"][task["id"]]["parent_task_id"] = parent["id"]
    runtime.store.transaction(child)
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["pending_approval"] is None
    assert boundary.calls == []
    results = [json.loads(item["content"]) for item in row["messages"] if item.get("role") == "tool"]
    result = next(item for item in results if item.get("error") == "agent_subagent_approval_blocked")
    assert "or use a non-forced form" in result["correction"]


def test_t5_shipped_automatic_kernel_never_asks_for_the_same_danger(tmp_path):
    runtime, boundary, task = shell_runtime(tmp_path)
    assert runtime.get(task["id"])["permission_mode"] == "automatic"
    advance(runtime, 2)
    assert len(boundary.calls) == 1
    assert runtime.get(task["id"])["pending_approval"] is None
