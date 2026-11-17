"""S04-I2: configurable bounded pages, coalesced output and interruptible waits."""
import sys
import os
import threading
import time

import pytest
from test_agent_codex_loop import workstation
from test_agent_local_processes import pending_process as pending_process
from test_agent_runtime import advance, assistant

from rynmesh.agent_runtime import local_processes
from rynmesh.agent_runtime.local_processes import Processes, _append_output
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import AgentToolRejected, encoded
from rynmesh.llm_package.chat import validate_tool_arguments


@pytest.mark.parametrize("requested,expected", [(100, 1000), (1000, 1000), (12000, 12000), (40000, 40000), (90000, 40000)])
def test_T1_page_size_clamps_and_preserves_absolute_offsets(pending_process, requested, expected):
    processes, _, item = pending_process
    _append_output(item, "x" * 50000)
    result = processes.read("process_test", "task", offset=1, wait_seconds=0, max_chars=requested)
    assert len(result["output"]) == expected
    assert result["next_offset"] == 1 + expected and result["output_length"] == 50000


def test_T1_default_and_finished_head_tail_scale_with_requested_page(pending_process):
    processes, state, item = pending_process
    _append_output(item, "H" * 10000 + "M" * 20000 + "T" * 20000)
    assert len(processes.read("process_test", "task", wait_seconds=0)["output"]) == 12000
    state.exit_code = 0
    item["reader_done"].set()
    page = processes.read("process_test", "task", wait_seconds=0, max_chars=3000)
    assert page["output"].startswith("H" * 1000 + "\n...")
    assert page["output"].endswith("T" * 2000) and page["next_offset"] == 1000


def test_T2_real_stream_accumulates_a_page_before_returning(tmp_path):
    processes = Processes()
    script = "import sys,time\nfor i in range(15):\n sys.stdout.write('x'*100);sys.stdout.flush();time.sleep(.04)"
    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 10, lambda: True, lambda: False)
    try:
        page = processes.read(ident, "task", wait_seconds=2, max_chars=1000)
        assert len(page["output"]) == 1000 and page["running"]
        assert page["next_offset"] in (None, 1000)
    finally:
        processes.close()


def test_T2_short_output_waits_for_deadline_but_zero_wait_is_immediate(pending_process):
    processes, _, item = pending_process
    _append_output(item, "ready")
    started = time.monotonic()
    assert processes.read("process_test", "task", wait_seconds=.12)["output"] == "ready"
    assert time.monotonic() - started >= .10
    started = time.monotonic()
    assert processes.read("process_test", "task", wait_seconds=0)["output"] == "ready"
    assert time.monotonic() - started < .1


def test_T3_long_wait_cancellation_is_checked_without_holding_process_lock(pending_process):
    processes, _, _ = pending_process
    started = time.monotonic()
    def cancelled():
        assert not processes.lock._is_owned()
        return time.monotonic() - started >= .04
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        processes.read("process_test", "task", wait_seconds=300, cancelled=cancelled)
    assert time.monotonic() - started < .2


def test_T4_schema_admits_300_and_page_bounds_but_rejects_301():
    schema = SPECS["ryn_process_read"][3]
    validate_tool_arguments(schema, {"process_id": "p", "wait_seconds": 300, "max_chars": 40000})
    for args in [{"wait_seconds": 301}, {"max_chars": 999}, {"max_chars": 40001}]:
        with pytest.raises(ValueError):
            validate_tool_arguments(schema, {"process_id": "p", **args})
    assert "max_seconds" in SPECS["ryn_process_read"][0]


def test_T5_wait_uses_condition_and_wakes_on_exit_without_sleep_polling(pending_process, monkeypatch):
    processes, state, item = pending_process
    entered, slept, result = threading.Event(), [], []
    original_wait, original_sleep = processes.changed.wait, local_processes.time.sleep
    def observed_wait(timeout=None):
        entered.set()
        return original_wait(timeout)
    def observed_sleep(seconds):
        if threading.current_thread() is worker:
            slept.append(seconds)
            entered.set()
        original_sleep(seconds)
    monkeypatch.setattr(processes.changed, "wait", observed_wait)
    monkeypatch.setattr(local_processes.time, "sleep", observed_sleep)
    worker = threading.Thread(target=lambda: result.append(processes.wait("process_test", "task", lambda: False)))
    worker.start()
    assert entered.wait(1)
    with processes.changed:
        state.exit_code = 0
        item["reader_done"].set()
        ended = time.monotonic()
        processes.changed.notify_all()
    worker.join(.1)
    assert not worker.is_alive() and time.monotonic() - ended < .1
    assert result[0]["exit_code"] == 0 and slept == []


@pytest.mark.parametrize("unit", ["x", "中", "\"\\\n\x01"])
@pytest.mark.parametrize("budget", [1000, 5000, 20000])
def test_T1_real_loop_clamps_page_to_task_json_budget_without_result_reference(tmp_path, unit, budget):
    runtime, model, workspace = workstation(tmp_path, assistant())
    runtime.tools.names = (*runtime.tools.names, "ryn_process_read")
    runtime.update_settings({"max_output_chars": budget})
    task = runtime.create({"message": "Read process output"})
    processes = runtime.tools.local.processes
    ident = processes.start([sys.executable, "-u", "-c", f"import sys;sys.stdout.write({unit!r}*50000);sys.stdout.flush()"],
                            workspace, task["id"], 10, lambda: True, lambda: False)
    try:
        processes.wait(ident, task["id"], lambda: False)
        model.responses[0] = assistant(name="ryn_process_read", args={"process_id": ident, "offset": 1,
                                                                     "wait_seconds": 0, "max_chars": 40000})
        row = runtime.store.read()["tasks"][task["id"]]
        assert runtime._execution_context(row)["result_budget_bytes"] == budget
        advance(runtime, 2)
        row = runtime.store.read()["tasks"][task["id"]]
        result = row["receipts"][0]["result"]
        assert "result_ref" not in result and "output" in result
        assert len(encoded(result)) <= budget
        assert len(result["output"]) <= max(1000, budget - 2000)
        assert result["next_offset"] == len(result["output"]) + 1
        assert result["output"] == (unit.replace("\n", os.linesep) * 50000)[1:result["next_offset"]]
    finally:
        processes.close()
