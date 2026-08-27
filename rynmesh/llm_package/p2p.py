"""ICE/STUN peer-to-peer datagram transport for private LLM tasks.

The registry is used only as a signed signaling mailbox.  Prompt and response
envelopes cross the nominated ICE candidate pair directly and remain protected
by the existing end-to-end task envelope encryption.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aioice


class P2PError(RuntimeError):
    pass


_MAGIC = b"RYNP2P1"
_DATA = 1
_ACK = 2
_HEADER = struct.Struct("!7sB16sHH32s")
_CHUNK_BYTES = 900
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_CHUNKS = math.ceil(_MAX_MESSAGE_BYTES / _CHUNK_BYTES)
_MAX_IN_FLIGHT_MESSAGES = 8
_MAX_BUFFERED_BYTES = _MAX_MESSAGE_BYTES * 2


@dataclass(frozen=True)
class IceSignal:
    username: str
    password: str
    candidates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password": self.password,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IceSignal":
        username = str(value.get("username") or "")
        password = str(value.get("password") or "")
        candidates = tuple(str(item) for item in value.get("candidates") or ())
        if not username or not password or not candidates:
            raise P2PError("incomplete ICE signal")
        if len(candidates) > 64 or any(len(item) > 1024 for item in candidates):
            raise P2PError("ICE candidate list exceeds safe limits")
        return cls(username=username, password=password, candidates=candidates)


def stun_server_from_env() -> tuple[str, int] | None:
    value = os.environ.get("RYNMESH_P2P_STUN", "stun.l.google.com:19302").strip()
    if not value or value.lower() in {"off", "none", "disabled"}:
        return None
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise P2PError("RYNMESH_P2P_STUN must be host:port")
    parsed_port = int(port)
    if parsed_port < 1 or parsed_port > 65535:
        raise P2PError("RYNMESH_P2P_STUN port is invalid")
    return host.strip("[]"), parsed_port


def new_connection(*, controlling: bool) -> aioice.Connection:
    # No TURN server is accepted here: strict P2P must never nominate a relay.
    return aioice.Connection(
        ice_controlling=controlling,
        components=1,
        stun_server=stun_server_from_env(),
        use_ipv4=True,
        use_ipv6=True,
    )


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
        candidate = aioice.Candidate.from_sdp(value)
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
        await connection.add_remote_candidate(aioice.Candidate.from_sdp(value))
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
    if public_nat_traversal_required() and str(getattr(remote, "type", "")) != "srflx":
        raise P2PError("strict public NAT traversal did not nominate the peer's STUN mapping")
    remote_type = str(getattr(remote, "type", ""))
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
    try:
        await asyncio.wait_for(connection.close(), timeout=2.0)
    except asyncio.TimeoutError:
        return


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


async def send_json(connection: aioice.Connection, value: dict[str, Any], *, timeout_s: float) -> int:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    message_id, frames = _encode_frames(payload)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in frames:
            await connection.send(frame)
        wait = min(1.5, max(0.05, deadline - time.monotonic()))
        try:
            packet = await asyncio.wait_for(connection.recv(), timeout=wait)
        except asyncio.TimeoutError:
            continue
        try:
            kind, received_id, _sequence, _total, _digest, _body = _decode_header(packet)
        except P2PError:
            continue
        if kind == _ACK and received_id == message_id:
            return len(payload)
    raise P2PError("timed out waiting for P2P message acknowledgement")


async def receive_json(connection: aioice.Connection, *, timeout_s: float) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout_s
    messages: dict[bytes, dict[str, Any]] = {}
    buffered_bytes = 0
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
        return value, len(payload)
    raise P2PError("timed out receiving P2P message")


async def consumer_exchange(
    *,
    signed_request: dict[str, Any],
    publish_offer: Callable[[IceSignal], Awaitable[IceSignal]],
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = new_connection(controlling=True)
    try:
        offer = await gather_signal(connection)
        answer = await publish_offer(offer)
        validate_distinct_public_egress(offer, answer)
        await apply_remote_signal(connection, answer)
        await asyncio.wait_for(connection.connect(), timeout=timeout_s)
        evidence = selected_pair(connection)
        evidence["request_bytes"] = await send_json(
            connection, signed_request, timeout_s=timeout_s
        )
        response, response_bytes = await receive_json(connection, timeout_s=timeout_s)
        evidence["response_bytes"] = response_bytes
        return response, evidence
    finally:
        await _close_connection(connection)


async def provider_exchange(
    *,
    offer: IceSignal,
    publish_answer: Callable[[IceSignal], Any],
    handle_request: Callable[[dict[str, Any]], dict[str, Any]],
    timeout_s: float,
) -> dict[str, Any]:
    connection = new_connection(controlling=False)
    try:
        answer = await gather_signal(connection)
        await apply_remote_signal(connection, offer)
        publish_answer(answer)
        validate_distinct_public_egress(answer, offer)
        await asyncio.wait_for(connection.connect(), timeout=timeout_s)
        evidence = selected_pair(connection)
        request, request_bytes = await receive_json(connection, timeout_s=timeout_s)
        response = await asyncio.to_thread(handle_request, request)
        evidence["request_bytes"] = request_bytes
        evidence["response_bytes"] = await send_json(
            connection, response, timeout_s=timeout_s
        )
        return evidence
    finally:
        await _close_connection(connection)
