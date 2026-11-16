"""Tests for S13-I69 (T5): Claude Code CLI borrowed .credentials.json writeback, serialization, and cleanup."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import pytest

from rynmesh.llm_package import cli_adapter
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.private_permissions import current_user_sid, read_windows_dacl


class _FakeClaudeProcess:
    def __init__(self, *, returncode: int = 0):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _assert_private_acl(path: Path) -> None:
    if os.name == "nt":
        dacl = read_windows_dacl(path)
        assert dacl.startswith("D:P"), dacl
        entries = re.findall(r"\(([^)]+)\)", dacl)
        assert {e.split(";")[-1] for e in entries} == {"SY", "BA", current_user_sid()}
    else:
        assert (path.stat().st_mode & 0o777) == 0o600


def test_t5_claude_credentials_writeback_conflict_serialization_and_replica_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    claude_dir = tmp_path / "user_claude"
    claude_dir.mkdir()
    cred_file = claude_dir / ".credentials.json"
    cred_file.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "c_acc_1", "refreshToken": "c_ref_1"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda self: "fake-claude-bin")

    adapter = CLIAgentAdapter("claude_cli")

    # 1. Fake Claude CLI refreshes temporary .credentials.json -> written back to user's .credentials.json with ACL
    def _spawn_refresh(args, *, cwd, env, timeout):
        rep = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        assert rep.is_file()
        rep.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "c_acc_2", "refreshToken": "c_ref_2"}}),
            encoding="utf-8",
        )
        return _FakeClaudeProcess(returncode=0)

    monkeypatch.setattr(cli_adapter, "spawn", _spawn_refresh)
    monkeypatch.setattr(
        cli_adapter,
        "communicate_bounded",
        lambda proc, data, *, timeout, root: json.dumps(
            {"result": "claude ok", "is_error": False, "usage": {"input_tokens": 5, "output_tokens": 3}}
        ).encode("utf-8"),
    )

    res1 = adapter.infer(prompt="hello", max_tokens=32, task_id="t5_1", timeout_s=5.0)
    assert res1["text"] == "claude ok"
    assert json.loads(cred_file.read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"] == "c_ref_2"
    _assert_private_acl(cred_file)

    # 2. Source .credentials.json modified externally during Claude run -> NOT overwritten
    def _spawn_conflict(args, *, cwd, env, timeout):
        rep = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        # User's own Claude Code updates source externally
        cred_file.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "ext_acc", "refreshToken": "ext_ref"}}),
            encoding="utf-8",
        )
        rep.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "stale_acc", "refreshToken": "stale_ref"}}),
            encoding="utf-8",
        )
        return _FakeClaudeProcess(returncode=0)

    monkeypatch.setattr(cli_adapter, "spawn", _spawn_conflict)
    res2 = adapter.infer(prompt="hello2", max_tokens=32, task_id="t5_2", timeout_s=5.0)
    assert json.loads(cred_file.read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"] == "ext_ref"
    assert (res2.get("diagnostics") or {}).get("auth_writeback") == "skipped_external_change"

    # 3. Concurrent Claude sessions serialize on the same .credentials.json, and timeout raises cli_auth_busy
    first_in = threading.Event()
    let_first_finish = threading.Event()
    second_seen: list[str] = []

    def _spawn_serial(args, *, cwd, env, timeout):
        rep = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        cur_tok = json.loads(rep.read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"]
        if not first_in.is_set():
            first_in.set()
            let_first_finish.wait(timeout=5.0)
            rep.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "s1_acc", "refreshToken": "s1_ref"}}),
                encoding="utf-8",
            )
        else:
            second_seen.append(cur_tok)
            rep.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "s2_acc", "refreshToken": "s2_ref"}}),
                encoding="utf-8",
            )
        return _FakeClaudeProcess(returncode=0)

    monkeypatch.setattr(cli_adapter, "spawn", _spawn_serial)
    errs: list[BaseException] = []
    th1 = threading.Thread(
        target=lambda: adapter.infer(prompt="p1", max_tokens=32, task_id="t5_s1", timeout_s=5.0)
    )
    th2 = threading.Thread(
        target=lambda: adapter.infer(prompt="p2", max_tokens=32, task_id="t5_s2", timeout_s=5.0)
    )
    th1.start()
    first_in.wait(timeout=5.0)
    th2.start()
    time.sleep(0.1)
    assert second_seen == []

    # A third call with short timeout_s=0.08 while first holds the lock must fail with cli_auth_busy
    with pytest.raises(AdapterError, match="cli_auth_busy"):
        adapter.infer(prompt="p3", max_tokens=32, task_id="t5_s3", timeout_s=0.08)

    let_first_finish.set()
    th1.join(timeout=5.0)
    th2.join(timeout=5.0)
    assert not errs
    assert second_seen == ["s1_ref"]
    assert json.loads(cred_file.read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"] == "s2_ref"

    # 4. Directory deletion failure still deletes temporary .credentials.json replica
    captured_replicas: list[Path] = []
    captured_roots: list[Path] = []
    orig_td_rmtree = tempfile.TemporaryDirectory._rmtree
    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        "_rmtree",
        classmethod(lambda cls, name, ignore_errors=False, repeated=False: None),
    )

    def _spawn_capture(args, *, cwd, env, timeout):
        captured_roots.append(Path(cwd))
        captured_replicas.append(Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json")
        return _FakeClaudeProcess(returncode=0)

    monkeypatch.setattr(cli_adapter, "spawn", _spawn_capture)
    try:
        adapter.infer(prompt="cleanup", max_tokens=32, task_id="t5_clean", timeout_s=5.0)
        assert len(captured_replicas) == 1
        assert captured_roots[0].is_dir()
        assert not captured_replicas[0].exists()
    finally:
        monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", orig_td_rmtree)
        import shutil

        for r in captured_roots:
            shutil.rmtree(r, ignore_errors=True)
