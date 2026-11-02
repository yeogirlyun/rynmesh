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
