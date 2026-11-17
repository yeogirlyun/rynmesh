"""S03-I23 running context exhaustion uses the real continuation transaction."""
import copy
import json
import uuid

import pytest
from test_agent_runtime import assistant, make_runtime
from test_agent_context_compaction_policy import WindowCatalog, exchange, seed, is_compaction_call

from rynmesh.agent_runtime import event_compaction
from rynmesh.agent_runtime.runtime import CONTINUATION_PREFIX, CONTINUATION_SEPARATOR, public_task
from rynmesh.agent_runtime.store import AgentError


def exhaust(tmp_path, *, mode='workspace', title='Work', goal='Finish the analysis', code='agent_context_limit'):
    runtime, model, tools, key = make_runtime(tmp_path)
    runtime.update_settings({'permission_mode': mode})
    created = runtime.create({'message': goal, 'title': title})
    def complete(*args, **kwargs):
        model.calls.append(args)
        raise AgentError(code, details={'request_bytes': 123456, 'capacity_bytes': 50000, 'private': 'DO_NOT_EXPORT'})
    model.complete = complete
    return runtime, model, created


def test_T1_s03_i23_real_compaction_failure_sets_flag_and_numeric_event(tmp_path):
    runtime, model, row = exhaust(tmp_path)
    runtime.catalog = WindowCatalog(32000, 4096)
    seed(runtime, row['id'], [m for i in range(12) for m in exchange(str(i), 'old observation '*800)])
    runtime.run_once()
    saved = runtime.get(row['id'])
    assert any(is_compaction_call(call[1], call[2]) for call in model.calls)
    assert saved['status'] == 'failed' and saved['error'] == 'agent_context_limit'
    assert saved['context_exhausted'] is True
    event = next(e for e in saved['events'] if e['type'] == 'context_exhausted')
    assert type(event['request_bytes']) is int and type(event['capacity_bytes']) is int
    assert event['request_bytes'] > 0 and event['capacity_bytes'] > 0
    assert 'DO_NOT_EXPORT' not in json.dumps(event)
    assert set(event) <= {'type','seq','at','context_epoch','run_elapsed_seconds','request_bytes','capacity_bytes'}
    assert saved['events'].index(event) < next(i for i,e in enumerate(saved['events']) if e['type'] == 'failed')
    assert public_task(runtime.store.read()['tasks'][row['id']], summary=True)['context_exhausted'] is True


def test_T2_s03_i23_owner_message_creates_summary_child_and_clears_flag(tmp_path):
    runtime, _, row = exhaust(tmp_path)
    runtime.run_once()
    failed = runtime.get(row['id'])
    child = runtime.add_message(row['id'], {'message': 'Continue with the next section.',
        'client_request_id': str(uuid.uuid4()), 'expected_control_revision': failed['control_revision']})
    assert child['id'] != row['id'] and child['continued_from'] == row['id']
    assert child['messages'][0]['content'].startswith(CONTINUATION_PREFIX)
    assert 'Finish the analysis' in child['messages'][0]['content']
    assert CONTINUATION_SEPARATOR+'Continue with the next section.' in child['messages'][0]['content']
    source = runtime.get(row['id'])
    assert source['context_exhausted'] is False and source['continued_to'] == child['id']
    assert source['status'] == 'failed' and source['error'] == 'agent_context_limit'
    assert runtime.store.read()['tasks'][row['id']].get('context_exhausted') is False


def test_T2_s03_i23_oversized_continuation_keeps_failed_source(tmp_path, monkeypatch):
    runtime, _, row = exhaust(tmp_path)
    runtime.run_once()
    monkeypatch.setattr(runtime, '_continuation_fits', lambda *args: False)
    with pytest.raises(AgentError, match='agent_context_limit'):
        runtime.add_message(row['id'], {'message': 'Too much to fit', 'expected_control_revision': runtime.get(row['id'])['control_revision']})
    assert len(runtime.store.read()['tasks']) == 1
    assert runtime.get(row['id'])['context_exhausted'] is True


