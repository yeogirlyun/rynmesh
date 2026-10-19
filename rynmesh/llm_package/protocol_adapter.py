"""Native upstream protocols behind Ryn's single validated chat/tool contract.

The adapter proposes calls only. Execution, authorization and durable receipts
remain with the local Agent; a protocol failure never retries a paid request.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request

from .adapters import AdapterError, OpenAICompatibleAdapter, _openai_usage


class NativeProtocolAdapter(OpenAICompatibleAdapter):
    supports_developer_role = True

    def __init__(self, *, protocol, **options):
        if protocol not in {"openai_responses", "anthropic_messages"}:
            raise ValueError("unsupported source protocol")
        super().__init__(**options)
        self.protocol = protocol

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

    def chat(self, body, *, task_id, timeout_s, on_event=None):
        from .chat import validate_chat
        from .protocol_codecs import StreamDecoder, _finish, _usage, make_request, parse_response

        started = time.monotonic()
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise AdapterError("invalid request timeout", code="inference_timeout")
        deadline = started + min(timeout_s, self.timeout_s)
        received_usage = received_finish = None
        decoder = None

        def check():
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            if time.monotonic() >= deadline:
                raise AdapterError("inference timed out", code="inference_timeout")

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
            try:
                payload = make_request({**self.request_defaults, **validated}, self.protocol, self.model, scope)
            except (ValueError, TypeError, KeyError):
                # Unsupported features are rejected before egress. Never use
                # a second protocol as an automatic fallback with task data.
                raise AdapterError("source_protocol_parameters_unsupported", code="source_protocol_parameters_unsupported") from None
            path = "/v1/responses" if self.protocol == "openai_responses" else "/v1/messages"
            request = urllib.request.Request(self._endpoint(path), data=json.dumps(payload).encode("utf-8"),
                                             headers=self._headers(), method="POST")
            with urllib.request.urlopen(request, timeout=max(.001, deadline-time.monotonic())) as response:
                with self._lock:
                    self._active_responses[task_id] = response
                check()
                is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
                if not is_sse:
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ValueError("response limit")
                    decoded = json.loads(raw)
                    if isinstance(decoded, dict):
                        received_usage = _openai_usage(_usage(decoded.get("usage"), self.protocol))
                        try:
                            received_finish = _finish(decoded, self.protocol)
                        except ValueError:
                            pass
                    check()
                    result = parse_response(decoded, self.protocol, self.model, scope)
                else:
                    decoder = StreamDecoder(self.protocol, self.model, scope)
                    # SSE permits multiple data lines and comments. A blank
                    # line closes a frame; socket EOF is never a success marker.
                    total = frame_size = 0
                    data = []

                    def frame():
                        nonlocal received_usage, received_finish
                        value = "\n".join(data)
                        if value == "[DONE]":
                            return
                        event = json.loads(value)
                        chunks = decoder.feed(event)
                        received_usage = _openai_usage(decoder.usage)
                        received_finish = decoder.finish_reason
                        for chunk in chunks:
                            check()
                            deliver(chunk)

                    while True:
                        check()
                        line = response.readline(1024 * 1024 + 1)
                        if not line:
                            if data:
                                frame()
                            break
                        total += len(line)
                        frame_size += len(line)
                        if len(line) > 1024 * 1024 or frame_size > 4 * 1024 * 1024 or total > 32 * 1024 * 1024:
                            raise ValueError("stream limit")
                        line = line.decode("utf-8").rstrip("\r\n")
                        if not line:
                            if data:
                                frame()
                            data = []
                            frame_size = 0
                        elif line.startswith("data:"):
                            data.append(line[5:].removeprefix(" "))
                    result = decoder.result()
            check()
            received_usage = _openai_usage(result.get("usage"))
            received_finish = result.get("finish_reason")
            message = result["message"]
            if not message.get("content") and not message.get("tool_calls"):
                raise ValueError("empty completion")
            duration = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += received_usage["input_tokens"] or 0
                self._metrics.output_tokens += received_usage["output_tokens"] or 0
                self._metrics.unknown_usage_requests += any(v is None for v in received_usage.values())
                self._metrics.total_duration_ms += duration
            return {"text": message.get("content") or "", "message": message,
                    "finish_reason": received_finish, **received_usage,
                    "reported_usage": received_usage, "duration_ms": duration}
        except Exception as exc:
            if decoder is not None:
                received_usage = _openai_usage(decoder.usage)
                received_finish = decoder.finish_reason
            with self._lock:
                self._metrics.failures += 1
            if task_id in self._cancelled:
                code, text = "inference_failed", "task_cancelled"
            elif isinstance(exc, AdapterError):
                code, text = exc.code, str(exc)
            elif isinstance(exc, urllib.error.HTTPError):
                status = exc.code
                exc.close()
                code = text = ("source_protocol_parameters_unsupported" if status in {400, 422}
                               else "upstream_http_failed")
            elif isinstance(exc, OSError):
                timeout = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
                code = text = "inference_timeout" if timeout else "runtime_connection_failed"
            else:
                # Wire/continuation failures are adapter errors, not a prompt
                # for the model to repeat an entire paid tool proposal batch.
                code = text = "source_protocol_response_invalid"
            raise AdapterError(text, code=code, reported_usage=received_usage, finish_reason=received_finish) from None
        finally:
            with self._lock:
                self._active_responses.pop(task_id, None)
                self._cancelled.discard(task_id)
