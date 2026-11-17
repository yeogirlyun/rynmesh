"""S06-I17: real plugin state/native verification and isolated menu sources."""
import copy
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_integrations import node as node
from test_agent_runtime import Catalog, Model, assistant
from test_craft_native import release as release
from test_craft_plugins import manager as manager
from test_plugin_updates import installed as installed_updates

from rynmesh.agent_runtime import diagnostic_log
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.node_apps import craft_native as native, plugins
from rynmesh.node_apps.craft_catalog import CRAFT
from rynmesh.node_apps.craft_agent import TOOLS as CRAFT_TOOLS
from rynmesh.node_apps.craft_host import CraftError
from rynmesh.node_apps.plugin_agent import ALL_LIST


@pytest.fixture
def narrow(manager, monkeypatch):
    monkeypatch.setitem(CRAFT, 'wordcraft', copy.deepcopy(CRAFT['wordcraft']))
    monkeypatch.setattr(plugins, 'CRAFT', {'wordcraft': plugins.CRAFT['wordcraft']})
    monkeypatch.setattr(plugins, 'installer_selection', lambda: None)
    monkeypatch.setattr(native, 'platform_supported', lambda: True)
    return manager


def row(catalog):
    return next(item for item in catalog['plugins'] if item['id'] == 'wordcraft')


