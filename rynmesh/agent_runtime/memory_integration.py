"""Backend integration helpers connecting AgentRuntime, AgentLoop, and FastAPI routes to memory/memory_runtime."""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import knowledge, memory, memory_natural, memory_runtime, projects
from .control import settle
from .store import AgentError


class MemoryDestinationUnavailable(AgentError):
    """The current catalog cannot establish availability; identity is unknown."""


MEMORY_TOOL_NAMES: frozenset[str] = frozenset(
    spec["function"]["name"]
    for spec in memory_runtime.MEMORY_TOOL_SPECS
    if isinstance(spec, dict)
    and isinstance(spec.get("function"), dict)
    and isinstance(spec["function"].get("name"), str)
)

_FORGED_RECEIPT_JSON_RE = re.compile(
    r"\{\s*\"memory_last_receipt\"\s*:\s*\{[^{}]*\}\s*\}\s*",
    re.DOTALL,
)


class ContextMessages(list):
    """Host-authored provider message list carrying trusted memory disclosure provenance."""

    disclosed_memory: bool = False


def messages_disclose_memory(
    messages: list[dict[str, Any]] | None,
    data: dict[str, Any] | None = None,
    *,
    row: dict[str, Any] | None = None,
) -> bool:
    """Return True only when the host-authored context projection actually included memory or restricted preferences."""
    if getattr(messages, "disclosed_memory", False) is True:
        return True
    if isinstance(row, dict) and bool(row.get("_context_disclosed_memory")):
        return True
    return False


def strip_forged_receipt_and_guard_claims(row: dict[str, Any], content: str) -> str:
    """Strip any model-forged memory_last_receipt JSON payload and guard unverified memory-save claims."""
    raw = str(content or "")
    if _FORGED_RECEIPT_JSON_RE.search(raw):
        cleaned = _FORGED_RECEIPT_JSON_RE.sub("", raw).strip()
        if not cleaned and raw:
            cleaned = raw
    else:
        cleaned = raw
    return memory_runtime.guard_false_memory_save_claim(row, cleaned)


