"""Private-AI stream-v1 envelopes and bounded process-memory event state.

Delta plaintext is intentionally never accepted by ``TaskOrderStore``.  It
exists only in the adapter, signed+sealed wire envelopes, this bounded memory
state, and (after the UI slice lands) the current browser session.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from .task_protocol import TERMINAL_STATES, TaskProtocolError, open_task, seal_task

STREAM_PROTOCOL_VERSION = "rynmesh.llm.stream.v1"
STREAM_DELTA_KIND = "llm_stream_delta"
DEFAULT_MAX_EVENT_BYTES = 256 * 1024
# The final response repeats the complete output inside one sealed/base64
# envelope. Keeping plaintext at 128 KiB ensures that terminal NDJSON line also
# remains below the 256 KiB wire-event ceiling.
DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024


def seal_stream_delta(
    *,
    task_id: str,
    service_id: str,
    sequence: int,
    delta: str,
    sender_peer_id: str,
    recipient_peer_id: str,
    sender_signing_key: bytes,
    recipient_messaging_pub: str,
    expires_at: str,
) -> dict[str, Any]:
    """Create one independently authenticated and encrypted stream event."""
    if sequence < 0:
        raise TaskProtocolError("stream sequence is invalid")
    if not isinstance(delta, str) or not delta:
        raise TaskProtocolError("stream delta is empty")
    body = {
        "stream_version": STREAM_PROTOCOL_VERSION,
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
    next_sequence: int = 0
    output_bytes: int = 0
    terminal: bool = False

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
        if str(body.get("stream_version") or "") != STREAM_PROTOCOL_VERSION:
            raise TaskProtocolError("stream protocol version unsupported")
        if str(body.get("service_id") or "") != self.service_id:
            raise TaskProtocolError("stream service mismatch")
        sequence = body.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise TaskProtocolError("stream sequence is invalid")
        if sequence != self.next_sequence:
            raise TaskProtocolError("stream sequence is not contiguous")
        delta = body.get("delta")
        if not isinstance(delta, str) or not delta:
            raise TaskProtocolError("stream delta is empty")
        event_bytes = len(delta.encode("utf-8"))
        if event_bytes > self.max_event_bytes:
            raise TaskProtocolError("stream delta exceeds event limit")
        if self.output_bytes + event_bytes > self.max_output_bytes:
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
        self.terminal = True
        return body


class StreamEventBroker:
    """Bounded, non-persistent event ring for local reconnect hand-off."""

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        max_events_per_task: int = 256,
        max_snapshot_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
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

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        """Publish only public state, delta, or terminal events in memory."""
        kind = str(event.get("event") or "")
        if kind not in {"state", "delta", "complete", "error"}:
            raise TaskProtocolError("stream broker event kind is invalid")
        with self._lock:
            if task_id not in self._tasks:
                while len(self._tasks) >= self.max_tasks:
                    evicted = self._order.popleft()
                    self._tasks.pop(evicted, None)
                    self._snapshots.pop(evicted, None)
                self._tasks[task_id] = deque(maxlen=self.max_events_per_task)
                self._order.append(task_id)
            if kind == "delta":
                sequence = int(event.get("sequence", -1))
                delta = event.get("delta")
                if sequence < 0 or not isinstance(delta, str):
                    raise TaskProtocolError("stream broker delta is invalid")
                previous_sequence, previous_text = self._snapshots.get(task_id, (-1, ""))
                if sequence != previous_sequence + 1:
                    raise TaskProtocolError("stream broker delta sequence is not contiguous")
                snapshot = previous_text + delta
                if len(snapshot.encode("utf-8")) > self.max_snapshot_bytes:
                    raise TaskProtocolError("stream broker snapshot exceeds output limit")
                self._snapshots[task_id] = (sequence, snapshot)
            self._tasks[task_id].append(dict(event))

    def replay(self, task_id: str, *, after_sequence: int = -1) -> list[dict[str, Any]]:
        with self._lock:
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
            try:
                self._order.remove(task_id)
            except ValueError:
                pass
