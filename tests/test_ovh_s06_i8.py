"""S06-I8 shared schema compaction on real MCP/Craft/menu paths."""
import copy
import importlib
import json
import sys

import pytest
from test_agent_mcp_schema_normalize import _make_store_and_adapter
from test_craft_agent import craft, node, manager, _context
from test_agent_extensions_runtime import ext_env, _write_fake_mcp_server

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.store import AgentToolRejected, encoded
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.llm_package.chat import validate_tool_arguments, validate_tool_schema
from rynmesh.node_apps.craft_agent import CraftTools, TOOLS


def compact(schema, max_bytes=5000):
    return importlib.import_module('rynmesh.agent_runtime.schema_compaction').compact_tool_schema(schema, max_bytes)


def object_schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


def fn(name, size=0):
    return {'type': 'function', 'function': {'name': name, 'description': 'x'*size, 'parameters': object_schema()}}


def test_T1_T2_s06_i8_shared_module_and_description_stage():
    schema = object_schema({'query': {'type': 'string', 'description': '中'*7000, 'default': 'status'}}, ['query'])
    before = copy.deepcopy(schema)
    result = compact(schema)
    assert len(encoded(result)) <= 5000
    assert result['properties']['query'] == {'type': 'string', 'default': 'status'}
    assert result['required'] == ['query'] and schema == before
    assert encoded(compact(schema)) == encoded(result)


@pytest.mark.parametrize('definition_key', ['$defs', 'definitions'])
def test_T3_s06_i8_definition_refs_become_accepting_schema(definition_key):
    schema = object_schema({'value': {'$ref': f'#/{definition_key}/huge'}}, ['value'])
    schema[definition_key] = {'huge': {'type': 'string', 'enum': ['v'*200 + str(i) for i in range(40)]}}
    result = compact(schema)
    assert definition_key not in result and result['properties']['value'] == {}
    validate_tool_schema(result)
    for value in [1, None, [], {'arbitrary': True}, 'anything']:
        validate_tool_arguments(result, {'value': value})
    assert result['required'] == ['value']


def test_T4_s06_i8_five_levels_collapse_at_three():
    leaf = {'type': 'string', 'enum': ['x'*200+str(i) for i in range(40)]}
    schema = leaf
    for _ in range(5):
        schema = object_schema({'child': schema}, ['child'])
    result = compact(schema)
    for _ in range(3):
        result = result['properties']['child']
    assert result == {}


def test_T5_s06_i8_small_schema_bytes_and_values_unchanged():
    schema = object_schema({'email': {'type': 'string', 'description': 'A complete useful description',
                                    'default': 'owner@example.test', 'format': 'email', 'enum': ['owner@example.test']}}, ['email'])
    assert encoded(compact(schema)) == encoded(schema)
    craft_schema = CraftTools._compact_schema(schema)
    assert encoded(craft_schema) == encoded(schema)
    incomplete = {'type': 'object', 'properties': {'name': {'type': 'string', 'description': 'n'*250, 'default': 'owner'}}}
    craft_schema = CraftTools._compact_schema(incomplete)
    assert craft_schema['required'] == [] and craft_schema['additionalProperties'] is False
    assert craft_schema['properties']['name']['description'] == 'n'*200
    assert craft_schema['properties']['name']['default'] == 'owner'


def test_T6_s06_i8_real_mcp_menu_load_raw_validation_and_cache(tmp_path, monkeypatch):
    store, adapter = _make_store_and_adapter(tmp_path)
    server = store.upsert_mcp_server({'id': 'large', 'name': 'Large', 'transport': 'stdio',
                                    'command': 'python', 'args': ['-V'], 'enabled': True})
    schema = object_schema({'count': {'type': 'integer', 'description': 'x'*20000}}, ['count'])
    store.record_mcp_discovery('large', expected_revision=server['revision'],
        discovered_tools=[{'name': 'count', 'description': 'Count records', 'inputSchema': schema}], status='ready')
    name = store.get_mcp_server_internal('large')['discovered_tools'][0]['model_tool_name']
    active = adapter._active_mcp_tools()
    cached = copy.deepcopy(active[name][1])
    specs = adapter.specs()
    spec = next(s for s in specs if s['function']['name'] == name)
    assert len(encoded(spec['function']['parameters'])) <= 5000
    assert spec['function']['description'].endswith('(schema simplified)')
    assert adapter._active_mcp_tools()[name][1] == cached
    assert encoded(adapter.specs()) == encoded(specs)
    _, state = tool_menu.project(specs, [], {}, max_context_bytes=16000, authority_bytes=1000, epoch=0, goal_revision=1)
    # Force discovery when this tiny registry fits in full mode.
    state.update(mode='directory', catalog_revision=tool_menu.catalog_revision(specs), loaded_names=[])
    result, state = tool_menu.discover(specs, [], lambda _: {}, state, {'load': [name]}, capacity_bytes=14744)
    assert result['loaded_names'] == [name]
    host = RynTools.__new__(RynTools)
    host.extensions = adapter
    host._validate(name, {'count': 2})
    with pytest.raises(AgentToolRejected) as error:
        host._validate(name, {'count': 'not an integer'})
    assert error.value.details['field_path'] == 'count'
    assert 'integer' in error.value.correction
    # Actual adapter execution dispatches to the MCP boundary after strict validation.
    adapter.validate(name, {'count': 3})
    assert active[name][1]['input_schema'] == schema


