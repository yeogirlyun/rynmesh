"""S13-I70: exercise registered workers, real stores and owner HTTP routes."""
import inspect
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import routes
from rynmesh.llm_package.sources import SourceStore
from rynmesh.llm_package.task_protocol import TaskOrderStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.fixture
def node(tmp_path, monkeypatch):
    home = tmp_path / 'node'
    monkeypatch.setenv('RYNMESH_HOME', str(home))
    store = RynmeshStore(home=home, network_dir=tmp_path / 'network')
    app = FastAPI()
    routes.install_llm_routes(app, store=store, home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / 'messaging.x25519'),
        resolve_endpoint=lambda _: '', resolve_pubkey=lambda _: '')
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/local/llm/orders')
    orders = inspect.getclosurevars(endpoint).nonlocals['consumer_orders']
    yield app, store, orders, app.state.llm_agent_services
    app.state.llm_shutdown()


def worker(app, name='llm.consumer-orders.maintenance'):
    spec = next((spec for spec in app.state.background_workers.specs() if spec.name == name), None)
    assert spec is not None, f'production worker {name} is not registered'
    return spec


def seed(orders, task_id, *, days=8, state='succeeded', dispatched=True, pending=False, awaiting=False):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    completed = (datetime.now(timezone.utc) - timedelta(days=1 if awaiting else days)).isoformat()
    history = [{'state': state, 'at': completed, 'amount': 1, 'billing_basis': 'own_request',
                'service_id': 'example-service', 'provider_peer_id': 'example-peer',
                'network_id': 'unit-network', 'settlement_pending': pending}]
    if dispatched:
        history.append({'state': state, 'at': old, 'checkpoint': True, 'settlement_dispatched': True})
    value = {'task_id': task_id, 'state': state, 'created_at': old, 'updated_at': old,
             'completed_at': completed, 'bindings': {}, 'history': history}
    if awaiting:
        value['encrypted_response'] = {'ciphertext': 'unit-ciphertext'}
    orders._write(value)
    return value


def test_t1_maintenance_prunes_old_settled_terminal_and_preserves_pending_and_archive(node, monkeypatch):
    app, _, orders, _ = node
    seed(orders, 'old-settled')
    seed(orders, 'unsettled', dispatched=False, pending=True)
    seed(orders, 'awaiting-archive', awaiting=True)
    seed(orders, 'recent', days=2)
    seed(orders, 'running', state='running', dispatched=False)
    monkeypatch.setattr(routes, 'dispatch_settlement', lambda *args, **kwargs: False)
    spec = worker(app)
    assert spec.policy.busy_delay_s == spec.policy.idle_max_s == 600
    spec.run_once()
    assert orders.get('old-settled') is None
    assert {record['task_id'] for record in orders.list()} == {'unsettled', 'awaiting-archive', 'recent', 'running'}


def test_t1_retention_is_configurable_for_longer_history(node, monkeypatch):
    app, _, orders, _ = node
    monkeypatch.setenv('RYNMESH_LLM_CONSUMER_RETENTION_DAYS', '30')
    seed(orders, 'eight-days')
    seed(orders, 'thirty-one-days', days=31)
    worker(app).run_once()
    assert orders.get('eight-days') is not None
    assert orders.get('thirty-one-days') is None


def test_t2_orders_default_limit_and_cursor_have_no_duplicates(node):
    app, _, orders, _ = node
    for index in range(70):
        value = seed(orders, f'order-{index:03d}', days=1)
        value['updated_at'] = (datetime.now(timezone.utc) + timedelta(seconds=index)).isoformat()
        orders._write(value)
    with TestClient(app) as client:
        first = client.get('/api/local/llm/orders').json()
        assert len(first['orders']) == 50
        second = client.get('/api/local/llm/orders', params={'cursor': first['next_cursor']}).json()
        assert len(second['orders']) == 20 and not second['next_cursor']
        ids = [row['task_id'] for page in (first, second) for row in page['orders']]
        assert ids == [f'order-{index:03d}' for index in reversed(range(70))]
        limited = client.get('/api/local/llm/orders', params={'limit': 3}).json()
        assert len(limited['orders']) == 3 and limited['next_cursor']


