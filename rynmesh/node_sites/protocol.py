"""Wire rules shared by the hosting node and the opening node.

A node site is a loopback web application on one node that selected peers open
inside their own Ryn, like a website whose address is that node. Each HTTP
request travels as one signed envelope over the existing device route (LAN,
direct HTTP or P2P); the request and the response are sealed to the two
devices' messaging keys, so a relay or LAN observer sees neither.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

CAPABILITY = "site.open"
REQUEST_KIND = "site_request"
LIST_KIND = "site_list"
REQUEST_PATH = "/api/peer/site/request"
LIST_PATH = "/api/peer/site/list"
REQUEST_INFO = b"rynmesh-site-request-v1"
RESPONSE_INFO = b"rynmesh-site-response-v1"
LIST_INFO = b"rynmesh-site-list-v1"

MAX_REQUEST_BODY = 25 * 1024 * 1024
MAX_RESPONSE_BODY = 32 * 1024 * 1024
# Base64 plus JSON framing around the largest sealed body.
MAX_REQUEST_WIRE = MAX_REQUEST_BODY * 4 // 3 + 256 * 1024
MAX_LIST_WIRE = 64 * 1024
MAX_PATH = 4096
MAX_HEADER_VALUE = 4096
MAX_SITES = 32
REQUEST_TIMEOUT_S = 30.0

METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
# Only these request headers cross to the hosting node; cookies and any
# caller-supplied identity are dropped (the host derives identity itself).
REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "cache-control", "content-type", "if-match", "if-modified-since",
    "if-none-match", "if-range", "range",
})
RESPONSE_HEADERS = frozenset({
    "accept-ranges", "cache-control", "content-disposition", "content-language", "content-range",
    "content-type", "etag", "expires", "last-modified", "location", "vary",
})

SITE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
SITE_KEY = re.compile(r"[0-9a-f]{32}")
HOST_SUFFIX = ".site.localhost"

# The opened page may talk only to its own origin: no third-party loads, no
# exfiltration of anything on the opening computer, no framing by other pages.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors http://tauri.localhost tauri://localhost http://localhost:* http://127.0.0.1:*"
)


class SiteError(Exception):
    """A refused or failed node-site operation; ``status`` maps to HTTP."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(value: Any, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > limit * 4 // 3 + 8:
        raise SiteError("site_bad_request")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise SiteError("site_bad_request") from None
    if len(raw) > limit:
        raise SiteError("site_too_large", 413)
    return raw


def site_id(value: Any) -> str:
    if not isinstance(value, str) or not SITE_ID.fullmatch(value):
        raise SiteError("site_bad_request")
    return value


def request_path(value: Any) -> str:
    """An origin-relative path with optional query, never a scheme or authority."""
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or len(value) > MAX_PATH or "\\" in value or "#" in value
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise SiteError("site_bad_request")
    return value


def clean_headers(pairs: Any, allowed: frozenset[str]) -> list[list[str]]:
    if not isinstance(pairs, list) or len(pairs) > 64:
        raise SiteError("site_bad_request")
    out: list[list[str]] = []
    for item in pairs:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not all(isinstance(part, str) for part in item)):
            raise SiteError("site_bad_request")
        name, value = item[0].strip().lower(), item[1]
        if name not in allowed:
            continue
        if len(value) > MAX_HEADER_VALUE or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value):
            raise SiteError("site_bad_request")
        out.append([name, value])
    return out


def identity_headers(*, token: str, caller: str, member: dict[str, Any], resource: str) -> dict[str, str]:
    """Headers the site backend may trust, because only Ryn knows ``token``."""
    return {
        "X-Ryn-Site-Token": token,
        "X-Ryn-Caller-Peer": caller,
        "X-Ryn-Caller-Member": quote(str(member.get("member_id") or ""), safe=""),
        "X-Ryn-Caller-Name": quote(str(member.get("member_name") or member.get("name") or ""), safe=""),
        "X-Ryn-Caller-Resource": quote(resource, safe=""),
    }


def host_for(key: str, port: int) -> str:
    return f"{key}{HOST_SUFFIX}:{port}"


def key_from_host(host: str, port: int) -> str | None:
    """The site key of an exact ``<key>.site.localhost:<port>`` Host, else None (DNS-rebinding guard)."""
    value = (host or "").strip().lower()
    suffix = f"{HOST_SUFFIX}:{port}"
    if not value.endswith(suffix):
        return None
    key = value[: -len(suffix)]
    return key if SITE_KEY.fullmatch(key) else None
