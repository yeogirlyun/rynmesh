"""Regression coverage for onefile workers and the private CLI environment."""
import pytest

from rynmesh.llm_package import process_guard


def test_private_storage_counts_files_but_not_external_symlink_targets(tmp_path):
    from rynmesh.llm_package.safety import private_storage_bytes
    workspace = tmp_path / "private"
    workspace.mkdir()
    (workspace / "payload").write_bytes(b"a" * 1234)
    external = tmp_path / "installed-cli"
    external.mkdir()
    binary = external / "codex"
    binary.write_bytes(b"x" * 1024 * 1024)
    file_link = workspace / "apply_patch"
    dir_link = workspace / "outside"
    try:
        file_link.symlink_to(binary)
        dir_link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    assert private_storage_bytes(workspace) == 1234 + file_link.lstat().st_size + dir_link.lstat().st_size
    (workspace / "nested").mkdir()
    (workspace / "nested" / "data").write_bytes(b"x" * 5678)
    assert private_storage_bytes(workspace) == 6912 + file_link.lstat().st_size + dir_link.lstat().st_size


def test_frozen_worker_inherits_only_bootloader_context(monkeypatch):
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/extracted-runtime")
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/Applications/Ryn.app/Contents/MacOS/rynmesh-peer")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("OWNER_SECRET", "must-not-be-copied")
    private = {"HOME": "/tmp/private-session", "TMPDIR": "/tmp/private-session"}
    result = process_guard._frozen_worker_env(private)
    assert result["_PYI_APPLICATION_HOME_DIR"] == "/tmp/extracted-runtime"
    assert result["_PYI_PARENT_PROCESS_LEVEL"] == "1"
    assert result["HOME"] == result["TMPDIR"] == "/tmp/private-session"
    assert "OWNER_SECRET" not in result
    assert "_PYI_APPLICATION_HOME_DIR" not in private
