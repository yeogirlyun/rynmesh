"""Pure callback-to-child planning and thin post-commit continuation delivery.

Phase A module: explicitly unmounted.
- Uses captured capability catalog, explicit trusted `host_method_mapping`, and
  existing `tool_router.normalize_tool_calls`.
- Recomputes `effect` and `computer_access` from host metadata; never trusts
  guest `effect` or `target_tool` and never infers MCP aliases or fabricates
  `WindowRef` tokens.
- Derives deterministic child `call_id` and `operation_id` from the full
  callback identity + canonical argument sha256.
- Dispatches only an already-committed `DeliveryToken` through an explicitly
  injected `coordinator_port`; never starts a manager/worker, never evaluates
  JS source, never retries uncertain sends, and never calls `tools.execute`.
"""
from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .js_runtime.protocol import (
    MAX_RESULT_BYTES,
    SAFE_ID_RE,
    SKY_METHOD_SPECS,
    JsProtocolError,
    JsRuntimeError,
    PendingHostCallEnvelope,
    validate_identifier,
    validate_sky_host_call,
)
from .js_state import (
    CONTAINER_TOOL_NAME,
    MAX_ADMITTED_CALLBACKS,
    TERMINAL_JS_PHASES,
    TOOL_NAME_RE,
    JsStateValidationError,
    _schema_digest,
    authority_matches_row,
    plan_continuation_lost_js_state,
    validate_capability_catalog,
    validate_delivery_transition,
    validate_js_parent_stamp,
    validate_js_state,
    validate_phase_transition,
    validate_result_reference,
    validate_task_js_state,
)
from .prompt import INTERNAL_NAMES
from .protocol import event as default_event_emitter
from .store import AgentError, encoded
from .tool_delivery import DeliveryToken, is_js_child_call
from .tool_router import ToolCallProtocolError, _function_spec, normalize_tool_calls


@dataclass(frozen=True)
class DeliveryPlan:
    """Bounded post-commit continuation action for a single durable outbox item."""

    action: str  # "resolve" | "reject" | "continuation_lost" | "fenced_unknown"
    task_id: str
    outbox_id: str
    session_id: str
    generation: int
    execution_id: str
    callback_id: str
    result: Any = None
    code: str | None = None
    message: str = ""
    not_executed: bool = True
    outcome: str = "not_executed"
    reason: str | None = None


def capture_capability_catalog(
    specs: list[dict[str, Any]],
    metadata_source: Callable[[str], dict[str, Any]] | Mapping[str, dict[str, Any]],
    host_method_mapping: Mapping[str, str] | None,
) -> dict[str, dict[str, Any]]:
    """Build a frozen capability catalog from registered host specs and explicit Sky mapping.

    Logical `SKY_METHOD_SPECS` target names (`computer_observe_windows`, etc.) are
    never authority to select an MCP alias (`mcp__*`) or infer a tool by similar name.
    Missing or mismatched registrations fail closed.
    """
    if not isinstance(host_method_mapping, Mapping) or not host_method_mapping:
        raise AgentError("agent_js_method_unavailable")
    if not isinstance(specs, list):
        raise AgentError("agent_tool_definition_changed")

    spec_by_name: dict[str, dict[str, Any]] = {}
    for item in specs:
        func = _function_spec(item)
        name = func["name"]
        if name in spec_by_name:
            raise AgentError("agent_tool_definition_changed")
        spec_by_name[name] = copy.deepcopy(func)

    catalog: dict[str, dict[str, Any]] = {}
    for method, target_tool_name in host_method_mapping.items():
        if not isinstance(method, str) or method not in SKY_METHOD_SPECS:
            raise AgentError("agent_js_method_unavailable")
        if not isinstance(target_tool_name, str) or not TOOL_NAME_RE.fullmatch(target_tool_name):
            raise AgentError("agent_js_method_unavailable")
        # Never allow container recursion, internal tools, or MCP alias selection.
        if (
            target_tool_name == CONTAINER_TOOL_NAME
            or target_tool_name in INTERNAL_NAMES
            or target_tool_name.startswith("mcp__")
        ):
            raise AgentError("agent_js_method_unavailable")
        function_spec = spec_by_name.get(target_tool_name)
        if function_spec is None:
            raise AgentError("agent_js_method_unavailable")

        try:
            meta = (
                metadata_source[target_tool_name]
                if isinstance(metadata_source, Mapping)
                else metadata_source(target_tool_name)
            )
        except (KeyError, ValueError, AgentError):
            raise AgentError("agent_js_method_unavailable") from None

        if not isinstance(meta, dict):
            raise AgentError("agent_tool_definition_changed")
        expected_effect = SKY_METHOD_SPECS[method]["effect"]
        host_effect = meta.get("effect")
        if host_effect not in {"read", "write"} or host_effect != expected_effect:
            raise AgentError("agent_js_effect_mismatch")

        entry: dict[str, Any] = {
            "method": method,
            "tool_name": target_tool_name,
            "effect": host_effect,
            "schema_snapshot": copy.deepcopy(function_spec),
            "schema_sha256": _schema_digest(function_spec),
            "computer_access": bool(
                meta.get("computer_access")
                or (meta.get("extension_binding") or {}).get("computer_access")
                or target_tool_name.startswith(("ryn_computer_", "ryn_browser_"))
            ),
        }
        if isinstance(meta.get("extension_binding"), dict):
            entry["extension_binding"] = copy.deepcopy(meta["extension_binding"])
        if "scope_context_supported" in meta:
            entry["scope_context_supported"] = bool(meta["scope_context_supported"])
        if "preview_precondition" in meta:
            entry["preview_precondition"] = bool(meta["preview_precondition"])
        if isinstance(meta.get("title"), str):
            entry["title"] = meta["title"][:256]
        catalog[method] = entry

    try:
        return validate_capability_catalog(catalog)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None


