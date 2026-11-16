"""S13-I30: token failure reasons travel through the production app wiring."""
import asyncio
import json

import pytest

from rynmesh.agent_runtime.store import AgentError
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore
from test_company_channel_identity import CompanyCenter
from test_peer_providers import MANIFEST


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network"))
    ext = app.state.agent_runtime.tools.extensions
    service = app.state.peer_providers
    servers = [{"id": "companion", "plugin_id": "fixture", "effective_enabled": True}]
    monkeypatch.setattr(ext.store, "list_peer_providers_internal", lambda: [
        {"plugin_id": "fixture", "peer_provider": MANIFEST, "instance_id": "fixture-instance", "enabled": True}
    ])
    monkeypatch.setattr(ext.store, "list_mcp_servers_internal", lambda: servers)
    service.sync_from_extensions()
    yield service, ext.manager.oauth, servers
    service.close()
    ext.manager.oauth.close()


@pytest.mark.parametrize("code", ["agent_mcp_auth_required", "agent_mcp_oauth_server_disabled"])
def test_t1_agent_failure_reaches_status(host, monkeypatch, code):
    service, oauth, _ = host

    def unavailable(server):
        raise AgentError(code)

    monkeypatch.setattr(oauth, "access_token", unavailable)
    result = asyncio.run(service.auto_sync("fixture"))
    assert result["status"] == "signed_out"
    assert result["sign_in_error"] == code
    assert service.status("fixture")["sign_in_error"] == code
    assert service.mcp_token_source("fixture") is None
    assert service.mcp_token_failure("fixture") == code


def test_t2_no_companion_reports_missing(host):
    service, _, servers = host
    servers.clear()
    assert asyncio.run(service.auto_sync("fixture"))["sign_in_error"] == "peer_provider_companion_missing"
    assert service.mcp_token_failure("fixture") == "peer_provider_companion_missing"


def test_t3_token_success_connects_and_clears_previous_failure(host, monkeypatch):
    service, oauth, servers = host
    original = list(servers)
    servers.clear()
    asyncio.run(service.auto_sync("fixture"))
    servers.extend(original)
    monkeypatch.setattr(oauth, "access_token", lambda server: "plugin-token-ziling")
    center = CompanyCenter()
    service.clock = lambda: center.clock[0]
    service.oauth.clock = service.clock
    service.oauth.transport = center.transport
    result = asyncio.run(service.auto_sync("fixture"))
    assert result["status"] == "connected" and result["bound"] is True
    assert not result.get("sign_in_error")
    assert service.mcp_token_failure("fixture") is None
    assert service.mcp_token_source("fixture") == "plugin-token-ziling"
    assert center.exchanges == 1
    assert "plugin-token" not in json.dumps(result)


def test_last_failure_is_used_and_unknown_exceptions_are_sanitized(host, monkeypatch):
    service, oauth, servers = host
    servers.append({**servers[0], "id": "last"})

    def unavailable(server):
        if server["id"] == "companion":
            raise AgentError("agent_mcp_auth_required")
        raise RuntimeError("https://secret.example/?token=synthetic-secret")

    monkeypatch.setattr(oauth, "access_token", unavailable)
    result = asyncio.run(service.auto_sync("fixture"))
    assert result["sign_in_error"] == "agent_mcp_token_unavailable"
    assert "synthetic-secret" not in json.dumps(result)


def test_empty_token_has_unavailable_reason(host, monkeypatch):
    service, oauth, _ = host
    monkeypatch.setattr(oauth, "access_token", lambda server: None)
    assert asyncio.run(service.auto_sync("fixture"))["sign_in_error"] == "agent_mcp_token_unavailable"
