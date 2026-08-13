import json
from pathlib import Path

from rynmesh.services.egress_control import (
    CN_SITES,
    EgressController,
    _uptime_seconds,
)


class FakeStore:
    def __init__(self, home): self.home = home; self.peer_id = "self"

def make_controller(tmp_path, **kw):
    return EgressController(FakeStore(tmp_path), **kw)

def test_status_disconnected_when_no_cache(tmp_path):
    c = make_controller(tmp_path, port_listening=lambda p: False)
    st = c.status("CN")
    assert st == {
        "region": "CN", "connected": False, "providerPeerId": "", "providerNodeName": "",
        "socksPort": None, "exitIp": None, "loc": "", "locVerified": False,
        "ttlExpiresAt": "", "priceCredits": 0.0, "lastError": None,
        "connectedAt": None, "uptimeSeconds": None,
    }

def test_status_connected_when_port_live_and_cache_present(tmp_path):
    c = make_controller(tmp_path, port_listening=lambda p: True)
    cache_dir = Path(tmp_path) / "egress"; cache_dir.mkdir(parents=True)
    (cache_dir / "CN.json").write_text(json.dumps({
        "region": "CN", "provider_peer_id": "sz", "provider_node_name": "sz-egress",
        "socks_port": 2080, "exit_ip": "1.2.3.4", "loc": "CN",
        "ttl_expires_at": "2026-06-02T13:00:00Z", "price_credits": 1.0,
        "gateway": "ops@sz-egress-exit", "session": {},
    }))
    st = c.status("CN")
    assert st["connected"] is True
    assert st["socksPort"] == 2080
    assert st["loc"] == "CN" and st["locVerified"] is True
    assert st["providerNodeName"] == "sz-egress"

def test_status_clears_stale_cache_when_port_dead(tmp_path):
    c = make_controller(tmp_path, port_listening=lambda p: False)
    cache_dir = Path(tmp_path) / "egress"; cache_dir.mkdir(parents=True)
    (cache_dir / "CN.json").write_text(json.dumps({"socks_port": 2080}))
    st = c.status("CN")
    assert st["connected"] is False
    assert not (cache_dir / "CN.json").exists()  # stale cache removed

def test_status_locverified_uses_region_not_hardcoded_cn(tmp_path):
    c = make_controller(tmp_path, port_listening=lambda p: True)
    cache_dir = Path(tmp_path) / "egress"; cache_dir.mkdir(parents=True)
    (cache_dir / "HK.json").write_text(json.dumps({
        "region": "HK", "socks_port": 1080, "loc": "HK", "session": {},
    }))
    st = c.status("HK")
    assert st["connected"] is True
    assert st["loc"] == "HK"
    assert st["locVerified"] is True  # was a bug when hardcoded to "CN"

def test_status_locverified_false_when_loc_mismatches_region(tmp_path):
    c = make_controller(tmp_path, port_listening=lambda p: True)
    cache_dir = Path(tmp_path) / "egress"; cache_dir.mkdir(parents=True)
    (cache_dir / "CN.json").write_text(json.dumps({
        "region": "CN", "socks_port": 2080, "loc": "HK", "session": {},
    }))
    st = c.status("CN")
    assert st["connected"] is True
    assert st["locVerified"] is False


SESSION = {
    "exit_host": "sz-egress-exit", "exit_user": "ops", "socks_port": 2080,
    "region": "CN", "expires_at": "2026-06-02T13:43:44Z", "price_credits": 1.0,
}

def test_connect_brokers_brings_up_verifies_and_caches(tmp_path):
    calls = {"run_vpn": []}
    c = make_controller(
        tmp_path,
        broker=lambda region, prov: dict(SESSION),
        run_vpn=lambda session, *, mode, url="": calls["run_vpn"].append((mode, url)),
        port_listening=lambda p: True,
        verify_loc=lambda p: {"ip": "192.0.2.20", "loc": "CN"},
        now=lambda: 1000.0,
    )
    st = c.connect("CN")
    assert calls["run_vpn"] == [("up", "")]
    assert st["connected"] is True
    assert st["socksPort"] == 2080
    assert st["exitIp"] == "192.0.2.20"
    assert st["loc"] == "CN" and st["locVerified"] is True
    assert st["priceCredits"] == 1.0
    cached = json.loads((Path(tmp_path) / "egress" / "CN.json").read_text())
    assert cached["gateway"] == "ops@sz-egress-exit"
    assert cached["session"]["exit_host"] == "sz-egress-exit"

def test_connect_returns_error_when_broker_fails(tmp_path):
    def boom(region, prov): raise RuntimeError("no provider")
    c = make_controller(tmp_path, broker=boom)
    st = c.connect("CN")
    assert st["connected"] is False
    assert "broker_failed" in st["lastError"]

def test_connect_errors_and_kills_tunnel_when_not_listening(tmp_path):
    killed = []
    c = make_controller(
        tmp_path, broker=lambda r, p: dict(SESSION),
        run_vpn=lambda s, *, mode, url="": None,
        port_listening=lambda p: False,
        kill_tunnel=lambda port, gw: killed.append((port, gw)),
    )
    st = c.connect("CN")
    assert st["connected"] is False
    assert "not listening" in st["lastError"]
    assert killed == [(2080, "ops@sz-egress-exit")]  # leaked ssh -f cleaned up

def test_connect_warns_but_connects_when_loc_not_cn(tmp_path):
    c = make_controller(
        tmp_path, broker=lambda r, p: dict(SESSION),
        run_vpn=lambda s, *, mode, url="": None,
        port_listening=lambda p: True,
        verify_loc=lambda p: {"ip": "1.2.3.4", "loc": "HK"},
    )
    st = c.connect("CN")
    assert st["connected"] is True
    assert st["loc"] == "HK" and st["locVerified"] is False

