"""S03-I7: durable numeric usage and one context notice per threshold/window."""
import copy
import json
import math

from test_agent_context_compaction_policy import exchange, seed
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget, context_compaction
from rynmesh.agent_runtime.loop import AgentLoop


def usage(request=750, capacity=1000, density=2.5, epoch=0):
    return {'request_bytes': request, 'capacity_bytes': capacity,
            'bytes_per_token': density, 'step': 1, 'context_epoch': epoch}


def expected_notice(tokens, percent):
    return (f'Server context notice: about {tokens} tokens ({percent}%) of this context window remain. '
            'Finish the current step or record key findings in the plan or files; '
            'older turns will be summarized automatically.')


def test_t1_each_threshold_is_strict_and_emits_once_without_rearming_on_recovery():
    notice, level = context_compaction.remaining_notice(usage(700), 0)
    assert (notice, level) == (None, 0)
    notice, level = context_compaction.remaining_notice(usage(701), level)
    assert (notice, level) == (expected_notice(119, 29), 1)
    assert context_compaction.remaining_notice(usage(750), level) == (None, 1)
    assert context_compaction.remaining_notice(usage(850), level) == (None, 1)
    notice, level = context_compaction.remaining_notice(usage(851), level)
    assert (notice, level) == (expected_notice(59, 14), 2)
    assert context_compaction.remaining_notice(usage(999), level) == (None, 2)
    assert context_compaction.remaining_notice(usage(100), level) == (None, 2)
    assert context_compaction.remaining_notice(usage(900), level) == (None, 2)


def test_legacy_missing_usage_does_not_emit_context_notice():
    assert context_compaction.remaining_notice(None, 0) == (None, 0)
    assert AgentLoop._budget_notice({'steps': 0, 'limits': {'max_steps': 10}}) is None


def test_t2_installed_checkpoint_rearms_context_notice(tmp_path):
    runtime, _, _, _ = make_runtime(
        tmp_path, assistant('Goal: inspect A. Findings: A observed. Next: answer.'),
        assistant('A inspected; nothing changed.'))
    runtime.update_settings({'max_context_chars': 12000})
    created = runtime.create({'message': 'Inspect A; never change anything.'})
    seed(runtime, created['id'], exchange('a', '观测' * 1400))
    runtime.store.transaction(lambda data: data['tasks'][created['id']].update(
        context_notice_level=2, context_usage=usage(900)))
    advance(runtime)
    saved = runtime.store.read()['tasks'][created['id']]
    assert saved['context_checkpoint']['content']
    assert saved['context_notice_level'] == 0
    saved['context_usage'] = usage(900)
    assert runtime._budget_notice(saved) == expected_notice(40, 10)
    assert saved['context_notice_level'] == 2


def test_t2_changed_epoch_discards_old_usage_and_rearms_notice():
    row = {'context_epoch': 3, 'context_usage': usage(900, epoch=2),
           'context_notice_level': 2, 'steps': 0, 'limits': {'max_steps': 10}}
    assert AgentLoop._budget_notice(row) is None
    assert row['context_notice_level'] == 0
    row['context_usage'] = usage(750, epoch=3)
    assert AgentLoop._budget_notice(row) == expected_notice(100, 25)


def test_t3_context_and_step_budget_notices_are_one_newline_joined_note():
    row = {'steps': 6, 'limits': {'max_steps': 10}, 'goal': {'revision': 1},
           'context_usage': usage()}
    step_note = budget.notice(copy.deepcopy(row))
    assert step_note.startswith('Server budget notice: 6 of 10 model steps are used.')
    assert AgentLoop._budget_notice(row) == step_note + '\n' + expected_notice(100, 25)
    assert row['budget_notice_level'] == row['context_notice_level'] == 1
    assert AgentLoop._budget_notice(row) is None


def test_t4_real_dispatch_records_only_numbers_and_rearms_on_epoch(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    created = runtime.create({'message': 'Inspect fixture; private text must not enter numeric metrics.'})
    runtime.store.transaction(lambda data: data['tasks'][created['id']].update(
        context_epoch=3, context_notice_level=2, context_usage=usage(900, epoch=2)))
    captured = []
    def complete(model_id, messages, specs, cancelled, on_event):
        saved = runtime.store.read()['tasks'][created['id']]
        captured.append((saved, messages, specs))
        return assistant('Fixture inspected.')
    model.complete = complete
    advance(runtime)
    saved, messages, specs = captured[0]
    measured = saved.get('context_usage')
    assert isinstance(measured, dict), measured
    assert set(measured) == {'request_bytes', 'capacity_bytes', 'bytes_per_token', 'step', 'context_epoch'}
    assert all(type(value) in {int, float} and math.isfinite(value) for value in measured.values())
    assert measured['request_bytes'] == runtime._request_context_bytes(saved, messages, specs)
    assert measured['capacity_bytes'] == runtime._effective_capacity(saved, runtime._context_limit(saved))[0]
    assert measured['bytes_per_token'] == context_compaction.DEFAULT_BYTES_PER_TOKEN
    assert measured['step'] == saved['steps'] == 1 and measured['context_epoch'] == 3
    assert saved['context_notice_level'] == 0
    assert 'get_context_remaining' not in {item['function']['name'] for item in specs}


def test_real_tool_history_keeps_notice_and_subsequent_request_prefix(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    created = runtime.create({'message': 'Inspect both fixtures; never modify anything.'})
    requests = []
    measurements = []
    def complete(model_id, messages, specs, cancelled, on_event):
        requests.append(copy.deepcopy(messages))
        measurements.append(runtime.store.read()['tasks'][created['id']].get('context_usage'))
        if len(requests) <= 3:
            return assistant(name='inspect_service', args={'service_id': f'fixture-{len(requests)}'})
        return assistant('Both fixtures inspected.')
    model.complete = complete
    # Establish the real tool-menu projection before sizing a smaller window.
    advance(runtime, 2)
    row = runtime.store.read()['tasks'][created['id']]
    next_size = runtime._request_context_bytes(row, specs=runtime._specs(row))
    # The smaller window also reduces the directory schema projection.
    capacity = math.ceil(next_size / .90)
    def configure(data):
        current = data['tasks'][created['id']]
        current['limits']['max_context_chars'] = capacity
        current['context_budget_provenance'] = 'owner_manual'
    runtime.store.transaction(configure)
    for _ in range(8):
        advance(runtime)
        if runtime.get(created['id'])['status'] == 'completed':
            break
    saved = runtime.store.read()['tasks'][created['id']]
    notes = [json.loads(m['content']).get('server_note') for m in saved['messages'] if m['role'] == 'tool']
    context_notes = [note for note in notes if note and 'Server context notice:' in note]
    assert len(context_notes) == 1, (notes, capacity, next_size, measurements)
    first_low = measurements[1]
    remaining = (first_low['capacity_bytes'] - first_low['request_bytes']) / first_low['capacity_bytes']
    assert .15 <= remaining < .30
    assert len(requests) == 4 and len(tools.calls) == 3
    first_result = [m for m in requests[2] if m['role'] == 'tool'][-1]
    assert json.loads(first_result['content'])['server_note'] == context_notes[0]
    assert next(m for m in requests[3] if m.get('tool_call_id') == first_result['tool_call_id']) == first_result
    assert requests[3][0] == requests[2][0]
    assert not any('Server context notice:' in str(m.get('content')) for m in requests[3] if m['role'] != 'tool')
