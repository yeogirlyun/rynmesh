#!/usr/bin/env python3
"""Fail-closed audit for three-node ordinary-peer P2P transit evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rynmesh.crypto import SignedPayload
from rynmesh.jobs import WorkResult, verify_work_result
from rynmesh.peer_transit import PROTOCOL_VERSION


class AuditError(RuntimeError):
    pass


def _require(value: dict[str, Any], key: str) -> Any:
    if key not in value:
        raise AuditError(f"missing evidence field: {key}")
    return value[key]


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
    if relay_result.status != "completed" or relay_result.requester_peer_id != source:
        raise AuditError("signed relay result has invalid lifecycle bindings")
    if target_result.status != "completed" or target_result.requester_peer_id != transit:
        raise AuditError("signed target result has invalid lifecycle bindings")
    if str(target_result.result_refs.get("sha256") or "") != source_hash:
        raise AuditError("signed target result does not attest the source hash")
    if str(target_result.result_refs.get("session_id") or "") != str(value.get("session_id") or ""):
        raise AuditError("signed target result session mismatch")
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
        "no_turn",
        "registry_has_no_payload_marker",
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

    direct_socket = dict(_require(value, "direct"))
    if direct_socket.get("ok") is not True or direct_socket.get("ice_relay_candidate_used") is not False:
        raise AuditError("healthy direct ICE probe did not pass")
    _audit_hop(dict(direct_socket.get("source") or {}), "direct.source")
    _audit_hop(dict(direct_socket.get("target") or {}), "direct.target")

    direct_file = dict(_require(value, "healthy_direct_file"))
    if direct_file.get("ok") is not True:
        raise AuditError("healthy direct file transfer did not pass")
    if direct_file.get("source_sha256") != direct_file.get("target_sha256"):
        raise AuditError("healthy direct file hashes do not match")
    if int(direct_file.get("transit_bytes_before", -1)) != int(
        direct_file.get("transit_bytes_after", -2)
    ):
        raise AuditError("transit peer carried bytes during healthy direct transfer")
    _audit_hop(dict(direct_file.get("source_hop") or {}), "direct_file.source_hop")

    hard_failure = dict(_require(value, "actual_hard_failure"))
    if (
        hard_failure.get("ok") is not True
        or hard_failure.get("selected_path") != "peer_transit"
        or not str(hard_failure.get("direct_fallback_error") or "")
        or float(hard_failure.get("elapsed_s", 999)) > 10
    ):
        raise AuditError("real direct-failure fallback did not meet the ten-second gate")

    route = dict(_require(value, "route"))
    reasons = {str(item.get("reason") or "") for item in route.get("events", [])}
    if route.get("ok") is not True or not {
        "direct_degraded",
        "transit_better",
        "direct_recovery_started",
        "direct_recovered",
    } <= reasons:
        raise AuditError("route degradation/recovery evidence is incomplete")

    unavailable = dict(_require(value, "unavailable"))
    if unavailable.get("ok") is not True or int(unavailable.get("partial_target_files", -1)) != 0:
        raise AuditError("transit-unavailable handling is not bounded and atomic")

    performance = dict(_require(value, "performance"))
    concurrent_completed = int(performance.get("concurrent_completed", 0))
    if performance.get("concurrency_ok") is not True or concurrent_completed < min_concurrent:
        raise AuditError("concurrent-session gate did not pass")
    if performance.get("memory_bounded") is not True:
        raise AuditError("streaming memory gate did not pass")
    if float(performance.get("session_established_s", 999)) > 5:
        raise AuditError("session establishment exceeded five seconds")
    if float(performance.get("protocol_overhead_ratio", 999)) > 0.15:
        raise AuditError("protocol overhead exceeded fifteen percent")
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
        "session_established_s": float(performance["session_established_s"]),
        "protocol_overhead_ratio": float(performance["protocol_overhead_ratio"]),
        "hard_failure_fallback_s": float(hard_failure["elapsed_s"]),
        "one_gib_required": bool(require_one_gib),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", help="peer-transit evidence JSON")
    parser.add_argument("--report", action="store_true", help="audit a full acceptance report")
    parser.add_argument("--require-one-gib", action="store_true")
    parser.add_argument("--min-concurrent", type=int, default=1)
    parser.add_argument("--output", default="", help="optional audit report path")
    args = parser.parse_args()
    if args.min_concurrent < 0:
        raise AuditError("minimum concurrent sessions cannot be negative")
    value = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError("evidence root must be a JSON object")
    report = (
        audit_acceptance_report(
            value,
            require_one_gib=args.require_one_gib,
            min_concurrent=args.min_concurrent,
        )
        if args.report
        else audit_peer_transit(value)
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
