"""Import-only data rules and exact-discovery read-only confirmations; no network."""
import copy
import json
import socket

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.data_policy import normalize_policy
from rynmesh.agent_runtime.extensions import ExtensionImportManager, ExtensionStore
from rynmesh.agent_runtime.extensions.store import _mcp_config_fingerprint
from rynmesh.agent_runtime.store import AgentError

DEFAULT = {"models": {"mode": "any"}, "memory": "allow"}
RESTRICTED = {"models": {"mode": "allowlist", "allow": [{"on_device": True}]}, "memory": "deny"}
SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}}


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("unexpected network access")
    for name in ("connect", "connect_ex", "sendto", "bind"):
        monkeypatch.setattr(socket.socket, name, reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)


@pytest.fixture
def store(tmp_path):
    return ExtensionStore(tmp_path / "node", X25519PrivateKey.generate())


def tool(name="read", hint=True, schema=None):
    return {"name": name, "inputSchema": copy.deepcopy(SCHEMA if schema is None else schema), "annotations": {"readOnlyHint": hint}}


def server(store, **values):
    return store.upsert_mcp_server({"id": "fixture", "transport": "streamable_http", "url": "https://mcp.example.test/tools", "enabled": True, **values})


def discover(store, tools=None):
    current = store.get_mcp_server_internal("fixture")
    return store.record_mcp_discovery("fixture", expected_revision=current["revision"], discovered_tools=[tool()] if tools is None else tools, status="ready")


def confirmation(row, names=None):
    selected = set(names) if names is not None else {candidate["name"] for candidate in row["server_hint_candidates"]}
    return {"expected_revision": row["revision"], "instance_id": row["instance_id"], "discovery_revision": row["discovery_revision"],
            "tools": [{"name": candidate["name"], "schema_sha256": candidate["schema_sha256"]}
                      for candidate in row["tools"] if candidate["name"] in selected]}


def test_default_and_legacy_policy_migration_do_not_change_config_identity(store):
    row = server(store, read_only_tools=["read"])
    assert row["data_policy"] == DEFAULT and row["read_only_sources"] == {"read": "owner"}
    state = store._read()
    for key in ("data_policy", "discovery_revision", "read_only_sources"):
        state["mcp_servers"]["fixture"].pop(key)
    state["mcp_servers"]["fixture"]["config_sha256"] = "legacy-fingerprint"
    store._write(state)
    reopened = ExtensionStore(store.root, store.key)
    migrated = reopened.get_mcp_server_internal("fixture")
    assert migrated["data_policy"] == DEFAULT and migrated["read_only_sources"] == {"read": "owner"}
    assert migrated["discovery_revision"] == 0 and migrated["config_generation"] == row["config_generation"]
    edited = reopened.upsert_mcp_server({"description": "display change"}, server_id="fixture", expected_revision=row["revision"])
    assert edited["config_generation"] == row["config_generation"]
    edited["data_policy"]["memory"] = "deny"
    assert reopened.get_mcp_server_internal("fixture")["data_policy"] == DEFAULT


@pytest.mark.parametrize("inline", [False, True])
def test_plugin_policy_inherits_and_server_override_is_persisted(store, tmp_path, inline):
    package = tmp_path / "package"
    package.mkdir()
    configs = {"inherited": {"url": "https://mcp.example.test/a"},
               "override": {"url": "https://mcp.example.test/b", "ryn": {"dataPolicy": DEFAULT}}}
    manifest = {"id": "bundle", "name": "Bundle", "ryn": {"dataPolicy": RESTRICTED}}
    if inline:
        manifest["mcpServers"] = configs
    else:
        (package / "mcp.json").write_text(json.dumps({"mcpServers": configs}), encoding="utf-8")
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    importer = ExtensionImportManager(store)
    preview = importer.preview({"kind": "plugin", "path": str(package)})
    assert preview["plugins"][0]["data_policy"] == RESTRICTED
    assert {row["id"]: row["data_policy"] for row in preview["mcp_servers"]} == {"bundle-inherited": RESTRICTED, "bundle-override": DEFAULT}
    importer.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    assert store.get_mcp_server_internal("bundle-inherited")["data_policy"] == RESTRICTED
    assert store.get_mcp_server_internal("bundle-override")["data_policy"] == DEFAULT
    plugin = next(row for row in store.overview()["plugins"] if row["id"] == "bundle")
    assert plugin["data_policy"] == RESTRICTED and not plugin["enabled"]


