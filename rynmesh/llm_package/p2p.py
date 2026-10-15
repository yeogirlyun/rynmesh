"""ICE/STUN peer-to-peer datagram transport for private LLM tasks.

The registry is used only as a signed signaling mailbox.  Prompt and response
envelopes cross the nominated ICE candidate pair directly and remain protected
by the existing end-to-end task envelope encryption.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import logging
import math
import os
import socket
import struct
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

import aioice


class P2PError(RuntimeError):
    pass


class P2PCapacityError(P2PError):
    """The configured fixed UDP port already belongs to an active session."""


_FIXED_PORTS: set[int] = set()
_FIXED_PORTS_LOCK = threading.Lock()
class P2PTimeoutError(asyncio.TimeoutError):
    def __init__(self, stage: str):
        super().__init__(f"P2P {stage} timed out")
        self.stage = stage


async def _connection_phase(awaitable, *, stage: str, timeout_s: float):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise P2PTimeoutError(stage) from exc


_MAGIC = b"RYNP2P1"
_DATA = 1
_ACK = 2
_HEADER = struct.Struct("!7sB16sHH32s")
_CHUNK_BYTES = 900
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_CHUNKS = math.ceil(_MAX_MESSAGE_BYTES / _CHUNK_BYTES)
_MAX_IN_FLIGHT_MESSAGES = 8
_MAX_BUFFERED_BYTES = _MAX_MESSAGE_BYTES * 2
_RECENT_MESSAGE_IDS = 128
# Keep each connection's burst small enough that 20 concurrent two-hop streams
# do not starve ICE consent/STUN traffic or overflow the UDP receive queue.
_SEND_WINDOW = 8
_ACK_WAIT_S = 0.25


def _candidate_ip_literal(value: Any, *, label: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    host = str(value or "").strip().strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise P2PError(f"ICE candidate {label} must be an IP literal") from exc
    if address.is_unspecified or address.is_multicast:
        raise P2PError(f"ICE candidate {label} is not a usable unicast address")
    if isinstance(address, ipaddress.IPv4Address) and address == ipaddress.IPv4Address(
        "255.255.255.255"
    ):
        raise P2PError(f"ICE candidate {label} is not a usable unicast address")
    return address


def _direct_candidate_from_sdp(value: str) -> aioice.Candidate:
    try:
        candidate = aioice.Candidate.from_sdp(value)
    except (AttributeError, ValueError) as exc:
        raise P2PError("invalid ICE candidate") from exc
    candidate_type = str(getattr(candidate, "type", "")).lower()
    transport = str(getattr(candidate, "transport", "")).lower()
    if candidate_type == "relay":
        raise P2PError("TURN/relay ICE candidate is forbidden in strict P2P mode")
    if candidate_type not in {"host", "srflx", "prflx"}:
        raise P2PError(f"non-direct ICE candidate is forbidden: {candidate_type}")
    if transport != "udp":
        raise P2PError(f"non-UDP ICE candidate is forbidden: {transport}")
    if int(getattr(candidate, "component", 0)) != 1:
        raise P2PError("ICE candidate component must be 1")
    port = int(getattr(candidate, "port", 0))
    if port < 1 or port > 65535:
        raise P2PError("ICE candidate port is invalid")
    _candidate_ip_literal(getattr(candidate, "host", ""), label="host")
    related_address = getattr(candidate, "related_address", None)
    if related_address:
        _candidate_ip_literal(related_address, label="related host")
        related_port = int(getattr(candidate, "related_port", 0) or 0)
        if related_port < 1 or related_port > 65535:
            raise P2PError("ICE candidate related port is invalid")
    return candidate


@dataclass(frozen=True)
class IceSignal:
    username: str
    password: str
    candidates: tuple[str, ...]
    session_protocol: str = ""
    # Native transport info (``rynmesh.native_transport``). An offer may carry
    # it next to the aioice candidates; an answer that carries it selects the
    # native data plane and may leave the aioice fields empty.
    native: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password": self.password,
            "candidates": list(self.candidates),
            **({"session_protocol": self.session_protocol} if self.session_protocol else {}),
            **({"native": self.native} if self.native else {}),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IceSignal":
        from ..native_transport import remote_info

        username = str(value.get("username") or "")
        password = str(value.get("password") or "")
        candidates = tuple(str(item) for item in value.get("candidates") or ())
        native = remote_info(value.get("native"))
        if native is not None and not (username or password or candidates):
            protocol = str(value.get("session_protocol") or "")
            if len(protocol) > 64:
                raise P2PError("invalid ICE session protocol")
            return cls(username="", password="", candidates=(), session_protocol=protocol, native=native)
        if not username or not password or not candidates:
            raise P2PError("incomplete ICE signal")
        if len(candidates) > 64 or any(len(item) > 1024 for item in candidates):
            raise P2PError("ICE candidate list exceeds safe limits")
        for candidate in candidates:
            _direct_candidate_from_sdp(candidate)
        protocol = str(value.get("session_protocol") or "")
        if len(protocol) > 64:
            raise P2PError("invalid ICE session protocol")
        return cls(username=username, password=password, candidates=candidates, session_protocol=protocol,
                   native=native)


# Several public STUN servers on different operators: one network blocking a
# server (or its DNS) must not cost every connection a 5 s gathering timeout
# and the server-reflexive candidate that public hole punching depends on.
DEFAULT_STUN_SERVERS = "stun.l.google.com:19302,stun.cloudflare.com:3478,stun.miwifi.com:3478"
STUN_PROBE_TIMEOUT_S = 1.5
STUN_CHOICE_TTL_S = 600.0
# Signaling: how long one registry long poll for P2P offers may wait.
SIGNAL_LONG_POLL_S = 20.0


def stun_servers_from_env() -> list[tuple[str, int]]:
    value = os.environ.get("RYNMESH_P2P_STUN", DEFAULT_STUN_SERVERS).strip()
    if not value or value.lower() in {"off", "none", "disabled"}:
        return []
    servers: list[tuple[str, int]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        host, separator, port = item.rpartition(":")
        if not separator or not host:
            raise P2PError("RYNMESH_P2P_STUN must be host:port[,host:port...]")
        parsed_port = int(port)
        if parsed_port < 1 or parsed_port > 65535:
            raise P2PError("RYNMESH_P2P_STUN port is invalid")
        if (host.strip("[]"), parsed_port) not in servers:
            servers.append((host.strip("[]"), parsed_port))
    return servers


def _stun_binding_reaches(servers: list[tuple[str, int]], timeout_s: float) -> tuple[str, int] | None:
    """Send one STUN Binding request to every server; the first answer wins."""
    import secrets
    import select

    pending: dict[socket.socket, tuple[tuple[str, int], bytes]] = {}
    try:
        for server in servers:
            try:
                address = socket.getaddrinfo(server[0], server[1], socket.AF_INET, socket.SOCK_DGRAM)[0][4]
            except OSError:
                continue
            transaction = secrets.token_bytes(12)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            try:
                sock.sendto(struct.pack("!HHI", 0x0001, 0, 0x2112A442) + transaction, address)
            except OSError:
                sock.close()
                continue
            pending[sock] = (server, transaction)
        deadline = time.monotonic() + timeout_s
        while pending and time.monotonic() < deadline:
            readable, _, _ = select.select(list(pending), [], [], max(0.0, deadline - time.monotonic()))
            for sock in readable:
                try:
                    data = sock.recv(2048)
                except OSError:
                    # e.g. ICMP port unreachable surfaced on Windows.
                    pending.pop(sock, None)
                    sock.close()
                    continue
                server, transaction = pending[sock]
                if len(data) >= 20 and data[:2] == b"\x01\x01" and data[8:20] == transaction:
                    return server
        return None
    finally:
        for sock in pending:
            sock.close()


class _StunChoice:
    """Remembers which configured STUN server currently answers fastest."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: tuple[tuple[str, int], ...] = ()
        self._winner: tuple[str, int] | None = None
        self._checked_at = 0.0
        self._refreshing = False

    def _refresh(self, servers: tuple[tuple[str, int], ...]) -> tuple[str, int] | None:
        winner = _stun_binding_reaches(list(servers), STUN_PROBE_TIMEOUT_S)
        with self._lock:
            if self._servers == servers:
                # Keep the previous choice when nothing answered this round.
                self._winner = winner or self._winner
                self._checked_at = time.monotonic()
            self._refreshing = False
        return winner

    def pick(self, servers: list[tuple[str, int]]) -> tuple[str, int]:
        key = tuple(servers)
        with self._lock:
            if self._servers != key:
                self._servers, self._winner, self._checked_at = key, None, 0.0
            stale = time.monotonic() - self._checked_at > STUN_CHOICE_TTL_S
            winner, refreshing = self._winner, self._refreshing
            if stale and not refreshing:
                self._refreshing = True
        if winner is not None:
            if stale and not refreshing:
                threading.Thread(target=self._refresh, args=(key,), daemon=True, name="ryn-stun-probe").start()
            return winner
        if refreshing:
            return servers[0]
        # First use with nothing known: a bounded probe beats a 5 s gather miss.
        return self._refresh(key) or servers[0]

    def warm(self, servers: list[tuple[str, int]]) -> None:
        key = tuple(servers)
        with self._lock:
            if self._refreshing or (self._servers == key and self._winner is not None):
                return
            self._servers, self._refreshing = key, True
        threading.Thread(target=self._refresh, args=(key,), daemon=True, name="ryn-stun-probe").start()


