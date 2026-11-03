"""Offline provisioning for the explicit seven-plugin Windows installer.

Runs after the ordinary Ryn installer exits. Never downloads or copies user data.
"""
import argparse
import base64
import csv
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

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
    # mkdtemp on newer Python creates OWNER RIGHTS-only Windows ACLs. When an
    # elevated installer owns that directory, the normal desktop cannot read it.
    stage = target.parent / ('.full-install-' + uuid.uuid4().hex)
    stage.mkdir()
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        # Antivirus/indexers can hold a just-copied DLL open on Windows.
        # Publish atomically after the temporary handle closes; never overwrite
        # a version another installer has already published.
        for attempt in range(12):
            try:
                stage.rename(target)
                break
            except OSError as exc:
                transient = os.name == 'nt' and (isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in {5,32,33})
                if not transient or attempt == 11:
                    raise
                time.sleep(min(.05 * 2 ** attempt, 1.5))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def require_office_compat(cache, slug):
    from rynmesh.node_apps import craft_compat
    if slug in craft_compat.COMPAT and craft_compat.installed(cache, slug, current_only=True) is None:
        raise ValueError('Required Office compatibility build is missing: ' + slug)


def validate_office_payload(payload):
    from rynmesh.node_apps import craft_host
    for slug in OFFICE:
        require_office_compat(payload / 'office', slug)
        craft_host.installed(payload / 'office', slug)


def repair_office_files(source, target, names):
    """Replace only bundled program resources, retaining originals for rollback."""
    if target.is_symlink() or target.is_junction():
        raise ValueError('Linked Office version cannot be repaired')
    source, target = source.resolve(), target.resolve()
    for name in names:
        for base in (source, target):
            path = base / name
            if path.is_symlink() or path.is_junction():
                raise ValueError('Linked Office resource cannot be repaired')
            if not path.resolve().is_relative_to(base):
                raise ValueError('Office resource escapes its version directory')
    backup = target / '.full-repair-backups' / uuid.uuid4().hex
    stage, previous = backup / 'replacement', backup / 'previous'
    stage.mkdir(parents=True)
    previous.mkdir()
    # Finish copying before moving any existing resource. Preferences, exports,
    # other compatibility versions and plugin registration stay in place.
    for name in names:
        src, dst = source / name, stage / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    for name in names:
        old, saved = target / name, previous / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        if old.exists():
            old.rename(saved)
        old.parent.mkdir(parents=True, exist_ok=True)
        (stage / name).rename(old)


def provision_office(payload_cache, cache, slug):
    from rynmesh.node_apps import craft_host, craft_native, craft_compat
    # Verify before touching an existing installation; never fall back to fontless builds.
    require_office_compat(payload_cache, slug)
    craft_host.installed(payload_cache, slug)
    if not craft_native.runtime_status(payload_cache, slug)['verified']:
        raise ValueError('Office payload runtime not verified: ' + slug)
    version = craft_host.specification(slug)['tag']
    source, target = payload_cache / slug / version, cache / slug / version
    copy_new(source, target)
    if slug in craft_compat.COMPAT:
        identifier = craft_compat.COMPAT[slug]['id']
        # copy_new(target) intentionally preserves existing versions, so provision the
        # current compatibility version separately on every upgrade (including disabled apps).
        copy_new(source / 'compatibility' / identifier, target / 'compatibility' / identifier)
        try:
            require_office_compat(cache, slug)
        except (OSError, ValueError):
            repair_office_files(source, target, ('compatibility/' + identifier,))
            require_office_compat(cache, slug)
    try:
        craft_host.installed(cache, slug)
    except (OSError, ValueError):
        repair_office_files(source, target, ('payload', 'receipt.json'))
        craft_host.installed(cache, slug)
    copy_new(source / 'agent', target / 'agent')
    if not craft_native.runtime_status(cache, slug)['verified']:
        repair_office_files(source, target, ('agent',))
        if not craft_native.runtime_status(cache, slug)['verified']:
            raise ValueError('Office runtime not verified: ' + slug)


