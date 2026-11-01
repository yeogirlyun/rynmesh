"""Optional native P2P transport: UDP hole punching + QUIC in Rust.

The ``ryn_transport`` extension (``native/ryn_transport``) does per-packet work
off the GIL: STUN, authenticated hole-punching probes, QUIC (quinn) and
congestion control. On the project's Windows VM it moved 133 MiB/s where the
pure-Python aioquic path managed ~2 MiB/s.

Device drop and private AI both negotiate it per connection: an offer carries
``native`` info next to the aioice signal, and the answer picks one. A peer
without the extension, an older build, or ``RYNMESH_P2P_NATIVE=off`` keeps the
existing aioice path, so rollout can happen device by device.

``RYNMESH_P2P_NATIVE``: ``auto`` (default: use when both sides have it),
``off``. ``RYNMESH_P2P_CC``: ``adaptive`` (default: CUBIC, BBR on random loss),
``cubic``, ``bbr``, ``newreno``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger("rynmesh.native_transport")

PROTOCOL = "ryn-native-p2p/1"
GATHER_TIMEOUT_S = 1.5
MAX_FRAME_BYTES = 8 * 1024 * 1024
_CANDIDATE_TYPES = {"host", "srflx", "prflx"}

_module: Any = None
_import_failed = False


def _load() -> Any:
    global _module, _import_failed
    if _module is None and not _import_failed:
        try:
            import ryn_transport  # type: ignore[import-not-found]
        except ImportError:
            _import_failed = True
        else:
            if getattr(ryn_transport, "PROTOCOL", "") == PROTOCOL:
                _module = ryn_transport
            else:
                _import_failed = True
    return _module


def enabled() -> bool:
    if os.environ.get("RYNMESH_P2P_NATIVE", "auto").strip().lower() in {"0", "off", "false", "no"}:
        return False
    return _load() is not None


# Peers whose last native attempt failed get aioice-only offers for a while
# (e.g. a path only IPv6 or aioice's candidate set can open; native is IPv4).
FAILURE_COOLDOWN_S = 600.0
_failed_at: dict[str, float] = {}


def note_failure(peer: str) -> None:
    _failed_at[peer] = time.monotonic()
    if len(_failed_at) > 1024:
        oldest = min(_failed_at, key=_failed_at.__getitem__)
        _failed_at.pop(oldest, None)


def note_success(peer: str) -> None:
    _failed_at.pop(peer, None)


def allowed_for(peer: str) -> bool:
    failed = _failed_at.get(peer)
    return failed is None or time.monotonic() - failed > FAILURE_COOLDOWN_S


def congestion_control() -> str:
    value = os.environ.get("RYNMESH_P2P_CC", "adaptive").strip().lower()
    return value if value in {"adaptive", "cubic", "bbr", "newreno"} else "adaptive"


def _stun_servers() -> list[str]:
    from .llm_package.p2p import P2PError, stun_server_from_env, stun_servers_from_env

    try:
        servers = stun_servers_from_env()
        preferred = stun_server_from_env() if servers else None
    except (P2PError, ValueError):
        return []
    ordered = ([preferred] if preferred else []) + [s for s in servers if s != preferred]
    return [f"[{host}]:{port}" if ":" in host else f"{host}:{port}" for host, port in ordered]


async def open_session(role: str) -> Any:
    """Binds a socket and gathers candidates (host + STUN mappings)."""
    module = _load()
    if module is None:
        raise RuntimeError("native transport is not available")
    servers = await asyncio.to_thread(_stun_servers)
    return await module.open_session(servers, GATHER_TIMEOUT_S if servers else 0.0, role)


def offer(session: Any) -> dict[str, Any]:
    info = dict(session.info())
    info["protocol"] = PROTOCOL
    return info


def remote_info(value: Any) -> dict[str, Any] | None:
    """Validated peer signaling, or None (then the aioice path is used)."""
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        return None
    secret = str(value.get("secret") or "")
    fingerprint = value.get("fingerprint")
    candidates = value.get("candidates")
    if not re.fullmatch(r"[0-9a-f]{32}", secret) or not isinstance(candidates, list) or not 0 < len(candidates) <= 32:
        return None
    if fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)):
        return None
    clean = []
    for item in candidates:
        if not isinstance(item, dict) or item.get("type") not in _CANDIDATE_TYPES:
            return None
        host, sep, port = str(item.get("addr") or "").rpartition(":")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
            port_number = int(port)
        except ValueError:
            return None
        # Literal unicast addresses only: no hostnames, broadcast or multicast.
        if not sep or address.is_multicast or address.is_unspecified or not 0 < port_number < 65536:
            return None
        if isinstance(address, ipaddress.IPv4Address) and address == ipaddress.IPv4Address("255.255.255.255"):
            return None
        clean.append({"type": item["type"], "addr": f"{address}:{port_number}" if address.version == 4
                      else f"[{address}]:{port_number}"})
    return {"protocol": PROTOCOL, "secret": secret, "candidates": clean,
            "fingerprint": str(fingerprint) if fingerprint is not None else None}


async def connect(session: Any, remote: dict[str, Any], timeout_s: float) -> Any:
    return await session.connect(remote, congestion_control(), float(timeout_s))


def evidence(connection: Any) -> dict[str, Any]:
    return dict(connection.evidence())


class StreamReader:
    """``readexactly``/``read`` over a native stream, with its own buffer."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self._buffer = bytearray()
        self._eof = False

    async def _fill(self, timeout_s: float | None) -> bool:
        if self._eof:
            return False
        chunk = await self.stream.read(1 << 20, timeout_s)
        if not chunk:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    async def readexactly(self, size: int, timeout_s: float | None = None) -> bytes:
        while len(self._buffer) < size:
            if not await self._fill(timeout_s):
                raise asyncio.IncompleteReadError(bytes(self._buffer), size)
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    async def read(self, size: int = -1, timeout_s: float | None = None) -> bytes:
        if not self._buffer and not await self._fill(timeout_s):
            return b""
        size = len(self._buffer) if size < 0 else min(size, len(self._buffer))
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out


