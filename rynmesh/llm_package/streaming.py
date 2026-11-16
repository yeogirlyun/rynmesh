"""Bounded thread-to-async streaming bridge and encrypted peer SSE reader."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import inspect
import json
import threading
import time
from typing import Any

from .connectivity import peer_response


def _extract_content_only(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("usage") is not None or "payload" in value:
        return None
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    if choice.get("index", 0) != 0 or choice.get("finish_reason") is not None:
        return None
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if not isinstance(content, str) or not content:
        return None
    if any(v is not None for k, v in delta.items() if k != "content"):
        return None
    return content


def _merge_content_chunk(template: dict[str, Any], merged_content: str) -> dict[str, Any]:
    out = dict(template)
    choice0 = dict(template["choices"][0])
    delta0 = dict(choice0["delta"])
    delta0["content"] = merged_content
    choice0["delta"] = delta0
    out["choices"] = [choice0]
    return out


async def events(run: Any, cancel: Any):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    closed = threading.Event()
    buffer_lock = threading.Lock()
    buffered_template: dict[str, Any] | None = None
    buffered_parts: list[str] = []

    def _pop_buffered() -> dict[str, Any] | None:
        nonlocal buffered_template
        with buffer_lock:
            if not buffered_parts or buffered_template is None:
                return None
            merged = _merge_content_chunk(buffered_template, "".join(buffered_parts))
            buffered_template = None
            buffered_parts.clear()
            return merged

    def _put_blocking(item: tuple[str, Any]) -> None:
        if closed.is_set():
            raise RuntimeError("task_cancelled")
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=15)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RuntimeError("stream consumer too slow") from exc

    def _drain_buffer_to_queue() -> None:
        if closed.is_set() or queue.full():
            return
        flushed = _pop_buffered()
        if flushed is not None:
            queue.put_nowait(("event", flushed))

    def emit(value: dict[str, Any]) -> None:
        nonlocal buffered_template
        if closed.is_set():
            raise RuntimeError("task_cancelled")
        content = _extract_content_only(value)
        if content is not None:
            with buffer_lock:
                if buffered_template is None:
                    buffered_template = copy.deepcopy(value)
                buffered_parts.append(content)
            loop.call_soon_threadsafe(_drain_buffer_to_queue)
            return
        flushed = _pop_buffered()
        if flushed is not None:
            _put_blocking(("event", flushed))
        _put_blocking(("event", value))

    async def work():
        try:
            result = await run(emit)
            if not closed.is_set():
                flushed = _pop_buffered()
                if flushed is not None:
                    await queue.put(("event", flushed))
                await queue.put(("result", result))
        except Exception as exc:
            if not closed.is_set():
                await queue.put(("error", exc))

    task = asyncio.create_task(work())
    try:
        while True:
            kind, value = await queue.get()
            if not queue.full():
                flushed = _pop_buffered()
                if flushed is not None:
                    queue.put_nowait(("event", flushed))
            if kind == "error":
                raise value
            yield kind, value
            if kind == "result":
                break
    finally:
        closed.set()
        if not task.done():
            # Do not abandon settlement/hold cleanup by cancelling its coroutine.
            # Starlette closes generators inside an already-cancelled AnyIO scope;
            # run cancellation independently so that scope cannot skip the I/O.
            cancel_task = asyncio.create_task(asyncio.to_thread(cancel))
            cancel_task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)


def _open_stream_response(factory: Any, url: str, body: dict[str, Any], *, timeout_s: float, idle_s: float):
    if factory is None:
        return peer_response(url, body, timeout_s=timeout_s, idle_s=idle_s)
    try:
        params = inspect.signature(factory).parameters
        accepts_idle = "idle_s" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_idle = False
    if accepts_idle:
        return factory(url, body, timeout_s=timeout_s, idle_s=idle_s)
    return factory(url, body, timeout_s=min(timeout_s, idle_s))


def _update_response_timeout(response: Any, timeout_s: float) -> None:
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(timeout_s)


def peer_stream(
    url: str,
    body: dict[str, Any],
    *,
    timeout_s: float,
    idle_s: float = 120.0,
    on_event: Any,
    response_factory=None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    with _open_stream_response(response_factory, url, body, timeout_s=timeout_s, idle_s=idle_s) as response:
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ValueError("peer_streaming_not_supported")
        total = 0
        consecutive_bad = 0
        skipped_bad = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("peer_stream_total_timeout")
            capped_by_deadline = remaining <= idle_s
            _update_response_timeout(response, min(idle_s, max(0.001, remaining)))
            try:
                line = response.readline(4 * 1024 * 1024 + 1)
            except TimeoutError as exc:
                if capped_by_deadline or time.monotonic() >= deadline:
                    raise ValueError("peer_stream_total_timeout") from exc
                raise ValueError("peer_stream_idle_timeout") from exc
            if not line:
                break
            if time.monotonic() > deadline:
                raise ValueError("peer_stream_total_timeout")
            if len(line) > 4 * 1024 * 1024:
                raise ValueError("peer stream line limit exceeded")
            total += len(line)
            if total > 128 * 1024 * 1024:
                raise ValueError("peer stream limit exceeded")
            if not line.startswith(b"data:"):
                continue
            try:
                envelope = json.loads(line[5:])
            except (UnicodeDecodeError, json.JSONDecodeError):
                envelope = None
            if not isinstance(envelope, dict) or (
                "payload" in envelope and not isinstance(envelope["payload"], dict)
            ):
                skipped_bad += 1
                consecutive_bad += 1
                if consecutive_bad >= 3:
                    raise ValueError(f"peer_stream_invalid_event:{skipped_bad}")
                continue
            consecutive_bad = 0
            if envelope.get("payload", {}).get("kind") == "llm_response":
                return envelope
            on_event(envelope)
    raise ValueError("peer stream truncated")
