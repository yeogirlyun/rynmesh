"""Bounded, reusable ICE sessions for private AI requests.

One reader owns the UDP socket, allowing a small window of encrypted stream
events to be in flight. Consumption applies bounded backpressure.
Task envelopes remain signed/encrypted and are authenticated by the caller.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict

from .p2p import (
    _ACK,
    _CHUNK_BYTES,
    _DATA,
    _HEADER,
    _MAGIC,
    _MAX_CHUNKS,
    _MAX_MESSAGE_BYTES,
    P2PError,
    _close_connection,
    _decode_header,
    _encode_frames,
)

PROTOCOL = "rynmesh.ai-session.v1"
WINDOW = 8
MAX_BUFFER = 8 * 1024 * 1024
IDLE_SECONDS = 600
# A session that answered this recently is used without a liveness probe.
FRESH_SECONDS = 2.0
# Idle sessions are probed this often so a dead path is dropped before the next
# request has to discover it (and pay up to 1.5 s for the failed probe).
KEEPALIVE_SECONDS = 15
_PING, _PONG, _CLOSE = 3, 4, 5


class SessionChannel:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False
        self.last_used = time.monotonic()
        self.last_received = time.monotonic()
        self._send_sequence = 0
        self._receive_sequence = 0
        self._slots = asyncio.Semaphore(WINDOW)
        self._acks = {}
        self._pings = {}
        self._incoming = asyncio.Queue(maxsize=WINDOW * 2)
        self._parts = {}
        self._complete = {}
        self._recent = OrderedDict()
        self._unconsumed = set()
        self._buffered = 0
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while not self.closed:
                packet = await self.connection.recv()
                self.last_received = time.monotonic()
                if len(packet) == _HEADER.size:
                    magic, control, mid, _, _, digest = _HEADER.unpack(packet)
                    if magic == _MAGIC and control in {_PING, _PONG, _CLOSE}:
                        if control == _CLOSE:
                            raise P2PError("peer closed session")
                        if control == _PING:
                            await self.connection.send(_HEADER.pack(_MAGIC, _PONG, mid, 0, 0, digest))
                        elif mid in self._pings:
                            self._pings[mid].set()
                        continue
                try:
                    kind, mid, sequence, total, digest, body = _decode_header(packet)
                except P2PError:
                    continue
                if kind == _ACK:
                    entry = self._acks.get(mid)
                    if entry and entry[1] == digest:
                        entry[0].set()
                    continue
                if kind != _DATA or not 0 < total <= _MAX_CHUNKS or sequence >= total or len(body) > _CHUNK_BYTES:
                    raise P2PError("invalid session frame")
                if mid in self._recent:
                    if self._recent[mid] != digest:
                        raise P2PError("session digest changed")
                    if mid not in self._unconsumed:
                        await self.connection.send(_HEADER.pack(_MAGIC, _ACK, mid, 0, 0, digest))
                    continue
                if mid not in self._parts:
                    if len(self._parts) >= WINDOW:
                        raise P2PError("session receive window exceeded")
                    self._parts[mid] = (total, digest, {})
                expected_total, expected_digest, parts = self._parts[mid]
                if total != expected_total or digest != expected_digest:
                    raise P2PError("session frame metadata changed")
                if sequence not in parts:
                    parts[sequence] = body
                    self._buffered += len(body)
                    if self._buffered > MAX_BUFFER:
                        raise P2PError("session receive buffer exceeded")
                if len(parts) != total:
                    continue
                raw = b"".join(parts[i] for i in range(total))
                del self._parts[mid]
                if len(raw) > _MAX_MESSAGE_BYTES or hashlib.sha256(raw).digest() != digest:
                    raise P2PError("invalid session message")
                value = json.loads(raw)
                index = value.get("sequence")
                if value.get("protocol") != PROTOCOL or type(index) is not int or not self._receive_sequence <= index < self._receive_sequence + WINDOW:
                    raise P2PError("invalid session sequence")
                if index in self._complete or not isinstance(value.get("body"), dict):
                    raise P2PError("duplicate session sequence")
                self._complete[index] = (value["body"], len(raw), mid, digest)
                self._recent[mid] = digest
                self._unconsumed.add(mid)
                while len(self._recent) > 256:
                    self._recent.popitem(last=False)
                # The reader never blocks on application consumption. ACK on
                # dequeue keeps slow consumers from overflowing the window.
                while self._receive_sequence in self._complete:
                    self._incoming.put_nowait(self._complete.pop(self._receive_sequence))
                    self._receive_sequence += 1
                self.last_used = time.monotonic()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.closed = True
            # Wake waiting callers without retaining exception text or payloads.
            for event, _ in self._acks.values():
                event.set()
            try:
                self._incoming.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def send(self, body, timeout_s):
        async with self._slots:
            if self.closed:
                raise P2PError("session closed")
            index = self._send_sequence
            self._send_sequence += 1
            raw = json.dumps({"protocol": PROTOCOL, "sequence": index, "body": body}, separators=(",", ":")).encode()
            mid, frames = _encode_frames(raw)
            digest = hashlib.sha256(raw).digest()
            event = asyncio.Event()
            self._acks[mid] = (event, digest)
            deadline = time.monotonic() + timeout_s
            try:
                while time.monotonic() < deadline:
                    for i, frame in enumerate(frames):
                        await self.connection.send(frame)
                        if i % 16 == 15:
                            await asyncio.sleep(0)
                    try:
                        await asyncio.wait_for(event.wait(), min(.5, max(.01, deadline - time.monotonic())))
                        if self.closed:
                            raise P2PError("session closed")
                        self.last_used = time.monotonic()
                        return len(raw)
                    except asyncio.TimeoutError:
                        continue
                raise P2PError("session acknowledgement timed out")
            finally:
                self._acks.pop(mid, None)

    async def receive(self, timeout_s):
        if self.closed and self._incoming.empty():
            raise P2PError("session closed")
        value = await asyncio.wait_for(self._incoming.get(), timeout_s)
        if value is None:
            raise P2PError("session closed")
        body, size, mid, digest = value
        self._buffered -= size
        self._unconsumed.discard(mid)
        await self.connection.send(_HEADER.pack(_MAGIC, _ACK, mid, 0, 0, digest))
        return body

    async def probe(self):
        if self.closed:
            return False
        mid, event = uuid.uuid4().bytes, asyncio.Event()
        self._pings[mid] = event
        try:
            for _ in range(3):
                await self.connection.send(_HEADER.pack(_MAGIC, _PING, mid, 0, 0, b"\0" * 32))
                try:
                    await asyncio.wait_for(event.wait(), .5)
                    return True
                except asyncio.TimeoutError:
                    pass
            return False
        finally:
            self._pings.pop(mid, None)

    async def close(self):
        if not self.closed:
            try:
                await self.connection.send(_HEADER.pack(_MAGIC, _CLOSE, uuid.uuid4().bytes, 0, 0, b"\0" * 32))
            except Exception:
                pass
        self.closed = True
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        await _close_connection(self.connection)
        self._parts.clear()
        self._complete.clear()


class SessionPool:
    """Node-owned event loop survives per-request asyncio.run invocations."""
    def __init__(self, max_sessions=3, idle_seconds=IDLE_SECONDS):
        self.max_sessions = max_sessions
        self.idle_seconds = idle_seconds
        self._loop = None
        self._thread = None
        self._guard = threading.Lock()
        self._sessions = {}
        self._locks = {}
        self._waiting = {}
        self._creating = set()
        self._closed = False

    def has_session(self, key):
        # Advisory route choice only; liveness is rechecked on the pool loop.
        return not self._closed and key in self._sessions

    def _start(self):
        with self._guard:
            if self._closed:
                raise P2PError("session pool stopped")
            if self._loop:
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._serve, name="ryn-ai-connections", daemon=True)
            self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._reap())
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _reap(self):
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            for key, (channel, _) in list(self._sessions.items()):
                lock = self._locks.get(key)
                if key not in self._sessions or lock is None or lock.locked():
                    continue
                expired = channel.closed or time.monotonic() - channel.last_used > self.idle_seconds
                if not expired and time.monotonic() - getattr(channel, "last_received", 0.0) >= KEEPALIVE_SECONDS:
                    # Holding the lock keeps a request from starting mid-probe.
                    async with lock:
                        expired = self._sessions.get(key, (None,))[0] is channel and not await channel.probe()
                if expired and self._sessions.get(key, (None,))[0] is channel:
                    self._sessions.pop(key, None)
                    await channel.close()
                    if not self._waiting.get(key):
                        self._locks.pop(key, None)
                        self._waiting.pop(key, None)

    async def run(self, key, factory, exchange):
        self._start()
        future = asyncio.run_coroutine_threadsafe(self._run(key, factory, exchange), self._loop)
        try:
            return await asyncio.wrap_future(future)
        except BaseException:
            future.cancel()
            raise

    async def _run(self, key, factory, exchange):
        if self._waiting.get(key, 0) >= 8:
            raise P2PError("session request queue full")
        if key not in self._locks and len(self._locks) >= 32:
            raise P2PError("session peer limit reached")
        self._waiting[key] = self._waiting.get(key, 0) + 1
        try:
            return await self._exchange(key, factory, exchange)
        finally:
            self._waiting[key] -= 1
            if not self._waiting[key] and key not in self._sessions:
                self._waiting.pop(key, None)
                self._locks.pop(key, None)

    async def _exchange(self, key, factory, exchange):
        if key not in self._locks:
            if len(self._locks) >= 32:
                # Remove only idle keys, so waiters cannot create parallel locks.
                raise P2PError("session peer limit reached")
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            reused = key in self._sessions
            if reused:
                channel, evidence = self._sessions[key]
                heard_at = getattr(channel, "last_received", 0.0)
                recently_heard = not channel.closed and time.monotonic() - heard_at < FRESH_SECONDS
                if not recently_heard and not await channel.probe():
                    self._sessions.pop(key, None)
                    await channel.close()
                    reused = False
            if not reused:
                if len(self._sessions) + len(self._creating) >= self.max_sessions:
                    idle = [k for k in self._sessions if not self._locks[k].locked()]
                    if not idle:
                        raise P2PError("session capacity reached")
                    old = min(idle, key=lambda k: self._sessions[k][0].last_used)
                    channel, _ = self._sessions.pop(old)
                    await channel.close()
                self._creating.add(key)
                try:
                    channel, evidence = await factory()
                finally:
                    self._creating.discard(key)
                # Legacy exchange may have completed inside the factory.
                if channel is None:
                    return evidence
                self._sessions[key] = (channel, evidence)
            try:
                return await exchange(channel, dict(evidence, connection_reused=reused))
            except BaseException as exc:
                self._sessions.pop(key, None)
                await channel.close()
                # Never replay a request that may have reached the model.
                exc.request_may_have_started = True
                raise

    def close(self):
        with self._guard:
            if self._closed:
                return
            self._closed = True
            if not self._loop:
                return
        async def cleanup():
            await asyncio.gather(*(c.close() for c, _ in self._sessions.values()), return_exceptions=True)
            self._sessions.clear()
        try:
            asyncio.run_coroutine_threadsafe(cleanup(), self._loop).result(timeout=5)
        except (TimeoutError, concurrent.futures.CancelledError):
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
