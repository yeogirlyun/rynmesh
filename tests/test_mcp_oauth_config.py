"""OAuth configuration and import identity boundaries; never connects a server."""
import json
import socket

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionImportManager, ExtensionStore
from rynmesh.agent_runtime.extensions.store import normalize_mcp_auth
from rynmesh.agent_runtime.store import AgentError

DEFAULT = {"mode": "auto", "client_id": None, "scopes": [], "callback_port": None}
AUTH = {"mode": "oauth", "client_id": "desktop", "scopes": ["profile.read", "tools.read"], "callback_port": 17861}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("unexpected test network")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)


@pytest.fixture
def store(tmp_path):
    return ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())


def add_server(store, **values):
    return store.upsert_mcp_server({"id": "fixture", "transport": "streamable_http", "url": "https://mcp.example.test/tools", **values})


def test_public_auth_defaults_and_legacy_migration_keep_config_identity(store):
    row = add_server(store)
    assert row["auth"] == DEFAULT
    legacy = store._read()
    legacy["mcp_servers"]["fixture"].pop("auth")
    legacy["mcp_servers"]["fixture"]["config_sha256"] = "legacy-without-auth"
    store._write(legacy)
    store = ExtensionStore(store.root, store.key)
    internal = store.get_mcp_server_internal("fixture")
    assert internal["auth"] == DEFAULT and internal["config_generation"] == row["config_generation"]
    updated = store.upsert_mcp_server({"description": "Display-only change"}, server_id="fixture", expected_revision=row["revision"])
    assert updated["config_generation"] == row["config_generation"]
    values = store.list_mcp_servers_internal()
    fixture = next(value for value in values if value["id"] == "fixture")
    assert fixture["effective_enabled"] is False and fixture["instance_id"] == row["instance_id"]
    fixture["auth"]["scopes"].append("mutated")
    assert store.get_mcp_server_internal("fixture")["auth"] == DEFAULT


@pytest.mark.parametrize("field,value", [("mode", "none"), ("client_id", "another"), ("scopes", ["other.read"]), ("callback_port", 17862)])
def test_each_auth_field_changes_generation_and_rejects_stale_probes(store, field, value):
    before = add_server(store, enabled=True, auth=AUTH)
    changed = {**AUTH, field: value}
    after = store.upsert_mcp_server({"auth": changed}, server_id="fixture", expected_revision=before["revision"])
    assert after["config_generation"] > before["config_generation"]
    assert after["instance_id"] == before["instance_id"] and after["auth"] == changed
    with pytest.raises(AgentError, match="agent_extensions_revision_conflict"):
        store.get_mcp_server_internal("fixture", expected_config_generation=before["config_generation"])


def test_discovery_does_not_change_auth_generation_but_disable_reenable_does(store):
    before = add_server(store, enabled=True, auth=AUTH)
    discovered = store.record_mcp_discovery("fixture", expected_revision=before["revision"], status="ready", discovered_tools=[])
    assert discovered["config_generation"] == before["config_generation"]
    disabled = store.upsert_mcp_server({"enabled": False}, server_id="fixture", expected_revision=discovered["revision"])
    enabled = store.upsert_mcp_server({"enabled": True}, server_id="fixture", expected_revision=disabled["revision"])
    assert before["config_generation"] < disabled["config_generation"] < enabled["config_generation"]


def test_scope_order_and_duplicates_normalize_without_generation_change(store):
    before = add_server(store, auth=AUTH)
    after = store.upsert_mcp_server({"auth": {**AUTH, "scopes": ["tools.read", "profile.read", "tools.read"]}},
                                   server_id="fixture", expected_revision=before["revision"])
    assert after["auth"] == AUTH and after["config_generation"] == before["config_generation"]


@pytest.mark.parametrize("auth", [
    [], "oauth", {"mode": "unknown"}, {"mode": True}, {"client_id": ""}, {"client_id": "a" * 257},
    {"client_id": "line\nbreak"}, {"client_id": 3}, {"scopes": "tools.read"}, {"scopes": ["a b"]},
    {"scopes": ["a\nb"]}, {"scopes": ["a" * 129]}, {"scopes": ["x"] * 17}, {"scopes": [1]},
    {"callback_port": True}, {"callback_port": "17861"}, {"callback_port": 1023}, {"callback_port": 65536},
    {"client_secret": "synthetic-private"}, {"clientSecret": "synthetic-private"},
])
def test_invalid_auth_is_rejected_without_echoing_values(store, auth):
    with pytest.raises(AgentError, match="^agent_mcp_invalid_auth$"):
        add_server(store, auth=auth)
    assert not any(row["id"] == "fixture" for row in store.list_mcp_servers_internal())


