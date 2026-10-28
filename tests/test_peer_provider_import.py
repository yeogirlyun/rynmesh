"""Provider declarations are inert, source-bound and never contain credentials."""
import copy
import json
import socket

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionImportManager, ExtensionStore
from rynmesh.agent_runtime.extensions.store import validate_peer_provider
from rynmesh.agent_runtime.store import AgentError

DECLARATION = {"url": "https://provider.example/connect", "oauth": {"clientId": "desktop", "scopes": ["peer.connect"]}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Unexpected test network")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)


def package(tmp_path, declaration=DECLARATION):
    path = tmp_path / "package"
    path.mkdir(exist_ok=True)
    (path / "plugin.json").write_text(json.dumps({"id": "peer-fixture", "name": "Peer fixture", "version": "1",
                                               "ryn": {"peerProvider": declaration}}), encoding="utf-8")
    return path


def test_provider_only_import_persists_disabled_and_tracks_installation(tmp_path):
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    manager = ExtensionImportManager(store)
    path = package(tmp_path)
    preview = manager.preview({"kind": "plugin", "path": str(path)})
    assert preview["can_import"] and preview["unsupported_components"] == []
    assert preview["plugins"][0]["peer_provider"] == DECLARATION
    overview = manager.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    plugin = next(row for row in overview["plugins"] if row["id"] == "peer-fixture")
    assert plugin["peer_provider"] == DECLARATION
    assert not plugin["enabled"] and not plugin["computer_access"]
    assert plugin["skills"] == [] and plugin["mcp_servers"] == []
    internal = store.list_peer_providers_internal()
    assert len(internal) == 1 and internal[0]["plugin_id"] == "peer-fixture"
    assert not internal[0]["enabled"]
    first_instance = internal[0]["instance_id"]
    internal[0]["peer_provider"]["oauth"]["scopes"].append("mutated")
    assert store.list_peer_providers_internal()[0]["peer_provider"] == DECLARATION
    store.set_plugin_enabled("peer-fixture", enabled=True, expected_revision=plugin["revision"])
    assert store.list_peer_providers_internal()[0]["enabled"]
    preview = manager.preview({"kind": "plugin", "path": str(path)})
    manager.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    replaced = store.list_peer_providers_internal()[0]
    assert replaced["instance_id"] != first_instance and not replaced["enabled"]
    store.delete_plugin("peer-fixture", expected_revision=replaced["revision"])
    assert store.list_peer_providers_internal() == []


@pytest.mark.parametrize("url", [
    "http://provider.example/connect", "https://user:password@provider.example/connect",
    "https://provider.example/connect?access_token=synthetic", "https://provider.example/connect#secret",
    "https://Provider.example/connect", "https://provider.example:443/connect",
    "https://provider.example", "https://provider.example/a/../connect", "https://provider.example/a//connect",
    "https://provider.example/%2e/connect", "https://provider.example/\\connect",
    "https://provider.example/connect\n", "https://provider.example:0/connect", "https://provider.example:99999/connect",
    "https://<invalid>/connect", "https://provider.example./connect", "https://provider.example/connect/",
])
def test_noncanonical_or_credential_url_rejected(url):
    value = copy.deepcopy(DECLARATION); value["url"] = url
    with pytest.raises(AgentError, match="^agent_invalid_peer_provider$"):
        validate_peer_provider(value)


@pytest.mark.parametrize("value", [
    None, [], {}, {**DECLARATION, "token": "synthetic"},
    {**DECLARATION, "oauth": {**DECLARATION["oauth"], "clientSecret": "synthetic"}},
    {**DECLARATION, "oauth": {"clientId": "desktop", "scopes": []}},
    {**DECLARATION, "oauth": {"clientId": "desktop", "scopes": ["peer.connect", "peer.connect"]}},
    {**DECLARATION, "oauth": {"clientId": "desktop", "scopes": ["peer connect"]}},
    {**DECLARATION, "oauth": {"clientId": "desktop", "scopes": "peer.connect"}},
    {**DECLARATION, "oauth": {"clientId": "desktop", "scopes": [1]}},
    {**DECLARATION, "oauth": {"clientId": "invalid\n", "scopes": ["peer.connect"]}},
])
def test_schema_rejected_without_echoing_credentials(tmp_path, value):
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    manager = ExtensionImportManager(store)
    with pytest.raises(AgentError, match="^agent_invalid_peer_provider$"):
        manager.preview({"kind": "plugin", "path": str(package(tmp_path, value))})
    assert store.list_peer_providers_internal() == []


def test_store_does_not_trust_internal_commit_declaration(tmp_path):
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    with pytest.raises(AgentError, match="^agent_invalid_peer_provider$"):
        store.commit_imported_items(plugins=[{"id": "bad", "peer_provider": {**DECLARATION, "headers": {}}}], skills=[], mcp_servers=[])
    assert store.list_peer_providers_internal() == []


def test_mcp_oauth_and_peer_provider_login_are_independent_declarations(tmp_path):
    path = package(tmp_path)
    (path / "mcp.json").write_text(json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://provider.example/mcp",
                                                                           "oauth": {"clientId": "desktop", "scopes": ["read"]}}}}), encoding="utf-8")
    preview = ExtensionImportManager(ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())).preview({"kind": "plugin", "path": str(path)})
    assert preview["can_import"] and preview["plugins"][0]["peer_provider"] == DECLARATION
    assert preview["mcp_servers"][0]["auth"]["client_id"] == "desktop"
    assert preview["mcp_servers"][0]["auth"]["mode"] == "oauth"
    assert preview["warnings"] == []
