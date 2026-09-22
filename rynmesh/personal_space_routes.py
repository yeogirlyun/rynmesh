"""Private local-control endpoints; membership traffic uses the existing mailbox."""

import asyncio

from fastapi import HTTPException, Request

from .personal_space import SpaceError


def install_space_routes(app, *, space, local_control):
    @app.get("/api/local/space")
    def status(request: Request):
        local_control(request)
        return space.status()

    @app.post("/api/local/space/{action}")
    async def action(action: str, request: Request):
        local_control(request)
        raw = await request.body()
        if len(raw) > 40000:
            raise HTTPException(413, "Space request is too large.")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise SpaceError("Expected an object.")
            if action == "create":
                return space.create(body.get("name"))
            if action == "join":
                return space.join(body.get("invitation"), body.get("name"))
            if action == "policy":
                return space.set_policy(body.get("access"))
            if action == "leave":
                return space.leave()
            if action == "sync":
                await asyncio.to_thread(space.tick)
                return space.status()
            if action == "backup":
                return await asyncio.to_thread(space.backup, body.get("password"))
            if action in {"invite", "remove", "role", "cancel_invite"}:
                return space.act(action, body)
            raise SpaceError("Unknown space action.")
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                400, str(exc) if isinstance(exc, SpaceError) else "Invalid space request."
            ) from exc
