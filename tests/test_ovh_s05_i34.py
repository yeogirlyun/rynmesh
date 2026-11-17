"""S05-I34: real download/fetch paths pin validated DNS answers."""
import urllib.request
from pathlib import Path

import httpx
import pytest

from test_agent_local_tools import execute, local as local
from rynmesh.agent_runtime import downloads, web_search
from rynmesh.agent_runtime.store import AgentToolRejected


PUBLIC = "93.184.216.34"
OTHER = "1.1.1.1"
URL = "https://files.example.test:8443/first"


@pytest.fixture
def network(monkeypatch):
    client_class = httpx.Client
    requests, lookups, clients = [], [], []
    config = {"respond": lambda request: httpx.Response(200, content=b"downloaded")}

    def dns(host):
        lookups.append(host)
        return [PUBLIC] if lookups.count(host) == 1 else ["127.0.0.1"]

    def connect(request):
        requests.append(request)
        # A hostname connection would perform another DNS lookup; pinning must
        # prevent that second answer from ever choosing the connection target.
        if request.url.host.endswith(".example.test"):
            dns(request.url.host)
        return config["respond"](request)

    def client(**kwargs):
        clients.append(kwargs)
        return client_class(transport=httpx.MockTransport(connect), trust_env=False,
                            headers=kwargs.get("headers"))

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(web_search, "_default_dns_resolver", dns)
    return config, requests, lookups, clients, dns


def test_t1_download_pins_each_redirect_and_preserves_host_sni(local, network):
    tools, context, root, _ = local
    config, requests, lookups, _, _ = network

    def respond(request):
        assert request.url.host == PUBLIC
        if len(requests) == 1:
            assert request.headers["host"] == "files.example.test:8443"
            assert request.extensions["sni_hostname"] == "files.example.test"
            return httpx.Response(302, headers={"location": "https://cdn.example.test/report.bin"})
        assert request.headers["host"] == "cdn.example.test"
        assert request.extensions["sni_hostname"] == "cdn.example.test"
        return httpx.Response(200, content=b"downloaded")

    config["respond"] = respond
    result = execute(tools, context, "ryn_download_file", {"source": URL})
    assert Path(result["path"]).name == "report.bin"
    assert Path(result["path"]).read_bytes() == b"downloaded"
    assert lookups == ["files.example.test", "cdn.example.test"]
    assert not list(root.glob(".ryn-download-*"))


@pytest.mark.parametrize("url,code", [
    ("ftp://files.example.test/a", "blocked_url"),
    ("http://127.0.0.1/private", "blocked_url"),
    ("", "invalid_url"),
])
def test_t2_download_errors_name_the_download_tool(local, network, url, code):
    tools, context, _, _ = local
    with pytest.raises(AgentToolRejected) as caught:
        downloads.save(tools, url, context, lambda: False)
    assert str(caught.value) == "agent_download_" + code
    assert "ryn_web_search" not in caught.value.correction
    if url.startswith("ftp:"):
        assert caught.value.correction == "Only http:// and https:// URLs are supported by ryn_download_file."


@pytest.mark.parametrize("case,code", [("dns", "dns_failed"), ("domain", "domain_not_allowed")])
def test_t2_resolver_codes_and_legacy_default(case, code):
    options = {"dns_resolver": lambda host: []} if case == "dns" else {"allowed_domains": ["allowed.example.test"]}
    with pytest.raises(AgentToolRejected) as caught:
        web_search.validate_public_http_url(URL, tool="ryn_download_file", **options)
    assert str(caught.value) == "agent_download_" + code
    assert "ryn_web_search" not in caught.value.correction
    with pytest.raises(AgentToolRejected, match="^agent_web_search_" + code + "$"):
        web_search.validate_public_http_url(URL, **options)


@pytest.mark.parametrize("failure", ["ftp_redirect", "transport_value_error"])
def test_t3_plugin_invalid_redirect_is_not_executed(local, network, monkeypatch, failure):
    tools, context, root, state = local
    config, requests, _, _, _ = network
    state.transaction(lambda data: data["tasks"]["task"].update(download_refs={
        "fileRef_fixture": {"url": URL, "name": "plugin.bin", "binding": {},
                            "scope_revision": context.get("scope_revision")}
    }))
    monkeypatch.setattr(tools, "download_authority",
                        lambda *args: ({"Authorization": "Bearer synthetic"}, downloads.origin(URL)), raising=False)

    def respond(request):
        assert request.url.host == "files.example.test"
        if failure == "transport_value_error":
            raise ValueError("invalid HTTP URL")
        return httpx.Response(302, headers={"location": "ftp://x/report.bin"})

    config["respond"] = respond
    with pytest.raises(AgentToolRejected, match="^agent_download_redirect_invalid$") as caught:
        execute(tools, context, "ryn_download_file", {"source": "fileRef_fixture"})
    assert caught.value.correction == "The server redirected to an address that is not an http(s) URL."
    assert len(requests) == 1
    row = tools.ledger_factory().get("op")
    assert row["status"] == "not_executed" and row["error"] == "agent_download_redirect_invalid"
    assert list(root.iterdir()) == []


