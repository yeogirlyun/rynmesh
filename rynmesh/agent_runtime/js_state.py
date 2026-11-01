"""Pure validation and monotonic state transitions for private `row.js_execution`.

Phase A module: pure validation and detached state transformations only.
No store, process, worker, network, or runtime imports/side effects.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any

from .js_runtime.protocol import (
    MAX_CODE_BYTES,
    MAX_JSON_DEPTH,
    MAX_OUTPUT_BYTES,
    MAX_RESULT_BYTES,
    SAFE_ID_RE,
    SKY_METHOD_SPECS,
)

JS_STATE_VERSION = 1
CONTAINER_TOOL_NAME = "ryn_js_execute"

# Preserve router's smaller 96 KiB tool-call argument limit for parent code/calls.
ROUTER_MAX_ARGUMENT_BYTES = 96 * 1024
MAX_PARENT_CODE_BYTES = min(MAX_CODE_BYTES, ROUTER_MAX_ARGUMENT_BYTES)
MAX_JS_STATE_BYTES = 1024 * 1024
MAX_OUTBOX_PAYLOAD_BYTES = MAX_RESULT_BYTES
MAX_ADMITTED_CALLBACKS = 64
MAX_OUTBOX_ITEMS = 64
MAX_CHILD_RECEIPT_REFS = 64
MAX_TERMINAL_IMAGES = 16
MAX_STATE_DEPTH = min(24, MAX_JSON_DEPTH)
MAX_ACTIVE_CPU_MS = 300_000
MAX_ERROR_MESSAGE_CHARS = 2000

CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\Z")
SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}\Z")
RECEIPT_ID_RE = re.compile(r"^receipt_[A-Za-z0-9_.:-]{1,128}\Z")
RESULT_REF_ID_RE = re.compile(r"^result_[a-f0-9]{32}\Z")
INERT_IMAGE_TOKEN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,95}\Z")

_DATA_URL_RE = re.compile(r"data:[A-Za-z0-9.+/-]*;base64,", re.IGNORECASE)
_CAPABILITY_URL_RE = re.compile(r"ryn-image:cap:[A-Za-z0-9_.:-]*", re.IGNORECASE)
_RAW_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,128}\Z")
_BASE64_LONG_RUN_RE = re.compile(r"^[A-Za-z0-9+/]{256,}={0,2}\Z")
_FORBIDDEN_URL_SCHEME_RE = re.compile(r"^(?:data|file|https?|ftp|ws|wss|ryn-image):", re.IGNORECASE)
_FORBIDDEN_STATE_KEYS = frozenset({
    "base64",
    "data_url",
    "image_url",
    "capability_url",
    "capability_token",
    "raw_token",
    "raw_bytes",
    "heap",
    "promise",
    "continuation_blob",
})

JS_PHASES = frozenset({
    "starting",
    "running",
    "awaiting_child",
    "awaiting_approval",
    "delivering",
    "fenced_unknown",
    "continuation_lost",
    "completed",
    "cancelled",
})

TERMINAL_JS_PHASES = frozenset({
    "continuation_lost",
    "completed",
    "cancelled",
})

ACTIVE_JS_PHASES = JS_PHASES - TERMINAL_JS_PHASES

PHASE_TRANSITION_TABLE: dict[str, frozenset[str]] = {
    "starting": frozenset({
        "starting",
        "running",
        "awaiting_child",
        "continuation_lost",
        "completed",
        "cancelled",
    }),
    "running": frozenset({
        "running",
        "awaiting_child",
        "awaiting_approval",
        "delivering",
        "fenced_unknown",
        "continuation_lost",
        "completed",
        "cancelled",
    }),
    "awaiting_child": frozenset({
        "awaiting_child",
        "awaiting_approval",
        "delivering",
        "fenced_unknown",
        "continuation_lost",
        "cancelled",
    }),
    "awaiting_approval": frozenset({
        "awaiting_approval",
        "awaiting_child",
        "delivering",
        "fenced_unknown",
        "continuation_lost",
        "cancelled",
    }),
    "delivering": frozenset({
        "delivering",
        "running",
        "awaiting_child",
        "fenced_unknown",
        "continuation_lost",
        "completed",
        "cancelled",
    }),
    "fenced_unknown": frozenset({
        "fenced_unknown",
        "delivering",
        "continuation_lost",
        "cancelled",
    }),
    "continuation_lost": frozenset({"continuation_lost"}),
    "completed": frozenset({"completed"}),
    "cancelled": frozenset({"cancelled"}),
}

CALLBACK_STATUSES = frozenset({
    "admitted",
    "awaiting_approval",
    "dispatched",
    "completed",
    "rejected",
    "unknown",
})

CALLBACK_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "admitted": frozenset({"admitted", "awaiting_approval", "dispatched", "completed", "rejected", "unknown"}),
    "awaiting_approval": frozenset({"awaiting_approval", "dispatched", "completed", "rejected", "unknown"}),
    "dispatched": frozenset({"dispatched", "completed", "rejected", "unknown"}),
    "completed": frozenset({"completed"}),
    "rejected": frozenset({"rejected"}),
    "unknown": frozenset({"unknown", "completed", "rejected"}),
}

DELIVERY_STATES = frozenset({
    "staged",
    "sent",
    "consumed",
    "lost",
})

DELIVERY_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "staged": frozenset({"staged", "sent", "consumed", "lost"}),
    "sent": frozenset({"sent", "consumed", "lost"}),
    "consumed": frozenset({"consumed"}),
    "lost": frozenset({"lost"}),
}

RECEIPT_STATUSES = frozenset({
    "confirmed",
    "not_executed",
    "failed",
})

KNOWN_OUTCOMES = frozenset({
    "succeeded",
    "not_executed",
    "failed",
})

TERMINAL_SNAPSHOT_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "lost",
})

JS_EXECUTION_KEYS = frozenset({
    "version",
    "phase",
    "parent_call",
    "source_sha256",
    "execution_id",
    "session_id",
    "generation",
    "authority",
    "capability_catalog",
    "admitted_callbacks",
    "delivery_outbox",
    "child_receipt_refs",
    "fatal_fence",
    "counters",
    "terminal_result",
})

AUTHORITY_KEYS = frozenset({
    "goal",
    "scope",
    "context_epoch",
    "workspace",
    "destination",
})

PARENT_CALL_ALLOWED_KEYS = frozenset({
    "call_id",
    "operation_id",
    "name",
    "arguments",
    "arguments_json",
    "raw_arguments_json",
    "arguments_sha256",
    "schema_snapshot",
    "schema_sha256",
    "effect",
    "error_result",
    "context_epoch",
    "goal_revision",
    "scope_revision",
    "execution_container",
})

PARENT_CALL_REQUIRED_KEYS = frozenset({
    "call_id",
    "operation_id",
    "name",
    "arguments",
    "arguments_sha256",
    "effect",
})

CATALOG_ENTRY_ALLOWED_KEYS = frozenset({
    "method",
    "tool_name",
    "effect",
    "schema_snapshot",
    "schema_sha256",
    "computer_access",
    "extension_binding",
    "scope_context_supported",
    "preview_precondition",
    "title",
})

CATALOG_ENTRY_REQUIRED_KEYS = frozenset({
    "method",
    "tool_name",
    "effect",
    "schema_snapshot",
    "schema_sha256",
    "computer_access",
})

JS_PARENT_STAMP_KEYS = frozenset({
    "parent_operation_id",
    "parent_call_id",
    "session_id",
    "generation",
    "execution_id",
    "callback_id",
    "ordinal",
    "method",
    "arguments_sha256",
})

ADMITTED_CALLBACK_KEYS = frozenset({
    "callback_id",
    "ordinal",
    "method",
    "tool_name",
    "effect",
    "arguments_sha256",
    "call_id",
    "operation_id",
    "parent_operation_id",
    "parent_call_id",
    "session_id",
    "generation",
    "execution_id",
    "status",
})

ADMITTED_CALLBACK_ALLOWED_KEYS = ADMITTED_CALLBACK_KEYS | frozenset({
    "tool_arguments_sha256",
    "router_rejected",
})

OUTBOX_ITEM_KEYS = frozenset({
    "outbox_id",
    "parent_operation_id",
    "parent_call_id",
    "session_id",
    "generation",
    "execution_id",
    "callback_id",
    "ordinal",
    "method",
    "tool_name",
    "arguments_sha256",
    "call_id",
    "operation_id",
    "receipt_id",
    "receipt_status",
    "outcome",
    "not_executed",
    "result_ref",
    "payload",
    "payload_sha256",
    "delivery_state",
    "recorded_at",
})

OUTBOX_ITEM_ALLOWED_KEYS = OUTBOX_ITEM_KEYS | frozenset({
    "result_ref_sha256",
})

FATAL_FENCE_ALLOWED_KEYS = frozenset({
    "operation_id",
    "callback_id",
    "reason",
    "recorded_at",
})

FATAL_FENCE_REQUIRED_KEYS = frozenset({
    "operation_id",
    "reason",
})

COUNTER_KEYS = frozenset({
    "active_cpu_ms",
    "host_calls",
    "output_bytes",
})

TERMINAL_RESULT_KEYS = frozenset({
    "status",
    "value",
    "value_type",
    "stdout",
    "images",
    "error",
    "child_receipt_refs",
})

OPAQUE_IMAGE_PLACEHOLDER_KEYS = frozenset({
    "token_id",
    "source_child_operation_id",
    "source_receipt_id",
    "source_sha256",
    "mime_type",
    "width",
    "height",
    "alt",
})

OPAQUE_IMAGE_REQUIRED_KEYS = frozenset({
    "token_id",
    "source_child_operation_id",
    "source_receipt_id",
    "source_sha256",
    "mime_type",
    "width",
    "height",
})


RESULT_REF_REQUIRED_KEYS = frozenset({
    "id",
    "task_id",
    "tool",
    "operation_id",
    "goal_revision",
    "scope_revision",
    "context_epoch",
    "sha256",
    "bytes",
})

RESULT_REF_ALLOWED_KEYS = RESULT_REF_REQUIRED_KEYS | frozenset({
    "call_id",
    "resource_origin",
})

MAX_RESULT_REFERENCE_BYTES = 2 * 1024 * 1024


class JsStateValidationError(ValueError):
    """Raised when `row.js_execution` or a JS state transition violates invariants."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = str(code)
        self.detail = str(detail or code)


