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
    SHARED_V2_CARRIER,
    check_legacy_public_carrier_compatible,
    cleared_internal_image_scope,
    internal_image_scope,
    prepare_internal_image_profile,
    shared_image_carrier_scope,
)
from . import data_policy, diagnostic_log, image_capabilities, output_policy, reasoning
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

SLOT_WAIT_MAX_SECONDS = 120.0
SLOT_POLL_INTERVAL_SECONDS = 0.2
SLOT_QUEUE_EVENT_AFTER_SECONDS = 2.0
BUSY_REASONS = frozenset({"node_slots", "service_slots", "memory_pressure", "gpu_temperature", "disk"})
RESPONSE_TOO_LARGE_FEEDBACK = (
    "上一次回复超过大小上限，没有执行任何工具。请把大段写入拆成多次较小的 apply_patch/写文件调用"
)
OUTPUT_LIMIT_REACHED_FEEDBACK = (
    "Server note: Your previous response used the entire output token budget on thinking/reasoning without "
    "producing any visible text or tool call. Keep internal reasoning concise and output the reply or tool call directly."
)
OUTPUT_LIMIT_REACHED_MESSAGE = "模型把输出额度都用在思考上了，请调高该模型的输出上限或降低推理强度"

_PEER_ERROR_MAP = {
    "cli_model_discovery_failed": "agent_model_discovery_failed",
    "model_configuration_changed": "agent_model_destination_changed",
    "ai_permission_denied": "agent_model_destination_changed",
    "source_protocol_parameters_unsupported": "agent_model_protocol_unsupported",
    "source_protocol_response_invalid": "agent_model_protocol_invalid",
    "upstream_image_rejected": "agent_model_image_rejected",
    "upstream_output_limit_reached": "agent_model_output_limit_reached",
    "model_daily_quota_exhausted": "agent_model_quota_exhausted",
    "peer_request_too_large": "agent_model_context_overflow",
    "estimated_context_window_exceeded": "agent_model_context_overflow",
    "task_cancelled": "agent_cancelled",
}
_ADAPTER_ERROR_MAP = {
    "cli_model_discovery_failed": "agent_model_discovery_failed",
    "inference_timeout": "agent_inference_timeout",
    "runtime_connection_failed": "agent_model_connection_failed",
    "upstream_http_failed": "agent_model_upstream_failed",
    "upstream_stream_error": "agent_model_upstream_failed",
    "upstream_stream_truncated": "agent_model_stream_interrupted",
    "upstream_image_rejected": "agent_model_image_rejected",
    "upstream_output_limit_reached": "agent_model_output_limit_reached",
    "upstream_request_rejected": "agent_model_request_rejected",
    "source_protocol_parameters_unsupported": "agent_model_protocol_unsupported",
    "source_protocol_response_invalid": "agent_model_protocol_invalid",
    "runtime_busy": "agent_model_busy",
    "ai_permission_denied": "agent_model_destination_changed",
    "model_configuration_changed": "agent_model_destination_changed",
    "model_daily_quota_exhausted": "agent_model_quota_exhausted",
    "model_not_ready": "agent_model_starting",
    "model_not_found": "agent_model_unavailable",
    "runtime_unavailable": "agent_model_starting",
    "task_cancelled": "agent_cancelled",
}
MODEL_ERROR_CODES = frozenset({
    *_ADAPTER_ERROR_MAP.values(),
    *_PEER_ERROR_MAP.values(),
    "agent_inference_failed",
    "agent_model_destination_unverifiable",
    "agent_model_tools_unsupported",
})


def retry_output_limit_cap(prev_cap: int, target: dict) -> int:
    return min(2 * prev_cap, output_policy.configured_output(target))


def _cli_binding(adapter, model):
    try:
        return adapter.binding_for_model(model)
    except AdapterError as exc:
        raise AgentError(_ADAPTER_ERROR_MAP.get(exc.code, "agent_inference_failed")) from None


