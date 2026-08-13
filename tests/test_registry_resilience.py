"""Tests for censorship-resistant registry/discovery (Task 9)."""

from __future__ import annotations

import json
import tempfile

import pytest

from rynmesh.crypto import public_key_from_private
from rynmesh.registry import PeerRecord, RegistryError, sign_peer_record
from rynmesh.registry_resilience import (
    FallbackRegistryChain,
    bootstrap_peers_from_path,
    make_fallback_chain,
)


def _key() -> bytes:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate().private_bytes_raw()


def _signed_peer(key: bytes, network_id: str = "test-net") -> dict:
    pk = public_key_from_private(key)
    rec = PeerRecord(
        peer_id=pk,
        node_name="test",
        endpoints=(f"https://{pk[:8]}.example.com",),
        network_id=network_id,
    )
    return sign_peer_record(rec, private_key_bytes=key).to_dict()


# ---------------------------------------------------------------------------
# FallbackRegistryChain
# ---------------------------------------------------------------------------

class _AlwaysFail:
    """Registry that always raises RegistryError."""
    def list_peers(self, **_kw):
        raise RegistryError("simulated failure")
    def publish(self, _r):
        raise RegistryError("simulated failure")
    def publish_job_capacity(self, _r):
        raise RegistryError("simulated failure")
    def list_job_capacities(self, **_kw):
        raise RegistryError("simulated failure")
    def submit_work_order(self, _r):
        raise RegistryError("simulated failure")
    def list_work_orders(self, **_kw):
        raise RegistryError("simulated failure")
    def publish_work_result(self, _r):
        raise RegistryError("simulated failure")
    def list_work_results(self, **_kw):
        raise RegistryError("simulated failure")


class _Returns:
    """Registry that always returns a fixed list."""
    def __init__(self, records):
        self.records = records
        self.called = False
    def list_peers(self, **_kw):
        self.called = True
        return self.records
    def publish(self, _r):
        return {"status": "ok"}
    def publish_job_capacity(self, _r): return {}
    def list_job_capacities(self, **_kw): return []
    def submit_work_order(self, _r): return {}
    def list_work_orders(self, **_kw): return []
    def publish_work_result(self, _r): return {}
    def list_work_results(self, **_kw): return []


def test_fallback_chain_skips_failed_and_uses_next() -> None:
    key = _key()
    from rynmesh.crypto import SignedPayload
    record = SignedPayload.from_dict(_signed_peer(key))
    good = _Returns([record])
    chain = FallbackRegistryChain([_AlwaysFail(), _AlwaysFail(), good])
    result = chain.list_peers(network_id="test-net")
    assert len(result) == 1
    assert good.called


def test_fallback_chain_raises_when_all_fail() -> None:
    chain = FallbackRegistryChain([_AlwaysFail(), _AlwaysFail()])
    with pytest.raises(RegistryError):
        chain.list_peers()


def test_fallback_chain_requires_at_least_one() -> None:
    with pytest.raises(ValueError):
        FallbackRegistryChain([])


def test_make_fallback_chain_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_REGISTRY_URLS", "https://r1.example.com,https://r2.example.com")
    chain = make_fallback_chain()
    assert isinstance(chain, FallbackRegistryChain)
    assert len(chain.registries) == 2


# ---------------------------------------------------------------------------
# Out-of-band bootstrap
# ---------------------------------------------------------------------------

def test_bootstrap_from_path_loads_valid_records() -> None:
    key1, key2 = _key(), _key()
    records = [_signed_peer(key1), _signed_peer(key2)]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(records, f)
        fpath = f.name
    loaded = bootstrap_peers_from_path(fpath)
    assert len(loaded) == 2


def test_bootstrap_skips_tampered_record() -> None:
    key = _key()
    bad = _signed_peer(key)
    bad["signature"] = "AAAA"  # tamper
    good = _signed_peer(key)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([bad, good], f)
        fpath = f.name
    loaded = bootstrap_peers_from_path(fpath)
    assert len(loaded) == 1  # tampered one dropped


def test_bootstrap_rejects_non_array() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"not": "an array"}, f)
        fpath = f.name
    with pytest.raises(RegistryError, match="expected a JSON array"):
        bootstrap_peers_from_path(fpath)


# ---------------------------------------------------------------------------
# /api/v1/peers gossip endpoint
# ---------------------------------------------------------------------------

def test_peer_exchange_endpoint_returns_json(tmp_path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    node = RynmeshStore(home=tmp_path / "n", network_dir=tmp_path / "m", node_name="n")
    client = TestClient(create_app(node))
    resp = client.get("/api/v1/peers?network_id=test-net")
    assert resp.status_code == 200
    data = resp.json()
    assert "peers" in data
    assert "network_id" in data
