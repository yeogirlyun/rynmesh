"""Uniform adapters for local OpenAI-compatible and Ollama runtimes."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse


def actual_usage(result: Any) -> dict[str, int | None]:
    """Reported metadata wins by presence, including an explicit unknown value.

    Legacy adapters without that marker may provide typed actual top-level
    counters. Estimates must never occupy either of these accounting fields.
    Keep this helper stdlib-only for runtime staging and plain inference.
    """
    result = result if isinstance(result, dict) else {}
    source = result.get("reported_usage") if "reported_usage" in result else result
    source = source if isinstance(source, dict) else {}
    return {key: source.get(key) if type(source.get(key)) is int and source[key] >= 0 else None
            for key in ("input_tokens", "output_tokens")}


def _openai_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    return actual_usage({"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, code: str = "inference_failed", reported_usage=None, finish_reason=None) -> None:
        super().__init__(message)
        self.code = code if code in RUNTIME_ERROR_CODES else "inference_failed"
        self.reported_usage = ({name: reported_usage.get(name) if type(reported_usage.get(name)) is int and reported_usage[name] >= 0 else None
                                for name in ("input_tokens", "output_tokens")} if isinstance(reported_usage, dict) else None)
        self.finish_reason = (finish_reason if isinstance(finish_reason, str) and
                              finish_reason in {"stop", "length", "tool_calls", "content_filter", "cancelled"} else "unknown")


RUNTIME_ERROR_CODES = frozenset({
    "runtime_busy", "model_not_ready", "model_not_found", "runtime_unavailable",
    "runtime_connection_failed", "inference_timeout", "inference_failed",
    "model_configuration_changed", "model_daily_quota_exhausted",
    "ai_permission_denied",
    "stream_delivery_failed", "upstream_http_failed", "upstream_response_invalid",
    "source_protocol_parameters_unsupported",
    "source_protocol_response_invalid",
})


class LLMAdapter(Protocol):
    def health(self) -> dict[str, Any]: ...
    def models(self) -> list[dict[str, Any]]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def infer(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float,
              messages: list[dict[str, str]] | None = None) -> dict[str, Any]: ...
    def infer_stream(
        self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float,
        on_delta: Callable[[str], None], messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]: ...
    def chat(self, body: dict[str, Any], *, task_id: str, timeout_s: float, on_event: Any = None) -> dict[str, Any]: ...
    def cancel(self, task_id: str) -> bool: ...
    def metrics(self) -> dict[str, Any]: ...
    def shutdown(self) -> None: ...


def deadline_options(options, deadline):
    """Consume an optional in-process deadline at the final admission boundary."""
    if deadline is None:
        return options
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise AdapterError("invalid_request_deadline")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AdapterError("request_deadline_exhausted", code="inference_timeout")
    return {**options, "timeout_s": min(options["timeout_s"], remaining)}


def validate_local_url(base_url: str, *, allow_non_loopback: bool = False) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AdapterError("local API URL must be http(s) with a hostname")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise AdapterError("credentials, fragments, and query strings are not allowed in the base URL")
    if allow_non_loopback:
        return cleaned
    host = parsed.hostname.lower()
    if host == "localhost":
        return cleaned
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 80)}
    except OSError as exc:
        raise AdapterError(f"cannot resolve local API host: {exc}") from exc
    if not addresses or any(not ipaddress.ip_address(addr).is_loopback for addr in addresses):
        raise AdapterError("non-loopback local API blocked; use explicit allow_non_loopback only on a trusted LAN")
    return cleaned


@dataclass
class AdapterMetrics:
    requests: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_usage_requests: int = 0
    total_duration_ms: int = 0


class OpenAICompatibleAdapter:
    supports_chat_messages = True

    def __init__(self, *, base_url: str, model: str = "", api_key_env: str = "",
                 allow_non_loopback: bool = False, timeout_s: float = 120.0,
                 api_prefix: str = "/v1", api_key: str = "", request_defaults: dict | None = None) -> None:
        self.base_url = validate_local_url(base_url, allow_non_loopback=allow_non_loopback)
        self.model = model
        self.api_prefix = api_prefix
        self.request_defaults = dict(request_defaults or {})
        self.api_key_env = api_key_env
        # A literal loopback token for a Rynmesh-owned runtime (never logged,
        # never echoed in an error). An owner-configured `api_key_env` wins.
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._cancelled: set[str] = set()
        self._active_responses: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._metrics = AdapterMetrics()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            secret = os.environ.get(self.api_key_env, "")
            if not secret:
                raise AdapterError(f"API key environment variable {self.api_key_env!r} is not set")
            headers["Authorization"] = "Bearer " + secret
        elif self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _endpoint(self, path: str) -> str:
        return self.base_url + (self.api_prefix + path[3:] if path.startswith("/v1/") else path)

    def _json(self, path: str, payload: dict[str, Any] | None, timeout_s: float,
              *, task_id: str = "", classify_errors: bool = False) -> dict[str, Any]:
        if payload is not None:
            payload = {**self.request_defaults, **payload}
        request = urllib.request.Request(
            self._endpoint(path),
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers=self._headers(), method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                if task_id:
                    with self._lock:
                        self._active_responses[task_id] = response
                try:
                    raw = response.read(4 * 1024 * 1024 + 1)
                finally:
                    if task_id:
                        with self._lock:
                            self._active_responses.pop(task_id, None)
        except urllib.error.HTTPError as exc:
            code = "upstream_http_failed" if classify_errors else "inference_failed"
            # Read only a bounded error envelope. Never expose runtime body text,
            # which can contain prompts, paths, or credentials.
            try:
                raw_error = exc.read(16 * 1024 + 1)
                value = json.loads(raw_error) if len(raw_error) <= 16 * 1024 else None
                detail = value.get("detail") if isinstance(value, dict) else None
                if isinstance(detail, str) and detail in RUNTIME_ERROR_CODES:
                    code = detail
            except (OSError, ValueError):
                pass
            finally:
                exc.close()
            raise AdapterError(f"local API request failed: {code}", code=code) from None
        except OSError as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            code = "inference_timeout" if timed_out else "runtime_connection_failed"
            raise AdapterError(f"local API request failed: {code}", code=code) from None
        if len(raw) > 4 * 1024 * 1024:
            raise AdapterError("local API response exceeded 4 MiB")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AdapterError("local API returned invalid JSON",
                               code="upstream_response_invalid" if classify_errors else "inference_failed") from None
        if not isinstance(value, dict):
            raise AdapterError("local API JSON root must be an object",
                               code="upstream_response_invalid" if classify_errors else "inference_failed")
        return value

    def models(self) -> list[dict[str, Any]]:
        result = self._json("/v1/models", None, min(8.0, self.timeout_s))
        entries = result.get("data", result.get("models", []))
        return [dict(item) for item in entries if isinstance(item, dict)]

    def health(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            models = self.models()
            names = [str(item.get("id") or item.get("name") or item.get("model") or "") for item in models]
            selected = self.model or next((name for name in names if name), "")
            if self.model and self.model not in names:
                return {"ok": False, "error": "configured model not reported", "error_code": "model_not_found", "model_count": len(models)}
            if not self.model:
                self.model = selected
            return {"ok": bool(selected), **({"error_code": "model_not_ready"} if not selected else {}), "model_count": len(models), "model": selected,
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        except AdapterError as exc:
            return {"ok": False, "error": str(exc), "error_code": exc.code, "latency_ms": int((time.monotonic() - started) * 1000)}

    def capabilities(self) -> dict[str, Any]:
        if not self.model and not self.health().get("ok"):
            return {"chat_completions": False, "streaming": False, "cancel": "best_effort"}
        request = urllib.request.Request(
            self._endpoint("/v1/chat/completions"),
            data=json.dumps({
                "model": self.model, "messages": [{"role": "user", "content": "Reply: ok"}],
                "max_tokens": 2, "stream": True,
            }).encode("utf-8"),
            headers=self._headers(), method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(15.0, self.timeout_s)) as response:
                sample = response.read(16 * 1024).decode("utf-8", errors="replace")
            streaming = "data:" in sample or "text/event-stream" in sample.lower()
        except (OSError, urllib.error.HTTPError):
            streaming = False
        result = {"chat_completions": True, "streaming": streaming, "cancel": "best_effort"}
        return dict(result)

    def infer(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float,
              messages: list[dict[str, str]] | None = None) -> dict[str, Any]:
        if not prompt:
            raise AdapterError("prompt is required")
        if task_id in self._cancelled:
            raise AdapterError("task_cancelled")
        if not self.model and not self.health().get("ok"):
            raise AdapterError("local API has no usable model")
        started = time.monotonic()
        try:
            result = self._json("/v1/chat/completions", {
                "model": self.model, "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
                "max_tokens": int(max_tokens), "stream": False,
            }, min(float(timeout_s), self.timeout_s), task_id=task_id)
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            choices = result.get("choices") or []
            text = ""
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                text = str(
                    message.get("content")
                    or message.get("reasoning_content")
                    or choices[0].get("text")
                    or ""
                )
            if not text:
                raise AdapterError("local API returned no completion text")
            usage = result.get("usage") or {}
            counts = _openai_usage(usage)
            duration_ms = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += counts["input_tokens"] or 0
                self._metrics.output_tokens += counts["output_tokens"] or 0
                self._metrics.unknown_usage_requests += any(value is None for value in counts.values())
                self._metrics.total_duration_ms += duration_ms
            return {"text": text, "model": self.model, **counts,
                    "reported_usage": counts, "duration_ms": duration_ms}
        except Exception:
            with self._lock:
                self._metrics.failures += 1
            raise
        finally:
            self._cancelled.discard(task_id)

    def chat(self, body: dict[str, Any], *, task_id: str, timeout_s: float,
             on_event: Any = None) -> dict[str, Any]:
        # Runtime staging reads package metadata with stdlib-only Python.
        # Load structured-chat dependencies only when a chat is requested.
        from .chat import ChatAccumulator, validate_chat

        body = {**self.request_defaults, **validate_chat(body)}
        model_id = str(body.get("model") or self.model or "").lower()
        base_url = str(self.base_url or "").lower()
        thinking_cfg = body.get("thinking")
        has_reasoning = (
            any("reasoning_content" in m for m in body["messages"] if m.get("role") == "assistant")
            or (isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled")
            or bool(body.get("reasoning_effort"))
            or "deepseek" in model_id
            or "deepseek" in base_url
        )
        for message in body["messages"]:
            # Native protocol continuation is never a Chat Completions field.
            message.pop("provider_state", None)
            message.pop("is_error", None)
            if has_reasoning and message.get("role") == "assistant" and message.get("reasoning_content") is None:
                message["reasoning_content"] = ""
        if task_id in self._cancelled:
            raise AdapterError("task_cancelled")
        if not self.model and not self.health().get("ok"):
            raise AdapterError("local API has no usable model")
        body["model"] = self.model
        streaming = bool(body.get("stream"))
        if streaming:
            body["stream_options"] = {"include_usage": True}
        started = time.monotonic()
        deadline = started + timeout_s
        received_usage, received_finish = None, None

        def observed_usage(usage):
            # Reported zero is real; estimates, booleans and malformed counters
            # are never evidence for durable accounting after a failed request.
            return _openai_usage(usage)

        def failure(code):
            cancelled = task_id in self._cancelled
            return AdapterError("task_cancelled" if cancelled else code,
                                code="inference_failed" if cancelled else code,
                                reported_usage=received_usage, finish_reason=received_finish)

        try:
            if not streaming:
                raw = self._json("/v1/chat/completions", body, timeout_s, task_id=task_id, classify_errors=True)
                choice = raw["choices"][0]
                message = choice["message"]
                usage = raw.get("usage") or {}
                finish = choice.get("finish_reason") or "stop"
                received_usage, received_finish = observed_usage(usage), choice.get("finish_reason")
            else:
                request = urllib.request.Request(self._endpoint("/v1/chat/completions"),
                                                 data=json.dumps(body).encode(), headers=self._headers())
                accumulator = ChatAccumulator()
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    is_sse = "text/event-stream" in response.headers.get("content-type", "")
                    with self._lock:
                        self._active_responses[task_id] = response
                    if not is_sse:
                        # Some compatible servers ignore stream=true and return
                        # one complete JSON response. Consume that same response;
                        # never retry inference or invent token delta events.
                        raw = response.read(4 * 1024 * 1024 + 1)
                        if len(raw) > 4 * 1024 * 1024:
                            raise AdapterError("local API response exceeded 4 MiB")
                        payload = json.loads(raw)
                        choice = payload["choices"][0]
                        message, usage = choice["message"], payload.get("usage") or {}
                        finish = choice.get("finish_reason") or "stop"
                        received_usage, received_finish = observed_usage(usage), choice.get("finish_reason")
                    total = 0
                    while is_sse:
                        line = response.readline(1024 * 1024 + 1)
                        if not line:
                            break
                        if len(line) > 1024 * 1024:
                            raise AdapterError("stream line exceeds 1 MiB")
                        total += len(line)
                        if total > 32 * 1024 * 1024:
                            raise AdapterError("stream exceeds 32 MiB")
                        if task_id in self._cancelled:
                            raise AdapterError("task_cancelled")
                        if time.monotonic() > deadline:
                            raise AdapterError("inference timed out", code="inference_timeout")
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            break
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise AdapterError("upstream_stream_error")
                        if isinstance(chunk, dict):
                            if "usage" in chunk:
                                fresh_usage = observed_usage(chunk["usage"])
                                received_usage = {name: value if value is not None else (received_usage or {}).get(name)
                                                  for name, value in fresh_usage.items()}
                            choices = chunk.get("choices")
                            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                                received_finish = choices[0].get("finish_reason") or received_finish
                        accumulator.add(chunk)
                        if on_event:
                            try:
                                on_event(chunk)
                            except AdapterError:
                                raise
                            except Exception:
                                # This is downstream delivery, not an upstream
                                # HTTP/read/parse failure. Never expose callback text.
                                raise failure("stream_delivery_failed") from None
                if is_sse:
                    if not accumulator.finished:
                        raise AdapterError("upstream_stream_truncated")
                    message, usage, finish = accumulator.message(), accumulator.usage, accumulator.finish_reason
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            if time.monotonic() > deadline:
                raise AdapterError("inference timed out", code="inference_timeout")
            if not message.get("content") and not message.get("tool_calls") and not message.get("reasoning_content"):
                raise AdapterError("upstream_empty_completion")
            counts = observed_usage(usage)
            return {"text": message.get("content") or "", "message": message, "finish_reason": finish,
                    **counts, "reported_usage": counts,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except AdapterError as exc:
            if received_usage is not None:
                exc.reported_usage = {name: (exc.reported_usage or {}).get(name)
                    if (exc.reported_usage or {}).get(name) is not None else received_usage.get(name)
                    for name in ("input_tokens", "output_tokens")}
            if (exc.finish_reason == "unknown" and isinstance(received_finish, str)
                    and received_finish in {"stop", "length", "tool_calls", "content_filter", "cancelled"}):
                exc.finish_reason = received_finish
            if task_id in self._cancelled and str(exc) != "task_cancelled":
                raise AdapterError("task_cancelled", reported_usage=exc.reported_usage,
                                   finish_reason=exc.finish_reason) from None
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            raise failure("upstream_http_failed") from None
        except OSError as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            raise failure("inference_timeout" if timed_out else "runtime_connection_failed") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise failure("upstream_response_invalid") from None
        finally:
            with self._lock:
                self._active_responses.pop(task_id, None)
                self._cancelled.discard(task_id)


    def infer_stream(
        self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float,
        on_delta: Callable[[str], None], messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Stream OpenAI-compatible SSE deltas and return final usage once.

        Raw frames and generated text never enter exception messages.  A
        runtime that answers the streaming request with ordinary JSON is
        safely treated as one delta, without issuing a second inference.
        """
        if not prompt:
            raise AdapterError("prompt is required")
        if task_id in self._cancelled:
            raise AdapterError("task_cancelled")
        if not self.model and not self.health().get("ok"):
            raise AdapterError("local API has no usable model")
        request = urllib.request.Request(
            self._endpoint("/v1/chat/completions"),
            data=json.dumps({
                "model": self.model,
                "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
                "max_tokens": int(max_tokens),
                "stream": True,
                "stream_options": {"include_usage": True},
            }).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        started = time.monotonic()
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        output_bytes = 0
        finished = False
        response: Any = None
        try:
            response = urllib.request.urlopen(
                request, timeout=min(float(timeout_s), self.timeout_s),
            )
            with self._lock:
                self._active_responses[task_id] = response
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if "application/json" in content_type and "event-stream" not in content_type:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise AdapterError("local API response exceeded 4 MiB")
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise AdapterError("local API returned invalid JSON") from None
                if not isinstance(value, dict):
                    raise AdapterError("local API JSON root must be an object")
                choices = value.get("choices") or []
                message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
                message = message if isinstance(message, dict) else {}
                delta = str(
                    message.get("content")
                    or (choices[0].get("text") if choices else "")
                    or ""
                )
                if not delta:
                    raise AdapterError("local API returned no completion text")
                on_delta(delta)
                text_parts.append(delta)
                usage = value.get("usage")
            else:
                while True:
                    if task_id in self._cancelled:
                        raise AdapterError("task_cancelled")
                    if time.monotonic() - started > timeout_s:
                        raise AdapterError("local API streaming timed out", code="inference_timeout")
                    raw_line = response.readline(256 * 1024 + 1)
                    if not raw_line:
                        if not finished:
                            raise AdapterError("local API stream ended before completion")
                        break
                    if len(raw_line) > 256 * 1024:
                        raise AdapterError("local API stream event exceeded 256 KiB")
                    try:
                        line = raw_line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        raise AdapterError("local API stream returned invalid UTF-8") from None
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        raise AdapterError("local API stream framing is invalid")
                    data = line[5:].lstrip()
                    if data == "[DONE]":
                        finished = True
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        raise AdapterError("local API stream event is invalid") from None
                    if not isinstance(event, dict):
                        raise AdapterError("local API stream event is not an object")
                    if isinstance(event.get("usage"), dict):
                        usage = dict(event["usage"])
                    choices = event.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason") is not None:
                        finished = True
                    delta_obj = choice.get("delta") or {}
                    delta_obj = delta_obj if isinstance(delta_obj, dict) else {}
                    delta = str(
                        delta_obj.get("content")
                        or choice.get("text")
                        or ""
                    )
                    if not delta:
                        continue
                    output_bytes += len(delta.encode("utf-8"))
                    if output_bytes > 4 * 1024 * 1024:
                        raise AdapterError("local API stream output exceeded 4 MiB")
                    on_delta(delta)
                    text_parts.append(delta)
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            text = "".join(text_parts)
            if not text:
                raise AdapterError("local API returned no completion text")
            counts = _openai_usage(usage)
            duration_ms = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += counts["input_tokens"] or 0
                self._metrics.output_tokens += counts["output_tokens"] or 0
                self._metrics.unknown_usage_requests += any(value is None for value in counts.values())
                self._metrics.total_duration_ms += duration_ms
            return {
                "text": text,
                "model": self.model,
                **counts, "reported_usage": counts,
                "duration_ms": duration_ms,
            }
        except AdapterError:
            with self._lock:
                self._metrics.failures += 1
            raise
        except (OSError, urllib.error.HTTPError) as exc:
            with self._lock:
                self._metrics.failures += 1
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled") from None
            code = "runtime_connection_failed"
            if isinstance(exc, urllib.error.HTTPError):
                code = "inference_failed"
                try:
                    raw_error = exc.read(16 * 1024 + 1)
                    value = json.loads(raw_error) if len(raw_error) <= 16 * 1024 else None
                    detail = value.get("detail") if isinstance(value, dict) else None
                    if isinstance(detail, str) and detail in RUNTIME_ERROR_CODES:
                        code = detail
                except (OSError, ValueError):
                    pass
                finally:
                    exc.close()
            elif isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                code = "inference_timeout"
            raise AdapterError("local API streaming request failed", code=code) from None
        finally:
            with self._lock:
                self._active_responses.pop(task_id, None)
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
            self._cancelled.discard(task_id)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            self._cancelled.add(task_id)
            response = self._active_responses.get(task_id)
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        return True

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return vars(self._metrics).copy()

    def shutdown(self) -> None:
        self._cancelled.clear()


class OllamaAdapter(OpenAICompatibleAdapter):
    """Ollama adapter using its OpenAI-compatible API plus native tag probing."""

    def models(self) -> list[dict[str, Any]]:
        result = self._json("/api/tags", None, min(8.0, self.timeout_s))
        return [dict(item) for item in result.get("models", []) if isinstance(item, dict)]

    def capabilities(self) -> dict[str, Any]:
        detected = super().capabilities()
        return {**detected, "ollama": True}


def adapter_from_manifest(manifest: Any) -> LLMAdapter:
    if manifest.adapter in {"codex_cli", "claude_cli", "antigravity_cli"}:
        from .cli_adapter import CLIAgentAdapter
        return CLIAgentAdapter(manifest.adapter)
    kwargs = {
        "base_url": manifest.base_url, "model": manifest.model,
        "api_key_env": manifest.api_key_env,
        "api_key": str(getattr(manifest, "runtime_api_key", "") or ""),
        "allow_non_loopback": manifest.allow_non_loopback,
        "timeout_s": manifest.timeout_seconds,
    }
    return OllamaAdapter(**kwargs) if manifest.adapter == "ollama" else OpenAICompatibleAdapter(**kwargs)
