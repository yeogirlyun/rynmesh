"""Audit a strict public-P2P LLM work order without reading task bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aioice

from rynmesh.crypto import SignedPayload, verify_signed_payload


class AuditError(RuntimeError):
    pass


def _load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "RYNMESH_NETWORK_KEY":
            os.environ[key.strip()] = value.strip()


def _registry_auth_header() -> dict[str, str]:
    network_key = os.environ.get("RYNMESH_NETWORK_KEY", "")
    if not network_key:
        return {}
    digest = hashlib.sha256(("rynmesh-net-key:" + network_key).encode()).hexdigest()
    return {"x-ryn-auth": digest}


def _json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_registry_auth_header())
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise AuditError("Registry response must be a JSON object")
    return value


def _verified_payload(value: dict[str, Any]) -> dict[str, Any]:
    signed = SignedPayload.from_dict(value)
    verify_signed_payload(signed)
    payload = signed.payload
    kind = str(payload.get("kind") or "")
    if kind == "work_order" and payload.get("requester_peer_id") != signed.public_key:
        raise AuditError("Work-order signer does not match requester identity")
    if kind == "work_result" and payload.get("provider_peer_id") != signed.public_key:
        raise AuditError("Work-result signer does not match Provider identity")
    return payload


def _public_hosts(signal: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    candidates = signal.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AuditError("ICE signal has no candidates")
    for raw in candidates:
        candidate = aioice.Candidate.from_sdp(str(raw))
        if str(candidate.transport).lower() != "udp" or str(candidate.type) != "srflx":
            raise AuditError("Strict public acceptance contains a non-srflx UDP candidate")
        hosts.add(str(candidate.host))
    return hosts


def audit_records(
    *,
    work_orders: list[dict[str, Any]],
    work_results: list[dict[str, Any]],
    work_order_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    orders = [_verified_payload(item) for item in work_orders]
    results = [_verified_payload(item) for item in work_results]
    wanted_idempotency = "p2p:" + task_id if task_id else ""
    matching = [
        item for item in orders
        if (not work_order_id or item.get("work_order_id") == work_order_id)
        and (not wanted_idempotency or item.get("idempotency_key") == wanted_idempotency)
    ]
    if len(matching) != 1:
        raise AuditError(f"Expected one strict P2P work order, found {len(matching)}")
    order = matching[0]
    if order.get("operation") != "rynmesh.llm.private.infer.v1.p2p_offer":
        raise AuditError("Selected work order is not a strict P2P offer")
    params = dict(order.get("params") or {})
    if set(params) - {"session_id", "ice_signal", "timeout_seconds"}:
        raise AuditError("Work order contains fields outside the body-free signaling allowlist")
    offer_hosts = _public_hosts(dict(params.get("ice_signal") or {}))

    order_id = str(order.get("work_order_id") or "")
    matching_results = [item for item in results if item.get("work_order_id") == order_id]
    accepted = next((item for item in matching_results if item.get("status") == "accepted"), None)
    completed = next((item for item in matching_results if item.get("status") == "completed"), None)
    terminal_failure = next(
        (item for item in matching_results if item.get("status") in {"failed", "cancelled"}),
        None,
    )
    if terminal_failure:
        raise AuditError(f"Strict P2P order ended as {terminal_failure.get('status')}")
    if not accepted or not completed:
        raise AuditError("Strict P2P order lacks accepted and completed signed results")
    accepted_refs = dict(accepted.get("result_refs") or {})
    if accepted_refs.get("relay_allowed") is not False:
        raise AuditError("Provider answer did not explicitly forbid relay")
    answer_hosts = _public_hosts(dict(accepted_refs.get("ice_signal") or {}))
    if not any(left != right for left in offer_hosts for right in answer_hosts):
        raise AuditError("Consumer and Provider do not prove distinct public egress addresses")

    completed_refs = dict(completed.get("result_refs") or {})
    evidence = dict(completed_refs.get("transport_evidence") or {})
    expected = {
        "transport": "ice_udp_direct",
        "relay_used": False,
        "public_nat_traversal_required": True,
        "distinct_public_egress_required": True,
        "peer_public_mapping_nominated": True,
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise AuditError(f"Transport evidence mismatch for {key}")
    if int(evidence.get("request_bytes") or 0) <= 0 or int(evidence.get("response_bytes") or 0) <= 0:
        raise AuditError("Transport evidence does not prove bidirectional task bytes")
    if dict(evidence.get("remote") or {}).get("type") != "srflx":
        raise AuditError("Nominated remote candidate is not a public STUN mapping")
    if order.get("provider_peer_id") != accepted.get("provider_peer_id") \
            or order.get("provider_peer_id") != completed.get("provider_peer_id"):
        raise AuditError("Provider identity changed across signed order states")
    if order.get("requester_peer_id") != accepted.get("requester_peer_id") \
            or order.get("requester_peer_id") != completed.get("requester_peer_id"):
        raise AuditError("Requester identity changed across signed order states")

    return {
        "ok": True,
        "network_id": order.get("network_id"),
        "work_order_id": order_id,
        "task_id": params.get("session_id"),
        "requester_peer_id": order.get("requester_peer_id"),
        "provider_peer_id": order.get("provider_peer_id"),
        "consumer_public_mappings": sorted(offer_hosts),
        "provider_public_mappings": sorted(answer_hosts),
        "transport": evidence.get("transport"),
        "relay_used": evidence.get("relay_used"),
        "peer_public_mapping_nominated": evidence.get("peer_public_mapping_nominated"),
        "request_bytes": evidence.get("request_bytes"),
        "response_bytes": evidence.get("response_bytes"),
        "signed_states": ["open", "accepted", "completed"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-url", default="http://127.0.0.1:18890")
    parser.add_argument("--network-id", default="rynmesh-llm-e2e")
    parser.add_argument("--work-order-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if not args.work_order_id and not args.task_id:
        parser.error("provide --work-order-id or --task-id")
    if args.env_file:
        _load_env(args.env_file)
    query = urllib.parse.urlencode({
        "network_id": args.network_id,
        "status": "",
        "max_age_hours": 24,
    })
    orders = _json(args.registry_url.rstrip("/") + "/api/v1/jobs/work-orders?" + query)
    results = _json(args.registry_url.rstrip("/") + "/api/v1/jobs/work-results?" + urllib.parse.urlencode({
        "network_id": args.network_id,
        "work_order_id": args.work_order_id,
    }))
    report = audit_records(
        work_orders=list(orders.get("work_orders") or []),
        work_results=list(results.get("work_results") or []),
        work_order_id=args.work_order_id,
        task_id=args.task_id,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
