"""Tests for Codex-aligned model-agnostic local web search (`ryn_web_search`)."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import code_mode, loop, tool_menu
from rynmesh.agent_runtime.code_mode import SPEC as CODE_MODE_SPEC
from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.agent_runtime.web_search import (
     WebSearchEngine,
    _BingHtmlParser,
    _DuckDuckGoHtmlParser,
    domain_matches,
    normalize_domains,
)
from rynmesh.llm_package import codex_tools


def _fake_dns(hostname: str) -> list[str]:
    if hostname in {"evil-internal.example.com", "rebind.example.com"}:
        return ["127.0.0.1"]
    if hostname == "metadata.example.com":
        return ["169.254.169.254"]
    return ["93.184.216.34"]


SAMPLE_DOCS_HTML = b"""<!DOCTYPE html>
<html>
<head><title>Ryn Agent Web Search Architecture Guide</title></head>
<body>
  <nav>Navigation Links</nav>
  <article>
    <h1>Ryn Agent Web Search Architecture</h1>
    <p>Ryn provides a model-agnostic local web search tool inspired by OpenAI Codex SearchCommands.</p>
    <h2>Batch Queries and Reference IDs</h2>
    <p>Models can issue up to four search queries in a single tool call and receive stable ref_id handles such as web_1 and web_2.</p>
    <h2>Readable Page Extraction</h2>
    <p>Opening a reference extracts clean readable blocks through ReaderCache without spawning a heavy desktop browser.</p>
    <pre>const res = await tools.ryn_web_search({ queries: ["ryn agent"] });</pre>
    <p>In-page find locates specific patterns and returns line numbers with surrounding context lines.</p>
  </article>
  <footer>Footer boilerplate</footer>
