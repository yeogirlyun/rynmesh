"""Regression coverage for permission changes, response retention and usage estimates."""
import pytest
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime.execution_policy import (
    Decision,
    decide,
    literal_command,
    shell_read,
    verified_read_classification,
)
from rynmesh.agent_runtime import budget, execution_policy, local_processes
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected, encoded


def test_powershell_paths_preserve_shell_interpolation_guards():
    command = r'Get-ChildItem "D:\code\rynmesh"'
    assert shell_read({"command": command, "shell": "powershell"})
    assert literal_command(command, "sh") is None
    for value in ('$HOME', '`whoami', '$(whoami)'):
        assert not shell_read({"command": f'Get-ChildItem "{value}"', "shell": "powershell"})


def test_global_permissions_do_not_wake_waiting_or_reorder_paused_tasks(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "s"}))
    waiting = runtime.create({"message": "Share"})
    advance(runtime, 2)
    assert runtime.get(waiting["id"])["status"] == "waiting_approval"
    paused = runtime.create({"message": "Old task"})
    def age(data):
        for ident in (waiting["id"], paused["id"]):
            data["tasks"][ident]["updated_at"] = 1
        data["tasks"][paused["id"]]["status"] = "paused"
    runtime.store.transaction(age)
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    for ident in (waiting["id"], paused["id"]):
        row = runtime.get(ident)
        assert row["status"] == "paused"
        assert row["updated_at"] == 1
        assert row["pending_approval"] is None
        assert row["events"][-1]["type"] == "permissions_changed"
    advance(runtime)
    assert not tools.calls
    assert len(model.calls) == 1


@pytest.mark.parametrize("chunks", [None, [500], [200, 300]])
def test_output_limit_keeps_utf8_prefix_after_store_reload(tmp_path, chunks):
    from rynmesh.agent_runtime import output_policy

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(output_policy, "answer_limit", lambda _row: 1000)
        text = "汉" * 500
        runtime, model, tools, key = make_runtime(tmp_path, assistant(text), assistant("续"))
        runtime.update_settings({"max_output_chars": 1000})
        if chunks:
            step = [0]
            def complete(model_id, messages, tool_specs, cancelled, on_event):
                step[0] += 1
                if step[0] == 1:
                    for size in chunks:
                        on_event({"type": "text_delta", "text": "汉" * size})
                    raise AssertionError("overflow must stop generation")
                return assistant("续")
            model.complete = complete
        created = runtime.create({"message": "Answer"})
        advance(runtime, 1)
        row = AgentStore(tmp_path, key).read()["tasks"][created["id"]]
        assert row["status"] == "queued"
        assert row["answer_continuation"]["text"] == "汉" * 333
        assert len(row["answer_continuation"]["text"].encode()) <= 1000
        advance(runtime, 3)
        done = AgentStore(tmp_path, key).read()["tasks"][created["id"]]
        assert done["status"] == "completed"
        assert done["result"] == ("汉" * 333) + "续"
        assert not tools.calls


def test_partial_usage_uses_actual_response_size_in_runtime(tmp_path):
    response = {**assistant("A short answer"), "usage": {"input_tokens": 100}}
    runtime, _, _, _ = make_runtime(tmp_path, response)
    created = runtime.create({"message": "Answer"})
    advance(runtime)
    record = runtime.store.read()["tasks"][created["id"]]["budget"]["records"][0]
    assert record["charged_tokens"] == 100 + len(encoded(response["message"]))
    assert record["output_tokens"] is None
    assert record["output_estimate_basis"] == "response_utf8_bytes"


def test_partial_usage_estimate_preserves_cached_discount_and_known_output():
    row = {"limits": {"max_seconds": 30}, "steps": 0, "elapsed_seconds": 0}
    rec = budget.reserve(row, "model", "main", input_bound=1000, output_bound=393216)
    budget.settle(row, rec["id"], usage={"input_tokens": 1000, "cached_tokens": 800}, response_bytes=300, cached_input_weight=0.1)
    assert row["budget"]["records"][0]["charged_tokens"] == 580
    rec = budget.reserve(row, "model", "main", input_bound=1000, output_bound=393216)
    budget.settle(row, rec["id"], usage={"input_tokens": 10, "output_tokens": 0}, response_bytes=300)
    assert row["budget"]["records"][1]["charged_tokens"] == 10


