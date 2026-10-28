"""Real signed CLI catalogs and sealed agent requests with fake upstream inference."""
import copy
import uuid

import pytest
from test_agent_space_binding import nodes, space_pair  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.cli_binding import CLIModelDispatchAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.peer_binding import expected
from rynmesh.llm_package.routes import ProviderService, _expires
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box


@pytest.fixture
def cli_pair(space_pair, monkeypatch, tmp_path):  # noqa: F811
    p = space_pair
    from rynmesh.llm_package import cli_models
    command = tmp_path / 'agy-test.exe'
    command.write_bytes(b'fixture-v1')
    monkeypatch.setattr(CLIAgentAdapter, '_command_path', lambda _: str(command))
    monkeypatch.setattr(CLIAgentAdapter, 'health', lambda _: {'ok': True})
    monkeypatch.setattr(cli_models, 'list_models', lambda *_, **__: [
        {'id': '', 'name': 'CLI default', 'default': True},
        {'id': 'gemini-3.8-flash-medium', 'name': 'Gemini 3.8 Flash Medium', 'default': False},
        {'id': 'unverified', 'name': 'Unverified model', 'default': False}])
    monkeypatch.setattr(cli_models, 'cached_models', cli_models.list_models)
    calls = []
    def answer(self, body, **options):
        calls.append({'body': copy.deepcopy(body), 'model': options.get('model', '')})
        return {'text': 'peer agent reply', 'message': {'role': 'assistant', 'content': 'peer agent reply'},
                'input_tokens': 3, 'output_tokens': 3, 'duration_ms': 1, 'finish_reason': 'stop'}
    monkeypatch.setattr(CLIAgentAdapter, 'chat', answer)
    manifest = LLMPackageManifest(package_id='antigravity-cli', mode='antigravity_cli', adapter='antigravity_cli', public_model_alias='Antigravity', capabilities=['text-generation', 'client-tools', 'buffered-streaming'])
    prior = p.provider
    p.provider = ProviderService(manifest=manifest, adapter=CLIAgentAdapter('antigravity_cli'), store=p.root.store,
        task_store=prior.task_store, balance=prior.balance, messaging_key=p.keys[0])
    p.root.store.llm_tool_capabilities.save(manifest, 'supported')
    p.root.store.llm_tool_capabilities.save(manifest, 'supported', 'gemini-3.8-flash-medium')
    # Exercise the installed routes' real provider collection, not a replacement
    # lambda: CLI services must be included in the authenticated space catalog.
    import json

    from rynmesh.llm_package.manifest import save_manifest
    manifest_path = p.root.store.home / 'antigravity-test.json'
    save_manifest(manifest, manifest_path)
    settings_path = p.root.store.home / 'llm' / 'cli-services.json'
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({'antigravity_cli': {
        'manifest': str(manifest_path), 'publication_enabled': True}}))
    p.provider = p.apps[0].state.llm_agent_services['provider_for']('antigravity-cli')
    # Populate the CLI inventory as the publication worker does. Signed catalog
    # requests intentionally read only this cache and never spawn CLI discovery.
    p.provider.public_status()
    p.calls, p.command = calls, command
    p.consumer.refresh(p.locator)
    return p


