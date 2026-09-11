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
    app.state.first_run.store.dismiss()
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
