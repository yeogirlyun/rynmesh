"""Installed CLI setup through the real approval/receipt state machine."""
import json
from types import SimpleNamespace

import pytest
from test_agent_default_model import setup as base_setup
from test_agent_runtime import advance, approve, assistant

from rynmesh.agent_runtime import cli_sources
from rynmesh.agent_runtime.cli_sources import CONNECT, DISCOVER, TEST
from rynmesh.agent_runtime.store import AgentToolRejected


def setup(tmp_path, monkeypatch):
    runtime, model, tools, key, rows = base_setup(tmp_path)
    executable = tmp_path / 'agy.exe'
    executable.write_bytes(b'cli-v1')
    monkeypatch.setattr(cli_sources.CLIAgentAdapter, '_command_path', lambda self: str(executable))
    calls = []
    class Adapter:
        def infer(self, **kwargs):
            calls.append(('chat', kwargs))
            return {'text': 'RYNMESH SELF TEST OK', 'usage': {'input_tokens': 5, 'output_tokens': 5}}
        def chat(self, body, **kwargs):
            calls.append(('tools', body))
            return {'text': 'synthetic response', 'usage': {'input_tokens': 5, 'output_tokens': 5}}
        def cancel(self, task_id):
            calls.append(('cancel', task_id))
    adapter = Adapter()
    manager = SimpleNamespace(adapter=adapter, manifest=SimpleNamespace(package_id='antigravity-cli'))
    def active():
        return {'antigravity_cli': manager} if tools.cli_sources.read().get('antigravity_cli') else {}
    tools.services.update(active_cli_managers=active,
        cli_services=lambda: {'services': [{'kind': 'antigravity_cli', 'installed': True, 'configured': bool(active())}], 'personal_space_ready': True})
    def connect(**kwargs):
        calls.append(('connect', kwargs['kind']))
        kwargs['self_test_fn'](manager.manifest)
        return {'manifest': str(tmp_path / 'manifest.json')}
    monkeypatch.setattr(cli_sources, 'connect_cli', connect)
    monkeypatch.setattr(cli_sources, 'adapter_from_manifest', lambda manifest: adapter)
    def probe(service_id, proxy):
        for _ in range(2):
            proxy.chat({'messages': [{'role': 'user', 'content': 'fixed probe'}], 'max_tokens': 64}, task_id='probe', timeout_s=60)
        return {'service_id': service_id, 'tool_support': 'supported'}
    tools.services['probe_service_tools'] = probe
    return runtime, model, tools, executable, calls


def waiting(runtime, model):
    model.responses.append(assistant(name=CONNECT, args={'kind': 'antigravity_cli'}))
    task = runtime.create({'message': 'Connect my installed CLI privately.'})
    advance(runtime, 2)
    pending = runtime.get(task['id'])
    assert pending['status'] == 'waiting_approval'
    return pending


def test_cli_connect_uses_approval_private_setup_metered_probe_and_durable_receipt(tmp_path, monkeypatch):
    runtime, model, tools, _, calls = setup(tmp_path, monkeypatch)
    settings = runtime.settings()
    pending = waiting(runtime, model)
    assert not calls and not tools.cli_sources.read()
    approve(runtime, pending['id'])
    advance(runtime)
    raw = runtime.store.read()['tasks'][pending['id']]
    receipt = raw['receipts'][-1]
    assert receipt['tool'] == CONNECT and receipt['result']['chat_probe'] == 'passed'
    assert receipt['result']['agent_usable'] is True
    assert tools.cli_sources.read()['antigravity_cli']['publication_enabled'] is False
    assert runtime.settings() == settings
    assert [c[0] for c in calls] == ['connect', 'chat']
    result = tools.verify(receipt, [{'verifier': 'local_cli_configuration_state'}], context=runtime._execution_context(raw), cancelled=lambda: False)
    assert result['status'] == 'passed'
    recovery = tools.reconcile(receipt['operation_id'], context={**runtime._execution_context(raw), 'operation': receipt}, cancelled=lambda: False)
    assert recovery['outcome'] == 'executed'
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['executable', 'configuration', 'permission'])
def test_changed_cli_or_configuration_or_revoked_permission_cannot_use_old_approval(tmp_path, monkeypatch, change):
    runtime, model, tools, executable, calls = setup(tmp_path, monkeypatch)
    pending = waiting(runtime, model)
    if change == 'executable':
        executable.write_bytes(b'cli-replaced')
    elif change == 'configuration':
        tools.cli_sources.path.parent.mkdir(parents=True, exist_ok=True)
        tools.cli_sources.path.write_text(json.dumps({'antigravity_cli': {'manifest': 'owner-added', 'publication_enabled': True}}))
    else:
        runtime.update_settings({'permission_mode': 'read_only'})
    if change != 'permission':
        approve(runtime, pending['id'])
    advance(runtime)
    assert not calls


