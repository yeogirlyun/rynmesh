"""Strict bounded wire protocol and schema validation for the Ryn QuickJS host runtime."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_CODE_BYTES = 256 * 1024
MAX_ARGUMENTS_BYTES = 256 * 1024
MAX_RESULT_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_AGGREGATE_EVENT_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

DEFAULT_LIMITS: dict[str, int] = {
    "memory_limit_bytes": 32 * 1024 * 1024,
    "max_stack_bytes": 512 * 1024,
    "cpu_timeout_ms": 2000,
    "max_pending_jobs": 10000,
    "max_output_bytes": MAX_OUTPUT_BYTES,
    "max_result_bytes": MAX_RESULT_BYTES,
    "max_host_calls": 64,
    "max_images": 16,
    "max_aggregate_event_bytes": MAX_AGGREGATE_EVENT_BYTES,
}

_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "memory_limit_bytes": (1024 * 1024, 128 * 1024 * 1024),
    "max_stack_bytes": (64 * 1024, 2 * 1024 * 1024),
    "cpu_timeout_ms": (50, 30000),
    "max_pending_jobs": (64, 100000),
    "max_output_bytes": (1024, 1024 * 1024),
    "max_result_bytes": (1024, 1024 * 1024),
    "max_host_calls": (1, 256),
    "max_images": (1, 64),
    "max_aggregate_event_bytes": (4096, MAX_FRAME_BYTES - 256 * 1024),
}


class JsRuntimeError(Exception):
    """Base error for Ryn JS runtime protocol, lifecycle, and execution failures."""

    def __init__(self, code: str, message: str = "", *, details: dict[str, Any] | None = None):
        super().__init__(message or code)
        self.code = str(code)
        self.message = str(message or code)
        self.details = copy.deepcopy(details) if isinstance(details, dict) else {}


class JsProtocolError(JsRuntimeError):
    """Raised when a message violates framing, size, schema, or correlation invariants."""


SKY_METHOD_SPECS: dict[str, dict[str, str]] = {
    "list_apps": {
        "effect": "read",
        "target_tool": "computer_observe_windows",
    },
    "list_windows": {
        "effect": "read",
        "target_tool": "computer_observe_windows",
    },
    "get_window": {
        "effect": "read",
        "target_tool": "computer_observe_windows",
    },
    "get_window_state": {
        "effect": "read",
        "target_tool": "computer_read_window",
    },
    "launch_app": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "activate_window": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "click": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "click_element": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "type_text": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "set_value": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "press_key": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "scroll": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "drag": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
    "perform_secondary_action": {
        "effect": "write",
        "target_tool": "computer_act_window",
    },
}

WORKER_FRAME_TYPES = frozenset({
    "ready",
    "host_call",
    "output",
    "image_request",
    "execution_result",
    "execution_error",
    "turn_ended",
    "protocol_error",
})


def validate_identifier(value: Any, field_name: str = "id") -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise JsProtocolError(
            f"ryn_js_invalid_{field_name}",
            f"{field_name} must be a non-empty bounded ASCII identifier (1..128 chars)",
        )
    return value


def validate_limits(limits: dict[str, Any] | None) -> dict[str, int]:
    if limits is None:
        return dict(DEFAULT_LIMITS)
    if not isinstance(limits, dict):
        raise JsProtocolError("ryn_js_invalid_limits", "limits must be a dictionary")
    validated = dict(DEFAULT_LIMITS)
    for key, val in limits.items():
        if key not in _LIMIT_BOUNDS:
            raise JsProtocolError("ryn_js_invalid_limits", f"Unsupported limit key: {key!r}")
        if type(val) is not int or val < 1:
            raise JsProtocolError("ryn_js_invalid_limits", f"Limit {key!r} must be a positive integer")
        low, high = _LIMIT_BOUNDS[key]
        validated[key] = max(low, min(high, val))
    return validated


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise JsProtocolError("ryn_js_json_depth_exceeded", "JSON nesting depth exceeds maximum limit")
    if value is None or isinstance(value, (bool, str)):
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsProtocolError("ryn_js_non_finite_number", "Non-finite floats are not permitted in protocol frames")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise JsProtocolError("ryn_js_invalid_key", "JSON object keys must be strings")
            _validate_json_value(v, depth=depth + 1)
        return
    raise JsProtocolError("ryn_js_unserializable_type", f"Unsupported protocol value type: {type(value).__name__}")


def canonical_json_bytes(value: Any, *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JsProtocolError("ryn_js_json_encode_failed", str(exc)) from exc
    if len(encoded) > max_bytes:
        raise JsProtocolError("ryn_js_frame_too_large", f"Encoded JSON exceeds {max_bytes} bytes")
    return encoded


def canonical_arguments_sha256(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        raise JsProtocolError("ryn_js_invalid_arguments", "Host call arguments must be a dictionary")
    raw = canonical_json_bytes(arguments, max_bytes=MAX_ARGUMENTS_BYTES)
    return hashlib.sha256(raw).hexdigest()


def _validate_window_dict(win: Any) -> dict[str, Any]:
    if not isinstance(win, dict):
        raise JsProtocolError("ryn_js_invalid_window", "window must be an object")
    app = win.get("app")
    win_id = win.get("id")
    if not isinstance(app, str) or not app.strip():
        raise JsProtocolError("ryn_js_invalid_window", "window.app must be a non-empty string")
    if type(win_id) is not int or win_id < 0:
        raise JsProtocolError("ryn_js_invalid_window", "window.id must be an integer >= 0")
    normalized: dict[str, Any] = {"app": app, "id": win_id}
    if isinstance(win.get("title"), str):
        normalized["title"] = win["title"]
    return normalized


def validate_sky_host_call(method: str, arguments: Any) -> tuple[dict[str, str], dict[str, Any], str]:
    """Validate a worker-proposed Sky method and canonicalize its arguments."""
    if not isinstance(method, str) or method not in SKY_METHOD_SPECS:
        raise JsProtocolError("ryn_sky_method_unsupported", f"Unsupported Sky host call method: {method!r}")
    if not isinstance(arguments, dict):
        raise JsProtocolError("ryn_js_invalid_arguments", "Host call arguments must be a JSON object")
    _validate_json_value(arguments)
    spec = SKY_METHOD_SPECS[method]
    norm: dict[str, Any] = {}

    if method in {"list_apps", "list_windows"}:
        norm = {}
    elif method == "launch_app":
        app = arguments.get("app")
        if not isinstance(app, str) or not app.strip():
            raise JsProtocolError("ryn_js_invalid_arguments", "launch_app requires non-empty string app")
        norm = {"app": app}
    elif method == "get_window":
        win_id = arguments.get("id")
        if type(win_id) is not int or win_id < 0:
            raise JsProtocolError("ryn_js_invalid_arguments", "get_window requires integer id >= 0")
        norm = {"id": win_id}
        if "app" in arguments and arguments["app"] is not None:
            if not isinstance(arguments["app"], str) or not arguments["app"].strip():
                raise JsProtocolError("ryn_js_invalid_arguments", "get_window app must be a non-empty string")
            norm["app"] = arguments["app"]
    elif method == "activate_window":
        norm = {"window": _validate_window_dict(arguments.get("window"))}
    elif method == "get_window_state":
        win = _validate_window_dict(arguments.get("window"))
        inc_shot = bool(arguments.get("include_screenshot", True))
        inc_text = bool(arguments.get("include_text", False))
        if not inc_shot and not inc_text:
            raise JsProtocolError(
                "ryn_js_invalid_arguments",
                "get_window_state must request include_screenshot, include_text, or both",
            )
        norm = {
            "window": win,
            "include_screenshot": inc_shot,
            "include_text": inc_text,
        }
    elif method == "click":
        win = _validate_window_dict(arguments.get("window"))
        x = arguments.get("x")
        y = arguments.get("y")
        click_count = arguments.get("click_count", 1)
        mouse_button = arguments.get("mouse_button", "left")
        if type(x) is not int or type(y) is not int:
            raise JsProtocolError("ryn_js_invalid_arguments", "click requires integer x and y coordinates")
        if type(click_count) is not int or click_count < 1:
            raise JsProtocolError("ryn_js_invalid_arguments", "click_count must be an integer >= 1")
        if mouse_button not in {"left", "right", "middle"}:
            raise JsProtocolError("ryn_js_invalid_arguments", "mouse_button must be left, right, or middle")
        norm = {
            "window": win,
            "x": x,
            "y": y,
            "click_count": click_count,
            "mouse_button": mouse_button,
        }
        if "screenshotId" in arguments and arguments["screenshotId"] is not None:
            shot_id = arguments["screenshotId"]
            if not isinstance(shot_id, str) or not shot_id.strip():
                raise JsProtocolError("ryn_js_invalid_arguments", "screenshotId must be a non-empty string")
            norm["screenshotId"] = shot_id
    elif method == "click_element":
        win = _validate_window_dict(arguments.get("window"))
        elem_idx = arguments.get("element_index")
        click_count = arguments.get("click_count", 1)
        mouse_button = arguments.get("mouse_button", "left")
        if type(elem_idx) is not int or elem_idx < 0:
            raise JsProtocolError("ryn_js_invalid_arguments", "element_index must be an integer >= 0")
        if type(click_count) is not int or click_count < 1:
            raise JsProtocolError("ryn_js_invalid_arguments", "click_count must be an integer >= 1")
        if mouse_button not in {"left", "right", "middle"}:
            raise JsProtocolError("ryn_js_invalid_arguments", "mouse_button must be left, right, or middle")
        norm = {
            "window": win,
            "element_index": elem_idx,
            "click_count": click_count,
            "mouse_button": mouse_button,
        }
    elif method == "type_text":
        win = _validate_window_dict(arguments.get("window"))
        text = arguments.get("text")
        if not isinstance(text, str):
            raise JsProtocolError("ryn_js_invalid_arguments", "type_text requires string text")
        norm = {"window": win, "text": text}
    elif method == "set_value":
        win = _validate_window_dict(arguments.get("window"))
        elem_idx = arguments.get("element_index")
        value = arguments.get("value")
        if type(elem_idx) is not int or elem_idx < 0:
            raise JsProtocolError("ryn_js_invalid_arguments", "element_index must be an integer >= 0")
        if not isinstance(value, str):
            raise JsProtocolError("ryn_js_invalid_arguments", "set_value requires string value")
        norm = {"window": win, "element_index": elem_idx, "value": value}
    elif method == "press_key":
        win = _validate_window_dict(arguments.get("window"))
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise JsProtocolError("ryn_js_invalid_arguments", "press_key requires non-empty string key")
        norm = {"window": win, "key": key}
    elif method == "scroll":
        win = _validate_window_dict(arguments.get("window"))
        x = arguments.get("x")
        y = arguments.get("y")
        scroll_x = arguments.get("scrollX", 0)
        scroll_y = arguments.get("scrollY", 0)
        if any(type(v) is not int for v in (x, y, scroll_x, scroll_y)):
            raise JsProtocolError("ryn_js_invalid_arguments", "scroll coordinates and deltas must be integers")
        norm = {"window": win, "x": x, "y": y, "scrollX": scroll_x, "scrollY": scroll_y}
        if "screenshotId" in arguments and arguments["screenshotId"] is not None:
            shot_id = arguments["screenshotId"]
            if not isinstance(shot_id, str) or not shot_id.strip():
                raise JsProtocolError("ryn_js_invalid_arguments", "screenshotId must be a non-empty string")
            norm["screenshotId"] = shot_id
    elif method == "drag":
        win = _validate_window_dict(arguments.get("window"))
        fx = arguments.get("from_x")
        fy = arguments.get("from_y")
        tx = arguments.get("to_x")
        ty = arguments.get("to_y")
        if any(type(v) is not int for v in (fx, fy, tx, ty)):
            raise JsProtocolError("ryn_js_invalid_arguments", "drag coordinates must be integers")
        norm = {"window": win, "from_x": fx, "from_y": fy, "to_x": tx, "to_y": ty}
        if "screenshotId" in arguments and arguments["screenshotId"] is not None:
            shot_id = arguments["screenshotId"]
            if not isinstance(shot_id, str) or not shot_id.strip():
                raise JsProtocolError("ryn_js_invalid_arguments", "screenshotId must be a non-empty string")
            norm["screenshotId"] = shot_id
    elif method == "perform_secondary_action":
        win = _validate_window_dict(arguments.get("window"))
        elem_idx = arguments.get("element_index")
        action = arguments.get("action")
        if type(elem_idx) is not int or elem_idx < 0:
            raise JsProtocolError("ryn_js_invalid_arguments", "element_index must be an integer >= 0")
        if not isinstance(action, str) or not action.strip():
            raise JsProtocolError("ryn_js_invalid_arguments", "action must be a non-empty string")
        norm = {"window": win, "element_index": elem_idx, "action": action}

    args_sha = canonical_arguments_sha256(norm)
    return dict(spec), norm, args_sha


REDACTED_CAPABILITY_PLACEHOLDER = "[ryn-image:redacted-capability]"
REDACTED_DATA_URL_PLACEHOLDER = "[ryn-image:redacted-data-url]"

_CAPABILITY_URL_SUBSTR_RE = re.compile(r"ryn-image:cap:[A-Za-z0-9_.:-]*")
_DATA_IMAGE_SUBSTR_RE = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]*",
    re.IGNORECASE,
)

_ALLOWED_IMAGE_METADATA_KEYS: frozenset[str] = frozenset({
    "id",
    "artifact_id",
    "provenance",
    "provenance_kind",
    "observation_id",
    "operation_id",
    "receipt_id",
    "emitting_execution_id",
    "issued_execution_id",
    "issued_callback_id",
    "sha256",
    "mime_type",
    "mimeType",
    "byte_length",
    "byteLength",
    "base64_length",
    "base64Length",
    "width",
    "height",
    "originX",
    "originY",
    "origin_x",
    "origin_y",
    "zIndex",
    "z_index",
    "stateToken",
    "state_token",
    "window",
    "native_window",
    "native_worker_instance",
    "promotion_id",
    "promotion_stage",
    "disposition",
    "status",
    "error_code",
    "reason",
    "transform",
    "evidence",
})


def sanitize_snapshot_text(text: str) -> str:
    """Replace raw capability URLs and inline base64 data:image URLs in snapshot text."""
    if not isinstance(text, str) or not text:
        return ""
    scrubbed = _CAPABILITY_URL_SUBSTR_RE.sub(REDACTED_CAPABILITY_PLACEHOLDER, text)
    scrubbed = _DATA_IMAGE_SUBSTR_RE.sub(REDACTED_DATA_URL_PLACEHOLDER, scrubbed)
    return scrubbed


def sanitize_snapshot_value(value: Any) -> Any:
    """Recursively scrub capability URLs and inline image data URLs from execution keys, values, and errors."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_snapshot_text(value)
    if isinstance(value, list):
        return [sanitize_snapshot_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_snapshot_value(item) for item in value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        reserved_unmodified_keys = {
            str(k)
            for k in value.keys()
            if sanitize_snapshot_text(str(k)) == str(k)
        }
        for k, v in value.items():
            raw_key_str = str(k)
            sanitized_key = sanitize_snapshot_text(raw_key_str)
            if raw_key_str == sanitized_key and sanitized_key not in out:
                target_key = sanitized_key
            elif sanitized_key not in out and sanitized_key not in reserved_unmodified_keys:
                target_key = sanitized_key
            else:
                collision_idx = 2
                while True:
                    candidate_key = f"{sanitized_key}#{collision_idx}"
                    if candidate_key not in out and candidate_key not in reserved_unmodified_keys:
                        target_key = candidate_key
                        break
                    collision_idx += 1

            if target_key in {"url", "image_url", "capability_url"} and isinstance(v, str):
                if v.startswith("ryn-image:cap:"):
                    out[target_key] = REDACTED_CAPABILITY_PLACEHOLDER
                    continue
                if v.lower().startswith("data:image/"):
                    out[target_key] = REDACTED_DATA_URL_PLACEHOLDER
                    continue
            out[target_key] = sanitize_snapshot_value(v)
        return out
    return copy.deepcopy(value)


def sanitize_image_metadata(image: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted copy of image metadata with URL, token, path, and base64 stripped."""
    if not isinstance(image, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, val in image.items():
        if key not in _ALLOWED_IMAGE_METADATA_KEYS or val is None:
            continue
        cleaned[key] = sanitize_snapshot_value(val)
    return cleaned


@dataclass(frozen=True)
class PendingHostCallEnvelope:
    """Bounded inert host_call envelope returned to the Python coordinator/loop."""

    task_id: str
    session_id: str
    generation: int
    execution_id: str
    callback_id: str
    ordinal: int
    method: str
    effect: str
    target_tool: str
    arguments: dict[str, Any]
    arguments_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "execution_id": self.execution_id,
            "callback_id": self.callback_id,
            "ordinal": self.ordinal,
            "method": self.method,
            "effect": self.effect,
            "target_tool": self.target_tool,
            "arguments": copy.deepcopy(self.arguments),
            "arguments_sha256": self.arguments_sha256,
        }


@dataclass
class JsExecutionSnapshot:
    """Point-in-time status of a guest JS evaluation."""

    task_id: str
    session_id: str
    generation: int
    execution_id: str
    status: str  # "running" | "awaiting_host" | "completed" | "failed" | "cancelled" | "lost"
    value: Any = None
    value_type: str = "undefined"
    stdout: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)
    pending_host_calls: list[PendingHostCallEnvelope] = field(default_factory=list)
    error: dict[str, Any] | None = None
    cpu_elapsed_ms: int = 0

    def __post_init__(self) -> None:
        self.value = sanitize_snapshot_value(self.value)
        self.stdout = sanitize_snapshot_text(self.stdout)
        self.images = [sanitize_image_metadata(img) for img in self.images if isinstance(img, dict)]
        if isinstance(self.error, dict):
            self.error = sanitize_snapshot_value(self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "execution_id": self.execution_id,
            "status": self.status,
            "value": sanitize_snapshot_value(self.value),
            "value_type": self.value_type,
            "stdout": sanitize_snapshot_text(self.stdout),
            "images": [sanitize_image_metadata(img) for img in self.images if isinstance(img, dict)],
            "pending_host_calls": [item.to_dict() for item in self.pending_host_calls],
            "error": sanitize_snapshot_value(self.error) if isinstance(self.error, dict) else None,
            "cpu_elapsed_ms": self.cpu_elapsed_ms,
        }


def encode_parent_frame(frame: dict[str, Any]) -> bytes:
    if not isinstance(frame, dict) or frame.get("v") != PROTOCOL_VERSION or not isinstance(frame.get("type"), str):
        raise JsProtocolError("ryn_js_invalid_parent_frame", "Parent frame must include v=1 and string type")
    raw = canonical_json_bytes(frame, max_bytes=MAX_FRAME_BYTES)
    return raw + b"\n"


def build_evaluate_frame(
    *,
    session_id: str,
    generation: int,
    execution_id: str,
    code: str,
    script_id: str | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = validate_identifier(session_id, "session_id")
    eid = validate_identifier(execution_id, "execution_id")
    if type(generation) is not int or generation < 1:
        raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
    if not isinstance(code, str) or not code.strip():
        raise JsProtocolError("ryn_js_code_invalid", "Evaluation code must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise JsProtocolError("ryn_js_code_invalid", f"Evaluation code exceeds {MAX_CODE_BYTES} bytes")
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "evaluate",
        "session_id": sid,
        "generation": generation,
        "execution_id": eid,
        "code": code,
    }
    if script_id is not None:
        frame["script_id"] = validate_identifier(script_id, "script_id")
    if limits is not None:
        frame["limits"] = validate_limits(limits)
    return frame


def build_host_result_frame(
    *,
    session_id: str,
    generation: int,
    execution_id: str,
    callback_id: str,
    ok: bool,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = validate_identifier(session_id, "session_id")
    eid = validate_identifier(execution_id, "execution_id")
    cid = validate_identifier(callback_id, "callback_id")
    if type(generation) is not int or generation < 1:
        raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
    if type(ok) is not bool:
        raise JsProtocolError("ryn_js_invalid_host_result", "ok must be a boolean")
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "host_result",
        "session_id": sid,
        "generation": generation,
        "execution_id": eid,
        "callback_id": cid,
        "ok": ok,
    }
    if ok:
        if result is not None:
            canonical_json_bytes(result, max_bytes=MAX_RESULT_BYTES)
            frame["result"] = copy.deepcopy(result)
    else:
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not error["code"].strip():
            raise JsProtocolError("ryn_js_invalid_host_result", "Rejected host_result requires error.code")
        err_obj = {
            "code": str(error["code"]),
            "message": str(error.get("message") or error["code"])[:2000],
            "not_executed": bool(error.get("not_executed", True)),
            "outcome": str(error.get("outcome") or ("not_executed" if error.get("not_executed", True) else "unknown")),
        }
        frame["error"] = err_obj
    return frame


def build_cancel_frame(
    *,
    session_id: str,
    generation: int,
    reason: str = "ryn_js_cancelled",
    execution_id: str | None = None,
) -> dict[str, Any]:
    sid = validate_identifier(session_id, "session_id")
    if type(generation) is not int or generation < 1:
        raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "cancel",
        "session_id": sid,
        "generation": generation,
        "reason": str(reason or "ryn_js_cancelled")[:256],
    }
    if execution_id is not None:
        frame["execution_id"] = validate_identifier(execution_id, "execution_id")
    return frame


def build_end_turn_frame(*, session_id: str, generation: int) -> dict[str, Any]:
    sid = validate_identifier(session_id, "session_id")
    if type(generation) is not int or generation < 1:
        raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
    return {
        "v": PROTOCOL_VERSION,
        "type": "end_turn",
        "session_id": sid,
        "generation": generation,
    }


def build_close_frame(*, session_id: str, generation: int) -> dict[str, Any]:
    sid = validate_identifier(session_id, "session_id")
    if type(generation) is not int or generation < 1:
        raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
    return {
        "v": PROTOCOL_VERSION,
        "type": "close",
        "session_id": sid,
        "generation": generation,
    }


def parse_worker_frame(raw_line: bytes | str) -> dict[str, Any]:
    if isinstance(raw_line, bytes):
        if len(raw_line) > MAX_FRAME_BYTES:
            raise JsProtocolError("ryn_js_frame_too_large", "Worker frame exceeds maximum byte limit")
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JsProtocolError("ryn_js_malformed_frame", "Worker frame is not valid UTF-8") from exc
    elif isinstance(raw_line, str):
        if len(raw_line.encode("utf-8")) > MAX_FRAME_BYTES:
            raise JsProtocolError("ryn_js_frame_too_large", "Worker frame exceeds maximum byte limit")
        text = raw_line
    else:
        raise JsProtocolError("ryn_js_malformed_frame", "Worker frame must be bytes or str")

    try:
        data = json.loads(text)
    except ValueError as exc:
        raise JsProtocolError("ryn_js_malformed_frame", "Worker frame is not valid JSON") from exc

    if not isinstance(data, dict):
        raise JsProtocolError("ryn_js_invalid_frame", "Worker frame must be a JSON object")
    if data.get("v") != PROTOCOL_VERSION:
        raise JsProtocolError("ryn_js_invalid_frame", f"Unsupported worker frame version: {data.get('v')!r}")
    frame_type = data.get("type")
    if not isinstance(frame_type, str) or frame_type not in WORKER_FRAME_TYPES:
        raise JsProtocolError("ryn_js_unknown_frame_type", f"Unknown worker frame type: {frame_type!r}")
    validate_identifier(data.get("session_id"), "session_id")
    gen = data.get("generation")
    if type(gen) is not int or gen < 1:
        raise JsProtocolError("ryn_js_invalid_generation", "Worker frame generation must be an integer >= 1")
    _validate_json_value(data)
    return data