def test_standalone_mcp_policy_and_read_only_import_cannot_grant_trust(store):
    importer = ExtensionImportManager(store)
    preview = importer.preview({"kind": "mcp", "config_text": json.dumps({"mcpServers": {"fixture": {
        "url": "https://mcp.example.test/tools", "ryn": {"dataPolicy": {"memory": "deny"}},
        "read_only_tools": ["read"], "read_only_sources": {"read": "owner"}, "discovery_revision": 999}}})})
    assert preview["mcp_servers"][0]["data_policy"] == {"models": {"mode": "any"}, "memory": "deny"}
    importer.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    row = store.get_mcp_server_internal("fixture")
    assert row["read_only_tools"] == [] and row["read_only_sources"] == {} and row["discovery_revision"] == 0


@pytest.mark.parametrize("policy", [[], "any", {"memory": True}, {"models": {"mode": "unknown"}},
    {"models": {"mode": "allowlist", "allow": [{"on_device": "true"}]}},
    {"models": {"mode": "allowlist", "allow": [{"api_base_url_prefix": "http://models.example.test/"}]}},
    {"models": {"mode": "any", "allow": []}}, {"unexpected": "synthetic-canary"}])
@pytest.mark.parametrize("level", ["plugin", "server"])
def test_invalid_policy_import_fails_before_install(store, tmp_path, policy, level):
    package = tmp_path / "package"
    package.mkdir()
    manifest = {"id": "bundle", "name": "Bundle", "mcpServers": {"fixture": {"url": "https://mcp.example.test/tools"}}}
    target = manifest if level == "plugin" else manifest["mcpServers"]["fixture"]
    target["ryn"] = {"dataPolicy": policy}
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
        ExtensionImportManager(store).preview({"kind": "plugin", "path": str(package)})
    assert not any(row["id"] == "bundle-fixture" for row in store.list_mcp_servers_internal())


@pytest.mark.parametrize("payload", [{"data_policy": DEFAULT}, {"data_policy": RESTRICTED}, {"dataPolicy": DEFAULT}, {"ryn": {"dataPolicy": DEFAULT}}])
def test_create_and_patch_cannot_write_policy(store, payload):
    with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
        server(store, **payload)
    store.commit_imported_items(plugins=[], skills=[], mcp_servers=[{"id": "fixture", "transport": "streamable_http", "url": "https://mcp.example.test/tools", "data_policy": RESTRICTED}])
    before = store.get_mcp_server_internal("fixture")
    with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
        store.upsert_mcp_server(payload, server_id="fixture", expected_revision=before["revision"])
    assert store.get_mcp_server_internal("fixture")["data_policy"] == RESTRICTED


def test_internal_import_validates_policy_and_normalized_policy_changes_fingerprint(store):
    before = store.revision()
    with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
        store.commit_imported_items(plugins=[{"id": "bundle", "data_policy": {"memory": "bad"}}], skills=[], mcp_servers=[{"id": "fixture"}])
    assert store.revision() == before
    result = store.commit_imported_items(plugins=[{"id": "bundle", "data_policy": RESTRICTED, "mcp_server_ids": ["fixture"]}], skills=[], mcp_servers=[{"id": "fixture", "plugin_id": "bundle"}])
    row = store.get_mcp_server_internal("fixture")
    assert row["data_policy"] == RESTRICTED and result["overview"]["mcp_servers"]
    assert _mcp_config_fingerprint(row) != _mcp_config_fingerprint({**row, "data_policy": DEFAULT})
    equivalent = {"memory": "deny", "models": {"allow": [{"on_device": True}, {"on_device": True}], "mode": "allowlist"}}
    assert normalize_policy(equivalent) == RESTRICTED
    assert _mcp_config_fingerprint(row) == _mcp_config_fingerprint({**row, "data_policy": equivalent})


