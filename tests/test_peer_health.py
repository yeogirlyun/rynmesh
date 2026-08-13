import hashlib
from datetime import UTC, datetime

from rynmesh.services.peer_health import PeerHealthProbe


def test_auth_header_empty_without_key():
    assert PeerHealthProbe(network_key="").auth_header() == {}


def test_auth_header_sha256_with_key():
    p = PeerHealthProbe(network_key="s3cret")
    expected = hashlib.sha256(b"rynmesh-net-key:s3cret").hexdigest()
    assert p.auth_header() == {"x-ryn-auth": expected}


def test_check_self_is_online_without_probing():
    called = []
    p = PeerHealthProbe(probe=lambda u, h, t: (called.append(u), True)[1])
    out = p.check([{"id": "self", "endpoint": "", "isSelf": True}])
    assert out[0]["peerId"] == "self" and out[0]["online"] is True
    assert called == []  # self is never probed


def test_check_empty_or_non_http_endpoint_offline():
    p = PeerHealthProbe(probe=lambda u, h, t: True)
    out = p.check([{"id": "a", "endpoint": ""}, {"id": "b", "endpoint": "file:///x"}])
    assert {r["peerId"]: r["online"] for r in out} == {"a": False, "b": False}


def test_check_probes_http_endpoint_and_maps_result():
    seen = {}
    def fake(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        return True
    p = PeerHealthProbe(probe=fake, network_key="k")
    out = p.check([{"id": "hk", "endpoint": "http://203.0.113.10:8791"}])
    assert out[0] == {"peerId": "hk", "online": True, "checkedAt": out[0]["checkedAt"]}
    assert seen["url"] == "http://203.0.113.10:8791/health"
    assert seen["headers"]["x-ryn-auth"] == hashlib.sha256(b"rynmesh-net-key:k").hexdigest()


def test_check_probe_exception_is_offline():
    def boom(u, h, t):
        raise RuntimeError("net")
    p = PeerHealthProbe(probe=boom)
    out = p.check([{"id": "x", "endpoint": "http://x:1"}])
    assert out[0]["online"] is False


def test_check_now_is_injectable():
    fixed = datetime(2026, 6, 3, tzinfo=UTC)
    p = PeerHealthProbe(probe=lambda u, h, t: False, now=lambda: fixed)
    out = p.check([{"id": "x", "endpoint": ""}])
    assert out[0]["checkedAt"] == fixed.isoformat()