def allow_desktop_user(path):
    if os.name != 'nt':
        return
    row = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
    sid = next(csv.reader(row.strip().splitlines()))[-1]
    if not sid.startswith('S-1-') or not all(part.isdigit() for part in sid[2:].split('-')):
        raise ValueError('Unable to determine installer user SID')
    subprocess.run(['icacls', str(path), '/grant', '*' + sid + ':(OI)(CI)F', '/T', '/Q'],
                   check=True, capture_output=True, timeout=120, creationflags=0x08000000)


def prepare_upgrade(payload, destination):
    print('stage: verify-package', flush=True)
    verify(Path(payload).resolve())  # Never uninstall before payload verification.
    print('stage: verify-office', flush=True)
    validate_office_payload(Path(payload).resolve())
    destination = Path(destination).resolve()
    if destination == Path(destination.anchor) or destination == Path.home().resolve():
        raise ValueError('Unsafe installation directory')
    old = destination / 'Ryn.exe'
    if not old.is_file():
        return
    backup = destination.parent / 'Ryn-install-backups' / uuid.uuid4().hex
    print('stage: backup-program', flush=True)
    backup.mkdir(parents=True)
    for name in ('Ryn.exe', 'rynmesh-peer.exe', 'ryn_computer_use.exe', 'uninstall.exe'):
        if (destination / name).is_file():
            shutil.copy2(destination / name, backup / name)
    # Match full executable paths. Never terminate another Ryn installation or
    # unrelated service merely because its process name is the same.
    encoded_root = base64.b64encode(str(destination).encode('utf-8')).decode('ascii')
    command = r'''$ErrorActionPreference='Stop';
$root=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ROOT__'));
$prefix=$root.TrimEnd('\')+'\';
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and
  ($_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) -and
  (($_.Name -in @('Ryn.exe','rynmesh-peer.exe','ryn_computer_use.exe','company-service.exe')) -or
   $_.ExecutablePath.StartsWith($prefix+'plugins\',[StringComparison]::OrdinalIgnoreCase) -or
   $_.ExecutablePath.StartsWith($prefix+'plugin-services\',[StringComparison]::OrdinalIgnoreCase))
} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue};
Start-Sleep -Seconds 2;
'''.replace('__ROOT__', encoded_root)
    print('stage: stop-owned-processes', flush=True)
    subprocess.run(['powershell.exe', '-NoProfile', '-EncodedCommand',
                    base64.b64encode(command.encode('utf-16le')).decode('ascii')],
                   check=True, capture_output=True, timeout=40, creationflags=0x08000000)
    uninstaller = destination / 'uninstall.exe'
    if uninstaller.is_file():
        print('stage: uninstall-update-mode', flush=True)
        # Tauri's update mode explicitly preserves app data, startup preferences
        # and editor registrations. _?= keeps this process synchronous.
        subprocess.run([str(uninstaller), '/S', '/UPDATE', '_?=' + str(destination)],
                       check=True, timeout=120, creationflags=0x08000000)
    print(json.dumps({'upgrade_backup': str(backup), 'user_data_preserved': True}))


def configure_local_mcp_services(store, declarations, port):
    """Migrate only declared stock endpoints; never override customized URLs/auth."""
    managed = []
    for declaration in declarations.get('mcp_servers', []):
        local = declaration.get('local_service') or {}
        path = local.get('path', '')
        if local.get('id') != 'company' or not path.startswith('/') or any(c in path for c in '?#\\'):
            continue
        endpoint = f'http://127.0.0.1:{port}{path}'
        try:
            server = store.get_mcp_server_internal(declaration['id'])
        except Exception:
            continue
        if server.get('plugin_id') != declaration.get('plugin_id'):
            continue
        current = server.get('url')
        # Any static bearer/auth customization is retained for owner review.
        headers = server.get('secret_headers') or {}
        if any(k.lower() == 'authorization' for k in headers) or server.get('bearer_token_env'):
            continue
        stock = set(local.get('legacy_urls', [])) | {declaration.get('url')}
        if current != endpoint:
            if current not in stock:
                continue
            store.upsert_mcp_server({'url': endpoint, 'auth': declaration.get('auth') or {}},
                                    server_id=server['id'], expected_revision=server['revision'])
        managed.append(server['id'])
    return managed


