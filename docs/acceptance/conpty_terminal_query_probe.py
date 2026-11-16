"""Empirical probe for S04-I20: test whether Windows ConPTY answers ESC[6n, ESC[5n, and ESC[18t on its own."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

QUERIES = (
    ("DSR_6n", "\x1b[6n"),
    ("DSR_5n", "\x1b[5n"),
    ("XTWINOPS_18t", "\x1b[18t"),
)


def _child_probe() -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    h_in = kernel.GetStdHandle(STD_INPUT_HANDLE)
    h_out = kernel.GetStdHandle(STD_OUTPUT_HANDLE)
    in_mode = w.DWORD()
    out_mode = w.DWORD()
    kernel.GetConsoleMode(h_in, ctypes.byref(in_mode))
    kernel.GetConsoleMode(h_out, ctypes.byref(out_mode))
    # Raw VT input + VT output processing (standard TUI / crossterm mode).
    kernel.SetConsoleMode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
    kernel.SetConsoleMode(h_out, out_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    results = {}
    try:
        for name, seq in QUERIES:
            # Drain any pending console events first.
            avail = w.DWORD()
            kernel.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(avail))
            if avail.value:
                kernel.FlushConsoleInputBuffer(h_in)

            written = w.DWORD()
            raw_seq = seq.encode("ascii")
            kernel.WriteFile(h_out, raw_seq, len(raw_seq), ctypes.byref(written), None)

            collected = bytearray()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                wait_rc = kernel.WaitForSingleObject(h_in, 100)
                if wait_rc != 0:
                    if collected:
                        break
                    continue
                buf = ctypes.create_string_buffer(128)
                n_read = w.DWORD()
                # Check if readable characters arrived on console input handle.
                events = w.DWORD()
                kernel.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(events))
                if events.value == 0:
                    continue
                ok = kernel.ReadFile(h_in, buf, 128, ctypes.byref(n_read), None)
                if ok and n_read.value > 0:
                    collected.extend(buf.raw[: n_read.value])
                    # Stop once response terminator arrives ('R', 'n', or 't').
                    if collected.endswith((b"R", b"n", b"t")):
                        break
            results[name] = {
                "sent": repr(seq),
                "received": repr(collected.decode("latin1")),
                "timed_out": len(collected) == 0,
            }
    finally:
        kernel.SetConsoleMode(h_in, in_mode.value)
        kernel.SetConsoleMode(h_out, out_mode.value)

    print("PROBE_JSON=" + json.dumps(results, ensure_ascii=True), flush=True)
    return 0


def run_host_probe() -> dict:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from rynmesh.agent_runtime.local_processes import Processes

    processes = Processes()
    try:
        ident = processes.start(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--child"],
            root,
            "probe_task",
            15,
            lambda: True,
            lambda: False,
            tty=True,
            rows=24,
            cols=80,
        )
        res = processes.wait(ident, "probe_task", lambda: False, yield_seconds=12)
        for line in res["output"].splitlines():
            if "PROBE_JSON=" in line:
                payload = line.split("PROBE_JSON=", 1)[1].strip()
                # Strip trailing VT sequences if ConPTY appended cursor movement.
                end = payload.rfind("}")
                return json.loads(payload[: end + 1])
        raise RuntimeError(f"Probe did not emit PROBE_JSON: {res!r}")
    finally:
        processes.close()


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(_child_probe())
    if os.name != "nt":
        print("ConPTY probe requires Windows")
        raise SystemExit(0)
    summary = run_host_probe()
    print(json.dumps(summary, indent=2))
