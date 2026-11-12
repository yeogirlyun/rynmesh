"""Stage verified code for the seven-plugin installer, or explicit company-only mode."""
import argparse
import hashlib
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

DEFAULT_ORIGIN = 'https://dashboard-hk-api.gshopper.com.cn'
DEFAULT_SERVICE_UPDATE_URL = DEFAULT_ORIGIN + '/ryn/updates/packs/company/latest.json'
DEFAULT_SERVICE_UPDATE_KEY_FILE = Path(__file__).resolve().parents[1] / 'deploy/windows/company-service-update.pub'


def default_updates():
    if not DEFAULT_SERVICE_UPDATE_KEY_FILE.is_file():
        return {}
    return {
        'service_url': DEFAULT_SERVICE_UPDATE_URL,
        'service_key': DEFAULT_SERVICE_UPDATE_KEY_FILE.read_text(encoding='utf-8').strip(),
    }


def prepare(output, company, company_source, office_compat_dir=None, *, company_only=False, updates=None):
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    company_manifest = json.loads((company / 'runtime-manifest.json').read_text(encoding='utf-8'))
    for name, digest in company_manifest['files'].items():
        if sha(company / name) != digest:
            raise ValueError('Company bundle mismatch: ' + name)
    shutil.copytree(company, output / 'company')
    if not company_only:
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
    declarations = company_declarations(company_source)
    (output / 'company-plugins.json').write_text(
        json.dumps(declarations, ensure_ascii=False, indent=2), encoding='utf-8')
    # Ship external skill assets separately from the service executable.
    write_company_skills(declarations['skills'], company_source, output / 'company-skills')
    write_update_sources(output, default_updates() if updates is None else updates)
    files = {}
    for path in output.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise ValueError('No links in distribution')
        if path.is_file():
            if path.suffix in {'.db', '.key'} or path.name.endswith('private.json') or path.name == '.env':
                raise ValueError('Private data in package')
            files[path.relative_to(output).as_posix()] = sha(path)
    (output / 'full-package.json').write_text(json.dumps({
        'schema': 1,
        'ryn_commit': revision,
        'company_commit': company_manifest['source_commit'],
        'plugins': list(COMPANY if company_only else COMPANY + OFFICE),
        'files': files,
    }, indent=2), encoding='utf-8')


def company_declarations(company_source):
    """The three company plugins' declarations as Ryn imports them (also shipped in update packs)."""
    source = json.loads((company_source / 'plugin-declarations.json').read_text(encoding='utf-8'))
    origin = DEFAULT_ORIGIN
    source = json.loads(json.dumps(source).replace('https://plugin.example.test', origin))
    plugins, skills, servers = [], [], []
    for slug in COMPANY:
        raw = source['plugins'][slug]
        plugin = {k: raw[k] for k in ('id', 'name', 'title', 'description', 'version', 'data_policy', 'peer_provider', 'desktop_routes') if k in raw}
        plugin.update(
            web_app={'url': 'http://127.0.0.1:18140/apps/' + slug,
                     'permissions': ['tools', 'models', 'messages', 'peer_provider', 'agent', 'downloads', 'dialogs']},
            identity_group={'id': 'gshopper', 'name': 'Gshopper', 'origin': origin},
            views=[], skill_ids=[], mcp_server_ids=[])
        for sid, record in source.get('skills', {}).items():
            if record.get('plugin_id') == slug:
                skills.append({k: record[k] for k in ('id', 'name', 'title', 'description', 'version', 'content', 'plugin_id') if k in record})
                plugin['skill_ids'].append(sid)
        for sid, record in source['mcp_servers'].items():
            if record.get('plugin_id') == slug:
                if (FINANCE_OAUTH_SERVERS.get(sid) == slug and record.get('url') == FINANCE_RESOURCE
                        and record['auth']['mode'] == 'oauth' and not record['auth'].get('client_id')):
                    record['auth']['client_id'] = 'ryn-desktop'
                servers.append({k: record[k] for k in ('id', 'name', 'description', 'transport', 'url', 'plugin_id', 'auth', 'data_policy', 'local_service') if k in record})
                plugin['mcp_server_ids'].append(sid)
        plugins.append(plugin)
    return {
        'plugins': plugins,
        'skills': skills,
        'mcp_servers': servers,
        'previous_defaults': source.get('previous_defaults', []),
        'retired_connections': source.get('retired_connections', []),
    }


