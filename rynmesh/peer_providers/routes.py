"""Owner-only control surface; no credential or OAuth callback data in responses."""
from __future__ import annotations

from fastapi import HTTPException, Request

from .validation import PeerProviderError


def install_peer_provider_routes(app, service, local_control):
    app.state.peer_providers = service

    async def run(request, action):
        local_control(request)
        try:
            result = action()
            if hasattr(result, "__await__"):
                result = await result
            return result
        except PeerProviderError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(503, "peer_provider_unavailable") from None

    async def body(request):
        raw = await request.body()
        if len(raw) > 32768:
            raise PeerProviderError("peer_provider_request_too_large")
        try:
            value = __import__("json").loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except Exception:
            raise PeerProviderError("peer_provider_invalid_request") from None

    @app.get("/api/local/peer-providers")
    async def list_providers(request: Request):
        return await run(request, lambda: {"providers": service.status()})

    @app.get("/api/local/peer-providers/{plugin_id}/status")
    async def status(plugin_id: str, request: Request):
        return await run(request, lambda: service.status(plugin_id))

    @app.post("/api/local/peer-providers/{plugin_id}/login/start")
    async def login(plugin_id: str, request: Request):
        return await run(request, lambda: service.start_login(plugin_id))

    @app.post("/api/local/peer-providers/{plugin_id}/logout")
    async def logout(plugin_id: str, request: Request):
        return await run(request, lambda: service.logout(plugin_id))

    @app.post("/api/local/peer-providers/{plugin_id}/bind")
    async def bind(plugin_id: str, request: Request):
        async def action():
            value = await body(request)
            if set(value) - {"name", "endpoints"}:
                raise PeerProviderError("peer_provider_invalid_request")
            return await service.bind(plugin_id, **value)
        return await run(request, action)

    @app.get("/api/local/peer-providers/{plugin_id}/catalog")
    @app.post("/api/local/peer-providers/{plugin_id}/refresh")
    async def catalog(plugin_id: str, request: Request):
        async def action():
            try:
                await service.auto_sync(plugin_id)
            except Exception:
                pass
            return await service.directory(plugin_id)
        return await run(request, action)

    @app.get("/api/local/peer-providers/{plugin_id}/workspace")
    @app.post("/api/local/peer-providers/{plugin_id}/auto-sync")
    async def workspace(plugin_id: str, request: Request):
        return await run(request, lambda: service.workspace(plugin_id))

    @app.get("/api/local/peer-providers/{plugin_id}/models")
    async def models(plugin_id: str, request: Request):
        def action():
            service.status(plugin_id)
            return {"models": [{"service_id": m["service"]["package_id"], "name": m["service"].get("model_alias", m["service"]["package_id"]), "model_binding": m["model_binding"]} for m in service.available_models()]}
        return await run(request, action)

    @app.post("/api/local/peer-providers/{plugin_id}/publish")
    async def publish(plugin_id: str, request: Request):
        async def action():
            value = await body(request)
            if set(value) != {"service_ids"}:
                raise PeerProviderError("peer_provider_invalid_request")
            return await service.publish(plugin_id, value["service_ids"])
        return await run(request, action)
