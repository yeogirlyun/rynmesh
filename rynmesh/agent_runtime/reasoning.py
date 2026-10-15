"""Owner-selected effort is available only with demonstrated CLI capabilities."""
from __future__ import annotations

from ..llm_package.reasoning import metadata, validate_effort
from .store import AgentError


def validate(model, value):
    try:
        effort = validate_effort(value)
    except ValueError:
        raise AgentError("agent_invalid_reasoning_effort") from None
    capabilities = metadata(model.get("supported_reasoning_efforts"), model.get("default_reasoning_effort")) if model else {}
    if (not model or model.get("adapter") != "codex_cli"
            or effort not in capabilities.get("supported_reasoning_efforts", [])):
        raise AgentError("agent_reasoning_effort_unsupported")
    return effort
