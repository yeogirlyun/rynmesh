"""Lossless exact-value reuse in a single checker request, never evidence pruning."""
from __future__ import annotations

import copy
from collections import Counter

from .store import encoded

GUIDANCE = (
    "shared_values stores exact JSON values: {$same: key} reuses that value; {$literal: value} is original data. "
    "Expand references when comparing facts. Reused values are not independent observations; "
    "each receipt retains its identity, scope, status and time."
)
ENCODING = "exact_shared_values_v1"
IDENTITIES = {"id", "receipt_id", "criterion_id", "operation_id", "source_ref", "evidence_refs"}


def _wire_size(value):
    # The payload is JSON inside the provider message's content string.
    return len(encoded(encoded(value).decode()))


def project(payload):
    """Keep all records/IDs and owner text; replace only exact repeated values.

    Only evidence and receipt values are factored. Headers and owner requirements
    stay inline. Escaped literal marker objects cannot become generated aliases.
    """
    if "shared_values" in payload or "shared_value_encoding" in payload:
        raise ValueError("reserved_checker_projection_field")
    counts = Counter()

    def count(value, depth):
        if isinstance(value, dict) and set(value) in ({"$same"}, {"$literal"}):
            return
        if depth >= 3 and isinstance(value, (dict, list, str)) and len(encoded(value)) >= 40:
            counts[encoded(value)] += 1
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in IDENTITIES:
                    count(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                count(item, depth + 1)

    for name in ("evidence", "receipts"):
        count(payload.get(name, []), 1)
    names, shared = {}, {}

    def visit(value, depth, *, reuse=True):
        if isinstance(value, dict) and set(value) in ({"$same"}, {"$literal"}):
            return {"$literal": copy.deepcopy(value)}
        raw = encoded(value) if reuse and depth >= 3 and isinstance(value, (dict, list, str)) else None
        if raw is not None and counts[raw] > 1:
            key = names.get(raw)
            if key is None:
                key = names[raw] = "v" + str(len(names) + 1)
                shared[key] = visit(value, depth, reuse=False)
            return {"$same": key}
        if isinstance(value, dict):
            return {key: (copy.deepcopy(item) if key in IDENTITIES else visit(item, depth + 1))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, depth + 1) for item in value]
        return value
    result = copy.deepcopy(payload)
    for name in ("evidence", "receipts"):
        if name in result:
            result[name] = visit(result[name], 1)
    # Factoring an outer object can leave a nested value physically used once.
    # Inline such entries; this changes storage only, never record membership.
    def uses(value):
        if isinstance(value, dict):
            if set(value) == {"$same"}:
                return Counter([value["$same"]])
            if set(value) == {"$literal"}:
                return Counter()
            return sum((uses(item) for key, item in value.items() if key not in IDENTITIES), Counter())
        if isinstance(value, list):
            return sum((uses(item) for item in value), Counter())
        return Counter()

    def inline(value, key, replacement):
        if isinstance(value, dict):
            if value == {"$same": key}:
                return copy.deepcopy(replacement)
            if set(value) == {"$literal"}:
                return value
            return {name: (copy.deepcopy(item) if name in IDENTITIES else inline(item, key, replacement))
                    for name, item in value.items()}
        if isinstance(value, list):
            return [inline(item, key, replacement) for item in value]
        return value
    while shared:
        usage = sum((uses(result.get(name, [])) for name in ("evidence", "receipts")), Counter()) + uses(shared)
        redundant = next((key for key in shared if usage[key] <= 1), None)
        if redundant is None:
            break
        value = shared.pop(redundant)
        for name in ("evidence", "receipts"):
            if name in result:
                result[name] = inline(result[name], redundant, value)
        shared = inline(shared, redundant, value)
    if shared:
        result.update(shared_value_encoding=ENCODING, shared_values=shared)
        if _wire_size(result) + len(encoded(GUIDANCE)) >= _wire_size(payload):
            return copy.deepcopy(payload)
        return result
    return copy.deepcopy(payload)


def expand(payload):
    """Restore the exact source payload for validation; never interpret raw aliases."""
    source = copy.deepcopy(payload)
    if source.get("shared_value_encoding") != ENCODING:
        return source
    source.pop("shared_value_encoding")
    shared = source.pop("shared_values")

    def restore(value, resolving):
        if isinstance(value, dict):
            if set(value) == {"$literal"}:
                return copy.deepcopy(value["$literal"])
            if set(value) == {"$same"}:
                target = value["$same"]
                if target in resolving or target not in shared:
                    raise ValueError("invalid_checker_value_reference")
                return restore(shared[target], resolving | {target})
            return {key: (copy.deepcopy(item) if key in IDENTITIES else restore(item, resolving))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [restore(item, resolving) for item in value]
        return value
    for name in ("evidence", "receipts"):
        if name in source:
            source[name] = restore(source[name], set())
    return source
