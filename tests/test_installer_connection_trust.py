"""Read-only trust for the release's own remote connections (e.g. Dashboard).

The signed installer vouches for the connections it ships. While a connection
keeps that configuration, its read-only declarations need no owner prompt.
"""
import pytest

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.services.peer_box import load_or_create_messaging_key

URL = "https://dashboard.example.test/ryn/mcp"
AUTH = {"mode": "oauth", "client_id": "ryn-desktop", "scopes": ["finance.read"]}
DECLARED = [{"id": "d", "plugin_id": "p", "transport": "streamable_http", "url": URL, "auth": AUTH}]
TOOLS = [("lookup", True), ("report", True), ("send", False)]


def _store(tmp_path):
    return ExtensionStore(tmp_path / "agent", load_or_create_messaging_key(tmp_path / "key"))


def _shipped(tmp_path):
    store = _store(tmp_path)
    store.commit_imported_items(plugins=[{"id": "p", "mcp_server_ids": ["d"], "skill_ids": []}], skills=[],
                                mcp_servers=[dict(DECLARED[0])])
    store.set_plugin_enabled("p", True)
    return store


def _discover(store, tools=TOOLS, schema=None):
    current = store.get_mcp_server_internal("d")
    return store.record_mcp_discovery("d", expected_revision=current["revision"], status="ready", discovered_tools=[
        {"name": name, "inputSchema": schema or {"type": "object"}, "annotations": {"readOnlyHint": hint}}
        for name, hint in tools])


def _public(store):
    return next(s for s in store.overview()["mcp_servers"] if s["id"] == "d")


def _candidates(row):
    return [c["name"] for c in row["server_hint_candidates"]]


def test_installer_trusts_its_remote_connection_and_converts_earlier_confirmations(tmp_path):
    store = _shipped(tmp_path)
    row = _discover(store)
    assert _candidates(row) == ["lookup", "report"] and row["read_only_tools"] == []
    # The owner confirmed one tool from the card before this release.
    lookup = next(t for t in row["tools"] if t["name"] == "lookup")
    store.apply_server_hints("d", {"expected_revision": row["revision"], "instance_id": row["instance_id"],
        "discovery_revision": row["discovery_revision"],
        "tools": [{"name": "lookup", "schema_sha256": lookup["schema_sha256"]}]})

    assert store.trust_installer_connections(DECLARED) == ["d"]
    row = _public(store)
    assert row["read_only_tools"] == ["lookup", "report"]
    assert row["read_only_sources"] == {"lookup": "installer", "report": "installer"}
    assert row["server_hint_candidates"] == []
    assert {t["name"]: t["effective_effect"] for t in row["tools"]} == {
        "lookup": "read", "report": "read", "send": "write"}
    # Repeating the install changes nothing.
    revision = row["revision"]
    assert store.trust_installer_connections(DECLARED) == ["d"]
    assert _public(store)["revision"] == revision


def test_trust_survives_rediscovery_restart_schema_change_and_enable_toggles(tmp_path):
    store = _shipped(tmp_path)
    store.trust_installer_connections(DECLARED)
    row = _discover(store, schema={"type": "object", "required": ["month"]})
    assert row["read_only_sources"] == {"lookup": "installer", "report": "installer"}
    store = _store(tmp_path)  # Ryn restart
    assert _discover(store)["server_hint_candidates"] == []
    store.set_plugin_enabled("p", False)
    store.set_plugin_enabled("p", True)
    row = store.get_mcp_server_internal("d")
    row = store.upsert_mcp_server({"enabled": False}, server_id="d", expected_revision=row["revision"])
    row = store.upsert_mcp_server({"enabled": True}, server_id="d", expected_revision=row["revision"])
    row = _discover(store)
    assert row["read_only_tools"] == ["lookup", "report"] and row["server_hint_candidates"] == []
    # A tool that stops declaring read-only becomes a change again.
    row = _discover(store, [("lookup", False), ("report", True), ("send", False)])
    assert row["read_only_tools"] == ["report"] and row["server_hint_candidates"] == []


