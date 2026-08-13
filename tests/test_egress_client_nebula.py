import pytest

from rynmesh.services import net_egress_client as nec

N = {"transport": "nebula-socks", "overlay_ip": "10.42.0.10", "socks_port": 1080}

def test_nebula_env_mapping():
    env = nec.build_vpn_env(N)
    assert env["RYNMESH_VPN_TRANSPORT"] == "nebula-socks"
    assert env["RYNMESH_VPN_SOCKS_HOST"] == "10.42.0.10"
    assert env["RYNMESH_VPN_PORT"] == "1080"
    assert "RYNMESH_VPN_GATEWAY" not in env

def test_nebula_requires_overlay_ip():
    with pytest.raises(nec.NetEgressClientError):
        nec.build_vpn_env({"transport": "nebula-socks", "socks_port": 1080})
