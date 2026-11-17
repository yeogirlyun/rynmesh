"""S05-I5: strict shell patch recognition through the actual patch tool path."""
import hashlib
import json
import os
import shlex
from types import SimpleNamespace

import pytest
from test_agent_apply_patch import patch, specs
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import local_patch, workspace_artifacts
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.tool_router import normalize_tool_calls

PATCH = patch("*** Add File: hello.txt", "+hello $world; | &&")
COMMAND = "apply_patch <<'EOF'\n" + PATCH + "\nEOF"


@pytest.mark.parametrize("wrapper", ["direct", "bash", "sh", "zsh", "powershell", "pwsh", "cmd", "alias", "cd", "here_string"])
def test_T1_recognizes_only_supported_wrappers_and_keeps_patch_literal(wrapper):
    command, cd = COMMAND, None
    if wrapper in {"bash", "sh", "zsh"}:
        command = wrapper + " -lc " + shlex.quote(COMMAND)
    elif wrapper in {"powershell", "pwsh"}:
        command = wrapper + ' -Command "' + COMMAND + '"'
    elif wrapper == "cmd":
        command = "cmd /c " + COMMAND
    elif wrapper == "alias":
        command = COMMAND.replace("apply_patch", "applypatch", 1)
    elif wrapper == "cd":
        command, cd = "cd 'a b' && " + COMMAND, "a b"
    elif wrapper == "here_string":
        command = "@'\n" + PATCH + "\n'@ | apply_patch"
    assert local_patch.shell_patch(command, "powershell") == {"patch": PATCH, "cd": cd}
    assert local_patch.shell_patch(PATCH, "bash") == {"error": "implicit_patch"}


@pytest.mark.parametrize("command", [
    COMMAND + " || echo fallback", COMMAND + "\necho next", COMMAND + "; echo next",
    "echo hi | " + COMMAND, "echo hi && " + COMMAND, COMMAND + " | cat",
    "cd a b && " + COMMAND, "cd $(pwd) && " + COMMAND, "cd a; " + COMMAND,
    "apply_patch <<'EOF'\n" + PATCH + "\nEOF\nEOF",
])
def test_T1_rejects_multiple_statements_and_nonliteral_cd(command):
    assert local_patch.shell_patch(command, "bash") is None


def test_T1_rebase_all_headers_without_changing_body_or_absolute_paths():
    source = patch("*** Add File: add.txt", "+*** Update File: body.txt", "*** Update File: old.txt",
                   "*** Move to: new.txt", "@@", "-a", "+b", "*** Delete File: gone.txt",
                   "*** Add File: /absolute.txt", "+x", "*** Add File: C:/absolute.txt", "+y")
    rebased = local_patch.rebase_paths(source, "pkg/sub")
    assert "*** Add File: pkg/sub/add.txt" in rebased
    assert "+*** Update File: body.txt" in rebased
    assert "*** Update File: pkg/sub/old.txt" in rebased
    assert "*** Move to: pkg/sub/new.txt" in rebased
    assert "*** Delete File: pkg/sub/gone.txt" in rebased
    assert "*** Add File: /absolute.txt" in rebased and "*** Add File: C:/absolute.txt" in rebased


def route(command=COMMAND, *, menu=True, **kwargs):
    return normalize_tool_calls([{"id": "call_patch", "type": "function", "function": {
        "name": "ryn_shell_execute", "arguments": json.dumps({"command": command, "shell": "bash", "cwd": "pkg"})}}],
        specs(["ryn_shell_execute", *(["ryn_apply_patch"] if menu else [])]),
        metadata=lambda _: {"effect": "write"}, **kwargs)[0]


def test_T2_rewrites_name_arguments_schema_and_hash_only_when_patch_is_offered():
    routed = route("cd sub && " + COMMAND)
    assert routed["name"] == "ryn_apply_patch" and routed["rewritten_from"] == "ryn_shell_execute"
    assert routed["arguments"] == {"patch": PATCH.replace("hello.txt", "pkg/sub/hello.txt")}
    assert routed["schema_snapshot"] == specs(["ryn_apply_patch"])[0]["function"]
    assert json.loads(routed["arguments_json"]) == routed["arguments"]
    assert routed["raw_arguments_json"] == routed["arguments_json"]
    assert routed["arguments_sha256"] == hashlib.sha256(routed["arguments_json"].encode()).hexdigest()
    assert routed["effect"] == "write" and routed["error_result"] is None
    assert route(menu=False)["name"] == "ryn_shell_execute"


