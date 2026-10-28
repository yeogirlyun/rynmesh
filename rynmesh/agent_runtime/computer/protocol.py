"""Strict wire-v1 protocol schemas, bounded JSONL reader, and fingerprint helpers for Ryn native computer control."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, BinaryIO

from ..store import AgentError, AgentToolRejected

WIRE_VERSION = 1
PROTOCOL_SEMVER = "1.0.0"

EXPECTED_WORKER_NAME = "ryn_computer_use"
EXPECTED_WORKER_VERSION = "0.1.0"

MAX_COMMAND_LINE_BYTES = 64 * 1024
MAX_RESPONSE_FRAME_BYTES = 16 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PENDING_REQUESTS = 64
MAX_EVENT_QUEUE = 256

MAX_ID_CHARS = 128
MAX_METHOD_CHARS = 64
MAX_WINDOWS_DEFAULT = 64
MAX_WINDOWS_CAP = 256
MAX_UIA_ELEMENTS_DEFAULT = 300
MAX_UIA_ELEMENTS_CAP = 1000
MAX_UIA_DEPTH_DEFAULT = 8
MAX_UIA_DEPTH_CAP = 16
MAX_TEXT_CHARS_DEFAULT = 200
MAX_TEXT_CHARS_CAP = 1000
MAX_SCREENSHOT_DIMENSION = 8192
MAX_SCREENSHOT_PIXELS = 33_554_432

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DECIMAL_U64_RE = re.compile(r"^[0-9]{1,20}$")
_HRESULT_HEX_RE = re.compile(r"^0x[0-9A-Fa-f]{8}$")

VALID_METHODS = frozenset({
    "initialize",
    "windows.list",
    "windows.get",
    "state.get",
    "apps.list",
    "apps.launch",
    "action.prepare",
    "action.commit",
    "action.cancel",
    "session.end",
    "shutdown",
})

VALID_EVENTS = frozenset({
    "action.ready",
    "action.stage",
    "session.retired",
})

PARITY_ACTION_KINDS = (
    "focus_window",
    "click",
    "double_click",
    "context_click",
    "move_pointer",
    "drag",
    "scroll",
    "type_text",
    "key_press",
    "key_chord",
    "uia_invoke",
    "uia_set_value",
    "uia_select",
    "uia_focus",
    "launch_app",
)

VALID_ACTION_KINDS = frozenset(PARITY_ACTION_KINDS)

DEFAULT_SUPPORTED_ACTIONS = (
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
)

DEFAULT_UNSUPPORTED_ACTIONS = (
    "drag",
    "launch_app",
)

VALID_ERROR_CODES = frozenset({
    "invalid_request",
    "unsupported_protocol_version",
    "not_initialized",
    "method_not_found",
    "invalid_params",
    "task_not_found",
    "task_conflict",
    "window_not_found",
    "window_stale",
    "window_minimized",
    "window_occluded",
    "window_capture_protected",
    "observation_unavailable",
    "uia_unavailable",
    "uia_element_stale",
    "state_token_invalid",
    "state_token_stale",
    "prepare_ticket_invalid",
    "prepare_ticket_expired",
    "duplicate_operation_id",
    "focus_failed",
    "input_lock_busy",
    "input_lock_quarantined",
    "input_blocked_by_uipi",
    "input_failed",
    "cancelled",
    "timeout",
    "unsupported",
    "internal_error",
})

VALID_ERROR_STAGES = frozenset({
    "protocol",
    "validate",
    "observe",
    "waiting_lock",
    "prepare",
    "ready",
    "activating",
    "validating",
    "input_started",
    "commit",
    "post_observe",
    "cancel",
    "session",
})

VALID_MUTATION_STATES = frozenset({
    "none",
    "possible",
    "delivered",
})

VALID_INTERNAL_MUTATION_DETAILS = frozenset({
    "none",
    "focus_only",
    "unknown_or_partial",
    "committed",
})

VALID_DIAGNOSTIC_REASONS = frozenset({
    "frame_too_large",
    "malformed_json",
    "duplicate_json_key",
    "missing_version",
    "version_mismatch",
    "missing_request_id",
    "missing_method",
    "already_initialized",
    "safety_constraint_disabled",
    "worker_instance_mismatch",
    "hwnd_parse_failed",
    "is_window_false",
    "pid_mismatch",
    "process_creation_time_mismatch",
    "generation_mismatch",
    "geometry_changed",
    "dpi_changed",
    "epoch_advanced",
    "token_task_mismatch",
    "token_window_mismatch",
    "point_out_of_bounds",
    "hit_test_unrelated_window",
    "foreground_verification_failed",
    "minimized_restore_disallowed",
    "mutex_wait_timeout",
    "mutex_abandoned",
    "mutex_release_failed",
    "desktop_identity_lookup_failed",
    "send_input_short_count",
    "uia_pattern_missing",
    "password_redacted",
    "direct_launch_disallowed",
    "cancelled_before_lock",
    "cancelled_while_ready",
    "cancelled_during_execution",
    "parent_stdin_closed",
})

VALID_CLIENT_PHASES = frozenset({
    "pre_dispatch",
    "prepare",
    "ready",
    "on_ready",
    "commit_send",
    "commit_wait",
    "post_dispatch",
    "read",
    "cancel",
    "session",
    "initialize",
    "shutdown",
})

ALLOWLISTED_BRIDGE_ERROR_CODES = frozenset({
    "agent_cancelled",
    "agent_time_limit",
    "agent_time_budget_exhausted",
    "agent_permission_denied",
    "agent_disabled",
    "agent_operation_id_required",
    "agent_operation_id_conflict",
    "agent_write_outcome_unknown",
    "agent_computer_windows_required",
    "agent_computer_arch_unsupported",
    "agent_computer_executable_not_found",
    "agent_computer_manifest_missing",
    "agent_computer_manifest_invalid",
    "agent_computer_path_invalid",
    "agent_computer_pe_invalid",
    "agent_computer_binary_invalid",
    "agent_computer_binary_tampered",
    "agent_computer_process_identity_mismatch",
    "agent_computer_config_conflict",
    "agent_computer_owner_conflict",
    "agent_computer_spawn_failed",
    "agent_computer_handshake_failed",
    "agent_computer_version_mismatch",
    "agent_computer_worker_identity_mismatch",
    "agent_computer_protocol_invalid",
    "agent_computer_frame_too_large",
    "agent_computer_request_too_large",
    "agent_computer_duplicate_json_key",
    "agent_computer_nonfinite_number",
    "agent_computer_foreign_response_id",
    "agent_computer_duplicate_response_id",
    "agent_computer_pending_limit",
    "agent_computer_event_limit",
    "agent_computer_process_disconnected",
    "agent_computer_write_failed",
    "agent_computer_timeout",
    "agent_computer_closed",
    "agent_computer_ready_invalid",
    "agent_computer_ready_expired",
    "agent_computer_ready_tampered",
    "agent_computer_callback_rejected",
    "agent_computer_callback_uncertain",
    "agent_computer_commit_already_attempted",
    "agent_computer_operation_in_progress",
    "agent_computer_invalid_params",
    "agent_computer_task_not_found",
    "agent_computer_task_conflict",
    "agent_computer_window_not_found",
    "agent_computer_window_stale",
    "agent_computer_window_minimized",
    "agent_computer_window_occluded",
    "agent_computer_window_capture_protected",
    "agent_computer_observation_unavailable",
    "agent_computer_uia_unavailable",
    "agent_computer_uia_element_stale",
    "agent_computer_state_token_invalid",
    "agent_computer_state_token_stale",
    "agent_computer_prepare_ticket_invalid",
    "agent_computer_prepare_ticket_expired",
    "agent_computer_duplicate_operation_id",
    "agent_computer_focus_failed",
    "agent_computer_input_lock_busy",
    "agent_computer_input_lock_quarantined",
    "agent_computer_input_blocked_by_uipi",
    "agent_computer_input_failed",
    "agent_computer_action_unsupported",
    "agent_computer_screenshot_invalid",
    "agent_computer_native_error",
})

NATIVE_CODE_TO_BRIDGE_CODE: dict[str, str] = {
    "invalid_request": "agent_computer_protocol_invalid",
    "unsupported_protocol_version": "agent_computer_version_mismatch",
    "not_initialized": "agent_computer_handshake_failed",
    "method_not_found": "agent_computer_protocol_invalid",
    "invalid_params": "agent_computer_invalid_params",
    "task_not_found": "agent_computer_task_not_found",
    "task_conflict": "agent_computer_task_conflict",
    "window_not_found": "agent_computer_window_not_found",
    "window_stale": "agent_computer_window_stale",
    "window_minimized": "agent_computer_window_minimized",
    "window_occluded": "agent_computer_window_occluded",
    "window_capture_protected": "agent_computer_window_capture_protected",
    "observation_unavailable": "agent_computer_observation_unavailable",
    "uia_unavailable": "agent_computer_uia_unavailable",
    "uia_element_stale": "agent_computer_uia_element_stale",
    "state_token_invalid": "agent_computer_state_token_invalid",
    "state_token_stale": "agent_computer_state_token_stale",
    "prepare_ticket_invalid": "agent_computer_prepare_ticket_invalid",
    "prepare_ticket_expired": "agent_computer_prepare_ticket_expired",
    "duplicate_operation_id": "agent_computer_duplicate_operation_id",
    "focus_failed": "agent_computer_focus_failed",
    "input_lock_busy": "agent_computer_input_lock_busy",
    "input_lock_quarantined": "agent_computer_input_lock_quarantined",
    "input_blocked_by_uipi": "agent_computer_input_blocked_by_uipi",
    "input_failed": "agent_computer_input_failed",
    "cancelled": "agent_cancelled",
    "timeout": "agent_computer_timeout",
    "unsupported": "agent_computer_action_unsupported",
    "internal_error": "agent_computer_native_error",
}


def sanitize_diagnostic(
    *,
    error_code: str,
    phase: str,
    stage: str | None = None,
    method: str | None = None,
    commit_attempted: bool = False,
    dispatched: bool = False,
    callback_uncertain: bool = False,
    native_code: str | None = None,
    native_stage: str | None = None,
    native_mutation: str | None = None,
    internal_mutation: str | None = None,
    reason: str | None = None,
    request_id: str | int | None = None,
    operation_id: str | None = None,
    task_session_id: str | None = None,
    worker_instance: str | None = None,
    worker_generation: int | None = None,
    worker_pid: int | None = None,
    win32_error: int | None = None,
    hresult_hex: str | None = None,
    target_hwnd: str | None = None,
    foreground_hwnd: str | None = None,
) -> dict[str, Any]:
    """Build a strictly sanitized diagnostic dictionary containing only allowlisted enums and IDs.

    Never exports raw exception messages, window titles, file paths, or typed text.
    """
    safe_err = (
        error_code
        if error_code in ALLOWLISTED_BRIDGE_ERROR_CODES
        else "agent_computer_native_error"
    )
    safe_phase = phase if phase in VALID_CLIENT_PHASES else "post_dispatch"
    out: dict[str, Any] = {
        "error": safe_err,
        "phase": safe_phase,
        "commit_attempted": bool(commit_attempted),
        "dispatched": bool(dispatched),
    }
    if callback_uncertain:
        out["callback_uncertain"] = True
    if stage and (stage in VALID_ERROR_STAGES or stage in VALID_CLIENT_PHASES):
        out["stage"] = stage
    if method and method in VALID_METHODS:
        out["method"] = method
    if native_code and native_code in VALID_ERROR_CODES:
        out["native_code"] = native_code
    if native_stage and native_stage in VALID_ERROR_STAGES:
        out["native_stage"] = native_stage
    if native_mutation and native_mutation in VALID_MUTATION_STATES:
        out["native_mutation"] = native_mutation
    if internal_mutation and internal_mutation in VALID_INTERNAL_MUTATION_DETAILS:
        out["internal_mutation"] = internal_mutation
    if reason and reason in VALID_DIAGNOSTIC_REASONS:
        out["reason"] = reason
    if isinstance(request_id, int) and not isinstance(request_id, bool):
        out["request_id"] = request_id
    elif isinstance(request_id, str) and _SAFE_TOKEN_RE.match(request_id):
        out["request_id"] = request_id
    if isinstance(operation_id, str) and _SAFE_TOKEN_RE.match(operation_id):
        out["operation_id"] = operation_id
    if isinstance(task_session_id, str) and _SAFE_TOKEN_RE.match(task_session_id):
        out["task_session_id"] = task_session_id
    if isinstance(worker_instance, str) and _SAFE_TOKEN_RE.match(worker_instance):
        out["worker_instance"] = worker_instance
    if type(worker_generation) is int and worker_generation >= 0:
        out["worker_generation"] = worker_generation
    if type(worker_pid) is int and worker_pid > 0:
        out["worker_pid"] = worker_pid
    if type(win32_error) is int and 0 <= win32_error <= 0xFFFFFFFF:
        out["win32_error"] = win32_error
    if isinstance(hresult_hex, str) and _HRESULT_HEX_RE.match(hresult_hex):
        out["hresult_hex"] = hresult_hex
    if isinstance(target_hwnd, str) and _DECIMAL_U64_RE.match(target_hwnd):
        out["target_hwnd"] = target_hwnd
    if isinstance(foreground_hwnd, str) and _DECIMAL_U64_RE.match(foreground_hwnd):
        out["foreground_hwnd"] = foreground_hwnd
    return out


class NativeBridgeError(AgentError):
    """Sanitized native bridge error with explicit commit/dispatch state flags."""

    def __init__(
        self,
        code: str,
        *,
        phase: str = "post_dispatch",
        stage: str | None = None,
        method: str | None = None,
        commit_attempted: bool = False,
        dispatched: bool = False,
        pre_dispatch: bool | None = None,
        callback_uncertain: bool = False,
        native_code: str | None = None,
        native_stage: str | None = None,
        native_mutation: str | None = None,
        internal_mutation: str | None = None,
        reason: str | None = None,
        request_id: str | int | None = None,
        operation_id: str | None = None,
        task_session_id: str | None = None,
        worker_instance: str | None = None,
        worker_generation: int | None = None,
        worker_pid: int | None = None,
        win32_error: int | None = None,
        hresult_hex: str | None = None,
        target_hwnd: str | None = None,
        foreground_hwnd: str | None = None,
    ) -> None:
        safe_code = (
            code
            if code in ALLOWLISTED_BRIDGE_ERROR_CODES
            else "agent_computer_native_error"
        )
        super().__init__(safe_code)
        self.code = safe_code
        self.commit_attempted = bool(commit_attempted)
        self.dispatched = bool(dispatched or commit_attempted)
        self.callback_uncertain = bool(callback_uncertain)
        if pre_dispatch is None:
            self.pre_dispatch = bool(
                not self.commit_attempted
                and not self.dispatched
                and not self.callback_uncertain
            )
        else:
            self.pre_dispatch = bool(
                pre_dispatch
                and not self.commit_attempted
                and not self.dispatched
                and not self.callback_uncertain
            )
        self.outcome = "not_executed" if self.pre_dispatch else "unknown"
        self.phase = phase if phase in VALID_CLIENT_PHASES else "post_dispatch"
        self.stage = stage
        self.method = method
        self.native_code = native_code if native_code in VALID_ERROR_CODES else None
        self.native_stage = native_stage if native_stage in VALID_ERROR_STAGES else None
        self.native_mutation = (
            native_mutation if native_mutation in VALID_MUTATION_STATES else None
        )
        self.internal_mutation = (
            internal_mutation
            if internal_mutation in VALID_INTERNAL_MUTATION_DETAILS
            else None
        )
        self.reason = reason if reason in VALID_DIAGNOSTIC_REASONS else None
        self.request_id = request_id
        self.operation_id = operation_id
        self.task_session_id = task_session_id
        self.worker_instance = worker_instance
        self.worker_generation = worker_generation
        self.worker_pid = worker_pid
        self.diagnostics = sanitize_diagnostic(
            error_code=safe_code,
            phase=self.phase,
            stage=stage,
            method=method,
            commit_attempted=self.commit_attempted,
            dispatched=self.dispatched,
            callback_uncertain=self.callback_uncertain,
            native_code=self.native_code,
            native_stage=self.native_stage,
            native_mutation=self.native_mutation,
            internal_mutation=self.internal_mutation,
            reason=self.reason,
            request_id=request_id,
            operation_id=operation_id,
            task_session_id=task_session_id,
            worker_instance=worker_instance,
            worker_generation=worker_generation,
            worker_pid=worker_pid,
            win32_error=win32_error,
            hresult_hex=hresult_hex,
            target_hwnd=target_hwnd,
            foreground_hwnd=foreground_hwnd,
        )


class NativeBridgeRejected(NativeBridgeError, AgentToolRejected):
    """Pre-dispatch rejection where strict protocol guarantees zero focus or input occurred."""

    def __init__(self, code: str, **kwargs: Any) -> None:
        kwargs["commit_attempted"] = False
        kwargs["dispatched"] = False
        kwargs["pre_dispatch"] = True
        kwargs["callback_uncertain"] = False
        kwargs.setdefault("phase", "pre_dispatch")
        NativeBridgeError.__init__(self, code, **kwargs)
        AgentToolRejected.__init__(self, self.code, details=self.diagnostics)


@dataclass(frozen=True)
class BoundedFrameRead:
    kind: str  # "eof" | "line" | "oversized" | "unterminated_eof" | "invalid_utf8"
    line: str = ""


class BoundedJsonlReader:
    """Reads newline-delimited UTF-8 frames enforcing `max_line_bytes` at stream read time.

    Never calls unbounded `readline()`, and stops reading immediately once `max_line_bytes + 1`
    content bytes are observed without newline.
    """

    def __init__(
        self,
        stream: BinaryIO,
        *,
        max_line_bytes: int = MAX_RESPONSE_FRAME_BYTES,
        chunk_size: int = 65536,
    ) -> None:
        self._stream = stream
        self._max_line_bytes = int(max_line_bytes)
        self._chunk_size = max(1, min(int(chunk_size), 65536))
        self._buffer = bytearray()

    def _read_some(self, max_bytes: int) -> bytes:
        read1 = getattr(self._stream, "read1", None)
        if callable(read1):
            return read1(max_bytes)
        return self._stream.read(max_bytes)

    def read_frame(self) -> BoundedFrameRead:
        cap_plus_two = self._max_line_bytes + 2
        while True:
            nl_pos = self._buffer.find(b"\n")
            if nl_pos != -1:
                raw_line = bytes(self._buffer[:nl_pos])
                del self._buffer[: nl_pos + 1]
                if raw_line.endswith(b"\r"):
                    content = raw_line[:-1]
                else:
                    content = raw_line
                if len(content) > self._max_line_bytes:
                    self._buffer.clear()
                    return BoundedFrameRead("oversized")
                try:
                    decoded = content.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    return BoundedFrameRead("invalid_utf8")
                return BoundedFrameRead("line", decoded)

            if len(self._buffer) == self._max_line_bytes + 1 and self._buffer[-1] != 0x0D:
                self._buffer.clear()
                return BoundedFrameRead("oversized")
            if len(self._buffer) >= cap_plus_two:
                self._buffer.clear()
                return BoundedFrameRead("oversized")

            need = min(self._chunk_size, cap_plus_two - len(self._buffer))
            chunk = self._read_some(max(1, need))
            if not chunk:
                if not self._buffer:
                    return BoundedFrameRead("eof")
                self._buffer.clear()
                return BoundedFrameRead("unterminated_eof")
            self._buffer.extend(chunk)


def parse_strict_json(
    raw: str,
    *,
    phase: str = "read",
) -> dict[str, Any]:
    """Parse a JSON object rejecting duplicate keys at any depth and non-finite numbers."""
    if not isinstance(raw, str):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )

    def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if not isinstance(k, str) or any(ord(c) < 0x20 for c in k):
                raise NativeBridgeError(
                    "agent_computer_protocol_invalid",
                    phase=phase,
                    reason="malformed_json",
                )
            if k in out:
                raise NativeBridgeError(
                    "agent_computer_duplicate_json_key",
                    phase=phase,
                    reason="duplicate_json_key",
                )
            out[k] = v
        return out

    def _parse_constant(val: str) -> Any:
        raise NativeBridgeError(
            "agent_computer_nonfinite_number",
            phase=phase,
            reason="malformed_json",
        )

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_parse_constant,
        )
    except NativeBridgeError:
        raise
    except (ValueError, TypeError):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        ) from None

    if not isinstance(parsed, dict):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    _verify_finite_numbers(parsed, phase=phase)
    return parsed


def _verify_finite_numbers(value: Any, *, phase: str, depth: int = 0) -> None:
    if depth > 32:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NativeBridgeError(
                "agent_computer_nonfinite_number",
                phase=phase,
                reason="malformed_json",
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise NativeBridgeError(
                    "agent_computer_protocol_invalid",
                    phase=phase,
                    reason="malformed_json",
                )
            _verify_finite_numbers(v, phase=phase, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _verify_finite_numbers(item, phase=phase, depth=depth + 1)


def validate_request_id(req_id: Any, *, phase: str = "pre_dispatch") -> str | int:
    if type(req_id) is int:
        return req_id
    if isinstance(req_id, str):
        if (
            not req_id.strip()
            or len(req_id) > MAX_ID_CHARS
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in req_id)
        ):
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="missing_request_id",
            )
        return req_id
    raise NativeBridgeError(
        "agent_computer_protocol_invalid",
        phase=phase,
        reason="missing_request_id",
    )


def request_id_correlation_key(req_id: str | int) -> str:
    if type(req_id) is int:
        return str(req_id)
    if isinstance(req_id, str):
        return req_id
    raise ValueError("Invalid request_id")


def encode_request_frame(
    req_id: str | int,
    method: str,
    params: dict[str, Any] | None = None,
) -> bytes:
    """Validate and serialize a wire-v1 request envelope to a bounded UTF-8 JSONL frame (<=64KiB)."""
    valid_id = validate_request_id(req_id, phase="pre_dispatch")
    if (
        not isinstance(method, str)
        or not method.strip()
        or len(method) > MAX_METHOD_CHARS
        or method not in VALID_METHODS
    ):
        raise NativeBridgeRejected(
            "agent_computer_protocol_invalid",
            phase="pre_dispatch",
            reason="missing_method",
        )
    if params is None:
        params_obj: dict[str, Any] = {}
    elif isinstance(params, dict):
        params_obj = params
    else:
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase="pre_dispatch",
            method=method,
            reason="malformed_json",
        )
    _verify_finite_numbers(params_obj, phase="pre_dispatch")
    envelope = {
        "v": WIRE_VERSION,
        "id": valid_id,
        "method": method,
        "params": params_obj,
    }
    try:
        raw_str = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase="pre_dispatch",
            method=method,
            reason="malformed_json",
        ) from None

    parse_strict_json(raw_str, phase="pre_dispatch")
    raw_bytes = raw_str.encode("utf-8")
    if len(raw_bytes) > MAX_COMMAND_LINE_BYTES:
        raise NativeBridgeRejected(
            "agent_computer_request_too_large",
            phase="pre_dispatch",
            method=method,
            reason="frame_too_large",
        )
    return raw_bytes + b"\n"


def _require_exact_keys(
    obj: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    phase: str = "read",
) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    allowed = required | (optional or set())
    keys = set(obj.keys())
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return obj


def _require_non_empty_str(
    val: Any,
    *,
    max_len: int = 256,
    phase: str = "read",
    forbid_control: bool = True,
) -> str:
    if not isinstance(val, str) or not val.strip() or len(val) > max_len:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if forbid_control and any(ord(c) < 0x20 or ord(c) == 0x7F for c in val):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return val


def _require_int(
    val: Any,
    *,
    min_val: int | None = None,
    max_val: int | None = None,
    phase: str = "read",
) -> int:
    if type(val) is not int:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if min_val is not None and val < min_val:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if max_val is not None and val > max_val:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return val


def _require_bool(val: Any, *, phase: str = "read") -> bool:
    if type(val) is not bool:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return val


def _require_decimal_u64(
    val: Any,
    *,
    allow_zero: bool = True,
    phase: str = "read",
) -> str:
    if not isinstance(val, str) or not _DECIMAL_U64_RE.match(val):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="hwnd_parse_failed",
        )
    num = int(val, 10)
    if num > 0xFFFFFFFFFFFFFFFF or (not allow_zero and num == 0):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="hwnd_parse_failed",
        )
    if str(num) != val:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="hwnd_parse_failed",
        )
    return val


def parse_wire_envelope(
    line: str,
    *,
    phase: str = "read",
) -> tuple[str, dict[str, Any]]:
    """Parse and validate a top-level wire-v1 ResponseEnvelope or EventEnvelope."""
    obj = parse_strict_json(line, phase=phase)
    v = obj.get("v")
    if type(v) is not int:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="missing_version" if v is None else "version_mismatch",
        )
    if v != WIRE_VERSION:
        raise NativeBridgeError(
            "agent_computer_version_mismatch",
            phase=phase,
            reason="version_mismatch",
        )

    if "event" in obj:
        _require_exact_keys(
            obj,
            required={"v", "event", "requestId", "data"},
            optional={"operationId"},
            phase=phase,
        )
        ev_name = _require_non_empty_str(obj["event"], max_len=64, phase=phase)
        if ev_name not in VALID_EVENTS:
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        validate_request_id(obj["requestId"], phase=phase)
        if obj.get("operationId") is not None:
            _require_non_empty_str(obj["operationId"], max_len=MAX_ID_CHARS, phase=phase)
        if not isinstance(obj["data"], dict):
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        return "event", obj

    _require_exact_keys(
        obj,
        required={"v", "id"},
        optional={"result", "error"},
        phase=phase,
    )
    validate_request_id(obj["id"], phase=phase)
    has_result = "result" in obj and obj["result"] is not None
    has_error = "error" in obj and obj["error"] is not None
    if has_result == has_error:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if has_result and not isinstance(obj["result"], dict):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if has_error:
        validate_protocol_error(obj["error"], phase=phase)
    return "response", obj


def validate_protocol_error(err_obj: Any, *, phase: str = "read") -> dict[str, Any]:
    """Validate a native `ProtocolError` wire object and strip any non-allowlisted strings."""
    _require_exact_keys(
        err_obj,
        required={"code", "stage", "mutation", "requestId", "retryable", "message"},
        optional={"operationId", "details"},
        phase=phase,
    )
    code = _require_non_empty_str(err_obj["code"], max_len=64, phase=phase)
    stage = _require_non_empty_str(err_obj["stage"], max_len=64, phase=phase)
    mutation = _require_non_empty_str(err_obj["mutation"], max_len=32, phase=phase)
    req_id = _require_non_empty_str(err_obj["requestId"], max_len=MAX_ID_CHARS, phase=phase)
    retryable = _require_bool(err_obj["retryable"], phase=phase)
    if not isinstance(err_obj["message"], str) or len(err_obj["message"]) > 512:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if (
        code not in VALID_ERROR_CODES
        or stage not in VALID_ERROR_STAGES
        or mutation not in VALID_MUTATION_STATES
    ):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    op_id: str | None = None
    if err_obj.get("operationId") is not None:
        op_id = _require_non_empty_str(
            err_obj["operationId"], max_len=MAX_ID_CHARS, phase=phase
        )

    details_raw = err_obj.get("details")
    clean_details: dict[str, Any] = {}
    if details_raw is not None:
        _require_exact_keys(
            details_raw,
            required=set(),
            optional={
                "win32Error",
                "hresultHex",
                "targetHwnd",
                "foregroundHwnd",
                "internalMutation",
                "reason",
            },
            phase=phase,
        )
        if details_raw.get("win32Error") is not None:
            clean_details["win32Error"] = _require_int(
                details_raw["win32Error"],
                min_val=0,
                max_val=0xFFFFFFFF,
                phase=phase,
            )
        if details_raw.get("hresultHex") is not None:
            hr = _require_non_empty_str(details_raw["hresultHex"], max_len=16, phase=phase)
            if not _HRESULT_HEX_RE.match(hr):
                raise NativeBridgeError(
                    "agent_computer_protocol_invalid",
                    phase=phase,
                    reason="malformed_json",
                )
            clean_details["hresultHex"] = hr
        if details_raw.get("targetHwnd") is not None:
            clean_details["targetHwnd"] = _require_decimal_u64(
                details_raw["targetHwnd"], allow_zero=True, phase=phase
            )
        if details_raw.get("foregroundHwnd") is not None:
            clean_details["foregroundHwnd"] = _require_decimal_u64(
                details_raw["foregroundHwnd"], allow_zero=True, phase=phase
            )
        if details_raw.get("internalMutation") is not None:
            im = _require_non_empty_str(
                details_raw["internalMutation"], max_len=64, phase=phase
            )
            if im not in VALID_INTERNAL_MUTATION_DETAILS:
                raise NativeBridgeError(
                    "agent_computer_protocol_invalid",
                    phase=phase,
                    reason="malformed_json",
                )
            clean_details["internalMutation"] = im
        if details_raw.get("reason") is not None:
            rsn = _require_non_empty_str(details_raw["reason"], max_len=64, phase=phase)
            if rsn not in VALID_DIAGNOSTIC_REASONS:
                raise NativeBridgeError(
                    "agent_computer_protocol_invalid",
                    phase=phase,
                    reason="malformed_json",
                )
            clean_details["reason"] = rsn

    return {
        "code": code,
        "stage": stage,
        "mutation": mutation,
        "requestId": req_id,
        "operationId": op_id,
        "retryable": retryable,
        "details": clean_details,
    }


def bridge_error_from_native(
    err_obj: dict[str, Any],
    *,
    phase: str,
    method: str,
    commit_attempted: bool = False,
    dispatched: bool = False,
    pre_dispatch_allowed: bool = False,
    worker_instance: str | None = None,
    worker_generation: int | None = None,
    worker_pid: int | None = None,
) -> NativeBridgeError:
    validated = validate_protocol_error(err_obj, phase=phase)
    native_code = validated["code"]
    native_stage = validated["stage"]
    native_mutation = validated["mutation"]
    details = validated["details"]
    internal_mutation = details.get("internalMutation")
    reason = details.get("reason")

    bridge_code = NATIVE_CODE_TO_BRIDGE_CODE.get(
        native_code, "agent_computer_native_error"
    )
    is_clean_pre_dispatch = (
        pre_dispatch_allowed
        and not commit_attempted
        and not dispatched
        and native_mutation == "none"
        and internal_mutation in (None, "none")
        and native_stage in {"protocol", "validate", "observe", "waiting_lock", "prepare", "ready", "cancel", "session"}
    )

    cls = NativeBridgeRejected if is_clean_pre_dispatch else NativeBridgeError
    return cls(
        bridge_code,
        phase="pre_dispatch" if is_clean_pre_dispatch else phase,
        stage=native_stage,
        method=method,
        commit_attempted=commit_attempted,
        dispatched=dispatched,
        pre_dispatch=is_clean_pre_dispatch,
        native_code=native_code,
        native_stage=native_stage,
        native_mutation=native_mutation,
        internal_mutation=internal_mutation,
        reason=reason,
        request_id=validated["requestId"],
        operation_id=validated.get("operationId"),
        worker_instance=worker_instance,
        worker_generation=worker_generation,
        worker_pid=worker_pid,
        win32_error=details.get("win32Error"),
        hresult_hex=details.get("hresultHex"),
        target_hwnd=details.get("targetHwnd"),
        foreground_hwnd=details.get("foregroundHwnd"),
    )


def validate_initialize_result(
    result: Any,
    *,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_worker_pid: int | None = None,
    expected_protocol_version: str = PROTOCOL_SEMVER,
) -> dict[str, Any]:
    _require_exact_keys(
        result,
        required={"protocolVersion", "workerInstance", "worker", "capabilities", "limits"},
        phase="initialize",
    )
    proto_ver = _require_non_empty_str(
        result["protocolVersion"], max_len=32, phase="initialize"
    )
    if proto_ver != expected_protocol_version:
        raise NativeBridgeRejected(
            "agent_computer_version_mismatch",
            phase="initialize",
            method="initialize",
            reason="version_mismatch",
        )
    worker_instance = _require_non_empty_str(
        result["workerInstance"], max_len=MAX_ID_CHARS, phase="initialize"
    )
    if not _SAFE_TOKEN_RE.match(worker_instance):
        raise NativeBridgeRejected(
            "agent_computer_handshake_failed",
            phase="initialize",
            method="initialize",
            reason="malformed_json",
        )

    worker = _require_exact_keys(
        result["worker"],
        required={"name", "version", "pid"},
        phase="initialize",
    )
    w_name = _require_non_empty_str(worker["name"], max_len=64, phase="initialize")
    w_ver = _require_non_empty_str(worker["version"], max_len=64, phase="initialize")
    w_pid = _require_int(worker["pid"], min_val=1, max_val=0xFFFFFFFF, phase="initialize")

    if w_name != expected_worker_name or (
        expected_worker_version is not None and w_ver != expected_worker_version
    ):
        raise NativeBridgeRejected(
            "agent_computer_worker_identity_mismatch",
            phase="initialize",
            method="initialize",
            worker_instance=worker_instance,
            worker_pid=w_pid,
        )

    if expected_worker_pid is not None and w_pid != expected_worker_pid:
        raise NativeBridgeRejected(
            "agent_computer_worker_identity_mismatch",
            phase="initialize",
            method="initialize",
            reason="pid_mismatch",
            worker_instance=worker_instance,
            worker_pid=w_pid,
        )

    caps = _require_exact_keys(
        result["capabilities"],
        required={
            "dpiAwareness",
            "uia",
            "windowCapture",
            "windowCaptureLimitations",
            "inputLock",
            "actions",
            "unsupportedActions",
        },
        phase="initialize",
    )
    dpi_awareness = _require_non_empty_str(
        caps["dpiAwareness"], max_len=64, phase="initialize"
    )
    uia_enabled = _require_bool(caps["uia"], phase="initialize")
    input_lock = _require_non_empty_str(caps["inputLock"], max_len=64, phase="initialize")

    def _validate_str_list(items: Any, *, max_items: int = 64) -> list[str]:
        if not isinstance(items, list) or len(items) > max_items:
            raise NativeBridgeRejected(
                "agent_computer_handshake_failed",
                phase="initialize",
                method="initialize",
                reason="malformed_json",
            )
        return [
            _require_non_empty_str(item, max_len=128, phase="initialize")
            for item in items
        ]

    win_cap = _validate_str_list(caps["windowCapture"])
    win_cap_lim = _validate_str_list(caps["windowCaptureLimitations"])
    supported_actions = _validate_str_list(caps["actions"])
    unsupported_actions = _validate_str_list(caps["unsupportedActions"])

    if any(a not in VALID_ACTION_KINDS for a in supported_actions) or any(
        a not in VALID_ACTION_KINDS for a in unsupported_actions
    ):
        raise NativeBridgeRejected(
            "agent_computer_handshake_failed",
            phase="initialize",
            method="initialize",
            reason="malformed_json",
        )
    if set(supported_actions) & set(unsupported_actions):
        raise NativeBridgeRejected(
            "agent_computer_handshake_failed",
            phase="initialize",
            method="initialize",
            reason="malformed_json",
        )

    limits = _require_exact_keys(
        result["limits"],
        required={
            "maxCommandLineBytes",
            "maxResponseFrameBytes",
            "maxPngBytes",
            "maxWindows",
            "maxUiaElements",
            "maxUiaDepth",
        },
        phase="initialize",
    )
    max_cmd = _require_int(
        limits["maxCommandLineBytes"],
        min_val=1024,
        max_val=MAX_COMMAND_LINE_BYTES,
        phase="initialize",
    )
    max_resp = _require_int(
        limits["maxResponseFrameBytes"],
        min_val=65536,
        max_val=MAX_RESPONSE_FRAME_BYTES,
        phase="initialize",
    )
    max_png = _require_int(
        limits["maxPngBytes"],
        min_val=65536,
        max_val=MAX_PNG_BYTES,
        phase="initialize",
    )
    max_win = _require_int(
        limits["maxWindows"], min_val=1, max_val=MAX_WINDOWS_CAP, phase="initialize"
    )
    max_uia_el = _require_int(
        limits["maxUiaElements"],
        min_val=1,
        max_val=MAX_UIA_ELEMENTS_CAP,
        phase="initialize",
    )
    max_uia_depth = _require_int(
        limits["maxUiaDepth"], min_val=1, max_val=MAX_UIA_DEPTH_CAP, phase="initialize"
    )

    return {
        "protocolVersion": proto_ver,
        "workerInstance": worker_instance,
        "worker": {
            "name": w_name,
            "version": w_ver,
            "pid": w_pid,
        },
        "capabilities": {
            "dpiAwareness": dpi_awareness,
            "uia": uia_enabled,
            "windowCapture": win_cap,
            "windowCaptureLimitations": win_cap_lim,
            "inputLock": input_lock,
            "actions": supported_actions,
            "unsupportedActions": unsupported_actions,
        },
        "limits": {
            "maxCommandLineBytes": max_cmd,
            "maxResponseFrameBytes": max_resp,
            "maxPngBytes": max_png,
            "maxWindows": max_win,
            "maxUiaElements": max_uia_el,
            "maxUiaDepth": max_uia_depth,
        },
    }


def validate_window_ref(
    wref: Any,
    *,
    expected_worker_instance: str | None = None,
    phase: str = "read",
) -> dict[str, Any]:
    _require_exact_keys(
        wref,
        required={
            "workerInstance",
            "windowKey",
            "hwnd",
            "pid",
            "processStarted",
            "generation",
        },
        optional={"appId"},
        phase=phase,
    )
    worker_instance = _require_non_empty_str(
        wref["workerInstance"], max_len=MAX_ID_CHARS, phase=phase
    )
    if not _SAFE_TOKEN_RE.match(worker_instance):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if (
        expected_worker_instance is not None
        and worker_instance != expected_worker_instance
    ):
        raise NativeBridgeError(
            "agent_computer_window_stale",
            phase=phase,
            reason="worker_instance_mismatch",
            worker_instance=expected_worker_instance,
        )
    window_key = _require_non_empty_str(
        wref["windowKey"], max_len=MAX_ID_CHARS, phase=phase
    )
    if not _SAFE_TOKEN_RE.match(window_key):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    hwnd = _require_decimal_u64(wref["hwnd"], allow_zero=False, phase=phase)
    pid = _require_int(wref["pid"], min_val=1, max_val=0xFFFFFFFF, phase=phase)
    process_started = _require_decimal_u64(
        wref["processStarted"], allow_zero=True, phase=phase
    )
    generation = _require_int(
        wref["generation"], min_val=1, max_val=0xFFFFFFFFFFFFFFFF, phase=phase
    )
    out: dict[str, Any] = {
        "workerInstance": worker_instance,
        "windowKey": window_key,
        "hwnd": hwnd,
        "pid": pid,
        "processStarted": process_started,
        "generation": generation,
    }
    if wref.get("appId") is not None:
        out["appId"] = _require_non_empty_str(wref["appId"], max_len=256, phase=phase)
    return out


def window_refs_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a.get("workerInstance") == b.get("workerInstance")
        and a.get("windowKey") == b.get("windowKey")
        and a.get("hwnd") == b.get("hwnd")
        and a.get("pid") == b.get("pid")
        and a.get("processStarted") == b.get("processStarted")
        and a.get("generation") == b.get("generation")
    )


def validate_rect_px(rect: Any, *, phase: str = "read") -> dict[str, int]:
    _require_exact_keys(
        rect,
        required={"left", "top", "right", "bottom", "width", "height"},
        phase=phase,
    )
    left = _require_int(rect["left"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase)
    top = _require_int(rect["top"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase)
    right = _require_int(rect["right"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase)
    bottom = _require_int(rect["bottom"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase)
    width = _require_int(rect["width"], min_val=0, max_val=0x7FFFFFFF, phase=phase)
    height = _require_int(rect["height"], min_val=0, max_val=0x7FFFFFFF, phase=phase)
    expected_w = max(0, right - left)
    expected_h = max(0, bottom - top)
    if width != expected_w or height != expected_h:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": width,
        "height": height,
    }


def validate_window_geometry(geom: Any, *, phase: str = "read") -> dict[str, Any]:
    _require_exact_keys(
        geom,
        required={"windowRect", "clientRect", "screenOrigin", "dpi", "scaleFactor"},
        phase=phase,
    )
    window_rect = validate_rect_px(geom["windowRect"], phase=phase)
    client_rect = validate_rect_px(geom["clientRect"], phase=phase)
    origin = geom["screenOrigin"]
    if (
        not isinstance(origin, list)
        or len(origin) != 2
        or type(origin[0]) is not int
        or type(origin[1]) is not int
    ):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    dpi = _require_int(geom["dpi"], min_val=24, max_val=1920, phase=phase)
    scale = geom["scaleFactor"]
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    scale_f = float(scale)
    if not math.isfinite(scale_f) or scale_f <= 0.0 or scale_f > 20.0:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return {
        "windowRect": window_rect,
        "clientRect": client_rect,
        "screenOrigin": [origin[0], origin[1]],
        "dpi": dpi,
        "scaleFactor": scale_f,
    }


def validate_window_entry(
    entry: Any,
    *,
    expected_worker_instance: str | None = None,
    phase: str = "read",
) -> dict[str, Any]:
    _require_exact_keys(
        entry,
        required={
            "window",
            "title",
            "className",
            "processName",
            "visible",
            "enabled",
            "minimized",
            "maximized",
            "foreground",
            "cloaked",
            "captureProtected",
            "bounds",
            "clientBounds",
            "dpi",
        },
        optional={"executablePath"},
        phase=phase,
    )
    wref = validate_window_ref(
        entry["window"],
        expected_worker_instance=expected_worker_instance,
        phase=phase,
    )
    if not isinstance(entry["title"], str) or len(entry["title"]) > 4096:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if not isinstance(entry["className"], str) or len(entry["className"]) > 1024:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    if not isinstance(entry["processName"], str) or len(entry["processName"]) > 1024:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    out: dict[str, Any] = {
        "window": wref,
        "title": entry["title"],
        "className": entry["className"],
        "processName": entry["processName"],
        "visible": _require_bool(entry["visible"], phase=phase),
        "enabled": _require_bool(entry["enabled"], phase=phase),
        "minimized": _require_bool(entry["minimized"], phase=phase),
        "maximized": _require_bool(entry["maximized"], phase=phase),
        "foreground": _require_bool(entry["foreground"], phase=phase),
        "cloaked": _require_bool(entry["cloaked"], phase=phase),
        "captureProtected": _require_bool(entry["captureProtected"], phase=phase),
        "bounds": validate_rect_px(entry["bounds"], phase=phase),
        "clientBounds": validate_rect_px(entry["clientBounds"], phase=phase),
        "dpi": _require_int(entry["dpi"], min_val=24, max_val=1920, phase=phase),
    }
    if entry.get("executablePath") is not None:
        if (
            not isinstance(entry["executablePath"], str)
            or len(entry["executablePath"]) > 4096
        ):
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        out["executablePath"] = entry["executablePath"]
    return out


def validate_windows_list_result(
    result: Any,
    *,
    expected_worker_instance: str,
    phase: str = "read",
) -> dict[str, Any]:
    _require_exact_keys(
        result,
        required={"capturedAt", "windows"},
        optional={"foregroundWindow"},
        phase=phase,
    )
    captured_at = _require_non_empty_str(result["capturedAt"], max_len=64, phase=phase)
    raw_windows = result["windows"]
    if not isinstance(raw_windows, list) or len(raw_windows) > MAX_WINDOWS_CAP:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    windows = [
        validate_window_entry(
            w, expected_worker_instance=expected_worker_instance, phase=phase
        )
        for w in raw_windows
    ]
    out: dict[str, Any] = {
        "capturedAt": captured_at,
        "windows": windows,
    }
    if result.get("foregroundWindow") is not None:
        out["foregroundWindow"] = validate_window_ref(
            result["foregroundWindow"],
            expected_worker_instance=expected_worker_instance,
            phase=phase,
        )
    return out


def validate_windows_get_result(
    result: Any,
    *,
    expected_worker_instance: str,
    expected_window: dict[str, Any] | None = None,
    phase: str = "read",
) -> dict[str, Any]:
    _require_exact_keys(result, required={"capturedAt", "entry"}, phase=phase)
    captured_at = _require_non_empty_str(result["capturedAt"], max_len=64, phase=phase)
    entry = validate_window_entry(
        result["entry"],
        expected_worker_instance=expected_worker_instance,
        phase=phase,
    )
    if expected_window is not None and not window_refs_equal(
        entry["window"], expected_window
    ):
        raise NativeBridgeError(
            "agent_computer_window_stale",
            phase=phase,
            reason="token_window_mismatch",
            worker_instance=expected_worker_instance,
        )
    return {
        "capturedAt": captured_at,
        "entry": entry,
    }


def validate_apps_list_result(result: Any, *, phase: str = "read") -> dict[str, Any]:
    _require_exact_keys(result, required={"apps"}, phase=phase)
    raw_apps = result["apps"]
    if not isinstance(raw_apps, list) or len(raw_apps) > 256:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    apps: list[dict[str, Any]] = []
    for item in raw_apps:
        _require_exact_keys(
            item,
            required={"appId", "name", "source"},
            optional={"executablePath", "aumid"},
            phase=phase,
        )
        app_entry: dict[str, Any] = {
            "appId": _require_non_empty_str(item["appId"], max_len=256, phase=phase),
            "name": _require_non_empty_str(item["name"], max_len=512, phase=phase),
            "source": _require_non_empty_str(item["source"], max_len=64, phase=phase),
        }
        if item.get("executablePath") is not None:
            app_entry["executablePath"] = _require_non_empty_str(
                item["executablePath"], max_len=4096, phase=phase
            )
        if item.get("aumid") is not None:
            app_entry["aumid"] = _require_non_empty_str(
                item["aumid"], max_len=512, phase=phase
            )
        apps.append(app_entry)
    return {"apps": apps}


def _parse_png_ihdr_dimensions(raw_png: bytes, *, phase: str) -> tuple[int, int]:
    if (
        len(raw_png) < 33
        or not raw_png.startswith(PNG_MAGIC)
        or raw_png[12:16] != b"IHDR"
    ):
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    ihdr_len = int.from_bytes(raw_png[8:12], "big")
    if ihdr_len != 13:
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    w = int.from_bytes(raw_png[16:20], "big")
    h = int.from_bytes(raw_png[20:24], "big")
    if (
        w <= 0
        or h <= 0
        or w > MAX_SCREENSHOT_DIMENSION
        or h > MAX_SCREENSHOT_DIMENSION
        or w * h > MAX_SCREENSHOT_PIXELS
    ):
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    return w, h


def validate_screenshot_artifact(shot: Any, *, phase: str = "read") -> dict[str, Any]:
    _require_exact_keys(
        shot,
        required={
            "id",
            "status",
            "captureMethod",
            "mimeType",
            "width",
            "height",
            "originX",
            "originY",
            "zIndex",
            "dpi",
            "scaleFactor",
            "sha256",
            "minimized",
            "occluded",
            "captureProtected",
        },
        optional={"dataBase64"},
        phase=phase,
    )
    art_id = _require_non_empty_str(shot["id"], max_len=MAX_ID_CHARS, phase=phase)
    if not _SAFE_TOKEN_RE.match(art_id):
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    status = _require_non_empty_str(shot["status"], max_len=64, phase=phase)
    if status not in {"ok", "unavailable", "minimized", "capture_protected"}:
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    capture_method = _require_non_empty_str(
        shot["captureMethod"], max_len=64, phase=phase
    )
    mime_type = _require_non_empty_str(shot["mimeType"], max_len=64, phase=phase)
    if mime_type != "image/png":
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    width = _require_int(
        shot["width"], min_val=0, max_val=MAX_SCREENSHOT_DIMENSION, phase=phase
    )
    height = _require_int(
        shot["height"], min_val=0, max_val=MAX_SCREENSHOT_DIMENSION, phase=phase
    )
    origin_x = _require_int(
        shot["originX"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase
    )
    origin_y = _require_int(
        shot["originY"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase
    )
    z_index = _require_int(shot["zIndex"], min_val=0, max_val=0xFFFFFFFF, phase=phase)
    dpi = _require_int(shot["dpi"], min_val=24, max_val=1920, phase=phase)
    scale = shot["scaleFactor"]
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    scale_f = float(scale)
    if not math.isfinite(scale_f) or scale_f <= 0.0 or scale_f > 20.0:
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    sha256_hex = _require_non_empty_str(shot["sha256"], max_len=64, phase=phase).lower()
    if not _HEX_64_RE.match(sha256_hex):
        raise NativeBridgeError(
            "agent_computer_screenshot_invalid",
            phase=phase,
            reason="malformed_json",
        )
    minimized = _require_bool(shot["minimized"], phase=phase)
    occluded = _require_bool(shot["occluded"], phase=phase)
    capture_protected = _require_bool(shot["captureProtected"], phase=phase)

    data_b64 = shot.get("dataBase64")
    if status == "ok":
        if not isinstance(data_b64, str) or not data_b64.strip():
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="malformed_json",
            )
        cleaned_b64 = data_b64.strip()
        if len(cleaned_b64) > (MAX_PNG_BYTES * 4) // 3 + 256:
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="frame_too_large",
            )
        try:
            raw_bytes = base64.b64decode(cleaned_b64, validate=True)
        except (binascii.Error, ValueError):
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="malformed_json",
            ) from None
        if not raw_bytes or len(raw_bytes) > MAX_PNG_BYTES:
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="frame_too_large",
            )
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha != sha256_hex:
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="malformed_json",
            )
        png_w, png_h = _parse_png_ihdr_dimensions(raw_bytes, phase=phase)
        if png_w != width or png_h != height:
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="malformed_json",
            )
    else:
        if data_b64 is not None or sha256_hex != EMPTY_SHA256:
            raise NativeBridgeError(
                "agent_computer_screenshot_invalid",
                phase=phase,
                reason="malformed_json",
            )
        cleaned_b64 = None

    out: dict[str, Any] = {
        "id": art_id,
        "status": status,
        "captureMethod": capture_method,
        "mimeType": mime_type,
        "width": width,
        "height": height,
        "originX": origin_x,
        "originY": origin_y,
        "zIndex": z_index,
        "dpi": dpi,
        "scaleFactor": scale_f,
        "sha256": sha256_hex,
        "minimized": minimized,
        "occluded": occluded,
        "captureProtected": capture_protected,
    }
    if cleaned_b64 is not None:
        out["dataBase64"] = cleaned_b64
    return out


def validate_uia_element(el: Any, *, phase: str = "read") -> dict[str, Any]:
    _require_exact_keys(
        el,
        required={
            "index",
            "runtimeId",
            "name",
            "controlType",
            "password",
            "enabled",
            "offscreen",
            "focusable",
            "focused",
            "bounds",
            "depth",
            "patterns",
        },
        optional={"automationId", "className", "value"},
        phase=phase,
    )
    index = _require_int(
        el["index"], min_val=0, max_val=MAX_UIA_ELEMENTS_CAP, phase=phase
    )
    runtime_id = _require_non_empty_str(el["runtimeId"], max_len=256, phase=phase)
    if not isinstance(el["name"], str) or len(el["name"]) > 4096:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    control_type = _require_non_empty_str(el["controlType"], max_len=128, phase=phase)
    password = _require_bool(el["password"], phase=phase)
    if password and el.get("value") is not None:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="password_redacted",
        )
    patterns_raw = el["patterns"]
    if not isinstance(patterns_raw, list) or len(patterns_raw) > 32:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    patterns = [
        _require_non_empty_str(p, max_len=64, phase=phase) for p in patterns_raw
    ]
    out: dict[str, Any] = {
        "index": index,
        "runtimeId": runtime_id,
        "name": el["name"],
        "controlType": control_type,
        "password": password,
        "enabled": _require_bool(el["enabled"], phase=phase),
        "offscreen": _require_bool(el["offscreen"], phase=phase),
        "focusable": _require_bool(el["focusable"], phase=phase),
        "focused": _require_bool(el["focused"], phase=phase),
        "bounds": validate_rect_px(el["bounds"], phase=phase),
        "depth": _require_int(
            el["depth"], min_val=0, max_val=MAX_UIA_DEPTH_CAP + 4, phase=phase
        ),
        "patterns": patterns,
    }
    if el.get("automationId") is not None:
        if not isinstance(el["automationId"], str) or len(el["automationId"]) > 1024:
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        out["automationId"] = el["automationId"]
    if el.get("className") is not None:
        if not isinstance(el["className"], str) or len(el["className"]) > 1024:
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        out["className"] = el["className"]
    if el.get("value") is not None:
        if not isinstance(el["value"], str) or len(el["value"]) > MAX_TEXT_CHARS_CAP * 4:
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        out["value"] = el["value"]
    return out


def validate_observation(
    obs: Any,
    *,
    expected_worker_instance: str,
    expected_task_session_id: str | None = None,
    expected_window: dict[str, Any] | None = None,
    phase: str = "read",
) -> dict[str, Any]:
    _require_exact_keys(
        obs,
        required={
            "taskSessionId",
            "window",
            "observationId",
            "stateToken",
            "capturedAt",
            "geometry",
            "screenshots",
            "accessibility",
            "foreground",
        },
        phase=phase,
    )
    task_session_id = _require_non_empty_str(
        obs["taskSessionId"], max_len=MAX_ID_CHARS, phase=phase
    )
    if (
        expected_task_session_id is not None
        and task_session_id != expected_task_session_id
    ):
        raise NativeBridgeError(
            "agent_computer_task_conflict",
            phase=phase,
            reason="token_task_mismatch",
            task_session_id=expected_task_session_id,
            worker_instance=expected_worker_instance,
        )
    window = validate_window_ref(
        obs["window"],
        expected_worker_instance=expected_worker_instance,
        phase=phase,
    )
    if expected_window is not None and not window_refs_equal(window, expected_window):
        raise NativeBridgeError(
            "agent_computer_window_stale",
            phase=phase,
            reason="token_window_mismatch",
            task_session_id=task_session_id,
            worker_instance=expected_worker_instance,
        )
    observation_id = _require_non_empty_str(
        obs["observationId"], max_len=MAX_ID_CHARS, phase=phase
    )
    state_token = _require_non_empty_str(
        obs["stateToken"], max_len=MAX_ID_CHARS, phase=phase
    )
    if not _SAFE_TOKEN_RE.match(observation_id) or not _SAFE_TOKEN_RE.match(state_token):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    captured_at = _require_non_empty_str(obs["capturedAt"], max_len=64, phase=phase)
    geometry = validate_window_geometry(obs["geometry"], phase=phase)

    shots_raw = obs["screenshots"]
    if not isinstance(shots_raw, list) or len(shots_raw) > 8:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    screenshots = [validate_screenshot_artifact(s, phase=phase) for s in shots_raw]

    acc_raw = _require_exact_keys(
        obs["accessibility"],
        required={"status", "treeHash", "truncated", "selectedElementIndices", "elements"},
        optional={"focusedElementIndex", "focusedRuntimeId", "selectedText"},
        phase=phase,
    )
    acc_status = _require_non_empty_str(acc_raw["status"], max_len=64, phase=phase)
    tree_hash = _require_non_empty_str(acc_raw["treeHash"], max_len=64, phase=phase).lower()
    if not _HEX_64_RE.match(tree_hash):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    truncated = _require_bool(acc_raw["truncated"], phase=phase)
    sel_indices_raw = acc_raw["selectedElementIndices"]
    if not isinstance(sel_indices_raw, list) or len(sel_indices_raw) > MAX_UIA_ELEMENTS_CAP:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    sel_indices = [
        _require_int(idx, min_val=0, max_val=MAX_UIA_ELEMENTS_CAP, phase=phase)
        for idx in sel_indices_raw
    ]
    elements_raw = acc_raw["elements"]
    if not isinstance(elements_raw, list) or len(elements_raw) > MAX_UIA_ELEMENTS_CAP:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            reason="malformed_json",
        )
    elements = [validate_uia_element(el, phase=phase) for el in elements_raw]
    accessibility: dict[str, Any] = {
        "status": acc_status,
        "treeHash": tree_hash,
        "truncated": truncated,
        "selectedElementIndices": sel_indices,
        "elements": elements,
    }
    if acc_raw.get("focusedElementIndex") is not None:
        accessibility["focusedElementIndex"] = _require_int(
            acc_raw["focusedElementIndex"],
            min_val=0,
            max_val=MAX_UIA_ELEMENTS_CAP,
            phase=phase,
        )
    if acc_raw.get("focusedRuntimeId") is not None:
        accessibility["focusedRuntimeId"] = _require_non_empty_str(
            acc_raw["focusedRuntimeId"], max_len=256, phase=phase
        )
    if acc_raw.get("selectedText") is not None:
        if (
            not isinstance(acc_raw["selectedText"], str)
            or len(acc_raw["selectedText"]) > MAX_TEXT_CHARS_CAP * 4
        ):
            raise NativeBridgeError(
                "agent_computer_protocol_invalid",
                phase=phase,
                reason="malformed_json",
            )
        accessibility["selectedText"] = acc_raw["selectedText"]

    fg_raw = _require_exact_keys(
        obs["foreground"],
        required={"isTargetWindow", "isRelatedToTarget"},
        optional={"foregroundHwnd", "foregroundPid"},
        phase=phase,
    )
    foreground: dict[str, Any] = {
        "isTargetWindow": _require_bool(fg_raw["isTargetWindow"], phase=phase),
        "isRelatedToTarget": _require_bool(fg_raw["isRelatedToTarget"], phase=phase),
    }
    if fg_raw.get("foregroundHwnd") is not None:
        foreground["foregroundHwnd"] = _require_decimal_u64(
            fg_raw["foregroundHwnd"], allow_zero=True, phase=phase
        )
    if fg_raw.get("foregroundPid") is not None:
        foreground["foregroundPid"] = _require_int(
            fg_raw["foregroundPid"], min_val=0, max_val=0xFFFFFFFF, phase=phase
        )

    return {
        "taskSessionId": task_session_id,
        "window": window,
        "observationId": observation_id,
        "stateToken": state_token,
        "capturedAt": captured_at,
        "geometry": geometry,
        "screenshots": screenshots,
        "accessibility": accessibility,
        "foreground": foreground,
    }


def _normalize_point_target(pt: Any, *, phase: str) -> dict[str, Any]:
    _require_exact_keys(pt, required={"coordinateSpace", "x", "y"}, phase=phase)
    cs = _require_non_empty_str(pt["coordinateSpace"], max_len=64, phase=phase)
    if cs not in {"window_physical_px", "client_physical_px"}:
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase=phase,
            reason="point_out_of_bounds",
        )
    x = _require_int(pt["x"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase)
    y = _require_int(pt["y"], min_val=-0x7FFFFFFF, max_val=0x7FFFFFFF, phase=phase)
    return {
        "coordinateSpace": cs,
        "x": x,
        "y": y,
    }


def _normalize_element_target(el: Any, *, phase: str) -> dict[str, Any]:
    _require_exact_keys(el, required={"index"}, optional={"runtimeId"}, phase=phase)
    idx = _require_int(
        el["index"], min_val=0, max_val=MAX_UIA_ELEMENTS_CAP, phase=phase
    )
    rid = el.get("runtimeId")
    if rid is not None:
        rid = _require_non_empty_str(rid, max_len=256, phase=phase)
    # Rust ElementTarget does NOT have skip_serializing_if on runtime_id: Option<String>,
    # so serde serializes runtimeId as null when None.
    return {
        "index": idx,
        "runtimeId": rid,
    }


def _normalize_app_launch_target(target: Any, *, phase: str) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase=phase,
            reason="malformed_json",
        )
    kind = target.get("kind")
    if kind == "app_id":
        _require_exact_keys(target, required={"kind", "appId"}, phase=phase)
        return {
            "kind": "app_id",
            "appId": _require_non_empty_str(target["appId"], max_len=256, phase=phase),
        }
    if kind == "aumid":
        _require_exact_keys(target, required={"kind", "aumid"}, phase=phase)
        return {
            "kind": "aumid",
            "aumid": _require_non_empty_str(target["aumid"], max_len=512, phase=phase),
        }
    if kind == "executable":
        _require_exact_keys(
            target,
            required={"kind", "executablePath"},
            optional={"args", "workingDirectory"},
            phase=phase,
        )
        exe_path = _require_non_empty_str(
            target["executablePath"], max_len=4096, phase=phase
        )
        raw_args = target.get("args")
        if raw_args is None:
            args_list: list[str] = []
        elif isinstance(raw_args, list) and len(raw_args) <= 64:
            args_list = [
                _require_non_empty_str(a, max_len=2048, phase=phase)
                if isinstance(a, str) and a
                else ""
                for a in raw_args
            ]
            if not all(isinstance(a, str) for a in raw_args):
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
        else:
            raise NativeBridgeRejected(
                "agent_computer_invalid_params",
                phase=phase,
                reason="malformed_json",
            )
        work_dir = target.get("workingDirectory")
        if work_dir is not None:
            work_dir = _require_non_empty_str(work_dir, max_len=4096, phase=phase)
        return {
            "kind": "executable",
            "executablePath": exe_path,
            "args": args_list,
            "workingDirectory": work_dir,
        }
    raise NativeBridgeRejected(
        "agent_computer_invalid_params",
        phase=phase,
        reason="malformed_json",
    )


def normalize_action_spec(action: Any, *, phase: str = "pre_dispatch") -> dict[str, Any]:
    """Validate and normalize an `ActionSpec` to match Rust serde `ActionSpec` field order & null Optionals."""
    if not isinstance(action, dict):
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase=phase,
            reason="malformed_json",
        )
    action_type = action.get("type")
    if not isinstance(action_type, str) or action_type not in VALID_ACTION_KINDS:
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase=phase,
            reason="malformed_json",
        )

    try:
        if action_type == "focus_window":
            _require_exact_keys(
                action,
                required={"type"},
                optional={"restoreIfMinimized"},
                phase=phase,
            )
            rim = action.get("restoreIfMinimized")
            if rim is not None:
                rim = _require_bool(rim, phase=phase)
            return {
                "type": "focus_window",
                "restoreIfMinimized": rim,
            }

        if action_type in {"click", "double_click"}:
            _require_exact_keys(
                action,
                required={"type"},
                optional={"button", "target", "element"},
                phase=phase,
            )
            btn = action.get("button")
            if btn is not None:
                btn = _require_non_empty_str(btn, max_len=16, phase=phase)
                if btn not in {"left", "right", "middle"}:
                    raise NativeBridgeRejected(
                        "agent_computer_invalid_params",
                        phase=phase,
                        reason="malformed_json",
                    )
            t_raw = action.get("target")
            e_raw = action.get("element")
            if (t_raw is None) == (e_raw is None):
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            t_norm = _normalize_point_target(t_raw, phase=phase) if t_raw is not None else None
            e_norm = _normalize_element_target(e_raw, phase=phase) if e_raw is not None else None
            return {
                "type": action_type,
                "button": btn,
                "target": t_norm,
                "element": e_norm,
            }

        if action_type == "context_click":
            _require_exact_keys(
                action,
                required={"type"},
                optional={"target", "element"},
                phase=phase,
            )
            t_raw = action.get("target")
            e_raw = action.get("element")
            if (t_raw is None) == (e_raw is None):
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            t_norm = _normalize_point_target(t_raw, phase=phase) if t_raw is not None else None
            e_norm = _normalize_element_target(e_raw, phase=phase) if e_raw is not None else None
            return {
                "type": "context_click",
                "target": t_norm,
                "element": e_norm,
            }

        if action_type == "move_pointer":
            _require_exact_keys(action, required={"type", "target"}, phase=phase)
            return {
                "type": "move_pointer",
                "target": _normalize_point_target(action["target"], phase=phase),
            }

        if action_type == "drag":
            _require_exact_keys(
                action,
                required={"type", "from", "to"},
                optional={"button", "steps"},
                phase=phase,
            )
            from_pt = _normalize_point_target(action["from"], phase=phase)
            to_pt = _normalize_point_target(action["to"], phase=phase)
            btn = action.get("button")
            if btn is not None:
                btn = _require_non_empty_str(btn, max_len=16, phase=phase)
                if btn not in {"left", "right", "middle"}:
                    raise NativeBridgeRejected(
                        "agent_computer_invalid_params",
                        phase=phase,
                        reason="malformed_json",
                    )
            steps = action.get("steps")
            if steps is not None:
                steps = _require_int(steps, min_val=1, max_val=256, phase=phase)
            return {
                "type": "drag",
                "from": from_pt,
                "to": to_pt,
                "button": btn,
                "steps": steps,
            }

        if action_type == "scroll":
            _require_exact_keys(
                action,
                required={"type", "target", "deltaX", "deltaY"},
                phase=phase,
            )
            pt = _normalize_point_target(action["target"], phase=phase)
            dx = _require_int(
                action["deltaX"], min_val=-100000, max_val=100000, phase=phase
            )
            dy = _require_int(
                action["deltaY"], min_val=-100000, max_val=100000, phase=phase
            )
            if dx == 0 and dy == 0:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            return {
                "type": "scroll",
                "target": pt,
                "deltaX": dx,
                "deltaY": dy,
            }

        if action_type == "type_text":
            _require_exact_keys(
                action,
                required={"type", "text"},
                optional={"target", "element"},
                phase=phase,
            )
            text = action["text"]
            if not isinstance(text, str) or not text or len(text) > 4096:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            t_raw = action.get("target")
            e_raw = action.get("element")
            if t_raw is not None and e_raw is not None:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            t_norm = _normalize_point_target(t_raw, phase=phase) if t_raw is not None else None
            e_norm = _normalize_element_target(e_raw, phase=phase) if e_raw is not None else None
            return {
                "type": "type_text",
                "text": text,
                "target": t_norm,
                "element": e_norm,
            }

        if action_type == "key_press":
            _require_exact_keys(
                action,
                required={"type", "key"},
                optional={"repeat"},
                phase=phase,
            )
            key = _require_non_empty_str(action["key"], max_len=64, phase=phase)
            rep = action.get("repeat")
            if rep is not None:
                rep = _require_int(rep, min_val=1, max_val=32, phase=phase)
            return {
                "type": "key_press",
                "key": key,
                "repeat": rep,
            }

        if action_type == "key_chord":
            _require_exact_keys(action, required={"type", "keys"}, phase=phase)
            raw_keys = action["keys"]
            if not isinstance(raw_keys, list) or not (1 <= len(raw_keys) <= 6):
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            keys = [
                _require_non_empty_str(k, max_len=64, phase=phase) for k in raw_keys
            ]
            return {
                "type": "key_chord",
                "keys": keys,
            }

        if action_type in {"uia_invoke", "uia_select", "uia_focus"}:
            _require_exact_keys(action, required={"type", "element"}, phase=phase)
            return {
                "type": action_type,
                "element": _normalize_element_target(action["element"], phase=phase),
            }

        if action_type == "uia_set_value":
            _require_exact_keys(
                action, required={"type", "element", "value"}, phase=phase
            )
            el_norm = _normalize_element_target(action["element"], phase=phase)
            val = action["value"]
            if not isinstance(val, str) or len(val) > 4096:
                raise NativeBridgeRejected(
                    "agent_computer_invalid_params",
                    phase=phase,
                    reason="malformed_json",
                )
            return {
                "type": "uia_set_value",
                "element": el_norm,
                "value": val,
            }

        if action_type == "launch_app":
            _require_exact_keys(action, required={"type", "target"}, phase=phase)
            return {
                "type": "launch_app",
                "target": _normalize_app_launch_target(action["target"], phase=phase),
            }
    except NativeBridgeRejected:
        raise
    except NativeBridgeError as exc:
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase=phase,
            reason=exc.reason or "malformed_json",
        ) from None

    raise NativeBridgeRejected(
        "agent_computer_invalid_params",
        phase=phase,
        reason="malformed_json",
    )


def serialize_rust_action_spec(action: dict[str, Any]) -> bytes:
    """Serialize an `ActionSpec` identically to Rust `serde_json::to_vec(&ActionSpec)`."""
    normalized = normalize_action_spec(action, phase="pre_dispatch")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_action_fingerprint(
    task_session_id: str,
    window: dict[str, Any],
    expected_state_token: str,
    action: dict[str, Any],
) -> str:
    """Compute the exact SHA-256 hex digest matching Rust `snapshot::compute_action_fingerprint`."""
    wref = validate_window_ref(window, phase="pre_dispatch")
    action_bytes = serialize_rust_action_spec(action)
    h = hashlib.sha256()
    h.update(task_session_id.encode("utf-8"))
    h.update(b"|")
    h.update(wref["workerInstance"].encode("utf-8"))
    h.update(b"|")
    h.update(wref["windowKey"].encode("utf-8"))
    h.update(b"|")
    h.update(wref["hwnd"].encode("utf-8"))
    h.update(b"|")
    h.update(str(wref["pid"]).encode("utf-8"))
    h.update(b"|")
    h.update(wref["processStarted"].encode("utf-8"))
    h.update(b"|")
    h.update(str(wref["generation"]).encode("utf-8"))
    h.update(b"|")
    h.update(expected_state_token.encode("utf-8"))
    h.update(b"|")
    h.update(action_bytes)
    return h.hexdigest()


def validate_prepare_constraints(
    constraints: Any,
    *,
    phase: str = "pre_dispatch",
) -> dict[str, Any] | None:
    if constraints is None:
        return None
    if not isinstance(constraints, dict):
        raise NativeBridgeRejected(
            "agent_computer_invalid_params",
            phase=phase,
            reason="malformed_json",
        )
    _require_exact_keys(
        constraints,
        required=set(),
        optional={
            "readyTimeoutMs",
            "lockWaitTimeoutMs",
            "focusDeadlineMs",
            "restoreIfMinimized",
        },
        phase=phase,
    )
    out: dict[str, Any] = {}
    for ms_key in ("readyTimeoutMs", "lockWaitTimeoutMs", "focusDeadlineMs"):
        if constraints.get(ms_key) is not None:
            out[ms_key] = _require_int(
                constraints[ms_key], min_val=1, max_val=120_000, phase=phase
            )
    if constraints.get("restoreIfMinimized") is not None:
        out["restoreIfMinimized"] = _require_bool(
            constraints["restoreIfMinimized"], phase=phase
        )
    return out


def validate_action_ready_event_data(
    data: Any,
    *,
    expected_worker_instance: str,
    phase: str = "ready",
) -> dict[str, Any]:
    _require_exact_keys(
        data,
        required={
            "taskSessionId",
            "operationId",
            "prepareTicket",
            "window",
            "expectedStateToken",
            "actionKind",
            "actionFingerprint",
            "inputLockHeld",
            "lockWasAbandoned",
            "expiresInMs",
            "mutation",
        },
        optional={"authorityEpoch"},
        phase=phase,
    )
    task_session_id = _require_non_empty_str(
        data["taskSessionId"], max_len=MAX_ID_CHARS, phase=phase
    )
    operation_id = _require_non_empty_str(
        data["operationId"], max_len=MAX_ID_CHARS, phase=phase
    )
    prepare_ticket = _require_non_empty_str(
        data["prepareTicket"], max_len=MAX_ID_CHARS, phase=phase
    )
    if not _SAFE_TOKEN_RE.match(prepare_ticket):
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase=phase,
            reason="malformed_json",
        )
    window = validate_window_ref(
        data["window"],
        expected_worker_instance=expected_worker_instance,
        phase=phase,
    )
    expected_state_token = _require_non_empty_str(
        data["expectedStateToken"], max_len=MAX_ID_CHARS, phase=phase
    )
    action_kind = _require_non_empty_str(data["actionKind"], max_len=64, phase=phase)
    if action_kind not in VALID_ACTION_KINDS:
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase=phase,
            reason="malformed_json",
        )
    action_fingerprint = _require_non_empty_str(
        data["actionFingerprint"], max_len=64, phase=phase
    ).lower()
    if not _HEX_64_RE.match(action_fingerprint):
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase=phase,
            reason="malformed_json",
        )
    input_lock_held = _require_bool(data["inputLockHeld"], phase=phase)
    lock_was_abandoned = _require_bool(data["lockWasAbandoned"], phase=phase)
    expires_in_ms = _require_int(
        data["expiresInMs"], min_val=1, max_val=60_000, phase=phase
    )
    mutation = _require_non_empty_str(data["mutation"], max_len=32, phase=phase)
    out: dict[str, Any] = {
        "taskSessionId": task_session_id,
        "operationId": operation_id,
        "prepareTicket": prepare_ticket,
        "window": window,
        "expectedStateToken": expected_state_token,
        "actionKind": action_kind,
        "actionFingerprint": action_fingerprint,
        "inputLockHeld": input_lock_held,
        "lockWasAbandoned": lock_was_abandoned,
        "expiresInMs": expires_in_ms,
        "mutation": mutation,
    }
    if data.get("authorityEpoch") is not None:
        out["authorityEpoch"] = _require_int(
            data["authorityEpoch"],
            min_val=0,
            max_val=0xFFFFFFFFFFFFFFFF,
            phase=phase,
        )
    return out


def validate_action_prepare_result(
    result: Any,
    *,
    expected_worker_instance: str,
    phase: str = "ready",
) -> dict[str, Any]:
    _require_exact_keys(
        result,
        required={
            "status",
            "taskSessionId",
            "operationId",
            "prepareTicket",
            "window",
            "expectedStateToken",
            "actionKind",
            "actionFingerprint",
            "expiresInMs",
            "inputLockHeld",
            "lockWasAbandoned",
            "mutation",
            "resolvedTarget",
        },
        optional={"authorityEpoch"},
        phase=phase,
    )
    status = _require_non_empty_str(result["status"], max_len=32, phase=phase)
    task_session_id = _require_non_empty_str(
        result["taskSessionId"], max_len=MAX_ID_CHARS, phase=phase
    )
    operation_id = _require_non_empty_str(
        result["operationId"], max_len=MAX_ID_CHARS, phase=phase
    )
    prepare_ticket = _require_non_empty_str(
        result["prepareTicket"], max_len=MAX_ID_CHARS, phase=phase
    )
    if not _SAFE_TOKEN_RE.match(prepare_ticket):
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase=phase,
            reason="malformed_json",
        )
    window = validate_window_ref(
        result["window"],
        expected_worker_instance=expected_worker_instance,
        phase=phase,
    )
    expected_state_token = _require_non_empty_str(
        result["expectedStateToken"], max_len=MAX_ID_CHARS, phase=phase
    )
    action_kind = _require_non_empty_str(result["actionKind"], max_len=64, phase=phase)
    if action_kind not in VALID_ACTION_KINDS:
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase=phase,
            reason="malformed_json",
        )
    action_fingerprint = _require_non_empty_str(
        result["actionFingerprint"], max_len=64, phase=phase
    ).lower()
    if not _HEX_64_RE.match(action_fingerprint):
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase=phase,
            reason="malformed_json",
        )
    expires_in_ms = _require_int(
        result["expiresInMs"], min_val=1, max_val=60_000, phase=phase
    )
    input_lock_held = _require_bool(result["inputLockHeld"], phase=phase)
    lock_was_abandoned = _require_bool(result["lockWasAbandoned"], phase=phase)
    mutation = _require_non_empty_str(result["mutation"], max_len=32, phase=phase)

    rt_raw = _require_exact_keys(
        result["resolvedTarget"],
        required={"windowHwnd"},
        optional={
            "screenPoint",
            "secondaryScreenPoint",
            "elementIndex",
            "elementRuntimeId",
        },
        phase=phase,
    )
    resolved_target: dict[str, Any] = {
        "windowHwnd": _require_decimal_u64(
            rt_raw["windowHwnd"], allow_zero=False, phase=phase
        ),
    }
    for pt_key in ("screenPoint", "secondaryScreenPoint"):
        if rt_raw.get(pt_key) is not None:
            pt_val = rt_raw[pt_key]
            if (
                not isinstance(pt_val, list)
                or len(pt_val) != 2
                or type(pt_val[0]) is not int
                or type(pt_val[1]) is not int
            ):
                raise NativeBridgeError(
                    "agent_computer_ready_invalid",
                    phase=phase,
                    reason="malformed_json",
                )
            resolved_target[pt_key] = [pt_val[0], pt_val[1]]
    if rt_raw.get("elementIndex") is not None:
        resolved_target["elementIndex"] = _require_int(
            rt_raw["elementIndex"],
            min_val=0,
            max_val=MAX_UIA_ELEMENTS_CAP,
            phase=phase,
        )
    if rt_raw.get("elementRuntimeId") is not None:
        resolved_target["elementRuntimeId"] = _require_non_empty_str(
            rt_raw["elementRuntimeId"], max_len=256, phase=phase
        )

    out: dict[str, Any] = {
        "status": status,
        "taskSessionId": task_session_id,
        "operationId": operation_id,
        "prepareTicket": prepare_ticket,
        "window": window,
        "expectedStateToken": expected_state_token,
        "actionKind": action_kind,
        "actionFingerprint": action_fingerprint,
        "expiresInMs": expires_in_ms,
        "inputLockHeld": input_lock_held,
        "lockWasAbandoned": lock_was_abandoned,
        "mutation": mutation,
        "resolvedTarget": resolved_target,
    }
    if result.get("authorityEpoch") is not None:
        out["authorityEpoch"] = _require_int(
            result["authorityEpoch"],
            min_val=0,
            max_val=0xFFFFFFFFFFFFFFFF,
            phase=phase,
        )
    return out


def verify_ready_integrity(
    *,
    prepare_result: dict[str, Any],
    ready_event: dict[str, Any] | None,
    expected_worker_instance: str,
    task_session_id: str,
    operation_id: str,
    window: dict[str, Any],
    expected_state_token: str,
    action: dict[str, Any],
    authority_epoch: int | None,
) -> dict[str, Any]:
    """Verify `action.prepare` result and `action.ready` event match the original immutable request."""
    validated_res = validate_action_prepare_result(
        prepare_result,
        expected_worker_instance=expected_worker_instance,
        phase="ready",
    )
    normalized_action = normalize_action_spec(action, phase="ready")
    expected_kind = normalized_action["type"]
    expected_fp = compute_action_fingerprint(
        task_session_id,
        window,
        expected_state_token,
        normalized_action,
    )

    if (
        validated_res["status"] != "ready"
        or validated_res["mutation"] != "none"
        or validated_res["inputLockHeld"] is not True
    ):
        raise NativeBridgeError(
            "agent_computer_ready_invalid",
            phase="ready",
            method="action.prepare",
            operation_id=operation_id,
            task_session_id=task_session_id,
            worker_instance=expected_worker_instance,
        )

    if (
        validated_res["taskSessionId"] != task_session_id
        or validated_res["operationId"] != operation_id
        or not window_refs_equal(validated_res["window"], window)
        or validated_res["expectedStateToken"] != expected_state_token
        or validated_res["actionKind"] != expected_kind
        or validated_res["actionFingerprint"] != expected_fp
        or validated_res.get("authorityEpoch") != authority_epoch
        or validated_res["resolvedTarget"]["windowHwnd"] != window["hwnd"]
    ):
        raise NativeBridgeError(
            "agent_computer_ready_tampered",
            phase="ready",
            method="action.prepare",
            operation_id=operation_id,
            task_session_id=task_session_id,
            worker_instance=expected_worker_instance,
        )

    if ready_event is not None:
        validated_ev = validate_action_ready_event_data(
            ready_event,
            expected_worker_instance=expected_worker_instance,
            phase="ready",
        )
        if (
            validated_ev["taskSessionId"] != task_session_id
            or validated_ev["operationId"] != operation_id
            or validated_ev["prepareTicket"] != validated_res["prepareTicket"]
            or not window_refs_equal(validated_ev["window"], window)
            or validated_ev["expectedStateToken"] != expected_state_token
            or validated_ev["actionKind"] != expected_kind
            or validated_ev["actionFingerprint"] != expected_fp
            or validated_ev.get("authorityEpoch") != authority_epoch
            or validated_ev["inputLockHeld"] is not True
            or validated_ev["mutation"] != "none"
        ):
            raise NativeBridgeError(
                "agent_computer_ready_tampered",
                phase="ready",
                method="action.prepare",
                operation_id=operation_id,
                task_session_id=task_session_id,
                worker_instance=expected_worker_instance,
            )

    return copy.deepcopy(validated_res)


def validate_action_commit_result(
    result: Any,
    *,
    expected_worker_instance: str,
    expected_task_session_id: str,
    expected_operation_id: str,
    expected_action_fingerprint: str,
    expected_window: dict[str, Any],
    phase: str = "commit_wait",
) -> dict[str, Any]:
    _require_exact_keys(
        result,
        required={
            "status",
            "taskSessionId",
            "operationId",
            "actionFingerprint",
            "mutation",
            "focusRecovery",
        },
        optional={"observation"},
        phase=phase,
    )
    status = _require_non_empty_str(result["status"], max_len=32, phase=phase)
    task_session_id = _require_non_empty_str(
        result["taskSessionId"], max_len=MAX_ID_CHARS, phase=phase
    )
    operation_id = _require_non_empty_str(
        result["operationId"], max_len=MAX_ID_CHARS, phase=phase
    )
    action_fp = _require_non_empty_str(
        result["actionFingerprint"], max_len=64, phase=phase
    ).lower()
    mutation = _require_non_empty_str(result["mutation"], max_len=32, phase=phase)

    if (
        status != "committed"
        or mutation != "delivered"
        or task_session_id != expected_task_session_id
        or operation_id != expected_operation_id
        or action_fp != expected_action_fingerprint.lower()
    ):
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            method="action.commit",
            commit_attempted=True,
            dispatched=True,
            operation_id=expected_operation_id,
            task_session_id=expected_task_session_id,
            worker_instance=expected_worker_instance,
        )

    fr_raw = _require_exact_keys(
        result["focusRecovery"],
        required={
            "attempted",
            "succeeded",
            "methodUsed",
            "durationMs",
            "finalForegroundVerified",
        },
        phase=phase,
    )
    focus_recovery = {
        "attempted": _require_bool(fr_raw["attempted"], phase=phase),
        "succeeded": _require_bool(fr_raw["succeeded"], phase=phase),
        "methodUsed": _require_non_empty_str(fr_raw["methodUsed"], max_len=64, phase=phase),
        "durationMs": _require_int(
            fr_raw["durationMs"], min_val=0, max_val=120_000, phase=phase
        ),
        "finalForegroundVerified": _require_bool(
            fr_raw["finalForegroundVerified"], phase=phase
        ),
    }

    out: dict[str, Any] = {
        "status": status,
        "taskSessionId": task_session_id,
        "operationId": operation_id,
        "actionFingerprint": action_fp,
        "mutation": mutation,
        "focusRecovery": focus_recovery,
    }
    if result.get("observation") is not None:
        out["observation"] = validate_observation(
            result["observation"],
            expected_worker_instance=expected_worker_instance,
            expected_task_session_id=expected_task_session_id,
            expected_window=expected_window,
            phase=phase,
        )
    return out


def validate_action_cancel_result(
    result: Any,
    *,
    expected_operation_id: str | None = None,
    phase: str = "cancel",
) -> dict[str, Any]:
    _require_exact_keys(
        result,
        required={
            "cancelled",
            "operationId",
            "stage",
            "mutation",
            "inputLockReleased",
        },
        phase=phase,
    )
    cancelled = _require_bool(result["cancelled"], phase=phase)
    operation_id = _require_non_empty_str(
        result["operationId"], max_len=MAX_ID_CHARS, phase=phase
    )
    if expected_operation_id is not None and operation_id != expected_operation_id:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            method="action.cancel",
            operation_id=expected_operation_id,
        )
    stage = _require_non_empty_str(result["stage"], max_len=64, phase=phase)
    mutation = _require_non_empty_str(result["mutation"], max_len=32, phase=phase)
    if stage not in VALID_ERROR_STAGES or mutation not in VALID_MUTATION_STATES:
        raise NativeBridgeError(
            "agent_computer_protocol_invalid",
            phase=phase,
            method="action.cancel",
            operation_id=operation_id,
        )
    input_lock_released = _require_bool(result["inputLockReleased"], phase=phase)
    return {
        "cancelled": cancelled,
        "operationId": operation_id,
        "stage": stage,
        "mutation": mutation,
        "inputLockReleased": input_lock_released,
    }


def validate_session_end_result(result: Any, *, phase: str = "session") -> dict[str, Any]:
    _require_exact_keys(
        result,
        required={"ended", "cancelledOperations", "invalidatedStateTokens"},
        phase=phase,
    )
    return {
        "ended": _require_bool(result["ended"], phase=phase),
        "cancelledOperations": _require_int(
            result["cancelledOperations"], min_val=0, max_val=1_000_000, phase=phase
        ),
        "invalidatedStateTokens": _require_int(
            result["invalidatedStateTokens"], min_val=0, max_val=1_000_000, phase=phase
        ),
    }


def validate_shutdown_result(result: Any, *, phase: str = "shutdown") -> dict[str, Any]:
    _require_exact_keys(result, required={"shuttingDown"}, phase=phase)
    return {
        "shuttingDown": _require_bool(result["shuttingDown"], phase=phase),
    }