</body>
</html>
"""


def _make_engine(tmp_path: Path) -> tuple[WebSearchEngine, list[str], list[str]]:
    searched_queries: list[str] = []
    fetched_urls: list[str] = []

    def fake_search(query: str, max_results: int, allowed_domains: list[str]) -> list[dict[str, str]]:
        searched_queries.append(query)
        return [
            {
                "url": "https://docs.python.org/3/library/urllib.parse.html",
                "title": "urllib.parse — Parse URLs into components",
                "snippet": "This module defines a standard interface to break Uniform Resource Locator strings up in components.",
            },
            {
                "url": "https://rust-lang.org/learn/get-started",
                "title": "Getting Started with Rust",
                "snippet": "Quickly set up a Rust development environment and write a small application with Cargo.",
            },
            {
                "url": "http://127.0.0.1:8080/internal-admin",
                "title": "Should be filtered out by SSRF guard",
                "snippet": "Internal loopback endpoint",
            },
        ][:max_results]

    def fake_fetch(url: str, allowed_domains: list[str]) -> tuple[bytes, str, str]:
        fetched_urls.append(url)
        if "redirect-to-local" in url:
            return b"redirected", "http://127.0.0.1/secret", "text/html"
        if url.endswith(".md"):
            return b"# Markdown Guide\nLine two of markdown\nSpecial token BETA_42 here\n", url, "text/markdown"
        return SAMPLE_DOCS_HTML, url, "text/html; charset=utf-8"

    engine = WebSearchEngine(
        tmp_path / "web_search_cache",
        search_provider=fake_search,
        http_fetcher=fake_fetch,
        dns_resolver=_fake_dns,
    )
    return engine, searched_queries, fetched_urls


def test_batch_queries_ref_ids_open_pagination_and_find(tmp_path: Path):
    engine, searched_queries, fetched_urls = _make_engine(tmp_path)
    ctx = {"task_id": "task_ws_1", "context_epoch": 0, "scope_revision": 1}

    # 1. Batch search (`queries`) assigns stable `web_1`, `web_2` refs and filters out SSRF hits
    search_res = engine.execute(
        {"queries": ["python urllib parse", "rust getting started"]},
        context=ctx,
    )
    assert search_res["mode"] == "live"
    assert search_res["untrusted_external_web"] is True
    assert search_res["origin"]["kind"] == "external_web"
    assert search_res["origin"]["untrusted"] is True
    assert ctx.get("external_web_observed") is True
    assert len(search_res["searches"]) == 2
    assert searched_queries == ["python urllib parse", "rust getting started"]
    # Loopback URL from provider was automatically stripped
    urls = [item["url"] for item in search_res["results"]]
    assert "http://127.0.0.1:8080/internal-admin" not in urls
    assert search_res["results"][0]["ref_id"] == "web_1"
    assert search_res["results"][1]["ref_id"] == "web_2"

    # 2. Open by `ref_id` with line pagination (`start_line`, `max_lines`)
    open_page_1 = engine.execute(
        {"open": "web_1", "start_line": 1, "max_lines": 4},
        context=ctx,
    )
    page1 = open_page_1["page"]
    assert page1["ref_id"] == "web_1"
    assert page1["url"] == "https://docs.python.org/3/library/urllib.parse.html"
    assert page1["cached"] is False
    assert page1["start_line"] == 1
    assert page1["end_line"] == 4
    assert page1["total_lines"] > 4
    assert page1["has_more"] is True
    assert page1["next_start_line"] == 5
    assert "[1] # Ryn Agent Web Search Architecture" in page1["content"]
    assert len(fetched_urls) == 1

    # 3. Second open of same `ref_id` (next page) uses session/ReaderCache without re-fetching HTTP
    open_page_2 = engine.execute(
        {"open": "web_1", "start_line": page1["next_start_line"], "max_lines": 20},
        context=ctx,
    )
    page2 = open_page_2["page"]
    assert page2["cached"] is True
    assert page2["start_line"] == 5
    assert page2["has_more"] is False
    assert len(fetched_urls) == 1

    # 4. In-page `find` on `web_1` locates matching lines and surrounding context
    find_res = engine.execute(
        {"open": "web_1", "find": "ReaderCache"},
        context=ctx,
    )
    assert find_res["find"]["total_matches"] >= 1
    match = find_res["find"]["matches"][0]
    assert match["ref_id"] == "web_1"
    assert "ReaderCache" in match["text"]
    assert f"[{match['line']}]" in match["excerpt"]


def test_web_search_modes_live_cached_disabled_and_disk_persistence(tmp_path: Path):
    engine, searched_queries, fetched_urls = _make_engine(tmp_path)
    ctx = {"task_id": "task_ws_mode", "context_epoch": 0, "scope_revision": 1}

    # Populate cache in live mode
    engine.execute({"query": "python urllib", "open": "https://docs.python.org/3/library/urllib.parse.html"}, context=ctx)
    assert len(searched_queries) == 1
    assert len(fetched_urls) == 1

    # Create a brand new engine instance pointing at the same cache directory (simulating process restart)
    engine2, searched_queries2, fetched_urls2 = _make_engine(tmp_path)
    ctx2 = {"task_id": "task_ws_mode_2", "context_epoch": 0, "scope_revision": 1}

    # In `cached` mode, both previously cached query and cached article succeed with zero network calls
    cached_res = engine2.execute(
        {
            "query": "python urllib",
            "open": "https://docs.python.org/3/library/urllib.parse.html",
            "mode": "cached",
        },
        context=ctx2,
    )
    assert cached_res["mode"] == "cached"
    assert cached_res["searches"][0]["cached"] is True
    assert cached_res["page"]["cached"] is True
    assert searched_queries2 == []
    assert fetched_urls2 == []

    # Opening an uncached URL in `cached` mode fails closed with `agent_web_search_cache_miss`
    with pytest.raises(AgentToolRejected, match="agent_web_search_cache_miss"):
        engine2.execute(
            {"open": "https://rust-lang.org/uncached-page", "mode": "cached"},
            context=ctx2,
        )

    # `disabled` mode (via argument or task local_execution policy) rejects calls
    with pytest.raises(AgentToolRejected, match="agent_web_search_disabled"):
        engine2.execute({"query": "rust", "mode": "disabled"}, context=ctx2)

    ctx_disabled_policy = {
        "task_id": "task_disabled",
        "local_execution": {"web_search_mode": "disabled"},
    }
    with pytest.raises(AgentToolRejected, match="agent_web_search_disabled"):
        engine2.execute({"query": "rust"}, context=ctx_disabled_policy)


def test_domains_allowlist_and_ssrf_protection(tmp_path: Path):
    engine, _, _ = _make_engine(tmp_path)
    ctx = {"task_id": "task_ssrf", "context_epoch": 0, "scope_revision": 1}

    assert normalize_domains(["*.Docs.Python.org.", "https://rust-lang.org/learn"]) == [
        "docs.python.org",
        "rust-lang.org",
    ]
    assert domain_matches("docs.python.org", ["python.org"])
    assert not domain_matches("evilpython.org", ["python.org"])

    # 1. Domain filter restricts search results to matching domains only
    res = engine.execute({"query": "getting started", "domains": ["rust-lang.org"]}, context=ctx)
    assert [r["domain"] for r in res["results"]] == ["rust-lang.org"]

    # 2. Disallowed domain on `open` is rejected
    with pytest.raises(AgentToolRejected, match="agent_web_search_domain_not_allowed"):
        engine.execute(
            {"open": "https://docs.python.org/3/library/urllib.parse.html", "domains": ["rust-lang.org"]},
            context=ctx,
        )

    # 3. Task-level `web_allowed_domains` cannot be widened by tool call arguments
    restricted_ctx = {
        "task_id": "task_restricted_dom",
        "local_execution": {"web_allowed_domains": ["docs.python.org"]},
    }
    with pytest.raises(AgentToolRejected, match="agent_web_search_domain_not_allowed"):
        engine.execute({"query": "rust", "domains": ["rust-lang.org"]}, context=restricted_ctx)

    # 4. SSRF checks block loopback, private RFC1918, link-local metadata, non-http schemes, DNS rebinding, and redirect hops
    for blocked_url in (
        "http://127.0.0.1:8000/api",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/router",
        "http://10.0.0.1/internal",
        "file:///etc/passwd",
        "https://evil-internal.example.com/secret",
        "https://metadata.example.com/token",
        "https://docs.python.org/redirect-to-local",
    ):
        with pytest.raises(AgentToolRejected, match="agent_web_search_blocked_url"):
            engine.execute({"open": blocked_url}, context=ctx)


def test_ddg_and_bing_html_parsers_extract_clean_hits():
    ddg_html = """
    <div class="results">
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=123">Python 3 Documentation</a>
        <a class="result__snippet">Official Python 3 language reference and library docs.</a>
      </div>
    </div>
    """
    ddg = _DuckDuckGoHtmlParser()
    ddg.feed(ddg_html)
    ddg.close()
    assert ddg.results == [
        {
            "url": "https://docs.python.org/3/",
            "title": "Python 3 Documentation",
            "snippet": "Official Python 3 language reference and library docs.",
        }
    ]

    bing_html = """
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://tokio.rs/tokio/tutorial">Tokio Tutorial</a></h2>
        <div class="b_caption"><p>Learn asynchronous Rust with Tokio runtime.</p></div>
      </li>
    </ol>
    """
    bing = _BingHtmlParser()
    bing.feed(bing_html)
    bing.close()
    assert bing.results == [
        {
            "url": "https://tokio.rs/tokio/tutorial",
            "title": "Tokio Tutorial",
            "snippet": "Learn asynchronous Rust with Tokio runtime.",
        }
    ]


def test_local_tools_and_code_mode_integration_without_desktop_access(tmp_path: Path):
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws), desktop_access=False))
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = agent_store
    engine, _, _ = _make_engine(tmp_path)
    local._web_search = engine

    # Verify `ryn_web_search` is in CORE_TOOLS, PARALLEL_READS, and passes strict schema normalization
    assert "ryn_web_search" in tool_menu.CORE_TOOLS
    assert "ryn_web_search" in loop.PARALLEL_READS
    strict_params = codex_tools._strict_parameters(copy.deepcopy(LOCAL_SPECS["ryn_web_search"][3]))
    assert strict_params["additionalProperties"] is False
    assert "queries" in strict_params["required"]

    # Execute via `LocalTools` with `desktop_access=False`
    ctx = {
        "task_id": "task_code_web",
        "context_epoch": 0,
        "scope_revision": 1,
        "local_execution": {"workspace_root": str(ws), "desktop_access": False},
    }
    direct_res = local.execute(
        "ryn_web_search",
        {"query": "python urllib"},
        operation_id="op_ws_direct",
        context=ctx,
        expected_preview=None,
        cancelled=lambda: False,
    )
    assert direct_res["results"][0]["ref_id"] == "web_1"

    # Execute inside `ryn_code_exec` (`CodeModeRunner`) to batch search + open + find in one turn
    class _HostTools:
        def __init__(self):
            self.local = local
            self.runner = code_mode.CodeModeRunner(self)

        def specs(self):
            return [
                {"type": "function", "function": {"name": k, "description": v[0], "parameters": copy.deepcopy(v[3])}}
                for k, v in LOCAL_SPECS.items()
            ] + [copy.deepcopy(CODE_MODE_SPEC)]

        def metadata(self, name: str):
            if name == code_mode.NAME:
                return {"effect": "read", "title": "Execute CodeMode JS", "scope_context_supported": True}
            desc, eff, risk, _ = LOCAL_SPECS[name]
            return {"effect": eff, "title": desc, "risk": risk, "scope_context_supported": True}

        def execute(self, name: str, arguments: dict, *, operation_id: str, cancelled, context=None, expected_preview=None):
            if name == code_mode.NAME:
                return self.runner.execute(arguments, operation_id=operation_id, cancelled=cancelled, context=context)
            return self.local.execute(
                name,
                arguments,
                operation_id=operation_id,
                context=context,
                expected_preview=expected_preview,
                cancelled=cancelled,
            )

    host = _HostTools()
    script = """
