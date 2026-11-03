import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('full_install', Path(__file__).resolve().parents[1] / 'scripts/full_package_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


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
    monkeypatch.setattr(installer, 'validate_office_payload', lambda payload: None)
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


@pytest.fixture
def office_payload(tmp_path, monkeypatch):
    from rynmesh.node_apps import craft_compat, craft_host, craft_native
    payload, cache = tmp_path / 'payload', tmp_path / 'cache'
    slug, version, identifier = 'gridcraft', 'v0.6.1', 'test-cjk-blank'
    monkeypatch.setattr(craft_compat, 'COMPAT', {slug: {'id': identifier}})
    monkeypatch.setattr(craft_host, 'specification', lambda slug: {'tag': version})
    def compat(root, slug, **kwargs):
        folder = root / slug / version / 'compatibility' / identifier
        if not folder.exists():
            return None
        if (folder / 'editor.wasm').read_bytes() != b'font-and-blank-fix':
            raise ValueError('corrupt compatibility payload')
        return folder
    monkeypatch.setattr(craft_compat, 'installed', compat)
    monkeypatch.setattr(craft_host, 'installed', lambda root, slug: root / slug / version / 'payload')
    monkeypatch.setattr(craft_native, 'runtime_status', lambda root, slug: {'verified': (root / slug / version / 'agent/cli').is_file()})
    source = payload / slug / version
    for name in ['payload', 'agent', 'compatibility/' + identifier]:
        (source / name).mkdir(parents=True)
    (source / 'payload/original').write_bytes(b'original')
    (source / 'agent/cli').write_bytes(b'native')
    (source / 'compatibility' / identifier / 'editor.wasm').write_bytes(b'font-and-blank-fix')
    return payload, cache, source, cache / slug / version, identifier


@pytest.mark.parametrize('existing', [False, True])
def test_office_fresh_and_upgrade_require_and_install_current_compat(office_payload, existing):
    payload, cache, source, target, identifier = office_payload
    if existing:
        (target / 'payload').mkdir(parents=True)
        (target / 'payload/original').write_bytes(b'keep-existing')
        (target / 'compatibility/old').mkdir(parents=True)
        (target / 'compatibility/old/receipt').write_bytes(b'keep-old')
        (target / 'preferences.json').write_bytes(b'keep-preferences')
    installer.provision_office(payload, cache, 'gridcraft')
    installer.provision_office(payload, cache, 'gridcraft')
    assert (target / 'compatibility' / identifier / 'editor.wasm').read_bytes() == b'font-and-blank-fix'
    if existing:
        assert (target / 'payload/original').read_bytes() == b'keep-existing'
        assert (target / 'compatibility/old/receipt').read_bytes() == b'keep-old'
        assert (target / 'preferences.json').read_bytes() == b'keep-preferences'


def test_missing_compat_fails_before_changing_existing_install(office_payload):
    payload, cache, source, target, identifier = office_payload
    (source / 'compatibility' / identifier / 'editor.wasm').unlink()
    (source / 'compatibility' / identifier).rmdir()
    with pytest.raises(ValueError, match='compatibility build is missing'):
        installer.provision_office(payload, cache, 'gridcraft')
    assert not cache.exists()


def test_corrupt_compat_fails_before_changing_existing_install(office_payload):
    payload, cache, source, target, identifier = office_payload
    (source / 'compatibility' / identifier / 'editor.wasm').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='corrupt'):
        installer.provision_office(payload, cache, 'gridcraft')
    assert not cache.exists()


def test_upgrade_rejects_missing_required_fonts_before_stopping_processes(tmp_path, monkeypatch):
    (tmp_path / 'full-package.json').write_text('{"files":{}}')
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match='compatibility build is missing'):
        installer.prepare_upgrade(tmp_path, tmp_path / 'install')
    assert not calls


@pytest.mark.parametrize('has_compat', [False, True])
def test_packager_cannot_publish_without_required_compat(tmp_path, monkeypatch, has_compat):
    from rynmesh.node_apps import craft_host, craft_native, craft_compat
    script_dir = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(script_dir))
    spec = importlib.util.spec_from_file_location('prepare_full', script_dir / 'prepare_full_package.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    monkeypatch.setattr(packager, 'OFFICE', ('gridcraft',))
    monkeypatch.setattr(packager, 'COMPANY', ())
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(packager.subprocess, 'check_output', lambda *a, **kw: 'test-commit')
    monkeypatch.setattr(craft_host, 'specification', lambda slug: {'tag':'v0.6.1'})
    monkeypatch.setattr(craft_host, 'installed', lambda *a: True)
    monkeypatch.setattr(craft_host, 'install', lambda *a: None)
    monkeypatch.setattr(craft_native, 'runtime_status', lambda *a: {'verified':True})
    monkeypatch.setattr(craft_compat, 'installed', lambda *a, **kw: tmp_path if has_compat else None)
    company = tmp_path / 'company'; company.mkdir()
    (company / 'runtime-manifest.json').write_text('{"source_commit":"company-test","files":{}}')
    source = tmp_path / 'source'; source.mkdir()
    (source / 'plugin-declarations.json').write_text('{"plugins":{},"mcp_servers":{}}')
    output = tmp_path / 'output'
    if has_compat:
        packager.prepare(output, company, source)
        assert (output / 'full-package.json').is_file()
    else:
        with pytest.raises(ValueError, match='compatibility build is missing'):
            packager.prepare(output, company, source)
        assert not (output / 'full-package.json').exists()