def derive_child_identities(
    *,
    task_id: str,
    parent_operation_id: str,
    session_id: str,
    generation: int,
    execution_id: str,
    callback_id: str,
    ordinal: int,
    method: str,
    arguments_sha256: str,
) -> tuple[str, str]:
    """Derive deterministic `(call_id, operation_id)` from callback identity + canonical args hash."""
    seed = encoded({
        "task_id": task_id,
        "parent_operation_id": parent_operation_id,
        "session_id": session_id,
        "generation": generation,
        "execution_id": execution_id,
        "callback_id": callback_id,
        "ordinal": ordinal,
        "method": method,
        "arguments_sha256": arguments_sha256,
    })
    digest_hex = hashlib.sha256(seed).hexdigest()
    return f"jsc_{digest_hex[:32]}", f"jsop_{digest_hex[:32]}"


def _coerce_envelope(envelope: PendingHostCallEnvelope | dict[str, Any]) -> PendingHostCallEnvelope:
    if isinstance(envelope, PendingHostCallEnvelope):
        return envelope
    if not isinstance(envelope, dict):
        raise AgentError("agent_js_invalid_envelope")
    required = {
        "task_id",
        "session_id",
        "generation",
        "execution_id",
        "callback_id",
        "ordinal",
        "method",
        "effect",
        "target_tool",
        "arguments",
        "arguments_sha256",
    }
    if set(envelope.keys()) != required:
        raise AgentError("agent_js_invalid_envelope")
    return PendingHostCallEnvelope(
        task_id=envelope["task_id"],
        session_id=envelope["session_id"],
        generation=envelope["generation"],
        execution_id=envelope["execution_id"],
        callback_id=envelope["callback_id"],
        ordinal=envelope["ordinal"],
        method=envelope["method"],
        effect=envelope["effect"],
        target_tool=envelope["target_tool"],
        arguments=copy.deepcopy(envelope["arguments"]),
        arguments_sha256=envelope["arguments_sha256"],
    )


def _existing_task_call_ids(row: dict[str, Any], *, exclude_call_id: str | None = None) -> set[str]:
    seen: set[str] = set()
    for msg in row.get("messages", []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool" and isinstance(msg.get("tool_call_id"), str):
            seen.add(msg["tool_call_id"])
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                cid = tc.get("id") or tc.get("call_id")
                if isinstance(cid, str):
                    seen.add(cid)
    for queued in row.get("queue", []):
        if isinstance(queued, dict) and isinstance(queued.get("call_id"), str):
            seen.add(queued["call_id"])
    for cb in ((row.get("js_execution") or {}).get("admitted_callbacks") or {}).values():
        if isinstance(cb, dict) and isinstance(cb.get("call_id"), str):
            seen.add(cb["call_id"])
    if exclude_call_id is not None:
        seen.discard(exclude_call_id)
    return seen


def _catalog_entry_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "effect": entry["effect"],
        "computer_access": bool(entry["computer_access"]),
    }
    if isinstance(entry.get("extension_binding"), dict):
        meta["extension_binding"] = copy.deepcopy(entry["extension_binding"])
    if "scope_context_supported" in entry:
        meta["scope_context_supported"] = bool(entry["scope_context_supported"])
    if "preview_precondition" in entry:
        meta["preview_precondition"] = bool(entry["preview_precondition"])
    if isinstance(entry.get("title"), str):
        meta["title"] = entry["title"]
    return meta


def _prepare_tool_arguments(
    method: str,
    canonical_sky_args: dict[str, Any],
    schema_snapshot: dict[str, Any],
    argument_translator: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any]:
    if argument_translator is not None:
        translated = argument_translator(method, copy.deepcopy(canonical_sky_args))
        if not isinstance(translated, dict):
            raise AgentError("agent_tool_arguments_invalid")
        return translated
    params = schema_snapshot.get("parameters")
    props = params.get("properties") if isinstance(params, dict) else None
    if isinstance(props, dict) and "method" in props and "method" not in canonical_sky_args:
        return {"method": method, **copy.deepcopy(canonical_sky_args)}
    return copy.deepcopy(canonical_sky_args)


