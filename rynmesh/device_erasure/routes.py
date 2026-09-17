import asyncio
import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from .adapters import CleanupAdapters
from .service import LIMIT, PATH, DeviceErasure
from .store import ErasureError, ErasureStore


async def body(request, limit=8192):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, detail='erasure_request_invalid')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail='erasure_request_invalid') from None


def install_device_erasure(app, *, home, messaging_key, local_control, workers):
    app.state.device_erasure = DeviceErasure(ErasureStore(home, messaging_key),
        lambda: app.state.device_sync.service, CleanupAdapters(app))
    def current():
        return app.state.device_erasure
    workers.register(BackgroundWorkerSpec(name='device-erasure.receipts', initial_delay_s=5,
        run_once=lambda: current().run_once(), policy=BackoffPolicy.fixed(5)), replace=True)
    if any(getattr(route, 'name', '') == 'device_erasure_status' for route in app.routes):
        return current()

    async def call(request, method, *args):
        local_control(request)
        try:
            return await asyncio.to_thread(getattr(current(), method), *args)
        except ErasureError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(503, detail='erasure_unavailable') from None

    @app.get('/api/local/device-erasure', name='device_erasure_status')
    async def status(request: Request):
        return JSONResponse(await call(request, 'status'), headers={'Cache-Control': 'no-store'})

    @app.post('/api/local/device-erasure/action')
    async def action(request: Request):
        local_control(request)
        value = await body(request)
        kind = value.get('action')
        if kind == 'begin':
            return await call(request, 'begin', value.get('id'), value.get('category'), value.get('pair_ids'))
        if kind == 'approve':
            return await call(request, 'approve', value.get('id'), value.get('review_token'))
        if kind in {'preview', 'resume', 'reject'}:
            return await call(request, kind, value.get('id'))
        if kind == 'exchange':
            return await call(request, kind, value.get('id'), value.get('pair_id'))
        raise HTTPException(400, detail='erasure_request_invalid')

    @app.post(PATH)
    async def peer(request: Request):
        value = await body(request, LIMIT)
        try:
            return await asyncio.to_thread(current().receive, value)
        except Exception:
            raise HTTPException(403, detail='erasure_request_rejected') from None
    return current()
