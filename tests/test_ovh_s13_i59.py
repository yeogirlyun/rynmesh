"""S13-I59: exercise the supervised production document path."""
import io
import os
import random
import subprocess
import sys
import zipfile

import pytest

from rynmesh.services import document_extract as de
from rynmesh.services.library_imports import LibraryImportStore, LibraryImportError


def package(parts, compression=zipfile.ZIP_STORED):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return raw.getvalue()


def office(suffix):
    if suffix == ".docx":
        return package({"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>中文报告</w:t></w:r></w:p></w:document>'})
    if suffix == ".xlsx":
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        return package({"xl/sharedStrings.xml": f'<sst xmlns="{ns}"><si><t>中文报告</t></si></sst>', "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}"><sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c><c t="inlineStr"><is><t>备注</t></is></c></row></sheetData></worksheet>'})
    return package({"ppt/slides/slide1.xml": '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>中文报告</a:t></a:r></a:p></p:sld>'})


def test_T1_s13_i59_real_memory_limit(tmp_path, monkeypatch):
    # Real suspended worker, real Job Object, real allocation; not a raised fake error.
    script = "from rynmesh.services import document_extract_child as c\ndef allocate(*args):\n b=bytearray(200*1024*1024)\n return 'allocated', '', None\nc._extract=allocate\nraise SystemExit(c.main())"
    monkeypatch.setattr(de, "_child_argv", lambda: [sys.executable, "-c", script])
    path = tmp_path / "allocate.txt"
    path.write_text("allocate")
    assert de.extract_document(path, memory_bytes=64*1024*1024)["status"] == "failed:memory"


@pytest.mark.parametrize("encoding", ["gbk", "utf-8-sig", "utf-16", "utf-16-be"])
def test_T2_s13_i59_encoding(tmp_path, encoding):
    path = tmp_path / "中文.csv"
    text = "姓名,金额\r\n张三,12\r\n"
    raw = text.encode(encoding)
    if encoding == "utf-16-be":
        raw = b"\xfe\xff" + raw
    path.write_bytes(raw)
    result = de.extract_document(path)
    assert result["status"] == "parsed"
    assert result["text"] == text.replace("\r\n", "\n")


def test_T3_s13_i59_binary_and_unknown_encoding(tmp_path):
    path = tmp_path / "binary.txt"
    path.write_bytes(random.Random(59).randbytes(4096))
    assert de.extract_document(path)["status"] == "failed:not_text"
    path.write_bytes(b"\x01"*100 + b"\x81")
    assert de.extract_document(path)["status"] == "failed:not_text"
    path.write_bytes(b"\x81")
    assert de.extract_document(path)["status"] == "failed:encoding_unknown"
    assert "failed:encoding_unknown" in de.FAILURE_CODES


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
def test_T4_s13_i59_office(tmp_path, suffix):
    path = tmp_path / ("report" + suffix)
    path.write_bytes(office(suffix))
    result = de.extract_document(path)
    assert result["status"] == "parsed"
    assert result["kind"] == "office"
    assert "中文报告" in result["text"]
    if suffix == ".xlsx":
        assert "42" in result["text"] and "备注" in result["text"]
    limited = de.extract_document(path, max_output_chars=2)
    assert limited["status"] == "truncated" and len(limited["text"]) == 2


@pytest.mark.parametrize("bomb", ["ratio", "entries"])
def test_T5_s13_i59_zip_limits(tmp_path, bomb):
    raw = (package({"word/document.xml": "x"*100_000}, zipfile.ZIP_DEFLATED)
           if bomb == "ratio" else package({f"entry{i}": "" for i in range(2001)}))
    path = tmp_path / "bomb.docx"
    path.write_bytes(raw)
    assert de.extract_document(path)["status"] == "failed:too_large"


@pytest.mark.parametrize("peak,expected", [(64*1024*1024, "failed:memory"), (1, "failed:crashed")])
def test_T1_s13_i59_abnormal_exit_peak_before_close(tmp_path, monkeypatch, peak, expected):
    from rynmesh.llm_package import process_guard
    path = tmp_path / "crash.txt"
    path.write_text("x")
    events = []
    def launch(*args, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", "import os;os._exit(3)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        process._ryn_job = object()
        return process
    monkeypatch.setattr(process_guard, "windows_job_peak_memory", lambda job: events.append("query") or peak, raising=False)
    monkeypatch.setattr(de, "_stop", lambda proc: events.append("close"))
    assert de.extract_document(path, memory_bytes=64*1024*1024, spawn=launch)["status"] == expected
    assert events == ["query", "close"]


def test_T4_s13_i59_library_real_call(tmp_path):
    store = LibraryImportStore(tmp_path / "imports")
    result = store.save(office(".docx"), filename="report.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert result["kind"] == "office"


def test_T3_s13_i59_library_encoding_error(tmp_path):
    store = LibraryImportStore(tmp_path / "imports")
    with pytest.raises(LibraryImportError, match="library_import_encoding_unknown"):
        store.save(b"\x81", filename="broken.txt", mime="text/plain")


def test_T1_s13_i59_real_windows_peak_query(tmp_path):
    from rynmesh.llm_package import process_guard
    process = de._spawn_worker([sys.executable, "-c", "b=bytearray(2*1024*1024)"],
                               memory_bytes=64*1024*1024, stdout=subprocess.PIPE)
    try:
        process.communicate(timeout=10)
        assert process.returncode == 0
        if os.name == "nt":
            peak = process_guard.windows_job_peak_memory(process._ryn_job)
            assert 2*1024*1024 <= peak <= 64*1024*1024
        else:
            assert not getattr(process, "_ryn_job", None)
    finally:
        de._stop(process)
