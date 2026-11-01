"""Closed, body-free transport diagnostics. Never retain exception messages or URLs."""
import errno
import json
import math
import socket
import ssl
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError

REASONS = {"dns_temporary", "dns_not_found", "tls_certificate", "tls", "reset", "refused", "timeout", "http", "connection", "quota"}


def describe(exc, phase="request", envelope=None):
    if isinstance(exc, HTTPError):
        result = {"network_reason": "http", "network_phase": phase, "http_status": exc.code}
        detail = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(detail, dict):
            code = detail.get("code") or detail.get("type")
            if isinstance(code, str) and code in {"insufficient_quota", "billing_hard_limit_reached", "credit_balance_too_low"}:
                result["network_reason"] = "quota"
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


def read_http_failure(exc, phase="request"):
    """Read a bounded envelope solely to recognize closed quota codes."""
    envelope = None
    try:
        raw = exc.read(16 * 1024 + 1)
        if len(raw) <= 16 * 1024:
            envelope = json.loads(raw)
    except (OSError, ValueError, TypeError):
        pass
    return describe(exc, phase, envelope)
