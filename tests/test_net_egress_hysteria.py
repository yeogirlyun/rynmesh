from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rynmesh.services import net_egress

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)

def _hy_config(**overrides):
    base = {
        "exit_host": "192.0.2.20", "exit_user": "ops", "exit_port": 20622,
        "region": "CN", "socks_port": 2080,
        "max_ttl_s": net_egress.DEFAULT_MAX_TTL_S, "price_credits": 1.0,
        "transport": "hysteria2",
        "hysteria_host": "192.0.2.10", "hysteria_udp_port": 443,
        "hysteria_auth": "secretpw", "hysteria_sni": "bing.com", "hysteria_obfs": "obfspw",
        "hysteria_up_mbps": 20, "hysteria_down_mbps": 100,
        "hysteria_tls_insecure": True, "hysteria_tls_pin": "ABC123",
    }
    base.update(overrides)
    return base

def test_hysteria_session_shape():
    s = net_egress.build_session({"region": "CN"}, _hy_config(), now=_NOW)
    assert s["kind"] == "net.egress.session"
    assert s["transport"] == "hysteria2"
    assert s["exit_host"] == "192.0.2.10"
    assert s["udp_port"] == 443
    assert s["socks_port"] == 2080
    assert s["auth"] == "secretpw"
    assert s["auth_kind"] == "password"
    assert s["sni"] == "bing.com"
    assert s["obfs"] == "obfspw"
    assert s["up_mbps"] == 20 and s["down_mbps"] == 100
    assert s["tls_insecure"] is True
    assert s["tls_pin"] == "ABC123"

def test_hysteria_host_falls_back_to_exit_host():
    s = net_egress.build_session({}, _hy_config(hysteria_host=""), now=_NOW)
    assert s["exit_host"] == "192.0.2.20"

def test_hysteria_requires_auth():
    with pytest.raises(net_egress.NetEgressConfigError):
        net_egress.build_session({}, _hy_config(hysteria_auth=""), now=_NOW)

def test_hysteria_respects_region_check():
    with pytest.raises(net_egress.NetEgressConfigError):
        net_egress.build_session({"region": "HK"}, _hy_config(region="CN"), now=_NOW)

def test_ssh_socks5_still_default_when_transport_unset():
    cfg = _hy_config(transport="ssh-socks5")
    s = net_egress.build_session({}, cfg, now=_NOW)
    assert s["transport"] == "ssh-socks5"
    assert s["auth"] == "shared_key"
    assert s["exit_user"] == "ops"
