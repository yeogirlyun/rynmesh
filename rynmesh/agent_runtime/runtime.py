"""Durable host controller for the model-driven AgentLoop.

The worker performs one external operation per tick. Intent is encrypted before
dispatch, and no write is retried after an uncertain result. The model can only
request tools: this module alone decides whether those tools may execute.
"""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from typing import Any

from ..background_workers import WorkerRunResult
from . import (
    automation_worker,
    automations,
    budget,
    credential_control,
    diagnostic_log,
    image_capabilities,
    knowledge,
    lifecycle,
    memory_integration,
    memory_natural,
    model_switch,
    output_policy,
    projects,
    reasoning,
    results,
    timing,
    tool_menu,
    verification,
)
from .control import (
    allowed_actions,
    ambiguous,
    check_version,
    close_queue,
    recoverable_read_in_flight,
    remember_request,
    request_replayed,
    settle,
)
from .input_attachments import attach_to_message, parse_attachments
from .loop import ALL_INTERNAL_NAMES, ALL_INTERNAL_SPECS, AgentLoop
from .prompt import (
    INTERNAL_NAMES,
    PROMPT_VERSION,
    select_base_instructions,
)
from .protocol import digest, event, model_retry_authority, now, sanitized, task
from .state import (
    PUBLIC_STATE_FIELDS,
    initialize,
    owner_update,
    synchronize,
)
from .store import (
    DEFAULT_SETTINGS,
    AgentError,
    AgentModelResponseError,
    AgentReadFailure,
    AgentStore,
    AgentToolRejected,
    cancellation_reason,
    encoded,
)
from .streamed_reads import recover_stream_reads

TERMINAL = {"completed", "cancelled", "failed", "interrupted"}
# A worker continues queued steps for at most this long before yielding its slot.
CONTINUE_SECONDS = 20.0
# Completed streaming reads retain their receipts before inference retry. Writes
# remain behind accepted responses. Retries are bounded by task and lifetime budgets.
RETRYABLE_MODEL_ERRORS = {"agent_inference_timeout", "agent_model_connection_failed", "agent_model_busy",
                          "agent_model_upstream_failed",
                          "agent_invalid_model_response", "agent_compaction_size_limit", "agent_compaction_incomplete"}
PUBLIC_FIELDS = ("id", "client_request_id", "title", "model_id", "reasoning_effort", "status", "revision", "created_at", "updated_at",
                 "messages", "events", "pending_approval", "steps", "result", "error", "limits", "permission_mode", "allowed_write_tools",
                 "model_destination", "local_execution", "project", "verification_evidence", "reconciliation", "archived", "result_refs", "file_artifact_revision",
                 "conversation_revision", "conversation_event_cursor", "source", "automation_id", "automation_run_id", "automation_kind",
                 "latest_turn_is_silent_heartbeat", "automation_notification", *PUBLIC_STATE_FIELDS)
VISUAL_SUFFIXES = (".svg", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".webp")


def public_task(row: dict, *, summary: bool = False, settings: dict | None = None,
                active_run: dict | None = None, monotonic_now: float | None = None) -> dict:
    synchronize(row)
    result = copy.deepcopy({key: row[key] for key in PUBLIC_FIELDS if key in row and not (summary and key in {"messages", "events"})})
    # Metadata changes advance revision too; only an actual completed response
    # identifies a new completion for clients that were away from the task.
    result["completion_event_seq"] = next((item["seq"] for item in reversed(row.get("events", []))
                                           if item["type"] == "completed"), 0)
    result.setdefault("archived", False)
    result["skills"] = [{key: value for key, value in skill.items() if key != "content"} for skill in row.get("skills", [])]
    result["budget"] = budget.public(row)
    result["run_timing"] = timing.snapshot(row, active_run=active_run, monotonic_now=monotonic_now)
    if not summary:
        result["streaming"] = None
    for message in result.get("messages", []):
        # Some providers require their opaque reasoning continuation field back.
        # Keep it encrypted internally; it is never an owner-facing transcript.
        message.pop("reasoning_content", None)
        message.pop("provider_state", None)
        message.pop("resource_origin", None)
        message.pop("owner_source_seq", None)
    for reference in result.get("result_refs", []):
        reference.pop("resource_origin", None)
    if settings is not None:
        result["allowed_actions"] = allowed_actions(row, settings)
    return result


def detached(row: dict, *, summary: bool = False) -> dict:
    """A row public_task may annotate without touching the store's cached copy.

    public_task deep-copies the fields it returns; it only writes run_state and
    the outcome, so those are the parts copied here.
    """
    # A legacy row without a budget derives its usage from events.
    # Keep the events reference for the read-only completion marker. Summaries
    # still omit both events and messages from the public deep copy.
    shallow = {key: value for key, value in row.items()
               if not (summary and "budget" in row and key == "messages")}
    if "outcome" in shallow:
        shallow["outcome"] = copy.deepcopy(shallow["outcome"])
    return shallow


def revision(row: dict, expected: Any) -> None:
    if type(expected) is not int or row["revision"] != expected:
        raise AgentError("agent_revision_conflict")


def write_permission_revoked(row: dict, settings: dict) -> bool:
    from .execution_policy import decide, verified_read_classification
    flight = row.get("in_flight") or {}
    has_computer_access = (
        str(flight.get("name") or "").startswith(("ryn_browser_", "ryn_computer_"))
        or bool(flight.get("computer_access"))
        or bool((flight.get("extension_binding") or {}).get("computer_access"))
    )
    if has_computer_access and (
        not settings.get("desktop_access")
        or not (row.get("local_execution") or {}).get("desktop_access")
    ):
        return True
    if flight.get("effect") != "write":
        return False
    if verified_read_classification(
        flight,
        task_id=row.get("id", ""),
        workspace_root=(row.get("local_execution") or {}).get("workspace_root", ""),
    ):
        return False
    if settings["permission_mode"] == "read_only":
        return True
    if flight.get("authorization") == "automatic":
        return decide(flight, {"effect": flight["effect"],
            "computer_access": flight.get("computer_access"),
            "workstation_permission_tool": flight.get("workstation_permission_tool")}, row, settings).action != "execute"
    return False


def message_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError("agent_invalid_message")
    return value.strip()


