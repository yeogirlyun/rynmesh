"""Regression tests for plugin manifest compatibility, skill path arrays, additive Claude skills, portable root precedence, and legacy SSE rejection."""

from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionImportManager, ExtensionStore
from rynmesh.agent_runtime.store import AgentError


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _add_skill(package: Path, relative_dir: str, name: str, body: str = "Skill instructions.\n") -> None:
    skill_dir = package / relative_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Synthetic skill {name}\n---\n{body}",
        encoding="utf-8",
    )


def _zip_directory_b64(src_dir: Path) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(src_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(src_dir).as_posix()
                zf.writestr(f"{src_dir.name}/{rel}", file_path.read_bytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_codex_and_claude_skills_path_arrays_and_deduplication(tmp_path: Path) -> None:
    for vendor in ("codex", "claude"):
        pkg = tmp_path / f"{vendor}-array-pkg"
        _write_json(
            pkg / f".{vendor}-plugin" / "plugin.json",
            {
                "name": f"{vendor}-array-probe",
                "version": "1.0.0",
                "description": "Synthetic skill array fixture",
                "author": {"name": "Synthetic"},
                "interface": {"displayName": "Synthetic array fixture", "capabilities": []},
                "skills": ["./first-skills/", "./second-skills/", "./first-skills/first"],
            },
        )
        _add_skill(pkg, "first-skills/first", f"{vendor}-array-first")
        _add_skill(pkg, "second-skills/second", f"{vendor}-array-second")

        store = ExtensionStore(tmp_path / f"{vendor}-store", X25519PrivateKey.generate())
        manager = ExtensionImportManager(store)

        preview = manager.preview({"kind": "plugin", "path": str(pkg)})
        expected_ids = [f"{vendor}-array-first", f"{vendor}-array-second"]
        assert preview["can_import"] is True
        assert preview["warnings"] == []
        assert preview["unsupported_components"] == []
        assert [s["id"] for s in preview["skills"]] == expected_ids
        assert preview["plugins"][0]["skill_ids"] == expected_ids

        overview = manager.commit(
            preview["preview_id"], expected_revision=preview["expected_revision"]
        )
        plugin_row = next(p for p in overview["plugins"] if p["id"] == f"{vendor}-array-probe")
        assert plugin_row["enabled"] is False
        assert plugin_row["skill_ids"] == expected_ids
        stored_skill_ids = [
            s["id"] for s in overview["skills"] if s.get("plugin_id") == f"{vendor}-array-probe"
        ]
        assert stored_skill_ids == expected_ids


def test_claude_skills_path_is_additive_to_default_skills_directory(tmp_path: Path) -> None:
    pkg = tmp_path / "claude-additive-pkg"
    _write_json(
        pkg / ".claude-plugin" / "plugin.json",
        {
            "name": "claude-additive-probe",
            "version": "1.0.0",
            "skills": "./extra-skills/",
        },
    )
    _add_skill(pkg, "skills/default", "claude-default-skill")
    _add_skill(pkg, "extra-skills/extra", "claude-extra-skill")

    store = ExtensionStore(tmp_path / "claude-additive-store", X25519PrivateKey.generate())
    manager = ExtensionImportManager(store)

    preview = manager.preview({"kind": "plugin", "path": str(pkg)})
    expected_ids = ["claude-default-skill", "claude-extra-skill"]
    assert preview["can_import"] is True
    assert preview["warnings"] == []
    assert [s["id"] for s in preview["skills"]] == expected_ids
    assert preview["plugins"][0]["skill_ids"] == expected_ids

    overview = manager.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    plugin_row = next(p for p in overview["plugins"] if p["id"] == "claude-additive-probe")
    assert plugin_row["skill_ids"] == expected_ids

    # Also verify ZIP import with additive array + overlap on default skills/
    pkg_arr = tmp_path / "claude-additive-array-pkg"
    _write_json(
        pkg_arr / ".claude-plugin" / "plugin.json",
        {
            "name": "claude-additive-array",
            "version": "1.1.0",
            "skills": ["./skills/", "./custom-a/", "./custom-b/leaf"],
        },
    )
    _add_skill(pkg_arr, "skills/base", "claude-base-skill")
    _add_skill(pkg_arr, "custom-a/one", "claude-custom-one")
    _add_skill(pkg_arr, "custom-b/leaf", "claude-custom-two")

    zip_preview = manager.preview(
        {"kind": "plugin", "zip_base64": _zip_directory_b64(pkg_arr), "filename": "claude.zip"}
    )
    assert zip_preview["can_import"] is True
    assert zip_preview["warnings"] == []
    assert [s["id"] for s in zip_preview["skills"]] == [
        "claude-base-skill",
        "claude-custom-one",
        "claude-custom-two",
    ]


def test_portable_root_plugin_json_canonical_over_codex_overlay_and_inline_openai_metadata(
    tmp_path: Path,
) -> None:
    portable_manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "portable-canonical",
        "version": "1.2.3",
        "description": "Root identity",
        "extensions": {
            "com.openai": {
                "interface": {
                    "displayName": "Portable canonical title",
                    "shortDescription": "Portable canonical subtitle",
                }
            }
        },
    }

    # Case 1: Portable root plugin.json + legacy .codex-plugin/plugin.json overlay
    dual_pkg = tmp_path / "portable-with-overlay"
    _write_json(dual_pkg / "plugin.json", portable_manifest)
    _write_json(
        dual_pkg / ".codex-plugin" / "plugin.json",
        {
            "name": "legacy-overlay-name",
            "version": "9.9.9",
            "description": "Legacy overlay",
            "author": {"name": "Synthetic"},
            "interface": {"displayName": "Legacy overlay title", "capabilities": []},
            "skills": "./legacy-skills/",
        },
    )
    _add_skill(dual_pkg, "skills/canonical", "portable-canonical-skill")
    _add_skill(dual_pkg, "legacy-skills/legacy", "legacy-overlay-skill")
    _write_json(
        dual_pkg / "mcp.json",
        {
            "mcpServers": {
                "canonical-srv": {
                    "command": sys.executable,
                    "args": ["${CODEX_PLUGIN_ROOT}/scripts/srv.py"],
                }
            }
        },
    )
    (dual_pkg / "scripts").mkdir(parents=True, exist_ok=True)
    (dual_pkg / "scripts" / "srv.py").write_text("# inert\n", encoding="utf-8")

    store1 = ExtensionStore(tmp_path / "store-dual", X25519PrivateKey.generate())
    manager1 = ExtensionImportManager(store1)
    preview1 = manager1.preview({"kind": "plugin", "path": str(dual_pkg)})

    assert preview1["can_import"] is True
    assert preview1["warnings"] == []
    assert preview1["name"] == "Portable canonical title"
    assert preview1["version"] == "1.2.3"
    assert [p["id"] for p in preview1["plugins"]] == ["portable-canonical"]
    assert preview1["plugins"][0]["title"] == "Portable canonical title"
    assert preview1["plugins"][0]["description"] == "Portable canonical subtitle"
    assert [s["id"] for s in preview1["skills"]] == ["portable-canonical-skill"]
    assert [m["id"] for m in preview1["mcp_servers"]] == ["portable-canonical-canonical-srv"]

    overview1 = manager1.commit(
        preview1["preview_id"], expected_revision=preview1["expected_revision"]
    )
    stored_plugin1 = next(p for p in overview1["plugins"] if p["id"] == "portable-canonical")
    assert stored_plugin1["title"] == "Portable canonical title"
    assert stored_plugin1["version"] == "1.2.3"
    assert stored_plugin1["skill_ids"] == ["portable-canonical-skill"]
    assert stored_plugin1["mcp_server_ids"] == ["portable-canonical-canonical-srv"]
    assert all(p["id"] != "legacy-overlay-name" for p in overview1["plugins"])
    assert all(s["id"] != "legacy-overlay-skill" for s in overview1["skills"])

    # Case 2: Standalone portable root plugin.json with inline extensions.com.openai.interface
    inline_pkg = tmp_path / "portable-inline-only"
    _write_json(
        inline_pkg / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-canonical",
            "version": "1.2.3",
            "description": "Root identity",
            "extensions": {
                "com.openai": {
                    "interface": {
                        "displayName": "Portable canonical title",
                    }
                }
            },
        },
    )
    _add_skill(inline_pkg, "skills/canonical", "portable-canonical-skill")

    store2 = ExtensionStore(tmp_path / "store-inline", X25519PrivateKey.generate())
    manager2 = ExtensionImportManager(store2)
    preview2 = manager2.preview({"kind": "plugin", "path": str(inline_pkg)})
    assert preview2["can_import"] is True
    assert preview2["name"] == "Portable canonical title"
    assert preview2["plugins"][0]["id"] == "portable-canonical"
    assert preview2["plugins"][0]["title"] == "Portable canonical title"
    assert preview2["plugins"][0]["description"] == "Root identity"
    assert [s["id"] for s in preview2["skills"]] == ["portable-canonical-skill"]

    overview2 = manager2.commit(
        preview2["preview_id"], expected_revision=preview2["expected_revision"]
    )
    stored_plugin2 = next(p for p in overview2["plugins"] if p["id"] == "portable-canonical")
    assert stored_plugin2["title"] == "Portable canonical title"

    # Case 3: Portable root without extensions.com.openai uses overlay only for display metadata
    fallback_pkg = tmp_path / "portable-overlay-display-fallback"
    _write_json(
        fallback_pkg / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-root-id",
            "version": "2.0.0",
            "description": "Canonical root description",
        },
    )
    _write_json(
        fallback_pkg / ".codex-plugin" / "plugin.json",
        {
            "name": "ignored-overlay-id",
            "version": "9.9.9",
            "interface": {"displayName": "Overlay Display Title"},
            "skills": "./ignored-overlay-skills/",
        },
    )
    _add_skill(fallback_pkg, "skills/root-skill", "canonical-root-skill")
    _add_skill(fallback_pkg, "ignored-overlay-skills/s", "ignored-overlay-skill")

    store3 = ExtensionStore(tmp_path / "store-fallback", X25519PrivateKey.generate())
    manager3 = ExtensionImportManager(store3)
    preview3 = manager3.preview({"kind": "plugin", "path": str(fallback_pkg)})
    assert preview3["can_import"] is True
    assert preview3["plugins"][0]["id"] == "portable-root-id"
    assert preview3["plugins"][0]["version"] == "2.0.0"
    assert preview3["plugins"][0]["title"] == "Overlay Display Title"
    assert preview3["plugins"][0]["description"] == "Canonical root description"
    assert [s["id"] for s in preview3["skills"]] == ["canonical-root-skill"]