def test_cli_agent_catalog_creation_and_sealed_concrete_model_dispatch(cli_pair, monkeypatch):
    p = cli_pair
    envelopes = []
    async def exchange(*, signed_request, **kwargs):
        envelopes.append(copy.deepcopy(signed_request))
        return p.provider.handle(signed_request), {'transport': 'signed_fixture', 'relay_used': False}
    monkeypatch.setattr('rynmesh.llm_package.routes.consumer_exchange', exchange)
    bridge, tools, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    models = catalog.list_models()
    model = next(m for m in models if m.get('cli_model') == 'gemini-3.8-flash-medium')
    assert model['agent_supported'] and model['destination_binding_supported']
    untested = next(m for m in models if m.get('cli_model') == 'unverified')
    assert untested['agent_supported'] and untested['tool_support'] == 'unknown'
    runtime = AgentRuntime(AgentStore(p.child.store.home, p.keys[1]), bridge, tools, catalog, consumer_node_id=p.child.store.peer_id)
    runtime.update_settings({'enabled': True})
    created = runtime.create({'message': 'Give one short reply.', 'model_id': model['id'], 'client_request_id': str(uuid.uuid4())})
    assert created['id']
    result = bridge.complete(model['id'], [{'role': 'user', 'content': 'hello'}], [], lambda: False,
                             expected_destination=catalog.destination(model))
    assert result['message']['content'] == 'peer agent reply'
    assert p.calls[-1]['model'] == 'gemini-3.8-flash-medium'
    body = open_task(envelopes[0], recipient_peer_id=p.root.store.peer_id, recipient_messaging_key=p.keys[0], expected_kind='llm_request')[1]
    assert body['cli_model'] == 'gemini-3.8-flash-medium'
    assert body['expected_model_binding'] == model['expected_model_binding']


def test_cli_binding_survives_restart_and_changes_with_model_or_executable(cli_pair):
    p = cli_pair
    adapter = p.provider.adapter
    bound = adapter.binding_for_model('gemini-3.8-flash-medium')
    restarted = CLIModelDispatchAdapter(CLIAgentAdapter('antigravity_cli'), p.provider.manifest, p.root.store.home)
    assert restarted.binding_for_model('gemini-3.8-flash-medium') == bound
    assert adapter.model_binding != bound
    p.command.write_bytes(b'fixture-updated-binary')
    assert adapter.binding_for_model('gemini-3.8-flash-medium') != bound
    with pytest.raises(Exception, match='model_configuration_changed'):
        adapter.call_bound('chat', bound, lambda: None, {'messages': []}, model='gemini-3.8-flash-medium')
    assert not p.calls


def test_cli_bound_request_rechecks_permission_at_dispatch(cli_pair):
    p = cli_pair
    record = next(row for row in p.consumer.records() if row['service']['package_id'] == 'antigravity-cli')
    bound = expected(record, p.child.store.peer_id)
    original = p.provider.adapter.call_bound
    def revoke(*args, **kwargs):
        p.root.set_policy('local')
        return original(*args, **kwargs)
    p.provider.adapter.call_bound = revoke
    wire = seal_task(body={'task_id': 'cli-revoke', 'service_id': 'antigravity-cli',
        'chat': {'messages': [{'role': 'user', 'content': 'hello'}]}, 'max_tokens': 32, 'max_amount': 1,
        'expected_model_binding': bound, 'reply_messaging_pub': peer_box.public_key_b64(p.keys[1])}, task_id='cli-revoke',
        kind='llm_request', sender_peer_id=p.child.store.peer_id, recipient_peer_id=p.root.store.peer_id,
        sender_signing_key=p.child.store.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(p.keys[0]), expires_at=_expires(300)).to_dict()
    result = open_task(p.provider.handle(wire), recipient_peer_id=p.child.store.peer_id, recipient_messaging_key=p.keys[1], expected_kind='llm_response')[1]
    assert result['error_code'] == 'ai_permission_denied'
    assert not p.calls


def test_installed_catalog_uses_lan_before_unreachable_registry(cli_pair, monkeypatch):
    from fastapi import FastAPI

    from rynmesh.llm_package.routes import install_llm_routes
    p = cli_pair
    app = FastAPI()
    attempts = []
    monkeypatch.setattr(p.child.store, 'lan_peer_endpoints', lambda *args, **kwargs: ['http://192.168.1.2:8791'])
    def post(endpoint, path, body, **kwargs):
        attempts.append(endpoint)
        p.publisher.clock = lambda: __import__('time').time() + 5
        return p.publisher.respond(body)
    monkeypatch.setattr('rynmesh.llm_package.routes._peer_post_json', post)
    def registry(_):
        raise AssertionError('LAN success must not resolve a stale registry address')
    install_llm_routes(app, store=p.child.store, home=p.child.store.home, messaging_key=p.keys[1],
                       resolve_endpoint=registry, resolve_pubkey=lambda _: '')
    try:
        records = app.state.llm_agent_services['space_catalog'].refresh(p.locator)
        assert any(row['service']['package_id'] == 'antigravity-cli' for row in records)
        assert attempts == ['http://192.168.1.2:8791']
    finally:
        app.state.llm_shutdown()


