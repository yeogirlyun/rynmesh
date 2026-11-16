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