def write_company_skills(skills, company_source, output_root):
    """Skill files next to SKILL.md; large files use the same verified-parts format as plugin ZIPs."""
    from rynmesh.agent_runtime.extensions.store import compute_directory_manifest
    for skill in skills:
        source_root = company_source / 'skills' / skill['id']
        if not source_root.is_dir():
            continue
        target_root = output_root / skill['id']
        target_root.mkdir(parents=True)
        (target_root / 'SKILL.md').write_text(skill['content'], encoding='utf-8')
        for resource in sorted(source_root.rglob('*')):
            if resource.is_symlink() or resource.is_junction():
                raise ValueError('Skill resource links are forbidden')
            relative = resource.relative_to(source_root)
            if not resource.is_file() or resource.name == 'SKILL.md' or '__pycache__' in relative.parts or resource.suffix == '.pyc':
                continue
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = resource.read_bytes()
            if len(raw) <= 512 * 1024:
                target.write_bytes(raw)
            else:
                names = []
                for i, start in enumerate(range(0, len(raw), 400 * 1024), 1):
                    part = target.with_name(target.name + '.part' + str(i))
                    part.write_bytes(raw[start:start + 400 * 1024])
                    names.append(part.name)
                target.with_name(target.name + '.parts.json').write_text(
                    json.dumps({'parts': names, 'sha256': hashlib.sha256(raw).hexdigest()}), encoding='utf-8')
        compute_directory_manifest(target_root)


def write_update_sources(output, updates):
    """Write the company plugin runtime's update source metadata (URL, public key,
    display name, included plugins, publisher and login provider)."""
    if not updates or not updates.get('service_url'):
        return
    if not updates.get('service_key'):
        raise ValueError('A service update source needs the publisher public key')
    source = {
        'name': updates.get('service_name') or {'zh-CN': '公司插件', 'en': 'Company plugins'},
        'plugins': list(COMPANY),
        'url': updates['service_url'],
        'public_key': updates['service_key'],
        'publisher': updates.get('service_publisher') or {'zh-CN': '公司服务器', 'en': 'the company server'},
        'auth': {
            'peer_provider': updates.get('service_auth') or 'company-workspace',
            'label': updates.get('service_auth_label') or {'zh-CN': '公司账号', 'en': 'company account'},
        },
    }
    from rynmesh.node_apps.service_updates import validate_source
    validate_source('company', source)
    (output / 'service-updates.json').write_text(
        json.dumps({'schema': 1, 'services': {'company': source}}, ensure_ascii=False, indent=2),
        encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--company', type=Path, required=True)
    parser.add_argument('--company-source', type=Path, required=True)
    parser.add_argument('--office-compat-dir', type=Path, help='Pinned offline archives named APP-web-VERSION-COMPAT_ID.zip')
    parser.add_argument('--company-only', action='store_true', help='Bundle only the three company plugins; leave existing Office apps untouched')
    parser.add_argument('--service-update-url', help='HTTPS latest.json of the signed company runtime pack')
    parser.add_argument('--service-update-key', help='Base64 Ed25519 public key of the company runtime publisher')
    parser.add_argument('--no-service-updates', action='store_true', help='Omit service-updates.json from this package')
    args = parser.parse_args()
    if args.no_service_updates:
        configured_updates = {}
    elif args.service_update_url or args.service_update_key:
        base = default_updates()
        configured_updates = {
            'service_url': args.service_update_url or base.get('service_url'),
            'service_key': args.service_update_key or base.get('service_key'),
        }
    else:
        configured_updates = None
    prepare(args.output, args.company, args.company_source, args.office_compat_dir,
            company_only=args.company_only, updates=configured_updates)
