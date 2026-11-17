"""S01-I42: discovery outages preserve authority only for the same CLI."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_agent_peer_cli import cli_pair, nodes, space_pair  # noqa: F401

from rynmesh.agent_runtime import data_policy, retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import cli_models
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_binding import CLIModelDispatchAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest


@pytest.fixture
def directory(tmp_path, monkeypatch):
    command = tmp_path / 'cli.exe'
    command.write_bytes(b'v1')
    calls = []
    inner = SimpleNamespace(kind='codex_cli', _command_path=lambda: str(command),
                            chat=lambda body, **kw: calls.append(kw) or {'text': 'ok'})
    manifest = LLMPackageManifest(package_id='codex-cli', mode='codex_cli', adapter='codex_cli',
                                  public_model_alias='Fixture CLI')
    adapter = CLIModelDispatchAdapter(inner, manifest, tmp_path)
    models = [{'id': 'model-a', 'default': True, 'context_window': 128000,
               'supported_reasoning_efforts': ['low', 'high']}]
    monkeypatch.setattr(cli_models, 'list_models', lambda *a, **kw: copy.deepcopy(models))
    adapter.current_models()
    return SimpleNamespace(adapter=adapter, command=command, models=models, calls=calls)


@pytest.mark.parametrize('failure', [AdapterError('offline'), OSError('offline'), TimeoutError('timeout'), None])
def test_t1_same_fingerprint_keeps_snapshot_and_dispatches_without_discovery(directory, monkeypatch, failure):
    adapter = directory.adapter
    bound = adapter.binding_for_model('model-a')
    def fail(*a, **kw):
        if failure is not None:
            raise failure
        return []
    monkeypatch.setattr(cli_models, 'list_models', fail)
    assert adapter.current_models(refresh=True) == directory.models
    assert adapter.diagnostics['discovery_stale'] is True
    monkeypatch.setattr(cli_models, 'list_models', lambda *a, **kw: pytest.fail('dispatch ran discovery'))
    assert adapter.binding_for_model('model-a') == bound
    monkeypatch.setattr(cli_models, 'cached_models', lambda *a: [])
    assert adapter.cached_binding_for_model('model-a') == bound
    assert adapter.call_bound('chat', bound, lambda: None, {}, model='model-a') == {'text': 'ok'}
    assert directory.calls == [{'model': 'model-a'}]
    monkeypatch.setattr(cli_models, 'list_models', lambda *a, **kw: copy.deepcopy(directory.models))
    assert adapter.current_models(refresh=True) == directory.models
    assert adapter.diagnostics['discovery_stale'] is False


@pytest.mark.parametrize('change', ['executable', 'manifest', 'no_snapshot'])
@pytest.mark.parametrize('empty', [False, True])
def test_t2_changed_or_missing_snapshot_reports_discovery_failure(directory, monkeypatch, change, empty):
    adapter = directory.adapter
    if change == 'executable':
        directory.command.write_bytes(b'updated executable')
    elif change == 'manifest':
        adapter.manifest.context_window += 1024
    else:
        adapter = CLIModelDispatchAdapter(adapter.inner, adapter.manifest, adapter.path.parent)
    def fail(*a, **kw):
        if empty:
            return []
        raise TimeoutError('offline')
    monkeypatch.setattr(cli_models, 'list_models', fail)
    with pytest.raises(AdapterError) as info:
        adapter.current_models(refresh=True)
    assert info.value.code == 'cli_model_discovery_failed'
    with pytest.raises(AdapterError) as info:
        adapter.binding_for_model('model-a')
    assert info.value.code == 'cli_model_discovery_failed'


@pytest.mark.parametrize('field,value', [('context_window', 16000), ('supported_reasoning_efforts', ['low'])])
def test_t3_successful_snapshot_change_rejects_old_binding(directory, field, value):
    adapter = directory.adapter
    bound = adapter.binding_for_model('model-a')
    directory.models[0][field] = value
    adapter.current_models(refresh=True)
    with pytest.raises(AdapterError) as info:
        adapter.call_bound('chat', bound, lambda: None, {}, model='model-a')
    assert info.value.code == 'model_configuration_changed'
    assert directory.calls == []


def test_missing_model_in_snapshot_uses_discovery_error(directory):
    with pytest.raises(AdapterError) as info:
        directory.adapter.binding_for_model('missing')
    assert info.value.code == 'cli_model_discovery_failed'


@pytest.mark.parametrize('transport', ['peer', 'http', 'adapter', 'adapter_binding', 'policy_context', 'adapter_restricted'])
def test_t4_bridge_maps_discovery_failure_to_retryable_code(cli_pair, monkeypatch, transport):
    p = cli_pair
    index = 0 if transport.startswith('adapter') or transport == 'policy_context' else 1
    store = p.root.store if index == 0 else p.child.store
    bridge, _, catalog = build_agent_integrations(p.apps[index], store, store.home)
    model = next(m for m in catalog.list_models() if m.get('cli_model') == 'gemini-3.8-flash-medium')
    destination = None
    state = None
    if transport.startswith('adapter') or transport == 'policy_context':
        checks = []
        def fail(*a, **kw):
            checks.append(True)
            if transport == 'adapter_restricted' and len(checks) == 1:
                return target['local_model_binding']
            raise AdapterError('directory unavailable', code='cli_model_discovery_failed')
        if transport == 'adapter':
            monkeypatch.setattr(p.provider.adapter.inner, 'chat', fail)
        else:
            target = catalog.resolve(model['id'])
            destination = catalog.destination(target)
            monkeypatch.setattr(catalog, 'resolve', lambda _: target)
            monkeypatch.setattr(p.provider.adapter, 'binding_for_model', fail)
            if transport == 'adapter_restricted':
                state = data_policy.merge_state(None, {
                    'server_id': 'fixture', 'instance_id': 'fixture-instance',
                    'policy': {'models': {'mode': 'allowlist', 'allow': [{'catalog_model_id': model['id']}]}}})
    else:
        async def fail(*a, **kw):
            if transport == 'http':
                raise HTTPException(409, 'cli_model_discovery_failed')
            return {'state': 'failed', 'error_code': 'cli_model_discovery_failed'}
        bridge.services['execute_order'] = fail
    with pytest.raises(AgentError) as info:
        if transport == 'policy_context':
            bridge.data_policy_context(model['id'], destination)
        else:
            bridge.complete(model['id'], [{'role': 'user', 'content': 'hello'}], [], lambda: False,
                            expected_destination=destination, data_policy_state=state)
    assert str(info.value) == 'agent_model_discovery_failed'
    assert str(info.value) in retry_policy.NETWORK_ERRORS
    assert retry_policy.limit(str(info.value), {}) == 5


@pytest.mark.parametrize('changed', [False, True])
def test_peer_request_uses_snapshot_through_real_provider_path(cli_pair, monkeypatch, changed):
    p = cli_pair
    bridge, _, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    model = next(m for m in catalog.list_models() if m.get('cli_model') == 'gemini-3.8-flash-medium')
    async def exchange(*, signed_request, **kwargs):
        return p.provider.handle(signed_request), {'transport': 'fixture', 'relay_used': False}
    monkeypatch.setattr('rynmesh.llm_package.routes.consumer_exchange', exchange)
    monkeypatch.setattr(cli_models, 'list_models', lambda *a, **kw: pytest.fail('request ran discovery'))
    monkeypatch.setattr(cli_models, 'cached_models', lambda *a, **kw: [])
    status = p.provider.public_status(cached_cli_models=True)
    selected = next(row for row in status['cli_models'] if row['id'] == model['cli_model'])
    assert selected['model_binding'] == p.provider.adapter.binding_for_model(model['cli_model'])
    if changed:
        p.command.write_bytes(b'replaced-cli')
        with pytest.raises(AgentError, match='agent_model_discovery_failed'):
            bridge.complete(model['id'], [{'role': 'user', 'content': 'hello'}], [], lambda: False,
                            expected_destination=catalog.destination(model))
        assert p.calls == []
        return
    result = bridge.complete(model['id'], [{'role': 'user', 'content': 'hello'}], [], lambda: False,
                             expected_destination=catalog.destination(model))
    assert result['message']['content'] == 'peer agent reply'
    assert p.calls[-1]['model'] == 'gemini-3.8-flash-medium'


def test_background_discovery_does_not_hold_up_bound_dispatch(directory, monkeypatch):
    adapter = directory.adapter
    bound = adapter.binding_for_model('model-a')
    entered, release = threading.Event(), threading.Event()
    def discover(*a, **kw):
        entered.set()
        assert release.wait(20)
        return copy.deepcopy(directory.models)
    monkeypatch.setattr(cli_models, 'list_models', discover)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(adapter.current_models, refresh=True)
        try:
            assert entered.wait(20)
            call = pool.submit(adapter.call_bound, 'chat', bound, lambda: None, {}, model='model-a')
            assert call.result(timeout=10) == {'text': 'ok'}
        finally:
            release.set()
        assert refresh.result(timeout=20) == directory.models


def test_cold_local_cli_order_returns_retryable_discovery_code(cli_pair, monkeypatch):
    p = cli_pair
    def unavailable(*args, **kwargs):
        raise TimeoutError('directory unavailable')
    monkeypatch.setattr(cli_models, 'list_models', unavailable)
    p.provider.adapter._last_good = None
    with TestClient(p.apps[0]) as client:
        response = client.post('/api/local/llm/orders', json={
            'provider_peer_id': p.root.store.peer_id,
            'service_id': 'antigravity-cli', 'prompt': 'hello'})
    assert response.status_code == 409
    assert response.json()['detail'] == 'cli_model_discovery_failed'
    assert p.calls == []