def test_repeat_setup_preserves_owner_sharing_and_tool_probe_uses_budget(tmp_path, monkeypatch):
    runtime, model, tools, _, calls = setup(tmp_path, monkeypatch)
    tools.cli_sources.path.parent.mkdir(parents=True, exist_ok=True)
    original = {'manifest': 'existing', 'publication_enabled': True}
    tools.cli_sources.path.write_text(json.dumps({'antigravity_cli': original}))
    pending = waiting(runtime, model)
    approve(runtime, pending['id'])
    advance(runtime)
    assert tools.cli_sources.read()['antigravity_cli'] == original and not calls
    model.responses.append(assistant(name=TEST, args={'kind': 'antigravity_cli'}))
    advance(runtime, 2)
    raw = runtime.store.read()['tasks'][pending['id']]
    receipt = raw['receipts'][-1]
    assert receipt['tool'] == TEST and receipt['result']['agent_usable'] is True
    assert len(calls) == 2
    assert tools.verify(receipt, [{'verifier': 'local_cli_tool_test_state'}], context=runtime._execution_context(raw), cancelled=lambda: False)['status'] == 'passed'


def test_cli_discovery_is_registered_and_arbitrary_commands_are_rejected(tmp_path, monkeypatch):
    runtime, model, tools, _, calls = setup(tmp_path, monkeypatch)
    assert {DISCOVER, CONNECT, TEST} <= {s['function']['name'] for s in tools.specs()}
    with pytest.raises(AgentToolRejected):
        tools._validate(CONNECT, {'kind': 'antigravity_cli', 'command': 'arbitrary command'})
    with pytest.raises(AgentToolRejected):
        tools._validate(CONNECT, {'kind': 'unknown_cli'})
    assert not calls


@pytest.mark.parametrize('support', ['unknown', 'unsupported'])
def test_optional_cli_diagnostic_does_not_disable_connected_model(tmp_path, monkeypatch, support):
    runtime, model, tools, _, calls = setup(tmp_path, monkeypatch)
    tools.cli_sources.path.parent.mkdir(parents=True, exist_ok=True)
    original = {'manifest': 'existing', 'publication_enabled': True}
    tools.cli_sources.path.write_text(json.dumps({'antigravity_cli': original}))
    tools.services['probe_service_tools'] = lambda service_id, proxy: {'service_id': service_id, 'tool_support': support}
    pending = waiting(runtime, model)
    approve(runtime, pending['id'])
    advance(runtime)
    model.responses.append(assistant(name=TEST, args={'kind': 'antigravity_cli'}))
    advance(runtime, 2)
    raw = runtime.store.read()['tasks'][pending['id']]
    receipt = raw['receipts'][-1]
    assert receipt['tool'] == TEST and receipt['result']['agent_usable'] is True
    assert receipt['result']['probe_passed'] is False and receipt['result']['tool_support'] == support
    assert tools.cli_sources.read()['antigravity_cli'] == original and not calls
    assert tools.verify(receipt, [{'verifier': 'local_cli_tool_test_state'}], context=runtime._execution_context(raw), cancelled=lambda: False)['status'] == 'unknown'


def test_antigravity_default_max_tokens_overflow_finish_length_and_missing_tool_choice(monkeypatch):
    from rynmesh.llm_package import antigravity_session, cli_models
    from rynmesh.llm_package.adapters import AdapterError
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter

    seen_budgets = []
    monkeypatch.setattr(cli_models, 'cached_models', lambda kind, exe: [
        {'id': 'gemini-2.5-pro', 'name': 'Gemini 2.5 Pro', 'default': True, 'max_output_tokens': 65536, 'agent_support': 'experimental'},
    ])
    monkeypatch.setattr(antigravity_session, 'infer', lambda _a, _p, **kw: (
        seen_budgets.append(kw['max_tokens']) or {'text': 'ok', 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}
    ))
    adapter = CLIAgentAdapter('antigravity_cli', executable='fixture-agy')
    adapter.chat({'messages': [{'role': 'user', 'content': 'hello'}]}, task_id='ag-default-max', timeout_s=5, model='gemini-2.5-pro')
    assert seen_budgets == [65536]

    monkeypatch.undo()
    monkeypatch.setattr(adapter, '_command_path', lambda: 'fixture-agy')
    monkeypatch.setattr(antigravity_session, 'spawn', lambda *_a, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(antigravity_session, 'terminate', lambda _p: None)
    monkeypatch.setattr(antigravity_session, 'communicate_bounded', lambda *_a, **_kw: json.dumps({
        'status': 'SUCCESS',
        'response': 'long reply',
        'usage': {'input_tokens': 10, 'output_tokens': 200},
    }))
    over = adapter.chat({'messages': [{'role': 'user', 'content': 'short'}], 'max_tokens': 64}, task_id='ag-over', timeout_s=5)
    assert over['finish_reason'] == 'length'
    assert over['diagnostics']['output_limit_not_enforced'] is True

    tool = {'type': 'function', 'function': {'name': 'probe', 'parameters': {'type': 'object'}}}
    with pytest.raises(AdapterError) as info:
        antigravity_session.chat(
            adapter,
            {'messages': [{'role': 'user', 'content': 'hi'}], 'tools': [tool], 'tool_choice': {'type': 'function', 'function': {'name': 'absent'}}},
            task_id='ag-missing-choice',
            timeout_s=5,
        )
    assert info.value.code == 'source_protocol_parameters_unsupported'
