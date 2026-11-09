"""Durable authorization journal acceptance. Synthetic identities, no network."""
import copy
import hashlib
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions.company_authorization import CompanyAuthorizationChannel, STATE_KEY, record_revocation
from rynmesh.agent_runtime.extensions.local_services import LocalServiceCredentials
from rynmesh.agent_runtime.extensions.mcp_oauth import McpOAuthManager
from rynmesh.agent_runtime.extensions.routes import install_extension_routes
from rynmesh.agent_runtime.store import AgentError
from rynmesh.services.peer_box import load_or_create_messaging_key


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError('unexpected network in authorization protocol test')
    owned_ports = set()
    bind, connect, resolve = socket.socket.bind, socket.socket.connect, socket.getaddrinfo
    def owned_bind(sock, address):
        if address[0] not in {'127.0.0.1', '::1'}:
            return reject()
        result = bind(sock, address)
        owned_ports.add(sock.getsockname()[1])
        return result
    def owned_connect(sock, address):
        if address[0] not in {'127.0.0.1', '::1'} or address[1] not in owned_ports:
            return reject()
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, 'bind', owned_bind)
    monkeypatch.setattr(socket.socket, 'connect', owned_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', reject)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw:
                        resolve(host, *a, **kw) if host in {'127.0.0.1', '::1'} else reject())
    monkeypatch.setattr(CompanyAuthorizationChannel, 'start', lambda _: None)


@pytest.fixture
def setup(tmp_path):
    store = ExtensionStore(tmp_path / 'agent', load_or_create_messaging_key(tmp_path / 'key'))
    plugins = ['company-workspace', 'company-dashboard', 'company-affiliate']
    store.commit_imported_items(plugins=[{'id': p, 'mcp_server_ids': [p+'-mcp'], 'skill_ids': []} for p in plugins], skills=[],
        mcp_servers=[{'id': p+'-mcp', 'plugin_id': p, 'transport': 'streamable_http',
                      'url': 'http://127.0.0.1:19293/'+p, 'auth': {'mode': 'oauth', 'client_id': 'synthetic'}} for p in plugins])
    for p in plugins:
        store.set_plugin_enabled(p, True)
    oauth = McpOAuthManager(store, clock=lambda: 1000)
    calls = []
    oauth.company_authorization.sender = lambda url, value, headers: (calls.append(copy.deepcopy(value)) or {'sequence': value['sequence']})
    services = LocalServiceCredentials(store, SimpleNamespace(oauth=oauth, invalidate_server=lambda _: None))
    prepared = services.prepare(service_id='company', plugin_ids=plugins,
        server_ids=[p+'-mcp' for p in plugins], port=19293)
    for p in plugins:
        server = store.get_mcp_server_internal(p+'-mcp')
        oauth._row(server)
        with oauth.credentials.transaction() as rows:
            rows[server['instance_id']]['tokens'] = {'access_token': 'synthetic-'+p, 'expires_at': 4600,
                                                     'refresh_token': 'synthetic-shared-refresh'}
    source = store.get_mcp_server_internal('company-workspace-mcp')
    with oauth.credentials.transaction(document=True) as document:
        document['company_action'] = {'id': 'synthetic-login', 'action': 'login', 'serverId': source['id'], 'instanceId': source['instance_id']}
    yield store, oauth, services, prepared, calls
    oauth.close()


def state(oauth):
    return oauth.credentials.read()[STATE_KEY]


def test_lost_send_persists_and_replays_after_host_restart(setup):
    store, oauth, _, _, calls = setup
    oauth.company_authorization.sender = lambda *args: (_ for _ in ()).throw(httpx.ConnectError('synthetic stopped service'))
    result = oauth.company_authorization.revoke(['company-dashboard'], 'disabled')
    assert result['pending'] and state(oauth)['revocations'] == {'company-dashboard': 1}
    restarted = McpOAuthManager(store, clock=lambda: 1000)
    restarted.company_authorization.sender = lambda url, payload, headers: (calls.append(payload) or {'sequence': payload['sequence']})
    assert restarted.company_authorization.flush()
    assert calls[-1]['revocations'] == {'company-dashboard': 1}
    assert state(restarted)['acknowledged'] == 1
    restarted.close()


