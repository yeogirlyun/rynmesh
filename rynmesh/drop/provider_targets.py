"""Source-scoped conversation identifiers; never transport addresses or trust."""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit

PREFIX = "provider:"


def provider_target(resource: str, peer_id: str) -> str:
    value = json.dumps([resource, peer_id], separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def parse_provider_target(value: str) -> tuple[str, str] | None:
    if not value.startswith(PREFIX):
        return None
    try:
        if len(value) > 4096:
            raise ValueError
        raw = value[len(PREFIX):]
        resource, peer_id = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        if not isinstance(resource, str) or not isinstance(peer_id, str):
            raise ValueError
        url = urlsplit(resource)
        key = base64.b64decode(peer_id, validate=True)
        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
                or len(key) != 32 or base64.b64encode(key).decode("ascii") != peer_id
                or provider_target(resource, peer_id) != value):
            raise ValueError
        return resource, peer_id
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("invalid_provider_target") from None