def _frame(value: dict[str, Any]) -> bytes:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("native session message too large")
    return len(raw).to_bytes(4, "big") + raw


class NativeSessionChannel:
    """Private-AI session over one QUIC stream; same API as ``SessionChannel``.

    QUIC already delivers reliably and in order, so a message is just a
    length-prefixed JSON frame. Pings answer liveness probes from the pool.
    """

    def __init__(self, connection: Any, stream: Any) -> None:
        self.connection = connection
        self.stream = stream
        self.closed = False
        self.last_used = time.monotonic()
        self.last_received = time.monotonic()
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._pongs: dict[str, asyncio.Event] = {}
        self._write_lock = asyncio.Lock()
        self._reader = asyncio.get_running_loop().create_task(self._read())

    @classmethod
    async def open(cls, connection: Any, *, initiator: bool, timeout_s: float) -> "NativeSessionChannel":
        if initiator:
            stream = await connection.open_stream(timeout_s)
            channel = cls(connection, stream)
            # A QUIC stream becomes visible to the peer with its first byte.
            await channel._write({"t": "hello", "protocol": PROTOCOL})
            return channel
        stream = await connection.accept_stream(timeout_s)
        if stream is None:
            raise ConnectionError("native session closed before its stream opened")
        return cls(connection, stream)

    async def _write(self, value: dict[str, Any], timeout_s: float = 30.0) -> int:
        frame = _frame(value)
        async with self._write_lock:
            # The timeout is applied natively; a write cut short leaves a
            # partial frame, so callers treat any failure as a closed session.
            await self.stream.write(frame, max(1.0, timeout_s))
        return len(frame) - 4

    async def _read(self) -> None:
        reader = StreamReader(self.stream)
        try:
            while not self.closed:
                size = int.from_bytes(await reader.readexactly(4), "big")
                if size <= 0 or size > MAX_FRAME_BYTES:
                    raise ConnectionError("invalid native session frame")
                value = json.loads(await reader.readexactly(size))
                self.last_received = time.monotonic()
                kind = value.get("t") if isinstance(value, dict) else None
                if kind == "m" and isinstance(value.get("body"), dict):
                    self._incoming.put_nowait((value["body"], size))
                    self.last_used = time.monotonic()
                elif kind == "ping":
                    await self._write({"t": "pong", "id": str(value.get("id") or "")})
                elif kind == "pong":
                    event = self._pongs.get(str(value.get("id") or ""))
                    if event is not None:
                        event.set()
                elif kind == "close":
                    break
                elif kind != "hello":
                    raise ConnectionError("unexpected native session frame")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("native session reader stopped", exc_info=True)
        finally:
            self.closed = True
            self._incoming.put_nowait(None)
            for event in self._pongs.values():
                event.set()

    async def send(self, body: dict[str, Any], timeout_s: float) -> int:
        from .llm_package.p2p import P2PError

        if self.closed:
            raise P2PError("session closed")
        try:
            size = await self._write({"t": "m", "body": body}, timeout_s)
        except (ConnectionError, OSError, TimeoutError) as exc:
            self.closed = True
            raise P2PError("session closed") from exc
        self.last_used = time.monotonic()
        return size

    async def receive(self, timeout_s: float) -> dict[str, Any]:
        from .llm_package.p2p import P2PError

        if self.closed and self._incoming.empty():
            raise P2PError("session closed")
        value = await asyncio.wait_for(self._incoming.get(), timeout_s)
        if value is None:
            self._incoming.put_nowait(None)  # wake every later receiver too
            raise P2PError("session closed")
        return value[0]

    async def probe(self) -> bool:
        if self.closed or self.connection.is_closed():
            return False
        ping = os.urandom(8).hex()
        event = asyncio.Event()
        self._pongs[ping] = event
        try:
            for _ in range(3):
                try:
                    await self._write({"t": "ping", "id": ping})
                    await asyncio.wait_for(event.wait(), 0.5)
                    return not self.closed
                except asyncio.TimeoutError:
                    continue
                except (ConnectionError, OSError):
                    return False
            return False
        finally:
            self._pongs.pop(ping, None)

    async def close(self) -> None:
        if not self.closed:
            try:
                await self._write({"t": "close"}, 1.0)
            except Exception:
                pass
        self.closed = True
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        try:
            self.connection.close(0, "done")
        except Exception:
            pass
