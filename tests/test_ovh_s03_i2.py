"""S03-I2: bounded repository-chain documents and immutable workspace snapshots."""
import hashlib
from pathlib import Path

import pytest

from test_agent_projects import project_runtime
from rynmesh.agent_runtime import projects
from rynmesh.agent_runtime import subagents
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def repo(tmp_path):
    root = tmp_path / "repo"
    leaf = root / "src" / "module"
    leaf.mkdir(parents=True)
    (root / ".git").mkdir()
    return root, leaf


def collect(root, **kw):
    return projects.collect_instructions(root, **kw)


def test_t1_repository_to_selected_directory_in_order_and_no_deeper_scan(tmp_path):
    root, leaf = repo(tmp_path)
    (root / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (leaf / "AGENTS.md").write_text("leaf rule", encoding="utf-8")
    deeper = leaf / "deeper"
    deeper.mkdir()
    (deeper / "AGENTS.md").write_text("not selected", encoding="utf-8")
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(leaf)})
    created = runtime.create({"message": "inspect", "project_id": selected["id"]})
    row = runtime.store.read()["tasks"][created["id"]]
    instructions = row["project"]["instructions"]
    assert instructions["text"].index("root rule") < instructions["text"].index("leaf rule")
    assert "\n\n--- src/module/AGENTS.md ---\n\n" in instructions["text"]
    assert len(instructions["files"]) == 2 and instructions["path"] == instructions["files"][0]["path"]
    assert "not selected" not in instructions["text"]
    assert str(leaf) in projects.guidance(row)
    assert "root rule" in str(runtime._context(row)) and "leaf rule" in str(runtime._context(row))


def test_t2_override_wins_per_directory_without_erasing_ancestor_rules(tmp_path):
    root, leaf = repo(tmp_path)
    (root / "AGENTS.md").write_text("root normal", encoding="utf-8")
    (root / "AGENTS.override.md").write_text("root override", encoding="utf-8")
    (leaf / "AGENTS.md").write_text("leaf normal", encoding="utf-8")
    result = collect(leaf)
    assert "root override" in result["text"] and "leaf normal" in result["text"]
    assert "root normal" not in result["text"] and len(result["files"]) == 2


