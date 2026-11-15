"""Closed, body-free transport diagnostics. Never retain exception messages or URLs."""
import errno
import json
import math
import re
import socket
import ssl
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError

REASONS = {
    "dns_temporary",
    "dns_not_found",
    "tls_certificate",
    "tls",
    "reset",
    "refused",
    "timeout",
    "idle_timeout",
    "total_timeout",
    "http",
    "connection",
    "quota",
    "request_rejected",
    "auth_rejected",
    "context_overflow",
}

_MAX_ERROR_BODY_BYTES = 64 * 1024
_FALLBACK_SCAN_BYTES = 16 * 1024
_SHORT_FIELD_RES = {
    field: re.compile(rf'"{field}"\s*:\s*"([A-Za-z0-9_.:-]{{1,64}})"')
    for field in ("code", "type", "param")
}
_MESSAGE_PREFIX_RE = re.compile(r'"message"\s*:\s*"((?:\\.|[^"\\]){1,2000})')
_SENTENCE_SPLIT_RE = re.compile(r"[.;\r\n]+")

_IMAGE_WORDS = ("image", "vision", "multimodal", "picture")
_REFUSAL_WORDS = (
    "not support",
    "unsupported",
    "unknown variant",
    "only supported",
    "does not accept",
    "not allowed",
    "expected `text`",
    "expected text",
    "not available",
    "invalid content",
    "cannot process",
)
_IMAGE_ERROR_CODES = frozenset({
    "image_not_supported",
    "invalid_image",
    "invalid_image_format",
    "invalid_image_url",
    "image_parse_error",
    "image_too_large",
    "unsupported_content_type",
    "unsupported_image_media_type",
})
_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context_window_exceeded",
    "context_exceeded",
    "maximum context length",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "context window",
)
_QUOTA_CODES = frozenset({
    "insufficient_quota",
    "billing_hard_limit_reached",
    "credit_balance_too_low",
})
_TRANSIENT_PREFIXES = ("rate_limit", "overloaded")
_TRANSIENT_CODES = frozenset({
    "server_error",
    "api_error",
    "internal_error",
    "service_unavailable",
    "bad_gateway",
    "gateway_timeout",
    "too_many_requests",
    "rate_limited",
    "generation_timeout",
    "generation_failed",
    "one_api_error",
    "new_api_error",
    "rix_api_error",
    "upstream_error",
})
_CLIENT_REJECTION_PREFIXES = (
    "invalid_request",
    "bad_request",
    "validation",
    "unsupported_",
    "unknown_",
    "unrecognized_",
    "invalid_",
    "missing_",
    "not_found",
    "model_not_",
    "content_policy",
)
_CLIENT_REJECTION_CODES = frozenset({
    "unsupported",
    "permission_error",
    "authentication_error",
    "unauthorized",
    "forbidden",
    "value_error",
    "prompt_blocked",
})


def _extract_envelope_fields(envelope):
    if not isinstance(envelope, dict):
        return "", "", "", ""
    err = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    code = err.get("code") if isinstance(err.get("code"), str) else (envelope.get("code") if isinstance(envelope.get("code"), str) else "")
    err_type = err.get("type") if isinstance(err.get("type"), str) else ""
    if not err_type and isinstance(envelope.get("type"), str) and envelope.get("type") != "error":
        err_type = envelope["type"]
    param = err.get("param") if isinstance(err.get("param"), str) else (envelope.get("param") if isinstance(envelope.get("param"), str) else "")
    messages = []
    for container in (err, envelope):
        for key in ("message", "detail", "error"):
            val = container.get(key)
            if isinstance(val, str) and val:
                messages.append(val[:2000])
    return code[:128], err_type[:128], param[:256], "\n".join(messages)[:2000]


def _is_transient_token(token: str) -> bool:
    low = token.lower()
    return bool(low) and (low in _TRANSIENT_CODES or low.startswith(_TRANSIENT_PREFIXES))


def _is_client_rejection_token(token: str) -> bool:
    low = token.lower()
    return bool(low) and (low in _CLIENT_REJECTION_CODES or low.startswith(_CLIENT_REJECTION_PREFIXES))


