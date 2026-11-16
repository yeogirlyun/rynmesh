"""Overhaul S04-I3: background process lifecycle, default 10s yield, per-task capacity, pause survival, and restart notes."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_local_tools import execute
from test_agent_local_tools import local as local_fixture
from test_agent_runtime import Catalog, Model, Tools

from rynmesh.agent_runtime import execution_policy, local_processes, local_tools
from rynmesh.agent_runtime.local_processes import AgentProcessStartRejected, Processes
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.request_context import normalize_history
from rynmesh.agent_runtime.runtime import AgentRuntime, public_task
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected


@pytest.fixture
def local(tmp_path):
    yield from local_fixture.__wrapped__(tmp_path)


def _python_sleep_cmd(seconds: float, *, banner: str = "READY") -> str:
    py = sys.executable.replace("'", "''") if os.name == "nt" else sys.executable
    code = f"import time; print('{banner}', flush=True); time.sleep({seconds}); print('DONE', flush=True)"
    if os.name == "nt":
        return f"& '{py}' -u -c \"{code}\""
    return f'"{py}" -u -c "{code}"'


def _make_runtime_with_local_processes(store_dir):
    key = X25519PrivateKey.generate()
    store = AgentStore(store_dir, key)
    tools = Tools()
    proc_mgr = Processes()
    tools.local = SimpleNamespace(processes=proc_mgr)
    tools.processes = proc_mgr
    runtime = AgentRuntime(store, Model(), tools, Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    return runtime, store, tools, key


def test_t1_unparameterized_30s_command_yields_after_10s_with_running_process_id(local):
    """T1: 未给参数的 30 秒命令 10 秒后返回 process_id 且仍在跑（并核对描述同步，覆盖改动 1、2、5）。"""
    tools, context, _, _ = local
    desc = SPECS["ryn_shell_execute"][0]
    assert "10 seconds" in desc and "process_id" in desc
    assert "ryn_process_stop" in desc
    assert "no automatic lifetime" in desc.lower() or "until" in desc.lower()

    cmd = _python_sleep_cmd(30, banner="T1_STARTED")
    t0 = time.monotonic()
    try:
        result = execute(tools, context, "ryn_shell_execute", {"command": cmd}, "op_t1_30s")
        elapsed = time.monotonic() - t0
        assert 9.5 <= elapsed < 16.0, f"expected ~10s yield, got {elapsed:.2f}s"
        assert isinstance(result.get("process_id"), str) and result["process_id"].startswith("process_")
        assert result["running"] is True
        assert result["exit_code"] is None
        stopped = execute(
            tools,
            context,
            "ryn_process_stop",
            {"process_id": result["process_id"]},
            "op_t1_stop",
        )
        assert stopped["running"] is False
    finally:
        tools.processes.close()


def test_t2_background_process_survives_past_120_seconds_without_auto_kill(local, monkeypatch):
    """T2: background=true 进程超过 120 秒不被杀（覆盖改动 2）。"""
    tools, context, _, _ = local
    real_mono = time.monotonic
    offset_lock = threading.Lock()
    state = {"offset": 0.0}

    def fake_monotonic():
        with offset_lock:
            return real_mono() + state["offset"]

    monkeypatch.setattr(local_processes.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(local_tools.time, "monotonic", fake_monotonic)

    cmd = _python_sleep_cmd(30, banner="BG_ALIVE")
    try:
        started = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd, "background": True},
            "op_t2_bg",
        )
        pid = started["process_id"]
        assert started["running"] is True

        deadline = real_mono() + 5.0
        while real_mono() < deadline:
            snap = tools.perform("ryn_process_read", {"process_id": pid, "wait_seconds": 0}, context, lambda: False)
            if "BG_ALIVE" in snap["output"]:
                break
            time.sleep(0.05)
        assert "BG_ALIVE" in snap["output"]

        # Advance virtual monotonic clock past both old 120s default and 600s schema max.
        with offset_lock:
            state["offset"] = 180.0
        time.sleep(0.25)

        after_180 = tools.perform("ryn_process_read", {"process_id": pid, "wait_seconds": 0}, context, lambda: False)
        assert after_180["running"] is True, f"background process was killed past 120s: {after_180}"
        assert after_180["stop_reason"] is None
    finally:
        tools.processes.close()


def test_t3_explicit_timeout_seconds_still_terminates_process(local):
    """T3: 显式 timeout_seconds 仍杀（对比无 timeout_seconds 存活，覆盖改动 2）。"""
    tools, context, _, _ = local
    cmd_timed = _python_sleep_cmd(30, banner="TIMED")
    cmd_untimed = _python_sleep_cmd(30, banner="UNTIMED")
    try:
        timed = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd_timed, "background": True, "timeout_seconds": 1},
            "op_t3_timed",
        )
        untimed = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd_untimed, "background": True},
            "op_t3_untimed",
        )
        assert tools.processes.get(timed["process_id"], context["task_id"])["timeout"] == 1
        assert tools.processes.get(untimed["process_id"], context["task_id"])["timeout"] is None

        deadline = time.monotonic() + 4.0
        snap = timed
        while time.monotonic() < deadline:
            snap = tools.perform(
                "ryn_process_read",
                {"process_id": timed["process_id"], "wait_seconds": 0},
                context,
                lambda: False,
            )
            if not snap["running"]:
                break
            time.sleep(0.05)
        assert snap["running"] is False
        assert snap["stop_reason"] == "timeout"

        untimed_snap = tools.perform(
            "ryn_process_read",
            {"process_id": untimed["process_id"], "wait_seconds": 0},
            context,
            lambda: False,
        )
        assert untimed_snap["running"] is True
    finally:
        tools.processes.close()


def test_t4_literal_readonly_command_still_blocks_without_default_yield(local, monkeypatch):
    """T4: 字面只读命令仍阻塞且默认超时保持 120 秒（覆盖改动 1、2）。"""
    tools, context, workspace, _ = local
    sample = workspace / "readme_t4.txt"
    sample.write_text("readonly-content-check\n", encoding="utf-8")

    observed_starts: list[object] = []
    observed_waits: list[object] = []
    real_start = tools.processes.start
    real_wait = tools.processes.wait

    def spy_start(args, cwd, task_id, timeout=None, *rest, **kwargs):
        observed_starts.append(timeout)
        return real_start(args, cwd, task_id, timeout, *rest, **kwargs)

    def spy_wait(ident, task_id, cancelled, *, yield_seconds=None):
        observed_waits.append(yield_seconds)
        return real_wait(ident, task_id, cancelled, yield_seconds=yield_seconds)

    monkeypatch.setattr(tools.processes, "start", spy_start)
    monkeypatch.setattr(tools.processes, "wait", spy_wait)

    read_cmd = "Get-Content readme_t4.txt" if os.name == "nt" else "cat readme_t4.txt"
    assert execution_policy.shell_read({"command": read_cmd}) is True
    try:
        res = execute(tools, context, "ryn_shell_execute", {"command": read_cmd}, "op_t4_read")
        assert res["running"] is False
        assert res["exit_code"] == 0
        assert "readonly-content-check" in res["output"]
        assert observed_starts == [120]
        assert observed_waits == [None]
    finally:
        tools.processes.close()


def test_t5_per_task_16_limit_allows_other_task_and_evicts_oldest_completed_unread(tmp_path):
    """T5: 任务 A 占满 16 个时任务 B 还能起；满时先淘汰最早结束、最久没读的已完成项；仍满拒绝并列 running ids（覆盖改动 3）。"""
    processes = Processes()
    sleep_argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    fast_argv = [sys.executable, "-c", "print('done')"]
    try:
        # First verify completed-item eviction order: earliest ended, least recently read first.
        c1 = processes.start(fast_argv, tmp_path, "task_evict", 10, lambda: True, lambda: False)
        processes.wait(c1, "task_evict", lambda: False)
        time.sleep(0.02)
        c2 = processes.start(fast_argv, tmp_path, "task_evict", 10, lambda: True, lambda: False)
        processes.wait(c2, "task_evict", lambda: False)
        # Force same ended_at on c1/c2 to test secondary tie-break by last_read_at, or read c1 later:
        processes.items[c1]["ended_at"] = 100.0
        processes.items[c1]["last_read_at"] = 50.0
        processes.items[c2]["ended_at"] = 100.0
        processes.items[c2]["last_read_at"] = 10.0  # c2 was read less recently than c1
        extra_completed = []
        for idx in range(14):
            cid = processes.start(fast_argv, tmp_path, "task_evict", 10, lambda: True, lambda: False)
            processes.wait(cid, "task_evict", lambda: False)
            processes.items[cid]["ended_at"] = 200.0 + idx
            processes.items[cid]["last_read_at"] = 200.0 + idx
            extra_completed.append(cid)
        assert sum(v["task_id"] == "task_evict" for v in processes.items.values()) == 16
        # Starting 17th item on task_evict should evict c2 (earliest ended_at=100, older last_read_at=10)
        c17 = processes.start(fast_argv, tmp_path, "task_evict", 10, lambda: True, lambda: False)
        processes.wait(c17, "task_evict", lambda: False)
        assert c2 not in processes.items
        assert c1 in processes.items

        # Now start 16 running processes for task_a.
        task_a_ids = [
            processes.start(sleep_argv, tmp_path, "task_a", None, lambda: True, lambda: False)
            for _ in range(16)
        ]
        assert len(task_a_ids) == 16

        # 17th running process for task_a must be rejected with running_process_ids & correction.
        with pytest.raises(AgentProcessStartRejected) as exc_info:
            processes.start(sleep_argv, tmp_path, "task_a", None, lambda: True, lambda: False)
        exc = exc_info.value
        assert str(exc) == "agent_process_capacity"
        assert isinstance(exc.details, dict)
        assert set(exc.details.get("running_process_ids") or []) == set(task_a_ids)
        assert exc.correction and "ryn_process_stop" in exc.correction

        # Meanwhile task_b can still start processes independently!
        task_b_id = processes.start(sleep_argv, tmp_path, "task_b", None, lambda: True, lambda: False)
        assert processes.read(task_b_id, "task_b", wait_seconds=0)["running"] is True
    finally:
        processes.close()


def test_t6_paused_task_keeps_background_process_alive_while_cancelled_kills_it(local):
    """T6: 暂停任务后后台进程存活（浏览器 allowed 仍为 False）、取消后被杀（覆盖改动 4）。"""
    tools, context, _, store = local
    cmd = _python_sleep_cmd(30, banner="PAUSE_SURVIVES")
    try:
        started = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd, "background": True},
            "op_t6_bg",
        )
        pid = started["process_id"]
        assert started["running"] is True

        # Transition task to paused in store.
        store.transaction(lambda data: data["tasks"]["task"].update(status="paused"))
        # Browser allowed() still treats paused as stopped:
        assert tools.allowed(context)() is False
        assert tools.process_allowed(context)() is True

        # Wait > 1.1s so the 1 Hz process_allowed cache refreshes against paused state.
        time.sleep(1.2)
        paused_snap = tools.perform("ryn_process_read", {"process_id": pid, "wait_seconds": 0}, context, lambda: False)
        assert paused_snap["running"] is True, f"paused task unexpectedly killed process: {paused_snap}"

        # Now cancel the task -> process must be terminated within ~1.5s.
        store.transaction(lambda data: data["tasks"]["task"].update(status="cancelled"))
        deadline = time.monotonic() + 3.0
        final_snap = paused_snap
        while time.monotonic() < deadline:
            final_snap = tools.perform("ryn_process_read", {"process_id": pid, "wait_seconds": 0}, context, lambda: False)
            if not final_snap["running"]:
                break
            time.sleep(0.05)
        assert final_snap["running"] is False
        assert final_snap["stop_reason"] == "permission_or_task_stopped"
    finally:
        tools.processes.close()


def test_t7_restart_recovery_still_marks_default_yield_write_shell_as_unknown_outcome(tmp_path):
    """T7: 重启恢复对默认转后台的写命令仍判'写结果未知'（覆盖改动 1、2）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, store, _, _ = _make_runtime_with_local_processes(tmp_path / "agent")
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": "workspace"})
    created = runtime.create({"message": "Run build script"})
    task_id = created["id"]

    write_args = {"command": "python -c \"open('out.txt', 'w').write('ok')\""}
    assert execution_policy.shell_read(write_args) is False
    classification = execution_policy.classify_operation(
        "ryn_shell_execute",
        write_args,
        {"effect": "write"},
        operation_id="op_write_default_yield",
        task_id=task_id,
        workspace_root=str(workspace),
    )
    assert classification["effect_class"] == "write"

    def _seed_in_flight(data):
        row = data["tasks"][task_id]
        row["permission_mode"] = "workspace"
        row["status"] = "running"
        row["in_flight"] = {
            "call_id": "call_write_1",
            "operation_id": "op_write_default_yield",
            "name": "ryn_shell_execute",
            "arguments": write_args,
            "effect": "write",
            "authorization": "workspace",
            "dispatched": True,
            "operation_classification": classification,
            "context_epoch": row.get("context_epoch", 0),
        }

    store.transaction(_seed_in_flight)
    runtime.reconcile()

    recovered = store.read()["tasks"][task_id]
    assert recovered["status"] == "interrupted"
    assert recovered["error"] == "agent_write_outcome_unknown"
    # Verify this task was NOT auto-continued as a read operation:
    interrupted_events = [e for e in recovered["events"] if e.get("type") == "interrupted"]
    assert interrupted_events and interrupted_events[-1].get("ambiguous_write") is True


