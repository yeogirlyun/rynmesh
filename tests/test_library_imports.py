import base64
import io

import pytest
from reportlab.pdfgen import canvas

from rynmesh.services.library_imports import LibraryImportError, LibraryImportStore


def _pdf(text: str) -> bytes:
    stream = io.BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(72, 720, text)
    document.save()
    return stream.getvalue()


def test_pdf_import_extracts_text_and_has_no_paths_in_metadata(tmp_path):
    store = LibraryImportStore(tmp_path / "imports")
    data = _pdf("Grounded PDF evidence")
    record = store.import_bytes({
        "filename": "evidence.pdf",
        "mime": "application/pdf",
        "data_base64": base64.b64encode(data).decode(),
    })
    assert record["kind"] == "pdf"
    assert record["page_count"] == 1
    assert "Grounded PDF evidence" in store.extracted_text(record["import_id"])
    assert store.read_bytes(record["import_id"]) == data
    assert str(tmp_path) not in str(record)


def test_read_bytes_rejects_tampered_managed_content(tmp_path):
    store = LibraryImportStore(tmp_path / "imports")
    record = store.import_bytes(
        {
            "filename": "shared.md",
            "mime": "text/markdown",
            "data_base64": base64.b64encode(b"# original").decode(),
        }
    )
    original = store.root / record["import_id"] / "original.bin"
    original.write_bytes(b"# modified")
    with pytest.raises(LibraryImportError) as raised:
        store.read_bytes(record["import_id"])
    assert raised.value.code in {
        "library_import_size_mismatch",
        "library_import_hash_mismatch",
    }


def test_oversize_base64_and_temporary_cleanup_fail_closed(tmp_path, monkeypatch):
    import rynmesh.services.library_imports as module

    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 4)
    store = LibraryImportStore(tmp_path / "imports")
    with pytest.raises(LibraryImportError) as raised:
        store.import_bytes({"filename": "large.txt", "mime": "text/plain", "data_base64": base64.b64encode(b"12345").decode()})
    assert raised.value.code == "library_import_size_limit"
    temporary = store.root / ".imp_deadbeef.tmp"
    temporary.mkdir()
    (temporary / "partial").write_bytes(b"partial")
    assert store.cleanup_temporary() == 1
    assert not temporary.exists()


def test_pdf_page_limit_fails_closed(tmp_path, monkeypatch):
    import rynmesh.services.library_imports as module

    monkeypatch.setattr(module, "MAX_PDF_PAGES", 0)
    store = LibraryImportStore(tmp_path / "imports")
    with pytest.raises(LibraryImportError) as raised:
        store.import_bytes({
            "filename": "bomb.pdf", "mime": "application/pdf",
            "data_base64": base64.b64encode(_pdf("page limit")).decode(),
        })
    assert raised.value.code == "library_pdf_page_limit"
