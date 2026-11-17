"""S04-I13: intercept patch invocations through the real agent/tool path."""
import json

import pytest
from test_agent_codex_loop import workstation, WorkstationTools
from test_agent_runtime import advance, assistant, approve

from rynmesh.agent_runtime.local_processes import prepare_shell_command
from rynmesh.agent_runtime.store import AgentToolRejected

PATCH = "*** Begin Patch\n*** Add File: result.txt\n+hello\n*** End Patch"
CORRECTION = "Call ryn_apply_patch with the patch text instead of running apply_patch in a shell."


def setup(tmp_path, command, mode="workspace", **args):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_shell_execute", args={"command": command, **args}),
        assistant("done"), mode=mode)
    runtime.tools.names = (*WorkstationTools.names, "ryn_apply_patch")
    return runtime, model, workspace


@pytest.mark.parametrize("command", [
    f"apply_patch <<'EOF'\n{PATCH}\nEOF",
    f"applypatch <<EOF\n{PATCH}\nEOF",
    f'apply_patch "{PATCH}"',
])
def test_T1_heredoc_really_applies_and_returns_marker(tmp_path, command):
    runtime, model, workspace = setup(tmp_path, command)
    task = runtime.create({"message": "Create result.txt"})
    advance(runtime, 3)
    assert (workspace / "result.txt").read_text() == "hello\n"
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["receipts"][0]["tool"] == "ryn_apply_patch"
    assert row["receipts"][0]["result"]["intercepted_from"] == "ryn_shell_execute"
    assert json.loads(model.calls[-1][1][-2]["content"])["intercepted_from"] == "ryn_shell_execute"


def test_T2_powershell_here_string(tmp_path):
    runtime, _, workspace = setup(tmp_path, f"@'\n{PATCH}\n'@ | apply_patch")
    runtime.create({"message": "Create result.txt"})
    advance(runtime, 3)
    assert (workspace / "result.txt").read_text() == "hello\n"


def test_T3_compound_is_executed_as_shell(tmp_path):
    # Define an actual shell function so this test does not require an apply_patch binary.
    command = "function apply_patch { param($p) Write-Output 'patch called' }; apply_patch '*** Begin Patch'; Write-Output 'after'"
    runtime, _, _ = setup(tmp_path, command)
    task = runtime.create({"message": "Run this compound command"})
    advance(runtime, 3)
    receipt = runtime.store.read()["tasks"][task["id"]]["receipts"][0]
    assert receipt["tool"] == "ryn_shell_execute"
    assert "after" in json.dumps(receipt["result"])
    from rynmesh.agent_runtime.patch_intercept import extract_patch
    assert extract_patch(f'apply_patch "{PATCH}"; echo after') is None
    assert prepare_shell_command({"command": f'apply_patch "{PATCH}"; echo after'})


def test_T4_approval_uses_patch_preview_and_resumes(tmp_path):
    runtime, _, workspace = setup(tmp_path, f"apply_patch <<EOF\n{PATCH}\nEOF", "confirm_changes")
    task = runtime.create({"message": "Create result.txt"})
    advance(runtime, 2)
    pending = runtime.get(task["id"])["pending_approval"]
    assert pending["tool"] == "ryn_apply_patch"
    assert "patch" in pending["arguments"] and "command" not in pending["arguments"]
    assert "hello" in json.dumps(pending["preview"])
    assert not (workspace / "result.txt").exists()
    approve(runtime, task["id"])
    advance(runtime, 2)
    assert (workspace / "result.txt").read_text() == "hello\n"
    assert runtime.tools.local.change_review(task["id"], str(workspace))["files"]


def test_T5_incomplete_patch_gets_correction(tmp_path):
    runtime, model, _ = setup(tmp_path, "apply_patch <<EOF\n*** Begin Patch\n*** Add File: x")
    runtime.create({"message": "Create x"})
    advance(runtime, 3)
    assert CORRECTION in json.dumps(model.calls[-1][1])


@pytest.mark.parametrize("command", [f"apply_patch <<EOF\n{PATCH}\nEOF", "apply_patch <<EOF\n*** Begin Patch"])
def test_fallback_refuses_patch_without_dispatch(command):
    with pytest.raises(AgentToolRejected) as error:
        prepare_shell_command({"command": command})
    assert error.value.correction == CORRECTION


@pytest.mark.parametrize("prefix,args", [("cd nested && ", {}), ("", {"cwd": "nested"})])
def test_cwd_is_preserved(tmp_path, prefix, args):
    runtime, _, workspace = setup(tmp_path, prefix + f"apply_patch <<EOF\n{PATCH}\nEOF", **args)
    (workspace / "nested").mkdir()
    runtime.create({"message": "Create nested/result.txt"})
    advance(runtime, 3)
    assert (workspace / "nested/result.txt").read_text() == "hello\n"
    assert not (workspace / "result.txt").exists()


def test_patch_is_a_queue_barrier_before_read(tmp_path):
    runtime, model, workspace = setup(tmp_path, f"apply_patch <<EOF\n{PATCH}\nEOF")
    read = assistant(name="ryn_file_read", args={"path": "result.txt"}, call_id="read_after")["message"]["tool_calls"][0]
    model.responses[0]["message"]["tool_calls"].append(read)
    task = runtime.create({"message": "Create result.txt then read it"})
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task["id"]]
    assert [r["tool"] for r in row["receipts"]] == ["ryn_apply_patch"]
    assert row["queue"][0]["name"] == "ryn_file_read"
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task["id"]]
    assert [r["tool"] for r in row["receipts"]] == ["ryn_apply_patch", "ryn_file_read"]
    assert "hello" in json.dumps(row["receipts"][1]["result"])


def test_prefixes_all_operation_paths():
    from rynmesh.agent_runtime.patch_intercept import patch_arguments
    from pathlib import Path
    root = Path.cwd()
    patch = "\n".join(["*** Begin Patch", "*** Update File: a", "*** Move to: b", "-old", "+new", "*** Delete File: c", "*** End Patch"])
    args = patch_arguments({"cwd": "tests", "patch": patch}, {}, str(root))
    assert "*** Update File: tests/a" in args["patch"]
    assert "*** Move to: tests/b" in args["patch"]
    assert "*** Delete File: tests/c" in args["patch"]


@pytest.mark.parametrize("command", [
    f"apply_patch <<EOF\n{PATCH}\nEOF\necho after",
    f"apply_patch <<EOF\n{PATCH}\nEOF && echo after",
    f"@'\n{PATCH}\n'@ | apply_patch; echo after",
])
def test_compound_forms_are_not_intercepted(command):
    from rynmesh.agent_runtime.patch_intercept import extract_patch
    assert extract_patch(command) is None
