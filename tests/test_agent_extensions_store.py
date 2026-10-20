"""Tests for Ryn Agent Extensions encrypted store, revisions, skill bounds, and tool naming."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rynmesh.agent_runtime.extensions import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    ExtensionStore,
    compute_directory_manifest,
    compute_model_tool_name,
)
from rynmesh.agent_runtime.extensions.store import MAX_SKILL_FILE_BYTES
from rynmesh.agent_runtime.knowledge import SKILLS as BUILTIN_SKILLS
from rynmesh.agent_runtime.store import AgentError
from rynmesh.services import peer_box

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def test_encrypt_persist_reload_and_secret_canary_isolation(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)

    env_canary = "CANARY-STDIO-SECRET-99887766-AABBCC"
    header_canary = "CANARY-HTTP-BEARER-11223344-DDEEFF"

    stdio_srv = store.upsert_mcp_server(
        {
            "id": "local-stdio",
            "name": "Local Stdio Server",
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "some_mcp"],
            "env": {"API_SECRET": env_canary},
            "enabled": True,
        }
    )
    http_srv = store.upsert_mcp_server(
        {
            "id": "remote-http",
            "name": "Remote HTTP Server",
            "transport": "streamable_http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": f"Bearer {header_canary}"},
            "enabled": True,
        }
    )

    # Canaries must never appear in public return values
    for public_payload in (stdio_srv, http_srv, store.overview()):
        serialized_public = json.dumps(public_payload, ensure_ascii=False)
        assert env_canary not in serialized_public
        assert header_canary not in serialized_public

    assert stdio_srv["masked_env"]["API_SECRET"]["has_value"] is True
    assert http_srv["masked_headers"]["Authorization"]["has_value"] is True

    # Canaries must never appear in raw persisted bytes on disk
    raw_disk_bytes = (tmp_path / "agent" / "extensions.json").read_bytes()
    assert env_canary.encode("utf-8") not in raw_disk_bytes
    assert header_canary.encode("utf-8") not in raw_disk_bytes

    # Reload from disk with the same X25519 messaging key
    reloaded = ExtensionStore(tmp_path / "agent", key)
    reloaded_overview = reloaded.overview()
    serialized_reloaded = json.dumps(reloaded_overview, ensure_ascii=False)
    assert env_canary not in serialized_reloaded
    assert header_canary not in serialized_reloaded

    # Internal transport accessor decrypts exact secrets (preserving "Bearer " prefix)
    internal_stdio = reloaded.get_mcp_server_internal("local-stdio", require_enabled=True)
    internal_http = reloaded.get_mcp_server_internal("remote-http", require_enabled=True)
    assert internal_stdio["secret_env"]["API_SECRET"] == env_canary
    assert internal_http["secret_headers"]["Authorization"] == f"Bearer {header_canary}"

    # Updating non-secret fields preserves existing secrets without re-entry
    updated_stdio = reloaded.upsert_mcp_server(
        {"description": "Updated description", "env": {"API_SECRET": "__KEEP__"}},
        server_id="local-stdio",
        expected_revision=stdio_srv["revision"],
    )
    assert updated_stdio["description"] == "Updated description"
    assert (
        reloaded.get_mcp_server_internal("local-stdio")["secret_env"]["API_SECRET"]
        == env_canary
    )


def test_builtin_skills_catalog_protection_and_per_skill_revision(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)

    catalog = store.enabled_skills_catalog()
    catalog_by_id = {item["id"]: item for item in catalog}
    assert set(catalog_by_id) == set(BUILTIN_SKILLS)

    for skill_id, builtin in BUILTIN_SKILLS.items():
        entry = catalog_by_id[skill_id]
        assert entry["version"] == builtin["version"]
        assert entry["title"] == builtin["title"]
        assert entry["description"] == builtin["description"]
        assert "content" not in entry

    selected = store.select_skills(["project-development", "system-diagnostics"])
    assert len(selected) == 2
    assert selected[0] == BUILTIN_SKILLS["project-development"]
    assert selected[1] == BUILTIN_SKILLS["system-diagnostics"]

    # Mutate another configuration entry first, then toggle a built-in skill using its public revision
    store.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, True)
    overview_skills = {s["id"]: s for s in store.overview()["skills"]}
    proj_dev_row = overview_skills["project-development"]
    toggled = store.set_skill_enabled(
        "project-development",
        False,
        expected_revision=proj_dev_row["revision"],
    )
    assert toggled["enabled"] is False
    assert toggled["revision"] > proj_dev_row["revision"]

    # Stale built-in skill revision is rejected
    with pytest.raises(AgentError) as exc_info:
        store.set_skill_enabled(
            "project-development",
            True,
            expected_revision=proj_dev_row["revision"],
        )
    assert exc_info.value.args == ("agent_extensions_revision_conflict",)

    # Re-enable with current revision
    reenabled = store.set_skill_enabled(
        "project-development",
        True,
        expected_revision=toggled["revision"],
    )
    assert reenabled["enabled"] is True

    # Built-in skills and plugins cannot be deleted, and AgentError uses single-arg code
    with pytest.raises(AgentError) as exc_info:
        store.delete_skill("project-development")
    assert exc_info.value.args == ("agent_extension_builtin_protected",)

    with pytest.raises(AgentError) as exc_info:
        store.delete_plugin(COMPUTER_USE_PLUGIN_ID)
    assert exc_info.value.args == ("agent_extension_builtin_protected",)

    with pytest.raises(AgentError) as exc_info:
        store.delete_mcp_server(COMPUTER_USE_SERVER_ID)
    assert exc_info.value.args == ("agent_extension_builtin_protected",)


def test_compute_model_tool_name_collision_resistance() -> None:
    name_hyphen = compute_model_tool_name("a-b", "read")
    name_underscore = compute_model_tool_name("a_b", "read")
    assert name_hyphen != name_underscore
    assert TOOL_NAME_RE.fullmatch(name_hyphen)
    assert TOOL_NAME_RE.fullmatch(name_underscore)

    tool_hyphen = compute_model_tool_name("srv", "read-file")
    tool_underscore = compute_model_tool_name("srv", "read_file")
    assert tool_hyphen != tool_underscore
    assert TOOL_NAME_RE.fullmatch(tool_hyphen)
    assert TOOL_NAME_RE.fullmatch(tool_underscore)

    long_server = "very-long-organization-mcp-server-identifier-with-many-segments-12345"
    long_tool = "perform_exhaustive_database_schema_migration_and_verification_step"
    long_alias_1 = compute_model_tool_name(long_server, long_tool + "_a")
    long_alias_2 = compute_model_tool_name(long_server, long_tool + "_b")
    assert len(long_alias_1) <= 64
    assert len(long_alias_2) <= 64
    assert long_alias_1 != long_alias_2
    assert TOOL_NAME_RE.fullmatch(long_alias_1)
    assert TOOL_NAME_RE.fullmatch(long_alias_2)


def test_parent_plugin_disable_overrides_child_skill_and_server_switches(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)

    pkg_dir = store.allocate_package_dir("plugin", "acme-suite")
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "SKILL.md").write_text("# Acme Skill\nFollow Acme rules.\n", encoding="utf-8")

    store.commit_imported_items(
        plugins=[
            {
                "id": "acme-suite",
                "name": "Acme Suite",
                "title": "Acme Suite",
                "description": "Bundled plugin",
                "version": "1.0.0",
                "package_dir": str(pkg_dir),
                "skill_ids": ["acme-skill"],
                "mcp_server_ids": ["acme-mcp"],
            }
        ],
        skills=[
            {
                "id": "acme-skill",
                "name": "acme-skill",
                "title": "Acme Skill",
                "description": "Bundled skill",
                "content": "# Acme Skill\nFollow Acme rules.\n",
                "package_dir": str(pkg_dir),
                "plugin_id": "acme-suite",
            }
        ],
        mcp_servers=[
            {
                "id": "acme-mcp",
                "name": "Acme MCP",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "acme_mcp"],
                "plugin_id": "acme-suite",
            }
        ],
    )

    # Enable parent plugin -> both child skill and child MCP server become effectively enabled
    plugin_view = store.set_plugin_enabled("acme-suite", True)
    assert plugin_view["enabled"] is True
    assert any(s["id"] == "acme-skill" for s in store.enabled_skills_catalog())
    assert any(m["id"] == "acme-mcp" for m in store.list_enabled_mcp_servers_internal())

    # Disable parent plugin
    plugin_view = store.set_plugin_enabled(
        "acme-suite", False, expected_revision=plugin_view["revision"]
    )
    assert plugin_view["enabled"] is False

    # Now explicitly set child skill and child MCP server switches to True while parent is disabled
    skill_view = store.set_skill_enabled("acme-skill", True)
    assert skill_view["component_enabled"] is True
    assert skill_view["parent_enabled"] is False
    assert skill_view["enabled"] is False

    srv_view = store.upsert_mcp_server({"enabled": True}, server_id="acme-mcp")
    assert srv_view["component_enabled"] is True
    assert srv_view["parent_enabled"] is False
    assert srv_view["enabled"] is False

    # Catalog and invocation boundaries MUST still reject both children
    assert all(s["id"] != "acme-skill" for s in store.enabled_skills_catalog())
    assert store.get_skill_for_runtime("acme-skill") is None
    with pytest.raises(AgentError) as exc_info:
        store.select_skills(["acme-skill"])
    assert exc_info.value.args == ("agent_invalid_skills",)
    with pytest.raises(AgentError) as exc_info:
        store.read_skill_file("acme-skill")
    assert exc_info.value.args == ("agent_skill_disabled",)

    assert all(m["id"] != "acme-mcp" for m in store.list_enabled_mcp_servers_internal())
    with pytest.raises(AgentError) as exc_info:
        store.get_mcp_server_internal("acme-mcp", require_enabled=True)
    assert exc_info.value.args == ("agent_mcp_server_disabled",)


def test_transactional_revisions_aba_protection_and_stale_mcp_discovery(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)

    srv_a = store.upsert_mcp_server(
        {
            "id": "changeable-http",
            "name": "Changeable HTTP",
            "transport": "streamable_http",
            "url": "https://host-a.example.com/mcp",
            "enabled": True,
        }
    )
    rev_a = srv_a["revision"]
    gen_a = srv_a["config_generation"]
    inst_a = srv_a["instance_id"]

    # Mutate server URL from A to B
    srv_b = store.upsert_mcp_server(
        {"url": "https://host-b.example.com/mcp"},
        server_id="changeable-http",
        expected_revision=rev_a,
    )
    assert srv_b["revision"] > rev_a
    assert srv_b["config_generation"] > gen_a

    # Stale update using rev_a must fail
    with pytest.raises(AgentError) as exc_info:
        store.upsert_mcp_server(
            {"description": "stale"},
            server_id="changeable-http",
            expected_revision=rev_a,
        )
    assert exc_info.value.args == ("agent_extensions_revision_conflict",)

    # Stale discovery result from probe against URL A must fail and not overwrite B
    with pytest.raises(AgentError) as exc_info:
        store.record_mcp_discovery(
            "changeable-http",
            expected_revision=rev_a,
            expected_config_generation=gen_a,
            discovered_tools=[{"name": "tool_from_a", "inputSchema": {"type": "object"}}],
            status="ready",
        )
    assert exc_info.value.args == ("agent_extensions_revision_conflict",)
    assert store.get_mcp_server_internal("changeable-http")["discovered_tools"] == []

    # ABA race regression: delete changeable-http, recreate with same ID 'changeable-http'
    store.delete_mcp_server("changeable-http", expected_revision=srv_b["revision"])
    srv_recreated = store.upsert_mcp_server(
        {
            "id": "changeable-http",
            "name": "Changeable HTTP Recreated",
            "transport": "streamable_http",
            "url": "https://host-c.example.com/mcp",
            "enabled": True,
        }
    )
    assert srv_recreated["instance_id"] != inst_a
    assert srv_recreated["revision"] > srv_b["revision"]
    assert srv_recreated["config_generation"] > srv_b["config_generation"]

    # Old discovery from before deletion must NOT write tools into the recreated server
    with pytest.raises(AgentError) as exc_info:
        store.record_mcp_discovery(
            "changeable-http",
            expected_revision=rev_a,
            expected_config_generation=gen_a,
            expected_instance_id=inst_a,
            discovered_tools=[{"name": "aba_stale_tool", "inputSchema": {"type": "object"}}],
            status="ready",
        )
    assert exc_info.value.args == ("agent_extensions_revision_conflict",)
    assert store.get_mcp_server_internal("changeable-http")["discovered_tools"] == []

    # Current discovery for recreated server succeeds
    discovered_c = store.record_mcp_discovery(
        "changeable-http",
        expected_revision=srv_recreated["revision"],
        expected_config_generation=srv_recreated["config_generation"],
        expected_instance_id=srv_recreated["instance_id"],
        discovered_tools=[{"name": "tool_from_c", "inputSchema": {"type": "object"}}],
        status="ready",
    )
    assert discovered_c["tools_count"] == 1
    assert discovered_c["tools"][0]["name"] == "tool_from_c"
    assert discovered_c["tools"][0]["effective_effect"] == "write"

    # Disable server; a late discovery must not resurrect it
    disabled_srv = store.upsert_mcp_server(
        {"enabled": False},
        server_id="changeable-http",
        expected_revision=discovered_c["revision"],
    )
    with pytest.raises(AgentError) as exc_info:
        store.record_mcp_discovery(
            "changeable-http",
            expected_revision=disabled_srv["revision"],
            discovered_tools=[{"name": "late_tool", "inputSchema": {"type": "object"}}],
            status="ready",
        )
    assert exc_info.value.args == ("agent_mcp_server_disabled",)


def test_skill_semver_reimport_version_binding_and_bounded_file_reads(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)

    # Import v1.0.0 skill package with a referenced guide (semver string!)
    pkg_v1 = store.allocate_package_dir("skill", "ops-runbook")
    (pkg_v1 / "refs").mkdir(parents=True, exist_ok=True)
    (pkg_v1 / "SKILL.md").write_text("# Ops Runbook v1.0.0\nSee refs/guide.md\n", encoding="utf-8")
    (pkg_v1 / "refs" / "guide.md").write_text("v1.0.0 reference content", encoding="utf-8")

    store.commit_imported_items(
        plugins=[],
        skills=[
            {
                "id": "ops-runbook",
                "name": "ops-runbook",
                "title": "Ops Runbook",
                "description": "Runbook v1.0.0",
                "version": "1.0.0",
                "content": "# Ops Runbook v1.0.0\nSee refs/guide.md\n",
                "package_dir": str(pkg_v1),
            }
        ],
        mcp_servers=[],
    )
    store.set_skill_enabled("ops-runbook", True)

    # Task 1 snapshots v1.0.0
    task1_snapshots = store.select_skills(["ops-runbook"])
    assert len(task1_snapshots) == 1
    v1_snapshot = task1_snapshots[0]
    assert v1_snapshot["version"] == "1.0.0"
    assert v1_snapshot["install_generation"] == 1
    assert v1_snapshot.get("manifest_sha256")

    # Normal reference read bound to v1.0.0 snapshot works
    ref_read = store.read_skill_file("ops-runbook", "refs/guide.md", snapshot=v1_snapshot)
    assert ref_read["content"] == "v1.0.0 reference content"
    assert ref_read["version"] == "1.0.0"

    # Path traversal is rejected
    with pytest.raises(AgentError) as exc_info:
        store.read_skill_file("ops-runbook", "../outside.md", snapshot=v1_snapshot)
    assert exc_info.value.args == ("agent_skill_invalid_path",)

    # Re-import v1.1.0 of the same skill in a new package directory (must not raise ValueError on int("1.0.0")!)
    pkg_v2 = store.allocate_package_dir("skill", "ops-runbook")
    (pkg_v2 / "refs").mkdir(parents=True, exist_ok=True)
    (pkg_v2 / "SKILL.md").write_text("# Ops Runbook v1.1.0\nSee refs/guide.md\n", encoding="utf-8")
    (pkg_v2 / "refs" / "guide.md").write_text("v1.1.0 different reference content", encoding="utf-8")

    store.commit_imported_items(
        plugins=[],
        skills=[
            {
                "id": "ops-runbook",
                "name": "ops-runbook",
                "title": "Ops Runbook",
                "description": "Runbook v1.1.0",
                "version": "1.1.0",
                "content": "# Ops Runbook v1.1.0\nSee refs/guide.md\n",
                "package_dir": str(pkg_v2),
            }
        ],
        mcp_servers=[],
    )
    store.set_skill_enabled("ops-runbook", True)

    # Task 1 (holding v1.0.0 snapshot) must NOT silently receive v1.1.0 references
    with pytest.raises(AgentError) as exc_info:
        store.read_skill_file("ops-runbook", "refs/guide.md", snapshot=v1_snapshot)
    assert exc_info.value.args == ("agent_skill_version_mismatch",)

    # Task 2 (selecting v1.1.0 snapshot) can read v1.1.0 references
    v2_snapshot = store.select_skills(["ops-runbook"])[0]
    assert v2_snapshot["version"] == "1.1.0"
    assert v2_snapshot["install_generation"] == 2
    ref_v2 = store.read_skill_file("ops-runbook", "refs/guide.md", snapshot=v2_snapshot)
    assert ref_v2["content"] == "v1.1.0 different reference content"

    # Tampering with a small file on disk is caught by manifest verification
    (pkg_v2 / "refs" / "guide.md").write_text("tampered content", encoding="utf-8")
    with pytest.raises(AgentError) as exc_info:
        store.read_skill_file("ops-runbook", "refs/guide.md", snapshot=v2_snapshot)
    assert exc_info.value.args == ("agent_skill_integrity_failed",)

    # Modifying a file to exceed MAX_SKILL_FILE_BYTES is caught by bounded stat/stream check
    (pkg_v2 / "refs" / "guide.md").write_bytes(b"A" * (MAX_SKILL_FILE_BYTES + 1024))
    with pytest.raises(AgentError) as exc_info:
        store.read_skill_file("ops-runbook", "refs/guide.md", snapshot=v2_snapshot)
    assert exc_info.value.args == ("agent_skill_file_too_large",)
    with pytest.raises(AgentError) as exc_info:
        compute_directory_manifest(pkg_v2)
    assert exc_info.value.args == ("agent_skill_file_too_large",)