def test_auth_limits_accept_boundary_values():
    for port in (1024, 65535):
        value = normalize_mcp_auth({"client_id": "a" * 256, "scopes": ["x" * 128], "callback_port": port})
        assert value["callback_port"] == port


@pytest.mark.parametrize("settings", [
    {"oauth": {"clientId": "desktop", "scopes": AUTH["scopes"], "callbackPort": 17861}},
    {"oauth": {"client_id": "desktop", "scopes": AUTH["scopes"], "callback_port": 17861}},
    {"oauthClientId": "desktop", "oauth": {"scopes": AUTH["scopes"], "callbackPort": 17861}},
    {"auth": AUTH},
])
def test_import_normalizes_public_aliases_without_unsupported_warning(store, settings):
    manager = ExtensionImportManager(store)
    preview = manager.preview({"kind": "mcp", "config_text": json.dumps({"mcpServers": {
        "fixture": {"type": "http", "url": "https://mcp.example.test/tools", **settings}}})})
    assert preview["warnings"] == [] and preview["unsupported_components"] == []
    assert preview["mcp_servers"][0]["auth"] == AUTH
    assert preview["mcp_servers"][0]["login_required"] and preview["mcp_servers"][0]["oauth_issuer"] is None
    manager.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    assert store.get_mcp_server_internal("fixture")["auth"] == AUTH
    assert store.get_mcp_server_internal("fixture")["enabled"] is False


def test_legacy_client_alias_and_auth_url_warning(store):
    manager = ExtensionImportManager(store)
    preview = manager.preview({"kind": "mcp", "config_text": json.dumps({"mcpServers": {
        "fixture": {"type": "http", "url": "https://mcp.example.test/tools", "oauthClientId": "desktop", "auth_url": "https://ignored.invalid/auth"}}})})
    assert preview["mcp_servers"][0]["auth"] == {**DEFAULT, "mode": "oauth", "client_id": "desktop"}
    assert preview["unsupported_components"] == []
    assert len(preview["warnings"]) == 1 and "auth_url is ignored" in preview["warnings"][0]
    assert "ignored.invalid" not in json.dumps(preview)


@pytest.mark.parametrize("settings", [
    {"oauth": {"client_id": "one", "clientId": "two"}},
    {"oauth": {"callback_port": 17861, "callbackPort": 17862}},
    {"oauthClientId": "one", "oauth": {"clientId": "two"}},
    {"auth": AUTH, "oauth": {}}, {"oauth": True},
    {"oauth": {"clientSecret": "synthetic-private"}}, {"oauth": {"client_secret": "synthetic-private"}},
    {"client_secret": "synthetic-private"}, {"auth": {"client_secret": "synthetic-private"}},
])
def test_ambiguous_or_secret_import_is_rejected_before_persistence(store, settings):
    manager = ExtensionImportManager(store)
    with pytest.raises(AgentError, match="^agent_mcp_invalid_auth$"):
        manager.preview({"kind": "mcp", "config_text": json.dumps({"mcpServers": {
            "fixture": {"type": "http", "url": "https://mcp.example.test/tools", **settings}}})})
    assert not any(row["id"] == "fixture" for row in store.list_mcp_servers_internal())


def test_package_secret_is_not_copied_into_installed_files(store, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "plugin.json").write_text(json.dumps({"id": "fixture", "name": "Fixture"}), encoding="utf-8")
    (package / "mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {
        "url": "https://mcp.example.test/tools", "oauth": {"clientId": "desktop", "client_secret": "synthetic-secret-canary"}}}}), encoding="utf-8")
    manager = ExtensionImportManager(store)
    with pytest.raises(AgentError, match="^agent_mcp_invalid_auth$"):
        manager.preview({"kind": "plugin", "path": str(package)})
    for path in store.root.rglob("*"):
        if path.is_file():
            assert b"synthetic-secret-canary" not in path.read_bytes()


def test_internal_commit_also_validates_auth(store):
    with pytest.raises(AgentError, match="^agent_mcp_invalid_auth$"):
        store.commit_imported_items(plugins=[], skills=[], mcp_servers=[{"id": "fixture", "auth": {"client_secret": "synthetic"}}])
    assert not any(row["id"] == "fixture" for row in store.list_mcp_servers_internal())
