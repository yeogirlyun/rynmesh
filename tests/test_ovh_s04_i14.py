"""Tests for S04-I14: fixed child environment variables (pagers, NO_COLOR, non-TTY TERM=dumb, RYN_TASK_ID, RYN_VERSION)."""

from __future__ import annotations

import json
import os
import sys
import threading

import rynmesh
from rynmesh.agent_runtime import local_processes
from rynmesh.agent_runtime.local_processes import Processes, child_environment


def _run_python_and_read_env(processes: Processes, tmp_path, task_id: str) -> dict[str, str]:
    out_file = tmp_path / f"env_{task_id}.json"
    script = "import json, os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)), encoding='utf-8')"
    ident = processes.start(
        [sys.executable, "-c", script, str(out_file)],
        tmp_path,
        task_id,
        15,
        lambda: True,
        lambda: False,
    )
    state = processes.wait(ident, task_id, lambda: False)
    assert state["exit_code"] == 0
    return json.loads(out_file.read_text(encoding="utf-8"))


def test_t1_child_process_env_contains_fixed_variables_and_overrides_user_pager(tmp_path, monkeypatch):
    monkeypatch.setenv("PAGER", "less -R")
    monkeypatch.setenv("GIT_PAGER", "delta")
    monkeypatch.setenv("GH_PAGER", "more")
    monkeypatch.delenv("NO_COLOR", raising=False)

    processes = Processes()
    try:
        spawned_env = _run_python_and_read_env(processes, tmp_path, "task_s04_i14_t1")
    finally:
        processes.close()

    norm = {k.upper(): v for k, v in spawned_env.items()}
    assert norm.get("PAGER") == "cat"
    assert norm.get("GIT_PAGER") == "cat"
    assert norm.get("GH_PAGER") == "cat"
    assert norm.get("NO_COLOR") == "1"
    assert norm.get("TERM") == "dumb"
    assert norm.get("RYN_TASK_ID") == "task_s04_i14_t1"
    assert norm.get("RYN_VERSION") == rynmesh.__version__

    # Step 3: permission mode is intentionally NOT injected into child environment.
    for forbidden in ("RYN_PERMISSION_MODE", "RYNMESH_PERMISSION_MODE", "PERMISSION_MODE"):
        assert forbidden not in norm


def test_t2_tty_mode_does_not_set_term_dumb(tmp_path, monkeypatch):
    # Even if parent environment had TERM=dumb (e.g. nested inside non-TTY runner), tty=True must not carry TERM=dumb.
    monkeypatch.setenv("TERM", "dumb")
    tty_env = child_environment(task_id="task_tty_1", tty=True)
    assert tty_env.get("TERM") != "dumb"
    assert tty_env["PAGER"] == "cat"
    assert tty_env["GIT_PAGER"] == "cat"
    assert tty_env["GH_PAGER"] == "cat"
    assert tty_env["NO_COLOR"] == "1"
    assert tty_env["RYN_TASK_ID"] == "task_tty_1"
    assert tty_env["RYN_VERSION"] == rynmesh.__version__

    # Real terminal type (e.g. xterm-256color) is preserved when tty=True, overridden to dumb when tty=False.
    monkeypatch.setenv("TERM", "xterm-256color")
    assert child_environment(task_id="task_tty_2", tty=True).get("TERM") == "xterm-256color"
    assert child_environment(task_id="task_nontty_2", tty=False).get("TERM") == "dumb"

    # Verify `Processes.start(..., tty=True)` passes `tty=True` and `task_id` to `child_environment`.
    captured_pty_envs: list[dict[str, str]] = []

    class _FakePty:
        backend = "conpty"

        def __init__(self, args, cwd, env, rows=24, cols=80):
            captured_pty_envs.append(dict(env))
            self.rows = rows
            self.cols = cols
            self.error = None
            self._closed = threading.Event()
            self._done = False

        def read_output(self) -> bytes:
            self._done = True
            self._closed.set()
            return b""

        def poll(self):
            return 0 if self._done else None

        def release_buffers(self) -> None:
            self._done = True

        def dispose(self) -> None:
            self._done = True
            self._closed.set()

        def terminate(self) -> None:
            self.dispose()

        def kill(self) -> None:
            self.dispose()

        def wait(self, timeout=None) -> int:
            self.dispose()
            return 0

    monkeypatch.setattr(local_processes, "check_pty_support", lambda: True)
    monkeypatch.setattr(local_processes, "ConPtyProcess", _FakePty)
    monkeypatch.setattr(local_processes, "terminate", lambda proc: proc.dispose())

    processes = Processes()
    try:
        ident = processes.start(["cmd-tty"], tmp_path, "task_pty_spawn", 10, lambda: True, lambda: False, tty=True)
        processes.wait(ident, "task_pty_spawn", lambda: False)
    finally:
        processes.close()

    assert len(captured_pty_envs) == 1
    assert captured_pty_envs[0].get("TERM") != "dumb"
    assert captured_pty_envs[0].get("RYN_TASK_ID") == "task_pty_spawn"
    assert captured_pty_envs[0].get("RYN_VERSION") == rynmesh.__version__


def test_t3_rynmesh_prefixed_variables_remain_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "secret-local-token")
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "secret-network-key")
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "private-home"))
    monkeypatch.setenv("rynmesh_mixed_case", "secret-mixed")

    processes = Processes()
    try:
        spawned_env = _run_python_and_read_env(processes, tmp_path, "task_s04_i14_t3")
    finally:
        processes.close()

    assert not [k for k in spawned_env if k.upper().startswith("RYNMESH_")]
    norm = {k.upper(): v for k, v in spawned_env.items()}
    assert norm["RYN_TASK_ID"] == "task_s04_i14_t3"
    assert norm["RYN_VERSION"] == rynmesh.__version__


def test_t4_ryn_task_id_equals_each_task_id_and_does_not_leak_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("RYN_TASK_ID", "stale_outer_task")
    untasked = child_environment()
    assert "RYN_TASK_ID" not in {k.upper() for k in untasked}

    processes = Processes()
    try:
        env_a = _run_python_and_read_env(processes, tmp_path, "task_alpha_101")
        env_b = _run_python_and_read_env(processes, tmp_path, "task_beta_202")
    finally:
        processes.close()

    assert {k.upper(): v for k, v in env_a.items()}["RYN_TASK_ID"] == "task_alpha_101"
    assert {k.upper(): v for k, v in env_b.items()}["RYN_TASK_ID"] == "task_beta_202"
