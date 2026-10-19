"""One bounded generation policy, independent of visible answer byte limits."""
from __future__ import annotations

import hashlib

from .store import UNKNOWN_WINDOW_CONTEXT_CHARS, AgentError, encoded

VERSION = "configured-output-v2"
TRANSPORT_MAX_TOKENS = 131072
MAX_RAW_RESPONSE_BYTES = 256 * 1024
MAX_TOOL_CALL_ARGUMENT_BYTES = 96 * 1024
MAX_TOOL_CALLS = 8
UNKNOWN_CONTEXT_WINDOW_TOKENS = 32768
FRAMING_RESERVE_TOKENS = 512
ESTIMATED_BYTES_PER_TOKEN = 3
ESTIMATE_BASIS = "serialized_utf8_bytes_div_3_ceil_plus_512_framing_heuristic"


def context_metadata(model):
    """A declared window or a labelled assumption, never an inferred capability."""
    window = model.get("context_window")
    known = type(window) is int and window > 0 and model.get("context_window_known") is not False
    if not known:
        return {"context_window_tokens": UNKNOWN_CONTEXT_WINDOW_TOKENS, "context_window_known": False,
                "context_window_basis": "unknown_fallback"}
    basis = model.get("context_window_basis")
    return {"context_window_tokens": window, "context_window_known": True,
            "context_window_basis": basis if isinstance(basis, str) and basis else "model_declared"}


def configured_output(model):
    configured = model.get("max_output_tokens", 4096)
    if type(configured) is not int or configured < 1:
        raise AgentError("agent_token_limit")
    return configured


def estimate_input(messages, specs):
    """Count the complete neutral request projection; this is not tokenization.

    JSON includes roles, tool schemas, history and escaping. Provider-specific
    framing is covered by a fixed reserve. Different model tokenizers can still
    disagree, so usage reported by the provider remains the actual token count.
    """
    size = len(encoded(messages)) + len(encoded(specs))
    return {"estimated_input_tokens": (size + ESTIMATED_BYTES_PER_TOKEN - 1) // ESTIMATED_BYTES_PER_TOKEN + FRAMING_RESERVE_TOKENS,
            "estimated_input_utf8_bytes": size, "estimate_basis": ESTIMATE_BASIS,
            "token_count_is_estimate": True, "framing_reserve_tokens": FRAMING_RESERVE_TOKENS}


def context_budget(model, manual_limit=None, *, provenance=None):
    """Derive a request-byte allowance while reserving the full output setting."""
    metadata = context_metadata(model)
    reserve = configured_output(model)
    effective = max(0, metadata["context_window_tokens"] - reserve - FRAMING_RESERVE_TOKENS) * ESTIMATED_BYTES_PER_TOKEN
    if not metadata["context_window_known"]:
        effective = min(effective, UNKNOWN_WINDOW_CONTEXT_CHARS)
    if manual_limit is not None:
        if type(manual_limit) is not int or manual_limit < 1:
            raise AgentError("agent_invalid_settings")
        effective = min(effective, manual_limit)
    return {**metadata, "mode": "auto" if manual_limit is None else "manual",
            "provenance": provenance or ("default_auto" if manual_limit is None else "legacy_ambiguous_manual"),
            "manual_max_context_bytes": manual_limit, "effective_max_context_bytes": effective,
            "reserved_output_tokens": reserve, "framing_reserve_tokens": FRAMING_RESERVE_TOKENS,
            "estimate_basis": ESTIMATE_BASIS, "token_count_is_estimate": True,
            "units": {"max_context_chars": "serialized_request_utf8_bytes", "context_window": "tokens"}}


def select(model, messages, specs, *, request_cap=None):
    configured = configured_output(model)
    metadata = context_metadata(model)
    window = metadata["context_window_tokens"]
    if request_cap is not None and (type(request_cap) is not int or request_cap < 1):
        raise AgentError("agent_token_limit")
    estimate = estimate_input(messages, specs)
    limits = {"configured": configured, "transport": TRANSPORT_MAX_TOKENS, "context": window - estimate["estimated_input_tokens"]}
    if request_cap is not None:
        limits["request"] = request_cap
    maximum = min(limits.values())
    if maximum < 1:
        raise AgentError("agent_context_limit")
    public = {**metadata, "max_output_tokens": configured, "estimate_basis": ESTIMATE_BASIS}
    return {"version": VERSION, "configured_cap": configured, "effective_cap": maximum,
            **metadata, **estimate,
            "clamp_reasons": [key for key, value in limits.items() if key != "configured" and value < configured],
            "policy_sha256": hashlib.sha256(encoded(public)).hexdigest()}


def budgeted(policy, allowed):
    value = {**policy, "clamp_reasons": list(policy["clamp_reasons"]), "effective_cap": allowed}
    if allowed < policy["effective_cap"]:
        value["clamp_reasons"].append("lifetime_budget")
    return value


def visible_message(message):
    """Only the typed private continuation and structured tool calls are excluded from visible prose bounds, never unknown data."""
    return {key: value for key, value in message.items() if key not in {"reasoning_content", "provider_state", "tool_calls"}}


def storage_growth(visible_limit):
    # Existing visible result/event reserves plus one raw bounded continuation
    # in the encrypted message history. No provider reasoning enters events.
    return 8 * visible_limit + 128 * 1024 + MAX_RAW_RESPONSE_BYTES
