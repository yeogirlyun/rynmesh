"""Bounded managed snapshots for private Library file imports."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import multiprocessing
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_PDF_PARSE_SECONDS = 10
_SAFE_NAME = re.compile(r"[^\w.()\- ]+", re.UNICODE)


class LibraryImportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _clean_filename(value: str) -> str:
    name = Path(str(value).replace("\\", "/")).name.strip()
    name = _SAFE_NAME.sub("_", name)[:180].strip(" .")
    return name or "imported-file"


def _parse_pdf_worker(data: bytes, max_pages: int, max_extracted: int, connection: Any) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=True)
        if len(reader.pages) > max_pages:
            connection.send(("error", "library_pdf_page_limit", "The PDF has too many pages."))
            return
        pages: list[str] = []
        for page in reader.pages:
            pages.append((page.extract_text() or "").strip())
            if len("\n\n".join(pages).encode("utf-8")) > max_extracted:
                connection.send(("error", "library_extracted_too_large", "The extracted text is too large."))
                return
        connection.send(("ok", "\n\n".join(value for value in pages if value), len(reader.pages)))
    except Exception:
        connection.send(("error", "library_pdf_invalid", "The PDF could not be parsed safely."))
    finally:
        connection.close()


def _extract_pdf(data: bytes) -> tuple[str, int]:
    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_parse_pdf_worker,
        args=(data, MAX_PDF_PAGES, MAX_EXTRACTED_BYTES, sender),
        daemon=True,
    )
    process.start()
    sender.close()
    if not receiver.poll(MAX_PDF_PARSE_SECONDS):
        process.terminate()
        process.join(timeout=1)
        receiver.close()
        raise LibraryImportError("library_pdf_timeout", "The PDF exceeded the safe parse time.")
    result = receiver.recv()
    receiver.close()
    process.join(timeout=1)
    if result[0] != "ok":
        raise LibraryImportError(str(result[1]), str(result[2]))
    return str(result[1]), int(result[2])


class LibraryImportStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup_temporary()

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self.root.glob("imp_*/metadata.json"):
            try:
                records.append(dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(records, key=lambda item: float(item.get("created_at_unix", 0)), reverse=True)

    def get(self, import_id: str) -> dict[str, Any]:
        directory = self._directory(import_id)
        try:
            return dict(json.loads((directory / "metadata.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise LibraryImportError("library_import_not_found", "The managed import does not exist.") from exc

    def extracted_text(self, import_id: str) -> str:
        directory = self._directory(import_id)
        try:
            payload = dict(json.loads((directory / "extracted.json").read_text(encoding="utf-8")))
            return str(payload.get("text", ""))
        except (OSError, ValueError, TypeError) as exc:
            raise LibraryImportError("library_import_not_ready", "The managed import is not ready for Ask Ryn.") from exc

    def read_bytes(self, import_id: str) -> bytes:
        directory = self._directory(import_id)
        metadata = self.get(import_id)
        try:
            data = (directory / "original.bin").read_bytes()
        except OSError as exc:
            raise LibraryImportError(
                "library_import_not_ready", "The managed import bytes are unavailable."
            ) from exc
        if len(data) != int(metadata.get("size_bytes", -1)):
            raise LibraryImportError(
                "library_import_size_mismatch", "The managed import size no longer matches."
            )
        if hashlib.sha256(data).hexdigest() != str(metadata.get("sha256", "")):
            raise LibraryImportError(
                "library_import_hash_mismatch", "The managed import hash no longer matches."
            )
        return data

    def import_bytes(self, body: Mapping[str, Any]) -> dict[str, Any]:
        filename = _clean_filename(str(body.get("filename", "")))
        mime = str(body.get("mime", "application/octet-stream")).lower().split(";", 1)[0]
        encoded = str(body.get("data_base64", ""))
        if len(encoded) > ((MAX_IMPORT_BYTES + 2) // 3) * 4 + 8:
            raise LibraryImportError("library_import_size_limit", "The file is empty or exceeds 32 MiB.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise LibraryImportError("library_import_invalid_base64", "The upload is not valid base64.") from exc
        if not data or len(data) > MAX_IMPORT_BYTES:
            raise LibraryImportError("library_import_size_limit", "The file is empty or exceeds 32 MiB.")
        total = sum(int(item.get("size_bytes", 0) or 0) for item in self.list())
        if total + len(data) > MAX_IMPORT_TOTAL_BYTES:
            raise LibraryImportError("library_import_quota", "The managed import quota is full.")

        suffix = Path(filename).suffix.lower()
        if suffix not in {".txt", ".md", ".markdown", ".pdf"}:
            raise LibraryImportError("library_import_type_unsupported", "Only TXT, Markdown, and PDF are supported.")
        if suffix == ".pdf":
            if mime != "application/pdf" or not data.startswith(b"%PDF-"):
                raise LibraryImportError("library_import_mime_mismatch", "The file does not match its PDF type.")
            text, page_count = _extract_pdf(data)
            kind = "pdf"
        else:
            if mime not in {"text/plain", "text/markdown", "application/octet-stream"} or data.startswith(b"%PDF-"):
                raise LibraryImportError("library_import_mime_mismatch", "The file does not match its text type.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LibraryImportError("library_import_not_utf8", "Text imports must be UTF-8.") from exc
            page_count = 0
            kind = "markdown" if suffix in {".md", ".markdown"} else "text"
        text = text.replace("\x00", "").strip()
        if not text:
            raise LibraryImportError("library_import_empty", "No extractable text was found.")
        if len(text.encode("utf-8")) > MAX_EXTRACTED_BYTES:
            raise LibraryImportError("library_extracted_too_large", "The extracted text exceeds 2 MiB.")

        import_id = f"imp_{uuid.uuid4().hex}"
        temporary = (self.root / f".{import_id}.tmp").resolve()
        final = (self.root / import_id).resolve()
        if temporary.parent != self.root or final.parent != self.root:
            raise LibraryImportError("library_import_path_invalid", "The managed import path is invalid.")
        temporary.mkdir()
        (temporary / "original.bin").write_bytes(data)
        os.chmod(temporary / "original.bin", 0o600)
        _atomic_json(temporary / "extracted.json", {"version": 1, "text": text})
        record = {
            "version": "ryn.library-import.v1",
            "import_id": import_id,
            "filename": filename,
            "kind": kind,
            "mime": mime,
            "state": "ready",
            "size_bytes": len(data),
            "extracted_bytes": len(text.encode("utf-8")),
            "page_count": page_count,
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at_unix": time.time(),
        }
        _atomic_json(temporary / "metadata.json", record)
        os.replace(temporary, final)
        return record

    def delete(self, import_id: str) -> bool:
        directory = self._directory(import_id)
        if not directory.exists():
            return False
        shutil.rmtree(directory)
        return True

    def clear(self) -> int:
        removed = 0
        for item in self.list():
            removed += int(self.delete(str(item["import_id"])))
        return removed

    def cleanup_temporary(self) -> int:
        removed = 0
        for path in self.root.glob(".imp_*.tmp"):
            if path.is_dir() and path.resolve().parent == self.root:
                shutil.rmtree(path)
                removed += 1
        return removed

    def _directory(self, import_id: str) -> Path:
        if not import_id.startswith("imp_") or not import_id[4:].isalnum():
            raise LibraryImportError("library_import_not_found", "The managed import does not exist.")
        directory = (self.root / import_id).resolve()
        if directory.parent != self.root:
            raise LibraryImportError("library_import_not_found", "The managed import does not exist.")
        return directory
