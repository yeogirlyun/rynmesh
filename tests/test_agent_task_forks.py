"""Installed API, actual encrypted storage/files and deterministic runtime; no paid model."""
import base64
import copy
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_agent_file_changes import environment
from test_agent_local_tools import execute
from test_agent_runtime import assistant
from rynmesh.agent_runtime import results, task_forks
from rynmesh.agent_runtime.store import AgentError


@pytest.fixture
def env(tmp_path):
    app, runtime, local, workspace, model = environment(tmp_path)
    client = TestClient(app, headers={"x-test-owner": "yes"})
    e = SimpleNamespace(app=app, runtime=runtime, local=local, workspace=workspace, model=model, client=client, base=tmp_path)
    yield e
    local.processes.close()
    client.close()


def parent(e, **kwargs):
    response = e.client.post('/api/local/agent/tasks', json={"message": "Original goal", **kwargs})
    assert response.status_code == 200, response.text
    row = response.json()
    e.runtime.store.transaction(lambda d: d['tasks'][row['id']].update(status='paused'))
    return row['id']


def choices(e, tid):
    r = e.client.get(f'/api/local/agent/tasks/{tid}/fork-boundaries')
    assert r.status_code == 200, r.text
    return r.json()['boundaries']


def request(e, tid, index=-1):
    b = choices(e, tid)[index]
    return dict(client_request_id=str(uuid.uuid4()), title='Alternative plan', message_count=b['message_count'], token=b['token'], shared_workspace_confirmed=True)


def fork(e, tid, value=None):
    response = e.client.post(f'/api/local/agent/tasks/{tid}/fork', json=value or request(e, tid))
    assert response.status_code == 200, response.text
    return response.json()


def stored(e, tid):
    return e.runtime.store.read()['tasks'][tid]


def append(e, tid, *messages, **fields):
    def update(data):
        row = data['tasks'][tid]
        row['messages'].extend(messages)
        row.update(fields)
    e.runtime.store.transaction(update)


