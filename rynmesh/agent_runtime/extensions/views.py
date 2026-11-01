"""Bounded, declarative tool views. No executable content or model dispatch."""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date
from typing import Any

from jsonschema.validators import validator_for
from referencing import Registry

from ..store import AgentError

INVALID = "agent_invalid_plugin_view"
_FORBIDDEN = {"__proto__", "prototype", "constructor"}
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_FORMATS = {"text", "number", "money"}
_SAFE_ERRORS = {
    INVALID, "agent_invalid_request", "agent_plugin_not_found", "agent_plugin_disabled",
    "agent_plugin_view_not_found", "agent_plugin_view_not_read_only", "agent_plugin_view_query_failed",
    "agent_plugin_view_unsupported_server",
    "agent_plugin_view_read_only_confirmation_required",
    "agent_plugin_view_result_invalid", "agent_plugin_view_result_limit", "agent_mcp_auth_required",
    "agent_mcp_server_not_found", "agent_mcp_server_disabled", "agent_mcp_tool_disabled",
    "agent_stale_revision", "agent_extensions_revision_conflict", "agent_mcp_timeout",
    "agent_mcp_output_limit", "agent_cancelled",
}


def _fail(code=INVALID):
    raise AgentError(code)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _fail()
    return value


def _text(value, limit=120, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail()
    return value


def _name(value, *, parameter=False, limit=128):
    _text(value, limit)
    if not (_PARAM if parameter else _NAME).fullmatch(value) or any(x.casefold() in _FORBIDDEN for x in value.split(".")):
        _fail()
    return value


def safe_path(value):
    """Property traversal only; no expressions, indices, attributes or prototype keys."""
    _text(value, 256)
    parts = value.split(".")
    if len(parts) > 8 or any(not part or len(part) > 64 or part.casefold() in _FORBIDDEN
                            or not all(c.isalnum() or c in "_-" for c in part) for part in parts):
        _fail()
    return value


def _list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        _fail()
    return value


def normalize_views(value: Any, server_ids: dict[str, str]) -> list[dict[str, Any]]:
    """Resolve package-local server keys; caller supplies only this installation's servers."""
    result, ids = [], set()
    for raw in _list(value, 8):
        _object(raw, {"id", "title", "mcpServer", "tool", "inputs", "columns", "rowsPath", "pagination"},
                {"description", "summaryFields"})
        view_id = _name(raw["id"], limit=64)
        server = _name(raw["mcpServer"])
        if view_id in ids or server not in server_ids:
            _fail()
        ids.add(view_id)
        item = {"id": view_id, "title": _text(raw["title"]),
                "description": _text(raw.get("description", ""), 500, empty=True),
                "mcpServer": _name(server_ids[server]), "tool": _name(raw["tool"]),
                "inputs": [], "columns": [], "rowsPath": safe_path(raw["rowsPath"]), "summaryFields": []}
        names = set()
        for field in _list(raw["inputs"], 16):
            _object(field, {"name", "label", "type"}, {"required", "default"})
            name = _name(field["name"], parameter=True)
            if name in names or field["type"] not in ("text", "date") or type(field.get("required", False)) is not bool:
                _fail()
            names.add(name)
            entry = {"name": name, "label": _text(field["label"]), "type": field["type"],
                     "required": field.get("required", False)}
            if "default" in field:
                if field["type"] != "date" or field["default"] not in ("today", "month_start"):
                    _fail()
                entry["default"] = field["default"]
            item["inputs"].append(entry)
        for key, maximum, path_key in (("columns", 32, "key"), ("summaryFields", 16, "path")):
            paths = set()
            for field in _list(raw.get(key, []), maximum):
                _object(field, {path_key, "label"}, {"format"})
                path = safe_path(field[path_key])
                if path in paths or not isinstance(field.get("format", "text"), str) or field.get("format", "text") not in _FORMATS:
                    _fail()
                paths.add(path)
                item[key].append({path_key: path, "label": _text(field["label"]), "format": field.get("format", "text")})
        if not item["columns"]:
            _fail()
        p = _object(raw["pagination"], {"pageArgument", "sizeArgument", "path", "pageSize"})
        page_arg, size_arg = _name(p["pageArgument"], parameter=True), _name(p["sizeArgument"], parameter=True)
        if page_arg == size_arg or {page_arg, size_arg} & names or type(p["pageSize"]) is not int or p["pageSize"] != 20:
            _fail()
        item["pagination"] = {"pageArgument": page_arg, "sizeArgument": size_arg, "path": safe_path(p["path"]), "pageSize": 20}
        result.append(item)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
        _fail()
    return result


def _arguments(view, payload):
    if not isinstance(payload, dict) or set(payload) != {"arguments", "page", "expected_instance_id", "expected_revision"}:
        _fail("agent_invalid_request")
    raw, page = payload["arguments"], payload["page"]
    if not isinstance(raw, dict) or set(raw) - {x["name"] for x in view["inputs"]} or type(page) is not int or not 1 <= page <= 100000:
        _fail("agent_invalid_request")
    result = {}
    for field in view["inputs"]:
        val = raw.get(field["name"])
        if field["name"] not in raw and "default" in field:
            today = date.today()
            val = (today.replace(day=1) if field["default"] == "month_start" else today).isoformat()
        if val is None and field["name"] not in raw:
            if field["required"]:
                _fail("agent_invalid_request")
            continue
        if not isinstance(val, str) or len(val) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in val):
            _fail("agent_invalid_request")
        if not val.strip():
            if field["required"]:
                _fail("agent_invalid_request")
            continue
        if field["type"] == "date":
            try:
                if not _DATE.fullmatch(val):
                    raise ValueError
                date.fromisoformat(val)
            except ValueError:
                _fail("agent_invalid_request")
        result[field["name"]] = val
    result[view["pagination"]["pageArgument"]] = page
    result[view["pagination"]["sizeArgument"]] = 20
    return result