def test_T6_s06_i8_craft_field_error_and_expanded_schema_hint():
    host = CraftTools.__new__(CraftTools)
    properties = {'field'+str(i): {'type': 'string', 'description': 'meaning '*14, 'default': 'value'} for i in range(12)}
    properties['count'] = {'type': 'integer'}
    schema = object_schema(properties, ['count'])
    session = {'tools': {'type_text': {'inputSchema': schema}}}
    with pytest.raises(AgentToolRejected) as error:
        host._arguments('wordcraft', 'type_text', {'count': 'wrong'}, session, {})
    assert error.value.details['field_path'] == 'count'
    assert 'integer' in error.value.correction
    assert 'default' in error.value.correction
    assert len(error.value.correction) > 1300
    assert len(error.value.correction) < 3500


def test_T7_s06_i8_craft_list_byte_budget_short_descriptions_and_exact_query(craft):
    runtime, _ = craft.runtime()
    task = runtime.create({'message': 'Inspect available office tools'})
    context = _context(runtime.store.read()['tasks'][task['id']])
    host = craft.tools.craft
    session = host._session('wordcraft', context, lambda: False)
    schema = object_schema({'query': {'type': 'string', 'description': 'Query terms', 'default': 'status'}})
    session['tools'] = {f'tool_{i}': {'name': f'tool_{i}', 'description': ('short description' if i == 0 else 'd'*1000),
                                      'inputSchema': copy.deepcopy(schema)} for i in range(30)}
    before = copy.deepcopy(session['tools'])
    result = craft.tools.execute(TOOLS, {'app': 'wordcraft'}, operation_id='catalog', context=context, cancelled=lambda: False)
    assert len(encoded(result)) <= 16*1024
    assert len(result['tools']) == 30
    assert result['tools'][0]['description'] == 'short description'
    assert any(len(item['description']) > 180 for item in result['tools'][1:])
    exact = craft.tools.execute(TOOLS, {'app': 'wordcraft', 'tool': 'tool_1'}, operation_id='exact', context=context, cancelled=lambda: False)
    assert exact['tools'] == [before['tool_1']]
    assert session['tools'] == before
    # Schemas themselves can exceed the total catalog budget; pages retain exact-query access.
    big = object_schema({f'field_{i}': {'type': 'string', 'description': 'meaning '*20} for i in range(25)})
    for item in session['tools'].values():
        item['inputSchema'] = copy.deepcopy(big)
    names, cursor = [], 0
    while True:
        page = craft.tools.execute(TOOLS, {'app': 'wordcraft', 'cursor': cursor},
            operation_id='page-'+str(cursor), context=context, cancelled=lambda: False)
        assert len(encoded(page)) <= 16*1024 and page['tools']
        names.extend(item['name'] for item in page['tools'])
        if page['next_cursor'] is None:
            break
        assert page['next_cursor'] > cursor
        cursor = page['next_cursor']
    assert names == list(before)
    # Definitions alone can exceed a page; cursor pages preserve every short description.
    for item in session['tools'].values():
        item['description'] = '短' * 250
        item['inputSchema'] = object_schema({f'p{i}': {'type': 'string'} for i in range(60)})
    seen, cursor = [], 0
    while True:
        page = craft.tools.execute(TOOLS, {'app': 'wordcraft', 'cursor': cursor},
            operation_id='page-'+str(cursor), context=context, cancelled=lambda: False)
        assert len(encoded(page)) <= 16*1024 and page['tools']
        assert all(item['description'] == '短'*250 for item in page['tools'])
        seen.extend(item['name'] for item in page['tools'])
        if page['next_cursor'] is None:
            break
        assert page['next_cursor'] > cursor
        cursor = page['next_cursor']
    assert seen == list(session['tools'])


