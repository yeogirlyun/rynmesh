"""Tests for S13-I69: Codex CLI borrowed auth.json writeback, serialization, and cleanup."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import pytest

from rynmesh.llm_package import codex_session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.private_permissions import current_user_sid, read_windows_dacl


def _assert_private_acl(path: Path) -> None:
    if os.name == "nt":
        dacl = read_windows_dacl(path)
        assert dacl.startswith("D:P"), dacl
        entries = re.findall(r"\(([^)]+)\)", dacl)
        assert {e.split(";")[-1] for e in entries} == {"SY", "BA", current_user_sid()}
    else:
        assert (path.stat().st_mode & 0o777) == 0o600


def test_t1_codex_refreshed_replica_updates_source_auth_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    codex_dir = tmp_path / "user_codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(
        json.dumps({"tokens": {"access_token": "acc_v1", "refresh_token": "ref_v1"}}),
        encoding="utf-8",
    )
    if os.name != "nt":
        auth_file.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))

    with codex_session.private_workspace() as (root, env):
        replica = Path(env["CODEX_HOME"]) / "auth.json"
        assert replica.is_file()
        assert json.loads(replica.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == "ref_v1"
        # Simulate Codex CLI rotating the one-time refresh token in its temporary CODEX_HOME
        replica.write_text(
            json.dumps({"tokens": {"access_token": "acc_v2", "refresh_token": "ref_v2"}}),
            encoding="utf-8",
        )

    updated = json.loads(auth_file.read_text(encoding="utf-8"))
    assert updated == {"tokens": {"access_token": "acc_v2", "refresh_token": "ref_v2"}}
    _assert_private_acl(auth_file)

    # Corrupt non-JSON replica must never clobber the valid source auth.json
    with codex_session.private_workspace() as (_root2, env2):
        replica2 = Path(env2["CODEX_HOME"]) / "auth.json"
        replica2.write_text("{corrupt-half-written", encoding="utf-8")

    assert json.loads(auth_file.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == "ref_v2"


def test_t2_codex_source_modified_externally_during_session_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    codex_dir = tmp_path / "user_codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"refresh_token": "initial_tok"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))

    diag: dict = {}
    with caplog.at_level(logging.WARNING):
        with codex_session.private_workspace(diagnostics_out=diag) as (_root, env):
            replica = Path(env["CODEX_HOME"]) / "auth.json"
            # User's own Codex CLI refreshes original auth.json externally
            auth_file.write_text(json.dumps({"refresh_token": "user_cli_newer_tok"}), encoding="utf-8")
            # Replica also changed during session
            replica.write_text(json.dumps({"refresh_token": "replica_conflicting_tok"}), encoding="utf-8")

    assert json.loads(auth_file.read_text(encoding="utf-8")) == {"refresh_token": "user_cli_newer_tok"}
    assert diag.get("auth_writeback") == "skipped_external_change"
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_t3_two_concurrent_codex_sessions_serialize_and_timeout_raises_cli_auth_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_dir = tmp_path / "user_codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"refresh_token": "tok_0"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))

    events: list[str] = []
    s1_entered = threading.Event()
    allow_s1_exit = threading.Event()
    s2_observed: dict[str, str] = {}
    errors: list[BaseException] = []

    def _session_one():
        try:
            with codex_session.private_workspace(auth_timeout=5.0) as (_r1, env1):
                events.append("s1_enter")
                s1_entered.set()
                allow_s1_exit.wait(timeout=5.0)
                rep1 = Path(env1["CODEX_HOME"]) / "auth.json"
                rep1.write_text(json.dumps({"refresh_token": "tok_1"}), encoding="utf-8")
                events.append("s1_before_exit")
        except BaseException as exc:
            errors.append(exc)

    def _session_two():
        s1_entered.wait(timeout=5.0)
        try:
            with codex_session.private_workspace(auth_timeout=5.0) as (_r2, env2):
                events.append("s2_enter")
                rep2 = Path(env2["CODEX_HOME"]) / "auth.json"
                s2_observed["token"] = json.loads(rep2.read_text(encoding="utf-8"))["refresh_token"]
                rep2.write_text(json.dumps({"refresh_token": "tok_2"}), encoding="utf-8")
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_session_one)
    t2 = threading.Thread(target=_session_two)
    t1.start()
    t2.start()
    s1_entered.wait(timeout=5.0)
    time.sleep(0.12)
    # While Session 1 is active, Session 2 must still be queued (not entered yet)
    assert events == ["s1_enter"]
    allow_s1_exit.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not errors, errors
    assert events == ["s1_enter", "s1_before_exit", "s2_enter"]
    # Session 2 must have received the refreshed token written back by Session 1!
    assert s2_observed.get("token") == "tok_1"
    assert json.loads(auth_file.read_text(encoding="utf-8")) == {"refresh_token": "tok_2"}

    # Exceeding auth_timeout while another session holds the replica must raise cli_auth_busy
    hold_entered = threading.Event()
    release_hold = threading.Event()

    def _holder():
        with codex_session.private_workspace(auth_timeout=5.0):
            hold_entered.set()
            release_hold.wait(timeout=5.0)

    holder_thread = threading.Thread(target=_holder)
    holder_thread.start()
    hold_entered.wait(timeout=5.0)
    try:
        assert (codex_dir / "auth.json.ryn.lock").is_file()
        with pytest.raises(AdapterError, match="cli_auth_busy"):
            with codex_session.private_workspace(auth_timeout=0.1):
                raise AssertionError("Should not acquire busy auth")
        # Cross-process verification: a separate Python process also receives cli_auth_busy on auth.json.ryn.lock
        import subprocess
        import sys

        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from rynmesh.llm_package.codex_session import private_workspace; "
                "from rynmesh.llm_package.adapters import AdapterError\n"
                "try:\n"
                "    with private_workspace(auth_timeout=0.15): pass\n"
                "except AdapterError as e:\n"
                "    print(str(e)); sys.exit(42)\n",
            ],
            env={**os.environ, "CODEX_HOME": str(codex_dir)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert child.returncode == 42, f"Expected cross-process cli_auth_busy, got rc={child.returncode} out={child.stdout} err={child.stderr}"
        assert "cli_auth_busy" in child.stdout
    finally:
        release_hold.set()
        holder_thread.join(timeout=5.0)


def test_t4_replica_auth_deleted_even_when_temp_directory_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_dir = tmp_path / "user_codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"refresh_token": "secret_tok"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))

    # Simulate TemporaryDirectory._rmtree failing to delete the session directory (e.g. locked SQLite handle on Windows)
    orig_td_rmtree = tempfile.TemporaryDirectory._rmtree
    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        "_rmtree",
        classmethod(lambda cls, name, ignore_errors=False, repeated=False: None),
    )

    # Also simulate transient OSError on first unlink attempt of replica auth.json
    orig_unlink = Path.unlink
    unlink_attempts = {"count": 0}

    def _flaky_unlink(self: Path, missing_ok: bool = False):
        if self.name == "auth.json" and unlink_attempts["count"] < 2:
            unlink_attempts["count"] += 1
            raise PermissionError("simulated transient antivirus lock")
        return orig_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    captured_root: Path | None = None
    captured_replica: Path | None = None
    try:
        with codex_session.private_workspace() as (root, env):
            captured_root = root
            captured_replica = Path(env["CODEX_HOME"]) / "auth.json"
            assert captured_replica.is_file()
        assert captured_root is not None and captured_root.is_dir()
        assert captured_replica is not None and not captured_replica.exists()
        assert unlink_attempts["count"] == 2
    finally:
        monkeypatch.setattr(Path, "unlink", orig_unlink)
        monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", orig_td_rmtree)
        if captured_root is not None and captured_root.exists():
            import shutil

            shutil.rmtree(captured_root, ignore_errors=True)


class _InMemoryPipe:
    def __init__(self):
        self._lines: list[bytes | None] = []
        self._cond = threading.Condition()
        self.closed = False

    def write(self, data: bytes) -> int:
        with self._cond:
            if self.closed:
                raise OSError("pipe closed")
            for chunk in data.splitlines(keepends=True):
                self._lines.append(chunk)
            self._cond.notify_all()
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self, limit: int = -1) -> bytes:
        with self._cond:
            while not self._lines and not self.closed:
                self._cond.wait(timeout=0.5)
            if not self._lines:
                return b""
            line = self._lines.pop(0)
            return b"" if line is None else line

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._lines.append(None)
            self._cond.notify_all()


class _FakeAppServerProcess:
    """In-memory stdio Codex app-server that inspects and rotates CODEX_HOME/auth.json on each turn."""

    def __init__(self, env: dict[str, str], on_turn_auth):
        self.env = dict(env)
        self._on_turn_auth = on_turn_auth
        self.stdin = _InMemoryPipe()
        self.stdout = _InMemoryPipe()
        self.pid = os.getpid()
        self._rc: int | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _emit(self, obj: dict) -> None:
        self.stdout.write((json.dumps(obj) + "\n").encode("utf-8"))

    def _serve(self) -> None:
        turn_seq = 0
        while self._rc is None:
            raw = self.stdin.readline()
            if not raw:
                break
            msg = json.loads(raw.decode("utf-8"))
            req_id = msg.get("id")
            method = msg.get("method")
            if method == "initialize" and req_id is not None:
                self._emit({"id": req_id, "result": {"serverInfo": {"name": "fake-codex"}}})
            elif method == "thread/start" and req_id is not None:
                self._emit({"id": req_id, "result": {"thread": {"id": "th-pooled-1"}}})
            elif method == "turn/start" and req_id is not None:
                turn_seq += 1
                turn_id = f"turn-{turn_seq}"
                auth_replica = Path(self.env["CODEX_HOME"]) / "auth.json"
                reply_obj = self._on_turn_auth(turn_seq, auth_replica)
                self._emit({"id": req_id, "result": {"turn": {"id": turn_id}}})
                self._emit({
                    "method": "item/completed",
                    "params": {
                        "threadId": "th-pooled-1",
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "text": json.dumps(reply_obj)},
                    },
                })
                self._emit({
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "th-pooled-1",
                        "turnId": turn_id,
                        "tokenUsage": {"last": {"inputTokens": 8, "outputTokens": 4}},
                    },
                })
                self._emit({
                    "method": "turn/completed",
                    "params": {"threadId": "th-pooled-1", "turn": {"id": turn_id, "status": "completed"}},
                })
        self.stdout.close()

    def poll(self) -> int | None:
        return self._rc

    def kill(self) -> None:
        self._rc = -9
        self.stdin.close()
        self.stdout.close()

    def wait(self, timeout: float | None = None) -> int | None:
        self._rc = 0 if self._rc is None else self._rc
        self.stdin.close()
        self.stdout.close()
        return self._rc


def test_t6_pooled_session_releases_auth_lock_writes_back_and_deletes_replica_between_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from rynmesh.llm_package import codex_threads
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter

    codex_dir = tmp_path / "user_codex_pool"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"refresh_token": "ref_init"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.setenv("RYN_CODEX_SESSION_REUSE", "1")

    fake_exe = tmp_path / "fake_codex.exe"
    fake_exe.write_bytes(b"stub")
    monkeypatch.setattr(codex_session, "_check_protocol", lambda executable, mtime: None)
    monkeypatch.setattr(codex_session, "terminate", lambda proc: proc.kill())

    seen_turn_tokens: list[str] = []

    def _on_turn_auth(turn_seq: int, replica: Path) -> dict:
        assert replica.is_file(), f"Turn {turn_seq} expected staged auth.json replica"
        current = json.loads(replica.read_text(encoding="utf-8"))["refresh_token"]
        seen_turn_tokens.append(current)
        replica.write_text(json.dumps({"refresh_token": f"ref_turn_{turn_seq}"}), encoding="utf-8")
        return {"content": f"done-{turn_seq}", "tool_calls": []}

    monkeypatch.setattr(
        codex_session,
        "spawn",
        lambda args, *, cwd, env, timeout: _FakeAppServerProcess(env, _on_turn_auth),
    )

    adapter = CLIAgentAdapter("codex_cli", executable=str(fake_exe))
    monkeypatch.setattr(adapter, "_command_path", lambda: str(fake_exe))
    scope = codex_threads.scope("local-owner", codex_threads.window_key("conv-1"))
    tools_spec = [
        {
            "type": "function",
            "function": {
                "name": "noop_tool",
                "description": "No-op",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
    ]
    messages = [{"role": "user", "content": "turn one"}]

    try:
        res1 = adapter.chat(
            {"messages": list(messages), "tools": tools_spec, "max_tokens": 64},
            task_id="pool_t1",
            timeout_s=5.0,
            conversation_scope=scope,
        )
        assert res1["message"]["content"] == "done-1"

        pool = codex_threads.pool_for(adapter)
        assert len(pool) == 1, "Expected pooled session to remain idle in pool"
        idle_session = pool.idle[0]
        idle_replica = idle_session.rpc.root / "codex" / "auth.json"

        # 1. Replica must already be deleted while session sits idle in pool!
        assert not idle_replica.exists(), "Idle pooled session left auth.json replica in %TEMP%!"
        # 2. Refreshed token from Turn 1 must already be written back to source auth.json!
        assert json.loads(auth_file.read_text(encoding="utf-8")) == {"refresh_token": "ref_turn_1"}

        # 3. Another Codex request while pooled session is idle must acquire the lock immediately (no cli_auth_busy)!
        with codex_session.private_workspace(auth_timeout=0.15) as (_other_root, other_env):
            other_rep = Path(other_env["CODEX_HOME"]) / "auth.json"
            assert json.loads(other_rep.read_text(encoding="utf-8")) == {"refresh_token": "ref_turn_1"}
            other_rep.write_text(json.dumps({"refresh_token": "ref_other_session"}), encoding="utf-8")

        assert json.loads(auth_file.read_text(encoding="utf-8")) == {"refresh_token": "ref_other_session"}

        # 4. Resuming the pooled session for Turn 2 must re-borrow the latest auth.json, then write back & clean up again!
        messages.append(res1["message"])
        messages.append({"role": "user", "content": "turn two"})
        res2 = adapter.chat(
            {"messages": list(messages), "tools": tools_spec, "max_tokens": 64},
            task_id="pool_t2",
            timeout_s=5.0,
            conversation_scope=scope,
        )
        assert res2["message"]["content"] == "done-2"
        assert pool.stats["resumed"] == 1
        assert seen_turn_tokens == ["ref_init", "ref_other_session"]
        assert not idle_replica.exists()
        assert json.loads(auth_file.read_text(encoding="utf-8")) == {"refresh_token": "ref_turn_2"}
    finally:
        adapter.close_sessions()
