"""In-process Agent inference through existing stateless provider services."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import threading
import time
import uuid

from fastapi import HTTPException

from ..llm_package import codex_threads, model_settings, peer_binding
from ..llm_package.adapters import AdapterError, actual_usage, cached_input_tokens
from ..llm_package.api_formats import normalize
from ..llm_package.chat import validate_chat
from ..llm_package.data_release import DataReleaseError, model_request_scope
from ..llm_package.image_input_profile import (
    check_legacy_public_carrier_compatible,
    cleared_internal_image_scope,
    internal_image_scope,
    prepare_internal_image_profile,
)
from . import data_policy, image_capabilities, output_policy, reasoning
from .model_events import (
    ModelEventError,
    ModelEventStream,
    project_leading_developer,
    response_calls,
)
from .models import ModelCatalog
from .store import (
    AgentError,
    AgentModelResponseError,
    AgentToolRejected,
    cancellation_reason,
)
from .tools import RynTools


class AgentModelBridge:
    supports_image_input = True
    supports_destination_binding = True
    supports_data_policy = True
    supports_output_token_limit = True
    supports_request_deadline = True
    supports_request_id = True
    supports_reasoning_effort = True
    # The runtime passes its task id; only an opaque hash leaves this process.
    supports_conversation_key = True
    supports_model_events = True


    def __init__(self, services, catalog):
        self.services, self.catalog = services, catalog

    def _data_policy_context(self, target, expected_destination, manager=None):
        """Use host-owned records and the actual adapter, never display claims."""
        destination = self.catalog.destination(target)
        if (not target.get("destination_revision") or expected_destination != destination
                or target.get("destination_binding_supported") is not True):
            raise AgentError("agent_model_destination_changed")
        consumer = self.catalog.store.peer_id
        context = {"consumer_node_id": consumer, "model": copy.deepcopy(target),
                   "model_destination": destination, "model_destination_verified": False}
        source = target.get("source")
        if source == "local_api":
            sources = self.services["source_store"]
            with sources.lock:
                record = sources.read().get(target.get("source_id"), {})
                if (not record or not record.get("enabled") or record.get("status") != "ready"
                        or target.get("model_id") not in record.get("models", [])
                        or target.get("peer_id") != consumer
                        or self.catalog.source_destination_revision(record, target["model_id"]) != destination["destination_revision"]
                        or manager is None
                        or getattr(manager, "_source_revision", None) != model_settings.fingerprint(record, target["model_id"])):
                    raise AgentError("agent_model_destination_changed")
                context["api_source"] = {"base_url": record["base_url"],
                                         "destination_revision": destination["destination_revision"]}
        elif source == "local_service":
            binding = target.get("local_model_binding")
            manifest = getattr(manager, "manifest", None)
            if manager is not None and target.get("adapter") in {"codex_cli", "antigravity_cli"}:
                method = getattr(manager.adapter, "binding_for_model", None)
                actual = method(target.get("cli_model", "")) if callable(method) else None
            else:
                actual = getattr(manager.adapter, "model_binding", None) if manager else None
            if (target.get("peer_id") != consumer or manager is None or binding != actual
                    or getattr(manifest, "package_id", None) != target.get("service_id")
                    or getattr(manifest, "runtime", None) != target.get("runtime")
                    or not peer_binding.valid(binding, target.get("service_id"))
                    or peer_binding.local_revision(binding, consumer) != destination["destination_revision"]):
                raise AgentError("agent_model_destination_changed")
        elif source == "peer":
            binding = target.get("expected_model_binding")
            if not isinstance(binding, dict) or target.get("consumer_peer_id") != consumer:
                raise AgentError("agent_model_destination_changed")
            record = {**binding, "binding_verified": True, "service_id": target["service_id"],
                      "peer_id": target["peer_id"]}
            if (peer_binding.expected(record, consumer) != binding
                    or peer_binding.revision(record, consumer) != destination["destination_revision"]
                    or (target.get("provider_resource") and
                        binding.get("external_permission") != {"resource": target["provider_resource"]})):
                raise AgentError("agent_model_destination_changed")
        else:
            raise AgentError("agent_extension_model_not_allowed")
        context["model_destination_verified"] = True
        return context

    def data_policy_context(self, model_id, expected_destination=None):
        try:
            target = self.catalog.resolve(model_id)
            manager = self.services["provider_for"](target["service_id"]) if target["source"] != "peer" else None
            destination = self.catalog.destination(target) if expected_destination is None else expected_destination
            return self._data_policy_context(target, destination, manager)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AgentError("agent_model_destination_changed") from None

    @staticmethod
    def _response_metadata(result, target):
        data = result if isinstance(result, dict) else {}
        finish = data.get("finish_reason")
        usage = actual_usage(data if "reported_usage" in data or target["source"] != "peer" else {})
        return usage, finish

    @staticmethod
    def _response(result, target, tool_specs, policy):
        data = result if isinstance(result, dict) else {}
        usage, finish = AgentModelBridge._response_metadata(result, target)
        detail = "the response was malformed"
        flags = {}
        try:
            if len(json.dumps(result).encode()) > output_policy.MAX_RAW_RESPONSE_BYTES:
                raise AgentModelResponseError(usage, finish, "agent_model_output_limit")
            message = copy.deepcopy(data.get("message"))
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
            calls = response_calls(message)
            if not message.get("content") and not calls:
                detail = "the response had neither text nor a tool call"
                raise ValueError
            for key in ("end_turn", "needs_follow_up"):
                if key in data:
                    if type(data[key]) is not bool:
                        raise ValueError
                    flags[key] = data[key]
            # Validate the assistant envelope separately from tool semantics.
            # A usable pairing id lets RynToolRouter return a tool error instead
            # of retrying an entire response that may contain valid sibling calls.
            envelope = {**message, "tool_calls": [
                {"id": call["id"], "type": "function", "function": {"name": "__ryn_envelope__", "arguments": "{}"}}
                for call in calls]}
            validate_chat({"messages": [envelope, *[
                {"role": "tool", "tool_call_id": call["id"], "content": ""} for call in calls]]})
        except AgentModelResponseError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            error = AgentModelResponseError(usage, finish)
            error.detail = detail
            raise error from None
        finish = finish if isinstance(finish, str) and finish in {"stop", "length", "tool_calls", "content_filter", "cancelled"} else "unknown"
        cached = cached_input_tokens(data) if "reported_usage" in data or target["source"] != "peer" else None
        if cached is not None:
            flags["cached_tokens"] = cached
        return {"message": message, "finish_reason": finish, "usage": usage, "output_policy": policy, **flags}

    def complete(self, model_id, messages, tool_specs, cancelled, on_event=None, *, expected_destination=None, max_output_tokens=None, deadline=None, on_request_policy=None, reasoning_effort=None, conversation=None, request_id=None, data_policy_state=None):
        if cancelled():
            raise AgentError(cancellation_reason(cancelled))
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                raise AgentError("agent_invalid_request_deadline")
            if time.monotonic() >= deadline:
                raise AgentError("agent_inference_timeout")
        target = self.catalog.resolve(model_id)
        restricted = data_policy.task_requires_model_restriction(data_policy_state)
        if restricted and expected_destination is None:
            raise AgentError("agent_extension_model_not_allowed")
        if expected_destination is not None and (not target.get("destination_revision") or
                expected_destination != self.catalog.destination(target)):
            raise AgentError("agent_model_destination_changed")
        task_id = request_id if isinstance(request_id, str) and re.fullmatch(r"agent_[0-9a-f]{32}", request_id) else "agent_" + uuid.uuid4().hex
        has_images = image_capabilities.message_has_image_input(messages)
        if has_images:
            if target.get("source") == "peer":
                cap = image_capabilities.evaluate_image_capability(target, model_client=self)
                if (
                    getattr(self, "supports_image_input", False) is not True
                    or target.get("agent_supported") is False
                    or target.get("adapter") in image_capabilities.TEXT_ONLY_CLI_ADAPTERS
                    or (
                        cap["model_image_support"] == "unsupported"
                        and cap["image_capability_reason"] != "peer_image_route_unverified"
                    )
                ):
                    image_capabilities.require_route_image_support(target, model_client=self)
            else:
                image_capabilities.require_route_image_support(target, model_client=self)
        policy = output_policy.select(target, messages, tool_specs, request_cap=max_output_tokens)
        maximum = policy["effective_cap"]
        image_profile = None
        if has_images:
            if target.get("source") == "peer":
                legacy_ok, legacy_reason = check_legacy_public_carrier_compatible(messages)
                if not legacy_ok:
                    raise AgentToolRejected(
                        "agent_image_input_unsupported",
                        correction=(
                            "The selected peer route only supports legacy inline image carrier bounds "
                            "(<=128 KiB decoded, <=180000 data URL chars, PNG/JPEG/WEBP) and cannot "
                            "transport expanded full-size local images without negotiated remote carrier support."
                        ),
                        details={
                            "reason": "peer_expanded_image_carrier_unsupported",
                            "carrier_limit_reason": legacy_reason or "peer_carrier_limit_exceeded",
                            "source": "peer",
                            "adapter": str(target.get("adapter") or "unknown"),
                            "image_carrier_available": False,
                            "supports_image_input": False,
                        },
                    )
            else:
                try:
                    image_profile = prepare_internal_image_profile(
                        messages,
                        route_identity=target,
                        request_identity=task_id,
                    )
                except ValueError as exc:
                    reason = str(exc) or "unsupported_image_mime"
                    if reason in {"unsupported_image_mime", "unsupported_route_image_carrier"}:
                        raise AgentToolRejected(
                            "agent_image_input_unsupported",
                            correction=(
                                "The selected route carrier does not support this image MIME format; "
                                "use a supported canonical image format without silent transcoding."
                            ),
                            details={
                                "reason": reason,
                                "source": str(target.get("source") or "unknown"),
                                "adapter": str(target.get("adapter") or "unknown"),
                                "image_carrier_available": False,
                                "supports_image_input": False,
                            },
                        ) from None
                    raise AgentError("agent_image_invalid") from None
        with internal_image_scope(image_profile):
            chat = normalize({"messages": messages, "tools": tool_specs, "max_tokens": maximum}, "chat")
        if reasoning_effort is not None:
            chat["reasoning_effort"] = reasoning.validate(target, reasoning_effort)
        streaming = bool(on_event and target.get("streaming"))
        if streaming:
            chat["stream"] = True
        window = codex_threads.window_key(conversation)
        manager = self.services["provider_for"](target["service_id"]) if target["source"] != "peer" else None
        if expected_destination is not None and target["source"] == "local_api":
            # Bind the actual immutable provider instance to the authorized
            # source snapshot, not only an earlier catalog lookup.
            sources = self.services["source_store"]
            with sources.lock:
                record = sources.read().get(target["source_id"], {})
                if not record or self.catalog.source_destination_revision(record, target["model_id"]) != expected_destination["destination_revision"]:
                    raise AgentError("agent_model_destination_changed")
                manager = self.services["provider_for"](target["service_id"])
                actual = model_settings.fingerprint(record, target["model_id"])
                if manager is None or getattr(manager, "_source_revision", None) != actual:
                    raise AgentError("agent_model_destination_changed")
        elif expected_destination is not None and target["source"] == "local_service":
            native_binding = target.get("local_model_binding")
            if manager is not None and target.get("adapter") in {"codex_cli", "antigravity_cli"}:
                actual_binding = manager.adapter.binding_for_model(target.get("cli_model", "")) if hasattr(manager.adapter, "binding_for_model") else None
            else:
                actual_binding = getattr(manager.adapter, "model_binding", None) if manager else None
            if (manager is None or native_binding != actual_binding
                    or peer_binding.local_revision(native_binding, target["peer_id"]) != expected_destination["destination_revision"]):
                raise AgentError("agent_model_destination_changed")
        if target["source"] != "peer" and manager is None:
            raise ValueError("agent_model_unavailable")

        def check_data_release():
            context = None
            if restricted:
                try:
                    current = self.catalog.resolve(model_id)
                    context = self._data_policy_context(current, expected_destination, manager)
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise AgentError("agent_model_destination_changed") from None
            if not data_policy.allows_task(data_policy_state, context):
                raise AgentError("agent_extension_model_not_allowed")

        check_data_release()
        adapter = manager.adapter if manager else None
        if (target.get("adapter") in {"openai_responses", "anthropic_messages"}
                and target["source"] == "peer"
                and ((expected_destination is None and not target.get("provider_resource"))
                     or target.get("destination_binding_supported") is not True)):
            raise AgentError("agent_model_destination_unverifiable")
        supports_developer = (target.get("supports_developer_role") is True
                              or getattr(adapter, "supports_developer_role", False) is True
                              or target.get("adapter") in {"codex_cli", "antigravity_cli"})
        if target.get("adapter") not in {"openai_responses", "anthropic_messages"}:
            for message in chat["messages"]:
                message.pop("provider_state", None)
        chat["messages"] = project_leading_developer(chat["messages"], supports_developer=supports_developer)
        # Local providers expose an authoritative manifest limit. Remote orders
        # enforce their own manifest/transport limits; no shorter remote timeout
        # field exists. Our watcher requests cancellation and rejects late data.
        provider_limit = manager.manifest.timeout_seconds if manager else None
        request_deadline = time.monotonic() + provider_limit if provider_limit is not None else deadline
        if deadline is not None:
            request_deadline = min(request_deadline, deadline) if request_deadline is not None else deadline
        done = threading.Event()

        def expired():
            return request_deadline is not None and time.monotonic() >= request_deadline

        def safe_on_event(event):
            if on_event is not None:
                with cleared_internal_image_scope():
                    on_event(event)

        model_events = ModelEventStream(safe_on_event if on_event else None, stopped=lambda: cancelled() or expired())

        def emit(chunk):
            with cleared_internal_image_scope():
                try:
                    model_events.feed(chunk)
                except ModelEventError:
                    raise AdapterError("invalid model event envelope", code="upstream_response_invalid") from None

        def cancel_transport():
            try:
                if manager:
                    manager.adapter.cancel(task_id)
                else:
                    self.services["cancel_order"](task_id)
            except Exception:
                pass

        def watch():
            while not done.wait(0.1):
                if cancelled() or expired():
                    cancel_transport()
                    return

        watcher = threading.Thread(target=watch, name="ryn-agent-inference-cancel", daemon=True)
        acquired = False
        try:
            if manager:
                slot_wait_deadline = time.monotonic() + min(
                    2.5,
                    max(0.0, (request_deadline - time.monotonic()) if request_deadline is not None else 2.5),
                )
                while True:
                    acquired = manager._slots.acquire(blocking=False)
                    if acquired or cancelled() or expired() or time.monotonic() >= slot_wait_deadline:
                        break
                    time.sleep(0.05)
                if not acquired:
                    if cancelled():
                        raise AgentError(cancellation_reason(cancelled))
                    if expired():
                        raise AgentError("agent_inference_timeout")
                    raise ValueError("agent_model_busy")
                with manager._lock:
                    manager._running += 1
            watcher.start()
            if on_event:
                safe_on_event({"type": "provider_selected", "model_id": model_id, "source": target["source"], "request_id": task_id})
            if cancelled():
                raise AgentError(cancellation_reason(cancelled))
            if expired():
                raise AgentError("agent_inference_timeout")
            request_policy = {"request_id": task_id, "provider_timeout_seconds": provider_limit,
                "effective_timeout_seconds": max(0.0, request_deadline - time.monotonic()) if request_deadline is not None else None,
                "measurement": "pre_admission_upper_bound",
                "enforcement": "adapter_timeout_and_cancel_watch" if manager else "consumer_cancel_watch_remote_transport_limits"}
            if on_request_policy:
                with cleared_internal_image_scope():
                    on_request_policy(request_policy)
            if expired():
                raise AgentError("agent_inference_timeout")
            check_data_release()
            if manager:
                options = {"task_id": task_id, "timeout_s": request_policy["effective_timeout_seconds"], "on_event": emit if streaming else None}
                if target.get("cli_model"):
                    options["model"] = target["cli_model"]
                if options["timeout_s"] <= 0:
                    raise AgentError("agent_inference_timeout")
                if getattr(manager.adapter, "supports_request_deadline", False) is True:
                    options["deadline"] = request_deadline
                if getattr(manager.adapter, "supports_conversation_scope", False) is True:
                    # This process is the authenticated requester of its own adapter.
                    options["conversation_scope"] = codex_threads.scope("local", window)
                with internal_image_scope(image_profile), model_request_scope(restricted=restricted, before_dispatch=check_data_release):
                    if expected_destination is not None and target["source"] == "local_service":
                        result = manager.adapter.call_bound("chat", target["local_model_binding"], check_data_release, chat, **options)
                    else:
                        result = manager.adapter.chat(chat, **options)
            else:
                # execute_order seals the complete structured chat and uses the
                # existing authenticated consumer transport and permission check.
                request = {
                    "network_id": self.catalog.network, "provider_peer_id": target["peer_id"],
                    "service_id": target["service_id"], "chat": chat, "max_tokens": maximum,
                    "task_id": task_id, "transport": "p2p", "_prefer_lan": True, "_no_retention": True,
                    **({"conversation_key": window} if window else {})}
                if target.get("cli_model"):
                    request["cli_model"] = target["cli_model"]
                if expected_destination is not None or target.get("provider_resource"):
                    if target.get("destination_binding_supported") is not True or not target.get("expected_model_binding"):
                        raise AgentError("agent_model_destination_unverifiable")
                    request["expected_model_binding"] = target["expected_model_binding"]
                result = asyncio.run(self.services["execute_order"](request, emit) if streaming
                                     else self.services["execute_order"](request))
                if not cancelled() and not expired() and result.get("state") != "succeeded":
                    raise AgentError({"model_configuration_changed": "agent_model_destination_changed",
                                      "ai_permission_denied": "agent_model_destination_changed",
                                      "source_protocol_parameters_unsupported": "agent_model_protocol_unsupported",
                                      "source_protocol_response_invalid": "agent_model_protocol_invalid",
                                      "model_daily_quota_exhausted": "agent_model_quota_exhausted"}.get(result.get("error_code"), "agent_inference_failed"))
            stopped = cancelled()
            if stopped or expired():
                usage, finish = self._response_metadata(result, target)
                err = AgentModelResponseError(usage, finish, cancellation_reason(cancelled) if stopped else "agent_inference_timeout")
                err.request_id = task_id
                raise err
            response = {**self._response(result, target, tool_specs, policy), "request_policy": request_policy, "request_id": task_id}
            try:
                model_events.finish(response)
            except ModelEventError:
                usage, finish = self._response_metadata(result, target)
                raise AgentModelResponseError(usage, finish) from None
            return response
        except DataReleaseError as exc:
            raise AgentError(str(exc)) from None
        except HTTPException as exc:
            if cancelled():
                raise AgentError(cancellation_reason(cancelled)) from None
            if expired():
                err = AgentError("agent_inference_timeout")
                err.request_id = task_id
                raise err from None
            raise AgentError("agent_model_destination_changed" if exc.detail == "model_configuration_changed" else "agent_inference_failed") from None
        except AdapterError as exc:
            from .diagnostic_log import safe_diagnostics
            diag = safe_diagnostics(getattr(exc, "diagnostics", None))
            if exc.code == "upstream_response_invalid" and not cancelled() and not expired():
                rejected = AgentModelResponseError(exc.reported_usage, exc.finish_reason)
                rejected.request_id = task_id
                if diag:
                    rejected.diagnostics = diag
                rejected.detail = "The model response or client tool proposal was invalid. Return the required response format and tool names, and obey each argument schema and size limit. No proposed tool was executed."
                from ..llm_package.codex_tools import FEEDBACK
                feedback = FEEDBACK.get(getattr(exc, "proposal_feedback_code", None))
                if feedback:
                    rejected.detail = feedback + " No proposed tool was executed."
                raise rejected from None
            # Adapters already reduce upstream errors to this closed code set.
            # Keep transport failures distinguishable without copying an HTTP
            # response, URL, credential, or exception message into task state.
            code = {"inference_timeout": "agent_inference_timeout",
                    "runtime_connection_failed": "agent_model_connection_failed",
                    "upstream_http_failed": "agent_model_upstream_failed",
                    "source_protocol_parameters_unsupported": "agent_model_protocol_unsupported",
                    "source_protocol_response_invalid": "agent_model_protocol_invalid",
                    "runtime_busy": "agent_model_busy",
                    "ai_permission_denied": "agent_model_destination_changed",
                    "model_configuration_changed": "agent_model_destination_changed",
                    "model_daily_quota_exhausted": "agent_model_quota_exhausted",
                    "model_not_ready": "agent_model_unavailable",
                    "model_not_found": "agent_model_unavailable",
                    "runtime_unavailable": "agent_model_unavailable"}.get(exc.code, "agent_inference_failed")
            if cancelled():
                code = cancellation_reason(cancelled)
            elif expired():
                code = "agent_inference_timeout"
            if isinstance(exc.reported_usage, dict):
                err = AgentModelResponseError(exc.reported_usage, exc.finish_reason, code)
            else:
                err = AgentError(code)
            err.request_id = task_id
            if diag:
                err.diagnostics = diag
            raise err from None
        finally:
            done.set()
            if watcher.is_alive():
                watcher.join(timeout=0.2)
            if acquired:
                with manager._lock:
                    manager._running -= 1
                manager._slots.release()


def build_agent_integrations(app, store, home):
    services = getattr(app.state, "llm_agent_services", None)
    if services is None:
        raise ValueError("agent_inference_services_unavailable")
    catalog = ModelCatalog(services, store)
    return AgentModelBridge(services, catalog), RynTools(app, store, services, catalog), catalog
