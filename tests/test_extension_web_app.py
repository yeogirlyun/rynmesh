import json
import pytest
from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions.imports import ExtensionImportManager
from rynmesh.agent_runtime.extensions.web_app import validate_web_app
from rynmesh.agent_runtime.store import AgentError

@pytest.mark.parametrize('url',['javascript:alert(1)','file:///C:/secret','http://example.com','https://user:pass@example.com','https://example.com/#token','https://example.com\\@evil.test'])
def test_reject_unsafe_app_origins(url):
    with pytest.raises(AgentError):validate_web_app({'url':url})

def test_web_app_import_is_explicit_disabled_and_survives_store_reload(tmp_path):
    package=tmp_path/'package';package.mkdir()
    web={'url':'https://app.example.test/workbench','permissions':['messages']}
    (package/'plugin.json').write_text(json.dumps({'id':'external-app','name':'External app','version':'1.0.0','ryn':{'webApp':web}}))
    store=ExtensionStore(tmp_path/'agent');imports=ExtensionImportManager(store)
    preview=imports.preview({'kind':'plugin','path':str(package)})
    assert preview['can_import']
    imports.commit(preview['preview_id'],expected_revision=preview['expected_revision'])
    plugin=next(p for p in ExtensionStore(tmp_path/'agent').overview()['plugins'] if p['id']=='external-app')
    assert plugin['web_app']==web
    assert plugin['enabled'] is False
