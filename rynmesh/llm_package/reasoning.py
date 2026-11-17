"""Bounded reasoning metadata shared by CLI discovery and request validation."""
from __future__ import annotations

import re

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def validate_effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError("reasoning_effort must be a supported effort name")
    return value


def metadata(levels, default=None):
    """Export only effort names, never arbitrary upstream metadata or text."""
    if not isinstance(levels, list):
        return {}
    supported, ignored = [], []
    for level in levels[:16]:
        effort = level.get("reasoningEffort", level.get("effort")) if isinstance(level, dict) else level
        if isinstance(effort, str) and effort in EFFORTS and effort not in supported:
            supported.append(effort)
        elif (isinstance(effort, str) and effort not in EFFORTS
              and re.fullmatch(r"[a-z0-9_-]{1,32}", effort) and effort not in ignored):
            ignored.append(effort)
    if not supported and not ignored:
        return {}
    result = {"supported_reasoning_efforts": supported} if supported else {}
    if ignored:
        result["ignored_reasoning_efforts"] = ignored
    if isinstance(default, str) and default in supported:
        result["default_reasoning_effort"] = default
    return result