def test_applied_before_lost_ack_is_idempotently_replayed(setup):
    _, oauth, _, _, _ = setup
    applied = []
    def missing_ack(url, payload, headers):
        applied.append(payload)
        raise httpx.ReadTimeout('synthetic ACK lost')
    oauth.company_authorization.sender = missing_ack
    oauth.company_authorization.revoke(['*'], 'logout')
    assert state(oauth)['acknowledged'] < 1
    oauth.company_authorization.sender = lambda url, payload, headers: (applied.append(payload) or {'sequence': payload['sequence']})
    assert oauth.company_authorization.flush()
    assert applied[0] == applied[1]


def test_cumulative_watermarks_keep_scoped_revocation_across_duplicates(setup):
    _, oauth, _, prepared, calls = setup
    channel = oauth.company_authorization
    channel.revoke(['company-dashboard'], 'disabled')
    channel.revoke(['company-affiliate'], 'removed')
    assert calls[-1]['revocations'] == {'company-dashboard': 1, 'company-affiliate': 2}
    channel.acknowledge(prepared['credential'], prepared['generation'], 1)
    assert state(oauth)['acknowledged'] == 2
    with pytest.raises(AgentError):
        channel.acknowledge(prepared['credential'], prepared['generation'], 99)


def test_new_service_generation_requires_full_replay_and_rejects_old_secret(setup):
    _, oauth, services, previous, calls = setup
    oauth.company_authorization.revoke(['*'], 'logout')
    prepared = services.prepare(service_id='company', plugin_ids=list(state(oauth)['service']['plugin_ids']),
        server_ids=['company-workspace-mcp'], port=19293)
    assert state(oauth)['acknowledged'] == -1
    with pytest.raises(AgentError):
        oauth.company_authorization.snapshot(previous['credential'], previous['generation'])
    result = oauth.company_authorization.snapshot(prepared['credential'], prepared['generation'])
    assert result['revocations']['*'] == 1
    assert oauth.company_authorization.flush()


def test_single_logout_retains_other_grants_and_does_not_revoke_shared_remote_token(setup, monkeypatch):
    store, oauth, _, _, _ = setup
    revokes = []
    monkeypatch.setattr(oauth, '_revoke', lambda row: revokes.append(row))
    oauth.logout('company-dashboard-mcp')
    assert state(oauth)['revocations'].keys() == {'company-dashboard'}
    assert revokes == []
    assert oauth.public_status(store.get_mcp_server_internal('company-affiliate-mcp')['instance_id'])['state'] == 'logged_in'


def test_company_logout_clears_tokens_and_commits_revoke_even_service_down(setup):
    _, oauth, _, _, _ = setup
    oauth.company_authorization.sender = lambda *args: (_ for _ in ()).throw(httpx.ConnectError('offline'))
    result = oauth.logout_company()
    assert result['cleared'] and result['authorizationDelivery']['pending']
    assert oauth.credentials.read()['servers'] == {}
    assert state(oauth)['revocations'].keys() == {'*'}
    with pytest.raises(AgentError):
        oauth.company_authorization.bootstrap('company-workspace')


@pytest.mark.parametrize('mutation', ['delete', 'disable', 'issuer'])
def test_startup_reconciles_crash_after_config_commit_before_notification(setup, mutation):
    store, oauth, _, _, _ = setup
    sid = 'company-dashboard-mcp'
    if mutation == 'delete':
        store.delete_mcp_server(sid)
    elif mutation == 'disable':
        store.set_plugin_enabled('company-dashboard', False)
    else:
        store.upsert_mcp_server({'url': 'https://new.example.test/mcp'}, server_id=sid)
    restarted = McpOAuthManager(store, clock=lambda: 1000)
    assert 'company-dashboard' in state(restarted)['revocations']
    assert 'company-workspace' not in state(restarted)['revocations']
    restarted.close()


