"""One bounded generation policy, independent of visible answer byte limits."""
from __future__ import annotations

import copy
import hashlib

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
IMAGE_ANALYSIS_VERSION = "canonical-image-heuristic-v1"
TRANSPORT_MAX_TOKENS = 393216
MAX_RAW_RESPONSE_BYTES = 256 * 1024
MAX_TOOL_CALL_ARGUMENT_BYTES = 96 * 1024
MAX_TOOL_CALLS = 8
UNKNOWN_CONTEXT_WINDOW_TOKENS = 32768
FRAMING_RESERVE_TOKENS = 512
ESTIMATED_BYTES_PER_TOKEN = 3
ESTIMATE_BASIS = "serialized_utf8_bytes_div_3_ceil_plus_512_framing_heuristic"
IMAGE_ESTIMATE_BASIS = "text_utf8_div_3_ceil_plus_512_and_image_patch16_x3_plus_1024_heuristic"
IMAGE_TOKEN_FORMULA_BASIS = "heuristic_3_times_ceil_w_16_times_ceil_h_16_plus_1024_not_provider_tokenizer"
IMAGE_PATCH_SIZE = 16
IMAGE_TOKENS_PER_PATCH = 3
IMAGE_FIXED_OVERHEAD_TOKENS = 1024
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


def configured_output(model):
    configured = model.get("max_output_tokens", 4096)
    if type(configured) is not int or configured < 1:
        raise AgentError("agent_token_limit")
    return configured


def estimate_image_tokens(width: int, height: int) -> int:
    """Conservative model-independent full-image token heuristic; never claimed as provider tokenizer truth."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("invalid_image_dimensions")
    patches_w = (width + IMAGE_PATCH_SIZE - 1) // IMAGE_PATCH_SIZE
    patches_h = (height + IMAGE_PATCH_SIZE - 1) // IMAGE_PATCH_SIZE
    return IMAGE_TOKENS_PER_PATCH * patches_w * patches_h + IMAGE_FIXED_OVERHEAD_TOKENS


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
    raw_messages_bytes = len(encoded(messages))
    specs_bytes = len(encoded(specs))
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
    else:
        text_projection_utf8_bytes = len(encoded(projected_messages)) + specs_bytes

    image_count = len(verified_images)
    image_decoded_bytes = sum(item["decoded_bytes"] for item in verified_images)
    total_image_pixels = sum(item["pixels"] for item in verified_images)
    estimated_text_tokens = (
        (text_projection_utf8_bytes + ESTIMATED_BYTES_PER_TOKEN - 1) // ESTIMATED_BYTES_PER_TOKEN
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


def _enforce_analysis_bounds(analysis):
    if analysis["has_invalid_image_parts"]:
        for item in analysis["invalid_image_parts"]:
            reason = item.get("reason")
            if reason == "single_image_pixels_exceeded":
                raise AgentError("agent_image_pixel_limit")
            if reason == "single_image_decoded_bytes_exceeded":
                raise AgentError("agent_context_limit")
        raise AgentError("agent_image_invalid")
    if analysis["transport_violations"]:
        if "single_image_pixels_exceeded" in analysis["transport_violations"] or "total_image_pixels_exceeded" in analysis["transport_violations"]:
            raise AgentError("agent_image_pixel_limit")
        raise AgentError("agent_context_limit")


def admit_request_context(
    model,
    messages,
    specs,
    *,
    manual_limit=None,
    provenance=None,
    context_policy=None,
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
    if analysis["projected_context_utf8_bytes"] > policy["effective_max_context_bytes"]:
        raise AgentError("agent_context_limit")
    return analysis


def select(model, messages, specs, *, request_cap=None):
    configured = configured_output(model)
    metadata = context_metadata(model)
    window = metadata["context_window_tokens"]
    if request_cap is not None and (type(request_cap) is not int or request_cap < 1):
        raise AgentError("agent_token_limit")
    analysis = analyze_request(messages, specs)
    _enforce_analysis_bounds(analysis)
    estimate = {k: v for k, v in analysis.items() if k not in {"images", "invalid_image_parts"}}
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
