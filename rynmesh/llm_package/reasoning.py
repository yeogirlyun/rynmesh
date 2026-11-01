"""Bounded reasoning metadata shared by CLI discovery and request validation."""
from __future__ import annotations

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def validate_effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError("reasoning_effort must be a supported effort name")
    return value


def metadata(levels, default=None):
    """Export only effort names, never arbitrary upstream metadata or text."""
    if not isinstance(levels, list) or len(levels) > 16:
        return {}
    supported = []
    for level in levels:
        effort = level.get("reasoningEffort", level.get("effort")) if isinstance(level, dict) else level
        if isinstance(effort, str) and effort in EFFORTS and effort not in supported:
            supported.append(effort)
    if not supported:
        return {}
    result = {"supported_reasoning_efforts": supported}
    if isinstance(default, str) and default in supported:
        result["default_reasoning_effort"] = default
    return result
