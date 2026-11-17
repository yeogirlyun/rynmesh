"""S12-I17 model facts and the real catalog/runtime provenance path."""
import ast
import copy
import inspect
import json
from datetime import datetime, timezone

import pytest

from rynmesh.agent_runtime import model_profiles
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import adapters, cli_models, codex_session, tool_capabilities
from rynmesh.llm_package.cli_models import CLI_FALLBACK_WINDOWS
from rynmesh.llm_package.sources import PROTOCOLS
from tests.test_agent_model_profiles import CODEX_MODEL, World, api_row, codex_row


FIELDS = {'parallel_tool_calls', 'max_tool_output_bytes', 'verbosity', 'patch_tool',
          'context_window', 'max_output_tokens'}


def test_t1_old_string_overrides_read_and_write_as_structured_entries():
    legacy = {CODEX_MODEL: 'generic'}
    normalized = {CODEX_MODEL: {'profile': 'generic'}}
    assert model_profiles.overrides_from({model_profiles.SETTING: legacy}) == normalized
    assert model_profiles.validate_overrides(legacy, lambda _: codex_row()) == normalized
    assert legacy == {CODEX_MODEL: 'generic'}
    assert model_profiles.resolve(codex_row(), override='generic')['profile'] == 'generic'
    assert model_profiles.resolve(codex_row(), override=normalized[CODEX_MODEL])['basis'] == 'override'


@pytest.mark.parametrize('facts', [
    {'parallel_tool_calls': False}, {'parallel_tool_calls': None},
    {'max_tool_output_bytes': 1}, {'verbosity': 'low'}, {'verbosity': 'medium'}, {'verbosity': 'high'},
    {'patch_tool': 'function'}, {'patch_tool': 'freeform'}, {'patch_tool': None},
    {'context_window': 256, 'max_output_tokens': 256},
    {'context_window': 2000000, 'max_output_tokens': 393216},
])
def test_t2_valid_fact_types_and_boundaries(facts):
    value = {CODEX_MODEL: {'facts': facts}}
    assert model_profiles.validate_overrides(value, lambda _: codex_row()) == value


@pytest.mark.parametrize('facts', [
    {'parallel_tool_calls': 0}, {'parallel_tool_calls': 'false'},
    {'max_tool_output_bytes': False}, {'max_tool_output_bytes': 0}, {'max_tool_output_bytes': -1},
    {'max_tool_output_bytes': 1.5}, {'verbosity': 'verbose'}, {'verbosity': []},
    {'patch_tool': True}, {'patch_tool': 'custom'},
    {'context_window': True}, {'context_window': 255}, {'context_window': 2000001},
    {'max_output_tokens': 0}, {'max_output_tokens': 393217},
    {'context_window': 512, 'max_output_tokens': 1024}, {'unknown_fact': True},
])
def test_t2_rejects_invalid_fact_types_and_ranges(facts):
    with pytest.raises(ValueError, match='model_profile'):
        model_profiles.validate_overrides({CODEX_MODEL: {'facts': facts}}, lambda _: codex_row())


@pytest.mark.parametrize('entry', [[], {'facts': []}, {'profile': 'missing'}, {'surprise': 'generic'}])
def test_t2_rejects_invalid_entry_shape(entry):
    with pytest.raises(ValueError, match='model_profile'):
        model_profiles.validate_overrides({CODEX_MODEL: entry}, lambda _: codex_row())


def test_t2_capacity_facts_are_only_owner_editable_for_services_peers_and_cli():
    value = {'api': {'facts': {'context_window': 128000}}}
    with pytest.raises(ValueError, match='model_profile'):
        model_profiles.validate_overrides(value, lambda _: api_row())
    for source in ('local_service', 'peer'):
        row = {'id': 'target', 'source': source, 'adapter': 'openai_compatible'}
        override = {'target': {'facts': {'context_window': 128000, 'max_output_tokens': 16000}}}
        assert model_profiles.validate_overrides(override, lambda _: row) == override
        assert model_profiles.facts_for(row, override)['context_window'] == 128000


def test_fact_precedence_unknown_defaults_and_no_name_guessing():
    assert set(model_profiles.FACTS) == FIELDS
    assert model_profiles.facts_for(api_row(model='named-like-any-famous-model'), {}) == dict.fromkeys(FIELDS)
    model = codex_row(parallel_tool_calls=True, verbosity='medium', patch_tool='function',
                      max_tool_output_bytes=12000, context_window=128000, context_window_known=True,
                      max_output_tokens=16000, max_output_tokens_known=True)
    override = {CODEX_MODEL: {'facts': {'parallel_tool_calls': False, 'verbosity': None, 'patch_tool': 'freeform'}}}
    before = copy.deepcopy(model)
    facts = model_profiles.facts_for(model, override)
    assert facts == {'parallel_tool_calls': False, 'verbosity': None, 'patch_tool': 'freeform',
                     'max_tool_output_bytes': 12000, 'context_window': 128000, 'max_output_tokens': 16000}
    assert model == before
    unknown = codex_row(context_window=32768, context_window_known=False,
                        max_output_tokens=4096, max_output_tokens_known=False)
    assert model_profiles.facts_for(unknown, {})['context_window'] is None
    assert model_profiles.facts_for(unknown, {})['max_output_tokens'] is None


