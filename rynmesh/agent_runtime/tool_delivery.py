"""Transaction-local JS child completion composition: receipt + outbox + audit event.

Phase A module: mutates only the caller-supplied `row` inside an active
`AgentStore.transaction`. Never appends model-visible child messages, never
consumes `budget.notice`, never creates a second business ledger, and never
dispatches IPC before transaction commit.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from . import verification
from .js_state import (
    ROUTER_MAX_ARGUMENT_BYTES,
    TERMINAL_JS_PHASES,
    JsStateValidationError,
    _invalid_json_constant,
    plan_fatal_fence_js_state,
    validate_callback_status_transition,
    validate_js_parent_stamp,
    validate_js_state,
    validate_phase_transition,
    validate_result_reference,
    validate_task_js_state,
)
from .protocol import event as default_event_emitter
from .protocol import now, sanitized
from .state import synchronize
from .store import AgentError, encoded


@dataclass(frozen=True)
class DeliveryToken:
    """Inert post-commit token identifying a staged durable outbox record."""

    task_id: str
    outbox_id: str
    parent_operation_id: str
    parent_call_id: str
    session_id: str
    generation: int
    execution_id: str
    callback_id: str
    ordinal: int
    method: str
    tool_name: str
    call_id: str
    operation_id: str
    arguments_sha256: str
    receipt_id: str | None
    receipt_status: str
    outcome: str
    not_executed: bool
    result_ref: str | None
    payload: Any
    payload_sha256: str
    delivery_state: str
    deliverable: bool
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "outbox_id": self.outbox_id,
            "parent_operation_id": self.parent_operation_id,
            "parent_call_id": self.parent_call_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "execution_id": self.execution_id,
            "callback_id": self.callback_id,
            "ordinal": self.ordinal,
            "method": self.method,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
            "operation_id": self.operation_id,
            "arguments_sha256": self.arguments_sha256,
            "receipt_id": self.receipt_id,
            "receipt_status": self.receipt_status,
            "outcome": self.outcome,
            "not_executed": self.not_executed,
            "result_ref": self.result_ref,
            "payload": copy.deepcopy(self.payload),
            "payload_sha256": self.payload_sha256,
            "delivery_state": self.delivery_state,
            "deliverable": self.deliverable,
            "recorded_at": self.recorded_at,
        }


def is_js_child_call(call: Any) -> bool:
    """Return True if `call` carries a host-authored `js_parent` stamp."""
    return isinstance(call, dict) and call.get("js_parent") is not None


def derive_outbox_id(
    *,
    task_id: str,
    parent_operation_id: str,
    execution_id: str,
    generation: int,
    callback_id: str,
) -> str:
    raw = encoded({
        "task_id": task_id,
        "parent_operation_id": parent_operation_id,
        "execution_id": execution_id,
        "generation": generation,
        "callback_id": callback_id,
    })
    return "outbox_" + hashlib.sha256(raw).hexdigest()[:32]


def _token_from_outbox_item(task_id: str, item: dict[str, Any], *, deliverable: bool | None = None) -> DeliveryToken:
    is_deliverable = (item["delivery_state"] == "staged") if deliverable is None else bool(deliverable)
    return DeliveryToken(
        task_id=task_id,
        outbox_id=item["outbox_id"],
        parent_operation_id=item["parent_operation_id"],
        parent_call_id=item["parent_call_id"],
        session_id=item["session_id"],
        generation=item["generation"],
        execution_id=item["execution_id"],
        callback_id=item["callback_id"],
        ordinal=item["ordinal"],
        method=item["method"],
        tool_name=item["tool_name"],
        call_id=item["call_id"],
        operation_id=item["operation_id"],
        arguments_sha256=item["arguments_sha256"],
        receipt_id=item["receipt_id"],
        receipt_status=item["receipt_status"],
        outcome=item["outcome"],
        not_executed=item["not_executed"],
        result_ref=item["result_ref"],
        payload=copy.deepcopy(item["payload"]),
        payload_sha256=item["payload_sha256"],
        delivery_state=item["delivery_state"],
        deliverable=is_deliverable,
        recorded_at=item["recorded_at"],
    )


def _remove_exact_child_from_queue(
    queue: list[dict[str, Any]],
    *,
    call_id: str,
    operation_id: str,
    callback_id: str,
) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for item in queue:
        if not isinstance(item, dict):
            raise AgentError("agent_invalid_js_state")
        matches_call = item.get("call_id") == call_id
        matches_op = item.get("operation_id") == operation_id
        if matches_call or matches_op:
            item_parent = item.get("js_parent")
            if (
                not matches_call
                or not matches_op
                or not isinstance(item_parent, dict)
                or item_parent.get("callback_id") != callback_id
            ):
                raise AgentError("agent_js_callback_conflict")
            continue
        retained.append(item)
    return retained


def _validate_router_raw_and_semantic_args(item: dict[str, Any]) -> tuple[dict[str, Any], bytes, str]:
    """Validate preserved router raw JSON/digest and parsed effective semantic arguments."""
    item_args = item.get("arguments")
    if not isinstance(item_args, dict):
        raise AgentError("agent_js_callback_conflict")
    try:
        canonical_arg_bytes = encoded(item_args)
    except (TypeError, ValueError):
        raise AgentError("agent_js_callback_conflict") from None
    if len(canonical_arg_bytes) > ROUTER_MAX_ARGUMENT_BYTES:
        raise AgentError("agent_js_callback_conflict")
    canonical_arg_sha = hashlib.sha256(canonical_arg_bytes).hexdigest()

    has_args_json = "arguments_json" in item and item["arguments_json"] is not None
    has_raw_args_json = "raw_arguments_json" in item and item["raw_arguments_json"] is not None
    has_args_sha = "arguments_sha256" in item and item["arguments_sha256"] is not None
    err_res = item.get("error_result")

    if has_args_json:
        if not isinstance(item["arguments_json"], str):
            raise AgentError("agent_js_callback_conflict")
        raw_bytes = item["arguments_json"].encode("utf-8")
        if len(raw_bytes) > ROUTER_MAX_ARGUMENT_BYTES:
            raise AgentError("agent_js_callback_conflict")
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
        if has_args_sha and (not isinstance(item["arguments_sha256"], str) or item["arguments_sha256"] != raw_sha):
            raise AgentError("agent_js_callback_conflict")
        if err_res is None:
            try:
                parsed_json = json.loads(item["arguments_json"], parse_constant=_invalid_json_constant)
            except (TypeError, ValueError, RecursionError):
                raise AgentError("agent_js_callback_conflict") from None
            if not isinstance(parsed_json, dict) or parsed_json != item_args:
                raise AgentError("agent_js_callback_conflict")

    if has_raw_args_json:
        if not isinstance(item["raw_arguments_json"], str):
            raise AgentError("agent_js_callback_conflict")
        raw_bytes2 = item["raw_arguments_json"].encode("utf-8")
        if len(raw_bytes2) > ROUTER_MAX_ARGUMENT_BYTES:
            raise AgentError("agent_js_callback_conflict")
        if has_args_json and item["raw_arguments_json"] != item["arguments_json"]:
            raise AgentError("agent_js_callback_conflict")
        raw_sha2 = hashlib.sha256(raw_bytes2).hexdigest()
        if has_args_sha and (not isinstance(item["arguments_sha256"], str) or item["arguments_sha256"] != raw_sha2):
            raise AgentError("agent_js_callback_conflict")
        if err_res is None:
            try:
                parsed_raw = json.loads(item["raw_arguments_json"], parse_constant=_invalid_json_constant)
            except (TypeError, ValueError, RecursionError):
                raise AgentError("agent_js_callback_conflict") from None
            if not isinstance(parsed_raw, dict) or parsed_raw != item_args:
                raise AgentError("agent_js_callback_conflict")

    if not has_args_json and not has_raw_args_json:
        if has_args_sha:
            if not isinstance(item["arguments_sha256"], str) or item["arguments_sha256"] != canonical_arg_sha:
                raise AgentError("agent_js_callback_conflict")
        elif err_res is None:
            raise AgentError("agent_js_callback_conflict")
    elif err_res is None and not has_args_sha:
        raise AgentError("agent_js_callback_conflict")

    return item_args, canonical_arg_bytes, canonical_arg_sha


def _validate_canonical_child_completion(
    row: dict[str, Any],
    js_state: dict[str, Any],
    stamp: dict[str, Any],
    admitted: dict[str, Any],
    call: dict[str, Any],
    flight: dict[str, Any] | None,
    *,
    receipt_status: str,
    record_receipt: bool,
    existing_outbox: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate `call` and `flight` against preserved router JSON/hashes, semantic arguments, captured catalog, and original durable queue/in_flight/receipt records."""
    callback_id = stamp["callback_id"]
    method = stamp["method"]
    cat_entry = (js_state.get("capability_catalog") or {}).get(method)
    if js_state.get("capability_catalog") and not isinstance(cat_entry, dict):
        raise AgentError("agent_js_method_unavailable")

    if (
        call.get("call_id") != admitted["call_id"]
        or call.get("operation_id") != admitted["operation_id"]
        or call.get("name") != admitted["tool_name"]
    ):
        raise AgentError("agent_js_callback_conflict")
    if isinstance(cat_entry, dict) and (
        admitted["tool_name"] != cat_entry["tool_name"]
        or admitted["effect"] != cat_entry["effect"]
    ):
        raise AgentError("agent_js_callback_conflict")

    call_args, canonical_arg_bytes, canonical_arg_sha = _validate_router_raw_and_semantic_args(call)
    expected_tool_sha = admitted.get("tool_arguments_sha256")
    admitted_router_rejected = bool(admitted.get("router_rejected", False))

    if admitted_router_rejected and call.get("error_result") is None:
        raise AgentError("agent_js_callback_conflict")

    if call.get("error_result") is None:
        if expected_tool_sha is not None and canonical_arg_sha != expected_tool_sha:
            raise AgentError("agent_js_callback_conflict")
        if call.get("effect") != admitted["effect"]:
            raise AgentError("agent_js_callback_conflict")
        if isinstance(cat_entry, dict):
            if "schema_snapshot" in call and call["schema_snapshot"] is not None and call["schema_snapshot"] != cat_entry["schema_snapshot"]:
                raise AgentError("agent_js_callback_conflict")
            if "schema_sha256" in call and call["schema_sha256"] is not None and call["schema_sha256"] != cat_entry["schema_sha256"]:
                raise AgentError("agent_js_callback_conflict")
            if "computer_access" in call and bool(call["computer_access"]) != bool(cat_entry["computer_access"]):
                raise AgentError("agent_js_callback_conflict")
            if call.get("extension_binding") != cat_entry.get("extension_binding"):
                raise AgentError("agent_js_callback_conflict")
    else:
        err_res = call.get("error_result")
        if (
            receipt_status != "not_executed"
            or record_receipt
            or not isinstance(err_res, dict)
            or err_res.get("not_executed") is not True
            or err_res.get("no_mutation") is not True
            or not isinstance(err_res.get("error"), str)
            or call.get("effect") not in {None, "read", admitted["effect"]}
        ):
            raise AgentError("agent_js_callback_conflict")
        if admitted_router_rejected:
            if call_args != {} or (expected_tool_sha is not None and call.get("arguments_sha256") != expected_tool_sha):
                raise AgentError("agent_js_callback_conflict")
        elif expected_tool_sha is not None and call_args != {} and canonical_arg_sha != expected_tool_sha:
            raise AgentError("agent_js_callback_conflict")

    authority = js_state.get("authority") or {}
    if "context_epoch" in call and call["context_epoch"] != authority.get("context_epoch"):
        raise AgentError("agent_js_callback_conflict")
    if "goal_revision" in call and call["goal_revision"] != authority.get("goal"):
        raise AgentError("agent_js_callback_conflict")
    if "scope_revision" in call and call["scope_revision"] != authority.get("scope"):
        raise AgentError("agent_js_callback_conflict")

    def _assert_matches_durable_call(orig: dict[str, Any]) -> None:
        if (
            orig.get("call_id") != admitted["call_id"]
            or orig.get("operation_id") != admitted["operation_id"]
            or orig.get("name") != admitted["tool_name"]
        ):
            raise AgentError("agent_js_callback_conflict")
        try:
            orig_stamp = validate_js_parent_stamp(orig.get("js_parent"), js_state=js_state)
        except JsStateValidationError as exc:
            raise AgentError(exc.code) from None
        if orig_stamp != stamp:
            raise AgentError("agent_js_callback_conflict")

        orig_args, orig_canonical_bytes, orig_canonical_sha = _validate_router_raw_and_semantic_args(orig)

        if orig.get("error_result") is not None:
            # A genuine router-rejected call in durable queue can only complete as not_executed with empty effective args.
            if (
                call.get("error_result") is None
                or orig.get("error_result") != call.get("error_result")
                or orig_args != {}
                or call_args != {}
                or orig.get("arguments_json") != call.get("arguments_json")
                or orig.get("raw_arguments_json") != call.get("raw_arguments_json")
                or orig.get("arguments_sha256") != call.get("arguments_sha256")
                or (
                    admitted_router_rejected
                    and expected_tool_sha is not None
                    and orig.get("arguments_sha256") != expected_tool_sha
                )
            ):
                raise AgentError("agent_js_callback_conflict")
        else:
            if admitted_router_rejected:
                raise AgentError("agent_js_callback_conflict")
            if expected_tool_sha is not None and orig_canonical_sha != expected_tool_sha:
                raise AgentError("agent_js_callback_conflict")
            if orig.get("effect") != admitted["effect"]:
                raise AgentError("agent_js_callback_conflict")
            if isinstance(cat_entry, dict):
                if "schema_snapshot" in orig and orig["schema_snapshot"] is not None and orig["schema_snapshot"] != cat_entry["schema_snapshot"]:
                    raise AgentError("agent_js_callback_conflict")
                if "schema_sha256" in orig and orig["schema_sha256"] is not None and orig["schema_sha256"] != cat_entry["schema_sha256"]:
                    raise AgentError("agent_js_callback_conflict")
                if "computer_access" in orig and bool(orig["computer_access"]) != bool(cat_entry["computer_access"]):
                    raise AgentError("agent_js_callback_conflict")
                if orig.get("extension_binding") != cat_entry.get("extension_binding"):
                    raise AgentError("agent_js_callback_conflict")
            if call.get("error_result") is None:
                if (
                    orig_args != call_args
                    or orig_canonical_bytes != canonical_arg_bytes
                    or orig_canonical_sha != canonical_arg_sha
                ):
                    raise AgentError("agent_js_callback_conflict")
                if (
                    "arguments_json" in orig
                    and orig["arguments_json"] is not None
                    and "arguments_json" in call
                    and call["arguments_json"] is not None
                    and (
                        orig["arguments_json"] != call["arguments_json"]
                        or orig.get("arguments_sha256") != call.get("arguments_sha256")
                    )
                ):
                    raise AgentError("agent_js_callback_conflict")
            else:
                if call_args not in ({}, orig_args):
                    raise AgentError("agent_js_callback_conflict")
                if (
                    "arguments_json" in orig
                    and orig["arguments_json"] is not None
                    and "arguments_json" in call
                    and call["arguments_json"] is not None
                    and (
                        orig["arguments_json"] != call["arguments_json"]
                        or orig.get("arguments_sha256") != call.get("arguments_sha256")
                    )
                ):
                    raise AgentError("agent_js_callback_conflict")

    matching_queued: list[dict[str, Any]] = []
    for item in row.get("queue", []):
        if not isinstance(item, dict):
            raise AgentError("agent_invalid_js_state")
        item_parent = item.get("js_parent")
        if (
            item.get("call_id") == call["call_id"]
            or item.get("operation_id") == call["operation_id"]
            or (isinstance(item_parent, dict) and item_parent.get("callback_id") == callback_id)
        ):
            matching_queued.append(item)
    if len(matching_queued) > 1:
        raise AgentError("agent_js_callback_conflict")
    queued_orig = matching_queued[0] if matching_queued else None
    if queued_orig is not None:
        _assert_matches_durable_call(queued_orig)

    current_flight = row.get("in_flight")
    flight_orig: dict[str, Any] | None = None
    if isinstance(current_flight, dict):
        flight_parent = current_flight.get("js_parent")
        if (
            current_flight.get("call_id") == call["call_id"]
            or current_flight.get("operation_id") == call["operation_id"]
            or (isinstance(flight_parent, dict) and flight_parent.get("callback_id") == callback_id)
        ):
            flight_orig = current_flight
            _assert_matches_durable_call(flight_orig)
        else:
            raise AgentError("agent_js_callback_conflict")

    if flight is not None:
        if not isinstance(flight, dict):
            raise AgentError("agent_js_callback_conflict")
        _assert_matches_durable_call(flight)

    existing_receipt = next(
        (
            r
            for r in row.get("receipts", [])
            if isinstance(r, dict)
            and (
                r.get("operation_id") == call["operation_id"]
                or (r.get("call_id") is not None and r.get("call_id") == call["call_id"])
            )
        ),
        None,
    )
    if existing_receipt is not None:
        if (
            existing_receipt.get("operation_id") != call["operation_id"]
            or existing_receipt.get("id") != "receipt_" + call["operation_id"]
            or (existing_receipt.get("call_id") is not None and existing_receipt.get("call_id") != call["call_id"])
            or existing_receipt.get("tool") != call["name"]
            or existing_receipt.get("arguments") != call_args
            or existing_receipt.get("arguments_sha256") != canonical_arg_sha
            or (
                expected_tool_sha is not None
                and not admitted_router_rejected
                and existing_receipt.get("arguments_sha256") != expected_tool_sha
            )
            or existing_receipt.get("effect") != admitted["effect"]
            or existing_receipt.get("goal_revision") != authority.get("goal")
            or existing_receipt.get("scope_revision") != authority.get("scope")
            or existing_receipt.get("context_epoch") != authority.get("context_epoch")
        ):
            raise AgentError("agent_js_callback_conflict")
        if isinstance(cat_entry, dict) and isinstance(cat_entry.get("extension_binding"), dict):
            if existing_receipt.get("extension_binding") != cat_entry["extension_binding"]:
                raise AgentError("agent_js_callback_conflict")
        if existing_receipt.get("js_parent") is not None and existing_receipt.get("js_parent") != stamp:
            raise AgentError("agent_js_callback_conflict")

    if existing_outbox is None and queued_orig is None and flight_orig is None:
        raise AgentError("agent_js_callback_conflict")

    return copy.deepcopy(flight_orig or queued_orig or call)


