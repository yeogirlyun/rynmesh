"""Document extraction through the packaged sidecar's dedicated worker entry."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_document_extract import _pdf_with_extractable_text, pdf_only

from rynmesh.services import document_extract as extract
from rynmesh.services import document_extract_child as child

ENTRY = Path(__file__).resolve().parents[1] / "webapp/src-tauri/sidecar/rynmesh_peer_entry.py"


def test_frozen_launch_uses_private_worker_instead_of_python_module(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert extract._child_argv() == [sys.executable, "--ryn-document-worker"]


@pdf_only
def test_sidecar_worker_extracts_pdf_without_starting_node(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(_pdf_with_extractable_text(2))
    home = tmp_path / "unused-node-home"
    env = {**os.environ, "RYNMESH_HOME": str(home), "RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS": "50"}
    process = subprocess.run([sys.executable, str(ENTRY), "--ryn-document-worker"], input=os.fsencode(source),
        capture_output=True, timeout=15, env=env,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    assert process.returncode == 0
    result = json.loads(process.stdout)
    assert result["status"] == "truncated" and result["kind"] == "pdf" and len(result["text"]) == 50
    assert result["text"].startswith("Lorem ipsum")
    assert not home.exists() and not process.stderr


@pdf_only
def test_parent_supervises_sidecar_worker_entry_and_output_cap(tmp_path, monkeypatch):
    source = tmp_path / "document.pdf"
    source.write_bytes(_pdf_with_extractable_text(4))
    monkeypatch.setattr(extract, "_child_argv", lambda: [sys.executable, str(ENTRY), "--ryn-document-worker"])
    result = extract.extract_document(source, max_output_chars=31)
    assert result["status"] == "truncated" and result["chars"] == 31
    assert result["text"] == "Lorem ipsum dolor sit amet cons"
    assert result["pdf_pages"] == {"total": 1, "processed": 1, "failed": 0, "without_text": 0}


@pdf_only
def test_partial_pdf_page_failure_is_visible_without_discarding_good_text(tmp_path, monkeypatch, capsys):
    import pypdf
    class Good:
        def extract_text(self):
            return "Verified page text"
    class Broken:
        def extract_text(self):
            raise ValueError("PRIVATE-PARSER-ERROR")
    class Scanned:
        def extract_text(self):
            return ""
    monkeypatch.setattr(pypdf, "PdfReader", lambda _: SimpleNamespace(pages=[Good(), Broken(), Scanned()]))
    source = tmp_path / "mixed.pdf"
    source.write_bytes(b"%PDF-1.7 synthetic parser boundary fixture")
    monkeypatch.setattr(child, "apply_limits", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(os.fsencode(source))))
    assert child.main() == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "truncated" and result["text"].startswith("Verified page text")
    assert result["pdf_pages"] == {"total": 3, "processed": 3, "failed": 1, "without_text": 1}
    assert "PRIVATE-PARSER-ERROR" not in output.out + output.err


def test_agent_pdf_result_preserves_partial_page_evidence(monkeypatch):
    from rynmesh.agent_runtime import resource_documents
    pages = {"total": 3, "processed": 2, "failed": 1, "without_text": 0}
    monkeypatch.setattr(resource_documents, "extract_document", lambda *_, **__: {"status": "truncated", "text": "Known page", "pdf_pages": pages})
    result = resource_documents.pdf_text(b"synthetic input")
    assert result["extraction_truncated"] and result["extraction_pages"] == pages
    assert "no OCR" in result["extraction_coverage"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process tree containment")
def test_windows_document_timeout_terminates_actual_descendant(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    source = tmp_path / "input.txt"
    source.write_text("safe", encoding="utf-8")
    marker = tmp_path / "descendant.pid"
    script = ("import subprocess,sys,time; "
              "from pathlib import Path; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)")
    monkeypatch.setattr(extract, "_child_argv", lambda: [sys.executable, "-c", script])
    result = extract.extract_document(source, timeout_s=2)
    assert result["status"] == extract.FAILED_TIMEOUT
    assert marker.exists(), "The test must actually start a descendant"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 3000) == 0
        finally:
            kernel.CloseHandle(handle)
    else:
        assert ctypes.get_last_error() == 87  # Already reaped, invalid PID.