def test_tool_refresh_equivalent_import_and_token_renewal_do_not_revoke(setup):
    store, oauth, _, _, _ = setup
    server = store.get_mcp_server_internal('company-dashboard-mcp')
    store.upsert_mcp_server({'description': 'same authorization, changed display'}, server_id=server['id'])
    current = store.get_mcp_server_internal(server['id'])
    store.record_mcp_discovery(current['id'], expected_revision=current['revision'], status='ready', discovered_tools=[])
    with oauth.credentials.transaction() as rows:
        rows[server['instance_id']]['tokens']['access_token'] = 'synthetic-renewed'
        rows[server['instance_id']]['tokens']['expires_at'] = 8000
    oauth.company_authorization.sync_bindings()
    assert state(oauth)['sequence'] == 0 and state(oauth)['revocations'] == {}


def test_launch_transport_rotation_keeps_oauth_and_account_identity(setup):
    store, oauth, services, prepared, _ = setup
    server = store.get_mcp_server_internal('company-workspace-mcp')
    before = oauth.public_status(server['instance_id'])
    services.activate(service_id='company', generation=prepared['generation'])
    assert oauth.public_status(server['instance_id']) == before
    oauth.company_authorization.sync_bindings()
    assert state(oauth)['sequence'] == 0


def test_bootstrap_only_sends_hash_and_matches_current_action_and_lease(setup):
    _, oauth, _, _, calls = setup
    result = oauth.company_authorization.bootstrap('company-workspace')
    payload = calls[-1]['bootstrap']
    assert payload['ticketHash'] == hashlib.sha256(result['ticket'].encode()).hexdigest()
    assert result['ticket'] not in json.dumps(state(oauth))
    assert payload['sessionExpiresAt'] == 4600 and payload['actionId'] == 'synthetic-login'
    assert set(payload['authorizedScopes']) == {'company-workspace', 'company-dashboard', 'company-affiliate'}
    assert 'synthetic-shared-refresh' not in json.dumps(calls)
    with pytest.raises(AgentError):
        oauth.company_authorization.bootstrap('office')


def test_http_sync_requires_registered_secret_loopback_and_no_browser_origin(setup, tmp_path):
    store, oauth, _, prepared, _ = setup
    app = FastAPI()
    extensions = SimpleNamespace(store=store, manager=SimpleNamespace(oauth=oauth))
    runtime = SimpleNamespace(store=SimpleNamespace(root=tmp_path, inspect=lambda fn: fn({'settings': {}})))
    install_extension_routes(app, runtime=runtime, extensions=extensions, local_control=lambda request: None)
    path = '/api/local/agent/extensions/company/authorization-state'
    headers = {'X-Ryn-Service-Token': prepared['credential'], 'X-Ryn-Service-Generation': prepared['generation']}
    with TestClient(app, client=('127.0.0.1', 10001)) as client:
        assert client.post(path, json={}, headers=headers).status_code == 200
        assert client.post(path, json={}).status_code == 403
        assert client.post(path, json={}, headers={**headers, 'Origin': 'http://evil.test'}).status_code == 403
        assert client.post(path, json={}, headers={**headers, 'Referer': 'http://127.0.0.1/'}).status_code == 403
    with TestClient(app, client=('192.0.2.1', 10001)) as client:
        assert client.post(path, json={}, headers=headers).status_code == 403


def test_scoped_revocation_cannot_be_undone_by_bootstrap_or_lease_renewal(setup):
    _, oauth, _, prepared, calls = setup
    oauth.company_authorization.revoke(['company-affiliate'], 'single_logout')
    oauth.company_authorization.bootstrap('company-workspace')
    assert 'company-affiliate' not in calls[-1]['bootstrap']['authorizedScopes']
    snapshot = oauth.company_authorization.snapshot(prepared['credential'], prepared['generation'])
    assert 'company-affiliate' not in snapshot['leases']['company-workspace-mcp']['authorizedScopes']


