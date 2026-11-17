"""S04-I17 stdin delivery, incremental output, and live transport boundaries."""
import collections
import io
import json
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime import local_pty, local_processes
from rynmesh.agent_runtime.local_processes import Processes, _append_output
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import AgentToolRejected
from test_agent_local_tools import local
from test_agent_integrations import node, fake_upstream, call
from test_agent_local_execution_reliability import _make_node_runtime

UNKNOWN = {
    "error": "agent_process_input_unconfirmed", "input_delivered": "unknown", "side_effects": "unknown",
    "correction": "Input may have been partly delivered; do not resend it. Read the process output first or stop the process.",
}


def add_process(processes, task_id="task", *, tty=False, stdin=None):
    process = SimpleNamespace(poll=lambda: None, stdin=stdin or io.BytesIO(), rows=24, cols=80)
    item = {"process": process, "task_id": task_id, "head": "", "tail": collections.deque(),
        "tail_chars": 0, "total_chars": 0, "dropped_chars": 0, "reason": None,
        "reader_done": threading.Event(), "tty": tty, "input_lock": threading.Lock(), "stdin_closed": False}
    processes.items["process_test"] = item
    return item


def test_t1_python_interactive_write_returns_only_new_output(tmp_path):
    processes = Processes()
    ident = processes.start([sys.executable, "-u", "-i"], tmp_path, "task", 30,
        lambda: True, lambda: False, stdin_mode="pipe")
    try:
        processes.read(ident, "task", wait_seconds=5)
        result = processes.write(ident, "task", "print(1)\n")
        assert result["input_delivered"] is True and "1" in result["output"]
        assert "Python " not in result["output"] and result["running"]
        assert "next_offset" in result and result["exit_code"] is None
    finally:
        processes.close()


def test_t2_8000_characters_and_explicit_wait_reach_real_local_dispatch(local, monkeypatch):
    tools, context, _, _ = local
    item = add_process(tools.processes, context["task_id"])
    schema = SPECS["ryn_process_write"][3]["properties"]
    assert schema["text"]["maxLength"] == 8000
    assert schema["wait_seconds"]["maximum"] == 30
    seen = []
    def read(ident, task_id, offset=0, **kwargs):
        seen.append((offset, kwargs["wait_seconds"]))
        return {"output": "fresh", "next_offset": None, "running": True, "exit_code": None}
    monkeypatch.setattr(tools.processes, "read", read)
    _append_output(item, "already consumed")
    result = tools.perform("ryn_process_write", {"process_id": "process_test", "text": "x" * 8000,
        "wait_seconds": 30}, context, lambda: False)
    assert item["process"].stdin.getvalue() == b"x" * 8000
    assert seen == [(16, 30)] and result["output"] == "fresh"


@pytest.mark.parametrize("tty,expected", [(True, .1), (False, .25)])
def test_t3_default_wait_is_transport_specific_and_no_old_output(tty, expected, monkeypatch):
    processes = Processes()
    item = add_process(processes, tty=tty)
    item["process"].write_input = lambda text: None
    _append_output(item, "old")
    seen = []
    def read(ident, task_id, offset=0, **kwargs):
        seen.append((offset, kwargs["wait_seconds"]))
        return {"output": "new", "next_offset": None, "running": True, "exit_code": None}
    monkeypatch.setattr(processes, "read", read)
    assert processes.write("process_test", "task", "hello")["output"] == "new"
    assert seen == [(3, expected)]


class BlockedInput:
    def __init__(self):
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
    def write(self, data):
        self.calls += 1
        self.release.wait(10)
    def flush(self):
        self.finished.set()
    def close(self):
        self.release.set()


def test_t8_blocked_pipe_returns_unknown_then_immediate_without_queued_sender():
    processes, stdin = Processes(), BlockedInput()
    item = add_process(processes, stdin=stdin)
    try:
        first = processes.write("process_test", "task", "x" * 8000)
        assert all(first[k] == v for k, v in UNKNOWN.items()) and item["stdin_blocked"]
        started = time.monotonic()
        second = processes.write("process_test", "task", "different")
        assert time.monotonic() - started < .5 and stdin.calls == 1
        assert second["side_effects"] == "unknown"
        assert second["correction"] == "The process is not reading its stdin; read its output or stop it."
    finally:
        stdin.release.set()
        stdin.finished.wait(2)
    deadline = time.monotonic() + 2
    while item.get("stdin_blocked") and time.monotonic() < deadline:
        time.sleep(.01)
    assert not item.get("stdin_blocked")