def context_overflow(envelope, status=None) -> bool:
    """True when a 4xx/stream error envelope indicates context length overflow. Keeps no text."""
    if status == 413:
        return True
    if not isinstance(envelope, dict):
        return False
    code, err_type, _param, msg = _extract_envelope_fields(envelope)
    combined = f"{code} {err_type} {msg}".lower()
    return any(marker in combined for marker in _CONTEXT_OVERFLOW_MARKERS)


def image_rejection(envelope, status=None):
    """True when a 4xx error body says the model refused image input. Keeps no text."""
    if status == 404 or not isinstance(envelope, dict):
        return False
    code, err_type, param, _msg = _extract_envelope_fields(envelope)
    code_l, type_l, param_l = code.lower(), err_type.lower(), param.lower()
    if code_l in {"model_not_found", "model_not_available"} or type_l in {"model_not_found", "model_not_available", "not_found_error"}:
        return False
    if "image_url" in param_l or "input_image" in param_l or ("content[" in param_l and "].image" in param_l):
        return True
    if code_l in _IMAGE_ERROR_CODES:
        return True
    err = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    for container in (err, envelope):
        for key in ("message", "detail", "error"):
            value = container.get(key)
            if isinstance(value, str) and value:
                for sentence in _SENTENCE_SPLIT_RE.split(value[:2000].lower()):
                    if any(word in sentence for word in _IMAGE_WORDS) and any(word in sentence for word in _REFUSAL_WORDS):
                        return True
    return False


def describe(exc, phase="request", envelope=None):
    if isinstance(exc, HTTPError):
        result = {"network_reason": "http", "network_phase": phase, "http_status": exc.code}
        code, err_type, param, msg = _extract_envelope_fields(envelope)
        code_l, type_l, msg_l = code.lower(), err_type.lower(), msg.lower()
        tokens = [t for t in (code, err_type) if t]
        tokens_l = [t.lower() for t in tokens]

        if (
            exc.code == 529
            or any(t in {"overloaded_error", "overloaded"} for t in tokens_l)
            or (exc.code == 503 and "overloaded" in f"{code_l} {type_l} {msg_l}")
        ):
            result["overloaded"] = True

        if 400 <= exc.code < 500 and image_rejection(envelope, status=exc.code):
            # A closed flag only; the response body itself is never retained.
            result["image_rejected"] = True

        if any(t in _QUOTA_CODES for t in tokens):
            result["network_reason"] = "quota"
        elif not result.get("image_rejected") and 400 <= exc.code < 500 and context_overflow(envelope, status=exc.code):
            result["network_reason"] = "context_overflow"
        elif exc.code in {401, 403}:
            result["network_reason"] = "auth_rejected"
        elif 400 <= exc.code < 500 and not result.get("image_rejected") and not result.get("overloaded"):
            if not any(_is_transient_token(t) for t in tokens_l):
                if any(_is_client_rejection_token(t) for t in tokens_l) or bool(param):
                    result["network_reason"] = "request_rejected"
                elif exc.code == 404:
                    result["network_reason"] = "request_rejected"
                elif exc.code == 422 and isinstance(envelope, dict) and any(k in envelope for k in ("detail", "error", "code", "type")):
                    result["network_reason"] = "request_rejected"

        value = (exc.headers or {}).get("Retry-After")
        if value:
            try:
                seconds = float(value)
            except (ValueError, TypeError):
                try:
                    seconds = parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    seconds = -1
            if math.isfinite(seconds) and seconds >= 0:
                # Values beyond a day always exceed the runtime's time budget.
                result["retry_after_seconds"] = min(seconds, 86400)
        return result
    reason = getattr(exc, "reason", None)
    cause = reason if isinstance(reason, BaseException) else exc
    number = getattr(cause, "winerror", None) or getattr(cause, "errno", None)
    if isinstance(cause, ssl.SSLCertVerificationError):
        kind = "tls_certificate"
    elif isinstance(cause, ssl.SSLError):
        kind = "tls"
    elif isinstance(cause, socket.gaierror):
        kind = "dns_temporary" if number == socket.EAI_AGAIN else "dns_not_found"
    elif isinstance(cause, TimeoutError):
        kind = "timeout"
    elif number in {errno.ECONNRESET, errno.ECONNABORTED, 10054, 10053}:
        kind = "reset"
    elif number in {errno.ECONNREFUSED, 10061}:
        kind = "refused"
    else:
        kind = "connection"
    return {"network_reason": kind, "network_phase": phase}


