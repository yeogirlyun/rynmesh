"""Owner-gated app controls and encrypted authenticated peer calls; no workers."""

import asyncio
import json

from fastapi import HTTPException, Request, Response

from ..crypto import canonical_json
from .engine import AppError
from .service import MAX_WIRE, PATH, NodeApps
from .store import AppStore


async def body(request, limit=65536):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, detail="apps_request_limit")
    try:
        result = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail="apps_invalid_request") from None


def install_node_apps(app, *, home, messaging_key, peer_id, friends, local_control):
    service = NodeApps(AppStore(home, messaging_key), peer_id, friends)
    app.state.node_apps = service

    async def call(request, fn, *args):
        local_control(request)
        try:
            return Response(
                json.dumps(await asyncio.to_thread(fn, *args)),
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        except AppError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(503, detail="apps_unavailable") from None

    @app.get("/api/local/node-apps")
    async def catalog(request: Request, provider_peer: str = ""):
        return await call(request, service.call, provider_peer, "catalog", {})

    @app.get("/api/local/node-apps/pending")
    async def pending(request: Request):
        return await call(request, service.pending)

    @app.post("/api/local/node-apps/resume")
    async def resume(request: Request):
        local_control(request)
        value = await body(request)
        if set(value) != {"operation_id"}:
            raise HTTPException(400, detail="apps_invalid_request")
        return await call(request, service.resume, value["operation_id"])

    @app.get("/api/local/node-apps/{app_id}/workspace")
    async def workspace(request: Request, app_id: str, provider_peer: str = ""):
        return await call(request, service.call, provider_peer, "view", {"app": app_id})

    @app.post("/api/local/node-apps/preview")
    async def preview(request: Request):
        local_control(request)
        value = await body(request)
        if set(value) != {"provider_peer", "app", "action", "arguments", "expected_context"}:
            raise HTTPException(400, detail="apps_invalid_request")
        return await call(
            request,
            service.preview,
            value["provider_peer"],
            value["app"],
            value["action"],
            value["arguments"],
            value["expected_context"],
        )

    @app.post("/api/local/node-apps/invoke")
    async def invoke(request: Request):
        local_control(request)
        value = await body(request)
        if set(value) != {
            "provider_peer",
            "app",
            "action",
            "arguments",
            "operation_id",
            "preview_hash",
        }:
            raise HTTPException(400, detail="apps_invalid_request")
        peer = value.pop("provider_peer")
        return await call(request, service.invoke, peer, value)

    @app.post("/api/local/node-apps/grants/preview")
    async def grant_preview(request: Request):
        local_control(request)
        value = await body(request)
        if set(value) != {"app", "consumer_peer", "allowed"}:
            raise HTTPException(400, detail="apps_invalid_request")
        return await call(
            request, service.grant_preview, value["app"], value["consumer_peer"], value["allowed"]
        )

    @app.post("/api/local/node-apps/grants")
    async def grant(request: Request):
        local_control(request)
        value = await body(request)
        if set(value) != {"app", "consumer_peer", "allowed", "preview_hash"}:
            raise HTTPException(400, detail="apps_invalid_request")
        return await call(
            request,
            service.grant,
            value["app"],
            value["consumer_peer"],
            value["allowed"],
            value["preview_hash"],
        )

    @app.post(PATH)
    async def peer(request: Request):
        wire = await body(request, MAX_WIRE)
        try:
            relation = await asyncio.to_thread(
                friends().verify_request,
                path=PATH,
                body=canonical_json(wire),
                headers=request.headers,
            )
            result = await asyncio.to_thread(service.respond, wire, relation)
            return Response(
                json.dumps(result),
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            raise HTTPException(403, detail="apps_request_rejected") from None

    return service
