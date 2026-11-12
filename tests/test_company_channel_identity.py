"""The company plugin's colleague channel: created from the plugin sign-in, owned by the same account.

The provider here answers like the company center: internal member ids are
UUID-like and differ from the company account id the plugin signs in with.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_peer_providers import MANIFEST, ORIGIN, RESOURCE, FakeServer, login, no_network, node  # noqa: F401

from rynmesh.peer_providers import install_peer_provider_routes
from rynmesh.peer_providers.oauth import ProviderHttpError
from rynmesh.peer_providers.service import companion_servers

PLUGIN = "company-workspace"
MEMBERS = {"member-uuid-1": "ziling", "member-uuid-2": "colleague"}


class CompanyCenter(FakeServer):
    """`modern=False` is a center without the companion grant, account id or publisher statement."""

    def __init__(self, *, modern=True, publishers=("ziling",)):
        super().__init__()
        self.modern, self.publishers = modern, set(publishers)
        self.plugin_tokens = {"plugin-token-ziling": "member-uuid-1", "plugin-token-colleague": "member-uuid-2"}
        self.exchanges = 0

    def respond(self, method, target, headers, body, data):
        if target == ORIGIN + "/token" and data.get("grant_type") == "urn:ryn:params:oauth:grant-type:peer-session":
            if not self.modern or data["subject_token"] not in self.plugin_tokens or data["resource"] != RESOURCE:
                raise ProviderHttpError(400)
            self.exchanges += 1
            return self.mint(RESOURCE, self.plugin_tokens[data["subject_token"]])
        result = super().respond(method, target, headers, body, data)
        subject = self.tokens.get(headers.get("Authorization", "").removeprefix("Bearer "), (None, None))[1]
        if self.modern and target == RESOURCE + "/bind":
            result["userid"] = MEMBERS[subject]
        if self.modern and target == RESOURCE + "/directory":
            result["self"] = {"member_id": subject, "userid": MEMBERS[subject],
                              "can_publish_models": MEMBERS[subject] in self.publishers}
        return result


def host(tmp_path, center, *, account="ziling", name="one"):
    """One Ryn node with the company plugin installed and signed in as `account`."""
    state = {"account": account}
    service = node(tmp_path, center, name, mcp_token_source=lambda plugin: "plugin-token-" + state["account"] if state["account"] else None)
    service.configure("test", MANIFEST, enabled=False)  # the harness default; this node uses the company plugin id
    service.configure(PLUGIN, MANIFEST)
    app = FastAPI()

    async def read_company_account():
        return {"state": "logged_in" if state["account"] else "logged_out", "memberId": state["account"] or ""}
    app.state.read_company_account = read_company_account
    install_peer_provider_routes(app, service, lambda request: None)
    return service, TestClient(app), state


def api(client, path, body=None):
    url = "/api/local/peer-providers/" + PLUGIN + "/" + path
    return client.post(url, json=body) if body is not None else client.get(url)


def test_plugin_sign_in_creates_the_channel_and_the_publisher_can_share(tmp_path):
    center = CompanyCenter()
    service, client, _ = host(tmp_path, center)
    workspace = api(client, "workspace")
    assert workspace.status_code == 200
    value = workspace.json()
    # No browser sign-in happened: the channel came from the plugin's own sign-in.
    assert center.exchanges == 1 and not center.flows
    assert value["bound"] is True and value["status"] == "connected"
    assert value["can_publish_models"] is True and value["current_member"]["can_publish_models"] is True
    # The stored channel owner is the company account id, not the center's internal id.
    binding = service._rows[PLUGIN]["binding"]
    assert binding["userid"] == "ziling" and binding["member_id"] == "member-uuid-1"
    published = api(client, "publish", {"service_ids": ["model-a"]})
    assert published.status_code == 200 and published.json()["selected_service_ids"] == ["model-a"]
    assert api(client, "catalog").status_code == 200 and api(client, "models").status_code == 200
    assert center.exchanges == 1  # reused, not re-created per request


def test_a_member_the_center_does_not_list_gets_no_publisher_flag(tmp_path):
    center = CompanyCenter(publishers=())
    _, client, _ = host(tmp_path, center)
    value = api(client, "workspace").json()
    assert value["bound"] is True and value["can_publish_models"] is False
    assert value["current_member"]["can_publish_models"] is False


def test_another_company_account_cannot_use_this_channel(tmp_path):
    center = CompanyCenter()
    service, client, state = host(tmp_path, center)
    assert api(client, "workspace").json()["bound"] is True
    state["account"] = "colleague"  # the plugin is now signed in as someone else
    for path, body in (("workspace", None), ("catalog", None), ("models", None), ("publish", {"service_ids": ["model-a"]})):
        response = api(client, path, body)
        assert (response.status_code, response.json()["detail"]) == (409, "peer_provider_login_required")
    assert service._rows[PLUGIN]["published"] == {}
    state["account"] = ""
    assert api(client, "workspace").status_code == 409


def test_center_without_the_companion_grant_leaves_the_channel_unconnected(tmp_path):
    center = CompanyCenter(modern=False)
    _, client, _ = host(tmp_path, center)
    value = api(client, "workspace").json()
    assert value["bound"] is False and value["can_publish_models"] is False
    assert api(client, "publish", {"service_ids": ["model-a"]}).status_code == 409


def test_channel_bound_before_the_center_reported_the_account_is_repaired_in_the_background(tmp_path):
    center = CompanyCenter(modern=False)
    service, client, _ = host(tmp_path, center)
    # A browser sign-in against the earlier center: bound, but the account id is unknown.
    asyncio.run(login(service, center, "member-uuid-1", PLUGIN))
    assert service.status(PLUGIN)["bound"] and "userid" not in service._rows[PLUGIN]["binding"]
    # Unknown ownership is never treated as a match.
    assert api(client, "publish", {"service_ids": ["model-a"]}).status_code == 409
    center.modern = True
    asyncio.run(service.refresh_directories())  # the background directory worker
    assert service._rows[PLUGIN]["binding"]["userid"] == "ziling"
    assert api(client, "publish", {"service_ids": ["model-a"]}).status_code == 200
    assert api(client, "workspace").json()["can_publish_models"] is True


@pytest.mark.parametrize("own", [{"userid": "ziling"}, {"userid": "", "can_publish_models": True}, {"userid": "ziling", "can_publish_models": "yes"}, "ziling"])
def test_malformed_caller_statement_is_rejected(tmp_path, own):
    center = CompanyCenter()
    service, _, _ = host(tmp_path, center)
    asyncio.run(service.auto_sync(PLUGIN))
    center.response_filter = lambda target, value: {**value, "self": own} if target.endswith("/directory") else value
    with pytest.raises(Exception):
        asyncio.run(service.directory(PLUGIN))
    assert service.cached_models(PLUGIN) == []


def test_only_a_sign_in_issued_by_the_company_center_is_offered_for_the_channel():
    company = frozenset({"company-workspace", "company-dashboard", "company-affiliate"})
    servers = [
        {"id": "local", "plugin_id": "company-workspace", "effective_enabled": True, "url": "http://127.0.0.1:18140/ryn/agent-mcp"},
        {"id": "dashboard", "plugin_id": "company-dashboard", "effective_enabled": True, "url": "https://provider.test/ryn/mcp"},
        {"id": "off", "plugin_id": "company-dashboard", "effective_enabled": False, "url": "https://provider.test/ryn/mcp"},
        {"id": "elsewhere", "plugin_id": "company-affiliate", "effective_enabled": True, "url": "https://other.test/ryn/mcp"},
        {"id": "lookalike", "plugin_id": "company-affiliate", "effective_enabled": True, "url": "https://provider.test.evil.test/ryn/mcp"},
        {"id": "plain", "plugin_id": "company-dashboard", "effective_enabled": True, "url": "http://provider.test/ryn/mcp"},
        {"id": "third", "plugin_id": "third-party", "effective_enabled": True, "url": "https://provider.test/ryn/mcp"},
    ]
    pick = lambda plugin: [row["id"] for row in companion_servers(servers, plugin, RESOURCE, company)]
    # The workspace plugin has no company-issued sign-in of its own; the Dashboard one on the same origin serves.
    assert pick("company-workspace") == ["dashboard"]
    # Another plugin only ever offers its own servers, and never borrows a company sign-in.
    assert pick("third-party") == ["third"]
    assert [row["id"] for row in companion_servers(servers, "company-workspace", "not a url", company)] == []
