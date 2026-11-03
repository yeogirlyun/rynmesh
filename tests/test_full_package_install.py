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
