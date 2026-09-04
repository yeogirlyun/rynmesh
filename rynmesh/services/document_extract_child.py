"""The child half of `rynmesh.services.document_extract`. Never imported.

Run as `python -m rynmesh.services.document_extract_child`, one document per
process. `apply_limits` runs before anything is opened, so a decompression bomb
meets the address-space ceiling rather than the host's memory.

The path arrives on stdin, never in `argv`: another local user can read a
process's arguments out of `ps`, and the name of a document a user imported is
itself private. Exactly one JSON object goes to stdout and nothing else; stderr
is the parent's problem and is discarded there, because document parsers print
fragments of document content in their warnings.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rynmesh.services.document_extract import (
    FAILED_NOT_TEXT,
    FAILED_UNREADABLE,
    MAX_OUTPUT_CHARS,
    MEMORY_BYTES,
    STATUS_PARSED,
    STATUS_TRUNCATED,
    STATUS_UNSUPPORTED,
    classify,
)


def apply_limits() -> None:
    """Cap address space and CPU for this process. POSIX only; a no-op on Windows.

    Self-applied rather than passed through `preexec_fn`, which runs between
    fork and exec and is unsafe in a threaded parent — and the node is threaded.
    """

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return
    for limit, value in (
        (resource.RLIMIT_AS, MEMORY_BYTES),
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 64),
    ):
        try:
            soft, hard = resource.getrlimit(limit)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (ceiling, hard))
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            # A limit the host refuses is not fatal: the parent's wall-clock
            # deadline and output cap still bound this process.
            continue


def _emit(status: str, kind: str, text: str = "") -> None:
    payload = {"status": status, "kind": kind, "text": text, "chars": len(text)}
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def _read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return "", FAILED_NOT_TEXT


def pdf_available() -> bool:
    """True when a PDF extractor is installed and not disabled."""

    if os.environ.get("RYNMESH_DOC_EXTRACT_DISABLE_PDF") == "1":
        return False
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def _read_pdf(path: Path, limit: int) -> tuple[str, str]:
    """Bounded text from a PDF. Returns (text, failure_code)."""

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        # A page's own extractor is the untrusted part; a failure on one page
        # must not discard the pages already read.
        try:
            piece = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - third-party detail must not leak
            continue
        parts.append(piece)
        total += len(piece)
        if total > limit:
            break
    return "\n".join(parts), ""


def main() -> int:
    apply_limits()
    raw = sys.stdin.readline().strip()
    if not raw:
        _emit(FAILED_UNREADABLE, "unknown")
        return 0
    path = Path(raw)
    kind = classify(path)
    try:
        if not path.is_file():
            _emit(FAILED_UNREADABLE, kind)
            return 0
    except OSError:
        _emit(FAILED_UNREADABLE, kind)
        return 0

    if kind in {"text", "markdown"}:
        try:
            text, failure = _read_text(path)
        except OSError:
            _emit(FAILED_UNREADABLE, kind)
            return 0
        if failure:
            _emit(failure, kind)
            return 0
    elif kind == "pdf" and pdf_available():
        try:
            text, failure = _read_pdf(path, MAX_OUTPUT_CHARS)
        except OSError:
            _emit(FAILED_UNREADABLE, kind)
            return 0
        except Exception:  # noqa: BLE001 - a malformed document is not a crash
            _emit(FAILED_UNREADABLE, kind)
            return 0
        if failure:
            _emit(failure, kind)
            return 0
    else:
        _emit(STATUS_UNSUPPORTED, kind)
        return 0

    if len(text) > MAX_OUTPUT_CHARS:
        _emit(STATUS_TRUNCATED, kind, text[:MAX_OUTPUT_CHARS])
        return 0
    _emit(STATUS_PARSED, kind, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
