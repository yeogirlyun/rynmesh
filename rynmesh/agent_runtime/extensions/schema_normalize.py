"""Normalize, prune, and compact third-party MCP tool JSON Schemas."""

from __future__ import annotations

import copy
import json
import re
from collections import deque
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ...llm_package.chat import validate_tool_schema

VALID_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

_SAFE_STRUCTURAL_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "description",
        "title",
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "items",
        "prefixItems",
        "enum",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$ref",
        "$defs",
        "definitions",
    }
)

_SCHEMA_ANCHOR_KEYS: frozenset[str] = frozenset(
    {"type", "$ref", "$dynamicRef", "anyOf", "oneOf", "allOf", "not", "enum"}
)

_LOCAL_DEFS_RE = re.compile(r"^#/(\$defs|definitions)/([^/]+)(?:/.*)?$")


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _is_valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _infer_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _infer_enum_type(values: list[Any]) -> str | list[str]:
    seen: list[str] = []
    for item in values:
        inferred = _infer_value_type(item)
        if inferred not in seen:
            seen.append(inferred)
    if "number" in seen and "integer" in seen:
        seen = [t for t in seen if t != "integer"]
    if len(seen) == 1:
        return seen[0]
    return seen


def _has_schema_type(node: dict[str, Any], target: str) -> bool:
    raw = node.get("type")
    if isinstance(raw, str):
        return raw == target
    if isinstance(raw, list):
        return target in raw
    return False