def test_t3_fallback_and_nearest_git_boundary_including_worktree_file(tmp_path):
    root, leaf = repo(tmp_path)
    (root / "AGENTS.md").write_text("outer repository", encoding="utf-8")
    (leaf / ".git").write_text("gitdir: somewhere\n", encoding="utf-8")
    raw = b"fallback instructions"
    (leaf / "CLAUDE.md").write_bytes(raw)
    result = collect(leaf)
    assert result["text"] == raw.decode() and len(result["files"]) == 1
    assert result["files"][0]["path"].endswith("CLAUDE.md")
    assert result["files"][0]["bytes"] == len(raw)
    assert result["files"][0]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_t4_malicious_fallback_names_ignored_and_no_repository_means_current_only(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside.md").write_text("outside secret", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("not in a git repository", encoding="utf-8")
    (root / "CLAUDE.md").write_text("safe fallback", encoding="utf-8")
    result = collect(root, fallback_names=["../outside.md", "..\\outside.md", "C:outside.md", "bad\0.md", ".", "..", "CLAUDE.md"])
    assert result["text"] == "safe fallback"
    assert len(result["files"]) == 1


def test_t5_instruction_symlink_cannot_leave_repository(tmp_path):
    root, leaf = repo(tmp_path)
    outside = tmp_path / "external.md"
    outside.write_text("outside secret", encoding="utf-8")
    (leaf / "AGENTS.override.md").symlink_to(outside)
    (leaf / "AGENTS.md").write_text("safe in-repository rule", encoding="utf-8")
    result = collect(leaf)
    assert result["text"] == "safe in-repository rule"
    assert "outside secret" not in result["text"]


def test_t6_new_workspace_task_snapshot_reaches_context_and_is_never_reread(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "AGENTS.md"
    document.write_text("workspace-original", encoding="utf-8")
    runtime, model, tools, key = project_runtime(tmp_path / "state")
    runtime.update_settings({"workspace_root": str(root)})
    task = runtime.create({"message": "ordinary workspace task"})
    row = runtime.store.read()["tasks"][task["id"]]
    assert "project" not in row and row["workspace_instructions"]["text"] == "workspace-original"
    document.write_text("workspace-changed", encoding="utf-8")
    restarted = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
    guidance = projects.guidance(restarted.store.read()["tasks"][task["id"]])
    assert "workspace-original" in guidance and "workspace-changed" not in guidance
    assert "<environment_context>" in guidance and f"<cwd>{root}</cwd>" in guidance and "<project>" not in guidance
    assert "workspace-original" in str(restarted._context(restarted.store.read()["tasks"][task["id"]]))
    newer = restarted.create({"message": "new task"})
    assert restarted.store.read()["tasks"][newer["id"]]["workspace_instructions"]["text"] == "workspace-changed"


def test_t6_legacy_workspace_tasks_are_not_lazily_backfilled(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    row = {"local_execution": {"workspace_root": str(root)}, "limits": {"max_context_chars": None}}
    (root / "AGENTS.md").write_text("new file on old task", encoding="utf-8")
    guidance = projects.guidance(row)
    assert "<environment_context>" in guidance and f"<cwd>{root}</cwd>" in guidance
    assert "new file on old task" not in guidance and "<project>" not in guidance


def test_t7_shared_read_budget_bounds_all_file_reads_and_marks_prefix_hash(tmp_path, monkeypatch):
    root, leaf = repo(tmp_path)
    (root / "AGENTS.md").write_text("R" * 40000, encoding="utf-8")
    (leaf / "AGENTS.md").write_text("L" * 40000, encoding="utf-8")
    read_sizes = []
    original = Path.open
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size=-1):
            assert size >= 0, "instruction reads must be bounded"
            value = self.stream.read(size)
            read_sizes.append(len(value))
            return value
    def opened(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        return Reader(stream) if path.name == "AGENTS.md" and mode == "rb" else stream
    monkeypatch.setattr(Path, "open", opened)
    result = collect(leaf)
    assert result["incomplete"] is True
    assert sum(read_sizes) <= 65536 and len(result["text"].encode()) <= 65536
    assert result["files"][0]["sha256"] == hashlib.sha256(b"R" * 40000).hexdigest()
    assert result["files"][1]["read_bytes"] < 40000
    assert result["files"][1]["sha256_scope"] == "read_prefix"


@pytest.mark.parametrize("names", [["../secret"], ["C:secret"], ["bad\0.md"], ["."], [1], "CLAUDE.md"])
def test_settings_reject_unsafe_fallback_lists(tmp_path, names):
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"project_doc_fallback_filenames": names})


def test_setting_custom_fallback_reaches_project_and_workspace_snapshots(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "TEAM.md").write_text("custom team rules", encoding="utf-8")
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    assert runtime.settings()["project_doc_fallback_filenames"] == ["CLAUDE.md"]
    runtime.update_settings({"project_doc_fallback_filenames": ["TEAM.md"], "workspace_root": str(root)})
    normal = runtime.create({"message": "ordinary"})
    assert runtime.store.read()["tasks"][normal["id"]]["workspace_instructions"]["text"] == "custom team rules"
    project = runtime.load_project({"path": str(root)})
    selected = runtime.create({"message": "project", "project_id": project["id"]})
    assert runtime.store.read()["tasks"][selected["id"]]["project"]["instructions"]["text"] == "custom team rules"


def test_workspace_snapshot_inherits_existing_injection_budget_and_event(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("rule\n" * 10000, encoding="utf-8")
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    runtime.update_settings({"workspace_root": str(root), "max_context_chars": 12000})
    task = runtime.create({"message": "ordinary"})
    row = runtime.store.read()["tasks"][task["id"]]
    info = projects.instructions_truncation(row)
    assert info["injected_bytes"] <= 3000
    assert "truncated:" in projects.guidance(row)
    assert any(event["type"] == "project_instructions_truncated" for event in row["events"])


def test_multi_file_injection_continuation_points_to_the_actual_truncated_source(tmp_path):
    root, leaf = repo(tmp_path)
    (root / "AGENTS.md").write_text("root-rule\n" * 10, encoding="utf-8")
    (leaf / "AGENTS.md").write_text("leaf-rule\n" * 1000, encoding="utf-8")
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    runtime.update_settings({"max_context_chars": 12000})
    project = runtime.load_project({"path": str(leaf)})
    task = runtime.create({"message": "inspect", "project_id": project["id"]})
    row = runtime.store.read()["tasks"][task["id"]]
    info = projects._instructions_slice(row)
    source = row["project"]["instructions"]["files"][1]
    prefix = info["text"].encode()[source["start_byte"]:]
    expected_line = len(prefix.splitlines()) + 1
    assert f"path={leaf / 'AGENTS.md'} start_line={expected_line}" in projects.guidance(row)


def test_subagent_uses_the_frozen_workspace_snapshot_without_new_file_reads(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "AGENTS.md"
    document.write_text("original shared rules", encoding="utf-8")
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    runtime.update_settings({"workspace_root": str(root)})
    parent = runtime.create({"message": "coordinate"})["id"]
    document.write_text("later rules", encoding="utf-8")
    def spawn(data):
        return subagents.spawn_agent_in_tx(data, data["tasks"][parent], {
            "task_name": "worker", "prompt": "inspect", "role": "explorer"})["agent_id"]
    child_id = runtime.store.transaction(spawn)
    child = runtime.store.read()["tasks"][child_id]
    assert child["workspace_instructions"]["text"] == "original shared rules"
    assert "original shared rules" in str(runtime._context(child))
    assert "later rules" not in projects.guidance(child)
