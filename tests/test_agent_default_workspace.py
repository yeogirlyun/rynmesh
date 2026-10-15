from pathlib import Path

import pytest

from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentError
from rynmesh.agent_runtime.prompt import authority


def test_fresh_task_can_create_and_read_file_without_directory_setup(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({'message': 'Create an SVG'})
    root = Path(task['local_execution']['workspace_root'])
    assert root == runtime.store.root.resolve() / 'files'
    assert root.is_dir()
    assert runtime.settings()['workspace_root'] == ''
    context = runtime._execution_context(runtime.store.read()['tasks'][task['id']])
    tools = LocalTools(runtime.store, lambda: OperationLedger(tmp_path))
    args = {'path': 'drawing.svg', 'content': '<svg xmlns="http://www.w3.org/2000/svg"/>'}
    tools.execute('ryn_file_write', args, operation_id='default-output', context=context,
                  expected_preview=tools.preview('ryn_file_write', args, context), cancelled=lambda: False)
    assert tools.perform('ryn_file_read', {'path': 'drawing.svg'}, context, lambda: False)['text'] == args['content']
    assert 'in workspace_root with ryn_apply_patch' in authority(runtime.store.read()['tasks'][task['id']])
    assert tools.allowed(context)()
    for path in ('../sessions.json', '../../control_token'):
        with pytest.raises(AgentError, match='outside_workspace'):
            tools.path(path, context)
    tools.processes.close()


def test_custom_directory_preserved_and_clear_uses_app_folder_only_for_new_tasks(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    custom = tmp_path / 'chosen'; custom.mkdir()
    runtime.update_settings({'workspace_root': str(custom)})
    old = runtime.create({'message': 'Use my folder'})
    runtime.update_settings({'workspace_root': ''})
    new = runtime.create({'message': 'Create another file'})
    assert old['local_execution']['workspace_root'] == str(custom)
    assert new['local_execution']['workspace_root'] == str(runtime.store.root.resolve() / 'files')
    tools = LocalTools(runtime.store, None)
    with pytest.raises(AgentError, match='workspace_changed'):
        tools.path('.', runtime._execution_context(runtime.store.read()['tasks'][old['id']]))


def test_legacy_unbound_task_gets_app_folder_on_followup(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant('What next?'))
    task = runtime.create({'message': 'Hello'})
    advance(runtime)
    runtime.store.transaction(lambda data: data['tasks'][task['id']]['local_execution'].update(workspace_root=''))
    task = runtime.get(task['id'])
    continued = runtime.add_message(task['id'], {'message': 'Create a drawing', 'expected_revision': task['revision']})
    assert continued['local_execution']['workspace_root'] == str(runtime.store.root.resolve() / 'files')


def test_default_directory_cannot_be_redirected_to_another_directory(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    target = tmp_path / 'other'; target.mkdir()
    link = runtime.store.root / 'files'
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('Creating directory symlinks is unavailable')
    with pytest.raises(AgentError, match='invalid_workspace'):
        runtime.create({'message': 'Create a drawing'})