def stage_child_completion(
    row: dict[str, Any],
    call: dict[str, Any],
    *,
    receipt_status: str,
    result: Any = None,
    flight: dict[str, Any] | None = None,
    event_emitter: Callable[..., None] = default_event_emitter,
    at: str | None = None,
    record_receipt: bool = True,
    result_ref: dict[str, Any] | str | None = None,
    timing: dict[str, Any] | None = None,
    event_kind: str | None = None,
    trusted_reconcile: bool = False,
) -> DeliveryToken:
    """Compose a JS child completion atomically inside the caller's store transaction.

    - Validates `call['js_parent']` and canonical tool arguments against
      `row['js_execution']`, `admitted_callbacks`, and the original durable
      queued/in-flight call before any mutation.
    - Records the business receipt via `verification.record` (unless `record_receipt=False`
      for a pre-dispatch router rejection).
    - Removes only the exact matching child item from `row['queue']` and `row['in_flight']`.
    - Never appends a child `role: tool` or `role: assistant` message to `row['messages']`.
    - Never consumes `budget.notice`.
    - Stages one unique bounded entry in `row['js_execution']['delivery_outbox']` (or sets
      `fatal_fence` when `receipt_status == 'unknown'`).
    - Returns an inert `DeliveryToken` for post-commit continuation dispatch.
    """
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise AgentError("agent_invalid_js_state")
    if not isinstance(call, dict):
        raise AgentError("agent_invalid_js_state")
    if call.get("internal"):
        raise AgentError("agent_js_internal_child_forbidden")

    try:
        js_state = validate_task_js_state(row)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None
    if js_state is None:
        raise AgentError("agent_js_no_active_parent")

    try:
        stamp = validate_js_parent_stamp(call.get("js_parent"), js_state=js_state)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None

    callback_id = stamp["callback_id"]
    admitted = js_state["admitted_callbacks"].get(callback_id)
    if not isinstance(admitted, dict):
        raise AgentError("agent_js_unknown_callback")

    existing_outbox = next(
        (
            item
            for item in js_state["delivery_outbox"]
            if item["callback_id"] == callback_id or item["operation_id"] == call.get("operation_id")
        ),
        None,
    )

    durable_call = _validate_canonical_child_completion(
        row,
        js_state,
        stamp,
        admitted,
        call,
        flight,
        receipt_status=receipt_status,
        record_receipt=record_receipt,
        existing_outbox=existing_outbox,
    )

    timestamp = at if isinstance(at, str) and at.strip() else now()
    outbox_id = derive_outbox_id(
        task_id=row["id"],
        parent_operation_id=stamp["parent_operation_id"],
        execution_id=stamp["execution_id"],
        generation=stamp["generation"],
        callback_id=callback_id,
    )

    if js_state["phase"] == "completed":
        raise AgentError("agent_js_parent_already_terminal")

    active_fence = js_state.get("fatal_fence")
    if active_fence is not None:
        is_same_fenced_op = active_fence.get("operation_id") == durable_call["operation_id"]
        if receipt_status == "unknown" and is_same_fenced_op:
            pass
        elif trusted_reconcile and is_same_fenced_op and receipt_status in {"confirmed", "not_executed"}:
            pass
        else:
            raise AgentError("agent_js_fenced_unknown")

    # Path 1: Dispatched write with unknown outcome -> fatal_fence + existing ambiguity marker.
    if receipt_status == "unknown":
        if not record_receipt:
            raise AgentError("agent_js_receipt_required")
        if result_ref is not None:
            raise AgentError("agent_invalid_js_state")
        effective_flight = copy.deepcopy(flight or row.get("in_flight") or durable_call)
        effective_flight.update(
            call_id=durable_call["call_id"],
            operation_id=durable_call["operation_id"],
            name=durable_call["name"],
            arguments=copy.deepcopy(durable_call["arguments"]),
            arguments_json=durable_call.get("arguments_json", encoded(durable_call["arguments"]).decode("utf-8")),
            arguments_sha256=durable_call.get(
                "arguments_sha256",
                hashlib.sha256(encoded(durable_call["arguments"])).hexdigest(),
            ),
            effect=admitted["effect"],
            kind="tool",
            dispatched=True,
            js_parent=copy.deepcopy(stamp),
        )
        try:
            fenced_state = plan_fatal_fence_js_state(
                js_state,
                operation_id=durable_call["operation_id"],
                callback_id=callback_id,
                reason="agent_write_outcome_unknown",
                recorded_at=timestamp,
            )
            receipt_id = "receipt_" + durable_call["operation_id"]
            if receipt_id not in fenced_state["child_receipt_refs"]:
                fenced_state["child_receipt_refs"].append(receipt_id)
            fenced_state = validate_js_state(fenced_state, task_row=row)  # type: ignore[assignment]
        except JsStateValidationError as exc:
            raise AgentError(exc.code) from None

        verification.record(row, effective_flight, "unknown", timestamp, flight=effective_flight)
        rec = next((r for r in row.get("receipts", []) if r.get("operation_id") == durable_call["operation_id"]), None)
        if rec is not None:
            rec["js_parent"] = copy.deepcopy(stamp)
        row["in_flight"] = effective_flight
        row.update(
            status="interrupted",
            error="agent_write_outcome_unknown",
            approved=None,
            pending_approval=None,
            js_execution=fenced_state,
        )
        synchronize(row)
        event_emitter(
            row,
            event_kind or "interrupted",
            error="agent_write_outcome_unknown",
            ambiguous_write=True,
            tool=durable_call["name"],
            call_id=durable_call["call_id"],
            operation_id=durable_call["operation_id"],
            js_parent=copy.deepcopy(stamp),
        )
        unknown_payload = {"error": "agent_write_outcome_unknown", "not_executed": False, "outcome": "unknown"}
        return DeliveryToken(
            task_id=row["id"],
            outbox_id=outbox_id,
            parent_operation_id=stamp["parent_operation_id"],
            parent_call_id=stamp["parent_call_id"],
            session_id=stamp["session_id"],
            generation=stamp["generation"],
            execution_id=stamp["execution_id"],
            callback_id=callback_id,
            ordinal=stamp["ordinal"],
            method=stamp["method"],
            tool_name=admitted["tool_name"],
            call_id=durable_call["call_id"],
            operation_id=durable_call["operation_id"],
            arguments_sha256=stamp["arguments_sha256"],
            receipt_id="receipt_" + durable_call["operation_id"],
            receipt_status="unknown",
            outcome="unknown",
            not_executed=False,
            result_ref=None,
            payload=unknown_payload,
            payload_sha256=hashlib.sha256(encoded(unknown_payload)).hexdigest(),
            delivery_state="lost",
            deliverable=False,
            recorded_at=timestamp,
        )

    # Path 2: Known child outcome (confirmed, not_executed, failed).
    if receipt_status not in {"confirmed", "not_executed", "failed"}:
        raise AgentError("agent_invalid_js_state")
    if not record_receipt and receipt_status != "not_executed":
        raise AgentError("agent_js_receipt_required")

    outcome_map = {
        "confirmed": ("succeeded", False),
        "not_executed": ("not_executed", True),
        "failed": ("failed", False),
    }
    outcome, not_executed = outcome_map[receipt_status]

    if result is None:
        if receipt_status == "confirmed":
            raw_payload: Any = {}
        elif isinstance(durable_call.get("error_result"), dict):
            raw_payload = copy.deepcopy(durable_call["error_result"])
        elif isinstance(call.get("error_result"), dict):
            raw_payload = copy.deepcopy(call["error_result"])
        else:
            raw_payload = {
                "error": "agent_tool_rejected" if not_executed else "agent_tool_failed",
                "not_executed": not_executed,
                "no_mutation": not_executed,
            }
    else:
        raw_payload = copy.deepcopy(result)

    payload = sanitized(raw_payload)
    if isinstance(payload, dict) and "server_note" in payload:
        # Never attach or consume model budget.notice on a JS child completion.
        payload = {k: v for k, v in payload.items() if k != "server_note"}

    try:
        payload_bytes = encoded(payload)
    except (TypeError, ValueError):
        raise AgentError("agent_invalid_output") from None
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

    try:
        ref_id, ref_entry = validate_result_reference(
            result_ref,
            task_id=row["id"],
            tool_name=durable_call["name"],
            operation_id=durable_call["operation_id"],
            call_id=durable_call["call_id"],
            authority=js_state.get("authority"),
        )
        if ref_entry is None and ref_id is not None:
            matched_row_ref = next(
                (
                    r
                    for r in row.get("result_refs", [])
                    if isinstance(r, dict) and r.get("id") == ref_id
                ),
                None,
            )
            if matched_row_ref is not None:
                _, ref_entry = validate_result_reference(
                    matched_row_ref,
                    task_id=row["id"],
                    tool_name=durable_call["name"],
                    operation_id=durable_call["operation_id"],
                    call_id=durable_call["call_id"],
                    authority=js_state.get("authority"),
                )
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None

    ref_meta_sha256: str | None = None
    if ref_entry is not None:
        ref_meta_sha256 = hashlib.sha256(encoded(ref_entry)).hexdigest()
        for existing_ref in row.get("result_refs", []):
            if isinstance(existing_ref, dict) and existing_ref.get("id") == ref_entry["id"]:
                if existing_ref != ref_entry:
                    raise AgentError("agent_js_outbox_conflict")

    receipt_id = ("receipt_" + durable_call["operation_id"]) if record_receipt else None

    if existing_outbox is not None:
        if (
            existing_outbox["outbox_id"] != outbox_id
            or existing_outbox["callback_id"] != callback_id
            or existing_outbox["parent_operation_id"] != stamp["parent_operation_id"]
            or existing_outbox["parent_call_id"] != stamp["parent_call_id"]
            or existing_outbox["session_id"] != stamp["session_id"]
            or existing_outbox["generation"] != stamp["generation"]
            or existing_outbox["execution_id"] != stamp["execution_id"]
            or existing_outbox["ordinal"] != stamp["ordinal"]
            or existing_outbox["method"] != stamp["method"]
            or existing_outbox["tool_name"] != admitted["tool_name"]
            or existing_outbox["arguments_sha256"] != stamp["arguments_sha256"]
            or existing_outbox["call_id"] != durable_call["call_id"]
            or existing_outbox["operation_id"] != durable_call["operation_id"]
            or existing_outbox["receipt_id"] != receipt_id
            or existing_outbox["receipt_status"] != receipt_status
            or existing_outbox["outcome"] != outcome
            or existing_outbox["not_executed"] != not_executed
            or existing_outbox["result_ref"] != ref_id
            or (
                existing_outbox.get("result_ref_sha256") is not None
                and ref_meta_sha256 is not None
                and existing_outbox["result_ref_sha256"] != ref_meta_sha256
            )
            or existing_outbox["payload_sha256"] != payload_sha256
        ):
            raise AgentError("agent_js_outbox_conflict")
        return _token_from_outbox_item(row["id"], existing_outbox)

    delivery_state = "staged" if js_state["phase"] not in TERMINAL_JS_PHASES else "lost"
    outbox_item: dict[str, Any] = {
        "outbox_id": outbox_id,
        "parent_operation_id": stamp["parent_operation_id"],
        "parent_call_id": stamp["parent_call_id"],
        "session_id": stamp["session_id"],
        "generation": stamp["generation"],
        "execution_id": stamp["execution_id"],
        "callback_id": callback_id,
        "ordinal": stamp["ordinal"],
        "method": stamp["method"],
        "tool_name": admitted["tool_name"],
        "arguments_sha256": stamp["arguments_sha256"],
        "call_id": durable_call["call_id"],
        "operation_id": durable_call["operation_id"],
        "receipt_id": receipt_id,
        "receipt_status": receipt_status,
        "outcome": outcome,
        "not_executed": not_executed,
        "result_ref": ref_id,
        "payload": copy.deepcopy(payload),
        "payload_sha256": payload_sha256,
        "delivery_state": delivery_state,
        "recorded_at": timestamp,
    }
    if ref_meta_sha256 is not None:
        outbox_item["result_ref_sha256"] = ref_meta_sha256

    target_cb_status = "completed" if receipt_status == "confirmed" else "rejected"
    try:
        admitted["status"] = validate_callback_status_transition(admitted["status"], target_cb_status)
        if trusted_reconcile and js_state["fatal_fence"] is not None:
            js_state["fatal_fence"] = None
        if js_state["phase"] not in TERMINAL_JS_PHASES:
            js_state["phase"] = validate_phase_transition(
                js_state["phase"],
                "delivering",
                fatal_fence=js_state["fatal_fence"],
                trusted_reconcile=trusted_reconcile,
            )
        if receipt_id is not None and receipt_id not in js_state["child_receipt_refs"]:
            js_state["child_receipt_refs"].append(receipt_id)
        js_state["delivery_outbox"].append(outbox_item)
        validated_js_state = validate_js_state(js_state, task_row=row)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None

    new_queue = _remove_exact_child_from_queue(
        list(row.get("queue", [])),
        call_id=durable_call["call_id"],
        operation_id=durable_call["operation_id"],
        callback_id=callback_id,
    )

    current_flight = row.get("in_flight")
    if isinstance(current_flight, dict):
        flight_call_id = current_flight.get("call_id")
        flight_op_id = current_flight.get("operation_id")
        if flight_call_id == durable_call["call_id"] or flight_op_id == durable_call["operation_id"]:
            if (
                (flight_call_id is not None and flight_call_id != durable_call["call_id"])
                or (flight_op_id is not None and flight_op_id != durable_call["operation_id"])
            ):
                raise AgentError("agent_js_callback_conflict")
            row["in_flight"] = None

    pending_approval = row.get("pending_approval")
    if isinstance(pending_approval, dict) and pending_approval.get("call_id") == durable_call["call_id"]:
        row["pending_approval"] = None
    approved = row.get("approved")
    if isinstance(approved, dict) and approved.get("call_id") == durable_call["call_id"]:
        row["approved"] = None

    if ref_entry is not None:
        refs_list = row.setdefault("result_refs", [])
        if not any(isinstance(r, dict) and r.get("id") == ref_entry["id"] for r in refs_list):
            refs_list.append(ref_entry)

    if record_receipt:
        effective_flight = flight if isinstance(flight, dict) else (current_flight if isinstance(current_flight, dict) else None)
        verification.record(row, durable_call, receipt_status, timestamp, result=payload, flight=effective_flight)
        rec = next((r for r in row.get("receipts", []) if r.get("operation_id") == durable_call["operation_id"]), None)
        if rec is None:
            raise AgentError("agent_js_receipt_missing")
        rec["js_parent"] = copy.deepcopy(stamp)

    row["queue"] = new_queue
    row["js_execution"] = validated_js_state

    effective_event_kind = event_kind or ("tool_rejected" if not record_receipt else "js_child_result")
    extra_event_fields: dict[str, Any] = {
        "tool": durable_call["name"],
        "call_id": durable_call["call_id"],
        "operation_id": durable_call["operation_id"],
        "result": copy.deepcopy(payload),
        "outbox_id": outbox_id,
        "receipt_status": receipt_status,
        "js_parent": copy.deepcopy(stamp),
    }
    if isinstance(timing, dict):
        extra_event_fields["timing"] = copy.deepcopy(timing)
    event_emitter(row, effective_event_kind, **extra_event_fields)

    return _token_from_outbox_item(row["id"], outbox_item)


def stage_tool_completion(
    row: dict[str, Any],
    call: dict[str, Any],
    *,
    receipt_status: str,
    result: Any = None,
    flight: dict[str, Any] | None = None,
    event_emitter: Callable[..., None] = default_event_emitter,
    at: str | None = None,
    record_receipt: bool = True,
    result_ref: dict[str, Any] | str | None = None,
    timing: dict[str, Any] | None = None,
    event_kind: str | None = None,
    trusted_reconcile: bool = False,
) -> DeliveryToken:
    """Entry-point alias for staging a host-stamped JS child tool completion."""
    return stage_child_completion(
        row,
        call,
        receipt_status=receipt_status,
        result=result,
        flight=flight,
        event_emitter=event_emitter,
        at=at,
        record_receipt=record_receipt,
        result_ref=result_ref,
        timing=timing,
        event_kind=event_kind,
        trusted_reconcile=trusted_reconcile,
    )
