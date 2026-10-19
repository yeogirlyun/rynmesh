"""Command startup boundaries and process polling without model busy loops."""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime import local_processes
from rynmesh.agent_runtime.local_processes import (
    AgentProcessStartRejected,
    Processes,
    prepare_shell_command,
)
from rynmesh.agent_runtime.store import AgentToolRejected


@pytest.fixture
def pending_process():
    processes = Processes()
    state = SimpleNamespace(exit_code=None)
    item = {"process": SimpleNamespace(poll=lambda: state.exit_code), "task_id": "task",
            "output": bytearray(), "truncated": False, "reason": None,
            "reader_done": threading.Event()}
    processes.items["process_test"] = item
    yield processes, state, item
    state.exit_code = 0
    item["reader_done"].set()


def test_missing_ripgrep_is_recoverable_before_any_process_starts(monkeypatch):
    monkeypatch.setattr(local_processes.shutil, "which", lambda name: None if name.lower() in {"rg", "rg.exe"} else "/available/shell")
    spawned = []
    monkeypatch.setattr(local_processes.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    with pytest.raises(AgentToolRejected, match="agent_ripgrep_unavailable_use_file_search"):
        prepare_shell_command({"command": "rg -n 'balance|query_balance' src", "shell": "powershell"})
    assert spawned == []


def test_ripgrep_preparation_preserves_arguments_without_a_shell(monkeypatch):
    monkeypatch.setattr(local_processes.shutil, "which", lambda name: "/available/rg")
    assert prepare_shell_command({"command": "rg -n -C 4 'balance|query_balance' 'src with spaces'", "shell": "powershell"}) == [
        "/available/rg", "--no-config", "-n", "-C", "4", "balance|query_balance", "src with spaces"]


def test_spawn_failure_is_safe_rejection_and_does_not_create_a_process(monkeypatch, tmp_path):
    processes = Processes()
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise FileNotFoundError("executable disappeared after preflight")

    monkeypatch.setattr(local_processes.subprocess, "Popen", unavailable)
    with pytest.raises(AgentProcessStartRejected, match="agent_process_start_failed"):
        processes.start(["missing"], tmp_path, "task", 30, lambda: True, lambda: False)
    assert len(calls) == 1 and processes.items == {}


def test_cancellation_before_spawn_has_a_safe_rejection_type(monkeypatch, tmp_path):
    processes = Processes()
    calls = []
    monkeypatch.setattr(local_processes.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))

    def cancelled():
        assert not processes.lock._is_owned()
        return True

    with pytest.raises(AgentProcessStartRejected, match="agent_cancelled"):
        processes.start(["unused"], tmp_path, "task", 30, lambda: True, cancelled)
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows post-spawn job setup")
def test_post_spawn_failure_is_not_classified_as_safe_to_retry(monkeypatch, tmp_path):
    processes = Processes()
    killed = []
    child = SimpleNamespace(kill=lambda: killed.append(True), wait=lambda **kwargs: 0)
    monkeypatch.setattr(local_processes.subprocess, "Popen", lambda *args, **kwargs: child)

    def setup_failed(*args, **kwargs):
        raise RuntimeError("job setup failed after creating the child")

    monkeypatch.setattr(local_processes, "_windows_job", setup_failed)
    with pytest.raises(RuntimeError, match="job setup failed") as raised:
        processes.start(["created-child"], tmp_path, "task", 30, lambda: True, lambda: False)
    assert not isinstance(raised.value, AgentProcessStartRejected)
    assert killed == [True]


def test_read_waits_for_new_output_and_releases_the_process_lock(pending_process):
    processes, state, item = pending_process
    waiting = threading.Event()
    results = []

    def poll():
        waiting.set()
        results.append(processes.read("process_test", "task", wait_seconds=2))

    reader = threading.Thread(target=poll)
    reader.start()
    assert waiting.wait(1)
    # An unrelated operation can inspect the table while this read is waiting.
    with processes.changed:
        item["output"].extend(b"new output")
        processes.changed.notify_all()
    reader.join(1)
    assert not reader.is_alive()
    assert results[0]["output"] == "new output" and results[0]["running"]


def test_read_returns_immediately_for_unread_output_or_zero_wait(pending_process):
    processes, state, item = pending_process
    item["output"].extend(b"ready")
    started = time.monotonic()
    assert processes.read("process_test", "task", wait_seconds=20)["output"] == "ready"
    assert processes.read("process_test", "task", 5, wait_seconds=0)["output"] == ""
    assert time.monotonic() - started < 0.5


def test_read_waits_at_the_consumed_offset_and_wakes_when_complete(pending_process):
    processes, state, item = pending_process
    item["output"].extend(b"old")
    results = []
    reader = threading.Thread(target=lambda: results.append(processes.read("process_test", "task", 3, wait_seconds=2)))
    reader.start()
    with processes.changed:
        state.exit_code = 7
        item["reader_done"].set()
        processes.changed.notify_all()
    reader.join(1)
    assert not reader.is_alive()
    assert results[0]["output"] == "" and results[0]["exit_code"] == 7 and not results[0]["running"]


