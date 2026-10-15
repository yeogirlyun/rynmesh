import copy
import json
from test_agent_space_binding import space_pair, nodes
from rynmesh.llm_package.peer_binding import expected, revision
from rynmesh.agent_runtime.prompt import authority
from test_agent_runtime import make_runtime, assistant, advance
from test_agent_tool_menu import ManyTools
from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.store import encoded


def test_wrapped_inference_fault_records_code_location_without_exception_text(tmp_path):
    from rynmesh.agent_runtime.store import AgentError
    runtime, model, _, _ = make_runtime(tmp_path)
    def fail(*args, **kwargs):
        try:
            raise RuntimeError('PRIVATE_UPSTREAM_PAYLOAD')
        except RuntimeError:
            raise AgentError('agent_inference_failed') from None
    model.complete = fail
    task = runtime.create({'message': 'Check'})
    advance(runtime)
    saved = runtime.get(task['id'])
    fault = next(e for e in saved['events'] if e['type'] == 'runtime_fault')
    assert fault['exception_type'] == 'RuntimeError'
    assert fault['frames'][-1]['function'] == 'fail'
    assert 'PRIVATE_UPSTREAM_PAYLOAD' not in json.dumps(saved)


def test_run_time_limit_can_be_adjusted_without_resuming_or_erasing_usage(tmp_path):
    import uuid
    import pytest
    from rynmesh.agent_runtime.store import AgentError
    runtime, _, _, _ = make_runtime(tmp_path, assistant('Done'))
    task = runtime.create({'message': 'Work'})
    body = {'limits': {}, 'run_max_seconds': 900, 'expected_control_revision': task['control_revision'], 'client_request_id': str(uuid.uuid4())}
    with pytest.raises(AgentError, match='invalid_transition'):
        runtime.update_budget(task['id'], body)
    advance(runtime)
    done = runtime.get(task['id'])
    body['expected_control_revision'] = done['control_revision']
    changed = runtime.update_budget(task['id'], body)
    assert changed['status'] == 'completed'
    assert changed['limits']['max_seconds'] == 900
    assert changed['budget']['lifetime'] == done['budget']['lifetime']
    assert runtime.update_budget(task['id'], body)['control_revision'] == changed['control_revision']
    body.update(run_max_seconds=True, client_request_id=str(uuid.uuid4()))
    with pytest.raises(AgentError, match='invalid_budget'):
        runtime.update_budget(task['id'], body)


def test_lease_renewal_and_name_sync_preserve_bound_model(space_pair):
    p = space_pair
    old = p.consumer.refresh(p.locator)[0]
    prior = revision(old, p.child.store.peer_id)
    # Routine renewal and display name changes both re-sign the snapshot.
    snapshot = copy.deepcopy(p.root._snapshot())
    snapshot['members'][p.child.store.peer_id]['name'] = 'Renamed laptop'
    p.root._sign(snapshot)
    p.child._accept_snapshot(p.root.data['snapshot'], p.root.status()['space'])
    assert p.consumer.records()
    fresh = p.consumer.refresh(p.locator)[0]
    assert revision(fresh, p.child.store.peer_id) == prior
    p.root.authorize_model(p.child.store.peer_id, expected(old, p.child.store.peer_id)['space_permission'])
    # Real role revocation changes the access revision even if later restored.
    p.root.act('role', {'peer_id': p.child.store.peer_id, 'role': 'manager'})
    p.root.act('role', {'peer_id': p.child.store.peer_id, 'role': 'device'})
    p.child._accept_snapshot(p.root.data['snapshot'], p.root.status()['space'])
    assert not p.consumer.records()
    fresh = p.consumer.refresh(p.locator)[0]
    assert revision(fresh, p.child.store.peer_id) != prior


def test_large_window_does_not_send_entire_tool_registry(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant('你好。'))
    runtime.tools = ManyTools()
    task = runtime.create({'message': 'i', 'response_language': 'zh-CN'})
    advance(runtime)
    specs = model.calls[0][2]
    assert tool_menu.NAME in {s['function']['name'] for s in specs}
    assert len(encoded(specs)) < len(encoded(runtime.tools.specs()))
    saved = runtime.store.read()['tasks'][task['id']]
    assert saved['response_language'] == 'zh-CN'
    assert 'zh-CN' in authority(saved)
    assert runtime.get(task['id'])['status'] == 'completed'
    assert not runtime.tools.calls


def test_chat_answer_skips_extra_model_but_never_claims_verification(tmp_path):
    from test_agent_runtime_verification import VerifyingTools
    runtime, model, _, _ = make_runtime(tmp_path, assistant('你好。'))
    runtime.tools = VerifyingTools()
    task = runtime.create({'message': 'i'})
    advance(runtime, 3)
    done = runtime.get(task['id'])
    assert done['status'] == 'completed'
    assert done['outcome']['status'] == 'unverified'
    assert len(model.calls) == 1


def test_workspace_is_an_explicit_owner_grant_and_replay_safe(tmp_path):
    import uuid
    import pytest
    from rynmesh.agent_runtime.store import AgentError
    from rynmesh.agent_runtime.local_tools import LocalTools
    runtime, _, _, _ = make_runtime(tmp_path, assistant('等待工作目录。'))
    created = runtime.create({'message': 'Create a drawing'})
    advance(runtime)
    # Legacy tasks created before Ryn supplied its own output folder.
    runtime.store.transaction(lambda data: data['tasks'][created['id']]['local_execution'].update(workspace_root=''))
    workspace = tmp_path / 'drawings'; workspace.mkdir()
    value = {'workspace_root': str(workspace), 'expected_control_revision': runtime.get(created['id'])['control_revision'], 'client_request_id': str(uuid.uuid4())}
    # Merely returning a directory in prose grants nothing.
    assert runtime.get(created['id'])['local_execution']['workspace_root'] == ''
    bound = runtime.bind_workspace(created['id'], value)
    assert bound['status'] == 'queued'
    assert bound['local_execution']['workspace_root'] == str(workspace.resolve())
    assert bound['permission_mode'] == 'workspace'
    assert runtime.bind_workspace(created['id'], value)['control_revision'] == bound['control_revision']
    assert len([e for e in runtime.get(created['id'])['events'] if e['type'] == 'workspace_bound']) == 1
    tools = LocalTools(runtime.store, None)
    context = runtime._execution_context(runtime.store.read()['tasks'][created['id']])
    assert tools.path('.', context) == workspace.resolve()
    with pytest.raises(AgentError):
        tools.path('..', context)


def test_workspace_cannot_change_under_running_work_or_expand_an_existing_grant(tmp_path):
    import uuid
    import pytest
    from rynmesh.agent_runtime.store import AgentError
    runtime, _, _, _ = make_runtime(tmp_path, assistant('Done'))
    task = runtime.create({'message': 'Work'})
    runtime.store.transaction(lambda data: data['tasks'][task['id']]['local_execution'].update(workspace_root=''))
    body = {'workspace_root': str(tmp_path), 'expected_control_revision': task['control_revision'], 'client_request_id': str(uuid.uuid4())}
    with pytest.raises(AgentError, match='invalid_transition'):
        runtime.bind_workspace(task['id'], body)
    advance(runtime)
    other = tmp_path / 'existing'; other.mkdir()
    runtime.update_settings({'workspace_root': str(other)})
    body['expected_control_revision'] = runtime.get(task['id'])['control_revision']
    with pytest.raises(AgentError, match='workspace_changed'):
        runtime.bind_workspace(task['id'], body)
