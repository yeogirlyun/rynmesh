from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from rynmesh.transport import CdnWebSocketTransport, TransportError, TransportProfile

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()).decode("ascii")


def _encode_server_frame(payload: bytes, *, opcode: int = 0x02, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes([first, length])
    elif length < 65536:
        header = bytes([first, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first, 127]) + length.to_bytes(8, "big")
    return header + payload


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError("unexpected EOF")
        buf.extend(chunk)
    return bytes(buf)


def _read_client_handshake(conn: socket.socket) -> dict[str, str]:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    lines = bytes(buf).split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def _read_masked_client_frame(conn: socket.socket) -> tuple[int, bool, bytes]:
    hdr = _recv_exact(conn, 2)
    fin = bool(hdr[0] & 0x80)
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    assert masked is True
    length = hdr[1] & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(conn, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(conn, 8), "big")
    mask = _recv_exact(conn, 4)
    raw = _recv_exact(conn, length)
    unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
    return opcode, fin, unmasked


def _serve_single_connection(handler):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    errors: list[BaseException] = []

    def run() -> None:
        try:
            conn, _ = listener.accept()
            conn.settimeout(2.0)
            with conn:
                handler(conn)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread, errors


def test_ws_connect_rejects_bad_accept_and_non_http11_101(monkeypatch) -> None:
    ok_frame = _encode_server_frame(
        b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}',
        opcode=0x02,
        fin=True,
    )

    def bad_accept_server(conn: socket.socket) -> None:
        _read_client_handshake(conn)
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: invalid-accept-value\r\n\r\n" + ok_frame
        )

    port, thread, _errors = _serve_single_connection(bad_accept_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port}/ryn-ws")
    tx = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    with pytest.raises(TransportError) as bad_accept_err:
        sock = tx._ws_connect(5)
        sock.close()
    thread.join(timeout=2)
    assert bad_accept_err.value.reason == "http_error"

    def http10_server(conn: socket.socket) -> None:
        hdrs = _read_client_handshake(conn)
        accept = _ws_accept(hdrs["sec-websocket-key"])
        conn.sendall(
            f"HTTP/1.0 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
            + ok_frame
        )

    port2, thread2, _errors2 = _serve_single_connection(http10_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port2}/ryn-ws")
    tx2 = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    with pytest.raises(TransportError) as http10_err:
        sock2 = tx2._ws_connect(5)
        sock2.close()
    thread2.join(timeout=2)
    assert http10_err.value.reason == "http_error"


def test_ws_handshake_failure_formats_status_and_never_leaks_raw_bytes(monkeypatch) -> None:
    def http_403_server(conn: socket.socket) -> None:
        _read_client_handshake(conn)
        conn.sendall(
            b"HTTP/1.1 403 Forbidden\r\n"
            b"X-Debug-Secret: LEAK_HEADER_MARKER\r\n\r\n"
            b"LEAK_BODY_MARKER"
        )

    port, thread, errors = _serve_single_connection(http_403_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port}/ryn-ws")
    tx = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    with pytest.raises(TransportError) as err_403:
        tx.get_bytes(f"http://peer.internal:{port}/health", timeout_s=5, max_bytes=4096)
    thread.join(timeout=2)
    assert not errors
    assert err_403.value.reason == "http_error"
    assert str(err_403.value) == "ws handshake failed: status 403"
    assert "LEAK_" not in str(err_403.value)

    raw_binary = b"\x89\xff\x00\xfeRAW_BINARY_SECRET_991\r\n\r\n\xde\xad\xbe\xef"

    def binary_garbage_server(conn: socket.socket) -> None:
        _read_client_handshake(conn)
        conn.sendall(raw_binary)

    port2, thread2, errors2 = _serve_single_connection(binary_garbage_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port2}/ryn-ws")
    tx2 = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    with pytest.raises(TransportError) as err_bin:
        tx2.get_bytes(f"http://peer.internal:{port2}/health", timeout_s=5, max_bytes=4096)
    thread2.join(timeout=2)
    assert not errors2
    assert err_bin.value.reason == "http_error"
    assert str(err_bin.value) == "ws handshake failed: status unknown"
    assert "RAW_BINARY_SECRET" not in str(err_bin.value)
    assert "\\x89" not in str(err_bin.value)


def test_ws_recv_frame_handles_ping_pong_and_fragmentation_with_max_bytes(monkeypatch) -> None:
    received_pongs: list[tuple[int, bool, bytes]] = []

    def fragmented_and_ping_server(conn: socket.socket) -> None:
        hdrs = _read_client_handshake(conn)
        accept = _ws_accept(hdrs["sec-websocket-key"])
        conn.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
        )
        req_opcode, req_fin, _req_bytes = _read_masked_client_frame(conn)
        assert (req_opcode, req_fin) == (0x02, True)

        body = b'{"fragmented":"reassembled-ok"}'
        full_http = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
        )
        part1 = full_http[:20]
        part2 = full_http[20:55]
        part3 = full_http[55:]

        conn.sendall(_encode_server_frame(part1, opcode=0x02, fin=False))
        conn.sendall(_encode_server_frame(b"unsolicited-pong", opcode=0x0A, fin=True))
        conn.sendall(_encode_server_frame(b"ping-payload-77", opcode=0x09, fin=True))
        # Read the pong reply from client before sending remaining fragments.
        received_pongs.append(_read_masked_client_frame(conn))
        conn.sendall(_encode_server_frame(part2, opcode=0x00, fin=False))
        conn.sendall(_encode_server_frame(part3, opcode=0x00, fin=True))

    port, thread, errors = _serve_single_connection(fragmented_and_ping_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port}/ryn-ws")
    tx = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    result = tx.get_bytes(f"http://peer.internal:{port}/frag", timeout_s=5, max_bytes=4096)
    thread.join(timeout=2)
    assert not errors
    assert result == b'{"fragmented":"reassembled-ok"}'
    assert received_pongs == [(0x0A, True, b"ping-payload-77")]

    # Fragmented total exceeding max_bytes must raise TransportError(reason="too_large").
    s_client, s_server = socket.socketpair()
    try:
        s_server.sendall(_encode_server_frame(b"a" * 40, opcode=0x02, fin=False))
        s_server.sendall(_encode_server_frame(b"b" * 40, opcode=0x00, fin=True))
        with pytest.raises(TransportError) as limit_err:
            CdnWebSocketTransport._ws_recv_frame(s_client, max_bytes=60)
        assert limit_err.value.reason == "too_large"
    finally:
        s_client.close()
        s_server.close()