def test_read_wait_is_bounded_and_can_be_cancelled_without_claiming_completion(pending_process):
    processes, state, item = pending_process
    started = time.monotonic()
    result = processes.read("process_test", "task", wait_seconds=0.05)
    assert 0.03 <= time.monotonic() - started < 0.5
    assert result["running"] and result["exit_code"] is None
    started = time.monotonic()

    def cancelled():
        assert not processes.lock._is_owned()
        return time.monotonic() - started >= 0.05

    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        processes.read("process_test", "task", wait_seconds=20, cancelled=cancelled)
    assert time.monotonic() - started < 0.5
    assert state.exit_code is None


def test_running_process_read_receives_output_then_completion(tmp_path):
    processes = Processes()
    script = "import time; time.sleep(.1); print('ready', flush=True); time.sleep(.2); print('done', flush=True)"
    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 10, lambda: True, lambda: False)
    try:
        first = processes.read(ident, "task", wait_seconds=2)
        output = first["output"]
        current = first
        while current["running"]:
            current = processes.read(ident, "task", current["output_length"], wait_seconds=2)
            output += current["output"]
        assert "ready" in output and "done" in output and current["exit_code"] == 0
    finally:
        processes.close()


@pytest.mark.parametrize("wait_seconds", [-1, 21, float("inf"), float("nan"), True, "1"])
def test_read_rejects_unbounded_or_invalid_waits(pending_process, wait_seconds):
    processes, _, _ = pending_process
    with pytest.raises(AgentToolRejected, match="agent_invalid_process_wait"):
        processes.read("process_test", "task", wait_seconds=wait_seconds)


def test_exited_process_populates_stop_reason_and_coalesces_repeated_empty_polls(pending_process):
    processes, state, item = pending_process
    item["coalesce_wait_seconds"] = 0.06

    # First zero-wait poll at offset 0 returns immediately and records empty_poll_count=1.
    t0 = time.monotonic()
    first = processes.read("process_test", "task", 0, wait_seconds=0, coalesce_empty_poll=True)
    assert time.monotonic() - t0 < 0.04
    assert first["running"] is True
    assert first["output"] == ""
    assert first["empty_poll_count"] == 1
    assert "poll_notice" not in first

    # Second zero-wait poll at the same offset coalesces wait and surfaces poll_notice.
    t1 = time.monotonic()
    second = processes.read("process_test", "task", 0, wait_seconds=0, coalesce_empty_poll=True)
    assert time.monotonic() - t1 >= 0.04
    assert second["running"] is True
    assert second["empty_poll_count"] == 2
    assert "No new output at this offset yet" in second["poll_notice"]

    # Process exits normally with code 0 -> stop_reason is "exited" (never None) and empty_poll_count resets.
    state.exit_code = 0
    item["reader_done"].set()
    finished = processes.read("process_test", "task", 0, wait_seconds=0, coalesce_empty_poll=True)
    assert finished["running"] is False
    assert finished["exit_code"] == 0
    assert finished["stop_reason"] == "exited"
    assert finished["empty_poll_count"] == 0

    # Non-zero exit without explicit reason -> stop_reason is "exited_nonzero".
    state.exit_code = 3
    failed = processes.read("process_test", "task", 0, wait_seconds=0, coalesce_empty_poll=True)
    assert failed["stop_reason"] == "exited_nonzero"


def test_shell_execute_surfaces_active_background_process_hint_to_prevent_overlapping_scans(tmp_path):
    from test_agent_local_tools import execute
    from test_agent_local_tools import local as local_fixture

    gen = local_fixture.__wrapped__(tmp_path)
    tools, context, _, _ = next(gen)
    sleep_cmd = "Start-Sleep 5" if os.name == "nt" else "sleep 5"
    fast_cmd = "Get-Location" if os.name == "nt" else "pwd"
    try:
        bg = execute(tools, context, "ryn_shell_execute", {"command": sleep_cmd, "background": True}, "op_bg_1")
        assert bg["running"] is True
        bg_pid = bg["process_id"]

        second = execute(tools, context, "ryn_shell_execute", {"command": fast_cmd}, "op_fg_2")
        assert bg_pid in second.get("active_background_processes", [])
        assert "Other background process(es) are still running" in second.get("background_process_notice", "")
    finally:
        tools.processes.close()
        try:
            next(gen)
        except StopIteration:
            pass


def test_compound_shell_command_with_unavailable_rg_is_rejected_before_spawn(tmp_path, monkeypatch):
    from test_agent_local_tools import local as local_fixture

    from rynmesh.agent_runtime import local_processes, local_tools

    monkeypatch.setattr(local_processes, "resolve_rg_executable", lambda: None)
    monkeypatch.setattr(local_tools, "resolve_rg_executable", lambda: None)

    for cmd in (
        "rg --files",
        "git status --short; rg --files",
        "rg --files -g 'AGENTS.md' -g 'CLAUDE.md'; git diff --stat",
        "Get-ChildItem | rg pattern",
    ):
        with pytest.raises(AgentToolRejected, match="agent_ripgrep_unavailable_use_file_search"):
            local_processes.prepare_shell_command({"command": cmd})

    # Quoted literals mentioning rg are not treated as ripgrep invocations.
    safe_cmd = local_processes.prepare_shell_command({"command": 'python -c "print(\'rg --files\')"'})
    assert isinstance(safe_cmd, list) and safe_cmd

    gen = local_fixture.__wrapped__(tmp_path)
    tools, context, _, _ = next(gen)
    try:
        env = tools.perform("ryn_local_environment", {}, context, lambda: False)
        assert env["rg_available"] is False
    finally:
        tools.processes.close()
        try:
            next(gen)
        except StopIteration:
            pass
