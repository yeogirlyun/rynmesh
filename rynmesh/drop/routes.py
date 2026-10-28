"""HTTP routes for device drop: local control API and the peer protocol."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .codec import FRAME_PLAINTEXT_BYTES, DropProtocolError, new_id
from .engine import DropEngine, DropError

STREAM_BATCH_BYTES = 4 * 1024 * 1024
MAX_FRAME_WIRE = FRAME_PLAINTEXT_BYTES + 16


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DropError):
        return JSONResponse({"detail": exc.message, "code": exc.code, **exc.extra}, status_code=exc.status)
    if isinstance(exc, DropProtocolError):
        return JSONResponse({"detail": str(exc), "code": "protocol"}, status_code=400)
    raise exc


def _source(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise DropError("bad_request", "Body must be JSON.", 400) from None
    if not isinstance(body, dict):
        raise DropError("bad_request", "Body must be an object.", 400)
    return body


def install_drop(app: FastAPI, *, engine: DropEngine, local_control: Callable[[Request], None]) -> DropEngine:
    app.state.drop_engine = engine
    engine.attach_app(app)

    def control(request: Request) -> None:
        local_control(request)

    local = [Depends(control)]
    base = "/api/local/drop"

    def run(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (DropError, DropProtocolError) as exc:
            return _error(exc)

    async def run_async(fn: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except (DropError, DropProtocolError) as exc:
            return _error(exc)

    # ---- local API ----------------------------------------------------------
    @app.get(f"{base}/capabilities", dependencies=local)
    async def drop_capabilities():
        return await run_async(engine.capabilities)

    @app.get(f"{base}/peers", dependencies=local)
    async def drop_peers():
        return await run_async(engine.peers)

    @app.get(f"{base}/snapshot", dependencies=local)
    async def drop_snapshot():
        return await run_async(engine.snapshot)

    @app.get(f"{base}/events", dependencies=local)
    async def drop_events(request: Request, since: int = 0):
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        engine.subscribe(loop, wake)

        async def stream():
            cursor = since
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    events = engine.events_since(cursor)
                    if events is None:
                        yield f"data: {json.dumps({'seq': engine.seq, 'type': 'resync'})}\n\n"
                        cursor = engine.seq
                        continue
                    for event in events:
                        yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                        cursor = event["seq"]
                    wake.clear()
                    if engine.events_since(cursor):
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                engine.unsubscribe(loop, wake)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get(f"{base}/conversation", dependencies=local)
    async def drop_conversation(peer_id: str, limit: int = 300):
        return await run_async(lambda: engine.conversation(peer_id, max(1, min(limit, 1000))))

    @app.post(f"{base}/conversation/read", dependencies=local)
    async def drop_read(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        await run_async(lambda: engine.mark_read(str(body.get("peer_id") or "")))
        return {"ok": True}

    @app.post(f"{base}/messages", dependencies=local)
    async def drop_send_message(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        return await run_async(lambda: engine.send_message(body))

    @app.post(f"{base}/messages/retry", dependencies=local)
    async def drop_retry_message(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        return await run_async(lambda: engine.retry_message(str(body.get("message_id") or "")))

    @app.post(f"{base}/preflight", dependencies=local)
    async def drop_preflight(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        return await run_async(lambda: engine.preflight(body))

    @app.post(f"{base}/transfers", dependencies=local)
    async def drop_create_transfer(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        return await run_async(lambda: engine.create_transfer(body))

    @app.get(f"{base}/transfers", dependencies=local)
    async def drop_list_transfers(peer_id: str = "", filter: str = "all"):
        def list_rows():
            rows = engine.db.transfers(peer_id=peer_id or None)
            done = {"completed", "rejected", "cancelled", "source_changed", "verify_failed", "failed"}
            if filter == "active":
                rows = [r for r in rows if r["status"] not in done]
            elif filter == "done":
                rows = [r for r in rows if r["status"] == "completed"]
            return [engine.transfer_api(r) for r in rows]

        return await run_async(list_rows)

    @app.get(f"{base}/transfers/{{transfer_id}}", dependencies=local)
    async def drop_get_transfer(transfer_id: str):
        def get():
            row = engine.db.transfer(transfer_id)
            if not row:
                raise DropError("not_found", "Transfer not found.", 404)
            return engine.transfer_api(row, with_files=True)

        return await run_async(get)

    @app.post(f"{base}/transfers/{{transfer_id}}/{{action}}", dependencies=local)
    async def drop_transfer_action(transfer_id: str, action: str, request: Request):
        allowed = {"pause", "resume", "cancel", "retry", "accept", "reject", "open", "reveal", "send_again"}
        if action not in allowed:
            return JSONResponse({"detail": "unknown action", "code": "not_found"}, status_code=404)
        body: dict[str, Any] = {}
        if action in ("accept", "open", "reveal"):
            raw = await request.body()
            if raw.strip():
                try:
                    body = await _json(request)
                except DropError as exc:
                    return _error(exc)
        # open/reveal answer {ok, path, how}. With {"delegate": true} nothing is
        # launched here: the desktop shell opens the path itself, from the
        # foreground process, so the window is not left behind other apps.
        return await run_async(lambda: engine.action(transfer_id, action, body))

    @app.put(f"{base}/uploads", dependencies=local)
    async def drop_upload(request: Request, name: str = "", rel: str = "", batch: str = ""):
        try:
            target, rel_path = await asyncio.to_thread(engine.upload_target, batch, rel or name)
        except DropError as exc:
            return _error(exc)
        tmp = target.with_name(target.name + f".{new_id()[:8]}.uploading")
        size = 0
        buffer = bytearray()
        try:
            fh = await asyncio.to_thread(open, tmp, "wb")
            try:
                async for piece in request.stream():
                    buffer += piece
                    if len(buffer) >= STREAM_BATCH_BYTES:
                        data = bytes(buffer)
                        buffer.clear()
                        await asyncio.to_thread(fh.write, data)
                        size += len(data)
                if buffer:
                    await asyncio.to_thread(fh.write, bytes(buffer))
                    size += len(buffer)
                await asyncio.to_thread(fh.flush)
            finally:
                await asyncio.to_thread(fh.close)
            await asyncio.to_thread(tmp.replace, target)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            return JSONResponse({"detail": "upload interrupted", "code": "upload_failed"}, status_code=400)
        return {"upload_id": f"{batch}:{rel_path}", "name": target.name, "rel_path": rel_path, "size": size}

    @app.get(f"{base}/prefs", dependencies=local)
    async def drop_get_prefs():
        return await run_async(engine.prefs_api)

    @app.put(f"{base}/prefs", dependencies=local)
    async def drop_put_prefs(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        return await run_async(lambda: engine.set_prefs_api(body))

    @app.post(f"{base}/peers/verify", dependencies=local)
    async def drop_verify(request: Request):
        try:
            body = await _json(request)
        except DropError as exc:
            return _error(exc)
        return await run_async(lambda: engine.verify_peer(str(body.get("peer_id") or ""), bool(body.get("accept"))))

    # ---- peer protocol --------------------------------------------------------
    peer = "/api/peer/drop"

    @app.get(f"{peer}/hello")
    async def drop_hello(nonce: str = ""):
        return run(lambda: engine.hello(nonce))

    def envelope_route(path: str, handler: Callable[[Any, str | None], dict[str, Any]]) -> None:
        async def endpoint(request: Request):
            try:
                if path == "message":
                    raw = bytearray()
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > 256 * 1024:
                            return JSONResponse({"detail": "Message is too large.", "code": "too_large"}, status_code=413)
                    body = json.loads(raw)
                else:
                    body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                return JSONResponse({"detail": "invalid body", "code": "bad_request"}, status_code=400)
            if (path != "message" and isinstance(body, dict) and isinstance(body.get("payload"), dict)
                    and "provider_resource" in body["payload"]):
                return JSONResponse({"detail": "Text messages only.", "code": "capability_denied"}, status_code=403)
            source = _source(request)
            return await run_async(lambda: handler(body, source))

        app.add_api_route(f"{peer}/{path}", endpoint, methods=["POST"], name=f"drop_peer_{path}")

    envelope_route("message", engine.handle_message)
    envelope_route("offer", engine.handle_offer)
    envelope_route("status", engine.handle_status)
    envelope_route("complete", engine.handle_complete)
    envelope_route("cancel", engine.handle_cancel)

    @app.put(f"{peer}/data/{{transfer_id}}/{{file_id}}")
    async def drop_data(transfer_id: str, file_id: str, request: Request, offset: int = -1, salt: str = ""):
        sender = request.headers.get("x-ryn-drop-from", "")
        auth = request.headers.get("x-ryn-drop-auth", "")
        if offset < 0:
            return JSONResponse({"detail": "offset required", "code": "bad_request"}, status_code=400)
        try:
            writer = await asyncio.to_thread(engine.begin_put, transfer_id, file_id, offset, salt, auth, sender,
                                             _source(request))
        except (DropError, DropProtocolError) as exc:
            return _error(exc)
        buffer = bytearray()
        # One batch is decrypted, written and fsynced while the next one is
        # read from the network; at most one feed runs, so order is kept.
        pending: asyncio.Future | None = None

        async def feed(frames: list[bytes]) -> None:
            nonlocal pending
            if pending is not None:
                previous, pending = pending, None
                await previous  # a failed feed surfaces before the next one starts
            pending = asyncio.ensure_future(asyncio.to_thread(writer.feed, frames))

        async def settle() -> None:
            nonlocal pending
            if pending is not None:
                previous, pending = pending, None
                await asyncio.gather(previous, return_exceptions=True)

        try:
            async for piece in request.stream():
                buffer += piece
                if len(buffer) < STREAM_BATCH_BYTES:
                    continue
                frames, consumed = _split_frames(buffer)
                del buffer[:consumed]
                if frames:
                    await feed(frames)
            frames, consumed = _split_frames(buffer)
            del buffer[:consumed]
            if frames:
                await feed(frames)
            if pending is not None:
                previous, pending = pending, None
                await previous
            if buffer:
                raise DropError("bad_request", "truncated frame", 400)
            # The last chunk's fsync runs in the background; report only what is durable.
            await asyncio.to_thread(writer.drain)
        except (DropError, DropProtocolError) as exc:
            await settle()
            await asyncio.to_thread(writer.finish)
            return _error(exc)
        except Exception:
            # Client went away mid-stream: keep only fsynced chunks.
            await settle()
            await asyncio.to_thread(writer.finish)
            return JSONResponse({"detail": "stream interrupted", "code": "interrupted"}, status_code=400)
        await asyncio.to_thread(writer.finish)
        return {"committed": writer.committed}

    return engine


def _split_frames(buffer: bytearray) -> tuple[list[bytes], int]:
    frames: list[bytes] = []
    pos = 0
    view = memoryview(buffer)
    try:
        while len(buffer) - pos >= 4:
            length = int.from_bytes(view[pos:pos + 4], "big")
            if length <= 16 or length > MAX_FRAME_WIRE:
                raise DropProtocolError("frame size is invalid")
            if len(buffer) - pos - 4 < length:
                break
            frames.append(bytes(view[pos + 4:pos + 4 + length]))
            pos += 4 + length
    finally:
        view.release()
    return frames, pos
