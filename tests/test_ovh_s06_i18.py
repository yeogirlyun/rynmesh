"""S06-I18: real Craft routing, per-document concurrency and verified leases."""
import copy
import hashlib
import io
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_agent_integrations import node as node
from test_craft_agent import craft as craft, _args, _context
from test_craft_plugins import manager as manager
from test_plugin_updates import installed as installed_updates

from rynmesh.agent_runtime import tool_dispatch
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.node_apps import craft_host, craft_native
from rynmesh.node_apps import builtin_plugins as builtin
from rynmesh.node_apps.craft_agent import ACTION, READ, TOOLS
from rynmesh.node_apps.craft_catalog import CRAFT


def contexts(craft):
    runtime, _ = craft.runtime(mode='automatic')
    result = [_context(runtime.create({'message': f'Document {index}'})) for index in range(2)]
    for index, context in enumerate(result):
        craft.tools.execute(TOOLS, {'app': 'wordcraft'}, context=context,
                            operation_id=f'discover-{index}', cancelled=lambda: False)
    return result


def edit(craft, context, operation, text='edited'):
    args = _args('type_text', {'text': text})
    return args, craft.tools.preview(ACTION, args, context=context), operation


def action(craft, context, prepared):
    args, preview, operation = prepared
    return craft.tools.execute(ACTION, args, context=context, operation_id=operation,
                               expected_preview=preview, cancelled=lambda: False)


def read(craft, context):
    return craft.tools.execute(READ, _args('get_text'), context=context,
                               operation_id='read', cancelled=lambda: False)


def delay_action(craft, context, monkeypatch, entered, release=None):
    process = craft.tools.craft.sessions[(context['task_id'], 'wordcraft')]['process']
    original = process.tools_call
    def call(name, args, **kwargs):
        if name == 'type_text':
            entered.set()
            if release is None:
                time.sleep(1)
            else:
                assert release.wait(15), 'test did not release the external engine'
        return original(name, args, **kwargs)
    monkeypatch.setattr(process, 'tools_call', call)
    return process


def test_t1_another_task_read_finishes_during_one_second_action(craft, monkeypatch):
    first, second = contexts(craft)
    prepared = edit(craft, first, 'slow-edit')
    entered = threading.Event()
    delay_action(craft, first, monkeypatch, entered)
    gate = tool_dispatch.ToolDispatchGate()
    for name in (READ, TOOLS):
        assert tool_dispatch.supports_parallel(name, craft.tools.metadata(name), {'app': 'wordcraft'},
                                               permission_mode='automatic', workspace_root=str(craft.workspace))
    assert not tool_dispatch.supports_parallel(ACTION, craft.tools.metadata(ACTION), prepared[0],
                                               permission_mode='automatic', workspace_root=str(craft.workspace))
    def run_action():
        with gate.enter(first['task_id'], parallel=False, cancelled=lambda: False):
            return action(craft, first, prepared)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(run_action)
        assert entered.wait(10)
        started = time.perf_counter()
        with gate.enter(second['task_id'], parallel=True, cancelled=lambda: False):
            result = read(craft, second)
        elapsed = time.perf_counter() - started
        assert result['data']['text'] == ''
        assert elapsed < .3, f'independent document read took {elapsed:.3f}s'
        assert not pending.done()
        assert pending.result(timeout=10)['ok']
    assert READ in tool_dispatch.PARALLEL_READS and TOOLS in tool_dispatch.PARALLEL_READS