def test_missing_and_malformed_manifest_skill_paths_rejected(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    manager = ExtensionImportManager(store)

    # 1. Missing explicit skills path produces visible warning and can_import=False
    missing_pkg = tmp_path / "missing-skills-pkg"
    _write_json(
        missing_pkg / ".codex-plugin" / "plugin.json",
        {
            "name": "missing-skills-plugin",
            "version": "1.0.0",
            "skills": ["./present-skills/", "./nonexistent-skills/"],
        },
    )
    _add_skill(missing_pkg, "present-skills/one", "present-skill")

    preview_missing = manager.preview({"kind": "plugin", "path": str(missing_pkg)})
    assert preview_missing["can_import"] is False
    assert any("nonexistent-skills" in w for w in preview_missing["warnings"])
    with pytest.raises(AgentError) as exc_info:
        manager.commit(
            preview_missing["preview_id"],
            expected_revision=preview_missing["expected_revision"],
        )
    assert exc_info.value.args == ("agent_import_not_allowed",)

    # 2. Traversal in skills array is rejected immediately
    traversal_pkg = tmp_path / "traversal-skills-pkg"
    _write_json(
        traversal_pkg / ".claude-plugin" / "plugin.json",
        {
            "name": "traversal-plugin",
            "version": "1.0.0",
            "skills": ["./skills/", "../outside-skills/"],
        },
    )
    _add_skill(traversal_pkg, "skills/ok", "ok-skill")
    with pytest.raises(AgentError) as exc_info:
        manager.preview({"kind": "plugin", "path": str(traversal_pkg)})
    assert exc_info.value.args == ("agent_extension_archive_traversal",)

    # 3. Malformed skills field types are rejected with agent_invalid_plugin_package
    for idx, bad_skills in enumerate([123, {"path": "./skills"}, [], ["valid", 99], ""]):
        bad_pkg = tmp_path / f"bad-skills-{idx}"
        _write_json(
            bad_pkg / ".codex-plugin" / "plugin.json",
            {"name": f"bad-{idx}", "version": "1.0.0", "skills": bad_skills},
        )
        with pytest.raises(AgentError) as exc_info:
            manager.preview({"kind": "plugin", "path": str(bad_pkg)})
        assert exc_info.value.args == ("agent_invalid_plugin_package",)


def test_legacy_mcp_sse_transport_marked_unsupported_and_never_relabeled(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    manager = ExtensionImportManager(store)

    # 1. Standalone JSON MCP config with type="sse"
    sse_json = json.dumps(
        {
            "mcpServers": {
                "legacy-events": {
                    "type": "sse",
                    "url": "https://example.com/sse",
                }
            }
        }
    )
    preview_json = manager.preview({"kind": "mcp", "config_text": sse_json})
    assert preview_json["can_import"] is False
    assert "sse" in preview_json["unsupported_components"]
    assert any("sse" in w.lower() for w in preview_json["warnings"])
    assert preview_json["mcp_servers"] == []
    with pytest.raises(AgentError) as exc_info:
        manager.commit(
            preview_json["preview_id"], expected_revision=preview_json["expected_revision"]
        )
    assert exc_info.value.args == ("agent_import_not_allowed",)

    # 2. Standalone TOML MCP config with transport="sse"
    sse_toml = '[mcp_servers.legacy_toml]\ntransport = "sse"\nurl = "https://example.com/sse"\n'
    preview_toml = manager.preview(
        {"kind": "mcp", "filename": "config.toml", "config_text": sse_toml}
    )
    assert preview_toml["can_import"] is False
    assert "sse" in preview_toml["unsupported_components"]
    assert preview_toml["mcp_servers"] == []

    # 3. Plugin package declaring a legacy SSE MCP server is marked unsupported and rejected on commit
    sse_plugin = tmp_path / "sse-plugin"
    _write_json(
        sse_plugin / ".claude-plugin" / "plugin.json",
        {
            "name": "sse-plugin",
            "version": "1.0.0",
            "mcpServers": {
                "legacy-sse-srv": {
                    "transport": "sse",
                    "url": "https://example.com/mcp-sse",
                }
            },
        },
    )
    _add_skill(sse_plugin, "skills/one", "sse-plugin-skill")

    preview_plugin = manager.preview({"kind": "plugin", "path": str(sse_plugin)})
    assert preview_plugin["can_import"] is False
    assert "sse" in preview_plugin["unsupported_components"]
    assert any("sse" in w.lower() for w in preview_plugin["warnings"])
    assert preview_plugin["mcp_servers"] == []

    # 4. StreamableHTTP and stdio remain supported
    valid_mcp = json.dumps(
        {
            "mcpServers": {
                "modern-http": {
                    "type": "streamable-http",
                    "url": "https://example.com/mcp",
                },
                "local-stdio": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-V"],
                },
            }
        }
    )
    preview_valid = manager.preview({"kind": "mcp", "config_text": valid_mcp})
    assert preview_valid["can_import"] is True
    assert preview_valid["unsupported_components"] == []
    by_id = {m["id"]: m for m in preview_valid["mcp_servers"]}
    assert by_id["modern-http"]["transport"] == "streamable_http"
    assert by_id["local-stdio"]["transport"] == "stdio"


def test_portable_canonical_mcp_json_ignores_legacy_dot_mcp_json_overlay_and_preserves_legacy_layouts(
    tmp_path: Path,
) -> None:
    # 1. Exact follow-up probe layout: portable root plugin.json + mcp.json + .codex-plugin/plugin.json + .mcp.json
    pkg_followup = tmp_path / "portable-canonical-mcp-with-overlay"
    _write_json(
        pkg_followup / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-canonical-mcp",
            "version": "1.0.0",
            "description": "Portable root and legacy compatibility overlay",
            "extensions": {"com.openai": {"interface": {"displayName": "Portable MCP"}}},
        },
    )
    _write_json(
        pkg_followup / "mcp.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "docs": {
                    "type": "streamable-http",
                    "url": "https://example.com/mcp",
                }
            },
        },
    )
    _write_json(
        pkg_followup / ".codex-plugin" / "plugin.json",
        {
            "name": "portable-canonical-mcp",
            "version": "1.0.0",
            "description": "Legacy compatibility only",
            "author": {"name": "Synthetic"},
            "mcpServers": "./.mcp.json",
            "interface": {"displayName": "Legacy fallback", "capabilities": []},
        },
    )
    _write_json(
        pkg_followup / ".mcp.json",
        {
            "mcpServers": {
                "docs": {
                    "type": "http",
                    "url": "https://legacy.example.com/mcp",
                },
                "legacy-extra": {
                    "type": "stdio",
                    "command": "legacy-should-not-import",
                    "args": ["--legacy"],
                },
            }
        },
    )

    store1 = ExtensionStore(tmp_path / "store-followup", X25519PrivateKey.generate())
    manager1 = ExtensionImportManager(store1)
    preview1 = manager1.preview({"kind": "plugin", "path": str(pkg_followup)})
    assert preview1["can_import"] is True
    assert preview1["name"] == "Portable MCP"
    assert preview1["version"] == "1.0.0"
    assert preview1["source_format"] == "ryn_plugin"
    assert preview1["warnings"] == []
    assert preview1["unsupported_components"] == []
    assert [m["id"] for m in preview1["mcp_servers"]] == ["portable-canonical-mcp-docs"]
    assert preview1["mcp_servers"][0]["transport"] == "streamable_http"
    assert preview1["mcp_servers"][0]["url"] == "https://example.com/mcp"

    overview1 = manager1.commit(
        preview1["preview_id"], expected_revision=preview1["expected_revision"]
    )
    stored_servers1 = [m for m in overview1["mcp_servers"] if not m.get("builtin")]
    assert [m["id"] for m in stored_servers1] == ["portable-canonical-mcp-docs"]
    assert stored_servers1[0]["url"] == "https://example.com/mcp"

    # 2. Portable root stdio command in mcp.json is preserved over .mcp.json command
    pkg_cmd = tmp_path / "portable-stdio-canonical-cmd"
    _write_json(
        pkg_cmd / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "portable-stdio-plugin",
            "version": "1.0.0",
        },
    )
    _write_json(
        pkg_cmd / "mcp.json",
        {
            "mcpServers": {
                "docs": {
                    "command": "canonical-root-cmd",
                    "args": ["--from-canonical-mcp-json"],
                }
            }
        },
    )
    _write_json(
        pkg_cmd / ".codex-plugin" / "plugin.json",
        {
            "name": "portable-stdio-plugin",
            "version": "1.0.0",
            "mcpServers": "./.mcp.json",
        },
    )
    _write_json(
        pkg_cmd / ".mcp.json",
        {
            "mcpServers": {
                "docs": {
                    "command": "legacy-dot-mcp-cmd",
                    "args": ["--from-legacy-dot-mcp"],
                }
            }
        },
    )

    store2 = ExtensionStore(tmp_path / "store-stdio-cmd", X25519PrivateKey.generate())
    manager2 = ExtensionImportManager(store2)
    preview2 = manager2.preview({"kind": "plugin", "path": str(pkg_cmd)})
    assert preview2["can_import"] is True
    assert [m["id"] for m in preview2["mcp_servers"]] == ["portable-stdio-plugin-docs"]
    assert preview2["mcp_servers"][0]["command"] == "canonical-root-cmd"
    assert preview2["mcp_servers"][0]["args"] == ["--from-canonical-mcp-json"]

    # 3. Legacy Codex/Claude packages without a portable root still load .mcp.json
    for vendor in ("codex", "claude"):
        legacy_pkg = tmp_path / f"legacy-{vendor}-dot-mcp"
        _write_json(
            legacy_pkg / f".{vendor}-plugin" / "plugin.json",
            {"name": f"legacy-{vendor}-pkg", "version": "1.0.0"},
        )
        _write_json(
            legacy_pkg / ".mcp.json",
            {
                "mcpServers": {
                    "docs": {
                        "command": f"{vendor}-dot-mcp-cmd",
                        "args": ["--ok"],
                    }
                }
            },
        )
        store_v = ExtensionStore(tmp_path / f"store-legacy-{vendor}", X25519PrivateKey.generate())
        mgr_v = ExtensionImportManager(store_v)
        prev_v = mgr_v.preview({"kind": "plugin", "path": str(legacy_pkg)})
        assert prev_v["can_import"] is True
        assert [m["id"] for m in prev_v["mcp_servers"]] == [f"legacy-{vendor}-pkg-docs"]
        assert prev_v["mcp_servers"][0]["command"] == f"{vendor}-dot-mcp-cmd"

