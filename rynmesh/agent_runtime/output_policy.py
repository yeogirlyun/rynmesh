"""One bounded generation policy, independent of visible answer byte limits."""
from __future__ import annotations

import copy
import hashlib
import math

from ..llm_package.context_safety import ESTIMATED_BYTES_PER_TOKEN
from ..llm_package.image_input_profile import (
    MAX_INTERNAL_RAW_REQUEST_BYTES,
    MAX_INTERNAL_REQUEST_IMAGES,
    MAX_INTERNAL_SINGLE_IMAGE_BYTES,
    MAX_INTERNAL_SINGLE_IMAGE_PIXELS,
    MAX_INTERNAL_TOTAL_IMAGE_BYTES,
    MAX_INTERNAL_TOTAL_IMAGE_PIXELS,
    inspect_canonical_user_image,
    is_candidate_image_part,
)
from .store import UNKNOWN_WINDOW_CONTEXT_CHARS, AgentError, encoded

VERSION = "configured-output-v2"
IMAGE_ANALYSIS_VERSION = "canonical-image-heuristic-v2"
TRANSPORT_MAX_TOKENS = 393216
MAX_RAW_RESPONSE_BYTES = 640 * 1024
MAX_PROVIDER_STATE_BYTES = 2 * 1024 * 1024
MAX_REASONING_CONTENT_BYTES = 256 * 1024
ANSWER_TOTAL_LIMIT = 1024 * 1024
ANSWER_PREVIEW_BYTES = 64 * 1024
MAX_TOOL_CALL_ARGUMENT_BYTES = 256 * 1024
STREAM_READ_CANCEL_TIMEOUT_S = 10.0
UNKNOWN_CONTEXT_WINDOW_TOKENS = 32768
FRAMING_RESERVE_TOKENS = 512
MIN_OUTPUT_RESERVE_TOKENS = 8192
MIN_USEFUL_OUTPUT_TOKENS = 4096
ESTIMATE_BASIS = "serialized_utf8_bytes_div_cjk_density_ceil_plus_512_framing_heuristic"
RYN_ADMISSION_ESTIMATE_BASIS = "serialized_utf8_bytes_as_tokens_ryn_provider_admission"
IMAGE_ESTIMATE_BASIS = "text_utf8_div_cjk_density_ceil_plus_512_and_image_area_div_750_capped_10000_plus_85_heuristic"
IMAGE_TOKEN_FORMULA_BASIS = "heuristic_min_10000_ceil_w_times_h_div_750_plus_85_not_provider_tokenizer"
IMAGE_AREA_DIVISOR = 750
IMAGE_MAX_TOKENS = 10000
IMAGE_FIXED_OVERHEAD_TOKENS = 85
IMAGE_TEXT_PROJECTION_PLACEHOLDER = "ryn-verified-image://inline"

MAX_RAW_REQUEST_UTF8_BYTES = MAX_INTERNAL_RAW_REQUEST_BYTES
MAX_REQUEST_IMAGES = MAX_INTERNAL_REQUEST_IMAGES
MAX_SINGLE_IMAGE_DECODED_BYTES = MAX_INTERNAL_SINGLE_IMAGE_BYTES
MAX_TOTAL_IMAGE_DECODED_BYTES = MAX_INTERNAL_TOTAL_IMAGE_BYTES
MAX_SINGLE_IMAGE_PIXELS = MAX_INTERNAL_SINGLE_IMAGE_PIXELS
MAX_TOTAL_IMAGE_PIXELS = MAX_INTERNAL_TOTAL_IMAGE_PIXELS


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


def admission_bytes_per_token(model):
    """Request bytes one window token may hold at the route's admission check.

    A Ryn peer provider admits an order only when its prompt's UTF-8 bytes plus
    output fit the window, one token per byte. Budgeting peer models at the
    default 3 bytes/token sent requests the provider refused instead of
    compacting first. Other routes keep the default estimate.
    """
    value = (model or {}).get("context_admission_bytes_per_token")
    if type(value) is int and 1 <= value < ESTIMATED_BYTES_PER_TOKEN:
        return value
    return ESTIMATED_BYTES_PER_TOKEN


