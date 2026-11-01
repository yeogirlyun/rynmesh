"""Device drop across the internet: ICE hole punching plus QUIC.

Hole punching and signaling are shared with private AI (``llm_package.p2p``):
a signed registry work order carries the ICE offer, the receiving device
answers with its candidates and the SHA-256 of a per-session QUIC certificate,
and both sides connect on the nominated UDP pair. TURN/relay candidates are
never used, so file bytes only travel between the two devices.

QUIC runs on that pair. It brings congestion control, loss recovery and many
independent streams on one connection, which the datagram framing used for AI
messages does not have. When both devices have the native transport
(``rynmesh.native_transport``: hole punching + quinn in Rust) the offer and
answer carry its info instead and the whole data plane runs natively, about
65x faster on the reference VM; otherwise aioice + aioquic are used as before.
Each QUIC stream carries one peer request
to the other device's ``/api/peer/drop/*`` routes, dispatched in-process, so
every signature, encryption, acceptance and resume rule of the HTTP path
applies unchanged. Nothing outside ``/api/peer/drop/`` is reachable.

Wire format of a stream, both directions::

    u32 header_length || header JSON || body bytes ... FIN

request header ``{"m": method, "p": path?query, "h": [[name, value], ...]}``,
response header ``{"s": status, "h": [[name, value], ...]}``.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import re
import ssl
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Iterator
from urllib.parse import quote, unquote

log = logging.getLogger("rynmesh.drop.p2p")

CAPABILITY = "rynmesh.drop.p2p.v1"
OPERATION = "drop.p2p_offer"
ALPN = "ryn-drop/1"
SCHEME = "ryn-p2p://"
PATH_PREFIX = "/api/peer/drop/"

GATHER_TIMEOUT_S = 10.0
ANSWER_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 20.0
HANDSHAKE_TIMEOUT_S = 10.0
# A QUIC PING on an idle link keeps the NAT mapping open (ICE consent does too)
# and finds a dead path before a send has to.
KEEPALIVE_S = 15.0
QUIC_IDLE_TIMEOUT_S = 60.0
LINK_IDLE_CLOSE_S = 600.0
RETRY_BASE_S = 30.0
RETRY_MAX_S = 600.0
URGENT_MIN_GAP_S = 5.0
MAX_LINKS = 16
MAX_HEADER_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Bytes queued in one QUIC stream before the writer waits for acknowledgements.
STREAM_HIGH_WATER = 8 * 1024 * 1024
# 1350 bytes fits typical PPPoE/VPN paths with IPv4 or IPv6 headers.
MAX_DATAGRAM_SIZE = 1350
# UDP socket buffers on the ICE sockets: absorbs one paced burst without loss
# while staying small enough not to add seconds of queueing delay.
SOCKET_BUFFER_BYTES = 1024 * 1024
# The event loop wakes only every timer tick (15.6 ms on Windows by default);
# a paced sender must be allowed to send one tick's worth per wakeup.
TIMER_TICK_S = 0.016 if os.name == "nt" else 0.004


def p2p_endpoint(peer_id: str) -> str:
    return SCHEME + quote(peer_id, safe="")


def is_p2p_endpoint(endpoint: str) -> bool:
    return str(endpoint or "").startswith(SCHEME)


def peer_of(endpoint: str) -> str:
    return unquote(endpoint[len(SCHEME):])


def _route_for(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "public"
    if address.is_private or address.is_loopback or address.is_link_local:
        return "lan"
    return "public"


def _allowed_target(method: str, target: str) -> bool:
    path = target.partition("?")[0]
    return (method in {"GET", "POST", "PUT"} and path.startswith(PATH_PREFIX)
            and re.fullmatch(r"[A-Za-z0-9/_\-.]+", path) is not None and ".." not in path)


def _frame_head(value: dict[str, Any]) -> bytes:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


async def _read_head(reader: Any) -> dict[str, Any]:
    size = int.from_bytes(await reader.readexactly(4), "big")
    if size <= 0 or size > MAX_HEADER_BYTES:
        raise ConnectionError("invalid p2p stream header")
    value = json.loads((await reader.readexactly(size)).decode("utf-8"))
    if not isinstance(value, dict):
        raise ConnectionError("invalid p2p stream header")
    return value


async def _read_to_end(reader: Any, limit: int) -> bytes:
    out = bytearray()
    while True:
        chunk = await reader.read(256 * 1024)
        if not chunk:
            return bytes(out)
        out += chunk
        if len(out) > limit:
            raise ConnectionError("p2p response too large")


def _self_signed() -> tuple[Any, Any, str]:
    """Per-session certificate; the peer pins its SHA-256 from signed signaling."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ryn-drop")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=2))
            .sign(key, hashes.SHA256()))
    return cert, key, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