def test_powershell_smart_quotes_and_dashes_rejected_as_read():
    exploit_cmd = 'Get-Content "x\u2019;Remove-Item y;\u2019"'
    assert literal_command(exploit_cmd, "powershell") is None
    assert not shell_read({"command": exploit_cmd, "shell": "powershell"})
    decision = decide(
        {"name": "ryn_shell_execute", "arguments": {"command": exploit_cmd, "shell": "powershell"}},
        {"effect": "write"},
        {"permission_mode": "read_only"},
        {"permission_mode": "read_only"},
    )
    assert decision == Decision("deny", "read_only")

    # U+2014 em-dash and all non-ASCII quote/dash aliases rejected inside or outside quotes
    assert literal_command("Get-ChildItem \u2014Recurse", "powershell") is None
    assert not shell_read({"command": "Get-ChildItem \u2014Recurse", "shell": "powershell"})

    non_ascii_chars = (
        "\u2018", "\u2019", "\u201a", "\u201b",
        "\u201c", "\u201d", "\u201e", "\u201f",
        "\u2013", "\u2014", "\u2015",
    )
    for ch in non_ascii_chars:
        assert literal_command(f'Get-Content "note{ch}name.txt"', "powershell") is None
        assert literal_command(f"Get-ChildItem {ch}Name", "powershell") is None
        assert not shell_read({"command": f'Get-Content "note{ch}name.txt"', "shell": "powershell"})

    # Ordinary ASCII-quoted reads remain read-only
    assert shell_read({"command": "Get-Content 'readme.md'", "shell": "powershell"})
    assert shell_read({"command": 'Get-Content "readme.md"', "shell": "powershell"})
    assert shell_read({"command": "Get-ChildItem -Recurse", "shell": "powershell"})


def test_verified_read_classification_rejects_legacy_smart_quote_records():
    import hashlib

    args = {"command": 'Get-Content "x\u2019;Remove-Item y;\u2019"', "shell": "powershell"}
    args_sha = hashlib.sha256(encoded(args)).hexdigest()
    legacy_record = {
        "name": "ryn_shell_execute",
        "arguments": args,
        "arguments_sha256": args_sha,
        "authorization": "read",
        "operation_id": "op_legacy_smart_quote",
        "task_id": "task_1",
        "operation_classification": {
            "version": 1,
            "effect_class": "read",
            "basis": "literal_shell_read",
            "operation_id": "op_legacy_smart_quote",
            "task_id": "task_1",
            "tool": "ryn_shell_execute",
            "arguments_sha256": args_sha,
            "workspace_root": "",
            "goal_revision": 1,
            "scope_revision": 1,
            "context_epoch": 0,
            "control_revision": 1,
        },
    }
    assert not verified_read_classification(legacy_record)

    ascii_args = {"command": 'Get-Content "readme.md"', "shell": "powershell"}
    ascii_sha = hashlib.sha256(encoded(ascii_args)).hexdigest()
    valid_record = {
        **legacy_record,
        "arguments": ascii_args,
        "arguments_sha256": ascii_sha,
        "operation_classification": {
            **legacy_record["operation_classification"],
            "arguments_sha256": ascii_sha,
        },
    }
    assert verified_read_classification(valid_record)


def test_literal_read_command_double_guard_rejects_smart_quotes(monkeypatch):
    monkeypatch.setattr(execution_policy, "shell_read", lambda arguments: True)
    monkeypatch.setattr(execution_policy, "literal_command", lambda command, shell: ["Get-Content", "x\u2019y"])
    with pytest.raises(AgentToolRejected) as exc_info:
        local_processes.literal_read_command({"command": 'Get-Content "x\u2019y"', "shell": "powershell"})
    assert exc_info.value.args[0] == "agent_invalid_operation_classification"


