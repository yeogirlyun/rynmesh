from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rynmesh.llm_runtime_server import create_app


@pytest.mark.parametrize("opening", ["", "<think>", "\n<think>"])
@pytest.mark.parametrize("chunk_size", [1, 3, 1000])
def test_qwen_thinking_separation_across_chunks(opening, chunk_size):
    from rynmesh.llm_package.chat import ChatAccumulator
    from rynmesh.llm_runtime_server import ThinkingSplitter
    text = opening + 'Consider <tool_call> as text.</think>Hello!'
    splitter = ThinkingSplitter(True)
    result = ChatAccumulator()
    for start in range(0, len(text), chunk_size):
        reasoning, answer = splitter.feed(text[start:start + chunk_size])
        result.add({"choices": [{"delta": {"reasoning_content": reasoning, "content": answer}}]})
    reasoning, answer = splitter.feed("", final=True)
    result.add({"choices": [{"delta": {"reasoning_content": reasoning, "content": answer}}]})
    assert result.message() == {"role": "assistant", "reasoning_content": "Consider <tool_call> as text.", "content": "Hello!"}


def test_qwen_thinking_truncation_and_disabled_mode():
    from rynmesh.llm_runtime_server import ThinkingSplitter
    splitter = ThinkingSplitter(True)
    first, _ = splitter.feed("<think>Unfinished")
    last, answer = splitter.feed("", final=True)
    assert first + last == "Unfinished" and answer == ""
    assert ThinkingSplitter(False).feed("literal <think> text") == ("", "literal <think> text")


def test_runtime_limits_allow_long_output_and_enforce_native_context(monkeypatch):
    from rynmesh.llm_runtime_server import TransformersChatRuntime
    runtime = TransformersChatRuntime("unused")
    runtime._model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=40960))
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_CONTEXT", "40960")
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_MAX_OUTPUT", "32768")
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_TIMEOUT", "7200")
    runtime.validate_length(8192, 32768)
    with pytest.raises(ValueError, match="context window"):
        runtime.validate_length(8193, 32768)
    with pytest.raises(ValueError, match="runtime limit"):
        runtime.validate_length(1, 32769)
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_CONTEXT", "131072")
    assert runtime.generation_limits() == (40960, 32768, 7200)