class _IceDatagrams(asyncio.DatagramTransport):
    """Lets aioquic send on the nominated ICE pair as if it were a UDP socket."""

    def __init__(self, ice: Any) -> None:
        super().__init__()
        self._ice = ice
        self._queue: list[bytes] = []
        self._ready = asyncio.Event()
        self._closed = False
        self._direct = self._nominated_socket(ice)
        self._pump = asyncio.get_running_loop().create_task(self._run())

    @staticmethod
    def _nominated_socket(ice: Any) -> tuple[Any, Any] | None:
        """(transport, remote address) of the nominated pair, when inspectable.

        Writing there directly saves a coroutine hop per datagram, which is a
        large share of the per-packet cost in Python. ICE consent and STUN keep
        running through aioice on the same socket.
        """
        nominated = getattr(ice, "_nominated", None)
        pair = nominated.get(1) if isinstance(nominated, dict) else None
        transport = getattr(getattr(pair, "protocol", None), "transport", None)
        remote = getattr(pair, "remote_addr", None)
        if transport is None or remote is None or not callable(getattr(transport, "sendto", None)):
            return None
        return transport, remote

    def sendto(self, data: Any, addr: Any = None) -> None:
        if self._closed:
            return
        if self._direct is not None:
            try:
                self._direct[0].sendto(bytes(data), self._direct[1])
                return
            except (OSError, AttributeError):
                self._direct = None
        self._queue.append(bytes(data))
        self._ready.set()

    async def _run(self) -> None:
        try:
            while not self._closed:
                await self._ready.wait()
                self._ready.clear()
                batch, self._queue = self._queue, []
                for data in batch:
                    await self._ice.send(data)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    def close(self) -> None:
        self._closed = True
        self._ready.set()
        self._pump.cancel()

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default

    def abort(self) -> None:
        self.close()


def _make_protocol_class():
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic import events

    class _Quic(QuicConnectionProtocol):
        def __init__(self, quic: Any, on_stream: Callable) -> None:
            super().__init__(quic, stream_handler=on_stream)
            self.handshake_done = asyncio.Event()
            self.terminated = asyncio.Event()
            self.last_received = time.monotonic()

        def datagram_received(self, data: Any, addr: Any) -> None:
            self.last_received = time.monotonic()
            super().datagram_received(data, addr)

        def quic_event_received(self, event: Any) -> None:
            if isinstance(event, events.HandshakeCompleted):
                self.handshake_done.set()
            elif isinstance(event, events.StreamReset):
                # An aborted upload must reach the route as an error, never as
                # a clean end of the body.
                reader = self._stream_readers.get(event.stream_id)
                if reader is not None:
                    reader.set_exception(ConnectionResetError("p2p stream reset"))
                return
            elif isinstance(event, events.ConnectionTerminated):
                self.terminated.set()
                for reader in self._stream_readers.values():
                    if not reader.at_eof():
                        reader.set_exception(ConnectionError("p2p connection closed"))
                return
            super().quic_event_received(event)

    return _Quic


class _Link:
    """One QUIC connection to a device over its ICE pair."""

    def __init__(self, owner: "DropP2P", peer_id: str, ice: Any, protocol: Any, bridge: _IceDatagrams,
                 remote: tuple[str, int], role: str) -> None:
        self.owner = owner
        self.peer_id = peer_id
        self.ice = ice
        self.protocol = protocol
        self.bridge = bridge
        self.remote = remote
        self.route = _route_for(remote[0])
        self.role = role
        self.active = 0
        self.last_used = time.monotonic()
        self.closed = False
        self.tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return not self.closed and not self.protocol.terminated.is_set()

    def describe(self) -> str:
        host = f"[{self.remote[0]}]" if ":" in self.remote[0] else self.remote[0]
        return f"{host}:{self.remote[1]}"

    def unsent(self, stream_id: int) -> int:
        stream = self.protocol._quic._streams.get(stream_id)
        sender = getattr(stream, "sender", None)
        return len(getattr(sender, "_buffer", b"")) if sender is not None else 0

    async def write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        """Queues data on a stream, waiting while too much is unacknowledged.

        aioquic's stream writers never apply backpressure; without this a fast
        disk would buffer an entire file in memory ahead of the network.
        """
        if not self.alive:
            raise ConnectionError("p2p link closed")
        writer.write(data)
        stream_id = writer.get_extra_info("stream_id")
        while self.unsent(stream_id) > STREAM_HIGH_WATER:
            if not self.alive:
                raise ConnectionError("p2p link closed")
            await asyncio.sleep(0.005)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.protocol.close()
            await asyncio.sleep(0.05)  # let CONNECTION_CLOSE leave
        except Exception:
            pass
        for task in self.tasks:
            task.cancel()
        self.bridge.close()
        from ..llm_package.p2p import _close_connection

        await _close_connection(self.ice)
        self.owner._forget(self)


    async def open_pipe(self) -> "_QuicPipe":
        reader, writer = await self.protocol.create_stream()
        return _QuicPipe(self, reader, writer)

    def details(self) -> dict[str, Any]:
        return {"native": False, "congestion": "cubic"}


