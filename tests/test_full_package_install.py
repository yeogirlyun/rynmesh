import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('full_install', Path(__file__).resolve().parents[1] / 'scripts/full_package_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_cli_retains_failure_stage_and_subprocess_stderr(tmp_path, monkeypatch):
    def failed(*args):
        print('stage: stop-owned-processes')
        raise installer.subprocess.CalledProcessError(1, ['powershell.exe'], stderr=b'CIM unavailable')
    monkeypatch.setattr(installer, 'prepare_upgrade', failed)
    log = tmp_path / 'logs' / 'prepare.log'
    result = installer.main(['--prepare-upgrade', '--payload', str(tmp_path),
                             '--destination', str(tmp_path / 'install'), '--log', str(log)])
    assert result == 1
    text = log.read_text(encoding='utf-8')
    assert 'stage: stop-owned-processes' in text
    assert 'CIM unavailable' in text
    assert 'operation: complete' not in text


def test_cli_appends_success_without_destroying_previous_log(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'prepare_upgrade', lambda *args: None)
    log = tmp_path / 'prepare.log'
    log.write_text('previous attempt\n', encoding='utf-8')
    assert installer.main(['--prepare-upgrade', '--payload', str(tmp_path),
                           '--destination', str(tmp_path), '--log', str(log)]) == 0
    assert log.read_text(encoding='utf-8').startswith('previous attempt\n')
    assert 'operation: complete' in log.read_text(encoding='utf-8')


def test_package_detects_changed_and_extra_files(tmp_path):
    asset = tmp_path / 'editor.wasm'
    asset.write_bytes(b'verified-build')
    (tmp_path / 'full-package.json').write_text(json.dumps({'files': {'editor.wasm': installer.sha(asset)}}))
    installer.verify(tmp_path)
    asset.write_bytes(b'changed-build')
    with pytest.raises(ValueError, match='integrity'):
        installer.verify(tmp_path)
    asset.write_bytes(b'verified-build')
    (tmp_path / 'unexpected.key').write_text('private')
    with pytest.raises(ValueError, match='inventory'):
        installer.verify(tmp_path)


def test_existing_version_is_preserved(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    (source / 'asset').write_text('new')
    installer.copy_new(source, target)
    (target / 'asset').write_text('existing')
    installer.copy_new(source, target)
    assert (target / 'asset').read_text() == 'existing'
    assert not list(tmp_path.glob('.full-install-*'))


def test_upgrade_verifies_package_before_stopping_or_uninstalling(tmp_path, monkeypatch):
    destination = tmp_path / 'Ryn'
    destination.mkdir()
    (destination / 'Ryn.exe').write_bytes(b'old')
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: calls.append(a))
    with pytest.raises(FileNotFoundError):
        installer.prepare_upgrade(tmp_path / 'invalid-payload', destination)
    assert calls == []
    assert (destination / 'Ryn.exe').read_bytes() == b'old'


def test_upgrade_backs_up_and_preserves_data_using_update_uninstall(tmp_path, monkeypatch):
    payload = tmp_path / 'payload'
    payload.mkdir()
    (payload / 'full-package.json').write_text('{"files":{}}')
    destination = tmp_path / 'Ryn'
    destination.mkdir()
    (destination / 'Ryn.exe').write_bytes(b'old-app')
    (destination / 'uninstall.exe').write_bytes(b'old-uninstaller')
    (destination / 'user-data').write_bytes(b'keep')
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda args, **kw: calls.append(args))
    installer.prepare_upgrade(payload, destination)
    assert calls[0][0] == 'powershell.exe'
    assert calls[1] == [str(destination / 'uninstall.exe'), '/S', '/UPDATE', '_?=' + str(destination)]
    assert (destination / 'user-data').read_bytes() == b'keep'
    backups = list((tmp_path / 'Ryn-install-backups').glob('*/Ryn.exe'))
    assert len(backups) == 1 and backups[0].read_bytes() == b'old-app'


@pytest.mark.skipif(installer.os.name != 'nt', reason='Windows sharing violations')
def test_copy_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    (source / 'asset').write_text('complete')
    rename = Path.rename
    calls = []
    def locked_then_ready(path, destination):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError('transient antivirus lock')
        return rename(path, destination)
    monkeypatch.setattr(Path, 'rename', locked_then_ready)
    monkeypatch.setattr(installer.time, 'sleep', lambda seconds: None)
    installer.copy_new(source, target)
    assert len(calls) == 3
    assert (target / 'asset').read_text() == 'complete'
    assert not list(tmp_path.glob('.full-install-*'))


