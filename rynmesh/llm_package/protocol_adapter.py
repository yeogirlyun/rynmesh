"""Native upstream protocols behind Ryn's single validated chat/tool contract.

The adapter proposes calls only. Execution, authorization and durable receipts
remain with the local Agent; a protocol failure never retries a paid request.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request

from .adapters import (
    MAX_JSON_RESPONSE_BYTES,
    MAX_SSE_LINE_BYTES,
    MAX_STREAM_BYTES,
    AdapterError,
    OpenAICompatibleAdapter,
    _openai_usage,
    _response_too_large,
    _set_response_timeout,
    cached_input_tokens,
)
from .data_release import DataReleaseError
from .data_release import urlopen as model_urlopen
from .transport_failure import (
    _MAX_ERROR_BODY_BYTES,
    _extract_envelope_fields,
    _fallback_minimal_envelope,
    describe,
    read_http_failure,
)

_CACHE_CONTROL_LOCK = threading.Lock()
_CACHE_CONTROL_UNSUPPORTED: set[tuple[str, str, str]] = set()


def _payload_has_cache_control(obj) -> bool:
    if isinstance(obj, dict):
        return "cache_control" in obj or any(_payload_has_cache_control(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_payload_has_cache_control(item) for item in obj)
    return False


def _read_http_failure_with_envelope(exc: urllib.error.HTTPError, phase: str):
    envelope = None
    try:
        raw = exc.read(_MAX_ERROR_BODY_BYTES + 1)
        if raw:
            if len(raw) <= _MAX_ERROR_BODY_BYTES:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        envelope = parsed
                except (ValueError, TypeError):
                    envelope = _fallback_minimal_envelope(raw)
            else:
                envelope = _fallback_minimal_envelope(raw)
    except OSError:
        envelope = None
    return describe(exc, phase, envelope), envelope


def _envelope_mentions_cache_control(envelope) -> bool:
    if not isinstance(envelope, dict):
        return False
    code, err_type, param, msg = _extract_envelope_fields(envelope)
    return "cache_control" in f"{code} {err_type} {param} {msg}".lower()


class NativeProtocolAdapter(OpenAICompatibleAdapter):
    supports_developer_role = True

    def __init__(self, *, protocol, cache_control_supported: bool | None = None, **options):
        if protocol not in {"openai_responses", "anthropic_messages"}:
            raise ValueError("unsupported source protocol")
        super().__init__(**options)
        self.protocol = protocol
        self._cache_control_supported = cache_control_supported

    def _cache_control_key(self) -> tuple[str, str, str]:
        return (self.base_url, self.api_prefix, self.model)

    def _allows_cache_control(self) -> bool:
        if self.prompt_cache == "off" or self._cache_control_supported is False:
            return False
        with _CACHE_CONTROL_LOCK:
            if self._cache_control_key() in _CACHE_CONTROL_UNSUPPORTED:
                return False
        return True

    def _mark_cache_control_unsupported(self) -> None:
        self._cache_control_supported = False
        with _CACHE_CONTROL_LOCK:
            _CACHE_CONTROL_UNSUPPORTED.add(self._cache_control_key())
        if callable(self._on_capability_evidence):
            try:
                self._on_capability_evidence("cache_control", "unsupported")
            except Exception:
                return

    def _headers(self):
        headers = super()._headers()
        if self.protocol == "anthropic_messages":
            token = headers.pop("Authorization", "")
            if token:
                headers["x-api-key"] = token.removeprefix("Bearer ")
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def health(self):
        # Listing the catalog never makes a paid inference request.
        return {"ok": bool(self.model), "model": self.model}

    def capabilities(self):
        return {"chat_completions": bool(self.model), "streaming": True,
                "cancel": "best_effort", "protocol": self.protocol}

    def _scope(self):
        # Opaque continuation is valid only for this configured source/model.
        return hashlib.sha256(json.dumps([self.protocol, self.base_url, self.api_prefix,
            self.model, self._headers()], sort_keys=True).encode()).hexdigest()

    def infer(self, *, prompt, max_tokens, task_id, timeout_s, messages=None):
        return self.chat({"messages": messages or [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens}, task_id=task_id, timeout_s=timeout_s)

    def infer_stream(self, *, prompt, max_tokens, task_id, timeout_s, on_delta, messages=None):
        def emit(chunk):
            for choice in chunk.get("choices", []):
                text = (choice.get("delta") or {}).get("content")
                if text:
                    on_delta(text)
        delivered = False
        def observed(chunk):
            nonlocal delivered
            for choice in chunk.get("choices", []):
                if (choice.get("delta") or {}).get("content"):
                    delivered = True
            emit(chunk)
        result = self.chat({"messages": messages or [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens, "stream": True},
                         task_id=task_id, timeout_s=timeout_s, on_event=observed)
        if not delivered and result["text"]:
            on_delta(result["text"])
        return result

    def chat(self, body, *, task_id, timeout_s=None, on_event=None, deadline=None):
        from .chat import validate_chat
        from .protocol_codecs import NativeStreamError, StreamDecoder, _finish, _usage, make_request, parse_response

        started = time.monotonic()
        if timeout_s is not None and (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise AdapterError("invalid request timeout", code="inference_timeout")
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise AdapterError("invalid_request_deadline")
        idle_s = (
            float(self.timeout_s)
            if deadline is not None
            else (min(float(timeout_s), float(self.timeout_s)) if timeout_s is not None else float(self.timeout_s))
        )
        last_byte = started
        is_sse = False
        received_usage = received_finish = received_cached = received_cache_creation = None
        request_diagnostics: dict[str, int] = {}
        ignored_bad_frames = 0
        phase = "request"
        decoder = None

        def read_timeout():
            if deadline is None:
                return idle_s
            return min(idle_s, max(.001, deadline - time.monotonic()))

        def check():
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise AdapterError("inference timed out", code="inference_timeout")
            if now - last_byte > idle_s:
                raise AdapterError(
                    "inference timed out",
                    code="inference_timeout",
                    diagnostics={"network_reason": "idle_timeout", "network_phase": phase},
                )

        def deliver(chunk):
            if on_event:
                try:
                    on_event(chunk)
                except AdapterError:
                    raise
                except Exception:
                    raise AdapterError("stream_delivery_failed", code="stream_delivery_failed") from None

        try:
            check()
            if not self.model:
                raise AdapterError("model_not_found", code="model_not_found")
            validated = validate_chat(body)
            scope = self._scope()
            effective_prompt_cache = self.prompt_cache or "auto"
            if self.protocol == "anthropic_messages" and not self._allows_cache_control():
                effective_prompt_cache = "off"
            path = "/v1/responses" if self.protocol == "openai_responses" else "/v1/messages"
            for cache_attempt in range(2):
                phase = "request"
                request_diagnostics = {}
                try:
                    payload = make_request(
                        {**self.request_defaults, **validated},
                        self.protocol,
                        self.model,
                        scope,
                        prompt_cache=effective_prompt_cache,
                        diagnostics=request_diagnostics,
                    )
                except (ValueError, TypeError, KeyError):
                    # Unsupported features are rejected before egress. Never use
                    # a second protocol as an automatic fallback with task data.
                    raise AdapterError("source_protocol_parameters_unsupported", code="source_protocol_parameters_unsupported") from None
                sent_cache_control = self.protocol == "anthropic_messages" and _payload_has_cache_control(payload)
                request = urllib.request.Request(
                    self._endpoint(path),
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._headers(),
                    method="POST",
                )
                try:
                    resp_ctx = model_urlopen(request, timeout=read_timeout())
                except urllib.error.HTTPError as exc:
                    diag, envelope = _read_http_failure_with_envelope(exc, phase)
                    exc.close()
                    if (
                        exc.code == 400
                        and sent_cache_control
                        and cache_attempt == 0
                        and _envelope_mentions_cache_control(envelope)
                    ):
                        self._mark_cache_control_unsupported()
                        effective_prompt_cache = "off"
                        continue
                    if diag.get("image_rejected"):
                        err_code = "upstream_image_rejected"
                    elif diag.get("network_reason") in {"request_rejected", "auth_rejected"}:
                        err_code = "upstream_request_rejected"
                    else:
                        err_code = "upstream_http_failed"
                    raise AdapterError(err_code, code=err_code, diagnostics=diag) from None
                break
            with resp_ctx as response:
                phase = "read"
                last_byte = time.monotonic()
                with self._lock:
                    self._active_responses[task_id] = response
                check()
                is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
                if not is_sse:
                    raw = response.read(MAX_JSON_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_JSON_RESPONSE_BYTES:
                        raise _response_too_large(MAX_JSON_RESPONSE_BYTES)
                    decoded = json.loads(raw)
                    if isinstance(decoded, dict):
                        received_usage = _openai_usage(_usage(decoded.get("usage"), self.protocol))
                        received_cached = cached_input_tokens(decoded.get("usage"))
                        if self.protocol == "anthropic_messages" and isinstance(decoded.get("usage"), dict):
                            raw_cc = decoded["usage"].get("cache_creation_input_tokens")
                            if type(raw_cc) is int and raw_cc >= 0:
                                received_cache_creation = raw_cc
                        try:
                            received_finish = _finish(decoded, self.protocol)
                        except NativeStreamError:
                            raise
                        except ValueError:
                            received_finish = None
                    check()
                    last_byte = time.monotonic()
                    result = parse_response(decoded, self.protocol, self.model, scope)
                else:
                    decoder = StreamDecoder(self.protocol, self.model, scope)
                    # SSE permits multiple data lines and comments. A blank
                    # line closes a frame; socket EOF is never a success marker.
                    total = frame_size = consecutive_bad_frames = 0
                    data = []

                    def frame():
                        nonlocal received_usage, received_finish, received_cached, received_cache_creation
                        nonlocal consecutive_bad_frames, ignored_bad_frames
                        value = "\n".join(data)
                        if value == "[DONE]":
                            return
                        try:
                            event = json.loads(value)
                        except ValueError:
                            consecutive_bad_frames += 1
                            ignored_bad_frames += 1
                            if consecutive_bad_frames >= 3:
                                raise
                            return
                        consecutive_bad_frames = 0
                        chunks = decoder.feed(event)
                        received_usage = _openai_usage(decoder.usage)
                        received_cached = decoder.cached_tokens
                        received_cache_creation = decoder.cache_creation_input_tokens
                        received_finish = decoder.finish_reason
                        for chunk in chunks:
                            check()
                            deliver(chunk)

                    while True:
                        check()
                        if deadline is not None:
                            _set_response_timeout(response, read_timeout())
                        line = response.readline(MAX_SSE_LINE_BYTES + 1)
                        check()
                        if not line:
                            if data:
                                frame()
                            break
                        last_byte = time.monotonic()
                        total += len(line)
                        frame_size += len(line)
                        if len(line) > MAX_SSE_LINE_BYTES:
                            raise _response_too_large(MAX_SSE_LINE_BYTES)
                        if frame_size > MAX_JSON_RESPONSE_BYTES:
                            raise _response_too_large(MAX_JSON_RESPONSE_BYTES)
                        if total > MAX_STREAM_BYTES:
                            raise _response_too_large(MAX_STREAM_BYTES)
                        line = line.decode("utf-8").rstrip("\r\n")
                        if not line:
                            if data:
                                frame()
                            data = []
                            frame_size = 0
                            if decoder._final is not None:
                                break
                        elif line.startswith("data:"):
                            data.append(line[5:].removeprefix(" "))
                    if decoder._final is None and not decoder._failed:
                        raise AdapterError("upstream_stream_truncated", code="upstream_stream_truncated")
                    result = decoder.result()
                    if received_cached is None:
                        received_cached = decoder.cached_tokens
                    if received_cache_creation is None:
                        received_cache_creation = decoder.cache_creation_input_tokens
            check()
            received_usage = _openai_usage(result.get("usage"))
            received_finish = result.get("finish_reason")
            message = result["message"]
            if received_finish != "content_filter" and not message.get("content") and not message.get("tool_calls"):
                raise ValueError("empty completion")
            duration = int((time.monotonic() - started) * 1000)
            response_diagnostics = dict(request_diagnostics)
            if decoder is not None:
                ignored_total = decoder.ignored_events + ignored_bad_frames
                if ignored_total > 0:
                    response_diagnostics["ignored_stream_events"] = ignored_total
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += received_usage["input_tokens"] or 0
                self._metrics.output_tokens += received_usage["output_tokens"] or 0
                self._metrics.unknown_usage_requests += any(v is None for v in received_usage.values())
                self._metrics.total_duration_ms += duration
            return {"text": message.get("content") or "", "message": message,
                    "finish_reason": received_finish, **received_usage,
                    "reported_usage": received_usage,
                    **({"cached_tokens": received_cached} if received_cached is not None else {}),
                    **({"cache_creation_input_tokens": received_cache_creation} if received_cache_creation is not None else {}),
                    **({"diagnostics": response_diagnostics} if response_diagnostics else {}),
                    "duration_ms": duration}
        except DataReleaseError:
            raise
        except Exception as exc:
            diag = (
                getattr(exc, "diagnostics", None)
                if isinstance(exc, (AdapterError, NativeStreamError))
                else describe(exc, phase)
                if isinstance(exc, OSError)
                else None
            )
            if decoder is not None:
                received_usage = _openai_usage(decoder.usage)
                received_finish = decoder.finish_reason
            with self._lock:
                self._metrics.failures += 1
            if task_id in self._cancelled:
                code, text = "inference_failed", "task_cancelled"
            elif isinstance(exc, AdapterError):
                code, text = exc.code, str(exc)
            elif isinstance(exc, NativeStreamError):
                code = text = exc.code
            elif isinstance(exc, urllib.error.HTTPError):
                diag = read_http_failure(exc, phase)
                exc.close()
                if diag.get("image_rejected"):
                    code = text = "upstream_image_rejected"
                elif diag.get("network_reason") in {"request_rejected", "auth_rejected"}:
                    code = text = "upstream_request_rejected"
                else:
                    code = text = "upstream_http_failed"
            elif isinstance(exc, OSError):
                timeout = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
                if timeout and phase == "read" and is_sse and not (deadline is not None and time.monotonic() >= deadline):
                    diag = {**(diag or {}), "network_reason": "idle_timeout"}
                code = text = "inference_timeout" if timeout else "runtime_connection_failed"
            else:
                # Wire/continuation failures are adapter errors, not a prompt
                # for the model to repeat an entire paid tool proposal batch.
                code = text = "source_protocol_response_invalid"
            raise AdapterError(text, code=code, reported_usage=received_usage, finish_reason=received_finish, diagnostics=diag) from None
        finally:
            with self._lock:
                self._active_responses.pop(task_id, None)
                self._cancelled.discard(task_id)