@pytest.mark.parametrize("hint,expected", [(True, True), (False, False), ("false", False), ("true", False), (1, False), (0, False), (None, False)])
def test_read_only_hint_requires_json_true_in_storage_and_public_candidates(store, hint, expected):
    server(store)
    row = discover(store, [tool(hint=hint)])
    assert row["tools"][0]["annotations"]["readOnlyHint"] is expected
    assert bool(row["server_hint_candidates"]) is expected
    assert store.get_mcp_server_internal("fixture")["discovered_tools"][0]["annotations"]["readOnlyHint"] is expected
    assert row["read_only_tools"] == []


def test_confirmation_applies_only_selected_tools_and_replay_is_rejected(store):
    server(store)
    row = discover(store, [tool("one"), tool("two"), tool("write", False)])
    payload = confirmation(row, ["one"])
    accepted = store.apply_server_hints("fixture", payload)
    assert accepted["read_only_tools"] == ["one"] and accepted["read_only_sources"] == {"one": "server_hint"}
    assert [item["name"] for item in accepted["server_hint_candidates"]] == ["two"]
    assert {item["name"]: item["effective_effect"] for item in accepted["tools"]} == {"one": "read", "two": "write", "write": "write"}
    with pytest.raises(AgentError, match="^agent_extensions_revision_conflict$"):
        store.apply_server_hints("fixture", payload)
    reopened = ExtensionStore(store.root, store.key)
    assert reopened.get_mcp_server_internal("fixture")["read_only_sources"] == {"one": "server_hint"}


@pytest.mark.parametrize("field,value", [("expected_revision", 999999), ("instance_id", "different-instance"), ("discovery_revision", 999999), ("schema_sha256", "a" * 64), ("name", "missing")])
def test_identity_and_schema_checks_are_atomic(store, field, value):
    server(store)
    row = discover(store, [tool("one"), tool("two")])
    payload = confirmation(row)
    if field in {"name", "schema_sha256"}:
        payload["tools"][1][field] = value
    else:
        payload[field] = value
    with pytest.raises(AgentError, match="^agent_extensions_revision_conflict$"):
        store.apply_server_hints("fixture", payload)
    assert store.get_mcp_server_internal("fixture")["read_only_tools"] == []


@pytest.mark.parametrize("mutation", ["extra", "missing", "bool_revision", "zero_discovery", "empty_tools", "duplicate", "extra_tool_field", "bad_hash", "bool_instance"])
def test_malformed_confirmation_rejected_without_partial_write(store, mutation):
    server(store)
    row = discover(store)
    payload = confirmation(row)
    if mutation == "extra":
        payload["read_only_sources"] = {"read": "owner"}
    elif mutation == "missing":
        payload.pop("instance_id")
    elif mutation == "bool_revision":
        payload["expected_revision"] = True
    elif mutation == "zero_discovery":
        payload["discovery_revision"] = 0
    elif mutation == "empty_tools":
        payload["tools"] = []
    elif mutation == "duplicate":
        payload["tools"] *= 2
    elif mutation == "extra_tool_field":
        payload["tools"][0]["owner"] = True
    elif mutation == "bad_hash":
        payload["tools"][0]["schema_sha256"] = "not-a-hash"
    else:
        payload["instance_id"] = True
    with pytest.raises(AgentError, match="^agent_mcp_invalid_server_hints$"):
        store.apply_server_hints("fixture", payload)
    assert store.get_mcp_server_internal("fixture")["read_only_tools"] == []


def test_identical_rediscovery_advances_confirmation_version_without_auth_change(store):
    server(store)
    row = discover(store)
    accepted = store.apply_server_hints("fixture", confirmation(row))
    current = discover(store)
    assert current["discovery_revision"] > accepted["discovery_revision"]
    assert current["read_only_sources"] == {"read": "server_hint"}
    assert current["config_generation"] == row["config_generation"]
    stale = {**confirmation(row), "expected_revision": current["revision"]}
    with pytest.raises(AgentError, match="^agent_extensions_revision_conflict$"):
        store.apply_server_hints("fixture", stale)


