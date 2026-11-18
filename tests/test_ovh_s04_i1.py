"""Overhaul S04-I1 tests: head/tail process output buffer, absolute offsets, incremental UTF-8 decode."""
from __future__ import annotations

import codecs
import collections
import sys
import time

import pytest

from rynmesh.agent_runtime import local_processes
from rynmesh.agent_runtime.local_processes import HEAD_CHARS, PAGE_CHARS, Processes
from rynmesh.agent_runtime.local_specs import SPECS


def test_t1_three_mib_output_retains_last_line_at_end(tmp_path):
    """T1: After >3 MiB of output, reading the end sees the true final line (Steps 1, 2, 3, 4, 7)."""
    assert local_processes.HEAD_KEEP_CHARS == 512 * 1024
    assert local_processes.TAIL_KEEP_CHARS == 512 * 1024
    assert not hasattr(local_processes, "MAX_OUTPUT")

    processes = Processes()
    script = (
        "import sys; "
        "line = b'0123456789abcdef' * 4 + b'\\n'; "
        "sys.stdout.buffer.write(b'HEAD_FIRST_LINE_S04_I1\\n'); "
        "sys.stdout.buffer.write(line * 50000); "
        "sys.stdout.buffer.write(b'TAIL_FINAL_LINE_S04_I1\\n'); "
        "sys.stdout.buffer.flush()"
    )
    expected_total = len("HEAD_FIRST_LINE_S04_I1\n") + 65 * 50000 + len("TAIL_FINAL_LINE_S04_I1\n")
    assert expected_total > 3 * 1024 * 1024

    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 30, lambda: True, lambda: False)
    try:
        item = processes.get(ident, "task")
        assert isinstance(item["head"], str)
        assert isinstance(item["tail"], collections.deque)
        assert "output" not in item

        finished = processes.wait(ident, "task", lambda: False)
        assert finished["exit_code"] == 0
        assert finished["output_length"] == expected_total
        assert finished["output"].endswith("TAIL_FINAL_LINE_S04_I1\n")

        tail_page = processes.read(ident, "task", offset=-1, wait_seconds=0)
        assert tail_page["output"].endswith("TAIL_FINAL_LINE_S04_I1\n")
        assert len(tail_page["output"]) == PAGE_CHARS
        assert tail_page["next_offset"] is None
        assert tail_page["output_length"] == expected_total
    finally:
        processes.close()


def test_t2_running_offset_in_dropped_region_returns_notice_and_tail_start(tmp_path, monkeypatch):
    """T2: While running, an offset inside the dropped middle returns '[N characters dropped]' and next_offset=tail_start (Steps 3, 4, 7)."""
    monkeypatch.setattr(local_processes, "HEAD_KEEP_CHARS", 2000)
    monkeypatch.setattr(local_processes, "TAIL_KEEP_CHARS", 2000)

    processes = Processes()
    script = (
        "import sys; "
        "sys.stdout.write('H' * 2000 + 'D' * 5000 + 'T' * 2000); "
        "sys.stdout.flush(); "
        "sys.stdin.readline()"
    )
    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 30, lambda: True, lambda: False)
    try:
        deadline = time.monotonic() + 5.0
        latest = processes.read(ident, "task", offset=-1, wait_seconds=1)
        while latest["output_length"] < 9000 and time.monotonic() < deadline:
            time.sleep(0.02)
            latest = processes.read(ident, "task", offset=-1, wait_seconds=0)

        assert latest["running"] is True
        assert latest["output_length"] == 9000
        assert latest["dropped_chars"] == 5000
        assert latest["output"] == "T" * 2000
        assert latest["next_offset"] is None

        # Read at head boundary (2000) -> entire dropped section of 5000 chars.
        at_head_end = processes.read(ident, "task", offset=2000, wait_seconds=0)
        assert at_head_end["running"] is True
        assert at_head_end["output"] == "[5000 characters dropped]\n"
        assert at_head_end["next_offset"] == 7000

        # Read mid-dropped region (3200) -> 3800 unread dropped chars before tail_start=7000.
        mid_dropped = processes.read(ident, "task", offset=3200, wait_seconds=0)
        assert mid_dropped["running"] is True
        assert mid_dropped["output"] == "[3800 characters dropped]\n"
        assert mid_dropped["next_offset"] == 7000

        # Following next_offset=7000 lands directly at tail start.
        from_tail = processes.read(ident, "task", offset=mid_dropped["next_offset"], wait_seconds=0)
        assert from_tail["output"] == "T" * 2000
        assert from_tail["next_offset"] is None

        processes.write(ident, "task", "exit\n", eof=True)
        done = processes.wait(ident, "task", lambda: False)
        assert done["running"] is False
    finally:
        processes.close()


