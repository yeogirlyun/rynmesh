"""Same-scope document task through the pinned upstream runtime."""

import copy
from contextlib import nullcontext

from budget_host import ReferenceBudget
from operation_host import ReferenceOperationAdmission
from tool_host import ReferenceToolHost
from tool_menu_host import ReferenceMenuTools
from worker_host import run_worker

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.output_policy import select
from rynmesh.agent_runtime.store import encoded


def execute(*, runtime, tools, catalog, selected, task, bridge, record, home, node, upstream, allow_write=None, real_model=True,
            dispatch_observer=None, dispatch_provenance=None, native_business_operations=False, source_owner_input=None,
            recovery=None, streaming=False, model_event=None, native_session_id=None,
            resume_action=None, owner_message=None, rpc_sequence_start=0, before_approval=None,
            compact_event=None):
    if (dispatch_observer is None) != (dispatch_provenance is None):
        raise ValueError("reference_dispatch_observer_incomplete")
    if native_session_id is not None and (recovery is not None or resume_action not in {"continue", "owner_turn"}):
        raise ValueError("reference_invalid_resume_action")
    if native_session_id is None and (resume_action is not None or owner_message is not None):
        raise ValueError("reference_resume_session_required")
    if recovery is not None and before_approval is not None:
        raise ValueError("reference_approval_observer_conflict")
    if type(rpc_sequence_start) is not int or rpc_sequence_start < (1 if native_session_id else 0):
        raise ValueError("reference_resume_rpc_sequence_required")
    if native_business_operations and selected != task["model_id"]:
        raise ValueError("reference_task_model_mismatch")
    current_task = runtime.store.read()["tasks"][task["id"]]
    context = runtime._execution_context(current_task)
    source_host = None
    if source_owner_input is not None:
        if not native_business_operations:
            raise ValueError("reference_source_admission_required")
        from source_input_host import ReferenceSourceInput
        source_host = tools = ReferenceSourceInput(tools, runtime, context, source_owner_input, record)
    limit = runtime._context_limit(current_task)
    tools = ReferenceMenuTools(tools, max_context_bytes=limit,
                              epoch=context.get("context_epoch", 0), goal_revision=context["goal_revision"])

    def approved(request):
        with meter.owner_wait():
            allowed = (allow_write(request) if allow_write is not None else
                       request["name"] == "ryn_create_text_file" and request["arguments"].get("name") == "week-41.md")
        record["approvals"].append({"approved": allowed, "approval": request})
        return allowed

    admission = ReferenceOperationAdmission(runtime, task["id"], context) if native_business_operations else None
    gate = ReferenceToolHost(tools, policy=approved, cancelled=lambda: False, context=context, admission=admission,
                            session_id=recovery.session_id if recovery else native_session_id,
                            before_approval=recovery.before_approval if recovery else before_approval,
                            after_business=recovery.after_business if recovery else None)
    meter = ReferenceBudget(runtime, task["id"], guard=gate.check_current_permissions, defer_start=True, admission=admission)
    if callable(getattr(dispatch_observer, "attach_reference_meter", None)):
        dispatch_observer.attach_reference_meter(meter)
    gate.cancelled = meter.cancelled
    if source_host is not None:
        source_host.bind(meter, admission)
    context["_model_probe"] = meter.probe
    model = catalog.resolve(selected)

    def business_dispatch(method, params, on_event=None):
        if method == "approval":
            return gate.approval(params)
        if method == "tool":
            if source_host is not None and params["name"] == "ryn_request_input":
                return gate.execute(params)  # Same uncharged internal control as candidate.
            result = meter.tool(lambda: gate.execute(params))
            return source_host.after_tool(params, result) if source_host is not None else result
        gate.before_inference()
        try:
            specs = tools.project(params["messages"], params["tools"])
        except Exception:
            record.setdefault("reference_context_requests", []).append({"messages_bytes": len(encoded(params["messages"])),
                "schema_bytes": None, "input_bytes": None, "projection_rejected": True,
                "native_query_source": params.get("query_source")})
            raise
        input_bytes = len(encoded(params["messages"])) + len(encoded(specs))
        record.setdefault("reference_context_requests", []).append({"input_bytes": input_bytes,
            "messages_bytes": len(encoded(params["messages"])), "schema_bytes": len(encoded(specs)),
            "tool_count": len(specs), "within_limit": input_bytes <= limit,
            "native_query_source": params.get("query_source")})
        if input_bytes > limit:
            raise ValueError("reference_context_limit")
        policy = select(model, params["messages"], specs, request_cap=params.get("max_output_tokens"))
        return meter.model(lambda allowed: bridge.complete(selected, params["messages"], specs, gate.cancelled,
                               **({"on_event": on_event} if on_event is not None else {}),
                               expected_destination=context["model_destination"], max_output_tokens=allowed,
                               **({"deadline": meter.request_deadline(), "on_request_policy": meter.record_request_policy}
                                  if getattr(bridge, "supports_request_deadline", False) is True else {})),
                           input_bytes=input_bytes,
                           max_output_tokens=policy["effective_cap"], output_policy=policy)

    def dispatch(method, params, *, request_context=None, on_event=None):
        if recovery is not None and recovery.phase == "resume":
            raise ValueError("reference_unexpected_recovery_dispatch")
        if callable(getattr(dispatch_observer, "invoke", None)):
            return dispatch_observer.invoke(method, params, request_context, lambda: business_dispatch(method, params, on_event))
        observation = (dispatch_observer(method, params, request_context)
                       if dispatch_observer is not None else nullcontext())
        with observation:
            return business_dispatch(method, params, on_event)

    config = {"session_id": gate.session, "session_db_path": str(home / "reference-session.sqlite"),
              "provider_id": "ryn-source", "model_id": model["model_id"], "context_window": model["context_window"],
              "max_output_tokens": select(model, [], [])["effective_cap"],
              "max_model_calls": task["limits"]["max_steps"], "max_seconds": task["limits"]["max_seconds"], "real_model": real_model,
              "prompt": record["prompt"], "tools": tools.specs(),
              "streaming": streaming,
              "observe_compaction": compact_event is not None,
              "rpc_sequence_start": rpc_sequence_start,
              "tool_metadata": {name: tools.metadata(name) for name in gate.allowed}}
    # Public upstream auto-compaction estimates characters / 4. Its window is
    # input + output; the acceptance cap is serialized input bytes including
    # schemas. This is a scheduling approximation, never permission to exceed
    # the unchanged exact UTF-8 byte gate above.
    initial_schemas, _ = tool_menu.project(tools.tools.specs(), [], {}, max_context_bytes=limit,
        authority_bytes=len(encoded([{"role": "user", "content": record["prompt"]}])),
        epoch=context.get("context_epoch", 0), goal_revision=context["goal_revision"])
    schema_bytes = len(encoded(initial_schemas))
    input_estimate = max(1, (limit - schema_bytes) // 4)
    output_reserve = min(config["max_output_tokens"], 21000)
    config["compact_policy"] = {"contextWindow": min(model["context_window"], input_estimate + output_reserve),
                                "bufferTokens": max(1, input_estimate // 10)}
    record["reference_context_policy"] = {"max_input_bytes": limit, "initial_schema_bytes": schema_bytes,
        "estimated_input_tokens": input_estimate, "estimated_bytes_per_token": 4,
        "physical_context_window": model["context_window"], "output_reserve_tokens": output_reserve,
        "native_compact": copy.deepcopy(config["compact_policy"]), "exact_byte_gate": True}
    if dispatch_provenance is not None:
        config["dispatch_provenance"] = dispatch_provenance
    if native_session_id is not None:
        config.update(resume=True, resume_action=resume_action, owner_message=owner_message)
    if recovery is not None:
        recovery.bind(gate, tools, admission, config)
    elif streaming or before_approval is not None or native_session_id is not None or compact_event is not None:
        # One native process owns this task's control session. Cold continuation
        # is admitted only after that process returned a real native stop result.
        def bind_control_session(data):
            row = data["tasks"][task["id"]]
            prior = row.get("reference_control_session")
            identity = {"task_id": task["id"], "session_id": gate.session,
                        "session_db_path": config["session_db_path"], "model_id": selected,
                        "model_destination": copy.deepcopy(context["model_destination"]),
                        "scope_revision": context["scope_revision"]}
            if native_session_id is not None:
                if (not prior or prior.get("state") != "stopped"
                        or any(prior.get(k) != v for k, v in identity.items())
                        or prior["last_rpc_id"] != rpc_sequence_start):
                    raise ValueError("reference_resume_binding_mismatch")
                if resume_action == "continue" and prior["goal_revision"] != context["goal_revision"]:
                    raise ValueError("reference_resume_goal_changed")
                if resume_action == "owner_turn":
                    owners = [*row["messages"], *row.get("pending_user_messages", [])]
                    latest = next((m for m in reversed(owners) if m["role"] == "user"), {})
                    if prior["goal_revision"] >= context["goal_revision"] or latest.get("content") != owner_message:
                        raise ValueError("reference_owner_turn_not_admitted")
            elif prior and prior.get("state") == "active":
                raise ValueError("reference_session_already_active")
            row["reference_control_session"] = {**identity, "goal_revision": context["goal_revision"],
                "state": "active", "last_rpc_id": rpc_sequence_start}
        runtime.store.transaction(bind_control_session)
    try:
        def cancelled_control():
            current = runtime.store.read()["tasks"][task["id"]]
            return {"cancelled": "cancel", "paused": "pause"}.get(current["status"])
        record["reference"] = run_worker(config, dispatch, node=node.resolve(), upstream=upstream.resolve(),
                                         owner_wait_seconds=meter.owner_wait_seconds,
                                         process_observer=recovery.attach if recovery else None, model_event=model_event,
                                         compact_event=compact_event, cancelled_control=cancelled_control)
        if recovery is not None:
            record["reference_recovery"] = recovery.finish(record["reference"])
        elif streaming or before_approval is not None or native_session_id is not None or compact_event is not None:
            def save_control_session(data):
                saved = data["tasks"][task["id"]]["reference_control_session"]
                if saved["session_id"] != gate.session or saved["state"] != "active":
                    raise ValueError("reference_control_session_changed")
                result = record["reference"].get("result", {})
                stopped = result.get("owner_control", {}).get("result", {}).get("kind") == "stopped"
                saved.update(state="stopped" if stopped else "ended", last_rpc_id=record["reference"]["last_rpc_id"])
            runtime.store.transaction(save_control_session)
    finally:
        record["tool_events"] = gate.events
        record["reference_budget"] = meter.snapshot()
        record["reference_owner_wait_seconds"] = meter.owner_wait_seconds()
        record["reference_tool_menu"] = tools.state
        if admission is not None:
            row = runtime.store.read()["tasks"][task["id"]]
            record["reference_business_operations"] = copy.deepcopy(row.get("reference_operations", {}))
            record["reference_current_task_model_unchanged"] = row["model_id"] == task["model_id"] == selected
            if source_host is not None:
                record["reference_source_inputs"] = copy.deepcopy(row.get("reference_source_inputs", {}))
