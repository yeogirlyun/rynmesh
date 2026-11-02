"""Verify CI desktop bundles and upload to an existing draft without creating a tag."""
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from prepare_desktop_update import PLATFORMS, assemble


def main():
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'yeogirlyun/rynmesh':
        raise ValueError('Official repository required')
    release_id = int(os.environ['RELEASE_ID'])
    run_id = int(os.environ['SOURCE_RUN_ID'])
    expected_sha = os.environ['SOURCE_SHA']
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
               'Accept': 'application/vnd.github+json', 'User-Agent': 'ryn-release'}
    base = f'https://api.github.com/repos/{repository}/'

    def get(route):
        with urllib.request.urlopen(urllib.request.Request(base + route, headers=headers), timeout=60) as response:
            return json.load(response)

    run = get(f'actions/runs/{run_id}')
    if run['head_sha'] != expected_sha or run['conclusion'] != 'success' or run['path'] != '.github/workflows/desktop-update.yml':
        raise ValueError('Expected successful desktop build at the exact source commit')
    release = get(f'releases/{release_id}')
    if not release['draft'] or release['tag_name'] != 'v0.8.0':
        raise ValueError('Expected existing v0.8.0 draft; published releases cannot be changed')
    root = Path('update-assets')
    public = base64.b64decode(base64.b64decode(os.environ['RYN_DESKTOP_UPDATE_PUBLIC_KEY']).decode().splitlines()[1])
    if len(public) != 42:
        raise ValueError('Invalid update public key')
    verifier = Ed25519PublicKey.from_public_bytes(public[10:])
    files = []
    for platform in sorted(PLATFORMS):
        row = json.loads((root / (platform + '.json')).read_text())
        filename = 'Ryn-0.8.0-' + platform + ('.exe' if platform.startswith('windows') else '.app.tar.gz')
        if row['version'] != '0.8.0' or row['platform'] != platform or row['url'] != f'https://github.com/{repository}/releases/download/v0.8.0/{filename}':
            raise ValueError('Invalid platform metadata')
        binary = root / filename
        sig_path = root / (filename + '.sig')
        if sig_path.read_text().strip() != row['signature']:
            raise ValueError('Detached signature differs from metadata')
        lines = base64.b64decode(row['signature']).decode().splitlines()
        packet = base64.b64decode(lines[1])
        if len(packet) != 74 or packet[:2] != b'ED' or packet[2:10] != public[2:10]:
            raise ValueError('Invalid signature algorithm or key ID')
        with binary.open('rb') as source:
            message = hashlib.file_digest(source, lambda: hashlib.blake2b(digest_size=64)).digest()
        verifier.verify(packet[10:], message)
        if not lines[2].startswith('trusted comment: '):
            raise ValueError('Missing trusted comment')
        verifier.verify(base64.b64decode(lines[3]), packet[10:] + lines[2][17:].encode())
        print('Verified update signature:', filename, flush=True)
        files.extend([binary, sig_path])
    assemble(root, '0.8.0', 'Ryn 0.8.0 current workspace snapshot.')
    files.append(root / 'desktop-latest.json')

    def sha256(path):
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()

    checksums = root / 'SHA256SUMS'
    checksums.write_text(''.join(sha256(path) + '  ' + path.name + '\n' for path in sorted(files)))
    files.append(checksums)
    existing = {asset['name']: asset for asset in get(f'releases/{release_id}/assets')}
    for path in files:
        digest = 'sha256:' + sha256(path)
        if path.name in existing:
            if existing[path.name].get('digest') != digest:
                raise ValueError('Existing asset differs: ' + path.name)
            continue
        endpoint = f'https://uploads.github.com/repos/{repository}/releases/{release_id}/assets?name=' + urllib.parse.quote(path.name)
        request = urllib.request.Request(endpoint, data=path.read_bytes(),
            headers={**headers, 'Content-Type': 'application/octet-stream'}, method='POST')
        with urllib.request.urlopen(request, timeout=300) as response:
            uploaded = json.load(response)
        if uploaded.get('digest') != digest:
            raise ValueError('Uploaded digest mismatch: ' + path.name)
        print('Uploaded and SHA-256 verified:', path.name, flush=True)
    print('Draft complete. No tag was created and no release was published.', flush=True)


if __name__ == '__main__':
    main()