def test_t8_single_transient_store_inspect_error_does_not_kill_running_process(local, monkeypatch):
    """T8: 打桩 store.inspect 先抛一次异常再正常，进程不被杀（覆盖改动 6）。"""
    tools, context, _, store = local
    real_inspect = store.inspect
    calls = {"count": 0}

    def flaky_inspect(fn):
        calls["count"] += 1
        if calls["count"] in {1, 2}:
            raise RuntimeError("transient sqlite/decrypt busy")
        return real_inspect(fn)

    cmd = _python_sleep_cmd(30, banner="FLAKY_OK")
    try:
        started = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd, "background": True},
            "op_t8_flaky",
        )
        pid = started["process_id"]
        monkeypatch.setattr(store, "inspect", flaky_inspect)

        # Wait > 2.2s so watch triggers multiple 1 Hz authority checks (first 2 fail, 3rd succeeds).
        time.sleep(2.3)
        assert calls["count"] >= 2
        snap = tools.perform("ryn_process_read", {"process_id": pid, "wait_seconds": 0}, context, lambda: False)
        assert snap["running"] is True, f"process killed on transient inspect error: {snap}"
    finally:
        tools.processes.close()


def test_t9_consecutive_store_inspect_errors_stop_process_only_at_threshold(local, monkeypatch):
    """T9: 连续异常超过阈值（20 次）才停（覆盖改动 6）。"""
    tools, context, _, store = local
    real_mono = time.monotonic
    clock = {"offset": 0.0}

    monkeypatch.setattr(local_tools.time, "monotonic", lambda: real_mono() + clock["offset"])
    monkeypatch.setattr(store, "inspect", lambda _fn: (_ for _ in ()).throw(OSError("store unreadable")))

    checker = tools.process_allowed(context)
    # First 19 consecutive inspect failures retain last_result (initial True):
    for step in range(19):
        clock["offset"] = step * 1.1
        assert checker() is True, f"stopped too early at failure #{step + 1}"
    # 20th consecutive failure transitions to False ("存储不可读"):
    clock["offset"] = 19 * 1.1
    assert checker() is False

    # Also verify end-to-end with watch() terminating the live process once threshold is crossed:
    cmd = _python_sleep_cmd(30, banner="THRESHOLD_WATCH")
    clock["offset"] = 0.0
    try:
        ident = tools.processes.start(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            local[2],
            context["task_id"],
            None,
            tools.process_allowed(context),
            lambda: False,
        )
        for step in range(1, 22):
            clock["offset"] = step * 1.1
            time.sleep(0.06)
        deadline = real_mono() + 2.0
        snap = tools.processes.read(ident, context["task_id"], wait_seconds=0)
        while real_mono() < deadline and snap["running"]:
            clock["offset"] += 1.1
            time.sleep(0.06)
            snap = tools.processes.read(ident, context["task_id"], wait_seconds=0)
        assert snap["running"] is False
        assert snap["stop_reason"] == "permission_or_task_stopped"
    finally:
        tools.processes.close()