def configured_output(model):
    configured = model.get("max_output_tokens")
    if model.get("max_output_basis") == "vendor_family_guess" and type(configured) is int:
        return min(configured, 16384)
    if model.get("max_output_tokens_known") is False or configured is None:
        metadata = context_metadata(model)
        return min(16384, metadata["context_window_tokens"] // 4) if metadata["context_window_known"] else 8192
    if type(configured) is not int or configured < 1:
        raise AgentError("agent_token_limit")
    return configured


def estimate_image_tokens(width: int, height: int) -> int:
    """Conservative model-independent full-image token heuristic; never claimed as provider tokenizer truth."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("invalid_image_dimensions")
    area_tokens = (width * height + IMAGE_AREA_DIVISOR - 1) // IMAGE_AREA_DIVISOR
    return min(IMAGE_MAX_TOKENS, area_tokens) + IMAGE_FIXED_OVERHEAD_TOKENS


def _inspect_canonical_image_part(
    msg_role,
    part,
    *,
    max_single_bytes,
    max_single_pixels,
    message_index: int = 0,
    part_index: int = 0,
):
    """Validate a strictly canonical user content[].type=image_url data URL part.

    Delegates strict structural/MIME/base64/decoder verification to the shared
    dependency-neutral parser in ``llm_package.image_input_profile``.
    Returns (verified_info_dict, None) on success or (None, rejection_reason) when invalid.
    """
    identity, err_reason = inspect_canonical_user_image(
        msg_role,
        part,
        message_index=message_index,
        part_index=part_index,
        max_single_bytes=max_single_bytes,
        max_single_pixels=max_single_pixels,
    )
    if identity is None:
        return None, err_reason
    tokens = estimate_image_tokens(identity.width, identity.height)
    return {
        "mime_type": identity.mime_type,
        "format": identity.format,
        "decoded_bytes": identity.decoded_bytes,
        "raw_url_utf8_bytes": identity.raw_url_utf8_bytes,
        "width": identity.width,
        "height": identity.height,
        "pixels": identity.pixels,
        "detail": identity.detail,
        "estimated_tokens": tokens,
        "sha256": identity.decoded_sha256,
    }, None


def analyze_request(
    messages,
    specs,
    *,
    max_raw_request_bytes: int = MAX_RAW_REQUEST_UTF8_BYTES,
    max_images: int = MAX_REQUEST_IMAGES,
    max_single_image_bytes: int = MAX_SINGLE_IMAGE_DECODED_BYTES,
    max_total_image_bytes: int = MAX_TOTAL_IMAGE_DECODED_BYTES,
    max_single_image_pixels: int = MAX_SINGLE_IMAGE_PIXELS,
    max_total_image_pixels: int = MAX_TOTAL_IMAGE_PIXELS,
):
    """Analyze a canonical request, separating verified inline data images from text projection.

    Only strictly validated canonical user `content[].type=image_url` data URLs receive
    text-projection base64 exemption. Prose, tool arguments, tool outputs, remote URLs,
    and malformed structures retain full raw UTF-8 text accounting.
    """
    raw_messages = encoded(messages)
    raw_specs = encoded(specs)
    raw_messages_bytes = len(raw_messages)
    specs_bytes = len(raw_specs)
    raw_serialized_utf8_bytes = raw_messages_bytes + specs_bytes

    verified_images = []
    invalid_image_parts = []
    transport_violations = []
    projected_messages = None

    if isinstance(messages, list):
        for msg_idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            role = msg.get("role")
            for part_idx, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                if not is_candidate_image_part(part):
                    continue
                info, err_reason = _inspect_canonical_image_part(
                    role,
                    part,
                    max_single_bytes=max_single_image_bytes,
                    max_single_pixels=max_single_image_pixels,
                    message_index=msg_idx,
                    part_index=part_idx,
                )
                if info is None:
                    invalid_image_parts.append({
                        "message_index": msg_idx,
                        "part_index": part_idx,
                        "reason": err_reason or "invalid_image_part",
                    })
                    if err_reason in {"single_image_decoded_bytes_exceeded", "single_image_pixels_exceeded"}:
                        transport_violations.append(err_reason)
                    continue
                if projected_messages is None:
                    projected_messages = copy.deepcopy(messages)
                projected_messages[msg_idx]["content"][part_idx] = {
                    "type": "image_url",
                    "image_url": {"url": IMAGE_TEXT_PROJECTION_PLACEHOLDER},
                }
                info["index"] = len(verified_images)
                info["message_index"] = msg_idx
                info["part_index"] = part_idx
                verified_images.append(info)

    if projected_messages is None:
        text_projection_utf8_bytes = raw_serialized_utf8_bytes
        projected_raw = raw_messages
    else:
        projected_raw = encoded(projected_messages)
        text_projection_utf8_bytes = len(projected_raw) + specs_bytes

    char_count = len(projected_raw.decode("utf-8")) + len(raw_specs.decode("utf-8"))
    cjk_char_estimate = (text_projection_utf8_bytes - char_count) // 2
    cjk_share = min(1.0, cjk_char_estimate * 3 / text_projection_utf8_bytes)

    image_count = len(verified_images)
    image_decoded_bytes = sum(item["decoded_bytes"] for item in verified_images)
    total_image_pixels = sum(item["pixels"] for item in verified_images)
    estimated_text_tokens = (
        math.ceil(text_projection_utf8_bytes / (ESTIMATED_BYTES_PER_TOKEN - cjk_share))
        + FRAMING_RESERVE_TOKENS
    )
    estimated_image_tokens = sum(item["estimated_tokens"] for item in verified_images)
    estimated_input_tokens = estimated_text_tokens + estimated_image_tokens
    projected_context_utf8_bytes = (
        text_projection_utf8_bytes + estimated_image_tokens * ESTIMATED_BYTES_PER_TOKEN
    )

    if raw_serialized_utf8_bytes > max_raw_request_bytes:
        transport_violations.append("raw_request_utf8_bytes_exceeded")
    if image_count > max_images:
        transport_violations.append("max_request_images_exceeded")
    if image_decoded_bytes > max_total_image_bytes:
        transport_violations.append("total_image_decoded_bytes_exceeded")
    if total_image_pixels > max_total_image_pixels:
        transport_violations.append("total_image_pixels_exceeded")

    basis = IMAGE_ESTIMATE_BASIS if image_count > 0 else ESTIMATE_BASIS
    return {
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_input_utf8_bytes": text_projection_utf8_bytes,
        "raw_serialized_utf8_bytes": raw_serialized_utf8_bytes,
        "text_projection_utf8_bytes": text_projection_utf8_bytes,
        "cjk_char_estimate": cjk_char_estimate,
        "cjk_share": cjk_share,
        "projected_context_utf8_bytes": projected_context_utf8_bytes,
        "image_count": image_count,
        "image_decoded_bytes": image_decoded_bytes,
        "total_pixels": total_image_pixels,
        "total_image_pixels": total_image_pixels,
        "estimated_text_tokens": estimated_text_tokens,
        "estimated_image_tokens": estimated_image_tokens,
        "text_estimate": estimated_text_tokens,
        "image_estimate": estimated_image_tokens,
        "estimate_basis": basis,
        "image_estimate_basis": IMAGE_TOKEN_FORMULA_BASIS if image_count > 0 else "none",
        "formula_version": IMAGE_ANALYSIS_VERSION,
        "token_count_is_estimate": True,
        "image_token_count_is_heuristic": True,
        "provider_tokenizer_verified": False,
        "framing_reserve_tokens": FRAMING_RESERVE_TOKENS,
        "images": verified_images,
        "invalid_image_count": len(invalid_image_parts),
        "has_invalid_image_parts": bool(invalid_image_parts),
        "invalid_image_parts": invalid_image_parts,
        "transport_violations": transport_violations,
        "limits": {"max_raw_request_bytes": max_raw_request_bytes, "max_images": max_images,
                   "max_single_image_bytes": max_single_image_bytes, "max_total_image_bytes": max_total_image_bytes,
                   "max_single_image_pixels": max_single_image_pixels, "max_total_image_pixels": max_total_image_pixels},
    }


def estimate_input(messages, specs):
    """Count the complete neutral request projection; this is not tokenization.

    JSON includes roles, tool schemas, history and escaping. Provider-specific
    framing is covered by a fixed reserve. Different model tokenizers can still
    disagree, so usage reported by the provider remains the actual token count.
    """
    analysis = analyze_request(messages, specs)
    return {k: v for k, v in analysis.items() if k not in {"images", "invalid_image_parts"}}


def context_budget(model, manual_limit=None, *, provenance=None):
    """Reserve useful output space; each request still clamps its actual cap."""
    metadata = context_metadata(model)
    reserve = min(configured_output(model), max(MIN_OUTPUT_RESERVE_TOKENS, metadata["context_window_tokens"] // 4))
    density = admission_bytes_per_token(model)
    effective = max(0, metadata["context_window_tokens"] - reserve - FRAMING_RESERVE_TOKENS) * density
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
            "estimate_basis": ESTIMATE_BASIS if density == ESTIMATED_BYTES_PER_TOKEN else RYN_ADMISSION_ESTIMATE_BASIS,
            "admission_bytes_per_token": density, "token_count_is_estimate": True,
            "units": {"max_context_chars": "serialized_request_utf8_bytes", "context_window": "tokens"}}


def _enforce_analysis_bounds(analysis):
    def reject(code, reason):
        raise AgentError(code, details={"reason": reason, "limits": analysis["limits"]})
    if analysis["has_invalid_image_parts"]:
        for item in analysis["invalid_image_parts"]:
            reason = item.get("reason")
            if reason == "single_image_pixels_exceeded":
                reject("agent_image_pixel_limit", reason)
            if reason == "single_image_decoded_bytes_exceeded":
                reject("agent_image_too_large", reason)
        raise AgentError("agent_image_invalid")
    if analysis["transport_violations"]:
        codes = {"single_image_pixels_exceeded": "agent_image_pixel_limit", "total_image_pixels_exceeded": "agent_image_pixel_limit",
                 "single_image_decoded_bytes_exceeded": "agent_image_too_large", "total_image_decoded_bytes_exceeded": "agent_image_too_large",
                 "max_request_images_exceeded": "agent_image_count_limit", "raw_request_utf8_bytes_exceeded": "agent_context_limit"}
        for reason, code in codes.items():
            if reason in analysis["transport_violations"]:
                reject(code, reason)


def admit_request_context(
    model,
    messages,
    specs,
    *,
    manual_limit=None,
    provenance=None,
    context_policy=None,
    capacity_bytes=None,
    max_raw_request_bytes: int = MAX_RAW_REQUEST_UTF8_BYTES,
    max_images: int = MAX_REQUEST_IMAGES,
    max_single_image_bytes: int = MAX_SINGLE_IMAGE_DECODED_BYTES,
    max_total_image_bytes: int = MAX_TOTAL_IMAGE_DECODED_BYTES,
    max_single_image_pixels: int = MAX_SINGLE_IMAGE_PIXELS,
    max_total_image_pixels: int = MAX_TOTAL_IMAGE_PIXELS,
):
    """Enforce manual raw-byte limit, transport/image limits, and image-aware auto context admission."""
    policy = context_policy if isinstance(context_policy, dict) else context_budget(
        model, manual_limit, provenance=provenance
    )
    effective_manual = policy.get("manual_max_context_bytes") if manual_limit is None else manual_limit
    analysis = analyze_request(
        messages,
        specs,
        max_raw_request_bytes=max_raw_request_bytes,
        max_images=max_images,
        max_single_image_bytes=max_single_image_bytes,
        max_total_image_bytes=max_total_image_bytes,
        max_single_image_pixels=max_single_image_pixels,
        max_total_image_pixels=max_total_image_pixels,
    )
    if effective_manual is not None and analysis["raw_serialized_utf8_bytes"] > effective_manual:
        raise AgentError("agent_context_limit")
    _enforce_analysis_bounds(analysis)
    capacity = capacity_bytes if capacity_bytes is not None and policy.get("mode") == "auto" else policy["effective_max_context_bytes"]
    if analysis["projected_context_utf8_bytes"] > capacity:
        raise AgentError("agent_context_limit")
    return analysis


def _input_tokens_at_density(analysis, bytes_per_token):
    if bytes_per_token is None:
        return analysis["estimated_input_tokens"]
    return math.ceil(analysis["text_projection_utf8_bytes"] / bytes_per_token) + FRAMING_RESERVE_TOKENS + analysis["estimated_image_tokens"]


def output_room(model, analysis, *, bytes_per_token=None):
    """Available window tokens without raising when the request overflows."""
    window = context_metadata(model)["context_window_tokens"]
    room = window - _input_tokens_at_density(analysis, bytes_per_token)
    density = admission_bytes_per_token(model)
    if density != ESTIMATED_BYTES_PER_TOKEN:
        admitted = ((analysis["text_projection_utf8_bytes"] + density - 1) // density
                    + FRAMING_RESERVE_TOKENS + analysis["estimated_image_tokens"])
        room = min(room, window - admitted)
    return room


def select(model, messages, specs, *, request_cap=None, bytes_per_token=None):
    configured = configured_output(model)
    metadata = context_metadata(model)
    window = metadata["context_window_tokens"]
    if request_cap is not None and (type(request_cap) is not int or request_cap < 1):
        raise AgentError("agent_token_limit")
    analysis = analyze_request(messages, specs)
    _enforce_analysis_bounds(analysis)
    analysis["estimated_input_tokens"] = _input_tokens_at_density(analysis, bytes_per_token)
    if bytes_per_token is not None:
        analysis["estimated_text_tokens"] = math.ceil(analysis["text_projection_utf8_bytes"] / bytes_per_token)
        analysis["estimate_basis"] = "provider_usage_calibrated_text_density_plus_512_and_image_heuristic"
    estimate = {k: v for k, v in analysis.items() if k not in {"images", "invalid_image_parts"}}
    limits = {"configured": configured, "transport": TRANSPORT_MAX_TOKENS, "context": window - estimate["estimated_input_tokens"]}
    density = admission_bytes_per_token(model)
    if density != ESTIMATED_BYTES_PER_TOKEN:
        # The route admits bytes + output against the window; keep the output cap
        # within that rule while the token estimate above stays realistic.
        admitted_input = ((estimate["text_projection_utf8_bytes"] + density - 1) // density
                          + FRAMING_RESERVE_TOKENS + estimate["estimated_image_tokens"])
        limits["admission"] = window - admitted_input
    if request_cap is not None:
        limits["request"] = request_cap
    maximum = min(*limits.values(), output_room(model, analysis))
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


def answer_limit(row):
    """Per-response visible prose byte limit, separate from inline tool result limits."""
    limits = (row or {}).get("limits") if isinstance(row, dict) else None
    configured = limits.get("max_output_chars", 0) if isinstance(limits, dict) else 0
    base = configured if type(configured) is int and configured > 0 else 0
    return min(MAX_RAW_RESPONSE_BYTES, max(base, 256 * 1024))


def visible_message(message):
    """Only the typed private continuation and structured tool calls are excluded from visible prose bounds, never unknown data."""
    return {key: value for key, value in message.items() if key not in {"reasoning_content", "provider_state", "tool_calls"}}


def storage_growth(visible_limit, *, row=None, kind="tool"):
    # Existing visible result/event reserves plus one raw bounded continuation
    # in the encrypted message history. Model steps reserve for answer_limit(row).
    if isinstance(visible_limit, dict):
        visible_limit = answer_limit(visible_limit)
    elif row is not None and kind == "model":
        visible_limit = answer_limit(row)
    return 8 * visible_limit + 128 * 1024 + MAX_RAW_RESPONSE_BYTES