def test_bootstrap_route_requires_owner_and_refuses_other_loopback_webpages(setup, tmp_path):
    from fastapi import HTTPException
    store, oauth, _, _, _ = setup
    app = FastAPI()
    def owner(request):
        if request.headers.get('X-Synthetic-Owner') != 'yes':
            raise HTTPException(401)
    install_extension_routes(app,
        runtime=SimpleNamespace(store=SimpleNamespace(root=tmp_path, inspect=lambda fn: fn({'settings': {}}))),
        extensions=SimpleNamespace(store=store, manager=SimpleNamespace(oauth=oauth)), local_control=owner)
    path = '/api/local/agent/extensions/company/browser-bootstrap'
    with TestClient(app, client=('127.0.0.1', 10001)) as client:
        assert client.post(path, json={'plugin_id':'company-workspace'}).status_code == 401
        for origin in ['http://127.0.0.1:18140', 'https://evil.test', 'null']:
            assert client.post(path, json={'plugin_id':'company-workspace'},
                               headers={'X-Synthetic-Owner':'yes', 'Origin':origin}).status_code == 403
        assert client.post(path, json={'plugin_id':'company-workspace'},
                           headers={'X-Synthetic-Owner':'yes', 'Origin':'http://tauri.localhost'}).status_code == 200


def test_explicit_logout_does_not_silently_borrow_shared_identity_again(setup, monkeypatch):
    store, oauth, _, _, _ = setup
    data = store._read()
    for server in data['mcp_servers'].values():
        server['identity_group_id'] = 'same-company-identity'
        server['url'] = 'http://127.0.0.1:19293/shared'
    store._write(data)
    for server in store.list_mcp_servers_internal(include_tools=False):
        oauth._row(server)
        with oauth.credentials.transaction() as rows:
            rows[server['instance_id']]['tokens'] = {'access_token':'synthetic-shared',
                'refresh_token':'synthetic-refresh', 'expires_at':4600}
    monkeypatch.setattr(oauth, '_revoke', lambda row: (_ for _ in ()).throw(AssertionError('must not revoke shared token')))
    assert oauth.logout('company-dashboard-mcp')['state'] == 'logged_out'
    for _ in range(3):
        target = store.get_mcp_server_internal('company-dashboard-mcp')
        assert oauth.public_status(target['instance_id'])['state'] == 'logged_out'
    source = store.get_mcp_server_internal('company-workspace-mcp')
    oauth._propagate_identity_group_tokens(source)
    assert oauth.public_status(target['instance_id'])['state'] == 'logged_out'


def test_lease_revision_orders_expiry_shrink_and_does_not_change_account_action(setup):
    store, oauth, _, prepared, _ = setup
    source = store.get_mcp_server_internal('company-workspace-mcp')
    row = oauth._row(source)
    action = oauth.company_action()
    oauth._update(source, row['epoch'], lambda value: value['tokens'].update(expires_at=2000), refresh=True)
    lease = oauth.company_authorization.snapshot(prepared['credential'], prepared['generation'])['leases'][source['id']]
    assert lease['expiresAt'] == 2000 and lease['revision'] == 1
    assert oauth.company_action() == action
    assert state(oauth)['revocations'] == {}


def test_foreign_registration_cannot_redirect_notifications(setup):
    store, oauth, _, _, _ = setup
    before = state(oauth)['service']
    with pytest.raises(AgentError):
        oauth.company_authorization.register(service_id='company',
            plugin_ids=['company-workspace'], server_ids=['company-workspace-mcp'],
            port=19294, credential='foreign', generation='foreign')
    assert state(oauth)['service'] == before


def test_late_ack_from_prior_service_cannot_ack_new_generation(setup):
    _, oauth, _, prepared, _ = setup
    channel = oauth.company_authorization
    with oauth.credentials.transaction(document=True) as document:
        document[STATE_KEY]['service']['generation'] = 'new-generation'
        document[STATE_KEY]['acknowledged'] = -1
    with pytest.raises(AgentError):
        channel.acknowledge(prepared['credential'], prepared['generation'], 0)
    assert state(oauth)['acknowledged'] == -1


