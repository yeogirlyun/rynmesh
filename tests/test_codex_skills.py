"""Tests for Codex-compatible Skills discovery, agents/openai.yaml metadata, recommended skills, and installation."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions.codex_skills import (
    extract_codex_skill_metadata,
)
from rynmesh.agent_runtime.store import AgentError


def test_extract_codex_skill_metadata_with_openai_yaml_and_svg_icon(tmp_path: Path) -> None:
    skill_dir = tmp_path / "figma-probe"
    (skill_dir / "agents").mkdir(parents=True)
    (skill_dir / "assets").mkdir(parents=True)

    svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/></svg>'
    (skill_dir / "assets" / "figma-small.svg").write_bytes(svg_bytes)

    (skill_dir / "agents" / "openai.yaml").write_text(
        'interface:\n'
        '  display_name: "Figma Design Probe"\n'
        '  short_description: "Use Figma MCP for design-to-code work"\n'
        '  icon_small: "./assets/figma-small.svg"\n'
        '  brand_color: "#0D99FF"\n'
        '  default_prompt: "Use $figma-probe to inspect the design."\n'
        'policy:\n'
        '  allow_implicit_invocation: false\n',
        encoding="utf-8",
    )
    md_text = (
        "---\n"
        "name: figma-probe\n"
        "description: Full description from SKILL.md frontmatter\n"
        "version: 2.1.0\n"
        "---\n"
        "# Figma Probe\n"
        "Detailed workflow instructions.\n"
    )
    (skill_dir / "SKILL.md").write_text(md_text, encoding="utf-8")

    meta = extract_codex_skill_metadata(skill_dir, md_text, fallback_id="figma-probe")
    assert meta["id"] == "figma-probe"
    assert meta["title"] == "Figma Design Probe"
    assert meta["short_description"] == "Use Figma MCP for design-to-code work"
    assert meta["description"] == "Full description from SKILL.md frontmatter"
    assert meta["version"] == "2.1.0"
    assert meta["interface"]["display_name"] == "Figma Design Probe"
    assert meta["interface"]["brand_color"] == "#0D99FF"
    assert meta["interface"]["default_prompt"] == "Use $figma-probe to inspect the design."
    assert meta["interface"]["icon_small"] is not None
    assert meta["interface"]["icon_small"].startswith("data:image/svg+xml;base64,")
    assert meta["policy"]["allow_implicit_invocation"] is False


def test_sync_discovered_skills_scopes_and_uninstall_protection(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())

    sys_root = tmp_path / "codex_home" / "skills" / ".system"
    user_root = tmp_path / "codex_home" / "skills"
    repo_root = tmp_path / "workspace" / ".codex" / "skills"

    # 1. System skill (scope='system', non-deletable)
    s_creator = sys_root / "skill-creator"
    (s_creator / "agents").mkdir(parents=True)
    (s_creator / "agents" / "openai.yaml").write_text(
        'interface:\n  display_name: "Skill Creator"\n  short_description: "Create or update a skill"\n',
        encoding="utf-8",
    )
    (s_creator / "SKILL.md").write_text(
        "---\nname: skill-creator\ndescription: System skill creator\n---\n# Skill Creator\n",
        encoding="utf-8",
    )

    # 2. User skill (scope='user', deletable)
    u_skill = user_root / "daily-report"
    (u_skill / "references").mkdir(parents=True)
    (u_skill / "SKILL.md").write_text(
        "---\nname: daily-report\ndescription: Write daily reports\n---\n# Daily Report\n",
        encoding="utf-8",
    )
    (u_skill / "references" / "template.md").write_text("## Daily Template\n", encoding="utf-8")

    # 3. Repo skill (scope='repo', deletable)
    r_skill = repo_root / "repo-linter"
    r_skill.mkdir(parents=True)
    (r_skill / "SKILL.md").write_text(
        "---\nname: repo-linter\ndescription: Lint repository files\n---\n# Repo Linter\n",
        encoding="utf-8",
    )

    res = store.sync_discovered_skills(
        roots=[
            (repo_root, "repo"),
            (sys_root, "system"),
            (user_root, "user"),
        ]
    )
    assert set(res["synced_skill_ids"]) == {"skill-creator", "daily-report", "repo-linter"}

    skills_by_id = {s["id"]: s for s in res["overview"]["skills"]}
    assert skills_by_id["skill-creator"]["scope"] == "system"
    assert skills_by_id["skill-creator"]["deletable"] is False
    assert skills_by_id["skill-creator"]["title"] == "Skill Creator"
    assert skills_by_id["skill-creator"]["enabled"] is True

    assert skills_by_id["daily-report"]["scope"] == "user"
    assert skills_by_id["daily-report"]["deletable"] is True
    assert skills_by_id["repo-linter"]["scope"] == "repo"

    # System skills cannot be deleted
    with pytest.raises(AgentError) as exc_info:
        store.delete_skill("skill-creator", expected_revision=skills_by_id["skill-creator"]["revision"])
    assert exc_info.value.args == ("agent_extension_builtin_protected",)

    # Disable daily-report and verify read_skill_file(..., allow_disabled=True) still works for UI preview
    disabled_view = store.set_skill_enabled(
        "daily-report",
        False,
        expected_revision=skills_by_id["daily-report"]["revision"],
    )
    assert disabled_view["enabled"] is False
    preview_file = store.read_skill_file("daily-report", "references/template.md", allow_disabled=True)
    assert preview_file["content"].strip() == "## Daily Template"

    # Delete daily-report -> subsequent sync_discovered_skills does not resurrect it unless reset_uninstalled=True
    store.delete_skill("daily-report", expected_revision=disabled_view["revision"])
    res_after_del = store.sync_discovered_skills(
        roots=[
            (repo_root, "repo"),
            (sys_root, "system"),
            (user_root, "user"),
        ]
    )
    assert "daily-report" not in {s["id"] for s in res_after_del["overview"]["skills"]}

    res_reset = store.sync_discovered_skills(
        roots=[
            (repo_root, "repo"),
            (sys_root, "system"),
            (user_root, "user"),
        ],
        reset_uninstalled=True,
    )
    assert "daily-report" in {s["id"] for s in res_reset["overview"]["skills"]}


def test_recommended_skills_listing_and_one_click_install(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    curated_root = tmp_path / "vendor_imports" / "skills" / "skills" / ".curated"
    pw_dir = curated_root / "playwright"
    (pw_dir / "agents").mkdir(parents=True)
    (pw_dir / "agents" / "openai.yaml").write_text(
        'interface:\n'
        '  display_name: "Playwright"\n'
        '  short_description: "Automate real browsers with Playwright"\n'
        '  default_prompt: "Automate this browser workflow with Playwright."\n',
        encoding="utf-8",
    )
    (pw_dir / "SKILL.md").write_text(
        "---\nname: playwright\ndescription: Browser automation skill\n---\n# Playwright\nRun browser checks.\n",
        encoding="utf-8",
    )

    rec_list = store.list_recommended_skills(curated_roots=[curated_root])
    assert rec_list["count"] == 1
    assert rec_list["skills"][0]["id"] == "playwright"
    assert rec_list["skills"][0]["installed"] is False

    installed = store.install_recommended_skill(
        {"skill_id": "playwright", "repo_path": "skills/.curated/playwright"},
        curated_roots=[curated_root],
    )
    assert installed["skill"]["id"] == "playwright"
    assert installed["skill"]["enabled"] is True
    assert installed["skill"]["interface"]["default_prompt"] == "Automate this browser workflow with Playwright."

    rec_after = store.list_recommended_skills(curated_roots=[curated_root])
    assert rec_after["skills"][0]["installed"] is True

    # Installed recommended skill can be selected for tasks immediately
    snapshots = store.select_skills(["playwright"])
    assert len(snapshots) == 1
    assert snapshots[0]["id"] == "playwright"