def test_T2_s03_i23_child_commit_failure_does_not_clear_exhaustion(tmp_path, monkeypatch):
    runtime, _, row = exhaust(tmp_path)
    runtime.run_once()
    import rynmesh.agent_runtime.runtime as module
    original = module.memory_natural.stamp_task_owner_messages_in_tx
    def fail(*args):
        raise OSError('synthetic child commit failure')
    monkeypatch.setattr(module.memory_natural, 'stamp_task_owner_messages_in_tx', fail)
    with pytest.raises(OSError, match='synthetic child commit failure'):
        runtime.add_message(row['id'], {'message': 'Continue', 'expected_control_revision': runtime.get(row['id'])['control_revision']})
    source = runtime.get(row['id'])
    assert source['context_exhausted'] is True and not source.get('continued_to')
    assert len(runtime.store.read()['tasks']) == 1
    monkeypatch.setattr(module.memory_natural, 'stamp_task_owner_messages_in_tx', original)
    child = runtime.add_message(row['id'], {'message': 'Continue', 'expected_control_revision': runtime.get(row['id'])['control_revision']})
    assert child['continued_from'] == row['id']


def test_T3_s03_i23_automatic_real_worker_creates_once_and_replay_is_idempotent(tmp_path, monkeypatch):
    runtime, model, row = exhaust(tmp_path, mode='automatic', goal='Continue the Chinese analysis')
    runtime.store.transaction(lambda data: data['tasks'][row['id']]['goal'].update(text='未完成任务。'*1800))
    original = runtime._continue_in_new_task
    calls = []
    def continue_spy(tid, value, content):
        calls.append((tid, copy.deepcopy(value), content))
        return original(tid, value, content)
    monkeypatch.setattr(runtime, '_continue_in_new_task', continue_spy)
    runtime.run_once()
    source = runtime.get(row['id'])
    assert source['continued_to'] and source['context_exhausted'] is False
    child = runtime.get(source['continued_to'])
    assert child['status'] == 'queued' and child['permission_mode'] == 'automatic'
    assert child['local_execution'] == source['local_execution']
    assert child['budget']['lifetime']['charged_model_calls'] == source['budget']['lifetime']['charged_model_calls']
    assert child['title'] == 'Work（续）' and child['continued_from'] == row['id']
    tid, request, content = calls[0]
    assert content.startswith('Continue the unfinished work from the previous conversation.')
    assert len(content.encode()) <= 4000 and '未完成任务' in content
    uuid.UUID(request['client_request_id'])
    assert child['client_request_id'] == request['client_request_id']
    replay = runtime.add_message(tid, request)
    assert replay['id'] == child['id'] and len(runtime.store.read()['tasks']) == 2
    def done(*args, **kwargs): return assistant('Continuing the original work.')
    model.complete = done
    runtime.run_once()
    assert runtime.get(child['id'])['status'] == 'completed'


@pytest.mark.parametrize('title', ['Work（续 5）', 'Work（续 9）', 'Work（续）（续 4）（续）'])
def test_T3_s03_i23_automatic_depth_five_stops(tmp_path, title):
    runtime, _, row = exhaust(tmp_path, mode='automatic', title=title)
    runtime.run_once()
    saved = runtime.get(row['id'])
    assert saved['status'] == 'failed' and saved['context_exhausted'] is True
    assert not saved.get('continued_to') and len(runtime.store.read()['tasks']) == 1
    assert event_compaction.continuation_depth(title) >= 5


def test_T3_s03_i23_existing_active_quota_keeps_failed_task(tmp_path, monkeypatch):
    runtime, _, row = exhaust(tmp_path, mode='automatic')
    original = runtime._continue_in_new_task
    def fill_quota(tid, value, content):
        def insert(data):
            for i in range(16):
                key = 'agent_'+uuid.uuid4().hex
                other = copy.deepcopy(data['tasks'][tid])
                other.update(id=key, status='queued', context_exhausted=False, error=None)
                data['tasks'][key] = other
        runtime.store.transaction(insert)
        return original(tid, value, content)
    monkeypatch.setattr(runtime, '_continue_in_new_task', fill_quota)
    runtime.run_once()
    source = runtime.get(row['id'])
    assert source['status'] == 'failed' and source['context_exhausted'] is True
    assert not source.get('continued_to') and len(runtime.store.read()['tasks']) == 17


