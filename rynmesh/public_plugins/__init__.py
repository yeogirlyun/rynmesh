"""Public plugin HTTP control; company/private installation and update paths stay separate."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .manager import PublicPlugins
from .packages import PluginError


def install_public_plugins(app, *, home, sites, local_control):
    manager = PublicPlugins(home, sites)
    app.state.public_plugins = manager
    app.router.on_shutdown.append(manager.close)

    async def call(request, fn):
        local_control(request)
        try:
            return await asyncio.to_thread(fn)
        except PluginError as exc:
            return JSONResponse({"code": str(exc)}, status_code=409 if str(exc) == "plugin_busy" else 400)

    @app.get("/api/local/public-plugins")
    async def overview(request: Request):
        return await call(request, manager.overview)

    @app.post("/api/local/public-plugins/refresh")
    async def refresh(request: Request):
        return await call(request, manager.refresh)

    @app.post("/api/local/public-plugins/{slug}/{action}")
    async def action(slug: str, action: str, request: Request):
        if action == "open":
            return await call(request, lambda: manager.open(slug))
        if action in {"install", "update"}:
            return await call(request, lambda: manager.install(slug))
        return await call(request, lambda: manager.action(slug, action))

    @app.api_route("/api/local/image-studio/{path:path}", methods=["GET", "POST"])
    async def legacy_studio_owner(path: str, request: Request):
        # Preserve the existing owner management screen and saved route.
        def invoke():
            status, headers, body = manager.owner("image-studio", request.method, "/" + path, b"")
            return Response(body, status_code=status, headers=dict(headers))
        return await call(request, invoke)

    return manager