def install(payload, destination, home):
    from rynmesh.crypto import public_key_from_private
    from rynmesh.store import load_or_create_private_key
    from rynmesh.services.peer_box import load_or_create_messaging_key
    from rynmesh.node_apps import craft_host
    from rynmesh.node_apps.plugins import PluginManager
    from rynmesh.agent_runtime.extensions.store import ExtensionStore
    from rynmesh.file_transactions import file_transaction

    payload, destination, home = map(lambda p: Path(p).resolve(), (payload, destination, home))
    manifest = verify(payload)
    if not (destination / 'Ryn.exe').is_file():
        raise ValueError('Install Ryn core first')
    validate_office_payload(payload)
    home.mkdir(parents=True, exist_ok=True)
    peer = public_key_from_private(load_or_create_private_key(home / 'identity.ed25519'))
    manager = PluginManager(home, peer)
    def no_download(*args, **kwargs):
        raise ValueError('Full installer must not access the network')
    manager.download = no_download
    for slug in OFFICE:
        provision_office(payload / 'office', manager.cache, slug)
        # Preserve an existing user's version, enable/disable state and preferences.
        if slug not in manager._read()['plugins']:
            manager.install(slug)
    manager.finish_setup()
    allow_desktop_user(manager.cache)

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
        allow_desktop_user(target)
        runtime_manifest = json.loads((payload / 'company/runtime-manifest.json').read_text(encoding='utf-8'))
        for name, digest in runtime_manifest['files'].items():
            if sha(target / name) != digest:
                raise ValueError('Installed company runtime integrity failure: ' + name)
        env = dict(os.environ)
        env.update(COMPANY_APP_HOME=str(company_home), COMPANY_APP_PORT=str(port), COMPANY_RUNTIME_OFFLINE_CHECK='1')
        # A second frozen application must initialize its own bootloader environment.
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        with (home / 'full-package-company-init.log').open('wb') as log:
            subprocess.run([str(target / 'company-service.exe'), '--initialize'], env=env,
                           stdout=log, stderr=log, check=True, timeout=180,
                           creationflags=0x08000000 if os.name == 'nt' else 0)
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
    managed_servers = configure_local_mcp_services(store, declarations, port)
    with file_transaction(services_root / '.install.lock'):
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        for service in config['services']:
            if service['id'] == 'company':
                service['mcp_servers'] = managed_servers
        atomic_json(config_path, config)
    receipt = {'schema': 1, 'ryn_commit': manifest['ryn_commit'], 'company_commit': revision,
               'plugins': list(COMPANY + OFFICE), 'offline': True, 'installation': str(destination)}
    atomic_json(home / 'full-package-install.json', receipt)
    print(json.dumps(receipt))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('RYNMESH_HOME', str(Path.home() / '.rynmesh'))))
    parser.add_argument('--prepare-upgrade', action='store_true')
    parser.add_argument('--log', type=Path)
    args = parser.parse_args(argv)
    with contextlib.ExitStack() as stack:
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            log = stack.enter_context(args.log.open('a', encoding='utf-8', buffering=1))
            stack.enter_context(contextlib.redirect_stdout(log))
            stack.enter_context(contextlib.redirect_stderr(log))
        print('operation: ' + ('prepare-upgrade' if args.prepare_upgrade else 'install'), flush=True)
        try:
            if args.prepare_upgrade:
                prepare_upgrade(args.payload, args.destination)
            else:
                install(args.payload, args.destination, args.home)
        except Exception as exc:
            traceback.print_exc()
            if isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
                for name in ('stdout', 'stderr'):
                    value = getattr(exc, name, None)
                    if value:
                        print(name + ':', value.decode(errors='replace') if isinstance(value, bytes) else value)
            return 1
        print('operation: complete', flush=True)
    return 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