def test_t4_fetch_pins_each_hop_and_returns_logical_url(tmp_path, network):
    config, requests, lookups, clients, dns = network

    def respond(request):
        assert request.url.host == PUBLIC
        if len(requests) == 1:
            assert request.headers["host"] == "files.example.test:8443"
            assert request.extensions["sni_hostname"] == "files.example.test"
            return httpx.Response(307, headers={"location": "https://cdn.example.test/page"})
        assert request.headers["host"] == "cdn.example.test"
        assert request.extensions["sni_hostname"] == "cdn.example.test"
        return httpx.Response(200, content=b"readable page", headers={"content-type": "text/plain"})

    config["respond"] = respond
    engine = web_search.WebSearchEngine(tmp_path / "cache", dns_resolver=dns)
    assert engine._fetch_http_bytes(URL, []) == (b"readable page", "https://cdn.example.test/page", "text/plain")
    assert lookups == ["files.example.test", "cdn.example.test"]
    assert all(client["trust_env"] is False for client in clients)


@pytest.mark.parametrize("ips", [[PUBLIC, OTHER], ["2606:4700:4700::1111", OTHER]])
def test_pinned_stream_tries_next_ip_and_formats_ipv6(network, ips):
    config, requests, _, _, _ = network

    def respond(request):
        assert request.headers["host"] == "files.example.test:8443"
        assert request.extensions["sni_hostname"] == "files.example.test"
        if len(requests) == 1:
            assert request.url.host == ips[0]
            if ":" in ips[0]:
                assert str(request.url).startswith("https://[" + ips[0] + "]:8443/")
            raise httpx.ConnectError("synthetic unavailable", request=request)
        assert request.url.host == OTHER
        return httpx.Response(200, content=b"next address")

    config["respond"] = respond
    with httpx.Client() as client:
        with web_search.pinned_stream(client, "GET", URL, ips, {"X-Fixture": "kept"}) as response:
            assert response.read() == b"next address"
    assert requests[-1].headers["x-fixture"] == "kept"


def test_all_addresses_failing_is_download_network_error(local, network, monkeypatch):
    tools, context, root, _ = local
    config, requests, _, _, _ = network
    monkeypatch.setattr(web_search, "_default_dns_resolver", lambda host: [PUBLIC, OTHER])

    def respond(request):
        raise httpx.ConnectTimeout("synthetic timeout", request=request)

    config["respond"] = respond
    with pytest.raises(AgentToolRejected, match="^agent_download_network_error$"):
        execute(tools, context, "ryn_download_file", {"source": URL})
    assert [request.url.host for request in requests] == [PUBLIC, OTHER]
    assert tools.ledger_factory().get("op")["status"] == "not_executed"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("kind", ["download", "fetch"])
def test_proxy_requests_keep_hostname(local, tmp_path, network, monkeypatch, kind):
    tools, context, _, _ = local
    _, requests, _, clients, dns = network
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://proxy.example.test:8080"})
    if kind == "download":
        assert downloads.save(tools, URL, context, lambda: False)["written"]
        assert clients[0]["proxy"] == "http://proxy.example.test:8080"
    else:
        engine = web_search.WebSearchEngine(tmp_path / "cache", dns_resolver=dns)
        monkeypatch.setattr(engine, "_proxy_active", lambda url: True)
        assert engine._fetch_http_bytes(URL, [])[0] == b"downloaded"
        assert clients[0]["trust_env"] is True
    assert requests[0].url.host == "files.example.test"
    assert "sni_hostname" not in requests[0].extensions


def test_literal_ip_resolution_and_legacy_return_value():
    url = "https://[2606:4700:4700::1111]/file"
    assert web_search.resolve_public_http_url(url) == (url, "2606:4700:4700::1111", ["2606:4700:4700::1111"])
    assert web_search.validate_public_http_url(url) == url


@pytest.mark.parametrize("kind", ["download", "fetch"])
def test_redirect_to_private_dns_is_blocked_before_connection(local, tmp_path, network, monkeypatch, kind):
    tools, context, root, _ = local
    config, requests, _, _, _ = network
    dns = lambda host: [PUBLIC] if host == "files.example.test" else ["127.0.0.1"]
    monkeypatch.setattr(web_search, "_default_dns_resolver", dns)
    config["respond"] = lambda request: httpx.Response(302, headers={"location": "https://internal.example.test/file"})
    code = "agent_download_blocked_url" if kind == "download" else "agent_web_search_blocked_url"
    with pytest.raises(AgentToolRejected, match="^" + code + "$"):
        if kind == "download":
            execute(tools, context, "ryn_download_file", {"source": URL})
        else:
            engine = web_search.WebSearchEngine(tmp_path / "cache", dns_resolver=dns)
            engine._fetch_http_bytes(URL, [])
    assert len(requests) == 1 and requests[0].url.host == PUBLIC
    assert list(root.iterdir()) == []
    if kind == "download":
        assert tools.ledger_factory().get("op")["status"] == "not_executed"