def test_T2_bare_patch_has_paired_correction():
    routed = route(PATCH)
    assert routed["error_result"]["not_executed"] is True
    assert "ryn_apply_patch" in routed["error_result"]["correction"]
    assert "patch" in routed["error_result"]["correction"]


def test_T2_resolved_cd_outside_workspace_keeps_shell(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for command in ["cd ../.. && " + COMMAND, "cd '" + str(tmp_path) + "' && " + COMMAND]:
        assert route(command, workspace_root=str(workspace))["name"] == "ryn_shell_execute"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "link"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert route("cd ../link && " + COMMAND, workspace_root=str(workspace))["name"] == "ryn_shell_execute"


class PatchTools:
    def __init__(self, store, home):
        ledger = OperationLedger(home)
        self.local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
        self.local.store = store

    def specs(self):
        return specs(["ryn_shell_execute", "ryn_apply_patch", "ryn_file_read"])

    def metadata(self, name):
        return {"effect": SPECS[name][1], "scope_context_supported": True,
                "preview_precondition": SPECS[name][1] == "write"}

    def preview(self, name, args, *, context):
        return self.local.preview(name, args, context)

    def execute(self, name, args, **kwargs):
        return self.local.execute(name, args, **kwargs)


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes", "read_only"])
def test_T3_real_loop_uses_patch_approval_receipt_review_and_artifacts(tmp_path, mode):
    workspace = tmp_path / "ws"
    (workspace / "pkg/sub").mkdir(parents=True)
    runtime, _, _, _ = make_runtime(tmp_path / "store", assistant(name="ryn_shell_execute", args={
        "command": "cd sub && " + COMMAND, "cwd": "pkg", "shell": "bash"}))
    tools = PatchTools(runtime.store, tmp_path / "store")
    runtime.tools = tools
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": mode})
    task = runtime.create({"message": "Create hello.txt with the supplied patch"})
    try:
        advance(runtime, 2)
        row = runtime.store.read()["tasks"][task["id"]]
        calls = [c for m in row["messages"] for c in m.get("tool_calls", [])]
        assert calls[0]["function"]["name"] == "ryn_apply_patch"
        assert json.loads(calls[0]["function"]["arguments"])["patch"].startswith("*** Begin Patch")
        if mode == "confirm_changes":
            assert row["status"] == "waiting_approval" and not (workspace / "pkg/sub/hello.txt").exists()
        elif mode == "read_only":
            assert not (workspace / "pkg/sub/hello.txt").exists()
            assert row["receipts"][0]["status"] == "not_executed"
            assert row["receipts"][0]["result"]["not_executed"] is True
        else:
            assert (workspace / "pkg/sub/hello.txt").read_text() == "hello $world; | &&\n"
            assert row["receipts"][0]["tool"] == "ryn_apply_patch"
            review = tools.local.change_review(task["id"], str(workspace))
            assert review["files"][0]["path"] == "pkg/sub/hello.txt"
            assert workspace_artifacts.listing(row)["files"][0]["path"] == "pkg/sub/hello.txt"
    finally:
        tools.local.processes.close()


def test_T3_outside_cd_is_not_rewritten_again_by_legacy_dispatch(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, _, _, _ = make_runtime(tmp_path / "store", assistant(name="ryn_shell_execute", args={
        "command": "cd .. && " + COMMAND, "shell": "bash"}))
    tools = PatchTools(runtime.store, tmp_path / "store")
    runtime.tools = tools
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": "automatic"})
    task = runtime.create({"message": "Run the given command"})
    try:
        advance(runtime, 2)
        row = runtime.store.read()["tasks"][task["id"]]
        assert not (tmp_path / "hello.txt").exists()
        assert row["receipts"][0]["tool"] == "ryn_shell_execute"
        assert row["receipts"][0]["status"] == "not_executed"
    finally:
        tools.local.processes.close()
