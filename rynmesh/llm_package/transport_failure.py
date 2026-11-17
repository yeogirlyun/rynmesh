"""Closed, body-free transport diagnostics. Never retain exception messages or URLs."""
import errno
import json
import math
import re
import socket
import ssl
import time
from email.utils import parsedate_to_datetime
from datetime import datetime
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
    "region_blocked",
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
    "contextwindowexceeded",
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
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "usage_limit_reached",
    "quota_exceeded",
    "Arrearage",
    "insufficient_balance",
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
    if status is not None and not 400 <= status < 500:
        return False
    if status == 413:
        return True
    if not isinstance(envelope, dict):
        return False
    code, err_type, _param, msg = _extract_envelope_fields(envelope)
    combined = f"{code} {err_type} {msg}".lower()
    return any(marker in combined for marker in _CONTEXT_OVERFLOW_MARKERS)


_TOOLS_UNSUPPORTED_MARKERS = (
    "does not support tools",
    "tools is not supported",
    "tool use is not supported",
    "function calling is not supported",
    "does not support function",
)


def tools_rejection(envelope, status=None) -> bool:
    """True when a 4xx error body says the model does not support client tools. Keeps no text."""
    if status in {401, 403, 404} or not isinstance(envelope, dict):
        return False
    code, err_type, param, msg = _extract_envelope_fields(envelope)
    combined = f"{code} {err_type} {param} {msg}".lower()
    return any(marker in combined for marker in _TOOLS_UNSUPPORTED_MARKERS)


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


def _message_retry_after(message):
    match = re.search(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)", message, re.I)
    if match:
        seconds = float(match[1]) / (1000 if match[2].lower() == "ms" else 1)
        return min(seconds, 86400)
    return None


