"""Loopback OpenAI-compatible runtime for local Transformers models.

This exists for GPU providers that cannot run the bundled Ollama/llama.cpp
binary but already have official Hugging Face/ModelScope weights available.
The process must stay on loopback; remote Consumers use the encrypted Rynmesh
LLM package instead of reaching this runtime directly.
Weights are validated at startup; inference is admitted one request at a time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import queue
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .llm_package.chat import ChatAccumulator, validate_chat


class ThinkingSplitter:
    """Separate Qwen reasoning from answer text even across partial markers."""

    def __init__(self, enabled: bool):
        self.thinking = enabled
        self.initial = enabled
        self.pending = ""

    def feed(self, text: str, *, final: bool = False) -> tuple[str, str]:
        if not self.thinking:
            return "", text
        self.pending += text
        if self.initial:
            stripped = self.pending.lstrip()
            if not final and "<think>".startswith(stripped):
                return "", ""
            if stripped.startswith("<think>"):
                self.pending = stripped[len("<think>"):]
            self.initial = False
        marker = "</think>"
        if marker in self.pending:
            reasoning, answer = self.pending.split(marker, 1)
            self.pending = ""
            self.thinking = False
            return reasoning, answer
        keep = 0 if final else len(marker) - 1
        count = max(0, len(self.pending) - keep)
        reasoning, self.pending = self.pending[:count], self.pending[count:]
        return reasoning, ""


def template_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = json.loads(json.dumps(messages))
    for message in cleaned:
        # Qwen's conversation history uses final answers, without old reasoning.
        message.pop("reasoning_content", None)
        message.pop("provider_state", None)
        # OpenAI uses null for tool-only assistant messages; Qwen's Jinja
        # template performs string membership checks and requires empty text.
        message["content"] = message.get("content") or ""
        if message["role"] == "developer":
            message["role"] = "system"
        for call in message.get("tool_calls", []):
            raw_args = call["function"]["arguments"]
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                    if not isinstance(parsed_args, dict):
                        parsed_args = {"_raw_arguments": raw_args[:4000]}
                except (ValueError, TypeError):
                    parsed_args = {"_raw_arguments": raw_args[:4000]}
                call["function"]["arguments"] = parsed_args
    return cleaned


class RuntimeBusyError(RuntimeError):
    """Raised when the single-slot GPU generation lock is already held."""


class ContextLengthError(ValueError):
    """Raised when input tokens plus max_tokens exceed the runtime context window."""

    def __init__(self, input_tokens: int, context_window: int) -> None:
        super().__init__(
            f"runtime context window exceeded ({input_tokens} tokens, limit {context_window})"
        )
        self.input_tokens = input_tokens
        self.context_window = context_window


def _parse_tool_call_payload(raw_text: str) -> tuple[str, str]:
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("name"), str) and parsed["name"].strip():
        arguments = parsed.get("arguments", {})
        serialized = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        return parsed["name"], serialized
    name = "malformed_tool_call"
    match = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text)
    if match:
        candidate = match.group(1)
        try:
            decoded = json.loads('"' + candidate + '"')
        except ValueError:
            decoded = candidate
        if isinstance(decoded, str) and decoded.strip():
            name = decoded
    return name, raw_text


def _earliest_stop(text: str, stop_sequences: list[str]) -> int:
    best = -1
    for seq in stop_sequences:
        idx = text.find(seq)
        if idx >= 0 and (best < 0 or idx < best):
            best = idx
    return best


class TransformersChatRuntime:
    def __init__(self, model_path: str, *, model_id: str = "Qwen3-14B-Q4") -> None:
        self.model_path = model_path
        self.model_id = model_id
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._load_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._load_error = ""

    def generation_limits(self) -> tuple[int, int, float]:
        raw_native = getattr(getattr(self._model, "config", None), "max_position_embeddings", None)
        native = raw_native if type(raw_native) is int and raw_native > 0 else None
        env_ctx = os.environ.get("RYNMESH_TRANSFORMERS_CONTEXT")
        if env_ctx is not None and env_ctx.strip() != "":
            context = int(env_ctx)
            if native is not None:
                context = min(context, native)
        else:
            context = native if native is not None else 8192
            try:
                import torch

                cuda = getattr(torch, "cuda", None)
                if (
                    cuda is not None
                    and callable(getattr(cuda, "mem_get_info", None))
                    and (not callable(getattr(cuda, "is_available", None)) or cuda.is_available())
                ):
                    free_bytes, _ = cuda.mem_get_info()
                    vram_cap = max(
                        min(context, 2048),
                        max(0, int(free_bytes) - 512 * 1024**2) // (256 * 1024),
                    )
                    context = min(context, vram_cap)
            except ImportError:
                context = context
        env_out = os.environ.get("RYNMESH_TRANSFORMERS_MAX_OUTPUT")
        if env_out is not None and env_out.strip() != "":
            output = min(context, int(env_out))
        else:
            output = min(context, max(1, min(context // 4, 8192)))
        timeout = float(os.environ.get("RYNMESH_TRANSFORMERS_TIMEOUT", "120"))
        if context < 1 or output < 1 or not 0 < timeout <= 86400:
            raise ValueError("invalid runtime generation limits")
        return context, output, timeout

    def validate_length(self, input_tokens: int, max_tokens: int) -> None:
        context, output, _ = self.generation_limits()
        if input_tokens >= context:
            raise ContextLengthError(input_tokens, context)
        if not 1 <= max_tokens <= output:
            raise ValueError(f"max_tokens exceeds runtime limit of {output}")
        if input_tokens + max_tokens > context:
            raise ContextLengthError(input_tokens, context)

    def prefill(self, encoded, stopped, deadline):
        """Bound attention working memory while building a long prompt's KV cache."""
        length = int(encoded["input_ids"].shape[-1])
        if length <= 512:
            return None
        cache = None
        # Leave the final prompt token for generate(), which reuses this cache.
        for start in range(0, length - 1, 512):
            if stopped.is_set():
                raise RuntimeError("generation_cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("generation_timed_out")
            end = min(start + 512, length - 1)
            output = self._model(input_ids=encoded["input_ids"][:, start:end],
                                 attention_mask=encoded["attention_mask"][:, :end],
                                 past_key_values=cache, use_cache=True, logits_to_keep=1)
            cache = output.past_key_values
        return cache

    def load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            if not (Path(self.model_path) / "config.json").is_file():
                raise RuntimeError("model_configuration_missing")
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            except ImportError as exc:
                raise RuntimeError("transformers_runtime_dependencies_missing") from exc
            if not torch.cuda.is_available():
                raise RuntimeError("cuda_unavailable")
            quantization = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.float16,
            )
            tokenizer = AutoTokenizer.from_pretrained(
                self.model_path, local_files_only=True, trust_remote_code=False
            )
            model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                local_files_only=True,
                trust_remote_code=False,
                quantization_config=quantization,
                device_map={"": 0},
                torch_dtype=torch.float16,
                low_cpu_mem_usage=True,
            )
            model.eval()
            self._tokenizer = tokenizer
            self._model = model
            self._load_error = ""

    def health(self) -> dict[str, Any]:
        try:
            import torch

            return {
                "ok": self._model is not None and torch.cuda.is_available(),
                "model": self.model_id,
                "loaded": self._model is not None,
                "torch": torch.__version__,
                "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
                "error": self._load_error or ("" if self._model is not None else "model_not_loaded"),
            }
        except ImportError:
            return {"ok": False, "model": self.model_id, "error": "torch_not_installed"}

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> dict[str, Any]:
        # Admit before loading or allocating CUDA tensors. Concurrent requests
        # must not accumulate GPU allocations while waiting for generation.
        if not self._generation_lock.acquire(blocking=False):
            raise RuntimeBusyError("runtime_busy")
        try:
            return self._complete(messages, max_tokens=max_tokens)
        finally:
            self._generation_lock.release()

    def _complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> dict[str, Any]:
        self.load()
        import torch

        cleaned = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in messages
            if isinstance(item, dict)
        ]
        if not cleaned or not cleaned[-1]["content"]:
            raise ValueError("messages_required")
        max_tokens = int(max_tokens or 256)
        try:
            rendered = self._tokenizer.apply_chat_template(
                cleaned,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        except TypeError:
            rendered = self._tokenizer.apply_chat_template(
                cleaned, tokenize=False, add_generation_prompt=True
            )
        encoded = self._tokenizer(rendered, return_tensors="pt")
        input_tokens = int(encoded["input_ids"].shape[-1])
        self.validate_length(input_tokens, max_tokens)
        encoded = {key: value.to("cuda") for key, value in encoded.items()}
        started = time.monotonic()
        with torch.inference_mode():
            timeout = self.generation_limits()[2]
            cache = self.prefill(encoded, threading.Event(), started + timeout)
            output = self._model.generate(
                **encoded,
                past_key_values=cache,
                max_new_tokens=max_tokens,
                max_time=max(0.01, timeout - (time.monotonic() - started)),
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        generated = output[0, input_tokens:]
        text = self._tokenizer.decode(generated, skip_special_tokens=True).strip()
        return {
            "text": text,
            "input_tokens": input_tokens,
            "output_tokens": int(generated.shape[-1]),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def stream_chat(
        self,
        body: dict[str, Any],
        *,
        stopped: threading.Event | None = None,
        _lock_acquired: bool = False,
    ):
        """Real token streaming with Qwen's native tool-call chat template."""
        if not _lock_acquired and not self._generation_lock.acquire(blocking=False):
            raise RuntimeBusyError("runtime_busy")
        release_guard = threading.Lock()
        released = False

        def release_lock() -> None:
            nonlocal released
            with release_guard:
                if not released:
                    released = True
                    self._generation_lock.release()

        try:
            self.load()
            import torch
            from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

            body = validate_chat(body)
            raw_stop = body.get("stop")
            stop_sequences = [
                seq
                for seq in ([raw_stop] if isinstance(raw_stop, str) else (raw_stop or []))
                if isinstance(seq, str) and seq
            ]
            messages = template_messages(body["messages"])
            choice = body.get("tool_choice", "auto")
            tools = body.get("tools") if choice != "none" else None
            if tools and choice != "auto":
                instruction = "You must call a provided tool in your next response."
                if isinstance(choice, dict):
                    instruction = (
                        "You must call the tool named "
                        + choice["function"]["name"]
                        + " in your next response."
                    )
                messages.insert(0, {"role": "system", "content": instruction})
            rendered = self._tokenizer.apply_chat_template(
                messages,
                tools=tools,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=body.get("enable_thinking", False),
            )
            encoded = self._tokenizer(rendered, return_tensors="pt")
            input_tokens = int(encoded["input_ids"].shape[-1])
            self.validate_length(input_tokens, body["max_tokens"])
            timeout_s = self.generation_limits()[2]
        except BaseException:
            release_lock()
            raise

        stopped = stopped or threading.Event()
        stop_hit = threading.Event()
        deadline = time.monotonic() + timeout_s
        outcome: dict[str, Any] = {}
        streamer = TextIteratorStreamer(
            self._tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=0.2
        )
        enable_thinking = bool(body.get("enable_thinking", False))
        tokenizer = self._tokenizer

        class Cancelled(StoppingCriteria):
            def __call__(self, *_args, **_kwargs):
                return stopped.is_set() or stop_hit.is_set() or time.monotonic() >= deadline

        class StopOnSequences(StoppingCriteria):
            def __call__(self, input_ids, *_args, **_kwargs):
                if not stop_sequences:
                    return False
                generated_ids = input_ids[0, input_tokens:]
                if int(generated_ids.shape[-1]) <= 0:
                    return False
                decoded = tokenizer.decode(generated_ids, skip_special_tokens=True)
                if enable_thinking:
                    if "</think>" not in decoded:
                        return False
                    decoded = decoded.split("</think>", 1)[1]
                if any(seq in decoded for seq in stop_sequences):
                    stop_hit.set()
                    return True
                return False

        criteria = [Cancelled()]
        if stop_sequences:
            criteria.append(StopOnSequences())

        def generate():
            try:
                temperature = body.get("temperature", 0)
                options = {"do_sample": temperature > 0}
                if temperature > 0:
                    options.update(temperature=temperature, top_p=body.get("top_p", 1))
                with torch.inference_mode():
                    gpu_inputs = {k: v.to("cuda") for k, v in encoded.items()}
                    cache = self.prefill(gpu_inputs, stopped, deadline)
                    result = self._model.generate(
                        **gpu_inputs,
                        past_key_values=cache,
                        max_new_tokens=body["max_tokens"],
                        streamer=streamer,
                        stopping_criteria=StoppingCriteriaList(criteria),
                        pad_token_id=self._tokenizer.eos_token_id,
                        **options,
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError("generation_timed_out")
                if stopped.is_set() and not stop_hit.is_set():
                    raise RuntimeError("generation_cancelled")
                outcome["tokens"] = int(result.shape[-1]) - input_tokens
            except Exception as exc:
                outcome["error"] = exc
            finally:
                streamer.end()
                release_lock()

        def _iter():
            worker = threading.Thread(target=generate, daemon=True)
            worker.start()
            pending = ""
            thinking = ThinkingSplitter(enable_thinking)
            in_tool = False
            calls = 0
            ident = "chatcmpl-" + uuid.uuid4().hex
            stopped_by_seq = False

            def chunk(delta, finish=None, usage=None):
                value = {
                    "id": ident,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                if usage is not None:
                    value["usage"] = usage
                return value

            try:
                yield chunk({"role": "assistant"})
                while True:
                    try:
                        piece = next(streamer)
                    except queue.Empty:
                        if not worker.is_alive():
                            break
                        continue
                    except StopIteration:
                        break
                    reasoning, answer = thinking.feed(piece)
                    if reasoning:
                        yield chunk({"reasoning_content": reasoning})
                    pending += answer
                    while pending:
                        marker = "</tool_call>" if in_tool else "<tool_call>"
                        if not in_tool and stop_sequences:
                            stop_pos = _earliest_stop(pending, stop_sequences)
                            tool_pos = pending.find(marker)
                            if stop_pos >= 0 and (tool_pos < 0 or stop_pos < tool_pos):
                                pending = pending[:stop_pos]
                                stopped_by_seq = True
                                stop_hit.set()
                                break
                        pos = pending.find(marker)
                        if pos < 0:
                            hold = max(
                                [len(marker), *(len(s) - 1 for s in stop_sequences if len(s) > 1)]
                            )
                            if not in_tool and len(pending) > hold:
                                yield chunk({"content": pending[:-hold]})
                                pending = pending[-hold:]
                            break
                        prefix, pending = pending[:pos], pending[pos + len(marker):]
                        if in_tool:
                            tool_name, tool_args = _parse_tool_call_payload(prefix.strip())
                            yield chunk({
                                "tool_calls": [{
                                    "index": calls,
                                    "id": "call_" + uuid.uuid4().hex,
                                    "type": "function",
                                    "function": {"name": tool_name, "arguments": tool_args},
                                }]
                            })
                            calls += 1
                        elif prefix:
                            yield chunk({"content": prefix})
                        in_tool = not in_tool
                    if stopped_by_seq:
                        break
                worker.join(timeout=5)
                if "error" in outcome:
                    err = outcome["error"]
                    if isinstance(err, ContextLengthError):
                        raise err
                    if isinstance(err, TimeoutError):
                        raise TimeoutError("generation_timeout") from err
                    if str(err) == "generation_cancelled" or (stopped.is_set() and not stop_hit.is_set()):
                        raise RuntimeError("generation_cancelled") from err
                    raise RuntimeError("generation_failed") from err
                if stopped.is_set() and not stop_hit.is_set() and not stopped_by_seq:
                    raise RuntimeError("generation_cancelled")
                reasoning, extra_answer = thinking.feed("", final=True)
                if reasoning:
                    yield chunk({"reasoning_content": reasoning})
                if extra_answer and not stopped_by_seq:
                    pending += extra_answer
                    if not in_tool and stop_sequences:
                        stop_pos = _earliest_stop(pending, stop_sequences)
                        if stop_pos >= 0:
                            pending = pending[:stop_pos]
                            stopped_by_seq = True
                incomplete_tool = False
                if in_tool:
                    incomplete_tool = True
                    yield chunk({"content": "<tool_call>" + pending})
                    pending = ""
                elif pending and (not calls or pending.strip()):
                    yield chunk({"content": re.sub(r"^\s+", "", pending) if calls else pending})
                count = outcome.get("tokens", 0)
                if incomplete_tool:
                    finish_reason = "length"
                elif calls:
                    finish_reason = "tool_calls"
                elif stopped_by_seq or stop_hit.is_set():
                    finish_reason = "stop"
                elif count >= body["max_tokens"]:
                    finish_reason = "length"
                else:
                    finish_reason = "stop"
                yield chunk({}, finish_reason)
                usage = {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": count,
                    "total_tokens": input_tokens + count,
                }
                yield {
                    "id": ident,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [],
                    "usage": usage,
                }
            finally:
                stopped.set()
                worker.join(timeout=2)
                release_lock()

        return _iter()


def _stream_error_code(exc: BaseException, stopped: threading.Event) -> str:
    if isinstance(exc, ContextLengthError) or isinstance(exc.__cause__, ContextLengthError):
        return "context_exceeded"
    if isinstance(exc, TimeoutError) or isinstance(exc.__cause__, TimeoutError) or str(exc) == "generation_timeout":
        return "generation_timeout"
    if str(exc) == "generation_cancelled" or stopped.is_set():
        return "generation_cancelled"
    return "generation_failed"


def _invoke_stream_chat(runtime: Any, validated: dict[str, Any], *, stopped: threading.Event, lock_acquired: bool):
    params = inspect.signature(runtime.stream_chat).parameters
    kwargs: dict[str, Any] = {}
    if "stopped" in params:
        kwargs["stopped"] = stopped
    if "_lock_acquired" in params:
        kwargs["_lock_acquired"] = lock_acquired
    return runtime.stream_chat(validated, **kwargs)


def create_app(*, runtime: TransformersChatRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi import Request as FastAPIRequest
        from fastapi.responses import JSONResponse, StreamingResponse
    except ImportError as exc:  # pragma: no cover
        raise ImportError("transformers LLM runtime requires fastapi") from exc
    globals()["FastAPIRequest"] = FastAPIRequest
    from starlette.concurrency import run_in_threadpool

    runtime = runtime or TransformersChatRuntime(
        os.environ.get("RYNMESH_TRANSFORMERS_MODEL_PATH", "/model/ModelScope/Qwen/Qwen3-14B"),
        model_id=os.environ.get("RYNMESH_TRANSFORMERS_MODEL_ID", "Qwen3-14B-Q4"),
    )

    def _busy_http_error() -> HTTPException:
        return HTTPException(status_code=503, detail="runtime_busy", headers={"Retry-After": "5"})

    def _context_error_json(exc: ContextLengthError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "context_length_exceeded",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                    "context_window": exc.context_window,
                }
            },
        )

    @asynccontextmanager
    async def lifespan(_app):
        # Validate weights/dependencies before advertising the model. Failure
        # leaves a live, degraded process that can report its readiness state.
        try:
            await run_in_threadpool(runtime.load)
        except Exception as exc:
            runtime._load_error = f"model_load_failed:{type(exc).__name__}"
        yield

    app = FastAPI(title="Rynmesh loopback Transformers LLM", version="0.1", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        if not runtime.health().get("ok"):
            raise HTTPException(status_code=503, detail="model_not_ready")
        entry: dict[str, Any] = {"id": runtime.model_id, "object": "model"}
        if hasattr(runtime, "generation_limits"):
            context_window, max_output_tokens, _ = runtime.generation_limits()
            entry["context_window"] = context_window
            entry["max_output_tokens"] = max_output_tokens
        return {"object": "list", "data": [entry]}

    @app.post("/v1/chat/completions")
    async def chat(request: FastAPIRequest) -> Any:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request_body_must_be_an_object")
        structured_chat = body.get("stream") or any(
            key in body for key in ("tools", "tool_choice", "enable_thinking", "temperature", "top_p", "stop")
        ) or any(item.get("tool_calls") or item.get("role") == "tool" for item in body.get("messages", []) if isinstance(item, dict))
        if hasattr(runtime, "stream_chat") and structured_chat:
            if body.get("model") not in {None, "", runtime.model_id}:
                raise HTTPException(status_code=404, detail="model_not_found")
            try:
                validated = validate_chat({k: v for k, v in body.items() if k not in {"model", "stream_options"}})
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            if validated.get("stream"):
                gen_lock = getattr(runtime, "_generation_lock", None)
                pre_acquired = False
                if gen_lock is not None:
                    if not gen_lock.acquire(blocking=False):
                        raise _busy_http_error()
                    pre_acquired = True
                stopped = threading.Event()
                try:
                    stream_iter = await run_in_threadpool(
                        lambda: _invoke_stream_chat(
                            runtime, validated, stopped=stopped, lock_acquired=pre_acquired
                        )
                    )
                except ContextLengthError as exc:
                    return _context_error_json(exc)
                except RuntimeBusyError as exc:
                    raise _busy_http_error() from exc
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except Exception as exc:
                    raise HTTPException(status_code=503, detail="runtime_unavailable") from exc

                loop = asyncio.get_running_loop()
                out_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

                def pump_stream() -> None:
                    try:
                        for value in stream_iter:
                            loop.call_soon_threadsafe(out_queue.put_nowait, ("chunk", value))
                        loop.call_soon_threadsafe(out_queue.put_nowait, ("done", None))
                    except BaseException as exc:
                        loop.call_soon_threadsafe(out_queue.put_nowait, ("error", exc))

                pump_thread = threading.Thread(target=pump_stream, daemon=True)
                pump_thread.start()

                async def chunks():
                    try:
                        while True:
                            try:
                                kind, payload = await asyncio.wait_for(out_queue.get(), timeout=0.2)
                            except asyncio.TimeoutError:
                                if await request.is_disconnected():
                                    stopped.set()
                                    break
                                continue
                            if kind == "chunk":
                                yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                            elif kind == "done":
                                yield "data: [DONE]\n\n"
                                break
                            else:
                                code = _stream_error_code(payload, stopped)
                                err_obj = {"error": {"type": "api_error", "code": code, "message": code}}
                                yield "data: " + json.dumps(err_obj, ensure_ascii=False) + "\n\n"
                                break
                    finally:
                        stopped.set()

                return StreamingResponse(chunks(), media_type="text/event-stream")

            stopped = threading.Event()

            def complete_chat():
                accumulator = ChatAccumulator()
                for value in _invoke_stream_chat(runtime, validated, stopped=stopped, lock_acquired=False):
                    accumulator.add(value)
                return {
                    "id": "chatcmpl-" + uuid.uuid4().hex,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": runtime.model_id,
                    "choices": [{"index": 0, "message": accumulator.message(), "finish_reason": accumulator.finish_reason}],
                    "usage": accumulator.usage,
                }

            work_task = asyncio.ensure_future(run_in_threadpool(complete_chat))

            async def watch_disconnect() -> None:
                while not work_task.done():
                    if await request.is_disconnected():
                        stopped.set()
                        return
                    await asyncio.sleep(0.5)

            watcher = asyncio.create_task(watch_disconnect())
            try:
                return await work_task
            except ContextLengthError as exc:
                return _context_error_json(exc)
            except RuntimeBusyError as exc:
                raise _busy_http_error() from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=503, detail="runtime_unavailable") from exc
            finally:
                stopped.set()
                watcher.cancel()
        if body.get("stream"):
            raise HTTPException(status_code=400, detail="streaming_not_supported")
        if body.get("model") not in {None, "", runtime.model_id}:
            raise HTTPException(status_code=404, detail="model_not_found")
        try:
            result = await run_in_threadpool(
                runtime.complete, list(body.get("messages") or []),
                max_tokens=int(body.get("max_tokens") or 256),
            )
        except ContextLengthError as exc:
            return _context_error_json(exc)
        except RuntimeBusyError as exc:
            raise _busy_http_error() from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid_completion_request") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="runtime_unavailable") from exc
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": runtime.model_id,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": result["text"]}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": result["input_tokens"],
                "completion_tokens": result["output_tokens"],
                "total_tokens": result["input_tokens"] + result["output_tokens"],
            },
            "rynmesh": {"duration_ms": result["duration_ms"]},
        }

    return app


def main() -> int:
    import uvicorn

    host = os.environ.get("RYNMESH_TRANSFORMERS_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("transformers runtime must bind to loopback")
    uvicorn.run(
        "rynmesh.llm_runtime_server:create_app",
        factory=True,
        host=host,
        port=int(os.environ.get("RYNMESH_TRANSFORMERS_PORT", "8080")),
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
