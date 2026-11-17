"""Strict owner selections and capability-aware dispatch effort resolution."""
from __future__ import annotations

from ..llm_package.reasoning import metadata, validate_effort
from .store import AgentError


def validate(model, value):
    try:
        effort = validate_effort(value)
    except ValueError:
        raise AgentError("agent_invalid_reasoning_effort") from None
    capabilities = metadata(model.get("supported_reasoning_efforts"), model.get("default_reasoning_effort")) if model else {}
    if effort not in capabilities.get("supported_reasoning_efforts", []):
        raise AgentError("agent_reasoning_effort_unsupported")
    return effort


def resolve(model, value):
    try:
        effort = validate_effort(value)
    except ValueError:
        raise AgentError("agent_invalid_reasoning_effort") from None
    levels = metadata(model.get("supported_reasoning_efforts"), model.get("default_reasoning_effort")) if model else {}
    supported = levels.get("supported_reasoning_efforts", [])
    if not supported:
        raise AgentError("agent_reasoning_effort_unsupported")
    order = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    return next((level for level in reversed(order[:order.index(effort) + 1]) if level in supported), supported[-1])
