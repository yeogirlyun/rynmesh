"""Shared-app discovery through the signed peer list and owner API."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_node_sites import Authority, RESOURCE, SiteNode, loopback_only, pair, publish
from test_public_plugins import setup


def test_grouping_cache_and_colleague_counts(pair, tmp_path):
    opener, first, _, backend = pair
    second = SiteNode(tmp_path, "second").start()
    try:
        authority = Authority([opener, first, second])
        authority._rows[2]["member_id"] = authority._rows[1]["member_id"]
        for node in (opener, first, second):
            node.e._provider_peers = authority.rows
        directory = {"resource": RESOURCE, "members": [
            {"id": "m-0", "devices": [authority._rows[0]]},
            {"id": "m-1", "devices": authority._rows[1:]},
            {"id": "no-device", "devices": []}]}
        opener.app.state.peer_providers = SimpleNamespace(_cached=lambda: [("plugin", directory)])
        publish(first, backend)
        api = TestClient(opener.app)
        url = "/api/local/node-sites/shared-apps?refresh=true"
        result = api.get(url).json()
        assert (result["colleagues_total"], result["sharing_total"]) == (2, 1)
        assert [p["peer_id"] for p in result["apps"][0]["providers"]] == [first.e.me]
        publish(second, backend)
        summary = {"state": "busy", "queue_length": 2, "eta_seconds": 90}
        first.sites.host.summaries["image-studio"] = lambda: summary
        result = api.get(url).json()
        app, = result["apps"]
        assert app["site_id"] == "image-studio" and len(app["providers"]) == 2
        by_peer = {p["peer_id"]: p for p in app["providers"]}
        assert by_peer[first.e.me]["summary"] == summary
        assert by_peer[second.e.me]["summary"] is None
        first_id = first.e.me
        first.stop()
        opener.e.provider_directory(RESOURCE).forget(first_id)
        opener.sites.client._lists.clear()
        providers = api.get(url).json()["apps"][0]["providers"]
        assert [(p["peer_id"], p["online"]) for p in providers] == [(second.e.me, True), (first_id, False)]
        assert providers[1]["last_seen"] and providers[1]["summary"] == summary
        assert next(p for p in opener.sites.client.remote() if p["peer_id"] == first_id)["sites"] == []
    finally:
        second.stop()


@pytest.mark.parametrize("bad", [
    {"state": "other", "queue_length": 0, "eta_seconds": 0},
    {"state": "idle", "queue_length": True, "eta_seconds": 0},
    {"state": "busy", "queue_length": 1, "eta_seconds": 2**40},
    lambda: 1 / 0,
])
def test_invalid_or_failed_summary_keeps_site(pair, bad):
    opener, host, _, backend = pair
    publish(host, backend)
    host.sites.host.summaries["image-studio"] = bad if callable(bad) else lambda: bad
    site, = opener.sites.client.list_sites(RESOURCE, host.e.me)
    assert site["site_id"] == "image-studio" and "summary" not in site


def test_image_summary_plugin_lifecycle(setup, monkeypatch):
    manager, sites, _, _, _ = setup
    manager.refresh()
    manager.install("image-studio")
    summary = sites.host.summaries["image-studio"]
    assert summary() == {"state": "idle", "queue_length": 0, "eta_seconds": 0}
    runtime = manager.running["image-studio"][0]
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "queue", lambda: [{}, {}])
        patch.setattr(runtime.store, "average_seconds", lambda: 7.5)
        assert summary() == {"state": "busy", "queue_length": 2, "eta_seconds": 15}
    manager.action("image-studio", "disable")
    assert "image-studio" not in sites.host.summaries