def instruct(e, tid, text):
    row = e.runtime.get(tid)
    r = e.client.post(f'/api/local/agent/tasks/{tid}/messages', json={'message': text, 'expected_revision': row['revision'], 'client_request_id': str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    return r.json()


def test_history_pairing_no_replay_parent_unchanged_and_independent_runs(env):
    e = env
    tid = parent(e)
    call = assistant(name='share_service', args={'service_id': 'historical'}, call_id='done')['message']
    append(e, tid, call, {'role': 'tool', 'tool_call_id': 'done', 'content': '{"shared":true}'},
           {'role':'user','content':'Correction: keep the original intact'},
           {'role':'assistant','content':'Progress: checked the original'},
           queue=[{'call_id':'pending'}], approved={'one_shot':True}, pending_input={'kind':'text'},
           in_flight={'kind':'model'}, status='running', receipts=[{'operation_id':'parent-only'}],
           provider_state={'session':'private'}, pending_user_messages=[{'role':'user','content':'Not persisted boundary yet'}])
    before = stored(e, tid)
    child = fork(e, tid)
    row = stored(e, child['id'])
    assert stored(e, tid) == before
    assert [m['content'] for m in row['messages']] == [m['content'] for m in before['messages']]
    assert row['queue'] == [] and row['in_flight'] is None and row['approved'] is None and row['pending_input'] is None
    assert 'receipts' not in row and 'provider_state' not in row and not row.get('pending_user_messages')
    assert row['status'] == 'paused' and not child['allowed_actions']['resume']['enabled']
    response = e.client.post(f"/api/local/agent/tasks/{child['id']}/actions", json={'action':'resume','expected_revision':child['revision']})
    assert response.status_code == 409 and response.json()['detail'] == 'agent_fork_instruction_required'
    # Simulate a non-message control scheduling it: worker still refuses dispatch.
    e.runtime.store.transaction(lambda d: d['tasks'][child['id']].update(status='queued'))
    e.runtime.run_once()
    assert not e.model.calls and not e.runtime.tools.calls
    e.runtime.store.transaction(lambda d: d['tasks'][tid].update(status='paused', in_flight=None, queue=[], pending_input=None, pending_user_messages=[]))
    e.model.responses.append(assistant('Child independent answer'))
    instruct(e, child['id'], 'Try the alternate approach')
    for _ in range(3): e.runtime.run_once()
    assert e.runtime.get(child['id'])['result'] == 'Child independent answer'
    assert not e.runtime.tools.calls  # historical share_service was never replayed
    assert any('Correction:' in m.get('content','') for m in e.model.calls[0][1])
    e.model.responses.append(assistant('Parent independent answer'))
    instruct(e, tid, 'Continue original approach')
    for _ in range(3): e.runtime.run_once()
    assert e.runtime.get(tid)['result'] == 'Parent independent answer'
    assert e.runtime.get(child['id'])['result'] == 'Child independent answer'
    assert all('Try the alternate' not in m.get('content','') for m in e.model.calls[-1][1])


def test_incomplete_and_malformed_pairs_and_changed_boundary(env):
    e=env; tid=parent(e)
    complete=request(e,tid)
    first=assistant(name='inspect_service',call_id='one')['message']
    first['tool_calls'].append(copy.deepcopy(first['tool_calls'][0])); first['tool_calls'][1]['id']='two'
    append(e,tid,first,{'role':'tool','tool_call_id':'one','content':'ok'},status='running')
    assert [b['message_count'] for b in choices(e,tid)] == [1]
    assert fork(e,tid,complete)['fork']['message_count'] == 1  # appended suffix does not stale a prefix
    append(e,tid,{'role':'tool','tool_call_id':'two','content':'ok'})
    assert [b['message_count'] for b in choices(e,tid)] == [1,4]
    stale=request(e,tid)
    e.runtime.store.transaction(lambda d: d['tasks'][tid]['messages'][0].update(content='edited'))
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=stale)
    assert r.status_code==409 and r.json()['detail']=='agent_fork_boundary_changed'
    append(e,tid,{'role':'tool','tool_call_id':'orphan','content':'not paired'})
    assert choices(e,tid)[-1]['message_count']==4


def test_attachments_large_results_checkpoint_and_restart(env):
    e=env
    tid=parent(e, attachments=[{'name':'notes.txt','media_type':'text/plain','data_base64':base64.b64encode(b'Important correction').decode()}])
    source=stored(e,tid)
    ref=results.save(e.runtime.store,source,{'name':'inspect_service','operation_id':'history'}, {'text':'large result '*10000})
    call=assistant(name='inspect_service',call_id='large')['message']
    append(e,tid,call,{'role':'tool','tool_call_id':'large','content':json.dumps({'result_ref':ref['id']})},
        result_refs=[ref],context_checkpoint={'through':3,'content':'Prior goal and progress','references':{}})
    child=fork(e,tid); row=stored(e,child['id'])
    oldpath=Path(stored(e,tid)['messages'][0]['attachments'][0]['path'])
    newpath=Path(row['messages'][0]['attachments'][0]['path'])
    assert oldpath!=newpath and newpath.read_bytes()==oldpath.read_bytes()
    assert str(newpath) in row['messages'][0]['content'] and str(oldpath) not in row['messages'][0]['content']
    assert row['context_checkpoint']['through']==3
    early=fork(e,tid,request(e,tid,0))
    assert 'context_checkpoint' not in stored(e,early['id']) and not early['result_refs']
    oldpath.unlink(); results.path(e.runtime.store,tid,ref['id']).unlink()
    app,runtime,local,_,model=environment(e.base)
    try:
        with TestClient(app,headers={'x-test-owner':'yes'}) as client:
            restored=client.get(f"/api/local/agent/tasks/{child['id']}").json()
            assert restored['fork']==child['fork'] and restored['status']=='paused'
            page=client.get(f"/api/local/agent/tasks/{child['id']}/results/{ref['id']}")
            assert page.status_code==200 and 'large result' in page.json()['content']
            assert newpath.read_bytes()==b'Important correction'
            assert not model.calls
    finally: local.processes.close()
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
    assert r.status_code==409  # missing attachment cannot become a successful child


def test_idempotency_atomic_commit_failure_and_retry_after_restart(env,monkeypatch):
    e=env; tid=parent(e); body=request(e,tid)
    before=copy.deepcopy(e.runtime.store.read())
    original=e.runtime.store._write
    def fail(data): raise OSError('simulated disk full before root commit')
    monkeypatch.setattr(e.runtime.store,'_write',fail)
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
    assert r.status_code==409
    monkeypatch.setattr(e.runtime.store,'_write',original)
    assert e.runtime.store.read()==before
    app,runtime,local,_,_=environment(e.base)
    try:
        with TestClient(app,headers={'x-test-owner':'yes'}) as client:
            first=client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
            second=client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
            assert first.status_code==200 and first.json()['id']==second.json()['id']
            assert len(runtime.store.read()['tasks'])==2
            r=client.post(f'/api/local/agent/tasks/{tid}/fork',json={**body,'title':'different'})
            assert r.status_code==409
    finally: local.processes.close()


def test_permission_intersection_workspace_change_and_owner_guard(env):
    e=env; tid=parent(e)
    e.runtime.update_settings({'permission_mode':'read_only'})
    child=fork(e,tid)
    assert child['permission_mode']=='read_only'
    e.runtime.update_settings({'permission_mode':'workspace'})
    assert stored(e,child['id'])['permission_mode']=='read_only'
    changed=e.base/'different';changed.mkdir()
    e.runtime.update_settings({'workspace_root':str(changed)})
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
    assert r.status_code==409 and 'workspace_changed' in r.text
    with TestClient(e.app) as unauthorized:
        assert unauthorized.get(f'/api/local/agent/tasks/{tid}/fork-boundaries').status_code==403
        assert unauthorized.post(f'/api/local/agent/tasks/{tid}/fork',json={}).status_code==403


def test_reference_limits_and_path_escape(env,monkeypatch):
    e=env; tid=parent(e,attachments=[{'name':'note.txt','data_base64':base64.b64encode(b'note').decode()}])
    monkeypatch.setattr(task_forks,'MAX_REFERENCE_BYTES',1)
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
    assert r.status_code==409 and 'reference_limit' in r.text
    monkeypatch.setattr(task_forks,'MAX_REFERENCE_BYTES',40*1024*1024)
    outside=e.base/'outside.txt';outside.write_text('keep')
    e.runtime.store.transaction(lambda d:d['tasks'][tid]['messages'][0]['attachments'][0].update(path=str(outside)))
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
    assert r.status_code==409 and outside.read_text()=='keep'
    assert len(e.runtime.store.read()['tasks'])==1


def test_fork_does_not_own_parent_edits_and_shared_file_undo_conflicts(env):
    e=env;tid=parent(e)
    file=e.workspace/'shared.txt';file.write_text('original')
    context={'task_id':tid,'local_execution':stored(e,tid)['local_execution']}
    execute(e.local,context,'ryn_file_write',{'path':'shared.txt','content':'parent result'},'parent-write')
    child=fork(e,tid)
    child_id=child['id']
    assert e.client.get(f'/api/local/agent/tasks/{child_id}/changes').json()['files']==[]
    context['task_id']=child_id
    execute(e.local,context,'ryn_file_write',{'path':'shared.txt','content':'child result'},'child-write')
    p=e.client.post(f'/api/local/agent/tasks/{tid}/changes/preview',json={}).json()
    assert p['conflicts'] and not p['can_undo']
    r=e.client.post(f'/api/local/agent/tasks/{tid}/changes/undo',json={'token':p['token'],'confirm':True,'request_id':uuid.uuid4().hex})
    assert r.status_code==200 and r.json()['status']=='conflict'
    assert file.read_text()=='child result'

class FileTools:
    """Only the production file tools; tests replace the model transport alone."""
    supports_model_context = True
    def __init__(self, local):
        self.local = local
        self.calls = []
        self.change_review = local.change_review
    def specs(self, context=None):
        from rynmesh.agent_runtime.local_specs import SPECS
        return [{'type':'function','function':{'name':name,'description':SPECS[name][0],'parameters':SPECS[name][3]}}
                for name in ('ryn_file_write','ryn_file_read')]
    def metadata(self, name):
        from rynmesh.agent_runtime.local_specs import SPECS
        description,effect,risk,_=SPECS[name]
        return {'effect':effect,'title':description,'risk':risk,'scope_context_supported':True,
                'supports_preview':effect=='write','preview_precondition':effect=='write'}
    def preview(self,name,arguments,*,context=None):
        return self.local.preview(name,arguments,context)
    def execute(self,name,arguments,*,operation_id,cancelled,expected_preview=None,context=None):
        self.calls.append((context['task_id'],name,arguments))
        return self.local.execute(name,arguments,operation_id=operation_id,cancelled=cancelled,expected_preview=expected_preview,context=context)
    def validate_context(self,context,*,cheap_only=False):
        self.local.policy(context)


def test_new_instruction_uses_actual_file_tool_and_rechecks_live_permission(env):
    e=env;tid=parent(e)
    e.runtime.tools=FileTools(e.local)
    child=fork(e,tid)
    e.model.responses.extend([assistant(name='ryn_file_write',args={'path':'child.txt','content':'child independent file'}), assistant('Child file created')])
    instruct(e,child['id'],'Create child.txt')
    for _ in range(5): e.runtime.run_once()
    assert (e.workspace/'child.txt').read_text()=='child independent file'
    assert e.runtime.get(child['id'])['result']=='Child file created'
    assert len(e.client.get(f"/api/local/agent/tasks/{child['id']}/changes").json()['files'])==1
    assert not e.client.get(f'/api/local/agent/tasks/{tid}/changes').json()['files']
    second=fork(e,tid)
    instruct(e,second['id'],'Try another file')
    e.runtime.update_settings({'permission_mode':'read_only'})
    e.model.responses.extend([assistant(name='ryn_file_write',args={'path':'denied.txt','content':'must not write'}),assistant('Permission denied')])
    for _ in range(5): e.runtime.run_once()
    assert not (e.workspace/'denied.txt').exists()
    assert len(e.runtime.tools.calls)==1


def test_model_switch_excludes_provider_history_and_keeps_initial_instruction_gate(env):
    e=env
    rows=[{'id':identifier,'name':identifier,'source':'local_api','source_id':identifier,'peer_id':'self',
           'destination_revision':identifier+'-v1','available':True,'agent_supported':True} for identifier in ('local:test','local:other')]
    e.runtime.catalog.list_models=lambda:copy.deepcopy(rows)
    e.model.supports_destination_binding=True
    tid=parent(e)
    append(e,tid,{'role':'assistant','content':'OLD PROVIDER ANSWER','provider_state':{'session':'old'}})
    child=fork(e,tid)
    assert 'provider_state' not in stored(e,child['id'])['messages'][-1]
    preview=e.client.get(f"/api/local/agent/tasks/{child['id']}/model",params={'model_id':'local:other'}).json()
    r=e.client.put(f"/api/local/agent/tasks/{child['id']}/model",json={
        'model_id':'local:other','expected_control_revision':preview['control_revision'],'expected_scope_revision':preview['scope_revision'],
        'expected_destination':preview['destination'],'confirm_data_destination':True,'transfer_resource_scope':False,'client_request_id':str(uuid.uuid4())})
    assert r.status_code==200,r.text
    e.runtime.run_once()
    assert not e.model.calls
    row=stored(e,child['id'])
    projected=json.dumps(e.runtime._context(row))
    assert 'OLD PROVIDER ANSWER' not in projected and 'Original goal' in projected
    assert row['fork']['awaiting_instruction'] and row['queue']==[]


def test_mid_copy_failure_is_invisible_and_same_request_recovers(env,monkeypatch):
    e=env;tid=parent(e,attachments=[{'name':'a.txt','data_base64':base64.b64encode(b'a').decode()}, {'name':'b.txt','data_base64':base64.b64encode(b'b').decode()}])
    body=request(e,tid); original=task_forks.atomic_write_bytes; writes=[]
    def fail_second(path,raw):
        writes.append(path)
        if len(writes)==2: raise OSError('interrupted between copied attachments')
        return original(path,raw)
    monkeypatch.setattr(task_forks,'atomic_write_bytes',fail_second)
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
    assert r.status_code==409 and len(e.runtime.store.read()['tasks'])==1
    monkeypatch.setattr(task_forks,'atomic_write_bytes',original)
    app,runtime,local,_,_=environment(e.base)
    try:
        with TestClient(app,headers={'x-test-owner':'yes'}) as client:
            r=client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
            assert r.status_code==200,r.text
            attachments=runtime.store.read()['tasks'][r.json()['id']]['messages'][0]['attachments']
            assert [Path(a['path']).read_bytes() for a in attachments]==[b'a',b'b']
    finally: local.processes.close()


def test_attachment_junction_is_rejected(env):
    if os.name!='nt': pytest.skip('Windows junction acceptance')
    import subprocess
    e=env;tid=parent(e,attachments=[{'name':'a.txt','data_base64':base64.b64encode(b'a').decode()}])
    directory=Path(stored(e,tid)['messages'][0]['attachments'][0]['path']).parent
    saved=directory.with_name(directory.name+'-original')
    directory.rename(saved)
    # mklink creates a junction only; cleanup removes the junction, never its target.
    proc=subprocess.run(['cmd','/c','mklink','/J',str(directory),str(saved)],capture_output=True)
    assert proc.returncode==0,proc.stderr
    try:
        r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
        assert r.status_code==409 and 'link_forbidden' in r.text
        assert len(e.runtime.store.read()['tasks'])==1
    finally: directory.rmdir()


def test_abrupt_process_exit_before_commit_and_restart(env):
    import subprocess
    import sys
    e=env;tid=parent(e,attachments=[{'name':'crash.txt','data_base64':base64.b64encode(b'persisted bytes').decode()}])
    body=request(e,tid)
    input_file=e.base/'fork-request.json';input_file.write_text(json.dumps(body),encoding='utf-8')
    program='''
import json, os, sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_agent_file_changes import environment
from fastapi.testclient import TestClient
base=Path(sys.argv[1]);app,runtime,local,_,_=environment(base)
def crash(data): os._exit(73)
runtime.store._write=crash
with TestClient(app,headers={'x-test-owner':'yes'}) as client:
    client.post('/api/local/agent/tasks/'+sys.argv[2]+'/fork',json=json.loads((base/'fork-request.json').read_text()))
'''
    process=subprocess.run([sys.executable,'-X','utf8','-c',program,str(e.base),tid],capture_output=True,timeout=60)
    assert process.returncode==73,process.stderr.decode(errors='replace')
    app,runtime,local,_,_=environment(e.base)
    try:
        assert len(runtime.store.read()['tasks'])==1
        with TestClient(app,headers={'x-test-owner':'yes'}) as client:
            r=client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
            assert r.status_code==200,r.text
            child=runtime.store.read()['tasks'][r.json()['id']]
            assert child['status']=='paused' and Path(child['messages'][0]['attachments'][0]['path']).read_bytes()==b'persisted bytes'
    finally:local.processes.close()


def test_task_bound_native_reference_is_reported_not_silently_copied(env):
    e=env;tid=parent(e)
    append(e,tid,assistant(name='inspect_service',call_id='native')['message'],
           {'role':'tool','tool_call_id':'native','content':json.dumps({'native_observation_artifacts':[{'path':'.ryn/computer/parent/image.png','task_id':tid}]})})
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
    assert r.status_code==409 and 'reference_requires_source_task' in r.text
    assert len(e.runtime.store.read()['tasks'])==1


def test_safe_boundary_epoch_and_staged_file_conflict(env,monkeypatch):
    e=env;tid=parent(e,attachments=[{'name':'staged.txt','data_base64':base64.b64encode(b'owner attachment').decode()}])
    append(e,tid,assistant(name='inspect_service',call_id='wrong-epoch')['message'],
           {'role':'tool','tool_call_id':'wrong-epoch','content':'ok','context_epoch':9})
    assert len(choices(e,tid))==1
    body=request(e,tid)
    original=e.runtime.store._write
    monkeypatch.setattr(e.runtime.store,'_write',lambda data: (_ for _ in ()).throw(OSError('root failure')))
    assert e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=body).status_code==409
    monkeypatch.setattr(e.runtime.store,'_write',original)
    child_id='agent_'+uuid.uuid5(uuid.NAMESPACE_URL,'ryn-fork:'+body['client_request_id']).hex
    staged=next((e.workspace/'.ryn/attachments'/child_id).glob('*.txt'))
    staged.write_bytes(b'user edited staging file')
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=body)
    assert r.status_code==409 and 'staging_changed' in r.text
    assert staged.read_bytes()==b'user edited staging file'
