"""Standalone node-app service. No registry, timers, models or background workers."""

import argparse
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..friends.crypto import validate_endpoint
from ..friends.service import FriendService
from ..node_auth import COOKIE_NAME, NodeAuth, is_browser_cross_site
from ..peer_http import HttpPeerClient, webui_dir
from ..services import peer_box
from ..store import RynmeshStore
from .routes import body, install_node_apps


def create_host(home, endpoint, name="Ryn Apps", allow_loopback=False):
    home = Path(home)
    endpoint = validate_endpoint(endpoint, allow_loopback=allow_loopback)
    store = RynmeshStore(home=home, network_dir=home / "network", node_name=name)
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    auth = NodeAuth(home)
    auth.token()
    app = FastAPI(title="Ryn node apps")

    def guard(request):
        decision = auth.authorize(
            client_host=request.client.host if request.client else "",
            headers=request.headers,
            cookie=request.cookies.get(COOKIE_NAME, ""),
        )
        if not decision.allowed or is_browser_cross_site(request.headers):
            raise HTTPException(401, detail="local_control_unauthorized")

    def post(url, path, value, headers, **kwargs):
        validate_endpoint(url, allow_loopback=allow_loopback)
        return HttpPeerClient(url, timeout_s=10).post_json(
            path, value, headers=headers, max_bytes=kwargs.get("max_response_bytes", 512 * 1024)
        )

    friends = FriendService(
        home=home,
        peer_id=store.peer_id,
        node_name=name,
        endpoint=endpoint,
        identity_private=store.private_key_bytes,
        messaging_private=key,
        post_json=post,
        allow_loopback=allow_loopback,
    )
    app.state.node_store = store
    app.state.friend_service = friends
    app.state.owner_auth = auth
    install_node_apps(
        app,
        home=home,
        messaging_key=key,
        peer_id=store.peer_id,
        friends=lambda: friends,
        local_control=guard,
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except (ValueError, OSError):
            raise HTTPException(409, detail="friend_operation_unavailable") from None

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "node_name": name,
            "peer_id": store.peer_id,
            "role": "node-apps",
            "background_workers": False,
        }

    @app.get("/api/local/friends")
    async def friend_list(request: Request):
        guard(request)
        return {"friends": await call(friends.list_friends)}

    @app.post("/api/local/friends/invites")
    async def invite(request: Request):
        guard(request)
        value = await body(request, 4096)
        return await call(
            friends.create_invite,
            ttl_minutes=value.get("ttl_minutes", 15),
            reviewed_endpoint=value.get("reviewed_endpoint"),
        )

    @app.post("/api/local/friends/join")
    async def join(request: Request):
        guard(request)
        value = await body(request, 32768)
        return await call(friends.join, value.get("invite_uri", ""))

    @app.delete("/api/local/friends/{relationship_id}")
    async def revoke(request: Request, relationship_id: str):
        guard(request)
        return await call(friends.revoke, relationship_id)

    @app.post("/api/peer/friends/accept")
    async def accept(request: Request):
        return await call(friends.accept, await body(request, 32768))

    @app.post("/api/peer/friends/revoke")
    async def receive_revoke(request: Request):
        value = await body(request, 32768)
        await call(friends.receive_revoke, value)
        return friends.delivery_receipt("/api/peer/friends/revoke", value)

    ui = webui_dir()
    if (ui / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui / "assets"), name="assets")

    @app.get("/")
    @app.get("/apps")
    async def interface():
        if not (ui / "node-apps.html").is_file():
            raise HTTPException(503, detail="node_apps_ui_not_built")
        return FileResponse(ui / "node-apps.html")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--name", default="Ryn Apps")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--allow-loopback", action="store_true")
    args = parser.parse_args()
    import uvicorn

    if os.environ.get("RYNMESH_ALLOW_REMOTE_CONTROL"):
        raise SystemExit(
            "Remove RYNMESH_ALLOW_REMOTE_CONTROL: app host requires owner authentication."
        )
    app = create_host(args.home, args.endpoint, args.name, args.allow_loopback)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