def test_t10_process_authority_check_invokes_store_inspect_at_most_6_times_in_5_seconds(local, monkeypatch):
    """T10: 权限检查 5 秒内最多调 6 次（计数打桩，覆盖改动 6）。"""
    tools, context, _, store = local
    real_inspect = store.inspect
    lock = threading.Lock()
    inspect_calls = {"count": 0}

    def counting_inspect(fn):
        with lock:
            inspect_calls["count"] += 1
        return real_inspect(fn)

    cmd = _python_sleep_cmd(30, banner="RATE_LIMIT")
    try:
        started = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"command": cmd, "background": True},
            "op_t10_rate",
        )
        pid = started["process_id"]
        with lock:
            inspect_calls["count"] = 0
        monkeypatch.setattr(store, "inspect", counting_inspect)

        time.sleep(5.0)
        with lock:
            total = inspect_calls["count"]
        snap = tools.perform("ryn_process_read", {"process_id": pid, "wait_seconds": 0}, context, lambda: False)
        assert snap["running"] is True
        assert 1 <= total <= 6, f"expected 1..6 inspect calls in 5s, got {total}"
    finally:
        tools.processes.close()


def test_t11_fresh_processes_instance_reports_may_have_been_lost_on_restart(tmp_path):
    """T11: 新建 Processes() 后读旧 id 返回带 may_have_been_lost_on_restart 的 not_found（覆盖改动 7）。"""
    p1 = Processes()
    try:
        assert isinstance(getattr(p1, "boot_id", None), str) and p1.boot_id
        old_id = p1.start([sys.executable, "-c", "print('hi')"], tmp_path, "task_1", 10, lambda: True, lambda: False)
        p1.wait(old_id, "task_1", lambda: False)
    finally:
        p1.close()

    p2 = Processes()
    try:
        assert isinstance(getattr(p2, "boot_id", None), str) and p2.boot_id != p1.boot_id
        with pytest.raises(AgentToolRejected) as exc_info:
            p2.read(old_id, "task_1", wait_seconds=0)
        exc = exc_info.value
        assert str(exc) == "agent_process_not_found"
        assert isinstance(exc.details, dict)
        assert exc.details.get("reason") == "process_table_is_in_memory"
        assert exc.details.get("may_have_been_lost_on_restart") is True
        assert exc.correction is not None
        assert (
            "Ryn may have restarted since this process was started; "
            "background processes do not survive a restart."
        ) in exc.correction
        assert "Re-run the command if it is still needed" in exc.correction
    finally:
        p2.close()


