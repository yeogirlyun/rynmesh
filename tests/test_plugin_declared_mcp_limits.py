"""Publisher-declared MCP limits (tool_timeout_ms, enabled/disabled tools) reach clients.

Covers the packaged declarations, the three-way release migration for already
installed clients, the runtime per-call timeout and the tool filter.
"""
import copy
import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
AFFILIATE_TOOLS = ['account_list', 'order_list', 'tracker_overview', 'withdrawal_list']
SHARED_TOOLS = AFFILIATE_TOOLS + ['journal_records', 'balance_list']


def _store(tmp_path):
    from rynmesh.agent_runtime.extensions import ExtensionStore
    from rynmesh.services.peer_box import load_or_create_messaging_key
    return ExtensionStore(tmp_path / 'agent', load_or_create_messaging_key(tmp_path / 'key'))


def _server(sid, plugin, **extra):
    return {'id': sid, 'plugin_id': plugin, 'transport': 'streamable_http',
            'url': 'https://synthetic.example.test/ryn/mcp', **extra}


def test_packager_carries_declared_timeouts_and_tool_lists(tmp_path, monkeypatch):
    script_dir = ROOT / 'scripts'
    monkeypatch.syspath_prepend(str(script_dir))
    spec = importlib.util.spec_from_file_location('prepare_declared_limits', script_dir / 'prepare_full_package.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    plugins = {slug: {'id': slug, 'name': slug} for slug in packager.COMPANY}
    servers = {}
    for slug in packager.COMPANY:
        servers[slug + '-mcp'] = _server(slug + '-mcp', slug, auth={'mode': 'none'}, startup_timeout_ms=10000,
                                         tool_timeout_ms=300000, enabled_tools=None, disabled_tools=[])
    servers['company-affiliate-mcp']['enabled_tools'] = AFFILIATE_TOOLS
    (tmp_path / 'plugin-declarations.json').write_text(json.dumps({'plugins': plugins, 'mcp_servers': servers}),
                                                       encoding='utf-8')
    packed = {s['id']: s for s in packager.company_declarations(tmp_path)['mcp_servers']}
    affiliate = packed['company-affiliate-mcp']
    assert affiliate['tool_timeout_ms'] == 300000 and affiliate['startup_timeout_ms'] == 10000
    assert affiliate['enabled_tools'] == AFFILIATE_TOOLS and affiliate['disabled_tools'] == []
    assert packed['company-dashboard-mcp']['enabled_tools'] is None


def _release(**dashboard):
    return {'mcp_servers': [
        _server('dash', 'p', tool_timeout_ms=300000, enabled_tools=None, disabled_tools=[], **dashboard),
        _server('aff', 'p', tool_timeout_ms=300000, enabled_tools=AFFILIATE_TOOLS, disabled_tools=[]),
    ]}


def test_installed_client_moves_from_old_default_but_keeps_owner_choices(tmp_path):
    store = _store(tmp_path)
    # Installed before the fields were carried: old built-in 30 s default, no filter.
    store.commit_imported_items(plugins=[{'id': 'p', 'mcp_server_ids': ['dash', 'aff', 'own'], 'skill_ids': []}],
        skills=[], mcp_servers=[_server('dash', 'p', tool_timeout_ms=30000), _server('aff', 'p', tool_timeout_ms=30000),
                                _server('own', 'p', tool_timeout_ms=120000, enabled_tools=['journal_records'])])
    before = copy.deepcopy(store._read()['mcp_servers'])
    declarations = _release()
    declarations['mcp_servers'].append(_server('own', 'p', tool_timeout_ms=300000, enabled_tools=AFFILIATE_TOOLS))
    result = store.migrate_release_defaults(declarations)
    after = store._read()['mcp_servers']
    assert after['dash']['tool_timeout_ms'] == 300000 and after['dash']['enabled_tools'] is None
    assert after['aff']['tool_timeout_ms'] == 300000 and after['aff']['enabled_tools'] == sorted(AFFILIATE_TOOLS)
    # Same connection identity; a new revision so no live session keeps the old limit.
    for sid in ('dash', 'aff'):
        assert after[sid]['instance_id'] == before[sid]['instance_id']
        assert after[sid]['config_generation'] == before[sid]['config_generation']
        assert after[sid]['revision'] > before[sid]['revision']
    # The owner's deliberate values are kept and reported.
    assert after['own']['tool_timeout_ms'] == 120000 and after['own']['enabled_tools'] == ['journal_records']
    assert {(c['id'], c['field']) for c in result['conflicts']} == {('own', 'tool_timeout_ms'), ('own', 'enabled_tools')}
    assert store.migrate_release_defaults(declarations)['updated'] == []

    # Later releases follow the saved default; an owner edit after that is kept.
    store.upsert_mcp_server({'tool_timeout_ms': 90000}, server_id='dash',
                            expected_revision=store.get_mcp_server_internal('dash')['revision'])
    later = _release()
    later['mcp_servers'][0]['tool_timeout_ms'] = 600000
    later['mcp_servers'][1]['tool_timeout_ms'] = 600000
    store.migrate_release_defaults(later)
    final = store._read()['mcp_servers']
    assert final['dash']['tool_timeout_ms'] == 90000
    assert final['aff']['tool_timeout_ms'] == 600000


def test_current_builtin_default_also_counts_as_unchanged(tmp_path):
    store = _store(tmp_path)
    store.commit_imported_items(plugins=[{'id': 'p', 'mcp_server_ids': ['dash', 'aff'], 'skill_ids': []}],
                                skills=[], mcp_servers=[_server('dash', 'p'), _server('aff', 'p')])
    assert store._read()['mcp_servers']['dash']['tool_timeout_ms'] == 300000
    declarations = _release()
    declarations['mcp_servers'][0]['tool_timeout_ms'] = 240000
    assert store.migrate_release_defaults(declarations)['conflicts'] == []
    assert store._read()['mcp_servers']['dash']['tool_timeout_ms'] == 240000


def test_runtime_session_uses_the_declared_300_second_limit():
    from rynmesh.agent_runtime.extensions.mcp_transport import StreamableHttpMcpSession, _http_timeout
    session = StreamableHttpMcpSession({**_server('dash', 'p', auth={'mode': 'none'}), 'tool_timeout_ms': 300000,
                                        'instance_id': 'i', 'revision': 1, 'config_generation': 1})
    try:
        assert session.tool_timeout == 300.0
        assert _http_timeout(session.tool_timeout).read == 300.0
    finally:
        close = getattr(session, 'close', None)
        if callable(close):
            close()


def _adapter(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    from rynmesh.agent_runtime.store import AgentStore
    home = tmp_path / 'home'
    ws = tmp_path / 'workspace'
    ws.mkdir(parents=True, exist_ok=True)
    agent_store = AgentStore(home / 'agent', X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data['settings'].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store
    return ExtensionToolAdapter(local, lambda: ledger, node_store=node_store)


def test_enabled_tools_removes_duplicates_and_ignores_retired_names(tmp_path):
    adapter = _adapter(tmp_path)
    store = adapter.store
    for sid, enabled in (('dash', None), ('aff', AFFILIATE_TOOLS + ['retired_tool'])):
        store.upsert_mcp_server({'id': sid, 'name': sid, 'transport': 'streamable_http', 'enabled': True,
                                 'url': 'http://127.0.0.1:9993/ryn/mcp', 'auth': {'mode': 'none'},
                                 'enabled_tools': enabled, 'tool_timeout_ms': 300000},
                                expected_revision=store.overview()['revision'])
        info = store.get_mcp_server_internal(sid, require_enabled=True)
        store.record_mcp_discovery(sid, expected_revision=info['revision'],
            expected_config_generation=info['config_generation'], expected_instance_id=info['instance_id'],
            status='ready', discovered_tools=[{'name': n, 'inputSchema': {'type': 'object'}} for n in SHARED_TOOLS])
    active = adapter._active_mcp_tools()
    by_server = {}
    for _name, (server, tool, _binding) in active.items():
        by_server.setdefault(server['id'], set()).add(tool['name'])
    assert by_server['dash'] == set(SHARED_TOOLS)
    assert by_server['aff'] == set(AFFILIATE_TOOLS)  # no journal/balance duplicates, no phantom tool
    assert store.get_mcp_server_internal('aff')['status'] == 'ready'
    name = next(n for n, (s, t, _) in active.items() if s['id'] == 'aff' and t['name'] == 'order_list')
    assert adapter.metadata(name)['tool_timeout_ms'] == 300000


class _SlowMcpTools:
    """A read-only MCP-bound tool that takes longer than the script's yield time."""

    def __init__(self, timeout_ms):
        from rynmesh.agent_runtime import code_mode
        self.timeout_ms = timeout_ms
        self.runner = code_mode.CodeModeRunner(self)

    def specs(self):
        return [{'type': 'function', 'function': {'name': 'mcp__report', 'description': 'report',
                 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}]

    def metadata(self, name):
        meta = {'effect': 'read', 'scope_context_supported': False}
        if self.timeout_ms is not None:
            meta.update(extension_binding={'server_id': 'dash'}, tool_timeout_ms=self.timeout_ms)
        return meta

    def execute(self, name, arguments, *, operation_id, cancelled, context=None, **_options):
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            if cancelled():
                from rynmesh.agent_runtime.store import AgentToolRejected
                raise AgentToolRejected('agent_cancelled')
            time.sleep(0.02)
        return {'text': 'report-done'}


@pytest.mark.parametrize('timeout_ms', [300000, None])
def test_code_mode_lets_a_nested_mcp_call_use_its_connection_limit(timeout_ms):
    from rynmesh.agent_runtime import code_mode
    from rynmesh.agent_runtime.store import AgentToolRejected
    if not code_mode.node_available():
        pytest.skip('Node.js is required for CodeMode')
    tools = _SlowMcpTools(timeout_ms)
    try:
        args = {'code': 'const r = await tools.mcp__report({}); text(JSON.stringify(r));', 'yield_time_ms': 500}
        try:
            outcome = json.dumps(tools.runner.execute(args, operation_id='op', context={'task_id': 't'}),
                                 ensure_ascii=False)
        except AgentToolRejected as exc:
            outcome = str(exc)
        # Without a connection limit the call is still cut at the yield time.
        assert ('report-done' in outcome) == (timeout_ms is not None), outcome
    finally:
        tools.runner.close()
