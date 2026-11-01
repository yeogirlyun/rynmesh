"""Independent failing acceptance criteria, synthetic usage; never real-model evidence."""
import copy

import pytest
from test_agent_context_compaction_policy import WindowCatalog
from test_agent_runtime import assistant, make_runtime
from rynmesh.agent_runtime import context_compaction, tool_menu
from rynmesh.agent_runtime.store import AgentError, encoded


def calibrated(runtime, task_id):
    row = runtime.store.read()['tasks'][task_id]
    row['budget']['records'].append({'kind': 'model', 'stage': 'model', 'status': 'settled',
        'input_tokens': 10000, 'output_policy': {'projected_context_utf8_bytes': 18000}})
    return row


@pytest.mark.parametrize('nested,stage', [(True, 'image_inspection'), (False, 'verification_model')])
def test_auxiliary_request_rejects_above_calibrated_capacity(tmp_path, monkeypatch, nested, stage):
    runtime, model, _, _ = make_runtime(tmp_path, assistant('OK'))
    runtime.catalog = WindowCatalog(32000, 4096)
    task = runtime.create({'message': 'Chinese capacity boundary'})
    row = calibrated(runtime, task['id'])
    capacity, _ = runtime._effective_capacity(row)
    messages = [{'role': 'user', 'content': '测' * (int(capacity) // 3 + 200)}]
    size = len(encoded(messages)) + len(encoded([]))
    assert capacity < size < runtime._context_limit(row)
    # Isolate shared request admission from accounting; spy at exact model dispatch boundary.
    monkeypatch.setattr(runtime, '_meter_model', lambda row, invoke, **kw: invoke(4096))
    with pytest.raises(AgentError, match='agent_context_limit'):
        runtime._complete(row, messages, [], lambda: False, lambda _: None, nested=nested, stage=stage)
    assert not model.calls


def test_menu_projection_uses_calibrated_capacity(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.catalog = WindowCatalog(32000, 4096)
    task = runtime.create({'message': 'Chinese menu boundary'})
    row = calibrated(runtime, task['id'])
    observed = []
    original = tool_menu.project
    def traced(*args, **kwargs):
        observed.append(kwargs['max_context_bytes'])
        return original(*args, **kwargs)
    monkeypatch.setattr(tool_menu, 'project', traced)
    runtime._menu(row)
    assert observed == [runtime._effective_capacity(row)[0]]


def test_density_floor_is_explicit_estimate_not_token_guarantee():
    policy = {'context_window_known': True, 'context_window_tokens': 32000,
              'reserved_output_tokens': 4096, 'framing_reserve_tokens': 512}
    tokens = 32000 - 4096 - 512
    capacity = context_compaction.input_capacity(tokens * 3, policy, 1.1)
    assert capacity == int(tokens * 1.5)
    assert capacity > tokens * 1.1  # Documented clamp cannot guarantee actual input-token fit.
