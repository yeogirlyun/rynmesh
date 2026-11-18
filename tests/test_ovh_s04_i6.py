"""S04-I6 real shell resolution, schema registration and environment projection."""
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_local_tools import local  # noqa: F401

from rynmesh.agent_runtime import execution_policy, local_processes, local_specs


@pytest.fixture(autouse=True)
def clear_detection_cache():
    detector = getattr(local_processes, "detect_shell", None)
    if detector is not None:
        detector.cache_clear()
    yield
    detector = getattr(local_processes, "detect_shell", None)
    if detector is not None:
        detector.cache_clear()


def host(monkeypatch, name="nt", system="win32", shell=""):
    monkeypatch.setattr(local_processes, "os", SimpleNamespace(**{
        **vars(os), "name": name, "environ": {"SHELL": shell, "ProgramFiles": r"C:\Program Files", "SystemRoot": r"C:\Windows"}}))
    monkeypatch.setattr(local_processes, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(local_processes, "child_environment", lambda **kwargs: {"PATH": "fixture-path"})


def specs_snapshot():
    source = Path(local_specs.__file__).read_text(encoding="utf-8")
    namespace = {"__name__": "rynmesh.agent_runtime.shell_specs_test", "__package__": "rynmesh.agent_runtime"}
    exec(compile(source, local_specs.__file__, "exec"), namespace)
    return namespace


@pytest.mark.parametrize("bad", ["exit", "timeout", "missing", "invalid_version"])
def test_t1_powershell_checks_candidates_in_order_and_caches_version(monkeypatch, bad):
    host(monkeypatch)
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: {
        "pwsh": "broken-pwsh.exe", "powershell": "working-powershell.exe"}.get(name))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "broken-pwsh.exe":
            if bad == "timeout":
                raise subprocess.TimeoutExpired(argv, 2)
            if bad == "missing":
                raise FileNotFoundError(argv[0])
            return SimpleNamespace(returncode=1 if bad == "exit" else 0, stdout="not a version", stderr="")
        return SimpleNamespace(returncode=0, stdout="5.1\n", stderr="")
    monkeypatch.setattr(local_processes.subprocess, "run", run)
    assert local_processes.detect_shell("powershell") == {"path": "working-powershell.exe", "version": "5.1"}
    assert local_processes.detect_shell("powershell")["version"] == "5.1"
    assert [argv[0] for argv, _ in calls] == ["broken-pwsh.exe", "working-powershell.exe"]
    for argv, kwargs in calls:
        assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
        assert "$PSVersionTable.PSVersion.Major" in argv[-1] and "$PSVersionTable.PSVersion.Minor" in argv[-1]
        assert kwargs["timeout"] == 2 and kwargs["capture_output"] is True


def test_t1_powershell_uses_fixed_path_when_path_candidates_fail(monkeypatch):
    host(monkeypatch)
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: None)
    calls = []
    monkeypatch.setattr(local_processes.subprocess, "run", lambda argv, **kwargs: (
        calls.append(argv), SimpleNamespace(returncode=0, stdout="7.4", stderr=""))[1])
    found = local_processes.detect_shell("powershell")
    assert found == {"path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "version": "7.4"}
    assert len(calls) == 1
    assert local_processes.shell_command("powershell", "echo ok", login=False)[0] == found["path"]
    assert len(calls) == 1  # Execution reuses the successful probe, including its version.


def test_t1_failed_probe_can_recover_and_successful_version_stays_cached(monkeypatch):
    host(monkeypatch)
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: "fixture-pwsh.exe" if name == "pwsh" else None)
    ready = [False]
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if not ready[0]:
            raise subprocess.TimeoutExpired(argv, 2)
        return SimpleNamespace(returncode=0, stdout="7.4", stderr="")
    monkeypatch.setattr(local_processes.subprocess, "run", run)
    assert local_processes.detect_shell("powershell") is None
    ready[0] = True
    assert local_processes.detect_shell("powershell") == {"path": "fixture-pwsh.exe", "version": "7.4"}
    count = len(calls)
    assert local_processes.detect_shell("powershell")["version"] == "7.4" and len(calls) == count


@pytest.mark.parametrize("system, available, expected", [
    ("linux", {"bash": "/usr/bin/bash", "zsh": "/usr/bin/zsh"}, "/usr/bin/bash"),
    ("darwin", {"/bin/zsh": "/bin/zsh", "bash": "/bin/bash"}, "/bin/zsh"),
    ("linux", {"zsh": "/usr/bin/zsh"}, "/usr/bin/zsh"),
    ("linux", {}, "/bin/sh"),
])
def test_t2_fish_falls_back_to_platform_shell_order(monkeypatch, system, available, expected):
    host(monkeypatch, "posix", system, "/usr/bin/fish")
    searched = []
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: (searched.append(name), available.get(name))[1])
    assert local_processes.detect_shell("sh") == {"path": expected}
    assert "/usr/bin/fish" not in searched
    assert local_processes.shell_command("sh", "echo ok", login=False) == [expected, "-c", "echo ok"]