_STUN_CHOICE = _StunChoice()


def stun_server_from_env() -> tuple[str, int] | None:
    servers = stun_servers_from_env()
    if not servers:
        return None
    if len(servers) == 1:
        return servers[0]
    return _STUN_CHOICE.pick(servers)


def warm_stun_choice() -> None:
    """Probe the STUN servers in the background so the first connection is fast."""
    try:
        servers = stun_servers_from_env()
    except (P2PError, ValueError):
        return
    if len(servers) > 1:
        _STUN_CHOICE.warm(servers)


def signal_poll_idle_max_s(store: Any) -> float:
    """Longest idle gap between signaling polls.

    A long-polling registry already waited inside the previous call, so the
    next poll follows at once. Otherwise this gap is the worst-case delay
    before an idle node sees a P2P offer.
    """
    if getattr(getattr(store, "registry", None), "supports_order_wait", False):
        return 1.0
    try:
        value = float(os.environ.get("RYNMESH_P2P_SIGNAL_POLL_MAX_S", "3") or 3)
    except ValueError:
        value = 3.0
    return min(max(value, 0.5), 30.0)


def new_connection(*, controlling: bool) -> aioice.Connection:
    if os.environ.get("RYNMESH_P2P_DIAGNOSTICS", "").lower() in {"1", "true"}:
        # INFO contains candidate-pair transitions, never STUN credentials or
        # application payloads (those would require DEBUG).
        logger = logging.getLogger("aioice.ice")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
            logger.addHandler(handler)
    # No TURN server is accepted here: strict P2P must never nominate a relay.
    bind_port = _bind_port_from_env()
    connection_type = _FixedPortConnection if bind_port is not None else aioice.Connection
    return connection_type(
        ice_controlling=controlling,
        components=1,
        stun_server=stun_server_from_env(),
        use_ipv4=True,
        use_ipv6=True,
        **({"bind_port": bind_port} if bind_port is not None else {}),
    )


