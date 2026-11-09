"""Stage only verified distributable code for a seven-plugin installer."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from full_package_install import (
    COMPANY,
    FINANCE_OAUTH_SERVERS,
    FINANCE_RESOURCE,
    OFFICE,
    require_office_compat,
    sha,
)

from rynmesh.node_apps import craft_compat, craft_host, craft_native


def prepare(output, company, company_source, office_compat_dir=None):
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    company_manifest = json.loads((company / 'runtime-manifest.json').read_text())
    for name, digest in company_manifest['files'].items():
        if sha(company / name) != digest:
            raise ValueError('Company bundle mismatch: ' + name)
    shutil.copytree(company, output / 'company')
    cache_base = Path(os.environ['LOCALAPPDATA']) / 'Ryn/plugins'
    bases = [cache_base, Path.home() / '.rynmesh/plugins',
             Path(os.environ['LOCALAPPDATA']) / 'Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn/plugins']
    caches = [p for base in bases if base.is_dir() for p in base.iterdir() if p.is_dir()]
    for slug in OFFICE:
        version = craft_host.specification(slug)['tag']
        found = None
        fallback = None
        for cache in caches:
            try:
                craft_host.installed(cache, slug)
                if fallback is None or craft_native.runtime_status(cache, slug)["verified"]:
                    fallback = cache
                require_office_compat(cache, slug)
                if found is None:
                    found = cache
                if craft_native.runtime_status(cache, slug)['verified']:
                    found = cache
                    if slug not in craft_compat.COMPAT or craft_compat.installed(cache, slug):
                        break
            except (OSError, ValueError):
                pass
        # Reuse a verified upstream cache when an explicit new archive is supplied;
        # the mandatory check below still rejects missing compatibility builds.
        found = found or fallback
        if found:
            root = found / slug / version
            target = output / 'office' / slug / version
            # Explicit allowlist excludes document exports, preferences and caches.
            target.mkdir(parents=True)
            for name in ('payload', 'agent', 'compatibility'):
                if (root / name).exists():
                    shutil.copytree(root / name, target / name)
            shutil.copy2(root / 'receipt.json', target / 'receipt.json')
        else:
            craft_host.install(output / 'office', slug)
        if slug in craft_compat.COMPAT and office_compat_dir is not None:
            archive = office_compat_dir / (slug + '-web-' + version.removeprefix('v') + '-' + craft_compat.COMPAT[slug]['id'] + '.zip')
            if archive.is_file():
                craft_compat.install(output / 'office', slug, archive)
        require_office_compat(output / 'office', slug)
        if not craft_native.runtime_status(output / 'office', slug)['verified']:
            craft_native.install_runtime(output / 'office', slug)
        craft_host.installed(output / 'office', slug)
        assert craft_native.runtime_status(output / 'office', slug)['verified']
        print('Staged', slug, flush=True)
    source = json.loads((company_source / 'plugin-declarations.json').read_text(encoding='utf-8'))
    origin = 'https://dashboard-hk-api.gshopper.com.cn'
    source = json.loads(json.dumps(source).replace('https://plugin.example.test', origin))
    plugins, skills, servers = [], [], []
    for slug in COMPANY:
        raw = source['plugins'][slug]
        plugin = {k: raw[k] for k in ('id','name','title','description','version','data_policy','peer_provider','desktop_routes') if k in raw}
        plugin.update(web_app={'url': 'http://127.0.0.1:18140/apps/' + slug,
                              'permissions': ['tools','models','messages','peer_provider','agent','downloads','dialogs']},
                      identity_group={'id':'gshopper','name':'Gshopper','origin':origin},
                      views=[], skill_ids=[], mcp_server_ids=[])
        for sid, record in source.get('skills', {}).items():
            if record.get('plugin_id') == slug:
                skills.append({k: record[k] for k in ('id','name','title','description','version','content','plugin_id') if k in record})
                plugin['skill_ids'].append(sid)
        for sid, record in source['mcp_servers'].items():
            if record.get('plugin_id') == slug:
                if (FINANCE_OAUTH_SERVERS.get(sid) == slug and record.get('url') == FINANCE_RESOURCE
                        and record['auth']['mode'] == 'oauth' and not record['auth'].get('client_id')):
                    record['auth']['client_id'] = 'ryn-desktop'
                servers.append({k: record[k] for k in ('id','name','description','transport','url','plugin_id','auth','data_policy','local_service') if k in record})
                plugin['mcp_server_ids'].append(sid)
        plugins.append(plugin)
    (output / 'company-plugins.json').write_text(json.dumps({'plugins':plugins,'skills':skills,'mcp_servers':servers,
        'previous_defaults': source.get('previous_defaults', []),
        'retired_connections': source.get('retired_connections', [])}, ensure_ascii=False, indent=2), encoding='utf-8')
    files = {}
    for path in output.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise ValueError('No links in distribution')
        if path.is_file():
            if path.suffix in {'.db','.key'} or path.name.endswith('private.json') or path.name == '.env':
                raise ValueError('Private data in package')
            files[path.relative_to(output).as_posix()] = sha(path)
    (output / 'full-package.json').write_text(json.dumps({'schema':1,'ryn_commit':revision,
        'company_commit':company_manifest['source_commit'],'plugins':list(COMPANY+OFFICE),'files':files}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--company', type=Path, required=True)
    parser.add_argument('--company-source', type=Path, required=True)
    parser.add_argument('--office-compat-dir', type=Path, help='Pinned offline archives named APP-web-VERSION-COMPAT_ID.zip')
    args = parser.parse_args()
    prepare(args.output, args.company, args.company_source, args.office_compat_dir)
