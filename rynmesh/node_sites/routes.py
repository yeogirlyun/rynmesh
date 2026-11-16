"""HTTP surface: peer routes for hosting nodes, owner routes for the local UI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..drop.engine import DropEngine
from .client import SiteClient
from .gateway import SiteGateway
from .host import SiteHost
from .protocol import LIST_PATH, MAX_LIST_WIRE, MAX_REQUEST_WIRE, REQUEST_PATH, SiteError
from .store import SiteStore


class NodeSites:
    def __init__(self, engine: DropEngine, home: Path, *, gateway_port: int | None = None) -> None:
        self.owner_paths: dict[str, str] = {}
        self.store = SiteStore(home)
        self.host = SiteHost(engine, self.store)
        self.client = SiteClient(engine, self.store)
        self.gateway = SiteGateway(self.client, port=gateway_port)
        self.engine = engine

    def start(self) -> "NodeSites":
        self.gateway.start()
        return self

    def close(self) -> None:
        self.gateway.close()

    def members(self) -> list[dict[str, Any]]:
        """Distinct provider members, for the owner's audience picker."""
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.engine.provider_members():
            key = (str(row.get("provider_resource") or ""), str(row.get("member_id") or ""))
            if key[1] and key not in seen:
                seen[key] = {"provider_resource": key[0], "member_id": key[1],
                             "member_name": str(row.get("member_name") or "")}
        return sorted(seen.values(), key=lambda m: (m["member_name"], m["member_id"]))


def _error(exc: SiteError) -> JSONResponse:
    return JSONResponse({"detail": exc.code, "code": exc.code}, status_code=exc.status)


async def _body(request: Request, limit: int) -> Any:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise SiteError("site_too_large", 413)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SiteError("site_bad_request") from None


def install_node_sites(app: FastAPI, *, engine: DropEngine, home: Path,
                       local_control: Callable[[Request], None], start: bool = True,
                       gateway_port: int | None = None) -> NodeSites:
    sites = NodeSites(engine, home, gateway_port=gateway_port)
    app.state.node_sites = sites

    async def run(fn: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except SiteError as exc:
            return _error(exc)

    # ---- peer protocol (no local control: the envelope is the credential) ----------
    @app.post(REQUEST_PATH)
    async def site_peer_request(request: Request):
        try:
            envelope = await _body(request, MAX_REQUEST_WIRE)
        except SiteError as exc:
            return _error(exc)
        return await run(lambda: sites.host.handle_request(envelope))

    @app.post(LIST_PATH)
    async def site_peer_list(request: Request):
        try:
            envelope = await _body(request, MAX_LIST_WIRE)
        except SiteError as exc:
            return _error(exc)
        return await run(lambda: sites.host.handle_list(envelope))

    # ---- owner: sites this node hosts ---------------------------------------------
    @app.get("/api/local/node-sites/mine")
    async def node_sites_mine(request: Request):
        local_control(request)
        return {"sites": [{**SiteStore.public(sid, row), "owner_path": sites.owner_paths.get(sid),
                            "available": not row.get("builtin") or sid in sites.host.builtins}
                           for sid, row in sorted(sites.store.sites().items())],
                "plugins": getattr(app.state, "site_plugins", {}),
                "members": await asyncio.to_thread(sites.members)}

    @app.post("/api/local/node-sites/mine")
    async def node_sites_register(request: Request):
        local_control(request)
        try:
            value = await _body(request, 8192)
            if not isinstance(value, dict) or set(value) - {"site_id", "title", "port", "entry"}:
                raise SiteError("site_bad_request")
        except SiteError as exc:
            return _error(exc)

        def register():
            site, token = sites.store.register(sid=value.get("site_id"), title=value.get("title"),
                                               port=value.get("port"), entry=value.get("entry", "/"))
            sites.host.revoke_cache()
            # The token is shown once; the site backend must require it on every request.
            return {"site": site, "token": token}
        return await run(register)

    @app.patch("/api/local/node-sites/mine/{site_id}")
    async def node_sites_update(site_id: str, request: Request):
        local_control(request)
        try:
            value = await _body(request, 64 * 1024)
        except SiteError as exc:
            return _error(exc)

        def update():
            site = sites.store.update(site_id, value)
            sites.host.revoke_cache()
            return {"site": site}
        return await run(update)

    @app.delete("/api/local/node-sites/mine/{site_id}")
    async def node_sites_remove(site_id: str, request: Request):
        local_control(request)

        def remove():
            removed = sites.store.remove(site_id)
            sites.host.revoke_cache()
            return {"removed": removed}
        return await run(remove)

    # ---- opener: sites other devices host -----------------------------------------
    @app.get("/api/local/node-sites/remote")
    async def node_sites_remote(request: Request, refresh: bool = False):
        local_control(request)
        return await run(lambda: {"devices": sites.client.remote(refresh=refresh)})

    @app.post("/api/local/node-sites/open")
    async def node_sites_open(request: Request):
        local_control(request)
        try:
            value = await _body(request, 8192)
            if not isinstance(value, dict) or set(value) != {"provider_resource", "peer_id", "site_id"}:
                raise SiteError("site_bad_request")
        except SiteError as exc:
            return _error(exc)

        def open_site():
            opened = sites.client.open(value["provider_resource"], value["peer_id"], value["site_id"])
            return {**opened, "url": sites.gateway.url(opened["key"], opened["entry"])}
        return await run(open_site)

    @app.get("/api/local/node-sites/opened/{key}")
    async def node_sites_status(key: str, request: Request, fresh: bool = False):
        local_control(request)

        def status():
            value = sites.client.status(key, fresh=fresh)
            row = sites.store.opened(key) or {}
            return {**value, "origin": sites.gateway.url(key, "/").rstrip("/"), "provider_resource": row.get("resource")}
        return await run(status)

    if start:
        sites.start()
        if hasattr(app, "router"):
            app.router.on_shutdown.append(sites.close)
    return sites
