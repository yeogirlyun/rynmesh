"""Tests for S01-I13 (T1..T9, T13): local Transformers runtime & catalog."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import rynmesh.llm_package.catalog as llm_catalog
import rynmesh.llm_package.hardware as llm_hardware
import rynmesh.llm_runtime_server as runtime_server
from rynmesh.llm_package.chat import ChatAccumulator
from rynmesh.llm_runtime_server import (
    RuntimeBusyError,
    TransformersChatRuntime,
    create_app,
    template_messages,
)


class _FakeTensor:
    def __init__(self, ids: list[int]) -> None:
        self._ids = list(ids)
        self.shape = (1, len(self._ids))

    def to(self, _device: str) -> "_FakeTensor":
        return self

    def __getitem__(self, key: Any) -> "_FakeTensor":
        if isinstance(key, tuple) and len(key) == 2:
            _, col = key
            if isinstance(col, slice):
                return _FakeTensor(self._ids[col])
            return _FakeTensor([self._ids[col]])
        return self


class _FakeStoppingCriteria:
    def __call__(self, input_ids: Any, scores: Any = None, **kwargs: Any) -> bool:
        return False


class _FakeStoppingCriteriaList(list):
    def __call__(self, input_ids: Any, scores: Any = None, **kwargs: Any) -> bool:
        return any(item(input_ids, scores, **kwargs) for item in self)


class _FakeTextIteratorStreamer:
    def __init__(self, tokenizer: Any, skip_prompt: bool = True, skip_special_tokens: bool = True, timeout: float | None = None) -> None:
        import queue

        self._tokenizer = tokenizer
        self._timeout = timeout
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._done = False

    def put_text(self, piece: str) -> None:
        self._queue.put(piece)

    def end(self) -> None:
        if not self._done:
            self._done = True
            self._queue.put(None)

    def __iter__(self) -> "_FakeTextIteratorStreamer":
        return self

    def __next__(self) -> str:
        item = self._queue.get(timeout=self._timeout)
        if item is None:
            raise StopIteration
        return item


class _FakeTokenizer:
    eos_token_id = 0

    def __init__(self, prompt_tokens: int = 12) -> None:
        self.prompt_tokens = prompt_tokens
        self.last_messages: list[dict[str, Any]] | None = None
        self._piece_table: dict[int, str] = {}

    def register_pieces(self, pieces: list[str]) -> list[int]:
        token_ids: list[int] = []
        for piece in pieces:
            tid = len(self._piece_table) + 100
            self._piece_table[tid] = piece
            token_ids.append(tid)
        return token_ids

    def apply_chat_template(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        # Ensure Jinja-like JSON/mapping serialization succeeds on tool_calls arguments.
        self.last_messages = json.loads(json.dumps(messages))
        for msg in self.last_messages:
            for tc in msg.get("tool_calls", []):
                args = tc["function"]["arguments"]
                assert isinstance(args, dict), "template_messages must supply dict arguments to template"
        return json.dumps({"messages": self.last_messages, "tools": kwargs.get("tools")})

    def __call__(self, rendered: str, return_tensors: str = "pt") -> dict[str, _FakeTensor]:
        ids = [1] * self.prompt_tokens
        return {"input_ids": _FakeTensor(ids), "attention_mask": _FakeTensor([1] * len(ids))}

    def decode(self, token_ids: _FakeTensor, skip_special_tokens: bool = True) -> str:
        return "".join(self._piece_table.get(tid, "") for tid in token_ids._ids)


class _InferenceMode:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: Any) -> None:
        return None


def _install_fake_ml_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    free_vram_bytes: int = 24 * 1024**3,
    total_vram_bytes: int = 24 * 1024**3,
    allocated_vram_bytes: int = 4 * 1024**3,
    reserved_vram_bytes: int = 4 * 1024**3,
):
    state = {
        "free": free_vram_bytes,
        "total": total_vram_bytes,
        "allocated": allocated_vram_bytes,
        "reserved": reserved_vram_bytes,
    }
    fake_torch = SimpleNamespace(
        __version__="2.5.0-fake",
        float16="float16",
        inference_mode=lambda: _InferenceMode(),
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_name=lambda _idx: "Fake GPU",
            mem_get_info=lambda: (state["free"], state["total"]),
            memory_allocated=lambda *_a, **_k: state["allocated"],
            memory_reserved=lambda *_a, **_k: state["reserved"],
        ),
    )
    fake_transformers = SimpleNamespace(
        StoppingCriteria=_FakeStoppingCriteria,
        StoppingCriteriaList=_FakeStoppingCriteriaList,
        TextIteratorStreamer=_FakeTextIteratorStreamer,
        AutoModelForCausalLM=object,
        AutoTokenizer=object,
        BitsAndBytesConfig=object,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    return state


def _make_scripted_runtime(
    monkeypatch: pytest.MonkeyPatch,
    pieces: list[str],
    *,
    max_position_embeddings: int = 32768,
    free_vram_bytes: int = 24 * 1024**3,
    allocated_vram_bytes: int = 4 * 1024**3,
    reserved_vram_bytes: int = 4 * 1024**3,
    prompt_tokens: int = 12,
    step_delay: float = 0.0,
    entered_event: threading.Event | None = None,
    hold_event: threading.Event | None = None,
    raise_in_generate: Exception | None = None,
    on_generate: Any = None,
):
    vram_state = _install_fake_ml_modules(
        monkeypatch,
        free_vram_bytes=free_vram_bytes,
        allocated_vram_bytes=allocated_vram_bytes,
        reserved_vram_bytes=reserved_vram_bytes,
    )
    tokenizer = _FakeTokenizer(prompt_tokens=prompt_tokens)
    piece_ids = tokenizer.register_pieces(pieces)
    stats = {"generated_steps": 0}

    def fake_generate(*, input_ids: _FakeTensor, max_new_tokens: int, streamer: _FakeTextIteratorStreamer | None = None,
                      stopping_criteria: Any = None, **_kwargs: Any) -> _FakeTensor:
        if entered_event is not None:
            entered_event.set()
        if hold_event is not None:
            hold_event.wait(timeout=5)
        if on_generate is not None:
            on_generate(vram_state)
        if raise_in_generate is not None:
            raise raise_in_generate
        emitted: list[int] = list(input_ids._ids)
        for tid, piece in zip(piece_ids[:max_new_tokens], pieces[:max_new_tokens]):
            if step_delay > 0:
                time.sleep(step_delay)
            emitted.append(tid)
            stats["generated_steps"] += 1
            if streamer is not None:
                streamer.put_text(piece)
            current = _FakeTensor(emitted)
            if stopping_criteria is not None and stopping_criteria(current, None):
                break
        if streamer is not None:
            streamer.end()
        return _FakeTensor(emitted)

    class _FakeModel:
        def __init__(self) -> None:
            self.config = SimpleNamespace(max_position_embeddings=max_position_embeddings)
            self.generate = fake_generate

        def __call__(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(past_key_values="fake-kv")

    runtime = TransformersChatRuntime("unused", model_id="fake-qwen")
    runtime._tokenizer = tokenizer
    runtime._model = _FakeModel()
    return runtime, tokenizer, stats, vram_state


def _parse_sse_events(raw_text: str) -> list[Any]:
    events: list[Any] = []
    for line in raw_text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):].strip()
        if payload == "[DONE]":
            events.append("[DONE]")
        else:
            events.append(json.loads(payload))
    return events


def test_t1_concurrent_streaming_second_returns_503_runtime_busy_and_stream_error_codes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    runtime, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ["Hello", " world"],
        entered_event=entered,
        hold_event=release,
    )
    app = create_app(runtime=runtime)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
                "stream": True,
            }

            async def run_first():
                async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                    assert resp.status_code == 200
                    return await resp.aread()

            first_task = asyncio.create_task(run_first())
            try:
                assert await asyncio.to_thread(entered.wait, 2.0)
                # Direct stream_chat call while busy must raise RuntimeBusyError.
                with pytest.raises(RuntimeBusyError):
                    next(iter(runtime.stream_chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})))

                second = await client.post("/v1/chat/completions", json=payload)
                assert second.status_code == 503
                assert second.json()["detail"] == "runtime_busy"
                assert second.headers.get("retry-after") == "5"
            finally:
                release.set()
                raw_first = await first_task
            events = _parse_sse_events(raw_first.decode("utf-8"))
            assert events[-1] == "[DONE]"

    asyncio.run(scenario())

    # Also verify in-stream error payload carries closed error code when generation fails mid-stream.
    timeout_rt, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ["x"],
        raise_in_generate=TimeoutError("generation_timed_out"),
    )
    with TestClient(create_app(runtime=timeout_rt)) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, "stream": True},
        )
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        err_events = [e for e in events if isinstance(e, dict) and "error" in e]
        assert len(err_events) == 1
        assert err_events[0]["error"]["type"] == "api_error"
        assert err_events[0]["error"]["code"] == "generation_timeout"


def test_t2_unknown_tool_name_and_unsatisfied_tool_choice_do_not_error(monkeypatch):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    runtime, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ['<tool_call>{"name": "not_declared_tool", "arguments": {"target": "abc"}}</tool_call>'],
    )
    with TestClient(create_app(runtime=runtime)) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "run tool"}],
                "tools": tools,
                "max_tokens": 32,
            },
        )
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        calls = choice["message"]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "not_declared_tool"
        assert json.loads(calls[0]["function"]["arguments"]) == {"target": "abc"}

    # Unsatisfied tool_choice ("required" and named function) returns stop + text instead of raising.
    plain_rt, _, _, _ = _make_scripted_runtime(monkeypatch, ["Direct plain text answer."])
    with TestClient(create_app(runtime=plain_rt)) as client:
        for forced_choice in ("required", {"type": "function", "function": {"name": "read_file"}}):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "fake-qwen",
                    "messages": [{"role": "user", "content": "answer"}],
                    "tools": tools,
                    "tool_choice": forced_choice,
                    "max_tokens": 32,
                },
            )
            assert resp.status_code == 200
            choice = resp.json()["choices"][0]
            assert choice["finish_reason"] == "stop"
            assert choice["message"]["content"] == "Direct plain text answer."
            assert not choice["message"].get("tool_calls")


def test_t3_incomplete_tool_call_returns_content_with_length_finish_reason(monkeypatch):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    runtime, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ["Let me check: ", '<tool_call>{"name": "read_file", "arguments": {"path": "/tmp/a'],
    )
    with TestClient(create_app(runtime=runtime)) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "check file"}],
                "tools": tools,
                "max_tokens": 2,
            },
        )
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] == "length"
        assert choice["message"].get("tool_calls") is None
        content = choice["message"]["content"]
        assert content.startswith("Let me check: ")
        assert '"read_file"' in content


def test_t4_v1_models_reports_context_window_from_model_and_vram(monkeypatch):
    monkeypatch.delenv("RYNMESH_TRANSFORMERS_CONTEXT", raising=False)
    monkeypatch.delenv("RYNMESH_TRANSFORMERS_MAX_OUTPUT", raising=False)
    # 2.5 GiB free VRAM at model load -> (2560 - 512) MiB / 256 KiB = 8192 context window (< 32768 native)
    runtime_low, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ["ok"],
        max_position_embeddings=32768,
        free_vram_bytes=2560 * 1024**2,
    )
    with TestClient(create_app(runtime=runtime_low)) as client_low:
        models_low = client_low.get("/v1/models")
        assert models_low.status_code == 200
        item_low = models_low.json()["data"][0]
        assert item_low["id"] == "fake-qwen"
        assert item_low["context_window"] == 8192
        assert item_low["max_output_tokens"] == 2048

    # Separate runtime loaded on 24 GiB free VRAM -> capped by model's native max_position_embeddings (32768)
    runtime_high, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ["ok"],
        max_position_embeddings=32768,
        free_vram_bytes=24 * 1024**3,
    )
    with TestClient(create_app(runtime=runtime_high)) as client_high:
        models_high = client_high.get("/v1/models")
        assert models_high.status_code == 200
        item_high = models_high.json()["data"][0]
        assert item_high["context_window"] == 32768
        assert item_high["max_output_tokens"] == 8192


def test_t5_context_length_exceeded_returns_400_for_non_streaming_and_streaming(monkeypatch):
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_CONTEXT", "512")
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_MAX_OUTPUT", "256")
    runtime, _, _, _ = _make_scripted_runtime(
        monkeypatch,
        ["ok"],
        max_position_embeddings=32768,
        prompt_tokens=450,
    )
    assert hasattr(runtime_server, "ContextLengthError")
    with pytest.raises(runtime_server.ContextLengthError) as exc_info:
        runtime.validate_length(450, 128)
    assert exc_info.value.context_window == 512

    with TestClient(create_app(runtime=runtime)) as client:
        # 1. Non-streaming structured path (temperature triggers structured_chat)
        non_stream_struct = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": [{"role": "user", "content": "long"}], "max_tokens": 128, "temperature": 0},
        )
        assert non_stream_struct.status_code == 400
        err1 = non_stream_struct.json()["error"]
        assert err1["code"] == "context_length_exceeded"
        assert err1["context_window"] == 512

        # 2. Non-streaming plain path
        non_stream_plain = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": [{"role": "user", "content": "long"}], "max_tokens": 128},
        )
        assert non_stream_plain.status_code == 400
        err2 = non_stream_plain.json()["error"]
        assert err2["code"] == "context_length_exceeded"
        assert err2["context_window"] == 512

        # 3. Streaming path (must reject with HTTP 400 before starting 200 event-stream)
        streaming = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": [{"role": "user", "content": "long"}], "max_tokens": 128, "stream": True},
        )
        assert streaming.status_code == 400
        err3 = streaming.json()["error"]
        assert err3["code"] == "context_length_exceeded"
        assert err3["context_window"] == 512


def test_t6_custom_stop_sequence_truncates_output_and_stops_generation(monkeypatch):
    pieces = ["Answer: ", "42", "<|ST", "OP|>", " extra_1", " extra_2", " extra_3"]
    runtime, _, stats, _ = _make_scripted_runtime(monkeypatch, pieces)
    with TestClient(create_app(runtime=runtime)) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "compute"}],
                "stop": ["<|STOP|>"],
                "max_tokens": 32,
            },
        )
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"] == "Answer: 42"
        # StoppingCriteria stopped generation as soon as "<|STOP|>" completed (4 tokens, not all 7)
        assert stats["generated_steps"] == 4

        # Verify streaming mode with string `stop` also strips the stop sequence cleanly.
        stats["generated_steps"] = 0
        stream_resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "compute"}],
                "stop": "<|STOP|>",
                "max_tokens": 32,
                "stream": True,
            },
        )
        assert stream_resp.status_code == 200
        acc = ChatAccumulator()
        for ev in _parse_sse_events(stream_resp.text):
            if isinstance(ev, dict):
                acc.add(ev)
        assert acc.finish_reason == "stop"
        assert acc.message()["content"] == "Answer: 42"


def test_t7_malformed_json_tool_call_becomes_tool_call_item(monkeypatch):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    bad_with_name = '<tool_call>{"name": "search_files", "arguments": {bad_json: 1}}</tool_call>'
    runtime1, _, _, _ = _make_scripted_runtime(monkeypatch, [bad_with_name])
    with TestClient(create_app(runtime=runtime1)) as client:
        resp1 = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": [{"role": "user", "content": "go"}], "tools": tools, "max_tokens": 32},
        )
        assert resp1.status_code == 200
        choice1 = resp1.json()["choices"][0]
        assert choice1["finish_reason"] == "tool_calls"
        tc1 = choice1["message"]["tool_calls"][0]
        assert tc1["function"]["name"] == "search_files"
        assert tc1["function"]["arguments"] == '{"name": "search_files", "arguments": {bad_json: 1}}'

    bad_without_name = "<tool_call>not json at all {{{</tool_call>"
    runtime2, _, _, _ = _make_scripted_runtime(monkeypatch, [bad_without_name])
    with TestClient(create_app(runtime=runtime2)) as client:
        resp2 = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": [{"role": "user", "content": "go"}], "tools": tools, "max_tokens": 32},
        )
        assert resp2.status_code == 200
        choice2 = resp2.json()["choices"][0]
        assert choice2["finish_reason"] == "tool_calls"
        tc2 = choice2["message"]["tool_calls"][0]
        assert tc2["function"]["name"] == "malformed_tool_call"
        assert tc2["function"]["arguments"] == "not json at all {{{"


def test_t8_client_disconnect_stops_generation_and_releases_lock(monkeypatch):
    many_pieces = [f"tok{i} " for i in range(60)]
    runtime, _, stats, _ = _make_scripted_runtime(monkeypatch, many_pieces, step_delay=0.05)
    app = create_app(runtime=runtime)

    async def streaming_disconnect_scenario():
        body_bytes = json.dumps({
            "model": "fake-qwen",
            "messages": [{"role": "user", "content": "stream long"}],
            "max_tokens": 60,
            "stream": True,
        }).encode("utf-8")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8080),
        }
        request_sent = False
        disconnect_now = asyncio.Event()
        received_chunks: list[bytes] = []

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            await disconnect_now.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    received_chunks.append(chunk)
                    # Simulate client closing TCP stream right after receiving first token chunk.
                    if len(received_chunks) >= 2:
                        disconnect_now.set()

        await app(scope, receive, send)
        deadline = time.monotonic() + 2.0
        while runtime._generation_lock.locked() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not runtime._generation_lock.locked()
        assert stats["generated_steps"] < 30

        # Subsequent request must succeed immediately without 503.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            follow_up = await client.post(
                "/v1/chat/completions",
                json={"model": "fake-qwen", "messages": [{"role": "user", "content": "after disconnect"}], "max_tokens": 2, "temperature": 0},
            )
            assert follow_up.status_code == 200

    asyncio.run(streaming_disconnect_scenario())

    # Also test non-streaming structured chat polling `request.is_disconnected()` every 0.5s.
    entered_ns = threading.Event()
    ns_runtime, _, ns_stats, _ = _make_scripted_runtime(
        monkeypatch,
        [f"ns{i} " for i in range(60)],
        step_delay=0.08,
        entered_event=entered_ns,
    )
    ns_app = create_app(runtime=ns_runtime)

    async def non_streaming_disconnect_scenario():
        disconnected = False
        orig_is_disconnected = runtime_server.FastAPIRequest.is_disconnected

        async def fake_is_disconnected(self):
            if disconnected:
                return True
            return await orig_is_disconnected(self)

        monkeypatch.setattr(runtime_server.FastAPIRequest, "is_disconnected", fake_is_disconnected)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ns_app), base_url="http://test") as client:
            req_task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json={"model": "fake-qwen", "messages": [{"role": "user", "content": "slow"}], "max_tokens": 60, "temperature": 0},
                )
            )
            assert await asyncio.to_thread(entered_ns.wait, 2.0)
            disconnected = True
            await asyncio.gather(req_task, return_exceptions=True)
            deadline = time.monotonic() + 2.0
            while ns_runtime._generation_lock.locked() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert not ns_runtime._generation_lock.locked()
            assert ns_stats["generated_steps"] < 30

            disconnected = False
            ok_resp = await client.post(
                "/v1/chat/completions",
                json={"model": "fake-qwen", "messages": [{"role": "user", "content": "ok"}], "max_tokens": 2, "temperature": 0},
            )
            assert ok_resp.status_code == 200

    asyncio.run(non_streaming_disconnect_scenario())


def test_t9_history_with_malformed_json_tool_arguments_succeeds(monkeypatch):
    bad_raw = "{broken_json: " + ("x" * 4500)
    history = [
        {"role": "user", "content": "find file"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_bad_1",
                    "type": "function",
                    "function": {"name": "search_files", "arguments": bad_raw},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_bad_1", "content": '{"error": "invalid_json_arguments"}'},
        {"role": "user", "content": "try again without error"},
    ]
    cleaned = template_messages(history)
    wrapped = cleaned[1]["tool_calls"][0]["function"]["arguments"]
    assert wrapped == {"_raw_arguments": bad_raw[:4000]}
    assert len(wrapped["_raw_arguments"]) == 4000
    assert cleaned[2]["role"] == "tool" and cleaned[2]["tool_call_id"] == "call_bad_1"

    runtime, tokenizer, _, _ = _make_scripted_runtime(monkeypatch, ["Recovered cleanly."])
    with TestClient(create_app(runtime=runtime)) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "fake-qwen", "messages": history, "max_tokens": 16},
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Recovered cleanly."
        assert tokenizer.last_messages[1]["tool_calls"][0]["function"]["arguments"] == {"_raw_arguments": bad_raw[:4000]}
        assert tokenizer.last_messages[2]["content"] == '{"error": "invalid_json_arguments"}'


def test_t13_hf_mirror_host_only_https_and_agent_capable_recommendation(monkeypatch, caplog):
    import logging

    monkeypatch.delenv("RYNMESH_HF_MIRROR", raising=False)
    monkeypatch.delenv("RYNMESH_DOWNLOAD_MIRROR", raising=False)

    # Every profile must declare boolean `agent_capable`, and at least one profile is agent_capable=True.
    agent_profiles = [p for p in llm_catalog.PROFILES if getattr(p, "agent_capable", None) is True]
    assert len(agent_profiles) >= 1
    for p in llm_catalog.PROFILES:
        assert isinstance(p.agent_capable, bool)
        assert len(p.sha256) == 64
        assert "/resolve/main/" not in p.url

    default_light_url = llm_catalog.profile_by_name("light").url
    default_light_sha = llm_catalog.profile_by_name("light").sha256
    assert default_light_url.startswith("https://huggingface.co/")
    expected_suffix = default_light_url[len("https://huggingface.co"):]

    # Valid https mirror replaces ONLY the host; path, revision, and sha256 stay identical.
    monkeypatch.setenv("RYNMESH_HF_MIRROR", "https://hf-mirror.com/")
    mirrored_profile = llm_catalog.profile_by_name("light")
    assert mirrored_profile.url == "https://hf-mirror.com" + expected_suffix
    assert mirrored_profile.sha256 == default_light_sha
    assert llm_catalog._pinned_url("Qwen/Test", "abc1234", "f.gguf") == "https://hf-mirror.com/Qwen/Test/resolve/abc1234/f.gguf"

    low_mem_report = llm_hardware.HardwareReport(
        os="Linux", architecture="x86_64", cpu="test", logical_cpus=8,
        ram_total_mb=8000, ram_available_mb=6000, disk_free_mb=32_000,
        nvidia_gpus=[], nvidia_probe="none", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=True, warnings=[],
    )

    # Non-https or path/credential mirrors must be rejected:
    # explicit `set_hf_mirror` / `_pinned_url` raise ValueError, while reading `.url` and `hardware.recommend`
    # fall back to the canonical https://huggingface.co URL and record a warning diagnostic without leaking secrets.
    for bad_mirror in (
        "http://hf-mirror.com",
        "hf-mirror.com",
        "ftp://hf-mirror.com",
        "https://hf-mirror.com/subpath",
        "https://user:secret@hf-mirror.com",
        "https://hf-mirror.com?token=1",
    ):
        monkeypatch.setenv("RYNMESH_HF_MIRROR", bad_mirror)
        with pytest.raises(ValueError):
            llm_catalog.set_hf_mirror(bad_mirror)
        with pytest.raises(ValueError):
            llm_catalog._pinned_url("Qwen/Test", "abc1234", "f.gguf")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=llm_catalog.__name__):
            fallback_url = llm_catalog.profile_by_name("light").url
            recs_under_bad_mirror = llm_hardware.recommend(low_mem_report)
        assert fallback_url == default_light_url
        assert len(recs_under_bad_mirror) >= 1
        assert recs_under_bad_mirror[0]["source_url"] == default_light_url
        assert any("invalid_hf_mirror" in rec.message for rec in caplog.records)
        assert "secret" not in caplog.text

    monkeypatch.delenv("RYNMESH_HF_MIRROR", raising=False)

    # `agent_capable` profile only appears in `hardware.recommend` when memory & disk fit.
    low_recs = llm_hardware.recommend(low_mem_report)
    assert all(rec.get("can_run") is True for rec in low_recs)
    assert all("agent_capable" in rec for rec in low_recs)
    assert not any(rec["agent_capable"] for rec in low_recs)

    high_mem_report = llm_hardware.HardwareReport(
        os="Linux", architecture="x86_64", cpu="test", logical_cpus=8,
        ram_total_mb=32_000, ram_available_mb=16_000, disk_free_mb=32_000,
        nvidia_gpus=[], nvidia_probe="none", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=True, warnings=[],
    )
    high_recs = llm_hardware.recommend(high_mem_report)
    assert any(rec["agent_capable"] is True for rec in high_recs)
    agent_rec = next(rec for rec in high_recs if rec["agent_capable"] is True)
    assert agent_rec["recommended"] is True


def test_t14_context_window_stays_stable_after_long_generation_reserves_vram(monkeypatch):
    monkeypatch.delenv("RYNMESH_TRANSFORMERS_CONTEXT", raising=False)
    monkeypatch.delenv("RYNMESH_TRANSFORMERS_MAX_OUTPUT", raising=False)

    def simulate_caching_allocator_growth(state: dict[str, int]) -> None:
        # Simulate PyTorch's CUDA caching allocator holding 1.5 GiB of freed KV/activation blocks:
        # driver-level free VRAM drops by 1.5 GiB while `memory_reserved()` rises by 1.5 GiB.
        if state["free"] > 1024 * 1024**2:
            delta = 1536 * 1024**2
            state["free"] -= delta
            state["reserved"] += delta

    runtime, _, _, vram_state = _make_scripted_runtime(
        monkeypatch,
        ["long_generation_ok"],
        max_position_embeddings=32768,
        free_vram_bytes=2560 * 1024**2,
        allocated_vram_bytes=4096 * 1024**2,
        reserved_vram_bytes=4096 * 1024**2,
        prompt_tokens=5000,
        on_generate=simulate_caching_allocator_growth,
    )

    with TestClient(create_app(runtime=runtime)) as client:
        before_models = client.get("/v1/models")
        assert before_models.status_code == 200
        before_item = before_models.json()["data"][0]
        assert before_item["context_window"] == 8192
        assert before_item["max_output_tokens"] == 2048

        first = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "first long prompt"}],
                "max_tokens": 1024,
                "temperature": 0,
            },
        )
        assert first.status_code == 200
        # Confirm driver-reported free VRAM dropped while PyTorch reserved pool grew.
        assert vram_state["free"] == 1024 * 1024**2
        assert vram_state["reserved"] - vram_state["allocated"] == 1536 * 1024**2

        after_models = client.get("/v1/models")
        assert after_models.status_code == 200
        after_item = after_models.json()["data"][0]
        assert after_item["context_window"] == 8192
        assert after_item["max_output_tokens"] == 2048

        second = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "second identical-length prompt"}],
                "max_tokens": 1024,
                "temperature": 0,
            },
        )
        assert second.status_code == 200

        second_stream = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-qwen",
                "messages": [{"role": "user", "content": "third identical-length streaming prompt"}],
                "max_tokens": 1024,
                "stream": True,
            },
        )
        assert second_stream.status_code == 200
