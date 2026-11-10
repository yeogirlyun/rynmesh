"""Bounded peer ciphertext transport without private mesh credentials."""
from __future__ import annotations

import http.client
import json
from contextlib import contextmanager
from urllib.parse import urlsplit

from ..transport import TransportError


@contextmanager
def response(url, body, *, timeout_s):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.fragment:
        raise TransportError("peer request failed", reason="http_error")
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = cls(p.hostname, p.port, timeout=timeout_s)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    try:
        # No redirects, proxies, ambient credentials, or mesh-key headers.
        conn.request("POST", p.path + ("?" + p.query if p.query else ""), body=data,
                     headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream, application/x-ndjson"})
        result = conn.getresponse()
        if not 200 <= result.status < 300:
            raise TransportError("peer request failed", reason="http_error")
        yield result
    except (OSError, http.client.HTTPException):
        raise TransportError("peer request failed", reason="http_error") from None
    finally:
        conn.close()


class ExternalPeerTransport:
    def post_bytes(self, url, body, *, timeout_s, max_bytes, headers=None):
        with response(url, body, timeout_s=timeout_s) as result:
            data = result.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise TransportError("peer response too large", reason="too_large")
        return data

    def iter_post_bytes(self, url, body, *, timeout_s, max_chunk_bytes, max_total_bytes, headers=None):
        with response(url, body, timeout_s=timeout_s) as result:
            total = 0
            while True:
                data = result.read1(max_chunk_bytes)
                if not data:
                    return
                total += len(data)
                if total > max_total_bytes:
                    raise TransportError("peer response too large", reason="too_large")
                yield data
