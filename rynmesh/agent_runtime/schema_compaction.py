"""Best-effort model-facing schemas; execution always validates the original."""
from __future__ import annotations

import copy

from .store import encoded


def _transform(node, stage, depth=0):
    if not isinstance(node, dict):
        return node
    if stage == 2:
        if any(isinstance(node.get(key), str) and node[key].startswith(("#/$defs/", "#/definitions/"))
               for key in ("$ref", "$dynamicRef")):
            if depth:
                return {}
            node.pop("$ref", None)
            node.pop("$dynamicRef", None)
        node.pop("$defs", None)
        node.pop("definitions", None)
    if stage == 3 and depth >= 3 and any(key in node for key in
            ("properties", "items", "prefixItems", "patternProperties", "anyOf", "oneOf", "allOf")):
        return {}
    if stage == 1:
        node.pop("description", None)
    if stage == 4:
        for key in ("anyOf", "oneOf", "allOf"):
            node.pop(key, None)
    # Visit schema positions only: defaults/enums and property names are data.
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        if isinstance(node.get(key), dict):
            node[key] = {name: _transform(value, stage, depth + 1) for name, value in node[key].items()}
    for key in ("items", "additionalProperties", "not", "if", "then", "else", "contains"):
        if isinstance(node.get(key), dict):
            node[key] = _transform(node[key], stage, depth + 1)
    for key in ("prefixItems", "anyOf", "oneOf", "allOf"):
        if isinstance(node.get(key), list):
            node[key] = [_transform(value, stage, depth + 1) for value in node[key]]
    return node


def compact_tool_schema(schema, max_bytes=5000):
    """Stop after the first sufficient stage; never delete top-level arguments."""
    current = copy.deepcopy(schema)
    if len(encoded(current)) <= max_bytes:
        return current
    for stage in range(1, 5):
        current = _transform(current, stage)
        if len(encoded(current)) <= max_bytes:
            return current
    return current