@pytest.mark.parametrize("change", ["false", "string", "schema", "missing", "duplicate"])
def test_rediscovery_revokes_only_server_hints_and_preserves_owner_settings(store, change):
    server(store, read_only_tools=["manual"])
    row = discover(store, [tool("read"), tool("manual")])
    accepted = store.apply_server_hints("fixture", confirmation(row, ["read"]))
    manual_save = store.upsert_mcp_server({"read_only_tools": ["read", "manual"]}, server_id="fixture", expected_revision=accepted["revision"])
    assert manual_save["read_only_sources"] == {"manual": "owner", "read": "server_hint"}
    replacements = {"false": [tool(hint=False)], "string": [tool(hint="false")],
                    "schema": [tool(schema={"type": "object", "required": ["new"]})], "missing": [], "duplicate": [tool(), tool()]}
    current = discover(store, replacements[change])
    assert current["read_only_tools"] == ["manual"] and current["read_only_sources"] == {"manual": "owner"}
    if change == "missing":
        readded = discover(store)
        assert readded["read_only_tools"] == ["manual"] and readded["server_hint_candidates"][0]["name"] == "read"


@pytest.mark.parametrize("patch", [{"url": "https://mcp.example.test/other"}, {"auth": {"mode": "none"}}, {"enabled": False}])
def test_config_or_lifecycle_changes_invalidate_server_hint_confirmation(store, patch):
    server(store, read_only_tools=["manual"])
    row = discover(store)
    accepted = store.apply_server_hints("fixture", confirmation(row))
    current = store.upsert_mcp_server(patch, server_id="fixture", expected_revision=accepted["revision"])
    assert current["read_only_tools"] == ["manual"] and current["discovery_revision"] == 0
    assert current["server_hint_candidates"] == []
    with pytest.raises(AgentError, match="^agent_extensions_revision_conflict$"):
        store.apply_server_hints("fixture", {**confirmation(row), "expected_revision": current["revision"]})


def test_delete_recreate_and_parent_disable_invalidate_confirmations(store):
    result = store.commit_imported_items(plugins=[{"id": "bundle", "mcp_server_ids": ["fixture"]}], skills=[],
        mcp_servers=[{"id": "fixture", "plugin_id": "bundle", "transport": "streamable_http", "url": "https://mcp.example.test/tools"}])
    parent = next(row for row in result["overview"]["plugins"] if row["id"] == "bundle")
    parent = store.set_plugin_enabled("bundle", True, expected_revision=parent["revision"])
    row = discover(store)
    store.apply_server_hints("fixture", confirmation(row))
    store.set_plugin_enabled("bundle", False, expected_revision=parent["revision"])
    current = store.get_mcp_server_internal("fixture")
    assert current["read_only_tools"] == [] and current["discovery_revision"] == 0
    store.delete_mcp_server("fixture", expected_revision=current["revision"])
    server(store)
    recreated = discover(store)
    with pytest.raises(AgentError, match="^agent_extensions_revision_conflict$"):
        store.apply_server_hints("fixture", {**confirmation(recreated), "instance_id": row["instance_id"]})


def test_status_only_update_does_not_renew_discovery_and_owner_cannot_inject_source(store):
    server(store)
    row = discover(store)
    status = store.record_mcp_discovery("fixture", expected_revision=row["revision"], status="error", status_message="offline")
    assert status["discovery_revision"] == row["discovery_revision"]
    for field, value in (("read_only_sources", {"read": "server_hint"}), ("discovery_revision", 123), ("server_hint_candidates", [])):
        with pytest.raises(AgentError, match="^agent_mcp_invalid_server_hints$"):
            store.upsert_mcp_server({field: value}, server_id="fixture", expected_revision=status["revision"])


def test_legacy_false_string_is_not_exposed_as_read_only_candidate(store):
    server(store)
    row = discover(store)
    state = store._read()
    state["mcp_servers"]["fixture"]["discovered_tools"][0]["annotations"]["readOnlyHint"] = "false"
    store._write(state)
    public = next(value for value in store.overview()["mcp_servers"] if value["id"] == "fixture")
    assert public["tools"][0]["annotations"]["readOnlyHint"] is False and not public["server_hint_candidates"]
    with pytest.raises(AgentError, match="^agent_extensions_revision_conflict$"):
        store.apply_server_hints("fixture", confirmation(row))
