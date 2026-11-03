from types import SimpleNamespace

from test_agent_space_binding import space_pair  # noqa: F401
from test_personal_space import nodes  # noqa: F401

from rynmesh.llm_package.lan_space_catalog import LanSpaceCatalogRefresh


def test_lan_discovery_authenticates_real_space_catalog_without_registry(space_pair):
    p = space_pair
    store = p.child.store
    store.lan_peer_endpoints = lambda peer, **kw: ['http://127.0.0.1:8791'] if peer == p.root.store.peer_id else []
    def no_registry(**kwargs):
        raise AssertionError('LAN refresh must not use the registry')
    store.list_job_capacities = no_registry
    refresh = LanSpaceCatalogRefresh(store, p.consumer,
        read_key=lambda endpoint, peer: p.locator['node_messaging_pub'])
    assert refresh.refresh_once('rynmesh-main')
    rows = p.consumer.records()
    assert len(rows) == 1
    assert rows[0]['peer_id'] == p.root.store.peer_id
    assert rows[0]['binding_verified'] is True
    assert not refresh.refresh_once('rynmesh-main')


def test_lan_discovery_skips_unpermitted_and_removed_members():
    calls = []
    space = SimpleNamespace(status=lambda: {'membership': 'active', 'members': [
        {'peer_id': 'self'}, {'peer_id': 'denied'}, {'peer_id': 'removed', 'removed': True}]},
        allows_ai=lambda peer: peer != 'denied')
    store = SimpleNamespace(peer_id='self', personal_space=space,
        lan_peer_endpoints=lambda *a, **kw: calls.append(a))
    refresh = LanSpaceCatalogRefresh(store, None)
    assert not refresh.refresh_once('rynmesh-main')
    assert calls == []


def test_failed_peer_is_bounded_and_does_not_starve_another_member():
    calls = []
    space = SimpleNamespace(status=lambda: {'membership': 'active', 'members': [
        {'peer_id': 'offline'}, {'peer_id': 'online'}]}, allows_ai=lambda peer: True)
    store = SimpleNamespace(peer_id='self', personal_space=space,
        lan_peer_endpoints=lambda peer, **kw: [peer])
    def read_key(endpoint, peer):
        calls.append(peer)
        if peer == 'offline':
            raise OSError('unreachable')
        return 'key'
    catalog = SimpleNamespace(refresh=lambda row: None)
    refresh = LanSpaceCatalogRefresh(store, catalog, read_key=read_key, clock=lambda: 100)
    assert not refresh.refresh_once('rynmesh-main')
    assert refresh.refresh_once('rynmesh-main')
    assert calls == ['offline', 'online']