from test_agent_resources import resources  # noqa: F401


def test_current_resource_revocation_blocks_fork_and_task_bound_refs_fail_closed(env,resources):
    from test_agent_resources import file_ref,invoke
    e=env; resource_tools,plugin,context,_=resources
    e.runtime.consumer_node_id=context['consumer_node_id']
    destination=context['model_destination']
    e.runtime.catalog.list_models=lambda:[{'id':destination['catalog_model_id'],'name':'Resource test','source':'local_api',
        'source_id':destination['source_id'],'destination_revision':destination['destination_revision'],'available':True,'agent_supported':True}]
    e.model.supports_destination_binding=True
    tid=parent(e,model_id=destination['catalog_model_id'])
    e.runtime.tools.resources=resource_tools
    e.runtime.tools.validate_context=resource_tools.validate_context
    def bind(data):
        row=data['tasks'][tid]
        row['scope']={'resource_grants':copy.deepcopy(context['resource_grants']),'egress_grants':copy.deepcopy(context['egress_grants'])}
    e.runtime.store.transaction(bind)
    child=fork(e,tid)
    assert stored(e,child['id'])['scope']==stored(e,tid)['scope']
    # Source resource handles are task-bound. They must not quietly become stale
    # pointers under a different task, even if the underlying grant is still valid.
    ref=file_ref(resource_tools,context)
    read=invoke(resource_tools,context,'ryn_read_resource',ref)
    append(e,tid,assistant(name='ryn_read_resource',args={'resource_ref':ref},call_id='resource')['message'],
        {'role':'tool','tool_call_id':'resource','content':json.dumps(read),'resource_origin':read['origin']})
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid))
    assert r.status_code==409 and 'reference_requires_source_task' in r.text
    plugin.update_source('docs',{'allow_ai':False})
    r=e.client.post(f'/api/local/agent/tasks/{tid}/fork',json=request(e,tid,0))
    assert r.status_code==409 and ('permission' in r.text or 'resource' in r.text)


