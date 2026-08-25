from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rynmesh.crypto import public_key_from_private, sign_payload
from scripts.audit_public_p2p import AuditError, audit_records


def _private_key() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes_raw()


def _signed_records(*, provider_public_host: str = "203.0.113.8"):
    requester_key = _private_key()
    provider_key = _private_key()
    requester = public_key_from_private(requester_key)
    provider = public_key_from_private(provider_key)
    order_id = "wo_public_audit"
    task_id = "task_public_audit"
    order = sign_payload({
        "kind": "work_order",
        "work_order_id": order_id,
        "requester_peer_id": requester,
        "provider_peer_id": provider,
        "capability": "rynmesh.llm.private.v1",
        "operation": "rynmesh.llm.private.infer.v1.p2p_offer",
        "network_id": "rynmesh-llm-e2e",
        "idempotency_key": "p2p:" + task_id,
        "params": {
            "session_id": task_id,
            "timeout_seconds": 60,
            "ice_signal": {
                "username": "consumer",
                "password": "consumer-password",
                "candidates": [
                    "consumer 1 udp 1694498815 198.51.100.7 50001 typ srflx "
                    "raddr 10.0.0.2 rport 50001",
                ],
            },
        },
    }, private_key_bytes=requester_key).to_dict()
    accepted = sign_payload({
        "kind": "work_result",
        "work_order_id": order_id,
        "requester_peer_id": requester,
        "provider_peer_id": provider,
        "network_id": "rynmesh-llm-e2e",
        "status": "accepted",
        "result_refs": {
            "relay_allowed": False,
            "ice_signal": {
                "username": "provider",
                "password": "provider-password",
                "candidates": [
                    f"provider 1 udp 1694498815 {provider_public_host} 50002 typ srflx "
                    "raddr 192.168.1.2 rport 50002",
                ],
            },
        },
    }, private_key_bytes=provider_key).to_dict()
    completed = sign_payload({
        "kind": "work_result",
        "work_order_id": order_id,
        "requester_peer_id": requester,
        "provider_peer_id": provider,
        "network_id": "rynmesh-llm-e2e",
        "status": "completed",
        "result_refs": {
            "transport_evidence": {
                "transport": "ice_udp_direct",
                "relay_used": False,
                "public_nat_traversal_required": True,
                "distinct_public_egress_required": True,
                "peer_public_mapping_nominated": True,
                "request_bytes": 512,
                "response_bytes": 384,
                "local": {"type": "host", "host": "192.168.1.2", "port": 50002},
                "remote": {"type": "srflx", "host": "198.51.100.7", "port": 50001},
            },
        },
    }, private_key_bytes=provider_key).to_dict()
    return order, accepted, completed, task_id


def test_public_p2p_audit_accepts_signed_distinct_egress_evidence():
    order, accepted, completed, task_id = _signed_records()

    report = audit_records(
        work_orders=[order],
        work_results=[accepted, completed],
        task_id=task_id,
    )

    assert report["ok"] is True
    assert report["transport"] == "ice_udp_direct"
    assert report["relay_used"] is False
    assert report["consumer_public_mappings"] == ["198.51.100.7"]
    assert report["provider_public_mappings"] == ["203.0.113.8"]


def test_public_p2p_audit_rejects_shared_public_exit():
    order, accepted, completed, task_id = _signed_records(
        provider_public_host="198.51.100.7",
    )

    with pytest.raises(AuditError, match="distinct public egress"):
        audit_records(
            work_orders=[order],
            work_results=[accepted, completed],
            task_id=task_id,
        )
