"""Explicit destination confirmation at a stopped, owner-controlled checkpoint."""
from __future__ import annotations

import copy

from . import projects, reasoning
from .control import ambiguous, check_version, close_queue, remember_request, request_replayed
from .store import AgentError

SAFE_STATES = {"paused", "completed", "failed", "waiting_input", "waiting_approval", "interrupted"}
DATA_POLICY = {
    "sent_after_resume": ["owner_messages", "current_goal", "selected_skills", "current_saved_preferences"],
    "excluded": ["previous_model_messages", "previous_tool_results", "previous_summaries", "previous_resource_grants"],
    "resources_require_explicit_transfer_or_new_authorization": True,
    "automatic_resume": False,
}


def available(row):
    return (not row.get("archived") and row["status"] in SAFE_STATES
            and not row.get("in_flight") and not ambiguous(row) and not row.get("pending_control"))


def resolve(runtime, model_id):
    if not isinstance(model_id, str) or not model_id or len(model_id) > 1024:
        raise AgentError("agent_invalid_model")
    model = runtime._model_metadata(model_id)
    if model is None or not model.get("available") or model.get("agent_supported") is not True:
        raise AgentError("agent_model_unavailable")
    destination = runtime._bound_destination(model)
    # Task history itself contains owner data. Never waive actual destination
    # binding merely because no NAS document is currently authorized.
    return model, destination


def _validate_transferable_scope(runtime, row, control_state=None):
    if row["scope"]["resource_grants"]:
        runtime._validate_context(row, control_state=control_state)
        return
    state = control_state if control_state is not None else runtime.store.read()
    if row.get("project"):
        projects.validate_binding(state, row["project"])
    context = runtime._execution_context(row)
    if runtime._requires_destination_binding(row, context):
        if (
            not context["model_destination"].get("destination_revision")
            or getattr(runtime.model, "supports_destination_binding", False) is not True
        ):
            raise AgentError("agent_model_destination_unverifiable")
    if hasattr(runtime.tools, "validate_context"):
        runtime.tools.validate_context(context)


def preview(runtime, task_id, model_id):
    from .runtime import task
    state = runtime.store.read()
    row = task(state, task_id)
    if not available(row):
        raise AgentError("agent_operation_unresolved" if ambiguous(row) or row.get("in_flight") else "agent_invalid_transition")
    model, destination = resolve(runtime, model_id)
    scope_authorized = bool(row["scope"]["resource_grants"] or row.get("project"))
    if scope_authorized:
        try:
            _validate_transferable_scope(runtime, row, control_state=state)
        except AgentError:
            scope_authorized = False
    return {"model": {key: model.get(key) for key in ("id", "name", "source", "source_name", "source_id", "peer_id")},
            "current_model_id": row["model_id"], "destination": destination,
            "control_revision": row["control_revision"], "scope_revision": row["scope_revision"],
            "current_scope_selection": copy.deepcopy(row["scope"].get("selection", {"resources": [], "allow_model_data": False})),
            "current_scope_authorized": scope_authorized,
            "current_project": ({key: row["project"][key] for key in ("id", "name", "path")} if row.get("project") else None),
            "data_policy": copy.deepcopy(DATA_POLICY), "confirmation_required": True}


