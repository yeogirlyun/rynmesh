"""Direct peer HTTP with separate connection and inference time budgets.

Peer traffic must not follow redirects or a machine's HTTP proxy: an advertised
LAN endpoint may be stale or belong to a different network after a move.
"""

from __future__ import annotations

import http.client
import json
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from rynmesh.transport import network_key_header


@contextmanager
def peer_response(url: str, body: dict[str, Any], *, timeout_s: float):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("invalid peer HTTP endpoint")
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=min(3.0, timeout_s))
    try:
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(timeout_s)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(
            "POST",
            path,
            json.dumps(body).encode(),
            {"Content-Type": "application/json", **network_key_header()},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"peer HTTP status {response.status}")
        yield response
    finally:
        connection.close()
