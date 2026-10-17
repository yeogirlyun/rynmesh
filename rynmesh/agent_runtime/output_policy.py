"""One bounded generation policy, independent of visible answer byte limits."""
from __future__ import annotations

import hashlib
import json

from .store import AgentError, encoded

VERSION = "configured-output-v1"
TRANSPORT_MAX_TOKENS = 131072
MAX_RAW_RESPONSE_BYTES = 256 * 1024
MAX_TOOL_CALL_ARGUMENT_BYTES = 96 * 1024
MAX_TOOL_CALLS = 8


def select(model, messages, specs, *, request_cap=None):
    configured = model.get("max_output_tokens", 4096)
    window = model.get("context_window", 32768)
    if type(configured) is not int or configured < 1 or type(window) is not int or window < 1:
        raise AgentError("agent_token_limit")
    if request_cap is not None and (type(request_cap) is not int or request_cap < 1):
        raise AgentError("agent_token_limit")
    # Preserve the bridge's existing estimate. Budget accounting separately
    # reserves the more conservative serialized input bytes plus overhead.
    estimated_input = max(1, len(json.dumps([messages, specs], ensure_ascii=False)) // 3)
    limits = {"configured": configured, "transport": TRANSPORT_MAX_TOKENS, "context": window - estimated_input}
    if request_cap is not None:
        limits["request"] = request_cap
    maximum = min(limits.values())
    if maximum < 1:
        raise AgentError("agent_context_limit")
    public = {"context_window": window, "max_output_tokens": configured}
    return {"version": VERSION, "configured_cap": configured, "effective_cap": maximum,
            "estimated_input_tokens": estimated_input,
            "clamp_reasons": [key for key, value in limits.items() if key != "configured" and value < configured],
            "policy_sha256": hashlib.sha256(encoded(public)).hexdigest()}


def budgeted(policy, allowed):
    value = {**policy, "clamp_reasons": list(policy["clamp_reasons"]), "effective_cap": allowed}
    if allowed < policy["effective_cap"]:
        value["clamp_reasons"].append("lifetime_budget")
    return value


def visible_message(message):
    """Only the typed private continuation and structured tool calls are excluded from visible prose bounds, never unknown data."""
    return {key: value for key, value in message.items() if key not in {"reasoning_content", "tool_calls"}}


def storage_growth(visible_limit):
    # Existing visible result/event reserves plus one raw bounded continuation
    # in the encrypted message history. No provider reasoning enters events.
    return 8 * visible_limit + 128 * 1024 + MAX_RAW_RESPONSE_BYTES
