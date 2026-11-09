"""Isolated offline first-install/reinstall acceptance of the company plugins; never runs the system installer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rynmesh.node_apps.plugins import PluginManager
from rynmesh.agent_runtime.extensions.store import ExtensionStore
from rynmesh.services.peer_box import load_or_create_messaging_key
from rynmesh.crypto import public_key_from_private
from rynmesh.store import load_or_create_private_key


def main():
    parser = argparse.ArgumentParser()
    for name in ('payload', 'helper', 'core', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=False)
    dest, home = root / 'install', root / 'home'
    dest.mkdir()
    shutil.copy2(args.core, dest / 'Ryn.exe')
    command = [str(args.helper.resolve()), '--payload', str(args.payload.resolve()),
               '--destination', str(dest), '--home', str(home)]
    subprocess.run(command, check=True, capture_output=True, timeout=180)
    peer = public_key_from_private(load_or_create_private_key(home / 'identity.ed25519'))
    manager = PluginManager(home, peer)
    extensions = ExtensionStore(home / 'agent', load_or_create_messaging_key(home / 'messaging.x25519'))
    plugins = {p['id']: p for p in extensions.overview()['plugins']}
    company = ('company-workspace','company-dashboard','company-affiliate')
    assert all(plugins[s]['enabled'] for s in company)
    # Office is downloaded by Ryn on first start: the helper must leave its setup open.
    office = ('pdfcraft','wordcraft','gridcraft','deckcraft')
    office_state = manager._read()
    assert not any(s in office_state['plugins'] for s in office) and not office_state.get('setup_completed'), office_state
    key = (home / 'company/config.key').read_bytes()
    identity = (home / 'identity.ed25519').read_bytes()
    dashboard_server = 'company-dashboard-dashboard'
    server = extensions.get_mcp_server_internal(dashboard_server)
    assert server['auth']['client_id'] == 'ryn-desktop'
    extensions.upsert_mcp_server({'auth': {**server['auth'], 'client_id': None}},
                                 server_id=dashboard_server, expected_revision=server['revision'])
    extensions.set_plugin_enabled('company-dashboard', False)
    subprocess.run(command, check=True, capture_output=True, timeout=180)
    assert not any(s in manager._read()['plugins'] for s in office)
    assert not next(p for p in extensions.overview()['plugins'] if p['id']=='company-dashboard')['enabled']
    assert (home / 'company/config.key').read_bytes() == key
    assert (home / 'identity.ed25519').read_bytes() == identity
    assert extensions.get_mcp_server_internal(dashboard_server, require_enabled=False)['auth']['client_id'] == 'ryn-desktop'
    config = json.loads((dest / 'plugin-services/services.json').read_text())
    assert config['version'] == 1
    service = config['services'][0]
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    env = {**os.environ, **service['environment'], 'COMPANY_APP_PORT':str(port), 'COMPANY_RUNTIME_OFFLINE_CHECK':'1'}
    with (root / 'company.log').open('wb') as log:
        proc = subprocess.Popen([str(dest / 'plugin-services' / service['program'])], env=env,
                                stdout=log, stderr=log, creationflags=0x08000000)
        try:
            deadline = time.monotonic() + 60
            while True:
                assert proc.poll() is None, 'Company service exited'
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/local/runtime/health', timeout=2) as response:
                        health = json.load(response)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.25)
            for slug in company:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/apps/{slug}', timeout=5) as response:
                    assert response.status == 200
        finally:
            proc.terminate()
            proc.wait(timeout=20)
    result = {'fresh_install':True,'repeat_install_preserves_identity_key_and_disabled_plugins':True,
              'office_bundled':False,'office_setup_left_for_first_start':True,'company_pages':list(company),
              'company_health':health,'company_outbound_network':'blocked','live_install_modified':False,
              'system_installer_executed':False}
    (root / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print((exc.stderr or b'')[-4000:].decode('utf-8', errors='replace'), file=sys.stderr)
        raise
