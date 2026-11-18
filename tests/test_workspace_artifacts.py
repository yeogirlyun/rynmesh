"""Tests for S05-I13: helper role classification, history LRU cache, unbacked mentions, and _digest LRU cache."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time

import pytest

from rynmesh.agent_runtime import workspace_artifacts


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _receipt(op: str, tool: str, arguments: dict, result: dict, *, goal: int = 1, created: bool = True) -> dict:
    return {
        "id": "receipt_" + op,
        "operation_id": op,
        "tool": tool,
        "arguments": arguments,
        "effect": "write",
        "goal_revision": goal,
        "scope_revision": 0,
        "context_epoch": 0,
        "status": "confirmed",
        "result": result,
        "preview": {"before_sha256": None if created else "prev"},
        "started_at": f"2026-11-16T00:00:0{goal}+00:00",
        "confirmed_at": f"2026-11-16T00:01:0{goal}+00:00",
    }


def test_t1_executed_py_is_helper_and_generated_json_is_supporting(tmp_path: Path) -> None:
    """T1: A task-created .py script executed by a task shell command gets role='helper'; generated result.json stays 'supporting'."""
    script = tmp_path / "compute_metrics.py"
    unused_script = tmp_path / "unused_helper.py"
    result_json = tmp_path / "result.json"
    report_md = tmp_path / "summary.md"

    script.write_text("import json; open('result.json','w').write('{}')", encoding="utf-8")
    unused_script.write_text("print('unused')", encoding="utf-8")
    result_json.write_text('{"total": 42}', encoding="utf-8")
    report_md.write_text("# Summary\nTotal: 42\n", encoding="utf-8")

    row = {
        "id": "task-t1",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "compute_metrics.py"},
                {"path": str(script), "sha256": _sha(script.read_bytes()), "bytes": script.stat().st_size, "written": True},
            ),
            _receipt(
                "op2",
                "ryn_file_write",
                {"path": "unused_helper.py"},
                {"path": str(unused_script), "sha256": _sha(unused_script.read_bytes()), "bytes": unused_script.stat().st_size, "written": True},
            ),
            _receipt(
                "op3",
                "ryn_shell_execute",
                {"command": "python compute_metrics.py"},
                {
                    "running": False,
                    "exit_code": 0,
                    "workspace_files": [
                        {"path": str(result_json), "sha256": _sha(result_json.read_bytes()), "bytes": result_json.stat().st_size, "created": True}
                    ],
                },
            ),
            _receipt(
                "op4",
                "ryn_file_write",
                {"path": "summary.md"},
                {"path": str(report_md), "sha256": _sha(report_md.read_bytes()), "bytes": report_md.stat().st_size, "written": True},
            ),
        ],
        "result": "Generated summary.md.",
        "messages": [],
    }

    listed = {item["name"]: item for item in workspace_artifacts.listing(row)["files"]}
    assert listed["summary.md"]["role"] == "deliverable"
    assert listed["compute_metrics.py"]["role"] == "helper"
    assert listed["result.json"]["role"] == "supporting"
    assert listed["unused_helper.py"]["role"] == "supporting"


def test_t3_repeated_listing_on_same_row_replays_history_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T3: Consecutive listing() calls on an unchanged row replay history once, and read() reuses location state without a second history() call."""
    out = tmp_path / "report.md"
    out.write_text("hello", encoding="utf-8")
    row = {
        "id": "task-t3",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "report.md"},
                {"path": str(out), "sha256": _sha(out.read_bytes()), "bytes": out.stat().st_size, "written": True},
            )
        ],
        "result": "Saved report.md",
        "messages": [{"role": "user", "content": "Write report"}],
    }

    touch_calls = 0
    real_touch = workspace_artifacts._touch

    def counting_touch(*args, **kwargs):
        nonlocal touch_calls
        touch_calls += 1
        return real_touch(*args, **kwargs)

    monkeypatch.setattr(workspace_artifacts, "_touch", counting_touch)

    first = workspace_artifacts.listing(row)
    second = workspace_artifacts.listing(row)
    assert first == second
    assert touch_calls == 1

    # Also verify read() calls history() only once (not twice via location() + history())
    history_calls = 0
    real_history = workspace_artifacts.history

    def counting_history(r):
        nonlocal history_calls
        history_calls += 1
        return real_history(r)

    monkeypatch.setattr(workspace_artifacts, "history", counting_history)
    file_id = first["files"][0]["id"]
    payload = workspace_artifacts.read(row, file_id)
    assert payload["bytes"] == b"hello"
    assert history_calls == 1