def test_cdn_ws_download_streams_to_tempfile_without_buffering_whole_body_in_memory(
    tmp_path: Path, monkeypatch
) -> None:
    total_size = 16 * 1024 * 1024  # 16 MiB
    chunk_unit = (b"0123456789abcdef" * 4096)  # 64 KiB
    repeats = total_size // len(chunk_unit)
    expected_digest = hashlib.sha256(chunk_unit * repeats).hexdigest()

    def large_download_server(conn: socket.socket) -> None:
        hdrs = _read_client_handshake(conn)
        accept = _ws_accept(hdrs["sec-websocket-key"])
        conn.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
        )
        _read_masked_client_frame(conn)
        http_head = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"Content-Length: {total_size}\r\n\r\n"
        ).encode("ascii")
        # Send as one large 16 MiB+ binary WebSocket frame so even single-frame
        # streaming cannot cheat by calling recv_exact(frame_length).
        frame_len = len(http_head) + total_size
        conn.sendall(bytes([0x82, 127]) + frame_len.to_bytes(8, "big") + http_head)
        for _ in range(repeats):
            conn.sendall(chunk_unit)

    port, thread, errors = _serve_single_connection(large_download_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port}/ryn-ws")
    tx = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    dest = tmp_path / "streamed-artifact.bin"

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        out = tx.download(
            f"http://peer.internal:{port}/content/bytes",
            dest,
            timeout_s=15,
            max_bytes=total_size + 1024,
        )
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    thread.join(timeout=5)
    assert not errors
    assert out == dest
    assert dest.stat().st_size == total_size
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == expected_digest
    # Peak Python memory during 16 MiB download must stay well below total_size (< 4 MiB).
    assert peak_bytes < 4 * 1024 * 1024
    assert list(tmp_path.iterdir()) == [dest]

    # Exceeding max_bytes removes the temp file and never leaves dest behind.
    def oversized_download_server(conn: socket.socket) -> None:
        hdrs = _read_client_handshake(conn)
        accept = _ws_accept(hdrs["sec-websocket-key"])
        conn.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
        )
        _read_masked_client_frame(conn)
        body = b"x" * 2048
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 2048\r\n\r\n" + body
        conn.sendall(_encode_server_frame(resp, opcode=0x02, fin=True))

    port2, thread2, errors2 = _serve_single_connection(oversized_download_server)
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port2}/ryn-ws")
    tx2 = CdnWebSocketTransport(TransportProfile(name="cdn-ws"))
    too_big_dest = tmp_path / "too-big.bin"
    with pytest.raises(TransportError) as too_big_err:
        tx2.download(
            f"http://peer.internal:{port2}/content/bytes",
            too_big_dest,
            timeout_s=5,
            max_bytes=1024,
        )
    thread2.join(timeout=2)
    assert not errors2
    assert too_big_err.value.reason == "too_large"
    assert not too_big_dest.exists()
    assert list(tmp_path.iterdir()) == [dest]


def test_ws_send_frame_fast_mask_matches_bytewise_xor_and_is_much_faster(monkeypatch) -> None:
    fixed_mask = b"\xa5\x5a\xf0\x0f"
    monkeypatch.setattr("os.urandom", lambda n: fixed_mask[:n])

    class SinkSocket:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, data: bytes) -> None:
            self.sent = data

    def legacy_bytewise_mask(payload: bytes, mask: bytes) -> bytes:
        return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    for length in (0, 1, 2, 3, 4, 5, 7, 125, 126, 513, 65537):
        payload = bytes((i * 37 + 13) % 256 for i in range(length))
        sink = SinkSocket()
        CdnWebSocketTransport._ws_send_frame(sink, payload)
        if length < 126:
            hdr_len = 2 + 4
        elif length < 65536:
            hdr_len = 4 + 4
        else:
            hdr_len = 10 + 4
        assert sink.sent[hdr_len - 4 : hdr_len] == fixed_mask
        assert sink.sent[hdr_len:] == legacy_bytewise_mask(payload, fixed_mask)

    one_mib = bytes((i * 17 + 3) % 256 for i in range(1024 * 1024 + 3))  # 1 MiB + 3 (non-multiple of 4)
    legacy_start = time.perf_counter()
    expected_1mib = legacy_bytewise_mask(one_mib, fixed_mask)
    legacy_elapsed = time.perf_counter() - legacy_start

    sink = SinkSocket()
    fast_start = time.perf_counter()
    CdnWebSocketTransport._ws_send_frame(sink, one_mib)
    fast_elapsed = time.perf_counter() - fast_start

    assert sink.sent[14:] == expected_1mib
    assert fast_elapsed < legacy_elapsed * 0.25