def test_t3_finished_first_read_shows_true_head_and_true_tail(tmp_path, monkeypatch):
    """T3: First read (offset=0) of a finished command with dropped middle shows head HEAD_CHARS + omission + real tail (Step 5)."""
    monkeypatch.setattr(local_processes, "HEAD_KEEP_CHARS", 5000)
    monkeypatch.setattr(local_processes, "TAIL_KEEP_CHARS", 5000)

    processes = Processes()
    head_prefix = "BEGIN_BUILD_LOG\n" + ("H" * 4000)
    middle_noise = "M" * 20000
    tail_suffix = ("Z" * 4500) + "\nBUILD_FAILED_AT_END_42\n"
    total_len = len(head_prefix) + len(middle_noise) + len(tail_suffix)

    script = (
        "import sys; "
        f"sys.stdout.buffer.write({head_prefix.encode('utf-8')!r}); "
        f"sys.stdout.buffer.write({middle_noise.encode('utf-8')!r}); "
        f"sys.stdout.buffer.write({tail_suffix.encode('utf-8')!r}); "
        "sys.stdout.buffer.flush()"
    )
    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 30, lambda: True, lambda: False)
    try:
        first = processes.wait(ident, "task", lambda: False)
        assert first["running"] is False
        assert first["output_length"] == total_len
        assert first["output"].startswith(head_prefix[:HEAD_CHARS])
        assert "characters omitted" in first["output"]
        expected_tail_len = PAGE_CHARS - HEAD_CHARS
        assert first["output"].endswith(tail_suffix[-expected_tail_len:])
        assert first["next_offset"] == HEAD_CHARS

        # Paging from next_offset reads the rest of head, then dropped notice, then full retained tail.
        second = processes.read(ident, "task", offset=first["next_offset"], wait_seconds=0)
        assert second["output"] == (head_prefix + middle_noise)[HEAD_CHARS:5000]
        assert second["next_offset"] == 5000

        third = processes.read(ident, "task", offset=second["next_offset"], wait_seconds=0)
        expected_dropped = total_len - 10000
        assert third["output"] == f"[{expected_dropped} characters dropped]\n"
        assert third["next_offset"] == total_len - 5000

        fourth = processes.read(ident, "task", offset=third["next_offset"], wait_seconds=0)
        assert fourth["output"].endswith("\nBUILD_FAILED_AT_END_42\n")
        assert len(fourth["output"]) == 5000
        assert fourth["next_offset"] is None
    finally:
        processes.close()


def test_t4_truncated_and_dropped_chars_are_consistent(tmp_path, monkeypatch):
    """T4: payload['dropped_chars'] and payload['truncated'] stay consistent across non-dropped and dropped runs (Step 6)."""
    monkeypatch.setattr(local_processes, "HEAD_KEEP_CHARS", 1500)
    monkeypatch.setattr(local_processes, "TAIL_KEEP_CHARS", 1500)

    processes = Processes()
    try:
        # Exact boundary (3000 chars == HEAD_KEEP_CHARS + TAIL_KEEP_CHARS): zero dropped.
        exact_id = processes.start(
            [sys.executable, "-u", "-c", "import sys; sys.stdout.write('A' * 3000)"],
            tmp_path, "task", 20, lambda: True, lambda: False,
        )
        exact_res = processes.wait(exact_id, "task", lambda: False)
        assert exact_res["output_length"] == 3000
        assert exact_res["dropped_chars"] == 0
        assert exact_res["truncated"] is False

        # One char over boundary (3001 chars): dropped_chars == 1 and truncated is True.
        over_id = processes.start(
            [sys.executable, "-u", "-c", "import sys; sys.stdout.write('B' * 3001)"],
            tmp_path, "task", 20, lambda: True, lambda: False,
        )
        over_res = processes.wait(over_id, "task", lambda: False)
        assert over_res["output_length"] == 3001
        assert over_res["dropped_chars"] == 1
        assert over_res["truncated"] is True
    finally:
        processes.close()


