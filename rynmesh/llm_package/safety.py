"""Provider-owned limits shared by every inference entry point."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from fastapi import HTTPException

MAX_REQUEST = 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
MAX_TEMP = 64 * 1024 * 1024
MIN_FREE_DISK = 512 * 1024 * 1024


def private_storage_bytes(root: Path) -> int:
    """Count workspace files without charging/following external link targets.

    Codex creates arg0 symlinks to its installed executable on macOS. stat()
    would count that executable repeatedly even though it is not stored here.
    """
    total = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # CLI may remove a scratch file during enumeration.
                reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if stat.S_ISDIR(info.st_mode) and not reparse:
                    pending.append(Path(entry.path))
                else:
                    total += info.st_size
    return total


NODE_SLOTS = threading.BoundedSemaphore(4)
HTTP_SLOTS = threading.BoundedSemaphore(32)
_pressure_lock = threading.Lock()
_pressure_checked = 0.0
_pressure_ok = True


def install_body_limit(app):
    if not getattr(app.state, "inference_body_limit", False):
        app.add_middleware(InferenceBodyLimit)
        app.state.inference_body_limit = True


def _available_memory():
    if os.name == "nt":
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        value = Memory()
        value.length = ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            return value.available
    elif Path("/proc/meminfo").exists():
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


def pressure_ok():
    """Admission circuit breaker, not a hardware damage guarantee."""
    global _pressure_checked, _pressure_ok
    with _pressure_lock:
        if time.monotonic() - _pressure_checked < 5:
            return _pressure_ok
        available = _available_memory()
        safe = available is None or available >= 256 * 1024**2
        nvidia = shutil.which("nvidia-smi")
        if nvidia:
            try:
                result = subprocess.run([nvidia, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                temperatures = [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
                if temperatures:
                    safe = safe and max(temperatures) < 85
            except (OSError, subprocess.TimeoutExpired):
                pass  # GPU metrics unavailable; do not claim thermal enforcement.
        _pressure_ok, _pressure_checked = safe, time.monotonic()
        return safe


class SharedSlots:
    """Per-service and process-wide admission; callers cannot enlarge either."""

    def __init__(self, capacity, path):
        self.local = threading.BoundedSemaphore(min(capacity, 4))
        self.path = path

    def acquire(self, blocking=False):
        try:
            check_disk(self.path)
        except RuntimeError:
            return False
        if not pressure_ok():
            return False
        if not NODE_SLOTS.acquire(blocking=False):
            return False
        if not self.local.acquire(blocking=False):
            NODE_SLOTS.release()
            return False
        return True

    def release(self):
        self.local.release()
        NODE_SLOTS.release()


def check_disk(path: str | Path):
    if shutil.disk_usage(path).free < MIN_FREE_DISK:
        raise RuntimeError("provider_disk_reserve_reached")


async def read_json(request, limit=MAX_REQUEST):
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > limit:
            raise HTTPException(413, "request_too_large")
        data.extend(part)
    try:
        value = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "request_must_be_object")
    return value


class InferenceBodyLimit:
    """Bound unauthenticated/chunked bodies before FastAPI parses them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(
            ("/api/local/llm/", "/api/peer/llm/", "/v1/")
        ):
            return await self.app(scope, receive, send)
        if not HTTP_SLOTS.acquire(blocking=False):
            await self.reject(send, 429, b"inference_connection_limit")
            return
        try:
            await self.bounded(scope, receive, send)
        finally:
            HTTP_SLOTS.release()

    async def bounded(self, scope, receive, send):
        if scope["path"] == "/api/local/llm/native/upload" and scope.get("method") == "POST":
            # Model uploads are authenticated by the control-surface guard and
            # bounded/streamed to disk by their handler. Never buffer GB of weights.
            async def upload_send(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", []), (b"cache-control", b"no-store")]}
                await send(message)
            return await self.app(scope, receive, upload_send)
        # Signed ciphertext envelopes include base64 overhead.
        limit = 4 * MAX_REQUEST if scope["path"].startswith("/api/peer/") else MAX_REQUEST
        packets, total = [], 0
        async def collect():
            nonlocal total
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return False
                total += len(event.get("body", b""))
                if total > limit:
                    await self.reject(send, 413, b"request_too_large")
                    return False
                packets.append(event)
                if not event.get("more_body"):
                    return True

        try:
            if not await asyncio.wait_for(collect(), timeout=15):
                return
        except asyncio.TimeoutError:
            await self.reject(send, 408, b"request_body_timeout")
            return
        iterator = iter(packets)

        async def replay():
            return next(iterator, None) or await receive()

        async def private_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                           (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, replay, private_send)

    @staticmethod
    async def reject(send, status, reason):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": reason})
