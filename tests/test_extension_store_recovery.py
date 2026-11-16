"""S13-I33: extension configuration recovery, disposable catalogs, and size errors."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions import store as extension_store
from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager
from rynmesh.agent_runtime.store import AgentError, encoded
from rynmesh.services import peer_box


def make_store(tmp_path):
    key = X25519PrivateKey.generate()
    return ExtensionStore(tmp_path / "agent", key), key


def add_server(store, **overrides):
    return store.upsert_mcp_server({
        "id": "sample", "name": "Saved server", "transport": "stdio",
        "command": "python", "enabled": True,
        "env": {"SYNTHETIC_SECRET": "test-private-config"}, **overrides,
    }, expected_revision=store.revision())


def discover(store, tools):
    server = store.get_mcp_server_internal("sample")
    return store.record_mcp_discovery(
        "sample", expected_revision=server["revision"], discovered_tools=tools, status="ready",
    )


def main_plaintext(store):
    envelope = json.loads(store.path.read_bytes())
    return json.loads(peer_box.open_sealed(
        store.key, store.pub, envelope["nonce"], envelope["ciphertext"],
        info=extension_store.EXTENSIONS_CHANNEL,
    ))


@pytest.mark.parametrize("warm", [False, True])
def test_s13_i33_t1_truncated_primary_recovers_previous_configuration(tmp_path, warm):
    store, key = make_store(tmp_path)
    server = add_server(store, description="previous description")
    previous = store.path.read_bytes()
    store.upsert_mcp_server({"description": "latest description"}, server_id="sample",
                            expected_revision=server["revision"])
    prev_path = store.path.with_name("extensions.json.prev")
    assert prev_path.read_bytes() == previous
    reader = store if warm else ExtensionStore(store.root, key)
    reader.overview()
    store.path.write_bytes(b'{"version":')
    view = reader.overview()
    assert next(s for s in view["mcp_servers"] if s["id"] == "sample")["description"] == "previous description"
    assert view["recovered_from_prev"] is True
    assert view["recovery"]["status"] == "recovered_from_prev"
    recovery_id = view["recovery"]["id"]
    assert isinstance(recovery_id, str) and recovery_id
    restarted = ExtensionStore(store.root, key)
    assert restarted.overview()["recovered_from_prev"] is True
    assert restarted.overview()["recovery"]["id"] == recovery_id
    restored = restarted.get_mcp_server_internal("sample")
    restarted.upsert_mcp_server({"description": "after recovery"}, server_id="sample",
                               expected_revision=restored["revision"])
    restarted.path.write_bytes(b"truncated again")
    second_recovery = ExtensionStore(store.root, key)
    assert second_recovery.get_mcp_server_internal("sample")["description"] == "previous description"
    assert second_recovery.overview()["recovery"]["id"] != recovery_id


@pytest.mark.parametrize("lost_key", [False, True])
def test_s13_i33_t2_both_unreadable_files_are_quarantined_and_defaults_start(tmp_path, lost_key):
    store, key = make_store(tmp_path)
    add_server(store)
    prev = store.path.with_name("extensions.json.prev")
    if not lost_key:
        store.path.write_bytes(b"damaged primary")
        prev.write_bytes(b"damaged previous")
    original = {path.name: path.read_bytes() for path in (store.path, prev)}
    reader = ExtensionStore(store.root, X25519PrivateKey.generate() if lost_key else key)
    view = reader.overview()
    assert {server["id"] for server in view["mcp_servers"]} == {"computer-use"}
    assert view["recovered_from_prev"] is False
    assert view["recovery"]["status"] == "reset"
    quarantine = Path(view["recovery"]["quarantine_path"])
    assert quarantine.resolve().is_relative_to((store.root / "quarantine").resolve())
    assert {p.name: p.read_bytes() for p in quarantine.iterdir()} == original
    assert not prev.exists()
    assert ExtensionStore(store.root, reader.key).overview()["recovery"] == view["recovery"]
    assert add_server(reader)["name"] == "Saved server"


@pytest.mark.parametrize("warm", [False, True])
def test_s13_i33_t3_bad_tool_cache_preserves_secrets_and_rebuilds_on_connection(tmp_path, monkeypatch, warm):
    store, key = make_store(tmp_path)
    add_server(store)
    discover(store, [{"name": "cached_tool", "inputSchema": {"type": "object"}}])
    cache = store.root / "extensions-tools.json"
    assert cache.is_file()
    assert "discovered_tools" not in main_plaintext(store)["mcp_servers"]["sample"]
    cache.write_bytes(b"broken cache")
    reader = store if warm else ExtensionStore(store.root, key)
    server = reader.get_mcp_server_internal("sample")
    assert server["secret_env"] == {"SYNTHETIC_SECRET": "test-private-config"}
    assert server["discovered_tools"] == []
    assert server["status"] == "NeedsTest"
    assert not cache.exists()
    session = SimpleNamespace(
        list_all_tools=lambda: [{"name": "rebuilt_tool", "inputSchema": {"type": "object"}}],
        close=lambda: None, config_generation=server["config_generation"], instance_id=server["instance_id"],
    )
    manager = McpSessionManager(reader)
    monkeypatch.setattr(manager, "_create_session", lambda _server: session)
    result = manager.test_server("sample", expected_revision=server["revision"])
    assert result["test_result"]["status"] == "connected"
    assert result["test_result"]["tools_count"] == 1
    assert cache.is_file()
    assert ExtensionStore(store.root, key).get_mcp_server_internal("sample")["discovered_tools"][0]["name"] == "rebuilt_tool"


def test_s13_i33_t3_legacy_inline_catalog_migrates_without_losing_configuration(tmp_path):
    store, key = make_store(tmp_path)
    add_server(store)
    discover(store, [{"name": "legacy_tool", "inputSchema": {"type": "object"}}])
    legacy = store._read()
    nonce, ciphertext = peer_box.seal(key, store.pub, encoded(legacy), info=extension_store.EXTENSIONS_CHANNEL)
    store.path.write_text(json.dumps({"version": extension_store.EXTENSIONS_VERSION,
                                    "nonce": nonce, "ciphertext": ciphertext}), encoding="utf-8")
    cache = store.root / "extensions-tools.json"
    cache.unlink(missing_ok=True)
    reader = ExtensionStore(store.root, key)
    server = reader.get_mcp_server_internal("sample")
    assert server["discovered_tools"][0]["name"] == "legacy_tool"
    reader.upsert_mcp_server({"description": "migrated"}, server_id="sample",
                            expected_revision=server["revision"])
    assert "discovered_tools" not in main_plaintext(reader)["mcp_servers"]["sample"]
    assert cache.is_file()
    reloaded = ExtensionStore(store.root, key).get_mcp_server_internal("sample")
    assert reloaded["description"] == "migrated"
    assert reloaded["discovered_tools"][0]["name"] == "legacy_tool"


def test_s13_i33_t3_configuration_only_change_does_not_rewrite_catalog(tmp_path):
    store, _ = make_store(tmp_path)
    add_server(store)
    discover(store, [{"name": "stable_tool", "inputSchema": {"type": "object"}}])
    cache = store.root / "extensions-tools.json"
    previous = cache.read_bytes()
    server = store.get_mcp_server_internal("sample")
    store.upsert_mcp_server({"description": "configuration only"}, server_id="sample",
                            expected_revision=server["revision"])
    assert cache.read_bytes() == previous


def test_s13_i33_t4_large_tool_schemas_do_not_consume_owner_configuration_limit(tmp_path):
    store, key = make_store(tmp_path)
    add_server(store)
    text = "x" * (extension_store.MAX_PLAINTEXT + 1024)
    discover(store, [{"name": "large_tool", "inputSchema": {"type": "object", "description": text}}])
    config = main_plaintext(store)
    assert len(encoded(config)) < 64 * 1024
    assert all("discovered_tools" not in server for server in config["mcp_servers"].values())
    reloaded = ExtensionStore(store.root, key).get_mcp_server_internal("sample")
    assert reloaded["discovered_tools"][0]["input_schema"]["description"] == text
    assert reloaded["secret_env"]["SYNTHETIC_SECRET"] == "test-private-config"


def test_s13_i33_t4_owner_configuration_write_limit_has_distinct_code(tmp_path):
    store, _ = make_store(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(AgentError, match="^agent_extension_state_limit$"):
        add_server(store, env={"BIG": "x" * (extension_store.MAX_PLAINTEXT + 1)})
    assert store.path.read_bytes() == before


def test_s13_i33_t4_owner_configuration_read_limit_has_distinct_code(tmp_path):
    store, key = make_store(tmp_path)
    state = store._read()
    state["owner_configuration"] = "x" * (extension_store.MAX_PLAINTEXT + 1)
    nonce, ciphertext = peer_box.seal(key, store.pub, encoded(state), info=extension_store.EXTENSIONS_CHANNEL)
    store.path.write_text(json.dumps({"version": extension_store.EXTENSIONS_VERSION,
                                    "nonce": nonce, "ciphertext": ciphertext}), encoding="utf-8")
    with pytest.raises(AgentError, match="^agent_extension_state_limit$"):
        ExtensionStore(store.root, key).overview()
    assert store.path.exists()
    assert not (store.root / "quarantine").exists()
