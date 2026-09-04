"""The extraction helper: classification, limits, and the code contract."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from rynmesh.services import document_extract as de


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
