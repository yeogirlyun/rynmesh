"""P2: one directory of devices and services, one freshness policy."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

import rynmesh.llm_package.routes as llm_routes
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.service_view import annotate_services
from rynmesh.peer_directory import (
    FreshnessPolicy,
    add_advertised_providers,
    build_snapshot,
    directory_violations,
)
from rynmesh.peer_http import create_app
from rynmesh.services import peer_box
from rynmesh.services.peer_health import PeerHealthProbe
from rynmesh.store import RynmeshStore

SELF = "self-peer"
NOW = datetime(2026, 10, 14, 6, 0, 0, tzinfo=UTC)


def item(peer="remote", **more):
    return {"id": peer, "slug": "", "name": peer, "endpoint": "http://10.0.0.2:8791", "lanEndpoints": [],
            "network": "net", "tier": "unverified", "credits": 0.0, "weight": 1.0,
            "lastSeen": NOW.isoformat(), "discoverySource": "registry", "served": 0, "fetched": 0,
            "trustedRoot": False, "isSelf": peer == SELF, **more}


def service(peer="remote", **more):
    return {"peer_id": peer, "node_name": peer, "online": True, "binding_verified": False,
            "access_policy": "personal_space",
            "service": {"package_id": "codex-cli", "model_alias": "Codex", "adapter": "codex_cli"}, **more}


def annotated(rows, **kwargs):
    return annotate_services(rows, self_id=SELF, **kwargs)


def snapshot(items, services, **kwargs):
    return build_snapshot(items, services, self_id=SELF, network_id="net", now=NOW, **kwargs)


# --- freshness policy -------------------------------------------------------------

def test_policy_defaults_and_environment_override():
    assert FreshnessPolicy.from_environment({}) == FreshnessPolicy()
    assert FreshnessPolicy.from_environment({"RYNMESH_DISCOVERY_MAX_AGE_HOURS": "6"}).peer_record_max_age_hours == 6
    # A garbage or non-positive value falls back instead of disabling discovery.
    assert FreshnessPolicy.from_environment({"RYNMESH_DISCOVERY_MAX_AGE_HOURS": "nope"}).peer_record_max_age_hours == 24
    assert FreshnessPolicy.from_environment({"RYNMESH_DISCOVERY_MAX_AGE_HOURS": "0"}).peer_record_max_age_hours == 24


# --- advertised providers are devices ---------------------------------------------

def test_provider_with_only_a_capacity_record_becomes_a_device():
    records = [{"peer_id": "known", "node_name": "Known"}]
    capacities = [
        {"peer_id": "advertiser", "node_name": "Advertiser", "updated_at": "2026-10-14T05:59:50+00:00",
         "capabilities": ["rynmesh.llm.private.v1"], "network_id": "net"},
        {"peer_id": "known", "node_name": "Known (capacity)"},   # already a device: not duplicated
        {"peer_id": SELF, "node_name": "me"},                   # never listed twice
        {"peer_id": "", "node_name": "no id"},
    ]
    merged = add_advertised_providers(records, capacities, self_id=SELF, network_id="net")
    assert [r["peer_id"] for r in merged] == ["known", "advertiser"]
    advertiser = merged[1]
    assert advertiser["discovery_source"] == "capacity"
    assert advertiser["endpoints"] == []
    assert advertiser["updated_at"] == "2026-10-14T05:59:50+00:00"
    assert records == [{"peer_id": "known", "node_name": "Known"}]   # input untouched


# --- snapshot ---------------------------------------------------------------------

def test_every_service_belongs_to_a_device_even_when_only_a_catalog_knows_it():
    rows = annotated([service("catalog-only")])
    snap = snapshot([item(SELF)], rows)
    ids = [p["id"] for p in snap["peers"]]
    assert "catalog-only" in ids
    entry = next(p for p in snap["peers"] if p["id"] == "catalog-only")
    assert entry["discoverySource"] == "service"
    assert [s["service"]["package_id"] for s in entry["services"]] == ["codex-cli"]
    assert directory_violations(snap) == []


def test_unconnected_device_is_visible_and_its_service_is_not_ready():
    snap = snapshot([item(SELF), item("ziling")], annotated([service("ziling")]))
    ziling = next(p for p in snap["peers"] if p["id"] == "ziling")
    assert ziling["relationship"] == "discovered"
    assert ziling["services"][0]["availability"]["state"] == "needs_connection"
    assert directory_violations(snap) == []


def test_member_device_is_ready_and_relationship_comes_from_local_state_only():
    rows = annotated([service("laptop")], members=["laptop"], allows_ai=lambda _p: True)
    snap = snapshot([item(SELF), item("laptop")], rows, members=["laptop"])
    laptop = next(p for p in snap["peers"] if p["id"] == "laptop")
    assert laptop["relationship"] == "space_member"
    assert laptop["services"][0]["availability"] == {"state": "ready", "reason": "space_member"}
    # A device that merely claims membership in its advertisement is not a member.
    forged = snapshot([item("liar")], annotated([service("liar", access_policy="personal_space")]))
    assert forged["peers"][0]["relationship"] == "discovered"


def test_friend_evidence_from_either_source_is_not_lost():
    from_store = snapshot([item("pal")], [], friend_ids=["pal"])
    assert from_store["peers"][0]["relationship"] == "friend"
    from_service = snapshot([item("pal2")], annotated([service("pal2", access="friend")]))
    assert from_service["peers"][0]["relationship"] == "friend"


def test_presence_is_the_observation_shared_by_device_and_service():
    probe = {"remote": {"peerId": "remote", "online": False, "presenceSource": "heartbeat_expired"}}
    rows = annotated([service("remote")], members=["remote"], allows_ai=lambda _p: True, probe_for=probe.get)
    snap = snapshot([item("remote")], rows, members=["remote"], probe_for=probe.get)
    remote = snap["peers"][0]
    assert remote["presence"] == {"state": "offline", "basis": "heartbeat_expired"}
    assert remote["services"][0]["availability"] == {"state": "offline", "reason": "peer_unreachable"}
    assert directory_violations(snap) == []


def test_stale_devices_are_flagged_not_hidden_and_self_is_never_stale():
    old = (NOW - timedelta(minutes=30)).isoformat()
    snap = snapshot([item(SELF, lastSeen=old), item("old", lastSeen=old), item("fresh")], [])
    by_id = {p["id"]: p for p in snap["peers"]}
    assert by_id["old"]["freshness"]["stale"] is True
    assert by_id["old"]["freshness"]["age_s"] == 1800
    assert by_id["fresh"]["freshness"]["stale"] is False
    assert by_id[SELF]["freshness"]["stale"] is False
    # LAN observations are signed and live; they are never considered stale by age alone.
    lan = snapshot([item("near", lastSeen=old, discoverySource="lan")], [])
    assert lan["peers"][0]["freshness"]["stale"] is False


def test_snapshot_order_and_revision_are_stable():
    items = [item("b"), item(SELF), item("a")]
    first = snapshot(items, [])
    again = snapshot(list(reversed(items)), [])
    assert [p["id"] for p in first["peers"]] == [SELF, "a", "b"]
    assert first["revision"] == again["revision"]
    changed = snapshot(items, annotated([service("a")]))
    assert changed["revision"] != first["revision"]


def test_invariant_checker_detects_a_broken_directory():
    bad = {"peers": [{"id": "x", "relationship": "discovered", "presence": {"state": "offline"},
                      "services": [{"peer_id": "y", "availability": {"state": "ready", "reason": "advertisement_only"}}]}]}
    problems = directory_violations(bad)
    assert any(p.startswith("I1") for p in problems)
    assert any(p.startswith("I3") for p in problems)
    assert any(p.startswith("I4") for p in problems)


# --- through the real application ---------------------------------------------------

def _app_for(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "network")
    return store, provider


def test_capacity_only_provider_appears_in_peers_health_and_directory(tmp_path, monkeypatch):
    store, provider = _app_for(tmp_path, monkeypatch)
    provider.register_job_capacity(
        capabilities=[llm_routes.CAPABILITY], network_id="rynmesh-main",
        metadata={"llm_service": {
            "service": {"package_id": "codex-cli", "model_alias": "Codex CLI", "adapter": "codex_cli",
                        "capabilities": [], "pricing": {}, "privacy": {}},
            "online": True, "ready": True, "access_policy": "personal_space", "chat_protocol": "rynmesh.chat.v1"}},
    )
    monkeypatch.setattr(PeerHealthProbe, "_check_one", lambda *_: False)
    client = TestClient(create_app(store))

    peers = client.get("/api/local/peers").json()
    ids = [p["id"] for p in peers]
    assert provider.peer_id in ids, "a provider that only advertised capacity must be a device"
    entry = next(p for p in peers if p["id"] == provider.peer_id)
    assert entry["discoverySource"] == "capacity" and entry["endpoint"] == ""

    health = {h["peerId"]: h for h in client.post("/api/local/peers/health").json()}
    assert health[provider.peer_id]["online"] is True
    assert health[provider.peer_id]["presenceSource"] == "heartbeat"

    directory = client.get("/api/local/directory").json()
    assert directory["self_id"] == store.peer_id
    assert directory_violations(directory) == []
    device = next(p for p in directory["peers"] if p["id"] == provider.peer_id)
    assert device["presence"] == {"state": "online", "basis": "heartbeat"}
    assert device["relationship"] == "discovered"
    assert [s["service"]["package_id"] for s in device["services"]] == ["codex-cli"]
    assert device["services"][0]["availability"]["state"] == "needs_connection"
    assert directory["peers"][0]["isSelf"] is True


def test_directory_requires_local_control(tmp_path, monkeypatch):
    store, _ = _app_for(tmp_path, monkeypatch)
    client = TestClient(create_app(store), client=("203.0.113.9", 50000))
    assert client.get("/api/local/directory").status_code in {401, 403, 404}


def test_llm_routes_publish_their_service_view_for_the_directory(tmp_path):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = FastAPI()
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    install_llm_routes(app, store=store, home=store.home, messaging_key=key,
                       resolve_endpoint=lambda _: None, resolve_pubkey=lambda _: None)
    assert callable(app.state.llm_discover)


def test_snapshot_reports_what_the_probe_reached():
    probe = {"near": {"peerId": "near", "online": True, "presenceSource": "http", "httpReachable": True, "lanReachable": True},
             "far": {"peerId": "far", "online": True, "presenceSource": "heartbeat", "httpReachable": False, "lanReachable": False}}
    snap = snapshot([item("near"), item("far"), item("blind")], [], probe_for=probe.get)
    by_id = {p["id"]: p for p in snap["peers"]}
    assert by_id["near"]["reachability"] == {"http": True, "lan": True}
    assert by_id["far"]["reachability"] == {"http": False, "lan": False}
    assert by_id["blind"]["reachability"] == {"http": None, "lan": None}   # never probed: unknown, not false


def test_directory_probes_so_the_device_poll_is_enough(tmp_path, monkeypatch):
    """The screens no longer poll /peers/health separately; the directory refreshes presence itself."""
    store, provider = _app_for(tmp_path, monkeypatch)
    provider.register_job_capacity(
        capabilities=[llm_routes.CAPABILITY], network_id="rynmesh-main",
        metadata={"llm_service": {
            "service": {"package_id": "codex-cli", "model_alias": "Codex CLI", "adapter": "codex_cli",
                        "capabilities": [], "pricing": {}, "privacy": {}},
            "online": True, "ready": True, "access_policy": "personal_space", "chat_protocol": "rynmesh.chat.v1"}},
    )
    probed = []
    real_check = PeerHealthProbe.check

    def counting_check(self, peers):
        probed.append([p["id"] for p in peers])
        return real_check(self, peers)

    monkeypatch.setattr(PeerHealthProbe, "check", counting_check)
    client = TestClient(create_app(store))

    # Before any probe the services view cannot know whether the device is there.
    before = client.get("/api/local/llm/services?network_id=rynmesh-main").json()["services"]
    assert before[0]["presence"] == {"state": "unconfirmed", "basis": "none"}

    directory = client.get("/api/local/directory").json()
    assert probed and provider.peer_id in probed[-1]
    device = next(p for p in directory["peers"] if p["id"] == provider.peer_id)
    assert device["presence"]["state"] == "online"
    assert device["reachability"]["http"] is False   # capacity-only: no endpoint to reach

    # The directory's probe fed the shared observation, so the services endpoint agrees.
    after = client.get("/api/local/llm/services?network_id=rynmesh-main").json()["services"]
    assert after[0]["presence"] == {"state": "online", "basis": "heartbeat"}

    # quick mode never probes (cache-only paint).
    probed.clear()
    client.get("/api/local/directory?quick=1")
    assert probed == []


def test_s13_i19_t4_stale_device_downgrades_ready_services_and_lan_probe_ignores_system_proxy(monkeypatch):
    """T4: Devices with lastSeen > 180s downgrade ready services to stale (advertisement_stale), directory_violations reports I5, and _default_probe ignores system proxies."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from rynmesh.services.peer_health import _default_probe

    stale_seen = (NOW - timedelta(seconds=181)).isoformat()
    rows = annotated([service("laptop")], members=["laptop"], allows_ai=lambda _p: True)
    assert rows[0]["availability"]["state"] == "ready"

    snap = snapshot([item(SELF), item("laptop", lastSeen=stale_seen)], rows, members=["laptop"])
    laptop = next(p for p in snap["peers"] if p["id"] == "laptop")
    assert laptop["freshness"]["stale"] is True
    assert laptop["services"][0]["availability"] == {"state": "stale", "reason": "advertisement_stale"}
    assert directory_violations(snap) == []

    # Forged snapshot with stale=True + availability.state='ready' must report I5.
    broken = {
        "peers": [
            {
                "id": "laptop",
                "relationship": "space_member",
                "presence": {"state": "online", "basis": "http"},
                "freshness": {"source": "registry", "age_s": 300, "stale": True},
                "services": [{"peer_id": "laptop", "availability": {"state": "ready", "reason": "space_member"}}],
            }
        ]
    }
    violations = directory_violations(broken)
    assert any(v.startswith("I5") for v in violations)

    # F2: System proxy env vars must be ignored by _default_probe so LAN /health connects directly.
    class OkHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *_args):
            return

    srv = HTTPServer(("127.0.0.1", 0), OkHealthHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # Point ambient OS proxy variables to an unreachable port; direct opener must still succeed.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        assert _default_probe(f"http://127.0.0.1:{srv.server_port}/health", {}, 2.0) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_s13_i19_t5_metadata_link_local_and_loopback_endpoints_are_not_probed():
    """T5: PeerHealthProbe refuses link-local (169.254.169.254), cloud metadata, and loopback endpoints without issuing HTTP requests."""
    probed_urls = []
    probe = PeerHealthProbe(probe=lambda url, _h, _t: (probed_urls.append(url), True)[1])
    out = probe.check([
        {"id": "meta-ip", "endpoint": "http://169.254.169.254/latest/meta-data"},
        {"id": "meta-host", "endpoint": "http://metadata.google.internal:80"},
        {"id": "loop-v4", "endpoint": "http://127.0.0.1:8791", "lanEndpoints": ["http://169.254.10.20:8791"]},
        {"id": "loop-v6", "endpoint": "http://[::1]:8791"},
    ])
    assert probed_urls == []
    assert all(r["httpReachable"] is False for r in out)


def test_s13_i19_t6_two_hour_old_capacity_excluded_from_jobs_capacity(tmp_path, monkeypatch):
    """T6: 2-hour-old capacity advertisement is excluded from /api/local/jobs/capacity under FreshnessPolicy (1h), while <1h stays."""
    assert FreshnessPolicy.from_environment(
        {"RYNMESH_ADVERTISEMENT_MAX_AGE_HOURS": "2.5", "RYNMESH_FRESH_WINDOW_S": "90"}
    ) == FreshnessPolicy(peer_record_max_age_hours=24.0, advertisement_max_age_hours=2.5, fresh_window_s=90.0)

    store, old_provider = _app_for(tmp_path, monkeypatch)
    fresh_provider = RynmeshStore(home=tmp_path / "fresh_provider", network_dir=tmp_path / "network")

    old_provider.register_job_capacity(capabilities=["cap.test"], network_id="rynmesh-main")
    fresh_provider.register_job_capacity(capabilities=["cap.test"], network_id="rynmesh-main")

    # Age old_provider's capacity record in the shared local registry directory to 2 hours ago.
    import json

    two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    for cap_file in old_provider.registry.job_capacity_dir.glob("*.json"):
        data = json.loads(cap_file.read_text(encoding="utf-8"))
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        if payload.get("peer_id") == old_provider.peer_id:
            # Re-sign with updated_at = 2 hours ago so signature verification passes and only age filters it.
            from rynmesh.crypto import sign_payload

            payload["updated_at"] = two_hours_ago
            signed = sign_payload(payload, private_key_bytes=old_provider.private_key_bytes).to_dict()
            cap_file.write_text(json.dumps(signed), encoding="utf-8")

    monkeypatch.setattr(PeerHealthProbe, "_check_one", lambda *_: False)
    client = TestClient(create_app(store))
    resp = client.get("/api/local/jobs/capacity?network_id=rynmesh-main")
    assert resp.status_code == 200
    body = resp.json()
    items = body["capacities"] if isinstance(body, dict) and "capacities" in body else body
    peer_ids = {item["peer_id"] for item in items}
    assert fresh_provider.peer_id in peer_ids
    assert old_provider.peer_id not in peer_ids


def test_s13_i19_t7_directory_endpoint_attaches_diagnostics_violations_when_snapshot_broken(tmp_path, monkeypatch):
    """T7: When snapshot contains a stale+ready violation, /api/local/directory includes diagnostics.violations."""
    import rynmesh.peer_directory as peer_directory_mod

    store, _ = _app_for(tmp_path, monkeypatch)
    monkeypatch.setattr(PeerHealthProbe, "_check_one", lambda *_: False)
    client = TestClient(create_app(store))

    real_build = peer_directory_mod.build_snapshot

    def broken_build(*args, **kwargs):
        snap = real_build(*args, **kwargs)
        snap["peers"].append({
            "id": "stale-ready-peer",
            "name": "StaleReady",
            "isSelf": False,
            "relationship": "space_member",
            "presence": {"state": "online", "basis": "http"},
            "reachability": {"http": True, "lan": False},
            "freshness": {"source": "registry", "age_s": 600, "stale": True},
            "services": [{"peer_id": "stale-ready-peer", "availability": {"state": "ready", "reason": "space_member"}}],
        })
        return snap

    monkeypatch.setattr(peer_directory_mod, "build_snapshot", broken_build)
    resp = client.get("/api/local/directory?quick=1")
    assert resp.status_code == 200
    body = resp.json()
    assert "diagnostics" in body
    assert any(v.startswith("I5") for v in body["diagnostics"]["violations"])
