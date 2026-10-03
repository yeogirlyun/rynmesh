"""Bounded peer protocols and owner-only exchange controls; no workers."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from fastapi import HTTPException, Request, Response

from .protocol import VERSION, ExchangeError, require
from .service import Exchange


async def body(request: Request, limit=512 * 1024):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, detail="exchange_capacity")
    try:
        value = json.loads(data or b"{}")
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail="exchange_invalid") from None


def install_exchange(app: Any, *, store: Any, messaging_key: Any, local_control):
    # Lazy construction isolates an unavailable/corrupt ledger from ordinary reading.
    app.state.exchange_factory = lambda: Exchange(
        store.home,
        store.private_key_bytes,
        messaging_key,
        allow_loopback=os.environ.get("RYNMESH_EXCHANGE_ALLOW_LOOPBACK") == "1",
    )
    app.state.exchange_control = local_control
    if any(getattr(route, "name", "") == "exchange_status" for route in app.routes):
        return

    creation_lock = threading.Lock()

    def current():
        with creation_lock:
            return create_current()

    def create_current():
        if not getattr(app.state, "exchange", None):
            app.state.exchange = app.state.exchange_factory()
        return app.state.exchange

    def shutdown():
        with creation_lock:
            service = getattr(app.state, "exchange", None)
            if service:
                with service.ledger.lock:
                    service.ledger.close()
                app.state.exchange = None

    app.state.exchange_shutdown = shutdown

    async def call(fn, *args):
        try:
            result = await asyncio.to_thread(fn, *args)
            return Response(
                json.dumps(result),
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        except ExchangeError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(503, detail="exchange_unavailable") from None

    @app.get("/api/local/exchange", name="exchange_status")
    async def status(request: Request):
        app.state.exchange_control(request)
        return await call(lambda: current().status())

    @app.post("/api/local/exchange/action")
    async def action(request: Request):
        app.state.exchange_control(request)
        value = await body(request)
        action = value.get("action")
        try:
            if action == "configure":
                require(set(value) == {"action", "manifest"})
                return await call(lambda: current().configure(value["manifest"]))
            if action == "options":
                require(set(value) == {"action", "value"})
                return await call(lambda: current().options(value["value"]))
            if action in {"refresh", "resume"}:
                require(set(value) == {"action"})
                return await call(
                    lambda: getattr(current(), "sync" if action == "refresh" else "resume")()
                )
            if action == "delivery":
                require(set(value) == {"action", "order_id"})
                return await call(lambda: current().delivery(value["order_id"]))
            require(
                set(value) == {"action", "value", "operation_id", "actor"}
                and isinstance(action, str)
                and isinstance(value["actor"], str)
            )
            return await call(
                lambda: current().action(
                    action, value["value"], value["operation_id"], expected_actor=value["actor"]
                )
            )
        except ExchangeError as exc:
            raise HTTPException(400, detail=str(exc)) from None

    @app.post("/api/peer/exchange/{operation}")
    async def peer(operation: str, request: Request):
        value = await body(request)
        if operation == "identity":
            if value != {}:
                raise HTTPException(400, detail="exchange_invalid")
            from rynmesh.crypto import sign_payload
            from rynmesh.services.peer_box import public_key_b64

            return await call(
                lambda: sign_payload(
                    {
                        "version": VERSION,
                        "kind": "identity",
                        "peer_id": store.peer_id,
                        "encryption_key": public_key_b64(messaging_key),
                    },
                    private_key_bytes=store.private_key_bytes,
                ).to_dict()
            )
        return await call(lambda: current().peer(operation, value))
