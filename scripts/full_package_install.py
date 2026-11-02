"""Offline provisioning for the explicit seven-plugin Windows installer.

Runs after the ordinary Ryn installer exits. Never downloads or copies user data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

OFFICE = ('pdfcraft', 'wordcraft', 'gridcraft', 'deckcraft')
COMPANY = ('company-workspace', 'company-dashboard', 'company-affiliate')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(root):
    manifest = json.loads((root / 'full-package.json').read_text(encoding='utf-8'))
    files = manifest['files']
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Package links are forbidden')
        if path.is_file() and path.name != 'full-package.json':
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError('Package inventory differs')
    for name, expected in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or sha(path) != expected:
            raise ValueError('Package integrity failure: ' + name)
    return manifest


def atomic_json(path, value):
    from rynmesh.atomic_io import atomic_write_json
    atomic_write_json(path, value)


def copy_new(source, target):
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.full-install-', dir=target.parent))
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def install(payload, destination, home):
    from rynmesh.crypto import public_key_from_private
    from rynmesh.store import load_or_create_private_key
    from rynmesh.services.peer_box import load_or_create_messaging_key
    from rynmesh.node_apps import craft_host, craft_native
    from rynmesh.node_apps.plugins import PluginManager
    from rynmesh.agent_runtime.extensions.store import ExtensionStore
    from rynmesh.file_transactions import file_transaction

    payload, destination, home = map(lambda p: Path(p).resolve(), (payload, destination, home))
    manifest = verify(payload)
    if not (destination / 'Ryn.exe').is_file():
        raise ValueError('Install Ryn core first')
    home.mkdir(parents=True, exist_ok=True)
    peer = public_key_from_private(load_or_create_private_key(home / 'identity.ed25519'))
    manager = PluginManager(home, peer)
    def no_download(*args, **kwargs):
        raise ValueError('Full installer must not access the network')
    manager.download = no_download
    for slug in OFFICE:
        version = craft_host.specification(slug)['tag']
        source = payload / 'office' / slug / version
        target = manager.cache / slug / version
        copy_new(source, target)
        craft_host.installed(manager.cache, slug)
        if not (target / 'agent').exists():
            copy_new(source / 'agent', target / 'agent')
        if not craft_native.runtime_status(manager.cache, slug)['verified']:
            raise ValueError('Office runtime not verified: ' + slug)
        # Preserve an existing user's version, enable/disable state and preferences.
        if slug not in manager._read()['plugins']:
            manager.install(slug)
    manager.finish_setup()

    services_root = destination / 'plugin-services'
    config_path = services_root / 'services.json'
    services_root.mkdir(exist_ok=True)
    with file_transaction(services_root / '.install.lock'):
        config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {'version': 1, 'services': []}
        previous = next((s for s in config['services'] if s['id'] == 'company'), None)
        port = previous['port'] if previous else 18140
        company_home = Path(previous['environment']['COMPANY_APP_HOME']) if previous else home / 'company'
        company_home.mkdir(parents=True, exist_ok=True)
        revision = manifest['company_commit']
        version = 'company-' + revision[:16]
        target = services_root / version
        copy_new(payload / 'company', target)
        env = dict(os.environ)
        env.update(COMPANY_APP_HOME=str(company_home), COMPANY_APP_PORT=str(port), COMPANY_RUNTIME_OFFLINE_CHECK='1')
        subprocess.run([str(target / 'company-service.exe'), '--initialize'], env=env,
                       check=True, timeout=120, creationflags=0x08000000 if os.name == 'nt' else 0)
        environment = dict(previous.get('environment', {})) if previous else {}
        environment.update(COMPANY_APP_HOME=str(company_home), COMPANY_APP_PORT=str(port))
        service = {'id': 'company', 'plugins': list(COMPANY), 'program': version + '/company-service.exe',
                   'port': port, 'health_path': '/api/local/runtime/health', 'environment': environment}
        config['services'] = [s for s in config['services'] if s['id'] != 'company'] + [service]
        if config_path.exists():
            shutil.copy2(config_path, config_path.with_name('services.before-full-install.json'))
        atomic_json(config_path, config)

    store = ExtensionStore(home / 'agent', load_or_create_messaging_key(home / 'messaging.x25519'))
    declarations = json.loads((payload / 'company-plugins.json').read_text(encoding='utf-8'))
    existing = {p['id'] for p in store.overview()['plugins']}
    missing = set(COMPANY) - existing
    plugins = [p for p in declarations['plugins'] if p['id'] in missing]
    for plugin in plugins:
        plugin['web_app']['url'] = f"http://127.0.0.1:{port}/apps/{plugin['id']}"
    store.commit_imported_items(plugins=plugins,
        skills=[s for s in declarations['skills'] if s.get('plugin_id') in missing],
        mcp_servers=[s for s in declarations['mcp_servers'] if s.get('plugin_id') in missing])
    for slug in sorted(missing):
        store.set_plugin_enabled(slug, True)
    receipt = {'schema': 1, 'ryn_commit': manifest['ryn_commit'], 'company_commit': revision,
               'plugins': list(COMPANY + OFFICE), 'offline': True, 'installation': str(destination)}
    atomic_json(home / 'full-package-install.json', receipt)
    print(json.dumps(receipt))


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('RYNMESH_HOME', str(Path.home() / '.rynmesh'))))
    args = parser.parse_args()
    install(args.payload, args.destination, args.home)
