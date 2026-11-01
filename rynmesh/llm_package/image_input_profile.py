"""Dependency-neutral canonical user-image parser and narrow internal image profile scope.

This module imports neither ``agent_runtime`` nor ``llm_package`` adapters/chat/codecs.
It authorizes bounded transport of canonical user image bytes for local Agent requests;
it never grants native-window provenance, pixel recognition, or remote peer expansion.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

MAX_INTERNAL_RAW_REQUEST_BYTES = 16 * 1024 * 1024
MAX_INTERNAL_REQUEST_IMAGES = 4
MAX_INTERNAL_SINGLE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_INTERNAL_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_INTERNAL_SINGLE_IMAGE_PIXELS = 16_000_000
MAX_INTERNAL_TOTAL_IMAGE_PIXELS = 32_000_000

LEGACY_PUBLIC_MAX_URL_CHARS = 180_000
LEGACY_PUBLIC_MAX_IMAGE_BYTES = 128 * 1024
LEGACY_PUBLIC_MAX_IMAGES_PER_MESSAGE = 4
LEGACY_PUBLIC_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})

_CANONICAL_DATA_IMAGE_URL_RE = re.compile(
    r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})\Z"
)
_ANY_DATA_BASE64_URL_RE = re.compile(
    r"data:([^;,]+);base64,([A-Za-z0-9+/]+={0,2})\Z",
    re.IGNORECASE,
)
_LEGACY_PUBLIC_DATA_IMAGE_URL_RE = re.compile(
    r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}\Z"
)
_MIME_TO_PIL_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}
_ROUTE_SUPPORTED_MIMES: dict[str, frozenset[str]] = {
    "openai_compatible": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    "openai_chat": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    "openai_responses": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    "anthropic_messages": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    "codex_cli": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
}


@dataclass(frozen=True)
class ImagePartLimits:
    max_single_bytes: int = MAX_INTERNAL_SINGLE_IMAGE_BYTES
    max_single_pixels: int = MAX_INTERNAL_SINGLE_IMAGE_PIXELS
    allowed_mimes: frozenset[str] = frozenset(_MIME_TO_PIL_FORMAT)


@dataclass(frozen=True)
class ImagePartIdentity:
    message_index: int
    part_index: int
    url_sha256: str
    raw_url_utf8_bytes: int
    decoded_sha256: str
    decoded_bytes: int
    mime_type: str
    format: str
    width: int
    height: int
    pixels: int
    detail: str
    explicit_detail: str | None

    def __copy__(self) -> "ImagePartIdentity":
        return self

    def __deepcopy__(self, memo: Any) -> "ImagePartIdentity":
        return self


_PARSER_ISSUED_PROOF = object()


def _identity_facts_tuple(identity: ImagePartIdentity) -> tuple[Any, ...]:
    return (
        identity.message_index,
        identity.part_index,
        identity.url_sha256,
        identity.raw_url_utf8_bytes,
        identity.decoded_sha256,
        identity.decoded_bytes,
        identity.mime_type,
        identity.format,
        identity.width,
        identity.height,
        identity.pixels,
        identity.detail,
        identity.explicit_detail,
    )


def _stamp_parser_issued_identity(identity: ImagePartIdentity) -> ImagePartIdentity:
    object.__setattr__(identity, "_parser_proof", (_PARSER_ISSUED_PROOF, _identity_facts_tuple(identity)))
    return identity


def _has_valid_parser_proof(candidate: Any) -> bool:
    if type(candidate) is not ImagePartIdentity:
        return False
    proof = getattr(candidate, "_parser_proof", None)
    if not isinstance(proof, tuple) or len(proof) != 2:
        return False
    token, facts = proof
    if token is not _PARSER_ISSUED_PROOF or facts != _identity_facts_tuple(candidate):
        return False
    expected_fmt = _MIME_TO_PIL_FORMAT.get(candidate.mime_type)
    if (
        not expected_fmt
        or candidate.format != expected_fmt
        or type(candidate.width) is not int
        or type(candidate.height) is not int
        or candidate.width <= 0
        or candidate.height <= 0
        or candidate.pixels != candidate.width * candidate.height
        or type(candidate.decoded_bytes) is not int
        or candidate.decoded_bytes <= 0
    ):
        return False
    return True


@dataclass(frozen=True)
class RouteImageIdentity:
    model_ref: str
    source: str
    adapter: str
    service_id: str
    model_id: str
    destination_revision: str


@dataclass(frozen=True)
class InternalImageProfile:
    request_identity: str
    route_identity: RouteImageIdentity
    admitted_images: tuple[ImagePartIdentity, ...]
    total_decoded_bytes: int
    total_pixels: int
    max_raw_request_bytes: int = MAX_INTERNAL_RAW_REQUEST_BYTES
    max_images: int = MAX_INTERNAL_REQUEST_IMAGES
    max_single_image_bytes: int = MAX_INTERNAL_SINGLE_IMAGE_BYTES
    max_total_image_bytes: int = MAX_INTERNAL_TOTAL_IMAGE_BYTES
    max_single_image_pixels: int = MAX_INTERNAL_SINGLE_IMAGE_PIXELS
    max_total_image_pixels: int = MAX_INTERNAL_TOTAL_IMAGE_PIXELS


def _is_valid_internal_image_profile(profile: Any) -> bool:
    if type(profile) is not InternalImageProfile:
        return False
    if type(profile.route_identity) is not RouteImageIdentity:
        return False
    if profile.route_identity.source not in {"local_api", "local_service"}:
        return False
    if not isinstance(profile.admitted_images, tuple) or not profile.admitted_images:
        return False
    if not all(_has_valid_parser_proof(item) for item in profile.admitted_images):
        return False
    if profile.total_decoded_bytes != sum(item.decoded_bytes for item in profile.admitted_images):
        return False
    if profile.total_pixels != sum(item.pixels for item in profile.admitted_images):
        return False
    return True


_CURRENT_PROFILE: ContextVar[InternalImageProfile | None] = ContextVar(
    "ryn_internal_image_input_profile", default=None
)


def current_internal_image_profile() -> InternalImageProfile | None:
    """Return the active internal image profile if it is an authentic InternalImageProfile."""
    profile = _CURRENT_PROFILE.get()
    return profile if _is_valid_internal_image_profile(profile) else None


@contextmanager
def internal_image_scope(profile: InternalImageProfile | None):
    """Enter a narrow internal image carrier scope and unconditionally reset on exit."""
    if profile is not None and not _is_valid_internal_image_profile(profile):
        if type(profile) is not InternalImageProfile:
            raise TypeError("invalid_internal_image_profile")
        raise ValueError("invalid_internal_image_profile")
    token = _CURRENT_PROFILE.set(profile)
    try:
        yield profile
    finally:
        _CURRENT_PROFILE.reset(token)


@contextmanager
def cleared_internal_image_scope():
    """Temporarily clear any ambient internal image profile (e.g., around untrusted/external callbacks)."""
    token = _CURRENT_PROFILE.set(None)
    try:
        yield
    finally:
        _CURRENT_PROFILE.reset(token)


def is_candidate_image_part(part: Any) -> bool:
    return isinstance(part, dict) and (
        part.get("type") in {"image_url", "image", "input_image"} or "image_url" in part
    )


def inspect_canonical_user_image(
    msg_role: Any,
    part: Any,
    *,
    message_index: int = 0,
    part_index: int = 0,
    limits: ImagePartLimits | None = None,
    max_single_bytes: int | None = None,
    max_single_pixels: int | None = None,
    allowed_mimes: frozenset[str] | set[str] | None = None,
) -> tuple[ImagePartIdentity | None, str | None]:
    """Strictly parse and decode one canonical user ``content[].type=image_url`` part.

    Never mutates or re-encodes pixels. Returns ``(ImagePartIdentity, None)`` on
    success or ``(None, error_reason)`` on any schema, MIME, base64, size, pixel,
    or decoder failure.
    """
    if type(message_index) is not int or message_index < 0 or type(part_index) is not int or part_index < 0:
        return None, "invalid_image_part_position"
    active_limits = limits if isinstance(limits, ImagePartLimits) else ImagePartLimits()
    single_bytes_cap = active_limits.max_single_bytes if max_single_bytes is None else max_single_bytes
    single_pixels_cap = active_limits.max_single_pixels if max_single_pixels is None else max_single_pixels
    mimes_allowed = (
        active_limits.allowed_mimes
        if allowed_mimes is None
        else frozenset(str(m).lower() for m in allowed_mimes)
    )

    if msg_role != "user":
        return None, "image_part_non_user_role"
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None, "noncanonical_image_part_type"
    if set(part) - {"type", "image_url"}:
        return None, "noncanonical_image_part_keys"
    image_url = part.get("image_url")
    if not isinstance(image_url, dict) or "url" not in image_url or (set(image_url) - {"url", "detail"}):
        return None, "noncanonical_image_url_object"
    detail = image_url.get("detail")
    if detail is not None and (not isinstance(detail, str) or detail not in {"high", "low", "auto"}):
        return None, "invalid_image_detail"
    url = image_url.get("url")
    if not isinstance(url, str) or not url:
        return None, "missing_image_url"
    match = _CANONICAL_DATA_IMAGE_URL_RE.fullmatch(url)
    if not match:
        if _ANY_DATA_BASE64_URL_RE.fullmatch(url):
            return None, "unsupported_image_mime"
        return None, "remote_or_non_data_image_url"
    mime_type = match.group(1)
    expected_fmt = _MIME_TO_PIL_FORMAT.get(mime_type)
    if not expected_fmt or mime_type not in mimes_allowed:
        return None, "unsupported_image_mime"
    b64_payload = match.group(2)
    if len(b64_payload) % 4 != 0:
        return None, "invalid_base64_length"
    max_b64_len = ((single_bytes_cap + 2) // 3) * 4
    if len(b64_payload) > max_b64_len:
        return None, "single_image_decoded_bytes_exceeded"
    try:
        raw = base64.b64decode(b64_payload, validate=True)
    except ValueError:
        return None, "invalid_base64_payload"
    if not raw:
        return None, "empty_image_payload"
    if len(raw) > single_bytes_cap:
        return None, "single_image_decoded_bytes_exceeded"

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            fmt = (img.format or "").upper()
            if type(width) is not int or type(height) is not int or width <= 0 or height <= 0 or fmt != expected_fmt:
                return None, "image_format_or_dimensions_invalid"
            if width * height > single_pixels_cap:
                return None, "single_image_pixels_exceeded"
            img.verify()
        with Image.open(io.BytesIO(raw)) as img2:
            img2.load()
    except Image.DecompressionBombError:
        return None, "single_image_pixels_exceeded"
    except (OSError, UnidentifiedImageError, ValueError):
        return None, "invalid_or_corrupt_image_bytes"

    url_bytes = url.encode("utf-8")
    return (
        _stamp_parser_issued_identity(
            ImagePartIdentity(
                message_index=message_index,
                part_index=part_index,
                url_sha256=hashlib.sha256(url_bytes).hexdigest(),
                raw_url_utf8_bytes=len(url_bytes),
                decoded_sha256=hashlib.sha256(raw).hexdigest(),
                decoded_bytes=len(raw),
                mime_type=mime_type,
                format=expected_fmt,
                width=width,
                height=height,
                pixels=width * height,
                detail=detail or "high",
                explicit_detail=detail,
            )
        ),
        None,
    )


def route_supported_image_mimes(target: dict[str, Any] | None) -> frozenset[str]:
    """Return canonical image MIME types supported by a target route carrier."""
    if not isinstance(target, dict):
        return frozenset()
    source = str(target.get("source") or "").strip()
    if source == "peer":
        base = LEGACY_PUBLIC_ALLOWED_MIMES
    else:
        adapter = str(target.get("adapter") or target.get("cli_source") or "").strip()
        if not adapter and source == "local_api":
            adapter = "openai_compatible"
        base = _ROUTE_SUPPORTED_MIMES.get(adapter, frozenset())
    for key in ("supported_image_mimes", "allowed_image_mimes", "image_mimes"):
        explicit = target.get(key)
        if isinstance(explicit, (list, tuple, set, frozenset)):
            return base & frozenset(str(m).strip().lower() for m in explicit if isinstance(m, str))
    return base


def build_route_image_identity(target: dict[str, Any]) -> RouteImageIdentity:
    if not isinstance(target, dict):
        raise ValueError("invalid_route_target")
    source = str(target.get("source") or "").strip()
    adapter = str(target.get("adapter") or target.get("cli_source") or "").strip()
    if not adapter and source == "local_api":
        adapter = "openai_compatible"
    return RouteImageIdentity(
        model_ref=str(target.get("id") or ""),
        source=source,
        adapter=adapter,
        service_id=str(target.get("service_id") or ""),
        model_id=str(target.get("model_id") or target.get("cli_model") or ""),
        destination_revision=str(target.get("destination_revision") or ""),
    )


def _match_part_to_detached_identity(
    msg_role: Any,
    part: Any,
    *,
    message_index: int,
    part_index: int,
    candidate: ImagePartIdentity,
) -> bool:
    """Check that a parser-issued ImagePartIdentity matches the exact current part."""
    if not _has_valid_parser_proof(candidate):
        return False
    if msg_role != "user":
        return False
    if candidate.message_index != message_index or candidate.part_index != part_index:
        return False
    if not isinstance(part, dict) or part.get("type") != "image_url" or (set(part) - {"type", "image_url"}):
        return False
    image_url = part.get("image_url")
    if not isinstance(image_url, dict) or "url" not in image_url or (set(image_url) - {"url", "detail"}):
        return False
    detail = image_url.get("detail")
    if detail != candidate.explicit_detail or (detail or "high") != candidate.detail:
        return False
    url = image_url.get("url")
    if not isinstance(url, str) or not url:
        return False
    url_bytes = url.encode("utf-8")
    if len(url_bytes) != candidate.raw_url_utf8_bytes:
        return False
    if hashlib.sha256(url_bytes).hexdigest() != candidate.url_sha256:
        return False
    return True


def prepare_internal_image_profile(
    messages: Any,
    *,
    route_identity: RouteImageIdentity | dict[str, Any],
    request_identity: str,
    admitted_image_identities: list[ImagePartIdentity] | tuple[ImagePartIdentity, ...] | None = None,
    max_raw_request_bytes: int = MAX_INTERNAL_RAW_REQUEST_BYTES,
    max_images: int = MAX_INTERNAL_REQUEST_IMAGES,
    max_single_image_bytes: int = MAX_INTERNAL_SINGLE_IMAGE_BYTES,
    max_total_image_bytes: int = MAX_INTERNAL_TOTAL_IMAGE_BYTES,
    max_single_image_pixels: int = MAX_INTERNAL_SINGLE_IMAGE_PIXELS,
    max_total_image_pixels: int = MAX_INTERNAL_TOTAL_IMAGE_PIXELS,
) -> InternalImageProfile | None:
    """Construct a bound InternalImageProfile for local Agent image dispatch.

    Returns ``None`` for text-only requests (zero images). Never trusts plain
    dictionaries, unverified caller claims, or mutated/replaced dataclasses:
    every image part in ``messages`` is parsed and decoded from actual canonical
    bytes, and any provided ``admitted_image_identities`` must be genuine
    parser-issued ``ImagePartIdentity`` records whose fields match all freshly
    verified facts.
    """
    if not isinstance(request_identity, str) or not request_identity.strip():
        raise ValueError("invalid_request_identity")
    resolved_route = (
        route_identity
        if type(route_identity) is RouteImageIdentity
        else build_route_image_identity(route_identity)
        if isinstance(route_identity, dict)
        else None
    )
    if resolved_route is None or resolved_route.source not in {"local_api", "local_service"}:
        raise ValueError("internal_image_profile_requires_local_route")
    allowed_mimes = (
        route_supported_image_mimes(route_identity)
        if isinstance(route_identity, dict)
        else _ROUTE_SUPPORTED_MIMES.get(resolved_route.adapter, frozenset())
    )
    if not allowed_mimes:
        raise ValueError("unsupported_route_image_carrier")

    if not isinstance(messages, list) or not messages:
        raise ValueError("invalid_messages_for_image_profile")

    detached_by_pos: dict[tuple[int, int], ImagePartIdentity] = {}
    if admitted_image_identities is not None:
        if not isinstance(admitted_image_identities, (list, tuple)):
            raise ValueError("invalid_admitted_image_identities")
        for item in admitted_image_identities:
            if type(item) is not ImagePartIdentity:
                raise ValueError("admitted_image_identity_must_be_typed")
            if not _has_valid_parser_proof(item):
                raise ValueError("admitted_image_identities_mismatch")
            pos = (item.message_index, item.part_index)
            if pos in detached_by_pos:
                raise ValueError("duplicate_admitted_image_position")
            detached_by_pos[pos] = item

    verified: list[ImagePartIdentity] = []
    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError("invalid_message_in_image_profile")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role")
        for part_idx, part in enumerate(content):
            if not isinstance(part, dict):
                raise ValueError("invalid_content_part_in_image_profile")
            if part.get("type") == "text" and isinstance(part.get("text"), str) and not (set(part) - {"type", "text"}):
                continue
            detached = detached_by_pos.get((msg_idx, part_idx))
            if admitted_image_identities is not None and (
                detached is None
                or not _match_part_to_detached_identity(
                    role, part, message_index=msg_idx, part_index=part_idx, candidate=detached
                )
            ):
                raise ValueError("admitted_image_identities_mismatch")
            identity, err = inspect_canonical_user_image(
                role,
                part,
                message_index=msg_idx,
                part_index=part_idx,
                max_single_bytes=max_single_image_bytes,
                max_single_pixels=max_single_image_pixels,
                allowed_mimes=allowed_mimes,
            )
            if identity is None:
                raise ValueError(err or "invalid_image_part")
            if detached is not None and _identity_facts_tuple(detached) != _identity_facts_tuple(identity):
                raise ValueError("admitted_image_identities_mismatch")
            if identity.mime_type not in allowed_mimes:
                raise ValueError("unsupported_image_mime")
            if identity.decoded_bytes <= 0 or identity.decoded_bytes > max_single_image_bytes:
                raise ValueError("single_image_decoded_bytes_exceeded")
            if identity.pixels <= 0 or identity.pixels > max_single_image_pixels:
                raise ValueError("single_image_pixels_exceeded")
            verified.append(identity)

    if admitted_image_identities is not None and tuple(verified) != tuple(admitted_image_identities):
        raise ValueError("admitted_image_identities_mismatch")

    if not verified:
        return None

    total_bytes = sum(item.decoded_bytes for item in verified)
    total_pixels = sum(item.pixels for item in verified)
    if len(verified) > max_images:
        raise ValueError("max_request_images_exceeded")
    if total_bytes > max_total_image_bytes:
        raise ValueError("total_image_decoded_bytes_exceeded")
    if total_pixels > max_total_image_pixels:
        raise ValueError("total_image_pixels_exceeded")

    return InternalImageProfile(
        request_identity=request_identity,
        route_identity=resolved_route,
        admitted_images=tuple(verified),
        total_decoded_bytes=total_bytes,
        total_pixels=total_pixels,
        max_raw_request_bytes=max_raw_request_bytes,
        max_images=max_images,
        max_single_image_bytes=max_single_image_bytes,
        max_total_image_bytes=max_total_image_bytes,
        max_single_image_pixels=max_single_image_pixels,
        max_total_image_pixels=max_total_image_pixels,
    )


def verify_messages_against_profile(messages: Any, profile: InternalImageProfile) -> list[ImagePartIdentity]:
    """Verify that ``messages`` contains the exact ordered canonical image parts bound in ``profile``."""
    if not _is_valid_internal_image_profile(profile):
        raise ValueError("invalid_internal_image_profile")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")

    observed: list[ImagePartIdentity] = []
    expected = profile.admitted_images
    expected_idx = 0

    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError("invalid message role")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role")
        for part_idx, part in enumerate(content):
            if not isinstance(part, dict):
                raise ValueError("invalid content part")
            if part.get("type") == "text" and isinstance(part.get("text"), str) and not (set(part) - {"type", "text"}):
                continue
            if part.get("type") != "image_url" or role != "user" or (set(part) - {"type", "image_url"}):
                raise ValueError("only user text and inline images are supported")
            if expected_idx >= len(expected):
                raise ValueError("image profile count or order mismatch")
            target_identity = expected[expected_idx]
            if not _match_part_to_detached_identity(
                role, part, message_index=msg_idx, part_index=part_idx, candidate=target_identity
            ):
                raise ValueError("image profile identity mismatch")
            observed.append(target_identity)
            expected_idx += 1

    if expected_idx != len(expected) or tuple(observed) != expected:
        raise ValueError("image profile count or order mismatch")
    if len(observed) > profile.max_images:
        raise ValueError("image count limit exceeded")
    if sum(item.decoded_bytes for item in observed) > profile.max_total_image_bytes:
        raise ValueError("image size limit exceeded")
    if sum(item.pixels for item in observed) > profile.max_total_image_pixels:
        raise ValueError("image pixel limit exceeded")
    return observed


def check_legacy_public_carrier_compatible(messages: Any) -> tuple[bool, str | None]:
    """Check whether all image parts in ``messages`` satisfy ordinary legacy public carrier limits.

    Used before ``execute_order`` on peer routes: a peer route cannot receive a
    local Python ``ContextVar`` profile, so any image requiring the expanded
    internal carrier must be refused before consumer/remote dispatch.
    """
    if not isinstance(messages, list):
        return False, "invalid_messages"
    for msg in messages:
        if not isinstance(msg, dict):
            return False, "invalid_message"
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role")
        images_in_msg = 0
        for part in content:
            if not isinstance(part, dict):
                return False, "invalid_content_part"
            if part.get("type") == "text" and isinstance(part.get("text"), str) and not (set(part) - {"type", "text"}):
                continue
            if part.get("type") != "image_url" or role != "user" or (set(part) - {"type", "image_url"}):
                return False, "non_user_or_noncanonical_image_part"
            image = part.get("image_url")
            if (
                not isinstance(image, dict)
                or (set(image) - {"url", "detail"})
                or not isinstance(image.get("detail", "auto"), str)
                or image.get("detail", "auto") not in {"auto", "low", "high"}
            ):
                return False, "invalid_image_part"
            url = image.get("url")
            if not isinstance(url, str) or not _LEGACY_PUBLIC_DATA_IMAGE_URL_RE.fullmatch(url):
                return False, "unsupported_peer_image_mime_or_url"
            if len(url) > LEGACY_PUBLIC_MAX_URL_CHARS:
                return False, "peer_image_url_length_exceeded"
            try:
                raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            except ValueError:
                return False, "invalid_image_data"
            if not raw or len(raw) > LEGACY_PUBLIC_MAX_IMAGE_BYTES:
                return False, "peer_image_decoded_bytes_exceeded"
            images_in_msg += 1
        if images_in_msg > LEGACY_PUBLIC_MAX_IMAGES_PER_MESSAGE:
            return False, "peer_image_count_exceeded"
    return True, None


def project_protocol_request_without_admitted_images(
    payload: dict[str, Any],
    protocol: str,
    profile: InternalImageProfile,
) -> dict[str, Any]:
    """Verify closed converted native image positions and return a non-image projection copy.

    Inspects ONLY the explicit closed native image positions:
    - Responses (``openai_responses``): ``input[].role == 'user'`` -> ``content[].type == 'input_image'``
    - Anthropic (``anthropic_messages``): ``messages[].role == 'user'`` -> ``content[].type == 'image'``
    Never subtracts arbitrary ``data`` or ``image_url`` keys from text, tool arguments,
    tool results, system prompts, or continuation blocks.
    """
    if not _is_valid_internal_image_profile(profile):
        raise ValueError("invalid native image profile")
    projected = copy.deepcopy(payload)
    expected = profile.admitted_images
    matched_idx = 0

    if protocol == "openai_responses":
        items = projected.get("input")
        if not isinstance(items, list):
            raise ValueError("invalid native Responses input")
        for item in items:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part_idx, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") != "input_image":
                    continue
                if set(part) != {"type", "image_url", "detail"}:
                    raise ValueError("invalid native Responses image part")
                if matched_idx >= len(expected):
                    raise ValueError("native Responses image count mismatch")
                ident = expected[matched_idx]
                url = part.get("image_url")
                detail = part.get("detail")
                if not isinstance(url, str) or not isinstance(detail, str):
                    raise ValueError("invalid native Responses image part")
                url_bytes = url.encode("utf-8")
                expected_detail = ident.explicit_detail if ident.explicit_detail is not None else "auto"
                if (
                    len(url_bytes) != ident.raw_url_utf8_bytes
                    or hashlib.sha256(url_bytes).hexdigest() != ident.url_sha256
                    or detail != expected_detail
                ):
                    raise ValueError("native Responses image identity mismatch")
                content[part_idx] = {
                    "type": "input_image",
                    "image_url": "ryn-verified-image://inline",
                    "detail": detail,
                }
                matched_idx += 1
    elif protocol == "anthropic_messages":
        msgs = projected.get("messages")
        if not isinstance(msgs, list):
            raise ValueError("invalid native Messages list")
        for msg in msgs:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block_idx, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                source = block.get("source")
                if (
                    set(block) != {"type", "source"}
                    or not isinstance(source, dict)
                    or set(source) != {"type", "media_type", "data"}
                    or source.get("type") != "base64"
                ):
                    raise ValueError("invalid native Messages image block")
                if matched_idx >= len(expected):
                    raise ValueError("native Messages image count mismatch")
                ident = expected[matched_idx]
                media_type = source.get("media_type")
                b64_data = source.get("data")
                if not isinstance(media_type, str) or not isinstance(b64_data, str):
                    raise ValueError("invalid native Messages image block")
                norm_media = "image/jpeg" if media_type.lower() == "image/jpg" else media_type.lower()
                if norm_media != ident.mime_type:
                    raise ValueError("native Messages image media_type mismatch")
                try:
                    raw = base64.b64decode(b64_data, validate=True)
                except ValueError:
                    raise ValueError("invalid native Messages image data") from None
                if len(raw) != ident.decoded_bytes or hashlib.sha256(raw).hexdigest() != ident.decoded_sha256:
                    raise ValueError("native Messages image identity mismatch")
                content[block_idx] = {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": ""},
                }
                matched_idx += 1
    else:
        raise ValueError("unsupported native protocol")

    if matched_idx != len(expected):
        raise ValueError("native protocol image count mismatch")
    return projected