class AgentRuntime(AgentLoop):
    def __init__(self, store: AgentStore, model: Any, tools: Any, catalog: Any, *, owner_id="local_owner", consumer_node_id=None):
        self.store, self.model, self.tools, self.catalog = store, model, tools, catalog
        if getattr(tools, "local", None) is not None:
            from .native_computer import bind_owner_native_computer

            tools.local.store = store
            tools.local.native_computer = bind_owner_native_computer(store, local_tools=tools.local)
            if hasattr(tools, "native_computer"):
                tools.native_computer = tools.local.native_computer
        if getattr(tools, "extensions", None) is not None:
            tools.extensions.bind_agent_store(store)
            self.extensions = tools.extensions
        else:
            self.extensions = None
        self.owner_id, self.consumer_node_id = owner_id, consumer_node_id or store.pub
        # Keep the scheduler gate for atomic task registration/recovery. Only
        # external IO runs concurrently; claiming a task remains exclusive.
        self._run_lock = threading.Lock()
        self._run_slots = threading.BoundedSemaphore(6)
        self._active_lock = threading.Lock()
        self._active_runs = {}
        self._queued_since = {}
        self._step = threading.local()  # Whether this thread's last step ended normally.
        self._stream_lock = threading.Lock()
        self._streams = {}
        # Display only. Never used by admission, cancellation or persistence.
        self._active_run = None

    @property
    def _active_run(self):
        with self._active_lock:
            return self._active_runs.get(threading.get_ident())

    @_active_run.setter
    def _active_run(self, value):
        with self._active_lock:
            if value is None:
                self._active_runs.pop(threading.get_ident(), None)
            else:
                self._active_runs[threading.get_ident()] = value

    def _task_run(self, task_id):
        with self._active_lock:
            return next((run.copy() for run in self._active_runs.values() if run["task_id"] == task_id), None)

    def _start_stream(self, selected, step):
        # Live text is a display projection, separate from encrypted recovery
        # checkpoints. The provider callback must not wait on a disk commit for
        # every token, and clients need not wait for the next checkpoint.
        stream = {"step": step, "context_epoch": selected.get("context_epoch", 0),
                  "control_revision": selected["_run_control_revision"], "text": ""}
        with self._stream_lock:
            self._streams[selected["id"]] = stream
        self._step.stream = stream
        return stream

    def _append_stream(self, task_id, stream, text):
        with self._stream_lock:
            if self._streams.get(task_id) is stream:
                stream["text"] += text

    def _clear_stream(self, task_id, stream):
        with self._stream_lock:
            # A following worker may already own the task's next model step.
            if self._streams.get(task_id) is stream:
                self._streams.pop(task_id, None)

    def _stream_snapshot(self, row):
        with self._stream_lock:
            stream = self._streams.get(row["id"])
            flight = row.get("in_flight") or {}
            if (not stream or not stream["text"] or row["status"] != "running"
                    or flight.get("kind") != "model" or not flight.get("dispatched")
                    or stream["control_revision"] != row["control_revision"]
                    or stream["step"] != row["steps"]
                    or stream["context_epoch"] != row.get("context_epoch", 0)):
                return None
            return {key: stream[key] for key in ("step", "context_epoch", "text")}

    def _context_limit(self, row):
        """A model-derived projection; never overwrite the owner's saved limit."""
        return self._context_policy(row)["effective_max_context_bytes"]

    def _context_policy(self, row):
        model = self._model_metadata(row["model_id"]) or {}
        return output_policy.context_budget(model, row["limits"].get("max_context_chars"),
                                            provenance=row.get("context_budget_provenance"))

    def _menu(self, row, *, business_specs=None, max_context_bytes=None):
        business_specs = self.tools.specs() if business_specs is None else business_specs
        if any(spec["function"]["name"] in ALL_INTERNAL_NAMES for spec in business_specs):
            raise AgentError("agent_tool_definition_changed")
        pinned = {**row, "messages": [message for message in row["messages"] if message["role"] == "user"],
                  "context_checkpoint": None, "_exclude_optional_memory": True}
        authority_bytes = len(encoded(self._context(pinned)))
        max_context_bytes = self._context_limit(row) if max_context_bytes is None else max_context_bytes
        desktop = tool_menu.BROWSER_TOOLS if (row.get("local_execution") or {}).get("desktop_access") else ()
        if desktop:
            # Expose installed desktop schemas immediately; dispatch still checks
            # the provider binding and workstation grants on every operation.
            computer_use = tuple(spec["function"]["name"] for spec in business_specs
                                 if spec["function"]["name"].startswith("mcp__computer_use__computer_"))
            desktop = computer_use + desktop
        skill_tools = ("ryn_read_skill_file",) if any(isinstance(s, dict) and s.get("files") for s in (row.get("skills") or [])) else ()
        return tool_menu.project(business_specs, ALL_INTERNAL_SPECS, row.get("tool_menu"),
            max_context_bytes=max_context_bytes, authority_bytes=authority_bytes,
            epoch=row.get("context_epoch", 0), goal_revision=row["goal"]["revision"],
            # Pins need room; in small windows the earlier seeds still apply.
            pinned_names=tool_menu.PLUGIN_DISCOVERY_TOOLS + (("ryn_project_inspect",) if row.get("project") else ()) + tool_menu.CORE_TOOLS + ("ryn_code_exec",) + desktop + skill_tools,
            initial_names=tool_menu.PLUGIN_DISCOVERY_TOOLS + (("ryn_project_inspect", "ryn_file_read", "ryn_file_search", "ryn_file_edit",
                           "ryn_shell_execute", "ryn_file_write", "ryn_file_list", "ryn_code_exec") if row.get("project") else
                          ("ryn_shell_execute", "ryn_file_read", "ryn_file_search", "ryn_code_exec")) + desktop + skill_tools), authority_bytes

    def _specs(self, row=None) -> list:
        specs = self.tools.specs()
        if any(spec["function"]["name"] in ALL_INTERNAL_NAMES for spec in specs):
            raise AgentError("agent_tool_definition_changed")
        if row is not None:
            return self._menu(row)[0][0]
        return [*specs, *(copy.deepcopy(spec) for spec in ALL_INTERNAL_SPECS if spec["function"]["name"] != tool_menu.NAME)]

    def _metadata(self, name: str) -> dict:
        return {"effect": "read", "title": name, "internal": True} if name in ALL_INTERNAL_NAMES else self.tools.metadata(name)

    def _history(self, row: dict) -> list[dict]:
        epoch = row.get("context_epoch", 0)
        return [message for message in row["messages"] if message["role"] == "user" or message.get("context_epoch", 0) == epoch]

    def _execution_context(self, row: dict, operation_id=None) -> dict:
        origins = {}
        for message in self._history(row):
            if message.get("role") == "tool" and isinstance(message.get("resource_origin"), dict) and "grant_id" in message["resource_origin"]:
                origins[digest(message["resource_origin"])] = copy.deepcopy(message["resource_origin"])
        return {"task_id": row["id"], "operation_id": operation_id, "owner_id": self.owner_id,
                "consumer_node_id": self.consumer_node_id, "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"], "scope_revision": row["scope_revision"],
                "local_execution": copy.deepcopy(row.get("local_execution") or {}),
                "model_destination": copy.deepcopy(row.get("model_destination") or {"catalog_model_id": row["model_id"], "provider_peer_id": "", "source_id": "", "destination_revision": ""}),
                "resource_grants": copy.deepcopy(row["scope"]["resource_grants"]),
                "egress_grants": copy.deepcopy(row["scope"]["egress_grants"]), "resource_origins": list(origins.values())}

    @staticmethod
    def _requires_destination_binding(row: dict, context: dict) -> bool:
        # Peer inference carries owner text even without resource access. Old
        # tasks already retain their selected destination; never replace that
        # snapshot with today's catalog while reopening them.
        return bool(row.get("project") or context["resource_grants"] or context["resource_origins"]
                    or row.get("model_destination_confirmed") or row.get("model_destination_required")
                    or row["model_id"].startswith("peer/"))

    def _validate_context(self, row: dict, *, cheap_only=False, control_state=None) -> None:
        if row.get("project"):
            if control_state is not None:
                projects.validate_binding(control_state, row["project"])
            elif hasattr(self.store, "inspect"):
                self.store.inspect(lambda data: projects.validate_binding(data, row["project"]))
            else:
                projects.validate_binding(self.store.read(), row["project"])
        context = self._execution_context(row)
        if self._requires_destination_binding(row, context):
            if not context["model_destination"].get("destination_revision") or getattr(self.model, "supports_destination_binding", False) is not True:
                raise AgentError("agent_model_destination_unverifiable")
            if not cheap_only and self._current_destination(row["model_id"], require_bound=True) != context["model_destination"]:
                raise AgentError("agent_model_destination_changed")
        if hasattr(self.tools, "validate_context"):
            if cheap_only:
                self.tools.validate_context(context, cheap_only=True)
            else:
                self.tools.validate_context(context)
        elif row["scope"]["resource_grants"]:
            raise AgentError("agent_resources_unavailable")

    @staticmethod
    def _destination(model: dict) -> dict:
        return {"catalog_model_id": model["id"], "provider_peer_id": model.get("peer_id", ""),
                "source_id": model.get("source_id", ""), "destination_revision": model.get("destination_revision", "")}

    def _current_destination(self, model_id: str, *, require_bound=False) -> dict:
        model = self._model_metadata(model_id)
        if model is None:
            raise AgentError("agent_model_unavailable")
        return self._bound_destination(model) if require_bound else self._destination(model)

    def _bound_destination(self, model: dict) -> dict:
        """Only locally authenticated catalog evidence can authorize egress."""
        from ..llm_package import peer_binding
        if (model.get("source") == "peer" and not model.get("destination_binding_supported")
                and hasattr(self.catalog, "refresh_destination")):
            model = self.catalog.refresh_destination(model["id"]) or model
        destination = self._destination(model)
        supported = model.get("source") == "local_api"
        if model.get("source") == "local_service":
            binding = model.get("local_model_binding")
            service_id = str(model.get("service_id", ""))
            native = model.get("runtime") == "native_llama_cpp"
            cli = model.get("runtime") == "external" and model.get("adapter") in {"codex_cli", "antigravity_cli"}
            expected_id = "local/" + service_id + ("/model/" + model["cli_model"] if cli and model.get("cli_model") else "")
            supported = ((native or cli)
                         and model.get("peer_id") == self.consumer_node_id
                         and model.get("id") == expected_id
                         and model.get("destination_binding_supported") is True
                         and peer_binding.valid(binding, model.get("service_id"))
                         and peer_binding.local_revision(binding, self.consumer_node_id) == destination["destination_revision"])
        if model.get("source") == "peer" and model.get("destination_binding_supported") is True:
            bound = model.get("expected_model_binding")
            if isinstance(bound, dict) and set(bound) in (
                    {"model_binding", "ai_permission", "consumer_peer_id"},
                    {"model_binding", "space_permission", "consumer_peer_id", "consumer_access_generation"}):
                record = {**bound, "binding_verified": True, "peer_id": model.get("peer_id"),
                          "service_id": model.get("service_id")}
                supported = (model.get("consumer_peer_id") == self.consumer_node_id
                             and peer_binding.expected(record, self.consumer_node_id) is not None
                             and peer_binding.revision(record, self.consumer_node_id) == destination["destination_revision"])
        if (not supported or not destination["destination_revision"]
                or getattr(self.model, "supports_destination_binding", False) is not True):
            raise AgentError("agent_model_destination_unverifiable")
        return destination

    def _prepare_scope(self, selection: Any, destination: dict, expected_revision: Any) -> dict:
        if not hasattr(self.tools, "prepare_scope"):
            raise AgentError("agent_resources_unavailable")
        if isinstance(selection, dict) and selection.get("resources"):
            if not destination.get("destination_revision") or getattr(self.model, "supports_destination_binding", False) is not True:
                raise AgentError("agent_model_destination_unverifiable")
            if expected_revision != destination["destination_revision"]:
                raise AgentError("agent_model_destination_changed")
            if self._current_destination(destination["catalog_model_id"], require_bound=True) != destination:
                raise AgentError("agent_model_destination_changed")
        return self.tools.prepare_scope(selection, owner_id=self.owner_id, consumer_node_id=self.consumer_node_id,
                                        model_destination=destination)

    def _native_artifact_loader(self):
        from .native_computer import NativeComputerFacade

        # __init__ binds this facade to this exact store. Reading it here must not
        # create a worker or reuse another runtime owner's registry.
        facade = getattr(self.store, "_ryn_native_computer", None)
        if isinstance(facade, NativeComputerFacade) and facade._owner is self.store:
            return facade.verify_and_load_registered_artifact
        return None

    def _prepare_dispatch_messages(self, row: dict, messages: list[dict] | None = None):
        msgs = self._context(row) if messages is None else messages
        model = self._model_metadata(row["model_id"]) or {}
        return image_capabilities.materialize_operation_images(
            row,
            msgs,
            model_row=model,
            model_client=self.model,
            native_artifact_loader=self._native_artifact_loader(),
        )

    def _complete(self, row: dict, messages: list, specs: list, cancelled, on_event, *, nested=False, stage=None, output_cap=None, request_id=None, image_context=None):
        # Measure the final provider-visible projection, including JSON string
        # escaping and validated inline image bounds, before reserving or dispatching inference.
        model = self._model_metadata(row["model_id"]) or {}
        context_policy = self._context_policy(row)
        output_policy.admit_request_context(
            model,
            messages,
            specs,
            manual_limit=row["limits"].get("max_context_chars"),
            provenance=row.get("context_budget_provenance"),
            context_policy=context_policy,
        )
        context = self._execution_context(row)
        options = {}
        discloses_memory = memory_integration.messages_disclose_memory(messages, row=row) or bool(row.get("memory_disclosed_in_epoch"))
        if self._requires_destination_binding(row, context):
            if not context["model_destination"].get("destination_revision") or getattr(self.model, "supports_destination_binding", False) is not True:
                raise AgentError("agent_model_destination_unverifiable")
            options["expected_destination"] = context["model_destination"]
        elif discloses_memory:
            ok_dest, verified_dest, _ = memory_integration.verify_task_destination_for_memory(self, row)
            if not ok_dest or verified_dest is None:
                raise AgentError("agent_model_destination_unverifiable")
            options["expected_destination"] = verified_dest
        if getattr(self.model, "supports_request_deadline", False) is True:
            if "_run_started" not in row or "_run_lifetime_remaining" not in row:
                raise AgentError("agent_request_deadline_unavailable")
            # Process-local monotonic values never enter durable task state.
            # Preparation and reservation time consume this same active tick.
            options["deadline"] = row["_run_started"] + min(
                row["limits"]["max_seconds"] - row["elapsed_seconds"], row["_run_lifetime_remaining"])
            def record_request_policy(policy):
                self.store.transaction(lambda data: budget.record_request_policy(task(data, row["id"]), policy))
            options["on_request_policy"] = record_request_policy
        active_request_id = (
            request_id
            if isinstance(request_id, str) and request_id
            else row.get("_request_id")
            if not nested and isinstance(row.get("_request_id"), str) and row.get("_request_id")
            else "req_" + uuid.uuid4().hex
        )
        if active_request_id and getattr(self.model, "supports_request_id", False) is True:
            options["request_id"] = active_request_id

        if "reasoning_effort" in row:
            options["reasoning_effort"] = reasoning.validate(model, row["reasoning_effort"])
            if getattr(self.model, "supports_reasoning_effort", False) is not True:
                raise AgentError("agent_reasoning_effort_unsupported")
        # The window's own conversation may continue a provider thread across
        # turns. Summaries (no tools) and nested checks are separate requests.
        if not nested and specs and getattr(self.model, "supports_conversation_key", False) is True:
            conv_key = (row["id"] + ":edit:" + str(row["conversation_revision"])
                        if row.get("conversation_revision") else row["id"])
            if row.get("memory_conversation_epoch"):
                conv_key = f"{conv_key}:mem:{row['memory_conversation_epoch']}"
            options["conversation"] = conv_key
        dispatch_messages = messages
        image_report = None
        if not nested and stage is None:
            dispatch_messages, image_report = image_capabilities.materialize_operation_images(
                row,
                messages,
                model_row=model,
                model_client=self.model,
                native_artifact_loader=self._native_artifact_loader(),
            )
            output_policy.admit_request_context(
                model,
                dispatch_messages,
                specs,
                manual_limit=row["limits"].get("max_context_chars"),
                provenance=row.get("context_budget_provenance"),
                context_policy=context_policy,
            )
        if image_capabilities.message_has_image_input(dispatch_messages):
            image_capabilities.require_route_image_support(model, model_client=self.model)
        policy = output_policy.select(model, dispatch_messages, specs, request_cap=output_cap)
        meter_row = {**row, "_request_id": active_request_id} if active_request_id else row
        dispatched = False
        def _invoke_model(allowed):
            nonlocal dispatched
            dispatched = True
            return self.model.complete(
                row["model_id"], dispatch_messages, specs, cancelled, on_event,
                **options, **({"max_output_tokens": allowed} if getattr(self.model, "supports_output_token_limit", False) else {}))
        try:
            response = self._meter_model(meter_row, _invoke_model,
                input_bound=policy["estimated_input_tokens"], output_bound=policy["effective_cap"], output_policy_record=policy,
                nested=nested, stage=stage,
                can_reduce_output=getattr(self.model, "supports_output_token_limit", False) is True)
        except Exception:
            if dispatched:
                if image_report and image_report.get("has_candidate_artifacts") and image_report.get("image_prepared"):
                    attempted_linkage = image_capabilities.build_image_request_linkage(
                        request_id=active_request_id,
                        model_id=row["model_id"],
                        capability=image_report["route_capability"],
                        messages=dispatch_messages,
                        stage="main_loop_mcp_observation",
                        source_artifacts=image_report["materialized_artifacts"],
                        task_row=row,
                        dispatch_status="attempted",
                        semantic_response_received=False,
                    )
                    attempted_linkage["observation_status"] = image_report["status"]
                    attempted_linkage["rejected_artifacts"] = list(image_report["rejected_artifacts"])[:4]
                    self._record_image_request_linkage(row, attempted_linkage)
                elif isinstance(image_context, dict):
                    attempted_linkage = image_capabilities.build_image_request_linkage(
                        request_id=active_request_id,
                        model_id=row["model_id"],
                        capability=image_context.get("capability") or image_capabilities.evaluate_image_capability(model, model_client=self.model),
                        messages=dispatch_messages,
                        stage=str(image_context.get("stage") or stage or "image_inspection"),
                        operation_id=image_context.get("operation_id"),
                        source_artifacts=image_context.get("source_artifacts") or [],
                        task_row=row,
                        dispatch_status="attempted",
                        semantic_response_received=False,
                    )
                    self._record_image_request_linkage(row, attempted_linkage)
                    if isinstance(image_context.get("linkage_out"), dict):
                        image_context["linkage_out"].update(attempted_linkage)
                elif image_capabilities.message_has_image_input(dispatch_messages):
                    attempted_linkage = image_capabilities.build_image_request_linkage(
                        request_id=active_request_id,
                        model_id=row["model_id"],
                        capability=image_capabilities.evaluate_image_capability(model, model_client=self.model),
                        messages=dispatch_messages,
                        stage=str(stage or "model"),
                        task_row=row,
                        dispatch_status="attempted",
                        semantic_response_received=False,
                    )
                    self._record_image_request_linkage(row, attempted_linkage)
            raise
        if image_report and image_report.get("has_candidate_artifacts"):
            dispatch_status = "submitted" if image_report.get("image_prepared") else "not_dispatched"
            msg = response.get("message") if isinstance(response, dict) else None
            semantic_ok = bool(
                isinstance(msg, dict)
                and ((isinstance(msg.get("content"), str) and msg["content"].strip()) or msg.get("tool_calls"))
            )
            linkage = image_capabilities.build_image_request_linkage(
                request_id=active_request_id,
                model_id=row["model_id"],
                capability=image_report["route_capability"],
                messages=dispatch_messages,
                stage="main_loop_mcp_observation",
                source_artifacts=image_report["materialized_artifacts"],
                task_row=row,
                dispatch_status=dispatch_status,
                semantic_response_received=semantic_ok,
            )
            linkage["observation_status"] = image_report["status"]
            linkage["rejected_artifacts"] = list(image_report["rejected_artifacts"])[:4]
            self._record_image_request_linkage(row, linkage)
        return response

    def _meter_model(self, selected, invoke, *, input_bound, output_bound, nested=False, stage=None, can_reduce_output=True, output_policy_record=None):
        def admit(data):
            row = task(data, selected["id"])
            if (row["status"] != "running" or row["control_revision"] != selected.get("_run_control_revision", selected["control_revision"])
                    or not data["settings"]["enabled"]):
                raise AgentToolRejected("agent_cancelled")
            state = budget.public(row)
            available = state["limits"]["max_total_tokens"] - state["lifetime"]["charged_tokens"] - input_bound
            if available <= 0 or (available < output_bound and not can_reduce_output):
                raise AgentError("agent_token_budget_exhausted")
            flight = row.get("in_flight") or {}
            record = budget.reserve(row, "model", stage or flight.get("stage") or flight.get("kind", "model"),
                input_bound=input_bound, output_bound=min(output_bound, available), seconds_bound=0 if nested else None)
            if output_policy_record:
                policy = output_policy.budgeted(output_policy_record, record["output_reservation"])
                if selected.get("_request_id"):
                    policy["request_id"] = selected["_request_id"]
                if selected.get("_attempt"):
                    policy["attempt"] = selected["_attempt"]
                row["budget"]["records"][-1]["output_policy"] = policy
                event(row, "model_output_policy", **policy)
            return record
        reservation = self.store.transaction(admit)
        started = time.monotonic()
        response = None
        try:
            response = invoke(reservation["output_reservation"])
            return response
        except AgentModelResponseError as exc:
            response = {"usage": exc.usage}
            rejected_metadata = {"error": str(exc), "finish_reason": exc.finish_reason,
                                 "usage": exc.usage, "stage": reservation["stage"]}
            detail = str(getattr(exc, "detail", "") or "")[:300]
            informative = bool(detail) and reservation["stage"] == "model" and exc.finish_reason != "length"

            def rejected(data):
                row = data["tasks"].get(selected["id"])
                if row is not None:
                    event(row, "model_response_rejected", **rejected_metadata, **({"detail": detail} if detail else {}))
                    if informative:
                        row["rejected_response_note"] = detail
            self.store.transaction(rejected)
            raise
        finally:
            usage = response.get("reported_usage", response.get("usage")) if isinstance(response, dict) else None
            self._settle_usage(selected["id"], reservation["id"], usage=usage, elapsed=0 if nested else time.monotonic() - started)

    def _settle_usage(self, task_id, reservation_id, *, usage=None, elapsed=None):
        def finish(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                budget.settle(row, reservation_id, usage=usage, elapsed_seconds=elapsed)
        self.store.transaction(finish)

    def _settle_usage_batch(self, task_id, reservation_ids, *, usage=None, elapsed=None):
        if not reservation_ids:
            return
        def finish(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                for reservation_id in reservation_ids:
                    budget.settle(row, reservation_id, usage=usage, elapsed_seconds=elapsed)
        self.store.transaction(finish)

    def _record_image_request_linkage(self, selected_row: dict, linkage: dict) -> None:
        clean = copy.deepcopy(linkage)
        task_id = selected_row["id"]
        def record(data):
            row = data["tasks"].get(task_id)
            if row is not None:
                clean["binding_current"] = bool(
                    row.get("model_id") == selected_row.get("model_id")
                    and (row.get("goal") or {}).get("revision") == (selected_row.get("goal") or {}).get("revision")
                    and row.get("scope_revision") == selected_row.get("scope_revision")
                    and row.get("control_revision") == selected_row.get("control_revision")
                    and row.get("context_epoch", 0) == selected_row.get("context_epoch", 0)
                )
                links = list(row.get("image_request_links") or [])
                links.append(clean)
                row["image_request_links"] = links[-16:]
        self.store.transaction(record)

    def _tool_context(self, selected, operation_id=None):
        context = self._execution_context(selected, operation_id)
        context["selected_skills"] = copy.deepcopy(selected.get("skills") or [])
        current_model = self._model_metadata(selected["model_id"]) or {}
        context["_image_capability"] = image_capabilities.evaluate_image_capability(
            current_model, model_client=self.model)
        def inspect_image(messages, cancelled, *, source_artifact=None, linkage_out=None):
            model = self._model_metadata(selected["model_id"]) or {}
            cap = image_capabilities.require_route_image_support(model, model_client=self.model)
            self._validate_context(selected)
            nested_request_id = "req_" + uuid.uuid4().hex
            nested_image_context = {
                "capability": cap,
                "stage": "image_inspection",
                "operation_id": operation_id,
                "source_artifacts": [source_artifact] if isinstance(source_artifact, dict) else [],
                "linkage_out": linkage_out,
            }
            response = self._complete(selected, messages, [], cancelled, lambda _event: None,
                                      nested=True, stage="image_inspection", output_cap=1024,
                                      request_id=nested_request_id,
                                      image_context=nested_image_context)
            message = response.get("message") or {}
            valid_observation = (
                isinstance(message.get("content"), str)
                and bool(message["content"].strip())
                and not message.get("tool_calls")
            )
            linkage = image_capabilities.build_image_request_linkage(
                request_id=nested_request_id,
                model_id=selected["model_id"],
                capability=cap,
                messages=messages,
                stage="image_inspection",
                operation_id=operation_id,
                source_artifacts=[source_artifact] if isinstance(source_artifact, dict) else [],
                task_row=selected,
                dispatch_status="submitted",
                semantic_response_received=valid_observation,
            )
            self._record_image_request_linkage(selected, linkage)
            if isinstance(linkage_out, dict):
                linkage_out.update(linkage)
            if not valid_observation:
                raise AgentToolRejected("agent_image_observation_unavailable")
            return message["content"]
        context["_vision_inference"] = inspect_image
        def probe(invoke, *, max_output_tokens, input_bytes, model_id, source_id, purpose="source_draft_probe"):
            run_started = selected.get("_run_started")
            if run_started is None:
                raise AgentError("agent_probe_budget_unavailable")
            deadline = run_started + min(selected["limits"]["max_seconds"] - selected["elapsed_seconds"],
                                         selected.get("_run_lifetime_remaining", 0))
            def guard():
                data = self.store.read()
                current = task(data, selected["id"])
                if (current["status"] != "running" or current["control_revision"] != selected["_run_control_revision"]
                        or not data["settings"]["enabled"]):
                    raise AgentToolRejected("agent_cancelled")
                self._validate_context(current, cheap_only=True)
                if time.monotonic() >= deadline:
                    raise AgentError("agent_time_budget_exhausted")
            def bounded(allowed):
                guard()
                return invoke(allowed, min(15.0, deadline - time.monotonic()), guard)
            guard()
            return self._meter_model(selected, bounded, input_bound=input_bytes + 512, output_bound=max_output_tokens,
                                     nested=True, stage=purpose)
        context["_model_probe"] = probe
        return context

    def _read_call(self, selected, stage, invoke):
        reservation = self.store.transaction(lambda data: budget.reserve(task(data, selected["id"]), "tool", stage))
        started = time.monotonic()
        try:
            return invoke()
        finally:
            self._settle_usage(selected["id"], reservation["id"], elapsed=time.monotonic() - started)

    def _reserve_output(self, selected):
        def reserve(data):
            row = task(data, selected["id"])
            if row.get("in_flight"):
                self.store.reserve(data, row["id"], output_policy.storage_growth(row["limits"]["max_output_chars"]),
                                   extra_disk=results.MAX_RESULT_BYTES if row["queue"] else 0)
        self.store.transaction(reserve)

    def scope_catalog(self) -> dict:
        result = sanitized(self.tools.scope_catalog()) if hasattr(self.tools, "scope_catalog") else {"connections": [], "supported_formats": []}
        result["create_scope_supported"] = hasattr(self.tools, "prepare_scope") and getattr(self.model, "supports_destination_binding", False) is True
        result["file_scope_supported"] = result["create_scope_supported"]
        return result

    def get_scope(self, task_id: str) -> dict:
        row = lifecycle.find(self.store, self.store.read(), task_id)
        try:
            current = self._current_destination(row["model_id"])
        except AgentError:
            current = None
        return {"scope": copy.deepcopy(row["scope"]), "scope_revision": row["scope_revision"], "control_revision": row["control_revision"],
                "model_destination": self._execution_context(row)["model_destination"], "current_model_destination": current}

    def update_scope(self, task_id: str, value: dict) -> dict:
        if set(value) - {"scope", "expected_control_revision", "expected_scope_revision", "expected_destination_revision", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        snapshot = self.store.read()
        selected = task(snapshot, task_id)
        if request_replayed(selected, value):
            return public_task(selected, settings=snapshot["settings"])
        check_version(selected, value)
        if type(value.get("expected_scope_revision")) is not int or value["expected_scope_revision"] != selected["scope_revision"]:
            raise AgentError("agent_scope_revision_conflict")
        selection = value.get("scope")
        destination = (self._current_destination(selected["model_id"]) if isinstance(selection, dict) and selection.get("resources")
                       else self._execution_context(selected)["model_destination"])
        scope = self._prepare_scope(selection, destination, value.get("expected_destination_revision"))

        def apply(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            if row["scope_revision"] != value["expected_scope_revision"]:
                raise AgentError("agent_scope_revision_conflict")
            if ambiguous(row) or row["status"] == "cancelled":
                raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_invalid_transition")
            row["scope_revision"] += 1
            row["scope"] = {**copy.deepcopy(scope), "revision": row["scope_revision"], "selection": copy.deepcopy(value["scope"])}
            row["model_destination"] = copy.deepcopy(destination)
            # Do not erase encrypted audit evidence. A new provider projection
            # excludes every old tool body, derived assistant text and summary.
            row["context_epoch"] = row.get("context_epoch", 0) + 1
            row.pop("context_checkpoint", None)
            if row.get("memory_disclosed_in_epoch") or row.get("memory_projection_watermark"):
                row["memory_conversation_epoch"] = int(row.get("memory_conversation_epoch", 0)) + 1
                row["memory_disclosed_in_epoch"] = False
            row["plan"] = {"revision": row["plan"]["revision"] + 1, "goal_revision": row["goal"]["revision"], "steps": []}
            row["criteria_proposals"] = []
            row["outcome"].update(status="unverified", criteria_results=[], evidence_refs=[], uncertainties=["Resource permissions changed; prior results are not current verification."])
            for criterion in row["criteria"]:
                criterion.update(status="pending", evidence_refs=[])
            row.update(pending_approval=None, approved=None, pending_input=None)
            row["pending_control"] = {"kind": "scope", "remain_paused": row["status"] in {"paused", "completed", "failed", "interrupted"} or not data["settings"]["enabled"]}
            event(row, "scope_changed", scope_revision=row["scope_revision"])
            close_queue(row, event, "agent_scope_changed")
            memory_integration.settle_in_tx(data, row, event)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(apply)

    def settings(self) -> dict:
        snapshot = self.configuration_snapshot(display_only=True)
        return {**snapshot["settings"], "context_budget_policy": snapshot["context_budget_policy"]}

    def list_projects(self) -> list[dict]:
        return [copy.deepcopy(item) for item in self.store.read().get("projects", {}).values() if item.get("enabled")]

    def load_project(self, value: dict) -> dict:
        def register_and_sync(data):
            res = projects.register(data, value)
            memory_integration.propagate_memory_revocation_in_tx(data)
            return res
        return self.store.transaction(register_and_sync)

    def rename_project(self, project_id: str, value: dict) -> dict:
        return self.store.transaction(lambda data: projects.rename(data, project_id, value))

    def project_selection(self) -> str:
        data = self.store.read()
        ident = data.get("selected_project_id", "")
        return ident if data.get("projects", {}).get(ident, {}).get("enabled") else ""

    def select_project(self, value: dict) -> dict:
        if set(value) != {"project_id"} or not isinstance(value["project_id"], str):
            raise AgentError("agent_invalid_project_id")
        def select(data):
            ident = value["project_id"]
            if ident:
                projects.selected(data, ident)
            data["selected_project_id"] = ident
            return {"selected_project_id": ident}
        return self.store.transaction(select)

    def remove_project(self, project_id: str) -> dict:
        def remove(data):
            project = data.get("projects", {}).get(project_id)
            if not project:
                raise AgentError("agent_project_unavailable")
            project["enabled"] = False
            if data.get("selected_project_id") == project_id:
                data["selected_project_id"] = ""
            memory_integration.propagate_memory_revocation_in_tx(data)
            for row in data["tasks"].values():
                if (row.get("project") or {}).get("id") == project_id and row["status"] not in TERMINAL:
                    row.update(approved=None, pending_approval=None, pending_control={"kind": "pause"}, status="paused")
                    event(row, "permissions_changed", reason="agent_project_permission_revoked")
                    settle(row, event, data=data)
            return {"id": project_id, "removed": True}
        return self.store.transaction(remove)

    def bind_workspace(self, task_id: str, value: dict) -> dict:
        """Owner-only initial directory grant, separate from ordinary chat answers."""
        if set(value) - {"workspace_root", "expected_control_revision", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        from pathlib import Path

        from .local_tools import validate_configuration
        validate_configuration({"workspace_root": value.get("workspace_root")})
        if not value.get("workspace_root"):
            raise AgentError("agent_invalid_workspace")
        root = str(Path(value["workspace_root"]).resolve(strict=True))
        def apply(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            if not data["settings"]["enabled"]:
                raise AgentError("agent_disabled")
            if (row["status"] not in {"failed", "paused", "waiting_input", "completed"}
                    or row.get("in_flight") or ambiguous(row) or row.get("pending_approval")
                    or (row.get("local_execution") or {}).get("workspace_root")):
                raise AgentError("agent_invalid_transition")
            current = data["settings"].get("workspace_root", "")
            if current and Path(current).resolve() != Path(root):
                raise AgentError("agent_workspace_changed_new_task_required")
            if not current:
                data["settings"]["workspace_root"] = root
                data["settings_revision"] = data.get("settings_revision", 0) + 1
            row.setdefault("local_execution", {})["workspace_root"] = data["settings"]["workspace_root"]
            row["observed_files"] = {}
            close_queue(row, event, "agent_owner_changed")
            row.update(pending_input=None, approved=None, status="queued", error=None, result="", steps=0, elapsed_seconds=0.0)
            row.pop("verification", None)
            budget.new_round(row)
            row["messages"].append({"role": "user", "message_id": "owner_" + uuid.uuid4().hex,
                "content": "The owner selected this working directory in task settings: " + root
                    + ". Continue the original request. Existing write approvals still apply."})
            event(row, "workspace_bound", workspace_root=root)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(apply)

    def configuration_snapshot(self, *, display_only=False) -> dict:
        data = self.store.inspect(lambda state: {key: state[key] for key in
            ("settings", "settings_revision", "settings_context_budget_provenance") if key in state})
        settings = {**DEFAULT_SETTINGS, **data["settings"]}
        display_model = getattr(self.catalog, "display_model", None)
        if display_only and callable(display_model):
            policy = output_policy.context_budget(display_model(settings["model_id"]) or {},
                settings.get("max_context_chars"), provenance=data.get("settings_context_budget_provenance"))
        else:
            policy = self._context_policy({"model_id": settings["model_id"], "limits": settings,
                                           "context_budget_provenance": data.get("settings_context_budget_provenance")})
        return {"settings": settings, "settings_revision": data.get("settings_revision", 0), "context_budget_policy": policy}

    def update_settings(self, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS):
            raise AgentError("agent_invalid_settings")
        from .local_tools import validate_configuration
        validate_configuration(values)
        if "enabled" in values and type(values["enabled"]) is not bool:
            raise AgentError("agent_invalid_settings")
        if "verify_text_responses" in values and type(values["verify_text_responses"]) is not bool:
            raise AgentError("agent_invalid_settings")
        if "model_id" in values and (not isinstance(values["model_id"], str) or len(values["model_id"]) > 1024):
            raise AgentError("agent_invalid_settings")
        if "permission_mode" in values and values["permission_mode"] not in {"workspace", "read_only", "confirm_changes", "automatic"}:
            raise AgentError("agent_invalid_settings")
        if "allowed_write_tools" in values:
            allowed = values["allowed_write_tools"]
            names = {row["name"] for row in self.tool_catalog() if row["effect"] == "write"}
            if not isinstance(allowed, list) or any(not isinstance(name, str) or name not in names for name in allowed) or len(set(allowed)) != len(allowed):
                raise AgentError("agent_invalid_settings")
        if values.get("model_id"):
            selected_model = self._model_metadata(values["model_id"])
            if selected_model is None or not selected_model.get("available") or selected_model.get("agent_supported") is not True:
                raise AgentError("agent_model_unavailable")
        if ("max_context_chars" in values and values["max_context_chars"] is not None
                and (type(values["max_context_chars"]) is not int or not 4000 <= values["max_context_chars"] <= 2000000)):
            raise AgentError("agent_invalid_settings")
        for key, low, high in (("max_steps", 1, 128), ("max_seconds", 10, 3600), ("max_output_chars", 1000, 40000)):
            if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
                raise AgentError("agent_invalid_settings")

        def update(data):
            workspace_changed = "workspace_root" in values and values["workspace_root"] != data["settings"].get("workspace_root")
            desktop_changed = "desktop_access" in values and bool(values["desktop_access"]) != bool(data["settings"].get("desktop_access"))
            permission_changed = any(key in values and values[key] != data["settings"].get(key) for key in ("permission_mode", "allowed_write_tools", "workspace_root", "desktop_access"))
            changed = any(data["settings"].get(key) != value for key, value in values.items())
            if "max_context_chars" in values:
                provenance = "owner_auto" if values["max_context_chars"] is None else "owner_manual"
                changed = changed or data.get("settings_context_budget_provenance") != provenance
                data["settings_context_budget_provenance"] = provenance
            if "model_id" in values and data.get("default_model_destination"):
                data.pop("default_model_destination")
                changed = True  # Explicit owner reselection supersedes the old binding.
            if changed:
                data["settings_revision"] = data.get("settings_revision", 0) + 1
            data["settings"].update(values)
            if permission_changed:
                for row in data["tasks"].values():
                    if desktop_changed and not row.get("archived"):
                        row.setdefault("local_execution", {})["desktop_access"] = bool(data["settings"].get("desktop_access"))
                    if row["status"] not in TERMINAL:
                        row.update(approved=None, pending_approval=None)
                        if workspace_changed:
                            row["observed_files"] = {}
                        if row["status"] == "waiting_approval":
                            row["status"] = "queued"
                        if row.get("in_flight") and (row.get("pending_control") or {}).get("kind") not in {"pause", "stop", "cancel"}:
                            prior_control = row.get("pending_control") or {}
                            row["pending_control"] = {
                                **prior_control,
                                "kind": prior_control.get("kind", "permissions"),
                                "remain_paused": bool(prior_control.get("remain_paused")) or row["status"] != "running" or not data["settings"]["enabled"],
                            }
                        event(row, "permissions_changed")
                        if row.get("pending_input"):
                            row["pending_input"]["control_revision"] = row["control_revision"]
                        memory_integration.settle_in_tx(data, row, event)
            if values.get("enabled") is False:
                for row in data["tasks"].values():
                    if (row.get("pending_control") or {}).get("kind") == "edit_message":
                        row["pending_control"]["remain_paused"] = True
                        continue
                    if row["status"] in {"queued", "running", "waiting_approval", "waiting_input"} or ambiguous(row):
                        row["status"] = "interrupted" if ambiguous(row) else "paused"
                        row["pending_control"] = {"kind": "pause"}
                        event(row, "paused", reason="agent_disabled")
                        memory_integration.settle_in_tx(data, row, event)
            return {**data["settings"], "context_budget_policy": self._context_policy({
                "model_id": data["settings"].get("model_id", ""), "limits": {**DEFAULT_SETTINGS, **data["settings"]},
                "context_budget_provenance": data.get("settings_context_budget_provenance")})}
        return self.store.transaction(update)

    def models(self) -> list[dict]:
        return sanitized(getattr(self.catalog, "display_models", self.catalog.list_models)())

    def _model_metadata(self, model_id: str) -> dict | None:
        if hasattr(self.catalog, "get_model"):
            return sanitized(self.catalog.get_model(model_id))
        return next((row for row in self.models() if row.get("id") == model_id), None)

    def preview_model_switch(self, task_id: str, model_id: str) -> dict:
        return model_switch.preview(self, task_id, model_id)

    def switch_model(self, task_id: str, value: dict) -> dict:
        res = model_switch.change(self, task_id, value)
        def sync_after_switch(data):
            row = data.get("tasks", {}).get(task_id)
            if row is not None and (row.get("memory_disclosed_in_epoch") or row.get("memory_projection_watermark")):
                row["memory_conversation_epoch"] = int(row.get("memory_conversation_epoch", 0)) + 1
                row["memory_disclosed_in_epoch"] = False
        self.store.transaction(sync_after_switch)
        return res

    def skills(self) -> dict:
        if self.extensions is not None:
            return {"skills": self.extensions.store.enabled_skills_catalog()}
        return {"skills": knowledge.catalog()}

    def _resolve_skills(
        self,
        explicit_ids: Any,
        message_text_val: str = "",
        *,
        default_project: bool = False,
    ) -> list[dict]:
        selector = self.extensions.store.select_skills if self.extensions is not None else knowledge.select
        enabled_catalog = (
            self.extensions.store.enabled_skills_catalog()
            if self.extensions is not None
            else knowledge.catalog()
        )
        enabled_by_lower = {
            str(item["id"]).lower(): str(item["id"])
            for item in enabled_catalog
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        selected = selector(explicit_ids)
        seen_ids = {str(item.get("id")) for item in selected if isinstance(item, dict)}

        if isinstance(message_text_val, str) and "$" in message_text_val:
            mentioned_ids: list[str] = []
            for match in re.finditer(
                r"(?<![A-Za-z0-9_-])\$([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![A-Za-z0-9_-])",
                message_text_val,
            ):
                canonical_id = enabled_by_lower.get(match.group(1).lower())
                if canonical_id and canonical_id not in seen_ids and canonical_id not in mentioned_ids:
                    mentioned_ids.append(canonical_id)
            if mentioned_ids:
                for item in selector(mentioned_ids):
                    if item["id"] not in seen_ids:
                        selected.append(item)
                        seen_ids.add(item["id"])

        return selected

    @staticmethod
    def _merge_task_skills(existing: list[dict] | None, additions: list[dict] | None) -> list[dict]:
        merged = [copy.deepcopy(item) for item in (existing or []) if isinstance(item, dict)]
        seen = {str(item.get("id")) for item in merged if item.get("id")}
        for item in additions or []:
            if isinstance(item, dict) and item.get("id") and str(item["id"]) not in seen:
                if len(merged) >= 8:
                    break
                merged.append(copy.deepcopy(item))
                seen.add(str(item["id"]))
        return merged

    def preferences(self) -> dict:
        return knowledge.preferences(self.store.read())

    def update_preferences(self, value: dict) -> dict:
        return self.store.transaction(lambda data: knowledge.update_preferences(data, value))

    def memory_snapshot(
        self,
        scope_kind: str | None = None,
        project_id: str | None = None,
        status: str = "active",
    ) -> dict:
        return memory_integration.get_memory_snapshot(
            self,
            scope_kind=scope_kind,
            project_id=project_id,
            status=status,
        )

    def memory_destinations(self) -> dict:
        return memory_integration.list_memory_destinations(self)

    def update_memory_settings(self, value: dict) -> dict:
        return memory_integration.update_owner_memory_settings(self, value)

    def create_memory_entry(self, value: dict) -> dict:
        return memory_integration.create_owner_memory_entry(self, value)

    def update_memory_entry(self, entry_id: str, value: dict) -> dict:
        return memory_integration.update_owner_memory_entry(self, entry_id, value)

    def delete_memory_entry(self, entry_id: str, value: dict) -> dict:
        return memory_integration.delete_owner_memory_entry(self, entry_id, value)

    def decide_memory_candidate(self, candidate_id: str, value: dict) -> dict:
        return memory_integration.decide_owner_memory_candidate(self, candidate_id, value)

    def search_memory(
        self,
        query: str = "",
        scope_kind: str | None = None,
        project_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
    ) -> dict:
        return memory_integration.search_owner_memories(
            self,
            query=query,
            scope_kind=scope_kind,
            project_id=project_id,
            category=category,
            limit=limit,
        )

    def reset_memory(self, value: dict) -> dict:
        return memory_integration.reset_owner_memory(self, value)

    def list_automations(self) -> dict:
        return self.store.transaction(lambda data: automations.list_automations_in_tx(self.store, data))

    def create_automation(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise AgentError("agent_invalid_request")
        item = self.store.transaction(lambda data: automations.create_or_update_automation_in_tx(self.store, data, value))
        return {"automation": item}

    def update_automation(self, automation_id: str, value: dict) -> dict:
        if not isinstance(value, dict) or not str(automation_id or "").strip():
            raise AgentError("agent_invalid_request")
        def _upd(data):
            automations.ensure_automation_state(data)
            if automation_id not in data["automations"]:
                raise AgentError("agent_automation_not_found")
            return automations.create_or_update_automation_in_tx(self.store, data, value, automation_id=automation_id)
        item = self.store.transaction(_upd)
        return {"automation": item}

    def delete_automation(self, automation_id: str) -> dict:
        if not str(automation_id or "").strip():
            raise AgentError("agent_invalid_request")
        deleted = self.store.transaction(lambda data: automations.delete_automation_in_tx(self.store, data, automation_id))
        if not deleted:
            raise AgentError("agent_automation_not_found")
        return {"deleted": True, "id": automation_id}

    def run_automation_now(self, automation_id: str) -> dict:
        if not str(automation_id or "").strip():
            raise AgentError("agent_invalid_request")
        outcome = self.store.transaction(
            lambda data: automation_worker.trigger_automation_in_tx(self, data, automation_id, force=True)
        )
        if outcome.get("task_id"):
            self._queued_since[str(outcome["task_id"])] = time.monotonic()
        return outcome

    def source_credential(self, task_id: str, value: dict, revoke=False) -> dict:
        return credential_control.change(self, task_id, value, revoke=revoke)

    def update_budget(self, task_id: str, value: dict) -> dict:
        if set(value) - {"expected_control_revision", "client_request_id", "limits", "run_max_seconds"}:
            raise AgentError("agent_invalid_request")
        requested = value.get("limits")
        budget.validate_limits(requested)
        run_max = value.get("run_max_seconds")
        if "run_max_seconds" in value and (type(run_max) is not int or not 10 <= run_max <= 3600):
            raise AgentError("agent_invalid_budget")
        def update(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            if not allowed_actions(row, data["settings"])["budget"]["enabled"]:
                raise AgentError("agent_invalid_transition")
            state = budget.initialize(row)
            state["limits"].update(requested)
            if run_max is not None:
                row["limits"]["max_seconds"] = run_max
            event(row, "budget_changed", limits=state["limits"], run_max_seconds=row["limits"]["max_seconds"])
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(update)

    def tool_catalog(self) -> list[dict]:
        return [sanitized({"name": spec["function"]["name"], **self.tools.metadata(spec["function"]["name"])}) for spec in self.tools.specs()]

    def list_tasks(self, archived: bool = False) -> list[dict]:
        if archived:
            data = self.store.read()
            rows = [lifecycle.find(self.store, data, task_id) for task_id in data.get("archives", {})]
            stamp = time.monotonic()
            return [public_task(row, summary=True, settings=data["settings"], active_run=self._task_run(row["id"]), monotonic_now=stamp)
                    for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)]

        def view(data):
            # Summaries never include transcripts, so nothing else is copied.
            stamp = time.monotonic()
            return [public_task(detached(row, summary=True), summary=True, settings=data["settings"],
                                active_run=self._task_run(row["id"]), monotonic_now=stamp)
                    for row in sorted(data["tasks"].values(), key=lambda row: row["created_at"], reverse=True)]
        return self.store.inspect(view)

    def get(self, task_id: str, since_revision: int | None = None) -> dict:
        def view(data):
            row = data["tasks"].get(task_id)
            if row is None:
                return None
            current = public_task(detached(row, summary=since_revision == row["revision"]), settings=data["settings"],
                                  active_run=self._task_run(task_id), monotonic_now=time.monotonic(),
                                  summary=since_revision == row["revision"])
            current["streaming"] = self._stream_snapshot(row)
            if since_revision != row["revision"]:
                return current
            # Unchanged since the caller's copy: only fields that move on their own.
            return {"id": task_id, "revision": row["revision"], "unchanged": True,
                    **{key: current[key] for key in ("run_timing", "budget", "allowed_actions", "run_state", "streaming") if key in current}}
        found = self.store.inspect(view)
        if found is not None:
            return found
        data = self.store.read()
        return public_task(lifecycle.find(self.store, data, task_id), settings=data["settings"],
                           active_run=self._task_run(task_id), monotonic_now=time.monotonic())

    def lifecycle(self, task_id: str, value: dict, operation: str) -> dict:
        with credential_control.gate(self.store, task_id):
            return self._lifecycle(task_id, value, operation)

    def rename_task(self, task_id: str, value: dict) -> dict:
        if set(value) - {"title", "expected_control_revision", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        title = value.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 160 or any(ord(char) < 32 for char in title):
            raise AgentError("agent_invalid_title")
        keyed = {**value, "metadata_action": "rename"}

        def rename(data):
            row = lifecycle.find(self.store, data, task_id)
            if request_replayed(row, keyed):
                return public_task(row, settings=data["settings"])
            check_version(row, value)
            row["title"] = title.strip()
            # A display label changes the poll revision, never execution authority.
            # Advancing control_revision here would discard an active model result
            # or invalidate the owner's pending tool approval.
            event(row, "title_changed")
            remember_request(row, keyed)
            if row.get("archived"):
                lifecycle.archive_store(self.store, task_id).transaction(lambda archive: archive["tasks"].update({task_id: row}))
                data["archives"][task_id]["updated_at"] = row["updated_at"]
            return public_task(row, settings=data["settings"])
        return self.store.transaction(rename)

    def _lifecycle(self, task_id: str, value: dict, operation: str) -> dict:
        result = lifecycle.change(self.store, task_id, value, operation, event, now,
                                  on_mutate=memory_integration.on_task_lifecycle_in_tx)
        if result.get("deleted"):
            pending = True
            try:
                results.purge(self.store, task_id)
                lifecycle.purge_archive(self.store, task_id)
                self.store.transaction(lambda _data: self.store.purge_orphan_snapshots())
                if hasattr(self.tools, "forget_task"):
                    self.tools.forget_task(task_id, operation_ids=self.store.read()["deleted_tasks"][task_id].get("operation_ids", []))
                pending = False
            except Exception:
                pass  # Tombstone wins; the same DELETE safely retries cleanup.
            self.store.transaction(lambda data: data["deleted_tasks"][task_id].update(cleanup_pending=pending))
            result["cleanup_pending"] = pending
        return result if result.get("deleted") else public_task(result, settings=self.settings())

    def get_result(self, task_id: str, result_id: str, offset=0, limit=8192) -> dict:
        row = lifecycle.find(self.store, self.store.read(), task_id)
        return results.page(self.store, row, result_id, offset, limit)

    def _file_artifact_row(self, task_id: str) -> dict:
        from . import workspace_artifacts
        collector = getattr(self.tools, "collect_command_artifacts", None)
        pending = bool(collector(task_id)) if collector else False
        row = lifecycle.find(self.store, self.store.read(), task_id)
        for receipt in row.get("receipts", []):
            tool_name = str(receipt.get("tool") or "")
            if tool_name not in {"ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_file_read",
                                 "ryn_render_inspect", "ryn_shell_execute", "ryn_process_read", "ryn_process_stop"} and not tool_name.startswith("mcp__"):
                continue
            result = receipt.get("result")
            if isinstance(result, dict) and result.get("result_ref"):
                try:
                    receipt["result"], _ = results.read(self.store, row, result["result_ref"])
                except AgentError:
                    pass
        referenced = {state["id"] for state in workspace_artifacts.history(row).values() if state["source"] == "read"}
        if referenced - set(row.get("referenced_file_ids", [])):
            def remember(data):
                current = lifecycle.find(self.store, data, task_id)
                current["referenced_file_ids"] = sorted(set(current.get("referenced_file_ids", [])) | referenced)
            self.store.transaction(remember)
            row["referenced_file_ids"] = sorted(set(row.get("referenced_file_ids", [])) | referenced)
        row["pending_file_commands"] = pending
        return row

    def artifacts(self, task_id: str) -> dict:
        from . import artifacts, workspace_artifacts
        row = self._file_artifact_row(task_id)
        return {**artifacts.listing(self, row), **workspace_artifacts.listing(row), "pending_files": row["pending_file_commands"]}

    def read_file_artifact(self, task_id: str, file_id: str) -> dict:
        from . import workspace_artifacts
        row = self._file_artifact_row(task_id)
        return workspace_artifacts.read(row, file_id)

    def open_file_artifact(self, task_id: str, file_id: str, action: str) -> dict:
        from . import workspace_artifacts
        return workspace_artifacts.open_file(self._file_artifact_row(task_id), file_id, action)

    def read_artifact(self, task_id: str, operation_id: str) -> dict:
        from . import artifacts
        row = lifecycle.find(self.store, self.store.read(), task_id)
        return artifacts.read(self, row, operation_id)

    def events(self, task_id: str, after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise AgentError("agent_invalid_cursor")
        row = lifecycle.find(self.store, self.store.read(), task_id)
        return {"events": [item for item in row["events"] if item["seq"] > after], "cursor": row["event_cursor"], "status": row["status"], "revision": row["revision"], "control_revision": row["control_revision"]}

    def create(self, value: dict) -> dict:
        if set(value) - {"message", "model_id", "title", "client_request_id", "scope", "expected_destination_revision", "skill_ids", "budget_limits", "response_language", "project_id", "reasoning_effort", "attachments"}:
            raise AgentError("agent_invalid_request")
        if value.get("response_language") not in {None, "zh-CN", "en"}:
            raise AgentError("agent_invalid_request")
        prepared = parse_attachments(value.get("attachments"))
        content = message_text(value.get("message") or ("Please review the attached files." if prepared else ""))
        skills = self._resolve_skills(value.get("skill_ids"), content)
        budget_limits = budget.validate_limits(value.get("budget_limits", {}))
        request_id = value.get("client_request_id")
        if request_id is not None:
            try:
                if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                    raise ValueError
            except ValueError:
                raise AgentError("agent_invalid_request") from None
        fingerprint = digest(value)
        if request_id:
            current = self.store.read()
            existing = lifecycle.duplicate(self.store, current, request_id, fingerprint)
            if existing is not None:
                return public_task(existing, settings=current["settings"])
        configuration = self.store.read()
        uses_default = not value.get("model_id")
        model_id = value.get("model_id") or configuration["settings"]["model_id"]
        if not isinstance(model_id, str) or not model_id:
            raise AgentError("agent_model_required")
        selected_model = self._model_metadata(model_id)
        if selected_model is None or not selected_model.get("available") or selected_model.get("agent_supported") is not True:
            raise AgentError("agent_model_unavailable")
        effort = reasoning.validate(selected_model, value["reasoning_effort"]) if "reasoning_effort" in value else None
        peer_destination = selected_model.get("source") == "peer"
        project_destination = value.get("project_id") is not None
        destination = self._bound_destination(selected_model) if peer_destination or project_destination else self._destination(selected_model)
        # The UI sends its selected id explicitly even when it uses the saved
        # default. Preserve that approval's destination for both entry paths.
        default_binding = configuration.get("default_model_destination") if model_id == configuration["settings"]["model_id"] else None
        if default_binding and self._bound_destination(selected_model) != default_binding:
            raise AgentError("agent_model_destination_changed")
        scope = self._prepare_scope(value["scope"], destination, value.get("expected_destination_revision")) if "scope" in value else None
        title = value.get("title", content[:80].replace("\n", " "))
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise AgentError("agent_invalid_title")

        def create(data):
            if request_id:
                existing = lifecycle.duplicate(self.store, data, request_id, fingerprint)
                if existing is not None:
                    return public_task(existing, settings=data["settings"])
            settings = {**DEFAULT_SETTINGS, **data["settings"]}
            if not settings["enabled"]:
                raise AgentError("agent_disabled")
            if (uses_default or default_binding) and data.get("settings_revision", 0) != configuration.get("settings_revision", 0):
                raise AgentError("agent_settings_revision_conflict")
            if len(data["tasks"]) >= 200 or sum(row["status"] not in TERMINAL for row in data["tasks"].values()) >= 16:
                raise AgentError("agent_task_limit")
            row = {"id": "agent_" + uuid.uuid4().hex, "client_request_id": request_id,
                   "request_fingerprint": fingerprint, "title": title.strip(), "model_id": model_id,
                   "status": "queued", "revision": 0, "created_at": now(), "updated_at": now(),
                   "messages": [{"role": "user", "content": content}], "events": [], "pending_approval": None,
                   "steps": 0, "result": "", "error": None,
                   "limits": {key: settings[key] for key in DEFAULT_SETTINGS if key.startswith("max_")},
                   "permission_mode": settings["permission_mode"], "allowed_write_tools": list(settings["allowed_write_tools"]), "queue": [], "in_flight": None,
                   "approved": None, "elapsed_seconds": 0.0}
            # New tasks pin the Ryn base for restart/model-switch continuity.
            # Legacy sessions stay readable and use the documented fallback.
            row["base_instructions"] = select_base_instructions()
            row["context_budget_provenance"] = data.get("settings_context_budget_provenance", "default_auto" if settings["max_context_chars"] is None else "legacy_ambiguous_manual")
            initialize(row)
            if effort is not None:
                row["reasoning_effort"] = effort
            row["local_execution"] = {"workspace_root": settings.get("workspace_root") or self.store.default_workspace(), "desktop_access": settings.get("desktop_access", False)}
            if value.get("project_id") is not None:
                row["project"] = copy.deepcopy(projects.selected(data, value["project_id"], refresh=True))
                row["local_execution"].update(workspace_root=row["project"]["path"],
                                              project_id=row["project"]["id"], project_generation=row["project"]["generation"])
            row["skills"] = skills
            row["verify_text_responses"] = settings.get("verify_text_responses", False)
            row["response_language"] = value.get("response_language") or "en"
            budget.initialize(row)["limits"] = budget_limits
            row["model_destination"] = copy.deepcopy(destination)
            if peer_destination or project_destination:
                # A dispatch constraint, not resource/egress authorization or a
                # claim that the owner approved a business mutation.
                row["model_destination_required"] = True
            if default_binding:
                row["model_destination_confirmed"] = True
            if scope is not None:
                row["scope"] = {**copy.deepcopy(scope), "revision": row["scope_revision"], "selection": copy.deepcopy(value["scope"])}
            if prepared:
                attach_to_message(self.store, row, row["messages"][0], prepared)
                row["goal"]["text"] = row["messages"][0]["content"]
                row["goal"]["constraints"][0]["text"] = row["messages"][0]["content"]
                (future_specs, _), authority_bytes = self._menu(row)
                if authority_bytes + len(encoded(future_specs)) > self._context_limit(row):
                    raise AgentError("agent_context_limit")
            row["prompt_version"] = PROMPT_VERSION
            memory_natural.stamp_task_owner_messages_in_tx(data, row)
            memory_integration.apply_owner_turn_memory_in_tx(self, data, row)
            event(row, "created")
            data["tasks"][row["id"]] = row
            return public_task(row, settings=settings)
        created = self.store.transaction(create)
        if created.get("status") == "queued":
            self._queued_since[created["id"]] = time.monotonic()
        return created

    def add_message(self, task_id: str, value: dict) -> dict:
        if set(value) - {"message", "intent", "expected_revision", "expected_control_revision", "expected_goal_revision", "client_request_id", "reasoning_effort", "edit_message_index", "attachments", "skill_ids"}:
            raise AgentError("agent_invalid_request")
        prepared = parse_attachments(value.get("attachments"))
        content = message_text(value.get("message") or ("Please review the attached files." if prepared else ""))
        added_skills = self._resolve_skills(value.get("skill_ids"), content)
        if "edit_message_index" in value:
            return self._edit_message(task_id, value, content, prepared, added_skills=added_skills)
        intent = value.get("intent")
        if intent not in {None, "steer", "information"}:
            raise AgentError("agent_invalid_request")

        def add(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value, goal=intent is not None)
            if not data["settings"]["enabled"]:
                raise AgentError("agent_disabled")
            if intent is None and (row["status"] not in {"completed", "failed", "paused"} or row["in_flight"] or row["queue"] or row["pending_approval"] or row["pending_input"]):
                raise AgentError("agent_task_busy")
            if intent is not None and not allowed_actions(row, data["settings"])[intent]["enabled"]:
                raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_invalid_transition")
            if len(row["events"]) >= 1000:
                raise AgentError("agent_task_limit")
            effort = reasoning.validate(self._model_metadata(row["model_id"]), value["reasoning_effort"]) if "reasoning_effort" in value else None
            # Recover old, unbound conversations when the owner continues them.
            # Never relocate an existing workspace or change an in-flight grant.
            if (not (row.get("local_execution") or {}).get("workspace_root")
                    and not data["settings"].get("workspace_root")
                    and not row.get("in_flight") and not ambiguous(row)):
                row.setdefault("local_execution", {})["workspace_root"] = self.store.default_workspace()
            # Original evidence survives compaction, so its archive size is not
            # the next prompt size. Only immutable owner constraints form a hard
            # context floor; the normal loop may compact the remaining history.
            message = {"role": "user", "content": content, "message_id": "owner_" + uuid.uuid4().hex,
                       "ui": {"kind": "steer" if intent == "steer" else "information" if intent == "information" else "message", "text": content}}
            attach_to_message(self.store, row, message, prepared)
            merged_skills = self._merge_task_skills(row.get("skills"), added_skills)
            owners = [message for message in [*row["messages"], *row.get("pending_user_messages", [])] if message["role"] == "user"]
            prospective = {**row, "messages": [*owners, message], "skills": merged_skills,
                           "context_checkpoint": None, "tool_menu": None}
            (future_specs, _), authority_bytes = self._menu(prospective)
            if authority_bytes + len(encoded(future_specs)) > self._context_limit(row):
                raise AgentError("agent_context_limit")
            if len(encoded(row)) + len(encoded(message)) > 2 * 1024 * 1024:
                raise AgentError("agent_task_limit")
            memory_natural.stamp_single_owner_message_in_tx(data, message)
            row["skills"] = merged_skills
            ended = row["status"] in {"completed", "failed"} or bool(row.get("turn_stopped"))
            if effort is not None:
                # Running requests already own an immutable task snapshot.
                # This preference starts with the next admitted request.
                row["reasoning_effort"] = effort
            budget.new_round(row)
            if intent != "information" or ended:
                owner_update(row, message)
            row.update(approved=None, pending_approval=None, pending_input=None, result="",
                       latest_turn_is_silent_heartbeat=False, automation_run_id=None)
            row.setdefault("pending_user_messages", []).append(message)
            was_stopped = row.pop("turn_stopped", False)
            row["pending_control"] = {"kind": "steer", "remain_paused": row["status"] == "paused" and intent is not None and not was_stopped}
            if ended or intent is None or was_stopped:
                row.update(steps=0, elapsed_seconds=0.0)
            event(row, "goal_changed" if intent == "steer" else "information_added" if intent else "message_added", goal_revision=row["goal"]["revision"])
            close_queue(row, event, "agent_owner_changed")
            settle(row, event, data=data)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(add)

    def _edit_message(self, task_id: str, value: dict, content: str, prepared=None, *, added_skills=None) -> dict:
        if value.get("intent") not in {None, "steer", "information"} or type(value["edit_message_index"]) is not int:
            raise AgentError("agent_invalid_request")

        def edit(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            check_version(row, value, goal=value.get("intent") is not None)
            if not data["settings"]["enabled"]:
                raise AgentError("agent_disabled")
            if not allowed_actions(row, data["settings"])["edit_message"]["enabled"]:
                raise AgentError("agent_operation_unresolved" if ambiguous(row) else "agent_invalid_transition")
            index = value["edit_message_index"]
            if index < 0 or index >= len(row["messages"]) or row["messages"][index].get("role") != "user":
                raise AgentError("agent_invalid_message_index")
            if len(row["events"]) >= 998 or len(encoded(row)) + len(content.encode()) > 2 * 1024 * 1024:
                raise AgentError("agent_task_limit")
            replacement = {"role": "user", "content": content, "message_id": "owner_" + uuid.uuid4().hex,
                           "ui": {"kind": "message", "text": content}}
            if prepared is None:
                replacement["attachments"] = copy.deepcopy(row["messages"][index].get("attachments", []))
            attach_to_message(self.store, row, replacement, prepared)
            if len(encoded(row)) + len(encoded(replacement)) > 2 * 1024 * 1024:
                raise AgentError("agent_task_limit")
            # Test the replacement owner's pinned floor before committing stop
            # intent. Discarded future constraints cannot consume its context.
            merged_skills = self._merge_task_skills(row.get("skills"), added_skills)
            retained = [*row["messages"][:index], replacement]
            owner_ids = {message.get("message_id") for message in retained if message["role"] == "user"}
            prospective = {**row, "messages": retained, "skills": merged_skills, "context_checkpoint": None, "tool_menu": None,
                           "goal": {**row["goal"], "constraints": [item for item in row["goal"]["constraints"] if item.get("message_id") in owner_ids]}}
            (future_specs, _), authority_bytes = self._menu(prospective)
            if authority_bytes + len(encoded(future_specs)) > self._context_limit(row):
                raise AgentError("agent_context_limit")
            memory_natural.stamp_single_owner_message_in_tx(data, replacement)
            row["skills"] = merged_skills
            if "reasoning_effort" in value:
                row["reasoning_effort"] = reasoning.validate(self._model_metadata(row["model_id"]), value["reasoning_effort"])
            row.update(status="paused", approved=None, pending_approval=None, pending_input=None,
                       pending_control={"kind": "edit_message", "message_index": index, "message": replacement})
            event(row, "message_edit_requested", message_index=index, operation_in_flight=bool(row["in_flight"]))
            close_queue(row, event, "agent_owner_changed")
            settle(row, event, data=data)
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(edit)

    def action(self, task_id: str, value: dict) -> dict:
        if set(value) - {"action", "expected_revision", "expected_control_revision", "expected_goal_revision", "approval_id", "pending_input_id", "answers", "client_request_id"}:
            raise AgentError("agent_invalid_request")
        action = value.get("action")

        def apply(data):
            row = task(data, task_id)
            if request_replayed(row, value):
                return public_task(row, settings=data["settings"])
            if action not in {"pause", "stop", "cancel"}:
                check_version(row, value, goal=action == "answer")
            if action == "cancel":
                if row["status"] not in {"completed", "failed", "cancelled"}:
                    row.update(status="cancelled", pending_approval=None, approved=None, pending_input=None, pending_control={"kind": "cancel"})
                    event(row, "cancelled", operation_in_flight=bool(row["in_flight"]))
                    close_queue(row, event, "agent_cancelled")
                    settle(row, event, data=data)
            elif action == "stop":
                if ambiguous(row):
                    row["pending_control"] = {"kind": "stop"}
                    event(row, "paused", operation_in_flight=True, reason="owner_stopped")
                elif row["status"] in {"queued", "running", "waiting_approval", "waiting_input", "paused"}:
                    # Capture the accepted text already shown in the composer
                    # before cancelling transport; a late response is discarded.
                    stream = self._stream_snapshot(row)
                    if stream:
                        row["messages"].append({"role": "assistant", "content": stream["text"],
                                                "context_epoch": row.get("context_epoch", 0), "interrupted": True})
                    row.update(status="paused", pending_approval=None, approved=None, pending_input=None,
                               pending_control={"kind": "stop"}, turn_stopped=True)
                    event(row, "paused", operation_in_flight=bool(row["in_flight"]), reason="owner_stopped")
                    close_queue(row, event, "agent_cancelled")
                    settle(row, event, data=data)
            elif action == "pause":
                row.pop("turn_stopped", None)
                if ambiguous(row):
                    # Stop intent survives reconciliation, even while the write's
                    # outcome is unknown. Pausing never clears that marker.
                    row["pending_control"] = {"kind": "pause"}
                    event(row, "paused", operation_in_flight=True)
                    remember_request(row, value)
                    return public_task(row, settings=data["settings"])
                if row["status"] in {"paused", "completed", "failed", "cancelled", "interrupted"}:
                    remember_request(row, value)
                    return public_task(row, settings=data["settings"])
                if row["status"] not in {"queued", "running", "waiting_approval", "waiting_input"}:
                    raise AgentError("agent_invalid_transition")
                row["status"] = "paused"
                row["pending_control"] = {"kind": "pause"}
                event(row, "paused", operation_in_flight=bool(row["in_flight"]))
                settle(row, event, data=data)
            elif action == "resume":
                if row["status"] not in {"paused", "interrupted"}:
                    raise AgentError("agent_invalid_transition")
                if not data["settings"]["enabled"]:
                    raise AgentError("agent_disabled")
                row.pop("turn_stopped", None)
                if row["in_flight"]:
                    if not recoverable_read_in_flight(row) or ambiguous(row):
                        raise AgentError("agent_operation_unresolved")
                    flight = row["in_flight"]
                    err_code = row.get("error") or "agent_read_interrupted"
                    cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=err_code)
                    if not cleanup_ok:
                        raise AgentError("agent_operation_unresolved")
                    if flight.get("effect") == "write":
                        self._settle_interrupted_read(row, flight, err_code, recovery=recovery)
                    row["in_flight"] = None
                    row["approved"] = None
                    settle(row, event, data=data)
                row["status"] = "waiting_input" if row["pending_input"] else "waiting_approval" if row["pending_approval"] else "queued"
                row["error"] = None
                event(row, "resumed")
                if row["pending_approval"]:
                    row["pending_approval"]["control_revision"] = row["control_revision"]
                if row["pending_input"]:
                    row["pending_input"]["control_revision"] = row["control_revision"]
            elif action in {"approve", "deny"}:
                approval = row["pending_approval"]
                if row["status"] != "waiting_approval" or not approval or value.get("approval_id") != approval["id"]:
                    raise AgentError("agent_approval_mismatch")
                call = row["queue"][0] if row["queue"] else None
                if not call or approval["arguments_sha256"] != digest(call["arguments"]) or call["call_id"] != approval["call_id"] or call["name"] != approval["tool"]:
                    raise AgentError("agent_approval_mismatch")
                if approval.get("goal_revision") != row["goal"]["revision"] or approval.get("scope_revision") != row["scope_revision"] or approval.get("control_revision") != row["control_revision"]:
                    raise AgentError("agent_approval_mismatch")
                if not data["settings"]["enabled"]:
                    raise AgentError("agent_disabled")
                if action == "approve":
                    if "read_only" in {row["permission_mode"], data["settings"]["permission_mode"]}:
                        raise AgentError("agent_permission_denied")
                    row["approved"] = {**approval, "revision": row["revision"]}
                    event(row, "approved", approval_id=approval["id"], tool=approval["tool"])
                else:
                    denied = {"error": "owner_denied_operation", "not_executed": True, "no_mutation": True}
                    row["messages"].append({"role": "tool", "tool_call_id": call["call_id"], "content": encoded(denied).decode(), "context_epoch": call.get("context_epoch", 0)})
                    verification.record(row, call, "not_executed", now(), result=denied)
                    row["queue"].pop(0)
                    event(row, "denied", approval_id=approval["id"], tool=approval["tool"])
                row.update(pending_approval=None, status="queued")
            elif action == "answer":
                pending = row.get("pending_input")
                if row["status"] != "waiting_input" or not pending or value.get("pending_input_id") != pending["id"]:
                    raise AgentError("agent_input_mismatch")
                if not data["settings"]["enabled"]:
                    raise AgentError("agent_disabled")
                if pending["goal_revision"] != row["goal"]["revision"] or pending["scope_revision"] != row["scope_revision"] or pending["control_revision"] != row["control_revision"]:
                    raise AgentError("agent_input_mismatch")
                if pending["kind"] == "credential_ref":
                    raise AgentError("agent_secure_credential_form_required")
                answers = value.get("answers")
                fields = {field["id"]: field for field in pending["fields"]}
                if not isinstance(answers, dict) or set(answers) - set(fields):
                    raise AgentError("agent_invalid_answer")
                for key, field in fields.items():
                    answer = answers.get(key)
                    if answer is None and not field["required"]:
                        continue
                    if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000 or (field["type"] == "choice" and answer not in {o["value"] for o in field["options"]}):
                        raise AgentError("agent_invalid_answer")
                message = {"role": "user", "message_id": "owner_" + uuid.uuid4().hex,
                           "content": "Owner supplied information (not new permission): " + encoded(answers).decode(),
                           "ui": {"kind": "input_answer", "prompt": pending["prompt"], "pending_input_id": pending["id"],
                                  "fields": [{"label": fields[key]["label"], "value": next((option["label"] for option in fields[key].get("options", []) if option["value"] == answer), answer)}
                                             for key, answer in answers.items()]}}
                memory_natural.stamp_single_owner_message_in_tx(data, message)
                row["messages"].append(message)
                budget.new_round(row)
                row.update(pending_input=None, status="queued")
                event(row, "input_answered", pending_input_id=pending["id"])
            elif action == "reconcile":
                if not allowed_actions(row, data["settings"])["reconcile"]["enabled"] or not hasattr(self.tools, "reconcile"):
                    raise AgentError("agent_invalid_transition")
                if recoverable_read_in_flight(row) and not ambiguous(row):
                    flight = row["in_flight"]
                    err_code = row.get("error") or "agent_read_interrupted"
                    attempts = row.get("reconciliation", {}).get("attempts", 0) + 1
                    cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=err_code)
                    if not cleanup_ok:
                        row["reconciliation"] = {
                            "state": "unknown",
                            "attempts": attempts,
                            "operation_id": flight.get("operation_id"),
                            "reason": "agent_operation_unresolved",
                            "checked_at": now(),
                        }
                        event(row, "reconciliation_finished", outcome="unknown", operation_id=flight.get("operation_id"), continued=False)
                    else:
                        outcome = "not_executed"
                        if flight.get("effect") == "write":
                            outcome = self._settle_interrupted_read(row, flight, err_code, recovery=recovery)
                        row["in_flight"] = None
                        row["approved"] = None
                        settle(row, event, data=data)
                        may_continue = self._read_may_continue_after_restart(row, flight, data)
                        row["reconciliation"] = {
                            "state": "executed" if outcome == "confirmed" else "not_executed",
                            "attempts": attempts,
                            "operation_id": flight.get("operation_id"),
                            "checked_at": now(),
                        }
                        row.update(status="queued" if may_continue else "paused", error=None)
                        event(row, "reconciliation_finished", outcome=row["reconciliation"]["state"], operation_id=flight.get("operation_id"), continued=may_continue)
                else:
                    row["reconciliation"] = {"state": "pending", "attempts": row.get("reconciliation", {}).get("attempts", 0)}
                    event(row, "reconciliation_requested")
            else:
                raise AgentError("agent_invalid_action")
            remember_request(row, value)
            return public_task(row, settings=data["settings"])
        return self.store.transaction(apply)

    def _recover_read_operation_details(
        self, row: dict, flight: dict | None, *, error: str = "agent_read_interrupted"
    ) -> tuple[bool, dict]:
        from .execution_policy import WORKSPACE_TOOLS
        recover = getattr(self.tools, "recover_operation", None)
        if not callable(recover) and getattr(self.tools, "local", None) is not None:
            recover = getattr(self.tools.local, "recover_operation", None)
        if not callable(recover):
            ok = (flight or {}).get("name") not in WORKSPACE_TOOLS
            return ok, (
                {
                    "ok": True,
                    "ledger_outcome": "failed",
                    "settled_result": {"error": error, "not_executed": False, "no_mutation": True},
                }
                if ok
                else {"ok": False}
            )
        try:
            stopped = recover(
                row["id"],
                (flight or {}).get("operation_id"),
                error=error,
                operation_classification=(flight or {}).get("operation_classification"),
            )
        except Exception:
            return False, {"ok": False}
        ok = (
            isinstance(stopped, dict)
            and stopped.get("still_running") is False
            and not stopped.get("cleanup_error")
            and not stopped.get("process_ids")
            and stopped.get("ok", True) is not False
            and stopped.get("ledger_settled", True) is not False
        )
        return ok, (stopped if isinstance(stopped, dict) else {"ok": False})

    def _recover_read_operation(self, row: dict, flight: dict | None, *, error: str = "agent_read_interrupted") -> bool:
        ok, _ = self._recover_read_operation_details(row, flight, error=error)
        return ok

    def _settle_interrupted_read(
        self, row: dict, flight: dict, error_code: str, *, recovery: dict | None = None
    ) -> str:
        call_id = flight.get("call_id")
        outcome = (recovery or {}).get("ledger_outcome")
        if outcome not in {"confirmed", "not_executed", "failed"}:
            outcome = "failed"
        if not call_id:
            return outcome
        raw_result = (recovery or {}).get("settled_result")
        if isinstance(raw_result, dict):
            result = sanitized(copy.deepcopy(raw_result))
        elif outcome == "not_executed":
            result = {"error": error_code, "not_executed": True, "no_mutation": True}
        else:
            result = {"error": error_code, "not_executed": False, "no_mutation": True}
        epoch = flight.get("context_epoch", row.get("context_epoch", 0))
        if outcome == "confirmed":
            result_size = len(encoded(result))
            if result_size > row["limits"]["max_output_chars"]:
                reference = results.save(self.store, row, flight, result)
                row.setdefault("result_refs", []).append(reference)
                result = {
                    "result_ref": reference["id"],
                    "output_sha256": reference["sha256"],
                    "bytes": result_size,
                    "note": "Full output is encrypted locally. Use ryn_read_result for bounded pages; this reference is not proof of the requested outcome.",
                }
        row["messages"].append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": encoded(result).decode(),
            "context_epoch": epoch,
        })
        verification.record(row, flight, outcome, now(), result=result, flight=flight)
        row["queue"] = [call for call in row.get("queue", []) if call.get("call_id") != call_id]
        event(
            row,
            "tool_result",
            tool=flight.get("name"),
            call_id=call_id,
            operation_id=flight.get("operation_id"),
            result=result,
            context_epoch=epoch,
        )
        return outcome

    def _read_may_continue_after_restart(self, row: dict, flight: dict, data: dict) -> bool:
        if (
            not data["settings"].get("enabled")
            or row.get("pending_control")
            or row.get("pending_input")
            or row.get("pending_approval")
            or flight.get("control_revision") != row["control_revision"]
            or flight.get("goal_revision") != row["goal"]["revision"]
            or flight.get("scope_revision") != row["scope_revision"]
            or flight.get("context_epoch", 0) != row.get("context_epoch", 0)
            or flight.get("authority") != model_retry_authority(data["settings"])
            or write_permission_revoked(row, data["settings"])
            or row["steps"] >= row["limits"]["max_steps"]
            or row["elapsed_seconds"] >= row["limits"]["max_seconds"]
            or budget.time_remaining(row) <= 0
        ):
            return False
        budget_state = budget.public(row)
        limits = budget_state["limits"]
        lifetime = budget_state["lifetime"]
        if (
            lifetime["charged_tokens"] >= limits["max_total_tokens"]
            or lifetime["charged_model_calls"] >= limits["max_total_model_calls"]
            or lifetime["charged_tool_calls"] >= limits["max_total_tool_calls"]
        ):
            return False
        bound = row.get("local_execution") or {}
        if bound.get("project_id"):
            try:
                project = projects.validate_binding(
                    data,
                    {
                        "id": bound["project_id"],
                        "path": bound.get("workspace_root"),
                        "generation": bound.get("project_generation"),
                    },
                )
                current_root = project["path"]
            except AgentError:
                return False
        else:
            current_root = data["settings"].get("workspace_root") or self.store.default_workspace()
        if bound.get("workspace_root", "") != current_root:
            return False
        try:
            self._validate_context(row, cheap_only=True, control_state=data)
        except AgentError:
            return False
        return True

    def reconcile(self) -> None:
        """Called once at node construction, before worker startup."""
        if not self.store.path.exists():
            return

        def reconcile(data):
            from .execution_policy import verified_read_classification
            for row in data["tasks"].values():
                journal = row.get("stream_read_journal", [])
                if journal:
                    recover_stream_reads(row, allow_publish=bool(data["settings"].get("enabled") and all(
                        entry["authority"] == model_retry_authority(data["settings"]) for entry in journal)))
                budget.abandon(row)
                flight = row.get("in_flight")
                if flight or row["status"] == "running" or row.get("approved"):
                    was_cancelled = row["status"] == "cancelled" or (row.get("pending_control") or {}).get("kind") == "cancel"
                    was_paused = row["status"] == "paused" or (row.get("pending_control") or {}).get("kind") == "pause"
                    was_editing = (row.get("pending_control") or {}).get("kind") == "edit_message"
                    workspace_root = (row.get("local_execution") or {}).get("workspace_root", "")
                    is_verified_read = bool(
                        flight
                        and flight.get("dispatched")
                        and verified_read_classification(flight, task_id=row["id"], workspace_root=workspace_root)
                    )
                    ambiguous_write = bool(
                        flight
                        and flight.get("effect") == "write"
                        and flight.get("dispatched")
                        and not is_verified_read
                    )
                    cleanup_ok = True
                    recovery = None
                    if is_verified_read or (flight and flight.get("effect") == "read" and flight.get("dispatched")):
                        cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error="agent_node_restarted")
                    if is_verified_read and flight.get("effect") == "write":
                        if not cleanup_ok:
                            row.update(
                                status="cancelled" if was_cancelled else "interrupted",
                                approved=None,
                                error="agent_node_restarted",
                            )
                            event(
                                row,
                                "interrupted",
                                ambiguous_write=False,
                                continued=False,
                                cleanup_confirmed=False,
                            )
                            continue
                        may_continue = (
                            not was_cancelled
                            and not was_paused
                            and self._read_may_continue_after_restart(row, flight, data)
                        )
                        settled_outcome = self._settle_interrupted_read(row, flight, "agent_node_restarted", recovery=recovery)
                        row["in_flight"] = None
                        row["approved"] = None
                        settle(row, event, data=data)
                        if was_editing:
                            # The owner already committed the replacement turn.
                            # Its old flight settled above; do not overwrite the
                            # edited branch's queued/paused state with recovery.
                            may_continue = row["status"] == "queued"
                        elif was_cancelled:
                            row.update(status="cancelled", error="agent_node_restarted")
                        elif was_paused:
                            row.update(status="paused", error="agent_node_restarted")
                        elif may_continue:
                            row.update(status="queued", error=None)
                        else:
                            row.update(status="interrupted", error="agent_node_restarted")
                        event(
                            row,
                            "interrupted",
                            ambiguous_write=False,
                            continued=may_continue,
                            cleanup_confirmed=True,
                            ledger_outcome=settled_outcome,
                        )
                        continue
                    row.update(status="interrupted", approved=None, error="agent_write_outcome_unknown" if ambiguous_write else "agent_node_restarted")
                    if ambiguous_write:
                        verification.record(row, flight, "unknown", now(), flight=flight)
                        self._schedule_reconciliation(row)
                    if not ambiguous_write:
                        if cleanup_ok:
                            row["in_flight"] = None
                            settle(row, event, data=data)
                        if not (was_editing and cleanup_ok):
                            row["status"] = "cancelled" if was_cancelled else "interrupted"
                    event(row, "interrupted", ambiguous_write=ambiguous_write)
        self.store.transaction(reconcile)

    def _schedule_reconciliation(self, row):
        prior = row.get("reconciliation")
        if hasattr(self.tools, "reconcile") and (prior is None or prior.get("state") == "running"):
            row["reconciliation"] = {"state": "pending", "attempts": (prior or {}).get("attempts", 0)}

    def _reconcile_operation(self, selected: dict) -> None:
        """Query the original operation only; never dispatch the mutation again."""
        started = time.monotonic()
        flight = selected["in_flight"]
        operation_id = flight.get("operation_id")
        receipt = next((r for r in selected.get("receipts", []) if r["operation_id"] == operation_id), None)

        def claim(data):
            row = task(data, selected["id"])
            pending = row.get("reconciliation", {})
            if not ambiguous(row) or pending.get("state") != "pending" or row["control_revision"] != selected["control_revision"]:
                return False
            if pending.get("attempts", 0) >= 3 or row["elapsed_seconds"] >= row["limits"]["max_seconds"] or receipt is None:
                row["reconciliation"] = {**pending, "state": "unknown", "reason": "agent_reconciliation_limit"}
                return False
            row["reconciliation"] = {"state": "running", "attempts": pending.get("attempts", 0) + 1, "operation_id": operation_id}
            self.store.reserve(data, row["id"], output_policy.storage_growth(row["limits"]["max_output_chars"]))
            event(row, "reconciliation_started", operation_id=operation_id)
            return True
        try:
            claimed = self.store.transaction(claim)
        except AgentError as exc:
            code = str(exc)
            self.store.transaction(lambda data: task(data, selected["id"])["reconciliation"].update(state="unknown", reason=code))
            return
        if not claimed:
            return

        self._active_run = {"task_id": selected["id"], "started": started,
                            "recorded_seconds": selected["elapsed_seconds"], "revision": selected["revision"]}

        def cancelled():
            current = self.store.read()
            row = task(current, selected["id"])
            return (not current["settings"]["enabled"] or row["control_revision"] != selected["control_revision"] or
                    row["reconciliation"]["state"] != "running" or
                    selected["elapsed_seconds"] + time.monotonic() - started >= selected["limits"]["max_seconds"])
        result = {"outcome": "unknown", "reason": "agent_reconciliation_failed"}
        context_valid = False
        try:
            context = self._tool_context(selected, operation_id)
            context["operation"] = copy.deepcopy(receipt)
            result = sanitized(self._read_call(selected, "reconcile", lambda: self.tools.reconcile(operation_id, context=context, cancelled=cancelled)))
            if len(encoded(result)) > selected["limits"]["max_output_chars"] or result.get("outcome") not in {"executed", "not_executed", "unknown"}:
                raise AgentError("agent_invalid_reconciliation_response")
            if result["outcome"] != "unknown":
                proven = result.get("receipt") or {}
                if any(proven.get(key) != receipt[key] for key in ("operation_id", "tool", "arguments_sha256")) or not isinstance(proven.get("result"), dict):
                    raise AgentError("agent_invalid_reconciliation_response")
                if result["outcome"] == "not_executed" and proven["result"].get("not_executed") is not True:
                    raise AgentError("agent_invalid_reconciliation_response")
            # This check authorizes only future inference, never another write.
            # Preserve a proven receipt even if resource egress was revoked.
            try:
                self._validate_context(selected)
                context_valid = True
            except AgentError:
                pass
        except Exception:
            result = {"outcome": "unknown", "reason": "agent_reconciliation_failed"}

        def save(data):
            row = task(data, selected["id"])
            row["elapsed_seconds"] += time.monotonic() - started
            if row["control_revision"] != selected["control_revision"] or not data["settings"]["enabled"]:
                row["reconciliation"].update(state="unknown", reason="agent_reconciliation_interrupted")
                return
            row["reconciliation"].update(state=result["outcome"], checked_at=now())
            if result["outcome"] == "unknown":
                row["reconciliation"]["reason"] = str(result.get("reason", "No attributable operation evidence."))[:2000]
                event(row, "reconciliation_finished", outcome="unknown", operation_id=operation_id)
                return
            proven_result = result["receipt"]["result"]
            may_continue = (
                result["outcome"] == "executed" and context_valid
                and row["status"] == "interrupted" and not row.get("pending_control")
                and not row.get("pending_input") and not row.get("pending_approval")
                and flight.get("authorization") in {"approved", "automatic"}
                and flight.get("control_revision") == row["control_revision"]
                and flight.get("goal_revision") == row["goal"]["revision"]
                and flight.get("scope_revision") == row["scope_revision"]
                and flight.get("context_epoch") == row.get("context_epoch", 0)
                and flight.get("authority") == model_retry_authority(data["settings"])
                and not write_permission_revoked(row, data["settings"])
                and row["steps"] < row["limits"]["max_steps"]
                and row["elapsed_seconds"] < row["limits"]["max_seconds"]
            )
            verification.record(row, flight, "confirmed" if result["outcome"] == "executed" else "not_executed", now(), result=proven_result)
            row["messages"].append({"role": "tool", "tool_call_id": flight["call_id"], "content": encoded(proven_result).decode(), "context_epoch": flight.get("context_epoch", 0)})
            row["queue"] = [call for call in row["queue"] if call["operation_id"] != operation_id]
            row["in_flight"] = None
            close_queue(row, event, "agent_reconciliation_requires_review", keep_flight=False)
            was_cancelled = row["status"] == "cancelled" or row.get("pending_control", {}).get("kind") == "cancel"
            settle(row, event, data=data)
            row.update(status="cancelled" if was_cancelled else "queued" if may_continue else "paused", error=None, approved=None, pending_approval=None)
            row["outcome"].update(status="unverified", uncertainties=["Original operation reconciled; complete goal verification is still required."])
            event(row, "reconciliation_finished", outcome=result["outcome"], operation_id=operation_id, continued=may_continue)
        self.store.transaction(save)

    def _fail(self, task_id: str, code: str, *, elapsed: float = 0.0, read_cleanup_ok: bool | None = None, read_recovery: dict | None = None) -> None:
        def fail(data):
            from .execution_policy import verified_read_classification
            row = task(data, task_id)
            row["elapsed_seconds"] += elapsed
            flight = row["in_flight"]
            workspace_root = (row.get("local_execution") or {}).get("workspace_root", "")
            is_verified_read = bool(
                flight
                and flight.get("dispatched")
                and verified_read_classification(flight, task_id=row["id"], workspace_root=workspace_root)
            )
            ambiguous_write = bool(
                flight
                and flight.get("effect") == "write"
                and flight.get("dispatched")
                and not is_verified_read
            )
            if ambiguous_write:
                if (row.get("pending_control") or {}).get("kind") == "permissions":
                    row.pop("pending_control", None)
                verification.record(row, flight, "unknown", now(), flight=flight)
                row.update(status="interrupted", error="agent_write_outcome_unknown", approved=None, pending_approval=None)
                self._schedule_reconciliation(row)
                event(row, "interrupted", error=row["error"], ambiguous_write=True)
            else:
                if is_verified_read:
                    if read_cleanup_ok is None:
                        cleanup_ok, recovery = self._recover_read_operation_details(row, flight, error=code)
                    else:
                        cleanup_ok, recovery = read_cleanup_ok, read_recovery
                    if not cleanup_ok:
                        if (row.get("pending_control") or {}).get("kind") == "permissions":
                            row.pop("pending_control", None)
                        row.update(status="interrupted", error=code, approved=None, pending_approval=None)
                        event(
                            row,
                            "interrupted",
                            error=code,
                            ambiguous_write=False,
                            continued=False,
                            cleanup_confirmed=False,
                        )
                        return
                    self._settle_interrupted_read(row, flight, code, recovery=recovery)
                row["in_flight"] = None
                if (row.get("pending_control") or {}).get("kind") == "permissions" and code not in {
                    "agent_owner_changed",
                    "agent_cancelled",
                    "agent_permission_denied",
                }:
                    row.pop("pending_control", None)
                if row.get("pending_control"):
                    settle(row, event, data=data)
                elif row["status"] in {"running", "queued"}:
                    row.update(status="failed", error=code, approved=None)
                    event(row, "failed", error=code)
        self.store.transaction(fail)

    def _operation_error(
        self,
        selected: dict,
        code: str,
        started: float,
        *,
        definitely_not_executed=False,
        read_connection=None,
        correction=None,
        details=None,
        diagnostics=None,
    ) -> None:
        try:
            self._handle_operation_error(
                selected,
                code,
                started,
                definitely_not_executed=definitely_not_executed,
                read_connection=read_connection,
                correction=None if code == "agent_owner_changed" else correction,
                details=details,
                diagnostics=diagnostics,
            )
        except AgentError as exc:
            if str(exc) not in {"agent_task_not_found", "agent_task_deleted"}:
                raise
            # A late operation from a retired process cannot resurrect a task
            # deleted after restart reconciliation cleared its read flight.

    def _handle_operation_error(
        self,
        selected: dict,
        code: str,
        started: float,
        *,
        definitely_not_executed=False,
        read_connection=None,
        correction=None,
        details=None,
        diagnostics=None,
    ) -> None:
        """Only proven no-side-effect failures may return to the correction loop."""
        if selected.get("verification"):
            def verification_failed(data):
                row = task(data, selected["id"])
                flight = row.get("in_flight") or {}
                row["in_flight"] = None
                row["elapsed_seconds"] += time.monotonic() - started
                if row.get("pending_control"):
                    settle(row, event, data=data)
                elif row["status"] == "running" and row["control_revision"] == selected["_run_control_revision"]:
                    pending = row.get("verification") or {}
                    if (code == "agent_invalid_verification_response" and flight.get("kind") == "verification_model"
                            and pending.get("format_retries", 0) < 1 and row["steps"] < row["limits"]["max_steps"]
                            and row["elapsed_seconds"] < row["limits"]["max_seconds"] and data["settings"]["enabled"]):
                        pending["format_retries"] = pending.get("format_retries", 0) + 1
                        row["status"] = "queued"
                        event(row, "verification_format_retry_scheduled", error=code, retry_count=pending["format_retries"])
                        return
                    verification.finish(row, error=code)
                    event(row, "verification_finished", outcome=row["outcome"]["status"], error=code)
                    event(row, "completed")
            self.store.transaction(verification_failed)
            return
        if code in RETRYABLE_MODEL_ERRORS and not selected["queue"]:
            now_mono = time.monotonic()
            req_started = selected.get("_request_started")
            prep_started = selected.get("_preparation_started", started)
            prep_persist = float(selected.get("_prep_persistence_seconds", 0.0))
            claim_persist = float(selected.get("_claim_persistence_seconds", 0.0))
            sched_wait = float(selected.get("_scheduler_wait_seconds", 0.0))
            failed_timing = {
                "scheduler_wait_seconds": round(sched_wait, 4),
                "preparation_seconds": round(max(0.0, (req_started if req_started is not None else now_mono) - prep_started), 4),
                "preparation_persistence_seconds": round(prep_persist, 4),
                "model_call_seconds": round(max(0.0, now_mono - req_started) if req_started is not None else 0.0, 4),
                "store_persistence_seconds": round(claim_persist + prep_persist, 4),
            }
            safe_diag = diagnostic_log.safe_diagnostics(diagnostics)
            req_id = selected.get("_request_id")
            attempt = selected.get("_attempt", int(selected.get("model_retry_count", 0)) + 1)
            diagnostic_log.failure(
                "model_inference",
                code,
                task_id=selected["id"],
                step=selected.get("_step") or selected.get("steps"),
                request_id=req_id,
                attempt=attempt,
                elapsed_seconds=now_mono - started,
                model_call_seconds=failed_timing["model_call_seconds"],
                diagnostics=safe_diag,
            )

            def retry_model(data):
                row = task(data, selected["id"])
                flight = row.get("in_flight") or {}
                if flight.get("kind") != "model" or not flight.get("dispatched") or row["queue"] or row["pending_approval"] or row["approved"]:
                    return False
                row["elapsed_seconds"] += time.monotonic() - started
                row["in_flight"] = None
                if row.get("pending_control") and row["pending_control"].get("kind") != "permissions":
                    settle(row, event, data=data)
                    return True
                if row["status"] != "running" or not data["settings"]["enabled"]:
                    row.pop("pending_control", None)
                    return True  # Pause/cancel/disable wins over late failure.
                authority = model_retry_authority(data["settings"])
                error = ("agent_permission_denied" if authority != selected.get("_model_retry_authority") else
                         "agent_time_limit" if row["elapsed_seconds"] >= row["limits"]["max_seconds"] else
                         "agent_step_limit" if row["steps"] >= row["limits"]["max_steps"] else
                         code if row.get("model_retry_count", 0) >= 1 else None)
                row.pop("pending_control", None)
                extra_event = {
                    "step": row["steps"],
                    "attempt": attempt,
                    "timing": failed_timing,
                    **({"request_id": req_id} if req_id else {}),
                    **({"diagnostics": safe_diag} if safe_diag else {}),
                }
                if error:
                    row.update(status="failed", error=error)
                    event(row, "failed", error=error, **extra_event)
                    return True
                row.update(status="queued", error=None, model_retry_count=1, model_retry_authority=authority)
                event(row, "model_retry_scheduled", error=code, retry_count=1, **extra_event)
                return True
            if self.store.transaction(retry_model):
                return
        elif not selected["queue"]:
            diagnostic_log.failure(
                "model_inference",
                code,
                task_id=selected["id"],
                step=selected.get("_step") or selected.get("steps"),
                request_id=selected.get("_request_id"),
                attempt=selected.get("_attempt"),
                elapsed_seconds=time.monotonic() - started,
                diagnostics=diagnostic_log.safe_diagnostics(diagnostics),
            )
        from .execution_policy import verified_read_classification
        current = task(self.store.read(), selected["id"])
        flight = current.get("in_flight") or {}
        queued_call = selected["queue"][0] if selected["queue"] else {}
        is_verified_read = bool(
            selected["queue"]
            and verified_read_classification(
                flight,
                task_id=current["id"],
                workspace_root=(current.get("local_execution") or {}).get("workspace_root", ""),
                operation_id=queued_call.get("operation_id"),
                tool=queued_call.get("name"),
                arguments=queued_call.get("arguments"),
            )
        )
        read_cleanup_ok = True
        read_recovery = None
        if is_verified_read and flight.get("dispatched"):
            if code == "agent_local_operation_unconfirmed":
                read_cleanup_ok = False
            else:
                read_cleanup_ok, read_recovery = self._recover_read_operation_details(current, flight, error=code)
        elif flight.get("effect") == "read" and flight.get("dispatched"):
            read_cleanup_ok, read_recovery = self._recover_read_operation_details(current, flight, error=code)
        if selected["queue"] and (
            definitely_not_executed
            or (selected["queue"][0]["effect"] == "read" and read_cleanup_ok)
            or (is_verified_read and read_cleanup_ok)
            or not flight.get("dispatched")
        ):
            call = selected["queue"][0]
            recovery_outcome = (read_recovery or {}).get("ledger_outcome")
            if recovery_outcome == "confirmed" and isinstance((read_recovery or {}).get("settled_result"), dict):
                result = sanitized(copy.deepcopy(read_recovery["settled_result"]))
                record_status = "confirmed"
            else:
                not_executed = (
                    (call["effect"] == "write" and not is_verified_read)
                    or (is_verified_read and (definitely_not_executed or recovery_outcome == "not_executed"))
                    or not flight.get("dispatched")
                )
                result = {"error": code, "not_executed": not_executed, "no_mutation": True}
                if isinstance(correction, str) and correction:
                    result["correction"] = sanitized(correction[:1600])
                if isinstance(details, dict) and details:
                    result["details"] = sanitized(copy.deepcopy(details))
                record_status = "not_executed" if result["not_executed"] else "failed"
            if code == "agent_ripgrep_unavailable_use_file_search":
                result["correction"] = (
                    "ripgrep (rg) is not installed in this node's command environment. Nothing was executed. "
                    "Use the built-in ryn_file_search with a path and literal query, or ryn_file_list to find files. "
                    "Search alternative terms in separate calls; do not repeat the unavailable rg command."
                )
            if call["name"] == "ryn_request_input" and code == "agent_invalid_control_proposal":
                result["correction"] = (
                    "Use kind=information/choice with text/choice fields for model names and other facts. "
                    "Use kind=credential_ref with ONLY credential_ref fields for credentials. "
                    "The secure form already collects the endpoint and credential. "
                    "Do not mix these field types or ask for secrets in chat."
                )
            if call["name"] == "ryn_update_plan" and code in {"agent_goal_revision_conflict", "agent_invalid_control_proposal"}:
                result.update(expected_goal_revision=current["goal"]["revision"],
                              allowed_criterion_ids=[item["id"] for item in current["criteria"]])
            if call["effect"] == "read" and read_connection:
                from .diagnostics import safe_label
                result.update({key: safe_label(read_connection.get(key)) for key in ("connection_id", "connection_name")})

            step_elapsed = max(0.0, time.monotonic() - started)
            was_not_executed = bool(result.get("not_executed"))
            error_timing = {
                "scheduler_wait_seconds": round(float(selected.get("_scheduler_wait_seconds", 0.0)), 4),
                "preparation_seconds": round(step_elapsed if was_not_executed else 0.0, 4),
                "tool_execution_seconds": round(0.0 if was_not_executed else step_elapsed, 4),
                "store_persistence_seconds": round(float(selected.get("_claim_persistence_seconds", 0.0)), 4),
            }
            can_continue_cleanly = False

            def reject(data):
                nonlocal can_continue_cleanly
                row = task(data, selected["id"])
                row.update(in_flight=None, approved=None, pending_approval=None)
                row["elapsed_seconds"] += time.monotonic() - started
                consecutive_same_error = 0
                if record_status != "confirmed":
                    for prev_event in reversed(row.get("events", [])):
                        if prev_event.get("type") != "tool_result":
                            continue
                        if prev_event.get("context_epoch", 0) != call.get("context_epoch", 0):
                            break
                        prev_res = prev_event.get("result")
                        prev_err = prev_res.get("error") if isinstance(prev_res, dict) else None
                        if prev_event.get("tool") == call["name"] and prev_err == code and code is not None:
                            consecutive_same_error += 1
                        else:
                            break
                    if consecutive_same_error >= 1 and "correction" not in result:
                        result["correction"] = (
                            "This tool failed with the same error on the previous attempt. "
                            "Adjust the arguments, switch to an alternative tool, or report the limitation "
                            "instead of repeating the same failing call."
                        )
                row["messages"].append({"role": "tool", "tool_call_id": call["call_id"], "content": encoded(result).decode(), "context_epoch": call.get("context_epoch", 0)})
                verification.record(row, call, record_status, now(), result=result)
                row["queue"].pop(0)
                event(row, "tool_result", tool=call["name"], call_id=call["call_id"], operation_id=call["operation_id"],
                      step=row["steps"], result=result, internal=call["name"] in INTERNAL_NAMES,
                      context_epoch=call.get("context_epoch", 0), timing=error_timing)
                if row["status"] == "running":
                    if code == "agent_time_limit" or code.endswith("_budget_exhausted"):
                        row.update(status="failed", error=code)
                        event(row, "failed", error=code)
                    else:
                        row["status"] = "queued"
                settle(row, event, data=data)
                budget_state = budget.public(row)
                b_limits = budget_state["limits"]
                b_life = budget_state["lifetime"]
                within_budget = (
                    budget.time_remaining(row) > 0
                    and b_life["charged_tokens"] < b_limits["max_total_tokens"]
                    and (bool(row.get("queue")) or b_life["charged_model_calls"] < b_limits["max_total_model_calls"])
                    and (not row.get("queue") or b_life["charged_tool_calls"] < b_limits["max_total_tool_calls"])
                )
                can_continue_cleanly = bool(
                    (result.get("no_mutation") is True or record_status == "confirmed")
                    and consecutive_same_error == 0
                    and row["status"] == "queued"
                    and data["settings"].get("enabled")
                    and row["control_revision"] == selected["_run_control_revision"]
                    and not row.get("pending_control")
                    and not row.get("pending_approval")
                    and not row.get("approved")
                    and not row.get("pending_input")
                    and row["elapsed_seconds"] < row["limits"]["max_seconds"]
                    and (bool(row.get("queue")) or row["steps"] < row["limits"]["max_steps"])
                    and within_budget
                )
            self.store.transaction(reject)
            if can_continue_cleanly:
                self._step.clean = True
        else:
            self._fail(selected["id"], code, elapsed=time.monotonic() - started, read_cleanup_ok=read_cleanup_ok, read_recovery=read_recovery)

    def run_continuously(self) -> WorkerRunResult:
        """Worker entry point: consecutive steps without a scheduler interval between them.

        run_once stays exactly one step. Only a step that ended normally continues
        at once; a failed step (including a scheduled model retry) returns to the
        worker's ordinary interval. Bounded so worker slots rotate between tasks.
        """
        deadline = time.monotonic() + CONTINUE_SECONDS
        activity = False
        while True:
            result = self.run_once()
            if not result.activity:
                return WorkerRunResult(activity=activity)
            activity = True
            if not getattr(self._step, "clean", False) or time.monotonic() >= deadline:
                return result

    def run_once(self) -> WorkerRunResult:
        self._step.clean = False
        self._step.stream = None
        if not self._run_slots.acquire(blocking=False):
            return WorkerRunResult()
        if not self._run_lock.acquire(blocking=False):
            self._run_slots.release()
            return WorkerRunResult()
        gate_owned = True
        selected = None
        scope_cm = None
        started = time.monotonic()
        try:
            # Idle ticks only look: copying every task's history three times a
            # second per worker cost more CPU than the work it was checking for.
            peek = self.store.inspect(lambda data: {
                "enabled": data["settings"]["enabled"],
                "computer_activity": {key: {"status": row["status"],
                    "pending_control": bool(row.get("pending_control")),
                    "kind": (row.get("in_flight") or {}).get("kind")}
                    for key, row in data["tasks"].items()},
                "queued": any(row["status"] == "queued" for row in data["tasks"].values()),
                "waiting": any(row["status"] == "waiting_approval" and row.get("queue") for row in data["tasks"].values()),
                "unresolved": next((row["id"] for row in data["tasks"].values()
                                    if ambiguous(row) and row.get("reconciliation", {}).get("state") == "pending"), None)})
            native_computer = getattr(self.tools, "native_computer", None)
            overlay = getattr(native_computer, "overlay", None)
            if overlay is not None:
                overlay.sync(peek["computer_activity"] if peek["enabled"] else {})
            if peek["enabled"]:
                if peek["waiting"]:
                    self._refresh_waiting_reads(self._waiting_policy_snapshot())
                    # A released dialog is claimable in this same tick.
                    peek["queued"] = self.store.inspect(lambda data: any(row["status"] == "queued" for row in data["tasks"].values()))
                unresolved = task(self.store.read(), peek["unresolved"]) if peek["unresolved"] else None
                if unresolved:
                    self._run_lock.release()
                    gate_owned = False
                    self._reconcile_operation(unresolved)
                    return WorkerRunResult(activity=True)
            def claim(data):
                if not data["settings"]["enabled"]:
                    return None
                rows = sorted(data["tasks"].values(), key=lambda row: row["updated_at"])
                for row in rows:
                    if row["status"] != "queued":
                        continue
                    if budget.time_remaining(row) <= 0:
                        row.update(status="failed", error="agent_time_budget_exhausted")
                        event(row, "failed", error=row["error"])
                        continue
                    if row.get("model_retry_count", 0) and row.get("model_retry_authority") != model_retry_authority(data["settings"]):
                        row.update(status="failed", error="agent_permission_denied")
                        event(row, "failed", error=row["error"])
                        continue
                    if row["elapsed_seconds"] >= row["limits"]["max_seconds"]:
                        if row.get("verification"):
                            verification.finish(row, error="agent_time_limit")
                            event(row, "verification_finished", outcome=row["outcome"]["status"], error="agent_time_limit")
                            event(row, "completed")
                            continue
                        row.update(status="failed", error="agent_time_limit")
                        event(row, "failed", error=row["error"])
                        continue
                    if not row["queue"] and row["steps"] >= row["limits"]["max_steps"]:
                        if row.get("verification"):
                            verification.finish(row, error="agent_step_limit")
                            event(row, "verification_finished", outcome=row["outcome"]["status"], error="agent_step_limit")
                            event(row, "completed")
                            continue
                        row.update(status="failed", error="agent_step_limit")
                        event(row, "failed", error=row["error"])
                        continue
                    row["status"] = "running"
                    row["in_flight"] = {"kind": "verification" if row.get("verification") else "tool" if row["queue"] else "model", "dispatched": False,
                                        "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"], "scope_revision": row["scope_revision"]}
                    queued_at = self._queued_since.pop(row["id"], None)
                    scheduler_wait_seconds = max(0.0, claim_started - queued_at) if queued_at is not None else 0.0
                    return {**copy.deepcopy(row), "_model_retry_authority": model_retry_authority(data["settings"]), "_run_control_revision": row["control_revision"],
                            "_run_lifetime_remaining": budget.time_remaining(row),
                            "_scheduler_wait_seconds": scheduler_wait_seconds}
                return None
            # Avoid rewriting encrypted history every idle worker tick.
            if not peek["enabled"] or not peek["queued"]:
                return WorkerRunResult()
            claim_started = time.monotonic()
            selected = self.store.transaction(claim)
            if selected is None:
                return WorkerRunResult()
            if overlay is not None:
                peek["computer_activity"][selected["id"]] = {
                    "status": selected.get("status"),
                    "pending_control": bool(selected.get("pending_control")),
                    "kind": (selected.get("in_flight") or {}).get("kind"),
                }
                overlay.sync(peek["computer_activity"])
            selected["_claim_persistence_seconds"] = max(0.0, time.monotonic() - claim_started)
            self._run_lock.release()
            gate_owned = False
            scope_cm = diagnostic_log.context_scope(task_id=selected["id"], step=selected.get("steps", 0))
            scope_cm.__enter__()
            started = time.monotonic()
            selected["_run_started"] = started
            self._active_run = {"task_id": selected["id"], "started": started,
                                "recorded_seconds": selected["elapsed_seconds"], "revision": selected["revision"]}

            def cancelled():
                current = self.store.read_control(selected["id"])
                row = task(current, selected["id"])
                if selected["_run_lifetime_remaining"] <= time.monotonic() - started:
                    cancelled.reason = "agent_time_budget_exhausted"
                    return True
                # Business tools call this again under their mutation lock.
                # Consuming one-shot approval does not immunize a queued write
                # against a subsequent owner permission revocation.
                reason = ("agent_permission_denied" if write_permission_revoked(row, current["settings"]) else
                          "agent_owner_changed" if row["control_revision"] != selected["_run_control_revision"] else
                          "agent_cancelled" if not current["settings"]["enabled"] or row["status"] != "running" else
                          "agent_time_limit" if selected["elapsed_seconds"] + time.monotonic() - started >= selected["limits"]["max_seconds"] else None)
                if reason is None:
                    try:
                        self._validate_context(row, cheap_only=True, control_state=current)
                    except AgentError as exc:
                        reason = str(exc)
                cancelled.reason = reason
                return reason is not None

            if selected.get("verification"):
                self._verify(selected, cancelled, started)
            elif selected["queue"]:
                self._tool(selected, cancelled, started)
            else:
                self._model(selected, cancelled, started)
            self._step.clean = True
            return WorkerRunResult(activity=True)
        except AgentToolRejected as exc:
            if selected:
                code = str(exc)
                if code == "agent_cancelled":
                    code = getattr(cancelled, "reason", None) or code
                self._operation_error(
                    selected,
                    code,
                    started,
                    definitely_not_executed=True,
                    correction=getattr(exc, "correction", None),
                    details=getattr(exc, "details", None),
                    diagnostics=getattr(exc, "diagnostics", None),
                )
            return WorkerRunResult(activity=bool(selected))
        except AgentError as exc:
            if selected:
                code = cancellation_reason(cancelled) if str(exc) == "agent_cancelled" else str(exc)
                if code == "agent_inference_failed" and exc.__context__ is not None:
                    self._record_runtime_fault(selected, exc.__context__)
                self._operation_error(
                    selected,
                    code,
                    started,
                    read_connection=exc.connection if isinstance(exc, AgentReadFailure) else None,
                    diagnostics=getattr(exc, "diagnostics", None),
                )
            return WorkerRunResult(activity=bool(selected))
        except ValueError as exc:
            # Only explicitly enumerated boundary codes may reach the owner.
            # Arbitrary provider exceptions can contain URLs or credentials.
            safe = {"agent_context_limit", "agent_inference_failed", "agent_invalid_model_response",
                    "agent_model_busy", "agent_model_output_limit", "agent_model_unavailable",
                    "agent_model_tools_unsupported", "agent_tool_call_limit", "agent_unknown_tool",
                    "agent_approval_preview_changed", "agent_friend_inactive", "agent_friend_model_not_shareable",
                    "agent_model_not_shareable", "agent_model_uses_personal_space_access",
                    "agent_permission_revision_conflict", "agent_personal_space_required", "agent_tool_arguments_invalid"}
            if selected:
                code = str(exc) if str(exc) in safe else "agent_operation_failed"
                if code == "agent_operation_failed":
                    self._record_runtime_fault(selected, exc)
                if str(exc) == "agent_cancelled":
                    code = getattr(cancelled, "reason", None) or ("agent_time_limit" if selected["elapsed_seconds"] + time.monotonic() - started >= selected["limits"]["max_seconds"] else "agent_cancelled")
                self._operation_error(selected, code, started)
            return WorkerRunResult(activity=bool(selected))
        except Exception as exc:
            if selected:
                self._record_runtime_fault(selected, exc)
                self._operation_error(selected, "agent_operation_failed", started)
            return WorkerRunResult(activity=bool(selected))
        finally:
            if selected is not None:
                self._clear_stream(selected["id"], self._step.stream)
                try:
                    post = self.store.read_control(selected["id"]).get("tasks", {}).get(selected["id"]) or {}
                    if post.get("status") == "queued":
                        self._queued_since[selected["id"]] = time.monotonic()
                    else:
                        self._queued_since.pop(selected["id"], None)
                    if overlay is not None and post:
                        peek["computer_activity"][selected["id"]] = {
                            "status": post.get("status"),
                            "pending_control": bool(post.get("pending_control")),
                            "kind": (post.get("in_flight") or {}).get("kind"),
                        }
                        overlay.sync(peek["computer_activity"])
                except Exception:
                    pass
            if scope_cm is not None:
                scope_cm.__exit__(None, None, None)
            self._active_run = None
            if gate_owned:
                self._run_lock.release()
            self._run_slots.release()

    def _record_runtime_fault(self, selected, exc):
        # Record code locations, never exception text, locals, paths, URLs or
        # upstream payloads, so an unexpected error can actually be diagnosed.
        frames, tb = [], exc.__traceback__
        while tb:
            code = tb.tb_frame.f_code
            filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
            frames.append({"file": filename, "function": code.co_name, "line": tb.tb_lineno})
            tb = tb.tb_next
        self.store.transaction(lambda data: event(task(data, selected["id"]), "runtime_fault",
            exception_type=type(exc).__name__, frames=frames[-6:]))

    def _change_review(self, row: dict):
        """Net file changes this task made through file tools, or None when there is nothing to review."""
        tool = getattr(self.tools, "change_review", None)
        root = (row.get("local_execution") or {}).get("workspace_root", "")
        goal = row["goal"]["revision"]
        if tool is None or not root or not any(
                r.get("effect") == "write" and r.get("status") == "confirmed" and r.get("goal_revision") == goal
                and str(r.get("tool", "")).startswith("ryn_file_") for r in row.get("receipts", [])):
            return None
        try:
            review = tool(row["id"], root)
        except Exception:
            return None
        if not isinstance(review, dict) or not review.get("files"):
            return None
        # A new plain-text deliverable has no original code/tests to compare.
        # Its content and current bytes are still checked by normal goal verification.
        if all(entry.get("status") == "created" and str(entry.get("path", "")).lower().endswith((".txt", ".md"))
               for entry in review["files"]):
            return None
        # Rendered appearance is evidence only for the exact bytes that were rendered.
        inspected = {receipt["result"].get("sha256") for receipt in row.get("receipts", [])
                     if receipt.get("tool") == "ryn_render_inspect" and receipt.get("status") == "confirmed"
                     and isinstance(receipt.get("result"), dict)}
        for entry in review["files"]:
            if entry.get("status") != "deleted" and str(entry.get("path", "")).lower().endswith(VISUAL_SUFFIXES):
                entry["render_inspected"] = entry.get("sha256") in inspected
        return review

    def _verify(self, selected: dict, cancelled, started: float) -> None:
        """One read-only check per worker tick, then a separate no-tool assessment."""
        self._reserve_output(selected)
        self._validate_context(selected)
        pending = selected["verification"]
        if cancelled():
            raise AgentError(cancelled.reason)
        if pending["phase"] == "mechanical":
            check = pending["checks"][pending["index"]]
            receipt = next(r for r in selected["receipts"] if r["id"] == check["receipt_id"])
            if isinstance(receipt.get("result"), dict) and receipt["result"].get("result_ref"):
                original, _ = results.read(self.store, selected, receipt["result"]["result_ref"], provider=True)
                receipt = {**receipt, "result": original}
            criterion = next(c for c in selected["criteria"] if c["id"] == check["criterion_id"])
            verify_context = self._tool_context(selected, receipt["operation_id"])
            dependencies = verification.observation_transitions.context(selected, criterion)
            for dependency in dependencies:
                item = dependency["receipt"]
                if isinstance(item.get("result"), dict) and item["result"].get("result_ref"):
                    item["result"], _ = results.read(self.store, selected, item["result"]["result_ref"], provider=True)
            if dependencies:
                verify_context["observation_dependencies"] = dependencies
            if criterion["verifier"] is None:
                result = {"status": "unknown", "evidence": {"reason": "No registered business verifier."}, "checked_at": now()}
            else:
                result = sanitized(self._read_call(selected, "verify", lambda: self.tools.verify(copy.deepcopy(receipt), [copy.deepcopy(criterion)],
                    context=verify_context, cancelled=cancelled)))
            if not isinstance(result, dict) or result.get("status") not in {"passed", "failed", "unknown"} or not isinstance(result.get("evidence"), dict):
                raise AgentError("agent_invalid_verification_response")
            if len(encoded(result)) > selected["limits"]["max_output_chars"]:
                raise AgentError("agent_output_limit")
            if cancelled():
                raise AgentError(cancelled.reason)

            def save_check(data):
                row = task(data, selected["id"])
                row["in_flight"] = None
                row["elapsed_seconds"] += time.monotonic() - started
                if row["status"] != "running" or row["control_revision"] != selected["_run_control_revision"]:
                    settle(row, event, data=data)
                    return
                evidence_id = "evidence_" + uuid.uuid4().hex
                row.setdefault("verification_evidence", []).append({"id": evidence_id, "criterion_id": criterion["id"], "receipt_id": receipt["id"],
                    "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"], "kind": "mechanical", **result})
                current = next(c for c in row["criteria"] if c["id"] == criterion["id"])
                current.update(status=result["status"], evidence_refs=[evidence_id])
                row["verification"]["index"] += 1
                if row["verification"]["index"] == len(row["verification"]["checks"]):
                    row["verification"]["phase"] = "semantic"
                row["status"] = "queued"
                event(row, "criterion_checked", criterion_id=criterion["id"], status=result["status"], evidence_id=evidence_id)
            self.store.transaction(save_check)
            return

        if len({ref for criterion in selected["criteria"] if criterion.get("required") for ref in criterion["evidence_refs"]}) > 128:
            raise AgentError("agent_verification_evidence_limit")
        checker_state = copy.deepcopy(selected)
        change_review = self._change_review(selected)
        if change_review:
            checker_state["change_review"] = change_review
        for receipt in checker_state.get("receipts", []):
            if (receipt["goal_revision"] == selected["goal"]["revision"] and receipt["context_epoch"] == selected.get("context_epoch", 0)
                    and isinstance(receipt.get("result"), dict) and receipt["result"].get("result_ref")):
                receipt["result"], _ = results.read(self.store, selected, receipt["result"]["result_ref"], provider=True)
        messages = verification.checker_messages(checker_state, context_limit=self._context_limit(selected))
        if len(encoded(messages)) > self._context_limit(selected):
            raise AgentError("agent_verification_context_limit")

        def mark(data):
            row = task(data, selected["id"])
            if row["status"] != "running" or row["control_revision"] != selected["_run_control_revision"]:
                row["in_flight"] = None
                row["elapsed_seconds"] += time.monotonic() - started
                settle(row, event, data=data)
                return False
            if row["steps"] >= row["limits"]["max_steps"]:
                raise AgentError("agent_step_limit")
            row["steps"] += 1
            row["in_flight"].update(kind="verification_model", dispatched=True)
            event(row, "verification_model_started", step=row["steps"])
            return True
        if not self.store.transaction(mark):
            return
        try:
            response = self._complete(selected, messages, [], cancelled, lambda _metadata: None)
        except AgentModelResponseError as exc:
            # The bridge can reject an empty, length-exhausted response before
            # JSON parsing. It belongs to the same ONE bounded format-repair
            # attempt as truncated JSON, not a separate retry allowance. The
            # meter already persisted safe stop/usage metadata and charged it.
            # Filters, cancellations and unknown failures retain their policy.
            if str(exc) == "agent_invalid_model_response" and exc.finish_reason == "length":
                raise AgentError("agent_invalid_verification_response") from None
            raise
        if cancelled():
            raise AgentError(cancelled.reason)
        if not isinstance(response, dict) or not isinstance(response.get("message"), dict):
            raise AgentError("agent_invalid_verification_response")
        if len(encoded(response)) > output_policy.MAX_RAW_RESPONSE_BYTES:
            raise AgentError("agent_model_output_limit")
        if len(encoded(output_policy.visible_message(response["message"]))) > selected["limits"]["max_output_chars"]:
            raise AgentError("agent_output_limit")
        try:
            assessment = sanitized(verification.parse_assessment(response, selected))
        except AgentError:
            def save_invalid(data):
                row = task(data, selected["id"])
                content = (response.get("message") or {}).get("content", "") if isinstance(response, dict) else ""
                content = content if isinstance(content, str) else ""
                row.setdefault("verification_rejected_responses", []).append({"goal_revision": selected["goal"]["revision"],
                    "context_epoch": selected.get("context_epoch", 0), "at": now(), "content": sanitized(content),
                    "response_sha256": digest(response), "usage": sanitized(response.get("usage")),
                    "metadata": sanitized(response.get("metadata")), "finish_reason": response.get("finish_reason")})
                event(row, "verification_response_rejected", error="agent_invalid_verification_response", response_sha256=digest(response))
            self.store.transaction(save_invalid)
            raise

        def finish_check(data):
            row = task(data, selected["id"])
            row["in_flight"] = None
            row["elapsed_seconds"] += time.monotonic() - started
            if row["status"] != "running" or row["control_revision"] != selected["_run_control_revision"]:
                settle(row, event, data=data)
                return
            if row["elapsed_seconds"] >= row["limits"]["max_seconds"]:
                verification.finish(row, error="agent_time_limit")
            else:
                if assessment.get("reported_status"):
                    event(row, "verification_assessment_normalized", reported_status=assessment["reported_status"],
                          status=assessment["status"], reason=assessment["normalization_reason"])
                verification.finish(row, assessment=assessment)
            event(row, "verification_finished", outcome=row["outcome"]["status"])
            if verification.repair(row, assessment):
                event(row, "verification_retry_scheduled", repair_count=row["verification_cycle"]["repairs"], goal_revision=row["goal"]["revision"])
            else:
                event(row, "completed")
        self.store.transaction(finish_check)
