"""Isolated Windows friend-AI acceptance node; production APIs and real model.

Only the optional imported manifest is prepared here. No friendships, grants,
answers or task states are seeded. Existing assets are read-only references.
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--reuse-manifest', type=Path)
    parser.add_argument('--runtime-port', type=int, default=19043)
    parser.add_argument('--lan', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace))  # Never import a different editable checkout.
    if not home.is_relative_to(workspace / '.rynmesh'):
        raise SystemExit('Acceptance data must be inside this worktree/.rynmesh.')
    home.mkdir(parents=True, exist_ok=True)
    configure(home, args.port)
    if args.lan:
        from rynmesh.windows_app import local_address
        os.environ['RYNMESH_FRIEND_ENDPOINT'] = f'http://{local_address()}:{args.port}'
    os.environ['RYNMESH_LLM_HOME'] = str(home / 'llm')
    if args.reuse_manifest:
        target = home / 'llm/packages/local-small/manifest.json'
        if not target.exists():
            from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
            row = json.loads(args.reuse_manifest.read_text(encoding='utf-8'))
            key = secrets.token_urlsafe(32)
            command = row['runtime_command']
            command[command.index('--port') + 1] = str(args.runtime_port)
            command[command.index('--api-key') + 1] = key
            row.update(mode='import_gguf', model_owned=False, runtime_dir=str(home / 'llm'),
                       base_url=f'http://127.0.0.1:{args.runtime_port}', runtime_api_key=key)
            row['pricing'].update(input_per_1k=0, output_per_1k=0, minimum=0)
            save_manifest(LLMPackageManifest.from_dict(row), target)
        os.environ['RYNMESH_LLM_SERVICE_MANIFEST'] = str(target)
        os.environ['RYNMESH_LLAMA_SERVER'] = json.loads(target.read_text(encoding='utf-8'))['runtime_command'][0]
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=args.name))
    (home / '.acceptance-node.json').write_text(json.dumps({'pid': os.getpid(), 'port': args.port, 'name': args.name}), encoding='utf-8')
    uvicorn.run(app, host='0.0.0.0' if args.lan else '127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
