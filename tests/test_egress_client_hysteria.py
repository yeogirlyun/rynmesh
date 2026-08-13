import pytest

from rynmesh.services import net_egress_client as nec

HY = {
    "kind": "net.egress.session", "transport": "hysteria2",
    "exit_host": "192.0.2.10", "udp_port": 443, "socks_port": 2080,
    "auth": "secretpw", "auth_kind": "password", "sni": "bing.com",
    "obfs": "obfspw", "up_mbps": 20, "down_mbps": 100,
    "tls_insecure": True, "tls_pin": "ABC123",
}

def test_hysteria_env_mapping():
    env = nec.build_vpn_env(HY)
    assert env["RYNMESH_VPN_TRANSPORT"] == "hysteria2"
    assert env["RYNMESH_VPN_HYSTERIA_SERVER"] == "192.0.2.10:443"
    assert env["RYNMESH_VPN_HYSTERIA_AUTH"] == "secretpw"
    assert env["RYNMESH_VPN_HYSTERIA_SNI"] == "bing.com"
    assert env["RYNMESH_VPN_HYSTERIA_OBFS"] == "obfspw"
    assert env["RYNMESH_VPN_HYSTERIA_UP_MBPS"] == "20"
    assert env["RYNMESH_VPN_HYSTERIA_DOWN_MBPS"] == "100"
    assert env["RYNMESH_VPN_HYSTERIA_INSECURE"] == "1"
    assert env["RYNMESH_VPN_HYSTERIA_PIN"] == "ABC123"
    assert env["RYNMESH_VPN_PORT"] == "2080"
    assert "RYNMESH_VPN_GATEWAY" not in env

def test_hysteria_env_requires_udp_port():
    bad = dict(HY); bad.pop("udp_port")
    with pytest.raises(nec.NetEgressClientError):
        nec.build_vpn_env(bad)

def test_hysteria_optional_fields_absent():
    minimal = {"transport": "hysteria2", "exit_host": "h", "udp_port": 443,
               "socks_port": 2080, "auth": "p", "sni": "s", "up_mbps": 5, "down_mbps": 5}
    env = nec.build_vpn_env(minimal)
    assert "RYNMESH_VPN_HYSTERIA_OBFS" not in env
    assert "RYNMESH_VPN_HYSTERIA_INSECURE" not in env
    assert "RYNMESH_VPN_HYSTERIA_PIN" not in env

def test_ssh_socks5_mapping_unchanged():
    ssh = {"transport": "ssh-socks5", "exit_host": "203.0.113.10", "exit_user": "rynmesh", "socks_port": 1080}
    env = nec.build_vpn_env(ssh)
    assert env["RYNMESH_VPN_GATEWAY"] == "rynmesh@203.0.113.10"
    assert env["RYNMESH_VPN_PORT"] == "1080"
    assert "RYNMESH_VPN_TRANSPORT" not in env