def test_t4_history_cache_invalidates_when_new_receipt_arrives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T4: Adding a receipt to receipts or workspace_file_receipts invalidates the history cache."""
    file_a = tmp_path / "a.md"
    file_b = tmp_path / "b.md"
    file_c = tmp_path / "c.json"
    file_a.write_text("A", encoding="utf-8")
    file_b.write_text("B", encoding="utf-8")
    file_c.write_text('{"c": 1}', encoding="utf-8")

    touch_calls = 0
    real_touch = workspace_artifacts._touch

    def counting_touch(*args, **kwargs):
        nonlocal touch_calls
        touch_calls += 1
        return real_touch(*args, **kwargs)

    monkeypatch.setattr(workspace_artifacts, "_touch", counting_touch)

    row = {
        "id": "task-t4",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op_a",
                "ryn_file_write",
                {"path": "a.md"},
                {"path": str(file_a), "sha256": _sha(file_a.read_bytes()), "bytes": 1, "written": True},
            )
        ],
        "workspace_file_receipts": {},
        "file_artifact_revision": 0,
        "result": "",
        "messages": [],
    }

    assert [f["name"] for f in workspace_artifacts.listing(row)["files"]] == ["a.md"]
    assert [f["name"] for f in workspace_artifacts.listing(row)["files"]] == ["a.md"]
    assert touch_calls == 1

    # 1. Append to row["receipts"] -> cache invalidates and replays (2 files)
    row["receipts"].append(
        _receipt(
            "op_b",
            "ryn_file_write",
            {"path": "b.md"},
            {"path": str(file_b), "sha256": _sha(file_b.read_bytes()), "bytes": 1, "written": True},
            goal=2,
        )
    )
    assert {f["name"] for f in workspace_artifacts.listing(row)["files"]} == {"a.md", "b.md"}
    assert touch_calls == 3

    # 2. Insert into workspace_file_receipts + increment file_artifact_revision -> cache invalidates and replays (3 files)
    row["workspace_file_receipts"]["op_c"] = {
        "operation_id": "op_c",
        "tool": "ryn_shell_execute",
        "status": "confirmed",
        "goal_revision": 2,
        "confirmed_at": "2026-11-16T00:05:00+00:00",
        "result": {
            "workspace_files": [
                {"path": str(file_c), "sha256": _sha(file_c.read_bytes()), "bytes": file_c.stat().st_size, "created": True}
            ]
        },
    }
    row["file_artifact_revision"] = 1
    assert {f["name"] for f in workspace_artifacts.listing(row)["files"]} == {"a.md", "b.md", "c.json"}
    assert touch_calls == 6


def test_t5_unbacked_mentions_includes_ghost_svg_and_excludes_backed_pelican_svg(tmp_path: Path) -> None:
    """T5: Answer mentioning ghost.svg without receipt reports it in unbacked_mentions while excluding backed pelican.svg."""
    pelican = tmp_path / "pelican.svg"
    pelican.write_text("<svg/>", encoding="utf-8")
    row = {
        "id": "task-t5",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "pelican.svg"},
                {"path": str(pelican), "sha256": _sha(pelican.read_bytes()), "bytes": pelican.stat().st_size, "written": True},
            )
        ],
        "result": "Saved pelican.svg and Pelican.svg; compare against ghost.svg and GHOST.svg.",
        "messages": [],
    }
    res = workspace_artifacts.listing(row)
    assert [f["name"] for f in res["files"]] == ["pelican.svg"]
    assert res["unbacked_mentions"] == ["ghost.svg"]


def test_t7_unchanged_aged_file_reads_bytes_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T7: An unchanged file older than 2s is opened only once across repeated listing() and command_files() calls."""
    before = workspace_artifacts.snapshot(tmp_path)
    target = tmp_path / "stable_report.md"
    raw = b"# Stable report\n" * 50
    target.write_bytes(raw)
    old_time = time.time() - 10.0
    os.utime(target, (old_time, old_time))

    open_count = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_count
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb" and self.resolve() == target.resolve():
            open_count += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    cmd_files = workspace_artifacts.command_files(tmp_path, before)["files"]
    assert len(cmd_files) == 1 and cmd_files[0]["sha256"] == _sha(raw)
    assert open_count == 1

    row = {
        "id": "task-t7",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "stable_report.md"},
                {"path": str(target), "sha256": _sha(raw), "bytes": len(raw), "written": True},
            )
        ],
        "result": "Saved stable_report.md",
        "messages": [],
    }
    for _ in range(3):
        listed = workspace_artifacts.listing(row)
        assert listed["files"][0]["sha256"] == _sha(raw)
        assert listed["files"][0]["status"] == "current"

    assert open_count == 1


