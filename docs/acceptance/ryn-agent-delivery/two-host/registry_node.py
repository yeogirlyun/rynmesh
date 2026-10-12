"""Isolated production HTTP signaling registry, no inference or content relay claim."""
from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from node import Audit, code_manifest, now, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", type=int, default=18780)
    parser.add_argument("--allowed-peer", required=True, action="append")
    args = parser.parse_args()
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    for name in list(os.environ):
        if name.startswith("RYNMESH_"):
            del os.environ[name]
    os.environ["RYNMESH_REGISTRY_DIR"] = str(home)
    from rynmesh.registry_http import create_app
    app = create_app()
    audit = Audit(home / "two-host-registry-audit.jsonl")
    allowed = set(args.allowed_peer) | {"127.0.0.1", "::1"}
    @app.middleware("http")
    async def boundary(request: Request, call_next):
        peer = request.client.host if request.client else None
        if peer not in allowed:
            return JSONResponse({"detail": "test_network_boundary"}, status_code=403)
        response = await call_next(request)
        audit.add("registry_http", client=peer, method=request.method, path=request.url.path,
                  status=response.status_code)
        return response
    identity = {"kind": "two-host-isolated-production-signaling-registry", "hostname": socket.gethostname(),
                "pid": os.getpid(), "at": now(), "bind": args.bind, "port": args.port,
                "allowed_clients": sorted(allowed), "code": code_manifest(),
                "scope": "Signed production coordination plane. ICE content remains direct UDP; no relay configured."}
    write(home / ("identity-" + str(os.getpid()) + ".json"), identity)
    import uvicorn
    uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