def test_t3_worker_retries_real_dispatch_with_per_order_backoff_and_durable_checkpoint(node, monkeypatch, caplog):
    app, store, orders, _ = node
    seed(orders, 'retry', days=1, dispatched=False, pending=True)
    attempts = []
    clock = [1000.0]
    monkeypatch.setattr(routes.time, 'time', lambda: clock[0])

    def submit(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError('PRIVATE_SETTLEMENT_CANARY')
        return {'ok': True}

    monkeypatch.setattr(store, 'submit_work_order', submit)
    spec = worker(app)
    spec.run_once()
    record = orders.get('retry')
    checkpoint = record['history'][-1]
    assert checkpoint['settlement_retry_attempts'] == 1
    assert checkpoint['settlement_retry_at'] > clock[0]
    assert not any(event.get('settlement_dispatched') for event in record['history'])
    spec.run_once()
    assert len(attempts) == 1
    clock[0] = checkpoint['settlement_retry_at'] + 1
    spec.run_once()
    history = orders.get('retry')['history']
    assert history[-1]['settlement_dispatched'] is True
    assert history[-1]['settlement_retry_attempts'] == 2
    assert len(attempts) == 2
    assert attempts[-1]['idempotency_key'] == 'settle:retry'
    assert attempts[-1]['network_id'] == 'unit-network'
    assert 'PRIVATE_SETTLEMENT_CANARY' not in caplog.text
    from rynmesh import diagnostic_events
    assert any(event['exception_type'] == 'OSError' and event['component'] == 'llm_settlement'
               for event in diagnostic_events.recent())
    spec.run_once()
    assert len(attempts) == 2


def test_t3_pending_usage_is_never_dispatched_or_pruned(node, monkeypatch):
    app, store, orders, _ = node
    value = seed(orders, 'unknown-usage', dispatched=False, pending=True)
    value['history'][0].update({'amount': None, 'billing_pending_usage': True})
    orders._write(value)
    calls = []
    monkeypatch.setattr(store, 'submit_work_order', lambda **kwargs: calls.append(kwargs))
    worker(app).run_once()
    assert calls == [] and orders.get('unknown-usage') is not None


def test_t3_restart_recovery_respects_persisted_backoff(node, monkeypatch):
    app, store, orders, _ = node
    seed(orders, 'later', days=1, dispatched=False, pending=True)
    orders.checkpoint(task_id='later', metadata={
        'settlement_retry_attempts': 3, 'settlement_retry_at': routes.time.time() + 3600})
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, 'path', '') == '/api/local/task-balance')
    balance = inspect.getclosurevars(endpoint).nonlocals['balance']
    calls = []
    monkeypatch.setattr(routes, 'dispatch_settlement', lambda *a, **kw: (calls.append(kw), True)[1])
    routes._recover_consumer_orders(orders, balance, store)
    assert calls == []


def configured_source(services):
    sources = services['source_store']
    record = sources.prepare({'name': 'Cache A', 'kind': 'api', 'provider': 'openai',
        'base_url': 'https://api.example.test/v1', 'models': ['example-model'],
        'api_key': 'unit-test-placeholder-key'})
    record['status'] = 'ready'
    sources.save(record)
    return sources, record


def test_t4_ten_unchanged_manager_lookups_decrypt_once_and_provider_uses_same_cache(node, monkeypatch):
    _, _, _, services = node
    sources, record = configured_source(services)
    active = services['active_source_managers']
    managers = active()
    sources._write(sources.read())  # same configuration, new write generation
    decrypted = []
    decrypt = sources.cipher.decrypt
    monkeypatch.setattr(sources.cipher, 'decrypt', lambda *a, **kw: (decrypted.append(1), decrypt(*a, **kw))[1])
    for _ in range(10):
        assert active() == managers
    assert len(decrypted) == 1
    service_id = sources.model_service_id(record, 'example-model')
    assert services['provider_for'](service_id) is managers[service_id]
    assert len(decrypted) == 1


def test_t4_write_generation_invalidates_same_stat_and_external_edits_and_deletion_invalidate(node):
    _, store, _, services = node
    sources, record = configured_source(services)
    active = services['active_source_managers']
    service_id = sources.model_service_id(record, 'example-model')
    active()
    initial_stat = sources.path.stat()
    record['base_url'] = 'https://apj.example.test/v1'
    sources._write({record['id']: record})
    os.utime(sources.path, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    assert sources.path.stat().st_size == initial_stat.st_size
    assert active()[service_id].manifest.base_url == 'https://apj.example.test/v1'
    other = SourceStore(store.home, store.private_key_bytes, migrate=False)
    record['enabled'] = False
    other._write({record['id']: record})
    os.utime(sources.path, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns + 10000000))
    assert service_id not in active()
    sources.path.unlink()
    assert service_id not in active()


def test_t5_permission_worker_backs_off_without_shared_tasks_and_is_busy_with_active_task(node, monkeypatch):
    app, _, _, services = node
    sources, record = configured_source(services)
    service_id = sources.model_service_id(record, 'example-model')
    service = services['active_source_managers']()[service_id]
    spec = worker(app, 'llm.friend-permissions')
    assert spec.policy.busy_delay_s == 1
    assert spec.policy.idle_max_s == 30 and spec.policy.idle_initial_s > 1
    assert not spec.run_once()
    monkeypatch.setattr(service, 'recheck_permissions', lambda: False)
    with service._lock:
        service._active_permissions['active-task'] = ('example-peer', {})
    assert spec.run_once()