def _invalid_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise JsStateValidationError("agent_invalid_js_state", str(exc)) from None


def _sha256_hex(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _schema_digest(function_spec: dict[str, Any]) -> str:
    if not isinstance(function_spec, dict) or not isinstance(function_spec.get("name"), str):
        raise JsStateValidationError("agent_invalid_js_state", "Invalid schema_snapshot")
    params = function_spec.get("parameters", {})
    if not isinstance(params, dict):
        raise JsStateValidationError("agent_invalid_js_state", "Invalid schema_snapshot parameters")
    return _sha256_hex({"name": function_spec["name"], "parameters": params})


def _require_id(value: Any, field_name: str, pattern: re.Pattern[str] = SAFE_ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise JsStateValidationError("agent_invalid_js_state", f"Invalid identifier for {field_name}")
    return value


def _require_sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not SHA256_HEX_RE.fullmatch(value):
        raise JsStateValidationError("agent_invalid_js_state", f"Invalid sha256 digest for {field_name}")
    return value


def _require_non_negative_int(value: Any, field_name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum:
        raise JsStateValidationError("agent_invalid_js_state", f"Invalid integer for {field_name}")
    if maximum is not None and value > maximum:
        raise JsStateValidationError("agent_js_state_limit", f"{field_name} exceeds maximum {maximum}")
    return value


def validate_inert_image_token_id(value: Any, field_name: str = "image.token_id") -> str:
    """Validate that an image placeholder reference ID is an inert artifact ref, never a live capability URL or raw hex token."""
    if not isinstance(value, str):
        raise JsStateValidationError("agent_invalid_js_state", f"Invalid inert artifact reference for {field_name}")
    lowered = value.lower()
    if (
        _CAPABILITY_URL_RE.search(value)
        or _RAW_HEX_TOKEN_RE.fullmatch(value)
        or ":" in value
        or lowered.startswith(("ryn-image", "ryn_image", "cap_", "cap-", "data", "http", "file"))
    ):
        raise JsStateValidationError(
            "agent_js_live_capability_forbidden",
            f"Live capability URL or raw token hex is forbidden for {field_name}",
        )
    if not INERT_IMAGE_TOKEN_ID_RE.fullmatch(value):
        raise JsStateValidationError("agent_invalid_js_state", f"Invalid inert artifact reference for {field_name}")
    return value


def _validate_string_no_forbidden_media(value: str, *, context: str = "durable JS state") -> None:
    if _CAPABILITY_URL_RE.search(value):
        raise JsStateValidationError(
            "agent_js_live_capability_forbidden",
            f"Live ryn-image:cap capability URL is forbidden in {context}",
        )
    if _DATA_URL_RE.search(value):
        raise JsStateValidationError("agent_js_inline_media_forbidden", f"Inline data: URL is forbidden in {context}")
    stripped = value.strip()
    if len(stripped) >= 256 and len(stripped) % 4 == 0 and _BASE64_LONG_RUN_RE.fullmatch(stripped):
        raise JsStateValidationError("agent_js_inline_media_forbidden", f"Inline base64 blob is forbidden in {context}")


def _validate_bounded_json_tree(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = MAX_STATE_DEPTH,
    forbid_inline_media: bool = True,
) -> None:
    if depth > max_depth:
        raise JsStateValidationError("agent_js_state_depth_limit", "State JSON exceeds maximum nesting depth")
    if value is None or isinstance(value, bool):
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsStateValidationError("agent_invalid_js_state", "Non-finite float in JS state")
        return
    if isinstance(value, str):
        if forbid_inline_media:
            _validate_string_no_forbidden_media(value)
        return
    if isinstance(value, list):
        if len(value) > 512:
            raise JsStateValidationError("agent_js_state_limit", "Array length in JS state exceeds limit")
        for item in value:
            _validate_bounded_json_tree(item, depth=depth + 1, max_depth=max_depth, forbid_inline_media=forbid_inline_media)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise JsStateValidationError("agent_js_state_limit", "Object key count in JS state exceeds limit")
        for k, v in value.items():
            if not isinstance(k, str) or not k:
                raise JsStateValidationError("agent_invalid_js_state", "Object keys in JS state must be non-empty strings")
            if forbid_inline_media:
                if k.lower() in _FORBIDDEN_STATE_KEYS:
                    raise JsStateValidationError("agent_js_inline_media_forbidden", f"Forbidden key {k!r} in durable JS state")
                _validate_string_no_forbidden_media(k, context="durable JS state key")
            _validate_bounded_json_tree(v, depth=depth + 1, max_depth=max_depth, forbid_inline_media=forbid_inline_media)
        return
    raise JsStateValidationError("agent_invalid_js_state", f"Unsupported value type in JS state: {type(value).__name__}")


def _require_closed_dict(raw: Any, required_keys: frozenset[str], allowed_keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise JsStateValidationError("agent_invalid_js_state", f"{label} must be a dict")
    keys = set(raw.keys())
    missing = required_keys - keys
    extra = keys - allowed_keys
    if missing or extra:
        raise JsStateValidationError(
            "agent_invalid_js_state",
            f"{label} keys mismatch (missing={sorted(missing)}, extra={sorted(extra)})",
        )
    return raw


def validate_result_reference(
    raw_ref: Any,
    *,
    task_id: str | None = None,
    tool_name: str | None = None,
    operation_id: str | None = None,
    call_id: str | None = None,
    authority: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate a child result_ref ID or metadata dictionary before copying or committing."""
    if raw_ref is None:
        return None, None
    if isinstance(raw_ref, str):
        _validate_string_no_forbidden_media(raw_ref, context="result_ref")
        ref_id = _require_id(raw_ref, "result_ref", RESULT_REF_ID_RE)
        return ref_id, None
    if not isinstance(raw_ref, dict):
        raise JsStateValidationError("agent_invalid_js_state", "result_ref must be a string ID or reference dict")
    _validate_bounded_json_tree(raw_ref, depth=1, max_depth=6, forbid_inline_media=True)
    if len(_canonical_bytes(raw_ref)) > 8192:
        raise JsStateValidationError("agent_js_state_limit", "result_ref metadata exceeds byte limit")
    ref_dict = _require_closed_dict(raw_ref, RESULT_REF_REQUIRED_KEYS, RESULT_REF_ALLOWED_KEYS, "result_ref")
    ref_id = _require_id(ref_dict["id"], "result_ref.id", RESULT_REF_ID_RE)
    _require_id(ref_dict["task_id"], "result_ref.task_id", SAFE_ID_RE)
    if task_id is not None and ref_dict["task_id"] != task_id:
        raise JsStateValidationError("agent_js_task_mismatch", "result_ref.task_id mismatch")
    _require_id(ref_dict["tool"], "result_ref.tool", TOOL_NAME_RE)
    if tool_name is not None and ref_dict["tool"] != tool_name:
        raise JsStateValidationError("agent_js_callback_conflict", "result_ref.tool mismatch")
    _require_id(ref_dict["operation_id"], "result_ref.operation_id", SAFE_ID_RE)
    if operation_id is not None and ref_dict["operation_id"] != operation_id:
        raise JsStateValidationError("agent_js_callback_conflict", "result_ref.operation_id mismatch")
    if "call_id" in ref_dict and ref_dict["call_id"] is not None:
        _require_id(ref_dict["call_id"], "result_ref.call_id", CALL_ID_RE)
        if call_id is not None and ref_dict["call_id"] != call_id:
            raise JsStateValidationError("agent_js_callback_conflict", "result_ref.call_id mismatch")
    gr = _require_non_negative_int(ref_dict["goal_revision"], "result_ref.goal_revision", minimum=1)
    if isinstance(authority, dict) and authority.get("goal") is not None and gr != authority.get("goal"):
        raise JsStateValidationError("agent_js_callback_conflict", "result_ref.goal_revision mismatch")
    sr = _require_non_negative_int(ref_dict["scope_revision"], "result_ref.scope_revision", minimum=1)
    if isinstance(authority, dict) and authority.get("scope") is not None and sr != authority.get("scope"):
        raise JsStateValidationError("agent_js_callback_conflict", "result_ref.scope_revision mismatch")
    ce = _require_non_negative_int(ref_dict["context_epoch"], "result_ref.context_epoch", minimum=0)
    if isinstance(authority, dict) and authority.get("context_epoch") is not None and ce != authority.get("context_epoch"):
        raise JsStateValidationError("agent_js_callback_conflict", "result_ref.context_epoch mismatch")
    _require_sha256(ref_dict["sha256"], "result_ref.sha256")
    _require_non_negative_int(ref_dict["bytes"], "result_ref.bytes", minimum=0, maximum=MAX_RESULT_REFERENCE_BYTES)
    return ref_id, copy.deepcopy(ref_dict)


def validate_authority_stamp(raw: Any) -> dict[str, Any]:
    auth = _require_closed_dict(raw, AUTHORITY_KEYS, AUTHORITY_KEYS, "authority")
    goal = _require_non_negative_int(auth["goal"], "authority.goal", minimum=1)
    scope = _require_non_negative_int(auth["scope"], "authority.scope", minimum=1)
    epoch = _require_non_negative_int(auth["context_epoch"], "authority.context_epoch", minimum=0)
    workspace = auth["workspace"]
    if not isinstance(workspace, str) or len(workspace.encode("utf-8")) > 4096:
        raise JsStateValidationError("agent_invalid_js_state", "Invalid authority.workspace")
    destination = auth["destination"]
    if destination is not None and not isinstance(destination, (str, dict)):
        raise JsStateValidationError("agent_invalid_js_state", "Invalid authority.destination")
    if isinstance(destination, str) and len(destination.encode("utf-8")) > 1024:
        raise JsStateValidationError("agent_invalid_js_state", "authority.destination string too large")
    if isinstance(destination, dict):
        _validate_bounded_json_tree(destination, depth=1, max_depth=6)
        if len(_canonical_bytes(destination)) > 8192:
            raise JsStateValidationError("agent_js_state_limit", "authority.destination exceeds byte limit")
    return {
        "goal": goal,
        "scope": scope,
        "context_epoch": epoch,
        "workspace": workspace,
        "destination": copy.deepcopy(destination),
    }


def build_authority_stamp(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise JsStateValidationError("agent_invalid_js_state", "Task row must be a dict")
    goal_obj = row.get("goal")
    if not isinstance(goal_obj, dict):
        raise JsStateValidationError("agent_invalid_js_state", "Task row missing goal dict")
    local_exec = row.get("local_execution")
    workspace = local_exec.get("workspace_root", "") if isinstance(local_exec, dict) else ""
    stamp = {
        "goal": goal_obj.get("revision"),
        "scope": row.get("scope_revision"),
        "context_epoch": row.get("context_epoch", 0),
        "workspace": workspace,
        "destination": copy.deepcopy(row.get("model_destination")),
    }
    return validate_authority_stamp(stamp)


def authority_matches_row(authority: dict[str, Any], row: dict[str, Any]) -> bool:
    try:
        current = build_authority_stamp(row)
        validated = validate_authority_stamp(authority)
    except JsStateValidationError:
        return False
    return validated == current


def _validate_parent_call(raw: Any) -> tuple[dict[str, Any], str]:
    call = _require_closed_dict(raw, PARENT_CALL_REQUIRED_KEYS, PARENT_CALL_ALLOWED_KEYS, "parent_call")
    _require_id(call["call_id"], "parent_call.call_id", CALL_ID_RE)
    _require_id(call["operation_id"], "parent_call.operation_id", SAFE_ID_RE)
    if call["name"] != CONTAINER_TOOL_NAME:
        raise JsStateValidationError("agent_invalid_js_state", f"parent_call.name must be {CONTAINER_TOOL_NAME!r}")
    if call["effect"] not in {"read", "write"}:
        raise JsStateValidationError("agent_invalid_js_state", "parent_call.effect must be read or write")
    if call.get("error_result") is not None:
        raise JsStateValidationError("agent_invalid_js_state", "Rejected parent_call cannot own active js_execution")
    args = call["arguments"]
    if not isinstance(args, dict) or set(args.keys()) - {"code", "timeout_ms"}:
        raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments must have closed keys")
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments.code must be a non-empty string")
    if _CAPABILITY_URL_RE.search(code):
        raise JsStateValidationError("agent_js_live_capability_forbidden", "Live capability URL is forbidden in JS code")
    if _DATA_URL_RE.search(code):
        raise JsStateValidationError("agent_js_inline_media_forbidden", "Inline data: URL is forbidden in JS code")
    code_bytes = code.encode("utf-8")
    if len(code_bytes) > MAX_PARENT_CODE_BYTES:
        raise JsStateValidationError("agent_js_state_limit", "parent_call.arguments.code exceeds byte limit")
    if "timeout_ms" in args and args["timeout_ms"] is not None:
        _require_non_negative_int(args["timeout_ms"], "parent_call.arguments.timeout_ms", minimum=50, maximum=30000)
    args_sha = _require_sha256(call["arguments_sha256"], "parent_call.arguments_sha256")
    canonical_arg_bytes = _canonical_bytes(args)
    if len(canonical_arg_bytes) > ROUTER_MAX_ARGUMENT_BYTES:
        raise JsStateValidationError("agent_js_state_limit", "parent_call.arguments exceeds router cap")
    has_args_json = "arguments_json" in call and call["arguments_json"] is not None
    has_raw_args_json = "raw_arguments_json" in call and call["raw_arguments_json"] is not None
    if has_args_json:
        if not isinstance(call["arguments_json"], str):
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments_json must be a string")
        raw_arg_bytes = call["arguments_json"].encode("utf-8")
        if len(raw_arg_bytes) > ROUTER_MAX_ARGUMENT_BYTES:
            raise JsStateValidationError("agent_js_state_limit", "parent_call.arguments_json exceeds router cap")
        try:
            parsed_json_args = json.loads(call["arguments_json"], parse_constant=_invalid_json_constant)
        except (TypeError, ValueError, RecursionError) as exc:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments_json is not valid JSON") from exc
        if parsed_json_args != args:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments does not match arguments_json")
        if hashlib.sha256(raw_arg_bytes).hexdigest() != args_sha:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments_sha256 does not match arguments_json")
    if has_raw_args_json:
        if not isinstance(call["raw_arguments_json"], str):
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.raw_arguments_json must be a string")
        raw_bytes = call["raw_arguments_json"].encode("utf-8")
        if len(raw_bytes) > ROUTER_MAX_ARGUMENT_BYTES:
            raise JsStateValidationError("agent_js_state_limit", "parent_call.raw_arguments_json exceeds router cap")
        if has_args_json and call["raw_arguments_json"] != call["arguments_json"]:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.raw_arguments_json does not match arguments_json")
        try:
            parsed_raw_args = json.loads(call["raw_arguments_json"], parse_constant=_invalid_json_constant)
        except (TypeError, ValueError, RecursionError) as exc:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.raw_arguments_json is not valid JSON") from exc
        if parsed_raw_args != args:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments does not match raw_arguments_json")
        if hashlib.sha256(raw_bytes).hexdigest() != args_sha:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments_sha256 does not match raw_arguments_json")
    if not has_args_json and not has_raw_args_json:
        if hashlib.sha256(canonical_arg_bytes).hexdigest() != args_sha:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments_sha256 does not match canonical arguments")
    if "schema_snapshot" in call and call["schema_snapshot"] is not None:
        expected_schema_sha = _require_sha256(call.get("schema_sha256"), "parent_call.schema_sha256")
        if _schema_digest(call["schema_snapshot"]) != expected_schema_sha:
            raise JsStateValidationError("agent_invalid_js_state", "parent_call.schema_sha256 mismatch")
    source_sha256 = hashlib.sha256(code_bytes).hexdigest()
    return copy.deepcopy(call), source_sha256


def validate_capability_catalog(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise JsStateValidationError("agent_invalid_js_state", "capability_catalog must be a dict")
    if len(raw) > len(SKY_METHOD_SPECS):
        raise JsStateValidationError("agent_js_state_limit", "capability_catalog has too many entries")
    validated: dict[str, dict[str, Any]] = {}
    for method, entry in raw.items():
        if not isinstance(method, str) or method not in SKY_METHOD_SPECS:
            raise JsStateValidationError("agent_invalid_js_state", f"Unsupported Sky method in catalog: {method!r}")
        item = _require_closed_dict(
            entry,
            CATALOG_ENTRY_REQUIRED_KEYS,
            CATALOG_ENTRY_ALLOWED_KEYS,
            f"capability_catalog[{method}]",
        )
        if item["method"] != method:
            raise JsStateValidationError("agent_invalid_js_state", "Catalog entry method key mismatch")
        tool_name = _require_id(item["tool_name"], f"capability_catalog[{method}].tool_name", TOOL_NAME_RE)
        if tool_name == CONTAINER_TOOL_NAME:
            raise JsStateValidationError("agent_invalid_js_state", "Container tool cannot be a child target")
        expected_effect = SKY_METHOD_SPECS[method]["effect"]
        if item["effect"] != expected_effect:
            raise JsStateValidationError("agent_invalid_js_state", f"Catalog effect mismatch for {method}")
        if type(item["computer_access"]) is not bool:
            raise JsStateValidationError("agent_invalid_js_state", "computer_access must be a bool")
        snapshot = item["schema_snapshot"]
        if not isinstance(snapshot, dict) or snapshot.get("name") != tool_name:
            raise JsStateValidationError("agent_invalid_js_state", "schema_snapshot.name must match tool_name")
        schema_sha = _require_sha256(item["schema_sha256"], f"capability_catalog[{method}].schema_sha256")
        if _schema_digest(snapshot) != schema_sha:
            raise JsStateValidationError("agent_invalid_js_state", "Catalog schema_sha256 mismatch")
        if "extension_binding" in item and item["extension_binding"] is not None:
            if not isinstance(item["extension_binding"], dict):
                raise JsStateValidationError("agent_invalid_js_state", "extension_binding must be a dict")
            _validate_bounded_json_tree(item["extension_binding"], depth=1, max_depth=6)
        for bool_key in ("scope_context_supported", "preview_precondition"):
            if bool_key in item and type(item[bool_key]) is not bool:
                raise JsStateValidationError("agent_invalid_js_state", f"{bool_key} must be a bool")
        if "title" in item and item["title"] is not None:
            if not isinstance(item["title"], str) or len(item["title"]) > 256:
                raise JsStateValidationError("agent_invalid_js_state", "Catalog title invalid")
        validated[method] = copy.deepcopy(item)
    return validated


def validate_js_parent_stamp(stamp: Any, *, js_state: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _require_closed_dict(stamp, JS_PARENT_STAMP_KEYS, JS_PARENT_STAMP_KEYS, "js_parent")
    parent_op_id = _require_id(raw["parent_operation_id"], "js_parent.parent_operation_id", SAFE_ID_RE)
    parent_call_id = _require_id(raw["parent_call_id"], "js_parent.parent_call_id", CALL_ID_RE)
    session_id = _require_id(raw["session_id"], "js_parent.session_id", SAFE_ID_RE)
    generation = _require_non_negative_int(raw["generation"], "js_parent.generation", minimum=1)
    execution_id = _require_id(raw["execution_id"], "js_parent.execution_id", SAFE_ID_RE)
    callback_id = _require_id(raw["callback_id"], "js_parent.callback_id", SAFE_ID_RE)
    ordinal = _require_non_negative_int(raw["ordinal"], "js_parent.ordinal", minimum=0, maximum=MAX_ADMITTED_CALLBACKS)
    method = raw["method"]
    if not isinstance(method, str) or method not in SKY_METHOD_SPECS:
        raise JsStateValidationError("agent_invalid_js_state", f"Unsupported method in js_parent: {method!r}")
    args_sha = _require_sha256(raw["arguments_sha256"], "js_parent.arguments_sha256")
    if js_state is not None:
        parent_call = js_state.get("parent_call") or {}
        if (
            parent_op_id != parent_call.get("operation_id")
            or parent_call_id != parent_call.get("call_id")
            or session_id != js_state.get("session_id")
            or generation != js_state.get("generation")
            or execution_id != js_state.get("execution_id")
        ):
            raise JsStateValidationError("agent_js_parent_mismatch", "js_parent does not match active js_execution")
        admitted = (js_state.get("admitted_callbacks") or {}).get(callback_id)
        if not isinstance(admitted, dict):
            raise JsStateValidationError("agent_js_unknown_callback", "Callback is not registered in admitted_callbacks")
        if (
            admitted.get("ordinal") != ordinal
            or admitted.get("method") != method
            or admitted.get("arguments_sha256") != args_sha
        ):
            raise JsStateValidationError("agent_js_callback_conflict", "js_parent metadata conflicts with admitted callback")
    return copy.deepcopy(raw)


def _validate_admitted_callbacks(
    raw: Any,
    *,
    parent_operation_id: str,
    parent_call_id: str,
    session_id: str,
    generation: int,
    execution_id: str,
    capability_catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise JsStateValidationError("agent_invalid_js_state", "admitted_callbacks must be a dict")
    if len(raw) > MAX_ADMITTED_CALLBACKS:
        raise JsStateValidationError("agent_js_state_limit", "admitted_callbacks exceeds max count")
    seen_ordinals: set[int] = set()
    seen_call_ids: set[str] = set()
    seen_op_ids: set[str] = set()
    validated: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        cid = _require_id(key, "admitted_callbacks.key", SAFE_ID_RE)
        item = _require_closed_dict(
            entry,
            ADMITTED_CALLBACK_KEYS,
            ADMITTED_CALLBACK_ALLOWED_KEYS,
            f"admitted_callbacks[{cid}]",
        )
        if item["callback_id"] != cid:
            raise JsStateValidationError("agent_invalid_js_state", "admitted_callbacks callback_id key mismatch")
        ordinal = _require_non_negative_int(item["ordinal"], "admitted_callback.ordinal", minimum=0, maximum=MAX_ADMITTED_CALLBACKS)
        if ordinal in seen_ordinals:
            raise JsStateValidationError("agent_invalid_js_state", "Duplicate callback ordinal in admitted_callbacks")
        seen_ordinals.add(ordinal)
        method = item["method"]
        if not isinstance(method, str) or method not in SKY_METHOD_SPECS:
            raise JsStateValidationError("agent_invalid_js_state", "Invalid method in admitted_callbacks")
        tool_name = _require_id(item["tool_name"], "admitted_callback.tool_name", TOOL_NAME_RE)
        if method in capability_catalog:
            cat_entry = capability_catalog[method]
            if tool_name != cat_entry["tool_name"] or item["effect"] != cat_entry["effect"]:
                raise JsStateValidationError("agent_invalid_js_state", "admitted_callback does not match capability_catalog")
        elif item["effect"] != SKY_METHOD_SPECS[method]["effect"]:
            raise JsStateValidationError("agent_invalid_js_state", "admitted_callback effect mismatch")
        _require_sha256(item["arguments_sha256"], "admitted_callback.arguments_sha256")
        if "tool_arguments_sha256" in item and item["tool_arguments_sha256"] is not None:
            _require_sha256(item["tool_arguments_sha256"], "admitted_callback.tool_arguments_sha256")
        if "router_rejected" in item and item["router_rejected"] is not None:
            if type(item["router_rejected"]) is not bool:
                raise JsStateValidationError("agent_invalid_js_state", "admitted_callback.router_rejected must be a bool")
        call_id = _require_id(item["call_id"], "admitted_callback.call_id", CALL_ID_RE)
        op_id = _require_id(item["operation_id"], "admitted_callback.operation_id", SAFE_ID_RE)
        if call_id in seen_call_ids or op_id in seen_op_ids:
            raise JsStateValidationError("agent_invalid_js_state", "Duplicate call_id or operation_id in admitted_callbacks")
        seen_call_ids.add(call_id)
        seen_op_ids.add(op_id)
        if (
            item["parent_operation_id"] != parent_operation_id
            or item["parent_call_id"] != parent_call_id
            or item["session_id"] != session_id
            or item["generation"] != generation
            or item["execution_id"] != execution_id
        ):
            raise JsStateValidationError("agent_js_parent_mismatch", "admitted_callback parent/generation linkage mismatch")
        if item["status"] not in CALLBACK_STATUSES:
            raise JsStateValidationError("agent_invalid_js_state", f"Invalid admitted_callback status: {item['status']!r}")
        validated[cid] = copy.deepcopy(item)
    return validated


def _validate_delivery_outbox(
    raw: Any,
    *,
    parent_operation_id: str,
    parent_call_id: str,
    session_id: str,
    generation: int,
    execution_id: str,
    admitted_callbacks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise JsStateValidationError("agent_invalid_js_state", "delivery_outbox must be a list")
    if len(raw) > MAX_OUTBOX_ITEMS:
        raise JsStateValidationError("agent_js_state_limit", "delivery_outbox exceeds max items")
    seen_outbox_ids: set[str] = set()
    seen_callbacks: set[str] = set()
    seen_operations: set[str] = set()
    validated: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw):
        item = _require_closed_dict(entry, OUTBOX_ITEM_KEYS, OUTBOX_ITEM_ALLOWED_KEYS, f"delivery_outbox[{idx}]")
        outbox_id = _require_id(item["outbox_id"], "outbox.outbox_id", SAFE_ID_RE)
        if outbox_id in seen_outbox_ids:
            raise JsStateValidationError("agent_invalid_js_state", "Duplicate outbox_id in delivery_outbox")
        seen_outbox_ids.add(outbox_id)
        cid = _require_id(item["callback_id"], "outbox.callback_id", SAFE_ID_RE)
        if cid in seen_callbacks:
            raise JsStateValidationError("agent_invalid_js_state", "Duplicate callback_id in delivery_outbox")
        seen_callbacks.add(cid)
        if cid not in admitted_callbacks:
            raise JsStateValidationError("agent_invalid_js_state", "Outbox item references unknown callback_id")
        admitted = admitted_callbacks[cid]
        if (
            item["parent_operation_id"] != parent_operation_id
            or item["parent_call_id"] != parent_call_id
            or item["session_id"] != session_id
            or item["generation"] != generation
            or item["execution_id"] != execution_id
        ):
            raise JsStateValidationError("agent_js_parent_mismatch", "Outbox item parent/generation linkage mismatch")
        if (
            item["ordinal"] != admitted["ordinal"]
            or item["method"] != admitted["method"]
            or item["tool_name"] != admitted["tool_name"]
            or item["arguments_sha256"] != admitted["arguments_sha256"]
            or item["call_id"] != admitted["call_id"]
            or item["operation_id"] != admitted["operation_id"]
        ):
            raise JsStateValidationError("agent_js_callback_conflict", "Outbox item conflicts with admitted_callbacks")
        if item["operation_id"] in seen_operations:
            raise JsStateValidationError("agent_invalid_js_state", "Duplicate operation_id in delivery_outbox")
        seen_operations.add(item["operation_id"])
        receipt_id = item["receipt_id"]
        if receipt_id is not None:
            _require_id(receipt_id, "outbox.receipt_id", RECEIPT_ID_RE)
            if receipt_id != "receipt_" + item["operation_id"]:
                raise JsStateValidationError("agent_invalid_js_state", "Outbox receipt_id must match operation_id")
        receipt_status = item["receipt_status"]
        if receipt_status not in RECEIPT_STATUSES:
            raise JsStateValidationError("agent_invalid_js_state", f"Invalid outbox receipt_status: {receipt_status!r}")
        if receipt_id is None and receipt_status != "not_executed":
            raise JsStateValidationError("agent_invalid_js_state", "Unrecorded receipt is only valid for not_executed router rejections")
        outcome = item["outcome"]
        if outcome not in KNOWN_OUTCOMES:
            raise JsStateValidationError("agent_invalid_js_state", f"Invalid outbox outcome: {outcome!r}")
        if type(item["not_executed"]) is not bool:
            raise JsStateValidationError("agent_invalid_js_state", "outbox.not_executed must be a bool")
        if receipt_status == "confirmed" and (outcome != "succeeded" or item["not_executed"] is not False):
            raise JsStateValidationError("agent_invalid_js_state", "Confirmed outbox item must have outcome=succeeded and not_executed=False")
        if receipt_status == "not_executed" and (outcome != "not_executed" or item["not_executed"] is not True):
            raise JsStateValidationError("agent_invalid_js_state", "not_executed outbox item must have outcome=not_executed and not_executed=True")
        if receipt_status == "failed" and (outcome != "failed" or item["not_executed"] is not False):
            raise JsStateValidationError("agent_invalid_js_state", "failed outbox item must have outcome=failed and not_executed=False")
        if item["result_ref"] is not None:
            _require_id(item["result_ref"], "outbox.result_ref", RESULT_REF_ID_RE)
            if "result_ref_sha256" in item and item["result_ref_sha256"] is not None:
                _require_sha256(item["result_ref_sha256"], "outbox.result_ref_sha256")
        elif item.get("result_ref_sha256") is not None:
            raise JsStateValidationError("agent_invalid_js_state", "outbox.result_ref_sha256 requires a non-null result_ref")
        payload = item["payload"]
        _validate_bounded_json_tree(payload, depth=1, max_depth=MAX_STATE_DEPTH - 2, forbid_inline_media=True)
        payload_bytes = _canonical_bytes(payload)
        if len(payload_bytes) > MAX_OUTBOX_PAYLOAD_BYTES:
            raise JsStateValidationError("agent_js_state_limit", "Outbox payload exceeds MAX_OUTBOX_PAYLOAD_BYTES")
        payload_sha = _require_sha256(item["payload_sha256"], "outbox.payload_sha256")
        if hashlib.sha256(payload_bytes).hexdigest() != payload_sha:
            raise JsStateValidationError("agent_invalid_js_state", "Outbox payload_sha256 mismatch")
        if item["delivery_state"] not in DELIVERY_STATES:
            raise JsStateValidationError("agent_invalid_js_state", f"Invalid delivery_state: {item['delivery_state']!r}")
        if not isinstance(item["recorded_at"], str) or not item["recorded_at"].strip() or len(item["recorded_at"]) > 64:
            raise JsStateValidationError("agent_invalid_js_state", "Invalid outbox.recorded_at")
        validated.append(copy.deepcopy(item))
    return validated


def _validate_terminal_result(raw: Any, *, phase: str, child_receipt_refs: list[str]) -> dict[str, Any] | None:
    if raw is None:
        if phase == "completed":
            raise JsStateValidationError("agent_invalid_js_state", "completed phase requires terminal_result")
        return None
    if phase not in {"completed", "continuation_lost", "cancelled", "fenced_unknown"}:
        raise JsStateValidationError("agent_invalid_js_state", f"Non-terminal phase {phase!r} cannot have terminal_result")
    term = _require_closed_dict(raw, TERMINAL_RESULT_KEYS, TERMINAL_RESULT_KEYS, "terminal_result")
    status = term["status"]
    if status not in TERMINAL_SNAPSHOT_STATUSES:
        raise JsStateValidationError("agent_invalid_js_state", "Invalid terminal_result.status")
    if phase == "completed" and status not in {"completed", "failed"}:
        raise JsStateValidationError("agent_invalid_js_state", "completed phase requires status in {'completed', 'failed'}")
    if phase == "cancelled" and status != "cancelled":
        raise JsStateValidationError("agent_invalid_js_state", "cancelled phase requires status='cancelled'")
    if phase == "continuation_lost" and status != "lost":
        raise JsStateValidationError("agent_invalid_js_state", "continuation_lost phase requires status='lost'")
    if not isinstance(term["value_type"], str) or not term["value_type"] or len(term["value_type"]) > 64:
        raise JsStateValidationError("agent_invalid_js_state", "Invalid terminal_result.value_type")
    _validate_bounded_json_tree(term["value"], depth=1, max_depth=MAX_STATE_DEPTH - 2, forbid_inline_media=True)
    if len(_canonical_bytes(term["value"])) > MAX_RESULT_BYTES:
        raise JsStateValidationError("agent_js_state_limit", "terminal_result.value exceeds MAX_RESULT_BYTES")
    stdout = term["stdout"]
    if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise JsStateValidationError("agent_js_state_limit", "terminal_result.stdout invalid or too large")
    if _CAPABILITY_URL_RE.search(stdout):
        raise JsStateValidationError("agent_js_live_capability_forbidden", "Live capability URL is forbidden in stdout")
    if _DATA_URL_RE.search(stdout):
        raise JsStateValidationError("agent_js_inline_media_forbidden", "Inline data: URL is forbidden in stdout")
    images = term["images"]
    if not isinstance(images, list) or len(images) > MAX_TERMINAL_IMAGES:
        raise JsStateValidationError("agent_js_state_limit", "terminal_result.images invalid or exceeds limit")
    for idx, img in enumerate(images):
        placeholder = _require_closed_dict(
            img,
            OPAQUE_IMAGE_REQUIRED_KEYS,
            OPAQUE_IMAGE_PLACEHOLDER_KEYS,
            f"terminal_result.images[{idx}]",
        )
        validate_inert_image_token_id(placeholder["token_id"], "image.token_id")
        child_op_id = _require_id(placeholder["source_child_operation_id"], "image.source_child_operation_id", CALL_ID_RE)
        if _RAW_HEX_TOKEN_RE.fullmatch(child_op_id) or _CAPABILITY_URL_RE.search(child_op_id):
            raise JsStateValidationError(
                "agent_js_live_capability_forbidden",
                "image.source_child_operation_id cannot be a live capability URL or raw hex token",
            )
        source_rec_id = _require_id(placeholder["source_receipt_id"], "image.source_receipt_id", RECEIPT_ID_RE)
        if source_rec_id != "receipt_" + child_op_id:
            raise JsStateValidationError(
                "agent_invalid_js_state",
                "image.source_receipt_id must match image.source_child_operation_id",
            )
        _require_sha256(placeholder["source_sha256"], "image.source_sha256")
        if placeholder["mime_type"] not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise JsStateValidationError("agent_invalid_js_state", "Invalid opaque image mime_type")
        _require_non_negative_int(placeholder["width"], "image.width", minimum=1, maximum=16384)
        _require_non_negative_int(placeholder["height"], "image.height", minimum=1, maximum=16384)
        if "alt" in placeholder and placeholder["alt"] is not None:
            if not isinstance(placeholder["alt"], str) or len(placeholder["alt"]) > 512:
                raise JsStateValidationError("agent_invalid_js_state", "Invalid opaque image alt")
            if _CAPABILITY_URL_RE.search(placeholder["alt"]):
                raise JsStateValidationError("agent_js_live_capability_forbidden", "Live capability URL forbidden in image alt")
            if _DATA_URL_RE.search(placeholder["alt"]) or _FORBIDDEN_URL_SCHEME_RE.match(placeholder["alt"].strip()):
                raise JsStateValidationError("agent_js_inline_media_forbidden", "URL scheme forbidden in image alt")
    err = term["error"]
    if status == "completed" and err is not None:
        raise JsStateValidationError("agent_invalid_js_state", "completed terminal_result cannot include error")
    if err is not None:
        err_dict = _require_closed_dict(
            err,
            frozenset({"code", "message"}),
            frozenset({"code", "message", "not_executed", "outcome"}),
            "terminal_result.error",
        )
        _require_id(err_dict["code"], "terminal_result.error.code", SAFE_ID_RE)
        if not isinstance(err_dict["message"], str) or len(err_dict["message"]) > MAX_ERROR_MESSAGE_CHARS:
            raise JsStateValidationError("agent_invalid_js_state", "Invalid terminal_result.error.message")
    refs = term["child_receipt_refs"]
    if not isinstance(refs, list) or refs != child_receipt_refs:
        raise JsStateValidationError("agent_invalid_js_state", "terminal_result.child_receipt_refs must match state child_receipt_refs")
    return copy.deepcopy(term)


def validate_js_state(raw: Any, *, task_row: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Validate `row.js_execution` and return a detached deep copy (or None when absent)."""
    if raw is None:
        return None
    state = _require_closed_dict(raw, JS_EXECUTION_KEYS, JS_EXECUTION_KEYS, "js_execution")
    _validate_bounded_json_tree(state, depth=0, max_depth=MAX_STATE_DEPTH, forbid_inline_media=True)
    serialized = _canonical_bytes(state)
    if len(serialized) > MAX_JS_STATE_BYTES:
        raise JsStateValidationError("agent_js_state_limit", "js_execution exceeds MAX_JS_STATE_BYTES")

    if type(state["version"]) is not int or state["version"] != JS_STATE_VERSION:
        raise JsStateValidationError("agent_js_state_version_unsupported", "Unsupported js_execution version")

    phase = state["phase"]
    if not isinstance(phase, str) or phase not in JS_PHASES:
        raise JsStateValidationError("agent_invalid_js_state", f"Invalid js_execution phase: {phase!r}")

    parent_call, computed_source_sha = _validate_parent_call(state["parent_call"])
    source_sha = _require_sha256(state["source_sha256"], "source_sha256")
    if source_sha != computed_source_sha:
        raise JsStateValidationError("agent_invalid_js_state", "source_sha256 does not match parent_call code")

    execution_id = _require_id(state["execution_id"], "execution_id", SAFE_ID_RE)
    session_id = _require_id(state["session_id"], "session_id", SAFE_ID_RE)
    generation = _require_non_negative_int(state["generation"], "generation", minimum=1)
    authority = validate_authority_stamp(state["authority"])
    capability_catalog = validate_capability_catalog(state["capability_catalog"])

    admitted_callbacks = _validate_admitted_callbacks(
        state["admitted_callbacks"],
        parent_operation_id=parent_call["operation_id"],
        parent_call_id=parent_call["call_id"],
        session_id=session_id,
        generation=generation,
        execution_id=execution_id,
        capability_catalog=capability_catalog,
    )
    delivery_outbox = _validate_delivery_outbox(
        state["delivery_outbox"],
        parent_operation_id=parent_call["operation_id"],
        parent_call_id=parent_call["call_id"],
        session_id=session_id,
        generation=generation,
        execution_id=execution_id,
        admitted_callbacks=admitted_callbacks,
    )

    raw_refs = state["child_receipt_refs"]
    if not isinstance(raw_refs, list) or len(raw_refs) > MAX_CHILD_RECEIPT_REFS:
        raise JsStateValidationError("agent_invalid_js_state", "Invalid child_receipt_refs list")
    child_receipt_refs: list[str] = []
    seen_refs: set[str] = set()
    known_child_receipt_ids = {"receipt_" + item["operation_id"] for item in admitted_callbacks.values()}
    for ref in raw_refs:
        rid = _require_id(ref, "child_receipt_refs.item", RECEIPT_ID_RE)
        if rid in seen_refs:
            raise JsStateValidationError("agent_invalid_js_state", "Duplicate receipt_id in child_receipt_refs")
        if rid not in known_child_receipt_ids:
            raise JsStateValidationError("agent_invalid_js_state", "child_receipt_refs contains unknown child operation")
        seen_refs.add(rid)
        child_receipt_refs.append(rid)

    raw_fence = state["fatal_fence"]
    fatal_fence: dict[str, Any] | None = None
    if raw_fence is not None:
        fence = _require_closed_dict(raw_fence, FATAL_FENCE_REQUIRED_KEYS, FATAL_FENCE_ALLOWED_KEYS, "fatal_fence")
        fence_op_id = _require_id(fence["operation_id"], "fatal_fence.operation_id", SAFE_ID_RE)
        known_ops = {item["operation_id"] for item in admitted_callbacks.values()}
        if fence_op_id not in known_ops:
            raise JsStateValidationError("agent_invalid_js_state", "fatal_fence.operation_id does not match an admitted child")
        _require_id(fence["reason"], "fatal_fence.reason", SAFE_ID_RE)
        if "callback_id" in fence and fence["callback_id"] is not None:
            cid = _require_id(fence["callback_id"], "fatal_fence.callback_id", SAFE_ID_RE)
            if cid not in admitted_callbacks or admitted_callbacks[cid]["operation_id"] != fence_op_id:
                raise JsStateValidationError("agent_invalid_js_state", "fatal_fence.callback_id mismatch")
        if "recorded_at" in fence and fence["recorded_at"] is not None:
            if not isinstance(fence["recorded_at"], str) or len(fence["recorded_at"]) > 64:
                raise JsStateValidationError("agent_invalid_js_state", "Invalid fatal_fence.recorded_at")
        if phase not in {"fenced_unknown", "continuation_lost", "cancelled"}:
            raise JsStateValidationError("agent_invalid_js_state", f"Phase {phase!r} is inconsistent with active fatal_fence")
        fatal_fence = copy.deepcopy(fence)
    elif phase == "fenced_unknown":
        raise JsStateValidationError("agent_invalid_js_state", "fenced_unknown phase requires a non-null fatal_fence")

    raw_counters = _require_closed_dict(state["counters"], COUNTER_KEYS, COUNTER_KEYS, "counters")
    counters = {
        "active_cpu_ms": _require_non_negative_int(raw_counters["active_cpu_ms"], "counters.active_cpu_ms", maximum=MAX_ACTIVE_CPU_MS),
        "host_calls": _require_non_negative_int(raw_counters["host_calls"], "counters.host_calls", maximum=MAX_ADMITTED_CALLBACKS),
        "output_bytes": _require_non_negative_int(raw_counters["output_bytes"], "counters.output_bytes", maximum=MAX_OUTPUT_BYTES),
    }
    if counters["host_calls"] < len(admitted_callbacks):
        raise JsStateValidationError("agent_invalid_js_state", "counters.host_calls cannot be less than admitted_callbacks count")

    terminal_result = _validate_terminal_result(
        state["terminal_result"],
        phase=phase,
        child_receipt_refs=child_receipt_refs,
    )

    if task_row is not None:
        if not isinstance(task_row, dict):
            raise JsStateValidationError("agent_invalid_js_state", "task_row must be a dict")
        # Verify the parent assistant tool call exists in durable messages and matches parent_call.
        parent_call_id = parent_call["call_id"]
        matching_tcs: list[dict[str, Any]] = []
        for msg in (task_row.get("messages") or []):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in (msg.get("tool_calls") or []):
                if isinstance(tc, dict) and (tc.get("id") == parent_call_id or tc.get("call_id") == parent_call_id):
                    matching_tcs.append(tc)
        if not matching_tcs:
            raise JsStateValidationError("agent_js_dangling_parent", "parent_call has no matching assistant tool_call in task messages")
        for tc in matching_tcs:
            func = tc.get("function")
            if isinstance(func, dict):
                if "name" in func and func.get("name") != parent_call["name"]:
                    raise JsStateValidationError("agent_invalid_js_state", "parent_call.name does not match assistant tool_call")
                if "arguments" in func and func["arguments"] is not None:
                    tc_args_raw = func["arguments"]
                    if isinstance(tc_args_raw, str):
                        try:
                            tc_args_parsed = json.loads(tc_args_raw)
                        except (TypeError, ValueError) as exc:
                            raise JsStateValidationError("agent_invalid_js_state", "Assistant tool_call arguments JSON invalid") from exc
                    elif isinstance(tc_args_raw, dict):
                        tc_args_parsed = tc_args_raw
                    else:
                        raise JsStateValidationError("agent_invalid_js_state", "Assistant tool_call arguments invalid")
                    if tc_args_parsed != parent_call["arguments"]:
                        raise JsStateValidationError("agent_invalid_js_state", "parent_call.arguments does not match assistant tool_call arguments")
        row_result_refs = task_row.get("result_refs")
        if row_result_refs is not None:
            if not isinstance(row_result_refs, list):
                raise JsStateValidationError("agent_invalid_js_state", "task_row.result_refs must be a list")
            for item in delivery_outbox:
                if item["result_ref"] is not None:
                    for r in row_result_refs:
                        if isinstance(r, dict) and r.get("id") == item["result_ref"]:
                            _, validated_ref = validate_result_reference(
                                r,
                                task_id=task_row.get("id") if isinstance(task_row.get("id"), str) else None,
                                tool_name=item["tool_name"],
                                operation_id=item["operation_id"],
                                call_id=item["call_id"],
                                authority=authority,
                            )
                            if (
                                item.get("result_ref_sha256") is not None
                                and validated_ref is not None
                                and _sha256_hex(validated_ref) != item["result_ref_sha256"]
                            ):
                                raise JsStateValidationError(
                                    "agent_js_outbox_conflict",
                                    "result_ref metadata conflicts with committed outbox result_ref_sha256",
                                )

    return {
        "version": JS_STATE_VERSION,
        "phase": phase,
        "parent_call": parent_call,
        "source_sha256": source_sha,
        "execution_id": execution_id,
        "session_id": session_id,
        "generation": generation,
        "authority": authority,
        "capability_catalog": capability_catalog,
        "admitted_callbacks": admitted_callbacks,
        "delivery_outbox": delivery_outbox,
        "child_receipt_refs": child_receipt_refs,
        "fatal_fence": fatal_fence,
        "counters": counters,
        "terminal_result": terminal_result,
    }


def validate_task_js_state(row: dict[str, Any]) -> dict[str, Any] | None:
    """Validate `row.get('js_execution')`; missing legacy field returns None."""
    if not isinstance(row, dict):
        raise JsStateValidationError("agent_invalid_js_state", "Task row must be a dict")
    if "js_execution" not in row or row["js_execution"] is None:
        return None
    return validate_js_state(row["js_execution"], task_row=row)


def validate_phase_transition(
    current_phase: str,
    target_phase: str,
    *,
    fatal_fence: dict[str, Any] | None = None,
    trusted_reconcile: bool = False,
) -> str:
    allowed = PHASE_TRANSITION_TABLE.get(current_phase)
    if allowed is None or target_phase not in allowed:
        raise JsStateValidationError(
            "agent_invalid_js_phase_transition",
            f"Illegal JS phase transition: {current_phase!r} -> {target_phase!r}",
        )
    if current_phase == "fenced_unknown" and target_phase == "delivering" and not trusted_reconcile:
        raise JsStateValidationError(
            "agent_js_fenced_unknown",
            "Cannot leave fenced_unknown to delivering without trusted reconciliation proof",
        )
    if fatal_fence is not None and not trusted_reconcile and target_phase not in {"fenced_unknown", "continuation_lost", "cancelled"}:
        raise JsStateValidationError(
            "agent_js_fenced_unknown",
            f"Active fatal_fence forbids transition to {target_phase!r}",
        )
    return target_phase


def validate_delivery_transition(current_state: str, target_state: str) -> str:
    allowed = DELIVERY_STATE_TRANSITIONS.get(current_state)
    if allowed is None or target_state not in allowed:
        raise JsStateValidationError(
            "agent_invalid_js_delivery_transition",
            f"Illegal outbox delivery transition: {current_state!r} -> {target_state!r}",
        )
    return target_state


def validate_callback_status_transition(current_status: str, target_status: str) -> str:
    allowed = CALLBACK_STATUS_TRANSITIONS.get(current_status)
    if allowed is None or target_status not in allowed:
        raise JsStateValidationError(
            "agent_invalid_js_callback_transition",
            f"Illegal callback status transition: {current_status!r} -> {target_status!r}",
        )
    return target_status


def create_js_execution_state(
    row: dict[str, Any],
    parent_call: dict[str, Any],
    *,
    execution_id: str,
    session_id: str,
    generation: int = 1,
    capability_catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct a validated initial `starting` private `js_execution` state."""
    validated_parent, source_sha256 = _validate_parent_call(parent_call)
    raw = {
        "version": JS_STATE_VERSION,
        "phase": "starting",
        "parent_call": validated_parent,
        "source_sha256": source_sha256,
        "execution_id": execution_id,
        "session_id": session_id,
        "generation": generation,
        "authority": build_authority_stamp(row),
        "capability_catalog": copy.deepcopy(capability_catalog or {}),
        "admitted_callbacks": {},
        "delivery_outbox": [],
        "child_receipt_refs": [],
        "fatal_fence": None,
        "counters": {"active_cpu_ms": 0, "host_calls": 0, "output_bytes": 0},
        "terminal_result": None,
    }
    return validate_js_state(raw, task_row=row)  # type: ignore[return-value]


def plan_fatal_fence_js_state(
    js_state: dict[str, Any],
    *,
    operation_id: str,
    callback_id: str | None = None,
    reason: str = "agent_write_outcome_unknown",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Return a detached copy of `js_state` transitioned to `fenced_unknown`."""
    current = validate_js_state(js_state)
    if current is None:
        raise JsStateValidationError("agent_invalid_js_state", "Missing js_execution state")
    validate_phase_transition(current["phase"], "fenced_unknown", fatal_fence=current["fatal_fence"])
    fence: dict[str, Any] = {"operation_id": operation_id, "reason": reason}
    if callback_id is not None:
        fence["callback_id"] = callback_id
    if recorded_at is not None:
        fence["recorded_at"] = recorded_at
    if callback_id and callback_id in current["admitted_callbacks"]:
        cb = current["admitted_callbacks"][callback_id]
        validate_callback_status_transition(cb["status"], "unknown")
        cb["status"] = "unknown"
    if any(cb.get("operation_id") == operation_id for cb in current["admitted_callbacks"].values()):
        receipt_id = "receipt_" + operation_id
        if receipt_id not in current["child_receipt_refs"]:
            current["child_receipt_refs"].append(receipt_id)
    current["fatal_fence"] = fence
    current["phase"] = "fenced_unknown"
    return validate_js_state(current)  # type: ignore[return-value]


def plan_continuation_lost_js_state(
    js_state: dict[str, Any],
    *,
    reason: str = "ryn_js_continuation_lost",
    message: str = "",
) -> dict[str, Any]:
    """Return a detached copy of `js_state` transitioned to `continuation_lost`."""
    current = validate_js_state(js_state)
    if current is None:
        raise JsStateValidationError("agent_invalid_js_state", "Missing js_execution state")
    validate_phase_transition(current["phase"], "continuation_lost", fatal_fence=current["fatal_fence"])
    candidate_terminal = {
        "status": "lost",
        "value": None,
        "value_type": "undefined",
        "stdout": "",
        "images": [],
        "error": {
            "code": reason,
            "message": str(message or reason)[:MAX_ERROR_MESSAGE_CHARS],
        },
        "child_receipt_refs": list(current["child_receipt_refs"]),
    }
    validated_candidate = _validate_terminal_result(
        candidate_terminal,
        phase="continuation_lost",
        child_receipt_refs=current["child_receipt_refs"],
    )
    if current["terminal_result"] is not None or current["phase"] in TERMINAL_JS_PHASES:
        if current["phase"] != "continuation_lost" or current["terminal_result"] != validated_candidate:
            raise JsStateValidationError("agent_js_terminal_immutable", "Terminal JS result is immutable once recorded")
        return validate_js_state(current)  # type: ignore[return-value]
    for item in current["delivery_outbox"]:
        if item["delivery_state"] == "staged":
            item["delivery_state"] = validate_delivery_transition(item["delivery_state"], "lost")
    current["phase"] = "continuation_lost"
    current["terminal_result"] = validated_candidate
    return validate_js_state(current)  # type: ignore[return-value]


def plan_cancellation_js_state(
    js_state: dict[str, Any],
    *,
    reason: str = "ryn_js_cancelled",
) -> dict[str, Any]:
    """Return a detached copy of `js_state` transitioned to `cancelled` (retaining any fatal_fence)."""
    current = validate_js_state(js_state)
    if current is None:
        raise JsStateValidationError("agent_invalid_js_state", "Missing js_execution state")
    validate_phase_transition(current["phase"], "cancelled", fatal_fence=current["fatal_fence"])
    candidate_terminal = {
        "status": "cancelled",
        "value": None,
        "value_type": "undefined",
        "stdout": "",
        "images": [],
        "error": {
            "code": reason,
            "message": str(reason)[:MAX_ERROR_MESSAGE_CHARS],
        },
        "child_receipt_refs": list(current["child_receipt_refs"]),
    }
    validated_candidate = _validate_terminal_result(
        candidate_terminal,
        phase="cancelled",
        child_receipt_refs=current["child_receipt_refs"],
    )
    if current["terminal_result"] is not None or current["phase"] in TERMINAL_JS_PHASES:
        if current["phase"] != "cancelled" or current["terminal_result"] != validated_candidate:
            raise JsStateValidationError("agent_js_terminal_immutable", "Terminal JS result is immutable once recorded")
        return validate_js_state(current)  # type: ignore[return-value]
    for item in current["delivery_outbox"]:
        if item["delivery_state"] == "staged":
            item["delivery_state"] = validate_delivery_transition(item["delivery_state"], "lost")
    for cb in current["admitted_callbacks"].values():
        if cb["status"] in {"admitted", "awaiting_approval"}:
            cb["status"] = validate_callback_status_transition(cb["status"], "rejected")
    current["phase"] = "cancelled"
    current["terminal_result"] = validated_candidate
    return validate_js_state(current)  # type: ignore[return-value]


def plan_terminal_js_state(
    js_state: dict[str, Any],
    *,
    status: str,
    value: Any = None,
    value_type: str = "undefined",
    stdout: str = "",
    images: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
    active_cpu_ms: int | None = None,
) -> dict[str, Any]:
    """Return a detached copy of `js_state` transitioned to terminal completion/cancellation/loss.

    Once `terminal_result` is recorded, it is strictly immutable: only an exact
    idempotent duplicate update is permitted; any change to status, value,
    value_type, stdout, images, error, child_receipt_refs, or counters fails closed.
    """
    current = validate_js_state(js_state)
    if current is None:
        raise JsStateValidationError("agent_invalid_js_state", "Missing js_execution state")
    if current["fatal_fence"] is not None and status == "completed":
        raise JsStateValidationError("agent_js_fenced_unknown", "Cannot mark completed while fatal_fence is active")
    target_phase = {
        "completed": "completed",
        "failed": "completed",
        "cancelled": "cancelled",
        "lost": "continuation_lost",
    }.get(status)
    if target_phase is None:
        raise JsStateValidationError("agent_invalid_js_state", f"Unsupported terminal snapshot status: {status!r}")
    validate_phase_transition(current["phase"], target_phase, fatal_fence=current["fatal_fence"])

    candidate_terminal = {
        "status": status,
        "value": copy.deepcopy(value),
        "value_type": value_type,
        "stdout": stdout,
        "images": copy.deepcopy(images or []),
        "error": copy.deepcopy(error),
        "child_receipt_refs": list(current["child_receipt_refs"]),
    }
    validated_candidate = _validate_terminal_result(
        candidate_terminal,
        phase=target_phase,
        child_receipt_refs=current["child_receipt_refs"],
    )

    if active_cpu_ms is not None:
        candidate_cpu_ms = _require_non_negative_int(
            active_cpu_ms,
            "counters.active_cpu_ms",
            minimum=current["counters"]["active_cpu_ms"],
            maximum=MAX_ACTIVE_CPU_MS,
        )
    else:
        candidate_cpu_ms = current["counters"]["active_cpu_ms"]
    stdout_bytes = len(stdout.encode("utf-8")) if isinstance(stdout, str) else 0
    candidate_output_bytes = max(current["counters"]["output_bytes"], stdout_bytes)

    if current["terminal_result"] is not None or current["phase"] in TERMINAL_JS_PHASES:
        if (
            current["phase"] != target_phase
            or current["terminal_result"] != validated_candidate
            or current["counters"]["active_cpu_ms"] != candidate_cpu_ms
            or current["counters"]["output_bytes"] != candidate_output_bytes
        ):
            raise JsStateValidationError("agent_js_terminal_immutable", "Terminal JS result is immutable once recorded")
        return validate_js_state(current)  # type: ignore[return-value]

    current["counters"]["active_cpu_ms"] = candidate_cpu_ms
    current["counters"]["output_bytes"] = candidate_output_bytes
    current["phase"] = target_phase
    current["terminal_result"] = validated_candidate
    return validate_js_state(current)  # type: ignore[return-value]


def project_js_control(row: dict[str, Any]) -> dict[str, Any] | None:
    """Project a tiny control-plane summary without cloning source code, outbox, or results."""
    state = validate_task_js_state(row)
    if state is None:
        return None
    return {
        "version": state["version"],
        "active": state["phase"] in ACTIVE_JS_PHASES,
        "phase": state["phase"],
        "parent_call_id": state["parent_call"]["call_id"],
        "parent_operation_id": state["parent_call"]["operation_id"],
        "execution_id": state["execution_id"],
        "session_id": state["session_id"],
        "generation": state["generation"],
        "authority_sha256": _sha256_hex(state["authority"]),
        "fatal_fence": copy.deepcopy(state["fatal_fence"]),
        "unsettled_outbox_count": sum(
            1 for item in state["delivery_outbox"] if item["delivery_state"] == "staged"
        ),
    }


def project_public_js_summary(row: dict[str, Any]) -> dict[str, Any] | None:
    """Project a bounded owner-safe summary of `js_execution` with no code, outbox, or raw errors."""
    state = validate_task_js_state(row)
    if state is None:
        return None
    return {
        "active": state["phase"] in ACTIVE_JS_PHASES,
        "phase": state["phase"],
        "parent_operation_id": state["parent_call"]["operation_id"],
        "host_calls": state["counters"]["host_calls"],
        "child_receipt_count": len(state["child_receipt_refs"]),
        "has_fatal_fence": state["fatal_fence"] is not None,
    }