def test_connect_errors_when_run_vpn_raises(tmp_path):
    def boom(s, *, mode, url=""): raise RuntimeError("ssh failed")
    c = make_controller(tmp_path, broker=lambda r, p: dict(SESSION), run_vpn=boom)
    st = c.connect("CN")
    assert st["connected"] is False
    assert "tunnel_up_failed" in st["lastError"]

def test_connect_errors_on_malformed_session(tmp_path):
    # session lacks exit_host -> build_vpn_env raises -> invalid_session
    c = make_controller(tmp_path, broker=lambda r, p: {"socks_port": 2080})
    st = c.connect("CN")
    assert st["connected"] is False
    assert "invalid_session" in st["lastError"]

def test_connect_errors_when_session_missing_socks_port(tmp_path):
    # has exit_host (so env build succeeds) but no port anywhere
    c = make_controller(tmp_path, broker=lambda r, p: {"exit_host": "h", "exit_user": "u"})
    st = c.connect("CN")
    assert st["connected"] is False
    assert st["lastError"] == "session missing socks_port"

def test_connect_returns_error_when_cache_write_fails(tmp_path, monkeypatch):
    c = make_controller(
        tmp_path, broker=lambda r, p: dict(SESSION),
        run_vpn=lambda s, *, mode, url="": None,
        port_listening=lambda p: True,
        verify_loc=lambda p: {"ip": "1.2.3.4", "loc": "CN"},
    )
    def boom(region, cache): raise OSError("disk full")
    monkeypatch.setattr(c, "_write_cache", boom)
    st = c.connect("CN")
    assert st["connected"] is False
    assert "cache_write_failed" in st["lastError"]


def _seed_connected(tmp_path):
    cache_dir = Path(tmp_path) / "egress"; cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "CN.json").write_text(json.dumps({
        "region": "CN", "socks_port": 2080, "gateway": "ops@sz-egress-exit",
        "loc": "CN", "session": dict(SESSION),
    }))
    return cache_dir

def test_launch_runs_chrome_with_cached_session(tmp_path):
    _seed_connected(tmp_path)
    calls = []
    c = make_controller(
        tmp_path, port_listening=lambda p: True,
        close_browser=lambda: calls.append(("close", None)),
        run_vpn=lambda session, *, mode, url="", urls=None: calls.append((mode, urls)),
    )
    out = c.launch("CN")
    assert out == {"launched": True, "count": len(CN_SITES)}
    # previous tabs are closed BEFORE the fresh browser opens
    assert calls == [("close", None), ("chrome", CN_SITES)]
    assert len(CN_SITES) == 8


def test_launch_does_not_close_browser_when_not_connected(tmp_path):
    closed = []
    c = make_controller(tmp_path, port_listening=lambda p: False,
                        close_browser=lambda: closed.append(1))
    out = c.launch("CN")
    assert out["connected"] is False and out["lastError"] == "not_connected"
    assert closed == []  # nothing to close if we never launch

def test_launch_rejects_when_not_connected(tmp_path):
    c = make_controller(tmp_path, port_listening=lambda p: False)
    out = c.launch("CN")
    assert out["connected"] is False and out["lastError"] == "not_connected"

def test_launch_rejects_non_http_url(tmp_path):
    _seed_connected(tmp_path)
    c = make_controller(tmp_path, port_listening=lambda p: True,
                        run_vpn=lambda *a, **k: None)
    out = c.launch("CN", ["file:///etc/passwd"])
    assert out["lastError"] == "invalid_url"

def test_disconnect_kills_tunnel_and_clears_cache(tmp_path):
    cache_dir = _seed_connected(tmp_path)
    killed = []
    c = make_controller(
        tmp_path, port_listening=lambda p: True,
        kill_tunnel=lambda port, gw: killed.append((port, gw)),
    )
    st = c.disconnect("CN")
    assert st["connected"] is False
    assert killed == [(2080, "ops@sz-egress-exit")]
    assert not (cache_dir / "CN.json").exists()

def test_launch_returns_error_when_run_vpn_raises(tmp_path):
    _seed_connected(tmp_path)
    def boom(session, *, mode, url="", urls=None): raise RuntimeError("rynmesh-vpn missing")
    c = make_controller(tmp_path, port_listening=lambda p: True, run_vpn=boom)
    out = c.launch("CN", ["https://tv.cctv.com"])
    assert out["connected"] is False
    assert "launch_failed" in out["lastError"]

def test_uptime_seconds_non_negative_for_recent_and_none_for_none():
    import time
    from datetime import UTC, datetime
    assert _uptime_seconds(None) is None
    epoch = _uptime_seconds(time.time() - 5)
    assert isinstance(epoch, int) and epoch >= 0
    iso = _uptime_seconds(datetime.now(UTC).isoformat())
    assert isinstance(iso, int) and iso >= 0


def test_disconnect_is_idempotent(tmp_path):
    _seed_connected(tmp_path)
    killed = []
    c = make_controller(
        tmp_path, port_listening=lambda p: True,
        kill_tunnel=lambda port, gw: killed.append((port, gw)),
    )
    first = c.disconnect("CN")
    second = c.disconnect("CN")  # cache already cleared -> no-op, must not raise
    assert first["connected"] is False
    assert second["connected"] is False
    assert killed == [(2080, "ops@sz-egress-exit")]  # tunnel killed exactly once
