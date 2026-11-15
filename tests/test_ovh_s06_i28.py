"""Tests for S06-I28: proxy/fake-ip web search tolerance and DNS-free search hit filtering."""
from __future__ import annotations

import time
import urllib.request
from pathlib import Path

import pytest

from rynmesh.agent_runtime import web_search
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.web_search import (
    WebSearchEngine,
    _is_forbidden_ip,
    validate_public_http_url,
)

SAMPLE_HTML = b"""<!DOCTYPE html>
<html>
<head><title>Proxy Tolerant Reader Page</title></head>
<body>
  <article>
    <h1>Proxy Tolerant Reader Page</h1>
    <p>Verified content loaded through fake-ip or upstream HTTP proxy.</p>
  </article>
</body>
</html>
"""


def test_t1_fake_ip_benchmark_range_resolved_host_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RYN_WEB_SEARCH_ALLOW_CIDRS", raising=False)
    assert _is_forbidden_ip("198.18.0.5", resolved=True) is False
    assert _is_forbidden_ip("198.19.255.254", resolved=True) is False

    url = "https://docs.example.com/guide"
    assert validate_public_http_url(url, dns_resolver=lambda _h: ["198.18.0.5"]) == url

    engine = WebSearchEngine(
        tmp_path / "ws_t1",
        http_fetcher=lambda u, _domains: (SAMPLE_HTML, u, "text/html; charset=utf-8"),
        dns_resolver=lambda _h: ["198.18.0.5"],
    )
    res = engine.execute({"open": url}, context={"task_id": "t1"})
    assert res["page"]["url"] == url
    assert "Proxy Tolerant Reader Page" in res["page"]["content"]


def test_t2_literal_fake_ip_url_still_rejected(tmp_path: Path) -> None:
    assert _is_forbidden_ip("198.18.0.5", resolved=False) is True
    assert _is_forbidden_ip("198.18.0.5") is True

    with pytest.raises(AgentToolRejected) as exc_direct:
        validate_public_http_url("http://198.18.0.5/", resolve=True)
    assert str(exc_direct.value) == "agent_web_search_blocked_url"

    engine = WebSearchEngine(
        tmp_path / "ws_t2",
        http_fetcher=lambda u, _domains: (SAMPLE_HTML, u, "text/html; charset=utf-8"),
        dns_resolver=lambda _h: ["93.184.216.34"],
    )
    with pytest.raises(AgentToolRejected) as exc_open:
        engine.execute({"open": "http://198.18.0.5/"}, context={"task_id": "t2"})
    assert str(exc_open.value) == "agent_web_search_blocked_url"


def test_t3_allow_cidrs_env_allows_resolved_ips_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RYN_WEB_SEARCH_ALLOW_CIDRS", raising=False)
    with pytest.raises(AgentToolRejected) as exc_before:
        validate_public_http_url("https://custom-tun.example.com/", dns_resolver=lambda _h: ["198.51.100.5"])
    assert str(exc_before.value) == "agent_web_search_blocked_url"

    monkeypatch.setenv("RYN_WEB_SEARCH_ALLOW_CIDRS", "not-a-cidr, 100.100.0.0/16, 198.51.100.0/24")
    assert _is_forbidden_ip("100.100.0.5", resolved=True) is False
    assert _is_forbidden_ip("198.51.100.5", resolved=True) is False
    assert _is_forbidden_ip("198.51.100.5", resolved=False) is True

    assert (
        validate_public_http_url(
            "https://tailscale-dns.example.com/doc",
            dns_resolver=lambda _h: ["100.100.0.5"],
        )
        == "https://tailscale-dns.example.com/doc"
    )
    assert (
        validate_public_http_url(
            "https://custom-tun.example.com/doc",
            dns_resolver=lambda _h: ["198.51.100.5"],
        )
        == "https://custom-tun.example.com/doc"
    )

    with pytest.raises(AgentToolRejected) as exc_literal:
        validate_public_http_url("http://198.51.100.5/doc")
    assert str(exc_literal.value) == "agent_web_search_blocked_url"

    # At most 8 segments are honored; the 9th segment (203.0.113.0/24) must be ignored.
    nine_segments = ",".join([f"10.{i}.0.0/16" for i in range(1, 9)] + ["203.0.113.0/24"])
    monkeypatch.setenv("RYN_WEB_SEARCH_ALLOW_CIDRS", nine_segments)
    assert _is_forbidden_ip("10.8.1.1", resolved=True) is False
    assert _is_forbidden_ip("203.0.113.9", resolved=True) is True


