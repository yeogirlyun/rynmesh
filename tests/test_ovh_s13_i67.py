"""S13-I67 route-first addresses and real signed discovery candidates."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import ifaddr
import pytest

from rynmesh import peer_http, store
from rynmesh.drop import peers


def adapter(name, *ips, nice_name=None):
    return SimpleNamespace(name=name, nice_name=nice_name or name, ips=[SimpleNamespace(ip=ip) for ip in ips])


def network(monkeypatch, *, route="192.168.50.8", adapters=None, dns=None):
    for name in ("RYNMESH_MACHINE_IP", "RYNMESH_AUTO_PEER_ENDPOINT", "RYNMESH_PEER_ENDPOINT", "RYNMESH_PEER_PUBLIC_HOST", "RYNMESH_REGISTRY_URL"):
        monkeypatch.delenv(name, raising=False)
    connections = []
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def settimeout(self, value): assert 0 < value <= .2
        def connect(self, destination):
            connections.append(destination)
            if route is None:
                raise OSError("offline route")
        def getsockname(self): return route, 12345
        def send(self, *args): raise AssertionError("route lookup must not send packets")
        def sendto(self, *args): raise AssertionError("route lookup must not send packets")
    monkeypatch.setattr(store.socket, "socket", lambda *args, **kwargs: Probe())
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: adapters if adapters is not None else [
        adapter("vEthernet (WSL)", "10.77.0.1"), adapter("Wi-Fi", "192.168.50.8")])
    monkeypatch.setattr(store.socket, "getaddrinfo", lambda *args, **kwargs: [
        (store.socket.AF_INET, 0, 0, "", (ip, 0)) for ip in (dns if dns is not None else ["10.77.0.1", "192.168.50.8"])])
    return connections


def test_T1_default_route_wins_over_first_virtual_hostname_address(monkeypatch):
    connections = network(monkeypatch)
    assert peer_http._desktop_lan_ip() == "192.168.50.8"
    assert store._primary_lan_ip() == "192.168.50.8"
    assert connections and all(destination == ("1.1.1.1", 80) for destination in connections)


@pytest.mark.parametrize("name", ["vEthernet (Default Switch)", "wSl", "Hyper-V Adapter", "VMware", "VirtualBox Host-Only"])
def test_T2_offline_route_filters_virtual_adapter_names_and_link_local(monkeypatch, name):
    network(monkeypatch, route=None, adapters=[adapter(name, "10.77.0.1"), adapter("Ethernet", "169.254.1.2"),
        adapter("Wi-Fi", "127.0.0.1", ("::1", 0, 0), "192.168.50.8")])
    assert store._primary_lan_ip() == "192.168.50.8"
    assert peer_http._desktop_lan_ip() == "192.168.50.8"


def test_T2_friendly_adapter_name_is_filtered_and_hostname_is_last_resort(monkeypatch):
    network(monkeypatch, route=None, adapters=[adapter("{adapter-guid}", "10.77.0.1", nice_name="Hyper-V Virtual Ethernet Adapter")],
            dns=["127.0.0.1", "169.254.1.2", "0.0.0.0", "192.168.50.8"])
    assert store._primary_lan_ip() == peer_http._desktop_lan_ip() == "192.168.50.8"


def test_T2_hostname_fallback_does_not_reintroduce_known_virtual_interface(monkeypatch):
    network(monkeypatch, route=None, adapters=[adapter("vEthernet", "10.77.0.1")], dns=["10.77.0.1"])
    assert store._primary_lan_ip() == peer_http._desktop_lan_ip() == "127.0.0.1"


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_T3_both_entry_points_use_default_route_on_all_platforms(monkeypatch, platform):
    network(monkeypatch)
    monkeypatch.setattr(store.sys, "platform", platform)
    monkeypatch.setattr(peer_http.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no route binary")))
    assert store._primary_lan_ip() == peer_http._desktop_lan_ip() == "192.168.50.8"


@pytest.mark.parametrize("auto", ["0", "1"])
def test_T3_explicit_machine_ip_wins_even_when_auto_endpoint_is_enabled(monkeypatch, auto):
    connections = network(monkeypatch)
    monkeypatch.setenv("RYNMESH_MACHINE_IP", "192.168.60.9")
    monkeypatch.setenv("RYNMESH_AUTO_PEER_ENDPOINT", auto)
    assert store._primary_lan_ip() == peer_http._desktop_lan_ip() == "192.168.60.9"
    assert connections == []


def test_T3_no_usable_address_has_consistent_loopback_fallback(monkeypatch):
    network(monkeypatch, route=None, adapters=[], dns=["127.0.0.1", "169.254.1.2"])
    assert store._primary_lan_ip() == peer_http._desktop_lan_ip() == "127.0.0.1"


def test_T1_registration_appends_three_private_backups_and_drop_uses_signed_record(tmp_path, monkeypatch):
    network(monkeypatch, adapters=[adapter("Wi-Fi", "192.168.50.8"), adapter("Ethernet", "10.77.0.2", "172.20.0.2", "192.168.60.2", "192.168.70.2"),
        adapter("vEthernet", "10.77.0.1"), adapter("Other", "1.1.1.2", "169.254.1.2", "127.0.0.1")])
    monkeypatch.setenv("RYNMESH_PEER_HOST", "0.0.0.0")
    monkeypatch.setenv("RYNMESH_PEER_PORT", "8791")
    monkeypatch.setenv("RYNMESH_REGISTRY_DIR", str(tmp_path / "registry"))
    provider = store.RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "network")
    consumer = store.RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "network")
    result = provider.register_node(network_id="test")
    expected = ["http://192.168.50.8:8791", "http://10.77.0.2:8791", "http://172.20.0.2:8791", "http://192.168.60.2:8791"]
    assert result["record"]["endpoints"] == expected
    assert result["record"]["metadata"]["peer_endpoint"] == expected[0]
    consumer.discover_peers(network_id="test")  # Populate the real signed-record cache used by Drop.
    directory = peers.PeerDirectory(store=consumer, network_id=lambda: "test", relationship=lambda _: "own")
    assert directory.candidates(provider.peer_id) == expected
    attempted = []
    def hello(endpoint, method, path, **kwargs):
        attempted.append(endpoint)
        if endpoint == expected[0]:
            raise ConnectionError("primary unavailable")
        nonce = parse_qs(urlsplit(path).query)["nonce"][0]
        return 200, peers.make_hello(peer_id=provider.peer_id, node_name="Provider", messaging_pub=provider.messaging_public_key(),
                                     nonce=nonce, private_key_bytes=provider.private_key_bytes)
    monkeypatch.setattr(peers, "http_json", hello)
    probe = directory.probe(provider.peer_id, fresh=True)
    assert probe.online and probe.endpoint in expected[1:] and expected[0] in attempted


def test_T3_explicit_endpoint_stays_first_and_file_registration_stays_file(tmp_path, monkeypatch):
    network(monkeypatch)
    monkeypatch.setenv("RYNMESH_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("RYNMESH_PEER_ENDPOINT", "https://node.example:9443")
    node = store.RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    assert node._default_registration_endpoints()[0] == "https://node.example:9443"
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT")
    monkeypatch.delenv("RYNMESH_PEER_PORT", raising=False)
    assert node._default_registration_endpoints() == (f"file://{node.network_peer_dir}",)