def test_agent_lan_inference_precedes_p2p_and_never_uses_registry(cli_pair, monkeypatch):
    p = cli_pair
    attempts = []
    monkeypatch.setenv('RYNMESH_LLM_TRANSPORT', 'relay')
    monkeypatch.setenv('RYNMESH_LLM_FORCE_RELAY', '1')
    monkeypatch.setattr(p.child.store, 'lan_peer_endpoints', lambda *a, **k: ['http://192.168.1.2:8791'])
    def post(endpoint, path, body, **kwargs):
        assert path == '/api/peer/llm/tasks'
        attempts.append(endpoint)
        return p.provider.handle(body)
    monkeypatch.setattr('rynmesh.llm_package.routes._peer_post_json', post)
    async def no_fallback(**kwargs):
        raise AssertionError('successful LAN request must not enter ICE or relay')
    monkeypatch.setattr('rynmesh.llm_package.routes.consumer_exchange', no_fallback)
    bridge, _, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    model = next(m for m in catalog.list_models() if m.get('cli_model') == 'gemini-3.8-flash-medium')
    result = bridge.complete(model['id'], [{'role': 'user', 'content': 'hello'}], [], lambda: False,
                             expected_destination=catalog.destination(model))
    assert result['message']['content'] == 'peer agent reply'
    assert attempts == ['http://192.168.1.2:8791']


def test_expired_catalog_refreshes_metadata_before_followup(cli_pair):
    p = cli_pair
    bridge, tools, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    original = next(m for m in catalog.list_models() if m.get('cli_model') == 'gemini-3.8-flash-medium')
    p.consumer.cache[p.root.store.peer_id]['checked_at'] -= 121
    # A locator can be public and unauthenticated; it must never become egress authority.
    discover = catalog.services['discover']
    record = {**p.locator, 'access_policy': 'personal_space', 'service': p.provider.manifest.public_dict(),
              'online': True, 'binding_verified': False}
    catalog.services['discover'] = lambda network: discover(network) or [record]
    fresh = catalog.refresh_destination(original['id'])
    assert fresh['destination_revision'] == original['destination_revision']
    assert fresh['destination_binding_supported']


