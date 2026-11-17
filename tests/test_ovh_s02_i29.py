"""S02-I29: repaired fork history and bounded forks through installed routes."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_agent_task_forks import append, assistant, choices, env as env, fork, instruct, parent, request, stored
from rynmesh.agent_runtime import diagnostics, results, task_forks
from rynmesh.agent_runtime.store import encoded
from rynmesh.agent_runtime.runtime import CONTINUATION_PREFIX


def assert_closed(messages):
    pending, seen = {}, set()
    for message in messages:
        if message['role'] == 'tool':
            call_id = message['tool_call_id']
            assert call_id in pending
            assert message.get('context_epoch', 0) == pending.pop(call_id)
        else:
            assert not pending and message['role'] in {'user', 'assistant', 'system'}
            for call in message.get('tool_calls', []):
                assert call['id'] and call['id'] not in seen
                seen.add(call['id'])
                pending[call['id']] = message.get('context_epoch', 0)
    assert not pending


@pytest.mark.parametrize('damage', ['orphan', 'missing', 'duplicate_batch', 'duplicate_history', 'unknown_role', 'wrong_epoch'])
def test_t1_bad_exchange_does_not_hide_later_boundaries_and_child_history_is_repaired(env, damage):
    tid = parent(env)
    call = assistant(name='inspect_service', call_id='history-call')['message']
    output = {'role': 'tool', 'tool_call_id': 'history-call', 'content': '{"ok":true}'}
    if damage == 'orphan':
        broken = [{'role': 'tool', 'tool_call_id': 'orphan', 'content': 'ORPHAN_CANARY'}]
        invalid_offset = 0
    elif damage == 'missing':
        broken = [call, {'role': 'assistant', 'content': 'Reply after a lost output'}]
        invalid_offset = 1
    elif damage == 'duplicate_batch':
        call['tool_calls'].append(copy.deepcopy(call['tool_calls'][0]))
        broken = [call, output, copy.deepcopy(output)]
        invalid_offset = 0
    elif damage == 'duplicate_history':
        broken = [call, output, copy.deepcopy(call), copy.deepcopy(output)]
        invalid_offset = 2
    elif damage == 'unknown_role':
        broken = [{'role': 'unknown', 'content': 'UNKNOWN_ROLE_CANARY'}]
        invalid_offset = 0
    else:
        call['context_epoch'] = 7
        output['context_epoch'] = 8
        broken = [call, output]
        invalid_offset = 1
    start = len(stored(env, tid)['messages'])
    append(env, tid, *broken, {'role': 'user', 'content': 'Healthy later question'},
           {'role': 'assistant', 'content': 'Healthy later answer'})
    before = stored(env, tid)
    counts = {boundary['message_count'] for boundary in choices(env, tid)}
    assert {len(before['messages']) - 1, len(before['messages'])} <= counts
    assert start + invalid_offset + 1 not in counts
    child = fork(env, tid)
    history = stored(env, child['id'])['messages']
    assert_closed(history)
    assert 'ORPHAN_CANARY' not in json.dumps(history)
    assert 'UNKNOWN_ROLE_CANARY' not in json.dumps(history)
    if damage in {'duplicate_batch', 'duplicate_history'}:
        calls = [call for message in history for call in message.get('tool_calls', [])]
        assert len(calls) == 1 and calls[0]['id'] == 'history-call'
    if damage in {'missing', 'wrong_epoch'}:
        missing = [m for m in history if m.get('is_error')]
        assert len(missing) == 1
        assert json.loads(missing[0]['content']) == {'error': 'agent_tool_result_missing', 'not_executed': False}
        assert missing[0].get('context_epoch', 0) == call.get('context_epoch', 0)
        if damage == 'missing':
            assert 'agent_tool_result_missing' in json.dumps(env.runtime._context(stored(env, child['id'])))
    assert stored(env, tid) == before
    assert env.runtime.tools.calls == [] and env.model.calls == []


def test_t1_valid_boundary_tokens_keep_the_existing_full_prefix_hash_chain(env):
    tid = parent(env)
    first = request(env, tid)
    append(env, tid, {'role': 'tool', 'tool_call_id': 'orphan', 'content': 'broken'},
           {'role': 'user', 'content': 'Recovered'}, {'role': 'assistant', 'content': 'Answer'})
    row = stored(env, tid)
    fingerprint = hashlib.sha256(encoded({key: row.get(key) for key in (
        'id', 'model_id', 'model_destination', 'context_epoch', 'conversation_revision',
        'scope_revision', 'scope', 'project', 'local_execution')}))
    expected = {}
    for index, message in enumerate(row['messages']):
        fingerprint.update(encoded(message))
        expected[index + 1] = fingerprint.hexdigest()
    current = choices(env, tid)
    assert current[-1]['message_count'] == len(row['messages'])
    assert all(boundary['token'] == expected[boundary['message_count']] for boundary in current)
    assert fork(env, tid, first)['fork']['message_count'] == first['message_count']


def large_history(env, tid):
    for number in range(4):
        append(env, tid, {'role': 'user', 'content': f'Old question {number}'},
            {'role': 'assistant', 'content': 'x' * 300_000 + f'OMITTED_RAW_CANARY_{number}' + 'y' * 300_000})


def test_t2_large_fork_compacts_before_copy_and_real_request_omits_early_raw_history(env):
    tid = parent(env)
    large_history(env, tid)
    append(env, tid, {'role': 'user', 'content': 'Latest small request'},
        {'role': 'assistant', 'content': 'Latest small answer'},
        context_checkpoint={'through': 1, 'content': 'Earlier safe checkpoint', 'references': {}})
    before = stored(env, tid)
    assert len(encoded(before)) > 2 * 1024 * 1024
    child = fork(env, tid)
    row = stored(env, child['id'])
    assert row['messages'][0]['role'] == 'user'
    assert row['messages'][0]['content'].startswith(CONTINUATION_PREFIX)
    assert len(encoded(row)) <= 2 * 1024 * 1024
    assert len(encoded(row['messages'][1:])) <= 512 * 1024
    assert row['messages'][1]['role'] == 'user'
    assert row['fork']['compacted'] is True and row['fork']['omitted_messages'] > 0
    assert 'context_checkpoint' not in row
    assert_closed(row['messages'])
    assert stored(env, tid) == before
    env.model.responses.append(assistant('Fork continued from the summary'))
    instruct(env, child['id'], 'Continue the compacted fork')
    for _ in range(5):
        env.runtime.run_once()
        if env.runtime.get(child['id'])['status'] in {'completed', 'failed'}:
            break
    assert env.runtime.get(child['id'])['status'] == 'completed'
    model_messages = env.model.calls[0][1]
    transmitted = json.dumps(model_messages, ensure_ascii=False)
    assert any(m['role'] == 'user' and str(m.get('content', '')).startswith(CONTINUATION_PREFIX) for m in model_messages)
    assert 'Latest small answer' in transmitted
    assert all(f'OMITTED_RAW_CANARY_{n}' not in transmitted for n in range(4))
    assert env.runtime.tools.calls == []


def test_t2_compact_summary_uses_only_selected_prefix_and_keeps_activity_and_read_coverage(tmp_path):
    call = assistant(name='ryn_file_read', call_id='read-prefix', args={'path': 'notes.txt', 'start_line': 1})['message']
    content = {'path': str(tmp_path / 'notes.txt'), 'start_line': 1, 'end_line': 20, 'total_lines': 40,
               'sha256': 'a' * 64}
    messages = [{'role': 'user', 'content': 'Selected goal'}, call,
                {'role': 'tool', 'tool_call_id': 'read-prefix', 'content': json.dumps(content)},
                {'role': 'user', 'content': 'FUTURE_OWNER_CANARY'}]
    source = {'id': 'source', 'messages': messages, 'context_epoch': 0,
        'goal': {'text': 'FUTURE_GOAL_CANARY'}, 'project': {'path': str(tmp_path)},
        'context_checkpoint': {'through': 4, 'content': 'FUTURE_CHECKPOINT_CANARY'},
        'events': [{'type': 'tool_result', 'tool': 'ryn_file_read', 'call_id': 'read-prefix', 'result': content},
                   {'type': 'tool_result', 'tool': 'ryn_file_read', 'call_id': 'read-prefix',
                    'result': {**content, 'path': str(tmp_path / 'FUTURE_FILE_CANARY')}}]}
    compacted, omitted = task_forks._compact_prefix(source, 3)
    summary = compacted[0]['content']
    assert summary.startswith(CONTINUATION_PREFIX)
    assert 'Server-recorded tool activity' in summary and 'ryn_file_read' in summary
    assert 'Server-recorded file read coverage' in summary and 'notes.txt' in summary
    assert '[[1,20]]' in summary and 'a' * 64 in summary
    assert 'FUTURE_' not in summary and omitted >= 0


def test_t3_fork_still_too_large_after_summary_reports_fork_specific_code_without_child(env):
    tid = parent(env)
    append(env, tid, base_instructions='Required configuration ' + 'x' * (2 * 1024 * 1024))
    before = stored(env, tid)
    response = env.client.post(f'/api/local/agent/tasks/{tid}/fork', json=request(env, tid))
    assert response.status_code == 409
    assert response.json()['detail'] == 'agent_fork_too_large'
    assert set(env.runtime.store.read()['tasks']) == {tid} and stored(env, tid) == before


def test_t4_only_retained_attachments_and_results_are_copied(env):
    tid = parent(env, attachments=[
        {'name': 'old.txt', 'data_base64': base64.b64encode(b'omitted bytes').decode()},
        {'name': 'kept.txt', 'data_base64': base64.b64encode(b'kept bytes').decode()}])
    original = stored(env, tid)
    old_attachment, kept_attachment = copy.deepcopy(original['messages'][0]['attachments'])
    env.runtime.store.transaction(lambda data: data['tasks'][tid]['messages'][0].update(attachments=[old_attachment]))
    old_ref = results.save(env.runtime.store, original, {'name': 'inspect_service', 'operation_id': 'old'}, {'text': 'old result'})
    kept_ref = results.save(env.runtime.store, original, {'name': 'inspect_service', 'operation_id': 'kept'}, {'text': 'kept result'})
    append(env, tid, assistant(name='inspect_service', call_id='old-result')['message'],
        {'role': 'tool', 'tool_call_id': 'old-result', 'content': json.dumps({'result_ref': old_ref['id']})})
    large_history(env, tid)
    append(env, tid, {'role': 'user', 'content': 'Retain attachment ' + kept_attachment['path'], 'attachments': [kept_attachment]},
        assistant(name='inspect_service', call_id='kept-result')['message'],
        {'role': 'tool', 'tool_call_id': 'kept-result', 'content': json.dumps({'result_ref': kept_ref['id']})},
        result_refs=[old_ref, kept_ref])
    child = fork(env, tid)
    row = stored(env, child['id'])
    assert row['fork']['compacted'] is True
    assert {item['id'] for item in row['result_refs']} == {kept_ref['id']}
    assert not results.path(env.runtime.store, child['id'], old_ref['id']).exists()
    assert results.read(env.runtime.store, row, kept_ref['id'])[0] == {'text': 'kept result'}
    attachments = [a for message in row['messages'] for a in message.get('attachments', [])]
    assert len(attachments) == 1 and Path(attachments[0]['path']).read_bytes() == b'kept bytes'
    old_target = env.workspace / '.ryn' / 'attachments' / child['id'] / Path(old_attachment['path']).name
    assert not old_target.exists()


def test_t4_reference_metadata_crossing_the_limit_also_compacts_before_publication(env, monkeypatch):
    tid = parent(env)
    source = stored(env, tid)
    refs = [results.save(env.runtime.store, source, {'name': 'inspect_service', 'operation_id': f'ref-{n}'},
                        {'text': f'Observed result {n}'}) for n in range(64)]
    append(env, tid, assistant(name='inspect_service', call_id='many-refs')['message'],
        {'role': 'tool', 'tool_call_id': 'many-refs', 'content': json.dumps({'items': [{'result_ref': r['id']} for r in refs]})},
        {'role': 'assistant', 'content': 'Padding'},
        {'role': 'user', 'content': 'Recent small question'}, {'role': 'assistant', 'content': 'Recent answer'},
        result_refs=refs)
    pilot = stored(env, fork(env, tid)['id'])
    padding = 2 * 1024 * 1024 - len(encoded(pilot)) + 2000
    env.runtime.store.transaction(lambda data: data['tasks'][tid]['messages'][3].update(content='x' * padding))
    observed = []
    original = task_forks._copy_references
    def record(runtime, source, child, written):
        observed.append((len(encoded(child)), child['fork'].get('compacted', False)))
        return original(runtime, source, child, written)
    monkeypatch.setattr(task_forks, '_copy_references', record)
    child = fork(env, tid)
    row = stored(env, child['id'])
    assert observed[0][0] <= 2 * 1024 * 1024 and observed[0][1] is False
    assert row['fork']['compacted'] is True and row['result_refs'] == []
    assert len(encoded(row)) <= 2 * 1024 * 1024
    assert all(not results.path(env.runtime.store, child['id'], r['id']).exists() for r in refs)


def test_t5_existing_diagnostics_entry_identifies_fork_length_instead_of_task_capacity():
    action, text, target = diagnostics._ERROR_GUIDANCE['agent_fork_too_large']
    assert action == 'fork_later_turn' and '无法分叉' in text and target == '/agent'