def change(runtime, task_id, value):
    from .runtime import event, public_task, task
    fields = {"model_id", "expected_control_revision", "expected_scope_revision", "expected_destination",
              "confirm_data_destination", "transfer_resource_scope", "client_request_id"}
    if (not isinstance(value, dict) or set(value) != fields or value["confirm_data_destination"] is not True
            or type(value["transfer_resource_scope"]) is not bool or not value["client_request_id"]):
        raise AgentError("agent_invalid_request")
    request = {**value, "operation": "switch_task_model"}
    initial = runtime.store.read()
    selected = task(initial, task_id)
    if request_replayed(selected, request):
        return public_task(selected, settings=initial["settings"])
    check_version(selected, value)
    if not available(selected):
        raise AgentError("agent_operation_unresolved" if ambiguous(selected) or selected.get("in_flight") else "agent_invalid_transition")
    model, destination = resolve(runtime, value["model_id"])
    if value["expected_destination"] != destination:
        raise AgentError("agent_model_destination_changed")
    selection = copy.deepcopy(selected["scope"].get("selection", {"resources": [], "allow_model_data": False}))
    prepared = None
    if value["transfer_resource_scope"]:
        if not selected.get("project") and (not selected["scope"]["resource_grants"] or not selection.get("resources")):
            raise AgentError("agent_scope_reselection_required")
        # A stale retained selection is not authority. Validate the actual old
        # grants before constructing fresh grants for the exact new destination.
        _validate_transferable_scope(runtime, selected, control_state=initial)
        if selected["scope"]["resource_grants"]:
            prepared = runtime._prepare_scope(selection, destination, destination["destination_revision"])
        _validate_transferable_scope(runtime, selected)

    def apply(data):
        row = task(data, task_id)
        if request_replayed(row, request):
            return public_task(row, settings=data["settings"])
        check_version(row, value)
        if type(value["expected_scope_revision"]) is not int or value["expected_scope_revision"] != row["scope_revision"]:
            raise AgentError("agent_scope_revision_conflict")
        if not available(row):
            raise AgentError("agent_operation_unresolved" if ambiguous(row) or row.get("in_flight") else "agent_invalid_transition")
        if value["transfer_resource_scope"] and row.get("project"):
            projects.validate_binding(data, row["project"])
        previous = {"project": copy.deepcopy(row.get("project")), "model_id": row["model_id"], "destination": copy.deepcopy(row.get("model_destination")),
                    "scope_revision": row["scope_revision"], "context_epoch": row.get("context_epoch", 0)}
        # Close protocol pairs in the old epoch before retiring that projection.
        close_queue(row, event, "agent_model_changed", keep_flight=False)
        row.setdefault("model_history", []).append(previous)
        row.setdefault("plan_history", []).append(copy.deepcopy(row["plan"]))
        row.setdefault("verification_history", []).append({"outcome": copy.deepcopy(row["outcome"]),
                                                            "evidence": copy.deepcopy(row.get("verification_evidence", []))})
        row.update(model_id=value["model_id"], model_destination=copy.deepcopy(destination), model_destination_confirmed=True,
                   status="paused", error=None, pending_approval=None, approved=None, pending_input=None, result="")
        if "reasoning_effort" in row:
            try:
                reasoning.validate(model, row["reasoning_effort"])
            except AgentError:
                row.pop("reasoning_effort")
        if row.get("project") and not value["transfer_resource_scope"]:
            row.pop("project")
            row["local_execution"] = {"workspace_root": runtime.store.default_workspace(), "desktop_access": False}
        row["scope_revision"] += 1
        retained_selection = copy.deepcopy(selection)
        if prepared is None:
            retained_selection["allow_model_data"] = False
        row["scope"] = {**(copy.deepcopy(prepared) if prepared is not None else {"resource_grants": [], "egress_grants": []}),
                        "revision": row["scope_revision"], "selection": retained_selection}
        row["context_epoch"] = row.get("context_epoch", 0) + 1
        for key in ("context_checkpoint", "verification", "verification_cycle", "model_retry_authority"):
            row.pop(key, None)
        row["model_retry_count"] = 0
        row["plan"] = {"revision": row["plan"]["revision"] + 1, "goal_revision": row["goal"]["revision"], "steps": []}
        row["criteria_proposals"] = []
        row["criteria"] = [criterion for criterion in row["criteria"] if criterion.get("source") != "tool"]
        for criterion in row["criteria"]:
            criterion.update(status="pending", evidence_refs=[])
        row["verification_evidence"] = []
        row["outcome"] = {"status": "unverified", "goal_revision": row["goal"]["revision"], "criteria_results": [],
                          "summary": "", "evidence_refs": [], "uncertainties": ["Model changed; resume explicitly and reauthorize any resource access."]}
        event(row, "model_changed", previous=previous, destination=destination, data_policy=DATA_POLICY,
              scope_revision=row["scope_revision"], resource_scope_transferred=prepared is not None, project_scope_transferred=bool(row.get("project")))
        remember_request(row, request)
        return public_task(row, settings=data["settings"])
    return runtime.store.transaction(apply)