def test_fresh_provider_conversation_key_and_nested_fork_references(env):
    e=env;tid=parent(e,attachments=[{'name':'context.txt','data_base64':base64.b64encode(b'goals and corrections').decode()}])
    append(e,tid,{'role':'assistant','content':'Prior progress','provider_state':{'session_id':'parent-session'}})
    child=fork(e,tid);grandchild=fork(e,child['id'])
    row=stored(e,grandchild['id'])
    attachment=row['messages'][0]['attachments'][0]
    assert Path(attachment['path']).parent.name==grandchild['id']
    assert Path(attachment['path']).read_bytes()==b'goals and corrections'
    assert child['id'] not in row['goal']['text'] and tid not in row['goal']['text']
    assert row['fork']['source_task_id']==child['id'] and not row.get('parent_task_id')
    conversations=[]
    def complete(model_id,messages,tool_specs,cancelled,on_event,**options):
        conversations.append(options.get('conversation'))
        assert all('provider_state' not in m for m in messages)
        assert any('Prior progress' in m.get('content','') for m in messages)
        return assistant('Independent transport session')
    e.model.supports_conversation_key=True;e.model.complete=complete
    instruct(e,grandchild['id'],'Proceed with new instructions')
    for _ in range(3):e.runtime.run_once()
    assert conversations==[grandchild['id']]
    assert e.runtime.get(grandchild['id'])['result']=='Independent transport session'
