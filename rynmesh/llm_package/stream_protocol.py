"""Private-AI stream-v1/v2 envelopes and bounded process-memory event state.

Delta plaintext is intentionally never accepted by ``TaskOrderStore``.  It
exists only in the adapter, signed+sealed wire envelopes, this bounded memory
state, and (after the UI slice lands) the current browser session.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import threading
import time
from typing import Any

from .task_protocol import TERMINAL_STATES, TaskProtocolError, open_task, seal_task

STREAM_PROTOCOL_VERSION = "rynmesh.llm.stream.v1"
STREAM_PROTOCOL_VERSION_V2 = "rynmesh.llm.stream.v2"
STREAM_DELTA_KIND = "llm_stream_delta"
DEFAULT_MAX_EVENT_BYTES = 256 * 1024
DEFAULT_MAX_TERMINAL_BYTES = 4 * 1024 * 1024
PREVIEW_MAX_OUTPUT_BYTES = 1024 * 1024
# Stream-v1 keeps plaintext at 128 KiB so the legacy terminal NDJSON line also
# stays below the 256 KiB v1 wire-event ceiling.
DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024


def seal_stream_delta(
    *,
    task_id: str,
    service_id: str,
    sequence: int,
    delta: str = "",
    state: str | None = None,
    stream_version: str = STREAM_PROTOCOL_VERSION,
    sender_peer_id: str,
    recipient_peer_id: str,
    sender_signing_key: bytes,
    recipient_messaging_pub: str,
    expires_at: str,
) -> dict[str, Any]:
    """Create one independently authenticated and encrypted stream event."""
    if sequence < 0:
        raise TaskProtocolError("stream sequence is invalid")
    if stream_version not in {STREAM_PROTOCOL_VERSION, STREAM_PROTOCOL_VERSION_V2}:
        raise TaskProtocolError("stream protocol version unsupported")
    if state is not None:
        if stream_version != STREAM_PROTOCOL_VERSION_V2 or state != "preview_truncated":
            raise TaskProtocolError("stream state is invalid")
        body: dict[str, Any] = {
            "stream_version": stream_version,
            "task_id": task_id,
            "service_id": service_id,
            "sequence": sequence,
            "state": state,
        }
    else:
        if not isinstance(delta, str) or not delta:
            raise TaskProtocolError("stream delta is empty")
        body = {
            "stream_version": stream_version,
            "task_id": task_id,
            "service_id": service_id,
            "sequence": sequence,
            "delta": delta,
        }
    return seal_task(
        body=body,
        task_id=task_id,
        kind=STREAM_DELTA_KIND,
        sender_peer_id=sender_peer_id,
        recipient_peer_id=recipient_peer_id,
        sender_signing_key=sender_signing_key,
        recipient_messaging_pub=recipient_messaging_pub,
        expires_at=expires_at,
    ).to_dict()


@dataclass
class StreamSequenceVerifier:
    """Fail-closed verifier for the delta sequence and terminal envelope."""

    task_id: str
    service_id: str
    provider_peer_id: str
    recipient_peer_id: str
    recipient_messaging_key: Any
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_terminal_bytes: int = DEFAULT_MAX_TERMINAL_BYTES
    protocol_version: str = STREAM_PROTOCOL_VERSION
    negotiate: bool = False
    next_sequence: int = 0
    output_bytes: int = 0
    terminal: bool = False
    truncated: bool = False
    _negotiated_version: str | None = None

    @property
    def effective_version(self) -> str:
        return self._negotiated_version or self.protocol_version

    def accept_delta(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self.terminal:
            raise TaskProtocolError("stream event received after terminal response")
        outer, body = open_task(
            envelope,
            recipient_peer_id=self.recipient_peer_id,
            recipient_messaging_key=self.recipient_messaging_key,
            expected_kind=STREAM_DELTA_KIND,
        )
        if str(outer.get("task_id") or "") != self.task_id:
            raise TaskProtocolError("stream task mismatch")
        if outer.get("from_peer_id") != self.provider_peer_id:
            raise TaskProtocolError("stream signer is not the selected provider")
        version = str(body.get("stream_version") or "")
        if self._negotiated_version is not None:
            if version != self._negotiated_version:
                raise TaskProtocolError("stream protocol version unsupported")
        elif self.negotiate and self.protocol_version == STREAM_PROTOCOL_VERSION_V2:
            if version not in {STREAM_PROTOCOL_VERSION, STREAM_PROTOCOL_VERSION_V2}:
                raise TaskProtocolError("stream protocol version unsupported")
            self._negotiated_version = version
        else:
            if version != self.protocol_version:
                raise TaskProtocolError("stream protocol version unsupported")
            self._negotiated_version = version
        if str(body.get("service_id") or "") != self.service_id:
            raise TaskProtocolError("stream service mismatch")
        sequence = body.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise TaskProtocolError("stream sequence is invalid")
        if sequence != self.next_sequence:
            raise TaskProtocolError("stream sequence is not contiguous")
        is_v2 = self._negotiated_version == STREAM_PROTOCOL_VERSION_V2
        if is_v2 and body.get("state") == "preview_truncated" and "delta" not in body:
            self.next_sequence += 1
            already = self.truncated
            self.truncated = True
            if already:
                return {"sequence": sequence, "ignored": True, "truncated": True}
            return {"sequence": sequence, "state": "preview_truncated", "truncated": True}
        delta = body.get("delta")
        if not isinstance(delta, str) or not delta:
            raise TaskProtocolError("stream delta is empty")
        event_bytes = len(delta.encode("utf-8"))
        if event_bytes > self.max_event_bytes:
            raise TaskProtocolError("stream delta exceeds event limit")
        if is_v2:
            if self.truncated:
                self.next_sequence += 1
                return {"sequence": sequence, "ignored": True, "truncated": True}
            if self.output_bytes + event_bytes > self.max_output_bytes:
                self.next_sequence += 1
                self.truncated = True
                return {"sequence": sequence, "state": "preview_truncated", "truncated": True}
        else:
            effective_limit = DEFAULT_MAX_OUTPUT_BYTES if self.negotiate else self.max_output_bytes
            if self.output_bytes + event_bytes > effective_limit:
                raise TaskProtocolError("stream output exceeds total limit")
        self.next_sequence += 1
        self.output_bytes += event_bytes
        return {"sequence": sequence, "delta": delta}

    def accept_terminal(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self.terminal:
            raise TaskProtocolError("duplicate terminal stream response")
        outer, body = open_task(
            envelope,
            recipient_peer_id=self.recipient_peer_id,
            recipient_messaging_key=self.recipient_messaging_key,
            expected_kind="llm_response",
        )
        if str(outer.get("task_id") or "") != self.task_id:
            raise TaskProtocolError("stream terminal task mismatch")
        if outer.get("from_peer_id") != self.provider_peer_id:
            raise TaskProtocolError("stream terminal signer is not the selected provider")
        if str(body.get("service_id") or "") != self.service_id:
            raise TaskProtocolError("stream terminal service mismatch")
        if str(body.get("state") or "") not in TERMINAL_STATES:
            raise TaskProtocolError("stream terminal state is invalid")
        if self._negotiated_version is None:
            if self.negotiate and self.protocol_version == STREAM_PROTOCOL_VERSION_V2:
                self._negotiated_version = (
                    STREAM_PROTOCOL_VERSION_V2
                    if body.get("stream_protocol") == STREAM_PROTOCOL_VERSION_V2
                    else STREAM_PROTOCOL_VERSION
                )
            else:
                self._negotiated_version = self.protocol_version
        terminal_limit = (
            self.max_terminal_bytes
            if self._negotiated_version == STREAM_PROTOCOL_VERSION_V2
            else DEFAULT_MAX_EVENT_BYTES
        )
        wire_bytes = len(
            json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        if wire_bytes > terminal_limit:
            raise TaskProtocolError("stream terminal exceeds wire limit")
        self.terminal = True
        return body


class StreamEventBroker:
    """Bounded, non-persistent event ring for local reconnect hand-off."""

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        max_events_per_task: int = 256,
        max_snapshot_bytes: int = PREVIEW_MAX_OUTPUT_BYTES,
    ) -> None:
        if max_tasks < 1 or max_events_per_task < 1 or max_snapshot_bytes < 1:
            raise ValueError("broker limits must be positive")
        self.max_tasks = max_tasks
        self.max_events_per_task = max_events_per_task
        self.max_snapshot_bytes = max_snapshot_bytes
        self._lock = threading.RLock()
        self._tasks: dict[str, deque[dict[str, Any]]] = {}
        self._snapshots: dict[str, tuple[int, str]] = {}
        self._order: deque[str] = deque()
        self._expires: dict[str, float] = {}
        self._stale_at: dict[str, float] = {}
        self._truncated: set[str] = set()

    def _prune(self) -> None:
        now = time.monotonic()
        for task_id, deadline in list(self._expires.items()):
            if now >= deadline:
                self.forget(task_id)
        for task_id, deadline in list(self._stale_at.items()):
            if now >= deadline:
                self.forget(task_id)

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        """Publish only public state, delta, or terminal events in memory."""
        kind = str(event.get("event") or "")
        if kind not in {"state", "delta", "complete", "error"}:
            raise TaskProtocolError("stream broker event kind is invalid")
        with self._lock:
            self._prune()
            if task_id not in self._tasks:
                if kind == "delta" and event.get("sequence") != 0:
                    # A bounded preview was evicted. Losing it must never turn
                    # successful compute into a failed/billable retry.
                    return
                while len(self._tasks) >= self.max_tasks:
                    evicted = next((tid for tid in self._order if tid in self._expires), None)
                    if evicted is not None:
                        self._order.remove(evicted)
                    else:
                        evicted = self._order.popleft()
                    self._tasks.pop(evicted, None)
                    self._snapshots.pop(evicted, None)
                    self._expires.pop(evicted, None)
                    self._stale_at.pop(evicted, None)
                    self._truncated.discard(evicted)
                self._tasks[task_id] = deque(maxlen=self.max_events_per_task)
                self._order.append(task_id)
            if kind == "delta":
                sequence = int(event.get("sequence", -1))
                delta = event.get("delta")
                if sequence < 0 or not isinstance(delta, str):
                    raise TaskProtocolError("stream broker delta is invalid")
                if task_id in self._truncated:
                    self._stale_at[task_id] = time.monotonic() + 3600
                    return
                previous_sequence, previous_text = self._snapshots.get(task_id, (-1, ""))
                if sequence != previous_sequence + 1:
                    raise TaskProtocolError("stream broker delta sequence is not contiguous")
                snapshot = previous_text + delta
                if len(snapshot.encode("utf-8")) > self.max_snapshot_bytes:
                    self._truncated.add(task_id)
                    self._stale_at[task_id] = time.monotonic() + 3600
                    self._tasks[task_id].append({"event": "state", "state": "preview_truncated"})
                    return
                self._snapshots[task_id] = (sequence, snapshot)
            if kind == "state" and event.get("state") == "preview_truncated":
                if task_id in self._truncated:
                    self._stale_at[task_id] = time.monotonic() + 3600
                    return
                self._truncated.add(task_id)
            if kind in {"complete", "error"}:
                # Final output lives in the established retained-result store.
                # This buffer only tells a reader to reconcile that original task.
                event = {k: event[k] for k in ("event", "task_id", "state", "error_code") if k in event}
                self._expires[task_id] = time.monotonic() + 60
                self._stale_at.pop(task_id, None)
            else:
                self._stale_at[task_id] = time.monotonic() + 3600
            self._tasks[task_id].append(dict(event))

    def replay(self, task_id: str, *, after_sequence: int = -1) -> list[dict[str, Any]]:
        with self._lock:
            self._prune()
            values = list(self._tasks.get(task_id, ()))
            snapshot = self._snapshots.get(task_id)
        result = [
            dict(item)
            for item in values
            if item.get("event") != "delta" or int(item.get("sequence", -1)) > after_sequence
        ]
        available_deltas = [
            int(item.get("sequence", -1))
            for item in values
            if item.get("event") == "delta" and int(item.get("sequence", -1)) > after_sequence
        ]
        gap = snapshot is not None and snapshot[0] > after_sequence and (
            not available_deltas or min(available_deltas) > after_sequence + 1
        )
        if gap and snapshot is not None:
            # A slow/reconnecting subscriber fell behind the event ring. Send
            # one bounded cumulative delta so it can replace its partial text,
            # then only non-delta events that follow in the ring.
            result = [
                {
                    "event": "delta",
                    "sequence": snapshot[0],
                    "delta": snapshot[1],
                    "snapshot": True,
                },
                *(item for item in result if item.get("event") != "delta"),
            ]
        return result

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._snapshots.pop(task_id, None)
            self._expires.pop(task_id, None)
            self._stale_at.pop(task_id, None)
            self._truncated.discard(task_id)
            try:
                self._order.remove(task_id)
            except ValueError:
                pass