def test_s04_i8_t1_git_status_and_git_diff_read_only_and_executes(tmp_path):
    import os
    import subprocess

    assert execution_policy.literal_segments("git status && git diff", "sh") == [
        ["git", "status"],
        ["git", "diff"],
    ]
    assert execution_policy.literal_segments("git status || git diff; git log", "powershell") == [
        ["git", "status"],
        ["git", "diff"],
        ["git", "log"],
    ]
    for bad in ("git status & git diff", "git status > out", "git status < in", "git status `id`", "git $(id)", "git (status)", "git {status}", "git #c", "git status\ngit diff"):
        assert execution_policy.literal_segments(bad, "sh") is None

    assert shell_read({"command": "git status && git diff", "shell": "sh"}) is True
    assert shell_read({"command": "git status && git diff", "shell": "powershell"}) is True
    assert "merge-base" in execution_policy.GIT_SAFE_SUBCOMMANDS

    if os.name == "nt":
        prepared = local_processes.literal_read_command({"command": "git status && git diff", "shell": "powershell"})
        assert prepared is not None
        assert "-NoProfile" in list(prepared)
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        proc = subprocess.run(list(prepared), cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0
    else:
        prepared_sh = local_processes.literal_read_command({"command": "git status && git diff", "shell": "sh"})
        assert prepared_sh is not None
        assert list(prepared_sh)[:2] == ["/bin/sh", "-c"]


def test_s04_i8_t2_git_status_semicolon_rm_rejected():
    assert shell_read({"command": "git status; rm x", "shell": "sh"}) is False
    assert shell_read({"command": "git status; rm x", "shell": "powershell"}) is False


def test_s04_i8_t3_rg_pipe_remove_item_and_non_filter_downstream_rejected():
    assert shell_read({"command": "rg x | Remove-Item", "shell": "powershell"}) is False
    assert shell_read({"command": "Get-ChildItem | Get-Content", "shell": "powershell"}) is False
    assert shell_read({"command": "git status | git diff", "shell": "sh"}) is False
    for pipe_only in ("Select-Object -First 5", "Measure-Object", "Sort-Object", "Where-Object Name -Like '*.py'"):
        assert shell_read({"command": pipe_only, "shell": "powershell"}) is False


def test_s04_i8_t4_get_childitem_pipe_select_object_read_only_and_qualified(tmp_path):
    import os
    import subprocess

    assert shell_read({"command": "Get-ChildItem | Select-Object -First 5", "shell": "powershell"}) is True
    assert shell_read({"command": "Get-ChildItem | Sort-Object Name | Measure-Object", "shell": "powershell"}) is True
    assert shell_read({"command": "Get-ChildItem | Where-Object Name -Like '*.py'", "shell": "powershell"}) is True

    if os.name == "nt":
        for idx in range(7):
            (tmp_path / f"item_{idx}.py").write_text("x", encoding="utf-8")
        argv = local_processes.literal_read_command(
            {"command": "Get-ChildItem -Name | Select-Object -First 5", "shell": "powershell"}
        )
        assert argv is not None
        assert "-NoProfile" in argv
        script = argv[-1]
        assert r"Microsoft.PowerShell.Management\get-childitem" in script
        assert r"Microsoft.PowerShell.Utility\select-object" in script
        proc = subprocess.run(list(argv), cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(lines) == 5


def test_s04_i8_t5_workspace_boundary_rejects_escape_and_propagates_call_chain(tmp_path):
    import hashlib

    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    assert shell_read({"command": r"Get-Content ..\..\x", "shell": "powershell"}, workspace_root=str(ws)) is False
    assert shell_read({"command": r"Get-Content ..\..\x", "shell": "powershell"}) is False
    assert shell_read({"command": f'Get-Content "{outside}"', "shell": "powershell"}, workspace_root=str(ws)) is False
    assert shell_read({"command": r"Get-Content sub\..\readme.md", "shell": "powershell"}, workspace_root=str(ws)) is True
    assert shell_read({"command": f'Get-Content "{ws / "readme.md"}"', "shell": "powershell"}, workspace_root=str(ws)) is True

    escape_args = {"command": r"Get-Content ..\secret.txt", "shell": "powershell"}
    cls = execution_policy.classify_operation(
        "ryn_shell_execute",
        escape_args,
        {"effect": "write"},
        operation_id="op_ws",
        task_id="task_ws",
        workspace_root=str(ws),
    )
    assert cls["effect_class"] == "write"

    decision = decide(
        {"name": "ryn_shell_execute", "arguments": {"command": f'Get-Content "{outside}"', "shell": "powershell"}},
        {"effect": "write"},
        {"permission_mode": "read_only", "local_execution": {"workspace_root": str(ws)}},
        {"permission_mode": "read_only"},
    )
    assert decision == Decision("deny", "read_only")

    outside_args = {"command": f'Get-Content "{outside}"', "shell": "powershell"}
    outside_sha = hashlib.sha256(encoded(outside_args)).hexdigest()
    legacy_record = {
        "name": "ryn_shell_execute",
        "arguments": outside_args,
        "arguments_sha256": outside_sha,
        "authorization": "read",
        "operation_id": "op_legacy_outside",
        "task_id": "task_ws",
        "operation_classification": {
            "version": 1,
            "effect_class": "read",
            "basis": "literal_shell_read",
            "operation_id": "op_legacy_outside",
            "task_id": "task_ws",
            "tool": "ryn_shell_execute",
            "arguments_sha256": outside_sha,
            "workspace_root": str(ws),
            "goal_revision": 1,
            "scope_revision": 1,
            "context_epoch": 0,
            "control_revision": 1,
        },
    }
    assert verified_read_classification(legacy_record) is False


def test_s04_i8_t6_executable_key_and_windows_git_exe_path_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_policy.os, "name", "nt", raising=False)
    assert execution_policy.executable_key(r"C:\Program Files\Git\cmd\git.exe") == "git"
    assert execution_policy.executable_key("/usr/local/bin/rg") == "rg"
    assert execution_policy.executable_key(r"D:\tools\Run.CMD") == "run"
    assert execution_policy.executable_key(r"D:\tools\Check.BAT") == "check"
    assert execution_policy.executable_key(r"D:\tools\Legacy.COM") == "legacy"

    assert shell_read(
        {"command": r'"C:\Program Files\Git\cmd\git.exe" status', "shell": "powershell"},
        workspace_root=str(tmp_path),
    ) is True


def test_s04_i8_t7_smart_quotes_still_rejected_in_multi_segment_and_expanded_posix_programs():
    assert shell_read(
        {"command": 'Get-ChildItem | Select-String "x\u2019;Remove-Item y;\u2019"', "shell": "powershell"}
    ) is False

    for cmd in (
        "grep -rn x .",
        "egrep -n foo a.txt",
        "fgrep bar a.txt",
        "find . -name '*.py'",
        "sed -n '1,20p' f",
        "sed -n '1,5p;10,15p' f",
        "git log | head",
        "sort a.txt | uniq -c",
        "cut -d: -f1 a.txt",
        "stat a.txt",
        "file a.txt",
        "du -sh .",
        "tree src",
        "which git",
        "basename src/a.py",
        "dirname src/a.py",
        "realpath a.py",
    ):
        assert shell_read({"command": cmd, "shell": "sh"}) is True, cmd

    for ps_cmd in ("Resolve-Path readme.md", "Split-Path readme.md"):
        assert shell_read({"command": ps_cmd, "shell": "powershell"}) is True, ps_cmd


def test_s04_i8_t8_posix_program_unsafe_flags_and_arguments_rejected():
    for bad_cmd in (
        "find . -delete",
        "find . -exec rm {} +",
        "find . -execdir rm x ;",
        "find . -ok rm x ;",
        "find . -okdir rm x ;",
        "find . -fprint out.txt",
        "find . -fprint0 out.txt",
        "find . -fprintf out.txt %p",
        "find . -fls out.txt",
        "sed '1,20p' f",
        "sed -n '1w out' f",
        "sed -n '1e id' f",
        "sed -n '1r other' f",
        "sed -i 's/a/b/' f",
        "sed --in-place -n '1p' f",
        "sed -ni '1,20p' f",
        "sed -n -f evil.sed f",
        "sort -o f f",
        "sort --output=f f",
        "sort -ro f f",
        "uniq a b",
        "tree -o out.txt src",
        "file -C -m magic",
    ):
        assert shell_read({"command": bad_cmd, "shell": "sh"}) is False, bad_cmd


def test_s04_i8_t9_get_filehash_read_only_and_uses_utility_module():
    assert shell_read({"command": "Get-FileHash x", "shell": "powershell"}) is True
    argv = local_processes.literal_read_command({"command": "Get-FileHash x", "shell": "powershell"})
    assert argv is not None
    assert "-NoProfile" in argv
    assert r"Microsoft.PowerShell.Utility\get-filehash 'x'" in argv[-1]

    mgmt_argv = local_processes.literal_read_command({"command": "Resolve-Path x", "shell": "powershell"})
    assert mgmt_argv is not None
    assert r"Microsoft.PowerShell.Management\resolve-path 'x'" in mgmt_argv[-1]


def test_s04_i8_t10_rg_zip_decompression_flags_rejected():
    for cmd in ("rg -z x", "rg --search-zip x", "rg -iz x", "rg -uz x"):
        assert shell_read({"command": cmd, "shell": "powershell"}) is False, cmd
        assert shell_read({"command": cmd, "shell": "sh"}) is False, cmd


def test_s04_i8_t11_rg_case_insensitive_still_read_only():
    assert shell_read({"command": "rg -i x", "shell": "powershell"}) is True
    assert shell_read({"command": "rg -i x", "shell": "sh"}) is True