def status_counter(monkeypatch):
    calls = []
    original = native.runtime_status
    def status(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(native, 'runtime_status', status)
    return calls


def test_t1_catalog_returns_independent_snapshots_and_refreshes_live_flags(narrow):
    first = narrow.catalog()
    original_name = row(first)['name']
    row(first)['name'] = 'caller mutation'
    row(first)['formats'].append('caller-format')
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    narrow.servers['wordcraft'] = (SimpleNamespace(should_exit=False), thread, SimpleNamespace(close=lambda: None))
    narrow.mutating.add('wordcraft')
    try:
        current = narrow.catalog()
        assert row(current)['name'] == original_name and 'caller-format' not in row(current)['formats']
        assert row(current)['running'] and row(current)['busy']
        narrow.mutating.clear()
        narrow.active['wordcraft'] = 1
        assert row(narrow.catalog())['busy']
        narrow.active.clear()
        assert not row(narrow.catalog())['busy']
        release.set()
        thread.join()
        assert not row(narrow.catalog())['running']
    finally:
        release.set()
        thread.join()
        narrow.servers.pop('wordcraft', None)
        narrow.mutating.clear()
        narrow.active.clear()


def test_t1_state_and_installer_changes_invalidate_catalog_without_state_writes(narrow, monkeypatch):
    calls = status_counter(monkeypatch)
    narrow.catalog()
    state = narrow._read()
    state['setup_completed'] = True
    narrow.state_path.parent.mkdir(parents=True, exist_ok=True)
    narrow.state_path.write_text(json.dumps(state), encoding='utf-8')
    assert narrow.catalog()['setup_completed'] is True
    monkeypatch.setattr(plugins, 'installer_selection', lambda: ['wordcraft'])
    assert narrow.catalog()['selection'] == ['wordcraft']
    assert narrow.catalog()['setup_requested'] is True
    assert len(calls) == 3


def test_t1_local_write_generation_invalidates_even_when_stat_signature_is_unchanged(narrow, monkeypatch):
    narrow._write(narrow._read())
    narrow.catalog()
    fixed = narrow.state_path.stat()
    stat = Path.stat
    def same_stat(path, *args, **kwargs):
        return fixed if path == narrow.state_path else stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', same_stat)
    calls = status_counter(monkeypatch)
    narrow.finish_setup()
    assert narrow.catalog()['setup_completed'] is True and len(calls) == 1


def test_t1_failed_refresh_does_not_revive_an_expired_snapshot(narrow, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(plugins.time, 'monotonic', lambda: clock[0])
    narrow.catalog()
    clock[0] += 31
    def broken(*args):
        raise CraftError('catalog_refresh_failed')
    monkeypatch.setattr(native, 'runtime_status', broken)
    for _ in range(2):
        with pytest.raises(CraftError, match='catalog_refresh_failed'):
            narrow.catalog()


def test_t2_fifty_catalog_reads_compute_native_status_once(narrow, monkeypatch):
    calls = status_counter(monkeypatch)
    for _ in range(50):
        assert row(narrow.catalog())['id'] == 'wordcraft'
    assert len(calls) == 1


def test_t3_enable_invalidates_cached_tools_without_shadowing_generation_api(narrow, monkeypatch):
    narrow.install('wordcraft')
    narrow.set_enabled('wordcraft', False)
    assert row(narrow.catalog())['enabled'] is False
    calls = status_counter(monkeypatch)
    narrow.set_enabled('wordcraft', True)
    assert row(narrow.catalog())['enabled'] is True
    assert len(calls) == 1
    assert callable(narrow.generation) and isinstance(narrow.generation('wordcraft'), str)


@pytest.mark.parametrize('action', ['install', 'configure', 'finish_setup', 'uninstall'])
def test_t3_state_writing_paths_refresh_catalog_once(narrow, monkeypatch, action):
    if action != 'install':
        narrow.install('wordcraft')
    narrow.catalog()
    calls = status_counter(monkeypatch)
    if action == 'install':
        narrow.install('wordcraft')
    elif action == 'configure':
        narrow.configure('wordcraft', {'theme': 'dark'})
    elif action == 'finish_setup':
        narrow.finish_setup()
    else:
        narrow.uninstall('wordcraft')
    result = narrow.catalog()
    assert len(calls) == 1
    if action == 'install':
        assert row(result)['installed'] and row(result)['enabled']
    elif action == 'uninstall':
        assert not row(result)['installed']
    elif action == 'finish_setup':
        assert result['setup_completed']
    else:
        assert narrow.get_configuration('wordcraft') == {'theme': 'dark'}


@pytest.mark.parametrize('restore', ['rollback', 'recover'])
def test_t3_signed_update_and_restore_refresh_the_real_cached_catalog(installed_updates, monkeypatch, restore):
    fixture = installed_updates
    manager = fixture.manager
    # The old shared fixture capped compatibility at 0.9.0; this host is 0.9.1.
    fixture.payload['max_host_version'] = '1.0.0'
    calls = status_counter(monkeypatch)
    manager.catalog()
    calls.clear()
    manager.catalog()
    assert not calls
    manager.updates.check('wordcraft')
    manager.updates.apply('wordcraft')
    assert row(manager.catalog())['version'] == '0.8.0'
    assert len(calls) == len(plugins.CRAFT)
    calls.clear()
    if restore == 'rollback':
        manager.updates.rollback('wordcraft')
    else:
        (manager.verify('wordcraft') / 'index.html').write_text('corrupt')
        manager.updates.recover()
    assert row(manager.catalog())['version'] == CRAFT['wordcraft']['tag']
    assert len(calls) == len(plugins.CRAFT)


def test_t3_explicit_automation_install_refreshes_catalog_without_a_state_write(narrow, release):
    narrow.install('wordcraft')
    assert row(narrow.catalog())['editing_supported'] is False
    narrow.download = release()
    result = narrow.install_automation('wordcraft')
    assert row(result)['editing_supported'] is True
    assert row(narrow.catalog())['automation_installed'] is True


def test_t4_menu_stamp_walk_is_cached_for_thirty_seconds_without_rehashing_unchanged_payload(tmp_path, release, monkeypatch):
    native.install_runtime(tmp_path, 'wordcraft', download=release())
    clock = [100.0]
    monkeypatch.setattr(native.time, 'monotonic', lambda: clock[0])
    scans, hashes = [], []
    stamp, digest = native._status_stamp, native.craft_host.digest
    def scan(root):
        scans.append(root)
        return stamp(root)
    def hash_file(path):
        hashes.append(path)
        return digest(path)
    monkeypatch.setattr(native, '_status_stamp', scan)
    monkeypatch.setattr(native.craft_host, 'digest', hash_file)
    assert native.runtime_status(tmp_path, 'wordcraft')['state'] == 'ready'
    initial_scans, initial_hashes = len(scans), len(hashes)
    for _ in range(50):
        assert native.runtime_status(tmp_path, 'wordcraft')['verified']
    assert len(scans) == initial_scans and len(hashes) == initial_hashes
    clock[0] += 31
    assert native.runtime_status(tmp_path, 'wordcraft')['verified']
    assert len(scans) == initial_scans + 1 and len(hashes) == initial_hashes


@pytest.fixture
def real_tools(tmp_path, node, narrow):
    app, store = node
    app.state.craft_plugins = narrow
    _, tools, _ = build_agent_integrations(app, store, store.home)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    model = Model()
    runtime = AgentRuntime(AgentStore(tmp_path / 'agent', X25519PrivateKey.generate()), model, tools, Catalog(),
                           consumer_node_id=store.peer_id)
    runtime.update_settings({'enabled': True, 'model_id': 'local:test', 'workspace_root': str(workspace),
                             'permission_mode': 'read_only', 'desktop_access': False})
    try:
        yield tools, runtime, model
    finally:
        tools.local.processes.close()
        tools.extensions.close()
        tools.close()


def test_t4_warm_menu_can_be_stale_but_real_session_start_still_verifies_every_byte(real_tools, narrow, release, monkeypatch):
    tools, runtime, _ = real_tools
    narrow.install('wordcraft')
    native.install_runtime(narrow.cache, 'wordcraft', download=release())
    clock = [100.0]
    monkeypatch.setattr(native.time, 'monotonic', lambda: clock[0])
    executable = Path(native.runtime_status(narrow.cache, 'wordcraft')['executable'])
    (executable.parent / 'runtime.dll').write_bytes(b'tampered native payload')
    clock[0] += 10
    assert native.runtime_status(narrow.cache, 'wordcraft')['state'] == 'ready'
    with pytest.raises(CraftError, match='not_verified'):
        native.installed_runtime(narrow.cache, 'wordcraft')
    started = []
    def no_process(*args, **kwargs):
        started.append(args)
        raise AssertionError('unverified payload reached process startup')
    monkeypatch.setattr(native, 'McpProcess', no_process)
    task = runtime.create({'message': 'Discover document tools'})
    context = runtime._execution_context(runtime.store.read()['tasks'][task['id']])
    with pytest.raises(CraftError, match='not_verified'):
        tools.craft._session('wordcraft', context, lambda: False)
    assert started == []
    clock[0] += 21
    assert native.runtime_status(narrow.cache, 'wordcraft')['state'] == 'unverified'


def test_t4_catalog_also_expires_menu_evidence_after_thirty_seconds(narrow, release, monkeypatch):
    narrow.install('wordcraft')
    native.install_runtime(narrow.cache, 'wordcraft', download=release())
    # The explicit lifecycle write makes the first catalog a fresh verified view.
    narrow._write(narrow._read())
    clock = [100.0]
    monkeypatch.setattr(native.time, 'monotonic', lambda: clock[0])
    assert row(narrow.catalog())['editing_supported']
    executable = native.installed_runtime(narrow.cache, 'wordcraft')
    (executable.parent / 'runtime.dll').write_bytes(b'tampered payload')
    clock[0] += 10
    assert row(narrow.catalog())['editing_supported']
    clock[0] += 21
    assert not row(narrow.catalog())['editing_supported']


def test_t5_menu_diagnostics_do_not_wait_for_another_business_operation(real_tools):
    tools, _, _ = real_tools
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    names = set()
    def operation():
        with tools.lock:
            held.set()
            release.wait()
    def menu():
        try:
            names.update(item['function']['name'] for item in tools.specs())
        finally:
            done.set()
    business = threading.Thread(target=operation)
    lookup = threading.Thread(target=menu)
    business.start()
    try:
        assert held.wait(3)
        lookup.start()
        assert done.wait(3), 'menu waited for the unrelated business operation lock'
        assert 'ryn_file_read' in names
    finally:
        release.set()
        business.join()
        if lookup.ident is not None:
            lookup.join()


def test_t5_source_failures_are_deduplicated_by_class_until_recovery(real_tools, monkeypatch):
    tools, _, _ = real_tools
    failures = []
    monkeypatch.setattr(diagnostic_log, 'failure', lambda stage, exc, **context: failures.append((stage, type(exc).__name__, context['source'])))
    original = tools.craft.specs
    def broken():
        raise CraftError('plugin_state_invalid')
    monkeypatch.setattr(tools.craft, 'specs', broken)
    for _ in range(5):
        tools.specs()
    assert tools.spec_source_errors['craft'] == 'CraftError'
    assert failures == [('tool_menu_source', 'CraftError', 'craft')]
    monkeypatch.setattr(tools.craft, 'specs', original)
    tools.specs()
    assert 'craft' not in tools.spec_source_errors
    monkeypatch.setattr(tools.craft, 'specs', broken)
    tools.specs()
    assert len(failures) == 2


def test_t4_catalog_never_extends_the_native_evidence_deadline(narrow, release, monkeypatch):
    narrow.install('wordcraft')
    native.install_runtime(narrow.cache, 'wordcraft', download=release())
    clock = [100.0]
    monkeypatch.setattr(native.time, 'monotonic', lambda: clock[0])
    assert native.runtime_status(narrow.cache, 'wordcraft')['verified']
    clock[0] = 120.0
    narrow.finish_setup()
    assert row(narrow.catalog())['editing_supported']
    executable = native.installed_runtime(narrow.cache, 'wordcraft')
    (executable.parent / 'runtime.dll').write_bytes(b'corrupt')
    clock[0] = 131.0
    assert not row(narrow.catalog())['editing_supported']


def test_t5_named_lookup_is_isolated_and_never_rebuilds_the_whole_menu(real_tools, monkeypatch):
    tools, _, _ = real_tools
    def broken():
        raise CraftError('plugin_state_invalid')
    monkeypatch.setattr(tools.craft, 'specs', broken)
    monkeypatch.setattr(tools, 'specs', lambda *args: pytest.fail('whole menu rebuilt'))
    assert tools.spec('ryn_file_read')['name'] == 'ryn_file_read'
    assert tools.spec(ALL_LIST)['name'] == ALL_LIST
    assert tools.spec(CRAFT_TOOLS) is None
    assert tools.spec_source_errors['craft'] == 'CraftError'


def test_t5_real_source_diagnostic_keeps_source_and_class_without_exception_text(caplog):
    with caplog.at_level(logging.WARNING, logger=diagnostic_log.__name__):
        with diagnostic_log.context_scope(task_id='task-safe'):
            diagnostic_log.failure('tool_menu_source', CraftError('PRIVATE_EXCEPTION_CANARY'), source='craft')
    assert "'source': 'craft'" in caplog.text and "'type': 'CraftError'" in caplog.text
    assert 'task-safe' in caplog.text
    assert 'PRIVATE_EXCEPTION_CANARY' not in caplog.text


@pytest.mark.parametrize('label', ['craft', 'craft_plugins', 'extensions', 'diagnostics'])
def test_t6_one_failed_source_keeps_other_sources_and_builtin_specs(real_tools, monkeypatch, label):
    tools, _, _ = real_tools
    def broken(*args, **kwargs):
        raise CraftError('plugin_state_invalid')
    monkeypatch.setattr(getattr(tools, label), 'specs', broken)
    names = {item['function']['name'] for item in tools.specs()}
    assert 'ryn_file_read' in names and 'ryn_file_search' in names
    assert tools.spec_source_errors[label] == 'CraftError'
    if label != 'diagnostics':
        assert 'ryn_diagnose_current_task' in names


def test_t7_real_runtime_survives_corrupt_plugin_state_and_next_menu_recovers(real_tools, narrow):
    tools, runtime, model = real_tools
    narrow.install('wordcraft')
    original = narrow.state_path.read_bytes()
    narrow.state_path.write_text('{broken state', encoding='utf-8')
    model.responses.append(assistant('Built-in tools remain available'))
    task = runtime.create({'message': 'Continue with available built-in tools'})
    for _ in range(4):
        runtime.run_once()
        if runtime.get(task['id'])['status'] in {'completed', 'failed'}:
            break
    assert runtime.get(task['id'])['status'] == 'completed'
    assert tools.spec_source_errors['craft'] == tools.spec_source_errors['craft_plugins'] == 'CraftError'
    assert any(item['function']['name'] == 'ryn_file_read' for item in tools.specs())
    narrow.state_path.write_bytes(original)
    names = {item['function']['name'] for item in runtime._business_specs(runtime.store.read()['tasks'][task['id']])}
    assert CRAFT_TOOLS in names and ALL_LIST in names
    assert 'craft' not in tools.spec_source_errors and 'craft_plugins' not in tools.spec_source_errors
