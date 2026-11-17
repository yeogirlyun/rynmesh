"""S05-I30: inventory bounds, receipts and synchronous snapshot reuse."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_agent_local_tools import local, execute
from test_workspace_artifacts import _receipt
from rynmesh.agent_runtime import local_tools, workspace_artifacts as artifacts


@pytest.fixture
def python_shell(monkeypatch):
    monkeypatch.setattr(local_tools, "prepare_shell_command", lambda args: [sys.executable, "-c", args["command"]])


def run(local, code, operation):
    tools, context, _, _ = local
    return execute(tools, context, "ryn_shell_execute", {"command": code}, operation)


def test_t1_generated_and_gitignored_directories_skipped_with_root_anchor(tmp_path):
    for name in ("dist", "build", "out", ".gradle", ".idea", "coverage", "cache", "root-cache", "src/root-cache"):
        target = tmp_path / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "data.txt").write_text("data")
    (tmp_path / ".gitignore").write_text("cache/\n/root-cache/\n")
    snapshot = artifacts.snapshot(tmp_path)
    assert snapshot["complete"]
    assert {str(Path(p).relative_to(tmp_path)).replace("\\", "/") for p in snapshot["files"]} == {
        ".gitignore", "src/root-cache/data.txt"}


def test_t2_6000_files_incomplete_reaches_shell_receipt_and_listing(local, python_shell):
    tools, context, workspace, state = local
    for index in range(6000):
        (workspace / f"f{index:04d}.txt").write_text("x")
    assert artifacts.snapshot(workspace)["complete"] is False
    result = run(local, "pass", "large-workspace")
    assert result["workspace_files_complete"] is False
    row = state.read()["tasks"]["task"]
    receipt = row["workspace_file_receipts"]["large-workspace"]["result"]
    assert receipt["workspace_files_complete"] is False and receipt["workspace_files"] == []
    assert artifacts.listing(row)["incomplete_inventory"] is True


@pytest.mark.parametrize("delete", [False, True])
def test_t3_100_changes_keep_64_count_all_and_only_hash_64(tmp_path, monkeypatch, delete):
    if delete:
        for i in range(100):
            (tmp_path / f"f{i:03d}.txt").write_text("x")
    before = artifacts.snapshot(tmp_path)
    for i in range(100):
        target = tmp_path / f"f{i:03d}.txt"
        if delete:
            target.unlink()
        else:
            target.write_text("x")
    digested = []
    original = artifacts._digest
    monkeypatch.setattr(artifacts, "_digest", lambda p: (digested.append(p), original(p))[1])
    changes = artifacts.command_files(tmp_path, before)
    assert len(changes["files"]) == 64
    assert changes["total_changes"] == 100 and changes["truncated"] is True
    assert changes["inventory_complete"] is True
    assert len(digested) == (0 if delete else 64)


def test_t3_shell_and_background_publication_keep_change_count(local, python_shell):
    _, _, _, state = local
    result = run(local, "from pathlib import Path; [Path(f'f{i:03d}.txt').write_text('x') for i in range(100)]", "hundred")
    assert len(result["workspace_files"]) == 64
    assert result["workspace_files_total"] == 100 and result["workspace_files_truncated"] is True
    row = state.read()["tasks"]["task"]
    assert row["workspace_file_receipts"]["hundred"]["result"]["workspace_files_total"] == 100
    listing = artifacts.listing(row)
    assert listing["additional_changes"] == 36


def test_t4_two_commands_reuse_after_and_patch_invalidates(local, python_shell, monkeypatch):
    tools, context, workspace, _ = local
    snapshots = []
    original = artifacts.snapshot
    def capture(root):
        snapshots.append(original(root))
        return snapshots[-1]
    monkeypatch.setattr(artifacts, "snapshot", capture)
    first = run(local, "from pathlib import Path; Path('a.txt').write_text('a')", "first")
    assert len(snapshots) == 2
    assert first["workspace_files"][0]["created"] is True
    run(local, "from pathlib import Path; Path('b.txt').write_text('b')", "second")
    assert len(snapshots) == 3
    execute(tools, context, "ryn_apply_patch", {"patch": "*** Begin Patch\n*** Add File: patched.txt\n+patch\n*** End Patch\n"}, "patch")
    third = run(local, "from pathlib import Path; Path('c.txt').write_text('c')", "third")
    assert len(snapshots) == 5
    assert {Path(f["path"]).name for f in third["workspace_files"]} == {"c.txt"}
    assert (workspace / "patched.txt").read_text() == "patch\n"


@pytest.mark.parametrize("invalidate", ["expiry", "receipt", "task"])
def test_t4_reuse_expires_and_respects_confirmed_receipts_and_task(local, python_shell, monkeypatch, invalidate):
    tools, context, workspace, state = local
    snapshots = []
    original = artifacts.snapshot
    monkeypatch.setattr(artifacts, "snapshot", lambda root: (snapshots.append(1), original(root))[1])
    run(local, "pass", "first")
    assert len(snapshots) == 2
    if invalidate == "expiry":
        now = time.monotonic()
        monkeypatch.setattr(local_tools, "time", SimpleNamespace(monotonic=lambda: now + 3))
    elif invalidate == "receipt":
        state.transaction(lambda data: data["tasks"]["task"].setdefault("receipts", []).append(
            _receipt("remote-write", "ryn_file_write", {"path": "remote.txt"}, {"written": True, "path": str(workspace / "remote.txt")})))
    else:
        other = {**context, "task_id": "other"}
        tools.perform("ryn_shell_execute", {"command": "pass"}, other, lambda: False)
        assert len(snapshots) == 4
        return
    run(local, "pass", "next")
    assert len(snapshots) == 4


def test_listing_inventory_notices_are_scoped_and_command_poll_not_double_counted(tmp_path):
    result = {"process_id": "process_one", "workspace_files": [], "workspace_files_complete": False,
              "workspace_files_total": 100, "workspace_files_truncated": True}
    first = _receipt("shell", "ryn_shell_execute", {}, result, goal=1)
    poll = _receipt("poll", "ryn_process_read", {}, result, goal=2)
    row = {"id": "inventory-turns", "local_execution": {"workspace_root": str(tmp_path)},
           "messages": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}],
           "receipts": [first, poll], "workspace_file_receipts": {"shell": first}}
    listing = artifacts.listing(row)
    assert listing["incomplete_inventory"] is True
    assert listing["additional_changes"] == 100
    assert listing["inventory_by_turn"]["0"] == {"incomplete_inventory": True, "additional_changes": 100}
    assert "1" not in listing["inventory_by_turn"]
    legacy = artifacts.listing({**row, "id": "legacy", "receipts": [_receipt("old", "ryn_shell_execute", {}, {"workspace_files": []})],
                                "workspace_file_receipts": {}})
    assert not legacy["incomplete_inventory"] and legacy["additional_changes"] == 0
