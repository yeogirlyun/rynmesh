import hashlib
from datetime import UTC, datetime, timedelta

import pytest

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


def test_check_empty_or_non_http_endpoint_unknown():
    p = PeerHealthProbe(probe=lambda u, h, t: True)
    out = p.check([{"id": "a", "endpoint": ""}, {"id": "b", "endpoint": "file:///x"}])
    assert {r["peerId"]: r["online"] for r in out} == {"a": None, "b": None}


def test_check_probes_http_endpoint_and_maps_result():
    seen = {}
    def fake(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        return True
    p = PeerHealthProbe(probe=fake, network_key="k")
    out = p.check([{"id": "hk", "endpoint": "http://203.0.113.10:8791"}])
    assert out[0]["online"] is True
    assert out[0]["httpReachable"] is True
    assert out[0]["presenceSource"] == "http"
    assert seen["url"] == "http://203.0.113.10:8791/health"
    assert seen["headers"]["x-ryn-auth"] == hashlib.sha256(b"rynmesh-net-key:k").hexdigest()


def test_signed_lan_endpoint_is_probed_before_advertised_vpn_endpoint():
    calls = []
    def probe(url, *_):
        calls.append(url)
        return url.startswith("http://172.16.8.117:")
    result = PeerHealthProbe(probe=probe).check([{
        "id": "mac", "endpoint": "http://172.19.0.1:8791",
        "lanEndpoints": ["http://172.16.8.117:8791"],
    }])[0]
    assert result["lanReachable"] is True
    assert result["httpReachable"] is True
    assert calls == ["http://172.16.8.117:8791/health"]


def test_check_probe_exception_alone_is_unknown():
    def boom(u, h, t):
        raise RuntimeError("net")
    p = PeerHealthProbe(probe=boom)
    out = p.check([{"id": "x", "endpoint": "http://x:1"}])
    assert out[0]["online"] is None


def test_check_now_is_injectable():
    fixed = datetime(2026, 6, 3, tzinfo=UTC)
    p = PeerHealthProbe(probe=lambda u, h, t: False, now=lambda: fixed)
    out = p.check([{"id": "x", "endpoint": ""}])
    assert out[0]["checkedAt"] == fixed.isoformat()


@pytest.mark.parametrize("age,discovery,expected,source", [
    (30, "registry", True, "heartbeat"),
    (-30, "registry", True, "heartbeat"),
    (30, "cache", True, "heartbeat"),
    (121, "registry", False, "heartbeat_expired"),
    (121, "cache", None, "unknown"),
    (-3600, "registry", None, "unknown"),
])
def test_presence_uses_fresh_heartbeat_not_unreachable_vpn_address(age, discovery, expected, source):
    now = datetime(2026, 9, 24, tzinfo=UTC)
    p = PeerHealthProbe(probe=lambda *_: False, now=lambda: now)
    result = p.check([{"id": "remote", "endpoint": "http://10.10.10.200:8791",
                       "lastSeen": (now - timedelta(seconds=age)).isoformat(),
                       "discoverySource": discovery}])[0]
    assert result["online"] is expected
    assert result["presenceSource"] == source
    assert result["httpReachable"] is False


@pytest.mark.parametrize("timestamp", ["", "local", "not-a-date", "2026-09-24T00:00:00"])
def test_missing_or_invalid_heartbeat_is_unknown(timestamp):
    p = PeerHealthProbe(probe=lambda *_: False)
    assert p.check([{"id": "remote", "lastSeen": timestamp}])[0]["online"] is None
