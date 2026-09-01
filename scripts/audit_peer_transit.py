#!/usr/bin/env python3
"""Fail-closed audit for three-node ordinary-peer P2P transit evidence."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from rynmesh.crypto import SignedPayload
from rynmesh.jobs import WorkResult, verify_work_result
from rynmesh.peer_transit import PROTOCOL_VERSION

MAX_REGISTRY_CONTROL_RECORD_BYTES = 64 * 1024


class AuditError(RuntimeError):
    pass


def _require(value: dict[str, Any], key: str) -> Any:
    if key not in value:
        raise AuditError(f"missing evidence field: {key}")
    return value[key]


def _finite_float(value: Any, label: str) -> float:
    """Parse an evidence number and reject NaN/Infinity fail-closed."""

    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuditError(f"{label} is not a finite number") from exc
    if not math.isfinite(parsed):
        raise AuditError(f"{label} is not a finite number")
    return parsed


def _audit_candidate(candidate: dict[str, Any], label: str) -> None:
    if str(candidate.get("transport") or "").lower() != "udp":
        raise AuditError(f"{label} is not UDP")
    candidate_type = str(candidate.get("type") or "")
    if candidate_type not in {"host", "srflx", "prflx"}:
        raise AuditError(f"{label} contains a non-direct ICE candidate: {candidate_type}")


def _audit_hop(hop: dict[str, Any], label: str) -> None:
    if hop.get("transport") != "ice_udp_direct" or hop.get("relay_used") is not False:
        raise AuditError(f"{label} did not prove a direct ICE/UDP pair")
    _audit_candidate(dict(hop.get("local") or {}), f"{label}.local")
    _audit_candidate(dict(hop.get("remote") or {}), f"{label}.remote")


def _path_score(metrics: dict[str, Any], label: str) -> float:
    if metrics.get("reachable") is not True:
        raise AuditError(f"{label} was not reachable")
    rtt = _finite_float(_require(metrics, "rtt_p95_ms"), f"{label}.rtt_p95_ms")
    loss = _finite_float(_require(metrics, "loss_ratio"), f"{label}.loss_ratio")
    failures = int(_require(metrics, "consecutive_failures"))
    if rtt < 0 or not 0 <= loss <= 1 or failures < 0:
        raise AuditError(f"{label} contains invalid path metrics")
    return rtt + 2000.0 * loss


def _audit_route_report(route: dict[str, Any]) -> None:
    if (
        route.get("ok") is not True
        or route.get("degraded_path") != "peer_transit"
        or route.get("recovered_path") != "direct"
        or route.get("hard_failure_path") != "peer_transit"
    ):
        raise AuditError("route degradation/recovery result is incomplete")

    policy = dict(_require(route, "policy"))
    degraded_hold = _finite_float(_require(policy, "degraded_hold_s"), "degraded_hold_s")
    transit_hold = _finite_float(_require(policy, "transit_min_hold_s"), "transit_min_hold_s")
    recovery_hold = _finite_float(_require(policy, "recovery_hold_s"), "recovery_hold_s")
    recovery_probe_count = int(_require(policy, "recovery_probe_count"))
    improvement = _finite_float(
        _require(policy, "transit_improvement_ratio"), "transit_improvement_ratio"
    )
    latency_threshold = _finite_float(
        _require(policy, "latency_threshold_ms"), "latency_threshold_ms"
    )
    loss_threshold = _finite_float(_require(policy, "loss_threshold"), "loss_threshold")
    if (
        degraded_hold < 0
        or degraded_hold > 30
        or transit_hold < 60
        or recovery_hold < 120
        or recovery_probe_count < 5
        or not 0.25 <= improvement <= 1
    ):
        raise AuditError("route policy does not meet hysteresis gates")

    healthy = dict(_require(route, "healthy_direct_metrics"))
    degraded = dict(_require(route, "degraded_direct_metrics"))
    transit = dict(_require(route, "transit_metrics"))
    healthy_score = _path_score(healthy, "healthy direct path")
    degraded_score = _path_score(degraded, "degraded direct path")
    transit_score = _path_score(transit, "transit path")
    degraded_jitter = _finite_float(
        _require(degraded, "jitter_ms"), "degraded_direct_metrics.jitter_ms"
    )
    if (
        _finite_float(healthy["rtt_p95_ms"], "healthy_direct_metrics.rtt_p95_ms")
        > latency_threshold
        or _finite_float(healthy["loss_ratio"], "healthy_direct_metrics.loss_ratio")
        > loss_threshold
        or not 250
        <= _finite_float(degraded["rtt_p95_ms"], "degraded_direct_metrics.rtt_p95_ms")
        <= 350
        or not 50 <= degraded_jitter <= 100
        or not 0.15
        <= _finite_float(degraded["loss_ratio"], "degraded_direct_metrics.loss_ratio")
        <= 0.20
        or transit_score > degraded_score * (1.0 - improvement)
        or healthy_score >= degraded_score
    ):
        raise AuditError("route quality metrics do not prove degradation and improvement")

    events = [dict(item) for item in _require(route, "events")]
    expected = [
        ("direct", "degraded", "direct_degraded"),
        ("degraded", "peer_transit", "transit_better"),
        ("peer_transit", "recovering", "direct_recovery_started"),
        ("recovering", "direct", "direct_recovered"),
    ]
    transitions = [
        (str(item.get("from") or ""), str(item.get("to") or ""), str(item.get("reason") or ""))
        for item in events
    ]
    if transitions != expected:
        raise AuditError("route transition sequence contains a gap or flap")
    event_times = [
        _finite_float(_require(item, "at"), f"route.events[{index}].at")
        for index, item in enumerate(events)
    ]
    if event_times != sorted(event_times):
        raise AuditError("route transition times are not monotonic")
    degraded_elapsed = event_times[1] - event_times[0]
    transit_elapsed = event_times[2] - event_times[1]
    recovery_elapsed = event_times[3] - event_times[2]
    if not degraded_hold <= degraded_elapsed <= 30.001:
        raise AuditError("degraded route did not switch within thirty seconds")
    if transit_elapsed < transit_hold:
        raise AuditError("transit minimum hold was not observed")
    if recovery_elapsed < recovery_hold:
        raise AuditError("direct recovery hold was not observed")

    recovery_probes = [
        _finite_float(item, f"recovery_probe_times[{index}]")
        for index, item in enumerate(_require(route, "recovery_probe_times"))
    ]
    if (
        len(recovery_probes) < recovery_probe_count
        or recovery_probes != sorted(recovery_probes)
        or recovery_probes[0] < event_times[2]
        or recovery_probes[-1] > event_times[3]
    ):
        raise AuditError("direct recovery probes are incomplete")

    hard_events = [dict(item) for item in _require(route, "hard_failure_events")]
    if [str(item.get("reason") or "") for item in hard_events] != [
        "direct_degraded",
        "hard_failure",
    ]:
        raise AuditError("hard-failure route transition evidence is incomplete")
    hard_switch = _finite_float(
        _require(route, "hard_failure_switch_s"), "hard_failure_switch_s"
    )
    if hard_switch < 0 or hard_switch > 10:
        raise AuditError("hard-failure route switch exceeded ten seconds")


def _audit_memory_gate(performance: dict[str, Any]) -> tuple[int, int]:
    peak_memory = int(_require(performance, "peak_python_memory_bytes"))
    peak_memory_limit = int(_require(performance, "peak_python_memory_limit_bytes"))
    if (
        performance.get("memory_bounded") is not True
        or peak_memory < 0
        or peak_memory_limit <= 0
        or peak_memory_limit > 128 * 1024 * 1024
        or peak_memory > peak_memory_limit
    ):
        raise AuditError("streaming memory gate did not pass")
    return peak_memory, peak_memory_limit


def _audit_concurrent_sessions(
    performance: dict[str, Any],
    concurrent_evidence: Any,
    *,
    transit_audit: dict[str, Any],
    min_concurrent: int,
) -> set[str]:
    concurrent_completed = int(performance.get("concurrent_completed", 0))
    concurrent_requested = int(_require(performance, "concurrent_sessions"))
    if not isinstance(concurrent_evidence, list):
        raise AuditError("concurrent evidence must be a list")
    if (
        performance.get("concurrency_ok") is not True
        or concurrent_requested < min_concurrent
        or concurrent_completed != concurrent_requested
        or len(concurrent_evidence) != concurrent_completed
    ):
        raise AuditError("concurrent-session gate did not pass")
    concurrent_session_ids: set[str] = set()
    for item in concurrent_evidence:
        if not isinstance(item, dict):
            raise AuditError("concurrent session evidence is malformed")
        item_audit = audit_peer_transit(item)
        if (
            item_audit["source_peer_id"] != transit_audit["source_peer_id"]
            or item_audit["transit_peer_id"] != transit_audit["transit_peer_id"]
            or item_audit["target_peer_id"] != transit_audit["target_peer_id"]
        ):
            raise AuditError("concurrent session peer identity continuity failed")
        session_id = str(_require(item, "session_id"))
        if not session_id or session_id in concurrent_session_ids:
            raise AuditError("concurrent session identifiers are missing or duplicated")
        concurrent_session_ids.add(session_id)
    return concurrent_session_ids


def _audit_overhead_gate(performance: dict[str, Any]) -> float:
    plaintext_bytes = int(_require(performance, "plaintext_request_bytes"))
    encrypted_bytes = int(_require(performance, "encrypted_request_bytes"))
    reported_ratio = _finite_float(
        _require(performance, "protocol_overhead_ratio"), "protocol_overhead_ratio"
    )
    if plaintext_bytes <= 0 or encrypted_bytes < plaintext_bytes:
        raise AuditError("protocol byte counters are inconsistent")
    computed_ratio = (encrypted_bytes - plaintext_bytes) / plaintext_bytes
    if (
        abs(reported_ratio - computed_ratio) > 1e-12
        or computed_ratio > 0.15
        or performance.get("protocol_overhead_within_15_percent") is not True
    ):
        raise AuditError("protocol overhead exceeded fifteen percent")
    return computed_ratio


def _audit_unavailable_gate(unavailable: dict[str, Any]) -> None:
    elapsed = _finite_float(_require(unavailable, "elapsed_s"), "unavailable.elapsed_s")
    operation_timeout = _finite_float(
        _require(unavailable, "operation_timeout_s"), "unavailable.operation_timeout_s"
    )
    maximum_elapsed = _finite_float(
        _require(unavailable, "maximum_elapsed_s"), "unavailable.maximum_elapsed_s"
    )
    error = str(_require(unavailable, "error"))
    if (
        unavailable.get("ok") is not True
        or unavailable.get("relay_worker_started") is not True
        or unavailable.get("relay_worker_stopped_before_request") is not True
        or unavailable.get("advertised_capacity_remained") is not True
        or operation_timeout <= 0
        or maximum_elapsed < operation_timeout
        or maximum_elapsed > 5
        or elapsed < 0
        or elapsed > maximum_elapsed
        or "timed out" not in error.lower()
        or int(_require(unavailable, "committed_target_files")) != 0
        or int(_require(unavailable, "partial_target_files")) != 0
    ):
        raise AuditError("transit-unavailable handling is not bounded and atomic")


def _audit_registry_control_plane(control: dict[str, Any]) -> None:
    raw_sizes = _require(control, "record_sizes_bytes")
    if not isinstance(raw_sizes, list):
        raise AuditError("registry control record sizes must be a list")
    try:
        sizes = [int(item) for item in raw_sizes]
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuditError("registry control record sizes are invalid") from exc
    if not sizes or any(size <= 0 for size in sizes):
        raise AuditError("registry control record sizes are invalid")
    record_count = int(_require(control, "record_count"))
    max_record = int(_require(control, "max_record_bytes"))
    total_records = int(_require(control, "total_record_bytes"))
    configured_max = int(_require(control, "maximum_record_bytes"))
    if (
        record_count != len(sizes)
        or max_record != max(sizes)
        or total_records != sum(sizes)
        or configured_max != MAX_REGISTRY_CONTROL_RECORD_BYTES
        or max_record > configured_max
        or int(_require(control, "application_payload_bytes")) != 0
        or control.get("plaintext_marker_found") is not False
    ):
        raise AuditError("registry control-plane size or payload audit failed")


def _audit_post_recovery_direct_file(
    direct_file: dict[str, Any],
    *,
    transit_audit: dict[str, Any],
) -> dict[str, Any]:
    direct_file_audit = audit_direct_file(dict(_require(direct_file, "evidence")))
    if direct_file.get("ok") is not True:
        raise AuditError("healthy direct file transfer did not pass")
    if direct_file.get("route_recovered_path") != "direct":
        raise AuditError("post-recovery file transfer was not bound to the direct route")
    if direct_file.get("source_sha256") != direct_file.get("target_sha256"):
        raise AuditError("healthy direct file hashes do not match")
    if int(direct_file.get("transit_bytes_before", -1)) != int(
        direct_file.get("transit_bytes_after", -2)
    ):
        raise AuditError("transit peer carried bytes during healthy direct transfer")
    if (
        direct_file_audit["source_peer_id"] != transit_audit["source_peer_id"]
        or direct_file_audit["target_peer_id"] != transit_audit["target_peer_id"]
        or direct_file_audit["source_sha256"] != direct_file.get("source_sha256")
    ):
        raise AuditError("healthy direct evidence identity or hash continuity failed")
    _audit_hop(dict(direct_file.get("source_hop") or {}), "direct_file.source_hop")
    return direct_file_audit


def _verify_flat_result(value: dict[str, Any], *, expected_provider: str) -> WorkResult:
    fields = {
        "kind",
        "work_order_id",
        "provider_peer_id",
        "requester_peer_id",
        "status",
        "message",
        "result_content_ids",
        "result_refs",
        "credit_amount",
        "network_id",
        "created_at",
    }
    payload = {key: value[key] for key in fields if key in value}
    signed = SignedPayload(
        payload=payload,
        signature=str(value.get("signature") or ""),
        public_key=str(value.get("provider_peer_id") or ""),
    )
    try:
        result = verify_work_result(signed)
    except Exception as exc:
        raise AuditError("work-result signature verification failed") from exc
    if result.provider_peer_id != expected_provider:
        raise AuditError("work-result provider identity continuity failed")
    return result


def audit_peer_transit(value: dict[str, Any]) -> dict[str, Any]:
    if _require(value, "protocol_version") != PROTOCOL_VERSION:
        raise AuditError("unexpected peer-transit protocol version")
    source = str(_require(value, "source_peer_id"))
    transit = str(_require(value, "transit_peer_id"))
    target = str(_require(value, "target_peer_id"))
    if not source or not transit or not target or len({source, transit, target}) != 3:
        raise AuditError("source, transit and target must be three distinct peer identities")
    if _require(value, "path_mode") != "peer_transit":
        raise AuditError("evidence is not a peer-transit path")
    if _require(value, "ice_relay_candidate_used") is not False:
        raise AuditError("TURN/ICE relay usage is forbidden")
    if int(_require(value, "registry_payload_bytes")) != 0:
        raise AuditError("registry carried application payload bytes")
    if _require(value, "plaintext_found_on_transit") is not False:
        raise AuditError("transit confidentiality was not proven")

    relay_refs = dict(_require(value, "relay_evidence"))
    if relay_refs.get("path_mode") != "peer_transit":
        raise AuditError("relay result did not identify peer transit")
    if relay_refs.get("transit_peer_id") != transit:
        raise AuditError("relay evidence peer identity mismatch")
    if relay_refs.get("ice_relay_candidate_used") is not False:
        raise AuditError("relay result contains TURN usage")
    _audit_hop(dict(relay_refs.get("hop_1") or {}), "hop_1")
    _audit_hop(dict(relay_refs.get("hop_2") or {}), "hop_2")
    _audit_hop(dict(_require(value, "source_hop")), "source_hop")

    source_hash = str(_require(value, "source_sha256"))
    target_hash = str(_require(value, "target_sha256"))
    if not source_hash.startswith("sha256:") or source_hash != target_hash:
        raise AuditError("source and target hashes do not match")
    source_size = int(_require(value, "source_size_bytes"))
    target_size = int(_require(value, "target_size_bytes"))
    if source_size < 0 or source_size != target_size:
        raise AuditError("source and target sizes do not match")
    rx_bytes = int(_require(value, "transit_rx_bytes"))
    tx_bytes = int(_require(value, "transit_tx_bytes"))
    if rx_bytes < source_size or tx_bytes < source_size:
        raise AuditError("transit byte counters do not cover the source payload")
    if int(_require(value, "request_frames")) < 1 or int(_require(value, "response_frames")) < 1:
        raise AuditError("transit frame counters are incomplete")

    relay_result = _verify_flat_result(
        dict(_require(value, "relay_result")),
        expected_provider=transit,
    )
    target_result = _verify_flat_result(
        dict(_require(value, "target_result")),
        expected_provider=target,
    )
    signed_relay_refs = dict(relay_result.result_refs)
    target_refs = dict(target_result.result_refs)
    session_id = str(_require(value, "session_id"))
    if signed_relay_refs != relay_refs:
        raise AuditError("relay evidence does not match the signed relay result")
    if relay_result.status != "completed" or relay_result.requester_peer_id != source:
        raise AuditError("signed relay result has invalid lifecycle bindings")
    if target_result.status != "completed" or target_result.requester_peer_id != transit:
        raise AuditError("signed target result has invalid lifecycle bindings")
    if (
        str(relay_refs.get("protocol_version") or "") != PROTOCOL_VERSION
        or str(relay_refs.get("session_id") or "") != session_id
        or str(relay_refs.get("target_work_order_id") or "") != target_result.work_order_id
    ):
        raise AuditError("signed relay result session or target-order binding failed")
    if (
        str(target_refs.get("protocol_version") or "") != PROTOCOL_VERSION
        or target_refs.get("path_mode") != "peer_transit"
        or target_refs.get("ice_relay_candidate_used") is not False
        or str(target_refs.get("sha256") or "") != source_hash
        or int(target_refs.get("size_bytes", -1)) != source_size
        or str(target_refs.get("session_id") or "") != session_id
    ):
        raise AuditError("signed target result does not bind the transit artifact")
    _audit_hop(dict(target_refs.get("hop") or {}), "target_hop")
    receipt = dict(_require(value, "receipt"))
    if (
        str(receipt.get("session_id") or "") != session_id
        or str(receipt.get("sha256") or "") != source_hash
        or int(receipt.get("size_bytes", -1)) != source_size
        or receipt.get("status") != "stored"
    ):
        raise AuditError("encrypted target receipt does not match signed evidence")
    if value.get("result") != "pass":
        raise AuditError("producer did not mark the evidence as passing")

    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "source_peer_id": source,
        "transit_peer_id": transit,
        "target_peer_id": target,
        "source_sha256": source_hash,
        "source_size_bytes": source_size,
        "transit_rx_bytes": rx_bytes,
        "transit_tx_bytes": tx_bytes,
        "ice_relay_candidate_used": False,
    }


def audit_direct_file(value: dict[str, Any]) -> dict[str, Any]:
    if _require(value, "protocol_version") != PROTOCOL_VERSION:
        raise AuditError("unexpected direct-file protocol version")
    source = str(_require(value, "source_peer_id"))
    target = str(_require(value, "target_peer_id"))
    if not source or not target or source == target:
        raise AuditError("direct source and target identities are invalid")
    if _require(value, "path_mode") != "direct":
        raise AuditError("direct evidence did not select the direct path")
    if _require(value, "ice_relay_candidate_used") is not False:
        raise AuditError("direct evidence contains TURN/ICE relay usage")
    if int(_require(value, "registry_payload_bytes")) != 0:
        raise AuditError("registry carried direct application payload bytes")
    _audit_hop(dict(_require(value, "source_hop")), "direct.source_hop")

    source_hash = str(_require(value, "source_sha256"))
    target_hash = str(_require(value, "target_sha256"))
    source_size = int(_require(value, "source_size_bytes"))
    target_size = int(_require(value, "target_size_bytes"))
    if (
        not source_hash.startswith("sha256:")
        or source_hash != target_hash
        or source_size < 0
        or source_size != target_size
    ):
        raise AuditError("direct source and target artifact evidence does not match")

    target_result = _verify_flat_result(
        dict(_require(value, "target_result")),
        expected_provider=target,
    )
    target_refs = dict(target_result.result_refs)
    if (
        target_result.status != "completed"
        or target_result.requester_peer_id != source
        or target_refs.get("path_mode") != "direct"
        or target_refs.get("ice_relay_candidate_used") is not False
        or str(target_refs.get("session_id") or "") != str(_require(value, "session_id"))
        or str(target_refs.get("sha256") or "") != source_hash
        or int(target_refs.get("size_bytes", -1)) != source_size
    ):
        raise AuditError("signed direct target result is inconsistent")
    _audit_hop(dict(target_refs.get("hop") or {}), "direct.target_hop")
    if value.get("result") != "pass":
        raise AuditError("direct producer did not mark the evidence as passing")
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "source_peer_id": source,
        "target_peer_id": target,
        "source_sha256": source_hash,
        "source_size_bytes": source_size,
        "ice_relay_candidate_used": False,
    }


def audit_acceptance_report(
    value: dict[str, Any],
    *,
    require_one_gib: bool = False,
    min_concurrent: int = 1,
) -> dict[str, Any]:
    """Independently enforce the complete hermetic acceptance report."""

    if value.get("result") != "pass":
        raise AuditError("acceptance producer did not report pass")
    required_checks = {
        "healthy_direct",
        "two_hop_peer_transit",
        "automatic_degrade_and_recovery",
        "actual_hard_failure_fallback",
        "bounded_transit_unavailability",
        "established_data_plane_survives_control_plane_blackout",
        "no_turn",
        "registry_has_no_payload_marker",
        "registry_control_sized",
        "transit_has_no_plaintext_marker",
        "target_hash_matches",
        "target_file_committed",
        "performance",
    }
    checks = dict(_require(value, "checks"))
    failed_checks = sorted(key for key in required_checks if checks.get(key) is not True)
    if failed_checks:
        raise AuditError(f"acceptance checks missing or failed: {', '.join(failed_checks)}")

    main = dict(_require(value, "main_evidence"))
    transit_audit = audit_peer_transit(main)
    if value.get("registry_plaintext_found") is not False:
        raise AuditError("registry plaintext scan did not pass")
    _audit_registry_control_plane(dict(_require(value, "registry_control_plane")))

    direct_socket = dict(_require(value, "direct"))
    if direct_socket.get("ok") is not True or direct_socket.get("ice_relay_candidate_used") is not False:
        raise AuditError("healthy direct ICE probe did not pass")
    _audit_hop(dict(direct_socket.get("source") or {}), "direct.source")
    _audit_hop(dict(direct_socket.get("target") or {}), "direct.target")

    direct_file = dict(_require(value, "healthy_direct_file"))
    _audit_post_recovery_direct_file(direct_file, transit_audit=transit_audit)

    hard_failure = dict(_require(value, "actual_hard_failure"))
    hard_failure_evidence = dict(_require(hard_failure, "evidence"))
    hard_failure_audit = audit_peer_transit(hard_failure_evidence)
    hard_failure_elapsed = _finite_float(
        hard_failure.get("elapsed_s", 999), "actual_hard_failure.elapsed_s"
    )
    direct_attempt_timeout = _finite_float(
        hard_failure_evidence.get("direct_attempt_timeout_s", 999),
        "actual_hard_failure.evidence.direct_attempt_timeout_s",
    )
    if (
        hard_failure.get("ok") is not True
        or hard_failure.get("selected_path") != "peer_transit"
        or not str(hard_failure.get("direct_fallback_error") or "")
        or hard_failure_elapsed < 0
        or hard_failure_elapsed > 10
        or hard_failure_evidence.get("path_mode") != "peer_transit"
        or hard_failure_evidence.get("selected_path") != "peer_transit"
        or hard_failure_evidence.get("direct_fallback_error")
        != hard_failure.get("direct_fallback_error")
        or direct_attempt_timeout <= 0
        or direct_attempt_timeout > 8
        or hard_failure_audit["source_peer_id"] != transit_audit["source_peer_id"]
        or hard_failure_audit["transit_peer_id"] != transit_audit["transit_peer_id"]
        or hard_failure_audit["target_peer_id"] != transit_audit["target_peer_id"]
    ):
        raise AuditError("real direct-failure fallback did not meet the ten-second gate")
    hard_reasons = [
        str(item.get("reason") or "")
        for item in hard_failure_evidence.get("route_events", [])
    ]
    if hard_reasons != ["direct_degraded", "hard_failure"]:
        raise AuditError("real direct-failure route evidence is incomplete")

    route = dict(_require(value, "route"))
    _audit_route_report(route)

    unavailable = dict(_require(value, "unavailable"))
    _audit_unavailable_gate(unavailable)

    blackout = dict(_require(value, "control_plane_blackout"))
    blackout_evidence = dict(_require(blackout, "evidence"))
    blackout_audit = audit_peer_transit(blackout_evidence)
    blackout_elapsed = _finite_float(
        blackout.get("blackout_elapsed_s", 0), "control_plane_blackout.blackout_elapsed_s"
    )
    blackout_timeout = _finite_float(
        _require(blackout, "transfer_timeout_s"), "control_plane_blackout.transfer_timeout_s"
    )
    if (
        blackout.get("ok") is not True
        or blackout.get("registry_probe_blocked") is not True
        or int(blackout.get("registry_blocked_calls", 0)) < 1
        or blackout.get("request_completed_during_blackout") is not True
        or blackout_elapsed <= 0
        or blackout_timeout <= 0
        or blackout_elapsed > blackout_timeout
        or blackout.get("stun_disabled") is not True
        or blackout.get("source_sha256") != blackout.get("target_sha256")
        or blackout.get("ice_relay_candidate_used") is not False
        or int(blackout.get("target_files", 0)) != 1
        or int(blackout.get("partial_target_files", -1)) != 0
        or blackout.get("worker_threads_stopped") is not True
        or blackout_audit["source_size_bytes"] != int(blackout.get("payload_size_bytes", 0))
        or blackout_audit["source_sha256"] != blackout.get("source_sha256")
    ):
        raise AuditError("established data plane did not survive control-plane blackout")

    performance = dict(_require(value, "performance"))
    concurrent_completed = int(performance.get("concurrent_completed", 0))
    concurrent_session_ids = _audit_concurrent_sessions(
        performance,
        _require(value, "concurrent_evidence"),
        transit_audit=transit_audit,
        min_concurrent=min_concurrent,
    )
    peak_memory, peak_memory_limit = _audit_memory_gate(performance)
    session_established = _finite_float(
        performance.get("session_established_s", 999), "performance.session_established_s"
    )
    if session_established < 0 or session_established > 5:
        raise AuditError("session establishment exceeded five seconds")
    protocol_overhead_ratio = _audit_overhead_gate(performance)
    if performance.get("hard_failure_fallback_within_10s") is not True:
        raise AuditError("performance report omitted bounded hard-failure fallback")
    if require_one_gib:
        if (
            int(main.get("source_size_bytes", 0)) < 1024**3
            or performance.get("one_gib_required") is not True
            or performance.get("one_gib_ok") is not True
        ):
            raise AuditError("one-GiB resource gate did not pass")

    return {
        "ok": True,
        "protocol_version": transit_audit["protocol_version"],
        "source_size_bytes": transit_audit["source_size_bytes"],
        "concurrent_completed": concurrent_completed,
        "concurrent_unique_sessions": len(concurrent_session_ids),
        "session_established_s": session_established,
        "protocol_overhead_ratio": protocol_overhead_ratio,
        "hard_failure_fallback_s": hard_failure_elapsed,
        "peak_python_memory_bytes": peak_memory,
        "peak_python_memory_limit_bytes": peak_memory_limit,
        "one_gib_required": bool(require_one_gib),
    }


def audit_soak_report(
    value: dict[str, Any],
    *,
    require_duration_s: float,
    min_sessions: int,
) -> dict[str, Any]:
    """Independently enforce a completed persistent-worker soak report."""

    required_duration = _finite_float(require_duration_s, "required soak duration")
    if required_duration < 0 or min_sessions < 0:
        raise AuditError("soak audit requirements cannot be negative")
    if value.get("result") != "pass" or value.get("completed_duration") is not True:
        raise AuditError("soak did not complete with a passing result")
    target_duration = _finite_float(_require(value, "duration_target_s"), "duration_target_s")
    elapsed = _finite_float(_require(value, "elapsed_s"), "elapsed_s")
    if target_duration < required_duration or elapsed < required_duration:
        raise AuditError("soak duration is below the required gate")
    sessions = int(_require(value, "sessions_completed"))
    if sessions < min_sessions:
        raise AuditError("soak completed too few sessions")
    failures = _require(value, "failures")
    if not isinstance(failures, list) or failures:
        raise AuditError("soak contains failed sessions")
    if value.get("plaintext_found_on_transit") is not False:
        raise AuditError("soak found plaintext on the transit peer")
    memory_growth = int(_require(value, "memory_growth_bytes"))
    memory_limit = int(_require(value, "memory_growth_limit_bytes"))
    if memory_growth < 0 or memory_growth > memory_limit:
        raise AuditError("soak memory growth exceeded its limit")
    if int(_require(value, "partial_files")) != 0:
        raise AuditError("soak left partial target files")
    if value.get("worker_threads_stopped") is not True:
        raise AuditError("soak worker threads did not stop")
    transit_frames = int(_require(value, "transit_frames"))
    transit_bytes = int(_require(value, "transit_bytes"))
    if transit_frames < sessions:
        raise AuditError("soak transit frame count is inconsistent")
    last_evidence = dict(_require(value, "last_evidence"))
    transit_audit = audit_peer_transit(last_evidence)
    minimum_transit_bytes = sessions * int(transit_audit["source_size_bytes"])
    if transit_bytes < minimum_transit_bytes:
        raise AuditError("soak transit byte count does not cover completed session payloads")
    return {
        "ok": True,
        "duration_target_s": target_duration,
        "elapsed_s": elapsed,
        "sessions_completed": sessions,
        "memory_growth_bytes": memory_growth,
        "memory_growth_limit_bytes": memory_limit,
        "minimum_transit_bytes": minimum_transit_bytes,
        "transit_bytes": transit_bytes,
        "last_session_id": last_evidence.get("session_id"),
        "protocol_version": transit_audit["protocol_version"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", help="peer-transit evidence JSON")
    parser.add_argument("--report", action="store_true", help="audit a full acceptance report")
    parser.add_argument("--soak-report", action="store_true", help="audit a completed soak report")
    parser.add_argument("--require-one-gib", action="store_true")
    parser.add_argument("--min-concurrent", type=int, default=1)
    parser.add_argument("--require-duration-seconds", type=float, default=0.0)
    parser.add_argument("--min-sessions", type=int, default=3)
    parser.add_argument("--output", default="", help="optional audit report path")
    args = parser.parse_args()
    if args.min_concurrent < 0 or args.min_sessions < 0:
        raise AuditError("minimum session counts cannot be negative")
    required_soak_duration = _finite_float(
        args.require_duration_seconds, "required soak duration"
    )
    if required_soak_duration < 0:
        raise AuditError("required soak duration cannot be negative")
    if args.report and args.soak_report:
        raise AuditError("choose either acceptance-report or soak-report audit")
    value = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError("evidence root must be a JSON object")
    if args.soak_report:
        report = audit_soak_report(
            value,
            require_duration_s=required_soak_duration,
            min_sessions=args.min_sessions,
        )
    elif args.report:
        report = audit_acceptance_report(
            value,
            require_one_gib=args.require_one_gib,
            min_concurrent=args.min_concurrent,
        )
    else:
        report = audit_peer_transit(value)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