def plan_child_call(
    row: dict[str, Any],
    envelope: PendingHostCallEnvelope | dict[str, Any],
    captured_catalog: Mapping[str, dict[str, Any]] | None = None,
    host_method_mapping: Mapping[str, str] | None = None,
    *,
    argument_translator: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    enqueue: bool = True,
) -> dict[str, Any]:
    """Validate a guest `PendingHostCallEnvelope` and construct a normalized host-stamped child call.

    - Recomputes `effect` and `computer_access` from the host's captured catalog
      and explicit `host_method_mapping`, never trusting `envelope.effect` or
      `envelope.target_tool`.
    - Uses real `tool_router.normalize_tool_calls` with the captured `schema_snapshot`.
    - Produces deterministic `call_id` and `operation_id` from callback identity +
      canonical argument sha256; duplicate polls with identical canonical arguments
      are idempotent, while changed arguments/identity fail closed.
    """
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise AgentError("agent_invalid_js_state")

    try:
        js_state = validate_task_js_state(row)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None
    if js_state is None:
        raise AgentError("agent_js_no_active_parent")

    if js_state.get("fatal_fence") is not None or js_state["phase"] == "fenced_unknown":
        raise AgentError("agent_js_fenced_unknown")
    if js_state["phase"] in TERMINAL_JS_PHASES:
        raise AgentError("agent_js_parent_already_terminal")
    if (
        not authority_matches_row(js_state["authority"], row)
        or row.get("status") in {"completed", "failed", "cancelled", "interrupted"}
        or (row.get("pending_control") or {}).get("kind") in {"cancel", "stop", "steer", "scope", "permissions", "edit_message"}
    ):
        raise AgentError("agent_js_authority_changed")

    env = _coerce_envelope(envelope)
    try:
        validate_identifier(env.task_id, "task_id")
        validate_identifier(env.session_id, "session_id")
        validate_identifier(env.execution_id, "execution_id")
        validate_identifier(env.callback_id, "callback_id")
    except JsProtocolError as exc:
        raise AgentError(exc.code) from None

    if env.task_id != row["id"]:
        raise AgentError("agent_js_task_mismatch")
    if env.session_id != js_state["session_id"]:
        raise AgentError("agent_js_session_mismatch")
    if type(env.generation) is not int or env.generation != js_state["generation"]:
        raise AgentError("agent_js_stale_generation")
    if env.execution_id != js_state["execution_id"]:
        raise AgentError("agent_js_execution_mismatch")
    if type(env.ordinal) is not int or env.ordinal < 0 or env.ordinal >= MAX_ADMITTED_CALLBACKS:
        raise AgentError("agent_js_invalid_ordinal")

    try:
        sky_spec, canonical_sky_args, canonical_args_sha = validate_sky_host_call(env.method, env.arguments)
    except JsProtocolError as exc:
        raise AgentError(exc.code) from None

    if env.arguments_sha256 != canonical_args_sha:
        raise AgentError("agent_js_arguments_hash_mismatch")

    effective_catalog_raw = (
        dict(captured_catalog)
        if captured_catalog is not None
        else js_state["capability_catalog"]
    )
    try:
        catalog = validate_capability_catalog(effective_catalog_raw)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None

    if js_state["capability_catalog"] and catalog != js_state["capability_catalog"]:
        raise AgentError("agent_tool_definition_changed")

    catalog_entry = catalog.get(env.method)
    if catalog_entry is None:
        raise AgentError("agent_js_method_unavailable")

    if host_method_mapping is not None:
        if not isinstance(host_method_mapping, Mapping):
            raise AgentError("agent_js_method_unavailable")
        mapped_tool = host_method_mapping.get(env.method)
        if mapped_tool != catalog_entry["tool_name"]:
            raise AgentError("agent_js_method_unavailable")
    else:
        mapped_tool = catalog_entry["tool_name"]

    if (
        mapped_tool == CONTAINER_TOOL_NAME
        or mapped_tool in INTERNAL_NAMES
        or mapped_tool.startswith("mcp__")
    ):
        raise AgentError("agent_js_method_unavailable")

    # Never trust guest envelope.effect or envelope.target_tool as host authority;
    # reject if guest envelope attempts to downgrade/forge effect classification.
    host_effect = catalog_entry["effect"]
    if host_effect != sky_spec["effect"]:
        raise AgentError("agent_js_effect_mismatch")
    if env.effect != host_effect:
        raise AgentError("agent_js_effect_mismatch")

    parent_call = js_state["parent_call"]
    parent_op_id = parent_call["operation_id"]
    parent_call_id = parent_call["call_id"]

    call_id, operation_id = derive_child_identities(
        task_id=row["id"],
        parent_operation_id=parent_op_id,
        session_id=js_state["session_id"],
        generation=js_state["generation"],
        execution_id=js_state["execution_id"],
        callback_id=env.callback_id,
        ordinal=env.ordinal,
        method=env.method,
        arguments_sha256=canonical_args_sha,
    )

    js_parent_stamp = validate_js_parent_stamp({
        "parent_operation_id": parent_op_id,
        "parent_call_id": parent_call_id,
        "session_id": js_state["session_id"],
        "generation": js_state["generation"],
        "execution_id": js_state["execution_id"],
        "callback_id": env.callback_id,
        "ordinal": env.ordinal,
        "method": env.method,
        "arguments_sha256": canonical_args_sha,
    })

    existing_admitted = js_state["admitted_callbacks"].get(env.callback_id)
    if existing_admitted is not None:
        if (
            existing_admitted["ordinal"] != env.ordinal
            or existing_admitted["method"] != env.method
            or existing_admitted["tool_name"] != mapped_tool
            or existing_admitted["effect"] != host_effect
            or existing_admitted["arguments_sha256"] != canonical_args_sha
            or existing_admitted["call_id"] != call_id
            or existing_admitted["operation_id"] != operation_id
            or existing_admitted["session_id"] != js_state["session_id"]
            or existing_admitted["generation"] != js_state["generation"]
            or existing_admitted["execution_id"] != js_state["execution_id"]
        ):
            raise AgentError("agent_js_callback_conflict")
    else:
        if any(item["ordinal"] == env.ordinal for item in js_state["admitted_callbacks"].values()):
            raise AgentError("agent_js_callback_conflict")
        if len(js_state["admitted_callbacks"]) >= MAX_ADMITTED_CALLBACKS:
            raise AgentError("agent_js_state_limit")
        # Enforce serial callback admission: no new callback while another child is active or staged.
        if any(
            item["status"] in {"admitted", "awaiting_approval", "dispatched", "unknown"}
            for item in js_state["admitted_callbacks"].values()
        ):
            raise AgentError("agent_js_child_already_active")
        if any(item["delivery_state"] == "staged" for item in js_state["delivery_outbox"]):
            raise AgentError("agent_js_outbox_pending")
        if is_js_child_call(row.get("in_flight")) or any(is_js_child_call(q) for q in row.get("queue", [])):
            raise AgentError("agent_js_child_already_active")

    tool_args = _prepare_tool_arguments(
        env.method,
        canonical_sky_args,
        catalog_entry["schema_snapshot"],
        argument_translator,
    )

    existing_ids = _existing_task_call_ids(
        row,
        exclude_call_id=call_id if existing_admitted is not None else None,
    )
    try:
        normalized_list = normalize_tool_calls(
            [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": mapped_tool,
                        "arguments": tool_args,
                    },
                }
            ],
            [{"type": "function", "function": copy.deepcopy(catalog_entry["schema_snapshot"])}],
            existing_call_ids=existing_ids,
            metadata=lambda _name: _catalog_entry_metadata(catalog_entry),
        )
    except ToolCallProtocolError as exc:
        raise AgentError(exc.code) from None

    normalized_call = normalized_list[0]
    normalized_call.pop("internal", None)
    if normalized_call.get("error_result") is None:
        router_rejected = False
        tool_arguments_sha256 = hashlib.sha256(encoded(normalized_call["arguments"])).hexdigest()
        normalized_call["effect"] = host_effect
        if catalog_entry["computer_access"]:
            normalized_call["computer_access"] = True
        else:
            normalized_call.pop("computer_access", None)
        if isinstance(catalog_entry.get("extension_binding"), dict):
            normalized_call["extension_binding"] = copy.deepcopy(catalog_entry["extension_binding"])
    else:
        router_rejected = True
        tool_arguments_sha256 = str(normalized_call["arguments_sha256"])

    normalized_call.update(
        operation_id=operation_id,
        context_epoch=row.get("context_epoch", 0),
        goal_revision=row["goal"]["revision"],
        scope_revision=row["scope_revision"],
        js_parent=copy.deepcopy(js_parent_stamp),
    )

    if existing_admitted is not None:
        if (
            (
                existing_admitted.get("tool_arguments_sha256") is not None
                and existing_admitted["tool_arguments_sha256"] != tool_arguments_sha256
            )
            or (
                existing_admitted.get("router_rejected") is not None
                and existing_admitted["router_rejected"] != router_rejected
            )
        ):
            raise AgentError("agent_js_callback_conflict")
        return normalized_call

    js_state["admitted_callbacks"][env.callback_id] = {
        "callback_id": env.callback_id,
        "ordinal": env.ordinal,
        "method": env.method,
        "tool_name": mapped_tool,
        "effect": host_effect,
        "arguments_sha256": canonical_args_sha,
        "tool_arguments_sha256": tool_arguments_sha256,
        "router_rejected": router_rejected,
        "call_id": call_id,
        "operation_id": operation_id,
        "parent_operation_id": parent_op_id,
        "parent_call_id": parent_call_id,
        "session_id": js_state["session_id"],
        "generation": js_state["generation"],
        "execution_id": js_state["execution_id"],
        "status": "admitted",
    }
    js_state["counters"]["host_calls"] += 1
    if not js_state["capability_catalog"]:
        js_state["capability_catalog"] = catalog
    try:
        js_state["phase"] = validate_phase_transition(
            js_state["phase"],
            "awaiting_child",
            fatal_fence=js_state["fatal_fence"],
        )
        validated_state = validate_js_state(js_state, task_row=row)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None

    row["js_execution"] = validated_state
    if enqueue:
        queue = list(row.get("queue", []))
        # Serialize child ahead of any tail sibling model calls.
        queue.insert(0, copy.deepcopy(normalized_call))
        row["queue"] = queue

    return normalized_call


