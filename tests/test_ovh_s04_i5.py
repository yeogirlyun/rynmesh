"""Overhaul S04-I5: POSIX login shell environment capture, Windows registry PATH refresh, and PowerShell login mode."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from rynmesh.agent_runtime import execution_policy, local_processes
from rynmesh.agent_runtime.local_specs import SPECS


def test_t1_posix_login_env_capture_merges_strips_rynmesh_drops_pwd_and_prepends_rg(monkeypatch):
    from rynmesh.agent_runtime import login_environment

    login_environment.reset_cache()
    monkeypatch.setattr(login_environment.os, "name", "posix", raising=False)
    monkeypatch.setattr(local_processes.os, "name", "posix", raising=False)

    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "secret-host-token")
    monkeypatch.setenv("INHERITED_ONLY", "kept-from-parent")
    monkeypatch.setenv("SHARED_VAR", "old-parent-value")
    monkeypatch.setenv("PWD", "/stale/daemon/cwd")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    captured_calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        captured_calls.append((list(cmd), dict(kwargs)))
        nul_payload = (
            b"Welcome from zshrc banner\n"
            b"PATH=/opt/homebrew/bin:/Users/dev/.nvm/versions/node/v22/bin:/usr/bin\x00"
            b"SHARED_VAR=from-login-shell\x00"
            b"NVM_DIR=/Users/dev/.nvm\x00"
            b"RYNMESH_NETWORK_KEY=leaked-from-shell\x00"
            b"rynmesh_lower=leaked-lower\x00"
            b"PWD=/Users/dev\x00"
            b"OLDPWD=/tmp\x00"
            b"SHLVL=2\x00"
            b"_=/usr/bin/env\x00"
        )
        return SimpleNamespace(returncode=0, stdout=nul_payload, stderr=b"")

    monkeypatch.setattr(login_environment.subprocess, "run", fake_run)
    monkeypatch.setattr(local_processes, "_bundled_rg_executable", lambda: "/opt/ryn/bin/rg")

    captured = login_environment.capture("/bin/zsh")
    assert len(captured_calls) == 1
    cmd, run_kwargs = captured_calls[0]
    assert cmd == ["/bin/zsh", "-lc", "env -0"]
    assert run_kwargs["stdin"] == subprocess.DEVNULL
    assert run_kwargs["start_new_session"] is True
    assert run_kwargs["timeout"] == 10
    assert "RYNMESH_LOCAL_TOKEN" not in run_kwargs["env"]

    assert captured["SHARED_VAR"] == "from-login-shell"
    assert captured["NVM_DIR"] == "/Users/dev/.nvm"
    assert "PWD" not in captured
    assert "OLDPWD" not in captured
    assert "SHLVL" not in captured
    assert "_" not in captured
    assert not any(k.upper().startswith("RYNMESH_") for k in captured)

    monkeypatch.setattr(login_environment, "default_posix_shell", lambda env=None: "/bin/zsh")
    merged = local_processes.child_environment()
    assert merged["INHERITED_ONLY"] == "kept-from-parent"
    assert merged["SHARED_VAR"] == "from-login-shell"
    assert merged["NVM_DIR"] == "/Users/dev/.nvm"
    assert "PWD" not in merged
    assert "OLDPWD" not in merged
    assert "SHLVL" not in merged
    assert "_" not in merged
    assert not any(k.upper().startswith("RYNMESH_") for k in merged)

    path_parts = merged["PATH"].split(":")
    assert path_parts[0] == "/opt/ryn/bin"
    assert "/opt/homebrew/bin" in path_parts
    login_environment.reset_cache()


def test_t2_login_capture_timeout_and_slow_warmup_fallback_without_blocking(monkeypatch):
    from rynmesh.agent_runtime import login_environment

    login_environment.reset_cache()
    monkeypatch.setattr(login_environment.os, "name", "posix", raising=False)
    monkeypatch.setattr(local_processes.os, "name", "posix", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "must-strip")
    monkeypatch.setattr(local_processes, "_bundled_rg_executable", lambda: None)

    timeout_calls = 0

    def timeout_run(cmd, **kwargs):
        nonlocal timeout_calls
        timeout_calls += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr(login_environment.subprocess, "run", timeout_run)
    monkeypatch.setattr(login_environment, "default_posix_shell", lambda env=None: "/bin/zsh")

    assert login_environment.capture("/bin/zsh") == {}
    # Subsequent calls in the same process do not retry after failure.
    assert login_environment.capture("/bin/zsh") == {}
    assert timeout_calls == 1

    fallback_env = local_processes.child_environment()
    assert fallback_env["PATH"] == "/usr/bin:/bin"
    assert "RYNMESH_LOCAL_TOKEN" not in fallback_env

    # Step 3: background warmup in Processes.__init__ + bounded wait fallback when warmup exceeds wait budget.
    login_environment.reset_cache()
    release_slow = threading.Event()
    warmup_started = threading.Event()

    def slow_run(cmd, **kwargs):
        warmup_started.set()
        release_slow.wait(3.0)
        return SimpleNamespace(returncode=0, stdout=b"PATH=/slow/login/bin:/usr/bin\x00", stderr=b"")

    monkeypatch.setattr(login_environment.subprocess, "run", slow_run)
    monkeypatch.setattr(login_environment, "WARMUP_WAIT_SECONDS", 0.15)
    procs = local_processes.Processes()
    try:
        assert warmup_started.wait(1.0), "Processes.__init__ did not start background warmup daemon"
        t0 = time.monotonic()
        non_blocking_env = local_processes.child_environment()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert non_blocking_env["PATH"] == "/usr/bin:/bin"
    finally:
        release_slow.set()
        procs.close()
        login_environment.reset_cache()


def test_t3_login_environment_captures_only_once_per_shell_even_under_concurrency(monkeypatch):
    from rynmesh.agent_runtime import login_environment

    login_environment.reset_cache()
    monkeypatch.setattr(login_environment.os, "name", "posix", raising=False)

    call_count = 0
    entered = threading.Event()
    proceed = threading.Event()

    def counted_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        entered.set()
        proceed.wait(1.0)
        return SimpleNamespace(returncode=0, stdout=b"FOO=from_once\x00PATH=/custom/bin:/usr/bin\x00", stderr=b"")

    monkeypatch.setattr(login_environment.subprocess, "run", counted_run)

    results: list[dict[str, str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(login_environment.capture("/bin/bash")))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    assert entered.wait(1.0)
    proceed.set()
    for t in threads:
        t.join(2.0)

    # Repeat sequential calls and verify returned copies cannot mutate cache.
    first = login_environment.capture("/bin/bash")
    first["FOO"] = "mutated-by-caller"
    second = login_environment.capture("/bin/bash")

    assert call_count == 1
    assert len(results) == 4
    assert all(r["FOO"] == "from_once" for r in results)
    assert second["FOO"] == "from_once"
    login_environment.reset_cache()


def test_t4_windows_registry_path_reread_expansion_and_deduplication(monkeypatch):
    monkeypatch.setattr(local_processes.os, "name", "nt", raising=False)
    monkeypatch.setenv("CUSTOM_SDK", r"C:\SDKs\Custom")
    monkeypatch.setenv("SYSTEMROOT_TEST", r"C:\Windows")
    monkeypatch.setenv("PATH", r"C:\Existing\Bin;C:\Shared\Tools\\")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "secret")
    monkeypatch.setattr(local_processes, "_bundled_rg_executable", lambda: r"C:\Ryn\bin\rg.exe")

    hkcu_marker = object()
    hklm_marker = object()

    class _FakeKey:
        def __init__(self, value: str):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_open_key(hive, subkey, *args):
        if hive is hkcu_marker and subkey == "Environment":
            return _FakeKey(r"%CUSTOM_SDK%\bin;c:\existing\bin;C:\UserNew\bin")
        if hive is hklm_marker and subkey == r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment":
            return _FakeKey(r"%SYSTEMROOT_TEST%\System32;C:\shared\tools;C:\MachineNew\bin")
        raise FileNotFoundError(subkey)

    def fake_query_value_ex(key_obj, value_name):
        assert value_name.lower() == "path"
        return key_obj.value, 2

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=hkcu_marker,
        HKEY_LOCAL_MACHINE=hklm_marker,
        OpenKey=fake_open_key,
        QueryValueEx=fake_query_value_ex,
    )
    monkeypatch.setattr(local_processes, "winreg", fake_winreg, raising=False)

    env = local_processes.child_environment()
    assert "RYNMESH_LOCAL_TOKEN" not in env
    parts = env["PATH"].split(";")

    # Bundled rg directory is placed at the very front after registry PATH merge.
    assert parts[0] == r"C:\Ryn\bin"
    assert r"C:\SDKs\Custom\bin" in parts
    assert r"C:\Windows\System32" in parts
    assert r"C:\UserNew\bin" in parts
    assert r"C:\MachineNew\bin" in parts

    norm_parts = [p.rstrip("\\/").lower() for p in parts]
    assert norm_parts.count(r"c:\existing\bin") == 1
    assert norm_parts.count(r"c:\shared\tools") == 1


def test_t5_shell_command_and_schema_login_parameter_controls_noprofile(monkeypatch):
    _, _, _, schema = SPECS["ryn_shell_execute"]
    assert schema["properties"]["login"] == {"type": "boolean", "default": True}
    assert "login" in execution_policy.SHELL_ARGUMENT_KEYS

    monkeypatch.setattr(local_processes, "_POWERSHELL_PROFILE_DISABLED", False, raising=False)
    monkeypatch.setattr(local_processes.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: r"C:\Program Files\PowerShell\7\pwsh.exe")
    monkeypatch.setattr(local_processes, "child_environment", lambda: {"PATH": "fixture-path"})
    monkeypatch.setattr(local_processes, "detect_shell", lambda kind, path=None: (
        {"path": "/bin/bash"} if kind == "sh" else {"path": r"C:\Program Files\PowerShell\7\pwsh.exe", "version": "7.4"}))

    default_cmd = local_processes.prepare_shell_command({"command": "Write-Output 'hello'", "shell": "powershell"})
    assert "-NoLogo" in default_cmd
    assert "-NonInteractive" in default_cmd
    assert "-NoProfile" not in default_cmd

    explicit_login_true = local_processes.prepare_shell_command(
        {"command": "Write-Output 'hello'", "shell": "powershell", "login": True}
    )
    assert "-NoProfile" not in explicit_login_true

    explicit_login_false = local_processes.prepare_shell_command(
        {"command": "Write-Output 'hello'", "shell": "powershell", "login": False}
    )
    assert "-NoLogo" in explicit_login_false
    assert "-NonInteractive" in explicit_login_false
    assert "-NoProfile" in explicit_login_false

    direct_false = local_processes.shell_command("powershell", "Write-Output 1", login=False)
    assert "-NoProfile" in direct_false

    # POSIX still uses -c even when login=True because login environment is captured once.
    monkeypatch.setattr(local_processes.os, "name", "posix", raising=False)
    assert local_processes.shell_command("sh", "echo ok", login=True) == ["/bin/bash", "-c", "echo ok"]
    assert local_processes.shell_command("sh", "echo ok", login=False) == ["/bin/bash", "-c", "echo ok"]


def test_t6_literal_read_always_keeps_noprofile_and_uses_merged_path_in_which(monkeypatch):
    monkeypatch.setattr(local_processes, "_POWERSHELL_PROFILE_DISABLED", False, raising=False)
    monkeypatch.setattr(local_processes.os, "name", "nt", raising=False)

    # shell_read remains True regardless of login=True or login=False.
    assert execution_policy.shell_read({"command": "Get-Location", "shell": "powershell", "login": True}) is True
    assert execution_policy.shell_read({"command": "Get-Location", "shell": "powershell", "login": False}) is True
    assert execution_policy.shell_read({"command": "git status --short", "shell": "powershell", "login": True}) is True

    which_paths: list[tuple[str, str | None]] = []

    def spy_which(name: str, path: str | None = None):
        which_paths.append((name, path))
        if name in {"pwsh", "powershell"}:
            return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if name in {"git", "git.exe"} and path and "MergedRegistryGit" in path:
            return r"C:\MergedRegistryGit\cmd\git.exe"
        return None

    monkeypatch.setattr(local_processes, "child_environment", lambda: {"PATH": r"C:\Ryn\bin;C:\MergedRegistryGit\cmd"})
    monkeypatch.setattr(local_processes.shutil, "which", spy_which)
    monkeypatch.setattr(local_processes, "detect_shell", lambda kind, path=None: {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "version": "5.1"})

    ps_read_argv = local_processes.prepare_shell_command(
        {"command": "Get-Location", "shell": "powershell", "login": True}
    )
    assert "-NoProfile" in ps_read_argv

    which_paths.clear()
    git_read_argv = local_processes.literal_read_command(
        {"command": "git status --short", "shell": "powershell", "login": True}
    )
    assert git_read_argv is not None
    assert git_read_argv[0] == r"C:\MergedRegistryGit\cmd\git.exe"
    assert any(name == "git" and path == r"C:\Ryn\bin;C:\MergedRegistryGit\cmd" for name, path in which_paths)


def test_t7_powershell_profile_pssecurity_exception_falls_back_to_noprofile_with_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(local_processes, "_POWERSHELL_PROFILE_DISABLED", False, raising=False)
    processes = local_processes.Processes()
    try:
        sim_cmd = local_processes.PreparedCommand(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stderr.write("
                    "'. : File C:\\\\Docs\\\\WindowsPowerShell\\\\Microsoft.PowerShell_profile.ps1 cannot be loaded.\\n"
                    "    + CategoryInfo          : SecurityError: (:) [], PSSecurityException\\n'"
                    "); print('BODY_OK')"
                ),
            ],
            powershell_login_profile=True,
        )
        ident = processes.start(sim_cmd, tmp_path, "task", 10, lambda: True, lambda: False)
        result = processes.wait(ident, "task", lambda: False)

        assert result["exit_code"] == 0
        assert "BODY_OK" in result["output"]
        assert "profile_notice" in result
        assert "-NoProfile" in result["profile_notice"]

        monkeypatch.setattr(local_processes.os, "name", "nt", raising=False)
        monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: r"C:\Program Files\PowerShell\7\pwsh.exe")
        monkeypatch.setattr(local_processes, "detect_shell", lambda kind, path=None: {
            "path": r"C:\Program Files\PowerShell\7\pwsh.exe", "version": "7.4"})
        next_cmd = local_processes.prepare_shell_command(
            {"command": "Write-Output 'after-fallback'", "shell": "powershell", "login": True}
        )
        assert "-NoProfile" in next_cmd
    finally:
        monkeypatch.setattr(local_processes, "_POWERSHELL_PROFILE_DISABLED", False, raising=False)
        processes.close()


def test_t8_rework_hung_warmup_caps_total_command_prep_wait_under_2_5s(monkeypatch):
    from rynmesh.agent_runtime import login_environment

    login_environment.reset_cache()
    monkeypatch.setattr(login_environment.os, "name", "posix", raising=False)
    monkeypatch.setattr(local_processes.os, "name", "posix", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(local_processes, "_bundled_rg_executable", lambda: None)
    monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: "/usr/bin/rg" if name == "rg" else "/bin/sh")
    monkeypatch.setattr(login_environment, "default_posix_shell", lambda env=None: "/bin/zsh")

    release_warmup = threading.Event()
    warmup_entered = threading.Event()

    def hung_run(cmd, **kwargs):
        warmup_entered.set()
        release_warmup.wait(8.0)
        return SimpleNamespace(returncode=0, stdout=b"PATH=/late/homebrew/bin:/usr/bin\x00", stderr=b"")

    monkeypatch.setattr(login_environment.subprocess, "run", hung_run)
    # Keep default WARMUP_WAIT_SECONDS = 2.0 to test real wall-clock ceiling (< 2.5s).
    assert login_environment.WARMUP_WAIT_SECONDS == 2.0

    try:
        t0 = time.monotonic()
        argv = local_processes.prepare_shell_command({"command": "rg -n needle src", "shell": "sh"})
        follow_env = local_processes.child_environment()
        elapsed = time.monotonic() - t0

        assert warmup_entered.is_set()
        assert argv[0] == "/usr/bin/rg"
        assert follow_env["PATH"] == "/usr/bin:/bin"
        assert elapsed < 2.5, f"expected <= 2.5s total wait across command prep + env, got {elapsed:.2f}s"

        # Once background warmup eventually finishes, subsequent calls pick up cached login env.
        release_warmup.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if "/late/homebrew/bin" in local_processes.child_environment()["PATH"]:
                break
            time.sleep(0.02)
        assert "/late/homebrew/bin" in local_processes.child_environment()["PATH"]
    finally:
        release_warmup.set()
        login_environment.reset_cache()


def test_t9_rework_user_command_pssecurity_exception_does_not_disable_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(local_processes, "_POWERSHELL_PROFILE_DISABLED", False, raising=False)
    processes = local_processes.Processes()
    try:
        # 1. User script blocked by ExecutionPolicy on line 1 (not a *profile.ps1 file) must NOT disable profile.
        user_script_cmd = local_processes.PreparedCommand(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stderr.write("
                    "'.\\\\deploy.ps1 : File C:\\\\repo\\\\deploy.ps1 cannot be loaded because running scripts is disabled.\\n"
                    "    + CategoryInfo          : SecurityError: (:) [], PSSecurityException\\n'"
                    "); print('USER_SCRIPT_DONE')"
                ),
            ],
            powershell_login_profile=True,
        )
        ident1 = processes.start(user_script_cmd, tmp_path, "task", 10, lambda: True, lambda: False)
        res1 = processes.wait(ident1, "task", lambda: False)
        assert "USER_SCRIPT_DONE" in res1["output"]
        assert "profile_notice" not in res1
        assert local_processes._POWERSHELL_PROFILE_DISABLED is False

        # 2. Late output mentioning profile.ps1 + PSSecurityException after 25 lines of user output must NOT disable profile.
        late_output_cmd = local_processes.PreparedCommand(
            [
                sys.executable,
                "-c",
                (
                    "for i in range(25): print(f'build step {i}')\n"
                    "print('File Microsoft.PowerShell_profile.ps1 PSSecurityException in test log')"
                ),
            ],
            powershell_login_profile=True,
        )
        ident2 = processes.start(late_output_cmd, tmp_path, "task", 10, lambda: True, lambda: False)
        res2 = processes.wait(ident2, "task", lambda: False)
        assert "profile_notice" not in res2
        assert local_processes._POWERSHELL_PROFILE_DISABLED is False

        monkeypatch.setattr(local_processes.os, "name", "nt", raising=False)
        monkeypatch.setattr(local_processes.shutil, "which", lambda name, path=None: r"C:\Program Files\PowerShell\7\pwsh.exe")
        still_login = local_processes.prepare_shell_command(
            {"command": "Write-Output 'still-with-profile'", "shell": "powershell", "login": True}
        )
        assert "-NoProfile" not in still_login

        # 3. Genuine startup profile load failure DOES disable profile and emit profile_notice.
        real_profile_err = local_processes.PreparedCommand(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stderr.write("
                    "'. : File C:\\\\Docs\\\\PowerShell\\\\profile.ps1 cannot be loaded.\\n"
                    "    + CategoryInfo          : SecurityError: (:) [], PSSecurityException\\n'"
                    "); print('AFTER_PROFILE_ERR')"
                ),
            ],
            powershell_login_profile=True,
        )
        ident3 = processes.start(real_profile_err, tmp_path, "task", 10, lambda: True, lambda: False)
        res3 = processes.wait(ident3, "task", lambda: False)
        assert "profile_notice" in res3
        assert local_processes._POWERSHELL_PROFILE_DISABLED is True
        after_fallback = local_processes.prepare_shell_command(
            {"command": "Write-Output 'now-noprofile'", "shell": "powershell", "login": True}
        )
        assert "-NoProfile" in after_fallback
    finally:
        monkeypatch.setattr(local_processes, "_POWERSHELL_PROFILE_DISABLED", False, raising=False)
        processes.close()
