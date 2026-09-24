"""Discovery authenticates fresh local addresses, without granting peer access."""

import ipaddress
import json
import time
from urllib import request as urlrequest
from types import SimpleNamespace

import pytest

from rynmesh.crypto import sign_payload
from rynmesh.lan_discovery import TTL, LanDiscovery
from rynmesh.registry import RegistryError
from rynmesh.store import RynmeshStore

A = ipaddress.IPv4Interface("192.168.50.10/24")
B = ipaddress.IPv4Interface("192.168.50.11/24")


def node(seed=1, **kwargs):
    return LanDiscovery(private_key=bytes([seed]) * 32, node_name="test",
                        network_id="test", peer_port=8791, **kwargs)


def exchange(consumer, provider):
    reply = provider._receive(consumer._query(), (str(A.ip), 12345), B)
    assert reply
    consumer._receive(reply, (str(B.ip), 12346), A)
    return reply


def test_signed_discovery_and_expiry(monkeypatch):
    consumer, provider = node(), node(2)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    reply = exchange(consumer, provider)
    assert consumer.endpoints(provider.peer_id, "test") == ["http://192.168.50.11:8791"]
    assert consumer.endpoints(provider.peer_id, "another-network") == []
    now += TTL + 1
    consumer._receive(reply, (str(B.ip), 12346), A)
    assert consumer.records("test") == []


def test_http_fallback_requires_nonce_bound_signed_identity(monkeypatch):
    from rynmesh.registry import PeerRecord

    consumer, provider = node(), node(2)
    nonce = "a" * 32
    payload = PeerRecord(peer_id=provider.peer_id, node_name="Mac",
                         endpoints=("http://192.168.50.11:8791",),
                         capabilities=(), safety_packs=(), network_id="test").to_dict()
    payload.update(lan_protocol="rynmesh-lan-v1", lan_nonce=nonce)
    signed = sign_payload(payload, private_key_bytes=provider.private_key)

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return json.dumps(signed.to_dict()).encode()

    class Opener:
        def open(self, *_args, **_kwargs): return Response()

    monkeypatch.setattr(urlrequest, "build_opener", lambda *_: Opener())
    assert consumer._probe_http("192.168.50.11", nonce).public_key == provider.peer_id
    assert consumer._probe_http("192.168.50.11", "b" * 32) is None


@pytest.mark.parametrize("attack", ["signature", "identity", "nonce", "endpoint", "network"])
def test_rejects_forged_or_redirected_reply(attack):
    consumer, provider = node(), node(2)
    raw = provider._receive(consumer._query(), (str(A.ip), 12345), B)
    packet = json.loads(raw)
    payload = packet["body"]["record"]["payload"]
    if attack == "signature":
        payload["node_name"] = "tampered"
    else:
        if attack == "identity":
            payload["peer_id"] = consumer.peer_id
        elif attack == "nonce":
            payload["lan_nonce"] = "x" * 32
        elif attack == "endpoint":
            payload["endpoints"] = ["http://192.168.50.99:8791"]
        else:
            payload["network_id"] = "another-network"
        packet["body"]["record"] = sign_payload(payload, private_key_bytes=provider.private_key).to_dict()
    consumer._receive(json.dumps(packet).encode(), (str(B.ip), 12346), A)
    assert consumer.records("test") == []


@pytest.mark.parametrize("source", ["10.0.0.1", "127.0.0.1", "169.254.169.254",
                                    "192.168.50.255", "192.168.50.0", "224.0.0.1"])
def test_off_subnet_or_special_sources_are_not_probed(source):
    consumer, provider = node(), node(2)
    assert provider._receive(consumer._query(), (source, 12345), B) is None


def test_network_key_gate_and_response_rate_limit():
    provider = node(2, network_key="secret")
    assert provider._receive(node()._query(), (str(A.ip), 12345), B) is None
    consumer = node(network_key="secret")
    exchange(consumer, provider)
    assert consumer.records("test")
    assert provider._receive(consumer._query(), (str(A.ip), 12345), B) is None


@pytest.mark.parametrize("data", [b"", b"null", b"[]", b"{}", b"x" * 4097, b"\xff"])
def test_malformed_datagrams_are_ignored(data):
    assert node()._receive(data, (str(A.ip), 12345), B) is None


def test_store_merges_lan_without_overwriting_registry_identity_or_endpoints(tmp_path, monkeypatch):
    store = RynmeshStore(home=tmp_path / "a", network_dir=tmp_path / "network")
    provider = RynmeshStore(home=tmp_path / "b", network_dir=tmp_path / "network")
    provider.register_node(network_id="test", endpoints=("http://old.example:8791",))
    consumer = node()
    responder = LanDiscovery(private_key=provider.private_key_bytes, node_name="new name",
                             network_id="test", peer_port=8791)
    exchange(consumer, responder)
    store.lan_discovery = consumer
    peers = store.discover_peers(network_id="test")["peers"]
    assert len(peers) == 1
    assert peers[0]["endpoints"] == ["http://old.example:8791"]
    assert peers[0]["lan_endpoints"] == ["http://192.168.50.11:8791"]
    assert peers[0]["node_name"] == provider.node_name
    assert store.trusted_root_peer_ids == ()

    def no_registry(**_):
        raise AssertionError("quick discovery must not wait for registry I/O")

    monkeypatch.setattr(store.registry, "list_peers", no_registry)
    quick = store.discover_peers(network_id="test", cache_only=True)["peers"]
    assert quick[0]["peer_id"] == provider.peer_id
    assert quick[0]["lan_endpoints"] == ["http://192.168.50.11:8791"]

    def offline(**kwargs):
        raise RegistryError("offline")

    monkeypatch.setattr(store.registry, "list_peers", offline)
    assert store.discover_peers(network_id="test")["peers"][0]["lan_endpoints"]
    # Also discover LAN peers when there has never been a registry record/cache.
    monkeypatch.setattr(store, "_cached_peer_records", lambda **_: [])
    found = store.discover_peers(network_id="test")["peers"][0]
    assert found["peer_id"] == provider.peer_id and found["discovery_source"] == "lan"


def test_configuration_respects_disable_loopback_and_custom_port(monkeypatch):
    store = SimpleNamespace(private_key_bytes=bytes([1]) * 32, node_name="test")
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_PEER_HOST", "127.0.0.1")
    assert LanDiscovery.from_environment(store) is None
    monkeypatch.setenv("RYNMESH_PEER_HOST", "0.0.0.0")
    monkeypatch.setenv("RYNMESH_PEER_PORT", "9876")
    assert LanDiscovery.from_environment(store).peer_port == 9876
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "0")
    assert LanDiscovery.from_environment(store) is None


def test_lifespan_starts_and_stops_discovery(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    calls = []
    fake = SimpleNamespace(start=lambda: calls.append("start"), close=lambda: calls.append("close"))
    monkeypatch.setattr(LanDiscovery, "from_environment", lambda store: fake)
    with TestClient(create_app()):
        assert calls == ["start"]
    assert calls == ["start", "close"]
