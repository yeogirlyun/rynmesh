"""S13-I58: service-owned capacity lifetime and production publishers."""
import time
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.store import RynmeshStore
from tests.test_llm_package import openai_server  # real local HTTP adapter fixture


@pytest.fixture
def store(tmp_path):
    return RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")


def observe(store, monkeypatch):
    calls = []
    original = store.register_job_capacity

    def register(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(store, "register_job_capacity", register)
    return calls


def test_T1_withdraw_republishes_only_remaining_owner(store):
    store.register_job_capacity(owner="video", capabilities=["video"],
                                price_credits={"video": 3}, metadata={"video": "on"})
    store.register_job_capacity(owner="llm", capabilities=["llm"], metadata={"llm": "on"})
    result = store.withdraw_job_capacity("video", "rynmesh-main")
    assert result["record"]["capabilities"] == ["llm"]
    assert result["record"]["metadata"] == {"llm": "on"}
    assert result["record"]["price_credits"] == {}
    assert store.list_job_capacities()["capacities"][0]["capabilities"] == ["llm"]


def test_T2_expired_owner_disappears_after_three_own_intervals(store, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    store.register_job_capacity(owner="short", capabilities=["short"], polling_interval_sec=2,
                                metadata={"expired": True})
    store.register_job_capacity(owner="long", capabilities=["long"], polling_interval_sec=20)
    now[0] = 106.01
    result = store.register_job_capacity(owner="live", capabilities=["live"])
    assert result["record"]["capabilities"] == ["live", "long"]
    assert "expired" not in result["record"]["metadata"]


def test_T3_withdraw_all_stops_publication(store, monkeypatch):
    calls = []
    publish = store.registry.publish_job_capacity
    monkeypatch.setattr(store.registry, "publish_job_capacity", lambda value: (calls.append(value), publish(value))[1])
    store.register_job_capacity(owner="only", capabilities=["one"])
    store.withdraw_job_capacity("only", "rynmesh-main")
    store.withdraw_job_capacity("only", "rynmesh-main")
    assert len(calls) == 1  # old signed record expires naturally; never renew an empty record


def test_withdraw_retry_republishes_remaining_owner_after_registry_failure(store, monkeypatch):
    store.register_job_capacity(owner="stopped", capabilities=["stopped"])
    store.register_job_capacity(owner="live", capabilities=["live"])
    publish = store.registry.publish_job_capacity

    def unavailable(_value):
        raise OSError("registry temporarily unavailable")

    monkeypatch.setattr(store.registry, "publish_job_capacity", unavailable)
    with pytest.raises(OSError):
        store.withdraw_job_capacity("stopped", "rynmesh-main")
    monkeypatch.setattr(store.registry, "publish_job_capacity", publish)
    store.withdraw_job_capacity("stopped", "rynmesh-main")
    assert store.list_job_capacities()["capacities"][0]["capabilities"] == ["live"]


def test_default_owner_replaces_its_old_capabilities_and_metadata(store):
    store.register_job_capacity(capabilities=["old"], metadata={"old": True}, price_credits={"old": 2})
    result = store.register_job_capacity(capabilities=["new"])
    assert result["record"]["capabilities"] == ["new"]
    assert result["record"]["metadata"] == {}
    assert result["record"]["price_credits"] == {}


def test_same_owner_is_isolated_by_network(store):
    store.register_job_capacity(owner="service", capabilities=["a"], network_id="a")
    store.register_job_capacity(owner="service", capabilities=["b"], network_id="b")
    store.withdraw_job_capacity("service", "a")
    assert store.list_job_capacities(network_id="b")["capacities"][0]["capabilities"] == ["b"]


def test_manual_http_and_mcp_really_pass_manual_owner(store, monkeypatch):
    from rynmesh.mcp_server import _dispatch_tool
    from rynmesh.peer_http import create_app
    calls = observe(store, monkeypatch)
    _dispatch_tool(store, "rynmesh_register_job_capacity", {"capabilities": ["mcp"]})
    with TestClient(create_app(store), client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/local/jobs/capacity/register", json={"capabilities": ["http"]})
        assert response.status_code == 200, response.text
    assert [call.get("owner") for call in calls] == ["manual", "manual"]


def test_base_worker_refreshes_its_owner_and_withdraws_on_stop(store, monkeypatch):
    from rynmesh.services._base import ServiceWorker
    worker = ServiceWorker()
    worker.capability = "service.test"
    calls = observe(store, monkeypatch)
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "serve_once", lambda *args, **kwargs: 0)

    def sleep(_seconds):
        if now[0] > 0:
            raise KeyboardInterrupt
        now[0] = 31.0

    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        worker.serve_forever(store, network_id="test")
    assert len(calls) == 2
    assert all(call.get("owner") == worker.capability for call in calls)
    assert worker.capability not in store._advertised_capacity.get("test", {})


def test_transit_owner_uses_actual_refresh_cadence_and_stops(store, monkeypatch):
    from rynmesh.peer_transit_service import PeerTransitWorker, DEFAULT_CAPACITY_REFRESH_S
    calls = observe(store, monkeypatch)
    worker = PeerTransitWorker(store, network_id="test", role="both")
    worker.register()
    assert calls[0].get("owner") == "transit"
    assert calls[0]["polling_interval_sec"] == int(DEFAULT_CAPACITY_REFRESH_S)
    stop = threading.Event()
    stop.set()
    worker.serve_forever(stop_event=stop)
    assert "transit" not in store._advertised_capacity.get("test", {})


def test_signal50_loop_refreshes_named_owner_and_withdraws(store, monkeypatch):
    import rynmesh.signal50_service as service
    monkeypatch.setattr(service, "RynmeshStore", lambda: store)
    monkeypatch.setattr(service, "_service_once", lambda *args, **kwargs: None)
    calls = observe(store, monkeypatch)
    sleeps = []

    def sleep(_seconds):
        sleeps.append(True)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        service.main(["--network-id", "test", "--signal50-api-token", "fixture-token"])
    assert len(calls) == 2
    assert all(call.get("owner") == "signal50" for call in calls)
    assert "signal50" not in store._advertised_capacity.get("test", {})


def test_llm_publish_pause_and_refresh_revoke_real_advertisement(store, monkeypatch, openai_server):
    from rynmesh.llm_package.routes import install_llm_routes, CAPABILITY
    from rynmesh.services.peer_box import load_or_create_messaging_key
    calls = observe(store, monkeypatch)
    app = FastAPI()
    install_llm_routes(app, store=store, home=store.home,
                       messaging_key=load_or_create_messaging_key(store.home / "messaging.x25519"),
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    with TestClient(app) as client:
        response = client.post("/api/local/llm/setup", json={
            "mode": "openai-compatible", "package_id": "configured-api", "alias": "test",
            "base_url": openai_server, "model": "test-real-api"})
        assert response.status_code == 200, response.text
        response = client.post("/api/local/llm/services/publish", json={"network_id": "test", "benchmark": False})
        assert response.status_code == 200, response.text
        assert calls[-1].get("owner") == CAPABILITY
        count = len(calls)
        assert app.state.llm_agent_services["refresh_publication"]() == "published"
        assert len(calls) == count + 1
        assert calls[-1].get("owner") == CAPABILITY  # aggregate refresh, not only ProviderService.publish
        store.register_job_capacity(owner="other", capabilities=["other"], network_id="test")
        response = client.post("/api/local/llm/services/pause")
        assert response.status_code == 200, response.text
        app.state.llm_agent_services["refresh_publication"]()
        record = store.list_job_capacities(network_id="test")["capacities"][0]
        assert record["capabilities"] == ["other"]
        assert "llm_service" not in record["metadata"]
        store.withdraw_job_capacity("other", "test")
        count = len(calls)
        app.state.llm_agent_services["refresh_publication"]()
        assert len(calls) == count
