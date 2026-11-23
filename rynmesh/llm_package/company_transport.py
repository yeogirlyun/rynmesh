"""Authenticated company rendezvous for the existing encrypted model protocol."""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import threading
import time
import uuid

from .p2p import IceSignal, provider_exchange
from .task_protocol import TaskProtocolError

log = logging.getLogger(__name__)
FRAME_BYTES = 32 * 1024
MAX_MESSAGE = 16 * 1024 * 1024


class CompanyModelTransport:
    def __init__(self, providers, dispatch, stop):
        self.providers = providers
        self.dispatch = dispatch
        self.stop = stop
        self.slots = threading.BoundedSemaphore(4)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="company-model")

    async def call(self, plugin, action, **fields):
        result, _ = await self.providers._request(plugin, "POST", "/model-transport", {"action": action, **fields})
        return result

    async def online(self, plugin, peer):
        return (await self.call(plugin, "presence", target=peer)).get("online") is True

    async def offer(self, plugin, peer, service, signal):
        session = str(uuid.uuid4())
        await self.call(plugin, "open", id=session, target=peer, service_id=service, mode="ice", signal=signal.to_dict())
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                answer = (await self.call(plugin, "read", id=session)).get("signal")
                if answer:
                    return IceSignal.from_dict(answer)
                await asyncio.sleep(0.4)
            raise TaskProtocolError("company_signaling_timeout")
        finally:
            await self.close_session(plugin, session)

    async def close_session(self, plugin, session):
        try:
            await self.call(plugin, "close", id=session)
        except Exception:
            log.debug("company model session cleanup deferred to expiry")

    async def write(self, plugin, session, sequence, envelope):
        data = json.dumps(envelope, separators=(",", ":")).encode() + b"\n"
        return await self.write_bytes(plugin, session, sequence, data)

    async def write_bytes(self, plugin, session, sequence, data):
        if len(data) > MAX_MESSAGE:
            raise TaskProtocolError("company_relay_message_too_large")
        for offset in range(0, len(data), FRAME_BYTES):
            frame = base64.b64encode(data[offset:offset + FRAME_BYTES]).decode()
            await self.call(plugin, "write", id=session, seq=sequence, bytes=frame)
            sequence += 1
        return sequence

    async def read(self, plugin, session, deadline):
        sequence = 0
        pending = bytearray()
        while time.monotonic() < deadline and not self.stop.is_set():
            result = await self.call(plugin, "read", id=session, seq=sequence)
            frames = result["frames"]
            if not frames:
                await asyncio.sleep(0.35)
                continue
            for frame in frames:
                if frame["seq"] != sequence:
                    raise TaskProtocolError("company_relay_sequence_mismatch")
                sequence += 1
                pending.extend(base64.b64decode(frame["bytes"], validate=True))
                if len(pending) > MAX_MESSAGE:
                    raise TaskProtocolError("company_relay_message_too_large")
                while (end := pending.find(b"\n")) >= 0:
                    yield json.loads(pending[:end])
                    del pending[:end + 1]
        raise TaskProtocolError("company_relay_timeout")

    async def relay(self, plugin, peer, service, signed, timeout, on_event):
        session = str(uuid.uuid4())
        await self.call(plugin, "open", id=session, target=peer, service_id=service, mode="relay")
        try:
            await self.write(plugin, session, 0, signed)
            async for envelope in self.read(plugin, session, time.monotonic()+timeout):
                if envelope.get("payload", {}).get("kind") == "llm_response":
                    return envelope
                on_event(envelope)
        finally:
            await self.close_session(plugin, session)

    def poll(self):
        for plugin, directory in self.providers._cached():
            if directory.get("model_transport") != 1 or not self.slots.acquire(blocking=False):
                continue
            try:
                row = asyncio.run(self.call(plugin, "poll")).get("session")
                if row:
                    self.executor.submit(self.serve, plugin, directory["resource"], row)
                    continue
            except Exception:
                self.slots.release()
                raise
            self.slots.release()
        return False

    def serve(self, plugin, resource, row):
        try:
            asyncio.run(self._serve(plugin, resource, row))
        except Exception as exc:
            log.warning("company model session failed mode=%s error=%s", row["mode"], type(exc).__name__)
        finally:
            self.slots.release()

    async def _serve(self, plugin, resource, row):
        session = row["id"]
        def events(body):
            return self.dispatch(resource, row["source_peer"], row["service_id"], body)
        def handle(body, emit=None):
            for event in events(body):
                if event.get("payload", {}).get("kind") == "llm_response":
                    return event
                if emit:
                    emit(event)
            raise TaskProtocolError("company_response_missing")
        if row["mode"] == "ice":
            async def answer(signal):
                await self.call(plugin, "answer", id=session, signal=signal.to_dict())
            await provider_exchange(offer=IceSignal.from_dict(row["offer"]), publish_answer=answer,
                                    handle_request=handle, handle_stream_request=handle, timeout_s=840,
                                    connect_timeout_s=15, stop_event=self.stop)
            return
        async for body in self.read(plugin, session, time.monotonic()+30):
            sequence = 0
            stream = events(body)
            try:
                batch = bytearray()
                flushed = 0.0
                while True:
                    envelope = await asyncio.to_thread(next, stream, None)
                    if envelope is None:
                        break
                    batch.extend(json.dumps(envelope, separators=(",", ":")).encode() + b"\n")
                    if (len(batch) >= FRAME_BYTES or time.monotonic()-flushed >= 0.2
                            or envelope.get("payload", {}).get("kind") == "llm_response"):
                        sequence = await self.write_bytes(plugin, session, sequence, batch)
                        batch.clear()
                        flushed = time.monotonic()
                if batch:
                    await self.write_bytes(plugin, session, sequence, batch)
            finally:
                await asyncio.to_thread(stream.close)
            return
