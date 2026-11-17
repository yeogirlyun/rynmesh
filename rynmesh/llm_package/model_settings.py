"""Validated per-model policy; configuration is separate from probe evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

from .reasoning import EFFORTS

FIELDS = {
    "supported_reasoning_efforts",
    "context_window",
    "max_output_tokens",
    "request_defaults",
    "max_concurrent",
    "requests_per_day",
    "tokens_per_day",
    "shared_tokens_per_day",
    "share_scope",
    "image_input",
    "prompt_cache",
    "stream_idle_seconds",
    "max_request_seconds",
    "stream_max_retries",
}
IMAGE_INPUT_VALUES = ("auto", "supported", "unsupported")
PROMPT_CACHE_VALUES = ("auto", "off")
# Model-name prefixes whose image input was confirmed with the owner's image test.
# Add an entry only after that test passed on a real account; never from memory.
KNOWN_VENDOR_IMAGE_INPUT: tuple[str, ...] = ()
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


def known_vendor_context_window(model) -> int | None:
    """Return the existing family default; deployments may declare a smaller window."""
    return _known_vendor_value(model, KNOWN_VENDOR_CONTEXT_WINDOWS)


# Existing API/CLI callers still use the private name.
_known_vendor_context_window = known_vendor_context_window


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


def output_metadata(record, model, *, overrides=True):
    manual = record.get("model_settings", {}).get(model, {}).get("overrides", {}) if overrides else {}
    if "max_output_tokens" in manual:
        return {"max_output_tokens": manual["max_output_tokens"], "max_output_tokens_known": True,
                "max_output_basis": "model_override"}
    window = context_metadata(record, model, overrides=overrides)["context_window"]
    metadata = record.get("model_metadata", {}).get(model, {})
    meta_output = metadata.get("max_output_tokens") if isinstance(metadata, dict) else None
    vendor_output = _known_vendor_max_output_tokens(model)
    source_output = record.get("max_output_tokens")
    explicit_output = record.get("max_output_tokens_explicit")
    basis = "unknown_fallback"
    if explicit_output is True and type(source_output) is int:
        max_output = source_output
        basis = "source_configuration"
    elif explicit_output is False:
        if type(meta_output) is int and 1 <= meta_output <= min(393216, window):
            max_output = meta_output
            basis = "provider_model_metadata"
        elif vendor_output is not None and vendor_output <= window:
            max_output = vendor_output
            basis = "vendor_family_default"
        else:
            max_output = source_output if type(source_output) is int else 4096
            if type(source_output) is int and source_output != 4096:
                basis = "legacy_source_configuration"
    else:
        if type(source_output) is int and source_output != 4096:
            max_output = source_output
            basis = "legacy_source_configuration"
        elif type(meta_output) is int and 1 <= meta_output <= min(393216, window):
            max_output = meta_output
            basis = "provider_model_metadata"
        elif vendor_output is not None and vendor_output <= window:
            max_output = vendor_output
            basis = "vendor_family_default"
        else:
            max_output = source_output if type(source_output) is int else 4096
    if basis == "unknown_fallback":
        context = context_metadata(record, model, overrides=overrides)
        max_output = min(16384, context["context_window"] // 4) if context["context_window_known"] else 8192
    return {"max_output_tokens": max_output, "max_output_tokens_known": basis != "unknown_fallback",
            "max_output_basis": basis}


def defaults(record, model=None):
    model = model if model is not None else next(iter(record.get("models", [])), "")
    window = context_metadata(record, model, overrides=False)["context_window"]
    max_output = output_metadata(record, model, overrides=False)["max_output_tokens"]
    source_idle = record.get("stream_idle_seconds")
    source_max_request = record.get("max_request_seconds")
    source_stream_retries = record.get("stream_max_retries")
    stream_idle = source_idle if type(source_idle) is int and 1 <= source_idle <= 86400 else 120
    max_request = source_max_request if type(source_max_request) is int and 1 <= source_max_request <= 86400 else 1800
    stream_max_retries = source_stream_retries if type(source_stream_retries) is int and 0 <= source_stream_retries <= 20 else 5
    protocol = record.get("protocol", "openai_chat")
    default_prompt_cache = "auto" if protocol in {"openai_responses", "anthropic_messages"} else "off"
    source_prompt_cache = record.get("prompt_cache")
    prompt_cache = source_prompt_cache if source_prompt_cache in PROMPT_CACHE_VALUES else default_prompt_cache
    return {"context_window": window,
            "max_output_tokens": max_output,
            "request_defaults": copy.deepcopy(record.get("request_defaults", {})),
            "max_concurrent": _known_vendor_max_concurrent(model) or (4 if record.get("kind") == "api" else 1),
            "requests_per_day": None,
            "tokens_per_day": None,
            "shared_tokens_per_day": None,
            "share_scope": "personal_space",
            "image_input": "auto",
            "prompt_cache": prompt_cache,
            "stream_idle_seconds": stream_idle,
            "max_request_seconds": max_request,
            "stream_max_retries": stream_max_retries}


def validate(values):
    if not isinstance(values, dict) or set(values) - FIELDS:
        raise ValueError("model_configuration_invalid")
    if "supported_reasoning_efforts" in values:
        levels = values["supported_reasoning_efforts"]
        if (not isinstance(levels, list) or len(levels) > 8
                or any(not isinstance(level, str) or level not in EFFORTS for level in levels)
                or len(set(levels)) != len(levels)):
            raise ValueError("model_supported_reasoning_efforts_invalid")
    for key, low, high in (
        ("context_window", 256, 2000000),
        ("max_output_tokens", 1, 393216),
        ("max_concurrent", 1, 2500),
        ("stream_idle_seconds", 1, 86400),
        ("max_request_seconds", 1, 86400),
        ("stream_max_retries", 0, 20),
    ):
        if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
            raise ValueError("model_" + key + "_invalid")
    quota = values.get("requests_per_day")
    if "share_scope" in values and values["share_scope"] not in ("personal_space", "friends"):
        raise ValueError("model_share_scope_invalid")
    if "image_input" in values and values["image_input"] not in IMAGE_INPUT_VALUES:
        raise ValueError("model_image_input_invalid")
    if "prompt_cache" in values and values["prompt_cache"] not in PROMPT_CACHE_VALUES:
        raise ValueError("model_prompt_cache_invalid")
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
    if "reasoning_effort" in request and (not isinstance(request["reasoning_effort"], str) or request["reasoning_effort"] not in EFFORTS):
        raise ValueError("model_reasoning_effort_invalid")
    if "thinking" in request:
        thinking = request["thinking"]
        if thinking not in ({"type": "enabled"}, {"type": "disabled"}):
            if (not isinstance(thinking, dict) or set(thinking) != {"type", "budget_tokens"}
                    or thinking["type"] != "enabled" or type(thinking["budget_tokens"]) is not int
                    or not 1024 <= thinking["budget_tokens"] < values.get("max_output_tokens", 393216)):
                raise ValueError("model_thinking_invalid")
    if values.get("max_output_tokens", 1) > values.get("context_window", 2000000):
        raise ValueError("model_output_exceeds_context")
    return copy.deepcopy(values)


def effective(record, model):
    row = record.get("model_settings", {}).get(model, {})
    overrides = row.get("overrides", {})
    return {**defaults(record, model), **copy.deepcopy(overrides), **output_metadata(record, model)}


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
        values = {key: value for key, value in effective(record, model).items() if key in FIELDS}
        # Existing source defaults may contain vendor-specific historical values.
        # Migration must not make the node unloadable; new per-model overrides
        # are strictly validated above and by the owner configuration endpoint.
        validate({k: v for k, v in values.items() if k != "request_defaults"})
        prior_values = {key: value for key, value in effective(old, model).items() if key in values}
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


def image_input_state(record, model):
    """What this computer knows about the model's image input: supported, unsupported or unknown.

    Same order as the Agent's own decision: owner setting, image test, the
    provider's model listing, then confirmed vendor prefixes. A sharing
    computer publishes this so every user of the model sees the same answer.
    """
    row = record.get("model_settings", {}).get(model, {})
    owner = (row.get("overrides") or {}).get("image_input")
    if owner in {"supported", "unsupported"}:
        return owner
    tested = ((row.get("evidence") or {}).get("image") or {}).get("status")
    if tested in {"supported", "unsupported"}:
        return tested
    modalities = (record.get("model_metadata", {}).get(model) or {}).get("input_modalities")
    if isinstance(modalities, list) and modalities:
        lowered = {str(item).strip().lower() for item in modalities}
        return "supported" if lowered & {"image", "images", "vision", "multimodal"} else "unsupported"
    normalized = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if normalized and any(normalized.startswith(prefix) for prefix in KNOWN_VENDOR_IMAGE_INPUT):
        return "supported"
    return "unknown"


def public(record, model):
    row = record.get("model_settings", {}).get(model, {})
    raw_evidence = row.get("evidence", {})
    evidence = {key: copy.deepcopy(raw_evidence.get(key, {"status": "unknown", "checked_at": None}))
                for key in ("connection", "model_exists", "tools", "image")}
    for extra_key in ("prompt_cache_key", "cache_control"):
        if extra_key in raw_evidence:
            evidence[extra_key] = copy.deepcopy(raw_evidence[extra_key])
    return {**copy.deepcopy(row), "effective": effective(record, model), **context_metadata(record, model),
            "inherited_fields": sorted(FIELDS - set(row.get("overrides", {}))),
            "evidence": evidence}


def fingerprint(record, model):
    # Internal only: never expose hashes of secret-bearing source snapshots.
    value = {key: record.get(key) for key in ("kind", "provider", "base_url", "api_key", "enabled")}
    value["protocol"] = record.get("protocol", "openai_chat")
    value.update(model=model, configuration=effective(record, model),
                 rule_revision=record.get("model_settings", {}).get(model, {}).get("rule_revision"))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
