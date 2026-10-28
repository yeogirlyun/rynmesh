"""P1 view model: one honest availability for every discovered AI service."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import rynmesh.llm_package.routes as llm_routes
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.service_view import annotate_services, is_usable, presence_from_probe
from rynmesh.services import peer_box
from rynmesh.services.peer_health import PeerPresenceCache
from rynmesh.store import RynmeshStore

SELF = "self-peer"


def row(peer="remote", **more):
    return {
        "peer_id": peer, "node_name": "ziling", "online": True, "binding_verified": False,
        "access_policy": "personal_space",
        "service": {"package_id": "codex-cli", "model_alias": "Codex", "adapter": "codex_cli"},
        **more,
    }


def annotate(rows, **kwargs):
    return annotate_services(rows, self_id=SELF, **kwargs)


def test_registry_advertisement_from_unrelated_device_is_not_ready():
    (item,) = annotate([row()])
    assert item["relationship"] == "discovered"
    assert item["availability"] == {"state": "needs_connection", "reason": "advertisement_only"}
    assert item["online"] is True            # the provider's claim is preserved...
    assert item["online_claim"] is True      # ...and named honestly
    assert is_usable(item) is False


def test_space_member_with_ai_access_is_ready():
    (item,) = annotate([row()], members=["remote"], allows_ai=lambda peer: peer == "remote")
    assert item["relationship"] == "space_member"
    assert item["availability"] == {"state": "ready", "reason": "space_member"}
    assert is_usable(item) is True


def test_space_member_without_ai_access_is_not_offered():
    (item,) = annotate([row()], members=["remote"], allows_ai=lambda _peer: False)
    assert item["relationship"] == "space_member"
    assert item["availability"] == {"state": "needs_connection", "reason": "space_ai_access_off"}


def test_bound_catalog_record_and_friend_and_self_are_ready():
    bound, friend, own = annotate([
        row(binding_verified=True), row(peer="friend", access="friend"), row(peer=SELF, local_only=True),
    ])
    assert bound["availability"]["reason"] == "catalog_verified"
    assert friend["relationship"] == "friend" and friend["availability"]["state"] == "ready"
    assert own["relationship"] == "self" and own["availability"]["reason"] == "self"


def test_probe_overrides_a_provider_that_claims_to_be_online():
    probe = {"remote": {"peerId": "remote", "online": False, "presenceSource": "heartbeat_expired"}}
    (item,) = annotate([row()], members=["remote"], allows_ai=lambda _p: True, probe_for=probe.get)
    assert item["presence"] == {"state": "offline", "basis": "heartbeat_expired"}
    assert item["availability"] == {"state": "offline", "reason": "peer_unreachable"}
    assert item["online"] is True
    assert is_usable(item) is False


def test_provider_that_reports_offline_is_offline_even_when_probe_succeeds():
    probe = {"remote": {"peerId": "remote", "online": True, "presenceSource": "http"}}
    (item,) = annotate([row(online=False)], members=["remote"], allows_ai=lambda _p: True, probe_for=probe.get)
    assert item["availability"] == {"state": "offline", "reason": "provider_offline"}


def test_no_probe_is_unconfirmed_not_online():
    assert presence_from_probe("remote", self_id=SELF, probe=None) == {"state": "unconfirmed", "basis": "none"}
    assert presence_from_probe(SELF, self_id=SELF, probe=None) == {"state": "online", "basis": "self"}
    assert presence_from_probe("remote", self_id=SELF, probe={"online": None, "presenceSource": "unknown"})["state"] == "unconfirmed"


def test_annotation_is_stable_across_reads_and_does_not_mutate_input():
    rows = [row()]
    first = annotate(rows)
    assert first == annotate(rows)
    assert "availability" not in rows[0]


def test_unannotated_rows_fall_back_to_the_claim():
    assert is_usable({"online": True}) is True
    assert is_usable({"online": False}) is False


def test_presence_cache_expires_and_never_reports_stale_as_online():
    now = [100.0]
    cache = PeerPresenceCache(ttl_s=10, clock=lambda: now[0])
    cache.update([{"peerId": "remote", "online": True, "presenceSource": "http"}])
    assert cache.get("remote")["online"] is True
    now[0] = 111.0
    assert cache.get("remote") is None
    assert cache.get("never-probed") is None


def _advertise(provider, *, online=True, policy="personal_space"):
    provider.register_job_capacity(
        capabilities=[llm_routes.CAPABILITY], network_id="network",
        metadata={"llm_service": {
            "service": {"package_id": "codex-cli", "model_alias": "Codex CLI", "adapter": "codex_cli",
                        "capabilities": [], "pricing": {}, "privacy": {}},
            "online": online, "ready": online, "access_policy": policy, "chat_protocol": "rynmesh.chat.v1",
        }},
    )


def test_discovery_route_exposes_view_model_for_registry_advertisement(tmp_path):
    network = tmp_path / "network"
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=network)
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=network)
    key = peer_box.load_or_create_messaging_key(consumer.home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(app, store=consumer, home=consumer.home, messaging_key=key,
                       resolve_endpoint=lambda _: None, resolve_pubkey=lambda _: None)
    client = TestClient(app)
    _advertise(provider)

    rows = client.get("/api/local/llm/services?network_id=network").json()["services"]
    (item,) = [r for r in rows if r["peer_id"] == provider.peer_id]
    # The same device that advertises "online" is not usable and not yet observed.
    assert item["online"] is True and item["online_claim"] is True
    assert item["relationship"] == "discovered"
    assert item["presence"] == {"state": "unconfirmed", "basis": "none"}
    assert item["availability"] == {"state": "needs_connection", "reason": "advertisement_only"}

    # A node-side probe result (shared with the device list) is reflected without re-probing.
    presence = PeerPresenceCache()
    presence.update([{"peerId": provider.peer_id, "online": True, "presenceSource": "http"}])
    app.state.peer_presence = presence
    (item,) = [r for r in client.get("/api/local/llm/services?network_id=network").json()["services"]
               if r["peer_id"] == provider.peer_id]
    assert item["presence"] == {"state": "online", "basis": "http"}
    assert item["availability"]["state"] == "needs_connection"   # observed online is still not entitlement
