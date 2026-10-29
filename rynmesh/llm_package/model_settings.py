"""Validated per-model policy; configuration is separate from probe evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

FIELDS = {
    "context_window",
    "max_output_tokens",
    "request_defaults",
    "max_concurrent",
    "requests_per_day",
    "tokens_per_day",
    "shared_tokens_per_day",
    "share_scope",
}
DESTINATION_FIELDS = {"context_window", "max_output_tokens", "request_defaults"}
FALLBACK_CONTEXT_WINDOW = 32768
KNOWN_VENDOR_CONTEXT_WINDOWS = (
    ("deepseek-", 1048576),
    ("glm-5.3", 1048576),
    ("glm-5", 202752),
    ("glm-4", 128000),
    ("gpt-6.1", 872000),
    ("gpt-6", 872000),
    ("gpt-5.6", 872000),
    ("gpt-5", 272000),
    ("gpt-4.1", 1047576),
    ("gpt-4o", 128000),
    ("gpt-oss-", 131072),
    ("o1-", 200000),
    ("o3-", 200000),
    ("o4-", 200000),
    ("gemini-", 1048576),
    ("claude-opus-5", 1000000),
    ("claude-sonnet-5", 1000000),
    ("claude-", 200000),
    ("qwen-", 128000),
    ("qwen2.5", 128000),
    ("qwen3", 128000),
    ("kimi-", 128000),
    ("moonshot-", 128000),
)
KNOWN_VENDOR_MAX_OUTPUT_TOKENS = (
    ("deepseek-", 393216),
    ("glm-5", 131072),
    ("glm-4", 128000),
    ("gpt-6", 128000),
    ("gpt-5", 128000),
    ("gpt-4.1", 32768),
    ("gpt-4o", 16384),
    ("gpt-oss-", 131072),
    ("o1-", 100000),
    ("o3-", 100000),
    ("o4-", 100000),
    ("gemini-", 65536),
    ("claude-opus-5", 128000),
    ("claude-sonnet-5", 128000),
    ("claude-", 64000),
    ("qwen3", 65536),
    ("qwen-", 32768),
    ("kimi-", 65536),
    ("moonshot-", 65536),
)
KNOWN_VENDOR_MAX_CONCURRENT = (
    ("deepseek-flash", 2500),
    ("deepseek-v4-pro", 500),
    ("deepseek-", 500),
    ("glm-5.3-flash", 200),
    ("glm-", 50),
)


def _known_vendor_value(model, table) -> int | None:
    if not isinstance(model, str) or not model:
        return None
    normalized = model.strip().lower().rsplit("/", 1)[-1]
    for prefix, value in table:
        if normalized.startswith(prefix):
            return value
    return None


def _known_vendor_context_window(model) -> int | None:
    return _known_vendor_value(model, KNOWN_VENDOR_CONTEXT_WINDOWS)


def _known_vendor_max_output_tokens(model) -> int | None:
    return _known_vendor_value(model, KNOWN_VENDOR_MAX_OUTPUT_TOKENS)


def _known_vendor_max_concurrent(model) -> int | None:
    return _known_vendor_value(model, KNOWN_VENDOR_MAX_CONCURRENT)


def context_metadata(record, model=None, *, overrides=True):
    """Separate owner limits and authenticated discovery from an unknown fallback."""
    model = model if model is not None else next(iter(record.get("models", [])), "")
    manual = record.get("model_settings", {}).get(model, {}).get("overrides", {}) if overrides else {}
    if "context_window" in manual:
        return {"context_window": manual["context_window"], "context_window_basis": "model_override", "context_window_known": True}
    if record.get("context_window_mode") != "auto" and type(record.get("context_window")) is int:
        return {"context_window": record["context_window"],
                "context_window_basis": "source_configuration" if record.get("context_window_mode") else "legacy_source_configuration",
                "context_window_known": True}
    metadata = record.get("model_metadata", {}).get(model, {})
    window = metadata.get("context_window")
    if type(window) is int and 256 <= window <= 2000000:
        return {"context_window": window, "context_window_basis": "provider_model_metadata", "context_window_known": True}
    inferred = _known_vendor_context_window(model)
    if inferred is not None:
        return {"context_window": inferred, "context_window_basis": "vendor_family_default", "context_window_known": True}
    return {"context_window": FALLBACK_CONTEXT_WINDOW, "context_window_basis": "unknown_fallback", "context_window_known": False}


def defaults(record, model=None):
    model = model if model is not None else next(iter(record.get("models", [])), "")
    window = context_metadata(record, model, overrides=False)["context_window"]
    metadata = record.get("model_metadata", {}).get(model, {})
    meta_output = metadata.get("max_output_tokens") if isinstance(metadata, dict) else None
    vendor_output = _known_vendor_max_output_tokens(model)
    source_output = record.get("max_output_tokens")
    if type(source_output) is int and source_output != 4096:
        max_output = source_output
    elif type(meta_output) is int and 1 <= meta_output <= min(393216, window):
        max_output = meta_output
    elif vendor_output is not None and vendor_output <= window:
        max_output = vendor_output
    else:
        max_output = source_output if type(source_output) is int else 4096
    return {"context_window": window,
            "max_output_tokens": max_output,
            "request_defaults": copy.deepcopy(record.get("request_defaults", {})),
            "max_concurrent": _known_vendor_max_concurrent(model) or 1,
            "requests_per_day": None,
            "tokens_per_day": None,
            "shared_tokens_per_day": None,
            "share_scope": "personal_space"}


def validate(values):
    if not isinstance(values, dict) or set(values) - FIELDS:
        raise ValueError("model_configuration_invalid")
    for key, low, high in (("context_window", 256, 2000000), ("max_output_tokens", 1, 393216), ("max_concurrent", 1, 2500)):
        if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
            raise ValueError("model_" + key + "_invalid")
    quota = values.get("requests_per_day")
    if "share_scope" in values and values["share_scope"] not in ("personal_space", "friends"):
        raise ValueError("model_share_scope_invalid")
    if quota is not None and (type(quota) is not int or not 1 <= quota <= 1000000):
        raise ValueError("model_requests_per_day_invalid")
    for tok_key in ("tokens_per_day", "shared_tokens_per_day"):
        tok_limit = values.get(tok_key)
        if tok_limit is not None and (type(tok_limit) is not int or not 1 <= tok_limit <= 10000000000):
            raise ValueError("model_" + tok_key + "_invalid")
    request = values.get("request_defaults", {})
    if not isinstance(request, dict) or set(request) - {"thinking", "reasoning_effort", "temperature", "top_p"}:
        raise ValueError("model_request_defaults_invalid")
    if "temperature" in request and (type(request["temperature"]) not in (int, float) or not 0 <= request["temperature"] <= 2):
        raise ValueError("model_temperature_invalid")
    if "top_p" in request and (type(request["top_p"]) not in (int, float) or not 0 < request["top_p"] <= 1):
        raise ValueError("model_top_p_invalid")
    if "reasoning_effort" in request and (not isinstance(request["reasoning_effort"], str) or request["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}):
        raise ValueError("model_reasoning_effort_invalid")
    if "thinking" in request and request["thinking"] not in ({"type": "enabled"}, {"type": "disabled"}):
        raise ValueError("model_thinking_invalid")
    if values.get("max_output_tokens", 1) > values.get("context_window", 2000000):
        raise ValueError("model_output_exceeds_context")
    return copy.deepcopy(values)


def effective(record, model):
    row = record.get("model_settings", {}).get(model, {})
    overrides = row.get("overrides", {})
    return {**defaults(record, model), **copy.deepcopy(overrides)}


def reconcile(record, old):
    """Server-owned revisions; adding a sibling does not invalidate this model."""
    settings = {}
    source_fields = ("kind", "provider", "base_url", "api_key", "enabled")
    common_same = (all(record.get(k) == old.get(k) for k in source_fields)
                   and record.get("protocol", "openai_chat") == old.get("protocol", "openai_chat"))
    for model in record["models"]:
        prior = old.get("model_settings", {}).get(model, {})
        proposed = record.get("model_settings", {}).get(model, prior)
        overrides = validate(proposed.get("overrides", {}))
        values = {**defaults(record, model), **overrides}
        # Existing source defaults may contain vendor-specific historical values.
        # Migration must not make the node unloadable; new per-model overrides
        # are strictly validated above and by the owner configuration endpoint.
        validate({k: v for k, v in values.items() if k != "request_defaults"})
        prior_values = effective(old, model)
        exists = model in old.get("models", [])
        inference_same = exists and common_same and all(values[k] == prior_values[k] for k in DESTINATION_FIELDS)
        rules_same = inference_same and values == prior_values and (model in record.get("shared_models", [])) == (model in old.get("shared_models", []))
        prepared = bool(proposed.get("destination_revision") and proposed.get("destination_revision") != prior.get("destination_revision"))
        settings[model] = {"overrides": overrides,
            "rule_revision": prior.get("rule_revision", uuid.uuid4().hex) if rules_same else uuid.uuid4().hex,
            "destination_revision": prior.get("destination_revision", uuid.uuid4().hex) if inference_same else proposed["destination_revision"] if prepared else uuid.uuid4().hex,
            "evidence": copy.deepcopy(proposed.get("evidence", prior.get("evidence", {}))) if inference_same or prepared else {}}
        if exists and not inference_same and not prepared:
            record.setdefault("tool_support", {}).pop(model, None)
    record["model_settings"] = settings


def public(record, model):
    row = record.get("model_settings", {}).get(model, {})
    return {**copy.deepcopy(row), "effective": effective(record, model), **context_metadata(record, model),
            "inherited_fields": sorted(FIELDS - set(row.get("overrides", {}))),
            "evidence": {key: copy.deepcopy(row.get("evidence", {}).get(key, {"status": "unknown", "checked_at": None}))
                         for key in ("connection", "model_exists", "tools")}}


def fingerprint(record, model):
    # Internal only: never expose hashes of secret-bearing source snapshots.
    value = {key: record.get(key) for key in ("kind", "provider", "base_url", "api_key", "enabled")}
    value["protocol"] = record.get("protocol", "openai_chat")
    value.update(model=model, configuration=effective(record, model),
                 rule_revision=record.get("model_settings", {}).get(model, {}).get("rule_revision"))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