def classify_stream_error(event, phase="read"):
    """Classify an in-stream error object without retaining any upstream text."""
    diagnostics = {"network_phase": phase}
    target = event
    if isinstance(target, dict) and isinstance(target.get("response"), dict):
        resp = target["response"]
        target = resp.get("error") if resp.get("error") is not None else resp
    envelope = target if (isinstance(target, dict) and isinstance(target.get("error"), dict)) else ({"error": target} if isinstance(target, dict) else {})
    code, err_type, param, msg = _extract_envelope_fields(envelope)
    code_l, type_l, msg_l = code.lower(), err_type.lower(), msg.lower()
    tokens = [t for t in (code, err_type) if t]
    tokens_l = [t.lower() for t in tokens]

    if any(t in _QUOTA_CODES for t in tokens):
        diagnostics["network_reason"] = "quota"
        return "upstream_request_rejected", diagnostics
    if any(t in {"context_length_exceeded", "context_window_exceeded", "context_exceeded"} for t in tokens_l) or context_overflow(envelope):
        diagnostics["network_reason"] = "context_overflow"
        return "upstream_request_rejected", diagnostics
    if code_l == "generation_cancelled":
        return "inference_failed", diagnostics
    if any(t in {"overloaded_error", "overloaded"} for t in tokens_l) or (
        any(_is_transient_token(t) for t in tokens_l) and "overloaded" in msg_l
    ):
        diagnostics["network_reason"] = "http"
        diagnostics["overloaded"] = True
        return "upstream_stream_error", diagnostics
    if any(_is_transient_token(t) for t in tokens_l):
        diagnostics["network_reason"] = "http"
        return "upstream_stream_error", diagnostics
    if any(t in {"authentication_error", "unauthorized", "permission_error", "forbidden"} for t in tokens_l):
        diagnostics["network_reason"] = "auth_rejected"
        return "upstream_request_rejected", diagnostics
    if any(_is_client_rejection_token(t) for t in tokens_l) or bool(param):
        diagnostics["network_reason"] = "request_rejected"
        return "upstream_request_rejected", diagnostics
    return "inference_failed", diagnostics


def _fallback_minimal_envelope(raw: bytes):
    head = raw[:_FALLBACK_SCAN_BYTES].decode("utf-8", errors="replace")
    extracted = {}
    for field, pattern in _SHORT_FIELD_RES.items():
        matches = pattern.findall(head)
        if matches:
            chosen = next((m for m in matches if not (field == "type" and m == "error")), matches[0])
            extracted[field] = chosen
    msg_match = _MESSAGE_PREFIX_RE.search(head)
    if msg_match:
        extracted["message"] = msg_match.group(1)[:2000]
    return {"error": extracted} if extracted else None


def read_http_failure(exc, phase="request", *, detail_codes=None):
    """Read a bounded 64 KiB envelope (with 16 KiB regex fallback) to extract closed error flags."""
    envelope = None
    matched_detail = None
    try:
        raw = exc.read(_MAX_ERROR_BODY_BYTES + 1)
        if raw:
            if len(raw) <= _MAX_ERROR_BODY_BYTES:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        envelope = parsed
                        if detail_codes is not None:
                            detail = parsed.get("detail")
                            if isinstance(detail, str) and detail in detail_codes:
                                matched_detail = detail
                except (ValueError, TypeError):
                    envelope = _fallback_minimal_envelope(raw)
            else:
                envelope = _fallback_minimal_envelope(raw)
    except OSError:
        envelope = None
    diagnostics = describe(exc, phase, envelope)
    if detail_codes is not None:
        return diagnostics, matched_detail
    return diagnostics