def _bind_port_from_env() -> int | None:
    value = os.environ.get("RYNMESH_P2P_BIND_PORT", "").strip()
    if not value:
        return None
    try:
        port = int(value)
    except ValueError as exc:
        raise P2PError("RYNMESH_P2P_BIND_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise P2PError("RYNMESH_P2P_BIND_PORT must be between 1 and 65535")
    return port


class _FixedPortConnection(aioice.Connection):
    """aioice connection whose host sockets use a firewall-friendly port.

    aioice normally binds an ephemeral UDP port. Cloud security groups cannot
    safely allow an unknown port, so Providers may opt into one UDP port while
    Consumers keep the normal ephemeral behavior.
    """

    def __init__(self, *, bind_port: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._rynmesh_bind_port = bind_port
        self._port_reserved = False

    async def close(self) -> None:
        if getattr(self, "_rynmesh_cleanup_complete", False):
            return
        transports = [protocol.transport for protocol in self._protocols]
        try:
            await super().close()
        finally:
            # Even a cancelled close must release every socket before another
            # session is admitted. Windows graceful close can retain a socket
            # with queued datagrams; calling close() again does not release it.
            try:
                for transport in transports:
                    transport.abort()
                await asyncio.sleep(0)  # run socket-close callbacks before reuse
                self._rynmesh_cleanup_complete = True
            finally:
                with _FIXED_PORTS_LOCK:
                    if self._port_reserved:
                        _FIXED_PORTS.discard(self._rynmesh_bind_port)
                        self._port_reserved = False

    async def get_component_candidates(
        self, component: int, addresses: list[str], timeout: int = 5
    ) -> list[aioice.Candidate]:
        from aioice.ice import (
            StunProtocol,
            candidate_foundation,
            candidate_priority,
            server_reflexive_candidate,
        )

        with _FIXED_PORTS_LOCK:
            if not self._port_reserved:
                if self._rynmesh_bind_port in _FIXED_PORTS:
                    raise P2PCapacityError("p2p_fixed_port_busy")
                _FIXED_PORTS.add(self._rynmesh_bind_port)
                self._port_reserved = True

        candidates: list[aioice.Candidate] = []
        host_protocols = []
        loop = asyncio.get_event_loop()
        port = self._rynmesh_bind_port + component - 1
        for address in addresses:
            try:
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: StunProtocol(self), local_addr=(address, port)
                )
            except OSError:
                continue
            self._protocols.append(protocol)
            sock = transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            host_protocols.append(protocol)
            candidate_address = transport.get_extra_info("sockname")
            protocol.local_candidate = aioice.Candidate(
                foundation=candidate_foundation("host", "udp", candidate_address[0]),
                component=component,
                transport="udp",
                priority=candidate_priority(component, "host"),
                host=candidate_address[0],
                port=candidate_address[1],
                type="host",
            )
            candidates.append(protocol.local_candidate)
        if not host_protocols:
            raise P2PError("p2p_fixed_port_unavailable")
        tasks = [
            asyncio.create_task(server_reflexive_candidate(protocol, self.stun_server))
            for protocol in host_protocols
            if self.stun_server
            and ipaddress.ip_address(protocol.local_candidate.host).version == 4
        ]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in done:
                if task.exception() is None:
                    candidate, protocol = task.result()
                    candidates.append(candidate)
                    if protocol is not None:
                        self._protocols.append(protocol)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return candidates


def native_sessions_enabled() -> bool:
    """Whether AI sessions may negotiate the native QUIC transport.

    Not with a fixed provider port (firewall rules name that one UDP port) or
    in the strict public-NAT acceptance modes, whose evidence is defined for
    aioice candidates.
    """
    from .. import native_transport

    return (native_transport.enabled() and not _bind_port_from_env()
            and not public_nat_traversal_required() and not distinct_public_egress_required())


def public_nat_traversal_required() -> bool:
    return os.environ.get("RYNMESH_P2P_REQUIRE_PUBLIC", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def distinct_public_egress_required() -> bool:
    return os.environ.get("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _public_mapping_hosts(signal: IceSignal) -> set[str]:
    hosts: set[str] = set()
    for value in signal.candidates:
        candidate = _direct_candidate_from_sdp(value)
        if str(getattr(candidate, "type", "")) == "srflx":
            host = str(getattr(candidate, "host", "")).strip()
            if host:
                hosts.add(host)
    return hosts


def validate_distinct_public_egress(local: IceSignal, remote: IceSignal) -> None:
    """Fail fast when an acceptance run cannot prove two public egresses."""
    if not distinct_public_egress_required():
        return
    local_hosts = _public_mapping_hosts(local)
    remote_hosts = _public_mapping_hosts(remote)
    if not local_hosts or not remote_hosts:
        raise P2PError("distinct public egress validation requires STUN mappings on both peers")
    if not any(local_host != remote_host for local_host in local_hosts for remote_host in remote_hosts):
        raise P2PError("strict P2P acceptance requires distinct public egress addresses")


async def gather_signal(connection: aioice.Connection) -> IceSignal:
    await connection.gather_candidates()
    local_candidates = list(connection.local_candidates)
    if public_nat_traversal_required():
        # Exchange only server-reflexive mappings. A local host candidate can
        # otherwise win ICE priority when the machines share a VPN/private
        # route, which is direct but does not prove public NAT traversal.
        local_candidates = [
            candidate for candidate in local_candidates
            if str(getattr(candidate, "type", "")) == "srflx"
        ]
    candidates = tuple(candidate.to_sdp() for candidate in local_candidates)
    if not candidates:
        if public_nat_traversal_required():
            raise P2PError(
                "public NAT traversal requires a server-reflexive STUN candidate; "
                "check outbound UDP and RYNMESH_P2P_STUN"
            )
        raise P2PError("ICE gathered no local candidates")
    return IceSignal(
        username=connection.local_username,
        password=connection.local_password,
        candidates=candidates,
    )


async def apply_remote_signal(connection: aioice.Connection, signal: IceSignal) -> None:
    connection.remote_username = signal.username
    connection.remote_password = signal.password
    for value in signal.candidates:
        await connection.add_remote_candidate(_direct_candidate_from_sdp(value))
    await connection.add_remote_candidate(None)


def _candidate_summary(candidate: Any) -> dict[str, Any]:
    if candidate is None:
        return {}
    return {
        "type": str(getattr(candidate, "type", "unknown")),
        "transport": str(getattr(candidate, "transport", "udp")),
        "host": str(getattr(candidate, "host", "")),
        "port": int(getattr(candidate, "port", 0) or 0),
    }


def selected_pair(connection: aioice.Connection) -> dict[str, Any]:
    """Return auditable endpoints for aioice's nominated component-1 pair."""
    nominated = getattr(connection, "_nominated", {})
    pair = nominated.get(1) if isinstance(nominated, dict) else None
    if pair is None:
        raise P2PError("ICE connected without an inspectable nominated pair")
    local = getattr(pair, "local_candidate", None)
    remote = getattr(pair, "remote_candidate", None)
    if str(getattr(local, "type", "")) == "relay" or str(getattr(remote, "type", "")) == "relay":
        raise P2PError("TURN/relay candidate was nominated in strict P2P mode")
    remote_type = str(getattr(remote, "type", ""))
    if public_nat_traversal_required() and remote_type not in {"srflx", "prflx"}:
        raise P2PError("strict public NAT traversal did not nominate a public peer mapping")
    return {
        "transport": "ice_udp_direct",
        "relay_used": False,
        "public_nat_traversal_required": public_nat_traversal_required(),
        "distinct_public_egress_required": distinct_public_egress_required(),
        "peer_public_mapping_nominated": remote_type in {"srflx", "prflx"},
        "path_kind": {
            "host": "host_direct",
            "srflx": "server_reflexive",
            "prflx": "peer_reflexive",
        }.get(remote_type, "direct_udp"),
        "local": _candidate_summary(local),
        "remote": _candidate_summary(remote),
    }


async def _close_connection(connection: aioice.Connection) -> None:
    # aioice waits for its receive/consent task while closing. Do not let a
    # completed request keep the HTTP response open indefinitely on Windows.
    if getattr(connection, "_rynmesh_cleanup_complete", False):
        return
    transports = [protocol.transport for protocol in getattr(connection, "_protocols", ())]
    try:
        await asyncio.wait_for(connection.close(), timeout=2.0)
    except asyncio.TimeoutError:
        return
    finally:
        # The exchange has already delivered/ACKed its terminal response or
        # failed. Force release if graceful shutdown timed out or was cancelled,
        # including ordinary ephemeral-port aioice connections on Windows.
        for transport in transports:
            transport.abort()
        await asyncio.sleep(0)
        # aioice's protocol-close future may have been cancelled by the timeout.
        # The sockets are now gone; repeated cleanup must not await it again.
        connection._rynmesh_cleanup_complete = True


def _encode_frames(payload: bytes) -> tuple[bytes, list[bytes]]:
    if not payload or len(payload) > _MAX_MESSAGE_BYTES:
        raise P2PError("P2P message size is invalid")
    message_id = uuid.uuid4().bytes
    digest = hashlib.sha256(payload).digest()
    total = math.ceil(len(payload) / _CHUNK_BYTES)
    frames = []
    for sequence in range(total):
        chunk = payload[sequence * _CHUNK_BYTES:(sequence + 1) * _CHUNK_BYTES]
        frames.append(_HEADER.pack(_MAGIC, _DATA, message_id, sequence, total, digest) + chunk)
    return message_id, frames


def _decode_header(packet: bytes) -> tuple[int, bytes, int, int, bytes, bytes]:
    if len(packet) < _HEADER.size:
        raise P2PError("short P2P datagram")
    magic, kind, message_id, sequence, total, digest = _HEADER.unpack(packet[:_HEADER.size])
    if magic != _MAGIC or kind not in {_DATA, _ACK}:
        raise P2PError("invalid P2P datagram")
    return kind, message_id, sequence, total, digest, packet[_HEADER.size:]


async def send_json(
    connection: aioice.Connection,
    value: dict[str, Any],
    *,
    timeout_s: float,
    reack_message_id: bytes | None = None,
    pending_out: list[bytes] | None = None,
) -> int:
    """Send one framed message and wait for its ACK.

    ACKs are datagrams and can all be lost in one congestion event, which used
    to deadlock both peers until timeout: the sender kept retransmitting while
    discarding the peer's next message. Two recovery paths close that hole:

    - ``reack_message_id``: an incoming DATA frame for this already-assembled
      message means the peer never got our ACK — answer it again.
    - ``pending_out``: an incoming DATA frame for a *new* message implies our
      message was delivered (this strict request/response protocol has no
      unsolicited traffic); the frame is buffered for receive_json and the
      send counts as acknowledged.
    """
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    message_id, frames = _encode_frames(payload)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in frames:
            await connection.send(frame)
        # Drain stale ACKs within this retry window. Resending on every unrelated
        # packet amplifies receive_json's three ACKs into growing DATA bursts.
        ack_deadline = min(deadline, time.monotonic() + 1.5)
        while time.monotonic() < ack_deadline:
            try:
                packet = await asyncio.wait_for(
                    connection.recv(), timeout=max(0.001, ack_deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                continue  # Windows timers can wake slightly before ack_deadline.
            try:
                kind, received_id, _sequence, _total, digest, _body = _decode_header(packet)
            except P2PError:
                continue
            if kind == _ACK and received_id == message_id:
                return len(payload)
            if kind == _DATA and reack_message_id is not None and received_id == reack_message_id:
                ack = _HEADER.pack(_MAGIC, _ACK, received_id, 0, 0, digest)
                for _ in range(3):
                    await connection.send(ack)
                continue
            if kind == _DATA and pending_out is not None and received_id != message_id:
                pending_out.append(packet)
                return len(payload)
    raise P2PError("timed out waiting for P2P message acknowledgement")


async def receive_json(
    connection: aioice.Connection,
    *,
    timeout_s: float,
    initial_packets: list[bytes] | None = None,
    message_id_out: list[bytes] | None = None,
) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout_s
    messages: dict[bytes, dict[str, Any]] = {}
    buffered_bytes = 0
    backlog = list(initial_packets or [])
    while time.monotonic() < deadline:
        if backlog:
            packet = backlog.pop(0)
        else:
            try:
                packet = await asyncio.wait_for(
                    connection.recv(), timeout=max(0.05, deadline - time.monotonic())
                )
            except asyncio.TimeoutError as exc:
                raise P2PError("timed out receiving P2P message") from exc
        try:
            kind, message_id, sequence, total, digest, body = _decode_header(packet)
        except P2PError:
            continue
        if kind != _DATA or total < 1 or sequence >= total:
            continue
        if total > _MAX_CHUNKS or len(body) > _CHUNK_BYTES:
            raise P2PError("P2P message declaration exceeds safe limits")
        if message_id not in messages:
            if len(messages) >= _MAX_IN_FLIGHT_MESSAGES:
                raise P2PError("too many simultaneous P2P messages")
            messages[message_id] = {"total": total, "digest": digest, "chunks": {}}
        state = messages[message_id]
        if state["total"] != total or state["digest"] != digest:
            continue
        if sequence not in state["chunks"]:
            buffered_bytes += len(body)
            if buffered_bytes > _MAX_BUFFERED_BYTES:
                raise P2PError("P2P buffered data exceeds safe limits")
        state["chunks"][sequence] = body
        if len(state["chunks"]) != total:
            continue
        payload = b"".join(state["chunks"][index] for index in range(total))
        if len(payload) > _MAX_MESSAGE_BYTES or hashlib.sha256(payload).digest() != digest:
            raise P2PError("P2P message integrity check failed")
        ack = _HEADER.pack(_MAGIC, _ACK, message_id, 0, 0, digest)
        for _ in range(3):
            await connection.send(ack)
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise P2PError("P2P message is not valid JSON") from exc
        if not isinstance(value, dict):
            raise P2PError("P2P message must be a JSON object")
        if message_id_out is not None:
            message_id_out.append(message_id)
        return value, len(payload)
    raise P2PError("timed out receiving P2P message")


async def send_bytes(connection: aioice.Connection, payload: bytes, *, timeout_s: float) -> int:
    """Reliably send one bounded opaque message over an ICE datagram pair.

    This is intentionally payload-agnostic so the peer-transit layer can
    forward end-to-end ciphertext without teaching the transit peer how to
    parse it.  Reliability remains hop-by-hop and bounded by
    ``_MAX_MESSAGE_BYTES``.
    """

    message_id, frames = _encode_frames(payload)
    deadline = time.monotonic() + timeout_s
    pending = set(range(len(frames)))
    while pending and time.monotonic() < deadline:
        window = sorted(pending)[:_SEND_WINDOW]
        for sequence in window:
            await connection.send(frames[sequence])
        ack_deadline = min(deadline, time.monotonic() + _ACK_WAIT_S)
        while pending.intersection(window) and time.monotonic() < ack_deadline:
            try:
                packet = await asyncio.wait_for(
                    connection.recv(),
                    timeout=max(0.01, ack_deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                break
            try:
                kind, received_id, sequence, total, _digest, _body = _decode_header(packet)
            except P2PError:
                continue
            if (
                kind == _ACK
                and received_id == message_id
                and total == len(frames)
                and sequence in pending
            ):
                pending.remove(sequence)
    if not pending:
        return len(payload)
    raise P2PError("timed out waiting for P2P message acknowledgement")


async def receive_bytes(connection: aioice.Connection, *, timeout_s: float) -> tuple[bytes, int]:
    """Receive and acknowledge one bounded opaque ICE message."""

    deadline = time.monotonic() + timeout_s
    messages: dict[bytes, dict[str, Any]] = {}
    buffered_bytes = 0
    recent = getattr(connection, "_rynmesh_recent_message_ids", None)
    if recent is None:
        recent = deque(maxlen=_RECENT_MESSAGE_IDS)
        connection._rynmesh_recent_message_ids = recent
    while time.monotonic() < deadline:
        try:
            packet = await asyncio.wait_for(
                connection.recv(), timeout=max(0.05, deadline - time.monotonic())
            )
        except asyncio.TimeoutError as exc:
            raise P2PError("timed out receiving P2P message") from exc
        try:
            kind, message_id, sequence, total, digest, body = _decode_header(packet)
        except P2PError:
            continue
        if kind != _DATA or total < 1 or sequence >= total:
            continue
        if message_id in recent:
            # A sender may retransmit just before our ACK arrives.  ACK the
            # already delivered message again but never surface it to the next
            # application receive call.
            ack = _HEADER.pack(_MAGIC, _ACK, message_id, sequence, total, digest)
            await _send_ack(connection, ack)
            continue
        if total > _MAX_CHUNKS or len(body) > _CHUNK_BYTES:
            raise P2PError("P2P message declaration exceeds safe limits")
        ack = _HEADER.pack(_MAGIC, _ACK, message_id, sequence, total, digest)
        await _send_ack(connection, ack)
        if message_id not in messages:
            if len(messages) >= _MAX_IN_FLIGHT_MESSAGES:
                raise P2PError("too many simultaneous P2P messages")
            messages[message_id] = {"total": total, "digest": digest, "chunks": {}}
        state = messages[message_id]
        if state["total"] != total or state["digest"] != digest:
            continue
        if sequence not in state["chunks"]:
            buffered_bytes += len(body)
            if buffered_bytes > _MAX_BUFFERED_BYTES:
                raise P2PError("P2P buffered data exceeds safe limits")
        state["chunks"][sequence] = body
        if len(state["chunks"]) != total:
            continue
        payload = b"".join(state["chunks"][index] for index in range(total))
        if len(payload) > _MAX_MESSAGE_BYTES or hashlib.sha256(payload).digest() != digest:
            raise P2PError("P2P message integrity check failed")
        # Duplicate the final acknowledgement because the receiver will now
        # return to its caller and may switch direction on the same ICE pair.
        # If the first final ACK is lost, this avoids a needless whole-session
        # timeout while keeping retransmission selective.
        for _ in range(2):
            await _send_ack(connection, ack)
        recent.append(message_id)
        return payload, len(payload)
    raise P2PError("timed out receiving P2P message")


async def _send_ack(connection: Any, ack: bytes) -> None:
    """Send an ACK while preserving receive-only protocol-test doubles."""

    sender = getattr(connection, "send", None)
    if sender is not None:
        await sender(ack)


async def _legacy_exchange_connected(connection, signed_request, evidence, timeout_s, on_event):
    pending: list[bytes] = []
    evidence["request_bytes"] = await send_json(
        connection, signed_request, timeout_s=timeout_s, pending_out=pending
    )
    seen: set[bytes] = set()
    deadline = time.monotonic() + timeout_s
    evidence["response_bytes"] = 0
    while True:
        response_id: list[bytes] = []
        response, response_bytes = await receive_json(
            connection, timeout_s=max(0.01, deadline - time.monotonic()),
            initial_packets=pending, message_id_out=response_id,
        )
        pending = []
        if response_id and response_id[0] in seen:
            continue
        if response_id:
            seen.add(response_id[0])
        if len(seen) > 131072 or time.monotonic() > deadline:
            raise P2PError("stream limit exceeded")
        evidence["response_bytes"] += response_bytes
        if response.get("payload", {}).get("kind") not in {"llm_stream", "llm_stream_delta"}:
            break
        if on_event is None:
            raise P2PError("unexpected stream event")
        await asyncio.to_thread(on_event, response)
    # Linger briefly re-ACKing response retransmits: if our assembly ACKs
    # were all lost, the provider is still resending and would otherwise
    # time out and misrecord the exchange as failed.
    linger_until = time.monotonic() + 0.5
    while time.monotonic() < linger_until:
        try:
            packet = await asyncio.wait_for(connection.recv(), timeout=0.1)
        except (asyncio.TimeoutError, Exception):
            break
        try:
            kind, received_id, _sequence, _total, digest, _body = _decode_header(packet)
        except P2PError:
            continue
        if kind == _DATA and response_id and received_id == response_id[0]:
            ack = _HEADER.pack(_MAGIC, _ACK, received_id, 0, 0, digest)
            for _ in range(3):
                await connection.send(ack)
    return response, evidence


async def consumer_exchange(
    *,
    signed_request: dict[str, Any],
    publish_offer: Callable[[IceSignal], Awaitable[IceSignal]],
    timeout_s: float,
    on_event: Any = None,
    connect_timeout_s: float | None = None,
    on_connected: Any = None,
    session_pool: Any = None,
    session_key: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if session_pool is not None and session_key and not _bind_port_from_env():
        return await _pooled_consumer_exchange(
            session_pool, session_key, signed_request, publish_offer, timeout_s,
            on_event, connect_timeout_s, on_connected,
        )
    connection = new_connection(controlling=True)
    try:
        # Waiting for the provider must not consume the time reserved for ICE
        # checks. Each phase is bounded separately; inference keeps its budget.
        phase_timeout = connect_timeout_s or timeout_s
        offer = await _connection_phase(gather_signal(connection), stage="gathering", timeout_s=phase_timeout)
        answer = await _connection_phase(publish_offer(offer), stage="signaling", timeout_s=phase_timeout)
        validate_distinct_public_egress(offer, answer)
        await apply_remote_signal(connection, answer)
        await _connection_phase(connection.connect(), stage="connecting", timeout_s=phase_timeout)
        evidence = selected_pair(connection)
        if on_connected is not None:
            on_connected(evidence)
        return await _legacy_exchange_connected(connection, signed_request, evidence, timeout_s, on_event)
    finally:
        await _close_connection(connection)


async def provider_exchange(
    *,
    offer: IceSignal,
    publish_answer: Callable[[IceSignal], Any],
    handle_request: Callable[[dict[str, Any]], dict[str, Any]],
    timeout_s: float,
    handle_stream_request: Any = None,
    connect_timeout_s: float | None = None,
    on_first_complete: Any = None,
    stop_event: Any = None,
) -> dict[str, Any]:
    from .p2p_session import PROTOCOL

    if offer.native is not None and offer.session_protocol == PROTOCOL and native_sessions_enabled():
        result = await _native_provider_session(
            offer, publish_answer, handle_request, timeout_s, handle_stream_request,
            connect_timeout_s, on_first_complete, stop_event,
        )
        if result is not None:
            return result
    connection = new_connection(controlling=False)
    try:
        phase_timeout = connect_timeout_s or timeout_s
        answer = await _connection_phase(gather_signal(connection), stage="gathering", timeout_s=phase_timeout)
        # A fixed-port deployment has one UDP slot shared with other peers and
        # file transfers. Do not reserve that slot for ten idle minutes.
        persistent = offer.session_protocol == PROTOCOL and not _bind_port_from_env()
        if persistent:
            answer = replace(answer, session_protocol=PROTOCOL)
        await apply_remote_signal(connection, offer)
        async def publish():
            result = publish_answer(answer)
            if inspect.isawaitable(result):
                await result
        await _connection_phase(publish(), stage="signaling", timeout_s=phase_timeout)
        validate_distinct_public_egress(answer, offer)
        await _connection_phase(connection.connect(), stage="connecting", timeout_s=phase_timeout)
        evidence = selected_pair(connection)
        if persistent:
            from .p2p_session import SessionChannel

            handler = handle_stream_request or (lambda request, emit: handle_request(request))
            return await _serve_session(SessionChannel(connection), handler,
                                        timeout_s, evidence, on_first_complete, stop_event)
        request_id: list[bytes] = []
        request, request_bytes = await receive_json(
            connection, timeout_s=timeout_s, message_id_out=request_id,
        )
        loop = asyncio.get_running_loop()

        def emit(value: dict[str, Any]) -> None:
            future = asyncio.run_coroutine_threadsafe(
                send_json(connection, value, timeout_s=min(timeout_s, 15),
                          reack_message_id=request_id[0] if request_id else None), loop,
            )
            try:
                future.result(timeout=min(timeout_s, 15) + 1)
            except Exception:
                future.cancel()
                raise

        if handle_stream_request:
            response = await asyncio.to_thread(handle_stream_request, request, emit)
        else:
            response = await asyncio.to_thread(handle_request, request)
        evidence["request_bytes"] = request_bytes
        evidence["response_bytes"] = await send_json(
            connection, response, timeout_s=timeout_s,
            reack_message_id=request_id[0] if request_id else None,
        )
        return evidence
    finally:
        await _close_connection(connection)


async def _pooled_consumer_exchange(pool, key, signed_request, publish_offer,
                                    timeout_s, on_event, connect_timeout_s, on_connected):
    from .p2p_session import PROTOCOL, SessionChannel
    started = time.monotonic()

    async def factory():
        from .. import native_transport

        connection = new_connection(controlling=True)
        native_session = None
        native_gather = (asyncio.ensure_future(native_transport.open_session("client"))
                         if native_sessions_enabled() and native_transport.allowed_for(key) else None)
        try:
            phase = connect_timeout_s or timeout_s
            offer = replace(await _connection_phase(gather_signal(connection), stage="gathering", timeout_s=phase), session_protocol=PROTOCOL)
            if native_gather is not None:
                try:
                    native_session = await native_gather
                    offer = replace(offer, native=native_transport.offer(native_session))
                except Exception:
                    logging.getLogger(__name__).debug("native gathering failed; offering aioice only", exc_info=True)
            answer = await _connection_phase(publish_offer(offer), stage="signaling", timeout_s=phase)
            if answer.native is not None and native_session is not None and answer.session_protocol == PROTOCOL:
                await _close_connection(connection)
                try:
                    native = await _connection_phase(
                        native_transport.connect(native_session, answer.native, phase), stage="connecting",
                        timeout_s=phase + 5,
                    )
                    channel = await native_transport.NativeSessionChannel.open(native, initiator=True, timeout_s=phase)
                except BaseException:
                    # The next session to this provider offers aioice only.
                    native_transport.note_failure(key)
                    raise
                native_transport.note_success(key)
                return channel, native_transport.evidence(native)
            if native_session is not None:
                native_session.close()
                native_session = None
            validate_distinct_public_egress(offer, answer)
            await apply_remote_signal(connection, answer)
            await _connection_phase(connection.connect(), stage="connecting", timeout_s=phase)
            evidence = selected_pair(connection)
            if answer.session_protocol != PROTOCOL:
                if on_connected:
                    on_connected(evidence)
                try:
                    result = await _legacy_exchange_connected(connection, signed_request, evidence, timeout_s, on_event)
                    return None, result
                finally:
                    await _close_connection(connection)
            return SessionChannel(connection), evidence
        except BaseException:
            if native_gather is not None and not native_gather.done():
                native_gather.cancel()
            if native_session is not None:
                native_session.close()
            await _close_connection(connection)
            raise

    async def exchange(channel, evidence):
        evidence["session_protocol"] = PROTOCOL
        evidence["connection_ms"] = round((time.monotonic() - started) * 1000)
        if on_connected:
            on_connected(evidence)
        evidence["request_bytes"] = await channel.send(signed_request, timeout_s)
        deadline = time.monotonic() + timeout_s
        count = 0
        first = True
        while True:
            response = await channel.receive(max(.01, deadline - time.monotonic()))
            if first:
                evidence["first_event_ms"] = round((time.monotonic() - started) * 1000)
                first = False
            count += 1
            if count > 131072:
                raise P2PError("stream limit exceeded")
            if response.get("payload", {}).get("kind") not in {"llm_stream", "llm_stream_delta"}:
                evidence["total_ms"] = round((time.monotonic() - started) * 1000)
                evidence["stream_events"] = count - 1
                return response, evidence
            if on_event is None:
                raise P2PError("unexpected stream event")
            await asyncio.to_thread(on_event, response)
    return await pool.run(key, factory, exchange)


async def _serve_session(channel, handler, timeout_s, evidence, on_first_complete, stop_event=None):
    """Serves requests on an established session channel (aioice or native)."""
    from .p2p_session import IDLE_SECONDS, PROTOCOL, WINDOW
    first = True
    loop = asyncio.get_running_loop()
    try:
        while not channel.closed and not (stop_event and stop_event.is_set()):
            deadline = time.monotonic() + (timeout_s if first else IDLE_SECONDS)
            request = None
            while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
                try:
                    request = await channel.receive(min(1, max(.01, deadline - time.monotonic())))
                    break
                except asyncio.TimeoutError:
                    continue
            if request is None:
                break
            pending = deque()

            def emit(value, pending=pending):
                if stop_event and stop_event.is_set():
                    raise P2PError("node stopping")
                pending.append(asyncio.run_coroutine_threadsafe(channel.send(value, min(timeout_s, 15)), loop))
                if len(pending) >= WINDOW:
                    pending.popleft().result(timeout=min(timeout_s, 15) + 1)

            def handle(request=request, emit=emit, pending=pending):
                try:
                    response = handler(request, emit)
                    while pending:
                        pending.popleft().result(timeout=min(timeout_s, 15) + 1)
                    return response
                finally:
                    for future in pending:
                        future.cancel()

            response = await asyncio.to_thread(handle)
            await channel.send(response, timeout_s)
            if first and on_first_complete:
                await asyncio.to_thread(on_first_complete, dict(evidence, session_protocol=PROTOCOL))
            first = False
        return evidence
    finally:
        await channel.close()


async def _native_provider_session(offer, publish_answer, handle_request, timeout_s, handle_stream_request,
                                   connect_timeout_s, on_first_complete, stop_event):
    """Answers a session offer on the native transport; None = fall back to aioice."""
    from .. import native_transport
    from .p2p_session import PROTOCOL

    try:
        session = await native_transport.open_session("server")
    except Exception:
        logging.getLogger(__name__).debug("native transport unavailable for this session", exc_info=True)
        return None
    phase_timeout = connect_timeout_s or timeout_s
    channel = None
    try:
        answer = IceSignal(username="", password="", candidates=(), session_protocol=PROTOCOL,
                           native=native_transport.offer(session))

        async def publish():
            result = publish_answer(answer)
            if inspect.isawaitable(result):
                await result

        await _connection_phase(publish(), stage="signaling", timeout_s=phase_timeout)
        connection = await _connection_phase(
            native_transport.connect(session, offer.native, phase_timeout), stage="connecting",
            timeout_s=phase_timeout + 5,
        )
        evidence = native_transport.evidence(connection)
        channel = await native_transport.NativeSessionChannel.open(connection, initiator=False, timeout_s=phase_timeout)
    except BaseException:
        session.close()
        if channel is not None:
            await channel.close()
        raise
    handler = handle_stream_request or (lambda request, emit: handle_request(request))
    return await _serve_session(channel, handler, timeout_s, evidence, on_first_complete, stop_event)