def transport():
    p = local_pty.ConPtyProcess.__new__(local_pty.ConPtyProcess)
    p._lock = threading.RLock()
    p._send_lock = threading.RLock()
    p._pending, p._sequence = {}, 0
    p._closed, p._events_done = threading.Event(), threading.Event()
    p._returncode, p.error = None, None
    p._output = queue.Queue(maxsize=64)
    p.dropped_chars = 0
    p.last_read_dropped_chars = 0
    p._reported_dropped_chars = 0
    p._input_previous_was_cr = False
    p.disposed = False
    def dispose():
        p.disposed = True
        p._closed.set()
    p.dispose = dispose
    return p


def test_t5_saturated_output_still_delivers_ack_and_reports_dropped_characters():
    p = transport()
    events = queue.Queue()
    p._connection = SimpleNamespace(recv=lambda: events.get(timeout=4), send=lambda msg: events.put(("ack", msg[0], True)))
    reader = threading.Thread(target=p._receive, daemon=True)
    reader.start()
    try:
        for _ in range(65):
            events.put(("output", "中🙂".encode()))
        assert p.write_input("x") is None
        assert p.dropped_chars == 2 and not p.disposed and p.poll() is None
        assert p.read_output().decode() == "中🙂" and p.last_read_dropped_chars == 2
    finally:
        events.put(("exit", 0))
        reader.join(4)
    assert not reader.is_alive()


def test_t6_ack_timeout_is_unknown_and_does_not_destroy_terminal():
    p = transport()
    p._connection = SimpleNamespace(send=lambda msg: None)
    result = p.write_input("x")
    assert result == UNKNOWN and not p.disposed and p.poll() is None and not p._pending


def test_t5_transport_drop_reaches_absolute_process_offsets(tmp_path, monkeypatch):
    p = transport()
    for _ in range(64):
        p._output.put("中🙂".encode())
    p.dropped_chars = 2
    p._returncode = 0
    p._events_done.set()
    p.rows, p.cols = 24, 80
    p.release_buffers = lambda: None
    monkeypatch.setattr(local_processes, "ConPtyProcess", lambda *args: p)
    monkeypatch.setattr(local_processes, "check_pty_support", lambda: None)
    monkeypatch.setattr(local_processes, "terminate", lambda process: None)
    processes = Processes()
    ident = processes.start(["transport-fixture"], tmp_path, "task", tty=True)
    assert processes.get(ident, "task")["reader_done"].wait(3)
    gap = processes.read(ident, "task", wait_seconds=0)
    assert gap["dropped_chars"] == 2 and gap["output_length"] == 130
    assert gap["output"] == "[2 characters dropped]\n" and gap["next_offset"] == 2
    tail = processes.read(ident, "task", offset=2, wait_seconds=0)
    assert tail["output"] == "中🙂" * 64 and tail["next_offset"] is None


def test_t7_connection_loss_is_unknown_and_disposes_terminal():
    p = transport()
    def lost(msg):
        raise EOFError()
    p._connection = SimpleNamespace(send=lost)
    assert p.write_input("x") == UNKNOWN and p.disposed and not p._pending


def test_t4_partial_delivery_reaches_model_and_identical_retry_never_dispatches(node, tmp_path, monkeypatch):
    runtime, store, _, tools, _, _ = _make_node_runtime(node, tmp_path, "s04-i17-unknown")
    runtime.update_settings({"permission_mode": "automatic"})
    args = {"process_id": "process_test", "text": "x" * 8000}
    requests = fake_upstream(monkeypatch, [call("ryn_discover_tools", json.dumps({"load": ["ryn_process_write"]}), "discover"),
        call("ryn_process_write", json.dumps(args), "first"),
        call("ryn_process_write", json.dumps(args), "retry")])
    task = runtime.create({"message": "Send the prepared text to the running process."})
    stdin = BlockedInput()
    add_process(tools.local.processes, task["id"], stdin=stdin)
    try:
        for _ in range(24):
            runtime.run_once()
            receipts = store.read()["tasks"][task["id"]].get("receipts", [])
            if any(r["result"].get("error") == "agent_repeated_failed_write" for r in receipts):
                break
        receipts = store.read()["tasks"][task["id"]].get("receipts", [])
        assert runtime.get(task["id"])["status"] == "queued"
        first = next(r["result"] for r in receipts if r["result"].get("error") == "agent_process_input_unconfirmed")
        assert first["side_effects"] == "unknown" and first["correction"] == UNKNOWN["correction"]
        assert any(r["result"].get("error") == "agent_repeated_failed_write" for r in receipts)
        assert stdin.calls == 1 and UNKNOWN["correction"] in json.dumps(requests[2]["messages"])
    finally:
        stdin.release.set()
        stdin.finished.wait(2)


@pytest.mark.parametrize("wait", [-1, 31, True, float("inf")])
def test_write_wait_rejected_before_input_is_delivered(wait):
    processes = Processes()
    item = add_process(processes)
    with pytest.raises(AgentToolRejected, match="agent_invalid_process_wait"):
        processes.write("process_test", "task", "never", wait_seconds=wait)
    assert item["process"].stdin.getvalue() == b""
