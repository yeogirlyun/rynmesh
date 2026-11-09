"""Stage only verified distributable code for the complete installer's company plugins.

Office editors are not staged: Ryn downloads them, with its pinned Chinese-font builds,
after installation."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from full_package_install import COMPANY, FINANCE_OAUTH_SERVERS, FINANCE_RESOURCE, sha


def prepare(output, company, company_source):
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    company_manifest = json.loads((company / 'runtime-manifest.json').read_text())
    for name, digest in company_manifest['files'].items():
        if sha(company / name) != digest:
            raise ValueError('Company bundle mismatch: ' + name)
    shutil.copytree(company, output / 'company')
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
        'company_commit':company_manifest['source_commit'],'plugins':list(COMPANY),'files':files}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--company', type=Path, required=True)
    parser.add_argument('--company-source', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output, args.company, args.company_source)
