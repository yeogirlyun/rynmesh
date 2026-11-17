"""S02-I10 restart continuation, owner steering and real process termination."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from test_agent_runtime import Catalog, Model, Tools, assistant, make_runtime
from test_agent_codex_loop import WorkstationTools
from rynmesh.agent_runtime import budget, diagnostics, verification
from rynmesh.agent_runtime.execution_policy import classify_operation, verified_read_classification
from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


class RecoveryTools(WorkstationTools):
    def reconcile(self, operation_id, *, context, cancelled=None):
        return self.local.reconcile(operation_id, context)


def stored(runtime, task_id):
    return runtime.store.read()['tasks'][task_id]


def set_running(runtime, task_id):
    runtime.store.transaction(lambda data: data['tasks'][task_id].update(status='running'))


def restart(runtime, key):
    restored = AgentRuntime(AgentStore(runtime.store.root, key), runtime.model, runtime.tools, runtime.catalog)
    restored.reconcile()
    return restored


@pytest.fixture
def workspace(tmp_path):
    home, work = tmp_path / 'private', tmp_path / 'work'
    work.mkdir()
    (work / 'info.txt').write_text('durable read\n', encoding='utf-8')
    runtime, model, _, key = make_runtime(home)
    tools = RecoveryTools(runtime, home)
    runtime.tools = tools
    runtime.update_settings({'workspace_root': str(work), 'permission_mode': 'workspace'})
    yield runtime, model, key, work
    tools.local.processes.close()


def flight_task(workspace, kind='read'):
    runtime, model, key, work = workspace
    name, args = ('ryn_shell_execute', {'command': 'Get-Location' if os.name == 'nt' else 'pwd'}) if kind == 'verified' else (
        ('ryn_file_write', {'path': 'info.txt', 'content': 'changed'}) if kind == 'write' else ('ryn_file_read', {'path': 'info.txt'}))
    model.responses.append(assistant(name=name, args=args))
    created = runtime.create({'message': 'Inspect the local file.'})
    runtime.run_once()
    def seed(data):
        row = data['tasks'][created['id']]
        call = row['queue'][0]
        flight = {**copy.deepcopy(call), 'kind': 'tool', 'dispatched': True, 'authorization': 'read' if kind == 'verified' else 'approved',
                  'effect': 'write' if kind in {'write', 'verified'} else 'read',
                  'goal_revision': row['goal']['revision'], 'control_revision': row['control_revision'],
                  'scope_revision': row['scope_revision'], 'context_epoch': row.get('context_epoch', 0),
                  'authority': model_retry_authority(data['settings'])}
        if kind == 'verified':
            flight['operation_classification'] = classify_operation(name, call['arguments'], runtime.tools.metadata(name),
                operation_id=call['operation_id'], task_id=row['id'], workspace_root=str(work),
                goal_revision=row['goal']['revision'], scope_revision=row['scope_revision'],
                context_epoch=row.get('context_epoch', 0), control_revision=row['control_revision'])
        row.update(status='running', in_flight=flight)
        verification.record(row, flight, 'intent', now(), flight=flight)
        return copy.deepcopy(flight)
    flight = runtime.store.transaction(seed)
    if kind == 'verified':
        assert verified_read_classification(flight, task_id=created['id'], workspace_root=str(work)), flight
        ledger = runtime.tools.local.ledger_factory()
        ledger.begin({'operation_id': flight['operation_id'], 'tool': name, 'task_id': created['id'],
                      'arguments_sha256': digest(flight['arguments']), 'local_execution': stored(runtime, created['id'])['local_execution']}, {'title': 'Read'})
        ledger.update(flight['operation_id'], 'dispatched', operation_classification=flight['operation_classification'])
    return created, flight


def pending(runtime, task_id, kind):
    def seed(data):
        row = data['tasks'][task_id]
        row['pending_control'] = {'kind': kind}
        if kind in {'steer', 'scope', 'permissions'}:
            row['control_revision'] += 1
            row['goal']['revision'] += 1
            row['pending_user_messages'] = [{'role': 'user', 'content': 'Owner correction after dispatch.'}]
        if kind in {'pause', 'cancel'}:
            row['status'] = 'paused' if kind == 'pause' else 'cancelled'
    runtime.store.transaction(seed)


def test_t1_model_wait_continues_once_per_goal_and_adds_hidden_note(tmp_path):
    runtime, model, _, key = make_runtime(tmp_path)
    row = runtime.create({'message': 'Continue my work.'})
    set_running(runtime, row['id'])
    restored = restart(runtime, key)
    current = stored(restored, row['id'])
    assert current['status'] == 'queued' and current['error'] is None
    assert current['restart_auto_continued'] == current['goal']['revision']
    notice = next(e for e in current['events'] if e['type'] == 'interrupted')
    assert notice['continued'] and notice['reason'] == 'node_restarted'
    notes = [m for m in current['messages'] if (m.get('ui') or {}).get('hidden')]
    assert len(notes) == 1 and '重复任何操作前先查状态' in notes[0]['content']
    set_running(restored, row['id'])
    again = restart(restored, key)
    assert again.get(row['id'])['status'] == 'interrupted'
    assert len([m for m in stored(again, row['id'])['messages'] if (m.get('ui') or {}).get('hidden')]) == 1
    assert '能自动续跑的任务已续跑' in diagnostics._ERROR_GUIDANCE['agent_node_restarted'][1]


@pytest.mark.parametrize('block', ['paused', 'cancelled', 'disabled', 'hard_budget', 'pending_input', 'pending_approval', 'old_interrupted'])
def test_t2_blocked_or_old_tasks_do_not_auto_continue(tmp_path, block):
    runtime, _, _, key = make_runtime(tmp_path)
    row = runtime.create({'message': 'Inspect.'})
    def seed(data):
        current = data['tasks'][row['id']]
        current['status'] = block if block in {'paused', 'cancelled'} else 'interrupted' if block == 'old_interrupted' else 'running'
        if block == 'disabled':
            data['settings']['enabled'] = False
        elif block == 'hard_budget':
            current['limits']['max_steps'] = 1
            current['limits_source']['max_steps'] = 'owner'
            current['steps'] = 1
        elif block in {'pending_input', 'pending_approval'}:
            current[block] = {'id': 'pending'}
    runtime.store.transaction(seed)
    after = restart(runtime, key).get(row['id'])
    assert after['status'] == (block if block in {'paused', 'cancelled'} else 'interrupted')
    assert not stored(runtime, row['id']).get('restart_auto_continued')


@pytest.mark.parametrize('kind', ['read', 'verified'])
def test_t3_dispatched_read_gets_one_paired_result_and_continues(workspace, kind):
    runtime, model, key, _ = workspace
    row, flight = flight_task(workspace, kind)
    restored = restart(runtime, key)
    saved = stored(restored, row['id'])
    assert saved['status'] == 'queued' and saved['in_flight'] is None
    results = [m for m in saved['messages'] if m.get('tool_call_id') == flight['call_id']]
    assert len(results) == 1
    result = json.loads(results[0]['content'])
    assert result['not_executed'] is False and result['no_mutation'] is True
    restored.reconcile()
    assert len([m for m in stored(restored, row['id'])['messages'] if m.get('tool_call_id') == flight['call_id']]) == 1
    model.responses.append(assistant('Recovered after the recorded read.'))
    restored.run_once()
    assert restored.get(row['id'])['status'] == 'completed'
    assert len(model.calls) == 2


def test_t3_cleanup_failure_keeps_flight_interrupted(workspace, monkeypatch):
    runtime, _, key, _ = workspace
    row, flight = flight_task(workspace, 'verified')
    monkeypatch.setattr(runtime.tools.local, 'recover_operation', lambda *a, **k: {'ok': False, 'still_running': True})
    after = stored(restart(runtime, key), row['id'])
    assert after['status'] == 'interrupted' and after['in_flight']['operation_id'] == flight['operation_id']
    assert not any(m.get('tool_call_id') == flight['call_id'] for m in stored(runtime, row['id'])['messages'])


def test_t6_verified_read_manual_reconciliation_keeps_pending_steer(workspace, monkeypatch):
    runtime, _, key, _ = workspace
    row, _ = flight_task(workspace, 'verified')
    pending(runtime, row['id'], 'steer')
    with monkeypatch.context() as failed_cleanup:
        failed_cleanup.setattr(runtime.tools.local, 'recover_operation', lambda *a, **k: {'ok': False, 'still_running': True})
        restored = restart(runtime, key)
    interrupted = restored.get(row['id'])
    assert interrupted['status'] == 'interrupted'
    restored.action(row['id'], {'action': 'reconcile', 'expected_revision': interrupted['revision']})
    saved = stored(restored, row['id'])
    assert saved['status'] == 'queued' and not saved.get('pending_user_messages')
    assert any(m.get('content') == 'Owner correction after dispatch.' for m in saved['messages'])
    assert 'restart_auto_continued' not in saved


@pytest.mark.parametrize('flight_kind', [None, 'read', 'verified'])
@pytest.mark.parametrize('control', ['steer', 'scope', 'permissions'])
def test_t5_t6_owner_pending_turn_is_merged_without_using_restart_allowance(workspace, flight_kind, control):
    runtime, _, key, _ = workspace
    if flight_kind:
        row, flight = flight_task(workspace, flight_kind)
    else:
        row = runtime.create({'message': 'Inspect.'})
        set_running(runtime, row['id'])
    pending(runtime, row['id'], control)
    restored = restart(runtime, key)
    saved = stored(restored, row['id'])
    assert saved['status'] == 'queued' and saved['error'] is None
    assert not saved.get('pending_user_messages') and not saved.get('pending_control')
    assert any(m.get('content') == 'Owner correction after dispatch.' for m in saved['messages'])
    assert 'restart_auto_continued' not in saved
    notice = next(e for e in reversed(saved['events']) if e['type'] == 'interrupted')
    assert notice['continued'] is True and notice['reason'] == 'owner_message_pending'


@pytest.mark.parametrize('kind', ['pause', 'cancel'])
@pytest.mark.parametrize('flight_kind', [None, 'read', 'verified'])
def test_t7_pending_pause_cancel_remain_stopped(workspace, kind, flight_kind):
    runtime, _, key, _ = workspace
    row, _ = flight_task(workspace, flight_kind) if flight_kind else (runtime.create({'message': 'Inspect.'}), None)
    pending(runtime, row['id'], kind)
    after = restart(runtime, key).get(row['id'])
    assert after['status'] == ('paused' if kind == 'pause' else 'cancelled')
    assert not stored(runtime, row['id']).get('restart_auto_continued')


def test_t8_unknown_write_does_not_use_read_or_owner_steer_recovery(workspace):
    runtime, _, key, work = workspace
    row, flight = flight_task(workspace, 'write')
    pending(runtime, row['id'], 'steer')
    after = stored(restart(runtime, key), row['id'])
    assert after['status'] == 'interrupted' and after['in_flight']['operation_id'] == flight['operation_id']
    assert (work / 'info.txt').read_text() == 'durable read\n'


def test_t2_automatic_write_keeps_s02_i9_acknowledgement_path(workspace):
    runtime, _, key, _ = workspace
    row, _ = flight_task(workspace, 'write')
    def automatic(data):
        current = data['tasks'][row['id']]
        current['permission_mode'] = 'automatic'
        current['in_flight']['authorization'] = 'automatic'
    runtime.store.transaction(automatic)
    after = stored(restart(runtime, key), row['id'])
    assert after['status'] == 'queued' and after['in_flight'] is None
    assert any(e['type'] == 'unknown_outcome_acknowledged' and e.get('auto_acknowledged') for e in after['events'])
    assert 'restart_auto_continued' not in stored(runtime, row['id'])


def test_t4_real_process_killed_while_waiting_for_model_resumes(tmp_path):
    script = Path(__file__).resolve()
    env = {**os.environ, 'PYTHONPATH': str(script.parent.parent)}
    child = subprocess.Popen([sys.executable, str(script), 'wait', str(tmp_path)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 25
        while not (tmp_path / 'ready.json').exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert (tmp_path / 'ready.json').exists(), child.communicate(timeout=2) if child.poll() is not None else 'model did not start'
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    result = subprocess.run([sys.executable, str(script), 'resume', str(tmp_path)], env=env,
                            capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    saved = json.loads((tmp_path / 'result.json').read_text(encoding='utf-8'))
    assert saved['status'] == 'completed' and saved['result'] == 'Recovered desktop work.'
    assert any(e['type'] == 'interrupted' and e.get('continued') for e in saved['events'])
    assert len([e for e in saved['events'] if e['type'] == 'model_started']) == 2


if __name__ == '__main__':
    phase, home = sys.argv[1], Path(sys.argv[2])
    key = peer_box.load_or_create_messaging_key(home / 'messaging.x25519')
    model = Model(assistant('Recovered desktop work.'))
    runtime = AgentRuntime(AgentStore(home / 'agent', key), model, Tools(), Catalog())
    if phase == 'wait':
        runtime.update_settings({'enabled': True, 'model_id': 'local:test'})
        row = runtime.create({'message': 'Continue desktop work.'})
        def blocked(*args, **kwargs):
            (home / 'ready.json').write_text(json.dumps({'id': row['id']}), encoding='utf-8')
            threading.Event().wait()
        model.complete = blocked
        runtime.run_once()
    else:
        runtime.reconcile()
        for _ in range(3):
            runtime.run_once()
        row = next(iter(runtime.store.read()['tasks'].values()))
        (home / 'result.json').write_text(json.dumps(row, ensure_ascii=False), encoding='utf-8')
