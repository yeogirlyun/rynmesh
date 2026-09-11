"""Serve an isolated real node for manual two-browser device pairing checks.

Start twice with distinct homes and ports. No personal data is seeded, discovery
is disabled, and no model runs. This is loopback evidence, not cross-NAT or
packaged desktop acceptance. --reuse preserves identity for restart checks.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from accept_local_search import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--reuse', action='store_true')
    parser.add_argument('--seed', action='store_true', help='Seed synthetic saved/progress/history sources on a new node.')
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith('rynmesh-device-pairing-acceptance-') or home.exists() != args.reuse:
        raise SystemExit('Use a new rynmesh-device-pairing-acceptance-* directory, or explicitly --reuse it.')
    configure(home, args.port)
    os.environ.update(RYNMESH_DEVICE_ENDPOINT=f'http://127.0.0.1:{args.port}', RYNMESH_DEVICE_ALLOW_LOOPBACK='1')
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=args.name))
    if args.seed:
        if args.reuse:
            raise SystemExit('--seed requires a new node home.')
        item = {'item_id': 'device-transfer-article', 'title': 'Device transfer reading sample',
                'source_title': 'Synthetic acceptance source', 'link': 'https://example.test/device-transfer', 'content_kind': 'article'}
        app.state.consumption_store.record(item, 'bookmark')
        app.state.consumption_store.record(item, 'progress', progress=.65)
        stamp = '2026-09-11T00:00:00Z'
        app.state.ask_ryn.conversations.save({'id': 'device-transfer-conversation', 'title': 'Conversation from the other computer',
            'serviceKey': 'acceptance-provider::original-model', 'providerPeerId': 'acceptance-provider',
            'serviceName': 'Original acceptance model', 'networkId': 'rynmesh-main', 'createdAt': stamp, 'updatedAt': stamp,
            'messages': [{'id': 'original-answer', 'role': 'assistant', 'status': 'complete', 'createdAt': stamp,
                          'content': 'This history arrived through encrypted device transfer. 原服务绑定保留，没有调用模型。'}]}, expected_revision=0)
    app.state.first_run.store.dismiss()
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