def test_t4_empty_dns_with_active_proxy_allowed_without_proxy_dns_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    getproxies_calls = 0

    def fake_getproxies_active() -> dict[str, str]:
        nonlocal getproxies_calls
        getproxies_calls += 1
        return {"https": "http://127.0.0.1:7890"}

    monkeypatch.setattr(urllib.request, "getproxies", fake_getproxies_active)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == "bypass.example.com")

    assert web_search._proxy_active("https://docs.example.com/a") is True
    assert web_search._proxy_active("https://another.example.com/b") is True
    assert getproxies_calls == 1
    assert web_search._proxy_active("https://bypass.example.com/c") is False
    assert getproxies_calls == 1
    assert web_search._proxy_active("http://docs.example.com/a") is False
    assert getproxies_calls == 2

    engine = WebSearchEngine(
        tmp_path / "ws_t4",
        http_fetcher=lambda u, _domains: (SAMPLE_HTML, u, "text/html; charset=utf-8"),
        dns_resolver=lambda _h: [],
    )
    opened = engine.execute({"open": "https://docs.example.com/page"}, context={"task_id": "t4_proxy"})
    assert opened["page"]["url"] == "https://docs.example.com/page"
    assert "Proxy Tolerant Reader Page" in opened["page"]["content"]

    with pytest.raises(AgentToolRejected) as exc_bypassed:
        engine.execute({"open": "https://bypass.example.com/page"}, context={"task_id": "t4_bypass"})
    assert str(exc_bypassed.value) == "agent_web_search_dns_failed"

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    assert web_search._proxy_active("https://docs.example.com/uncached") is False

    with pytest.raises(AgentToolRejected) as exc_no_proxy:
        engine.execute({"open": "https://docs.example.com/uncached"}, context={"task_id": "t4_noproxy"})
    assert str(exc_no_proxy.value) == "agent_web_search_dns_failed"


def test_t5_active_proxy_with_private_ip_resolution_still_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RYN_WEB_SEARCH_ALLOW_CIDRS", raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:7890"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)

    engine = WebSearchEngine(
        tmp_path / "ws_t5",
        http_fetcher=lambda u, _domains: (SAMPLE_HTML, u, "text/html; charset=utf-8"),
        dns_resolver=lambda _h: ["10.0.0.5"],
    )
    with pytest.raises(AgentToolRejected) as exc:
        engine.execute({"open": "https://intranet-leak.example.com/admin"}, context={"task_id": "t5"})
    assert str(exc.value) == "agent_web_search_blocked_url"
    assert exc.value.correction is not None
    assert (
        "resolved to non-public IP 10.0.0.5; if you use a proxy/TUN fake-ip, set RYN_WEB_SEARCH_ALLOW_CIDRS"
        in exc.value.correction
    )


def test_t6_search_phase_skips_dns_for_20_hits_and_reports_filtered_count(tmp_path: Path) -> None:
    dns_calls = 0

    def slow_dns(_hostname: str) -> list[str]:
        nonlocal dns_calls
        dns_calls += 1
        time.sleep(0.2)
        return ["93.184.216.34"]

    def twenty_hits_provider(_query: str, _max_results: int, _domains: list[str]) -> list[dict[str, str]]:
        blocked = [
            {"url": "http://127.0.0.1:8080/internal", "title": "Loopback", "snippet": "blocked 1"},
            {"url": "http://localhost/admin", "title": "Localhost", "snippet": "blocked 2"},
            {"url": "ftp://files.example.com/pub", "title": "FTP", "snippet": "blocked 3"},
        ]
        valid = [
            {
                "url": f"https://hit-{idx}.example.com/article",
                "title": f"Result {idx}",
                "snippet": f"Snippet {idx}",
            }
            for idx in range(1, 18)
        ]
        return blocked + valid

    engine = WebSearchEngine(
        tmp_path / "ws_t6",
        search_provider=twenty_hits_provider,
        dns_resolver=slow_dns,
    )
    t0 = time.perf_counter()
    res = engine.execute({"query": "distributed consensus", "max_results": 10}, context={"task_id": "t6"})
    elapsed = time.perf_counter() - t0

    assert dns_calls == 0
    assert elapsed < 0.5
    assert len(res["searches"]) == 1
    assert res["searches"][0]["filtered"] == 3
    assert len(res["searches"][0]["results"]) == 10


def test_t7_open_phase_and_redirects_still_enforce_full_dns_ssrf_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)

    dns_queried: list[str] = []

    def fake_dns(hostname: str) -> list[str]:
        dns_queried.append(hostname)
        if hostname in {"rebind-target.example.com", "internal-hop.example.com"}:
            return ["10.0.0.8"]
        return ["93.184.216.34"]

    def fake_fetch(url: str, _domains: list[str]) -> tuple[bytes, str, str]:
        if "redirect-hop" in url:
            return b"redirected", "https://internal-hop.example.com/secret", "text/html"
        return SAMPLE_HTML, url, "text/html; charset=utf-8"

    engine = WebSearchEngine(
        tmp_path / "ws_t7",
        search_provider=lambda _q, _m, _d: [
            {
                "url": "https://rebind-target.example.com/post",
                "title": "Rebind Post",
                "snippet": "Looks public syntactically",
            }
        ],
        http_fetcher=fake_fetch,
        dns_resolver=fake_dns,
    )
    ctx = {"task_id": "t7"}
    search_res = engine.execute({"query": "rebind check"}, context=ctx)
    assert len(search_res["results"]) == 1
    assert dns_queried == []

    with pytest.raises(AgentToolRejected) as exc_open:
        engine.execute({"open": search_res["results"][0]["ref_id"]}, context=ctx)
    assert str(exc_open.value) == "agent_web_search_blocked_url"
    assert "rebind-target.example.com" in dns_queried

    with pytest.raises(AgentToolRejected) as exc_redirect:
        engine.execute({"open": "https://public-entry.example.com/redirect-hop"}, context=ctx)
    assert str(exc_redirect.value) == "agent_web_search_blocked_url"
    assert "public-entry.example.com" in dns_queried
    assert "internal-hop.example.com" in dns_queried
