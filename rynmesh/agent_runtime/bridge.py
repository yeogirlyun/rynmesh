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
from ..llm_package.adapters import AdapterError
from ..llm_package.api_formats import normalize
from ..llm_package.chat import validate_chat, validate_tool_arguments
from . import output_policy, reasoning
from .models import ModelCatalog
from .prompt import INTERNAL_NAMES
from .store import AgentError, AgentModelResponseError, cancellation_reason
from .tools import RynTools


class AgentModelBridge:
    supports_image_input = True
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True
    supports_reasoning_effort = True
    # The runtime passes its task id; only an opaque hash leaves this process.
    supports_conversation_key = True

    def __init__(self, services, catalog):
        self.services, self.catalog = services, catalog

    @staticmethod
    def _response_metadata(result, target):
        data = result if isinstance(result, dict) else {}
        finish = data.get("finish_reason")
        reported = data.get("reported_usage")
        usage_source = reported if isinstance(reported, dict) else ({} if target["source"] == "peer" else data)
        usage = {key: usage_source.get(key) if type(usage_source.get(key)) is int and usage_source[key] >= 0 else None
                 for key in ("input_tokens", "output_tokens")}
        return usage, finish

    @staticmethod
    def _response(result, target, tool_specs, policy):
        data = result if isinstance(result, dict) else {}
        usage, finish = AgentModelBridge._response_metadata(result, target)
        detail = "the response was malformed"
        try:
            if len(json.dumps(result).encode()) > output_policy.MAX_RAW_RESPONSE_BYTES:
                raise AgentModelResponseError(usage, finish, "agent_model_output_limit")
            message = copy.deepcopy(data.get("message"))
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
            specs = {s["function"]["name"]: s["function"].get("parameters", {}) for s in tool_specs}
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > output_policy.MAX_TOOL_CALLS or not message.get("content") and not calls:
                detail = (f"more than {output_policy.MAX_TOOL_CALLS} tool calls in one response" if isinstance(calls, list) and len(calls) > output_policy.MAX_TOOL_CALLS
                          else "the response had neither text nor a tool call")
                raise ValueError
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name")
                shown = name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) else "(unnamed)"
                if name not in specs:
                    detail = f"'{shown}' is not one of the available tools; use only the tools offered"
                    raise ValueError
                try:
                    arguments = json.loads(function["arguments"])
                except (ValueError, TypeError, KeyError):
                    detail = f"the arguments of '{shown}' were not valid JSON"
                    raise ValueError from None
                if not isinstance(arguments, dict):
                    detail = f"the arguments of '{shown}' must be a JSON object"
                    raise ValueError
                if name not in INTERNAL_NAMES:
                    try:
                        validate_tool_arguments(specs[name], arguments)
                    except ValueError as exc:
                        cause = exc.__cause__
                        where = "/".join(re.sub(r"[^A-Za-z0-9_]", "", str(part))[:40] for part in getattr(cause, "absolute_path", ())) or "the top level"
                        keyword = re.sub(r"[^A-Za-z]", "", str(getattr(cause, "validator", "schema")))[:24] or "schema"
                        extra = ""
                        if keyword in {"required", "additionalProperties"}:
                            # These messages only name properties; they never echo argument values.
                            extra = ": " + re.sub(r"[^A-Za-z0-9_ ',.()-]", "", str(getattr(cause, "message", "")))[:140]
                        detail = f"the arguments of '{shown}' violate its schema at {where} ({keyword}){extra}"
                        raise
            validate_chat({"messages": [message, *[
                {"role": "tool", "tool_call_id": call["id"], "content": ""} for call in calls]]})
        except AgentModelResponseError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            error = AgentModelResponseError(usage, finish)
            error.detail = detail
            raise error from None
        finish = finish if isinstance(finish, str) and finish in {"stop", "length", "tool_calls", "content_filter", "cancelled"} else "unknown"
        return {"message": message, "finish_reason": finish, "usage": usage, "output_policy": policy}

    def complete(self, model_id, messages, tool_specs, cancelled, on_event=None, *, expected_destination=None, max_output_tokens=None, deadline=None, on_request_policy=None, reasoning_effort=None, conversation=None):
        if cancelled():
            raise AgentError(cancellation_reason(cancelled))
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                raise AgentError("agent_invalid_request_deadline")
            if time.monotonic() >= deadline:
                raise AgentError("agent_inference_timeout")
        target = self.catalog.resolve(model_id)
        if expected_destination is not None and (not target.get("destination_revision") or
                expected_destination != self.catalog.destination(target)):
            raise AgentError("agent_model_destination_changed")
        policy = output_policy.select(target, messages, tool_specs, request_cap=max_output_tokens)
        maximum = policy["effective_cap"]
        chat = normalize({"messages": messages, "tools": tool_specs, "max_tokens": maximum}, "chat")
        if reasoning_effort is not None:
            chat["reasoning_effort"] = reasoning.validate(target, reasoning_effort)
        streaming = bool(on_event and tool_specs and target.get("streaming"))
        if streaming:
            chat["stream"] = True
        emitted_chars = 0

        def emit(chunk):
            nonlocal emitted_chars
            if cancelled() or expired():
                return
            # Only actual assistant text deltas are public. Provider reasoning,
            # partial function arguments and transport details stay internal.
            for choice in chunk.get("choices", []):
                text = (choice.get("delta") or {}).get("content")
                if isinstance(text, str) and text and emitted_chars < 20000:
                    text = text[:20000 - emitted_chars]
                    emitted_chars += len(text)
                    on_event({"type": "text_delta", "text": text})
        task_id = "agent_" + uuid.uuid4().hex
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
                acquired = manager._slots.acquire(blocking=False)
                if not acquired:
                    raise ValueError("agent_model_busy")
                with manager._lock:
                    manager._running += 1
            watcher.start()
            if on_event:
                on_event({"type": "provider_selected", "model_id": model_id, "source": target["source"]})
            if cancelled():
                raise AgentError(cancellation_reason(cancelled))
            if expired():
                raise AgentError("agent_inference_timeout")
            request_policy = {"provider_timeout_seconds": provider_limit,
                "effective_timeout_seconds": max(0.0, request_deadline - time.monotonic()) if request_deadline is not None else None,
                "measurement": "pre_admission_upper_bound",
                "enforcement": "adapter_timeout_and_cancel_watch" if manager else "consumer_cancel_watch_remote_transport_limits"}
            if on_request_policy:
                on_request_policy(request_policy)
            if expired():
                raise AgentError("agent_inference_timeout")
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
                if expected_destination is not None and target["source"] == "local_service":
                    result = manager.adapter.call_bound("chat", target["local_model_binding"], lambda: None, chat, **options)
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
                if expected_destination is not None:
                    if target.get("destination_binding_supported") is not True or not target.get("expected_model_binding"):
                        raise AgentError("agent_model_destination_unverifiable")
                    request["expected_model_binding"] = target["expected_model_binding"]
                result = asyncio.run(self.services["execute_order"](request, emit) if streaming
                                     else self.services["execute_order"](request))
                if not cancelled() and not expired() and result.get("state") != "succeeded":
                    raise AgentError({"model_configuration_changed": "agent_model_destination_changed",
                                      "ai_permission_denied": "agent_model_destination_changed",
                                      "model_daily_quota_exhausted": "agent_model_quota_exhausted"}.get(result.get("error_code"), "agent_inference_failed"))
            stopped = cancelled()
            if stopped or expired():
                usage, finish = self._response_metadata(result, target)
                raise AgentModelResponseError(usage, finish, cancellation_reason(cancelled) if stopped else "agent_inference_timeout")
            return {**self._response(result, target, tool_specs, policy), "request_policy": request_policy}
        except HTTPException as exc:
            if cancelled():
                raise AgentError(cancellation_reason(cancelled)) from None
            if expired():
                raise AgentError("agent_inference_timeout") from None
            raise AgentError("agent_model_destination_changed" if exc.detail == "model_configuration_changed" else "agent_inference_failed") from None
        except AdapterError as exc:
            if exc.code == "upstream_response_invalid" and not cancelled() and not expired():
                rejected = AgentModelResponseError(exc.reported_usage, exc.finish_reason)
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
                    "runtime_busy": "agent_model_busy",
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
                raise AgentModelResponseError(exc.reported_usage, exc.finish_reason, code) from None
            raise AgentError(code) from None
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
