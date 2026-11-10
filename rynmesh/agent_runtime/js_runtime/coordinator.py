"""Host-call authorization and suspension coordinator for QuickJS Computer Use execution."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .images import HostImageRegistry, ImagePromotionResult, PromotedImageAttachment
from .manager import JsRuntimeManager, JsRuntimeSession
from .protocol import (
    JsExecutionSnapshot,
    JsProtocolError,
    JsRuntimeError,
    PendingHostCallEnvelope,
    sanitize_snapshot_value,
)

MAX_COORDINATED_HISTORY = 32

TERMINAL_HOST_CALL_STATUSES: frozenset[str] = frozenset({
    "completed",
    "rejected",
    "unknown",
})

HOST_CALL_TRANSITION_TABLE: dict[str, frozenset[str]] = {
    "pending_admission": frozenset({"admitted", "rejected"}),
    "admitted": frozenset({"awaiting_approval", "dispatched", "completed", "rejected", "unknown"}),
    "awaiting_approval": frozenset({"awaiting_approval", "dispatched", "completed", "rejected", "unknown"}),
    "dispatched": frozenset({"dispatched", "completed", "rejected", "unknown"}),
    "completed": frozenset(),
    "rejected": frozenset(),
    "unknown": frozenset(),
}


def validate_host_call_transition(
    current_status: str,
    target_status: str,
    *,
    callback_id: str = "",
) -> None:
    """Validate that a HostCallLedgerRecord transition is legal and monotonic."""
    allowed = HOST_CALL_TRANSITION_TABLE.get(current_status)
    if allowed is None or target_status not in allowed:
        detail = f" for callback {callback_id!r}" if callback_id else ""
        raise JsProtocolError(
            "ryn_js_invalid_host_call_transition",
            f"Illegal host_call state transition{detail}: {current_status!r} -> {target_status!r}",
        )


@dataclass
class HostCallLedgerRecord:
    """Host-side lifecycle state for a single QuickJS guest callback."""

    envelope: PendingHostCallEnvelope
    status: str = "pending_admission"
    approval_id: str | None = None
    operation_id: str | None = None
    outcome: str = "pending"
    not_executed: bool = True
    error: dict[str, Any] | None = None
    result: Any = None


@dataclass
class CoordinatedStepResult:
    """Outcome of a single coordinator step: either a pending host_call or a terminal snapshot."""

    status: str
    snapshot: JsExecutionSnapshot
    host_call: PendingHostCallEnvelope | None = None
    ledger_record: HostCallLedgerRecord | None = None


@dataclass
class _TaskCoordinationState:
    task_id: str
    session_id: str
    execution_id: str
    generation: int
    session: JsRuntimeSession | None = None
    records: dict[str, HostCallLedgerRecord] = field(default_factory=dict)
    ready_queue: deque[str] = field(default_factory=deque)
    deferred_writes: deque[str] = field(default_factory=deque)
    active_write_callback_id: str | None = None
    terminal_snapshot: JsExecutionSnapshot | None = None
    terminal_result: CoordinatedStepResult | None = None
    retired: bool = False


class JsExecutionCoordinator:
    """Bridges guest `host_call` suspension with Ryn's single-authority approval/ledger pipeline.

    This coordinator never invokes `LocalTools.perform` or `ExtensionToolAdapter.execute`
    directly. Instead, it surfaces validated `PendingHostCallEnvelope` items one step at a
    time (serializing desktop writes even if the guest uses `Promise.all`) so the outer
    agent runtime loop can run standard policy evaluation, human approval wait, operation
    ledger recording, and MCP/native dispatch before calling `resolve_host_call` or
    `reject_host_call`.
    """

    def __init__(
        self,
        manager: JsRuntimeManager,
        *,
        image_registry: HostImageRegistry | None = None,
    ) -> None:
        self.manager = manager
        if image_registry is not None and self.manager.image_registry is None:
            self.manager.image_registry = image_registry
        self._lock = threading.RLock()
        self._tasks: dict[str, _TaskCoordinationState] = {}
        self._history: dict[str, deque[_TaskCoordinationState]] = {}

    @property
    def image_registry(self) -> HostImageRegistry | None:
        return self.manager.image_registry

    def _archive_state_unlocked(self, state: _TaskCoordinationState) -> None:
        if state.retired:
            return
        bucket = self._history.get(state.task_id)
        if bucket is None:
            bucket = deque(maxlen=MAX_COORDINATED_HISTORY)
            self._history[state.task_id] = bucket
        if all(item.execution_id != state.execution_id for item in bucket):
            bucket.append(state)

    def start(
        self,
        task_id: str,
        code: str,
        *,
        execution_id: str | None = None,
        script_id: str | None = None,
        limits: dict[str, int] | None = None,
    ) -> tuple[str, int]:
        """Begin a new guest JS evaluation for `task_id` and return `(execution_id, generation)`."""
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            prev_state = self._tasks.get(key)
            if prev_state is not None and not prev_state.retired:
                if prev_state.terminal_snapshot is None:
                    raise JsProtocolError(
                        "ryn_js_busy",
                        f"Task {key!r} already has an active coordinated execution ({prev_state.execution_id}).",
                    )
                self._archive_state_unlocked(prev_state)

        session, exec_id = self.manager.evaluate(
            key,
            code,
            execution_id=execution_id,
            script_id=script_id,
            limits=limits,
        )
        with self._lock:
            self._tasks[key] = _TaskCoordinationState(
                task_id=key,
                session_id=session.session_id,
                execution_id=exec_id,
                generation=session.generation,
                session=session,
            )
        return exec_id, session.generation

    def poll_step(
        self,
        task_id: str,
        *,
        timeout: float = 0.25,
    ) -> CoordinatedStepResult:
        """Advance until either an actionable host_call is ready for host admission or execution finishes."""
        key = str(task_id or "").strip()
        with self._lock:
            state = self._tasks.get(key)
            if state is None:
                sess = self.manager.get_session(key, include_inactive=True)
                if sess is None:
                    raise JsProtocolError("ryn_js_no_session", f"No active QuickJS session for task {key!r}")
                raise JsProtocolError("ryn_js_no_execution", f"No coordinated execution for task {key!r}")
            if state.retired:
                raise JsProtocolError(
                    "ryn_js_no_execution",
                    f"Coordinated execution {state.execution_id!r} for task {key!r} has been retired",
                )
            if state.terminal_result is not None:
                return state.terminal_result
            session = state.session or self.manager.get_session(key, include_inactive=True)
            if session is None:
                raise JsProtocolError("ryn_js_no_session", f"No active QuickJS session for task {key!r}")
            exec_id = state.execution_id

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            while True:
                envelope = session.next_host_call(exec_id, timeout=0.0)
                if envelope is None:
                    break
                self._ingest_envelope(key, envelope)

            snapshot = session.poll(exec_id, timeout=0.0)
            if snapshot.status in {"completed", "failed", "cancelled", "lost"}:
                final_res = self._finalize_terminal_records(key, snapshot=snapshot)
                if final_res is not None:
                    return final_res
                return CoordinatedStepResult(
                    status=snapshot.status,
                    snapshot=snapshot,
                    host_call=None,
                    ledger_record=None,
                )

            next_record = self._pop_next_admissible_record(key)
            if next_record is not None:
                return CoordinatedStepResult(
                    status="host_call_pending",
                    snapshot=snapshot,
                    host_call=next_record.envelope,
                    ledger_record=next_record,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return CoordinatedStepResult(
                    status=snapshot.status,
                    snapshot=snapshot,
                    host_call=None,
                    ledger_record=None,
                )

            envelope = session.next_host_call(exec_id, timeout=min(0.05, remaining))
            if envelope is not None:
                self._ingest_envelope(key, envelope)

    def _ingest_envelope(self, task_id: str, envelope: PendingHostCallEnvelope) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if (
                state is None
                or state.retired
                or state.terminal_snapshot is not None
                or state.execution_id != envelope.execution_id
                or state.session_id != envelope.session_id
                or state.generation != envelope.generation
            ):
                return
            if envelope.callback_id in state.records:
                return
            record = HostCallLedgerRecord(envelope=envelope)
            state.records[envelope.callback_id] = record
            if envelope.effect == "write":
                if state.active_write_callback_id is None:
                    state.active_write_callback_id = envelope.callback_id
                    state.ready_queue.append(envelope.callback_id)
                else:
                    state.deferred_writes.append(envelope.callback_id)
            else:
                state.ready_queue.append(envelope.callback_id)

    def _pop_next_admissible_record(self, task_id: str) -> HostCallLedgerRecord | None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.retired or state.terminal_snapshot is not None:
                return None
            while state.ready_queue:
                cid = state.ready_queue.popleft()
                record = state.records.get(cid)
                if record is not None and record.status == "pending_admission":
                    validate_host_call_transition(record.status, "admitted", callback_id=cid)
                    record.status = "admitted"
                    return record
            return None

    def mark_awaiting_approval(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        approval_id: str | None = None,
        session_id: str | None = None,
    ) -> HostCallLedgerRecord:
        """Record that a pending guest callback is suspended waiting on owner approval."""
        key = str(task_id or "").strip()
        norm_approval_id = str(approval_id).strip() if approval_id is not None else None
        if approval_id is not None and not norm_approval_id:
            raise JsProtocolError("ryn_js_invalid_approval_id", "approval_id must not be empty when provided")
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            record = self._require_record_unlocked(
                key,
                execution_id=execution_id,
                generation=generation,
                callback_id=callback_id,
                session_id=session_id,
            )
            validate_host_call_transition(
                record.status,
                "awaiting_approval",
                callback_id=callback_id,
            )
            if record.status == "awaiting_approval":
                if (
                    record.approval_id is not None
                    and norm_approval_id is not None
                    and norm_approval_id != record.approval_id
                ):
                    raise JsProtocolError(
                        "ryn_js_invalid_host_call_transition",
                        f"Cannot rebind approval_id on callback {callback_id!r} from {record.approval_id!r} to {norm_approval_id!r}",
                    )
                if record.approval_id is None and norm_approval_id is not None:
                    record.approval_id = norm_approval_id
                return record

            record.status = "awaiting_approval"
            if norm_approval_id is not None:
                record.approval_id = norm_approval_id
            record.not_executed = True
            record.outcome = "awaiting_approval"
            return record

    def mark_dispatched(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> HostCallLedgerRecord:
        """Record that a callback passed policy/approval and has been dispatched to the provider."""
        key = str(task_id or "").strip()
        norm_operation_id = str(operation_id).strip() if operation_id is not None else None
        if operation_id is not None and not norm_operation_id:
            raise JsProtocolError("ryn_js_invalid_operation_id", "operation_id must not be empty when provided")
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            record = self._require_record_unlocked(
                key,
                execution_id=execution_id,
                generation=generation,
                callback_id=callback_id,
                session_id=session_id,
            )
            validate_host_call_transition(
                record.status,
                "dispatched",
                callback_id=callback_id,
            )
            if record.status == "dispatched":
                if (
                    record.operation_id is not None
                    and norm_operation_id is not None
                    and norm_operation_id != record.operation_id
                ):
                    raise JsProtocolError(
                        "ryn_js_invalid_host_call_transition",
                        f"Cannot rebind operation_id on callback {callback_id!r} from {record.operation_id!r} to {norm_operation_id!r}",
                    )
                if record.operation_id is None and norm_operation_id is not None:
                    record.operation_id = norm_operation_id
                return record

            record.status = "dispatched"
            if norm_operation_id is not None:
                record.operation_id = norm_operation_id
            record.not_executed = False
            record.outcome = "dispatched"
            return record

    def bind_post_commit_window_state_result(
        self,
        task_id: str,
        durable_payload: dict[str, Any] | None = None,
        *,
        execution_id: str,
        generation: int | None = None,
        callback_id: str,
        durable_outbox_payload: dict[str, Any] | None = None,
        native_worker_instance: str | None = None,
        native_worker_id: str | None = None,
        native_window: dict[str, Any] | None = None,
        operation_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        outbox_committed: bool = False,
    ) -> dict[str, Any]:
        """Mint transient capability descriptor URLs from a durable outbox window_state payload after commit."""
        if outbox_committed is not True:
            raise JsProtocolError(
                "ryn_js_image_precommit_forbidden",
                "Cannot mint or bind host screenshot capabilities before durable outbox commit",
            )
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            state = self._tasks.get(key)
            eff_gen = generation if generation is not None else (state.generation if state is not None else 1)
            record = self._require_record_unlocked(
                key,
                execution_id=execution_id,
                generation=eff_gen,
                callback_id=callback_id,
                session_id=session_id,
            )
            if record.status in TERMINAL_HOST_CALL_STATUSES:
                raise JsProtocolError(
                    "ryn_js_duplicate_host_result",
                    f"Callback {callback_id!r} was already settled ({record.status}).",
                )
            state = self._tasks[key]
            session = state.session or self.manager.get_session(key, include_inactive=True)
            if session is None or not session.is_alive:
                self._sync_session_terminal_state_unlocked(key)
                raise JsProtocolError(
                    "ryn_js_duplicate_host_result",
                    f"Callback {callback_id!r} cannot bind window_state because worker session is no longer active.",
                )
            if (
                operation_id is not None
                and record.operation_id is not None
                and str(operation_id).strip() != record.operation_id
            ):
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Conflicting operation_id {operation_id!r} for callback {callback_id!r} (expected {record.operation_id!r})",
                )
            eff_op_id = str(operation_id or record.operation_id or "").strip()
            if not eff_op_id:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Callback {callback_id!r} requires an explicit committed operation_id before post-commit binding",
                )
            if record.operation_id is None:
                record.operation_id = eff_op_id

        return session.bind_post_commit_window_state_result(
            durable_payload,
            durable_outbox_payload=durable_outbox_payload,
            execution_id=execution_id,
            generation=eff_gen,
            callback_id=callback_id,
            operation_id=eff_op_id,
            request_id=request_id,
            native_worker_instance=native_worker_instance,
            native_worker_id=native_worker_id,
            native_window=native_window,
            outbox_committed=outbox_committed,
        )

    def resolve_window_state_from_outbox(
        self,
        task_id: str,
        durable_payload: dict[str, Any] | None = None,
        *,
        execution_id: str,
        generation: int | None = None,
        callback_id: str,
        durable_outbox_payload: dict[str, Any] | None = None,
        native_worker_instance: str | None = None,
        native_worker_id: str | None = None,
        native_window: dict[str, Any] | None = None,
        operation_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        outbox_committed: bool = False,
    ) -> HostCallLedgerRecord:
        """Mint transient capability descriptors after durable outbox commit and deliver `host_result`."""
        key = str(task_id or "").strip()
        with self._lock:
            state = self._tasks.get(key)
            eff_gen = generation if generation is not None else (state.generation if state is not None else 1)

        bound_result = self.bind_post_commit_window_state_result(
            task_id,
            durable_payload,
            execution_id=execution_id,
            generation=eff_gen,
            callback_id=callback_id,
            durable_outbox_payload=durable_outbox_payload,
            native_worker_instance=native_worker_instance,
            native_worker_id=native_worker_id,
            native_window=native_window,
            operation_id=operation_id,
            request_id=request_id,
            session_id=session_id,
            outbox_committed=outbox_committed,
        )
        return self.resolve_host_call(
            task_id,
            execution_id=execution_id,
            generation=eff_gen,
            callback_id=callback_id,
            result=bound_result,
            session_id=session_id,
        )

    def resolve_host_call(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        result: Any,
        session_id: str | None = None,
    ) -> HostCallLedgerRecord:
        """Deliver a successful provider result back to the suspended QuickJS Promise."""
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            record = self._require_record_unlocked(
                key,
                execution_id=execution_id,
                generation=generation,
                callback_id=callback_id,
                session_id=session_id,
            )
            if record.status in TERMINAL_HOST_CALL_STATUSES:
                raise JsProtocolError(
                    "ryn_js_duplicate_host_result",
                    f"Callback {callback_id!r} was already settled ({record.status}).",
                )
            validate_host_call_transition(
                record.status,
                "completed",
                callback_id=callback_id,
            )
            state = self._tasks[key]
            session = state.session or self.manager.get_session(key, include_inactive=True)
            if session is None or not session.is_alive:
                self._sync_session_terminal_state_unlocked(key)
                raise JsProtocolError(
                    "ryn_js_duplicate_host_result",
                    f"Callback {callback_id!r} cannot be resolved because worker session is no longer active ({record.status}).",
                )
            eff_op_id = record.operation_id

        try:
            session.respond(
                session_id=session_id or session.session_id,
                execution_id=execution_id,
                generation=generation,
                callback_id=callback_id,
                ok=True,
                result=result,
                operation_id=eff_op_id,
            )
        except JsRuntimeError:
            with self._lock:
                self._sync_session_terminal_state_unlocked(key)
            raise

        with self._lock:
            record.status = "completed"
            record.outcome = "succeeded"
            record.not_executed = False
            record.result = sanitize_snapshot_value(result)
            self._release_write_slot_unlocked(key, callback_id)
            return record

    def reject_host_call(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        code: str,
        message: str = "",
        not_executed: bool = True,
        outcome: str | None = None,
        session_id: str | None = None,
    ) -> HostCallLedgerRecord:
        """Reject a suspended QuickJS Promise (e.g. policy deny, approval reject, or provider error)."""
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            record = self._require_record_unlocked(
                key,
                execution_id=execution_id,
                generation=generation,
                callback_id=callback_id,
                session_id=session_id,
            )
            if record.status in TERMINAL_HOST_CALL_STATUSES:
                raise JsProtocolError(
                    "ryn_js_duplicate_host_result",
                    f"Callback {callback_id!r} was already settled ({record.status}).",
                )
            was_dispatched = record.status == "dispatched"
            if was_dispatched:
                effective_not_executed = False
                if outcome in {None, "unknown", "not_executed"}:
                    resolved_outcome = "unknown"
                else:
                    resolved_outcome = str(outcome)
            else:
                if outcome == "unknown":
                    effective_not_executed = False
                    resolved_outcome = "unknown"
                else:
                    effective_not_executed = bool(not_executed)
                    resolved_outcome = str(outcome or ("not_executed" if effective_not_executed else "failed"))

            target_status = "unknown" if resolved_outcome == "unknown" else "rejected"
            validate_host_call_transition(
                record.status,
                target_status,
                callback_id=callback_id,
            )
            state = self._tasks[key]
            session = state.session or self.manager.get_session(key, include_inactive=True)
            if session is None or not session.is_alive:
                self._sync_session_terminal_state_unlocked(key)
                raise JsProtocolError(
                    "ryn_js_duplicate_host_result",
                    f"Callback {callback_id!r} cannot be rejected because worker session is no longer active ({record.status}).",
                )

        err_payload = {
            "code": str(code or "ryn_sky_host_rejected"),
            "message": str(message or code or "Host call rejected"),
            "not_executed": effective_not_executed,
            "outcome": resolved_outcome,
        }
        try:
            session.respond(
                session_id=session_id or session.session_id,
                execution_id=execution_id,
                generation=generation,
                callback_id=callback_id,
                ok=False,
                error=err_payload,
            )
        except JsRuntimeError:
            with self._lock:
                self._sync_session_terminal_state_unlocked(key)
            raise

        with self._lock:
            record.status = target_status
            record.outcome = resolved_outcome
            record.not_executed = effective_not_executed
            record.error = err_payload
            if target_status == "unknown":
                self._finalize_non_terminal_records_unlocked(
                    state,
                    reason_code=err_payload["code"],
                    reason_message=err_payload["message"],
                )
            else:
                self._release_write_slot_unlocked(key, callback_id)
            return record

    def get_record(
        self,
        task_id: str,
        callback_id: str,
        *,
        execution_id: str | None = None,
        generation: int | None = None,
    ) -> HostCallLedgerRecord | None:
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            state = self._tasks.get(key)
            if (
                state is not None
                and not state.retired
                and (execution_id is None or state.execution_id == execution_id)
                and (generation is None or state.generation == generation)
                and callback_id in state.records
            ):
                return state.records[callback_id]
            for past in reversed(self._history.get(key, ())):
                if past.retired:
                    continue
                if execution_id is not None and past.execution_id != execution_id:
                    continue
                if generation is not None and past.generation != generation:
                    continue
                if callback_id in past.records:
                    return past.records[callback_id]
            return None

    def get_terminal_snapshot(
        self,
        task_id: str,
        *,
        execution_id: str | None = None,
        generation: int | None = None,
    ) -> JsExecutionSnapshot | None:
        """Return the preserved terminal snapshot for the current or prior unretired generation."""
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            state = self._tasks.get(key)
            if (
                state is not None
                and not state.retired
                and state.terminal_snapshot is not None
                and (execution_id is None or state.execution_id == execution_id)
                and (generation is None or state.generation == generation)
            ):
                return state.terminal_snapshot
            for past in reversed(self._history.get(key, ())):
                if past.retired or past.terminal_snapshot is None:
                    continue
                if execution_id is not None and past.execution_id != execution_id:
                    continue
                if generation is not None and past.generation != generation:
                    continue
                return past.terminal_snapshot
            return None

    def get_unknown_records(self, task_id: str) -> list[HostCallLedgerRecord]:
        """Return all unretired HostCallLedgerRecord entries with status 'unknown' for `task_id`."""
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            found: list[HostCallLedgerRecord] = []
            for past in self._history.get(key, ()):
                if past.retired:
                    continue
                for rec in past.records.values():
                    if rec.status == "unknown":
                        found.append(rec)
            state = self._tasks.get(key)
            if state is not None and not state.retired:
                for rec in state.records.values():
                    if rec.status == "unknown":
                        found.append(rec)
            return found

    def retire_task(
        self,
        task_id: str,
        *,
        execution_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Retire consumed terminal coordination state(s) once the host owner has reconciled receipts."""
        key = str(task_id or "").strip()
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)
            state = self._tasks.get(key)
            if state is not None and not state.retired:
                matches_current = (
                    (execution_id is None or state.execution_id == execution_id)
                    and (generation is None or state.generation == generation)
                )
                if matches_current:
                    if state.terminal_snapshot is None:
                        raise JsProtocolError(
                            "ryn_js_busy",
                            f"Cannot retire active execution {state.execution_id!r} for task {key!r} before termination.",
                        )
                    state.retired = True
            for past in self._history.get(key, ()):
                if past.retired:
                    continue
                if execution_id is not None and past.execution_id != execution_id:
                    continue
                if generation is not None and past.generation != generation:
                    continue
                past.retired = True

    def update_task_scope(
        self,
        task_id: str,
        *,
        context_epoch: str | None = None,
        selection_scope: str | None = None,
    ) -> None:
        key = str(task_id or "").strip()
        self.manager.update_task_scope(
            key,
            context_epoch=str(context_epoch) if context_epoch is not None else None,
            selection_scope=selection_scope,
        )

    def drain_image_promotions(
        self,
        task_id: str,
        *,
        generation: int | None = None,
        execution_id: str | None = None,
        carrier: Callable[[PromotedImageAttachment], Any] | None = None,
        parent_request_id: str | None = None,
        max_promotions: int | None = None,
        max_total_bytes: int | None = None,
    ) -> list[ImagePromotionResult]:
        """Drain validated image promotion requests for the current or specified coordinated execution."""
        key = str(task_id or "").strip()
        return self.manager.drain_image_promotions(
            key,
            generation=generation,
            execution_id=execution_id,
            carrier=carrier,
            parent_request_id=parent_request_id,
            max_promotions=max_promotions,
            max_total_bytes=max_total_bytes,
        )

    def cancel_task(self, task_id: str, *, reason: str = "ryn_js_cancelled") -> JsExecutionSnapshot | None:
        key = str(task_id or "").strip()
        snapshot = self.manager.cancel_task(key, reason=reason)
        self._finalize_terminal_records(key, snapshot=snapshot)
        return snapshot

    def end_turn(self, task_id: str) -> None:
        key = str(task_id or "").strip()
        self.manager.end_turn(key)
        with self._lock:
            self._sync_session_terminal_state_unlocked(key)

    def close_task(self, task_id: str) -> None:
        key = str(task_id or "").strip()
        with self._lock:
            self._tasks.pop(key, None)
            self._history.pop(key, None)
        self.manager.close_task(key)

    def _release_write_slot_unlocked(self, task_id: str, callback_id: str) -> None:
        state = self._tasks.get(task_id)
        if state is None:
            return
        if state.active_write_callback_id == callback_id:
            state.active_write_callback_id = None
            while state.deferred_writes:
                next_cid = state.deferred_writes.popleft()
                next_rec = state.records.get(next_cid)
                if next_rec is not None and next_rec.status == "pending_admission":
                    state.active_write_callback_id = next_cid
                    state.ready_queue.append(next_cid)
                    break

    def _sync_session_terminal_state_unlocked(self, task_id: str) -> None:
        state = self._tasks.get(task_id)
        if state is None or state.retired or state.terminal_snapshot is not None:
            return
        session = state.session or self.manager.get_session(task_id, include_inactive=True)
        if session is None:
            return
        while True:
            try:
                envelope = session.next_host_call(state.execution_id, timeout=0.0)
            except Exception:
                break
            if envelope is None:
                break
            self._ingest_envelope(task_id, envelope)
        try:
            snap = session.poll(state.execution_id, timeout=0.0)
        except Exception:
            return
        if snap.status in {"completed", "failed", "cancelled", "lost"}:
            self._finalize_terminal_records(task_id, snapshot=snap)

    def _finalize_non_terminal_records_unlocked(
        self,
        state: _TaskCoordinationState,
        *,
        reason_code: str,
        reason_message: str,
    ) -> None:
        state.ready_queue.clear()
        state.deferred_writes.clear()
        state.active_write_callback_id = None
        for record in state.records.values():
            if record.status == "dispatched":
                record.status = "unknown"
                record.outcome = "unknown"
                record.not_executed = False
                record.result = None
                if record.error is None:
                    record.error = {
                        "code": reason_code,
                        "message": reason_message,
                        "not_executed": False,
                        "outcome": "unknown",
                    }
            elif record.status not in TERMINAL_HOST_CALL_STATUSES:
                record.status = "rejected"
                record.outcome = "not_executed"
                record.not_executed = True
                record.result = None
                if record.error is None:
                    record.error = {
                        "code": reason_code,
                        "message": reason_message,
                        "not_executed": True,
                        "outcome": "not_executed",
                    }

    def _finalize_terminal_records(
        self,
        task_id: str,
        *,
        snapshot: JsExecutionSnapshot | None = None,
    ) -> CoordinatedStepResult | None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return None
            if snapshot is None:
                session = state.session or self.manager.get_session(task_id, include_inactive=True)
                if session is not None:
                    try:
                        snapshot = session.poll(state.execution_id, timeout=0.0)
                    except Exception:
                        snapshot = None
            err_dict = (
                snapshot.error
                if snapshot is not None and isinstance(snapshot.error, dict)
                else None
            ) or {}
            reason_code = str(
                err_dict.get("code")
                or (f"ryn_js_{snapshot.status}" if snapshot is not None else "ryn_js_terminated")
            )
            reason_message = str(err_dict.get("message") or reason_code)
            self._finalize_non_terminal_records_unlocked(
                state,
                reason_code=reason_code,
                reason_message=reason_message,
            )
            if snapshot is not None and snapshot.status in {"completed", "failed", "cancelled", "lost"}:
                state.terminal_snapshot = snapshot
                state.terminal_result = CoordinatedStepResult(
                    status=snapshot.status,
                    snapshot=snapshot,
                    host_call=None,
                    ledger_record=None,
                )
            return state.terminal_result

    def _require_record_unlocked(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        session_id: str | None = None,
    ) -> HostCallLedgerRecord:
        state = self._tasks.get(task_id)
        if state is None or state.retired or state.execution_id != execution_id:
            raise JsProtocolError(
                "ryn_js_unknown_execution",
                f"Unknown execution {execution_id!r} for task {task_id!r}",
            )
        if session_id is not None and session_id != state.session_id:
            raise JsProtocolError(
                "ryn_js_session_mismatch",
                f"Session mismatch for task {task_id!r}: expected {state.session_id!r}, got {session_id!r}",
            )
        if type(generation) is not int or generation != state.generation:
            raise JsProtocolError(
                "ryn_js_stale_generation",
                f"Stale generation {generation} for task {task_id!r} (expected {state.generation})",
            )
        record = state.records.get(callback_id)
        if record is None:
            raise JsProtocolError(
                "ryn_js_unknown_callback",
                f"Unknown callback {callback_id!r} on execution {execution_id!r}",
            )
        return record