class _QuicPipe:
    """One aioquic stream: the request/response channel of a peer call."""

    def __init__(self, link: _Link, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.link, self.reader, self.writer = link, reader, writer

    async def write(self, data: bytes) -> None:
        await self.link.write(self.writer, data)

    async def finish(self) -> None:
        self.writer.write_eof()

    async def read(self, size: int) -> bytes:
        return await self.reader.read(size)

    async def readexactly(self, size: int) -> bytes:
        return await self.reader.readexactly(size)

    async def abort(self) -> None:
        try:
            self.link.protocol._quic.reset_stream(self.writer.get_extra_info("stream_id"), 0)
            self.link.protocol.transmit()
        except Exception:
            pass


class _NativeLink:
    """One native QUIC connection (``ryn_transport``) to a device."""

    def __init__(self, owner: "DropP2P", peer_id: str, connection: Any, role: str) -> None:
        self.owner = owner
        self.peer_id = peer_id
        self.connection = connection
        host, _, port = str(connection.remote_addr).rpartition(":")
        self.remote = (host.strip("[]"), int(port or 0))
        self.route = _route_for(self.remote[0])
        self.role = role
        self.active = 0
        self.last_used = time.monotonic()
        self.closed = False
        self.tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return not self.closed and not self.connection.is_closed()

    def describe(self) -> str:
        return str(self.connection.remote_addr)

    async def open_pipe(self) -> "_NativePipe":
        return _NativePipe(await self.connection.open_stream(HANDSHAKE_TIMEOUT_S))

    def details(self) -> dict[str, Any]:
        stats = self.connection.stats()
        return {"native": True, "congestion": stats.get("congestion"), "rtt_ms": round(stats.get("rtt_ms") or 0, 1),
                "lost_packets": stats.get("lost_packets"), "sent_packets": stats.get("sent_packets")}

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.connection.close(0, "closed")
        except Exception:
            pass
        for task in self.tasks:
            task.cancel()
        self.owner._forget(self)


class _NativePipe:
    """One native QUIC stream. Writes wait for flow-control credit natively."""

    def __init__(self, stream: Any) -> None:
        from ..native_transport import StreamReader

        self.stream = stream
        self.reader = StreamReader(stream)

    async def write(self, data: bytes) -> None:
        await self.stream.write(data, 120.0)

    async def finish(self) -> None:
        await self.stream.finish()

    async def read(self, size: int) -> bytes:
        return await self.reader.read(size)

    async def readexactly(self, size: int) -> bytes:
        return await self.reader.readexactly(size)

    async def abort(self) -> None:
        try:
            await self.stream.reset(0)
        except Exception:
            pass


class _Upload:
    def __init__(self, link: Any, pipe: Any) -> None:
        self.link, self.pipe = link, pipe

    async def write(self, data: bytes) -> None:
        if not self.link.alive:
            raise ConnectionError("p2p link closed")
        await self.pipe.write(data)
        self.link.last_used = time.monotonic()

    async def finish(self, timeout_s: float) -> tuple[int, bytes]:
        await self.pipe.finish()
        head = await asyncio.wait_for(_read_head(self.pipe), timeout_s)
        body = await asyncio.wait_for(_read_to_end(self.pipe, MAX_RESPONSE_BYTES), timeout_s)
        return int(head.get("s") or 0), body

    async def release(self) -> None:
        self.link.active -= 1
        self.link.last_used = time.monotonic()

    async def abort(self) -> None:
        await self.pipe.abort()


class DropP2P:
    """Owns the P2P links of one drop engine; its API is synchronous (threads)."""

    def __init__(self, *, store: Any, network_id: Callable[[], str], allowed: Callable[[str], bool]) -> None:
        self.store = store
        self.network_id = network_id
        self.allowed = allowed
        self.app: Any = None
        self.app_loop: asyncio.AbstractEventLoop | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        self._links: dict[str, Any] = {}
        self._extra: set[Any] = set()
        self._dialing: dict[str, asyncio.Future] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._attempt_at: dict[str, float] = {}
        self._handled: OrderedDict[str, None] = OrderedDict()
        self._answering = 0
        self._quic_class: Any = None

    # ---- availability ------------------------------------------------------
    @staticmethod
    def supported(store: Any) -> bool:
        if os.environ.get("RYNMESH_DROP_P2P", "1").strip().lower() in {"0", "false", "off", "no"}:
            return False
        if getattr(store, "registry", None) is None:
            return False
        if not all(callable(getattr(store, name, None)) for name in
                   ("submit_work_order", "poll_work_orders", "publish_work_result", "list_work_results")):
            return False
        try:
            import aioice  # noqa: F401
            import aioquic  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def running(self) -> bool:
        return self._loop is not None and not self._stop.is_set()

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        from ..llm_package.p2p import warm_stun_choice

        self._stop.clear()
        self._quic_class = _make_protocol_class()
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def serve() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.create_task(self._maintain())
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._thread = threading.Thread(target=serve, name="drop-p2p", daemon=True)
        self._thread.start()
        ready.wait(5)
        self._loop = loop
        self._poller = threading.Thread(target=self._poll_offers, name="drop-p2p-signal", daemon=True)
        self._poller.start()
        warm_stun_choice()

    def stop(self) -> None:
        if self._loop is None:
            return
        self._stop.set()
        loop, self._loop = self._loop, None

        async def shutdown() -> None:
            links = [*self._links.values(), *self._extra]
            await asyncio.gather(*(link.close() for link in links), return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self, coro: Any, timeout_s: float) -> Any:
        loop = self._loop
        if loop is None:
            coro.close()
            raise ConnectionError("p2p is not running")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("p2p operation timed out") from exc

    # ---- sync API (engine threads) ----------------------------------------
    def link_info(self, peer_id: str) -> dict[str, Any] | None:
        link = self._links.get(peer_id)
        if link is None or not link.alive:
            return None
        return {"route": link.route, "address": link.describe(), "role": link.role, **link.details()}

    def connect(self, peer_id: str, *, urgent: bool = False) -> dict[str, Any]:
        """Returns a live link, dialing through the registry when there is none."""
        self._run(self._ensure(peer_id, urgent), GATHER_TIMEOUT_S + ANSWER_TIMEOUT_S + CONNECT_TIMEOUT_S
                  + HANDSHAKE_TIMEOUT_S + 5)
        info = self.link_info(peer_id)
        if info is None:
            raise ConnectionError("p2p link closed")
        return info

    def request(self, peer_id: str, method: str, target: str, *, body: bytes | None = None,
                headers: dict[str, str] | None = None, timeout_s: float = 15.0) -> tuple[int, bytes]:
        return self._run(self._request(peer_id, method, target, body, headers or {}, timeout_s), timeout_s + 5)

    def stream_put(self, peer_id: str, target: str, chunks: Iterator[bytes], *,
                   headers: dict[str, str] | None = None, timeout_s: float = 60.0) -> tuple[int, bytes]:
        upload = self._run(self._open(peer_id, "PUT", target, headers or {}), timeout_s)
        try:
            for chunk in chunks:
                self._run(upload.write(chunk), timeout_s)
            return self._run(upload.finish(timeout_s), timeout_s + 5)
        except BaseException:
            try:
                self._run(upload.abort(), 5)
            except Exception:
                pass
            raise
        finally:
            try:
                self._run(upload.release(), 5)
            except Exception:
                pass

    # ---- requests ----------------------------------------------------------
    async def _open(self, peer_id: str, method: str, target: str, headers: dict[str, str]) -> _Upload:
        link = self._links.get(peer_id)
        if link is None or not link.alive:
            raise ConnectionError("no p2p link")
        link.active += 1
        try:
            pipe = await link.open_pipe()
            await pipe.write(_frame_head({"m": method, "p": target, "h": list(headers.items())}))
        except BaseException:
            link.active -= 1
            raise
        link.last_used = time.monotonic()
        return _Upload(link, pipe)

    async def _request(self, peer_id: str, method: str, target: str, body: bytes | None,
                       headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
        upload = await self._open(peer_id, method, target, headers)
        try:
            if body:
                await upload.write(body)
            return await upload.finish(timeout_s)
        finally:
            await upload.release()

    # ---- serving the other device's requests -------------------------------
    def _on_stream(self, link: _Link) -> Callable:
        def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self._spawn_serve(link, _QuicPipe(link, reader, writer))
        return handler

    def _spawn_serve(self, link: Any, pipe: Any) -> None:
        task = asyncio.get_running_loop().create_task(self._serve(link, pipe))
        link.tasks.append(task)
        task.add_done_callback(lambda done: link.tasks.remove(done) if done in link.tasks else None)

    async def _accept_native(self, link: _NativeLink) -> None:
        """Serves the streams the other device opens (both roles can call)."""
        try:
            while link.alive:
                stream = await link.connection.accept_stream()
                if stream is None:
                    break
                self._spawn_serve(link, _NativePipe(stream))
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("native p2p accept loop stopped", exc_info=True)
        if not link.closed:
            asyncio.get_running_loop().create_task(link.close())

    async def _serve(self, link: Any, pipe: Any) -> None:
        link.active += 1
        try:
            try:
                head = await asyncio.wait_for(_read_head(pipe), 30)
                method, target = str(head.get("m") or ""), str(head.get("p") or "")
                headers = [(str(k), str(v)) for k, v in head.get("h") or []]
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, TypeError, ConnectionError):
                await self._reply(pipe, 400, b'{"detail":"bad request"}')
                return
            if self.app is None or not _allowed_target(method, target):
                await self._reply(pipe, 404, b'{"detail":"Not Found"}')
                return
            await self._dispatch(link, pipe, method, target, headers)
        except Exception:
            log.debug("p2p stream failed", exc_info=True)
        finally:
            link.active -= 1
            link.last_used = time.monotonic()

    async def _reply(self, pipe: Any, status: int, body: bytes) -> None:
        await pipe.write(_frame_head({"s": status, "h": [["content-type", "application/json"]]}) + body)
        await pipe.finish()

    async def _dispatch(self, link: Any, pipe: Any, method: str, target: str,
                        headers: list[tuple[str, str]]) -> None:
        own_loop = asyncio.get_running_loop()
        path, _, query = target.partition("?")
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": method, "scheme": "https", "path": path, "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii", "ignore"), "root_path": "",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers
                        if k.lower() not in {"host", "content-length", "transfer-encoding"}],
            # The real remote ICE address: route and trust checks see the peer,
            # never loopback, so /api/local stays unreachable even by mistake.
            "client": (link.remote[0], int(link.remote[1])), "server": ("ryn-p2p", 0),
        }

        async def on_own(coro: Any) -> Any:
            if asyncio.get_running_loop() is own_loop:
                return await coro
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, own_loop))

        async def run_app() -> None:
            finished = asyncio.Event()
            body_done = False
            started = False

            async def receive() -> dict[str, Any]:
                nonlocal body_done
                if body_done:
                    await finished.wait()
                    return {"type": "http.disconnect"}
                chunk = await on_own(pipe.read(256 * 1024))
                if not chunk:
                    body_done = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.request", "body": chunk, "more_body": True}

            async def send(message: dict[str, Any]) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    kept = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in message.get("headers", [])
                            if k.lower() == b"content-type"]
                    await on_own(pipe.write(_frame_head({"s": int(message["status"]), "h": kept})))
                elif message["type"] == "http.response.body":
                    data = message.get("body", b"")
                    if data:
                        await on_own(pipe.write(data))
                    if not message.get("more_body", False):
                        await on_own(pipe.finish())
                        finished.set()

            try:
                await self.app(scope, receive, send)
            finally:
                finished.set()
                if not started:
                    await on_own(self._reply(pipe, 500, b'{"detail":"internal error"}'))

        app_loop = self.app_loop
        if app_loop is not None and app_loop.is_running() and app_loop is not own_loop:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(run_app(), app_loop))
        else:
            await run_app()

    # ---- dialing (sender side) ---------------------------------------------
    async def _ensure(self, peer_id: str, urgent: bool) -> Any:
        link = self._links.get(peer_id)
        if link is not None and link.alive:
            return link
        pending = self._dialing.get(peer_id)
        if pending is not None:
            return await asyncio.shield(pending)
        now = time.monotonic()
        if now < self._retry_at.get(peer_id, 0.0) and not (
                urgent and now - self._attempt_at.get(peer_id, 0.0) >= URGENT_MIN_GAP_S):
            raise ConnectionError("p2p_backoff")
        if len(self._live_links()) >= MAX_LINKS:
            await self._evict_idle()
        self._attempt_at[peer_id] = now
        future = asyncio.get_running_loop().create_future()
        self._dialing[peer_id] = future
        try:
            link = await self._dial(peer_id)
        except BaseException as exc:
            failures = self._failures.get(peer_id, 0) + 1
            self._failures[peer_id] = failures
            self._retry_at[peer_id] = time.monotonic() + min(RETRY_BASE_S * 2 ** (failures - 1), RETRY_MAX_S)
            future.set_exception(exc if isinstance(exc, Exception) else ConnectionError("p2p dial cancelled"))
            future.exception()
            raise
        finally:
            self._dialing.pop(peer_id, None)
        self._failures.pop(peer_id, None)
        self._retry_at.pop(peer_id, None)
        future.set_result(link)
        return link

    async def _dial(self, peer_id: str) -> Any:
        from .. import native_transport
        from ..llm_package.p2p import (
            _close_connection,
            apply_remote_signal,
            gather_signal,
            new_connection,
        )

        session_id = uuid.uuid4().hex
        ice = new_connection(controlling=True)
        native_session: Any = None
        native_gather = (asyncio.ensure_future(native_transport.open_session("client"))
                         if native_transport.enabled() and native_transport.allowed_for("drop:" + peer_id) else None)
        try:
            offer = await asyncio.wait_for(gather_signal(ice), GATHER_TIMEOUT_S)
            native_info = None
            if native_gather is not None:
                try:
                    native_session = await native_gather
                    native_info = native_transport.offer(native_session)
                except Exception:
                    log.debug("native p2p gathering failed; offering aioice only", exc_info=True)
            order_id = await asyncio.to_thread(self._submit_offer, peer_id, session_id, offer.to_dict(), native_info)
            answer = await self._await_answer(peer_id, order_id, session_id)
            if answer["kind"] == "native" and native_session is not None:
                await _close_connection(ice)
                try:
                    connection = await native_transport.connect(native_session, answer["native"], CONNECT_TIMEOUT_S)
                except BaseException:
                    native_transport.note_failure("drop:" + peer_id)  # next dial offers aioice only
                    raise
                native_transport.note_success("drop:" + peer_id)
                return self._adopt_native(peer_id, connection, "client")
            if native_session is not None:
                native_session.close()
            await apply_remote_signal(ice, answer["ice_signal"])
            await asyncio.wait_for(ice.connect(), CONNECT_TIMEOUT_S)
            return await self._start_quic(peer_id, ice, client=True, fingerprint=answer["fingerprint"])
        except BaseException:
            if native_gather is not None and not native_gather.done():
                native_gather.cancel()
            if native_session is not None:
                native_session.close()
            await _close_connection(ice)
            raise

    def _submit_offer(self, peer_id: str, session_id: str, signal: dict[str, Any],
                      native: dict[str, Any] | None = None) -> str:
        submitted = self.store.submit_work_order(
            provider_peer_id=peer_id, capability=CAPABILITY, operation=OPERATION,
            params={"session_id": session_id, "ice_signal": signal, "alpn": ALPN,
                    **({"native": native} if native else {})},
            network_id=self.network_id(), idempotency_key="drop-p2p:" + session_id,
            expires_in_hours=0.05,
        )
        return str(submitted["order"]["work_order_id"])

    async def _await_answer(self, peer_id: str, order_id: str, session_id: str) -> dict[str, Any]:
        from .. import native_transport
        from ..llm_package.p2p import IceSignal

        deadline = time.monotonic() + ANSWER_TIMEOUT_S
        while time.monotonic() < deadline:
            values = await asyncio.to_thread(self.store.list_work_results, work_order_id=order_id,
                                             network_id=self.network_id())
            for item in values.get("work_results", []):
                if item.get("provider_peer_id") != peer_id:
                    continue
                if item.get("status") in {"failed", "cancelled"}:
                    code = str(dict(item.get("result_refs") or {}).get("error_code") or "refused")
                    raise ConnectionError(f"p2p_{code}")
                if item.get("status") != "accepted":
                    continue
                refs = dict(item.get("result_refs") or {})
                if refs.get("session_id") != session_id:
                    raise ConnectionError("p2p answer does not match the offer")
                native = native_transport.remote_info(refs.get("native"))
                if native is not None and native.get("fingerprint"):
                    return {"kind": "native", "native": native}
                fingerprint = str(refs.get("quic_cert_sha256") or "")
                if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                    raise ConnectionError("p2p answer does not match the offer")
                return {"kind": "ice", "ice_signal": IceSignal.from_dict(dict(refs.get("ice_signal") or {})),
                        "fingerprint": fingerprint}
            await asyncio.sleep(0.2)
        raise TimeoutError("p2p_no_answer")

    # ---- answering (receiver side) -----------------------------------------
    def _poll_offers(self) -> None:
        from ..llm_package.p2p import SIGNAL_LONG_POLL_S, signal_poll_idle_max_s

        error_delay = 2.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                polled = self.store.poll_work_orders(network_id=self.network_id(), capability=CAPABILITY,
                                                     wait_s=SIGNAL_LONG_POLL_S)
                error_delay = 2.0
            except Exception:
                log.debug("drop p2p signaling poll failed", exc_info=True)
                self._stop.wait(error_delay)
                error_delay = min(error_delay * 2, 60.0)
                continue
            fresh = []
            for order in polled.get("work_orders", []):
                order_id = str(order.get("work_order_id") or "")
                if not order_id or order_id in self._handled:
                    continue
                self._handled[order_id] = None
                while len(self._handled) > 1024:
                    self._handled.popitem(last=False)
                fresh.append(order)
            loop = self._loop
            for order in fresh:
                if loop is not None:
                    asyncio.run_coroutine_threadsafe(self._answer(order), loop)
            if fresh:
                continue
            if polled.get("waited") and time.monotonic() - started >= 1.0:
                continue  # the registry already waited for us
            self._stop.wait(signal_poll_idle_max_s(self.store))

    def _publish(self, order: dict[str, Any], status: str, message: str, refs: dict[str, Any]) -> None:
        try:
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]), requester_peer_id=str(order["requester_peer_id"]),
                status=status, message=message, result_refs=refs,
                network_id=str(order.get("network_id") or self.network_id()),
            )
        except Exception:
            log.debug("drop p2p result publish failed", exc_info=True)

    async def _answer(self, order: dict[str, Any]) -> None:
        from ..llm_package.p2p import (
            IceSignal,
            _close_connection,
            apply_remote_signal,
            gather_signal,
            new_connection,
        )

        requester = str(order.get("requester_peer_id") or "")
        params = dict(order.get("params") or {})
        session_id = str(params.get("session_id") or "")
        refuse = ""
        if order.get("operation") != OPERATION or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            refuse = "bad_offer"
        elif not requester or not self.allowed(requester):
            refuse = "not_paired"  # only own devices and friends may open a path to us
        elif self._answering >= 4 or len(self._live_links()) >= MAX_LINKS:
            refuse = "busy"
        if refuse:
            await asyncio.to_thread(self._publish, order, "failed", "drop p2p offer refused", {"error_code": refuse})
            return
        if await self._answer_native(order, requester, session_id, params):
            return
        self._answering += 1
        ice = new_connection(controlling=False)
        try:
            offer = IceSignal.from_dict(dict(params.get("ice_signal") or {}))
            cert, key, fingerprint = await asyncio.to_thread(_self_signed)
            answer = await asyncio.wait_for(gather_signal(ice), GATHER_TIMEOUT_S)
            await apply_remote_signal(ice, offer)
            await asyncio.to_thread(self._publish, order, "accepted", "drop p2p answer", {
                "transport": "ice_udp_direct", "relay_allowed": False, "session_id": session_id,
                "ice_signal": answer.to_dict(), "quic_cert_sha256": fingerprint, "alpn": ALPN,
            })
            await asyncio.wait_for(ice.connect(), CONNECT_TIMEOUT_S)
            link = await self._start_quic(requester, ice, client=False, cert=cert, key=key)
            await asyncio.to_thread(self._publish, order, "completed", "drop p2p link established",
                                    {"transport": "ice_udp_direct", "relay_used": False, "route": link.route})
        except Exception as exc:
            await _close_connection(ice)
            await asyncio.to_thread(self._publish, order, "failed", "drop p2p connection failed",
                                    {"error_code": type(exc).__name__.lower()[:40]})
        finally:
            self._answering -= 1

    async def _answer_native(self, order: dict[str, Any], requester: str, session_id: str,
                             params: dict[str, Any]) -> bool:
        """Answers on the native transport when the offer allows it; False = use aioice."""
        from .. import native_transport

        remote = native_transport.remote_info(params.get("native")) if native_transport.enabled() else None
        if remote is None:
            return False
        try:
            session = await native_transport.open_session("server")
        except Exception:
            log.debug("native p2p unavailable for this answer", exc_info=True)
            return False
        self._answering += 1
        try:
            await asyncio.to_thread(self._publish, order, "accepted", "drop p2p answer", {
                "transport": "ice_udp_direct", "relay_allowed": False, "session_id": session_id,
                "native": native_transport.offer(session),
            })
            connection = await native_transport.connect(session, remote, CONNECT_TIMEOUT_S)
            link = self._adopt_native(requester, connection, "server")
            await asyncio.to_thread(self._publish, order, "completed", "drop p2p link established",
                                    {"transport": "ice_udp_direct", "relay_used": False, "route": link.route,
                                     "data_plane": "quic_native"})
        except Exception as exc:
            session.close()
            await asyncio.to_thread(self._publish, order, "failed", "drop p2p connection failed",
                                    {"error_code": type(exc).__name__.lower()[:40]})
        finally:
            self._answering -= 1
        return True

    def _adopt_native(self, peer_id: str, connection: Any, role: str) -> _NativeLink:
        link = _NativeLink(self, peer_id, connection, role)
        loop = asyncio.get_running_loop()
        link.tasks.append(loop.create_task(self._accept_native(link)))

        async def watch() -> None:
            try:
                await connection.wait_closed()
            except asyncio.CancelledError:
                return
            if not link.closed:
                await link.close()

        link.tasks.append(loop.create_task(watch()))
        self._adopt(link)
        return link

    # ---- QUIC on the ICE pair ----------------------------------------------
    async def _start_quic(self, peer_id: str, ice: Any, *, client: bool, fingerprint: str = "",
                          cert: Any = None, key: Any = None) -> _Link:
        from aioquic.buffer import Buffer
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from aioquic.quic.packet import pull_quic_header
        from cryptography.hazmat.primitives import serialization

        from ..llm_package.p2p import selected_pair

        pair = selected_pair(ice)
        remote = (str(pair["remote"].get("host") or ""), int(pair["remote"].get("port") or 0))
        _enlarge_socket_buffers(ice)
        config = QuicConfiguration(
            is_client=client, alpn_protocols=[ALPN], idle_timeout=QUIC_IDLE_TIMEOUT_S,
            max_datagram_size=MAX_DATAGRAM_SIZE, max_data=64 * 1024 * 1024, max_stream_data=16 * 1024 * 1024,
            congestion_control_algorithm="cubic",
        )
        bridge = _IceDatagrams(ice)
        first: bytes | None = None
        if client:
            # Identity is the pinned fingerprint from signed signaling, not a CA.
            config.verify_mode = ssl.CERT_NONE
            quic = QuicConnection(configuration=config)
        else:
            config.certificate, config.private_key = cert, key
            first = await asyncio.wait_for(ice.recv(), HANDSHAKE_TIMEOUT_S)
            header = pull_quic_header(Buffer(data=first), host_cid_length=config.connection_id_length)
            quic = QuicConnection(configuration=config, original_destination_connection_id=header.destination_cid)
        _pace_per_timer_tick(quic)
        holder: dict[str, _Link] = {}
        protocol = self._quic_class(quic, lambda r, w: self._on_stream(holder["link"])(r, w))
        protocol.connection_made(bridge)
        link = _Link(self, peer_id, ice, protocol, bridge, remote, "client" if client else "server")
        holder["link"] = link
        link.tasks.append(asyncio.get_running_loop().create_task(self._receive(link)))
        try:
            if client:
                protocol.connect(remote)
            else:
                protocol.datagram_received(first, remote)
            await asyncio.wait_for(protocol.handshake_done.wait(), HANDSHAKE_TIMEOUT_S)
            if client:
                peer_cert = getattr(quic.tls, "_peer_certificate", None)
                if peer_cert is None:
                    raise ConnectionError("p2p peer presented no certificate")
                seen = hashlib.sha256(peer_cert.public_bytes(serialization.Encoding.DER)).hexdigest()
                if seen != fingerprint:
                    raise ConnectionError("p2p certificate does not match the signed answer")
        except BaseException:
            await link.close()
            raise
        link.tasks.append(asyncio.get_running_loop().create_task(self._keepalive(link)))
        self._adopt(link)
        return link

    async def _receive(self, link: _Link) -> None:
        try:
            while not link.closed:
                data = await link.ice.recv()
                link.protocol.datagram_received(data, link.remote)
        except (ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            return
        if not link.closed:
            asyncio.get_running_loop().create_task(link.close())

    async def _keepalive(self, link: _Link) -> None:
        try:
            while link.alive:
                await asyncio.sleep(KEEPALIVE_S / 3)
                if time.monotonic() - link.protocol.last_received < KEEPALIVE_S:
                    continue
                try:
                    await asyncio.wait_for(link.protocol.ping(), 10)
                except (asyncio.TimeoutError, ConnectionError):
                    break
        except asyncio.CancelledError:
            return
        if not link.closed:
            asyncio.get_running_loop().create_task(link.close())

    # ---- bookkeeping -------------------------------------------------------
    def _adopt(self, link: Any) -> None:
        current = self._links.get(link.peer_id)
        if current is not None and current.alive:
            # Both devices dialed at once: keep serving the second connection
            # until it idles out, but route new requests over the first.
            self._extra.add(link)
            return
        self._links[link.peer_id] = link

    def _forget(self, link: Any) -> None:
        self._extra.discard(link)
        if self._links.get(link.peer_id) is link:
            self._links.pop(link.peer_id, None)
            promoted = next((extra for extra in self._extra if extra.peer_id == link.peer_id and extra.alive), None)
            if promoted is not None:
                self._extra.discard(promoted)
                self._links[link.peer_id] = promoted

    def _live_links(self) -> list[Any]:
        return [link for link in (*self._links.values(), *self._extra) if link.alive]

    async def _evict_idle(self) -> None:
        idle = [link for link in self._live_links() if link.active == 0]
        if idle:
            await min(idle, key=lambda link: link.last_used).close()

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for link in [*self._links.values(), *self._extra]:
                if not link.alive or (link.active == 0 and now - link.last_used > LINK_IDLE_CLOSE_S):
                    await link.close()


def _enlarge_socket_buffers(ice: Any) -> None:
    import socket

    for protocol in getattr(ice, "_protocols", ()):
        sock = getattr(getattr(protocol, "transport", None), "get_extra_info", lambda _name: None)("socket")
        if sock is None:
            continue
        for option in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, option, SOCKET_BUFFER_BYTES)
            except OSError:
                pass


def _pace_per_timer_tick(quic: Any) -> None:
    """Let aioquic's pacer send a timer tick's worth of data per wakeup.

    aioquic caps a paced burst at 16 datagrams. With the event loop waking only
    every ~15.6 ms on Windows that caps a connection near 1.4 MB/s no matter how
    large the congestion window is. The rate itself stays cwnd / RTT; only the
    burst grows to one tick (and never beyond half an RTT of sending).
    """
    pacer = getattr(getattr(quic, "_loss", None), "_pacer", None)
    update_rate = getattr(pacer, "update_rate", None)
    if pacer is None or not callable(update_rate):
        return

    def widened(congestion_window: int, smoothed_rtt: float) -> None:
        update_rate(congestion_window=congestion_window, smoothed_rtt=smoothed_rtt)
        if pacer.packet_time is not None:
            pacer.bucket_max = max(pacer.bucket_max, min(TIMER_TICK_S, smoothed_rtt / 2))

    pacer.update_rate = widened