const search = await tools.ryn_web_search({ queries: ["python urllib", "rust cargo"] });
const opened = await tools.ryn_web_search({ open: search.results[0].ref_id, max_lines: 6, find: "ReaderCache" });
return {
  first_ref: search.results[0].ref_id,
  total_lines: opened.page.total_lines,
  matches: opened.find.total_matches,
};
"""
    code_res = host.execute(
        "ryn_code_exec",
        {"code": script},
        operation_id="op_code_ws",
        cancelled=lambda: False,
        context=ctx,
    )
    assert code_res["status"] == "completed", code_res
    assert code_res["return_value"]["first_ref"] == "web_1"
    assert code_res["return_value"]["total_lines"] > 4
    assert code_res["return_value"]["matches"] >= 1


def test_markdown_fetch_task_wide_find_and_forget_cleanup(tmp_path: Path):
    engine, _, _ = _make_engine(tmp_path)
    ctx = {"task_id": "task_md_1", "context_epoch": 0, "scope_revision": 1}

    # Open a markdown URL directly
    md_open = engine.execute(
        {"open": "https://docs.python.org/guide.md"},
        context=ctx,
    )
    assert md_open["page"]["ref_id"] == "web_1"
    assert "BETA_42" in md_open["page"]["content"]

    # Task-wide `find` (omitting `open`) searches across all refs registered in the current task session
    task_find = engine.execute({"find": "BETA_42"}, context=ctx)
    assert task_find["find"]["total_matches"] == 1
    assert task_find["find"]["matches"][0]["ref_id"] == "web_1"

    # Empty command and > 4 queries are rejected with clear error codes
    with pytest.raises(AgentToolRejected, match="agent_web_search_empty_command"):
        engine.execute({}, context=ctx)
    with pytest.raises(AgentToolRejected, match="agent_tool_arguments_invalid"):
        engine.execute({"queries": ["q1", "q2", "q3", "q4", "q5"]}, context=ctx)

    # `forget_task` clears the task's `ref_id` table
    engine.forget_task("task_md_1")
    with pytest.raises(AgentToolRejected, match="agent_web_search_unknown_ref"):
        engine.execute({"open": "web_1"}, context=ctx)
