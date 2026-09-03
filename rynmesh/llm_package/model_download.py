"""Resumable HTTPS model download: Range-based resume, size guard, checksum quarantine.

Split out of `lifecycle.py` to keep that module under the project's module
size ceiling. `lifecycle._download` re-exports `download` here under its
historical name so existing monkeypatch call sites (`lifecycle._download`)
keep working; a caller may also patch `model_download.download` directly,
or this module's own `_urlopen`.

Every request goes through the shared HTTPS-only opener (`https_only`), so a
redirect off HTTPS is refused mid-download rather than followed.

Nothing raised from here may contain a filesystem path or a URL: only fixed,
path-free strings and (for genuine I/O failures) the exception's type name.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import LifecycleError
from .https_only import build_https_only_opener

ProgressCallback = Callable[[str, int, str], None]
CancelCheck = Callable[[], bool]

CHUNK_BYTES = 1024 * 1024
_CONTENT_RANGE_START = re.compile(r"bytes\s+(\d+)-")


def _urlopen(request: urllib.request.Request, timeout: float = 300) -> Any:
    """Open `request` with the HTTPS-only opener (the whole redirect chain).

    The single seam tests replace to serve a model body without a network.
    """
    return build_https_only_opener().open(request, timeout=timeout)


def _header(headers: object, name: str) -> str | None:
    """Case-tolerant header lookup for both real and test-double header objects."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name) or getter(name.lower())


def _report(progress: ProgressCallback | None, cancel_check: CancelCheck | None,
           stage: str, percent: int, message: str) -> None:
    if cancel_check and cancel_check():
        raise LifecycleError("setup cancelled")
    if progress:
        progress(stage, max(0, min(100, percent)), message)


def file_sha256(path: Path) -> str:
    """Fresh SHA-256 of the complete file (never a running/partial digest)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    size_bytes: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> str:
    """Download `url` to `destination`, resuming a prior partial attempt.

    A `<destination>.part` left over from an earlier call (a dropped
    connection, a cancelled setup, or the app quitting) is resumed with a
    `Range` request rather than restarted from zero:

    - `206` appends to the existing part.
    - `200` (the server ignored `Range`) truncates and restarts.
    - A `206` whose `Content-Range` start does not match what was asked for
      is treated the same as an ignored `Range`: truncate and restart —
      trusting the byte offset the server actually used, not just its
      status code.
    - `416` means the part already holds the whole file; it is verified as
      complete without any further network read.

    The part is deleted only for a size-guard violation (untrustworthy
    data) or replaced with `.corrupt` on a checksum mismatch. Every other
    failure — cancellation, a network error — leaves it in place so the
    next call can resume.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LifecycleError("install downloads require an HTTPS URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    resume_from = temporary.stat().st_size if temporary.exists() else 0
    headers = {"User-Agent": "Rynmesh/0.6"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        _report(progress, cancel_check, "download_model", 15, "Resuming verified model download")

    request = urllib.request.Request(url, headers=headers)
    already_complete = False
    try:
        connection = _urlopen(request, timeout=300)
    except urllib.error.HTTPError as exc:
        if resume_from and exc.code == 416:
            # The server confirms there is nothing left to fetch: the part on
            # disk already holds the whole file (only its checksum is unverified).
            already_complete = True
            connection = None
            exc.close()
        else:
            raise LifecycleError("download failed: " + type(exc).__name__) from exc
    except OSError as exc:
        raise LifecycleError("download failed: " + type(exc).__name__) from exc

    if not already_complete:
        overflow = False
        try:
            with connection as response:
                status = int(getattr(response, "status", 200) or 200)
                content_range = _header(response.headers, "Content-Range")
                range_start = None
                if content_range:
                    match = _CONTENT_RANGE_START.match(content_range.strip())
                    if match:
                        range_start = int(match.group(1))
                # A 206 that starts somewhere other than where we asked is not
                # a resume at all (some servers/proxies re-serve from byte 0
                # while still labeling the response 206); treat it as a restart.
                range_mismatch = status == 206 and range_start is not None and range_start != resume_from
                restart = (bool(resume_from) and status != 206) or range_mismatch
                downloaded = 0 if restart else resume_from
                total = int(_header(response.headers, "content-length") or 0)
                if size_bytes:
                    full_size = size_bytes
                elif not restart and status == 206 and total:
                    full_size = total + resume_from
                else:
                    full_size = total or None
                mode = "wb" if restart or not resume_from else "ab"
                with temporary.open(mode) as handle:
                    while chunk := response.read(CHUNK_BYTES):
                        if cancel_check and cancel_check():
                            raise LifecycleError("setup cancelled")
                        downloaded += len(chunk)
                        if size_bytes is not None and downloaded > size_bytes:
                            # Not trustworthy: stop writing and discard below,
                            # once the handle is closed (Windows cannot unlink
                            # an open file).
                            overflow = True
                            break
                        handle.write(chunk)
                        percent = 15 + int(downloaded / full_size * 45) if full_size else 35
                        if progress:
                            progress("download_model", min(60, percent), "Downloading verified model data")
        except LifecycleError:
            raise
        except OSError as exc:
            raise LifecycleError("download failed: " + type(exc).__name__) from exc
        if overflow:
            temporary.unlink(missing_ok=True)
            raise LifecycleError("download exceeded the pinned size")

    # Hash the complete file fresh (not a running digest), so a resumed part
    # verifies against the bytes actually on disk rather than just this session.
    actual = file_sha256(temporary)
    if actual != expected_sha256.lower():
        corrupt = destination.with_suffix(destination.suffix + ".corrupt")
        corrupt.unlink(missing_ok=True)
        temporary.replace(corrupt)
        raise LifecycleError("model checksum mismatch; the download was quarantined and will restart")
    temporary.replace(destination)
    return actual