def test_t12_f3_j02_7_not_found_details_include_restart_note_in_history_and_skip_invalid_or_foreign_ids(tmp_path):
    """T12: F3 J02-7 —— get 对合法旧 process_id 带 restart_note_in_history: true 且 correction 与重启说明一致；非法/跨任务 id 不误标（覆盖改动 7、9）。"""
    processes = Processes()
    try:
        valid_missing = "process_" + "a" * 32
        with pytest.raises(AgentToolRejected) as exc_info:
            processes.get(valid_missing, "task_owner")
        exc = exc_info.value
        assert str(exc) == "agent_process_not_found"
        assert exc.details == {
            "reason": "process_table_is_in_memory",
            "may_have_been_lost_on_restart": True,
            "restart_note_in_history": True,
        }
        assert "do not read" in exc.correction.lower()

        # Malformed ID without process_ prefix gets bare agent_process_not_found:
        with pytest.raises(AgentToolRejected) as bad_exc:
            processes.get("not_a_process_id", "task_owner")
        assert str(bad_exc.value) == "agent_process_not_found"
        assert bad_exc.value.details is None

        # Existing process belonging to another task gets bare agent_process_not_found:
        live_id = processes.start(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            "task_a",
            None,
            lambda: True,
            lambda: False,
        )
        with pytest.raises(AgentToolRejected) as cross_exc:
            processes.get(live_id, "task_b")
        assert str(cross_exc.value) == "agent_process_not_found"
        assert cross_exc.value.details is None
    finally:
        processes.close()