def test_t1_another_application_of_the_same_task_can_read_concurrently(craft, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('gridcraft/index.html', '<title>Grid boundary fixture</title>')
        output.writestr('gridcraft/LICENSE', 'Fixture license')
    raw = archive.getvalue()
    monkeypatch.setitem(CRAFT, 'gridcraft', {**CRAFT['gridcraft'], 'web_sha256': hashlib.sha256(raw).hexdigest()})
    previous_download = craft.manager.download
    def download(url, path):
        if 'gridcraft-web-' in url:
            path.write_bytes(raw)
        else:
            previous_download(url, path)
    monkeypatch.setattr(craft.manager, 'download', download)
    status = craft_native.runtime_status
    monkeypatch.setattr(craft_native, 'runtime_status', lambda cache, app:
                        {**status(cache, 'wordcraft' if app == 'gridcraft' else app), 'app': app})
    factory = craft.backend.process
    def process(executable, workspace, app, **kwargs):
        result = factory(executable, workspace, app, **kwargs)
        if app == 'gridcraft':
            def tools_list(*, cancelled=None):
                result.check(cancelled)
                return {'tools': [{'name': 'inspect_workbook', 'description': 'Grid boundary inspection',
                                   'inputSchema': {'type': 'object', 'properties': {}, 'required': []}}]}
            def tools_call(name, args, *, cancelled=None, **_):
                result.check(cancelled)
                assert name == 'inspect_workbook' and args == {}
                craft.backend.calls.append((result, name, copy.deepcopy(args)))
                return {'content': [{'type': 'text', 'text': json.dumps({'cells': []})}], 'isError': False}
            result.tools_list, result.tools_call = tools_list, tools_call
        return result
    monkeypatch.setattr(craft_native, 'McpProcess', process)
    craft.manager.install('gridcraft')
    first, _ = contexts(craft)
    craft.tools.execute(TOOLS, {'app': 'gridcraft'}, context=first,
                        operation_id='grid-discovery', cancelled=lambda: False)
    prepared = edit(craft, first, 'word-edit')
    entered = threading.Event()
    delay_action(craft, first, monkeypatch, entered)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(action, craft, first, prepared)
        assert entered.wait(10)
        started = time.perf_counter()
        result = craft.tools.execute(READ, {'app': 'gridcraft', 'tool': 'inspect_workbook', 'arguments': {}},
                                     context=first, operation_id='grid-read', cancelled=lambda: False)
        elapsed = time.perf_counter() - started
        assert result['data']['cells'] == [] and elapsed < .3
        assert not pending.done() and pending.result(timeout=10)['ok']


@pytest.mark.parametrize('waiter', ['read', 'preview'])
def test_t1_waiting_for_one_session_does_not_hold_the_session_dictionary(craft, monkeypatch, waiter):
    first, second = contexts(craft)
    prepared = edit(craft, first, 'blocked-edit')
    entered, release, requested = threading.Event(), threading.Event(), threading.Event()
    delay_action(craft, first, monkeypatch, entered, release)
    def wait_same_document():
        requested.set()
        if waiter == 'preview':
            return craft.tools.preview(ACTION, _args('type_text', {'text': 'next'}), context=first)
        return read(craft, first)
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = pool.submit(action, craft, first, prepared)
        blocked = None
        try:
            assert entered.wait(10)
            blocked = pool.submit(wait_same_document)
            assert requested.wait(5)
            assert not blocked.done()
            independent = pool.submit(read, craft, second)
            assert independent.result(timeout=.3)['data']['text'] == ''
            assert not pending.done() and not blocked.done()
        finally:
            release.set()
        assert pending.result(timeout=10)['ok']
        assert blocked.result(timeout=10)


def test_t2_same_document_read_waits_for_edit_and_observes_its_revision(craft, monkeypatch):
    first, _ = contexts(craft)
    prepared = edit(craft, first, 'serialized-edit', 'new text')
    entered, release, read_entered, requested = (threading.Event() for _ in range(4))
    process = delay_action(craft, first, monkeypatch, entered, release)
    delayed = process.tools_call
    def call(name, args, **kwargs):
        if name == 'get_text':
            read_entered.set()
        return delayed(name, args, **kwargs)
    monkeypatch.setattr(process, 'tools_call', call)
    def request_read():
        requested.set()
        return read(craft, first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        editing = pool.submit(action, craft, first, prepared)
        reading = None
        try:
            assert entered.wait(10)
            reading = pool.submit(request_read)
            assert requested.wait(5)
            assert not read_entered.wait(.3)
        finally:
            release.set()
        changed = editing.result(timeout=10)
        result = reading.result(timeout=10)
    assert result['data']['text'] == 'new text'
    assert result['session_revision'] == changed['session_revision'] == 1


@pytest.mark.parametrize('close_all', [False, True])
def test_t2_closing_detaches_sessions_before_waiting_for_their_calls(craft, monkeypatch, close_all):
    first, second = contexts(craft)
    prepared = edit(craft, first, 'closing-edit')
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    process = delay_action(craft, first, monkeypatch, entered, release)
    adapter = craft.tools.craft
    def close():
        closing.set()
        if close_all:
            adapter.close()
        else:
            adapter.close_task(first['task_id'])
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = pool.submit(action, craft, first, prepared)
        closed = None
        try:
            assert entered.wait(10)
            closed = pool.submit(close)
            assert closing.wait(5)
            deadline = time.monotonic() + .3
            detached = False
            while time.monotonic() < deadline:
                if adapter.lock.acquire(blocking=False):
                    try:
                        detached = (first['task_id'], 'wordcraft') not in adapter.sessions
                    finally:
                        adapter.lock.release()
                    if detached:
                        break
                time.sleep(.005)
            assert detached, 'close waited while retaining the session dictionary lock'
            assert not closed.done() and not process.closed.is_set()
            if not close_all:
                assert pool.submit(read, craft, second).result(timeout=.3)['data']['text'] == ''
        finally:
            release.set()
        assert pending.result(timeout=10)['ok']
        closed.result(timeout=10)
    assert process.closed.is_set()


def test_t1_another_action_does_not_wait_for_a_peer_mcp_call(craft, monkeypatch):
    first, second = contexts(craft)
    prepared = edit(craft, first, 'held-action')
    other = edit(craft, second, 'independent-action', 'other text')
    entered, release = threading.Event(), threading.Event()
    delay_action(craft, first, monkeypatch, entered, release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(action, craft, first, prepared)
        try:
            assert entered.wait(10)
            result = pool.submit(action, craft, second, other).result(timeout=5)
            assert result['ok'] and not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=10)['ok']


def test_t2_ledger_api_calls_remain_serialized_between_independent_sessions(craft, monkeypatch):
    first, second = contexts(craft)
    prepared = [edit(craft, first, 'ledger-first'), edit(craft, second, 'ledger-second')]
    original = OperationLedger.update
    guard, start = threading.Lock(), threading.Barrier(2)
    active, peak = 0, 0
    def update(ledger, *args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.1)
            return original(ledger, *args, **kwargs)
        finally:
            with guard:
                active -= 1
    monkeypatch.setattr(OperationLedger, 'update', update)
    def perform(context, operation):
        start.wait(timeout=10)
        return action(craft, context, operation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(perform, context, operation)
                   for context, operation in zip((first, second), prepared)]
        assert all(future.result(timeout=15)['ok'] for future in futures)
    assert peak == 1
    for context, operation in zip((first, second), prepared):
        row = craft.tools.craft.ledger().get(operation[2])
        assert row['status'] == 'confirmed' and row['binding']['task_id'] == context['task_id']


def installed_counter(monkeypatch):
    calls = []
    original = craft_host.installed
    def installed(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(craft_host, 'installed', installed)
    return calls


def test_t3_twenty_leases_hash_the_real_installed_package_once(manager, monkeypatch):
    manager.install('wordcraft')
    calls = installed_counter(monkeypatch)
    for _ in range(20):
        with manager.lease('wordcraft') as spec:
            assert spec['tag'] == 'v0.6.1' and manager.active['wordcraft'] == 1
    assert len(calls) == 1 and manager.active['wordcraft'] == 0


def test_t3_verification_runs_without_manager_lock_but_reserves_active_lease(manager, monkeypatch):
    manager.install('wordcraft')
    entered, release = threading.Event(), threading.Event()
    original = manager.verify
    def verify(slug):
        entered.set()
        assert release.wait(10)
        return original(slug)
    monkeypatch.setattr(manager, 'verify', verify)
    def lease():
        with manager.lease('wordcraft'):
            return manager.active['wordcraft']
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lease)
        try:
            assert entered.wait(10)
            acquired = manager.lock.acquire(blocking=False)
            assert acquired, 'full verification still holds the global manager lock'
            try:
                assert manager.active['wordcraft'] == 1
                with pytest.raises(craft_host.CraftError, match='plugin_busy'):
                    manager.set_enabled('wordcraft', False)
            finally:
                manager.lock.release()
        finally:
            release.set()
        assert pending.result(timeout=10) == 1
    assert manager.active['wordcraft'] == 0


def test_t4_generation_write_invalidates_even_with_the_same_receipt(manager, monkeypatch):
    manager.install('wordcraft')
    calls = installed_counter(monkeypatch)
    with manager.lease('wordcraft'):
        assert manager.active['wordcraft'] == 1
    state = manager._read()
    state['plugins']['wordcraft']['generation'] = 'replacement-generation'
    manager._write(state)
    with manager.lease('wordcraft'):
        assert manager.generation('wordcraft') == 'replacement-generation'
    assert len(calls) == 2
    with manager.lease('wordcraft'):
        assert manager.active['wordcraft'] == 1
    assert len(calls) == 2


def test_t4_receipt_mtime_invalidates_without_a_generation_write(manager, monkeypatch):
    manager.install('wordcraft')
    calls = installed_counter(monkeypatch)
    with manager.lease('wordcraft'):
        assert manager.active['wordcraft'] == 1
    receipt = manager.cache / 'wordcraft/v0.6.1/receipt.json'
    before = receipt.stat()
    os.utime(receipt, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    with manager.lease('wordcraft'):
        assert manager.active['wordcraft'] == 1
    assert len(calls) == 2
    with manager.lease('wordcraft'):
        assert manager.active['wordcraft'] == 1
    assert len(calls) == 2


def test_t4_real_signed_upgrade_invalidates_verified_lease(installed_updates, monkeypatch):
    fixture = installed_updates
    fixture.payload['max_host_version'] = '1.0.0'
    manager = fixture.manager
    calls = installed_counter(monkeypatch)
    with manager.lease('wordcraft') as spec:
        assert spec['tag'] == 'v0.6.1'
    manager.updates.check('wordcraft')
    manager.updates.apply('wordcraft')
    calls.clear()
    for _ in range(3):
        with manager.lease('wordcraft') as spec:
            assert spec['tag'] == '0.8.0'
    assert len(calls) == 1


def test_t3_failed_verification_is_not_cached_and_releases_the_reservation(manager, monkeypatch):
    manager.install('wordcraft')
    target = manager.cache / 'wordcraft/v0.6.1/payload/wordcraft/index.html'
    original = target.read_bytes()
    calls = installed_counter(monkeypatch)
    target.write_bytes(b'corrupt')
    with pytest.raises(craft_host.CraftError, match='craft_build_not_verified'):
        with manager.lease('wordcraft'):
            pytest.fail('a corrupt web package was leased')
    assert manager.active.get('wordcraft', 0) == 0
    target.write_bytes(original)
    for _ in range(2):
        with manager.lease('wordcraft'):
            assert manager.active['wordcraft'] == 1
    assert len(calls) == 2 and manager.active['wordcraft'] == 0


def test_t4_generation_changed_during_verification_is_not_admitted_or_cached(manager, monkeypatch):
    manager.install('wordcraft')
    calls = installed_counter(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = manager.verify
    def verify(slug):
        result = original(slug)
        entered.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(manager, 'verify', verify)
    def lease():
        with manager.lease('wordcraft'):
            return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lease)
        try:
            assert entered.wait(10)
            acquired = manager.lock.acquire(blocking=False)
            assert acquired, 'verification blocks a state change behind the manager lock'
            try:
                manager.finish_setup()
            finally:
                manager.lock.release()
        finally:
            release.set()
        with pytest.raises(craft_host.CraftError, match='plugin_busy'):
            pending.result(timeout=10)
    assert manager.active['wordcraft'] == 0
    monkeypatch.setattr(manager, 'verify', original)
    with manager.lease('wordcraft'):
        assert manager.active['wordcraft'] == 1
    assert len(calls) == 2


def test_t3_cached_lease_does_not_replace_explicit_launch_verification(manager, monkeypatch):
    manager.install('wordcraft')
    calls = installed_counter(monkeypatch)
    for _ in range(3):
        with manager.lease('wordcraft'):
            assert manager.active['wordcraft'] == 1
    assert len(calls) == 1
    (manager.cache / 'wordcraft/v0.6.1/payload/wordcraft/index.html').write_bytes(b'corrupt')
    with pytest.raises(craft_host.CraftError, match='craft_build_not_verified'):
        manager.preview_launch('wordcraft')
    assert len(calls) == 2 and manager.active['wordcraft'] == 0


def test_t4_builtin_manifest_changes_invalidate_without_receipt_changes(manager, monkeypatch):
    manager.install('resume')
    calls = []
    original = builtin.verify
    def verify(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(builtin, 'verify', verify)
    for _ in range(2):
        with manager.lease('resume'):
            assert manager.active['resume'] == 1
    assert len(calls) == 1
    (builtin.location(manager.cache, 'resume') / 'manifest.json').write_text('{"command":"evil"}')
    with pytest.raises(craft_host.CraftError, match='craft_build_not_verified'):
        with manager.lease('resume'):
            pytest.fail('a changed authorization manifest was leased')
    assert len(calls) == 2 and manager.active['resume'] == 0