def _coerce_token(token: DeliveryToken | dict[str, Any]) -> DeliveryToken:
    if isinstance(token, DeliveryToken):
        return token
    if not isinstance(token, dict):
        raise AgentError("agent_js_invalid_delivery_token")
    try:
        return DeliveryToken(
            task_id=token["task_id"],
            outbox_id=token["outbox_id"],
            parent_operation_id=token["parent_operation_id"],
            parent_call_id=token["parent_call_id"],
            session_id=token["session_id"],
            generation=token["generation"],
            execution_id=token["execution_id"],
            callback_id=token["callback_id"],
            ordinal=token["ordinal"],
            method=token["method"],
            tool_name=token["tool_name"],
            call_id=token["call_id"],
            operation_id=token["operation_id"],
            arguments_sha256=token["arguments_sha256"],
            receipt_id=token.get("receipt_id"),
            receipt_status=token["receipt_status"],
            outcome=token["outcome"],
            not_executed=token["not_executed"],
            result_ref=token.get("result_ref"),
            payload=copy.deepcopy(token.get("payload")),
            payload_sha256=token["payload_sha256"],
            delivery_state=token["delivery_state"],
            deliverable=bool(token.get("deliverable", True)),
            recorded_at=token["recorded_at"],
        )
    except (KeyError, TypeError):
        raise AgentError("agent_js_invalid_delivery_token") from None