def test_t8_modified_file_with_changed_mtime_or_size_rehashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T8: Modifying file contents (changing size/mtime) misses _digest cache and re-hashes."""
    target = tmp_path / "mutable.txt"
    v1 = b"version-one"
    v2 = b"version-two-updated"
    target.write_bytes(v1)
    t1 = time.time() - 20.0
    os.utime(target, (t1, t1))

    open_count = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_count
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb" and self.resolve() == target.resolve():
            open_count += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    row = {
        "id": "task-t8",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "mutable.txt"},
                {"path": str(target), "sha256": _sha(v1), "bytes": len(v1), "written": True},
            )
        ],
        "result": "Saved mutable.txt",
        "messages": [],
    }
    first = workspace_artifacts.listing(row)["files"][0]
    assert first["sha256"] == _sha(v1) and first["status"] == "current"
    workspace_artifacts.listing(row)
    assert open_count == 1

    # Rewrite file with different content & mtime (still >2s old)
    target.write_bytes(v2)
    t2 = time.time() - 10.0
    os.utime(target, (t2, t2))

    second = workspace_artifacts.listing(row)["files"][0]
    assert second["sha256"] == _sha(v2)
    assert second["status"] == "changed_outside_task"
    workspace_artifacts.listing(row)
    assert open_count == 2


def test_t9_recently_written_file_within_two_seconds_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T9: A file whose mtime is within 2 seconds of current time is not cached in _digest."""
    target = tmp_path / "just_written.txt"
    raw = b"freshly written content"
    target.write_bytes(raw)
    now = time.time()
    os.utime(target, (now, now))

    open_count = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_count
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb" and self.resolve() == target.resolve():
            open_count += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    row = {
        "id": "task-t9",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "just_written.txt"},
                {"path": str(target), "sha256": _sha(raw), "bytes": len(raw), "written": True},
            )
        ],
        "result": "Saved just_written.txt",
        "messages": [],
    }
    workspace_artifacts.listing(row)
    workspace_artifacts.listing(row)
    assert open_count == 2
    stat = target.stat()
    assert (str(target.resolve()), stat.st_size, stat.st_mtime_ns) not in workspace_artifacts._DIGEST_CACHE


def test_t10_mentioned_executed_script_is_visible_while_unmentioned_stays_helper(tmp_path: Path) -> None:
    """Rework test: when the answer names script.py, it is classified as deliverable/supporting (visible in results); when unnamed, it stays helper."""
    script = tmp_path / "script.py"
    report = tmp_path / "report.md"
    script.write_text("print('ok')\n", encoding="utf-8")
    report.write_text("# Report\n", encoding="utf-8")

    # 1. User asked for script.py (only file, executed to verify, named in answer) -> deliverable (not helper)
    only_script_row = {
        "id": "task-t10-only-script",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "script.py"},
                {"path": str(script), "sha256": _sha(script.read_bytes()), "bytes": script.stat().st_size, "written": True},
            ),
            _receipt(
                "op2",
                "ryn_shell_execute",
                {"command": "python script.py"},
                {"running": False, "exit_code": 0, "workspace_files": []},
            ),
        ],
        "result": "已编写并验证 script.py。",
        "messages": [],
    }
    listed_only = {item["name"]: item for item in workspace_artifacts.listing(only_script_row)["files"]}
    assert listed_only["script.py"]["mentioned_in_answer"] is True
    assert listed_only["script.py"]["role"] == "deliverable"

    # 2. Both report.md and script.py named in answer -> script.py is supporting (not helper)
    both_named_row = {
        "id": "task-t10-both-named",
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            _receipt(
                "op1",
                "ryn_file_write",
                {"path": "script.py"},
                {"path": str(script), "sha256": _sha(script.read_bytes()), "bytes": script.stat().st_size, "written": True},
            ),
            _receipt(
                "op2",
                "ryn_shell_execute",
                {"command": "python script.py"},
                {"running": False, "exit_code": 0, "workspace_files": []},
            ),
            _receipt(
                "op3",
                "ryn_file_write",
                {"path": "report.md"},
                {"path": str(report), "sha256": _sha(report.read_bytes()), "bytes": report.stat().st_size, "written": True},
            ),
        ],
        "result": "已生成 report.md，处理脚本见 script.py。",
        "messages": [],
    }
    listed_both = {item["name"]: item for item in workspace_artifacts.listing(both_named_row)["files"]}
    assert listed_both["report.md"]["role"] == "deliverable"
    assert listed_both["script.py"]["mentioned_in_answer"] is True
    assert listed_both["script.py"]["role"] == "supporting"

    # 3. Answer does NOT name script.py -> script.py stays helper
    unmentioned_row = {
        **both_named_row,
        "id": "task-t10-unmentioned",
        "result": "已生成 report.md。",
    }
    listed_unmentioned = {item["name"]: item for item in workspace_artifacts.listing(unmentioned_row)["files"]}
    assert listed_unmentioned["script.py"]["mentioned_in_answer"] is False
    assert listed_unmentioned["script.py"]["role"] == "helper"
