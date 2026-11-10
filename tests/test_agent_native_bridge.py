"""Subprocess fake-worker and protocol/state-machine tests for Ryn NativeBridge (`rynmesh.agent_runtime.computer`).

Never interacts with OS desktop windows or invokes real native binaries or paid models.
Spawns real Python subprocess fake workers in temporary directories via `NativeBridge(dev_executable=...)`
to exercise the real Windows Job Object spawn, bounded JSONL wire transport, and two-phase action coordinator.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import sys
import textwrap
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import pytest

from rynmesh.agent_runtime.computer import (
    PARITY_ACTION_KINDS,
    BoundedJsonlReader,
    NativeBridge,
    NativeBridgeError,
    NativeBridgeRejected,
    NativeWorkerClient,
    bind_node_native_bridge,
    compute_action_fingerprint,
    parse_strict_json,
    parse_wire_envelope,
    serialize_rust_action_spec,
    validate_screenshot_artifact,
)
from rynmesh.agent_runtime.computer.client import _spawn_guarded_worker, _worker_environment
from rynmesh.agent_runtime.store import AgentToolRejected


def _make_valid_png_bytes(width: int = 4, height: int = 3) -> bytes:
    """Construct a valid minimal RGBA PNG byte stream with exact IHDR dimensions."""
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw_rows = b"".join(b"\x00" + (b"\x10\x20\x30\xff" * width) for _ in range(height))
    idat = zlib.compress(raw_rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


FAKE_WORKER_SCRIPT = textwrap.dedent(
    r"""
    from __future__ import annotations
    import base64
    import hashlib
    import json
    import os
    import queue
    import struct
    import sys
    import threading
    import time
    import zlib

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
    STATE_FILE = sys.argv[2] if len(sys.argv) > 2 else ""

    WORKER_INSTANCE = f"wrk_{os.getpid()}_{int(time.time() * 1000) % 1000000}"

    def make_png(w: int = 4, h: int = 3) -> bytes:
        def chunk(t: bytes, d: bytes) -> bytes:
            crc = zlib.crc32(t + d) & 0xFFFFFFFF
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", crc)
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
        raw = b"".join(b"\x00" + (b"\x22\x44\x88\xff" * w) for _ in range(h))
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

    PNG_BYTES = make_png(4, 3)
    PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
    PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
    EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

    out_lock = threading.Lock()
    state_lock = threading.Lock()
    cancel_events: dict[str, threading.Event] = {}
    retired_tasks: set[str] = set()
    tokens_by_id: dict[str, dict] = {}
    active_ticket: dict | None = None
    commit_count = 0
    prepare_count = 0

    def record_state(event_name: str, extra: dict | None = None) -> None:
        if not STATE_FILE:
            return
        with state_lock:
            payload = {
                "event": event_name,
                "pid": os.getpid(),
                "workerInstance": WORKER_INSTANCE,
                "prepareCount": prepare_count,
                "commitCount": commit_count,
                **(extra or {}),
            }
            with open(STATE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
                f.flush()

    def send_frame(obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        with out_lock:
            sys.stdout.write(raw)
            sys.stdout.flush()

    def normalize_action_for_fp(action: dict) -> bytes:
        t = action["type"]
        if t == "focus_window":
            norm = {"type": "focus_window", "restoreIfMinimized": action.get("restoreIfMinimized")}
        elif t in ("click", "double_click"):
            norm = {
                "type": t,
                "button": action.get("button"),
                "target": action.get("target"),
                "element": (
                    {"index": action["element"]["index"], "runtimeId": action["element"].get("runtimeId")}
                    if action.get("element") is not None else None
                ),
            }
        elif t == "context_click":
            norm = {
                "type": "context_click",
                "target": action.get("target"),
                "element": (
                    {"index": action["element"]["index"], "runtimeId": action["element"].get("runtimeId")}
                    if action.get("element") is not None else None
                ),
            }
        elif t == "move_pointer":
            norm = {"type": "move_pointer", "target": action["target"]}
        elif t == "drag":
            norm = {
                "type": "drag",
                "from": action["from"],
                "to": action["to"],
                "button": action.get("button"),
                "steps": action.get("steps"),
            }
        elif t == "scroll":
            norm = {
                "type": "scroll",
                "target": action["target"],
                "deltaX": action["deltaX"],
                "deltaY": action["deltaY"],
            }
        elif t == "type_text":
            norm = {
                "type": "type_text",
                "text": action["text"],
                "target": action.get("target"),
                "element": (
                    {"index": action["element"]["index"], "runtimeId": action["element"].get("runtimeId")}
                    if action.get("element") is not None else None
                ),
            }
        elif t == "key_press":
            norm = {"type": "key_press", "key": action["key"], "repeat": action.get("repeat")}
        elif t == "key_chord":
            norm = {"type": "key_chord", "keys": action["keys"]}
        elif t in ("uia_invoke", "uia_select", "uia_focus"):
            norm = {
                "type": t,
                "element": {"index": action["element"]["index"], "runtimeId": action["element"].get("runtimeId")},
            }
        elif t == "uia_set_value":
            norm = {
                "type": "uia_set_value",
                "element": {"index": action["element"]["index"], "runtimeId": action["element"].get("runtimeId")},
                "value": action["value"],
            }
        elif t == "launch_app":
            norm = {"type": "launch_app", "target": action["target"]}
        else:
            norm = action
        return json.dumps(norm, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def compute_fp(task_id: str, wref: dict, st: str, action: dict) -> str:
        h = hashlib.sha256()
        for part in (
            task_id,
            wref["workerInstance"],
            wref["windowKey"],
            wref["hwnd"],
            str(wref["pid"]),
            wref["processStarted"],
            str(wref["generation"]),
            st,
        ):
            h.update(part.encode("utf-8"))
            h.update(b"|")
        h.update(normalize_action_for_fp(action))
        return h.hexdigest()

    def make_window_ref(hwnd: str = "100100", gen: int = 1) -> dict:
        return {
            "workerInstance": WORKER_INSTANCE,
            "windowKey": f"win_{hwnd}_{gen}",
            "hwnd": hwnd,
            "pid": 4242,
            "processStarted": "133700000000000000",
            "generation": gen,
            "appId": "notepad.exe",
        }

    def make_rect(l: int, t: int, r: int, b: int) -> dict:
        return {"left": l, "top": t, "right": r, "bottom": b, "width": r - l, "height": b - t}

    def make_window_entry(wref: dict) -> dict:
        return {
            "window": wref,
            "title": "Untitled - Notepad",
            "className": "Notepad",
            "processName": "notepad.exe",
            "executablePath": "C:\\Windows\\System32\\notepad.exe",
            "visible": True,
            "enabled": True,
            "minimized": False,
            "maximized": False,
            "foreground": True,
            "cloaked": False,
            "captureProtected": False,
            "bounds": make_rect(100, 100, 500, 400),
            "clientBounds": make_rect(108, 130, 492, 392),
            "dpi": 96,
        }

    def make_observation(task_id: str, wref: dict, st: str) -> dict:
        return {
            "taskSessionId": task_id,
            "window": wref,
            "observationId": f"obs_{st}",
            "stateToken": st,
            "capturedAt": "2026-10-19T00:00:00.000Z",
            "geometry": {
                "windowRect": make_rect(100, 100, 500, 400),
                "clientRect": make_rect(108, 130, 492, 392),
                "screenOrigin": [0, 0],
                "dpi": 96,
                "scaleFactor": 1.0,
            },
            "screenshots": [
                {
                    "id": f"art_{st}",
                    "status": "ok",
                    "captureMethod": "print_window_full_content",
                    "mimeType": "image/png",
                    "width": 4,
                    "height": 3,
                    "originX": 100,
                    "originY": 100,
                    "zIndex": 0,
                    "dpi": 96,
                    "scaleFactor": 1.0,
                    "sha256": PNG_SHA256,
                    "dataBase64": PNG_B64,
                    "minimized": False,
                    "occluded": False,
                    "captureProtected": False,
                }
            ],
            "accessibility": {
                "status": "ok",
                "treeHash": EMPTY_SHA256,
                "truncated": False,
                "focusedElementIndex": 0,
                "focusedRuntimeId": "42.1",
                "selectedElementIndices": [],
                "elements": [
                    {
                        "index": 0,
                        "runtimeId": "42.1",
                        "automationId": "Edit1",
                        "name": "Text Editor",
                        "controlType": "Edit",
                        "className": "Edit",
                        "value": "hello",
                        "password": False,
                        "enabled": True,
                        "offscreen": False,
                        "focusable": True,
                        "focused": True,
                        "bounds": make_rect(110, 140, 480, 380),
                        "depth": 1,
                        "patterns": ["ValuePattern", "TextPattern"],
                    }
                ],
            },
            "foreground": {
                "isTargetWindow": True,
                "isRelatedToTarget": True,
                "foregroundHwnd": wref["hwnd"],
                "foregroundPid": wref["pid"],
            },
        }

    def make_error(req_id, code: str, stage: str, mutation: str = "none", reason: str = "malformed_json", op_id=None, internal_mutation: str = "none"):
        err = {
            "code": code,
            "stage": stage,
            "mutation": mutation,
            "requestId": str(req_id),
            "retryable": False,
            "message": "Fixed static error message.",
            "details": {
                "internalMutation": internal_mutation,
                "reason": reason,
            },
        }
        if op_id is not None:
            err["operationId"] = op_id
        return {"v": 1, "id": req_id, "error": err}

    def handle_rpc(req: dict) -> None:
        global active_ticket, commit_count, prepare_count
        req_id = req["id"]
        method = req["method"]
        params = req.get("params") or {}

        if method == "initialize":
            if MODE == "slow_handshake":
                time.sleep(0.35)
            if MODE == "handshake_wrong_version":
                send_frame({
                    "v": 1,
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2.0.0",
                        "workerInstance": WORKER_INSTANCE,
                        "worker": {"name": "ryn_computer_use", "version": "0.1.0", "pid": os.getpid()},
                        "capabilities": {
                            "dpiAwareness": "per_monitor_aware_v2",
                            "uia": True,
                            "windowCapture": ["print_window_full_content"],
                            "windowCaptureLimitations": [],
                            "inputLock": "win32_named_mutex_dacl_current_user",
                            "actions": ["click", "type_text"],
                            "unsupportedActions": ["drag", "launch_app"],
                        },
                        "limits": {
                            "maxCommandLineBytes": 65536,
                            "maxResponseFrameBytes": 16777216,
                            "maxPngBytes": 8388608,
                            "maxWindows": 256,
                            "maxUiaElements": 1000,
                            "maxUiaDepth": 16,
                        },
                    },
                })
                return
            if MODE == "handshake_wrong_worker":
                send_frame({
                    "v": 1,
                    "id": req_id,
                    "result": {
                        "protocolVersion": "1.0.0",
                        "workerInstance": WORKER_INSTANCE,
                        "worker": {"name": "rogue_worker", "version": "9.9.9", "pid": os.getpid()},
                        "capabilities": {
                            "dpiAwareness": "per_monitor_aware_v2",
                            "uia": True,
                            "windowCapture": ["print_window_full_content"],
                            "windowCaptureLimitations": [],
                            "inputLock": "win32_named_mutex_dacl_current_user",
                            "actions": ["click"],
                            "unsupportedActions": ["drag", "launch_app"],
                        },
                        "limits": {
                            "maxCommandLineBytes": 65536,
                            "maxResponseFrameBytes": 16777216,
                            "maxPngBytes": 8388608,
                            "maxWindows": 256,
                            "maxUiaElements": 1000,
                            "maxUiaDepth": 16,
                        },
                    },
                })
                return

            if MODE == "handshake_wrong_pid":
                send_frame({
                    "v": 1,
                    "id": req_id,
                    "result": {
                        "protocolVersion": "1.0.0",
                        "workerInstance": WORKER_INSTANCE,
                        "worker": {"name": "ryn_computer_use", "version": "0.1.0", "pid": os.getpid() + 1},
                        "capabilities": {
                            "dpiAwareness": "per_monitor_aware_v2",
                            "uia": True,
                            "windowCapture": ["print_window_full_content", "print_window"],
                            "windowCaptureLimitations": ["phase1_gdi_print_window_candidate_only"],
                            "inputLock": "win32_named_mutex_dacl_current_user",
                            "actions": ["focus_window", "click"],
                            "unsupportedActions": ["drag", "launch_app"],
                        },
                        "limits": {
                            "maxCommandLineBytes": 65536,
                            "maxResponseFrameBytes": 16777216,
                            "maxPngBytes": 8388608,
                            "maxWindows": 256,
                            "maxUiaElements": 1000,
                            "maxUiaDepth": 16,
                        },
                    },
                })
                return

            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "protocolVersion": "1.0.0",
                    "workerInstance": WORKER_INSTANCE,
                    "worker": {"name": "ryn_computer_use", "version": "0.1.0", "pid": os.getpid()},
                    "capabilities": {
                        "dpiAwareness": "per_monitor_aware_v2",
                        "uia": True,
                        "windowCapture": ["print_window_full_content", "print_window"],
                        "windowCaptureLimitations": ["phase1_gdi_print_window_candidate_only"],
                        "inputLock": "win32_named_mutex_dacl_current_user",
                        "actions": [
                            "focus_window",
                            "click",
                            "double_click",
                            "context_click",
                            "move_pointer",
                            "scroll",
                            "type_text",
                            "key_press",
                            "key_chord",
                            "uia_invoke",
                            "uia_set_value",
                            "uia_select",
                            "uia_focus",
                        ],
                        "unsupportedActions": ["drag", "launch_app"],
                    },
                    "limits": {
                        "maxCommandLineBytes": 65536,
                        "maxResponseFrameBytes": 16777216,
                        "maxPngBytes": 8388608,
                        "maxWindows": 256,
                        "maxUiaElements": 1000,
                        "maxUiaDepth": 16,
                    },
                },
            })
            return

        if method == "windows.list":
            if MODE in ("lifecycle_once", "lifecycle_persistent", "capture_failure"):
                with open(STATE_FILE, encoding="utf-8") as source:
                    starts = sum(json.loads(line)["event"] == "worker_started" for line in source)
                if MODE != "lifecycle_once" or starts == 1:
                    reason = "generation_mismatch" if MODE != "capture_failure" else "malformed_json"
                    send_frame(make_error(req_id, "observation_unavailable", "observe", reason=reason))
                    return
            if MODE == "foreign_id":
                send_frame({"v": 1, "id": "foreign_req_999", "result": {"capturedAt": "2026-10-19T00:00:00.000Z", "windows": []}})
                return
            if MODE == "duplicate_key":
                with out_lock:
                    sys.stdout.write('{"v":1,"id":"' + str(req_id) + '","result":{"capturedAt":"2026-10-19T00:00:00.000Z","capturedAt":"dup","windows":[]}}\n')
                    sys.stdout.flush()
                return
            if MODE == "oversized_frame":
                with out_lock:
                    sys.stdout.write('{"v":1,"id":"' + str(req_id) + '","result":{"pad":"' + ("A" * (16 * 1024 * 1024 + 1024)) + '"}}\n')
                    sys.stdout.flush()
                return
            if MODE == "eof_on_list":
                os._exit(1)
            if MODE == "event_flood_on_list":
                for i in range(300):
                    send_frame({
                        "v": 1,
                        "event": "action.stage",
                        "requestId": req_id,
                        "data": {"step": i},
                    })
                return
            if MODE == "slow_list":
                time.sleep(0.6)

            w1 = make_window_ref("100100", 1)
            w2 = make_window_ref("200200", 1)
            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "capturedAt": "2026-10-19T00:00:00.000Z",
                    "foregroundWindow": w1,
                    "windows": [make_window_entry(w1), make_window_entry(w2)],
                },
            })
            return

        if method == "windows.get":
            wref = params.get("window") or make_window_ref(params.get("hwnd") or "100100", 1)
            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "capturedAt": "2026-10-19T00:00:00.000Z",
                    "entry": make_window_entry(wref),
                },
            })
            return

        if method == "apps.list":
            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "apps": [
                        {
                            "appId": "notepad.exe",
                            "name": "Notepad",
                            "source": "system_catalog",
                            "executablePath": "C:\\Windows\\System32\\notepad.exe",
                        }
                    ]
                },
            })
            return

        if method == "state.get":
            task_id = params["taskSessionId"]
            wref = params["window"]
            if wref.get("workerInstance") != WORKER_INSTANCE:
                send_frame(make_error(req_id, "window_stale", "validate", "none", "worker_instance_mismatch"))
                return
            with state_lock:
                retired_tasks.discard(task_id)
                st = f"st_{len(tokens_by_id) + 1}_{os.getpid()}"
                tokens_by_id[st] = {"taskSessionId": task_id, "window": wref}
            obs = make_observation(task_id, wref, st)
            if MODE == "bad_screenshot_sha":
                obs["screenshots"][0]["sha256"] = "0" * 64
            send_frame({"v": 1, "id": req_id, "result": obs})
            return

        if method == "action.prepare":
            task_id = params["taskSessionId"]
            op_id = params["operationId"]
            wref = params["window"]
            st = params["expectedStateToken"]
            action = params["action"]
            auth_epoch = params.get("authorityEpoch")
            constraints = params.get("constraints") or {}

            with state_lock:
                prepare_count += 1
                ev = cancel_events.setdefault(op_id, threading.Event())
                if task_id in retired_tasks:
                    send_frame(make_error(req_id, "task_not_found", "validate", "none", "token_task_mismatch", op_id))
                    return
                tok = tokens_by_id.get(st)
                if tok is None:
                    send_frame(make_error(req_id, "state_token_invalid", "validate", "none", "token_window_mismatch", op_id))
                    return
                if tok["taskSessionId"] != task_id:
                    send_frame(make_error(req_id, "task_conflict", "validate", "none", "token_task_mismatch", op_id))
                    return

            record_state("prepare_received", {"operationId": op_id, "taskSessionId": task_id})

            if MODE == "prepare_denied":
                send_frame(make_error(req_id, "window_occluded", "prepare", "none", "hit_test_unrelated_window", op_id))
                return

            if MODE == "wait_lock_slow":
                # Simulate waiting on global desktop mutex while control thread can receive cancel
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if ev.is_set():
                        record_state("prepare_cancelled_while_waiting_lock", {"operationId": op_id})
                        send_frame(make_error(req_id, "cancelled", "waiting_lock", "none", "cancelled_before_lock", op_id))
                        return
                    time.sleep(0.02)

            if MODE == "delay_before_ready":
                time.sleep(0.15)

            fp = compute_fp(task_id, wref, st, action)
            ticket_id = f"pt_{op_id}_{os.getpid()}"

            if MODE == "tamper_ready_fp":
                fp = "f" * 64
            if MODE == "tamper_ready_window":
                wref = {**wref, "hwnd": "999999"}
            if MODE == "tamper_ready_mutation":
                mut = "possible"
            else:
                mut = "none"

            ready_ttl = int(constraints.get("readyTimeoutMs") or 5000)

            with state_lock:
                active_ticket = {
                    "taskSessionId": task_id,
                    "operationId": op_id,
                    "prepareTicket": ticket_id,
                    "window": wref,
                    "expectedStateToken": st,
                    "actionFingerprint": fp,
                }

            ready_data = {
                "taskSessionId": task_id,
                "operationId": op_id,
                "prepareTicket": ticket_id,
                "window": wref,
                "expectedStateToken": st,
                "actionKind": action["type"],
                "actionFingerprint": fp,
                "inputLockHeld": True,
                "lockWasAbandoned": False,
                "expiresInMs": ready_ttl,
                "mutation": mut,
            }
            if auth_epoch is not None:
                ready_data["authorityEpoch"] = auth_epoch

            send_frame({
                "v": 1,
                "event": "action.ready",
                "requestId": req_id,
                "operationId": op_id,
                "data": ready_data,
            })

            prep_result = {
                "status": "ready",
                "taskSessionId": task_id,
                "operationId": op_id,
                "prepareTicket": ticket_id,
                "window": wref,
                "expectedStateToken": st,
                "actionKind": action["type"],
                "actionFingerprint": fp,
                "expiresInMs": ready_ttl,
                "inputLockHeld": True,
                "lockWasAbandoned": False,
                "mutation": mut,
                "resolvedTarget": {
                    "windowHwnd": wref["hwnd"],
                    "screenPoint": [150, 150],
                },
            }
            if auth_epoch is not None:
                prep_result["authorityEpoch"] = auth_epoch

            record_state("prepare_ready", {"operationId": op_id, "prepareTicket": ticket_id})
            send_frame({"v": 1, "id": req_id, "result": prep_result})
            return

        if method == "action.commit":
            task_id = params["taskSessionId"]
            op_id = params["operationId"]
            pt = params["prepareTicket"]
            with state_lock:
                commit_count += 1
                cur = active_ticket
                active_ticket = None
            record_state("commit_received", {"operationId": op_id, "prepareTicket": pt, "paramsKeys": sorted(params.keys())})

            if MODE == "commit_eof_no_reply":
                os._exit(1)

            if MODE == "commit_error_mutation_none":
                # Native error after commit was sent, claiming mutation="none"
                send_frame(make_error(req_id, "focus_failed", "activating", "none", "foreground_verification_failed", op_id, "none"))
                return

            if MODE == "commit_error_input_failed":
                send_frame(make_error(req_id, "input_failed", "input_started", "possible", "send_input_short_count", op_id, "unknown_or_partial"))
                return

            if cur is None or cur["prepareTicket"] != pt or cur["operationId"] != op_id:
                send_frame(make_error(req_id, "prepare_ticket_invalid", "commit", "none", "token_window_mismatch", op_id))
                return

            with state_lock:
                tokens_by_id.pop(cur["expectedStateToken"], None)
                new_st = f"st_post_{commit_count}_{os.getpid()}"
                tokens_by_id[new_st] = {"taskSessionId": task_id, "window": cur["window"]}

            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "status": "committed",
                    "taskSessionId": task_id,
                    "operationId": op_id,
                    "actionFingerprint": cur["actionFingerprint"],
                    "mutation": "delivered",
                    "focusRecovery": {
                        "attempted": True,
                        "succeeded": True,
                        "methodUsed": "already_foreground",
                        "durationMs": 1,
                        "finalForegroundVerified": True,
                    },
                    "observation": make_observation(task_id, cur["window"], new_st),
                },
            })
            return

        if method == "action.cancel":
            task_id = params["taskSessionId"]
            op_id = params["operationId"]
            with state_lock:
                ev = cancel_events.setdefault(op_id, threading.Event())
                ev.set()
                if active_ticket is not None and active_ticket["operationId"] == op_id:
                    active_ticket = None
            record_state("cancel_received", {"operationId": op_id, "taskSessionId": task_id})
            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "cancelled": True,
                    "operationId": op_id,
                    "stage": "ready",
                    "mutation": "none",
                    "inputLockReleased": True,
                },
            })
            return

        if method == "session.end":
            task_id = params["taskSessionId"]
            with state_lock:
                retired_tasks.add(task_id)
                removed = [k for k, v in tokens_by_id.items() if v["taskSessionId"] == task_id]
                for k in removed:
                    tokens_by_id.pop(k, None)
                cancelled_ops = 0
                if active_ticket is not None and active_ticket["taskSessionId"] == task_id:
                    active_ticket = None
                    cancelled_ops = 1
            record_state("session_end_received", {"taskSessionId": task_id, "invalidatedStateTokens": len(removed)})
            send_frame({
                "v": 1,
                "event": "session.retired",
                "requestId": req_id,
                "data": {
                    "taskSessionId": task_id,
                    "cancelledOperations": cancelled_ops,
                    "invalidatedStateTokens": len(removed),
                },
            })
            send_frame({
                "v": 1,
                "id": req_id,
                "result": {
                    "ended": True,
                    "cancelledOperations": cancelled_ops,
                    "invalidatedStateTokens": len(removed),
                },
            })
            return

        if method == "shutdown":
            record_state("shutdown_received")
            send_frame({"v": 1, "id": req_id, "result": {"shuttingDown": True}})
            return

    def main() -> None:
        record_state("worker_started")
        while True:
            line = sys.stdin.readline()
            if not line:
                record_state("worker_stdin_eof")
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            if MODE == "stall_stdin_after_init" and req.get("method") == "initialize":
                handle_rpc(req)
                time.sleep(60)
                break
            method = req.get("method")
            if method in ("action.cancel", "session.end", "shutdown", "windows.list", "windows.get", "apps.list"):
                threading.Thread(target=handle_rpc, args=(req,), daemon=True).start()
            else:
                threading.Thread(target=handle_rpc, args=(req,), daemon=True).start()
            if method == "shutdown":
                time.sleep(0.05)
                break

    if __name__ == "__main__":
        main()
    """
).lstrip()


@pytest.fixture
def fake_worker_factory(tmp_path: Path):
    script_path = tmp_path / "fake_native_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = getattr(sys, "_base_executable", None) or sys.executable

    def _make(mode: str = "normal", state_name: str = "state.jsonl") -> tuple[list[str], Path]:
        state_file = tmp_path / state_name
        cmd = [str(Path(base_py).resolve()), str(script_path.resolve()), mode, str(state_file.resolve())]
        return cmd, state_file

    return _make


def _read_state_events(state_file: Path) -> list[dict[str, Any]]:
    if not state_file.exists():
        return []
    lines = state_file.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
@pytest.mark.parametrize("mode,expected_starts,success", [
    ("lifecycle_once", 2, True),
    ("lifecycle_persistent", 2, False),
    ("capture_failure", 1, False),
])
def test_window_list_recovers_only_lifecycle_failure_once(fake_worker_factory, mode, expected_starts, success):
    cmd, state_file = fake_worker_factory(mode)
    bridge = NativeBridge(dev_executable=cmd)
    try:
        session = bridge.client.ensure_session()
        old = bridge._get_or_create_task_state("previous_task", worker_instance=session.worker_instance, generation=session.generation)
        old.tokens["old_token"] = {}
        # Receipts must survive recovery, including unresolved writes.
        receipt = object()
        bridge._operations["previous_write"] = receipt
        if success:
            result = bridge.list_windows(task_session_id="new_task")
            assert len(result["windows"]) == 2
            assert result["windows"][0]["window"]["workerInstance"] != session.worker_instance
            assert "previous_task" not in bridge._tasks
            assert bridge._operations["previous_write"] is receipt
        else:
            with pytest.raises(NativeBridgeError):
                bridge.list_windows(task_session_id="new_task")
        events = _read_state_events(state_file)
        assert sum(e["event"] == "worker_started" for e in events) == expected_starts
        assert all(e["prepareCount"] == e["commitCount"] == 0 for e in events)
    finally:
        bridge._operations.clear()
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_window_list_does_not_restart_during_write(fake_worker_factory):
    cmd, state_file = fake_worker_factory("lifecycle_once")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        with bridge._write_mutex:
            with pytest.raises(NativeBridgeError):
                bridge.list_windows(task_session_id="new_task")
        assert sum(e["event"] == "worker_started" for e in _read_state_events(state_file)) == 1
    finally:
        bridge.close()


# ============================================================================
# 1. Pure Protocol & Fingerprint Tests
# ============================================================================


def test_cache_status_is_strictly_read_only(fake_worker_factory) -> None:
    cmd, state_file = fake_worker_factory("normal", "readonly_state.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        status_before = bridge.cache_status()
        assert status_before["status"] == "unknown"
        assert status_before["cached"] is False
        assert status_before["live"] is False
        assert status_before["workerInstance"] is None
        assert not state_file.exists()

        # Trigger actual read so handshake happens
        win_list = bridge.list_windows(task_session_id="task_1")
        assert len(win_list["windows"]) == 2

        status_live = bridge.cache_status()
        assert status_live["status"] == "ready"
        assert status_live["cached"] is True
        assert status_live["live"] is True
        assert status_live["workerInstance"].startswith("wrk_")
        assert status_live["worker"]["name"] == "ryn_computer_use"
        assert status_live["handshakeTimestamp"] is not None
    finally:
        bridge.close()

    status_after_close = bridge.cache_status()
    assert status_after_close["status"] == "stale"
    assert status_after_close["cached"] is True
    assert status_after_close["live"] is False
    assert status_after_close["workerInstance"].startswith("wrk_")


def test_strict_json_and_bounded_reader_validation() -> None:
    # Duplicate key at top level or nested level must be rejected
    with pytest.raises(NativeBridgeError) as exc_info:
        parse_strict_json('{"v":1,"id":"req_1","result":{"a":1,"a":2}}')
    assert exc_info.value.code == "agent_computer_duplicate_json_key"

    # Non-finite numbers rejected
    with pytest.raises(NativeBridgeError) as exc_info2:
        parse_strict_json('{"v":1,"id":"req_1","result":{"x":NaN}}')
    assert exc_info2.value.code == "agent_computer_nonfinite_number"

    # Unknown top-level envelope key rejected
    with pytest.raises(NativeBridgeError) as exc_info3:
        parse_wire_envelope('{"v":1,"id":"req_1","result":{},"extra":123}')
    assert exc_info3.value.code == "agent_computer_protocol_invalid"

    # Both result and error present rejected
    with pytest.raises(NativeBridgeError):
        parse_wire_envelope(
            '{"v":1,"id":"req_1","result":{},"error":{"code":"internal_error","stage":"protocol","mutation":"none","requestId":"req_1","retryable":false,"message":"err"}}'
        )

    # BoundedJsonlReader stops at max_line_bytes + 1 without reading rest of huge stream
    stream = io.BytesIO(b"A" * 2048 + b"\n" + b'{"v":1}\n')
    reader = BoundedJsonlReader(stream, max_line_bytes=256, chunk_size=64)
    frame = reader.read_frame()
    assert frame.kind == "oversized"
    assert stream.tell() <= 320


def test_rust_action_spec_serialization_and_fingerprint_parity() -> None:
    # All 15 parity action kinds must normalize and serialize with exact Rust serde field order and null Optionals
    assert len(PARITY_ACTION_KINDS) == 15
    wref = {
        "workerInstance": "wrk_abc123",
        "windowKey": "win_100",
        "hwnd": "100100",
        "pid": 1234,
        "processStarted": "133700000000000000",
        "generation": 1,
    }
    # Click omitting optional button and element must serialize button:null and element:null in exact order
    click_spec = {
        "target": {"y": 20, "x": 10, "coordinateSpace": "window_physical_px"},
        "type": "click",
    }
    raw_json = serialize_rust_action_spec(click_spec).decode("utf-8")
    assert (
        raw_json
        == '{"type":"click","button":null,"target":{"coordinateSpace":"window_physical_px","x":10,"y":20},"element":null}'
    )

    # ElementTarget omitting runtimeId must serialize runtimeId:null
    invoke_spec = {
        "type": "uia_invoke",
        "element": {"index": 3},
    }
    assert (
        serialize_rust_action_spec(invoke_spec).decode("utf-8")
        == '{"type":"uia_invoke","element":{"index":3,"runtimeId":null}}'
    )

    # Key direction order should not change fingerprint
    fp1 = compute_action_fingerprint("task_a", wref, "st_1", click_spec)
    fp2 = compute_action_fingerprint(
        "task_a",
        wref,
        "st_1",
        {
            "type": "click",
            "button": None,
            "target": {"coordinateSpace": "window_physical_px", "x": 10, "y": 20},
            "element": None,
        },
    )
    assert fp1 == fp2


def test_screenshot_validation_dimensions_and_sha256() -> None:
    png_bytes = _make_valid_png_bytes(4, 3)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    sha = hashlib.sha256(png_bytes).hexdigest()

    valid_shot = {
        "id": "art_1",
        "status": "ok",
        "captureMethod": "print_window_full_content",
        "mimeType": "image/png",
        "width": 4,
        "height": 3,
        "originX": 0,
        "originY": 0,
        "zIndex": 0,
        "dpi": 96,
        "scaleFactor": 1.0,
        "sha256": sha,
        "dataBase64": b64,
        "minimized": False,
        "occluded": False,
        "captureProtected": False,
    }
    validated = validate_screenshot_artifact(valid_shot)
    assert validated["width"] == 4
    assert validated["height"] == 3

    # Dimension mismatch against PNG IHDR must fail
    bad_dim = {**valid_shot, "width": 5}
    with pytest.raises(NativeBridgeError) as exc_info:
        validate_screenshot_artifact(bad_dim)
    assert exc_info.value.code == "agent_computer_screenshot_invalid"

    # SHA-256 mismatch must fail
    bad_sha = {**valid_shot, "sha256": "a" * 64}
    with pytest.raises(NativeBridgeError) as exc_info2:
        validate_screenshot_artifact(bad_sha)
    assert exc_info2.value.code == "agent_computer_screenshot_invalid"


# ============================================================================
# 2. Subprocess Worker Lifecycle, Handshake, and Protocol Failure Cleanup
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_handshake_mismatch_and_job_cleanup(fake_worker_factory) -> None:
    cmd_ver, _ = fake_worker_factory("handshake_wrong_version", "hs_ver.jsonl")
    bridge_ver = NativeBridge(dev_executable=cmd_ver)
    try:
        with pytest.raises(NativeBridgeRejected) as exc_info:
            bridge_ver.list_windows(task_session_id="t1")
        assert exc_info.value.code == "agent_computer_version_mismatch"
        assert exc_info.value.pre_dispatch is True
        assert bridge_ver.cache_status()["status"] == "unknown"
    finally:
        bridge_ver.close()

    cmd_wrk, _ = fake_worker_factory("handshake_wrong_worker", "hs_wrk.jsonl")
    bridge_wrk = NativeBridge(dev_executable=cmd_wrk)
    try:
        with pytest.raises(NativeBridgeRejected) as exc_info2:
            bridge_wrk.list_windows(task_session_id="t1")
        assert exc_info2.value.code == "agent_computer_worker_identity_mismatch"
        assert exc_info2.value.pre_dispatch is True
    finally:
        bridge_wrk.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("foreign_id", "agent_computer_foreign_response_id"),
        ("duplicate_key", "agent_computer_duplicate_json_key"),
        ("oversized_frame", "agent_computer_frame_too_large"),
        ("eof_on_list", "agent_computer_process_disconnected"),
    ],
)
def test_wire_violations_and_eof_fail_closed_and_cleanup(
    fake_worker_factory, mode: str, expected_code: str
) -> None:
    cmd, _ = fake_worker_factory(mode, f"wire_{mode}.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        with pytest.raises(NativeBridgeError) as exc_info:
            bridge.list_windows(task_session_id="t1")
        assert exc_info.value.code == expected_code
        # Session should be marked non-live after wire violation
        assert bridge.cache_status()["live"] is False
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_concurrent_request_map_and_generation_isolation(fake_worker_factory) -> None:
    cmd, _ = fake_worker_factory("slow_list", "concurrent.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def _worker_call(idx: int) -> None:
            try:
                res = bridge.list_windows(task_session_id=f"task_{idx}")
                results.append(res)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker_call, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors
        assert len(results) == 4
        # All 4 concurrent first-connect calls must share generation 1!
        assert bridge.cache_status()["workerGeneration"] == 1

        # Now test that a timed-out request in generation 1 is not adopted into generation 2
        with pytest.raises(NativeBridgeError) as exc_info:
            bridge.list_windows(task_session_id="task_timeout", timeout=0.1)
        assert exc_info.value.code == "agent_computer_timeout"

        # Retire generation 1 and switch to fast normal worker for generation 2
        cmd_fast, _ = fake_worker_factory("normal", "concurrent_fast.jsonl")
        bridge._client._dev_executable = cmd_fast
        bridge._client.retire_generation()

        res_gen2 = bridge.list_windows(task_session_id="task_gen2")
        assert len(res_gen2["windows"]) == 2
        assert bridge.cache_status()["workerGeneration"] == 2
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_single_flight_first_connect_and_close_during_handshake(
    fake_worker_factory, tmp_path: Path
) -> None:
    cmd_normal, _ = fake_worker_factory("normal", "sf_normal.jsonl")
    spawned: list[Any] = []
    spawn_lock = threading.Lock()

    def _factory() -> Any:
        proc = _spawn_guarded_worker(
            cmd_normal,
            cwd=str(tmp_path),
            env=_worker_environment(str(tmp_path)),
        )
        with spawn_lock:
            spawned.append(proc)
        return proc

    client = NativeWorkerClient(transport_factory=_factory)
    start_barrier = threading.Barrier(4)
    sessions: list[Any] = []
    failures: list[BaseException] = []

    def _connect() -> None:
        try:
            start_barrier.wait(timeout=5.0)
            sessions.append(client.ensure_session())
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=_connect) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not failures
        assert len(sessions) == 4
        assert len(spawned) == 1
        assert {s.generation for s in sessions} == {1}
        client.close()
        assert spawned[0].poll() is not None
    finally:
        client.close()

    # Now verify close-during-handshake wakes and cleans up the candidate worker
    cmd_slow, _ = fake_worker_factory("slow_handshake", "sf_slow.jsonl")
    slow_spawned: list[Any] = []
    entered_factory = threading.Event()

    def _slow_factory() -> Any:
        proc = _spawn_guarded_worker(
            cmd_slow,
            cwd=str(tmp_path),
            env=_worker_environment(str(tmp_path)),
        )
        slow_spawned.append(proc)
        entered_factory.set()
        return proc

    client_close_race = NativeWorkerClient(transport_factory=_slow_factory)
    race_errors: list[BaseException] = []

    def _slow_connect() -> None:
        try:
            client_close_race.ensure_session()
        except BaseException as exc:
            race_errors.append(exc)

    t_slow = threading.Thread(target=_slow_connect)
    t_slow.start()
    assert entered_factory.wait(timeout=3.0)
    time.sleep(0.05)
    client_close_race.close()
    t_slow.join(timeout=3.0)
    assert not t_slow.is_alive()
    assert len(race_errors) == 1
    assert len(slow_spawned) == 1
    assert slow_spawned[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_sequential_legitimate_events_exceed_256_while_flood_still_rejected(
    fake_worker_factory,
) -> None:
    cmd, state_file = fake_worker_factory("normal", "seq_events.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        # Ending a nonexistent session when no worker is running must not spawn a worker
        pre_init_end = bridge.end_task_session("task_no_worker_yet", timeout=2.0)
        assert pre_init_end == {
            "ended": True,
            "cancelledOperations": 0,
            "invalidatedStateTokens": 0,
        }
        pre_status = bridge.cache_status()
        assert pre_status["status"] == "unknown"
        assert pre_status["cached"] is False
        assert pre_status["live"] is False
        assert not state_file.exists()

        # Explicitly initialize worker before the 265-event loop and record PID/generation
        init_list = bridge.list_windows(task_session_id="task_init")
        assert len(init_list["windows"]) == 2
        init_status = bridge.cache_status()
        assert init_status["live"] is True
        assert init_status["workerGeneration"] == 1
        init_pid = init_status["worker"]["pid"]
        init_instance = init_status["workerInstance"]

        for i in range(265):
            end_res = bridge.end_task_session(f"task_seq_{i}", timeout=2.0)
            assert end_res["ended"] is True

        # Verify all 265 actual session.end fixture events occurred on the same healthy worker
        recorded_ends = [
            e for e in _read_state_events(state_file) if e["event"] == "session_end_received"
        ]
        assert len(recorded_ends) == 265

        status = bridge.cache_status()
        assert status["live"] is True
        assert status["workerGeneration"] == init_status["workerGeneration"] == 1
        assert status["workerInstance"] == init_instance
        assert status["worker"]["pid"] == init_pid
    finally:
        bridge.close()

    # Unconsumed event flood (>256 events for a single pending request) must still fail closed
    cmd_flood, _ = fake_worker_factory("event_flood_on_list", "flood_events.jsonl")
    bridge_flood = NativeBridge(dev_executable=cmd_flood)
    try:
        with pytest.raises(NativeBridgeError) as exc_flood:
            bridge_flood.list_windows(task_session_id="t_flood", timeout=2.0)
        assert exc_flood.value.code == "agent_computer_event_limit"
        assert bridge_flood.cache_status()["live"] is False
    finally:
        bridge_flood.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_interleaved_event_accounting_idempotent_across_response_timeout_and_close(
    fake_worker_factory,
) -> None:
    cmd, _ = fake_worker_factory("normal", "event_accounting.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        session = bridge.client.ensure_session()
        pending_a = session._allocate_pending("action.prepare", operation_id="op_a")
        pending_b = session._allocate_pending("windows.list")

        session._handle_incoming_event(
            {
                "v": 1,
                "event": "action.stage",
                "requestId": pending_a.request_id,
                "data": {"stage": "validate"},
            }
        )
        session._handle_incoming_event(
            {
                "v": 1,
                "event": "action.ready",
                "requestId": pending_a.request_id,
                "data": {"prepareTicket": "prep_a", "inputLockHeld": True},
            }
        )
        for _ in range(2):
            session._handle_incoming_event(
                {
                    "v": 1,
                    "event": "action.stage",
                    "requestId": pending_b.request_id,
                    "data": {"stage": "validate"},
                }
            )

        assert session._unconsumed_event_count == 4
        assert pending_a.unconsumed_events == 2
        assert pending_b.unconsumed_events == 2

        # Response handling consumes pending_a once (4 -> 2)
        session._handle_incoming_response(
            {"v": 1, "id": pending_a.request_id, "result": {}}
        )
        assert session._unconsumed_event_count == 2
        assert pending_a.events_consumed is True
        assert pending_a.unconsumed_events == 0
        # ready_event and diagnostic history on pending_a must remain intact
        assert pending_a.ready_event == {"prepareTicket": "prep_a", "inputLockHeld": True}
        assert len(pending_a.events) == 2

        # Repeated consumption at request completion / cleanup must be idempotent and never subtract pending_b's balance
        with session._state_lock:
            session._consume_pending_events_locked(pending_a)
            session._consume_pending_events_locked(pending_a)
        assert session._unconsumed_event_count == 2
        assert pending_b.unconsumed_events == 2
        assert pending_b.events_consumed is False

        # Allocate pending_c with 3 events (2 + 3 = 5), then simulate timeout cleanup and repeated consume
        pending_c = session._allocate_pending("windows.list")
        for _ in range(3):
            session._handle_incoming_event(
                {
                    "v": 1,
                    "event": "action.stage",
                    "requestId": pending_c.request_id,
                    "data": {"stage": "validate"},
                }
            )
        assert session._unconsumed_event_count == 5
        with session._state_lock:
            session._pending.pop(pending_c.correlation_key, None)
            session._consume_pending_events_locked(pending_c)
            session._consume_pending_events_locked(pending_c)
        assert session._unconsumed_event_count == 2
        assert pending_c.events_consumed is True
        assert pending_c.unconsumed_events == 0

        # Closing session consumes remaining pending_b cleanly and subsequent consume stays at 0
        session.close()
        assert session._unconsumed_event_count == 0
        assert pending_b.events_consumed is True
        assert pending_b.unconsumed_events == 0
        with session._state_lock:
            session._consume_pending_events_locked(pending_b)
        assert session._unconsumed_event_count == 0
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_stalled_stdin_write_respects_deadline_and_closes_job(
    fake_worker_factory,
) -> None:
    cmd, _ = fake_worker_factory("stall_stdin_after_init", "stall_stdin.jsonl")
    client = NativeWorkerClient(dev_executable=cmd)
    try:
        session = client.ensure_session()
        start = time.monotonic()
        with pytest.raises(NativeBridgeError) as exc_info:
            session.request(
                "windows.list",
                {"titleQuery": "X" * 60000},
                timeout=0.15,
            )
        elapsed = time.monotonic() - start
        assert exc_info.value.code == "agent_computer_timeout"
        assert elapsed < 1.5

        close_start = time.monotonic()
        client.close()
        assert (time.monotonic() - close_start) < 1.5
        assert session.process.poll() is not None
    finally:
        client.close()


# ============================================================================
# 3. Two-Phase Write State Machine (Prepare -> Ready -> on_ready -> Commit)
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_prepare_denied_and_unsupported_actions_invoke_zero_commit(
    fake_worker_factory,
) -> None:
    cmd, state_file = fake_worker_factory("prepare_denied", "prep_denied.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_1")
        target_win = win_list["windows"][1]["window"]  # Explicitly select 2nd window, no windows[0] assumption
        obs = bridge.get_state(task_session_id="task_1", window=target_win)

        on_ready_called = False

        def _on_ready(_ready: dict[str, Any]) -> None:
            nonlocal on_ready_called
            on_ready_called = True

        with pytest.raises(NativeBridgeRejected) as exc_info:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_denied_1",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={
                    "type": "click",
                    "target": {"coordinateSpace": "window_physical_px", "x": 50, "y": 50},
                },
                on_ready=_on_ready,
            )

        err = exc_info.value
        assert err.code == "agent_computer_window_occluded"
        assert err.pre_dispatch is True
        assert err.commit_attempted is False
        assert err.dispatched is False
        assert not on_ready_called

        # Unsupported action (`drag` and `launch_app`) must be rejected honestly before commit
        with pytest.raises(NativeBridgeRejected) as exc_unsup:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_drag_1",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={
                    "type": "drag",
                    "from": {"coordinateSpace": "window_physical_px", "x": 10, "y": 10},
                    "to": {"coordinateSpace": "window_physical_px", "x": 40, "y": 40},
                },
                on_ready=_on_ready,
            )
        assert exc_unsup.value.code == "agent_computer_action_unsupported"
        assert exc_unsup.value.pre_dispatch is True
        assert exc_unsup.value.commit_attempted is False

        events = _read_state_events(state_file)
        assert not any(e["event"] == "commit_received" for e in events)
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_on_ready_ordering_and_single_fixed_commit(fake_worker_factory) -> None:
    cmd, state_file = fake_worker_factory("normal", "ready_order.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_1")
        target_win = win_list["windows"][0]["window"]
        obs = bridge.get_state(task_session_id="task_1", window=target_win)

        # Ensure NativeBridge did NOT durably store base64 screenshot in its task metadata
        assert "dataBase64" in obs["screenshots"][0]
        tok_meta = bridge._tasks["task_1"].tokens[obs["stateToken"]]
        assert "screenshots" not in tok_meta

        events_at_on_ready: list[str] = []

        def _on_ready(ready_info: dict[str, Any]) -> None:
            # Verify worker has already emitted prepare_ready, and commit has NOT been received yet
            current_events = [e["event"] for e in _read_state_events(state_file)]
            events_at_on_ready.extend(current_events)
            assert "prepare_ready" in current_events
            assert "commit_received" not in current_events
            assert ready_info["inputLockHeld"] is True
            assert ready_info["mutation"] == "none"
            # Attempt to mutate the dict passed to on_ready; must not affect commit parameters
            ready_info["prepareTicket"] = "forged_ticket"

        commit_res = bridge.execute_action(
            task_session_id="task_1",
            operation_id="op_ok_1",
            window=target_win,
            expected_state_token=obs["stateToken"],
            action={
                "type": "type_text",
                "text": "测试电脑控制",
                "element": {"index": 0, "runtimeId": "42.1"},
            },
            authority_epoch=7,
            on_ready=_on_ready,
        )

        assert commit_res["status"] == "committed"
        assert commit_res["mutation"] == "delivered"
        assert commit_res["operationId"] == "op_ok_1"

        all_events = _read_state_events(state_file)
        commit_events = [e for e in all_events if e["event"] == "commit_received"]
        assert len(commit_events) == 1
        # Commit payload only contains fixed keys (taskSessionId, operationId, prepareTicket)
        assert commit_events[0]["paramsKeys"] == [
            "operationId",
            "prepareTicket",
            "taskSessionId",
        ]

        # Re-invoking the same operation_id after commit must be rejected without calling worker again
        with pytest.raises(NativeBridgeError) as exc_replay:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_ok_1",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={
                    "type": "type_text",
                    "text": "测试电脑控制",
                    "element": {"index": 0, "runtimeId": "42.1"},
                },
                authority_epoch=7,
                on_ready=_on_ready,
            )
        assert exc_replay.value.code == "agent_computer_commit_already_attempted"
        assert exc_replay.value.commit_attempted is True
        assert exc_replay.value.pre_dispatch is False
        assert len([e for e in _read_state_events(state_file) if e["event"] == "commit_received"]) == 1
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_cancel_or_expiry_during_on_ready_blocks_commit_before_first_byte(
    fake_worker_factory,
) -> None:
    cmd, state_file = fake_worker_factory("normal", "cancel_in_on_ready.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_1")
        target_win = win_list["windows"][0]["window"]
        obs = bridge.get_state(task_session_id="task_1", window=target_win)

        # Case 1: on_ready sets cancel flag True and returns normally -> MUST NOT send action.commit!
        cancel_flag = threading.Event()

        def _ready_sets_cancel(_info: dict[str, Any]) -> None:
            cancel_flag.set()

        with pytest.raises(NativeBridgeError) as exc_cancel:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_cancel_after_ready",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_ready_sets_cancel,
                cancelled=cancel_flag.is_set,
            )
        err1 = exc_cancel.value
        assert not isinstance(err1, NativeBridgeRejected)
        assert err1.code == "agent_cancelled"
        assert err1.commit_attempted is False
        assert err1.dispatched is True
        assert err1.pre_dispatch is False
        assert err1.outcome == "unknown"

        # Case 2: ticket expires while inside on_ready -> MUST NOT send action.commit!
        obs2 = bridge.get_state(task_session_id="task_1", window=target_win)

        def _slow_on_ready(_info: dict[str, Any]) -> None:
            time.sleep(0.12)

        with pytest.raises(NativeBridgeError) as exc_exp:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_expire_after_ready",
                window=target_win,
                expected_state_token=obs2["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                constraints={"readyTimeoutMs": 60},
                on_ready=_slow_on_ready,
            )
        err2 = exc_exp.value
        assert not isinstance(err2, NativeBridgeRejected)
        assert err2.code == "agent_computer_ready_expired"
        assert err2.commit_attempted is False
        assert err2.dispatched is True
        assert err2.pre_dispatch is False

        events = _read_state_events(state_file)
        assert not any(e["event"] == "commit_received" for e in events)
        cancel_ops = [e["operationId"] for e in events if e["event"] == "cancel_received"]
        assert "op_cancel_after_ready" in cancel_ops
        assert "op_expire_after_ready" in cancel_ops
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
@pytest.mark.parametrize(
    "tamper_mode",
    ["tamper_ready_fp", "tamper_ready_window", "tamper_ready_mutation"],
)
def test_ready_tamper_rejected_before_on_ready_and_commit(
    fake_worker_factory, tamper_mode: str
) -> None:
    cmd, state_file = fake_worker_factory(tamper_mode, f"{tamper_mode}.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_1")
        target_win = win_list["windows"][0]["window"]
        obs = bridge.get_state(task_session_id="task_1", window=target_win)

        on_ready_called = False

        def _on_ready(_ready: dict[str, Any]) -> None:
            nonlocal on_ready_called
            on_ready_called = True

        with pytest.raises(NativeBridgeRejected) as exc_info:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id=f"op_{tamper_mode}",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "focus_window"},
                on_ready=_on_ready,
            )

        assert exc_info.value.code in {
            "agent_computer_ready_tampered",
            "agent_computer_ready_invalid",
        }
        assert exc_info.value.pre_dispatch is True
        assert exc_info.value.commit_attempted is False
        assert not on_ready_called

        events = _read_state_events(state_file)
        assert not any(e["event"] == "commit_received" for e in events)
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_revoked_authority_in_on_ready_and_uncertain_ledger_callback(
    fake_worker_factory,
) -> None:
    cmd, state_file = fake_worker_factory("normal", "revoked_auth.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_1")
        target_win = win_list["windows"][0]["window"]
        obs = bridge.get_state(task_session_id="task_1", window=target_win)

        # Case 1: Authority revoked while waiting for lock -> on_ready raises AgentToolRejected("agent_permission_denied")
        def _revoke_on_ready(_ready: dict[str, Any]) -> None:
            raise AgentToolRejected("agent_permission_denied")

        with pytest.raises(NativeBridgeRejected) as exc_revoked:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_revoked_1",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "focus_window"},
                on_ready=_revoke_on_ready,
            )
        assert exc_revoked.value.code == "agent_permission_denied"
        assert exc_revoked.value.pre_dispatch is True
        assert exc_revoked.value.commit_attempted is False
        assert exc_revoked.value.dispatched is False

        # Case 2: Host ledger callback raises an unexpected RuntimeError -> must cancel native ticket
        # AND mark callback_uncertain=True / pre_dispatch=False (never blindly labeled safe retry!)
        def _failing_ledger_callback(_ready: dict[str, Any]) -> None:
            raise RuntimeError("disk I/O failure while persisting dispatched")

        with pytest.raises(NativeBridgeError) as exc_uncertain:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id="op_ledger_err_1",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "focus_window"},
                on_ready=_failing_ledger_callback,
            )
        err = exc_uncertain.value
        assert not isinstance(err, NativeBridgeRejected)
        assert err.code == "agent_computer_callback_uncertain"
        assert err.callback_uncertain is True
        assert err.pre_dispatch is False
        assert err.commit_attempted is False
        assert err.outcome == "unknown"

        # Verify native ticket was cancelled and zero commits were sent
        events = _read_state_events(state_file)
        assert not any(e["event"] == "commit_received" for e in events)
        cancel_ops = [e["operationId"] for e in events if e["event"] == "cancel_received"]
        assert "op_revoked_1" in cancel_ops
        assert "op_ledger_err_1" in cancel_ops
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("commit_eof_no_reply", "agent_computer_process_disconnected"),
        ("commit_error_mutation_none", "agent_computer_focus_failed"),
        ("commit_error_input_failed", "agent_computer_input_failed"),
    ],
)
def test_post_commit_failure_stays_unknown_and_never_retries(
    fake_worker_factory, mode: str, expected_code: str
) -> None:
    cmd, state_file = fake_worker_factory(mode, f"post_commit_{mode}.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_1")
        target_win = win_list["windows"][0]["window"]
        obs = bridge.get_state(task_session_id="task_1", window=target_win)

        dispatched_recorded = False

        def _on_ready(_ready: dict[str, Any]) -> None:
            nonlocal dispatched_recorded
            dispatched_recorded = True

        with pytest.raises(NativeBridgeError) as exc_info:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id=f"op_{mode}",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_on_ready,
            )

        err = exc_info.value
        assert dispatched_recorded is True
        assert not isinstance(err, NativeBridgeRejected)
        assert err.code == expected_code
        assert err.commit_attempted is True
        assert err.dispatched is True
        assert err.pre_dispatch is False
        assert err.outcome == "unknown"
        assert err.diagnostics["commit_attempted"] is True
        assert err.diagnostics["dispatched"] is True

        # Even if worker generation is recreated, re-running the same operation_id must be blocked
        cmd_normal, _ = fake_worker_factory("normal", f"post_commit_recreate_{mode}.jsonl")
        bridge._client._dev_executable = cmd_normal
        bridge._client.retire_generation()

        with pytest.raises(NativeBridgeError) as exc_retry:
            bridge.execute_action(
                task_session_id="task_1",
                operation_id=f"op_{mode}",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_on_ready,
            )
        assert exc_retry.value.code == "agent_computer_commit_already_attempted"
        assert exc_retry.value.commit_attempted is True
        assert exc_retry.value.pre_dispatch is False
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge subprocess tests")
def test_control_cancellation_during_prepare_and_task_session_isolation(
    fake_worker_factory,
) -> None:
    cmd, state_file = fake_worker_factory("wait_lock_slow", "cancel_lock.jsonl")
    bridge = NativeBridge(dev_executable=cmd)
    try:
        win_list = bridge.list_windows(task_session_id="task_a")
        target_win = win_list["windows"][0]["window"]
        obs_a = bridge.get_state(task_session_id="task_a", window=target_win)
        bridge.get_state(task_session_id="task_b", window=target_win)

        # Cross-task token misuse must be rejected before commit
        with pytest.raises(NativeBridgeRejected) as exc_cross:
            bridge.execute_action(
                task_session_id="task_b",
                operation_id="op_cross_task",
                window=target_win,
                expected_state_token=obs_a["stateToken"],
                action={"type": "focus_window"},
                on_ready=lambda _: None,
            )
        assert exc_cross.value.code == "agent_computer_task_conflict"
        assert exc_cross.value.pre_dispatch is True

        # Start an action in task_a that blocks in waiting_lock, then cancel via control path
        cancel_flag = threading.Event()
        threading.Timer(0.15, cancel_flag.set).start()

        with pytest.raises(NativeBridgeRejected) as exc_cancel:
            bridge.execute_action(
                task_session_id="task_a",
                operation_id="op_wait_cancel",
                window=target_win,
                expected_state_token=obs_a["stateToken"],
                action={"type": "focus_window"},
                on_ready=lambda _: None,
                cancelled=cancel_flag.is_set,
            )
        assert exc_cancel.value.code == "agent_cancelled"
        assert exc_cancel.value.pre_dispatch is True
        assert exc_cancel.value.commit_attempted is False

        # Ending task_a session retires task_a tokens without closing apps or breaking task_b
        end_res = bridge.end_task_session("task_a")
        assert end_res["ended"] is True
        assert end_res["invalidatedStateTokens"] >= 1
        assert bridge.resolve_task_window("task_a", hwnd=target_win["hwnd"]) is None
        assert bridge.resolve_task_window("task_b", hwnd=target_win["hwnd"]) is not None
    finally:
        bridge.close()


def test_node_ownership_injection_scoping() -> None:
    class DummyOwner:
        pass

    owner_1 = DummyOwner()
    owner_2 = DummyOwner()

    with pytest.raises(ValueError):
        bind_node_native_bridge(None)

    b1 = bind_node_native_bridge(owner_1)
    b1_again = bind_node_native_bridge(owner_1)
    b2 = bind_node_native_bridge(owner_2)

    assert b1 is b1_again
    assert b1 is not b2
    b1.close()
    b2.close()
