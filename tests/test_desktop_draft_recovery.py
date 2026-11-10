"""Exercise recovery against signed artifacts and a fake GitHub draft API."""
import base64
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('draft_recovery', SCRIPTS / 'upload_desktop_draft.py')
uploader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uploader)


@pytest.mark.parametrize('tag,draft,actual', [
    ('v0.9.0', False, 'v0.9.0'),
    ('v0.9.0', True, 'v0.8.0'),
    ('v00.9.0', True, 'v00.9.0'),
    ('v0.9.0-rc1', True, 'v0.9.0-rc1'),
])
def test_rejects_wrong_or_published_release(tag, draft, actual):
    with pytest.raises(ValueError):
        uploader.release_version({'draft': draft, 'tag_name': actual}, tag)


def test_recovers_exact_signed_build_without_touching_other_assets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / 'update-assets'
    root.mkdir()
    key = Ed25519PrivateKey.generate()
    key_id = b'testonly'
    public = b'Ed' + key_id + key.public_key().public_bytes_raw()
    public_text = 'untrusted comment: test key\n' + base64.b64encode(public).decode()
    env = {'GITHUB_REPOSITORY': 'yeogirlyun/rynmesh', 'RELEASE_ID': '99',
           'SOURCE_RUN_ID': '77', 'SOURCE_SHA': 'a' * 40, 'RELEASE_TAG': 'v0.9.0',
           'GH_TOKEN': 'test-placeholder',
           'RYN_DESKTOP_UPDATE_PUBLIC_KEY': base64.b64encode(public_text.encode()).decode()}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for platform in uploader.PLATFORMS:
        name = 'Ryn-0.9.0-' + platform + ('.exe' if platform.startswith('windows') else '.app.tar.gz')
        data = platform.encode()
        (root / name).write_bytes(data)
        signature = key.sign(hashlib.blake2b(data, digest_size=64).digest())
        packet = b'ED' + key_id + signature
        comment = 'test artifact'
        text = '\n'.join(['untrusted comment: test', base64.b64encode(packet).decode(),
                          'trusted comment: ' + comment,
                          base64.b64encode(key.sign(signature + comment.encode())).decode()])
        encoded = base64.b64encode(text.encode()).decode()
        (root / (name + '.sig')).write_text(encoded)
        (root / (platform + '.json')).write_text(json.dumps({
            'version': '0.9.0', 'platform': platform, 'signature': encoded,
            'url': 'https://github.com/yeogirlyun/rynmesh/releases/download/v0.9.0/' + name}))
    uploaded = {}
    notes = 'Release notes retained exactly.'

    def api(request, timeout):
        url = request.full_url
        if request.get_method() == 'POST':
            assert '/releases/99/assets?' in url
            name = uploader.urllib.parse.parse_qs(uploader.urllib.parse.urlsplit(url).query)['name'][0]
            uploaded[name] = request.data
            reply = {'digest': 'sha256:' + hashlib.sha256(request.data).hexdigest()}
        elif url.endswith('/actions/runs/77'):
            reply = {'head_sha': 'a' * 40, 'conclusion': 'success', 'path': '.github/workflows/desktop-update.yml'}
        elif url.endswith('/releases/99/assets'):
            reply = [{'name': 'SHA256SUMS', 'digest': 'sha256:python-package-checksum'}]
        elif url.endswith('/releases/99'):
            reply = {'draft': True, 'tag_name': 'v0.9.0', 'body': notes}
        else:
            pytest.fail('Unexpected API action: ' + url)
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(uploader.urllib.request, 'urlopen', api)
    uploader.main()
    assert len(uploaded) == 8
    assert 'SHA256SUMS' not in uploaded
    assert 'SHA256SUMS-desktop' in uploaded
    manifest = json.loads(uploaded['desktop-latest.json'])
    assert manifest['version'] == '0.9.0'
    assert manifest['notes'] == notes
    assert set(manifest['platforms']) == uploader.PLATFORMS
