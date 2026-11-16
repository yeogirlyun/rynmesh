"""Acceptance & unit tests for S11-I18: Clear reasons for computer control errors and observations."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from rynmesh.agent_runtime import image_capabilities
from rynmesh.agent_runtime.computer import protocol as comp_proto
from rynmesh.agent_runtime.computer.coordinator import NativeBridge
from rynmesh.agent_runtime.computer.protocol import (
    ALLOWLISTED_BRIDGE_ERROR_CODES,
    EMPTY_SHA256,
    VALID_DIAGNOSTIC_REASONS,
    VALID_ERROR_CODES,
    NativeBridgeError,
    NativeBridgeRejected,
    bridge_error_from_native,
    compute_action_fingerprint,
    validate_action_commit_result,
    validate_observation,
    validate_screenshot_artifact,
    validate_window_entry,
    validate_windows_list_result,
)
from rynmesh.agent_runtime.native_computer import NativeComputerFacade
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def _sample_window_ref(worker_instance: str = "wrk_test1") -> dict[str, Any]:
    return {
        "workerInstance": worker_instance,
        "windowKey": "win_1001_g1",
        "hwnd": "1001",
        "pid": 4242,
        "processStarted": "133000000000000000",
        "generation": 1,
        "appId": "notepad.exe",
    }


def _sample_rect(l: int = 100, t: int = 100, r: int = 500, b: int = 400) -> dict[str, int]:
    return {
        "left": l,
        "top": t,
        "right": r,
        "bottom": b,
        "width": r - l,
        "height": b - t,
    }


def _sample_geometry() -> dict[str, Any]:
    return {
        "windowRect": _sample_rect(100, 100, 500, 400),
        "clientRect": _sample_rect(108, 130, 492, 392),
        "screenOrigin": [100, 100],
        "dpi": 96,
        "scaleFactor": 1.0,
    }


def _sample_observation(
    *,
    worker_instance: str = "wrk_test1",
    task_id: str = "task_1",
    state_token: str = "st_fresh_1",
    captured_at: str = "2026-11-14T10:00:00.123Z",
    element_name: str = "OK",
    screenshot_reason: str | None = None,
    accessibility_reason: str | None = None,
    accessibility_hresult: str | None = None,
) -> dict[str, Any]:
    shot: dict[str, Any] = {
        "id": "art_1",
        "status": "unavailable",
        "captureMethod": "none",
        "mimeType": "image/png",
        "width": 0,
        "height": 0,
        "originX": 100,
        "originY": 100,
        "zIndex": 0,
        "dpi": 96,
        "scaleFactor": 1.0,
        "sha256": EMPTY_SHA256,
        "minimized": False,
        "occluded": False,
        "captureProtected": False,
    }
    if screenshot_reason is not None:
        shot["reason"] = screenshot_reason

    acc: dict[str, Any] = {
        "status": "unavailable" if accessibility_reason else "ok",
        "treeHash": EMPTY_SHA256,
        "truncated": False,
        "selectedElementIndices": [],
        "elements": []
        if accessibility_reason
        else [
            {
                "index": 0,
                "runtimeId": "42.1",
                "name": element_name,
                "controlType": "button",
                "password": False,
                "enabled": True,
                "offscreen": False,
                "focusable": True,
                "focused": False,
                "bounds": _sample_rect(120, 150, 200, 180),
                "depth": 1,
                "patterns": ["invoke"],
            }
        ],
    }
    if accessibility_reason is not None:
        acc["reason"] = accessibility_reason
    if accessibility_hresult is not None:
        acc["hresultHex"] = accessibility_hresult

    return {
        "taskSessionId": task_id,
        "window": _sample_window_ref(worker_instance),
        "observationId": "obs_1",
        "stateToken": state_token,
        "capturedAt": captured_at,
        "geometry": _sample_geometry(),
        "screenshots": [shot],
        "accessibility": acc,
        "foreground": {
            "isTargetWindow": True,
            "isRelatedToTarget": True,
            "foregroundHwnd": "1001",
            "foregroundPid": 4242,
        },
    }


def _make_facade_with_fake_bridge(fake_bridge: Any, store: Any = None, local_tools: Any = None) -> NativeComputerFacade:
    owner = SimpleNamespace(_ryn_owner_id="owner_s11_i18", store=store)
    facade = NativeComputerFacade(owner, local_tools=local_tools, bridge=fake_bridge)
    return facade


def test_t2_timestamps_parseable_by_datetime_fromisoformat() -> None:
    """T2: RFC3339 timestamps emitted by windows.list / state.get parse cleanly with datetime.fromisoformat."""
    ts = "2026-11-14T10:00:00.123Z"
    win_list = validate_windows_list_result(
        {"capturedAt": ts, "windows": []},
        expected_worker_instance="wrk_test1",
    )
    parsed_list_dt = datetime.fromisoformat(win_list["capturedAt"])
    assert parsed_list_dt.year == 2026 and parsed_list_dt.month == 11 and parsed_list_dt.day == 14
    assert parsed_list_dt.microsecond == 123_000

    obs = validate_observation(
        _sample_observation(captured_at=ts, screenshot_reason="disabled"),
        expected_worker_instance="wrk_test1",
    )
    parsed_obs_dt = datetime.fromisoformat(obs["capturedAt"])
    assert parsed_obs_dt == parsed_list_dt


def test_t3_uipi_error_translated_to_chinese_prompt_and_retryable_preserved() -> None:
    """T3: UIPI error preserves retryable=False and translates to clear Chinese administrator-window guidance."""
    err_wire = {
        "code": "input_blocked_by_uipi",
        "stage": "input_started",
        "mutation": "possible",
        "requestId": "req_uipi",
        "operationId": "op_uipi",
        "retryable": False,
        "message": "UNTRUSTED_NATIVE_MESSAGE_MUST_NOT_LEAK",
        "details": {
            "win32Error": 5,
            "internalMutation": "focus_only",
            "reason": "send_input_short_count",
        },
    }
    bridge_err = bridge_error_from_native(
        err_wire,
        phase="commit_wait",
        method="action.commit",
        commit_attempted=True,
        dispatched=True,
    )
    assert bridge_err.code == "agent_computer_input_blocked_by_uipi"
    assert bridge_err.retryable is False
    assert bridge_err.diagnostics.get("retryable") is False

    facade = _make_facade_with_fake_bridge(MagicMock())
    with pytest.raises(AgentToolRejected) as exc_info:
        facade._translate_bridge_exception(bridge_err, is_write=True)
    rej = exc_info.value
    assert str(rej) == "agent_computer_input_blocked_by_uipi"
    assert rej.correction is not None
    assert "目标是管理员窗口，Ryn 无权操作" in rej.correction
    assert "无法输入" in rej.correction
    assert "UNTRUSTED_NATIVE_MESSAGE_MUST_NOT_LEAK" not in rej.correction


def test_t4_post_observation_error_passthrough_to_tool_result() -> None:
    """T4: postObservationError on committed action is accepted and surfaced in structuredContent."""
    wref = _sample_window_ref()
    action = {"type": "focus_window", "restoreIfMinimized": True}
    fp = compute_action_fingerprint("task_1", wref, "st_old", action)

    validated = validate_action_commit_result(
        {
            "status": "committed",
            "taskSessionId": "task_1",
            "operationId": "op_commit_1",
            "actionFingerprint": fp,
            "mutation": "delivered",
            "focusRecovery": {
                "attempted": True,
                "succeeded": True,
                "methodUsed": "set_foreground_window",
                "durationMs": 12,
                "finalForegroundVerified": True,
            },
            "postObservationError": "observation_unavailable",
        },
        expected_worker_instance="wrk_test1",
        expected_task_session_id="task_1",
        expected_operation_id="op_commit_1",
        expected_action_fingerprint=fp,
        expected_window=wref,
    )
    assert validated["status"] == "committed"
    assert validated["postObservationError"] == "observation_unavailable"

    fake_client = MagicMock()
    fake_client.active_session_info.return_value = (1, "wrk_test1", 9999)
    fake_bridge = MagicMock()
    fake_bridge._client = fake_client
    fake_bridge.execute_action.return_value = validated

    facade = _make_facade_with_fake_bridge(fake_bridge)
    init_obs = validate_observation(
        _sample_observation(state_token="st_old", screenshot_reason="disabled"),
        expected_worker_instance="wrk_test1",
    )
    facade._ingest_observation(
        task_id="task_1",
        operation_id="op_read_init",
        obs=init_obs,
        window_meta={"title": "Notepad", "className": "Notepad", "processName": "notepad.exe"},
        context={"task_id": "task_1"},
    )
    binding = {
        "server_id": "computer-use",
        "server_name": "Computer Use",
        "original_tool_name": "computer_act_window",
        "model_tool_name": "mcp_computer_use_computer_act_window",
        "computer_access": True,
    }
    preview = facade.preview_mcp_write(
        "computer_act_window",
        {"hwnd": 1001, "action": "focus"},
        binding=binding,
        policy={"desktop_access": True, "workspace_root": ""},
        server={"id": "computer-use", "name": "Computer Use"},
        tool={"name": "computer_act_window"},
        context={"task_id": "task_1"},
    )
    res = facade.execute_mcp_write(
        "computer_act_window",
        {"hwnd": 1001, "action": "focus"},
        binding=binding,
        operation_id="op_commit_1",
        approved_preview=preview,
        context={"task_id": "task_1"},
        cancelled=lambda: False,
        on_ready_host_gate=lambda _r: None,
    )
    assert res["structuredContent"]["status"] == "committed"
    assert res["structuredContent"]["post_observation_error"] == "observation_unavailable"


def test_t5_oversized_5000_char_element_name_in_post_observation_keeps_commit_confirmed() -> None:
    """T5: 5000-char element name in post-observation discards only observation, sets postObservationError, and records commit as confirmed."""
    wref = _sample_window_ref()
    action = {"type": "focus_window", "restoreIfMinimized": True}
    fp = compute_action_fingerprint("task_1", wref, "st_old", action)
    bad_obs = _sample_observation(
        state_token="st_after",
        element_name="X" * 5000,
    )

    fake_client = MagicMock()
    session = SimpleNamespace(
        generation=1,
        worker_instance="wrk_test1",
        worker_pid=4321,
        supported_actions=frozenset({"focus_window", "click"}),
        handshake={},
    )
    fake_client.ensure_started.return_value = session
    fake_client.ensure_session.return_value = session
    fake_client.active_session_info.return_value = (1, "wrk_test1", 4321)

    ready_payload = {
        "taskSessionId": "task_1",
        "operationId": "op_5000",
        "prepareTicket": "pt_5000",
        "window": copy.deepcopy(wref),
        "expectedStateToken": "st_old",
        "actionKind": "focus_window",
        "actionFingerprint": fp,
        "inputLockHeld": True,
        "lockWasAbandoned": False,
        "expiresInMs": 1500,
        "mutation": "none",
    }
    prepare_res = {
        **ready_payload,
        "status": "ready",
        "resolvedTarget": {"windowHwnd": "1001"},
    }
    commit_res_raw = {
        "status": "committed",
        "taskSessionId": "task_1",
        "operationId": "op_5000",
        "actionFingerprint": fp,
        "mutation": "delivered",
        "focusRecovery": {
            "attempted": True,
            "succeeded": True,
            "methodUsed": "set_foreground_window",
            "durationMs": 5,
            "finalForegroundVerified": True,
        },
        "observation": bad_obs,
    }

    def _session_request(method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        if method == "state.get":
            return _sample_observation(state_token="st_old", screenshot_reason="disabled"), None
        if method == "action.prepare":
            return prepare_res, SimpleNamespace(ready_event=ready_payload)
        if method == "action.commit":
            cb = kwargs.get("on_before_write")
            if callable(cb):
                cb()
            return commit_res_raw, None
        raise AssertionError(f"unexpected method {method}")

    session.is_usable = lambda: True
    session.request = _session_request

    bridge = NativeBridge(owner_id="owner_t5")
    bridge._client = fake_client
    bridge.get_state(task_session_id="task_1", window=wref, include_screenshot=False)
    out = bridge.execute_action(
        task_session_id="task_1",
        operation_id="op_5000",
        window=wref,
        expected_state_token="st_old",
        action=action,
        on_ready=lambda _r: True,
    )
    assert out["status"] == "committed"
    assert "observation" not in out or out["observation"] is None
    assert out.get("postObservationError") == "agent_computer_protocol_invalid"
    recorded = bridge._operations["op_5000"]
    assert recorded.status == "confirmed"


def test_t6_old_descriptor_after_restart_reports_expired_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """T6: Sealed descriptor from previous host instance raises native_observation_expired_after_restart, while same-process tampering raises native_descriptor_tampered_after_seal."""
    wref = {
        "worker_instance_id": "wrk_test1",
        "window_key": "win_1001_g1",
        "hwnd": "0x3e9",
        "pid": 4242,
        "creation_time": 133000000000000000,
        "generation": 1,
    }
    dummy_sha = hashlib.sha256(b"png_bytes").hexdigest()
    sealed = image_capabilities._issue_native_observation_descriptor(
        task_id="task_1",
        operation_id="op_img_1",
        observation_id="obs_1",
        state_token="st_1",
        window_ref=wref,
        source_sha256=dummy_sha,
        source_width=100,
        source_height=80,
        source_bytes=120,
        dpi=96,
        dpi_scale=1.0,
        window_rect={"x": 100, "y": 100, "width": 400, "height": 300},
        client_rect={"x": 100, "y": 100, "width": 400, "height": 300},
        physical_origin=[0, 0],
        screen_origin=[100, 100],
    )
    assert "host_instance_id" in sealed

    # Same-process mutation still reports tampered_after_seal
    tampered = copy.deepcopy(sealed)
    tampered["dpi"] = 144
    with pytest.raises(ValueError, match="native_descriptor_tampered_after_seal"):
        image_capabilities.validate_native_observation_descriptor(
            tampered,
            expected_task_id="task_1",
            expected_operation_id="op_img_1",
            expected_observation_id="obs_1",
            current_state_token="st_1",
            expected_window_ref=wref,
        )

    # Simulated host process restart: new process instance ID AND new HMAC secret
    monkeypatch.setattr(image_capabilities, "_HOST_PROCESS_INSTANCE_ID", "restarted_host_instance_999")
    monkeypatch.setattr(image_capabilities, "_HOST_NATIVE_SEAL_SECRET", os.urandom(32))
    with pytest.raises(ValueError, match="native_observation_expired_after_restart"):
        image_capabilities.validate_native_observation_descriptor(
            sealed,
            expected_task_id="task_1",
            expected_operation_id="op_img_1",
            expected_observation_id="obs_1",
            current_state_token="st_1",
            expected_window_ref=wref,
        )


def test_t7_authority_mismatches_report_specific_paused_goal_scope_permission_codes() -> None:
    """T7: Task pause, goal change, scope change, and permission revocation raise specific codes instead of generic agent_cancelled."""
    wref = _sample_window_ref()
    action = {"type": "focus_window", "restoreIfMinimized": True}
    fp = compute_action_fingerprint("task_1", wref, "st_1", action)
    approved_preview = {
        "window_ref": wref,
        "expected_state_token": "st_1",
        "action_fingerprint": fp,
        "native_action": action,
    }

    state_holder: dict[str, Any] = {
        "settings": {
            "enabled": True,
            "desktop_access": True,
            "permission_mode": "auto",
            "allowed_write_tools": ["computer_act_window"],
        },
        "tasks": {
            "task_1": {
                "id": "task_1",
                "status": "running",
                "pending_control": None,
                "control_revision": 1,
                "context_epoch": 0,
                "scope_revision": 2,
                "goal": {"revision": 3},
                "local_execution": {"desktop_access": True, "workspace_root": ""},
            }
        },
    }

    class _FakeStore:
        def inspect(self, fn: Any) -> Any:
            return fn(state_holder)

    fake_client = MagicMock()
    fake_client.active_session_info.return_value = (1, "wrk_test1", 1111)
    fake_bridge = MagicMock()
    fake_bridge._client = fake_client
    facade = _make_facade_with_fake_bridge(fake_bridge, store=_FakeStore())
    obs = validate_observation(
        _sample_observation(state_token="st_1", screenshot_reason="disabled"),
        expected_worker_instance="wrk_test1",
    )
    facade._ingest_observation(
        task_id="task_1",
        operation_id="op_obs",
        obs=obs,
        window_meta={"title": "Doc"},
        context={"task_id": "task_1", "scope_revision": 2},
    )

    ctx = {
        "task_id": "task_1",
        "control_revision": 1,
        "context_epoch": 0,
        "scope_revision": 2,
        "goal_revision": 3,
    }
    stamp = facade.capture_trusted_write_authority(
        task_id="task_1",
        operation_id="op_w",
        tool_name="computer_act_window",
        arguments={"hwnd": 1001, "action": "focus"},
        context=ctx,
        approved_preview=approved_preview,
    )
    validated_ready = {
        "status": "ready",
        "mutation": "none",
        "inputLockHeld": True,
        "taskSessionId": "task_1",
        "operationId": "op_w",
        "expectedStateToken": "st_1",
        "actionFingerprint": fp,
        "window": wref,
    }

    # 1. Paused task -> agent_task_paused
    state_holder["tasks"]["task_1"]["status"] = "paused"
    with pytest.raises(AgentToolRejected) as e_pause:
        facade.verify_ready_write_authority(
            stamp, validated_ready=validated_ready, context=ctx, cancelled=lambda: False
        )
    assert str(e_pause.value) == "agent_task_paused"
    state_holder["tasks"]["task_1"]["status"] = "running"

    # 2. Goal revision changed -> agent_goal_changed
    state_holder["tasks"]["task_1"]["goal"]["revision"] = 4
    state_holder["tasks"]["task_1"]["control_revision"] = 2
    with pytest.raises(AgentToolRejected) as e_goal:
        facade.verify_ready_write_authority(
            stamp, validated_ready=validated_ready, context=ctx, cancelled=lambda: False
        )
    assert str(e_goal.value) == "agent_goal_changed"
    state_holder["tasks"]["task_1"]["goal"]["revision"] = 3
    state_holder["tasks"]["task_1"]["control_revision"] = 1

    # 3. Scope revision changed -> agent_scope_changed
    state_holder["tasks"]["task_1"]["scope_revision"] = 9
    state_holder["tasks"]["task_1"]["control_revision"] = 2
    with pytest.raises(AgentToolRejected) as e_scope:
        facade.verify_ready_write_authority(
            stamp, validated_ready=validated_ready, context=ctx, cancelled=lambda: False
        )
    assert str(e_scope.value) == "agent_scope_changed"
    state_holder["tasks"]["task_1"]["scope_revision"] = 2
    state_holder["tasks"]["task_1"]["control_revision"] = 1

    # 4. Permission revoked mid-flight -> agent_permission_revoked
    state_holder["settings"]["permission_mode"] = "manual"
    state_holder["tasks"]["task_1"]["pending_control"] = {"kind": "permissions"}
    with pytest.raises(AgentToolRejected) as e_perm:
        facade.verify_ready_write_authority(
            stamp, validated_ready=validated_ready, context=ctx, cancelled=lambda: False
        )
    assert str(e_perm.value) == "agent_permission_revoked"


def test_t8_elevated_window_zorder_and_monitor_exposed_in_windows_list() -> None:
    """T8: WindowEntry validates and exposes is_elevated=True, z_order, and monitor in computer_observe_windows."""
    raw_entry = {
        "window": _sample_window_ref(),
        "title": "管理员: Windows PowerShell",
        "className": "ConsoleWindowClass",
        "processName": "powershell.exe",
        "visible": True,
        "enabled": True,
        "minimized": False,
        "maximized": False,
        "foreground": True,
        "cloaked": False,
        "captureProtected": False,
        "bounds": _sample_rect(),
        "clientBounds": _sample_rect(),
        "dpi": 96,
        "zOrder": 0,
        "monitor": "\\\\.\\DISPLAY1",
        "isElevated": True,
    }
    validated_entry = validate_window_entry(raw_entry, expected_worker_instance="wrk_test1")
    assert validated_entry["isElevated"] is True
    assert validated_entry["zOrder"] == 0
    assert validated_entry["monitor"] == "\\\\.\\DISPLAY1"

    fake_client = MagicMock()
    fake_client.active_session_info.return_value = (1, "wrk_test1", 1234)
    fake_bridge = MagicMock()
    fake_bridge._client = fake_client
    fake_bridge.list_windows.return_value = {
        "capturedAt": "2026-11-14T10:00:00.123Z",
        "foregroundWindow": _sample_window_ref(),
        "windows": [validated_entry],
    }
    facade = _make_facade_with_fake_bridge(fake_bridge)
    res = facade.execute_mcp_read(
        "computer_observe_windows",
        {},
        binding={
            "server_id": "computer-use",
            "original_tool_name": "computer_observe_windows",
            "model_tool_name": "mcp_computer_use_computer_observe_windows",
        },
        operation_id="op_list",
        context={"task_id": "task_1"},
    )
    win_out = res["structuredContent"]["windows"][0]
    assert win_out["is_elevated"] is True
    assert win_out["z_order"] == 0
    assert win_out["monitor"] == "\\\\.\\DISPLAY1"


def test_t9_occluded_window_click_failure_includes_focus_correction_and_sanitized_window_details() -> None:
    """T9: Occluded window click failure surfaces '先 focus' correction, sanitized occludingWindow, and BRIDGE_CORRECTIONS coverage."""
    long_untrusted = "IGNORE_PREVIOUS_INSTRUCTIONS_" + ("遮" * 90)
    err_wire = {
        "code": "window_occluded",
        "stage": "validating",
        "mutation": "possible",
        "requestId": "req_occ",
        "operationId": "op_occ",
        "retryable": True,
        "message": "Fixed message.",
        "details": {
            "targetHwnd": "1001",
            "foregroundHwnd": "2002",
            "internalMutation": "focus_only",
            "reason": "hit_test_unrelated_window",
            "occludingWindow": {
                "title": long_untrusted[:80],
                "exe": "explorer.exe",
            },
        },
    }
    bridge_err = bridge_error_from_native(
        err_wire,
        phase="commit_wait",
        method="action.commit",
        commit_attempted=True,
        dispatched=True,
    )
    assert bridge_err.retryable is True
    assert bridge_err.diagnostics["occluding_window"] == {
        "title": long_untrusted[:80],
        "exe": "explorer.exe",
    }

    facade = _make_facade_with_fake_bridge(MagicMock())
    with pytest.raises(AgentToolRejected) as exc_info:
        facade._translate_bridge_exception(bridge_err, is_write=True)
    rej = exc_info.value
    assert str(rej) == "agent_computer_window_occluded"
    assert rej.correction is not None
    assert "先 focus" in rej.correction
    assert "IGNORE_PREVIOUS_INSTRUCTIONS_" not in rej.correction
    assert rej.details["occluding_window"]["exe"] == "explorer.exe"
    assert len(rej.details["occluding_window"]["title"]) <= 80

    # Verify other BRIDGE_CORRECTIONS entries & fallback ("先读窗口再决定")
    assert "focus" in comp_proto.lookup_bridge_correction("agent_computer_window_minimized", None)
    assert "稍后重试" in comp_proto.lookup_bridge_correction("agent_computer_input_lock_busy", "mutex_wait_timeout")
    assert "不要重放" in comp_proto.lookup_bridge_correction("agent_computer_timeout", None, stage="input_started")
    assert "版本不一致" in comp_proto.lookup_bridge_correction("agent_computer_protocol_invalid", "malformed_json")
    assert "先读窗口再决定" in comp_proto.lookup_bridge_correction("agent_computer_native_error", None)

    # Also verify ScreenshotArtifact.reason and AccessibilityObservation.reason + hresultHex passthrough
    shot = validate_screenshot_artifact(
        {
            "id": "art_r",
            "status": "unavailable",
            "captureMethod": "none",
            "mimeType": "image/png",
            "width": 0,
            "height": 0,
            "originX": 0,
            "originY": 0,
            "zIndex": 0,
            "dpi": 96,
            "scaleFactor": 1.0,
            "sha256": EMPTY_SHA256,
            "minimized": False,
            "occluded": False,
            "captureProtected": False,
            "reason": "cloaked",
        }
    )
    assert shot["reason"] == "cloaked"
    obs = validate_observation(
        _sample_observation(
            screenshot_reason="blank",
            accessibility_reason="root_failed",
            accessibility_hresult="0x80040201",
        ),
        expected_worker_instance="wrk_test1",
    )
    assert obs["screenshots"][0]["reason"] == "blank"
    assert obs["accessibility"]["reason"] == "root_failed"
    assert obs["accessibility"]["hresultHex"] == "0x80040201"


def test_t10_locked_desktop_prepare_raises_desktop_locked_with_uac_lock_prompt() -> None:
    """T10: Locked / UAC desktop during prepare raises pre_dispatch agent_computer_desktop_locked with clear Chinese guidance."""
    err_wire = {
        "code": "desktop_locked",
        "stage": "prepare",
        "mutation": "none",
        "requestId": "req_lock",
        "operationId": "op_lock",
        "retryable": False,
        "message": "Input desktop is locked or switched to secure desktop.",
        "details": {
            "internalMutation": "none",
            "reason": "desktop_locked",
        },
    }
    bridge_err = bridge_error_from_native(
        err_wire,
        phase="prepare",
        method="action.prepare",
        pre_dispatch_allowed=True,
    )
    assert isinstance(bridge_err, NativeBridgeRejected)
    assert bridge_err.code == "agent_computer_desktop_locked"
    assert bridge_err.pre_dispatch is True

    facade = _make_facade_with_fake_bridge(MagicMock())
    with pytest.raises(AgentToolRejected) as exc_info:
        facade._translate_bridge_exception(bridge_err, is_write=True)
    assert str(exc_info.value) == "agent_computer_desktop_locked"
    assert "屏幕已锁定或正在显示 UAC，暂时无法操作" in (exc_info.value.correction or "")


def test_t11_snake_case_waiting_lock_stage_in_protocol_diagnostics() -> None:
    """T11: waiting_lock stage and possible/none mutation stay snake_case end-to-end with retryable preserved."""
    err = NativeBridgeError(
        "agent_computer_input_lock_busy",
        phase="prepare",
        stage="waiting_lock",
        native_code="input_lock_busy",
        native_stage="waiting_lock",
        native_mutation="none",
        reason="mutex_wait_timeout",
        retryable=True,
    )
    assert err.diagnostics["stage"] == "waiting_lock"
    assert err.diagnostics["native_stage"] == "waiting_lock"
    assert err.diagnostics["native_mutation"] == "none"
    assert err.diagnostics["retryable"] is True


def test_t12_element_disabled_and_element_offscreen_codes_and_corrections() -> None:
    """T12: Disabled button reports element_disabled (retryable=False, '控件当前不可用') and offscreen element reports element_offscreen ('先滚动到可见')."""
    facade = _make_facade_with_fake_bridge(MagicMock())

    dis_err = bridge_error_from_native(
        {
            "code": "element_disabled",
            "stage": "validate",
            "mutation": "none",
            "requestId": "r1",
            "operationId": "op1",
            "retryable": False,
            "message": "Target UIA element is disabled.",
            "details": {"internalMutation": "none", "reason": "element_disabled"},
        },
        phase="prepare",
        method="action.prepare",
        pre_dispatch_allowed=True,
    )
    assert dis_err.retryable is False
    with pytest.raises(AgentToolRejected) as e_dis:
        facade._translate_bridge_exception(dis_err, is_write=True)
    assert str(e_dis.value) == "agent_computer_element_disabled"
    assert e_dis.value.retryable is False
    assert "控件当前不可用" in (e_dis.value.correction or "")

    off_err = bridge_error_from_native(
        {
            "code": "element_offscreen",
            "stage": "validating",
            "mutation": "possible",
            "requestId": "r2",
            "operationId": "op2",
            "retryable": True,
            "message": "Target UIA element is offscreen.",
            "details": {"internalMutation": "focus_only", "reason": "element_offscreen"},
        },
        phase="commit_wait",
        method="action.commit",
        commit_attempted=True,
        dispatched=True,
    )
    with pytest.raises(AgentToolRejected) as e_off:
        facade._translate_bridge_exception(off_err, is_write=True)
    assert str(e_off.value) == "agent_computer_element_offscreen"
    assert "先滚动到可见" in (e_off.value.correction or "")


def test_t13_cancelled_after_focus_distinguished_from_cancelled_during_input() -> None:
    """T13: Validating + focus_only + cancelled_while_ready rejects cleanly as no-input-delivered, whereas input_started + cancelled_during_execution reports unknown outcome."""
    facade = _make_facade_with_fake_bridge(MagicMock())

    ready_cancel = bridge_error_from_native(
        {
            "code": "cancelled",
            "stage": "validating",
            "mutation": "possible",
            "requestId": "rc1",
            "operationId": "opc1",
            "retryable": False,
            "message": "Cancelled.",
            "details": {
                "internalMutation": "focus_only",
                "reason": "cancelled_while_ready",
            },
        },
        phase="commit_wait",
        method="action.commit",
        commit_attempted=True,
        dispatched=True,
    )
    with pytest.raises(AgentToolRejected) as e_ready_cancel:
        facade._translate_bridge_exception(ready_cancel, is_write=True)
    assert str(e_ready_cancel.value) == "agent_cancelled"
    assert getattr(e_ready_cancel.value, "input_not_delivered", False) is True

    mid_input_cancel = bridge_error_from_native(
        {
            "code": "cancelled",
            "stage": "input_started",
            "mutation": "possible",
            "requestId": "rc2",
            "operationId": "opc2",
            "retryable": False,
            "message": "Cancelled.",
            "details": {
                "internalMutation": "unknown_or_partial",
                "reason": "cancelled_during_execution",
            },
        },
        phase="commit_wait",
        method="action.commit",
        commit_attempted=True,
        dispatched=True,
    )
    with pytest.raises(AgentError) as e_mid_cancel:
        facade._translate_bridge_exception(mid_input_cancel, is_write=True)
    assert not isinstance(e_mid_cancel.value, AgentToolRejected)
    assert str(e_mid_cancel.value) == "agent_write_outcome_unknown"


def test_t14_removed_diagnostic_reasons_absent_from_python_allowlists() -> None:
    """T14: Dead diagnostic reasons removed from Rust no longer appear in Python VALID_DIAGNOSTIC_REASONS and are rejected on the wire."""
    for dead_reason in ("safety_constraint_disabled", "mutex_abandoned", "parent_stdin_closed"):
        assert dead_reason not in VALID_DIAGNOSTIC_REASONS
        with pytest.raises(NativeBridgeError) as exc_info:
            comp_proto.validate_protocol_error(
                {
                    "code": "internal_error",
                    "stage": "protocol",
                    "mutation": "none",
                    "requestId": "req_dead",
                    "retryable": False,
                    "message": "err",
                    "details": {"reason": dead_reason},
                }
            )
        assert exc_info.value.code == "agent_computer_protocol_invalid"

    for added_reason in (
        "uia_element_not_found",
        "uia_com_call_failed",
        "uia_app_not_responding",
        "element_disabled",
        "element_offscreen",
        "desktop_locked",
    ):
        assert added_reason in VALID_DIAGNOSTIC_REASONS

    for added_code in ("desktop_locked", "element_disabled", "element_offscreen"):
        assert added_code in VALID_ERROR_CODES

    for added_bridge in (
        "agent_computer_desktop_locked",
        "agent_computer_element_disabled",
        "agent_computer_element_offscreen",
        "agent_task_paused",
        "agent_goal_changed",
        "agent_scope_changed",
        "agent_permission_revoked",
    ):
        assert added_bridge in ALLOWLISTED_BRIDGE_ERROR_CODES