def test_packager_stages_company_plugins_without_office(tmp_path, monkeypatch):
    script_dir = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(script_dir))
    spec = importlib.util.spec_from_file_location('prepare_full', script_dir / 'prepare_full_package.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    monkeypatch.setattr(packager, 'COMPANY', ())
    monkeypatch.setattr(packager.subprocess, 'check_output', lambda *a, **kw: 'test-commit')
    company = tmp_path / 'company'; company.mkdir()
    (company / 'runtime-manifest.json').write_text('{"source_commit":"company-test","files":{}}')
    source = tmp_path / 'source'; source.mkdir()
    (source / 'plugin-declarations.json').write_text('{"plugins":{},"mcp_servers":{}}')
    output = tmp_path / 'output'
    packager.prepare(output, company, source)
    manifest = json.loads((output / 'full-package.json').read_text())
    # Ryn downloads the Office editors (with its Chinese-font builds) after installation.
    assert manifest['plugins'] == [] and not (output / 'office').exists()
    assert not any(name.startswith('office/') for name in manifest['files'])


@pytest.mark.parametrize('disabled', [False, True])
@pytest.mark.parametrize('custom', [False, True])
def test_managed_endpoint_migration_preserves_identity_and_disable(tmp_path, disabled, custom):
    from rynmesh.agent_runtime.extensions import ExtensionStore
    from rynmesh.services.peer_box import load_or_create_messaging_key
    store = ExtensionStore(tmp_path / 'agent', load_or_create_messaging_key(tmp_path / 'key'))
    original = 'https://custom.example.test/mcp' if custom else 'https://stock.example.test/ryn/mcp'
    store.commit_imported_items(plugins=[{'id': 'p', 'mcp_server_ids': ['s'], 'skill_ids': []}], skills=[],
        mcp_servers=[{'id': 's', 'plugin_id': 'p', 'transport': 'streamable_http', 'url': original}])
    if not disabled:
        store.set_plugin_enabled('p', True)
    before = store.get_mcp_server_internal('s')
    declaration = {'mcp_servers': [{'id': 's', 'plugin_id': 'p', 'url': 'http://127.0.0.1:18140/ryn/agent-mcp',
        'auth': {'mode': 'oauth', 'client_id': 'ryn-desktop', 'scopes': ['workspace.access']},
        'local_service': {'id': 'company', 'path': '/ryn/agent-mcp', 'legacy_urls': ['https://stock.example.test/ryn/mcp']}}]}
    mapped = installer.configure_local_mcp_services(store, declaration, 19293)
    after = store.get_mcp_server_internal('s')
    assert after['instance_id'] == before['instance_id']
    assert after['effective_enabled'] == before['effective_enabled'] == (not disabled)
    assert after['url'] == (original if custom else 'http://127.0.0.1:19293/ryn/agent-mcp')
    assert mapped == ([] if custom else ['s'])
    generation = after['config_generation']
    assert installer.configure_local_mcp_services(store, declaration, 19293) == mapped
    assert store.get_mcp_server_internal('s')['config_generation'] == generation


def test_release_three_way_upgrade_preserves_custom_skill_and_user_state(tmp_path):
    import copy

    from rynmesh.agent_runtime.extensions import ExtensionStore
    from rynmesh.services.peer_box import load_or_create_messaging_key
    store = ExtensionStore(tmp_path / 'agent', load_or_create_messaging_key(tmp_path / 'key'))
    plugins = [{'id': 'p', 'name': 'Publisher', 'version': '1', 'skill_ids': ['stock', 'custom'], 'mcp_server_ids': ['s']}]
    skills = [{'id': sid, 'plugin_id': 'p', 'content': 'old' if sid == 'stock' else 'user instructions', 'version': '1'}
              for sid in ['stock', 'custom']]
    servers = [{'id': 's', 'plugin_id': 'p', 'transport': 'streamable_http', 'url': 'https://custom.example.test/mcp'}]
    store.commit_imported_items(plugins=plugins, skills=skills, mcp_servers=servers)
    before = copy.deepcopy(store._read())
    declarations = {'plugins': [{**plugins[0], 'version': '2'}],
                    'skills': [{**s, 'content': 'new workflow', 'version': '2'} for s in skills], 'mcp_servers': servers,
                    'previous_defaults': [{'plugins': {'p': plugins[0]},
                       'skills': {s['id']: {**s, 'content': 'old'} for s in skills}}]}
    result = store.migrate_release_defaults(declarations)
    after = store._read()
    assert after['skills']['stock']['content'] == 'new workflow'
    assert after['skills']['custom']['content'] == 'user instructions'
    assert after['skills']['custom']['version'] == '1'
    assert result['conflicts'] and after['mcp_servers'] == before['mcp_servers']
    assert after['plugins']['p']['enabled'] is False
    assert after['skills']['stock']['instance_id'] == before['skills']['stock']['instance_id']
    repeated = store.migrate_release_defaults(declarations)
    assert repeated['updated'] == [] and store._read() == after
    # Newly shipped connections are added disabled; previously removed ones
    # stay removed. Neither migration can enable the existing parent plugin.
    next_release = copy.deepcopy(declarations)
    next_release['mcp_servers'] += [{'id': 'operations', 'plugin_id': 'p', 'transport': 'streamable_http',
                                     'url': 'https://synthetic.example.test/operations'}]
    store.migrate_release_defaults(next_release)
    migrated = store._read()
    assert migrated['mcp_servers']['operations']['enabled'] is False
    assert 'operations' in migrated['plugins']['p']['mcp_server_ids']
    store.delete_mcp_server('operations')
    store.migrate_release_defaults(next_release)
    assert 'operations' not in store._read()['mcp_servers']


def test_release_moves_only_unchanged_connection_to_new_sign_in_mode(tmp_path):
    from rynmesh.agent_runtime.extensions import ExtensionStore
    from rynmesh.services.peer_box import load_or_create_messaging_key
    store = ExtensionStore(tmp_path / 'agent', load_or_create_messaging_key(tmp_path / 'key'))
    published = {'mode': 'oauth', 'client_id': 'ryn-desktop', 'scopes': ['workspace.access']}
    owner = {**published, 'scopes': ['workspace.access', 'owner.extra']}
    plugins = [{'id': 'p', 'name': 'Publisher', 'version': '1', 'mcp_server_ids': ['stock', 'custom']}]
    servers = [{'id': sid, 'plugin_id': 'p', 'transport': 'streamable_http', 'auth': auth,
                'url': 'http://127.0.0.1:18140/' + sid} for sid, auth in (('stock', published), ('custom', owner))]
    store.commit_imported_items(plugins=plugins, skills=[], mcp_servers=servers)
    before = store._read()['mcp_servers']
    declarations = {'mcp_servers': [{**s, 'auth': {'mode': 'none'}} for s in servers],
                    'previous_defaults': [{'mcp_servers': {s['id']: {'auth': {**published, 'callback_port': None}} for s in servers}}]}
    result = store.migrate_release_defaults(declarations)
    after = store._read()['mcp_servers']
    stock, custom = after['stock'], after['custom']
    assert stock['auth'] == {'mode': 'none', 'client_id': None, 'scopes': [], 'callback_port': None}
    assert stock['instance_id'] == before['stock']['instance_id'] and stock['enabled'] == before['stock']['enabled']
    assert stock['config_generation'] > before['stock']['config_generation'] and stock['status'] == 'NeedsTest'
    assert stock['config_sha256'] != before['stock']['config_sha256'] and stock['discovered_tools'] == []
    # An owner-changed sign-in setting is kept and reported, never replaced.
    assert custom == before['custom']
    assert result['conflicts'] == [{'category': 'mcp_servers', 'id': 'custom', 'field': 'auth'}]
    repeated = store.migrate_release_defaults(declarations)
    assert repeated['updated'] == [] and store._read()['mcp_servers']['stock'] == stock


def test_release_retires_only_known_duplicate_default_connections(tmp_path):
    from rynmesh.agent_runtime.extensions import ExtensionStore
    from rynmesh.services.peer_box import load_or_create_messaging_key
    store = ExtensionStore(tmp_path / 'agent', load_or_create_messaging_key(tmp_path / 'key'))
    for sid, url in [('stock', 'https://synthetic.example.test/mcp'), ('custom', 'https://owner.example.test/mcp')]:
        store.upsert_mcp_server({'id': sid, 'transport': 'streamable_http', 'url': url, 'enabled': True,
            'auth': {'mode': 'oauth', 'client_id': 'ryn-desktop', 'scopes': ['finance.read']}})
    declarations = {'retired_connections': [{'id': sid, 'plugin_id': None,
        'urls': ['https://synthetic.example.test/mcp'], 'scopes': ['finance.read']} for sid in ['stock', 'custom']]}
    before = store.get_mcp_server_internal('stock')
    result = store.migrate_release_defaults(declarations)
    retired = store.get_mcp_server_internal('stock')
    assert not retired['enabled'] and retired['instance_id'] == before['instance_id'] and retired['auth'] == before['auth']
    assert store.get_mcp_server_internal('custom')['enabled']
    assert result['conflicts'] == [{'category': 'mcp_servers', 'id': 'custom', 'field': 'retirement'}]
    store.migrate_release_defaults(declarations)
    assert store.get_mcp_server_internal('stock')['revision'] == retired['revision']
