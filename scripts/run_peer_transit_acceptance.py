#!/usr/bin/env python3
"""Run deterministic three-node peer-transit acceptance on real local ICE pairs.

This is the hermetic gate.  It proves protocol behavior, identity continuity,
two separate non-TURN ICE legs, ciphertext-only forwarding, streaming hashes,
route hysteresis, bounded relay unavailability and concurrent callers.  Public
NAT acceptance still requires running the same workers on three physical
networks with STUN enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import rynmesh.peer_transit_service as peer_transit_service
from rynmesh.llm_package.p2p import (
    _close_connection,
    apply_remote_signal,
    gather_signal,
    new_connection,
    selected_pair,
)
from rynmesh.peer_transit import PathMetrics, RouteManager, RoutePolicy, validate_ice_hop
from rynmesh.peer_transit_service import (
    PeerTransitError,
    PeerTransitWorker,
    advertise_transit_capacity,
    send_file_adaptive,
    send_file_direct,
    send_file_via_peer,
)
from rynmesh.registry import FilePeerRegistry
from rynmesh.store import RynmeshStore

try:
    from scripts.audit_peer_transit import audit_acceptance_report, audit_peer_transit
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from audit_peer_transit import audit_acceptance_report, audit_peer_transit

MARKER = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"


class _FrameAudit:
    def __init__(self) -> None:
        self.frames = 0
        self.bytes = 0
        self.max_frame_bytes = 0
        self.plaintext_found = False

    def __call__(self, frame: bytes) -> None:
        self.frames += 1
        self.bytes += len(frame)
        self.max_frame_bytes = max(self.max_frame_bytes, len(frame))
        if MARKER in frame:
            self.plaintext_found = True


class _RegistryBlackout:
    """Fail registry calls while preserving the same underlying registry."""

    def __init__(self, registry: FilePeerRegistry) -> None:
        self._registry = registry
        self.blackout = threading.Event()
        self.two_hops_ready = threading.Event()
        self.blocked_calls = 0

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._registry, name)
        if not callable(attribute):
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.blackout.is_set():
                self.blocked_calls += 1
                raise OSError("acceptance control-plane blackout")
            result = attribute(*args, **kwargs)
            signed_payload = dict(getattr(args[0], "payload", {}) or {}) if args else {}
            result_refs = dict(signed_payload.get("result_refs") or {})
            if (
                name == "publish_work_result"
                and signed_payload.get("status") == "running"
                and result_refs.get("path_mode") == "peer_transit"
            ):
                self.two_hops_ready.set()
            return result

        return guarded


def _write_payload(path: Path, size_bytes: int, *, marker: bytes = MARKER) -> None:
    block = (b"rynmesh-peer-transit-acceptance-" * 2048)[:64 * 1024]
    remaining = max(0, size_bytes - len(marker))
    with path.open("wb") as handle:
        handle.write(marker[:size_bytes])
        while remaining > 0:
            chunk = block[:remaining]
            handle.write(chunk)
            remaining -= len(chunk)


async def _direct_probe() -> dict[str, Any]:
    source = new_connection(controlling=True)
    target = new_connection(controlling=False)
    started = time.monotonic()
    try:
        source_signal, target_signal = await asyncio.gather(
            gather_signal(source),
            gather_signal(target),
        )
        await asyncio.gather(
            apply_remote_signal(source, target_signal),
            apply_remote_signal(target, source_signal),
        )
        await asyncio.gather(source.connect(), target.connect())
        source_evidence = selected_pair(source)
        target_evidence = selected_pair(target)
        validate_ice_hop(source_evidence)
        validate_ice_hop(target_evidence)
        return {
            "ok": True,
            "elapsed_s": time.monotonic() - started,
            "source": source_evidence,
            "target": target_evidence,
            "ice_relay_candidate_used": False,
        }
    finally:
        await asyncio.gather(_close_connection(source), _close_connection(target))


def _route_acceptance() -> dict[str, Any]:
    policy = RoutePolicy(
        degraded_hold_s=30,
        transit_min_hold_s=60,
        recovery_hold_s=120,
        recovery_probe_count=5,
    )
    manager = RouteManager(policy)
    healthy = PathMetrics(True, 40, 0)
    degraded = PathMetrics(True, 330, 0.18)
    hard_failed = PathMetrics(False, 0, 1, consecutive_failures=3)
    transit = PathMetrics(True, 80, 0.01)
    manager.update(direct=healthy, transit=transit, now_monotonic=0)
    manager.update(direct=degraded, transit=transit, now_monotonic=1)
    manager.update(direct=degraded, transit=transit, now_monotonic=31)
    degraded_path = manager.path_mode
    recovery_probe_times = [92.0, 150.0, 180.0, 211.0, 212.0]
    for probe_at in recovery_probe_times:
        manager.update(direct=healthy, transit=transit, now_monotonic=probe_at)
    recovered_path = manager.path_mode

    hard_manager = RouteManager()
    started = time.monotonic()
    hard_path = hard_manager.update(
        direct=hard_failed,
        transit=transit,
        now_monotonic=started,
    )
    hard_elapsed = time.monotonic() - started
    ok = (
        degraded_path == "peer_transit"
        and recovered_path == "direct"
        and hard_path == "peer_transit"
        and hard_elapsed < 10
    )
    return {
        "ok": ok,
        "degraded_path": degraded_path,
        "recovered_path": recovered_path,
        "hard_failure_path": hard_path,
        "hard_failure_switch_s": hard_elapsed,
        "events": manager.events,
        "hard_failure_events": hard_manager.events,
        "policy": {
            "hard_failure_count": policy.hard_failure_count,
            "loss_threshold": policy.loss_threshold,
            "latency_threshold_ms": policy.latency_threshold_ms,
            "transit_improvement_ratio": policy.transit_improvement_ratio,
            "degraded_hold_s": policy.degraded_hold_s,
            "transit_min_hold_s": policy.transit_min_hold_s,
            "recovery_hold_s": policy.recovery_hold_s,
            "recovery_probe_count": policy.recovery_probe_count,
        },
        "healthy_direct_metrics": {
            "reachable": healthy.reachable,
            "rtt_p95_ms": healthy.rtt_p95_ms,
            "loss_ratio": healthy.loss_ratio,
            "consecutive_failures": healthy.consecutive_failures,
        },
        "degraded_direct_metrics": {
            "reachable": degraded.reachable,
            "rtt_p95_ms": degraded.rtt_p95_ms,
            "loss_ratio": degraded.loss_ratio,
            "consecutive_failures": degraded.consecutive_failures,
        },
        "transit_metrics": {
            "reachable": transit.reachable,
            "rtt_p95_ms": transit.rtt_p95_ms,
            "loss_ratio": transit.loss_ratio,
            "consecutive_failures": transit.consecutive_failures,
        },
        "recovery_probe_times": recovery_probe_times,
    }


def _relay_unavailable_acceptance(root: Path) -> dict[str, Any]:
    registry = FilePeerRegistry(root / "failure-registry")
    source = RynmeshStore(home=root / "failure-source", network_dir=root / "failure-source-net")
    relay = RynmeshStore(home=root / "failure-relay", network_dir=root / "failure-relay-net")
    target = RynmeshStore(home=root / "failure-target", network_dir=root / "failure-target-net")
    for store in (source, relay, target):
        store.registry = registry
    network_id = "peer-transit-unavailable"
    advertise_transit_capacity(relay, network_id=network_id, roles=("transit",))
    advertise_transit_capacity(target, network_id=network_id, roles=("target",))
    payload = root / "failure-payload.bin"
    _write_payload(payload, 4096)
    started = time.monotonic()
    error = ""
    try:
        send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=0.5,
        )
    except PeerTransitError as exc:
        error = str(exc)
    elapsed = time.monotonic() - started
    return {
        "ok": bool(error) and elapsed < 5,
        "elapsed_s": elapsed,
        "error": error,
        "partial_target_files": len(list((target.home / "transit-inbox").glob("*"))),
    }


def _control_plane_blackout_acceptance(root: Path, *, timeout_s: float) -> dict[str, Any]:
    """Prove an established two-hop data plane survives registry/STUN isolation."""

    registry = _RegistryBlackout(FilePeerRegistry(root / "blackout-registry"))
    source = RynmeshStore(home=root / "blackout-source", network_dir=root / "blackout-source-net")
    relay = RynmeshStore(home=root / "blackout-relay", network_dir=root / "blackout-relay-net")
    target = RynmeshStore(home=root / "blackout-target", network_dir=root / "blackout-target-net")
    for store in (source, relay, target):
        store.registry = registry
    network_id = "peer-transit-control-plane-blackout"
    inbox = root / "blackout-target-inbox"
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=timeout_s,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=inbox,
        timeout_s=timeout_s,
    )
    relay_worker.register()
    target_worker.register()
    stop = threading.Event()
    workers = [
        threading.Thread(
            target=relay_worker.serve_forever,
            kwargs={"poll_interval_s": 0.02, "stop_event": stop},
            daemon=True,
        ),
        threading.Thread(
            target=target_worker.serve_forever,
            kwargs={"poll_interval_s": 0.02, "stop_event": stop},
            daemon=True,
        ),
    ]
    payload = root / "control-plane-blackout.bin"
    _write_payload(payload, 4 * 1024 * 1024)
    original_send = peer_transit_service.send_encrypted_stream
    original_receive = peer_transit_service.receive_encrypted_stream
    state: dict[str, Any] = {
        "registry_probe_blocked": False,
        "request_completed_during_blackout": False,
        "blackout_started": 0.0,
        "blackout_ended": 0.0,
    }

    async def send_with_blackout(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("direction") == "request" and not registry.blackout.is_set():
            if not registry.two_hops_ready.wait(timeout=timeout_s):
                raise PeerTransitError("two ICE hops were not ready before control-plane blackout")
            registry.blackout.set()
            state["blackout_started"] = time.monotonic()
            try:
                registry.list_job_capacities(
                    network_id=network_id,
                    capability=peer_transit_service.TRANSIT_CAPABILITY,
                    max_age_hours=1,
                )
            except OSError:
                state["registry_probe_blocked"] = True
            else:
                raise PeerTransitError("registry remained reachable during blackout")
        return await original_send(*args, **kwargs)

    async def receive_with_blackout(*args: Any, **kwargs: Any) -> Any:
        result = await original_receive(*args, **kwargs)
        if kwargs.get("direction") == "request":
            state["request_completed_during_blackout"] = registry.blackout.is_set()
            state["blackout_ended"] = time.monotonic()
            registry.blackout.clear()
        return result

    peer_transit_service.send_encrypted_stream = send_with_blackout
    peer_transit_service.receive_encrypted_stream = receive_with_blackout
    for worker in workers:
        worker.start()
    evidence: dict[str, Any] = {}
    error = ""
    try:
        evidence = send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - acceptance records the exact failure
        error = f"{type(exc).__name__}: {exc}"
    finally:
        registry.blackout.clear()
        peer_transit_service.send_encrypted_stream = original_send
        peer_transit_service.receive_encrypted_stream = original_receive
        stop.set()
        for worker in workers:
            worker.join(timeout=5)

    blackout_elapsed = max(
        0.0,
        float(state["blackout_ended"]) - float(state["blackout_started"]),
    )
    partial_files = len(list(inbox.glob("*.part")))
    delivered = list(inbox.glob("*-control-plane-blackout.bin"))
    workers_stopped = all(not worker.is_alive() for worker in workers)
    ok = (
        not error
        and state["registry_probe_blocked"] is True
        and state["request_completed_during_blackout"] is True
        and blackout_elapsed > 0
        and registry.blocked_calls >= 1
        and evidence.get("source_sha256") == evidence.get("target_sha256")
        and evidence.get("ice_relay_candidate_used") is False
        and len(delivered) == 1
        and partial_files == 0
        and workers_stopped
    )
    return {
        "ok": ok,
        "error": error,
        "registry_probe_blocked": state["registry_probe_blocked"],
        "registry_blocked_calls": registry.blocked_calls,
        "request_completed_during_blackout": state["request_completed_during_blackout"],
        "blackout_elapsed_s": blackout_elapsed,
        "stun_disabled": os.environ.get("RYNMESH_P2P_STUN", "").lower() == "off",
        "source_sha256": evidence.get("source_sha256"),
        "target_sha256": evidence.get("target_sha256"),
        "ice_relay_candidate_used": evidence.get("ice_relay_candidate_used"),
        "payload_size_bytes": payload.stat().st_size,
        "target_files": len(delivered),
        "partial_target_files": partial_files,
        "worker_threads_stopped": workers_stopped,
    }


def run_acceptance(
    *,
    size_bytes: int,
    concurrent_sessions: int,
    timeout_s: float,
    work_root: Path,
) -> dict[str, Any]:
    # Hermetic acceptance uses real host ICE candidates and never contacts an
    # external STUN service.  A physical three-network run overrides this with
    # its chosen STUN endpoint to prove server-reflexive mappings.
    os.environ.setdefault("RYNMESH_P2P_STUN", "off")
    work_root.mkdir(parents=True, exist_ok=True)
    registry_root = work_root / "registry"
    registry = FilePeerRegistry(registry_root)
    source = RynmeshStore(home=work_root / "source", network_dir=work_root / "source-net")
    relay = RynmeshStore(home=work_root / "relay", network_dir=work_root / "relay-net")
    target = RynmeshStore(home=work_root / "target", network_dir=work_root / "target-net")
    for store in (source, relay, target):
        store.registry = registry
    network_id = "peer-transit-acceptance"
    frame_audit = _FrameAudit()
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=timeout_s,
        max_concurrent=max(8, concurrent_sessions),
        audit_frame=frame_audit,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=work_root / "target-inbox",
        timeout_s=timeout_s,
        max_concurrent=max(8, concurrent_sessions),
        allow_direct=True,
    )
    relay_worker.register()
    target_worker.register()
    stop = threading.Event()
    relay_thread = threading.Thread(
        target=relay_worker.serve_forever,
        kwargs={"poll_interval_s": 0.02, "stop_event": stop},
        daemon=True,
    )
    target_thread = threading.Thread(
        target=target_worker.serve_forever,
        kwargs={"poll_interval_s": 0.02, "stop_event": stop},
        daemon=True,
    )
    relay_thread.start()
    target_thread.start()

    payload = work_root / "acceptance-payload.bin"
    _write_payload(payload, size_bytes)
    tracemalloc.start()
    started = time.monotonic()
    try:
        evidence = send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
        main_elapsed = time.monotonic() - started
        _current, peak_memory = tracemalloc.get_traced_memory()
        evidence["plaintext_found_on_transit"] = frame_audit.plaintext_found
        evidence["transit_frame_audit"] = {
            "frames": frame_audit.frames,
            "bytes": frame_audit.bytes,
            "max_frame_bytes": frame_audit.max_frame_bytes,
        }
        evidence["elapsed_s"] = main_elapsed
        evidence["peak_python_memory_bytes"] = peak_memory
        main_audit = audit_peer_transit(evidence)

        concurrency_files: list[Path] = []
        for index in range(concurrent_sessions):
            item = work_root / f"concurrent-{index:02d}.bin"
            _write_payload(item, 64 * 1024, marker=MARKER + str(index).encode())
            concurrency_files.append(item)
        concurrency_started = time.monotonic()
        concurrent_results: list[dict[str, Any]] = []
        if concurrency_files:
            with ThreadPoolExecutor(max_workers=concurrent_sessions) as pool:
                futures = [
                    pool.submit(
                        send_file_via_peer,
                        source,
                        item,
                        relay_peer_id=relay.peer_id,
                        target_peer_id=target.peer_id,
                        network_id=network_id,
                        timeout_s=timeout_s,
                    )
                    for item in concurrency_files
                ]
                concurrent_results = [future.result(timeout=timeout_s + 10) for future in futures]
        concurrency_elapsed = time.monotonic() - concurrency_started
        concurrency_ok = all(
            result.get("source_sha256") == result.get("target_sha256")
            and result.get("ice_relay_candidate_used") is False
            for result in concurrent_results
        )

        direct_file = work_root / "healthy-direct.bin"
        _write_payload(direct_file, 64 * 1024)
        transit_bytes_before_direct = frame_audit.bytes
        direct_file_started = time.monotonic()
        direct_file_evidence = send_file_direct(
            source,
            direct_file,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
        direct_file_elapsed = time.monotonic() - direct_file_started
        healthy_direct_file = {
            "ok": (
                direct_file_evidence.get("path_mode") == "direct"
                and direct_file_evidence.get("ice_relay_candidate_used") is False
                and direct_file_evidence.get("source_sha256")
                == direct_file_evidence.get("target_sha256")
                and frame_audit.bytes == transit_bytes_before_direct
            ),
            "elapsed_s": direct_file_elapsed,
            "source_sha256": direct_file_evidence.get("source_sha256"),
            "target_sha256": direct_file_evidence.get("target_sha256"),
            "transit_bytes_before": transit_bytes_before_direct,
            "transit_bytes_after": frame_audit.bytes,
            "source_hop": direct_file_evidence.get("source_hop"),
        }

        hard_failure_file = work_root / "hard-failure-fallback.bin"
        _write_payload(hard_failure_file, 64 * 1024)
        target_worker.allow_direct = False
        hard_failure_started = time.monotonic()
        hard_failure_evidence = send_file_adaptive(
            source,
            hard_failure_file,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
            direct_attempt_timeout_s=min(8.0, timeout_s),
        )
        hard_failure_elapsed = time.monotonic() - hard_failure_started
        audit_peer_transit(hard_failure_evidence)
        actual_hard_failure = {
            "ok": (
                hard_failure_evidence.get("path_mode") == "peer_transit"
                and bool(hard_failure_evidence.get("direct_fallback_error"))
                and hard_failure_elapsed <= 10
            ),
            "elapsed_s": hard_failure_elapsed,
            "selected_path": hard_failure_evidence.get("path_mode"),
            "direct_fallback_error": hard_failure_evidence.get("direct_fallback_error"),
            "route_events": hard_failure_evidence.get("route_events"),
        }
    finally:
        tracemalloc.stop()
        stop.set()
        relay_thread.join(timeout=5)
        target_thread.join(timeout=5)

    registry_plaintext_found = any(
        MARKER in path.read_bytes()
        for path in registry_root.rglob("*.json")
    )
    direct = asyncio.run(_direct_probe())
    route = _route_acceptance()
    unavailable = _relay_unavailable_acceptance(work_root)
    control_plane_blackout = _control_plane_blackout_acceptance(
        work_root,
        timeout_s=timeout_s,
    )
    delivered = list((work_root / "target-inbox").glob("*-acceptance-payload.bin"))
    one_gib_required = size_bytes >= 1024 ** 3
    one_gib_ok = not one_gib_required or (
        evidence["source_size_bytes"] == 1024 ** 3
        and evidence["source_sha256"] == evidence["target_sha256"]
    )
    memory_bounded = evidence["peak_python_memory_bytes"] < max(128 * 1024 * 1024, size_bytes // 4)
    encrypted_request_bytes = int(evidence["sent"]["wire_bytes"])
    plaintext_request_bytes = int(evidence["sent"]["plaintext_bytes"])
    protocol_overhead_ratio = (
        (encrypted_request_bytes - plaintext_request_bytes) / plaintext_request_bytes
    )
    session_established_s = float(evidence["session_established_s"])
    performance = {
        "ok": (
            main_elapsed <= timeout_s
            and session_established_s <= 5
            and memory_bounded
            and one_gib_ok
            and concurrency_ok
            and protocol_overhead_ratio <= 0.15
            and actual_hard_failure["ok"]
        ),
        "main_elapsed_s": main_elapsed,
        "transfer_within_timeout": main_elapsed <= timeout_s,
        "session_established_s": session_established_s,
        "session_established_within_5s": session_established_s <= 5,
        "encrypted_request_bytes": encrypted_request_bytes,
        "plaintext_request_bytes": plaintext_request_bytes,
        "protocol_overhead_ratio": protocol_overhead_ratio,
        "protocol_overhead_within_15_percent": protocol_overhead_ratio <= 0.15,
        "hard_failure_fallback_within_10s": actual_hard_failure["ok"],
        "peak_python_memory_bytes": evidence["peak_python_memory_bytes"],
        "memory_bounded": memory_bounded,
        "one_gib_required": one_gib_required,
        "one_gib_ok": one_gib_ok,
        "concurrent_sessions": concurrent_sessions,
        "concurrent_completed": len(concurrent_results),
        "concurrent_elapsed_s": concurrency_elapsed,
        "concurrency_ok": concurrency_ok,
    }
    checks = {
        "healthy_direct": direct["ok"] and healthy_direct_file["ok"],
        "two_hop_peer_transit": main_audit["ok"],
        "automatic_degrade_and_recovery": route["ok"],
        "actual_hard_failure_fallback": actual_hard_failure["ok"],
        "bounded_transit_unavailability": unavailable["ok"],
        "established_data_plane_survives_control_plane_blackout": control_plane_blackout["ok"],
        "no_turn": evidence["ice_relay_candidate_used"] is False,
        "registry_has_no_payload_marker": not registry_plaintext_found,
        "transit_has_no_plaintext_marker": not frame_audit.plaintext_found,
        "target_hash_matches": evidence["source_sha256"] == evidence["target_sha256"],
        "target_file_committed": len(delivered) == 1,
        "performance": performance["ok"],
    }
    report = {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "main_evidence": evidence,
        "main_audit": main_audit,
        "direct": direct,
        "healthy_direct_file": healthy_direct_file,
        "route": route,
        "actual_hard_failure": actual_hard_failure,
        "unavailable": unavailable,
        "control_plane_blackout": control_plane_blackout,
        "performance": performance,
        "registry_plaintext_found": registry_plaintext_found,
        "work_root": str(work_root),
    }
    report["report_audit"] = audit_acceptance_report(
        report,
        require_one_gib=one_gib_required,
        min_concurrent=concurrent_sessions,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=8)
    parser.add_argument("--concurrent", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--work-root", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--evidence", default="")
    args = parser.parse_args()
    if args.size_mib < 1 or args.concurrent < 0:
        raise SystemExit("size-mib must be >= 1 and concurrent must be >= 0")
    root = (
        Path(args.work_root).expanduser()
        if args.work_root
        else Path(tempfile.mkdtemp(prefix="rynmesh-peer-transit-acceptance-"))
    )
    report = run_acceptance(
        size_bytes=args.size_mib * 1024 * 1024,
        concurrent_sessions=args.concurrent,
        timeout_s=args.timeout,
        work_root=root,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    if args.evidence:
        Path(args.evidence).write_text(
            json.dumps(report["main_evidence"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(rendered, end="")
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