def test_t3_parallel_false_adds_one_note_and_request_record_is_redacted():
    model = api_row(parallel_tool_calls=False, verbosity='low', patch_tool='function',
                    max_tool_output_bytes=8000, api_key='PRIVATE_FACT_CANARY')
    selected = model_profiles.resolve(model)
    assert selected['instructions'].count('emit at most one tool call per response') == 1
    assert selected['facts']['parallel_tool_calls'] is False
    assert 'at most one tool call' not in model_profiles.resolve(api_row())['instructions']
    record = model_profiles.request_record(selected,
        [{'role': 'developer', 'content': selected['instructions']}], {})
    assert record['facts'] == selected['facts']
    assert record['instructions'] == 'sent'
    assert 'PRIVATE_FACT_CANARY' not in json.dumps(record)
    assert selected['instructions'] not in json.dumps(record)


def test_t4_routes_cover_the_package_adapter_enumerations():
    known = set(CLI_FALLBACK_WINDOWS) | set(PROTOCOLS)
    for function in (adapters.adapter_from_manifest, tool_capabilities.client_tools_contract):
        for node in ast.walk(ast.parse(inspect.getsource(function))):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            is_adapter = (isinstance(left, ast.Name) and left.id == 'adapter'
                          or isinstance(left, ast.Attribute) and left.attr == 'adapter')
            if is_adapter:
                known.update(child.value for item in node.comparators for child in ast.walk(item)
                             if isinstance(child, ast.Constant) and isinstance(child.value, str))
    assert known <= model_profiles.ROUTES.keys(), known - model_profiles.ROUTES.keys()
    selection = model_profiles.resolve({'id': 'local/claude', 'adapter': 'claude_cli'})
    assert selection['route'] == 'claude_cli' and selection['profile'] == 'generic'
    assert selection['fallback_reason'] == 'no_route_profile'
    unknown = model_profiles.resolve({'id': 'local/future', 'adapter': 'future_adapter'})
    assert model_profiles.request_record(unknown, [], {})['fallback_reason'] == 'unknown_route'


def test_real_catalog_and_runtime_settings_reach_current_request_and_provenance(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    upstream = [{'id': 'gpt-fixture', 'name': 'Fixture', 'default': True,
                 'parallel_tool_calls': True, 'verbosity': 'medium', 'patch_tool': 'function',
                 'max_tool_output_bytes': 20000}]
    monkeypatch.setattr(cli_models, 'list_models', lambda *_: copy.deepcopy(upstream))
    world.settings(model_id=CODEX_MODEL,
        model_profile_overrides={CODEX_MODEL: {'profile': 'generic', 'facts': {'parallel_tool_calls': False}}})
    stored = world.runtime.store.read()['settings'][model_profiles.SETTING]
    assert stored[CODEX_MODEL]['facts']['parallel_tool_calls'] is False
    metadata = world.runtime._model_metadata(CODEX_MODEL)
    assert model_profiles.facts_for(metadata, {})['max_tool_output_bytes'] == 20000
    task = world.runtime.create({'message': 'Summarize notes.txt without changing it.'})
    world.run(task['id'])
    record = world.records(task['id'])[-1]
    assert record['facts']['parallel_tool_calls'] is False
    assert record['facts']['verbosity'] == 'medium'
    assert record['facts']['patch_tool'] == 'function'
    assert record['facts']['max_tool_output_bytes'] == 20000
    assert record['instructions'] == 'sent'
    assert any('emit at most one tool call per response' in json.dumps(turn['body']) for turn in world.codex_turns)
    world.runtime.update_settings({model_profiles.SETTING: {CODEX_MODEL: 'generic'}})
    assert world.runtime.store.read()['settings'][model_profiles.SETTING] == {CODEX_MODEL: {'profile': 'generic'}}
    with pytest.raises(AgentError, match='agent_invalid_settings'):
        world.runtime.update_settings({model_profiles.SETTING: {CODEX_MODEL: {'facts': {'verbosity': True}}}})


def test_installed_codex_catalog_facts_survive_existing_discovery_parser(tmp_path):
    cache = tmp_path / 'models.json'
    cache.write_text(json.dumps({'fetched_at': datetime.now(timezone.utc).isoformat(), 'models': [{
        'slug': 'fixture-model', 'context_window': 128000, 'max_output_tokens': 16000,
        'parallel_tool_calls': False, 'support_verbosity': True, 'default_verbosity': 'low',
        'apply_patch_tool_type': 'freeform', 'truncation_policy': {'mode': 'bytes', 'limit': 32000},
        'base_instructions': 'PRIVATE_CATALOG_CANARY'}]}), encoding='utf-8')
    model = codex_session._model_windows([{'id': 'fixture-model', 'name': 'Fixture'}], cache)[0]
    facts = model_profiles.facts_for({**model, 'adapter': 'codex_cli', 'source': 'local_service'}, {})
    assert facts['parallel_tool_calls'] is False
    assert facts['verbosity'] == 'low' and facts['patch_tool'] == 'freeform'
    assert facts['max_tool_output_bytes'] == 32000
    assert facts['max_output_tokens'] == 16000
    assert 'PRIVATE_CATALOG_CANARY' not in json.dumps(model)
    row = json.loads(cache.read_text(encoding='utf-8'))
    row['models'][0]['truncation_policy']['mode'] = 'tokens'
    cache.write_text(json.dumps(row), encoding='utf-8')
    model = codex_session._model_windows([{'id': 'fixture-model', 'name': 'Fixture'}], cache)[0]
    assert model_profiles.facts_for({**model, 'adapter': 'codex_cli'}, {})['max_tool_output_bytes'] is None