def has_output_limit_feedback(messages) -> bool:
    return any(
        isinstance(m, dict) and OUTPUT_LIMIT_REACHED_FEEDBACK in str(m.get("content") or "")
        for m in (messages or [])
    )


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
    supports_cache_key = True
    supports_model_events = True


    def __init__(self, services, catalog, *, local_model_status=None):
        self.services, self.catalog = services, catalog
        self.local_model_status = local_model_status

    def _local_model_starting(self, model_id):
        if not model_id.startswith("local/") or self.local_model_status is None:
            return False
        status = self.local_model_status()
        return (model_id == "local/" + status.get("id", "")
                and status.get("state") in {"starting", "installing"})

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
                actual = _cli_binding(manager.adapter, target.get("cli_model", "")) if callable(method) else None
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
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            if isinstance(exc, AgentError) and str(exc) == "agent_model_discovery_failed":
                raise
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
            raw_message = data.get("message")
            if not isinstance(raw_message, dict) or raw_message.get("role") != "assistant":
                raise ValueError
            measured = {}
            if "content" in raw_message and raw_message["content"] is not None:
                measured["content"] = raw_message["content"]
            if "tool_calls" in raw_message and raw_message["tool_calls"] is not None:
                measured["tool_calls"] = raw_message["tool_calls"]
            if len(json.dumps(measured, ensure_ascii=False).encode("utf-8")) > output_policy.MAX_RAW_RESPONSE_BYTES:
                detail = RESPONSE_TOO_LARGE_FEEDBACK
                raise ValueError
            keep_provider_state = "provider_state" in raw_message
            if keep_provider_state:
                if len(json.dumps(raw_message["provider_state"], ensure_ascii=False).encode("utf-8")) > output_policy.MAX_PROVIDER_STATE_BYTES:
                    keep_provider_state = False
            message = {
                k: copy.deepcopy(v)
                for k, v in raw_message.items()
                if k != "provider_state" or keep_provider_state
            }
            if isinstance(message.get("reasoning_content"), str):
                rc_bytes = message["reasoning_content"].encode("utf-8")
                if len(rc_bytes) > output_policy.MAX_REASONING_CONTENT_BYTES:
                    message["reasoning_content"] = rc_bytes[:output_policy.MAX_REASONING_CONTENT_BYTES].decode("utf-8", errors="ignore")
            calls = response_calls(message)
            if not message.get("content") and not calls:
                if finish == "content_filter":
                    raise AgentError("agent_model_content_filtered")
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
        except AgentError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            error = AgentModelResponseError(usage, finish)
            error.detail = detail
            raise error from None
        finish = finish if isinstance(finish, str) and finish in {"stop", "length", "tool_calls", "content_filter", "cancelled"} else "unknown"
        cached = cached_input_tokens(data) if "reported_usage" in data or target["source"] != "peer" else None
        if cached is not None:
            flags["cached_tokens"] = cached
        cache_created = data.get("cache_creation_input_tokens") if ("reported_usage" in data or target["source"] != "peer") else None
        if type(cache_created) is int and cache_created >= 0:
            flags["cache_creation_input_tokens"] = cache_created
        if isinstance(data.get("diagnostics"), dict) and data["diagnostics"]:
            flags["diagnostics"] = copy.deepcopy(data["diagnostics"])
        return {"message": message, "finish_reason": finish, "usage": usage, "output_policy": policy, **flags}

    def complete(self, model_id, messages, tool_specs, cancelled, on_event=None, *, expected_destination=None, max_output_tokens=None, deadline=None, on_request_policy=None, reasoning_effort=None, conversation=None, cache_key=None, request_id=None, data_policy_state=None):
        if cancelled():
            raise AgentError(cancellation_reason(cancelled))
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                raise AgentError("agent_invalid_request_deadline")
            if time.monotonic() >= deadline:
                raise AgentError("agent_inference_timeout")
        try:
            target = self.catalog.resolve(model_id)
        except ValueError as exc:
            if str(exc) == "agent_model_unavailable" and self._local_model_starting(model_id):
                raise ValueError("agent_model_starting") from None
            raise
        restricted = data_policy.task_requires_model_restriction(data_policy_state)
        if restricted and expected_destination is None:
            raise AgentError("agent_extension_model_not_allowed")
        if expected_destination is not None and (not target.get("destination_revision") or
                expected_destination != self.catalog.destination(target)):
            raise AgentError("agent_model_destination_changed")
        task_id = request_id if isinstance(request_id, str) and re.fullmatch(r"agent_[0-9a-f]{32}", request_id) else "agent_" + uuid.uuid4().hex
        has_images = image_capabilities.message_has_image_input(messages)
        shared_carrier = None
        shared_resize = None
        if has_images:
            if target.get("source") == "peer":
                cap = image_capabilities.evaluate_image_capability(target, model_client=self)
                if (
                    getattr(self, "supports_image_input", False) is not True
                    or target.get("agent_supported") is False
                    or target.get("adapter") in image_capabilities.TEXT_ONLY_CLI_ADAPTERS
                    or cap["supports_image_input"] is not True
                ):
                    image_capabilities.require_route_image_support(target, model_client=self)
                # A shared model gets the same images as a local one; only what
                # exceeds the agreed carrier is compressed, and that is reported.
                shared_carrier = SHARED_V2_CARRIER if cap["image_transport"] == "peer_inline_v2" else None
                try:
                    messages, shared_resize = image_capabilities.fit_images_for_shared_route(
                        messages, shared_carrier or "inline-v1")
                except (ValueError, OSError):
                    raise AgentError("agent_image_invalid") from None
            else:
                image_capabilities.require_route_image_support(target, model_client=self)
        policy = output_policy.select(target, messages, tool_specs, request_cap=max_output_tokens)
        maximum = policy["effective_cap"]
        image_profile = None
        if has_images:
            if target.get("source") == "peer":
                legacy_ok, legacy_reason = (True, None) if shared_carrier else check_legacy_public_carrier_compatible(messages)
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
        with internal_image_scope(image_profile), shared_image_carrier_scope(shared_carrier):
            chat = normalize({"messages": messages, "tools": tool_specs, "max_tokens": maximum}, "chat")
        if reasoning_effort is not None:
            chat["reasoning_effort"] = reasoning.resolve(target, reasoning_effort)
        if isinstance(cache_key, str) and cache_key and target.get("source") != "peer":
            prompt_cache_mode = None
            if isinstance(target.get("model_settings"), dict):
                eff = target["model_settings"].get("effective")
                if isinstance(eff, dict) and eff.get("prompt_cache") in {"auto", "off"}:
                    prompt_cache_mode = eff["prompt_cache"]
            if prompt_cache_mode is None and target.get("source") == "local_api" and "source_store" in self.services:
                src_record = self.services["source_store"].read().get(target.get("source_id"), {})
                if src_record:
                    prompt_cache_mode = model_settings.effective(src_record, target.get("model_id", "")).get("prompt_cache")
            if prompt_cache_mode not in {"auto", "off"}:
                prompt_cache_mode = "auto" if target.get("adapter") in {"openai_responses", "anthropic_messages"} else "off"
            if prompt_cache_mode == "auto":
                from ..llm_package.chat import validate_prompt_cache_key
                chat["prompt_cache_key"] = validate_prompt_cache_key(cache_key)
        streaming = bool(on_event and target.get("streaming"))
        if streaming:
            chat["stream"] = True
        window = codex_threads.window_key(conversation)
        manager = self.services["provider_for"](target["service_id"]) if target["source"] != "peer" else None
        if target["source"] != "peer" and manager is None:
            raise ValueError("agent_model_starting" if self._local_model_starting(model_id) else "agent_model_unavailable")
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
                actual_binding = _cli_binding(manager.adapter, target.get("cli_model", "")) if hasattr(manager.adapter, "binding_for_model") else None
            else:
                actual_binding = getattr(manager.adapter, "model_binding", None) if manager else None
            if (manager is None or native_binding != actual_binding
                    or peer_binding.local_revision(native_binding, target["peer_id"]) != expected_destination["destination_revision"]):
                raise AgentError("agent_model_destination_changed")
        def check_data_release():
            context = None
            if restricted:
                try:
                    current = self.catalog.resolve(model_id)
                    context = self._data_policy_context(current, expected_destination, manager)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    if isinstance(exc, AgentError) and str(exc) == "agent_model_discovery_failed":
                        raise
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
        # Total duration on the Agent path is bounded solely by the caller's
        # task budget `deadline` (stream progress is guarded by adapter idle
        # timeouts). `provider_limit` is retained in `request_policy` for diagnostics.
        provider_limit = manager.manifest.timeout_seconds if manager else None
        request_deadline = deadline
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
                except ModelEventError as exc:
                    err_code = "upstream_response_too_large" if "limit exceeded" in str(exc) else "upstream_response_invalid"
                    raise AdapterError("invalid model event envelope", code=err_code) from None

        def cancel_transport():
            try:
                if manager:
                    manager.adapter.cancel(task_id)
                else:
                    self.services["cancel_order"](task_id)
            except Exception as exc:
                diagnostic_log.failure("inference_cancel", exc)
                request_policy["cancel_failed"] = True
                if on_request_policy:
                    with cleared_internal_image_scope():
                        on_request_policy(request_policy)

        def watch():
            while not done.wait(0.1):
                if cancelled() or expired():
                    cancel_transport()
                    return

        watcher = threading.Thread(target=watch, name="ryn-agent-inference-cancel", daemon=True)
        acquired = False
        try:
            if manager:
                wait_started = time.monotonic()
                max_wait = float(SLOT_WAIT_MAX_SECONDS)
                slot_wait_deadline = (
                    min(wait_started + max_wait, request_deadline)
                    if request_deadline is not None
                    else wait_started + max_wait
                )
                acquire_reason_fn = getattr(manager._slots, "acquire_reason", None)
                slot_reason = None
                queue_notified = False
                while True:
                    if callable(acquire_reason_fn):
                        slot_reason = acquire_reason_fn(blocking=False)
                        acquired = slot_reason is None
                    else:
                        acquired = manager._slots.acquire(blocking=False)
                        slot_reason = None if acquired else "service_slots"
                    if acquired or cancelled() or expired():
                        break
                    now = time.monotonic()
                    if not queue_notified and now - wait_started >= SLOT_QUEUE_EVENT_AFTER_SECONDS:
                        safe_on_event({
                            "type": "model_queue",
                            "reason": slot_reason or "service_slots",
                            "model_id": model_id,
                            "request_id": task_id,
                        })
                        queue_notified = True
                    if now >= slot_wait_deadline:
                        break
                    next_try = min(slot_wait_deadline, now + SLOT_POLL_INTERVAL_SECONDS)
                    for _ in range(4):
                        if cancelled() or expired():
                            break
                        rem = next_try - time.monotonic()
                        if rem <= 0:
                            break
                        time.sleep(min(0.05, rem))
                if not acquired:
                    if cancelled():
                        raise AgentError(cancellation_reason(cancelled))
                    if expired():
                        raise AgentError("agent_inference_timeout")
                    err = AgentError("agent_model_busy")
                    err.request_id = task_id
                    if slot_reason in BUSY_REASONS:
                        err.diagnostics = {"reason": slot_reason}
                    raise err
                with manager._lock:
                    manager._running += 1
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
            watcher.start()
            if expired():
                raise AgentError("agent_inference_timeout")
            check_data_release()
            if manager:
                effective_timeout = request_policy["effective_timeout_seconds"]
                if effective_timeout is None:
                    adapter_idle = getattr(manager.adapter, "timeout_s", None)
                    fallback_timeout = float(adapter_idle) if isinstance(adapter_idle, (int, float)) and adapter_idle > 0 else float(provider_limit or 120.0)
                else:
                    if effective_timeout <= 0:
                        raise AgentError("agent_inference_timeout")
                    fallback_timeout = effective_timeout
                options = {"task_id": task_id, "timeout_s": fallback_timeout, "on_event": emit if streaming else None}
                if target.get("cli_model"):
                    options["model"] = target["cli_model"]
                if getattr(manager.adapter, "supports_request_deadline", False) is True:
                    options["deadline"] = request_deadline
                if getattr(manager.adapter, "supports_cancel_callback", False) is True:
                    options["cancelled"] = cancelled
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
                    **({"image_carrier": shared_carrier} if shared_carrier else {}),
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
                    peer_err = result.get("error_code")
                    if peer_err in {"upstream_response_too_large", "peer_stream_too_large"}:
                        usage, finish = self._response_metadata(result, target)
                        rejected = AgentModelResponseError(usage, None if finish == "length" else finish)
                        rejected.request_id = task_id
                        rejected.detail = RESPONSE_TOO_LARGE_FEEDBACK
                        raise rejected
                    if peer_err == "upstream_output_limit_reached":
                        usage, _ = self._response_metadata(result, target)
                        if not has_output_limit_feedback(messages):
                            rejected = AgentModelResponseError(usage, "length")
                            rejected.request_id = task_id
                            rejected.detail = OUTPUT_LIMIT_REACHED_FEEDBACK
                            rejected.diagnostics = {"max_tokens": maximum}
                            raise rejected
                        err = (AgentModelResponseError(usage, "length", "agent_model_output_limit_reached")
                               if isinstance(usage, dict) else AgentError("agent_model_output_limit_reached"))
                        err.request_id = task_id
                        err.detail = OUTPUT_LIMIT_REACHED_MESSAGE
                        err.details = OUTPUT_LIMIT_REACHED_MESSAGE
                        err.diagnostics = {"max_tokens": maximum}
                        raise err
                    raise AgentError(_PEER_ERROR_MAP.get(peer_err, "agent_inference_failed"))
            stopped = cancelled()
            if stopped or expired():
                usage, finish = self._response_metadata(result, target)
                err = AgentModelResponseError(usage, finish, cancellation_reason(cancelled) if stopped else "agent_inference_timeout")
                err.request_id = task_id
                raise err
            response = {**self._response(result, target, tool_specs, policy), "request_policy": request_policy, "request_id": task_id,
                        **({"shared_image_resize": shared_resize} if shared_resize and shared_resize.get("resized") else {})}
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
            detail_code = str(exc.detail or "").strip()
            if detail_code.startswith("peer_delivery_failed:"):
                detail_code = detail_code.split(":", 1)[1]
            if detail_code == "peer_stream_too_large":
                rejected = AgentModelResponseError(None, None)
                rejected.request_id = task_id
                rejected.detail = RESPONSE_TOO_LARGE_FEEDBACK
                raise rejected from None
            if detail_code in {"peer_request_too_large", "request_too_large", "estimated_context_window_exceeded"}:
                err = AgentError("agent_model_context_overflow")
                err.request_id = task_id
                raise err from None
            if detail_code in {"direct_stream_interrupted", "p2p_stream_interrupted"}:
                err = AgentError("agent_model_stream_interrupted")
                err.request_id = task_id
                raise err from None
            raise AgentError(_PEER_ERROR_MAP.get(detail_code, "agent_inference_failed")) from None
        except AdapterError as exc:
            from .diagnostic_log import safe_diagnostics
            raw_diag = dict(getattr(exc, "diagnostics", None) or {})
            effective_settings = (
                (target.get("model_settings") or {}).get("effective")
                if isinstance(target.get("model_settings"), dict)
                else None
            )
            if not isinstance(effective_settings, dict) and target.get("source") == "local_api" and "source_store" in self.services:
                src_record = self.services["source_store"].read().get(target.get("source_id"), {})
                if src_record:
                    effective_settings = model_settings.effective(src_record, target.get("model_id", ""))
            if isinstance(effective_settings, dict) and type(effective_settings.get("stream_max_retries")) is int:
                raw_diag.setdefault("stream_max_retries", effective_settings["stream_max_retries"])
            diag = safe_diagnostics(raw_diag)
            if raw_diag.get("reason") in BUSY_REASONS:
                diag["reason"] = raw_diag["reason"]
            if type(raw_diag.get("limit_bytes")) is int and raw_diag["limit_bytes"] > 0:
                diag["limit_bytes"] = raw_diag["limit_bytes"]
            if type(raw_diag.get("max_tokens")) is int and raw_diag["max_tokens"] > 0:
                diag["max_tokens"] = raw_diag["max_tokens"]
            if raw_diag.get("tools_unsupported") is True and raw_diag.get("http_status") == 400 and not cancelled() and not expired():
                if manager is not None and getattr(manager, "manifest", None) is not None:
                    from ..llm_package.tool_capabilities import ToolCapabilities

                    caps = getattr(getattr(self.catalog, "store", None), "llm_tool_capabilities", None)
                    if caps is None and getattr(getattr(self.catalog, "store", None), "home", None) is not None:
                        caps = ToolCapabilities(self.catalog.store.home)
                    if caps is not None:
                        caps.save(manager.manifest, "unsupported", target.get("cli_model") or "")
                err = (
                    AgentModelResponseError(exc.reported_usage, exc.finish_reason, "agent_model_tools_unsupported")
                    if isinstance(exc.reported_usage, dict)
                    else AgentError("agent_model_tools_unsupported")
                )
                err.request_id = task_id
                if diag:
                    err.diagnostics = diag
                raise err from None
            if exc.code == "upstream_output_limit_reached" and not cancelled() and not expired() and not has_output_limit_feedback(messages):
                diag.setdefault("max_tokens", maximum)
                rejected = AgentModelResponseError(exc.reported_usage, "length")
                rejected.request_id = task_id
                rejected.detail = OUTPUT_LIMIT_REACHED_FEEDBACK
                rejected.diagnostics = diag
                raise rejected from None
            if exc.code in {"upstream_response_invalid", "upstream_response_too_large"} and not cancelled() and not expired():
                rej_finish = None if exc.code == "upstream_response_too_large" and exc.finish_reason == "length" else exc.finish_reason
                rejected = AgentModelResponseError(exc.reported_usage, rej_finish)
                rejected.request_id = task_id
                if diag:
                    rejected.diagnostics = diag
                if exc.code == "upstream_response_too_large":
                    rejected.detail = RESPONSE_TOO_LARGE_FEEDBACK
                else:
                    rejected.detail = "The model response or client tool proposal was invalid. Return the required response format and tool names, and obey each argument schema and size limit. No proposed tool was executed."
                    from ..llm_package.codex_tools import FEEDBACK
                    feedback = FEEDBACK.get(getattr(exc, "proposal_feedback_code", None))
                    if feedback:
                        rejected.detail = feedback + " No proposed tool was executed."
                raise rejected from None
            # Adapters already reduce upstream errors to this closed code set.
            # Keep transport failures distinguishable without copying an HTTP
            # response, URL, credential, or exception message into task state.
            code = _ADAPTER_ERROR_MAP.get(exc.code, "agent_inference_failed")
            if cancelled():
                code = cancellation_reason(cancelled)
            elif expired():
                code = "agent_inference_timeout"
            elif exc.code == "task_cancelled":
                code = cancellation_reason(cancelled)
            if isinstance(exc.reported_usage, dict):
                err = AgentModelResponseError(exc.reported_usage, exc.finish_reason, code)
            else:
                err = AgentError(code)
            err.request_id = task_id
            if code == "agent_model_output_limit_reached":
                err.detail = OUTPUT_LIMIT_REACHED_MESSAGE
                err.details = OUTPUT_LIMIT_REACHED_MESSAGE
                diag.setdefault("max_tokens", maximum)
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
    native_model = getattr(app.state, "native_model", None)
    return AgentModelBridge(services, catalog,
                            local_model_status=native_model.status if native_model is not None else None), RynTools(app, store, services, catalog), catalog
