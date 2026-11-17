"""Bounded model-call normalization; execution stays in the existing runtime.

A usable call id gets a paired, model-visible rejection for an unknown tool or
bad arguments. Identity/budget failures reject the whole batch before any
metadata lookup. Nothing here authorizes, executes or records a business tool.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from ..llm_package.chat import validate_tool_arguments, validate_tool_schema
from .output_policy import MAX_RAW_RESPONSE_BYTES, MAX_TOOL_CALL_ARGUMENT_BYTES
from .protocol import sanitized
from .store import AgentError, AgentToolRejected

CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
INVALID_TOOL_NAME = "__invalid_tool_name"
MAX_SCHEMA_ERROR_DETAIL_CHARS = 300


class ToolCallProtocolError(AgentError):
    """The batch cannot safely enter assistant/tool history at all."""

    def __init__(self, reason, *, code="agent_invalid_model_response"):
        super().__init__(code)
        self.reason = reason
        self.detail = {
            "invalid_calls": "Tool calls must be a bounded array of call objects.",
            "invalid_call_id": "Each tool call needs a valid, nonempty call id.",
            "duplicate_call_id": "Tool call ids must be unique within this task history.",
            "arguments_too_large": "Tool arguments exceed the per-call byte limit.",
            "response_too_large": "The tool call batch exceeds the response byte limit.",
        }.get(reason, "The tool call batch is malformed.")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _invalid_json_constant(_value):
    raise ValueError("non-finite JSON number")


def _function_spec(spec):
    if not isinstance(spec, dict):
        raise AgentError("agent_tool_definition_changed")
    function = spec.get("function") if spec.get("type") == "function" else spec
    if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
            or not TOOL_NAME.fullmatch(function["name"])
            or not isinstance(function.get("parameters", {}), dict)):
        raise AgentError("agent_tool_definition_changed")
    return function


def _schema_digest(function):
    try:
        raw = _json({"name": function["name"], "parameters": function.get("parameters", {})})
        return hashlib.sha256(raw.encode()).hexdigest()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise AgentError("agent_tool_definition_changed") from None


def assert_schema_unchanged(call, current_spec):
    """Check the request-bound schema immediately before existing authorization.

    This is an extra precondition, never permission to execute. Rejected calls
    must be handled as model feedback before metadata/authorization/ledger work.
    """
    if call.get("error_result") is not None:
        raise AgentToolRejected("agent_tool_not_executable")
    try:
        snapshot = _function_spec(call["schema_snapshot"])
        current = _function_spec(current_spec)
        expected = call["schema_sha256"]
        if (call["name"] != snapshot["name"] or current["name"] != call["name"]
                or _schema_digest(snapshot) != expected or _schema_digest(current) != expected):
            raise AgentError("agent_tool_definition_changed")
    except (KeyError, TypeError, AgentError):
        raise AgentToolRejected("agent_tool_definition_changed") from None


def _json_type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def format_schema_rejection(name: str, exc: Exception) -> tuple[str, dict[str, str]]:
    cause = getattr(exc, "__cause__", None) or exc
    path_parts = [re.sub(r"[^A-Za-z0-9_]", "", str(part))[:40] for part in getattr(cause, "absolute_path", ())]
    field_path = "/".join(p for p in path_parts if p) or "<root>"
    validator = re.sub(r"[^A-Za-z0-9_]", "", str(getattr(cause, "validator", "schema")))[:24] or "schema"
    raw_detail = getattr(exc, "detail", None)
    if getattr(cause, "validator", None) == "additionalProperties":
        if not (isinstance(raw_detail, str) and raw_detail.startswith("unexpected properties are not allowed")):
            raw_detail = "unexpected properties are not allowed"
    elif not isinstance(raw_detail, str):
        raw_detail = getattr(cause, "message", None) if hasattr(cause, "message") else ""
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", sanitized(str(raw_detail or ""))).strip()[:MAX_SCHEMA_ERROR_DETAIL_CHARS]
    detail = sanitized(detail)[:MAX_SCHEMA_ERROR_DETAIL_CHARS]
    if detail:
        message = f"Invalid arguments for '{name}' at field '{field_path}' ({validator}): {detail}"
    else:
        message = f"Invalid arguments for '{name}' at field '{field_path}' ({validator}). Check the tool parameter schema and retry."
    return message, {"field_path": field_path, "validator": validator}


def schema_rejection_error(name: str, exc: Exception) -> AgentToolRejected:
    message, details = format_schema_rejection(name, exc)
    return AgentToolRejected(
        "agent_tool_arguments_invalid",
        correction=message,
        details={"tool": name, **details},
    )


def _reject(call, code, message, *, details=None):
    error_result = {"error": code, "message": sanitized(message),
                    "not_executed": True, "no_mutation": True}
    if isinstance(details, dict) and details:
        error_result["details"] = sanitized(details)
    call["error_result"] = error_result
    # This read classification is only a queue placeholder. It is never passed
    # to authorization or a handler; error_result is checked first by the host.
    call["effect"] = "read"
    return call


def normalize_tool_calls(calls, specs, *, existing_call_ids=(), metadata=None,
                         internal_names=(),
                         max_argument_bytes=MAX_TOOL_CALL_ARGUMENT_BYTES,
                         rewrite_ids_seed=None, index_offset=0):
    """Return request-ordered calls with deep-copied schemas or paired errors.

    Accepts normalized chat calls and Responses function_call items. Original
    argument strings survive unchanged, even when malformed; arguments is an
    empty dict on rejection and must never be executed when error_result exists.
    Schema validation reuses Ryn's shared validator. Permissions, effects,
    approval, operation ids and result accounting remain the host's authority.
    A request seed repairs invalid/reused ids deterministically; without it,
    identity checks remain strict. index_offset is the completed stream item's
    position in the full response, so early and final normalization agree.
    """
    if type(max_argument_bytes) is not int or not 1 <= max_argument_bytes <= MAX_TOOL_CALL_ARGUMENT_BYTES:
        raise ValueError("invalid tool-call budget")
    if not isinstance(calls, list):
        raise ToolCallProtocolError("invalid_calls")
    try:
        if len(_json(calls).encode()) > MAX_RAW_RESPONSE_BYTES:
            raise ToolCallProtocolError("response_too_large", code="agent_model_output_limit")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        if isinstance(exc, ToolCallProtocolError):
            raise
        raise ToolCallProtocolError("invalid_calls") from None

    # Preflight all identities/argument sizes before even consulting metadata.
    seen = set(existing_call_ids)
    entries = []
    for index, raw in enumerate(calls, start=index_offset):
        if not isinstance(raw, dict):
            raise ToolCallProtocolError("invalid_calls")
        response_item = raw.get("type") == "function_call"
        call_id = raw.get("call_id") if response_item else raw.get("id")
        provider_call_id = call_id
        invalid_id = not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id)
        needs_rewrite = invalid_id or call_id in seen
        if needs_rewrite:
            if rewrite_ids_seed is None:
                raise ToolCallProtocolError("invalid_call_id" if invalid_id else "duplicate_call_id")
            call_id = "call_ryn_" + hashlib.sha256(
                f"{rewrite_ids_seed}:{index}:{provider_call_id}".encode()
            ).hexdigest()[:24]
        seen.add(call_id)
        function = raw if response_item else raw.get("function")
        function = function if isinstance(function, dict) else {}
        arguments = function.get("arguments")
        arguments_json = arguments if isinstance(arguments, str) else _json(arguments)
        if len(arguments_json.encode()) > max_argument_bytes:
            raise ToolCallProtocolError("arguments_too_large", code="agent_model_output_limit")
        entries.append((raw, function, call_id, arguments, arguments_json, needs_rewrite, provider_call_id))

    catalog = {}
    for spec in specs:
        function = _function_spec(spec)
        if function["name"] in catalog:
            raise AgentError("agent_tool_definition_changed")
        catalog[function["name"]] = copy.deepcopy(function)
    internal_names = frozenset(internal_names)
    normalized = []
    for raw, function, call_id, original_arguments, arguments_json, rewritten, provider_call_id in entries:
        raw_name = function.get("name")
        valid_name = isinstance(raw_name, str) and bool(TOOL_NAME.fullmatch(raw_name))
        namespace = raw.get("namespace")
        bad_type = raw.get("type", "function") not in ("function", "function_call")
        if bad_type:
            name = raw_name if valid_name else INVALID_TOOL_NAME
            snapshot = None
        elif namespace not in (None, ""):
            name = raw_name if valid_name else INVALID_TOOL_NAME
            snapshot = None
            if isinstance(namespace, str) and valid_name:
                prefixed = f"{namespace}__{raw_name}"
                if TOOL_NAME.fullmatch(prefixed) and prefixed in catalog:
                    name = prefixed
                    snapshot = catalog[name]
                elif (namespace == "functions" or raw_name.startswith(f"{namespace}__")) and raw_name in catalog:
                    name = raw_name
                    snapshot = catalog[name]
        else:
            name = raw_name if valid_name else INVALID_TOOL_NAME
            snapshot = catalog.get(name) if valid_name else None
        call = {"call_id": call_id, "name": name, "arguments": {},
                "arguments_json": arguments_json, "raw_arguments_json": arguments_json,
                "arguments_sha256": hashlib.sha256(arguments_json.encode()).hexdigest(),
                "schema_snapshot": copy.deepcopy(snapshot),
                "schema_sha256": _schema_digest(snapshot) if snapshot is not None else None,
                "effect": None, "error_result": None}
        if rewritten:
            call["provider_call_id"] = copy.deepcopy(provider_call_id)
        if raw.get("wire_kind") == "custom":
            call["wire_kind"] = "custom"
        if bad_type:
            normalized.append(_reject(call, "agent_unknown_tool", "This tool payload is not in the request's function catalog."))
            continue
        if snapshot is None:
            if name == "ryn_proposal_overflow":
                normalized.append(_reject(call, "agent_tool_proposal_overflow",
                    "Too many tool proposals in one reply; this one was not executed. Send it again in the next step."))
            else:
                normalized.append(_reject(call, "agent_unknown_tool", "Choose a tool from the tools offered in this request."))
            continue
        try:
            if isinstance(original_arguments, str):
                arguments = (
                    {}
                    if not original_arguments.strip()
                    else json.loads(original_arguments, parse_constant=_invalid_json_constant)
                )
            else:
                arguments = copy.deepcopy(original_arguments)
        except json.JSONDecodeError as exc:
            normalized.append(_reject(
                call,
                "agent_tool_arguments_invalid",
                f"failed to parse function arguments: {exc.msg} at line {exc.lineno} column {exc.colno}",
            ))
            continue
        except (ValueError, TypeError, RecursionError) as exc:
            normalized.append(_reject(
                call,
                "agent_tool_arguments_invalid",
                f"failed to parse function arguments: {str(exc)[:MAX_SCHEMA_ERROR_DETAIL_CHARS]}",
            ))
            continue
        if not isinstance(arguments, dict):
            normalized.append(_reject(
                call,
                "agent_tool_arguments_invalid",
                f"arguments must be a JSON object, got {_json_type_name(arguments)}",
            ))
            continue
        try:
            _json(arguments)  # Reject non-finite/unserializable values consistently.
        except (ValueError, TypeError, RecursionError) as exc:
            normalized.append(_reject(
                call,
                "agent_tool_arguments_invalid",
                f"failed to parse function arguments: {str(exc)[:MAX_SCHEMA_ERROR_DETAIL_CHARS]}",
            ))
            continue
        try:
            validate_tool_schema(snapshot.get("parameters", {}))
        except ValueError:
            raise AgentError("agent_tool_definition_changed") from None
        if name not in internal_names:
            try:
                validate_tool_arguments(snapshot.get("parameters", {}), arguments)
            except ValueError as exc:
                message, details = format_schema_rejection(name, exc)
                normalized.append(_reject(call, "agent_tool_arguments_invalid", message, details=details))
                continue
        call["arguments"] = arguments
        if metadata is not None:
            meta = metadata(name)
            if not isinstance(meta, dict) or meta.get("effect") not in {"read", "write"}:
                raise AgentError("agent_tool_definition_changed")
            call["effect"] = meta["effect"]
            if isinstance(meta.get("extension_binding"), dict):
                call["extension_binding"] = copy.deepcopy(meta["extension_binding"])
            if meta.get("computer_access"):
                call["computer_access"] = True
        normalized.append(call)
    return normalized
