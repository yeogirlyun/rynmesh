from __future__ import annotations

import gzip
import http.server
import threading

import pytest

from rynmesh.peer_http import HttpPeerClient, PeerTransportError
from rynmesh.transport import (
    _BROWSER_HEADERS,
    FrontedHttpsTransport,
    StdlibHttpsTransport,
    TransportError,
    TransportProfile,
    resolve_profile,
)


def test_browser_profiles_advertise_identity_encoding_only(monkeypatch) -> None:
    assert _BROWSER_HEADERS.get("Accept-Encoding") == "identity"
    for profile_name in ("camouflage", "hardened", "cdn-ws", "reality", "meek", "ech"):
        monkeypatch.setenv("RYNMESH_TRANSPORT", profile_name)
        profile = resolve_profile()
        assert profile.headers.get("Accept-Encoding") == "identity"
        assert "gzip" not in profile.headers.get("Accept-Encoding", "").lower()


def test_stdlib_and_fronted_transports_send_identity_and_reject_gzip_responses(
    tmp_path, monkeypatch
) -> None:
    observed_accept_encodings: list[str] = []
    compressed_json = gzip.compress(b'{"ok":true}\n')

    class EncodingHandler(http.server.BaseHTTPRequestHandler):
        def _reply(self) -> None:
            observed_accept_encodings.append(self.headers.get("Accept-Encoding", ""))
            if self.path.startswith("/gzip") or self.path == "/api/v1/node":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed_json)))
                self.end_headers()
                self.wfile.write(compressed_json)
                return
            plain = b'{"ok":true}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Encoding", "identity")
            self.send_header("Content-Length", str(len(plain)))
            self.end_headers()
            self.wfile.write(plain)

        def do_GET(self) -> None:  # noqa: N802
            self._reply()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self._reply()

        def log_message(self, *args) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), EncodingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.delenv("RYNMESH_TRANSPORT", raising=False)
        profile = resolve_profile()
        stdlib_tx = StdlibHttpsTransport(profile)
        fronted_tx = FrontedHttpsTransport(
            TransportProfile(
                name="camouflage",
                headers=dict(profile.headers),
                connect_host="127.0.0.1",
            )
        )
        base_url = f"http://127.0.0.1:{port}"

        # Identity responses succeed and outbound requests declare Accept-Encoding: identity.
        assert stdlib_tx.get_bytes(f"{base_url}/identity", timeout_s=5, max_bytes=1024) == b'{"ok":true}\n'
        assert list(
            stdlib_tx.iter_post_bytes(
                f"{base_url}/identity",
                b"{}",
                timeout_s=5,
                max_chunk_bytes=256,
                max_total_bytes=1024,
            )
        ) == [b'{"ok":true}\n']
        assert fronted_tx.get_bytes(f"{base_url}/identity", timeout_s=5, max_bytes=1024) == b'{"ok":true}\n'
        assert observed_accept_encodings
        assert all(val == "identity" for val in observed_accept_encodings)

        # Gzip responses fail with TransportError(reason="unsupported_encoding") across all read paths.
        for index, tx in enumerate((stdlib_tx, fronted_tx)):
            with pytest.raises(TransportError) as get_err:
                tx.get_bytes(f"{base_url}/gzip", timeout_s=5, max_bytes=4096)
            assert get_err.value.reason == "unsupported_encoding"

            with pytest.raises(TransportError) as post_err:
                tx.post_bytes(f"{base_url}/gzip", b"{}", timeout_s=5, max_bytes=4096)
            assert post_err.value.reason == "unsupported_encoding"

            with pytest.raises(TransportError) as stream_err:
                list(
                    tx.iter_post_bytes(
                        f"{base_url}/gzip",
                        b"{}",
                        timeout_s=5,
                        max_chunk_bytes=256,
                        max_total_bytes=4096,
                    )
                )
            assert stream_err.value.reason == "unsupported_encoding"

            dest = tmp_path / f"dl-{index}.bin"
            with pytest.raises(TransportError) as dl_err:
                tx.download(f"{base_url}/gzip", dest, timeout_s=5, max_bytes=4096)
            assert dl_err.value.reason == "unsupported_encoding"
            assert not dest.exists()

        # HttpPeerClient maps unsupported_encoding to an explicit peer error rather than JSON/UTF-8 decode failure.
        client = HttpPeerClient(
            f"http://peer.example:{port}",
            timeout_s=5,
            transport=fronted_tx,
        )
        with pytest.raises(PeerTransportError, match="peer_unsupported_encoding"):
            client.node_info()
        with pytest.raises(PeerTransportError, match="peer_unsupported_encoding"):
            client.post_json("/gzip", {"hello": "world"})
        with pytest.raises(PeerTransportError, match="peer_unsupported_encoding"):
            list(client.iter_post_ndjson("/gzip", {"hello": "world"}))
        with pytest.raises(PeerTransportError, match="peer_unsupported_encoding"):
            client._download("/gzip", tmp_path / "peer-dl.bin")
    finally:
        server.shutdown()
        thread.join(timeout=2)
