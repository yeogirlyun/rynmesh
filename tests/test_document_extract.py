"""The extraction helper: classification, limits, and the code contract."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rynmesh.services import document_extract as de

CHILD = [sys.executable, "-m", "rynmesh.services.document_extract_child"]

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="resource limits are POSIX-only"
)


def _run_child(path: Path, timeout: float = 30.0) -> dict:
    done = subprocess.run(
        CHILD,
        input=f"{path}\n".encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return json.loads(done.stdout.decode("utf-8"))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("notes.txt", "text"),
        ("notes.TXT", "text"),
        ("readme.md", "markdown"),
        ("readme.markdown", "markdown"),
        ("paper.pdf", "pdf"),
        ("archive.zip", "unknown"),
        ("noextension", "unknown"),
    ],
)
def test_classify_maps_suffix_to_kind(tmp_path: Path, name: str, expected: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"x")
    assert de.classify(path) == expected


def test_every_failure_code_is_path_free_and_prefixed() -> None:
    codes = de.FAILURE_CODES
    assert codes, "the closed failure set must not be empty"
    for code in codes:
        assert code.startswith("failed:")
        assert "/" not in code and "\\" not in code
        assert code == code.strip()


def test_limits_have_sane_defaults() -> None:
    assert de.MAX_INPUT_BYTES > 0
    assert de.MAX_OUTPUT_CHARS > 0
    assert de.TIMEOUT_S > 0
    assert de.MEMORY_BYTES > de.MAX_OUTPUT_CHARS


def test_env_override_well_formed_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """An integer override is honoured if well-formed."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", "1234")
    try:
        importlib.reload(de)
        assert de.MAX_INPUT_BYTES == 1234
    finally:
        importlib.reload(de)


def test_env_override_well_formed_float(monkeypatch: pytest.MonkeyPatch) -> None:
    """A float override is honoured if well-formed."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_TIMEOUT_S", "42.5")
    try:
        importlib.reload(de)
        assert de.TIMEOUT_S == 42.5
    finally:
        importlib.reload(de)


def test_env_override_malformed_falls_back_to_default_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed integer override falls back to the default."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", "not_a_number")
    try:
        importlib.reload(de)
        assert de.MAX_INPUT_BYTES == 32 * 1024 * 1024
    finally:
        importlib.reload(de)


def test_env_override_malformed_falls_back_to_default_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed float override falls back to the default."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_TIMEOUT_S", "not_a_float")
    try:
        importlib.reload(de)
        assert de.TIMEOUT_S == 20.0
    finally:
        importlib.reload(de)


def test_env_absent_yields_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent environment variable yields the default."""
    monkeypatch.delenv("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", raising=False)
    try:
        importlib.reload(de)
        assert de.MAX_OUTPUT_CHARS == 1024 * 1024
    finally:
        importlib.reload(de)


def test_child_extracts_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello document\n", encoding="utf-8")
    result = _run_child(path)
    assert result["status"] == "parsed"
    assert result["kind"] == "text"
    assert result["text"] == "hello document\n"
    assert result["chars"] == len(result["text"])


def test_child_truncates_beyond_the_output_cap(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "big.txt"
    path.write_text("a" * 5000, encoding="utf-8")
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", "1000")
    result = _run_child(path)
    assert result["status"] == "truncated"
    assert result["chars"] == 1000


def test_child_reports_unsupported_for_unknown_kinds(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04rest")
    result = _run_child(path)
    assert result["status"] == "unsupported"
    assert result["kind"] == "unknown"
    assert result["text"] == ""


def test_child_reports_not_text_for_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "broken.txt"
    path.write_bytes(b"\xff\xfe\x00binary")
    result = _run_child(path)
    assert result["status"] == "failed:not_text"


def test_child_reports_unreadable_for_a_missing_file(tmp_path: Path) -> None:
    result = _run_child(tmp_path / "absent.txt")
    assert result["status"] == "failed:unreadable"


@posix_only
def test_child_applies_an_address_space_limit(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resource,sys;"
            "sys.path.insert(0,'.');"
            "from rynmesh.services import document_extract_child as c;"
            "c.apply_limits();"
            "print(resource.getrlimit(resource.RLIMIT_AS)[0])",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert 0 < int(probe.stdout.strip())
