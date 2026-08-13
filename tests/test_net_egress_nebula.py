from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rynmesh.services import net_egress

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)

def _cfg(**o):
    base = {
        "exit_host": "192.0.2.20", "exit_user": "ops", "exit_port": 20622,
        "region": "CN", "socks_port": 1080,
        "max_ttl_s": net_egress.DEFAULT_MAX_TTL_S, "price_credits": 1.0,
        "transport": "nebula-socks",
        "overlay_ip": "10.42.0.10",
    }
    base.update(o); return base

def test_nebula_session_shape():
    s = net_egress.build_session({"region": "CN"}, _cfg(), now=_NOW)
    assert s["transport"] == "nebula-socks"
    assert s["overlay_ip"] == "10.42.0.10"
    assert s["socks_port"] == 1080
    assert s["region"] == "CN"
    assert "auth" not in s or s["auth"] == "nebula-cert"

def test_nebula_requires_overlay_ip():
    with pytest.raises(net_egress.NetEgressConfigError):
        net_egress.build_session({}, _cfg(overlay_ip=""), now=_NOW)

def test_default_transport_still_ssh():
    s = net_egress.build_session({}, _cfg(transport="ssh-socks5"), now=_NOW)
    assert s["transport"] == "ssh-socks5"


def test_worker_execute_nebula_socks_does_not_keyerror(monkeypatch, tmp_path):
    """Regression: execute() wrote a ledger line hardcoding session['exit_host'],
    which KeyError'd for the nebula-socks descriptor (no exit_host)."""
    monkeypatch.setenv("RYNMESH_EGRESS_EXIT_HOST", "10.42.0.10")
    monkeypatch.setenv("RYNMESH_EGRESS_REGION", "CN")
    monkeypatch.setenv("RYNMESH_EGRESS_TRANSPORT", "nebula-socks")
    monkeypatch.setenv("RYNMESH_EGRESS_OVERLAY_IP", "10.42.0.10")
    monkeypatch.setenv("RYNMESH_EGRESS_SOCKS_PORT", "1080")
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    import json as _json
    out = net_egress.NetEgressWorker().execute({"region": "CN", "client_peer_id": "peerC"})
    session = _json.loads(out["message"])
    assert session["transport"] == "nebula-socks"
    assert session["overlay_ip"] == "10.42.0.10"
    line = _json.loads((tmp_path / "egress_sessions.jsonl").read_text().strip())
    assert line["exit_host"] == "10.42.0.10"  # falls back to overlay_ip
