"""The extraction helper: classification, limits, and the code contract."""

from __future__ import annotations

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