def test_bootstrap_racing_with_source_logout_fails_closed(setup, monkeypatch):
    store, oauth, _, _, _ = setup
    channel = oauth.company_authorization
    original = channel._authorized_scopes
    def racing_scopes():
        scopes = original()
        with oauth.credentials.transaction() as rows:
            rows[store.get_mcp_server_internal('company-workspace-mcp')['instance_id']].pop('tokens')
        return scopes
    monkeypatch.setattr(channel, '_authorized_scopes', racing_scopes)
    with pytest.raises(AgentError):
        channel.bootstrap('company-workspace')
    assert 'bootstrap' not in state(oauth)


def test_remote_refresh_hang_does_not_hold_revocation_delivery_lock(setup, monkeypatch):
    import threading
    import time
    store, oauth, _, _, calls = setup
    source = store.get_mcp_server_internal('company-workspace-mcp')
    with oauth.credentials.transaction() as rows:
        rows[source['instance_id']]['tokens']['expires_at'] = 1010
    entered, release = threading.Event(), threading.Event()
    def stalled(server):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(oauth, 'access_token', stalled)
    oauth.company_authorization._start_renewal()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        result = oauth.company_authorization.revoke(['company-affiliate'], 'disabled')
        assert result['acknowledged']
        assert time.monotonic() - started < 1
        assert calls[-1]['revocations'] == {'company-affiliate': 1}
    finally:
        release.set()


def test_scope_downgrade_is_durable_but_normal_refresh_is_not_logout(setup):
    store, oauth, _, _, _ = setup
    source = store.get_mcp_server_internal('company-dashboard-mcp')
    row = oauth._row(source)
    with oauth.credentials.transaction() as rows:
        rows[source['instance_id']]['granted_scopes'] = ['read', 'write']
    oauth._update(source, row['epoch'], lambda r: r.update(granted_scopes=['read']), refresh=True)
    assert state(oauth)['revocations'] == {'company-dashboard': 1}
    oauth._update(source, row['epoch'], lambda r: r['tokens'].update(access_token='rotated'), refresh=True)
    assert state(oauth)['revocations'] == {'company-dashboard': 1}


def test_non_identity_connection_login_never_replaces_company_person_or_lease(setup):
    store, oauth, _, prepared, _ = setup
    action = oauth.company_action()
    source = store.get_mcp_server_internal('company-affiliate-mcp')
    row = oauth._row(source)
    oauth.company_authorization.revoke(['company-affiliate'], 'single_logout')
    oauth._update(source, row['epoch'], lambda r: r['tokens'].update(access_token='new-affiliate-grant'),
                  login_id='explicit-affiliate-login')
    assert oauth.company_action() == action
    assert '*' not in state(oauth)['revocations']
    assert state(oauth)['scope_grants']['company-affiliate'] > state(oauth)['revocations']['company-affiliate']
    lease = oauth.company_authorization.snapshot(prepared['credential'], prepared['generation'])['leases']
    assert set(lease) == {'company-workspace-mcp'}
    assert 'company-affiliate' in lease['company-workspace-mcp']['authorizedScopes']


def test_exact_company_reimport_preserves_enabled_state_and_browser_authority(setup):
    store, oauth, _, _, _ = setup
    plugin = 'company-dashboard'
    server = store.get_mcp_server_internal(plugin+'-mcp')
    before = oauth.public_status(server['instance_id'])
    store.commit_imported_items(plugins=[{'id':plugin, 'mcp_server_ids':[plugin+'-mcp'], 'skill_ids':[]}], skills=[],
        mcp_servers=[{'id':plugin+'-mcp', 'plugin_id':plugin, 'transport':'streamable_http',
                      'url':'http://127.0.0.1:19293/'+plugin, 'auth':{'mode':'oauth', 'client_id':'synthetic'}}])
    current = store.get_mcp_server_internal(plugin+'-mcp')
    assert current['effective_enabled']
    assert oauth.public_status(server['instance_id']) == before
    oauth.company_authorization.sync_bindings()
    assert state(oauth)['revocations'] == {}
    store.commit_imported_items(plugins=[{'id':plugin, 'mcp_server_ids':[plugin+'-mcp'], 'skill_ids':[]}], skills=[],
        mcp_servers=[{'id':plugin+'-mcp', 'plugin_id':plugin, 'transport':'streamable_http',
                      'url':'https://different.example.test/mcp', 'auth':{'mode':'oauth', 'client_id':'synthetic'}}])
    assert not store.get_mcp_server_internal(plugin+'-mcp')['effective_enabled']
    oauth.company_authorization.sync_bindings()
    assert 'company-dashboard' in state(oauth)['revocations']


