"""Bounded plain-text extraction from untrusted local documents.

Parsing a user's document is an attack surface: a decompression bomb, a
malformed cross-reference table, or a parser bug can exhaust memory or crash
the interpreter. Nothing here parses in the node process. `extract_document`
validates and supervises; the real work happens in
`rynmesh.services.document_extract_child`, a short-lived child that applies its
own address-space and CPU limits before it opens anything.

Privacy rule for everything in both modules: no filesystem path, no byte of
document content, and no third-party exception message may appear in a returned
value, in a raised message, or in anything the node logs. Every failure is one
of a closed set of fixed literals.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

__all__ = [
    "FAILED_CRASHED",
    "FAILED_INTERNAL",
    "FAILED_MEMORY",
    "FAILED_NOT_TEXT",
    "FAILED_TIMEOUT",
    "FAILED_TOO_LARGE",
    "FAILED_UNREADABLE",
    "FAILURE_CODES",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_CHARS",
    "MEMORY_BYTES",
    "STATUS_PARSED",
    "STATUS_TRUNCATED",
    "STATUS_UNSUPPORTED",
    "TIMEOUT_S",
    "classify",
]

# A 32 MiB ceiling on what is worth opening at all. The parent stats the file
# and refuses before spawning, so an oversized input costs no process.
MAX_INPUT_BYTES = int(os.environ.get("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", 32 * 1024 * 1024))
# Matches RYNMESH_LOCAL_BODY_MAX_BYTES, the cap the local content-body route
# already applies, so a caller that shows both sees one consistent bound.
MAX_OUTPUT_CHARS = int(os.environ.get("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", 1024 * 1024))
TIMEOUT_S = float(os.environ.get("RYNMESH_DOC_EXTRACT_TIMEOUT_S", "20"))
MEMORY_BYTES = int(os.environ.get("RYNMESH_DOC_EXTRACT_MEMORY_BYTES", 512 * 1024 * 1024))

FAILED_TOO_LARGE = "failed:too_large"
FAILED_TIMEOUT = "failed:timeout"
FAILED_MEMORY = "failed:memory"
FAILED_CRASHED = "failed:crashed"
FAILED_UNREADABLE = "failed:unreadable"
FAILED_NOT_TEXT = "failed:not_text"
FAILED_INTERNAL = "failed:internal"

FAILURE_CODES = frozenset(
    {
        FAILED_TOO_LARGE,
        FAILED_TIMEOUT,
        FAILED_MEMORY,
        FAILED_CRASHED,
        FAILED_UNREADABLE,
        FAILED_NOT_TEXT,
        FAILED_INTERNAL,
    }
)

STATUS_PARSED = "parsed"
STATUS_TRUNCATED = "truncated"
STATUS_UNSUPPORTED = "unsupported"

_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_TEXT_SUFFIXES = {".txt", ".text", ".log", ".json", ".yaml", ".yml", ".xml", ".csv"}
_PDF_SUFFIXES = {".pdf"}


def classify(path: str | Path) -> str:
    """The extractor family for ``path``, from its suffix and guessed type.

    Suffix first, media type second: `mimetypes` is configured from the host's
    own tables and varies between machines, so it decides only what the suffix
    list does not already cover.
    """

    suffix = Path(path).suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    if suffix in _PDF_SUFFIXES:
        return "pdf"
    guessed, _encoding = mimetypes.guess_type(str(path))
    if guessed == "application/pdf":
        return "pdf"
    if guessed and guessed.startswith("text/"):
        return "text"
    return "unknown"
