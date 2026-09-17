import asyncio
import json

from fastapi import HTTPException, Request

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy
from ..crypto import canonical_json
from .service import MAX_WIRE, PATH, SharedReading
from .store import ListError, ListStore


async def body(request, limit=8192):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, detail='shared_request_invalid')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail='shared_request_invalid') from None


def install_shared_reading(app, *, home, messaging_key, friends, local_control, workers):
    app.state.shared_reading = SharedReading(ListStore(home, messaging_key), friends)
    def current():
        return app.state.shared_reading
    workers.register(BackgroundWorkerSpec(name='shared-reading.sync', initial_delay_s=5,
        run_once=lambda: current().run_once(), policy=BackoffPolicy.fixed(5)), replace=True)
    if any(getattr(route, 'name', '') == 'shared_reading_status' for route in app.routes):
        return current()

    async def call(request, method, *args, **kwargs):
        local_control(request)
        try:
            return await asyncio.to_thread(getattr(current(), method), *args, **kwargs)
        except ListError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(503, detail='shared_sync_unconfirmed') from None

    @app.get('/api/local/shared-reading', name='shared_reading_status')
    async def status(request: Request):
        return {'lists': await call(request, 'listing')}

    @app.post('/api/local/shared-reading/action')
    async def action(request: Request):
        local_control(request)
        value = await body(request)
        kind = value.get('action')
        if kind == 'create':
            return await call(request, 'create', value.get('relationship_id'), value.get('title'), value.get('id'))
        if kind == 'discover':
            return {'invitations': await call(request, 'discover', value.get('relationship_id'))}
        if kind == 'accept':
            return await call(request, 'accept', value.get('relationship_id'), value.get('id'))
        if kind == 'edit':
            return await call(request, 'edit', value.get('id'), value.get('operation_id'), value.get('edit'), value.get('value'))
        if kind == 'sync':
            await call(request, 'sync', value.get('id'))
            return {'lists': await call(request, 'listing')}
        raise HTTPException(400, detail='shared_request_invalid')

    @app.post(PATH)
    async def peer(request: Request):
        wire = await body(request, MAX_WIRE)
        try:
            relation = await asyncio.to_thread(current().friends().verify_request, path=PATH,
                                               body=canonical_json(wire), headers=request.headers)
            return await asyncio.to_thread(current().respond, wire, relation)
        except Exception:
            raise HTTPException(403, detail='shared_request_rejected') from None
    return current()
