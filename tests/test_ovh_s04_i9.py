"""Overhaul S04-I9: safe git config/env overrides for host and literal read-only git commands."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from test_agent_local_tools import execute
from test_agent_local_tools import local as local  # noqa: F401
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime import execution_policy, local_processes, projects


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "tester@example.test"],
        ["config", "user.name", "Tester"],
    ):
        res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
        if res.returncode != 0 and args[0] == "init":
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True, env=env)
            subprocess.run(["git", "-C", str(repo), "checkout", "-B", "main"], check=True, capture_output=True, text=True, env=env)
    (repo / "tracked.txt").write_text("initial line\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, capture_output=True, text=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True, text=True, env=env)


def _write_probe_script(script_path: Path, body: str) -> str:
    script_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{Path(sys.executable).as_posix()}" "{script_path.as_posix()}"'


def test_t1_opening_workspace_does_not_execute_repo_fsmonitor_or_hooks(tmp_path):
    repo = tmp_path / "untrusted-repo"
    _init_git_repo(repo)

    marker = tmp_path / "fsmonitor_triggered.marker"
    fsmonitor_cmd = _write_probe_script(
        tmp_path / "fsmonitor_probe.py",
        f"""
        import pathlib, sys
        pathlib.Path({str(marker)!r}).write_text("executed", encoding="utf-8")
        sys.stdout.buffer.write(b"\\x00")
        """,
    )
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", fsmonitor_cmd], check=True)

    runtime, _, _, _ = make_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(repo)})
    assert selected["id"].startswith("project_")
    snapshot = projects.inspect(repo)

    assert snapshot["git"]["branch"] == "main"
    assert snapshot["git"]["changes"] == []
    assert not marker.exists(), "opening/inspecting workspace executed untrusted core.fsmonitor"


def test_t2_project_git_strips_rynmesh_tokens_sets_optional_locks_and_timeout(tmp_path, monkeypatch):
    repo = tmp_path / "token-probe-repo"
    _init_git_repo(repo)

    env_dump = tmp_path / "fsmonitor_env.json"
    fsmonitor_cmd = _write_probe_script(
        tmp_path / "fsmonitor_env_probe.py",
        f"""
        import json, os, pathlib, sys
        pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding="utf-8")
        sys.stdout.buffer.write(b"\\x00")
        """,
    )
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", fsmonitor_cmd], check=True)

    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "node-local-secret-token")
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "node-network-secret-key")
    monkeypatch.setenv("rynmesh_case_test", "node-lowercase-secret")

    recorded_calls: list[tuple[list[str], dict]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        recorded_calls.append((list(cmd), dict(kwargs)))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(projects.subprocess, "run", spy_run)
    projects.inspect(repo)

    assert len(recorded_calls) == 2

    for _cmd, kwargs in recorded_calls:
        env = kwargs.get("env")
        assert isinstance(env, dict), "projects.inspect subprocess.run must pass explicit env"
        assert env.get("GIT_OPTIONAL_LOCKS") == "0"
        assert not any(k.upper().startswith("RYNMESH_") for k in env), "RYNMESH_* leaked to git env"
        assert "node-local-secret-token" not in json.dumps(env)
        assert kwargs.get("timeout") == 5

    from rynmesh.agent_runtime.git_safety import SAFE_GIT_CONFIG

    for cmd, _kwargs in recorded_calls:
        assert cmd[: 1 + len(SAFE_GIT_CONFIG)] == ["git", *SAFE_GIT_CONFIG]
        assert cmd[1 + len(SAFE_GIT_CONFIG) : 3 + len(SAFE_GIT_CONFIG)] == ["-C", str(repo.resolve())]

    assert not env_dump.exists()

    # Even if -c core.fsmonitor=false were hypothetically bypassed, passed env still withholds RYNMESH_*.
    bypass_env = recorded_calls[1][1]["env"]
    real_run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, env=bypass_env, check=True)
    assert env_dump.exists()
    leaked_env = json.loads(env_dump.read_text(encoding="utf-8"))
    assert leaked_env.get("GIT_OPTIONAL_LOCKS") == "0"
    assert not any(k.upper().startswith("RYNMESH_") for k in leaked_env)


def test_t3_literal_readonly_git_blocks_fsmonitor_ext_diff_textconv_and_scopes_optional_locks(local, tmp_path, monkeypatch):  # noqa: F811
    tools, context, workspace, _ = local
    _init_git_repo(workspace)
    (workspace / ".gitattributes").write_text("*.txt diff=probe\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("initial line\nmodified line\n", encoding="utf-8")

    fsmonitor_marker = tmp_path / "shell_fsmonitor.marker"
    ext_diff_marker = tmp_path / "shell_ext_diff.marker"
    textconv_marker = tmp_path / "shell_textconv.marker"

    fsmonitor_cmd = _write_probe_script(
        tmp_path / "shell_fsmonitor.py",
        f"""
        import pathlib, sys
        pathlib.Path({str(fsmonitor_marker)!r}).write_text("triggered", encoding="utf-8")
        sys.stdout.buffer.write(b"\\x00")
        """,
    )
    ext_diff_cmd = _write_probe_script(
        tmp_path / "shell_ext_diff.py",
        f"""
        import pathlib
        pathlib.Path({str(ext_diff_marker)!r}).write_text("triggered", encoding="utf-8")
        print("EXT_DIFF_RAN")
        """,
    )
    textconv_cmd = _write_probe_script(
        tmp_path / "shell_textconv.py",
        f"""
        import pathlib
        pathlib.Path({str(textconv_marker)!r}).write_text("triggered", encoding="utf-8")
        print("TEXTCONV_RAN")
        """,
    )
    subprocess.run(["git", "-C", str(workspace), "config", "core.fsmonitor", fsmonitor_cmd], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "diff.external", ext_diff_cmd], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "diff.probe.textconv", textconv_cmd], check=True)

    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "secret-token")
    monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)

    popen_envs: list[tuple[list[str], dict]] = []
    real_popen = local_processes.subprocess.Popen

    def spy_popen(args, *p_args, **p_kwargs):
        popen_envs.append((list(args), dict(p_kwargs.get("env") or {})))
        return real_popen(args, *p_args, **p_kwargs)

    monkeypatch.setattr(local_processes.subprocess, "Popen", spy_popen)

    status_res = execute(tools, context, "ryn_shell_execute", {"command": "git status --short"}, "git-status")
    diff_res = execute(tools, context, "ryn_shell_execute", {"command": "git diff"}, "git-diff")
    show_res = execute(tools, context, "ryn_shell_execute", {"command": "git show HEAD"}, "git-show")
    log_res = execute(tools, context, "ryn_shell_execute", {"command": "git log -n 1 -p"}, "git-log")

    assert status_res["exit_code"] == 0
    assert diff_res["exit_code"] == 0
    assert "+modified line" in diff_res["output"]
    assert "EXT_DIFF_RAN" not in diff_res["output"]
    assert "TEXTCONV_RAN" not in diff_res["output"]
    assert show_res["exit_code"] == 0
    assert log_res["exit_code"] == 0

    assert not fsmonitor_marker.exists()
    assert not ext_diff_marker.exists()
    assert not textconv_marker.exists()

    # Step 5: GIT_OPTIONAL_LOCKS=0 is set on literal read-only git Popen, NOT in global child_environment.
    assert "GIT_OPTIONAL_LOCKS" not in local_processes.child_environment()
    assert len(popen_envs) == 4
    for _argv, env in popen_envs:
        assert env.get("GIT_OPTIONAL_LOCKS") == "0"
        assert "RYNMESH_LOCAL_TOKEN" not in env

    # Non-literal shell command must NOT receive GIT_OPTIONAL_LOCKS=0 automatically.
    popen_envs.clear()
    non_literal = execute(tools, context, "ryn_shell_execute", {"command": "git status --short; echo ok"}, "git-compound")
    assert non_literal["exit_code"] == 0
    assert "GIT_OPTIONAL_LOCKS" not in popen_envs[-1][1]

    # Direct Processes.start env_overrides parameter works explicitly.
    popen_envs.clear()
    echo_cmd = local_processes.shell_command("powershell" if os.name == "nt" else "sh", "echo override-check")
    pid = tools.processes.start(
        echo_cmd,
        workspace,
        "task",
        10,
        lambda: True,
        lambda: False,
        env_overrides={"GIT_OPTIONAL_LOCKS": "0"},
    )
    tools.processes.wait(pid, "task", lambda: False)
    assert popen_envs[-1][1].get("GIT_OPTIONAL_LOCKS") == "0"


def test_t4_git_merge_base_read_classification_and_unsafe_flag_rejection():
    assert execution_policy.shell_read({"command": "git merge-base HEAD main"}) is True
    cls = execution_policy.classify_operation(
        "ryn_shell_execute",
        {"command": "git merge-base HEAD main"},
        {"effect": "write"},
    )
    assert cls["effect_class"] == "read"
    assert cls["basis"] == "literal_shell_read"

    assert execution_policy.shell_read({"command": "git merge-base -c x HEAD"}) is False
    assert execution_policy.shell_read({"command": "git merge-base -c core.fsmonitor=true HEAD main"}) is False
    assert execution_policy.shell_read({"command": "git merge-base --config-env=core.fsmonitor=X HEAD main"}) is False
    assert execution_policy.shell_read({"command": "git merge-base --git-dir=/tmp/other HEAD main"}) is False
    assert execution_policy.shell_read({"command": "git -c core.fsmonitor=true merge-base HEAD main"}) is False


def test_t5_literal_read_git_argv_contains_three_c_pairs_and_two_no_diff_flags():
    expected_hooks = f"core.hooksPath={'NUL' if os.name == 'nt' else '/dev/null'}"

    for subcmd, extra in (("diff", ["HEAD~1"]), ("show", ["HEAD"]), ("log", ["-n", "3"])):
        cmd_str = " ".join(["git", subcmd, *extra])
        argv = local_processes.literal_read_command({"command": cmd_str})
        assert argv is not None
        assert argv[1] == "--no-pager"
        c_indices = [idx for idx, token in enumerate(argv) if token == "-c"]
        assert len(c_indices) == 3
        c_values = [argv[idx + 1] for idx in c_indices]
        assert c_values == [
            "core.fsmonitor=false",
            expected_hooks,
            "safe.bareRepository=explicit",
        ]
        sub_idx = argv.index(subcmd)
        assert argv[sub_idx + 1 : sub_idx + 3] == ["--no-ext-diff", "--no-textconv"]
        assert argv[sub_idx + 3 :] == extra

    for subcmd, extra in (("status", ["--short"]), ("merge-base", ["HEAD", "main"]), ("rev-parse", ["HEAD"])):
        cmd_str = " ".join(["git", subcmd, *extra])
        argv = local_processes.literal_read_command({"command": cmd_str})
        assert argv is not None
        assert argv[1] == "--no-pager"
        c_indices = [idx for idx, token in enumerate(argv) if token == "-c"]
        assert len(c_indices) == 3
        assert [argv[idx + 1] for idx in c_indices] == [
            "core.fsmonitor=false",
            expected_hooks,
            "safe.bareRepository=explicit",
        ]
        assert argv[argv.index(subcmd) :] == [subcmd, *extra]
