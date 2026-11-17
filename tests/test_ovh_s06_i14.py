"""S06-I14 real default-router diagnostics, health ordering and batch execution."""
import json
import threading
import time
from urllib.parse import urlparse

import httpx
import pytest

from rynmesh.agent_runtime import web_search
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.web_search import WebSearchEngine


DDG = '<a class="result__a" href="https://example.com/ddg">DDG result</a>'
BING = '<li class="b_algo"><h2><a href="https://example.com/bing">Bing result</a></h2><p>Summary</p></li>'


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("RYN_WEB_SEARCH_SEARXNG_URL", "RYN_WEB_SEARCH_TAVILY_API_KEY", "RYN_WEB_SEARCH_BRAVE_API_KEY", "RYN_WEB_SEARCH_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RYN_WEB_SEARCH_REGION", "us")


def http_boundary(monkeypatch, respond):
    calls, timeouts = [], []
    class Client:
        def __init__(self, **kwargs):
            timeouts.append(kwargs.get("timeout"))
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def get(self, url, **kwargs):
            calls.append(url)
            return respond(url, kwargs)
        def post(self, url, **kwargs):
            return self.get(url, **kwargs)
    monkeypatch.setattr(web_search.httpx, "Client", Client)
    return calls, timeouts


def html(text="", status=200):
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://example.com"))


def test_t1_all_backends_timeout_are_unavailable_with_safe_correction(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("RYN_WEB_SEARCH_SEARXNG_URL", "https://search.example/search")
    monkeypatch.setenv("RYN_WEB_SEARCH_TAVILY_API_KEY", "SECRET_TAVILY")
    monkeypatch.setenv("RYN_WEB_SEARCH_BRAVE_API_KEY", "SECRET_BRAVE")
    def timeout(*args): raise httpx.ReadTimeout("secret response body")
    calls, limits = http_boundary(monkeypatch, timeout)
    with pytest.raises(AgentToolRejected, match="agent_web_search_unavailable") as caught:
        WebSearchEngine(tmp_path).execute({"query": "test"})
    correction = caught.value.correction
    assert all(name in correction.lower() for name in ("searxng", "tavily", "brave", "duckduckgo", "bing"))
    assert "RYN_WEB_SEARCH_" in correction
    assert "secret" not in correction.lower()
    assert len(calls) == 5
    assert all(isinstance(limit, httpx.Timeout) and limit.connect == 4.0 for limit in limits)


def test_t2_successful_empty_results_are_not_cached(tmp_path, monkeypatch, clean_env):
    calls, _ = http_boundary(monkeypatch, lambda *_: html())
    engine = WebSearchEngine(tmp_path)
    first = engine.execute({"query": "empty"})
    assert first["results"] == [] and "error" not in first["searches"][0]
    assert engine._get_cached_query("empty", []) is None
    count = len(calls)
    engine.execute({"query": "empty"})
    assert len(calls) > count


def test_t2_legacy_empty_cache_does_not_mask_recovered_network(tmp_path, monkeypatch, clean_env):
    calls, _ = http_boundary(monkeypatch, lambda *_: html(DDG))
    engine = WebSearchEngine(tmp_path)
    path = engine._query_cache_path("recovered", [])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cached_at": time.time(), "results": []}), encoding="utf-8")
    result = engine.execute({"query": "recovered"})
    assert calls and result["results"][0]["title"] == "DDG result"


def test_t3_failed_ddg_yields_to_recent_success_and_recovers(tmp_path, monkeypatch, clean_env):
    def respond(url, kwargs):
        if "duckduckgo" in url: raise httpx.ConnectError("blocked")
        return html(BING)
    calls, _ = http_boundary(monkeypatch, respond)
    engine = WebSearchEngine(tmp_path)
    engine.execute({"query": "first"})
    assert "duckduckgo" in calls[0] and "bing.com" in calls[1]
    calls.clear()
    engine.execute({"query": "second"})
    assert len(calls) == 1 and "bing.com" in calls[0]


def test_t4_four_queries_run_concurrently_and_refs_remain_consistent(tmp_path):
    active, maximum = 0, 0
    lock = threading.Lock()
    def provider(query, count, domains):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.5)
        with lock: active -= 1
        return [{"url": "https://example.com/" + query, "title": query}]
    engine = WebSearchEngine(tmp_path, search_provider=provider)
    started = time.monotonic()
    result = engine.execute({"queries": ["one", "two", "three", "four"]})
    elapsed = time.monotonic() - started
    assert elapsed < 1.2 and maximum == 4
    assert [row["query"] for row in result["searches"]] == ["one", "two", "three", "four"]
    assert len({row["ref_id"] for row in result["results"]}) == 4


def test_t5_cancellation_between_backends_is_not_swallowed(tmp_path, monkeypatch, clean_env):
    stopped = threading.Event()
    def respond(*args):
        stopped.set()
        raise httpx.ConnectError("blocked")
    calls, _ = http_boundary(monkeypatch, respond)
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        WebSearchEngine(tmp_path).execute({"query": "cancel"}, cancelled=stopped.is_set)
    assert len(calls) == 1


@pytest.mark.parametrize("region, host", [("cn", "cn.bing.com"), ("us", "html.duckduckgo.com")])
def test_t6_t7_region_selects_first_backend(tmp_path, monkeypatch, clean_env, region, host):
    monkeypatch.setenv("RYN_WEB_SEARCH_REGION", region)
    calls, _ = http_boundary(monkeypatch, lambda url, _: html(BING if "bing.com" in url else DDG))
    WebSearchEngine(tmp_path).execute({"query": "region"})
    assert urlparse(calls[0]).hostname == host


def test_t8_cn_bing_failure_falls_back_to_existing_last_source(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("RYN_WEB_SEARCH_REGION", "cn")
    def respond(url, kwargs):
        return html(status=503) if "bing.com" in url else html(DDG)
    calls, _ = http_boundary(monkeypatch, respond)
    result = WebSearchEngine(tmp_path).execute({"query": "fallback"})
    assert [urlparse(url).hostname for url in calls] == ["cn.bing.com", "html.duckduckgo.com"]
    assert result["results"][0]["title"] == "DDG result"


def test_t6_locale_zh_defaults_to_cn_but_explicit_region_wins(tmp_path, monkeypatch, clean_env):
    monkeypatch.delenv("RYN_WEB_SEARCH_REGION")
    monkeypatch.setattr(web_search.locale, "getlocale", lambda: ("zh_CN", "UTF-8"))
    calls, _ = http_boundary(monkeypatch, lambda url, _: html(BING if "bing.com" in url else DDG))
    WebSearchEngine(tmp_path).execute({"query": "locale"})
    assert "cn.bing.com" in calls[0]
    calls.clear()
    monkeypatch.setenv("RYN_WEB_SEARCH_REGION", "us")
    WebSearchEngine(tmp_path / "other").execute({"query": "explicit"})
    assert "duckduckgo" in calls[0]


def test_t1_partial_batch_failure_and_empty_success_are_distinguishable(tmp_path, monkeypatch, clean_env):
    def respond(url, kwargs):
        query = kwargs.get("params", {}).get("q", url)
        return html() if "empty" in query else html(status=429)
    http_boundary(monkeypatch, respond)
    result = WebSearchEngine(tmp_path).execute({"queries": ["bad", "empty"]})
    bad, empty = result["searches"]
    assert bad["error"] == "agent_web_search_unavailable"
    assert all(item["reason"] == "http_429" for item in bad["backends_failed"])
    assert "error" not in empty and empty["results"] == []
    assert "SECRET" not in json.dumps(result)