def _sanitize_node(
    schema: Any,
    *,
    is_root: bool,
    known_defs: set[str],
    known_definitions: set[str],
    depth: int = 0,
) -> dict[str, Any]:
    if depth > 28:
        return {}
    if schema is True:
        return {}
    if schema is False:
        return {"not": {}}
    if not isinstance(schema, dict):
        return {}

    node: dict[str, Any] = copy.deepcopy(schema)

    # Whole-table delete when $defs / definitions is not a JSON object (note bool is int, not dict).
    for defs_key in ("$defs", "definitions"):
        if defs_key in node:
            raw_table = node[defs_key]
            if not isinstance(raw_table, dict):
                node.pop(defs_key, None)

    if is_root:
        if isinstance(node.get("$defs"), dict):
            known_defs = {str(k) for k in node["$defs"].keys()}
        if isinstance(node.get("definitions"), dict):
            known_definitions = {str(k) for k in node["definitions"].keys()}

    for defs_key in ("$defs", "definitions"):
        if isinstance(node.get(defs_key), dict):
            node[defs_key] = {
                str(k): _sanitize_node(
                    v,
                    is_root=False,
                    known_defs=known_defs,
                    known_definitions=known_definitions,
                    depth=depth + 1,
                )
                for k, v in node[defs_key].items()
            }

    # Strip external or dangling $ref / $dynamicRef.
    for ref_key in ("$ref", "$dynamicRef"):
        if ref_key in node:
            ref_val = node[ref_key]
            if not isinstance(ref_val, str) or not ref_val.startswith("#"):
                node.pop(ref_key, None)
                continue
            matched = _LOCAL_DEFS_RE.match(ref_val)
            if matched is not None:
                bucket, raw_name = matched.group(1), _unescape_pointer_token(matched.group(2))
                pool = known_defs if bucket == "$defs" else known_definitions
                if raw_name not in pool:
                    node.pop(ref_key, None)

    # Convert const -> enum.
    if "const" in node:
        const_val = node.pop("const")
        if not isinstance(node.get("enum"), list) or not node["enum"]:
            node["enum"] = [const_val]

    if "enum" in node:
        if not isinstance(node["enum"], list) or len(node["enum"]) == 0:
            node.pop("enum", None)

    # Recognize only the 7 valid JSON Schema type names.
    if "type" in node:
        raw_type = node["type"]
        if isinstance(raw_type, str):
            if raw_type not in VALID_SCHEMA_TYPES:
                node.pop("type", None)
        elif isinstance(raw_type, list):
            valid_types: list[str] = []
            for item in raw_type:
                if isinstance(item, str) and item in VALID_SCHEMA_TYPES and item not in valid_types:
                    valid_types.append(item)
            if not valid_types:
                node.pop("type", None)
            elif len(valid_types) == 1:
                node["type"] = valid_types[0]
            else:
                node["type"] = valid_types
        else:
            node.pop("type", None)

    # Infer missing type from properties / items / enum clues.
    if "type" not in node:
        if isinstance(node.get("properties"), dict):
            node["type"] = "object"
        elif "items" in node or "prefixItems" in node:
            node["type"] = "array"
        elif isinstance(node.get("enum"), list) and node["enum"]:
            node["type"] = _infer_enum_type(node["enum"])

    # Sanitize subschemas.
    if "properties" in node:
        if isinstance(node["properties"], dict):
            node["properties"] = {
                str(k): _sanitize_node(
                    v,
                    is_root=False,
                    known_defs=known_defs,
                    known_definitions=known_definitions,
                    depth=depth + 1,
                )
                for k, v in node["properties"].items()
            }
        else:
            node.pop("properties", None)

    if "patternProperties" in node:
        if isinstance(node["patternProperties"], dict):
            cleaned_patterns = {
                str(k): _sanitize_node(
                    v,
                    is_root=False,
                    known_defs=known_defs,
                    known_definitions=known_definitions,
                    depth=depth + 1,
                )
                for k, v in node["patternProperties"].items()
                if isinstance(k, str) and _is_valid_regex(k)
            }
            if cleaned_patterns:
                node["patternProperties"] = cleaned_patterns
            else:
                node.pop("patternProperties", None)
        else:
            node.pop("patternProperties", None)

    if "additionalProperties" in node:
        ap = node["additionalProperties"]
        if isinstance(ap, bool):
            node["additionalProperties"] = ap
        elif isinstance(ap, dict):
            node["additionalProperties"] = _sanitize_node(
                ap,
                is_root=False,
                known_defs=known_defs,
                known_definitions=known_definitions,
                depth=depth + 1,
            )
        else:
            node.pop("additionalProperties", None)

    if "items" in node:
        raw_items = node["items"]
        if isinstance(raw_items, list):
            if raw_items and "prefixItems" not in node:
                node["prefixItems"] = [
                    _sanitize_node(
                        elem,
                        is_root=False,
                        known_defs=known_defs,
                        known_definitions=known_definitions,
                        depth=depth + 1,
                    )
                    for elem in raw_items
                ]
            node["items"] = {"type": "string"}
        elif isinstance(raw_items, (dict, bool)):
            node["items"] = _sanitize_node(
                raw_items,
                is_root=False,
                known_defs=known_defs,
                known_definitions=known_definitions,
                depth=depth + 1,
            )
        else:
            node.pop("items", None)

    if "prefixItems" in node:
        if isinstance(node["prefixItems"], list) and node["prefixItems"]:
            node["prefixItems"] = [
                _sanitize_node(
                    elem,
                    is_root=False,
                    known_defs=known_defs,
                    known_definitions=known_definitions,
                    depth=depth + 1,
                )
                for elem in node["prefixItems"]
            ]
        else:
            node.pop("prefixItems", None)

    for comb_key in ("anyOf", "oneOf", "allOf"):
        if comb_key in node:
            raw_comb = node[comb_key]
            if isinstance(raw_comb, list) and raw_comb:
                node[comb_key] = [
                    _sanitize_node(
                        branch,
                        is_root=False,
                        known_defs=known_defs,
                        known_definitions=known_definitions,
                        depth=depth + 1,
                    )
                    for branch in raw_comb
                ]
            else:
                node.pop(comb_key, None)

    if "not" in node:
        node["not"] = _sanitize_node(
            node["not"],
            is_root=False,
            known_defs=known_defs,
            known_definitions=known_definitions,
            depth=depth + 1,
        )

    if "required" in node:
        if isinstance(node["required"], list):
            node["required"] = [
                item for item in dict.fromkeys(node["required"]) if isinstance(item, str)
            ]
        else:
            node.pop("required", None)

    if "description" in node and not isinstance(node["description"], str):
        node.pop("description", None)
    if "title" in node and not isinstance(node["title"], str):
        node.pop("title", None)

    # Fill object.properties and array.items defaults.
    if _has_schema_type(node, "object") and not isinstance(node.get("properties"), dict):
        node["properties"] = {}
    if _has_schema_type(node, "array") and not isinstance(node.get("items"), dict):
        node["items"] = {"type": "string"}

    # Unknown structure -> {}.
    if not (_SCHEMA_ANCHOR_KEYS & node.keys()):
        return {}

    try:
        Draft202012Validator.check_schema(node)
    except (SchemaError, RecursionError):
        node = {k: v for k, v in node.items() if k in _SAFE_STRUCTURAL_KEYS}
        try:
            Draft202012Validator.check_schema(node)
        except (SchemaError, RecursionError):
            return {}

    return node