def test_t2_preferred_bash_or_zsh_is_used(monkeypatch):
    host(monkeypatch, "posix", "linux", "/opt/shells/zsh")
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: name if name == "/opt/shells/zsh" else None)
    assert local_processes.detect_shell("sh")["path"] == "/opt/shells/zsh"


@pytest.mark.parametrize("source", ["git", "program_files", "path", "wsl_only"])
def test_t3_git_bash_order_excludes_wsl_and_registers_matching_schema(monkeypatch, source):
    host(monkeypatch)
    git_bash = r"C:\Tools\Git\bin\bash.exe"
    program_bash = r"C:\Program Files\Git\bin\bash.exe"
    path_bash = r"C:\Portable\bash.exe"
    seen = []
    mapping = {"git": r"C:\Tools\Git\cmd\git.exe", "bash.exe": path_bash}
    if source == "git":
        mapping[git_bash] = git_bash
    elif source == "program_files":
        mapping[program_bash] = program_bash
    elif source == "wsl_only":
        mapping["bash.exe"] = r"C:\Windows\System32\bash.exe"
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: (seen.append(name), mapping.get(name))[1])
    snapshot = specs_snapshot()
    expected = ["powershell", "cmd"] + ([] if source == "wsl_only" else ["bash"])
    assert snapshot["SHELLS"] == expected
    assert snapshot["SPECS"]["ryn_shell_execute"][3]["properties"]["shell"]["enum"] == expected
    detected = local_processes.detect_shell("bash")
    assert detected == (None if source == "wsl_only" else {"path": {"git": git_bash, "program_files": program_bash, "path": path_bash}[source]})
    assert seen[:2] == ["git", git_bash]
    if source != "git":
        assert seen[2] == program_bash
    if source in {"path", "wsl_only"}:
        assert seen[3] == "bash.exe"


def test_t4_git_bash_reaches_actual_preparation_and_remains_nonliteral(monkeypatch):
    host(monkeypatch)
    bash = r"C:\Tools\Git\bin\bash.exe"
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: {
        "git": r"C:\Tools\Git\cmd\git.exe", bash: bash}.get(name))
    monkeypatch.setattr(local_processes, "resolve_rg_executable", lambda: "fixture-rg")
    command = "printf '%s' ready"
    assert local_processes.prepare_shell_command({"shell": "bash", "command": command}) == [bash, "-c", command]
    assert execution_policy.literal_command("cat file.txt", "bash") is None
    assert execution_policy.shell_read({"shell": "bash", "command": "cat file.txt"}) is False


@pytest.mark.parametrize("version", ["5.1", "7.4"])
def test_t5_environment_reports_real_detection_and_host_fields(local, monkeypatch, version):
    from rynmesh.agent_runtime import local_browser, native_computer

    tools, context, _, _ = local
    host(monkeypatch)
    ps = "fixture-powershell.exe"
    bash = r"C:\Tools\Git\bin\bash.exe"
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: {
        "pwsh": ps, "git": r"C:\Tools\Git\cmd\git.exe", bash: bash}.get(name))
    probes = []
    monkeypatch.setattr(local_processes.subprocess, "run", lambda argv, **kwargs: (
        probes.append(argv), SimpleNamespace(returncode=0, stdout=version, stderr=""))[1])
    monkeypatch.setattr(local_browser, "find_browser", lambda: None)
    monkeypatch.setattr(native_computer, "environment_computer_fields", lambda store: {})
    result = tools.perform("ryn_local_environment", {}, context, lambda: False)
    assert result["powershell_version"] == version and result["posix_shell"] is None
    assert result["shells"] == specs_snapshot()["SHELLS"] == ["powershell", "cmd", "bash"]
    assert result["home"] == str(Path.home()) and result["temp_dir"] == tempfile.gettempdir()
    assert result["os_version"] == platform.version()
    if version.startswith("5."):
        assert result["powershell_notes"] == "Windows PowerShell 5.1: no && / || / ?? / ternary; use ; and if"
    else:
        assert "powershell_notes" not in result
    assert local_processes.shell_command("powershell", "Write-Output 1", login=False)[0] == ps
    assert len(probes) == 1
    desc = specs_snapshot()["SPECS"]["ryn_shell_execute"][0]
    assert "ryn_local_environment" in desc and "bash/zsh" in desc


def test_t5_posix_environment_reports_selected_shell(local, monkeypatch):
    tools, context, _, _ = local
    host(monkeypatch, "posix", "linux", "/usr/bin/fish")
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: "/usr/bin/bash" if name == "bash" else None)
    result = tools.perform("ryn_local_environment", {}, context, lambda: False)
    assert result["shells"] == ["sh"] and result["posix_shell"] == "/usr/bin/bash"
    assert result["powershell_version"] is None and "powershell_notes" not in result