def pending_event(oauth, *, server_id=None, event_id='synthetic-logout-event-0001'):
    return {'eventId':event_id, 'serverId':server_id, 'expectedActionId':oauth.company_action()['id'],
            'observedSequence':state(oauth)['sequence']}


def test_service_logout_atomically_deduplicates_lost_ack_across_restart(setup):
    store, oauth, _, prepared, _ = setup
    event = pending_event(oauth)
    channel = oauth.company_authorization
    ack = channel.apply_service_logouts(prepared['credential'], prepared['generation'], [event])
    assert ack == [{'eventId':event['eventId'], 'applied':True}]
    assert oauth.company_action()['action'] == 'logout'
    assert all(not row.get('tokens') for row in oauth.credentials.read()['servers'].values())
    sequence = state(oauth)['sequence']
    restarted = McpOAuthManager(store, clock=lambda:1000)
    assert restarted.company_authorization.apply_service_logouts(prepared['credential'], prepared['generation'], [event]) == ack
    assert state(restarted)['sequence'] == sequence
    restarted.close()


def test_old_user_service_logout_acknowledges_without_revoking_new_user(setup):
    _, oauth, _, prepared, _ = setup
    event = pending_event(oauth)
    with oauth.credentials.transaction(document=True) as document:
        document['company_action']['id'] = 'new-user-login'
    result = oauth.company_authorization.apply_service_logouts(prepared['credential'], prepared['generation'], [event])
    assert result == [{'eventId':event['eventId'], 'applied':False}]
    assert oauth.company_action()['id'] == 'new-user-login'
    assert all(row.get('tokens') for row in oauth.credentials.read()['servers'].values())
    assert state(oauth)['revocations'] == {}


def test_queued_single_logout_cannot_kill_later_explicit_connection_grant(setup):
    store, oauth, _, prepared, _ = setup
    event = pending_event(oauth, server_id='company-affiliate-mcp')
    server = store.get_mcp_server_internal('company-affiliate-mcp')
    row = oauth._row(server)
    oauth._update(server, row['epoch'], lambda r:r['tokens'].update(access_token='new-grant'), login_id='explicit-new-grant')
    result = oauth.company_authorization.apply_service_logouts(prepared['credential'], prepared['generation'], [event])
    assert result[0]['applied'] is False
    assert oauth.credentials.read()['servers'][server['instance_id']]['tokens']['access_token'] == 'new-grant'


def test_service_single_logout_is_scoped_and_unregistered_caller_cannot_mutate(setup):
    store, oauth, _, prepared, _ = setup
    event = pending_event(oauth, server_id='company-dashboard-mcp')
    with pytest.raises(AgentError):
        oauth.company_authorization.apply_service_logouts('wrong', prepared['generation'], [event])
    result = oauth.company_authorization.apply_service_logouts(prepared['credential'], prepared['generation'], [event])
    assert result[0]['applied'] is True
    assert oauth.public_status(store.get_mcp_server_internal('company-dashboard-mcp')['instance_id'])['state'] == 'logged_out'
    assert oauth.public_status(store.get_mcp_server_internal('company-workspace-mcp')['instance_id'])['state'] == 'logged_in'
    assert set(state(oauth)['revocations']) == {'company-dashboard'}