def _append_shell_bg_exchange(row: dict, call_id: str, command: str, process_id: str, *, running: bool = True) -> None:
    epoch = row.get("context_epoch", 0)
    row["messages"].append(
        {
            "role": "assistant",
            "content": "",
            "context_epoch": epoch,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "ryn_shell_execute",
                        "arguments": json.dumps({"command": command, "background": True}, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    row["messages"].append(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "context_epoch": epoch,
            "content": json.dumps(
                {
                    "process_id": process_id,
                    "running": running,
                    "exit_code": None if running else 0,
                    "output": "listening on 3000\n",
                    "stop_reason": None if running else "exited",
                },
                ensure_ascii=False,
            ),
        }
    )


def test_t13_reconcile_adds_hidden_lost_process_note_for_queued_paused_and_interrupted_tasks(tmp_path):
    """T13: queued / paused / interrupted 任务历史里有 running 的 process_id → reconcile 后多一条隐藏说明并列出该 id（覆盖改动 8）。"""
    runtime, store, tools, _ = _make_runtime_with_local_processes(tmp_path / "agent")
    cases = [
        ("queued", "process_" + "1" * 32, "npm run dev -- --port 3000"),
        ("paused", "process_" + "2" * 32, "python -m http.server 8080"),
        ("interrupted", "process_" + "3" * 32, "cargo watch -x test"),
    ]
    task_ids: dict[str, tuple[str, str, int]] = {}
    for status, pid, cmd in cases:
        created = runtime.create({"message": f"Task for {status}"})
        tid = created["id"]

        def _seed(data, tid=tid, status=status, pid=pid, cmd=cmd):
            row = data["tasks"][tid]
            _append_shell_bg_exchange(row, f"call_{status}", cmd, pid, running=True)
            row["status"] = status
            row["in_flight"] = None
            row["approved"] = None
            row["queue"] = []

        store.transaction(_seed)
        before_len = len(store.read()["tasks"][tid]["messages"])
        task_ids[status] = (tid, pid, before_len)

    try:
        runtime.reconcile()
        all_tasks = store.read()["tasks"]
        for status, cmd_pair in zip(("queued", "paused", "interrupted"), cases):
            _, expected_pid, expected_cmd = cmd_pair
            tid, _, before_len = task_ids[status]
            row = all_tasks[tid]
            assert row["status"] == status
            assert len(row["messages"]) == before_len + 1
            note = row["messages"][-1]
            assert note.get("ui", {}).get("hidden") is True
            assert "Ryn 节点已重启，以下后台进程已结束：" in note["content"]
            assert expected_pid in note["content"]
            assert expected_cmd in note["content"]
            assert "需要时重新启动命令，不要再读这些 id。" in note["content"]
            assert row.get("process_note_boot") == tools.processes.boot_id

            # Verify public_task keeps ui.hidden=True and normalize_history retains the note for model context.
            pub = public_task(row)
            assert pub["messages"][-1]["ui"]["hidden"] is True
            normalized = normalize_history(row["messages"])
            assert any(expected_pid in str(m.get("content") or "") for m in normalized)
    finally:
        tools.processes.close()


def test_t14_stopped_or_exited_process_ids_are_excluded_from_restart_note(tmp_path):
    """T14: 已 stopped / exited 的 id 不列（覆盖改动 8）。"""
    runtime, store, tools, _ = _make_runtime_with_local_processes(tmp_path / "agent")
    t_mixed = runtime.create({"message": "Mixed running and stopped"})["id"]
    t_all_stopped = runtime.create({"message": "All stopped"})["id"]

    pid_alive = "process_" + "a" * 32
    pid_stopped = "process_" + "b" * 32
    pid_exited = "process_" + "c" * 32

    def _seed(data):
        r1 = data["tasks"][t_mixed]
        _append_shell_bg_exchange(r1, "c_alive", "uvicorn app:app", pid_alive, running=True)
        _append_shell_bg_exchange(r1, "c_stop_start", "vite --port 5173", pid_stopped, running=True)
        r1["messages"].append(
            {
                "role": "assistant",
                "content": "",
                "context_epoch": 0,
                "tool_calls": [
                    {
                        "id": "c_stop_do",
                        "type": "function",
                        "function": {
                            "name": "ryn_process_stop",
                            "arguments": json.dumps({"process_id": pid_stopped}),
                        },
                    }
                ],
            }
        )
        r1["messages"].append(
            {
                "role": "tool",
                "tool_call_id": "c_stop_do",
                "context_epoch": 0,
                "content": json.dumps(
                    {
                        "process_id": pid_stopped,
                        "running": False,
                        "exit_code": -15,
                        "output": "",
                        "stop_reason": "owner_or_agent_stopped",
                    }
                ),
            }
        )
        r1["status"] = "paused"
        r1["in_flight"] = None
        r1["queue"] = []

        r2 = data["tasks"][t_all_stopped]
        _append_shell_bg_exchange(r2, "c_exited_start", "pytest -q", pid_exited, running=True)
        r2["messages"].append(
            {
                "role": "assistant",
                "content": "",
                "context_epoch": 0,
                "tool_calls": [
                    {
                        "id": "c_exited_read",
                        "type": "function",
                        "function": {
                            "name": "ryn_process_read",
                            "arguments": json.dumps({"process_id": pid_exited}),
                        },
                    }
                ],
            }
        )
        r2["messages"].append(
            {
                "role": "tool",
                "tool_call_id": "c_exited_read",
                "context_epoch": 0,
                "content": json.dumps(
                    {
                        "process_id": pid_exited,
                        "running": False,
                        "exit_code": 0,
                        "output": "12 passed\n",
                        "stop_reason": "exited",
                    }
                ),
            }
        )
        r2["status"] = "queued"
        r2["in_flight"] = None
        r2["queue"] = []

    store.transaction(_seed)
    r2_before_len = len(store.read()["tasks"][t_all_stopped]["messages"])

    try:
        runtime.reconcile()
        tasks = store.read()["tasks"]
        mixed_note = tasks[t_mixed]["messages"][-1]
        assert mixed_note.get("ui", {}).get("hidden") is True
        assert pid_alive in mixed_note["content"]
        assert pid_stopped not in mixed_note["content"]

        assert len(tasks[t_all_stopped]["messages"]) == r2_before_len
        assert not any(
            "Ryn 节点已重启，以下后台进程已结束" in str(m.get("content") or "")
            for m in tasks[t_all_stopped]["messages"]
        )
    finally:
        tools.processes.close()


def test_t15_repeated_reconcile_in_same_boot_does_not_duplicate_note(tmp_path):
    """T15: 同一启动重复 reconcile 不重复写（覆盖改动 8）。"""
    runtime, store, tools, _ = _make_runtime_with_local_processes(tmp_path / "agent")
    tid = runtime.create({"message": "Idempotent boot note"})["id"]
    pid = "process_" + "d" * 32

    def _seed(data):
        row = data["tasks"][tid]
        _append_shell_bg_exchange(row, "call_d", "node server.js", pid, running=True)
        row["status"] = "paused"
        row["in_flight"] = None
        row["queue"] = []

    store.transaction(_seed)
    try:
        runtime.reconcile()
        after_first = store.read()["tasks"][tid]["messages"]
        notes_first = [
            m for m in after_first if "Ryn 节点已重启，以下后台进程已结束" in str(m.get("content") or "")
        ]
        assert len(notes_first) == 1

        # Second and third reconcile on the SAME boot_id must not append again.
        runtime.reconcile()
        runtime.reconcile()
        after_repeat = store.read()["tasks"][tid]["messages"]
        notes_repeat = [
            m for m in after_repeat if "Ryn 节点已重启，以下后台进程已结束" in str(m.get("content") or "")
        ]
        assert len(notes_repeat) == 1
        assert len(after_repeat) == len(after_first)
    finally:
        tools.processes.close()


def test_t16_tasks_without_background_processes_receive_no_restart_note(tmp_path):
    """T16: 没有后台进程的任务不加说明（覆盖改动 8）。"""
    runtime, store, tools, _ = _make_runtime_with_local_processes(tmp_path / "agent")
    t_queued = runtime.create({"message": "Plain queued task"})["id"]
    t_paused = runtime.create({"message": "Paused after synchronous command"})["id"]
    t_completed = runtime.create({"message": "Completed task"})["id"]

    def _seed(data):
        rq = data["tasks"][t_queued]
        rq["status"] = "queued"
        rq["in_flight"] = None

        rp = data["tasks"][t_paused]
        _append_shell_bg_exchange(rp, "call_sync", "git status", "process_" + "e" * 32, running=False)
        rp["status"] = "paused"
        rp["in_flight"] = None

        rc = data["tasks"][t_completed]
        _append_shell_bg_exchange(rc, "call_done_task", "npm start", "process_" + "f" * 32, running=True)
        rc["status"] = "completed"
        rc["in_flight"] = None

    store.transaction(_seed)
    before_counts = {tid: len(row["messages"]) for tid, row in store.read()["tasks"].items()}

    try:
        runtime.reconcile()
        after_tasks = store.read()["tasks"]
        for tid in (t_queued, t_paused, t_completed):
            assert len(after_tasks[tid]["messages"]) == before_counts[tid]
            assert "process_note_boot" not in after_tasks[tid]
            assert not any(
                "Ryn 节点已重启，以下后台进程已结束" in str(m.get("content") or "")
                for m in after_tasks[tid]["messages"]
            )
    finally:
        tools.processes.close()