@pytest.mark.parametrize('code', ['agent_model_unavailable', 'agent_operation_failed', 'agent_token_limit'])
def test_T4_s03_i23_other_worker_errors_do_not_mark_exhaustion(tmp_path, code):
    runtime, _, row = exhaust(tmp_path, code=code)
    runtime.run_once()
    saved = runtime.get(row['id'])
    assert not saved.get('context_exhausted')
    assert not any(e['type'] == 'context_exhausted' for e in saved['events'])
    assert len(runtime.store.read()['tasks']) == 1


def test_T5_s03_i23_owner_entry_rejection_never_marks_exhaustion(tmp_path, monkeypatch):
    runtime, _, row = exhaust(tmp_path)
    menu = runtime._menu
    def too_large(prospective, *args, **kwargs):
        result, size = menu(prospective, *args, **kwargs)
        return result, size+10_000_000
    monkeypatch.setattr(runtime, '_menu', too_large)
    monkeypatch.setattr(runtime, '_continuation_fits', lambda *args: False)
    with pytest.raises(AgentError, match='agent_context_limit'):
        runtime.add_message(row['id'], {'message': 'New owner message', 'intent': 'information',
            'expected_control_revision': row['control_revision'], 'expected_goal_revision': row['goal']['revision']})
    saved = runtime.get(row['id'])
    assert saved['status'] == 'queued' and not saved.get('context_exhausted')
    assert not any(e['type'] == 'context_exhausted' for e in saved['events'])


def test_T4_s03_i23_legacy_public_tasks_default_false(tmp_path):
    runtime, _, row = exhaust(tmp_path)
    raw = runtime.store.read()['tasks'][row['id']]
    assert 'context_exhausted' not in raw
    assert public_task(raw)['context_exhausted'] is False


def test_T2_s03_i23_stale_owner_control_does_not_create_child(tmp_path):
    runtime, _, row = exhaust(tmp_path)
    runtime.run_once()
    failed = runtime.get(row['id'])
    with pytest.raises(AgentError, match='agent_control_revision_conflict'):
        runtime.add_message(row['id'], {'message': 'Continue', 'intent': 'information',
            'expected_control_revision': failed['control_revision']-1,
            'expected_goal_revision': failed['goal']['revision']})
    assert len(runtime.store.read()['tasks']) == 1
    assert runtime.get(row['id'])['context_exhausted'] is True


def test_T3_s03_i23_depth_four_can_create_the_fifth_continuation(tmp_path):
    runtime, _, row = exhaust(tmp_path, mode='automatic', title='Work（续 4）')
    runtime.run_once()
    source = runtime.get(row['id'])
    assert source['context_exhausted'] is False
    assert runtime.get(source['continued_to'])['title'] == 'Work（续 5）'


def test_T3_s03_i23_safe_value_error_context_code_also_auto_continues(tmp_path):
    runtime, model, row = exhaust(tmp_path, mode='automatic')
    def complete(*args, **kwargs):
        raise ValueError('agent_context_limit')
    model.complete = complete
    runtime.run_once()
    source = runtime.get(row['id'])
    assert source['error'] == 'agent_context_limit'
    assert source.get('continued_to') and source['context_exhausted'] is False
    assert len(runtime.store.read()['tasks']) == 2


def test_T1_s03_i23_completed_compaction_then_new_observation_still_exhausts_window(tmp_path):
    runtime, model, row = exhaust(tmp_path)
    runtime.catalog = WindowCatalog(32000, 4096)
    seed(runtime, row['id'], [m for i in range(12) for m in exchange(str(i), 'old observation '*800)])
    def summarize(model_id, messages, specs, cancelled, on_event, **kwargs):
        model.calls.append((model_id, messages, specs))
        assert is_compaction_call(messages, specs)
        # A new closed observation arrives while the older prefix is summarized.
        seed(runtime, row['id'], exchange('late-observation', 'late result '*12000))
        return assistant('Goal: Finish the analysis. Earlier observations were read; continue analyzing.')
    model.complete = summarize
    runtime.run_once()
    saved = runtime.get(row['id'])
    assert any(e['type'] == 'context_compacted' for e in saved['events'])
    assert saved['status'] == 'failed' and saved['context_exhausted'] is True
    event = next(e for e in saved['events'] if e['type'] == 'context_exhausted')
    assert event['request_bytes'] > event['capacity_bytes']
    assert len(model.calls) == 1  # The over-window main request never dispatches.