def _extract_live_identity(live_identity: Any) -> dict[str, Any] | None:
    if live_identity is None:
        return None
    if isinstance(live_identity, Mapping):
        return {
            "task_id": live_identity.get("task_id"),
            "session_id": live_identity.get("session_id"),
            "generation": live_identity.get("generation"),
            "execution_id": live_identity.get("execution_id"),
            "alive": live_identity.get("alive", True),
        }
    return {
        "task_id": getattr(live_identity, "task_id", None),
        "session_id": getattr(live_identity, "session_id", None),
        "generation": getattr(live_identity, "generation", None),
        "execution_id": getattr(live_identity, "execution_id", None),
        "alive": getattr(live_identity, "alive", getattr(live_identity, "is_alive", True)),
    }


def _match_committed_outbox_entry(
    row: dict[str, Any],
    js_state: dict[str, Any],
    tok: DeliveryToken,
) -> dict[str, Any]:
    """Validate that `tok` matches a committed durable outbox item on all immutable and payload/outcome fields."""
    if tok.task_id != row["id"]:
        raise AgentError("agent_js_task_mismatch")
    if (
        tok.parent_operation_id != js_state["parent_call"]["operation_id"]
        or tok.parent_call_id != js_state["parent_call"]["call_id"]
        or tok.session_id != js_state["session_id"]
        or tok.generation != js_state["generation"]
        or tok.execution_id != js_state["execution_id"]
    ):
        raise AgentError("agent_js_parent_mismatch")

    outbox_entry = next(
        (
            item
            for item in js_state["delivery_outbox"]
            if item["outbox_id"] == tok.outbox_id and item["callback_id"] == tok.callback_id
        ),
        None,
    )
    if outbox_entry is None:
        if any(
            item["outbox_id"] == tok.outbox_id
            or item["callback_id"] == tok.callback_id
            or item["operation_id"] == tok.operation_id
            or item["call_id"] == tok.call_id
            for item in js_state["delivery_outbox"]
        ):
            raise AgentError("agent_js_outbox_conflict")
        raise AgentError("agent_js_uncommitted_delivery_token")

    admitted = js_state["admitted_callbacks"].get(outbox_entry["callback_id"])
    if not isinstance(admitted, dict):
        raise AgentError("agent_js_outbox_conflict")

    try:
        tok_payload_bytes = encoded(tok.payload)
    except (TypeError, ValueError):
        raise AgentError("agent_js_outbox_conflict") from None
    tok_payload_sha = hashlib.sha256(tok_payload_bytes).hexdigest()

    if (
        outbox_entry["outbox_id"] != tok.outbox_id
        or outbox_entry["parent_operation_id"] != tok.parent_operation_id
        or outbox_entry["parent_call_id"] != tok.parent_call_id
        or outbox_entry["session_id"] != tok.session_id
        or outbox_entry["generation"] != tok.generation
        or outbox_entry["execution_id"] != tok.execution_id
        or outbox_entry["callback_id"] != tok.callback_id
        or outbox_entry["ordinal"] != tok.ordinal
        or outbox_entry["method"] != tok.method
        or outbox_entry["tool_name"] != tok.tool_name
        or outbox_entry["call_id"] != tok.call_id
        or outbox_entry["operation_id"] != tok.operation_id
        or outbox_entry["arguments_sha256"] != tok.arguments_sha256
        or outbox_entry["receipt_id"] != tok.receipt_id
        or outbox_entry["receipt_status"] != tok.receipt_status
        or outbox_entry["outcome"] != tok.outcome
        or outbox_entry["not_executed"] != tok.not_executed
        or outbox_entry["result_ref"] != tok.result_ref
        or outbox_entry["payload"] != tok.payload
        or outbox_entry["payload_sha256"] != tok.payload_sha256
        or outbox_entry["payload_sha256"] != tok_payload_sha
        or outbox_entry["recorded_at"] != tok.recorded_at
        or admitted["operation_id"] != outbox_entry["operation_id"]
        or admitted["call_id"] != outbox_entry["call_id"]
        or admitted["ordinal"] != outbox_entry["ordinal"]
        or admitted["method"] != outbox_entry["method"]
        or admitted["tool_name"] != outbox_entry["tool_name"]
        or admitted["arguments_sha256"] != outbox_entry["arguments_sha256"]
    ):
        raise AgentError("agent_js_outbox_conflict")

    if outbox_entry["receipt_id"] is not None:
        rec = next(
            (
                r
                for r in row.get("receipts", [])
                if isinstance(r, dict) and r.get("operation_id") == outbox_entry["operation_id"]
            ),
            None,
        )
        if rec is None:
            raise AgentError("agent_js_outbox_conflict")
        rec_call_id = rec.get("call_id")
        rec_args = rec.get("arguments")
        if not isinstance(rec_args, dict):
            raise AgentError("agent_js_outbox_conflict")
        try:
            rec_args_sha = hashlib.sha256(encoded(rec_args)).hexdigest()
        except (TypeError, ValueError):
            raise AgentError("agent_js_outbox_conflict") from None
        cap_entry = js_state["capability_catalog"].get(outbox_entry["method"])
        rec_parent = rec.get("js_parent")
        if (
            rec.get("id") != outbox_entry["receipt_id"]
            or (rec_call_id is not None and rec_call_id != outbox_entry["call_id"])
            or rec.get("tool") != outbox_entry["tool_name"]
            or rec.get("status") != outbox_entry["receipt_status"]
            or rec.get("arguments_sha256") != rec_args_sha
            or (
                admitted.get("tool_arguments_sha256") is not None
                and not admitted.get("router_rejected")
                and rec_args_sha != admitted["tool_arguments_sha256"]
            )
            or rec.get("goal_revision") != js_state["authority"]["goal"]
            or rec.get("scope_revision") != js_state["authority"]["scope"]
            or rec.get("context_epoch") != js_state["authority"]["context_epoch"]
            or not isinstance(cap_entry, dict)
            or cap_entry["tool_name"] != outbox_entry["tool_name"]
            or rec.get("effect") != cap_entry["effect"]
            or rec.get("extension_binding") != cap_entry.get("extension_binding")
            or (
                isinstance(rec_parent, dict)
                and (
                    rec_parent.get("parent_operation_id") != outbox_entry["parent_operation_id"]
                    or rec_parent.get("parent_call_id") != outbox_entry["parent_call_id"]
                    or rec_parent.get("session_id") != outbox_entry["session_id"]
                    or rec_parent.get("generation") != outbox_entry["generation"]
                    or rec_parent.get("execution_id") != outbox_entry["execution_id"]
                    or rec_parent.get("callback_id") != outbox_entry["callback_id"]
                    or rec_parent.get("ordinal") != outbox_entry["ordinal"]
                    or rec_parent.get("method") != outbox_entry["method"]
                    or rec_parent.get("arguments_sha256") != outbox_entry["arguments_sha256"]
                )
            )
        ):
            raise AgentError("agent_js_outbox_conflict")

    if outbox_entry["result_ref"] is not None:
        matched_refs = [
            r
            for r in row.get("result_refs", [])
            if r == outbox_entry["result_ref"]
            or (isinstance(r, dict) and r.get("id") == outbox_entry["result_ref"])
        ]
        if not matched_refs:
            raise AgentError("agent_js_outbox_conflict")
        for ref_item in matched_refs:
            try:
                _, validated_ref = validate_result_reference(
                    ref_item,
                    task_id=row["id"],
                    tool_name=outbox_entry["tool_name"],
                    operation_id=outbox_entry["operation_id"],
                    call_id=outbox_entry["call_id"],
                    authority=js_state.get("authority"),
                )
            except JsStateValidationError as exc:
                raise AgentError(exc.code) from None
            if outbox_entry.get("result_ref_sha256") is not None:
                if (
                    validated_ref is None
                    or hashlib.sha256(encoded(validated_ref)).hexdigest() != outbox_entry["result_ref_sha256"]
                ):
                    raise AgentError("agent_js_outbox_conflict")

    return outbox_entry


