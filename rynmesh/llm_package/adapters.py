"""Uniform adapters for local OpenAI-compatible and Ollama runtimes."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse


class AdapterError(RuntimeError):
    pass


class LLMAdapter(Protocol):
    def health(self) -> dict[str, Any]: ...
    def models(self) -> list[dict[str, Any]]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def infer(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float) -> dict[str, Any]: ...
    def infer_stream(
        self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float,
        on_delta: Callable[[str], None],
    ) -> dict[str, Any]: ...
    def cancel(self, task_id: str) -> bool: ...
    def metrics(self) -> dict[str, Any]: ...
    def shutdown(self) -> None: ...


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
    total_duration_ms: int = 0


class OpenAICompatibleAdapter:
    def __init__(self, *, base_url: str, model: str = "", api_key_env: str = "",
                 allow_non_loopback: bool = False, timeout_s: float = 120.0) -> None:
        self.base_url = validate_local_url(base_url, allow_non_loopback=allow_non_loopback)
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self._cancelled: set[str] = set()
        self._active_responses: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._metrics = AdapterMetrics()
        self._capabilities_cache: tuple[float, dict[str, Any]] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            secret = os.environ.get(self.api_key_env, "")
            if not secret:
                raise AdapterError(f"API key environment variable {self.api_key_env!r} is not set")
            headers["Authorization"] = "Bearer " + secret
        return headers

    def _json(self, path: str, payload: dict[str, Any] | None, timeout_s: float,
              *, task_id: str = "") -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
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
        except (OSError, urllib.error.HTTPError) as exc:
            raise AdapterError(f"local API request failed ({path}): {exc}") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise AdapterError("local API response exceeded 4 MiB")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("local API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError("local API JSON root must be an object")
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
                return {"ok": False, "error": "configured model not reported", "model_count": len(models)}
            if not self.model:
                self.model = selected
            return {"ok": bool(selected), "model_count": len(models), "model": selected,
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        except AdapterError as exc:
            return {"ok": False, "error": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            cached = self._capabilities_cache
        if cached and time.monotonic() - cached[0] < 300:
            return dict(cached[1])
        if not self.model and not self.health().get("ok"):
            return {"chat_completions": False, "streaming": False, "cancel": "best_effort"}
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
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
        with self._lock:
            self._capabilities_cache = (time.monotonic(), result)
        return dict(result)

    def infer(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float) -> dict[str, Any]:
        if not prompt:
            raise AdapterError("prompt is required")
        if task_id in self._cancelled:
            raise AdapterError("task_cancelled")
        if not self.model and not self.health().get("ok"):
            raise AdapterError("local API has no usable model")
        started = time.monotonic()
        try:
            result = self._json("/v1/chat/completions", {
                "model": self.model, "messages": [{"role": "user", "content": prompt}],
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
            input_tokens = int(usage.get("prompt_tokens") or max(1, len(prompt) // 4))
            output_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
            duration_ms = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += input_tokens
                self._metrics.output_tokens += output_tokens
                self._metrics.total_duration_ms += duration_ms
            return {"text": text, "model": self.model, "input_tokens": input_tokens,
                    "output_tokens": output_tokens, "duration_ms": duration_ms}
        except Exception:
            with self._lock:
                self._metrics.failures += 1
            raise
        finally:
            self._cancelled.discard(task_id)

    def infer_stream(
        self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float,
        on_delta: Callable[[str], None],
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
            self.base_url + "/v1/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
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
                    or message.get("reasoning_content")
                    or (choices[0].get("text") if choices else "")
                    or ""
                )
                if not delta:
                    raise AdapterError("local API returned no completion text")
                on_delta(delta)
                text_parts.append(delta)
                usage = dict(value.get("usage") or {})
            else:
                while True:
                    if task_id in self._cancelled:
                        raise AdapterError("task_cancelled")
                    raw_line = response.readline(256 * 1024 + 1)
                    if not raw_line:
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
                    delta_obj = choice.get("delta") or {}
                    delta_obj = delta_obj if isinstance(delta_obj, dict) else {}
                    delta = str(
                        delta_obj.get("content")
                        or delta_obj.get("reasoning_content")
                        or choice.get("text")
                        or ""
                    )
                    if not delta:
                        continue
                    total_bytes = sum(len(part.encode("utf-8")) for part in text_parts) + len(delta.encode("utf-8"))
                    if total_bytes > 4 * 1024 * 1024:
                        raise AdapterError("local API stream output exceeded 4 MiB")
                    on_delta(delta)
                    text_parts.append(delta)
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            text = "".join(text_parts)
            if not text:
                raise AdapterError("local API returned no completion text")
            input_tokens = int(usage.get("prompt_tokens") or max(1, len(prompt) // 4))
            output_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
            duration_ms = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += input_tokens
                self._metrics.output_tokens += output_tokens
                self._metrics.total_duration_ms += duration_ms
            return {
                "text": text,
                "model": self.model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
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
            raise AdapterError("local API streaming request failed") from exc
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
    kwargs = {
        "base_url": manifest.base_url, "model": manifest.model,
        "api_key_env": manifest.api_key_env, "allow_non_loopback": manifest.allow_non_loopback,
        "timeout_s": manifest.timeout_seconds,
    }
    return OllamaAdapter(**kwargs) if manifest.adapter == "ollama" else OpenAICompatibleAdapter(**kwargs)