def test_qwen_template_accepts_openai_tool_only_assistant():
    from rynmesh.llm_runtime_server import template_messages

    original = [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Shanghai"}'}}]}]
    cleaned = template_messages(original)
    assert cleaned[0]["content"] == ""
    assert cleaned[0]["tool_calls"][0]["function"]["arguments"] == {"city": "Shanghai"}
    assert original[0]["content"] is None


class FakeRuntime:
    model_id = "fake-qwen"

    def health(self):
        return {"ok": True, "model": self.model_id, "loaded": True}

    def complete(self, messages, *, max_tokens):
        assert messages[-1]["content"] == "你好"
        assert max_tokens == 12
        return {"text": "你好，节点正常。", "input_tokens": 3, "output_tokens": 6, "duration_ms": 9}


def test_openai_compatible_chat_surface():
    client = TestClient(create_app(runtime=FakeRuntime()))
    assert client.get("/health").json()["ok"] is True
    assert client.get("/v1/models").json()["data"][0]["id"] == "fake-qwen"
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fake-qwen", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 12},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "你好，节点正常。"
    assert body["usage"]["completion_tokens"] == 6


def test_streaming_fails_explicitly():
    client = TestClient(create_app(runtime=FakeRuntime()))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fake-qwen", "messages": [{"role": "user", "content": "你好"}], "stream": True},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "streaming_not_supported"


def test_generation_keeps_health_responsive_and_rejects_overlap():
    import asyncio
    import threading

    import httpx

    from rynmesh.llm_runtime_server import TransformersChatRuntime

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowRuntime(TransformersChatRuntime):
        def health(self):
            return {"ok": True}

        def _complete(self, messages, *, max_tokens):
            entered.set()
            release.wait(timeout=3)
            finished.set()
            return {"text": "ok", "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}

    async def scenario():
        app = create_app(runtime=SlowRuntime("unused"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            request = {"messages": [{"content": "test"}]}
            first = asyncio.create_task(client.post("/v1/chat/completions", json=request))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (await client.get("/health")).json()["ok"]
                assert (await client.get("/v1/models")).status_code == 200
                assert not finished.is_set(), "generation blocked the HTTP event loop"
                busy = await client.post("/v1/chat/completions", json=request)
                assert busy.status_code == 503
                assert busy.json()["detail"] == "runtime_busy"
                # Cancelling the HTTP task must not release the GPU slot while
                # its synchronous inference is still using it.
                first.cancel()
                import contextlib
                with contextlib.suppress(asyncio.CancelledError):
                    await first
                busy = await client.post("/v1/chat/completions", json=request)
                assert busy.status_code == 503
            finally:
                release.set()
                assert await asyncio.to_thread(finished.wait, 2)
                await asyncio.gather(first, return_exceptions=True)
            assert (await client.post("/v1/chat/completions", json=request)).status_code == 200

    asyncio.run(scenario())


def test_unloaded_runtime_is_not_advertised_even_when_cuda_exists(monkeypatch):
    import sys
    from types import SimpleNamespace

    from rynmesh.llm_runtime_server import TransformersChatRuntime

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        __version__="test", cuda=SimpleNamespace(is_available=lambda: True, get_device_name=lambda n: "fake"),
    ))
    client = TestClient(create_app(runtime=TransformersChatRuntime("missing")))
    assert client.get("/health").json()["ok"] is False
    assert client.get("/v1/models").status_code == 503


def test_startup_load_failure_keeps_health_degraded_without_leaking_paths():
    from rynmesh.llm_runtime_server import TransformersChatRuntime

    class BrokenRuntime(TransformersChatRuntime):
        def load(self):
            raise RuntimeError("PRIVATE_MODEL_PATH_AND_PROMPT")

    with TestClient(create_app(runtime=BrokenRuntime("missing"))) as client:
        assert client.get("/health").json()["ok"] is False
        assert client.get("/v1/models").status_code == 503
        response = client.post("/v1/chat/completions", json={"messages": [{"content": "test"}]})
        assert response.status_code == 503
        assert "PRIVATE_MODEL_PATH_AND_PROMPT" not in response.text + client.get("/health").text


def test_startup_loads_model_before_advertising_readiness():
    class LoadingRuntime(FakeRuntime):
        loaded = False

        def load(self):
            self.loaded = True

        def health(self):
            return {"ok": self.loaded}

    runtime = LoadingRuntime()
    with TestClient(create_app(runtime=runtime)) as client:
        assert runtime.loaded
        assert client.get("/v1/models").status_code == 200


def test_missing_weights_cuda_and_dependencies_fail_readiness(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from rynmesh.llm_runtime_server import TransformersChatRuntime

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    fake_torch = SimpleNamespace(
        __version__="test",
        cuda=SimpleNamespace(is_available=lambda: True, get_device_name=lambda n: "fake"),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForCausalLM=object, AutoTokenizer=object, BitsAndBytesConfig=object,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    for failure in ("weights", "cuda", "dependencies"):
        if failure != "weights":
            (model_dir / "config.json").write_text("{}")
        fake_torch.cuda.is_available = lambda failure=failure: failure != "cuda"
        if failure == "dependencies":
            monkeypatch.setitem(sys.modules, "transformers", None)
        runtime = TransformersChatRuntime(str(model_dir))
        with TestClient(create_app(runtime=runtime)) as client:
            assert runtime._model is None
            assert runtime._load_error == "model_load_failed:RuntimeError"
            assert client.get("/health").json()["ok"] is False
            assert client.get("/v1/models").status_code == 503
