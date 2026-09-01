"""Registry-signaled file streaming through an ordinary P2P transit peer.

The registry carries only signed ICE/session control records.  File bytes cross
two direct ICE/UDP legs and remain end-to-end encrypted between source and
target.  This module provides the runnable worker and source client used by the
three-node acceptance scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .crypto import SignedPayload
from .llm_package.p2p import (
    IceSignal,
    _close_connection,
    apply_remote_signal,
    gather_signal,
    new_connection,
    selected_pair,
    validate_distinct_public_egress,
)
from .peer_transit import (
    PROTOCOL_VERSION,
    TRANSIT_CAPABILITY,
    PathMetrics,
    PeerTransitError,
    RouteManager,
    TransitCipher,
    TransitSessionOpen,
    messaging_public_key,
    new_session_id,
    receive_encrypted_stream,
    relay_bidirectional_once,
    send_encrypted_stream,
    sign_session_open,
    validate_ice_hop,
    verify_session_open,
)
from .services.peer_box import load_or_create_messaging_key
from .store import RynmeshStore

OPEN_OPERATION = "open_peer_transit"
ACCEPT_OPERATION = "accept_peer_transit"
DIRECT_OPERATION = "accept_direct_file"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_DIRECT_ATTEMPT_TIMEOUT_S = 8.0
CAPACITY_MAX_AGE_HOURS = 1.0
DEFAULT_CAPACITY_REFRESH_S = 15 * 60.0
CAPACITY_LOOKUP_ATTEMPTS = 5
CAPACITY_LOOKUP_RETRY_S = 0.05
WORKER_ERROR_LOG_INTERVAL_S = 30.0
# Keep a reliable-message burst below typical UDP socket receive buffers.  The
# lower layer fragments this again into ~900-byte datagrams.
DEFAULT_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024


def _expires(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=float(seconds))).isoformat()


def _messaging_key(store: RynmeshStore):
    return load_or_create_messaging_key(store.home / "messaging.x25519")


def advertise_transit_capacity(
    store: RynmeshStore,
    *,
    network_id: str,
    roles: Iterable[str] = ("target", "transit"),
    max_concurrent: int = 8,
) -> dict[str, Any]:
    normalized_roles = sorted({str(role).strip() for role in roles if str(role).strip()})
    if not normalized_roles or any(role not in {"target", "transit"} for role in normalized_roles):
        raise PeerTransitError("transit capacity roles are invalid")
    return store.register_job_capacity(
        network_id=network_id,
        capabilities=[TRANSIT_CAPABILITY],
        max_concurrent=max_concurrent,
        polling_interval_sec=1,
        metadata={
            "protocol_version": PROTOCOL_VERSION,
            "roles": normalized_roles,
            "messaging_public_key": messaging_public_key(_messaging_key(store)),
            "max_file_bytes": _max_file_bytes(),
        },
    )


def _find_capacity(
    store: RynmeshStore,
    *,
    peer_id: str,
    role: str,
    network_id: str,
) -> dict[str, Any]:
    # A file-backed registry refreshes a capacity record with os.replace().  On
    # Windows, a reader that lands in the replacement window can briefly get a
    # sharing OSError; FilePeerRegistry deliberately skips an unreadable record
    # so one lookup can appear empty.  Retry the *discovery read* for a bounded
    # 200 ms before treating the signed advertisement as absent.  This also
    # tolerates the same short eventual-consistency window in remote registries.
    for attempt in range(CAPACITY_LOOKUP_ATTEMPTS):
        capacities = store.list_job_capacities(
            network_id=network_id,
            capability=TRANSIT_CAPABILITY,
            max_age_hours=CAPACITY_MAX_AGE_HOURS,
        ).get("capacities", [])
        for capacity in capacities:
            metadata = dict(capacity.get("metadata") or {})
            if (
                str(capacity.get("peer_id") or "") == peer_id
                and metadata.get("protocol_version") == PROTOCOL_VERSION
                and role in (metadata.get("roles") or [])
            ):
                return capacity
        if attempt + 1 < CAPACITY_LOOKUP_ATTEMPTS:
            time.sleep(CAPACITY_LOOKUP_RETRY_S)
    raise PeerTransitError(f"peer does not advertise {role} transit capacity")


def _poll_result(
    store: RynmeshStore,
    *,
    work_order_id: str,
    network_id: str,
    expected_provider_peer_id: str,
    expected_requester_peer_id: str,
    wanted_status: str,
    timeout_s: float,
) -> dict[str, Any]:
    expected_order = str(work_order_id or "").strip()
    expected_network = str(network_id or "").strip()
    expected_provider = str(expected_provider_peer_id or "").strip()
    expected_requester = str(expected_requester_peer_id or "").strip()
    if (
        not expected_order
        or not expected_network
        or not expected_provider
        or not expected_requester
    ):
        raise PeerTransitError("result identity binding is incomplete")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        results = store.list_work_results(
            work_order_id=work_order_id,
            network_id=network_id,
            provider_peer_id=expected_provider,
            requester_peer_id=expected_requester,
        ).get("work_results", [])
        for result in reversed(results):
            if (
                str(result.get("work_order_id") or "") != expected_order
                or str(result.get("network_id") or "") != expected_network
                or str(result.get("provider_peer_id") or "") != expected_provider
                or str(result.get("requester_peer_id") or "") != expected_requester
            ):
                continue
            status = str(result.get("status") or "")
            if status == "failed":
                raise PeerTransitError(str(result.get("message") or "peer transit failed"))
            if status == wanted_status:
                return result
        time.sleep(0.05)
    raise PeerTransitError(f"timed out waiting for transit result: {wanted_status}")


def _file_chunks(path: Path, manifest: dict[str, Any], chunk_bytes: int) -> Iterable[bytes]:
    yield b"M" + json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            yield b"D" + chunk


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class _TargetFileSink:
    def __init__(self, inbox: Path, *, session_id: str, max_file_bytes: int) -> None:
        self.inbox = inbox
        self.session_id = session_id
        self.max_file_bytes = max_file_bytes
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = self.inbox / ".tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.manifest: dict[str, Any] | None = None
        self._path = self.tmp_dir / f"{session_id}.part"
        self._handle = None
        self._digest = hashlib.sha256()
        self._size = 0

    def write(self, chunk: bytes) -> None:
        if not chunk:
            raise PeerTransitError("empty file protocol chunk")
        kind, body = chunk[:1], chunk[1:]
        if self.manifest is None:
            if kind != b"M":
                raise PeerTransitError("file manifest must be the first transit chunk")
            try:
                manifest = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PeerTransitError("invalid encrypted file manifest") from exc
            if not isinstance(manifest, dict) or manifest.get("kind") != "file":
                raise PeerTransitError("unsupported transit request kind")
            size = int(manifest.get("size_bytes", -1))
            if size < 0 or size > self.max_file_bytes:
                raise PeerTransitError("transit file exceeds target size policy")
            self.manifest = manifest
            self._handle = self._path.open("wb")
            return
        if kind != b"D":
            raise PeerTransitError("invalid transit file data chunk")
        self._size += len(body)
        if self._size > self.max_file_bytes:
            raise PeerTransitError("transit file exceeds target size policy")
        self._digest.update(body)
        assert self._handle is not None
        self._handle.write(body)

    def finish(self) -> dict[str, Any]:
        if self.manifest is None or self._handle is None:
            raise PeerTransitError("transit file manifest is missing")
        self._handle.close()
        expected_size = int(self.manifest["size_bytes"])
        expected_hash = str(self.manifest.get("sha256") or "")
        actual_hash = "sha256:" + self._digest.hexdigest()
        if self._size != expected_size:
            self._path.unlink(missing_ok=True)
            raise PeerTransitError("transit target file size mismatch")
        if actual_hash != expected_hash:
            self._path.unlink(missing_ok=True)
            raise PeerTransitError("transit target file hash mismatch")
        filename = Path(str(self.manifest.get("filename") or "artifact.bin")).name
        destination = self.inbox / f"{self.session_id}-{filename}"
        self._path.replace(destination)
        return {
            "status": "stored",
            "session_id": self.session_id,
            "filename": filename,
            "size_bytes": self._size,
            "sha256": actual_hash,
            "stored_path": str(destination),
        }

    def abort(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._path.unlink(missing_ok=True)


class PeerTransitWorker:
    def __init__(
        self,
        store: RynmeshStore,
        *,
        role: str,
        network_id: str,
        inbox: str | Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_concurrent: int = 8,
        allow_direct: bool = True,
        audit_frame: Any | None = None,
        audit_session: Any | None = None,
        capacity_refresh_s: float = DEFAULT_CAPACITY_REFRESH_S,
    ) -> None:
        if role not in {"target", "transit", "both"}:
            raise ValueError("peer transit role must be target, transit, or both")
        self.store = store
        self.role = role
        self.network_id = network_id
        self.inbox = Path(inbox or store.home / "transit-inbox")
        self.timeout_s = float(timeout_s)
        if max_concurrent < 1:
            raise ValueError("peer transit max_concurrent must be positive")
        self.max_concurrent = int(max_concurrent)
        self.allow_direct = bool(allow_direct)
        self.audit_frame = audit_frame
        self.audit_session = audit_session
        if capacity_refresh_s <= 0:
            raise ValueError("peer transit capacity refresh interval must be positive")
        self.capacity_refresh_s = float(capacity_refresh_s)
        self._control_error_lock = threading.Lock()
        self._control_error_count = 0
        self._first_control_error = ""
        self._last_control_error = ""

    def control_error_snapshot(self) -> dict[str, Any]:
        with self._control_error_lock:
            return {
                "count": self._control_error_count,
                "first": self._first_control_error,
                "last": self._last_control_error,
            }

    def _record_control_error(self, message: str) -> None:
        with self._control_error_lock:
            self._control_error_count += 1
            if not self._first_control_error:
                self._first_control_error = message
            self._last_control_error = message

    def register(self) -> dict[str, Any]:
        roles = ("target", "transit") if self.role == "both" else (self.role,)
        return advertise_transit_capacity(
            self.store,
            network_id=self.network_id,
            roles=roles,
            max_concurrent=self.max_concurrent,
        )

    def serve_forever(self, *, poll_interval_s: float = 0.2, stop_event: Any | None = None) -> None:
        self.register()
        next_capacity_refresh = time.monotonic() + self.capacity_refresh_s
        print(
            f"[{TRANSIT_CAPABILITY}] peer={self.store.peer_id} role={self.role} "
            f"network={self.network_id}"
        )
        in_flight: dict[str, Future[None]] = {}
        last_error_message = ""
        last_error_logged_at = float("-inf")
        with ThreadPoolExecutor(
            max_workers=self.max_concurrent,
            thread_name_prefix=f"rynmesh-{self.role}",
        ) as pool:
            while stop_event is None or not stop_event.is_set():
                try:
                    for order_id, future in list(in_flight.items()):
                        if not future.done():
                            continue
                        try:
                            future.result()
                        finally:
                            del in_flight[order_id]
                    now = time.monotonic()
                    if now >= next_capacity_refresh:
                        self.register()
                        next_capacity_refresh = now + self.capacity_refresh_s
                    available = self.max_concurrent - len(in_flight)
                    if available > 0:
                        for order in self._pending_orders():
                            order_id = str(order.get("work_order_id") or "")
                            handler = self._handler_for_order(order)
                            if not order_id or handler is None or order_id in in_flight:
                                continue
                            in_flight[order_id] = pool.submit(self._run_order, order, handler)
                            available -= 1
                            if available == 0:
                                break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    message = f"{type(exc).__name__}: {exc}"
                    self._record_control_error(message)
                    error_at = time.monotonic()
                    if (
                        message != last_error_message
                        or error_at - last_error_logged_at >= WORKER_ERROR_LOG_INTERVAL_S
                    ):
                        print(f"[{TRANSIT_CAPABILITY}] worker error: {message}")
                        last_error_message = message
                        last_error_logged_at = error_at
                if stop_event is None:
                    time.sleep(poll_interval_s)
                else:
                    stop_event.wait(poll_interval_s)

    def _pending_orders(self) -> list[dict[str, Any]]:
        return self.store.poll_work_orders(
            network_id=self.network_id,
            capability=TRANSIT_CAPABILITY,
        ).get("work_orders", [])

    def _handler_for_order(self, order: dict[str, Any]) -> Any | None:
        operation = str(order.get("operation") or "")
        if operation == OPEN_OPERATION and self.role in {"transit", "both"}:
            return self._serve_transit
        if operation in {ACCEPT_OPERATION, DIRECT_OPERATION} and self.role in {
            "target",
            "both",
        }:
            return self._serve_target
        return None

    def serve_once(self) -> int:
        processed = 0
        for order in self._pending_orders():
            handler = self._handler_for_order(order)
            if handler is not None:
                processed += 1
                self._run_order(order, handler)
        return processed

    def _emit_session_audit(self, phase: str, order: dict[str, Any]) -> None:
        if self.audit_session is None:
            return
        signed_open = dict((order.get("params") or {}).get("signed_session_open") or {})
        payload = dict(signed_open.get("payload") or {})
        event = {
            "phase": phase,
            "at_monotonic": time.monotonic(),
            "work_order_id": str(order.get("work_order_id") or ""),
            "operation": str(order.get("operation") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "role": self.role,
        }
        try:
            self.audit_session(event)
        except Exception as exc:  # noqa: BLE001
            print(f"[{TRANSIT_CAPABILITY}] session audit error: {exc}")

    def _run_order(self, order: dict[str, Any], handler: Any) -> None:
        self._emit_session_audit("started", order)
        try:
            asyncio.run(handler(order))
        except Exception as exc:
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]),
                requester_peer_id=str(order["requester_peer_id"]),
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
                network_id=self.network_id,
            )
        finally:
            self._emit_session_audit("finished", order)

    async def _serve_transit(self, order: dict[str, Any]) -> None:
        params = dict(order.get("params") or {})
        session = verify_session_open(dict(params.get("signed_session_open") or {}))
        if session.source_peer_id != str(order.get("requester_peer_id") or ""):
            raise PeerTransitError("source order identity does not match signed session")
        _find_capacity(
            self.store,
            peer_id=session.target_peer_id,
            role="target",
            network_id=self.network_id,
        )
        source_offer = IceSignal.from_dict(dict(params.get("source_ice_offer") or {}))
        left = new_connection(controlling=False)
        right = new_connection(controlling=True)
        try:
            left_answer, right_offer = await asyncio.gather(
                gather_signal(left),
                gather_signal(right),
            )
            await apply_remote_signal(left, source_offer)
            validate_distinct_public_egress(left_answer, source_offer)
            target_order = self.store.submit_work_order(
                provider_peer_id=session.target_peer_id,
                capability=TRANSIT_CAPABILITY,
                operation=ACCEPT_OPERATION,
                params={
                    "relay_ice_offer": right_offer.to_dict(),
                    "signed_session_open": dict(params["signed_session_open"]),
                    "transit_peer_id": self.store.peer_id,
                },
                network_id=self.network_id,
                expires_in_hours=max(self.timeout_s / 3600.0, 0.01),
            )
            target_order_id = str(target_order["order"]["work_order_id"])
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]),
                requester_peer_id=session.source_peer_id,
                status="accepted",
                message="source-to-transit ICE answer ready",
                result_refs={
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": session.session_id,
                    "source_ice_answer": left_answer.to_dict(),
                    "target_work_order_id": target_order_id,
                    "ice_relay_candidate_used": False,
                },
                network_id=self.network_id,
            )
            left_connect = asyncio.create_task(left.connect())
            target_accepted = await asyncio.to_thread(
                _poll_result,
                self.store,
                work_order_id=target_order_id,
                network_id=self.network_id,
                expected_provider_peer_id=session.target_peer_id,
                expected_requester_peer_id=self.store.peer_id,
                wanted_status="accepted",
                timeout_s=self.timeout_s,
            )
            target_answer = IceSignal.from_dict(
                dict((target_accepted.get("result_refs") or {}).get("relay_ice_answer") or {})
            )
            validate_distinct_public_egress(right_offer, target_answer)
            await apply_remote_signal(right, target_answer)
            await asyncio.wait_for(asyncio.gather(left_connect, right.connect()), self.timeout_s)
            hop_1 = selected_pair(left)
            hop_2 = selected_pair(right)
            validate_ice_hop(hop_1)
            validate_ice_hop(hop_2)
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]),
                requester_peer_id=session.source_peer_id,
                status="running",
                message="two direct ICE legs nominated; forwarding ciphertext",
                result_refs={
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": session.session_id,
                    "path_mode": "peer_transit",
                    "hop_1": hop_1,
                    "hop_2": hop_2,
                    "ice_relay_candidate_used": False,
                },
                network_id=self.network_id,
            )
            counters = await relay_bidirectional_once(
                left,
                right,
                session_id=session.session_id,
                timeout_s=self.timeout_s,
                audit_frame=self.audit_frame,
            )
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]),
                requester_peer_id=session.source_peer_id,
                status="completed",
                message="peer transit completed",
                result_refs={
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": session.session_id,
                    "path_mode": "peer_transit",
                    "transit_peer_id": self.store.peer_id,
                    "hop_1": hop_1,
                    "hop_2": hop_2,
                    "ice_relay_candidate_used": False,
                    "target_work_order_id": target_order_id,
                    **counters,
                },
                network_id=self.network_id,
            )
        finally:
            await asyncio.gather(_close_connection(left), _close_connection(right))

    async def _serve_target(self, order: dict[str, Any]) -> None:
        params = dict(order.get("params") or {})
        signed_open = SignedPayload.from_dict(dict(params.get("signed_session_open") or {}))
        session = verify_session_open(
            signed_open,
            expected_target_peer_id=self.store.peer_id,
        )
        direct = str(order.get("operation") or "") == DIRECT_OPERATION
        if direct and not self.allow_direct:
            raise PeerTransitError("direct path disabled by target policy")
        requester_peer_id = str(order.get("requester_peer_id") or "")
        if direct:
            if requester_peer_id != session.source_peer_id:
                raise PeerTransitError("direct target order source identity mismatch")
            offer_value = params.get("source_ice_offer")
        else:
            if requester_peer_id != str(params.get("transit_peer_id") or ""):
                raise PeerTransitError("target order transit identity mismatch")
            offer_value = params.get("relay_ice_offer")
        offer = IceSignal.from_dict(dict(offer_value or {}))
        connection = new_connection(controlling=False)
        sink = _TargetFileSink(
            self.inbox,
            session_id=session.session_id,
            max_file_bytes=_max_file_bytes(),
        )
        try:
            answer = await gather_signal(connection)
            await apply_remote_signal(connection, offer)
            validate_distinct_public_egress(answer, offer)
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]),
                requester_peer_id=str(order["requester_peer_id"]),
                status="accepted",
                message="transit-to-target ICE answer ready",
                result_refs={
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": session.session_id,
                    ("source_ice_answer" if direct else "relay_ice_answer"): answer.to_dict(),
                    "path_mode": "direct" if direct else "peer_transit",
                    "ice_relay_candidate_used": False,
                },
                network_id=self.network_id,
            )
            await asyncio.wait_for(connection.connect(), self.timeout_s)
            hop = selected_pair(connection)
            validate_ice_hop(hop)
            cipher = TransitCipher.for_target(
                session=session,
                target_messaging_key=_messaging_key(self.store),
            )
            await receive_encrypted_stream(
                connection,
                cipher,
                direction="request",
                sink=sink.write,
                timeout_s=self.timeout_s,
            )
            receipt = sink.finish()
            await send_encrypted_stream(
                connection,
                cipher,
                direction="response",
                chunks=[json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode("utf-8")],
                timeout_s=self.timeout_s,
            )
            self.store.publish_work_result(
                work_order_id=str(order["work_order_id"]),
                requester_peer_id=str(order["requester_peer_id"]),
                status="completed",
                message="target stored peer-transit artifact",
                result_refs={
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": session.session_id,
                    "path_mode": "direct" if direct else "peer_transit",
                    "hop": hop,
                    "ice_relay_candidate_used": False,
                    **receipt,
                },
                network_id=self.network_id,
            )
        except Exception:
            sink.abort()
            raise
        finally:
            await _close_connection(connection)


async def _send_file_async(
    store: RynmeshStore,
    *,
    path: Path,
    relay_peer_id: str,
    target_peer_id: str,
    network_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    session_started = time.monotonic()
    target_capacity = _find_capacity(
        store,
        peer_id=target_peer_id,
        role="target",
        network_id=network_id,
    )
    _find_capacity(
        store,
        peer_id=relay_peer_id,
        role="transit",
        network_id=network_id,
    )
    target_messaging_pub = str(
        (target_capacity.get("metadata") or {}).get("messaging_public_key") or ""
    )
    if not target_messaging_pub:
        raise PeerTransitError("target capacity is missing its messaging public key")

    connection = new_connection(controlling=True)
    try:
        offer = await gather_signal(connection)
        session_id = new_session_id()
        cipher, ephemeral = TransitCipher.for_source(
            session_id=session_id,
            source_peer_id=store.peer_id,
            target_peer_id=target_peer_id,
            target_messaging_pub=target_messaging_pub,
        )
        session = TransitSessionOpen(
            session_id=session_id,
            source_peer_id=store.peer_id,
            target_peer_id=target_peer_id,
            source_ephemeral_pub=messaging_public_key(ephemeral),
            expires_at=_expires(timeout_s * 2),
        )
        signed_open = sign_session_open(session, source_signing_key=store.private_key_bytes)
        submitted = store.submit_work_order(
            provider_peer_id=relay_peer_id,
            capability=TRANSIT_CAPABILITY,
            operation=OPEN_OPERATION,
            params={
                "source_ice_offer": offer.to_dict(),
                "signed_session_open": signed_open.to_dict(),
            },
            network_id=network_id,
            expires_in_hours=max(timeout_s / 3600.0, 0.01),
        )
        work_order_id = str(submitted["order"]["work_order_id"])
        accepted = await asyncio.to_thread(
            _poll_result,
            store,
            work_order_id=work_order_id,
            network_id=network_id,
            expected_provider_peer_id=relay_peer_id,
            expected_requester_peer_id=store.peer_id,
            wanted_status="accepted",
            timeout_s=timeout_s,
        )
        answer = IceSignal.from_dict(
            dict((accepted.get("result_refs") or {}).get("source_ice_answer") or {})
        )
        validate_distinct_public_egress(offer, answer)
        target_work_order_id = str(
            (accepted.get("result_refs") or {}).get("target_work_order_id") or ""
        )
        if not target_work_order_id:
            raise PeerTransitError("relay answer omitted the target work order")
        await apply_remote_signal(connection, answer)
        await asyncio.wait_for(connection.connect(), timeout_s)
        session_established_s = time.monotonic() - session_started
        source_hop = selected_pair(connection)
        validate_ice_hop(source_hop)

        source_hash = _file_sha256(path)
        manifest = {
            "kind": "file",
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": source_hash,
        }
        sent = await send_encrypted_stream(
            connection,
            cipher,
            direction="request",
            chunks=_file_chunks(path, manifest, DEFAULT_CHUNK_BYTES),
            timeout_s=timeout_s,
        )
        response = bytearray()
        await receive_encrypted_stream(
            connection,
            cipher,
            direction="response",
            sink=response.extend,
            timeout_s=timeout_s,
        )
        try:
            receipt = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PeerTransitError("target returned an invalid encrypted receipt") from exc
        if not isinstance(receipt, dict) or receipt.get("sha256") != source_hash:
            raise PeerTransitError("target receipt hash does not match source")
        completed = await asyncio.to_thread(
            _poll_result,
            store,
            work_order_id=work_order_id,
            network_id=network_id,
            expected_provider_peer_id=relay_peer_id,
            expected_requester_peer_id=store.peer_id,
            wanted_status="completed",
            timeout_s=timeout_s,
        )
        relay_refs = dict(completed.get("result_refs") or {})
        if relay_refs.get("ice_relay_candidate_used") is not False:
            raise PeerTransitError("relay evidence did not prove TURN-free ICE legs")
        target_completed = await asyncio.to_thread(
            _poll_result,
            store,
            work_order_id=target_work_order_id,
            network_id=network_id,
            expected_provider_peer_id=target_peer_id,
            expected_requester_peer_id=relay_peer_id,
            wanted_status="completed",
            timeout_s=timeout_s,
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "source_peer_id": store.peer_id,
            "transit_peer_id": relay_peer_id,
            "target_peer_id": target_peer_id,
            "session_id": session_id,
            "session_established_s": session_established_s,
            "path_mode": "peer_transit",
            "ice_relay_candidate_used": False,
            "source_hop": source_hop,
            "source_sha256": source_hash,
            "target_sha256": str(receipt["sha256"]),
            "source_size_bytes": path.stat().st_size,
            "target_size_bytes": int(receipt.get("size_bytes") or 0),
            "transit_rx_bytes": int(relay_refs.get("transit_rx_bytes") or 0),
            "transit_tx_bytes": int(relay_refs.get("transit_tx_bytes") or 0),
            "request_frames": int(relay_refs.get("request_frames") or 0),
            "response_frames": int(relay_refs.get("response_frames") or 0),
            "registry_payload_bytes": 0,
            "plaintext_found_on_transit": False,
            "receipt": receipt,
            "sent": sent,
            "relay_evidence": relay_refs,
            "relay_result": completed,
            "target_result": target_completed,
            "result": "pass",
        }
    finally:
        await _close_connection(connection)


def send_file_via_peer(
    store: RynmeshStore,
    path: str | Path,
    *,
    relay_peer_id: str,
    target_peer_id: str,
    network_id: str = "rynmesh-main",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise PeerTransitError(f"transit source file not found: {source}")
    if source.stat().st_size > _max_file_bytes():
        raise PeerTransitError("transit source file exceeds size policy")
    if relay_peer_id in {store.peer_id, target_peer_id}:
        raise PeerTransitError("transit peer must differ from source and target")
    return asyncio.run(
        _send_file_async(
            store,
            path=source,
            relay_peer_id=relay_peer_id,
            target_peer_id=target_peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    )


async def _send_file_direct_async(
    store: RynmeshStore,
    *,
    path: Path,
    target_peer_id: str,
    network_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    target_capacity = _find_capacity(
        store,
        peer_id=target_peer_id,
        role="target",
        network_id=network_id,
    )
    target_messaging_pub = str(
        (target_capacity.get("metadata") or {}).get("messaging_public_key") or ""
    )
    if not target_messaging_pub:
        raise PeerTransitError("target capacity is missing its messaging public key")
    connection = new_connection(controlling=True)
    try:
        offer = await gather_signal(connection)
        session_id = new_session_id()
        cipher, ephemeral = TransitCipher.for_source(
            session_id=session_id,
            source_peer_id=store.peer_id,
            target_peer_id=target_peer_id,
            target_messaging_pub=target_messaging_pub,
        )
        session = TransitSessionOpen(
            session_id=session_id,
            source_peer_id=store.peer_id,
            target_peer_id=target_peer_id,
            source_ephemeral_pub=messaging_public_key(ephemeral),
            expires_at=_expires(timeout_s * 2),
        )
        signed_open = sign_session_open(session, source_signing_key=store.private_key_bytes)
        submitted = store.submit_work_order(
            provider_peer_id=target_peer_id,
            capability=TRANSIT_CAPABILITY,
            operation=DIRECT_OPERATION,
            params={
                "source_ice_offer": offer.to_dict(),
                "signed_session_open": signed_open.to_dict(),
            },
            network_id=network_id,
            expires_in_hours=max(timeout_s / 3600.0, 0.01),
        )
        work_order_id = str(submitted["order"]["work_order_id"])
        accepted = await asyncio.to_thread(
            _poll_result,
            store,
            work_order_id=work_order_id,
            network_id=network_id,
            expected_provider_peer_id=target_peer_id,
            expected_requester_peer_id=store.peer_id,
            wanted_status="accepted",
            timeout_s=timeout_s,
        )
        accepted_refs = dict(accepted.get("result_refs") or {})
        if accepted_refs.get("path_mode") != "direct":
            raise PeerTransitError("target direct answer has an invalid path mode")
        answer = IceSignal.from_dict(dict(accepted_refs.get("source_ice_answer") or {}))
        validate_distinct_public_egress(offer, answer)
        await apply_remote_signal(connection, answer)
        await asyncio.wait_for(connection.connect(), timeout_s)
        source_hop = selected_pair(connection)
        validate_ice_hop(source_hop)

        source_hash = _file_sha256(path)
        manifest = {
            "kind": "file",
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": source_hash,
        }
        sent = await send_encrypted_stream(
            connection,
            cipher,
            direction="request",
            chunks=_file_chunks(path, manifest, DEFAULT_CHUNK_BYTES),
            timeout_s=timeout_s,
        )
        response = bytearray()
        await receive_encrypted_stream(
            connection,
            cipher,
            direction="response",
            sink=response.extend,
            timeout_s=timeout_s,
        )
        try:
            receipt = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PeerTransitError("target returned an invalid encrypted direct receipt") from exc
        if not isinstance(receipt, dict) or receipt.get("sha256") != source_hash:
            raise PeerTransitError("direct target receipt hash does not match source")
        completed = await asyncio.to_thread(
            _poll_result,
            store,
            work_order_id=work_order_id,
            network_id=network_id,
            expected_provider_peer_id=target_peer_id,
            expected_requester_peer_id=store.peer_id,
            wanted_status="completed",
            timeout_s=timeout_s,
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "source_peer_id": store.peer_id,
            "target_peer_id": target_peer_id,
            "session_id": session_id,
            "path_mode": "direct",
            "ice_relay_candidate_used": False,
            "source_hop": source_hop,
            "source_sha256": source_hash,
            "target_sha256": str(receipt["sha256"]),
            "source_size_bytes": path.stat().st_size,
            "target_size_bytes": int(receipt.get("size_bytes") or 0),
            "registry_payload_bytes": 0,
            "receipt": receipt,
            "sent": sent,
            "target_result": completed,
            "result": "pass",
        }
    finally:
        await _close_connection(connection)


def send_file_direct(
    store: RynmeshStore,
    path: str | Path,
    *,
    target_peer_id: str,
    network_id: str = "rynmesh-main",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise PeerTransitError(f"direct source file not found: {source}")
    if source.stat().st_size > _max_file_bytes():
        raise PeerTransitError("direct source file exceeds size policy")
    if target_peer_id == store.peer_id:
        raise PeerTransitError("direct target must differ from source")
    return asyncio.run(
        _send_file_direct_async(
            store,
            path=source,
            target_peer_id=target_peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    )


def send_file_adaptive(
    store: RynmeshStore,
    path: str | Path,
    *,
    target_peer_id: str,
    relay_peer_id: str,
    direct_metrics: PathMetrics | None = None,
    transit_metrics: PathMetrics | None = None,
    route_manager: RouteManager | None = None,
    network_id: str = "rynmesh-main",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    direct_attempt_timeout_s: float = DEFAULT_DIRECT_ATTEMPT_TIMEOUT_S,
) -> dict[str, Any]:
    """Prefer direct P2P and automatically fall back to an ordinary peer.

    Quality monitors can pass rolling metrics to make a proactive decision.
    Without metrics the client tries direct first.  A hard direct failure is
    recorded in the route state machine and retried through ``relay_peer_id``.
    """

    if direct_attempt_timeout_s <= 0 or direct_attempt_timeout_s > 10:
        raise PeerTransitError("direct attempt timeout must be within (0, 10] seconds")
    manager = route_manager or RouteManager()
    direct = direct_metrics or PathMetrics(True, 0, 0)
    transit = transit_metrics or PathMetrics(True, 0, 0)
    selected = manager.update(direct=direct, transit=transit)
    fallback_error = ""
    if selected == "direct":
        try:
            result = send_file_direct(
                store,
                path,
                target_peer_id=target_peer_id,
                network_id=network_id,
                timeout_s=min(timeout_s, direct_attempt_timeout_s),
            )
        except Exception as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"
            manager.update(
                direct=PathMetrics(False, 0, 1, consecutive_failures=3),
                transit=transit,
            )
            result = send_file_via_peer(
                store,
                path,
                relay_peer_id=relay_peer_id,
                target_peer_id=target_peer_id,
                network_id=network_id,
                timeout_s=timeout_s,
            )
    else:
        result = send_file_via_peer(
            store,
            path,
            relay_peer_id=relay_peer_id,
            target_peer_id=target_peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    result["route_events"] = list(manager.events)
    result["direct_fallback_error"] = fallback_error
    result["direct_attempt_timeout_s"] = min(timeout_s, direct_attempt_timeout_s)
    result["selected_path"] = result.get("path_mode")
    return result


def _max_file_bytes() -> int:
    return max(1, int(os.environ.get("RYNMESH_TRANSIT_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Rynmesh ordinary-peer P2P transit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="advertise and serve target/transit orders")
    worker.add_argument("--role", choices=("target", "transit", "both"), default="both")
    worker.add_argument(
        "--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
    )
    worker.add_argument("--inbox", default="")
    worker.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    worker.add_argument("--deny-direct", action="store_true")

    send = subparsers.add_parser("send-file", help="send a file through another P2P peer")
    send.add_argument("path")
    send.add_argument("--relay-peer", required=True)
    send.add_argument("--target-peer", required=True)
    send.add_argument("--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"))
    send.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    send.add_argument("--evidence", default="")
    direct = subparsers.add_parser("send-file-direct", help="send a file over direct P2P")
    direct.add_argument("path")
    direct.add_argument("--target-peer", required=True)
    direct.add_argument(
        "--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
    )
    direct.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    direct.add_argument("--evidence", default="")
    adaptive = subparsers.add_parser(
        "send-file-adaptive",
        help="try direct P2P and automatically use an ordinary transit peer",
    )
    adaptive.add_argument("path")
    adaptive.add_argument("--relay-peer", required=True)
    adaptive.add_argument("--target-peer", required=True)
    adaptive.add_argument(
        "--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
    )
    adaptive.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    adaptive.add_argument(
        "--direct-timeout",
        type=float,
        default=DEFAULT_DIRECT_ATTEMPT_TIMEOUT_S,
        help="maximum direct-path attempt timeout before peer transit (max 10s)",
    )
    adaptive.add_argument("--evidence", default="")
    args = parser.parse_args()

    store = RynmeshStore()
    if args.command == "worker":
        PeerTransitWorker(
            store,
            role=args.role,
            network_id=args.network_id,
            inbox=args.inbox or None,
            timeout_s=args.timeout,
            allow_direct=not args.deny_direct,
        ).serve_forever()
        return 0
    if args.command == "send-file-direct":
        evidence = send_file_direct(
            store,
            args.path,
            target_peer_id=args.target_peer,
            network_id=args.network_id,
            timeout_s=args.timeout,
        )
    elif args.command == "send-file-adaptive":
        evidence = send_file_adaptive(
            store,
            args.path,
            relay_peer_id=args.relay_peer,
            target_peer_id=args.target_peer,
            network_id=args.network_id,
            timeout_s=args.timeout,
            direct_attempt_timeout_s=args.direct_timeout,
        )
    else:
        evidence = send_file_via_peer(
            store,
            args.path,
            relay_peer_id=args.relay_peer,
            target_peer_id=args.target_peer,
            network_id=args.network_id,
            timeout_s=args.timeout,
        )
    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    if args.evidence:
        Path(args.evidence).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