def test_t5_multibyte_utf8_split_across_chunks_decodes_cleanly(tmp_path, monkeypatch):
    """T5: Multi-byte UTF-8 sequences split across chunks do not corrupt into U+FFFD on PIPE or TTY (Steps 2, 3, 8)."""
    processes = Processes()
    # Child sends partial UTF-8 sequences synchronized via stdin lines:
    # '你' = e4 bd a0; '🚀' = f0 9f 9a 80; '世界' = e4 b8 96 e7 95 8c; trailing incomplete e4 bd flushed on EOF.
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'OK:\\xe4\\xbd')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdin.readline()\n"
        "sys.stdout.buffer.write(b'\\xa0\\xf0\\x9f')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdin.readline()\n"
        "sys.stdout.buffer.write(b'\\x9a\\x80\\xe4\\xb8\\x96\\xe7\\x95\\x8c\\xe4\\xbd')\n"
        "sys.stdout.buffer.flush()\n"
    )
    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 20, lambda: True, lambda: False)
    try:
        first = processes.read(ident, "task", offset=0, wait_seconds=2)
        assert first["output"] == "OK:"
        assert "\ufffd" not in first["output"]
        assert first["output_length"] == 3

        processes.write(ident, "task", "step2\n")
        second = processes.read(ident, "task", offset=3, wait_seconds=2)
        assert second["output"] == "你"
        assert "\ufffd" not in second["output"]
        assert second["output_length"] == 4

        processes.write(ident, "task", "step3\n", eof=True)
        processes.wait(ident, "task", lambda: False)
        third = processes.read(ident, "task", offset=4, wait_seconds=0)
        # Complete '🚀世界' plus 1 replacement char flushed at EOF from dangling b'\xe4\xb8'.
        assert third["output"] == "🚀世界\ufffd"
    finally:
        processes.close()

    # Also verify Step 8: tty=True branch feeds ConPtyProcess.read_output() bytes through incremental decoder.
    import threading

    chunks = iter([b"TTY:\xe4\xbd", b"\xa0\xf0\x9f", b"\x9a\x80\xe5\xa5\xbd", b""])

    class FakePty:
        backend = "conpty"
        rows = 24
        cols = 80
        error = None

        def __init__(self, args, cwd, env, rows=24, cols=80):
            self._closed = threading.Event()
            self._rc = None

        def read_output(self):
            nxt = next(chunks, b"")
            if not nxt:
                self._rc = 0
            return nxt

        def poll(self):
            return self._rc

        def release_buffers(self):
            return None

        def dispose(self):
            self._closed.set()
            self._rc = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(local_processes, "check_pty_support", lambda: None)
    monkeypatch.setattr(local_processes, "ConPtyProcess", FakePty)
    monkeypatch.setattr(local_processes, "terminate", lambda proc: proc.dispose())

    tty_processes = Processes()
    try:
        tty_id = tty_processes.start(["fake-pty"], tmp_path, "task", 10, lambda: True, lambda: False, tty=True)
        tty_res = tty_processes.wait(tty_id, "task", lambda: False)
        assert tty_res["output"] == "TTY:你🚀好"
        assert "\ufffd" not in tty_res["output"]
        assert tty_res["dropped_chars"] == 0
        assert tty_res["truncated"] is False
    finally:
        tty_processes.close()