def verify_task_destination_for_memory(runtime: Any, row: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Verify that a task has an exact, currently valid bound model destination before disclosing memory."""
    if getattr(runtime.model, "supports_destination_binding", False) is not True:
        return False, None, "destination_binding_unsupported"
    model_id = str(row.get("model_id") or "").strip()
    if not model_id:
        return False, None, "missing_model_id"
    task_dest = memory.normalize_destination(row.get("model_destination"))
    if not task_dest or not task_dest.get("destination_revision"):
        return False, None, "destination_unverifiable"
    try:
        current_bound = runtime._current_destination(model_id, require_bound=True)
    except AgentError as exc:
        return False, None, str(exc)
    norm_current = memory.normalize_destination(current_bound)
    if not norm_current or norm_current != task_dest:
        return False, None, "destination_drifted"
    return True, norm_current, None


def verify_exact_catalog_destination(
    runtime: Any,
    raw_dest: Any,
    *,
    require_available: bool = True,
) -> dict[str, Any]:
    """Verify that raw_dest is an exact destination tuple matching a bound model in the current catalog."""
    norm = memory.normalize_destination(raw_dest)
    if norm is None:
        raise AgentError("agent_memory_invalid_destination")
    model_id = norm.get("catalog_model_id", "")
    peer_id = norm.get("provider_peer_id", "")
    rev = norm.get("destination_revision", "")
    if not model_id or not peer_id or not rev:
        raise AgentError("agent_memory_invalid_destination")
    model = runtime._model_metadata(model_id)
    if not isinstance(model, dict):
        raise MemoryDestinationUnavailable("agent_memory_invalid_destination")
    if model.get("agent_supported") is not True:
        raise AgentError("agent_memory_invalid_destination")
    try:
        bound = runtime._bound_destination(model)
    except AgentError as exc:
        raise AgentError("agent_memory_invalid_destination") from exc
    norm_bound = memory.normalize_destination(bound)
    if norm_bound != norm:
        raise AgentError("agent_memory_invalid_destination")
    if require_available and not model.get("available"):
        raise MemoryDestinationUnavailable("agent_memory_invalid_destination")
    return norm_bound


def list_memory_destinations(runtime: Any) -> dict[str, Any]:
    """Return all verifiable model destinations from the catalog and the current default destination."""
    raw_models = runtime.catalog.list_models() if hasattr(runtime.catalog, "list_models") else []
    destinations: list[dict[str, Any]] = []
    by_model_id: dict[str, dict[str, Any]] = {}
    for item in raw_models:
        if not isinstance(item, dict) or item.get("agent_supported") is not True:
            continue
        try:
            bound = runtime._bound_destination(item)
        except AgentError:
            continue
        norm = memory.normalize_destination(bound)
        if not norm or not norm.get("destination_revision"):
            continue
        label = str(item.get("name") or item.get("label") or item.get("id") or "").strip()
        avail = bool(item.get("available", False))
        destinations.append(
            {
                "label": label,
                "destination": norm,
                "available": avail,
            }
        )
        if avail:
            by_model_id[str(item.get("id") or "")] = norm

    default_dest = None
    try:
        settings_model_id = str((runtime.store.read().get("settings") or {}).get("model_id") or "")
        if settings_model_id and settings_model_id in by_model_id:
            default_dest = copy.deepcopy(by_model_id[settings_model_id])
    except Exception:
        default_dest = None

    return {
        "destinations": destinations,
        "default_destination": default_dest,
    }


def resolve_and_verify_scope(
    data: dict[str, Any],
    raw_scope: Any,
    *,
    required: bool = True,
    allow_all: bool = False,
) -> dict[str, Any] | None:
    """Server-author and validate a memory scope object against live project state."""
    if raw_scope is None:
        if required:
            raise AgentError("agent_memory_invalid_scope")
        return None
    if not isinstance(raw_scope, dict):
        raise AgentError("agent_memory_invalid_scope")
    allowed_keys = {"kind", "owner_id", "project_id", "project_generation"}
    if any(k not in allowed_keys for k in raw_scope):
        raise AgentError("agent_memory_invalid_scope")
    if "owner_id" in raw_scope and raw_scope["owner_id"] not in {None, "", "local_owner"}:
        raise AgentError("agent_memory_invalid_scope")

    kind = str(raw_scope.get("kind") or "").strip()
    if allow_all and kind == "all":
        if raw_scope.get("project_id") not in {None, ""} or raw_scope.get("project_generation") is not None:
            raise AgentError("agent_memory_invalid_scope")
        return None
    if kind == "personal":
        if raw_scope.get("project_id") not in {None, ""} or raw_scope.get("project_generation") is not None:
            raise AgentError("agent_memory_invalid_scope")
        return {
            "kind": "personal",
            "owner_id": "local_owner",
            "project_id": None,
            "project_generation": None,
        }
    if kind == "project":
        pid = raw_scope.get("project_id")
        if not isinstance(pid, str) or not pid.strip():
            raise AgentError("agent_memory_project_id_required")
        pid = pid.strip()
        proj = (data.get("projects") or {}).get(pid)
        if not isinstance(proj, dict) or not proj.get("enabled", False):
            raise AgentError("agent_memory_invalid_scope")
        actual_gen = int(proj.get("generation", 1))
        req_gen = raw_scope.get("project_generation")
        if req_gen is not None:
            if type(req_gen) is not int or req_gen != actual_gen:
                raise AgentError("agent_memory_invalid_scope")
        return {
            "kind": "project",
            "owner_id": "local_owner",
            "project_id": pid,
            "project_generation": actual_gen,
        }
    raise AgentError("agent_memory_invalid_scope")


def validate_owner_exposure_policy(
    runtime: Any,
    raw_policy: Any,
    *,
    required: bool = True,
    prev_revision: int = 0,
) -> dict[str, Any] | None:
    """Validate an entry exposure_policy object from the owner API, verifying exact catalog destinations."""
    if raw_policy is None:
        if required:
            raise AgentError("agent_memory_invalid_exposure_policy")
        return None
    if not isinstance(raw_policy, dict):
        raise AgentError("agent_memory_invalid_exposure_policy")
    allowed_keys = {"mode", "allowed_destinations", "revision"}
    if any(k not in allowed_keys for k in raw_policy):
        raise AgentError("agent_memory_invalid_exposure_policy")
    raw_mode = raw_policy.get("mode")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise AgentError("agent_memory_invalid_exposure_policy")
    mode = raw_mode.strip()
    if mode not in {"local_only", "destinations", "selected_models", "all_selected"}:
        raise AgentError("agent_memory_invalid_exposure_policy")
    if mode in {"destinations", "selected_models"}:
        raw_dests = raw_policy.get("allowed_destinations")
        if not isinstance(raw_dests, list) or not raw_dests:
            raise AgentError("agent_memory_invalid_exposure_policy")
        verified_dests = [verify_exact_catalog_destination(runtime, item, require_available=False) for item in raw_dests]
        return memory.normalize_exposure_policy(
            {"mode": "destinations", "allowed_destinations": verified_dests},
            prev_revision=prev_revision,
        )
    if mode == "local_only":
        allowed_dests = raw_policy.get("allowed_destinations")
        if allowed_dests is not None and allowed_dests != []:
            raise AgentError("agent_memory_invalid_exposure_policy")
        return memory.normalize_exposure_policy(
            {"mode": "local_only", "allowed_destinations": []},
            prev_revision=prev_revision,
        )
    # mode == "all_selected": only when explicitly requested by the owner
    allowed_dests = raw_policy.get("allowed_destinations")
    if allowed_dests is not None and allowed_dests != []:
        raise AgentError("agent_memory_invalid_exposure_policy")
    return memory.normalize_exposure_policy(
        {"mode": "all_selected", "allowed_destinations": []},
        prev_revision=prev_revision,
    )


def validate_owner_default_exposure(runtime: Any, raw_policy: Any) -> dict[str, Any]:
    """Validate settings.default_exposure using the distinct settings schema (destination/selected_models/restricted)."""
    if not isinstance(raw_policy, dict):
        raise AgentError("agent_memory_invalid_exposure_policy")
    allowed_keys = {"mode", "allowed_destinations"}
    if any(k not in allowed_keys for k in raw_policy):
        raise AgentError("agent_memory_invalid_exposure_policy")
    raw_mode = raw_policy.get("mode")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise AgentError("agent_memory_invalid_exposure_policy")
    mode = raw_mode.strip()
    if mode not in memory.VALID_DEFAULT_EXPOSURE_MODES:
        raise AgentError("agent_memory_invalid_exposure_policy")
    raw_dests = raw_policy.get("allowed_destinations")
    if raw_dests is None:
        raw_dests = []
    if not isinstance(raw_dests, list) or len(raw_dests) > 32:
        raise AgentError("agent_memory_invalid_exposure_policy")
    if mode == "restricted" and raw_dests != []:
        raise AgentError("agent_memory_invalid_exposure_policy")
    if mode == "destination" and not raw_dests:
        raise AgentError("agent_memory_invalid_exposure_policy")
    verified_dests = [verify_exact_catalog_destination(runtime, item, require_available=False) for item in raw_dests]
    return {
        "mode": mode,
        "allowed_destinations": verified_dests,
    }


def require_common_write_fields(payload: Any) -> tuple[int, str]:
    """Validate required expected_generation and client_request_id on every owner memory mutation."""
    if not isinstance(payload, dict):
        raise AgentError("agent_invalid_request")
    expected_gen = payload.get("expected_generation")
    if type(expected_gen) is not int or expected_gen < 0:
        raise AgentError("agent_memory_invalid_generation")
    req_id = payload.get("client_request_id")
    if not isinstance(req_id, str) or not req_id.strip() or len(req_id) > 128:
        raise AgentError("agent_memory_invalid_request_id")
    return expected_gen, req_id.strip()


def _revoke_task_row_in_tx(data: dict[str, Any], row: dict[str, Any], *, force: bool = False) -> bool:
    prior_wm = row.get("memory_projection_watermark")
    had_disclosed = bool(row.get("memory_disclosed_in_epoch"))
    if not force and prior_wm is None and not had_disclosed:
        return False
    current_wm = memory_runtime.memory_projection_watermark(data, row).get("watermark", "")
    if force or prior_wm != current_wm or had_disclosed:
        row["context_epoch"] = int(row.get("context_epoch", 0)) + 1
        row.pop("context_checkpoint", None)
        row["memory_projection_watermark"] = current_wm
        row["memory_disclosed_in_epoch"] = False
        row["memory_conversation_epoch"] = int(row.get("memory_conversation_epoch", 0)) + 1
        return True
    return False


def propagate_memory_revocation_in_tx(data: dict[str, Any], *, exclude_task_id: str | None = None) -> None:
    """Rotate provider conversation epoch and clear derived checkpoints on tasks whose memory watermark changed."""
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return
    for tid, row in tasks.items():
        if not isinstance(row, dict):
            continue
        if exclude_task_id and tid == exclude_task_id:
            continue
        _revoke_task_row_in_tx(data, row)


def sync_task_memory_watermark_in_tx(data: dict[str, Any], row: dict[str, Any]) -> bool:
    """Ensure a task's context_epoch and checkpoint are rotated if memory watermark drifted since last turn."""
    current_wm_info = memory_runtime.memory_projection_watermark(data, row)
    if not current_wm_info.get("touched"):
        return False
    current_wm = current_wm_info.get("watermark", "")
    prior_wm = row.get("memory_projection_watermark")
    if prior_wm is not None and prior_wm != current_wm:
        row["context_epoch"] = int(row.get("context_epoch", 0)) + 1
        row.pop("context_checkpoint", None)
        row["memory_projection_watermark"] = current_wm
        row["memory_disclosed_in_epoch"] = False
        row["memory_conversation_epoch"] = int(row.get("memory_conversation_epoch", 0)) + 1
        return True
    return False


def apply_owner_turn_memory_in_tx(runtime: Any, data: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Execute explicit owner remember/update/forget intent in the current transaction and record turn receipt."""
    if isinstance(data, dict) and isinstance(data.get("memory"), dict):
        memory_natural.stamp_task_owner_messages_in_tx(data, row)
    sync_task_memory_watermark_in_tx(data, row)
    latest = memory_runtime.latest_authentic_owner_message(row)
    if not latest:
        return {"applied": False, "saved": False, "intent": "none"}
    receipt_key = f"{latest['message_id']}:r{latest['conversation_revision']}"
    already_applied = receipt_key in (row.get("memory_explicit_receipts") or {})
    receipt = memory_runtime.execute_explicit_owner_intent_in_tx(data, row, owner_msg=latest)
    if not receipt.get("applied"):
        row["memory_last_receipt"] = {
            "applied": False,
            "saved": False,
            "intent": receipt.get("intent", "none"),
            "message_id": latest["message_id"],
            "conversation_revision": latest["conversation_revision"],
            "reason": receipt.get("reason") or receipt.get("disqualified_reason") or "not_explicit_memory_command",
            "entries": [],
            "deleted_ids": [],
            "errors": list(receipt.get("errors") or []),
        }
    else:
        row["memory_last_receipt"] = copy.deepcopy(receipt)
        if not already_applied:
            tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
            row_in_tasks = row.get("id") in tasks and tasks.get(row.get("id")) is row
            row_epoch_before = int(row.get("context_epoch", 0))
            propagate_memory_revocation_in_tx(data)
            row_rotated = int(row.get("context_epoch", 0)) > row_epoch_before
            if not row_rotated:
                has_prior_derived = bool(
                    row.get("context_checkpoint") is not None
                    or any(
                        isinstance(m, dict)
                        and m.get("role") != "user"
                        and m.get("context_epoch", 0) == row_epoch_before
                        for m in (row.get("messages") or [])
                    )
                )
                if not row_in_tasks:
                    row_rotated = _revoke_task_row_in_tx(
                        data, row, force=bool(receipt.get("deleted_ids") and has_prior_derived)
                    )
                elif receipt.get("deleted_ids") and has_prior_derived:
                    _revoke_task_row_in_tx(data, row, force=True)
    return receipt


_SAFE_HOST_MEMORY_REASON_CODES = frozenset({
    "no_matching_memory",
    "not_applied",
    "operation_failed",
    "agent_memory_limit_exceeded",
    "agent_memory_state_size_limit_exceeded",
    "agent_memory_invalid_text",
    "agent_memory_invalid_category",
    "agent_memory_invalid_fact_key",
    "agent_memory_invalid_scope",
    "agent_memory_invalid_exposure_policy",
    "agent_memory_revision_conflict",
    "agent_memory_generation_conflict",
    "agent_memory_not_found",
    "agent_memory_missing_owner_source",
})

_HOST_MEMORY_OPERATION_INSTRUCTION = (
    "Host memory operations in this result already executed independently of business read_only controls; "
    "do not call business tools or repeat a plan to perform them; naturally and briefly confirm the actual "
    "recorded result, including failure/no-op accurately. Do not expose JSON field names, receipts or "
    "implementation details in the normal user reply unless the owner explicitly requests diagnostics. "
    "No genuine result means no positive save/delete claim."
)


def current_automatic_memory_capture_note(runtime: Any) -> str | None:
    """Describe current host capture policy, independently of memory disclosure."""
    def enabled(data: dict[str, Any]) -> bool:
        settings = (data.get("memory") or {}).get("settings") or {}
        return bool(data.get("settings", {}).get("enabled", True)
                    and settings.get("capture_enabled") is True
                    and settings.get("activation_policy") == "automatic")

    active = (runtime.store.inspect(enabled) if hasattr(runtime.store, "inspect")
              else enabled(runtime.store.read()))
    if not active:
        return None
    return (
        '{"host_memory_capture_policy":{"activation_policy":"automatic","capture_enabled":true,'
        '"ordinary_owner_statements":"background_after_task_completion","write_completion":"not_established"}}\n'
        "The host considers durable facts from ordinary owner conversation in its background memory pipeline. "
        "No remember/update wording is required. For incidental personal statements and natural corrections, "
        "answer the task using the latest owner information; do not call ryn_memory_propose or ask the owner "
        "to repeat a special memory command. That tool handles a separate explicit foreground operation; "
        "a rejected/not_explicit_command result does not disable or describe automatic background capture. "
        "Capture depends on completion, source validity, model availability and budgets. This policy note "
        "does not prove any write completed: never claim saved/updated without a genuine successful host result. "
        "Acknowledge corrections naturally; do not narrate memory processing unless asked."
    )


def current_host_memory_operation_note(runtime: Any, row: dict[str, Any]) -> str | None:
    """Build a bounded developer note for a genuine current-turn host memory receipt, or None."""
    if not isinstance(row, dict):
        return None
    latest = memory_runtime.latest_authentic_owner_message(row)
    if not isinstance(latest, dict):
        return None
    latest_msg_id = latest.get("message_id")
    latest_conv_rev = latest.get("conversation_revision")
    if not isinstance(latest_msg_id, str) or not latest_msg_id or type(latest_conv_rev) is not int:
        return None

    if "memory_last_receipt" in row:
        receipt = row.get("memory_last_receipt")
    elif runtime is not None and hasattr(runtime, "store") and isinstance(row.get("id"), str):
        def _read_stored_receipt(data: dict[str, Any]) -> Any:
            tasks = data.get("tasks") if isinstance(data, dict) else None
            stored = tasks.get(row["id"]) if isinstance(tasks, dict) else None
            return copy.deepcopy(stored.get("memory_last_receipt")) if isinstance(stored, dict) else None

        receipt = (
            runtime.store.inspect(_read_stored_receipt)
            if hasattr(runtime.store, "inspect")
            else _read_stored_receipt(runtime.store.read())
        )
    else:
        receipt = None

    if not isinstance(receipt, dict):
        return None
    if receipt.get("message_id") != latest_msg_id:
        return None
    if type(receipt.get("conversation_revision")) is not int or receipt.get("conversation_revision") != latest_conv_rev:
        return None

    intent = receipt.get("intent")
    if intent not in {"remember", "update", "forget"}:
        return None

    raw_errors = receipt.get("errors")
    has_errors = isinstance(raw_errors, list) and len(raw_errors) > 0
    raw_applied = bool(receipt.get("applied") is True and not has_errors)

    raw_entries = receipt.get("entries")
    valid_entries = (
        [
            e
            for e in raw_entries
            if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"].strip()
        ]
        if isinstance(raw_entries, list)
        else []
    )
    raw_deleted = receipt.get("deleted_ids")
    valid_deleted = (
        [d for d in raw_deleted if isinstance(d, str) and d.strip()]
        if isinstance(raw_deleted, list)
        else []
    )

    saved_count = len(valid_entries) if raw_applied and intent in {"remember", "update"} else 0
    deleted_count = len(valid_deleted) if raw_applied and intent == "forget" else 0
    applied = bool(
        raw_applied
        and (saved_count > 0 if intent in {"remember", "update"} else deleted_count > 0)
    )
    if not applied:
        saved_count = 0
        deleted_count = 0
        raw_reason = receipt.get("reason")
        if not isinstance(raw_reason, str) and has_errors and isinstance(raw_errors[0], str):
            raw_reason = raw_errors[0]
        if isinstance(raw_reason, str) and raw_reason in _SAFE_HOST_MEMORY_REASON_CODES:
            reason: str | None = raw_reason
        elif has_errors:
            reason = "operation_failed"
        else:
            reason = "no_matching_memory" if intent == "forget" else "not_applied"
    else:
        reason = None

    envelope = {
        "host_memory_operation_result": {
            "intent": intent,
            "applied": applied,
            "saved_count": saved_count,
            "deleted_count": deleted_count,
            "reason": reason,
        }
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n" + _HOST_MEMORY_OPERATION_INSTRUCTION


def settle_in_tx(data: dict[str, Any], row: dict[str, Any], emit) -> None:
    """Settle pending task control inside the existing root store transaction."""
    settle(row, emit, data=data)


def on_task_lifecycle_in_tx(data: dict[str, Any], task_id: str, operation: str) -> None:
    """Hook called inside the same store transaction when a task is archived, restored, or deleted."""
    if operation in {"archive", "restore", "delete"}:
        if "memory" in data or operation == "delete":
            memory.record_task_lifecycle_fence(data, task_id=task_id, operation=operation)
            propagate_memory_revocation_in_tx(data, exclude_task_id=task_id)


def select_guidance_and_memory(
    runtime: Any,
    row: dict[str, Any],
    *,
    exclude_optional_memory: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read filtered preferences and eligible active memories for `row` without mutating store state."""
    ok_dest, bound_dest, _ = verify_task_destination_for_memory(runtime, row)
    row_for_prefs = (
        {**row, "model_destination": bound_dest}
        if ok_dest and bound_dest is not None
        else {**row, "model_destination": None, "model_id": ""}
    )

    def inspect_state(data: dict[str, Any]):
        prefs = knowledge.preferences(data)
        filtered_prefs = memory.filter_manual_preferences_for_task(data, row_for_prefs, prefs)
        overrides = (
            ((data.get("memory") or {}).get("manual_preference_exposure_overrides") or {})
            if isinstance(data, dict)
            else {}
        )
        disclosed_restricted_prefs = bool(
            overrides
            and isinstance(filtered_prefs, dict)
            and any(
                isinstance(item, dict) and item.get("id") in overrides
                for item in (filtered_prefs.get("items") or [])
            )
        )
        if exclude_optional_memory or row.get("_exclude_optional_memory"):
            return filtered_prefs, None, disclosed_restricted_prefs
        if isinstance(row.get("project"), dict):
            try:
                projects.validate_binding(data, row["project"])
            except AgentError:
                return filtered_prefs, None, disclosed_restricted_prefs
        if not ok_dest or bound_dest is None:
            return filtered_prefs, None, disclosed_restricted_prefs
        row_for_select = {**row, "model_destination": bound_dest}
        selection = memory.select_memories_for_task(
            data,
            row_for_select,
        )
        return filtered_prefs, selection, disclosed_restricted_prefs

    if hasattr(runtime.store, "inspect"):
        prefs, selection, disclosed_restricted_prefs = runtime.store.inspect(inspect_state)
    else:
        prefs, selection, disclosed_restricted_prefs = inspect_state(runtime.store.read())
    if not (exclude_optional_memory or row.get("_exclude_optional_memory")):
        row["_disclosed_restricted_prefs"] = bool(disclosed_restricted_prefs)
    base_guidance = knowledge.messages(row, prefs, memory_selection=None)
    return base_guidance, selection


# --- Owner Memory API Handlers on AgentRuntime ---


def get_memory_snapshot(
    runtime: Any,
    *,
    scope_kind: str | None = None,
    project_id: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    if scope_kind not in {None, "", "personal", "project"}:
        raise AgentError("agent_memory_invalid_scope")
    if status not in {None, "", "active", "superseded", "all"}:
        raise AgentError("agent_memory_invalid_status")
    norm_scope_kind = scope_kind if scope_kind else None
    norm_project_id = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
    norm_status = status if status else "active"

    def view(data: dict[str, Any]) -> dict[str, Any]:
        proj_gen = None
        if norm_project_id is not None:
            proj = (data.get("projects") or {}).get(norm_project_id)
            if isinstance(proj, dict) and proj.get("enabled"):
                proj_gen = int(proj.get("generation", 1))
        return memory.list_memories_snapshot(
            data,
            scope_kind=norm_scope_kind,
            project_id=norm_project_id,
            project_generation=proj_gen,
            status=norm_status,
        )

    return runtime.store.inspect(view) if hasattr(runtime.store, "inspect") else view(runtime.store.read())


def search_owner_memories(
    runtime: Any,
    *,
    query: str = "",
    scope_kind: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    if scope_kind not in {None, "", "personal", "project"}:
        raise AgentError("agent_memory_invalid_scope")
    if type(limit) is not int or limit < 1 or limit > 100:
        raise AgentError("agent_memory_invalid_limit")
    norm_scope_kind = scope_kind if scope_kind else None
    norm_project_id = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
    norm_category = category.strip() if isinstance(category, str) and category.strip() else None

    def view(data: dict[str, Any]) -> dict[str, Any]:
        proj_gen = None
        if norm_project_id is not None:
            proj = (data.get("projects") or {}).get(norm_project_id)
            if isinstance(proj, dict) and proj.get("enabled"):
                proj_gen = int(proj.get("generation", 1))
        return memory.search_memories(
            data,
            query=str(query or ""),
            scope_kind=norm_scope_kind,
            project_id=norm_project_id,
            project_generation=proj_gen,
            category=norm_category,
            include_candidates=False,
            for_model_tool=False,
            limit=limit,
        )

    return runtime.store.inspect(view) if hasattr(runtime.store, "inspect") else view(runtime.store.read())


def update_owner_memory_settings(runtime: Any, value: dict[str, Any]) -> dict[str, Any]:
    expected_gen, req_id = require_common_write_fields(value)
    allowed = {
        "expected_generation",
        "client_request_id",
        "capture_enabled",
        "use_enabled",
        "activation_policy",
        "extraction_destination",
        "default_exposure",
        "budgets",
    }
    if set(value) - allowed:
        raise AgentError("agent_memory_invalid_settings")

    raw_patch = {
        k: copy.deepcopy(value[k])
        for k in (
            "capture_enabled",
            "use_enabled",
            "activation_policy",
            "extraction_destination",
            "default_exposure",
            "budgets",
        )
        if k in value
    }

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        hit, cached = memory._record_idempotency(mem, req_id, "owner_update_settings", raw_patch)
        if hit:
            return memory.list_memories_snapshot(data)

        # Ensure all existing owner messages in tasks are stamped with source_seq before frontier is updated
        memory_natural.stamp_all_tasks_owner_messages_in_tx(data)

        patch: dict[str, Any] = {}
        if "capture_enabled" in value:
            if not isinstance(value["capture_enabled"], bool):
                raise AgentError("agent_memory_invalid_settings")
            patch["capture_enabled"] = value["capture_enabled"]
        if "use_enabled" in value:
            if not isinstance(value["use_enabled"], bool):
                raise AgentError("agent_memory_invalid_settings")
            patch["use_enabled"] = value["use_enabled"]
        if "activation_policy" in value:
            if value["activation_policy"] not in memory.VALID_ACTIVATION_POLICIES:
                raise AgentError("agent_memory_invalid_settings")
            patch["activation_policy"] = value["activation_policy"]
        if patch.get("capture_enabled") is True:
            if "extraction_destination" not in value or value["extraction_destination"] is None:
                raise AgentError("agent_memory_destination_consent_required")
        if "extraction_destination" in value:
            if value["extraction_destination"] is None:
                patch["extraction_destination"] = None
            else:
                patch["extraction_destination"] = verify_exact_catalog_destination(
                    runtime,
                    value["extraction_destination"],
                    require_available=True,
                )
        if "default_exposure" in value:
            patch["default_exposure"] = validate_owner_default_exposure(runtime, value["default_exposure"])
        if "budgets" in value:
            patch["budgets"] = value["budgets"]

        memory._check_expected_generation(mem, expected_gen)

        current_settings = mem["settings"]
        target_capture = patch.get("capture_enabled", current_settings.get("capture_enabled", False))
        target_dest = (
            patch["extraction_destination"]
            if "extraction_destination" in patch
            else current_settings.get("extraction_destination")
        )
        if target_capture and not target_dest:
            raise AgentError("agent_memory_destination_consent_required")

        memory.update_memory_settings(
            data,
            {**patch, "require_destination_consent": True},
            expected_generation=expected_gen,
            client_request_id=None,
        )
        if not mem["settings"].get("capture_enabled"):
            for job in mem.get("jobs", {}).values():
                if job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = "capture_disabled"
        propagate_memory_revocation_in_tx(data)
        snapshot = memory.list_memories_snapshot(data)
        return memory._save_idempotency(mem, req_id, cached, snapshot)

    return runtime.store.transaction(mutate)


def create_owner_memory_entry(runtime: Any, value: dict[str, Any]) -> dict[str, Any]:
    expected_gen, req_id = require_common_write_fields(value)
    allowed = {
        "expected_generation",
        "client_request_id",
        "text",
        "category",
        "fact_key",
        "kind",
        "scope",
        "exposure_policy",
        "keywords",
    }
    if set(value) - allowed:
        raise AgentError("agent_invalid_request")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        raw_req_sig = {
            "text": value.get("text"),
            "category": value.get("category"),
            "fact_key": value.get("fact_key"),
            "kind": value.get("kind"),
            "scope": value.get("scope"),
            "exposure_policy": value.get("exposure_policy"),
            "keywords": value.get("keywords"),
        }
        hit, cached = memory._record_idempotency(mem, req_id, "owner_create_entry", raw_req_sig)
        if hit:
            return memory.list_memories_snapshot(data)
        if "scope" not in value:
            raise AgentError("agent_memory_invalid_scope")
        if "exposure_policy" not in value:
            raise AgentError("agent_memory_invalid_exposure_policy")
        exposure = validate_owner_exposure_policy(runtime, value.get("exposure_policy"), required=True)
        scope = resolve_and_verify_scope(data, value.get("scope"), required=True)
        op_payload = {
            "text": value.get("text"),
            "category": value.get("category"),
            "fact_key": value.get("fact_key"),
            "kind": value.get("kind"),
            "scope": scope,
            "exposure_policy": exposure,
        }
        memory.upsert_active_entry(
            data,
            op_payload,
            origin="manual",
            expected_generation=expected_gen,
            client_request_id=None,
        )
        propagate_memory_revocation_in_tx(data)
        snapshot = memory.list_memories_snapshot(data)
        return memory._save_idempotency(mem, req_id, cached, snapshot)

    return runtime.store.transaction(mutate)


def update_owner_memory_entry(runtime: Any, entry_id: str, value: dict[str, Any]) -> dict[str, Any]:
    expected_gen, req_id = require_common_write_fields(value)
    expected_entry_rev = value.get("expected_entry_revision")
    if type(expected_entry_rev) is not int or expected_entry_rev < 1:
        raise AgentError("agent_memory_invalid_revision")
    allowed = {
        "expected_generation",
        "expected_entry_revision",
        "client_request_id",
        "text",
        "category",
        "fact_key",
        "kind",
        "scope",
        "exposure_policy",
        "keywords",
    }
    if set(value) - allowed:
        raise AgentError("agent_invalid_request")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        raw_req_sig = {
            "entry_id": entry_id,
            "text": value.get("text"),
            "category": value.get("category"),
            "fact_key": value.get("fact_key"),
            "kind": value.get("kind"),
            "scope": value.get("scope"),
            "exposure_policy": value.get("exposure_policy"),
            "keywords": value.get("keywords"),
        }
        hit, cached = memory._record_idempotency(mem, req_id, "owner_update_entry", raw_req_sig)
        if hit:
            return memory.list_memories_snapshot(data)
        memory._check_expected_generation(mem, expected_gen)
        existing = mem["entries"].get(entry_id)
        if existing is None or existing.get("status") != "active":
            raise AgentError("agent_memory_not_found")
        if existing.get("revision") != expected_entry_rev:
            raise AgentError("agent_memory_revision_conflict")

        scope = (
            resolve_and_verify_scope(data, value["scope"], required=True)
            if "scope" in value and value["scope"] is not None
            else copy.deepcopy(existing["scope"])
        )
        prev_exp_rev = int((existing.get("exposure_policy") or {}).get("revision", 0))
        exposure = (
            validate_owner_exposure_policy(runtime, value["exposure_policy"], required=True, prev_revision=prev_exp_rev)
            if "exposure_policy" in value and value["exposure_policy"] is not None
            else copy.deepcopy(existing["exposure_policy"])
        )
        op_payload = {
            "id": entry_id,
            "text": value.get("text", existing.get("text")),
            "category": value.get("category", existing.get("category")),
            "fact_key": value.get("fact_key", existing.get("fact_key")),
            "kind": value.get("kind", existing.get("kind")),
            "scope": scope,
            "exposure_policy": exposure,
        }
        memory.upsert_active_entry(
            data,
            op_payload,
            origin="manual",
            expected_generation=expected_gen,
            expected_entry_revision=expected_entry_rev,
            client_request_id=None,
        )
        propagate_memory_revocation_in_tx(data)
        snapshot = memory.list_memories_snapshot(data)
        return memory._save_idempotency(mem, req_id, cached, snapshot)

    return runtime.store.transaction(mutate)


def delete_owner_memory_entry(runtime: Any, entry_id: str, value: dict[str, Any]) -> dict[str, Any]:
    expected_gen, req_id = require_common_write_fields(value)
    expected_entry_rev = value.get("expected_entry_revision")
    if type(expected_entry_rev) is not int or expected_entry_rev < 1:
        raise AgentError("agent_memory_invalid_revision")
    if set(value) - {"expected_generation", "expected_entry_revision", "client_request_id"}:
        raise AgentError("agent_invalid_request")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        hit, cached = memory._record_idempotency(mem, req_id, "owner_delete_entry", {"entry_id": entry_id})
        if hit:
            return memory.list_memories_snapshot(data)
        memory.delete_memory_entry(
            data,
            entry_id,
            expected_entry_revision=expected_entry_rev,
            expected_generation=expected_gen,
            client_request_id=None,
        )
        propagate_memory_revocation_in_tx(data)
        snapshot = memory.list_memories_snapshot(data)
        return memory._save_idempotency(mem, req_id, cached, snapshot)

    return runtime.store.transaction(mutate)


def decide_owner_memory_candidate(runtime: Any, candidate_id: str, value: dict[str, Any]) -> dict[str, Any]:
    expected_gen, req_id = require_common_write_fields(value)
    expected_cand_rev = value.get("expected_candidate_revision")
    if type(expected_cand_rev) is not int or expected_cand_rev < 1:
        raise AgentError("agent_memory_invalid_revision")
    allowed = {
        "expected_generation",
        "expected_candidate_revision",
        "client_request_id",
        "decision",
        "edited_text",
        "edited_category",
        "category",
        "edited_fact_key",
        "fact_key",
        "edited_scope",
        "scope",
        "exposure_policy",
    }
    if set(value) - allowed:
        raise AgentError("agent_invalid_request")

    decision = value.get("decision")
    raw_edited_text = value.get("edited_text")
    raw_category = value.get("edited_category") if "edited_category" in value else value.get("category")
    raw_fact_key = value.get("edited_fact_key") if "edited_fact_key" in value else value.get("fact_key")
    raw_scope = value.get("edited_scope") if "edited_scope" in value else value.get("scope")
    raw_exposure = value.get("exposure_policy")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        sig_payload = {
            "candidate_id": candidate_id,
            "decision": decision,
            "edited_text": raw_edited_text,
            "edited_category": raw_category,
            "edited_fact_key": raw_fact_key,
            "edited_scope": raw_scope,
            "exposure_policy": raw_exposure,
        }
        hit, cached = memory._record_idempotency(mem, req_id, "owner_decide_candidate", sig_payload)
        if hit:
            return memory.list_memories_snapshot(data)

        if decision not in {"accept", "reject", "edit_and_accept"}:
            raise AgentError("agent_memory_invalid_decision")
        has_edits = any(
            item is not None
            for item in (raw_edited_text, raw_category, raw_fact_key, raw_scope, raw_exposure)
        )
        if decision == "edit_and_accept" and not has_edits:
            raise AgentError("agent_memory_invalid_decision")
        if decision == "reject" and has_edits:
            raise AgentError("agent_memory_invalid_decision")

        edited_scope = (
            resolve_and_verify_scope(data, raw_scope, required=True)
            if raw_scope is not None
            else None
        )
        verified_exposure = (
            validate_owner_exposure_policy(runtime, raw_exposure, required=True)
            if raw_exposure is not None
            else None
        )

        actual_decision = "edit_and_accept" if decision == "accept" and has_edits else decision
        memory.decide_candidate(
            data,
            candidate_id,
            decision=actual_decision,
            edited_text=raw_edited_text,
            edited_category=raw_category,
            edited_fact_key=raw_fact_key,
            edited_scope=edited_scope,
            exposure_policy=verified_exposure,
            expected_candidate_revision=expected_cand_rev,
            expected_generation=expected_gen,
            client_request_id=None,
        )
        propagate_memory_revocation_in_tx(data)
        snapshot = memory.list_memories_snapshot(data)
        return memory._save_idempotency(mem, req_id, cached, snapshot)

    return runtime.store.transaction(mutate)


def reset_owner_memory(runtime: Any, value: dict[str, Any]) -> dict[str, Any]:
    expected_gen, req_id = require_common_write_fields(value)
    if set(value) - {"expected_generation", "client_request_id", "scope"}:
        raise AgentError("agent_invalid_request")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        hit, cached = memory._record_idempotency(mem, req_id, "owner_reset_memory", {"scope": value.get("scope")})
        if hit:
            return memory.list_memories_snapshot(data)
        norm_scope = resolve_and_verify_scope(data, value.get("scope"), required=False, allow_all=True)
        memory.reset_memory(
            data,
            scope=norm_scope,
            expected_generation=expected_gen,
            client_request_id=None,
        )
        propagate_memory_revocation_in_tx(data)
        snapshot = memory.list_memories_snapshot(data)
        return memory._save_idempotency(mem, req_id, cached, snapshot)

    return runtime.store.transaction(mutate)