def select_delivery(
    row: dict[str, Any],
    token: DeliveryToken | dict[str, Any],
    live_identity: Any,
) -> DeliveryPlan:
    """Select a bounded resolve/reject plan for an already-committed `DeliveryToken`."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise AgentError("agent_invalid_js_state")
    try:
        js_state = validate_task_js_state(row)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None
    if js_state is None:
        raise AgentError("agent_js_no_active_parent")

    tok = _coerce_token(token)
    if tok.task_id != row["id"]:
        raise AgentError("agent_js_task_mismatch")
    if (
        tok.parent_operation_id != js_state["parent_call"]["operation_id"]
        or tok.parent_call_id != js_state["parent_call"]["call_id"]
        or tok.session_id != js_state["session_id"]
        or tok.generation != js_state["generation"]
        or tok.execution_id != js_state["execution_id"]
    ):
        raise AgentError("agent_js_parent_mismatch")

    if js_state.get("fatal_fence") is not None or js_state["phase"] == "fenced_unknown" or tok.receipt_status == "unknown":
        admitted = js_state["admitted_callbacks"].get(tok.callback_id)
        if (
            not isinstance(admitted, dict)
            or admitted["operation_id"] != tok.operation_id
            or admitted["call_id"] != tok.call_id
            or admitted["ordinal"] != tok.ordinal
            or admitted["method"] != tok.method
            or admitted["tool_name"] != tok.tool_name
            or admitted["arguments_sha256"] != tok.arguments_sha256
        ):
            raise AgentError("agent_js_outbox_conflict")
        return DeliveryPlan(
            action="fenced_unknown",
            task_id=row["id"],
            outbox_id=tok.outbox_id,
            session_id=js_state["session_id"],
            generation=js_state["generation"],
            execution_id=js_state["execution_id"],
            callback_id=admitted["callback_id"],
            not_executed=False,
            outcome="unknown",
            reason="agent_write_outcome_unknown",
        )

    outbox_entry = _match_committed_outbox_entry(row, js_state, tok)

    if js_state["phase"] in TERMINAL_JS_PHASES or outbox_entry["delivery_state"] != "staged":
        return DeliveryPlan(
            action="continuation_lost",
            task_id=row["id"],
            outbox_id=outbox_entry["outbox_id"],
            session_id=js_state["session_id"],
            generation=js_state["generation"],
            execution_id=js_state["execution_id"],
            callback_id=outbox_entry["callback_id"],
            not_executed=outbox_entry["not_executed"],
            outcome=outbox_entry["outcome"],
            reason="ryn_js_outbox_not_staged",
        )

    if (
        not authority_matches_row(js_state["authority"], row)
        or row.get("status") in {"completed", "failed", "cancelled", "interrupted"}
        or (row.get("pending_control") or {}).get("kind") in {"cancel", "stop", "steer", "scope", "permissions", "edit_message"}
    ):
        return DeliveryPlan(
            action="continuation_lost",
            task_id=row["id"],
            outbox_id=outbox_entry["outbox_id"],
            session_id=js_state["session_id"],
            generation=js_state["generation"],
            execution_id=js_state["execution_id"],
            callback_id=outbox_entry["callback_id"],
            not_executed=outbox_entry["not_executed"],
            outcome=outbox_entry["outcome"],
            reason="ryn_js_authority_changed",
        )

    live = _extract_live_identity(live_identity)
    if (
        live is None
        or live.get("alive") is False
        or (live.get("task_id") is not None and live.get("task_id") != row["id"])
        or live.get("session_id") != js_state["session_id"]
        or live.get("generation") != js_state["generation"]
        or live.get("execution_id") != js_state["execution_id"]
    ):
        return DeliveryPlan(
            action="continuation_lost",
            task_id=row["id"],
            outbox_id=outbox_entry["outbox_id"],
            session_id=js_state["session_id"],
            generation=js_state["generation"],
            execution_id=js_state["execution_id"],
            callback_id=outbox_entry["callback_id"],
            not_executed=outbox_entry["not_executed"],
            outcome=outbox_entry["outcome"],
            reason="ryn_js_live_identity_mismatch",
        )

    payload = copy.deepcopy(outbox_entry["payload"])
    if len(encoded(payload)) > MAX_RESULT_BYTES:
        return DeliveryPlan(
            action="continuation_lost",
            task_id=row["id"],
            outbox_id=outbox_entry["outbox_id"],
            session_id=js_state["session_id"],
            generation=js_state["generation"],
            execution_id=js_state["execution_id"],
            callback_id=outbox_entry["callback_id"],
            not_executed=outbox_entry["not_executed"],
            outcome=outbox_entry["outcome"],
            reason="ryn_js_result_too_large",
        )

    if outbox_entry["outcome"] == "succeeded":
        return DeliveryPlan(
            action="resolve",
            task_id=row["id"],
            outbox_id=outbox_entry["outbox_id"],
            session_id=js_state["session_id"],
            generation=js_state["generation"],
            execution_id=js_state["execution_id"],
            callback_id=outbox_entry["callback_id"],
            result=payload,
            not_executed=False,
            outcome="succeeded",
        )

    err_code = "ryn_sky_host_rejected"
    err_message = "Host call rejected"
    if isinstance(payload, dict):
        if isinstance(payload.get("error"), str) and SAFE_ID_RE.fullmatch(payload["error"]):
            err_code = payload["error"]
        elif isinstance(payload.get("code"), str) and SAFE_ID_RE.fullmatch(payload["code"]):
            err_code = payload["code"]
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            err_message = payload["message"][:2000]
        elif isinstance(payload.get("correction"), str) and payload["correction"].strip():
            err_message = payload["correction"][:2000]
        else:
            err_message = err_code

    return DeliveryPlan(
        action="reject",
        task_id=row["id"],
        outbox_id=outbox_entry["outbox_id"],
        session_id=js_state["session_id"],
        generation=js_state["generation"],
        execution_id=js_state["execution_id"],
        callback_id=outbox_entry["callback_id"],
        code=err_code,
        message=err_message,
        not_executed=outbox_entry["not_executed"],
        outcome=outbox_entry["outcome"],
    )


def dispatch_delivery_plan(coordinator_port: Any, plan: DeliveryPlan) -> str:
    """Thin injected coordinator-port call for an already-selected `DeliveryPlan`.

    Never starts a worker, never evaluates JS source, and never retries on
    transport error. Any send uncertainty returns `'continuation_lost'`.
    """
    if not isinstance(plan, DeliveryPlan):
        raise AgentError("agent_js_invalid_delivery_plan")
    if plan.action == "fenced_unknown":
        return "fenced_unknown"
    if plan.action not in {"resolve", "reject"}:
        return "continuation_lost"
    if coordinator_port is None:
        raise AgentError("agent_js_coordinator_required")

    try:
        if plan.action == "resolve":
            coordinator_port.resolve_host_call(
                plan.task_id,
                execution_id=plan.execution_id,
                generation=plan.generation,
                callback_id=plan.callback_id,
                result=copy.deepcopy(plan.result),
                session_id=plan.session_id,
            )
        else:
            coordinator_port.reject_host_call(
                plan.task_id,
                execution_id=plan.execution_id,
                generation=plan.generation,
                callback_id=plan.callback_id,
                code=plan.code or "ryn_sky_host_rejected",
                message=plan.message,
                not_executed=plan.not_executed,
                outcome=plan.outcome,
                session_id=plan.session_id,
            )
        return "sent"
    except (JsRuntimeError, JsProtocolError, OSError, RuntimeError, ValueError):
        return "continuation_lost"
    except Exception:
        return "continuation_lost"


def deliver_committed_token(
    row: dict[str, Any],
    token: DeliveryToken | dict[str, Any],
    coordinator_port: Any,
    *,
    live_identity: Any = None,
) -> str:
    """Validate that `token` is committed in `row` and dispatch once via `coordinator_port`."""
    if coordinator_port is None:
        raise AgentError("agent_js_coordinator_required")
    plan = select_delivery(row, token, live_identity)
    return dispatch_delivery_plan(coordinator_port, plan)


def complete_delivery(
    row: dict[str, Any],
    token: DeliveryToken | dict[str, Any],
    known_transport_disposition: str,
    *,
    event_emitter: Callable[..., None] = default_event_emitter,
    allow_idempotent: bool = False,
) -> dict[str, Any]:
    """Commit the monotonic post-send outbox/container state inside caller's transaction.

    Preserves all confirmed business receipts in `row['receipts']` even when
    `known_transport_disposition` is `'continuation_lost'` or `'lost'`.
    Never invents a durable guest-consumption ACK (`'consumed'` is rejected if
    the item was `'lost'`, and `'sent'` is the normal IPC completion state).
    """
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise AgentError("agent_invalid_js_state")
    try:
        js_state = validate_task_js_state(row)
    except JsStateValidationError as exc:
        raise AgentError(exc.code) from None
    if js_state is None:
        raise AgentError("agent_js_no_active_parent")

    tok = _coerce_token(token)
    outbox_entry = _match_committed_outbox_entry(row, js_state, tok)
    current_delivery_state = outbox_entry["delivery_state"]

    if known_transport_disposition == "sent":
        if current_delivery_state != "staged":
            if allow_idempotent and current_delivery_state == "sent":
                return copy.deepcopy(js_state)
            raise AgentError("agent_invalid_js_delivery_transition")
        try:
            outbox_entry["delivery_state"] = validate_delivery_transition(current_delivery_state, "sent")
            js_state["phase"] = validate_phase_transition(
                js_state["phase"],
                "running",
                fatal_fence=js_state["fatal_fence"],
            )
            validated_state = validate_js_state(js_state, task_row=row)
        except JsStateValidationError as exc:
            raise AgentError(exc.code) from None
        row["js_execution"] = validated_state
        event_emitter(
            row,
            "js_child_delivered",
            outbox_id=outbox_entry["outbox_id"],
            callback_id=outbox_entry["callback_id"],
            operation_id=outbox_entry["operation_id"],
            call_id=outbox_entry["call_id"],
            tool=outbox_entry["tool_name"],
            delivery_state="sent",
        )
        return copy.deepcopy(validated_state)

    if known_transport_disposition in {"lost", "continuation_lost", "uncertain", "error"}:
        if allow_idempotent and current_delivery_state == "lost" and js_state["phase"] == "continuation_lost":
            return copy.deepcopy(js_state)
        try:
            outbox_entry["delivery_state"] = validate_delivery_transition(current_delivery_state, "lost")
            lost_state = plan_continuation_lost_js_state(
                js_state,
                reason="ryn_js_continuation_lost",
                message="Host call continuation delivery was lost or uncertain.",
            )
            validated_state = validate_js_state(lost_state, task_row=row)
        except JsStateValidationError as exc:
            raise AgentError(exc.code) from None
        row["js_execution"] = validated_state
        event_emitter(
            row,
            "js_continuation_lost",
            outbox_id=outbox_entry["outbox_id"],
            callback_id=outbox_entry["callback_id"],
            operation_id=outbox_entry["operation_id"],
            call_id=outbox_entry["call_id"],
            tool=outbox_entry["tool_name"],
            delivery_state="lost",
        )
        return copy.deepcopy(validated_state)

    raise AgentError("agent_invalid_js_delivery_transition")