def sanitize(schema: Any) -> dict[str, Any]:
    """Sanitize a JSON Schema node into a Draft 2020-12 compliant dict."""
    return _sanitize_node(
        schema,
        is_root=True,
        known_defs=set(),
        known_definitions=set(),
        depth=0,
    )


def _collect_local_refs(value: Any, *, skip_root_tables: bool = False) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    stack: list[tuple[Any, bool]] = [(value, skip_root_tables)]
    while stack:
        curr, is_root = stack.pop()
        if isinstance(curr, dict):
            for key, child in curr.items():
                if is_root and key in {"$defs", "definitions"}:
                    continue
                if key in {"$ref", "$dynamicRef"} and isinstance(child, str):
                    matched = _LOCAL_DEFS_RE.match(child)
                    if matched is not None:
                        found.append((matched.group(1), _unescape_pointer_token(matched.group(2))))
                else:
                    stack.append((child, False))
        elif isinstance(curr, list):
            for elem in curr:
                stack.append((elem, False))
    return found


def prune_unreferenced_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove unreferenced entries from root $defs and definitions tables."""
    if not isinstance(schema, dict):
        return {}
    out = copy.deepcopy(schema)
    defs_table = out.get("$defs") if isinstance(out.get("$defs"), dict) else None
    legacy_table = out.get("definitions") if isinstance(out.get("definitions"), dict) else None
    if defs_table is None:
        out.pop("$defs", None)
    if legacy_table is None:
        out.pop("definitions", None)
    if not defs_table and not legacy_table:
        if defs_table == {}:
            out.pop("$defs", None)
        if legacy_table == {}:
            out.pop("definitions", None)
        return out

    used_defs: set[str] = set()
    used_legacy: set[str] = set()
    queue: deque[tuple[str, str]] = deque(_collect_local_refs(out, skip_root_tables=True))

    while queue:
        bucket, def_name = queue.popleft()
        if bucket == "$defs":
            if defs_table is None or def_name not in defs_table or def_name in used_defs:
                continue
            used_defs.add(def_name)
            queue.extend(_collect_local_refs(defs_table[def_name], skip_root_tables=False))
        else:
            if legacy_table is None or def_name not in legacy_table or def_name in used_legacy:
                continue
            used_legacy.add(def_name)
            queue.extend(_collect_local_refs(legacy_table[def_name], skip_root_tables=False))

    if defs_table is not None:
        kept_defs = {k: v for k, v in defs_table.items() if k in used_defs}
        if kept_defs:
            out["$defs"] = kept_defs
        else:
            out.pop("$defs", None)

    if legacy_table is not None:
        kept_legacy = {k: v for k, v in legacy_table.items() if k in used_legacy}
        if kept_legacy:
            out["definitions"] = kept_legacy
        else:
            out.pop("definitions", None)

    return out


def _serialized_utf8_bytes(schema: dict[str, Any]) -> int:
    return len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))


def _fits_budget(schema: dict[str, Any], max_bytes: int) -> bool:
    if _serialized_utf8_bytes(schema) > max_bytes:
        return False
    try:
        validate_tool_schema(schema)
        return True
    except ValueError:
        return False


def _strip_descriptions_in_schema(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    node.pop("description", None)
    for map_key in ("properties", "patternProperties", "$defs", "definitions"):
        sub_map = node.get(map_key)
        if isinstance(sub_map, dict):
            for prop_key, prop_schema in sub_map.items():
                sub_map[prop_key] = _strip_descriptions_in_schema(prop_schema)
    if isinstance(node.get("additionalProperties"), dict):
        node["additionalProperties"] = _strip_descriptions_in_schema(node["additionalProperties"])
    if isinstance(node.get("items"), dict):
        node["items"] = _strip_descriptions_in_schema(node["items"])
    if isinstance(node.get("prefixItems"), list):
        node["prefixItems"] = [_strip_descriptions_in_schema(x) for x in node["prefixItems"]]
    for comb_key in ("anyOf", "oneOf", "allOf"):
        if isinstance(node.get(comb_key), list):
            node[comb_key] = [_strip_descriptions_in_schema(x) for x in node[comb_key]]
    if isinstance(node.get("not"), dict):
        node["not"] = _strip_descriptions_in_schema(node["not"])
    return node


def _inline_local_refs_to_empty(node: Any, *, is_root: bool = False) -> Any:
    if not isinstance(node, dict):
        return node
    if not is_root:
        for ref_key in ("$ref", "$dynamicRef"):
            ref_val = node.get(ref_key)
            if isinstance(ref_val, str) and ref_val.startswith("#"):
                return {}
    node.pop("$defs", None)
    node.pop("definitions", None)
    for map_key in ("properties", "patternProperties"):
        sub_map = node.get(map_key)
        if isinstance(sub_map, dict):
            for prop_key, prop_schema in list(sub_map.items()):
                sub_map[prop_key] = _inline_local_refs_to_empty(prop_schema, is_root=False)
    if isinstance(node.get("additionalProperties"), dict):
        node["additionalProperties"] = _inline_local_refs_to_empty(
            node["additionalProperties"], is_root=False
        )
    if isinstance(node.get("items"), dict):
        node["items"] = _inline_local_refs_to_empty(node["items"], is_root=False)
    if isinstance(node.get("prefixItems"), list):
        node["prefixItems"] = [
            _inline_local_refs_to_empty(x, is_root=False) for x in node["prefixItems"]
        ]
    for comb_key in ("anyOf", "oneOf", "allOf"):
        if isinstance(node.get(comb_key), list):
            node[comb_key] = [
                _inline_local_refs_to_empty(x, is_root=False) for x in node[comb_key]
            ]
    if isinstance(node.get("not"), dict):
        node["not"] = _inline_local_refs_to_empty(node["not"], is_root=False)
    return node


def _is_complex_schema_node(node: dict[str, Any]) -> bool:
    if _has_schema_type(node, "object") or _has_schema_type(node, "array"):
        return True
    complex_keys = {
        "properties",
        "patternProperties",
        "additionalProperties",
        "items",
        "prefixItems",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
    }
    if complex_keys & node.keys():
        return True
    return _serialized_utf8_bytes(node) > 64


def _collapse_deep_complex_nodes(node: Any, *, depth: int) -> Any:
    if not isinstance(node, dict):
        return node
    if depth >= 3 and _is_complex_schema_node(node):
        return {}
    for map_key in ("properties", "patternProperties", "$defs", "definitions"):
        sub_map = node.get(map_key)
        if isinstance(sub_map, dict):
            for prop_key, prop_schema in list(sub_map.items()):
                sub_map[prop_key] = _collapse_deep_complex_nodes(prop_schema, depth=depth + 1)
    if isinstance(node.get("additionalProperties"), dict):
        node["additionalProperties"] = _collapse_deep_complex_nodes(
            node["additionalProperties"], depth=depth + 1
        )
    if isinstance(node.get("items"), dict):
        node["items"] = _collapse_deep_complex_nodes(node["items"], depth=depth + 1)
    if isinstance(node.get("prefixItems"), list):
        node["prefixItems"] = [
            _collapse_deep_complex_nodes(x, depth=depth + 1) for x in node["prefixItems"]
        ]
    for comb_key in ("anyOf", "oneOf", "allOf"):
        if isinstance(node.get(comb_key), list):
            node[comb_key] = [
                _collapse_deep_complex_nodes(x, depth=depth + 1) for x in node[comb_key]
            ]
    if isinstance(node.get("not"), dict):
        node["not"] = _collapse_deep_complex_nodes(node["not"], depth=depth + 1)
    return node


def _strip_combinators(node: Any, *, is_root: bool) -> Any:
    if not isinstance(node, dict):
        return node
    node.pop("anyOf", None)
    node.pop("oneOf", None)
    node.pop("allOf", None)
    for map_key in ("properties", "patternProperties", "$defs", "definitions"):
        sub_map = node.get(map_key)
        if isinstance(sub_map, dict):
            for prop_key, prop_schema in list(sub_map.items()):
                sub_map[prop_key] = _strip_combinators(prop_schema, is_root=False)
    if isinstance(node.get("additionalProperties"), dict):
        node["additionalProperties"] = _strip_combinators(
            node["additionalProperties"], is_root=False
        )
    if isinstance(node.get("items"), dict):
        node["items"] = _strip_combinators(node["items"], is_root=False)
    if isinstance(node.get("prefixItems"), list):
        node["prefixItems"] = [_strip_combinators(x, is_root=False) for x in node["prefixItems"]]
    if isinstance(node.get("not"), dict):
        node["not"] = _strip_combinators(node["not"], is_root=False)
    if is_root:
        if not isinstance(node.get("properties"), dict):
            node["properties"] = {}
        return node
    if not (_SCHEMA_ANCHOR_KEYS & node.keys()):
        return {}
    return node


def compact(schema: Any, max_bytes: int = 5000) -> dict[str, Any] | None:
    """Compact a schema in 4 stages down to max_bytes; return None if top-level type != object."""
    current = prune_unreferenced_defs(sanitize(schema))
    if not isinstance(current, dict) or current.get("type") != "object":
        return None
    if not isinstance(current.get("properties"), dict):
        current["properties"] = {}
    if _fits_budget(current, max_bytes):
        return current

    # Stage 1: delete descriptions on schema nodes.
    current = _strip_descriptions_in_schema(current)
    if _fits_budget(current, max_bytes):
        return current

    # Stage 2: replace local $ref with {} and delete $defs / definitions.
    current = _inline_local_refs_to_empty(current, is_root=True)
    if _fits_budget(current, max_bytes):
        return current

    # Stage 3: collapse complex nodes at depth >= 3 to {}.
    current = _collapse_deep_complex_nodes(current, depth=0)
    if _fits_budget(current, max_bytes):
        return current

    # Stage 4: strip anyOf / oneOf / allOf.
    current = _strip_combinators(current, is_root=True)
    if _fits_budget(current, max_bytes):
        return current

    # Final fallback for extreme flat schemas still exceeding max_bytes after all 4 stages.
    props = current.get("properties")
    if isinstance(props, dict):
        by_size = sorted(
            props.keys(),
            key=lambda k: _serialized_utf8_bytes(props[k]) if isinstance(props[k], dict) else 0,
            reverse=True,
        )
        for key in by_size:
            if props[key] != {}:
                props[key] = {}
                if _fits_budget(current, max_bytes):
                    return current
        required_set = set(current.get("required") or [])
        for key in list(props.keys()):
            if key not in required_set:
                props.pop(key, None)
                if _fits_budget(current, max_bytes):
                    return current

    return current


def normalize_mcp_input_schema(schema: Any, *, max_bytes: int = 5000) -> dict[str, Any] | None:
    """Full pipeline: sanitize -> prune_unreferenced_defs -> compact -> validate_tool_schema."""
    compacted = compact(schema, max_bytes=max_bytes)
    if compacted is None:
        return None
    try:
        validate_tool_schema(compacted)
    except ValueError:
        return None
    return compacted
