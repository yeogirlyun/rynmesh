"""Owner presentation settings must not reinstall a plugin or reset its MCP login."""
import copy
import json
import socket
from types import SimpleNamespace

import pytest


@pytest.fixture
def installed(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('external network denied before application imports')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    from rynmesh.agent_runtime.extensions import ExtensionStore
    from rynmesh.agent_runtime.extensions.imports import ExtensionImportManager
    from rynmesh.agent_runtime.store import AgentError
    package = tmp_path/'package'
    (package/'skills/helper').mkdir(parents=True)
    (package/'skills/helper/SKILL.md').write_text('---\nname: synthetic-helper\ndescription: Synthetic only\n---\nSynthetic instructions.\n', encoding='utf-8')
    view = {'id':'summary','title':'Summary','mcpServer':'primary','tool':'read_report','inputs':[],
            'columns':[{'key':'name','label':'Name'}],'rowsPath':'data.rows',
            'pagination':{'pageArgument':'page','sizeArgument':'pageSize','path':'data.pagination','pageSize':20}}
    declaration = {'id':'synthetic-web','name':'Synthetic','version':'1','ryn':{
        'identityGroup':{'id':'synthetic.example','name':'Synthetic','origin':'https://identity.example.test'},
        'webApp':{'url':'https://app.example.test/workbench','permissions':['agent','tools']},'views':[view]}}
    (package/'plugin.json').write_text(json.dumps(declaration),encoding='utf-8')
    (package/'mcp.json').write_text(json.dumps({'mcpServers':{key:{'type':'http','url':'https://identity.example.test/mcp',
        'oauth':{'clientId':'synthetic-client','scopes':['finance.read']}} for key in ['primary','secondary']}}),encoding='utf-8')
    store=ExtensionStore(tmp_path/'agent')
    imports=ExtensionImportManager(store)
    preview=imports.preview({'kind':'plugin','path':str(package)})
    assert preview['can_import']
    imports.commit(preview['preview_id'],expected_revision=preview['expected_revision'])
    plugin=next(p for p in store.overview()['plugins'] if p['id']=='synthetic-web')
    store.set_plugin_enabled(plugin['id'],True,expected_revision=plugin['revision'])
    return store,AgentError


def current(store):
    return next(p for p in store.overview()['plugins'] if p['id']=='synthetic-web')


def configure(store, **overrides):
    p=current(store)
    return store.configure_plugin_web_capabilities(p['id'],**{
        'downloads':True,'dialogs':True,'expected_instance_id':p['instance_id'],
        'expected_revision':p['revision'],**overrides})


def test_capability_update_preserves_identity_mcp_configuration_skills_and_views(installed):
    store,_=installed
    before=copy.deepcopy(store._read())
    old=before['plugins']['synthetic-web']
    changed=configure(store)
    after=store._read()
    assert changed['web_app']=={'url':old['web_app']['url'],'permissions':['agent','dialogs','downloads','tools']}
    assert changed['instance_id']==old['instance_id'] and changed['enabled'] is True
    assert changed['revision']>old['revision']
    assert after['mcp_servers']==before['mcp_servers']
    assert after['skills']==before['skills']
    for key,value in old.items():
        if key not in {'web_app','revision','updated_at'}:
            assert after['plugins']['synthetic-web'][key]==value
    from rynmesh.agent_runtime.extensions import ExtensionStore
    assert current(ExtensionStore(store.root))['web_app']==changed['web_app']


def test_noop_does_not_rotate_revision_and_revocation_preserves_other_permissions(installed):
    store,_=installed
    enabled=configure(store)
    assert configure(store)['revision']==enabled['revision']
    revoked=configure(store,downloads=False,dialogs=False)
    assert revoked['web_app']['permissions']==['agent','tools']
    assert revoked['instance_id']==enabled['instance_id']


def test_stale_identity_revision_and_non_boolean_values_cannot_change_state(installed):
    store,error=installed
    old=current(store)
    configure(store)
    for args in [{'expected_revision':old['revision']},{'expected_instance_id':'another-instance'},
                 {'expected_revision':None},{'downloads':'true'},{'dialogs':1}]:
        before=copy.deepcopy(store._read())
        with pytest.raises(error):configure(store,**args)
        assert store._read()==before


def test_missing_web_app_or_builtin_cannot_receive_capabilities(installed):
    store,error=installed
    state=store._read()
    state['plugins']['synthetic-web']['web_app']=None
    store._write(state)
    before=copy.deepcopy(store._read())
    with pytest.raises(error):configure(store)
    assert store._read()==before
    state=store._read()
    state['plugins']['synthetic-web']['builtin']=True
    store._write(state)
    before=copy.deepcopy(store._read())
    with pytest.raises(error):configure(store)
    assert store._read()==before


def test_endpoint_uses_owner_gate_and_accepts_only_the_two_capabilities(installed,monkeypatch):
    store,_=installed
    from fastapi import HTTPException

    from rynmesh.agent_runtime.extensions import routes
    registry={}
    class Router:
        def __getattr__(self,method):
            return lambda path:lambda fn:registry.setdefault((method,path),fn)
    async def inline(fn):return fn()
    monkeypatch.setattr(routes.asyncio,'to_thread',inline)
    monkeypatch.setattr(routes,'failure',lambda *args,**kwargs:None)
    gated=[]
    def owner(request):
        gated.append(True)
        if not request.allowed:raise HTTPException(403,'owner_required')
    routes.install_extension_routes(Router(),runtime=SimpleNamespace(store=SimpleNamespace(inspect=lambda fn:False)),
        extensions=SimpleNamespace(store=store,manager=SimpleNamespace()),local_control=owner)
    handler=registry[('patch','/api/local/agent/extensions/plugins/{plugin_id}/web-capabilities')]
    class Request:
        def __init__(self,body,allowed=True):self.body=body;self.allowed=allowed
        async def stream(self):yield json.dumps(self.body).encode()
    def execute(request):
        coroutine=handler('synthetic-web',request)
        try:coroutine.send(None)
        except StopIteration as done:return done.value
        raise AssertionError('unexpected external await in local route test')
    p=current(store)
    body={'downloads':True,'dialogs':True,'expected_instance_id':p['instance_id'],'expected_revision':p['revision']}
    before=copy.deepcopy(store._read())
    with pytest.raises(HTTPException) as denied:execute(Request(body,False))
    assert denied.value.status_code==403 and store._read()==before
    with pytest.raises(HTTPException) as invalid:execute(Request({**body,'url':'https://other.example.test'}))
    assert invalid.value.status_code==400 and store._read()==before
    changed=execute(Request(body))
    assert changed['web_app']['permissions']==['agent','dialogs','downloads','tools']
    assert len(gated)==3