def test_T8_s06_i8_directory_only_and_recompute_larger_window():
    business = [fn('read_large', 12000)]
    internal = [fn('ryn_update_plan', 12000), fn('ryn_request_input', 12000)]
    specs, state = tool_menu.project(business, internal, {}, max_context_bytes=8000, authority_bytes=1000,
        epoch=0, goal_revision=1, pinned_names=['read_large'])
    assert [s['function']['name'] for s in specs] == [tool_menu.NAME]
    assert state['degraded'] == 'directory_only' and state['pins_released'] is True
    assert state['pinned_names'] == [] and state['lazy_controls'] is True
    result, loaded = tool_menu.discover(business, internal, lambda _: {'effect': 'read'}, state, {}, capacity_bytes=6744)
    assert result['total'] == 3
    specs, restored = tool_menu.project(business, internal, state, max_context_bytes=200000, authority_bytes=1000,
        epoch=0, goal_revision=1, pinned_names=['read_large'])
    assert 'read_large' in [s['function']['name'] for s in specs]
    assert 'degraded' not in restored and not restored.get('pins_released')
    staged, state = tool_menu.project([fn('small')], internal, {}, max_context_bytes=8000,
        authority_bytes=1000, epoch=0, goal_revision=1)
    assert state['lazy_controls'] is True and 'degraded' not in state
    assert {item['function']['name'] for item in staged} == {tool_menu.NAME, 'small'}


def test_T9_s06_i8_directory_too_big_has_actionable_rejection():
    with pytest.raises(AgentToolRejected, match='agent_tool_schema_limit') as error:
        tool_menu.project([fn('read', 12000)], [fn('ryn_update_plan', 12000)], {},
            max_context_bytes=8000, authority_bytes=7700, epoch=0, goal_revision=1)
    assert error.value.correction == '请换更大上下文的模型'


def test_T4_s06_i8_combinator_stage_and_top_level_names_survive():
    schema = object_schema({'choice': {'type': 'string', 'anyOf': [{'enum': ['a'*100+str(i)]} for i in range(70)]}}, ['choice'])
    result = compact(schema)
    assert result['properties']['choice'] == {'type': 'string'}
    assert result['required'] == ['choice']
    flat = object_schema({f'argument_{i}': {'type': 'string'} for i in range(200)}, ['argument_0'])
    result = compact(flat, 100)
    assert list(result['properties']) == list(flat['properties']) and result['required'] == ['argument_0']
    assert len(encoded(result)) > 100  # Best effort never silently deletes parameter names.


def test_T6_s06_i8_actual_stdio_call_after_small_window_discovery(ext_env, tmp_path):
    runtime, model, tools, workspace, home = ext_env
    script, counts = tmp_path/'large_mcp.py', tmp_path/'counts.json'
    _write_fake_mcp_server(script, counts)
    script.write_text(script.read_text(encoding='utf-8').replace('"label": {"type": "string"}',
        '"label": {"type": "string", "description": "x"*20000}'), encoding='utf-8')
    store = tools.extensions.store
    server = store.upsert_mcp_server({'id': 'large-real', 'name': 'Large real', 'transport': 'stdio',
        'command': sys.executable, 'args': ['-u', str(script)], 'enabled': True, 'read_only_tools': ['ping_info']},
        expected_revision=store.overview()['revision'])
    tools.extensions.manager.test_server('large-real', expected_revision=server['revision'])
    name = next(item['model_tool_name'] for item in store.get_mcp_server_internal('large-real')['discovered_tools']
                if item['name'] == 'ping_info')
    specs = tools.extensions.specs() + [fn('filler_'+str(i), 1000) for i in range(20)]
    exposed, state = tool_menu.project(specs, [], {}, max_context_bytes=16000, authority_bytes=1000, epoch=0, goal_revision=1)
    assert state['mode'] == 'directory'
    _, state = tool_menu.discover(specs, [], tools.metadata, state, {'load': [name]}, capacity_bytes=14744)
    exposed, _ = tool_menu.project(specs, [], state, max_context_bytes=16000, authority_bytes=1000, epoch=0, goal_revision=1)
    assert name in [item['function']['name'] for item in exposed]
    assert next(item for item in exposed if item['function']['name'] == name)['function']['description'].endswith('(schema simplified)')
    task = runtime.create({'message': 'Read ping'})
    task = runtime.store.read()['tasks'][task['id']]
    result = tools.execute(name, {'label': 'real'}, operation_id='large-read', cancelled=lambda: False,
                           context={'task_id': task['id'], 'local_execution': task['local_execution']})
    assert json.loads(counts.read_text())['ping_info'] == 1
    assert 'real' in json.dumps(result)
    host = RynTools.__new__(RynTools)
    host.extensions = tools.extensions
    with pytest.raises(AgentToolRejected) as error:
        host._validate(name, {'label': 3})
    assert error.value.details['field_path'] == 'label'
    assert json.loads(counts.read_text())['ping_info'] == 1