def _prepare_tool_arguments(view, schema, arguments):
    call_args = dict(arguments)
    if (
        isinstance(schema, dict)
        and schema.get("additionalProperties") is False
        and isinstance(schema.get("properties"), dict)
    ):
        props = schema["properties"]
        for key in (view["pagination"]["pageArgument"], view["pagination"]["sizeArgument"]):
            if key not in props:
                call_args.pop(key, None)
    return call_args




def _validate_arguments(schema, arguments):
    # An MCP-supplied schema must never cause network retrieval during validation.
    def no_resources(uri):
        raise ValueError("external_schema_disabled")
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        validator(schema, registry=Registry(retrieve=no_resources)).validate(arguments)
    except Exception:
        _fail("agent_invalid_request")


def _result(raw):
    if not isinstance(raw, dict) or raw.get("isError") or not isinstance(raw.get("structuredContent"), dict):
        _fail("agent_plugin_view_query_failed")
    result = raw["structuredContent"]
    if result.get("success") is False:
        _fail("agent_plugin_view_query_failed")
    # Validate before serializing: bounded traversal also rejects non-JSON data.
    nodes = 0
    secret_keys = {"access_token", "refresh_token", "id_token", "client_secret", "authorization", "cookie", "secret", "password"}
    def visit(value, depth):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 50000:
            _fail("agent_plugin_view_result_limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 256 or key.casefold() in _FORBIDDEN | secret_keys:
                    _fail("agent_plugin_view_result_invalid")
                visit(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 1000:
                _fail("agent_plugin_view_result_limit")
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 65536:
                _fail("agent_plugin_view_result_limit")
        elif value is not None and type(value) not in (bool, int, float):
            _fail("agent_plugin_view_result_invalid")
        elif type(value) is float and not math.isfinite(value):
            _fail("agent_plugin_view_result_invalid")
    visit(result, 0)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
        _fail("agent_plugin_view_result_limit")
    return copy.deepcopy(result)


def query_view(store, manager, plugin_id: str, view_id: str, payload: Any):
    """Execute only the current declared read-only tool; never persist its result."""
    try:
        initial = store.get_plugin_view_internal(plugin_id, view_id)
        view, server, tool = initial["view"], initial["server"], initial["tool"]
        arguments = _arguments(view, payload)
        if (payload["expected_instance_id"] != initial["plugin_instance_id"]
                or type(payload["expected_revision"]) is not int
                or payload["expected_revision"] != initial["plugin_revision"]):
            _fail("agent_stale_revision")
        call_arguments = _prepare_tool_arguments(view, tool["input_schema"], arguments)
        _validate_arguments(tool["input_schema"], call_arguments)

        def session_generation():
            with manager._lock:
                return manager._manager_generation, manager._server_generations.get(server["id"], 0)

        generation = session_generation()

        def recheck():
            if store.get_plugin_view_internal(plugin_id, view_id) != initial or session_generation() != generation:
                _fail("agent_stale_revision")
            if manager.oauth.public_status(server["instance_id"])["state"] not in ("logged_in", "not_required"):
                _fail("agent_mcp_auth_required")

        def cancelled():
            try:
                recheck()
                return False
            except AgentError:
                return True

        recheck()
        raw = manager.call_tool(server["id"], view["tool"], call_arguments,
            expected_revision=server["revision"], expected_config_generation=server["config_generation"],
            expected_instance_id=server["instance_id"], expected_schema_hash=tool["schema_sha256"],
            before_dispatch=recheck, cancelled=cancelled)
        result = _result(raw)
        recheck()
        return {"view_id": view_id, "result": result}
    except Exception as exc:
        code = str(exc) if isinstance(exc, AgentError) and str(exc) in _SAFE_ERRORS else "agent_plugin_view_query_failed"
        raise AgentError(code) from None
