"""Tests for the net.egress VPN service (MVP) — pure/brokering logic only.

No network, no real rynmesh-vpn: we test the session builder, the env mapping,
the result parsing, dry-run connect planning, and the worker's execute() with a
monkeypatched provider config + ledger.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from rynmesh.services import net_egress
from rynmesh.services import net_egress_client as client

_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)


def _config(**overrides):
    base = {
        "exit_host": "203.0.113.10",
        "exit_user": "rynmesh",
        "exit_port": 22,
        "region": "HK",
        "socks_port": 1080,
        "max_ttl_s": net_egress.DEFAULT_MAX_TTL_S,
        "price_credits": 1.0,
    }
    base.update(overrides)
    return base


def test_build_session_matching_and_empty_region():
    for params in ({}, {"region": "hk"}, {"region": "HK"}):
        s = net_egress.build_session(params, _config(), now=_NOW)
        assert s["kind"] == "net.egress.session"
        assert s["exit_host"] == "203.0.113.10"
        assert s["exit_user"] == "rynmesh"
        assert s["region"] == "HK"
        assert s["transport"] == "ssh-socks5"


def test_build_session_rejects_wrong_region():
    with pytest.raises(net_egress.NetEgressConfigError):
        net_egress.build_session({"region": "CN"}, _config(), now=_NOW)


def test_build_session_clamps_ttl():
    short = net_egress.build_session({"ttl_s": 5}, _config(max_ttl_s=3600), now=_NOW)
    assert short["ttl_s"] == 60  # floored
    long = net_egress.build_session({"ttl_s": 99999}, _config(max_ttl_s=3600), now=_NOW)
    assert long["ttl_s"] == 3600  # capped
    assert long["expires_at"] > long["issued_at"]


def test_build_vpn_env_maps_session():
    session = net_egress.build_session({}, _config(), now=_NOW)
    env = client.build_vpn_env(session)
    assert env["RYNMESH_VPN_GATEWAY"] == "rynmesh@203.0.113.10"
    assert env["RYNMESH_VPN_PORT"] == "1080"


def test_build_vpn_env_requires_host():
    with pytest.raises(client.NetEgressClientError):
        client.build_vpn_env({"exit_host": "", "exit_user": "x"})


def test_parse_session_rejects_bad_payload():
    with pytest.raises(client.NetEgressClientError):
        client._parse_session(json.dumps({"kind": "something_else"}))
    with pytest.raises(client.NetEgressClientError):
        client._parse_session("not json")


def test_connect_dry_run_plan():
    session = net_egress.build_session({}, _config(), now=_NOW)
    plan = client.connect(session, mode="chrome", url="https://iq.com", dry_run=True)
    assert plan == ["rynmesh-vpn", "chrome", "https://iq.com"]


def test_worker_execute_returns_session_and_records_credit(monkeypatch, tmp_path):
    monkeypatch.setenv("RYNMESH_EGRESS_EXIT_HOST", "203.0.113.10")
    monkeypatch.setenv("RYNMESH_EGRESS_REGION", "HK")
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))

    out = net_egress.NetEgressWorker().execute({"region": "HK", "ttl_s": 600, "client_peer_id": "peerB"})
    session = json.loads(out["message"])
    assert session["kind"] == "net.egress.session"
    assert session["ttl_s"] == 600
    assert session["region"] == "HK"

    ledger = tmp_path / "egress_sessions.jsonl"
    assert ledger.exists()
    line = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert line["event"] == "egress_session_opened"
    assert line["consumer_peer_id"] == "peerB"
    assert line["price_credits"] == 1.0


def test_worker_execute_missing_config_raises(monkeypatch):
    monkeypatch.delenv("RYNMESH_EGRESS_EXIT_HOST", raising=False)
    with pytest.raises(net_egress.NetEgressConfigError):
        net_egress.NetEgressWorker().execute({"region": "HK"})