def test_t6_long_wait_does_not_repeatedly_decode_full_buffer(tmp_path, monkeypatch):
    """T6: Waiting in Processes.read does not repeatedly decode the buffered output on each poll wakeup (Steps 2, 3, 4)."""
    assert hasattr(local_processes, "OutputDecoder"), "local_processes must use incremental OutputDecoder"
    real_decoder_cls = local_processes.OutputDecoder
    stats = {"calls": 0, "bytes": 0}

    class CountingOutputDecoder(real_decoder_cls):
        def decode(self, input_bytes: bytes, final: bool = False) -> str:
            stats["calls"] += 1
            stats["bytes"] += len(input_bytes)
            return super().decode(input_bytes, final=final)

    monkeypatch.setattr(local_processes, "OutputDecoder", CountingOutputDecoder)

    processes = Processes()
    script = (
        "import sys, time; "
        "sys.stdout.write('X' * 20000); "
        "sys.stdout.flush(); "
        "sys.stdin.readline()"
    )
    ident = processes.start([sys.executable, "-u", "-c", script], tmp_path, "task", 20, lambda: True, lambda: False)
    try:
        deadline = time.monotonic() + 5.0
        cur = processes.read(ident, "task", offset=-1, wait_seconds=1)
        while cur["output_length"] < 20000 and time.monotonic() < deadline:
            time.sleep(0.02)
            cur = processes.read(ident, "task", offset=-1, wait_seconds=0)
        assert cur["output_length"] == 20000

        calls_before_wait = stats["calls"]
        bytes_before_wait = stats["bytes"]
        assert bytes_before_wait == 20000

        # Wait ~0.45s (multiple 0.1s condition-wait wakeups) with no new output arriving.
        t0 = time.monotonic()
        polled = processes.read(ident, "task", offset=20000, wait_seconds=0.45)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.35
        assert polled["output"] == ""
        assert polled["running"] is True

        # Zero decode calls and zero bytes decoded during the entire polling wait!
        assert stats["calls"] == calls_before_wait
        assert stats["bytes"] == bytes_before_wait

        processes.write(ident, "task", "done\n", eof=True)
        processes.wait(ident, "task", lambda: False)
    finally:
        processes.close()


def test_tail_read_mode_and_schema_end_to_end(tmp_path):
    """Step 7: ryn_process_read schema allows offset=-1 and LocalTools/Processes return latest page while running."""
    from test_agent_local_tools import execute
    from test_agent_local_tools import local as local_fixture

    _, _, _, schema = SPECS["ryn_process_read"]
    assert schema["properties"]["offset"]["minimum"] == -1
    assert SPECS["ryn_file_read"][3]["properties"]["offset"]["minimum"] == 0

    import os

    gen = local_fixture.__wrapped__(tmp_path)
    tools, context, _, _ = next(gen)
    script = (
        "import sys, time; "
        "sys.stdout.buffer.write(b'A' * 8000 + b'LATEST_RUNNING_TAIL_MARKER\\n'); "
        "sys.stdout.buffer.flush(); "
        "time.sleep(10)"
    )
    if os.name == "nt":
        cmd = f"& '{sys.executable}' -u -c \"{script}\""
    else:
        cmd = f"'{sys.executable}' -u -c \"{script}\""
    try:
        bg = execute(tools, context, "ryn_shell_execute", {"command": cmd, "background": True}, "op_tail_bg")
        pid = bg["process_id"]

        deadline = time.monotonic() + 5.0
        res = tools.perform("ryn_process_read", {"process_id": pid, "offset": -1, "wait_seconds": 1}, context, lambda: False)
        while "LATEST_RUNNING_TAIL_MARKER" not in res["output"] and time.monotonic() < deadline:
            time.sleep(0.05)
            res = tools.perform("ryn_process_read", {"process_id": pid, "offset": -1, "wait_seconds": 0}, context, lambda: False)

        assert res["running"] is True
        assert res["output"].strip().endswith("LATEST_RUNNING_TAIL_MARKER")
        assert len(res["output"]) == min(PAGE_CHARS, res["output_length"])
        assert res["next_offset"] is None

        via_kw = tools.processes.read(pid, context["task_id"], tail=True, wait_seconds=0)
        assert via_kw["output"] == res["output"]

        tools.processes.stop(pid, context["task_id"])
    finally:
        tools.processes.close()
        for _ in gen:
            break
