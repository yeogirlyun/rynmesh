"""Tests for S09-I4: automatic workspace skill discovery, repo-scoped visibility, Ryn roots, fingerprint cache, and source_missing lifecycle."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from types import SimpleNamespace

from rynmesh.agent_runtime.extensions import ExtensionStore, ExtensionToolAdapter, codex_skills
from rynmesh.agent_runtime.extensions.codex_skills import (
    default_local_skill_roots,
    discover_local_skills,
)
from rynmesh.agent_runtime.extensions.routes import install_extension_routes
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def _write_skill(skill_dir: Path, skill_id: str, description: str, body: str, *, display_name: str | None = None) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    if display_name:
        (skill_dir / "agents").mkdir(parents=True, exist_ok=True)
        (skill_dir / "agents" / "openai.yaml").write_text(
            f'interface:\n  display_name: "{display_name}"\n',
            encoding="utf-8",
        )
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return md


def _make_runtime(tmp_path: Path, default_ws: Path) -> tuple[AgentRuntime, ExtensionStore]:
    home = tmp_path / "node_home"
    home.mkdir(parents=True, exist_ok=True)
    default_ws.mkdir(parents=True, exist_ok=True)
    key = X25519PrivateKey.generate()
    agent_store = AgentStore(home / "agent", key)
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store
    ext_adapter = ExtensionToolAdapter(local, lambda: ledger)
    ext_adapter.bind_agent_store(agent_store)

    class _DummyCatalog:
        def list_models(self) -> list[dict]:
            return [
                {
                    "id": "local:test",
                    "name": "Test Model",
                    "source": "local_api",
                    "source_id": "fixture",
                    "destination_revision": "fixture-v1",
                    "available": True,
                    "agent_supported": True,
                }
            ]

    class _DummyModel:
        supports_image_input = True

        def complete(self, *_args, **_kw):
            return {"message": {"role": "assistant", "content": "Done."}}

    class _DummyTools:
        extensions = ext_adapter

        def specs(self) -> list[dict]:
            return ext_adapter.specs()

        def metadata(self, name: str) -> dict:
            if ext_adapter.handles(name):
                return ext_adapter.metadata(name)
            return {"effect": "read", "title": name, "risk": "low", "scope_context_supported": True}

        def close(self) -> None:
            local.processes.close()
            ext_adapter.close()

    rt = AgentRuntime(agent_store, _DummyModel(), _DummyTools(), _DummyCatalog())
    rt.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "workspace_root": str(default_ws),
            "permission_mode": "confirm_changes",
            "desktop_access": False,
        }
    )
    return rt, ext_adapter.store


def test_t1_subworkspace_discovers_git_root_and_intermediate_layer_skills(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    repo = tmp_path / "repo"
    sub = repo / "sub"
    (repo / ".git").mkdir(parents=True)
    sub.mkdir(parents=True)

    _write_skill(repo / ".ryn" / "skills" / "ryn-root", "ryn-root", "Ryn root skill", "# Ryn Root")
    _write_skill(repo / ".agents" / "skills" / "repo-shared", "repo-shared", "Repo root skill", "# Repo Shared")
    _write_skill(sub / ".agents" / "skills" / "sub-agent", "sub-agent", "Subdir agent skill", "# Sub Agent")
    _write_skill(sub / ".codex" / "skills" / "sub-codex", "sub-codex", "Subdir codex skill", "# Sub Codex")

    roots = default_local_skill_roots([sub])
    root_paths = [Path(r[0]).resolve() for r in roots]
    assert (repo / ".ryn" / "skills").resolve() in root_paths
    assert (repo / ".agents" / "skills").resolve() in root_paths
    assert (sub / ".agents" / "skills").resolve() in root_paths
    assert (sub / ".codex" / "skills").resolve() in root_paths
    # .ryn/skills at the same layer has higher priority than .agents/skills and .codex/skills
    assert root_paths.index((repo / ".ryn" / "skills").resolve()) < root_paths.index((repo / ".agents" / "skills").resolve())

    discovered = discover_local_skills(workspace_roots=[sub])
    by_id = {d["id"]: d for d in discovered}
    assert {"ryn-root", "repo-shared", "sub-agent", "sub-codex"} <= set(by_id.keys())
    assert by_id["repo-shared"]["scope"] == "repo"
    assert by_id["sub-agent"]["scope"] == "repo"
    assert by_id["repo-shared"].get("workspace_root")


def test_t2_fingerprint_cache_skips_rescan_when_unchanged_and_picks_up_skill_md_edit_on_next_task(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    md_path = _write_skill(ws / ".agents" / "skills" / "auto-check", "auto-check", "Auto skill v1", "Body version 1")

    rt, store = _make_runtime(tmp_path, ws)

    real_discover = codex_skills.discover_local_skills
    scan_calls = 0

    def counting_discover(*args, **kwargs):
        nonlocal scan_calls
        scan_calls += 1
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(codex_skills, "discover_local_skills", counting_discover)

    # Task 1 triggers automatic discovery on task creation
    t1 = rt.create({"message": "Run $auto-check now", "model_id": "local:test"})
    row1 = rt.store.read()["tasks"][t1["id"]]
    assert scan_calls == 1
    assert any(s["id"] == "auto-check" and "Body version 1" in s["content"] for s in row1["skills"])

    # Direct second sync with unchanged roots hits fingerprint cache -> scan_calls stays 1!
    store.sync_discovered_skills(workspace_roots=[ws])
    assert scan_calls == 1

    # Edit SKILL.md on disk and bypass/advance the 10s runtime throttle -> next task gets Body version 2!
    md_path.write_text(
        "---\nname: auto-check\ndescription: Auto skill v2\n---\nBody version 2 updated\n",
        encoding="utf-8",
    )
    rt._skill_sync_last_at.clear()
    t2 = rt.create({"message": "Run $auto-check again", "model_id": "local:test"})
    row2 = rt.store.read()["tasks"][t2["id"]]
    assert scan_calls == 2
    assert any(s["id"] == "auto-check" and "Body version 2 updated" in s["content"] for s in row2["skills"])

    # Also verify POST /api/local/agent/extensions/skills/refresh passes current default workspace (Change 5)
    _write_skill(ws / ".ryn" / "skills" / "via-route", "via-route", "Discovered via refresh route", "Route body")
    app = FastAPI()
    install_extension_routes(app, runtime=rt, extensions=rt.extensions, local_control=lambda _req: None)
    with TestClient(app) as client:
        refreshed = client.post("/api/local/agent/extensions/skills/refresh").json()
        skill_ids = {s["id"] for s in refreshed["overview"]["skills"]}
        assert "via-route" in skill_ids
        assert refreshed["overview"].get("skill_roots")


def test_t3_workspace_a_repo_skill_invisible_in_workspace_b_and_legacy_repo_skill_stays_visible(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    (ws_a / ".git").mkdir(parents=True)
    (ws_b / ".git").mkdir(parents=True)
    (ws_a / "child").mkdir(parents=True)

    _write_skill(ws_a / ".agents" / "skills" / "only-a", "only-a", "Workspace A skill", "Secret A instructions")
    _write_skill(ws_b / ".agents" / "skills" / "only-b", "only-b", "Workspace B skill", "Secret B instructions")
    legacy_dir = tmp_path / "legacy_repo_skills"
    _write_skill(legacy_dir / "legacy-shared", "legacy-shared", "Legacy repo skill without workspace_roots", "Legacy body")

    rt, store = _make_runtime(tmp_path, ws_a)
    store.sync_discovered_skills(roots=[(legacy_dir, "repo")])  # legacy record without workspace_roots
    store.sync_discovered_skills(workspace_roots=[ws_a, ws_b])

    cat_a = {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_a)}
    cat_a_child = {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_a / "child")}
    cat_b = {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_b)}

    assert "only-a" in cat_a and "only-b" not in cat_a and "legacy-shared" in cat_a
    assert "only-a" in cat_a_child and "only-b" not in cat_a_child
    assert "only-b" in cat_b and "only-a" not in cat_b and "legacy-shared" in cat_b

    with pytest.raises(AgentError, match="agent_invalid_skills"):
        store.select_skills(["only-a"], workspace_root=ws_b)

    # Switch runtime workspace to ws_b: $only-a is invisible, explicit skill_ids=["only-a"] is rejected!
    rt.update_settings({"workspace_root": str(ws_b)})
    with pytest.raises(AgentError, match="agent_invalid_skills"):
        rt.create({"message": "try a", "model_id": "local:test", "skill_ids": ["only-a"]})

    task_b = rt.create({"message": "Use $only-a and $only-b", "model_id": "local:test"})
    row_b = rt.store.read()["tasks"][task_b["id"]]
    selected_b_ids = {s["id"] for s in row_b["skills"]}
    assert "only-b" in selected_b_ids
    assert "only-a" not in selected_b_ids


def test_t4_two_repos_same_id_do_not_overwrite_and_ryn_user_root_prioritized_with_duplicate_warning(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    # Part 1: ~/.ryn/skills/x/SKILL.md vs ~/.agents/skills/x/SKILL.md -> Ryn root wins + duplicate_id warning
    _write_skill(fake_home / ".ryn" / "skills" / "x", "x", "From Ryn user root", "RYN_PRIORITY_CONTENT")
    _write_skill(fake_home / ".agents" / "skills" / "x", "x", "From Agents user root", "AGENTS_SHADOWED_CONTENT")

    # Part 2: repo_1 and repo_2 both have repo skill id="deploy"
    repo_1 = tmp_path / "repo_1"
    repo_2 = tmp_path / "repo_2"
    (repo_1 / ".git").mkdir(parents=True)
    (repo_2 / ".git").mkdir(parents=True)
    _write_skill(repo_1 / ".agents" / "skills" / "deploy", "deploy", "Deploy repo 1", "REPO_1_DEPLOY", display_name="Deploy Workflow")
    _write_skill(repo_2 / ".agents" / "skills" / "deploy", "deploy", "Deploy repo 2", "REPO_2_DEPLOY", display_name="Deploy Workflow")

    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    res = store.sync_discovered_skills(workspace_roots=[repo_1, repo_2])

    warnings = res.get("warnings") or []
    assert any(w.get("reason") == "duplicate_id" and "x" in str(w.get("path", "")) for w in warnings)

    x_snap = store.select_skills(["x"], workspace_root=repo_1)[0]
    assert "RYN_PRIORITY_CONTENT" in x_snap["content"]

    ov_skills = res["overview"]["skills"]
    deploy_entries = [s for s in ov_skills if s["name"] == "deploy"]
    assert len(deploy_entries) == 2
    deploy_ids = {s["id"] for s in deploy_entries}
    assert any(re.fullmatch(r"deploy\.[0-9a-f]{6}", sid) for sid in deploy_ids)
    for entry in deploy_entries:
        assert entry["title"] == "Deploy Workflow"

    cat_1 = [s for s in store.enabled_skills_catalog(workspace_root=repo_1) if s.get("title") == "Deploy Workflow"]
    cat_2 = [s for s in store.enabled_skills_catalog(workspace_root=repo_2) if s.get("title") == "Deploy Workflow"]
    assert len(cat_1) == 1 and len(cat_2) == 1 and cat_1[0]["id"] != cat_2[0]["id"]

    snap_1 = store.select_skills([cat_1[0]["id"]], workspace_root=repo_1)[0]
    snap_2 = store.select_skills([cat_2[0]["id"]], workspace_root=repo_2)[0]
    assert "REPO_1_DEPLOY" in snap_1["content"]
    assert "REPO_2_DEPLOY" in snap_2["content"]


def test_t5_deleted_source_dir_marks_source_missing_hides_from_catalog_and_rejects_selection(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    ws_a = tmp_path / "ws_a"
    (ws_a / ".git").mkdir(parents=True)
    skill_a_dir = ws_a / ".agents" / "skills" / "skill-a"
    _write_skill(skill_a_dir, "skill-a", "Workspace A skill", "Skill A content")

    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    res0 = store.sync_discovered_skills(workspace_roots=[ws_a])
    ov0 = {s["id"]: s for s in res0["overview"]["skills"]}
    pkg_copy_dir = Path(ov0["skill-a"]["package_dir"])
    assert pkg_copy_dir.is_dir()
    assert "skill-a" in {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_a)}

    shutil.rmtree(skill_a_dir)
    res_missing = store.sync_discovered_skills(workspace_roots=[ws_a])
    ov_missing = {s["id"]: s for s in res_missing["overview"]["skills"]}
    assert ov_missing["skill-a"]["source_missing"] is True
    assert ov_missing["skill-a"]["deletable"] is True
    assert pkg_copy_dir.is_dir()
    assert "skill-a" not in {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_a)}
    with pytest.raises(AgentError, match="agent_invalid_skills"):
        store.select_skills(["skill-a"], workspace_root=ws_a)

    # Manual uninstall removes the retained record and package directory
    del_res = store.delete_skill("skill-a", expected_revision=ov_missing["skill-a"]["revision"])
    assert del_res["deleted"] is True
    assert not pkg_copy_dir.exists()
    assert "skill-a" not in {s["id"] for s in store.overview()["skills"]}


def test_t6_restoring_deleted_source_dir_clears_source_missing_and_restores_visibility(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    ws_a = tmp_path / "ws_a"
    (ws_a / ".git").mkdir(parents=True)
    skill_a_dir = ws_a / ".agents" / "skills" / "skill-a"
    _write_skill(skill_a_dir, "skill-a", "Workspace A skill", "Skill A content")

    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    store.sync_discovered_skills(workspace_roots=[ws_a])
    shutil.rmtree(skill_a_dir)
    assert {s["id"]: s for s in store.sync_discovered_skills(workspace_roots=[ws_a])["overview"]["skills"]}["skill-a"]["source_missing"] is True

    _write_skill(skill_a_dir, "skill-a", "Workspace A skill", "Skill A content")
    res_restored = store.sync_discovered_skills(workspace_roots=[ws_a])
    ov_restored = {s["id"]: s for s in res_restored["overview"]["skills"]}
    assert ov_restored["skill-a"]["source_missing"] is False
    assert "skill-a" in {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_a)}
    selected = store.select_skills(["skill-a"], workspace_root=ws_a)
    assert len(selected) == 1 and "Skill A content" in selected[0]["content"]


def test_t7_syncing_workspace_b_does_not_mark_workspace_a_skill_as_source_missing(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("RYN_HOME", str(fake_home / ".ryn"))

    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    (ws_a / ".git").mkdir(parents=True)
    (ws_b / ".git").mkdir(parents=True)

    _write_skill(ws_a / ".agents" / "skills" / "skill-a", "skill-a", "Workspace A skill", "Skill A content")
    _write_skill(ws_b / ".agents" / "skills" / "skill-b", "skill-b", "Workspace B skill", "Skill B content")

    store = ExtensionStore(tmp_path / "store", X25519PrivateKey.generate())
    store.sync_discovered_skills(workspace_roots=[ws_a])

    # Delete ws_b skill? No: keep ws_a skill on disk, sync ONLY ws_b (and also delete a separate ws_b skill to ensure missing-sweep runs):
    res_b = store.sync_discovered_skills(workspace_roots=[ws_b])
    ov_after_b = {s["id"]: s for s in res_b["overview"]["skills"]}
    assert ov_after_b["skill-a"]["source_missing"] is False
    assert "skill-a" in {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_a)}
    assert "skill-a" not in {s["id"] for s in store.enabled_skills_catalog(workspace_root=ws_b)}


def test_missing_source_dollar_mention_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home/.codex"))
    monkeypatch.setenv("RYN_HOME", str(tmp_path / "home/.ryn"))
    ws = tmp_path / "workspace"
    source = ws / ".ryn/skills/vanished"
    _write_skill(source, "vanished", "Missing workflow", "Instructions")
    runtime, store = _make_runtime(tmp_path, ws)
    store.sync_discovered_skills(workspace_roots=[ws])
    shutil.rmtree(source)
    with pytest.raises(AgentError, match="agent_invalid_skills"):
        runtime.create({"message": "Use $vanished", "model_id": "local:test"})


def test_project_task_discovers_project_skills_and_throttles_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home/.codex"))
    monkeypatch.setenv("RYN_HOME", str(tmp_path / "home/.ryn"))
    runtime, store = _make_runtime(tmp_path, tmp_path / "default")
    project = tmp_path / "project"
    _write_skill(project / ".ryn/skills/project-only", "project-only", "Project workflow", "PROJECT INSTRUCTIONS")
    selected = runtime.load_project({"path": str(project)})
    runtime.model.supports_destination_binding = True
    with patch.object(store, "sync_discovered_skills", wraps=store.sync_discovered_skills) as sync:
        for _ in range(2):
            task = runtime.create({"message": "Use $project-only", "model_id": "local:test", "project_id": selected["id"]})
            row = runtime.store.read()["tasks"][task["id"]]
            assert row["local_execution"]["workspace_root"] == str(project)
            assert any(s["id"] == "project-only" for s in row["skills"])
        assert sync.call_count == 1
        assert sync.call_args.kwargs["workspace_roots"] == [project]