def _header_retry_after(headers, now):
    waits = []
    value = headers.get("retry-after")
    if value:
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - now
            except (ValueError, TypeError, OverflowError):
                seconds = -1
        if math.isfinite(seconds) and seconds >= 0:
            waits.append(seconds)
    for kind in ("requests", "tokens"):
        value = headers.get("x-ratelimit-reset-" + kind, "")
        if re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", value):
            seconds = sum(float(n) * {"h": 3600, "m": 60, "s": 1, "ms": .001}[unit]
                          for n, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", value))
            waits.append(seconds)
        value = headers.get("anthropic-ratelimit-" + kind + "-reset")
        if value:
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    waits.append(max(0, stamp.timestamp() - now))
            except (ValueError, OverflowError):
                pass
    return min(max(waits), 86400) if waits else None


def describe(exc, phase="request", envelope=None):
    if isinstance(exc, HTTPError):
        result = {"network_reason": "http", "network_phase": phase, "http_status": exc.code}
        code, err_type, param, msg = _extract_envelope_fields(envelope)
        code_l, type_l, msg_l = code.lower(), err_type.lower(), msg.lower()
        tokens = [t for t in (code, err_type) if t]
        tokens_l = [t.lower() for t in tokens]
        if exc.code in {400, 422} and (param.lower() in {"stream", "body.stream"}
                or ("stream" in msg_l and any(word in msg_l for word in _REFUSAL_WORDS))):
            result["streaming_unsupported"] = True

        if exc.code == 400 and any(name in (param + " " + msg).lower() for name in
                                   ("max_tokens", "max_completion_tokens", "max_output_tokens")) and any(
                marker in msg_l for marker in ("at most", "exceed", "too large", "less than", "maximum", "<=", "upper bound")):
            result["output_tokens_too_large"] = True

        if (
            exc.code == 529
            or any(t in {"overloaded_error", "overloaded"} for t in tokens_l)
            or (exc.code == 503 and "overloaded" in f"{code_l} {type_l} {msg_l}")
        ):
            result["overloaded"] = True

        if 400 <= exc.code < 500 and image_rejection(envelope, status=exc.code):
            # A closed flag only; the response body itself is never retained.
            result["image_rejected"] = True
        if 400 <= exc.code < 500 and tools_rejection(envelope, status=exc.code):
            result["tools_unsupported"] = True

        headers = {name.lower(): value for name, value in (exc.headers or {}).items()}
        if exc.code == 413:
            result["network_reason"] = "context_overflow"
        elif exc.code == 402 or any(t in _QUOTA_CODES for t in tokens):
            result["network_reason"] = "quota"
        elif 400 <= exc.code < 500 and context_overflow(envelope, status=exc.code):
            result["network_reason"] = "context_overflow"
        elif exc.code == 403 and "blocked" in msg_l and (
                "cloudflare" in headers.get("server", "").lower() or "cloudflare" in msg_l):
            result["network_reason"] = "region_blocked"
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

        now = time.time()
        seconds = _header_retry_after(headers, now)
        if seconds is None and exc.code == 429:
            seconds = _message_retry_after(msg)
        if seconds is not None:
            result["retry_after_seconds"] = seconds
            if result["network_reason"] == "quota":
                result["reset_at"] = int(now + seconds)
        for name in ("x-request-id", "request-id", "cf-ray"):
            value = headers.get(name, "")
            if re.fullmatch(r"[A-Za-z0-9_:\-]+", value):
                result["upstream_request_id"] = value[:128]
                break
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
    elif isinstance(cause, ConnectionResetError) or number in {errno.ECONNRESET, errno.ECONNABORTED, 10054, 10053}:
        kind = "reset"
    elif isinstance(cause, ConnectionRefusedError) or number in {errno.ECONNREFUSED, 10061}:
        kind = "refused"
    else:
        kind = "connection"
    return {"network_reason": kind, "network_phase": phase}


DOWNLOAD_REASON_LABELS = {
    "dns_not_found": "DNS 解析失败",
    "dns_temporary": "DNS 解析失败",
    "reset": "连接被重置",
    "tls_certificate": "证书校验失败",
    "tls": "TLS 连接失败",
    "timeout": "超时",
    "idle_timeout": "超时",
    "total_timeout": "超时",
    "refused": "连接被拒绝",
    "connection": "网络连接失败",
}


def download_retry_delay(exc, default_backoff: float) -> tuple[bool, float]:
    """Return `(should_retry, wait_seconds)` for a download transport failure."""
    if isinstance(exc, HTTPError):
        if exc.code == 429 or 500 <= exc.code < 600:
            info = describe(exc, phase="download")
            wait = min(float(info["retry_after_seconds"]), 60.0) if "retry_after_seconds" in info else default_backoff
            return True, wait
        return False, 0.0
    return True, default_backoff


def format_download_failure(exc, *, filename: str, sha256: str, directory) -> str:
    """Closed Chinese failure reason plus manual placement instructions."""
    info = describe(exc, phase="download")
    if "http_status" in info:
        reason_text = f"HTTP {int(info['http_status'])}"
    else:
        reason_text = DOWNLOAD_REASON_LABELS.get(info.get("network_reason", "connection"), "网络连接失败")
    return f"下载失败：{reason_text}。可手动下载 {filename}（SHA-256: {sha256.lower()}）并放到目录 {directory}"


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

    if any(t.startswith("rate_limit") or t == "too_many_requests" for t in tokens_l):
        seconds = _message_retry_after(msg)
        if seconds is not None:
            diagnostics["retry_after_seconds"] = seconds

    if any(t in _QUOTA_CODES for t in tokens):
        diagnostics["network_reason"] = "quota"
        return "upstream_request_rejected", diagnostics
    if any(t in {"context_length_exceeded", "context_window_exceeded", "context_exceeded"} for t in tokens_l) or context_overflow(envelope):
        diagnostics["network_reason"] = "context_overflow"
        return "upstream_context_overflow", diagnostics
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
    elif "blocked" in head.lower():
        extracted["message"] = "cloudflare blocked" if "cloudflare" in head.lower() else "blocked"
    else:
        seconds = _message_retry_after(head)
        if seconds is not None:
            extracted["message"] = f"try again in {seconds}s"
    return {"error": extracted} if extracted else None


def read_http_failure(exc, phase="request", *, detail_codes=None):
    """Read a bounded 64 KiB envelope (with 16 KiB regex fallback) to extract closed error flags."""
    envelope = None
    raw = b""
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
    if exc.code == 429 and "retry_after_seconds" not in diagnostics and raw:
        seconds = _message_retry_after(raw[:_FALLBACK_SCAN_BYTES].decode("utf-8", errors="replace"))
        if seconds is not None:
            diagnostics["retry_after_seconds"] = seconds
    if detail_codes is not None:
        return diagnostics, matched_detail
    return diagnostics
