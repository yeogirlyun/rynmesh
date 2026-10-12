"""Reuse the supervised document extractor for bounded PDF text only."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from ..services.document_extract import extract_document
from .store import AgentToolRejected

PDF_BYTES = 8 * 1024 * 1024
PDF_CHARS = 100_000


def pdf_text(raw):
    if len(raw) > PDF_BYTES:
        raise AgentToolRejected("agent_resource_size_limit")
    with tempfile.TemporaryDirectory(prefix="ryn-agent-document-") as folder:
        target = Path(folder) / "source.pdf"
        target.write_bytes(raw)
        os.chmod(target, 0o600)
        result = extract_document(target, max_input_bytes=PDF_BYTES, max_output_chars=PDF_CHARS,
                                  timeout_s=10.0, memory_bytes=256 * 1024 * 1024)
    if result["status"] not in {"parsed", "truncated"}:
        code = {"unsupported": "agent_resource_extractor_unavailable", "failed:timeout": "agent_resource_extract_timeout",
                "failed:too_large": "agent_resource_size_limit", "failed:memory": "agent_resource_extract_memory_limit"}
        raise AgentToolRejected(code.get(result["status"], "agent_resource_extract_failed"))
    return {"text": result["text"], "format": "pdf-extracted-text", "input_version": hashlib.sha256(raw).hexdigest(),
            "extraction_truncated": result["status"] == "truncated",
            **({"extraction_pages": result["pdf_pages"]} if "pdf_pages" in result else {}),
            "extraction_coverage": "Extractable PDF text only; no OCR, page-layout fidelity, or Office extraction. At most 100000 characters."}