def test_owner_turning_a_trusted_tool_into_a_change_is_kept(tmp_path):
    store = _shipped(tmp_path)
    _discover(store)
    store.trust_installer_connections(DECLARED)
    row = _public(store)
    for selected in (["report"], ["lookup", "report"], ["report"]):
        row = store.upsert_mcp_server({"read_only_tools": selected}, server_id="d", expected_revision=row["revision"])
    row = _discover(store)
    assert row["read_only_tools"] == ["report"] and row["server_hint_candidates"] == []
    store.trust_installer_connections(DECLARED)  # next install
    row = _discover(store)
    assert row["read_only_tools"] == ["report"] and row["server_hint_candidates"] == []
    row = store.upsert_mcp_server({"read_only_tools": ["lookup", "report"]}, server_id="d",
                                  expected_revision=row["revision"])
    assert row["read_only_sources"] == {"lookup": "owner", "report": "installer"}


def test_owner_change_to_the_connection_ends_trust_until_restored(tmp_path):
    store = _shipped(tmp_path)
    _discover(store)
    store.trust_installer_connections(DECLARED)
    row = _public(store)
    row = store.upsert_mcp_server({"url": "https://owner.example.test/mcp"}, server_id="d",
                                  expected_revision=row["revision"])
    row = _discover(store)
    assert row["read_only_tools"] == [] and _candidates(row) == ["lookup", "report"]
    # The next install does not vouch for the owner's own endpoint.
    assert store.trust_installer_connections(DECLARED) == []
    assert _candidates(_public(store)) == ["lookup", "report"]


@pytest.mark.parametrize("change", [
    {"auth": {**AUTH, "scopes": ["finance.read", "owner.extra"]}},
    {"secret_headers": {"Authorization": "Bearer owner"}},
    {"bearer_token_env": "OWNER_TOKEN"},
])
def test_customized_connection_is_left_to_the_owner(tmp_path, change):
    store = _shipped(tmp_path)
    row = store.get_mcp_server_internal("d")
    store.upsert_mcp_server(change, server_id="d", expected_revision=row["revision"])
    _discover(store)
    assert store.trust_installer_connections(DECLARED) == []
    row = _public(store)
    assert row["read_only_tools"] == [] and _candidates(row) == ["lookup", "report"]


def test_only_declared_plugin_connections_are_trusted(tmp_path):
    store = _shipped(tmp_path)
    # An owner-added server at the same address is not the release's connection.
    store.upsert_mcp_server({"id": "owner", "transport": "streamable_http", "url": URL, "auth": AUTH, "enabled": True})
    declared = [{**DECLARED[0], "id": "owner"}, {**DECLARED[0], "plugin_id": "other"},
                {**DECLARED[0], "local_service": {"id": "company", "path": "/ryn/mcp"}},
                {**DECLARED[0], "id": "missing"}]
    assert store.trust_installer_connections(declared) == []
    assert store.get_mcp_server_internal("d").get("installer_config_sha256") is None


def test_installer_sign_in_migration_then_trust_needs_no_prompt(tmp_path):
    """The installer's own auth migration used to wipe every confirmation."""
    store = _shipped(tmp_path)
    row = _discover(store)
    lookup, report = (next(t for t in row["tools"] if t["name"] == n) for n in ("lookup", "report"))
    store.apply_server_hints("d", {"expected_revision": row["revision"], "instance_id": row["instance_id"],
        "discovery_revision": row["discovery_revision"], "tools": [
            {"name": t["name"], "schema_sha256": t["schema_sha256"]} for t in (lookup, report)]})
    moved = {**AUTH, "scopes": ["finance.read", "workspace.access"]}
    declarations = {"mcp_servers": [{**DECLARED[0], "auth": moved}],
                    "previous_defaults": [{"mcp_servers": {"d": {"auth": AUTH}}}]}
    result = store.migrate_release_defaults(declarations)
    assert result["updated"] == [{"category": "mcp_servers", "id": "d"}]
    assert store.get_mcp_server_internal("d")["read_only_tools"] == []
    assert store.trust_installer_connections(declarations["mcp_servers"]) == ["d"]
    row = _discover(store)
    assert row["read_only_sources"] == {"lookup": "installer", "report": "installer"}
    assert row["server_hint_candidates"] == []