def test_clarification_answer_and_followup_survive_name_sync(cli_pair, monkeypatch):
    import json

    from rynmesh.llm_package import cli_models
    p = cli_pair
    # This test exercises authenticated name sync/clarification, not a 4k
    # pressure case. Advertise capacity through the actual signed CLI catalog.
    prior_models = cli_models.list_models
    def capacity_models(*args, **kwargs):
        return [{**row, 'context_window': 131072} for row in prior_models(*args, **kwargs)]
    monkeypatch.setattr(cli_models, 'list_models', capacity_models)
    monkeypatch.setattr(cli_models, 'cached_models', capacity_models)
    p.provider.public_status()
    p.consumer.refresh(p.locator)
    calls = []
    def answer(self, body, **options):
        calls.append(body)
        message = {'role': 'assistant', 'content': '收到你的回答。'}
        if len(calls) == 1:
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'ask-one', 'type': 'function', 'function': {'name': 'ryn_request_input', 'arguments': json.dumps({
                    'goal_revision': 1, 'prompt': '请选择报告语言', 'kind': 'information',
                    'fields': [{'id': 'language', 'label': '语言', 'type': 'text', 'required': True}]})}}]}
        return {'message': message, 'text': message['content'], 'input_tokens': 3, 'output_tokens': 3, 'duration_ms': 1, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}
    monkeypatch.setattr(CLIAgentAdapter, 'chat', answer)
    async def exchange(*, signed_request, **kwargs):
        return p.provider.handle(signed_request, on_event=lambda _: None), {'transport': 'signed_fixture', 'relay_used': False}
    monkeypatch.setattr('rynmesh.llm_package.routes.consumer_exchange', exchange)
    bridge, tools, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    model = next(m for m in catalog.list_models() if m.get('cli_model') == 'gemini-3.8-flash-medium')
    runtime = AgentRuntime(AgentStore(p.child.store.home, p.keys[1]), bridge, tools, catalog, consumer_node_id=p.child.store.peer_id)
    runtime.update_settings({'enabled': True})
    task = runtime.create({'message': '帮我准备报告，先问我语言。', 'model_id': model['id']})
    assert any(spec['function']['name'] == 'ryn_request_input'
               for spec in runtime._specs(runtime.store.read()['tasks'][task['id']]))
    runtime.run_once(); runtime.run_once()
    row = runtime.get(task['id'])
    assert row['status'] == 'waiting_input', row.get('error')
    snapshot = copy.deepcopy(p.root._snapshot())
    snapshot['members'][p.child.store.peer_id]['name'] = 'A new display name'
    p.root._sign(snapshot)
    p.child._accept_snapshot(p.root.data['snapshot'], p.root.status()['space'])
    p.consumer.refresh(p.locator)
    runtime.action(task['id'], {'action': 'answer', 'pending_input_id': row['pending_input']['id'],
        'expected_goal_revision': row['goal']['revision'], 'expected_control_revision': row['control_revision'],
        'answers': {'language': '中文'}, 'client_request_id': str(uuid.uuid4())})
    runtime.run_once()
    row = runtime.get(task['id'])
    assert not row['error']
    assert len(calls) == 2
    assert row['result'] == '收到你的回答。'
    assert row['model_destination'] == task['model_destination']


@pytest.mark.parametrize("model_name", ["", "gemini-3.8-flash-medium"])
def test_local_cli_project_uses_exact_upstream_binding_and_configuration_changes_stop_dispatch(cli_pair, tmp_path, model_name):
    from test_agent_projects import project_folder
    p = cli_pair
    bridge, tools, catalog = build_agent_integrations(p.apps[0], p.root.store, p.root.store.home)
    identifier = "local/antigravity-cli" + ("/model/" + model_name if model_name else "")
    model = next(m for m in catalog.list_models() if m["id"] == identifier)
    assert model["destination_binding_supported"] and model["destination_revision"]
    other = next(m for m in catalog.list_models() if m["id"] != identifier and m["source"] == "local_service")
    assert other["destination_revision"] != model["destination_revision"]
    runtime = AgentRuntime(AgentStore(p.root.store.home / "project-test-agent", p.keys[0]), bridge, tools, catalog,
                           consumer_node_id=p.root.store.peer_id)
    runtime.update_settings({"enabled": True})
    project = runtime.load_project({"path": str(project_folder(tmp_path))})
    task = runtime.create({"message": "Which project is selected?", "model_id": identifier, "project_id": project["id"]})
    runtime.run_once()
    assert not runtime.get(task["id"])["error"]
    assert p.calls[-1]["model"] == model_name
    assert "sample-backend" in str(p.calls[-1]["body"]["messages"])
    calls_before = len(p.calls)
    next_task = runtime.create({"message": "Read project", "model_id": identifier, "project_id": project["id"]})
    p.command.write_bytes(b"fixture-v2-config-change")
    runtime.run_once()
    assert len(p.calls) == calls_before
    assert runtime.get(next_task["id"])["error"] == "agent_model_destination_changed"
