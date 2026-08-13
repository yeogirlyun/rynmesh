"""Model provider seam + Search & Ask + digest AI enrichment."""
from __future__ import annotations

import pytest

from rynmesh.services import ask
from rynmesh.services.digest import DigestService
from rynmesh.services.model_provider import (
    AnthropicProvider,
    ModelProviderError,
    OllamaProvider,
    resolve_provider,
)

NOW = 1_800_000_000.0


class FakeProvider:
    id = "fake"
    model = "fake-1"

    def __init__(self, reply="a fine summary", fail=False):
        self.reply = reply
        self.fail = fail
        self.calls: list[str] = []

    def generate(self, prompt, *, system="", max_tokens=1024):
        self.calls.append(prompt)
        if self.fail:
            raise ModelProviderError("boom")
        return self.reply


def make_http(tags=None, response="hello", fail=False):
    def http(url, payload, timeout_s):
        if fail:
            raise OSError("connection refused")
        if url.endswith("/api/tags"):
            return {"models": [{"name": name} for name in (tags or [])]}
        if url.endswith("/api/generate"):
            return {"response": response, "model": payload["model"]}
        raise AssertionError(f"unexpected url {url}")

    return http


# ---------------------------------------------------------------- ollama ----

def test_ollama_picks_first_installed_model_and_generates(monkeypatch):
    monkeypatch.delenv("RYNMESH_OLLAMA_MODEL", raising=False)
    provider = OllamaProvider(http=make_http(tags=["gemma3:4b", "qwen3.6:35b"]))
    assert provider.available() is True
    assert provider.model == "gemma3:4b"
    assert provider.generate("hi") == "hello"


def test_ollama_unavailable(monkeypatch):
    monkeypatch.delenv("RYNMESH_OLLAMA_MODEL", raising=False)
    provider = OllamaProvider(http=make_http(fail=True))
    assert provider.available() is False
    with pytest.raises(ModelProviderError):
        provider.generate("hi")


# ------------------------------------------------------------- anthropic ----

class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _FakeAnthropicClient:
    def __init__(self, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs

        class Response:
            stop_reason = self.stop_reason
            content = [_Block("thinking"), _Block("text", "answer "), _Block("text", "text")]

        return Response()


def test_anthropic_provider_joins_text_blocks():
    provider = AnthropicProvider(model="claude-opus-5", client=_FakeAnthropicClient())
    assert provider.generate("q") == "answer text"


def test_anthropic_provider_surfaces_refusal():
    provider = AnthropicProvider(model="claude-opus-5", client=_FakeAnthropicClient("refusal"))
    with pytest.raises(ModelProviderError, match="refused"):
        provider.generate("q")


# -------------------------------------------------------------- resolve ----

def test_resolve_forced_none(monkeypatch):
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    assert resolve_provider() is None


def test_resolve_forced_ollama_unavailable_raises(monkeypatch):
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "ollama")
    with pytest.raises(ModelProviderError):
        resolve_provider(http=make_http(fail=True))


def test_resolve_auto_falls_back_to_ollama(monkeypatch):
    monkeypatch.delenv("RYNMESH_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RYNMESH_OLLAMA_MODEL", raising=False)
    provider = resolve_provider(http=make_http(tags=["gemma3:4b"]))
    assert provider is not None and provider.id == "ollama"


def test_resolve_auto_none_when_nothing_reachable(monkeypatch):
    monkeypatch.delenv("RYNMESH_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_provider(http=make_http(fail=True)) is None


# ------------------------------------------------------------ search-ask ----

def test_ask_without_provider_is_honest():
    result = ask.answer("anything", provider=None)
    assert "No AI model is connected" in result["assistant"]["text"]
    assert result["assistant"]["cites"] == []


def test_ask_grounds_answer_in_evidence_and_cites_mesh():
    provider = FakeProvider(reply="Grounded answer [1].")
    digest_items = [
        {"title": "Rust 2.0 released", "summary": "big release", "link": "https://ex.com/rust",
         "source_title": "HN"},
        {"title": "Gardening tips", "summary": "soil", "link": "https://ex.com/soil",
         "source_title": "Blog"},
    ]
    content_items = [
        {"content_id": "cid_1", "title": "Rust clip", "description": "video about rust",
         "tags": ["rust"], "source_peer_name": "dan-node"},
    ]
    result = ask.answer(
        "what happened with rust?",
        provider=provider,
        digest_items=digest_items,
        content_items=content_items,
    )
    assert result["assistant"]["text"] == "Grounded answer [1]."
    assert result["assistant"]["cites"] == ["cid_1"]
    prompt = provider.calls[0]
    assert "Rust 2.0 released" in prompt
    assert "Gardening tips" not in prompt  # keyword-filtered out


def test_ask_provider_failure_is_reported_not_raised():
    result = ask.answer("q", provider=FakeProvider(fail=True),
                        digest_items=[{"title": "q item", "summary": "", "link": "x",
                                       "source_title": "s"}])
    assert "failed" in result["assistant"]["text"]


# ------------------------------------------------- digest AI enrichment ----

RSS = (b'<rss version="2.0"><channel><title>T</title>'
       b'<item><title>One</title><link>https://e.com/1</link>'
       b'<description>first thing</description></item>'
       b'<item><title>Two</title><link>https://e.com/2</link>'
       b'<description>second thing</description></item>'
       b'</channel></rss>')


def test_digest_enrichment_brief_and_cached_summaries(tmp_path):
    service = DigestService(tmp_path, fetcher=lambda url, timeout: RSS)
    service.add_source("https://e.com/feed")
    provider = FakeProvider(reply="- worth reading")

    digest = service.build(now_unix=NOW, provider=provider)
    assert digest["brief"] == "- worth reading"
    assert digest["ai"] == {"provider": "fake", "model": "fake-1"}
    assert all(item["ai_summary"] == "- worth reading" for item in digest["items"])
    first_calls = len(provider.calls)  # 2 summaries + 1 brief

    # summaries persist: second build only pays for the brief
    again = service.build(now_unix=NOW, provider=provider)
    assert len(provider.calls) == first_calls + 1
    assert again["items"][0]["ai_summary"]


def test_digest_survives_provider_failure(tmp_path):
    service = DigestService(tmp_path, fetcher=lambda url, timeout: RSS)
    service.add_source("https://e.com/feed")
    digest = service.build(now_unix=NOW, provider=FakeProvider(fail=True))
    assert digest["items"]  # ranking still delivered
    assert digest["brief"] == ""
