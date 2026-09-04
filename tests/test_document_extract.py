"""The extraction helper: classification, limits, and the code contract."""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rynmesh.services import document_extract as de
from rynmesh.services import document_extract_child as child

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


def test_extract_document_reads_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# heading\n\nbody\n", encoding="utf-8")
    result = de.extract_document(path)
    assert result["status"] == "parsed"
    assert result["kind"] == "markdown"
    assert "heading" in result["text"]


def test_extract_document_refuses_an_oversized_file_without_spawning(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("a" * 4096, encoding="utf-8")
    spawned: list[object] = []

    def _never(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("must not spawn for an oversized input")

    result = de.extract_document(path, max_input_bytes=10, spawn=_never)
    assert result["status"] == de.FAILED_TOO_LARGE
    assert result["text"] == ""
    assert spawned == []


def test_extract_document_times_out_and_kills_the_child(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _sleeper(*args, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, timeout_s=0.5, spawn=_sleeper)
    assert result["status"] == de.FAILED_TIMEOUT


def test_extract_document_reports_a_crashed_child(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _crasher(*args, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import os; os._exit(3)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, spawn=_crasher)
    assert result["status"] == de.FAILED_CRASHED


def test_extract_document_reports_internal_for_unparseable_output(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _garbage(*args, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "print('not json')"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, spawn=_garbage)
    assert result["status"] == de.FAILED_INTERNAL


@posix_only
def test_a_memory_bomb_is_contained(tmp_path: Path) -> None:
    """The child dies; the node keeps running and gets a code back."""
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _bomb(*args, **kwargs):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import resource;"
                "resource.setrlimit(resource.RLIMIT_AS,(64*1024*1024,)*2);"
                "b=bytearray();\n"
                "while True: b.extend(b'x'*(1024*1024))",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, timeout_s=30, spawn=_bomb)
    assert result["status"] in {de.FAILED_CRASHED, de.FAILED_MEMORY}


pdf_only = pytest.mark.skipif(
    not child.pdf_available(), reason="pypdf is not installed (the documents extra)"
)


def _minimal_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pdf_only
def test_a_valid_pdf_parses(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(_minimal_pdf())
    result = de.extract_document(path)
    assert result["status"] in {"parsed", "truncated"}
    assert result["kind"] == "pdf"


def test_a_corrupt_pdf_fails_without_taking_the_node_down(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nnot really a pdf at all\n%%EOF\n")
    result = de.extract_document(path)
    assert result["kind"] == "pdf"
    assert result["status"] in {"unsupported", de.FAILED_UNREADABLE, de.FAILED_CRASHED}
    # The node is still here to make the next call.
    assert de.extract_document(path)["kind"] == "pdf"


def test_pdf_is_unsupported_when_the_parser_is_absent(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_DISABLE_PDF", "1")
    result = de.extract_document(path)
    assert result["status"] == "unsupported"
    assert result["kind"] == "pdf"


def _pdf_with_extractable_text(paragraph_count: int = 200) -> bytes:
    """A one-page PDF whose content stream actually draws text.

    `_minimal_pdf` above builds a blank page, so its extracted text is always
    empty and the truncation branch in `document_extract_child.main` is never
    exercised. This builds a minimal content stream by hand — one `Tj` on a
    standard Helvetica font, no embedding required — so pypdf's extractor
    returns the literal string back out, long enough that a small
    `max_output_chars` cap truncates it.
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    text = "Lorem ipsum dolor sit amet consectetur " * paragraph_count
    content = f"BT /F1 12 Tf 10 750 Td ({text}) Tj ET".encode("latin-1")

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)

    stream_obj = DecodedStreamObject()
    stream_obj.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream_obj)

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pdf_only
def test_a_pdf_with_real_text_truncates_to_exactly_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(_pdf_with_extractable_text())
    result = de.extract_document(path, max_output_chars=500)
    assert result["status"] == "truncated"
    assert result["chars"] == 500
    assert len(result["text"]) == 500


def test_no_path_or_content_reaches_the_result_or_logs(tmp_path: Path, caplog) -> None:
    """The one defect class that using the product cannot reveal."""
    marker_path = "SECRET_PATH_MARKER"
    marker_content = "SECRET_CONTENT_MARKER"
    directory = tmp_path / marker_path
    directory.mkdir()

    cases = [
        (directory / f"{marker_path}.zip", marker_content.encode("utf-8")),
        (directory / f"{marker_path}.txt", b"\xff\xfe" + marker_content.encode("utf-8")),
        (directory / f"{marker_path}.pdf", b"%PDF-1.7 " + marker_content.encode("utf-8")),
        (directory / f"{marker_path}.absent.txt", None),
    ]
    with caplog.at_level(logging.DEBUG):
        for path, data in cases:
            if data is not None:
                path.write_bytes(data)
            result = de.extract_document(path)
            blob = json.dumps(result)
            assert marker_path not in blob
            assert marker_content not in blob
            assert str(path) not in blob
            if result["status"] in de.FAILURE_CODES:
                assert "/" not in result["status"] and "\\" not in result["status"]

    logged = "\n".join(record.message for record in caplog.records)
    assert marker_path not in logged
    assert marker_content not in logged


def test_oversized_result_never_exceeds_the_caller_s_cap(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("b" * 20_000, encoding="utf-8")
    result = de.extract_document(path, max_output_chars=500)
    assert result["status"] == "truncated"
    assert result["chars"] <= 500


def test_works_on_a_cold_home_with_no_prior_state(tmp_path: Path, monkeypatch) -> None:
    """No node home, no store, no config: the helper owns no state at all."""
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "empty-home"))
    path = tmp_path / "first.md"
    path.write_text("# cold\n", encoding="utf-8")
    assert de.extract_document(path)["status"] == "parsed"
