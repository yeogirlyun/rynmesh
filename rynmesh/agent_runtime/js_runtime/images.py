"""Trusted host image capability registry and artifact-backed promotion core for Ryn QuickJS."""

from __future__ import annotations

import copy
import hashlib
import io
import os
import re
import secrets
import stat
import struct
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..computer.protocol import (
    NativeBridgeError,
)
from ..computer.protocol import (
    validate_window_ref as _computer_validate_window_ref,
)
from ..computer.protocol import (
    window_refs_equal as _computer_window_refs_equal,
)
from .protocol import (
    JsProtocolError,
    canonical_json_bytes,
    sanitize_image_metadata,
    sanitize_snapshot_value,
    validate_identifier,
)

CAPABILITY_URL_PREFIX = "ryn-image:cap:"
CAPABILITY_TOKEN_BYTES = 32
CAPABILITY_TOKEN_HEX_LEN = CAPABILITY_TOKEN_BYTES * 2
CAPABILITY_URL_RE = re.compile(r"^ryn-image:cap:([0-9a-f]{64})$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_SESSION_IMAGE_ENTRIES = 16
MAX_ACTIVE_PINNED_IMAGES = 4
MAX_NATIVE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_GUEST_INLINE_BASE64_BYTES = 128 * 1024
MAX_SESSION_ORIGINAL_BYTES = 32 * 1024 * 1024
MAX_TOTAL_PROMOTION_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_SCREENSHOT_DESCRIPTOR_BYTES = 4096
MAX_PROMOTION_QUEUE_ENTRIES = 64

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

ALLOWED_ARTIFACT_PROVENANCE_KINDS: frozenset[str] = frozenset({
    "production_native_receipt",
    "test_fixture_owned_artifact",
})

PROMOTION_STAGES: tuple[str, ...] = (
    "requested",
    "prepared",
    "displayed",
    "submission-attempted",
    "submitted",
    "semantic-response",
    "pixel-verified",
)

PROMOTION_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"prepared"}),
    "prepared": frozenset({"prepared", "displayed", "submission-attempted", "submitted"}),
    "displayed": frozenset({"displayed", "submission-attempted", "submitted"}),
    "submission-attempted": frozenset({"submission-attempted", "submitted"}),
    "submitted": frozenset({"submitted", "semantic-response"}),
    "semantic-response": frozenset({"semantic-response", "pixel-verified"}),
    "pixel-verified": frozenset({"pixel-verified"}),
}

_ARTIFACT_SEAL_SENTINEL = object()

_NATIVE_WINDOW_REQUIRED_KEYS: frozenset[str] = frozenset({
    "workerInstance",
    "windowKey",
    "hwnd",
    "pid",
    "processStarted",
    "generation",
})
_NATIVE_WINDOW_ALL_KEYS: frozenset[str] = _NATIVE_WINDOW_REQUIRED_KEYS | frozenset({"appId"})
_COMBINED_WINDOW_ALL_KEYS: frozenset[str] = _NATIVE_WINDOW_ALL_KEYS | frozenset({"app", "id", "title", "native_window"})


def validate_promotion_stage_transition(current_stage: str, target_stage: str) -> None:
    """Validate that an image promotion stage transition is legal and monotonic."""
    allowed = PROMOTION_STAGE_TRANSITIONS.get(current_stage)
    if allowed is None or target_stage not in allowed:
        raise JsProtocolError(
            "ryn_js_invalid_promotion_stage",
            f"Illegal image promotion stage transition: {current_stage!r} -> {target_stage!r}",
        )


def normalize_window_identity(window: Any) -> tuple[dict[str, Any], str]:
    """Validate and canonicalize a public SkyWindow dictionary (`{app, id, title?}`) and return `(normalized_dict, window_key)`."""
    if not isinstance(window, dict):
        raise JsProtocolError("ryn_js_invalid_window", "Window identity must be a dictionary")
    app = window.get("app")
    win_id = window.get("id")
    if not isinstance(app, str) or not app.strip():
        raise JsProtocolError("ryn_js_invalid_window", "window.app must be a non-empty string")
    if type(win_id) is not int or win_id < 0:
        raise JsProtocolError("ryn_js_invalid_window", "window.id must be an integer >= 0")
    clean_app = app.strip()
    normalized: dict[str, Any] = {"app": clean_app, "id": win_id}
    title = window.get("title")
    if isinstance(title, str):
        normalized["title"] = title
    return normalized, f"{clean_app}::{win_id}"


def normalize_native_window_ref(
    native_window: Any = None,
    *,
    fallback_window: Any = None,
    expected_worker_instance: str | None = None,
) -> dict[str, Any]:
    """Validate and losslessly normalize a native `WindowRef` (`workerInstance`, `windowKey`, `hwnd`, `pid`, `processStarted`, `generation`, optional `appId`)."""
    clean_expected_worker = str(expected_worker_instance or "").strip()
    if not clean_expected_worker:
        raise JsProtocolError(
            "ryn_js_image_scope_mismatch",
            "expected_worker_instance is required for native WindowRef validation",
        )
    validate_identifier(clean_expected_worker, "expected_worker_instance")

    candidate: dict[str, Any] | None = None
    if native_window is not None:
        if not isinstance(native_window, dict):
            raise JsProtocolError("ryn_js_invalid_window", "native_window must be a WindowRef dictionary")
        if (
            _NATIVE_WINDOW_REQUIRED_KEYS.issubset(native_window.keys())
            and set(native_window.keys()).issubset(_COMBINED_WINDOW_ALL_KEYS)
            and any(k in native_window for k in ("app", "id", "title", "native_window"))
        ):
            candidate = {
                k: native_window[k]
                for k in _NATIVE_WINDOW_ALL_KEYS
                if k in native_window and native_window[k] is not None
            }
        else:
            candidate = dict(native_window)
    elif isinstance(fallback_window, dict):
        nested = fallback_window.get("native_window")
        if isinstance(nested, dict):
            candidate = dict(nested)
        elif _NATIVE_WINDOW_REQUIRED_KEYS.issubset(fallback_window.keys()):
            if not set(fallback_window.keys()).issubset(_COMBINED_WINDOW_ALL_KEYS):
                raise JsProtocolError("ryn_js_invalid_window", "Window dictionary contains unsupported fields")
            candidate = {
                k: fallback_window[k]
                for k in _NATIVE_WINDOW_ALL_KEYS
                if k in fallback_window and fallback_window[k] is not None
            }

    if candidate is None:
        raise JsProtocolError(
            "ryn_js_invalid_window",
            "Full native WindowRef (workerInstance, windowKey, hwnd, pid, processStarted, generation) is required",
        )

    try:
        validated = _computer_validate_window_ref(
            candidate,
            expected_worker_instance=clean_expected_worker,
        )
    except NativeBridgeError as exc:
        if getattr(exc, "reason", None) == "worker_instance_mismatch":
            raise JsProtocolError(
                "ryn_js_image_window_mismatch",
                f"Native WindowRef workerInstance {candidate.get('workerInstance')!r} does not match expected {clean_expected_worker!r}",
            ) from exc
        raise JsProtocolError(
            "ryn_js_invalid_window",
            f"Invalid native WindowRef: {getattr(exc, 'reason', None) or exc.code}",
        ) from exc
    return validated


def native_window_refs_equal(a: Any, b: Any) -> bool:
    """Return True iff two native WindowRef dictionaries match across all wire fields including optional appId."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return _computer_window_refs_equal(a, b) and a.get("appId") == b.get("appId")


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return True
        file_attrs = getattr(st, "st_file_attributes", 0)
        if isinstance(file_attrs, int) and (file_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return True
    except OSError:
        return True
    return False


def _resolve_opened_fd_path(fd: int) -> Path:
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.windll.kernel32
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        get_final_path.restype = ctypes.c_uint32
        buf = ctypes.create_unicode_buffer(4096)
        length = get_final_path(ctypes.c_void_p(handle), buf, 4096, 0)
        if not length or length >= 4096:
            raise JsProtocolError("ryn_js_image_artifact_unreadable", "Opened artifact handle path could not be resolved")
        raw_final = buf.value
        if raw_final.startswith("\\\\?\\UNC\\"):
            raw_final = "\\\\" + raw_final[8:]
        elif raw_final.startswith("\\\\?\\"):
            raw_final = raw_final[4:]
        return Path(raw_final).resolve(strict=True)
    proc_fd = Path(f"/proc/self/fd/{fd}")
    if proc_fd.exists():
        return proc_fd.resolve(strict=True)
    raise JsProtocolError("ryn_js_image_artifact_unreadable", "Opened artifact handle path could not be resolved")


def _verify_lineage_no_reparse(root_resolved: Path, target_resolved: Path) -> None:
    if not target_resolved.is_relative_to(root_resolved):
        raise JsProtocolError("ryn_js_image_outside_containment", "Artifact path resolves outside containment root")
    rel_parts = target_resolved.relative_to(root_resolved).parts
    if not rel_parts:
        raise JsProtocolError("ryn_js_image_artifact_unreadable", "Artifact path is not a regular file")
    if _is_symlink_or_reparse(root_resolved):
        raise JsProtocolError("ryn_js_image_outside_containment", "Containment root is invalid or a symlink/reparse point")
    current = root_resolved
    for part in rel_parts:
        current = current / part
        if _is_symlink_or_reparse(current):
            raise JsProtocolError("ryn_js_image_outside_containment", "Artifact lineage contains a symlink or reparse point")


def _validate_png_structure_and_header(
    raw_bytes: bytes,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> tuple[int, int]:
    """Verify PNG signature, chunk framing, IHDR dimensions, PIL header, and pixel cap without full pixel decode."""
    if len(raw_bytes) < 33 or not raw_bytes.startswith(PNG_SIGNATURE):
        raise JsProtocolError("ryn_js_image_invalid_png", "Artifact does not have a valid PNG signature")

    offset = 8
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    ihdr_width = 0
    ihdr_height = 0
    total_len = len(raw_bytes)

    while offset < total_len:
        if offset + 8 > total_len:
            raise JsProtocolError("ryn_js_image_invalid_png", "Truncated PNG chunk header")
        chunk_len = struct.unpack(">I", raw_bytes[offset : offset + 4])[0]
        chunk_type = raw_bytes[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + chunk_len
        crc_end = data_end + 4
        if crc_end > total_len:
            raise JsProtocolError("ryn_js_image_invalid_png", "Truncated PNG chunk payload")
        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_len != 13:
                raise JsProtocolError("ryn_js_image_invalid_png", "First PNG chunk must be a 13-byte IHDR")
            ihdr_width, ihdr_height, bit_depth, color_type, comp_m, filter_m, interlace_m = struct.unpack(
                ">IIBBBBB",
                raw_bytes[data_start:data_end],
            )
            if ihdr_width <= 0 or ihdr_height <= 0:
                raise JsProtocolError("ryn_js_image_invalid_png", "PNG IHDR dimensions must be positive")
            if ihdr_width * ihdr_height > max_pixels:
                raise JsProtocolError(
                    "ryn_js_image_dimensions_exceeded",
                    f"PNG dimensions {ihdr_width}x{ihdr_height} exceed maximum pixel cap {max_pixels}",
                )
            if bit_depth not in {1, 2, 4, 8, 16} or comp_m != 0 or filter_m != 0 or interlace_m not in {0, 1}:
                raise JsProtocolError("ryn_js_image_invalid_png", "Invalid PNG IHDR encoding fields")
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            if chunk_len != 0 or crc_end != total_len:
                raise JsProtocolError("ryn_js_image_invalid_png", "Invalid or trailing bytes after PNG IEND")
            saw_iend = True
        offset = crc_end

    if not (saw_ihdr and saw_idat and saw_iend):
        raise JsProtocolError("ryn_js_image_invalid_png", "PNG is missing required IHDR, IDAT, or IEND chunk")

    from PIL import Image, UnidentifiedImageError

    prev_max_pixels = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = max_pixels
        with Image.open(io.BytesIO(raw_bytes)) as img:
            pil_width, pil_height = img.size
            pil_format = (img.format or "").upper()
    except Image.DecompressionBombError as exc:
        raise JsProtocolError(
            "ryn_js_image_dimensions_exceeded",
            f"PIL rejected PNG dimensions as exceeding pixel cap: {exc}",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise JsProtocolError(
            "ryn_js_image_invalid_png",
            f"PIL header validation failed for PNG artifact: {exc}",
        ) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev_max_pixels

    if pil_format != "PNG" or pil_width != ihdr_width or pil_height != ihdr_height:
        raise JsProtocolError("ryn_js_image_invalid_png", "PIL header format or dimensions did not match PNG IHDR")
    if pil_width * pil_height > max_pixels:
        raise JsProtocolError(
            "ryn_js_image_dimensions_exceeded",
            f"PNG dimensions {pil_width}x{pil_height} exceed maximum pixel cap {max_pixels}",
        )
    if expected_width is not None and pil_width != expected_width:
        raise JsProtocolError(
            "ryn_js_image_artifact_changed",
            f"PNG width mismatch: expected {expected_width}, got {pil_width}",
        )
    if expected_height is not None and pil_height != expected_height:
        raise JsProtocolError(
            "ryn_js_image_artifact_changed",
            f"PNG height mismatch: expected {expected_height}, got {pil_height}",
        )
    return pil_width, pil_height


def revalidate_artifact_png_bytes(
    *,
    artifact_path: Path,
    containment_root: Path,
    expected_parent: Path | None = None,
    expected_sha256: str | None = None,
    expected_bytes: int | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
    max_bytes: int = MAX_NATIVE_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> tuple[bytes, str, int, int]:
    """Perform handle-contained bounded read, SHA-256 verification, and PNG/PIL header validation."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise JsProtocolError("ryn_js_image_invalid", "max_bytes must be a positive integer")
    if type(max_pixels) is not int or max_pixels <= 0:
        raise JsProtocolError("ryn_js_image_invalid", "max_pixels must be a positive integer")
    if expected_bytes is not None:
        if type(expected_bytes) is not int or expected_bytes <= 0:
            raise JsProtocolError("ryn_js_image_invalid", "expected_bytes must be a positive integer")
        if expected_bytes > max_bytes:
            raise JsProtocolError(
                "ryn_js_image_too_large",
                f"Artifact byte length {expected_bytes} exceeds cap {max_bytes}",
            )

    try:
        root_path = Path(containment_root)
        if not root_path.exists() or _is_symlink_or_reparse(root_path):
            raise JsProtocolError("ryn_js_image_outside_containment", "Containment root is missing or a symlink")
        root_resolved = root_path.resolve(strict=True)
        if not root_resolved.is_dir():
            raise JsProtocolError("ryn_js_image_outside_containment", "Containment root is not a directory")
        target = Path(artifact_path)
        if not target.exists() or _is_symlink_or_reparse(target):
            raise JsProtocolError("ryn_js_image_artifact_unreadable", "Artifact file is missing or a symlink/reparse point")
        target_resolved = target.resolve(strict=True)
        _verify_lineage_no_reparse(root_resolved, target_resolved)
        parent_resolved = expected_parent.resolve(strict=True) if expected_parent is not None else None
        if parent_resolved is not None and target_resolved.parent != parent_resolved:
            raise JsProtocolError("ryn_js_image_outside_containment", "Artifact parent directory mismatch")
    except OSError as exc:
        raise JsProtocolError("ryn_js_image_artifact_unreadable", f"Failed to resolve artifact path: {exc}") from exc

    try:
        st_before = os.lstat(target_resolved)
    except OSError as exc:
        raise JsProtocolError("ryn_js_image_artifact_unreadable", f"Failed to stat artifact: {exc}") from exc

    if stat.S_ISLNK(st_before.st_mode) or not stat.S_ISREG(st_before.st_mode):
        raise JsProtocolError("ryn_js_image_artifact_unreadable", "Artifact is not a regular file")
    file_attrs = getattr(st_before, "st_file_attributes", 0)
    if isinstance(file_attrs, int) and (file_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise JsProtocolError("ryn_js_image_outside_containment", "Artifact has reparse point attribute")
    if st_before.st_size <= 0:
        raise JsProtocolError("ryn_js_image_invalid_png", "Artifact file is empty")
    if st_before.st_size > max_bytes:
        raise JsProtocolError(
            "ryn_js_image_too_large",
            f"Artifact file size {st_before.st_size} exceeds maximum {max_bytes} bytes",
        )
    if expected_bytes is not None and st_before.st_size != expected_bytes:
        raise JsProtocolError(
            "ryn_js_image_artifact_changed",
            f"Artifact byte length changed on disk: expected {expected_bytes}, found {st_before.st_size}",
        )

    open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target_resolved, open_flags)
    except OSError as exc:
        raise JsProtocolError("ryn_js_image_artifact_unreadable", f"Failed to open artifact handle: {exc}") from exc

    try:
        try:
            st_fd = os.fstat(fd)
            st_after = os.lstat(target_resolved)
        except OSError as exc:
            raise JsProtocolError("ryn_js_image_artifact_unreadable", f"Failed to fstat artifact handle: {exc}") from exc

        if not stat.S_ISREG(st_fd.st_mode) or not stat.S_ISREG(st_after.st_mode):
            raise JsProtocolError("ryn_js_image_artifact_unreadable", "Opened handle is not a regular file")
        after_attrs = getattr(st_after, "st_file_attributes", 0)
        if isinstance(after_attrs, int) and (after_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise JsProtocolError("ryn_js_image_outside_containment", "Artifact became a reparse point")
        if (st_before.st_ino or st_fd.st_ino) and (
            st_fd.st_ino != st_before.st_ino
            or st_fd.st_dev != st_before.st_dev
            or st_after.st_ino != st_fd.st_ino
            or st_after.st_dev != st_fd.st_dev
        ):
            raise JsProtocolError("ryn_js_image_artifact_changed", "Artifact inode/device changed during open")

        handle_path = _resolve_opened_fd_path(fd)
        _verify_lineage_no_reparse(root_resolved, handle_path)
        if parent_resolved is not None and handle_path.parent != parent_resolved:
            raise JsProtocolError("ryn_js_image_outside_containment", "Opened handle parent directory mismatch")

        read_cap = expected_bytes if expected_bytes is not None else max_bytes
        read_limit = read_cap + 1
        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        total = 0

        while total < read_limit:
            try:
                chunk = os.read(fd, min(65536, read_limit - total))
            except OSError as exc:
                raise JsProtocolError("ryn_js_image_artifact_unreadable", f"Artifact read error: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            hasher.update(chunk)
            total += len(chunk)

        try:
            st_end = os.fstat(fd)
        except OSError as exc:
            raise JsProtocolError("ryn_js_image_artifact_unreadable", f"Artifact post-read fstat failed: {exc}") from exc

        if (st_fd.st_ino or st_end.st_ino) and (
            st_end.st_ino != st_fd.st_ino or st_end.st_dev != st_fd.st_dev
        ):
            raise JsProtocolError("ryn_js_image_artifact_changed", "Artifact handle identity changed during read")
        if total > max_bytes or st_fd.st_size > max_bytes or st_end.st_size > max_bytes:
            raise JsProtocolError("ryn_js_image_too_large", f"Artifact exceeds maximum {max_bytes} bytes")
        if expected_bytes is not None and (
            total != expected_bytes or st_fd.st_size != expected_bytes or st_end.st_size != expected_bytes
        ):
            raise JsProtocolError(
                "ryn_js_image_artifact_changed",
                f"Artifact byte length changed during read: expected {expected_bytes}, read {total}",
            )
        if total <= 0 or total != st_end.st_size:
            raise JsProtocolError("ryn_js_image_artifact_changed", "Artifact size changed while reading")

        raw_bytes = b"".join(chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    actual_sha256 = hasher.hexdigest()
    if expected_sha256 is not None:
        norm_expected = str(expected_sha256).strip().lower()
        if not SHA256_HEX_RE.fullmatch(norm_expected):
            raise JsProtocolError("ryn_js_image_invalid", "expected_sha256 must be 64 lowercase hex characters")
        if actual_sha256 != norm_expected:
            raise JsProtocolError(
                "ryn_js_image_artifact_changed",
                f"Artifact SHA-256 mismatch: expected {norm_expected}, got {actual_sha256}",
            )

    width, height = _validate_png_structure_and_header(
        raw_bytes,
        expected_width=expected_width,
        expected_height=expected_height,
        max_pixels=max_pixels,
    )
    return raw_bytes, actual_sha256, width, height


@dataclass(frozen=True)
class HostImageIssuanceContext:
    """Exact host execution, callback, producer, and native WindowRef context required to mint a screenshot capability."""

    owner_id: str
    node_runtime_id: str
    task_id: str
    session_id: str
    generation: int
    turn: int
    execution_id: str
    callback_id: str
    request_id: str
    window: dict[str, Any]
    operation_id: str = ""
    context_epoch: str = "epoch_1"
    selection_scope: str = "default"
    native_worker_instance: str = ""
    native_window: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.owner_id, "owner_id")
        validate_identifier(self.node_runtime_id, "node_runtime_id")
        validate_identifier(self.task_id, "task_id")
        validate_identifier(self.session_id, "session_id")
        if type(self.generation) is not int or self.generation < 1:
            raise JsProtocolError("ryn_js_invalid_generation", "generation must be an integer >= 1")
        if type(self.turn) is not int or self.turn < 1:
            raise JsProtocolError("ryn_js_invalid_turn", "turn must be an integer >= 1")
        validate_identifier(self.execution_id, "execution_id")
        validate_identifier(self.callback_id, "callback_id")
        validate_identifier(self.request_id, "request_id")
        if self.operation_id:
            validate_identifier(self.operation_id, "operation_id")
        validate_identifier(self.context_epoch, "context_epoch")
        validate_identifier(self.selection_scope, "selection_scope")
        worker_inst = str(self.native_worker_instance or "").strip()
        if worker_inst:
            worker_inst = validate_identifier(worker_inst, "native_worker_instance")
        object.__setattr__(self, "native_worker_instance", worker_inst)

        norm_win, _ = normalize_window_identity(self.window)
        object.__setattr__(self, "window", norm_win)

        raw_native_win: dict[str, Any] | None = None
        if self.native_window is not None:
            if not isinstance(self.native_window, dict):
                raise JsProtocolError("ryn_js_image_window_mismatch", "native_window must be a WindowRef dictionary")
            raw_native_win = self.native_window
        elif isinstance(self.window, dict) and any(
            k in self.window for k in ("workerInstance", "windowKey", "hwnd", "pid", "processStarted")
        ):
            raw_native_win = self.window

        if raw_native_win is not None:
            if not worker_inst:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    "HostImageIssuanceContext requires explicit native_worker_instance when native_window is provided",
                )
            norm_native_win = normalize_native_window_ref(
                raw_native_win,
                fallback_window=self.window,
                expected_worker_instance=worker_inst,
            )
            object.__setattr__(self, "native_window", norm_native_win)


@dataclass(frozen=True)
class VerifiedHostImageArtifact:
    """Immutable verified host PNG artifact reference (stores handle/path metadata, never pixel arrays)."""

    artifact_id: str
    task_id: str
    operation_id: str
    receipt_id: str
    native_worker_instance: str
    window: dict[str, Any]
    state_token: str
    observation_id: str
    sha256: str
    byte_length: int
    width: int
    height: int
    mime_type: str
    origin_x: int
    origin_y: int
    z_index: int
    screenshot_id: str
    artifact_path: Path
    containment_root: Path
    expected_parent: Path | None = None
    provenance_kind: str = "production_native_receipt"
    native_window: dict[str, Any] = field(default_factory=dict)
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ARTIFACT_SEAL_SENTINEL:
            raise JsProtocolError(
                "ryn_js_image_untrusted",
                "VerifiedHostImageArtifact must be constructed via seal_verified_host_artifact",
            )

    def to_outbox_metadata(self) -> dict[str, Any]:
        """Return a durable outbox-safe representation without capability URLs, tokens, paths, or base64."""
        return {
            "id": self.screenshot_id,
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "native_worker_instance": self.native_worker_instance,
            "observation_id": self.observation_id,
            "stateToken": self.state_token,
            "sha256": self.sha256,
            "mimeType": self.mime_type,
            "byteLength": self.byte_length,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "zIndex": self.z_index,
            "window": copy.deepcopy(self.window),
            "native_window": copy.deepcopy(self.native_window),
            "provenance_kind": self.provenance_kind,
        }


def seal_verified_host_artifact(
    *,
    artifact_id: str,
    task_id: str,
    operation_id: str,
    receipt_id: str,
    native_worker_instance: str,
    window: dict[str, Any],
    native_window: dict[str, Any] | None = None,
    state_token: str,
    observation_id: str,
    sha256: str,
    byte_length: int,
    width: int,
    height: int,
    artifact_path: str | Path,
    containment_root: str | Path,
    expected_parent: str | Path | None = None,
    mime_type: str = "image/png",
    origin_x: int = 0,
    origin_y: int = 0,
    z_index: int = 0,
    screenshot_id: str = "screenshot-0",
    provenance_kind: str = "production_native_receipt",
    max_native_bytes: int = MAX_NATIVE_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> VerifiedHostImageArtifact:
    """Validate and seal a host-owned PNG artifact without caching raw image bytes in memory."""
    aid = validate_identifier(artifact_id, "artifact_id")
    tid = validate_identifier(task_id, "task_id")
    op_id = validate_identifier(operation_id, "operation_id")
    rec_id = validate_identifier(receipt_id, "receipt_id")
    worker_inst = validate_identifier(native_worker_instance, "native_worker_instance")
    st_tok = validate_identifier(state_token, "state_token")
    obs_id = validate_identifier(observation_id, "observation_id")
    shot_id = validate_identifier(screenshot_id, "screenshot_id")
    norm_win, _ = normalize_window_identity(window)
    norm_native_win = normalize_native_window_ref(
        native_window,
        fallback_window=window,
        expected_worker_instance=worker_inst,
    )

    if provenance_kind not in ALLOWED_ARTIFACT_PROVENANCE_KINDS:
        raise JsProtocolError(
            "ryn_js_image_untrusted",
            f"Unsupported artifact provenance_kind: {provenance_kind!r}",
        )
    if str(mime_type).strip().lower() != "image/png":
        raise JsProtocolError("ryn_js_image_invalid_png", "Host screenshot artifacts must be image/png")
    norm_sha = str(sha256 or "").strip().lower()
    if not SHA256_HEX_RE.fullmatch(norm_sha):
        raise JsProtocolError("ryn_js_image_invalid", "sha256 must be 64 lowercase hex characters")
    if type(byte_length) is not int or byte_length <= 0:
        raise JsProtocolError("ryn_js_image_invalid", "byte_length must be a positive integer")
    if byte_length > max_native_bytes:
        raise JsProtocolError(
            "ryn_js_image_too_large",
            f"Artifact byte_length {byte_length} exceeds cap {max_native_bytes}",
        )
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise JsProtocolError("ryn_js_image_invalid", "width and height must be positive integers")
    if width * height > max_pixels:
        raise JsProtocolError(
            "ryn_js_image_dimensions_exceeded",
            f"Artifact dimensions {width}x{height} exceed pixel cap {max_pixels}",
        )
    if any(type(v) is not int for v in (origin_x, origin_y, z_index)):
        raise JsProtocolError("ryn_js_image_invalid", "origin_x, origin_y, and z_index must be integers")

    target_path = Path(artifact_path)
    root_path = Path(containment_root)
    parent_path = Path(expected_parent) if expected_parent is not None else None

    _, actual_sha, actual_w, actual_h = revalidate_artifact_png_bytes(
        artifact_path=target_path,
        containment_root=root_path,
        expected_parent=parent_path,
        expected_sha256=norm_sha,
        expected_bytes=byte_length,
        expected_width=width,
        expected_height=height,
        max_bytes=max_native_bytes,
        max_pixels=max_pixels,
    )

    return VerifiedHostImageArtifact(
        artifact_id=aid,
        task_id=tid,
        operation_id=op_id,
        receipt_id=rec_id,
        native_worker_instance=worker_inst,
        window=norm_win,
        native_window=norm_native_win,
        state_token=st_tok,
        observation_id=obs_id,
        sha256=actual_sha,
        byte_length=byte_length,
        width=actual_w,
        height=actual_h,
        mime_type="image/png",
        origin_x=origin_x,
        origin_y=origin_y,
        z_index=z_index,
        screenshot_id=shot_id,
        artifact_path=target_path.resolve(strict=True),
        containment_root=root_path.resolve(strict=True),
        expected_parent=parent_path.resolve(strict=True) if parent_path is not None else None,
        provenance_kind=provenance_kind,
        _seal=_ARTIFACT_SEAL_SENTINEL,
    )


HostArtifactVerifier = Callable[[HostImageIssuanceContext, Any], VerifiedHostImageArtifact]


@dataclass(frozen=True)
class HostImageBinding:
    """Authoritative host registry binding for a single issued capability token."""

    owner_id: str
    node_runtime_id: str
    task_id: str
    session_id: str
    generation: int
    turn: int
    context_epoch: str
    selection_scope: str
    issued_execution_id: str
    callback_id: str
    request_id: str
    operation_id: str
    receipt_id: str
    native_worker_instance: str
    observation_id: str
    window_key: str
    window: dict[str, Any]
    state_token: str
    screenshot_id: str
    token: str
    capability_url: str
    artifact_id: str
    sha256: str
    mime_type: str
    byte_length: int
    width: int
    height: int
    origin_x: int
    origin_y: int
    z_index: int
    artifact: VerifiedHostImageArtifact
    native_window: dict[str, Any] = field(default_factory=dict)

    def to_guest_descriptor(self) -> dict[str, Any]:
        """Build the bounded guest-facing screenshot descriptor with opaque capability URL."""
        descriptor = {
            "id": self.screenshot_id,
            "url": self.capability_url,
            "sha256": self.sha256,
            "mimeType": self.mime_type,
            "byteLength": self.byte_length,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "zIndex": self.z_index,
            "stateToken": self.state_token,
        }
        canonical_json_bytes(descriptor, max_bytes=MAX_SCREENSHOT_DESCRIPTOR_BYTES)
        return descriptor

    def to_public_metadata(self) -> dict[str, Any]:
        """Return allowlisted metadata with no capability URL, token, filesystem path, or base64."""
        return sanitize_image_metadata({
            "id": self.screenshot_id,
            "artifact_id": self.artifact_id,
            "provenance": "host_window_state",
            "provenance_kind": self.artifact.provenance_kind,
            "observation_id": self.observation_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "native_worker_instance": self.native_worker_instance,
            "issued_execution_id": self.issued_execution_id,
            "issued_callback_id": self.callback_id,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "byte_length": self.byte_length,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "zIndex": self.z_index,
            "stateToken": self.state_token,
            "window": copy.deepcopy(self.window),
            "native_window": copy.deepcopy(self.native_window),
        })


@dataclass
class _RegistryCapabilityState:
    binding: HostImageBinding
    retired: bool = False
    retired_reason: str | None = None
    active_pins: int = 0


@dataclass
class ImagePromotionRequest:
    """Transient promotion request queued when guest emits a validated host screenshot descriptor."""

    promotion_id: str
    owner_id: str
    task_id: str
    session_id: str
    generation: int
    turn: int
    context_epoch: str
    selection_scope: str
    emitting_execution_id: str
    issued_execution_id: str
    issued_callback_id: str
    operation_id: str
    receipt_id: str
    observation_id: str
    artifact_id: str
    screenshot_id: str
    provenance: str
    provenance_kind: str
    token: str = field(repr=False)
    window: dict[str, Any] = field(default_factory=dict)
    native_window: dict[str, Any] = field(default_factory=dict)
    native_worker_instance: str = ""
    state_token: str = ""
    sha256: str = ""
    mime_type: str = "image/png"
    byte_length: int = 0
    width: int = 0
    height: int = 0
    origin_x: int = 0
    origin_y: int = 0
    z_index: int = 0
    emission_ordinal: int = 1
    stage: str = "requested"
    disposition: str = "pending"
    pinned_by: str | None = None
    retired: bool = False
    retired_reason: str | None = None
    error_code: str | None = None
    consumed: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return sanitize_image_metadata({
            "promotion_id": self.promotion_id,
            "id": self.screenshot_id,
            "artifact_id": self.artifact_id,
            "provenance": self.provenance,
            "provenance_kind": self.provenance_kind,
            "observation_id": self.observation_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "native_worker_instance": self.native_worker_instance,
            "emitting_execution_id": self.emitting_execution_id,
            "issued_execution_id": self.issued_execution_id,
            "issued_callback_id": self.issued_callback_id,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "byte_length": self.byte_length,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "zIndex": self.z_index,
            "stateToken": self.state_token,
            "window": copy.deepcopy(self.window),
            "native_window": copy.deepcopy(self.native_window),
            "promotion_stage": self.stage,
            "disposition": self.disposition,
            **({"error_code": self.error_code} if self.error_code else {}),
            **({"reason": self.retired_reason} if self.retired_reason else {}),
        })


@dataclass(frozen=True)
class PromotedImageAttachment:
    """Validated artifact-backed PNG attachment delivered only to the outer parent carrier assembler."""

    promotion_id: str
    artifact_id: str
    task_id: str
    session_id: str
    generation: int
    turn: int
    emitting_execution_id: str
    issued_execution_id: str
    issued_callback_id: str
    operation_id: str
    receipt_id: str
    observation_id: str
    screenshot_id: str
    provenance: str
    provenance_kind: str
    window: dict[str, Any]
    state_token: str
    sha256: str
    mime_type: str
    byte_length: int
    width: int
    height: int
    origin_x: int
    origin_y: int
    z_index: int
    emission_ordinal: int
    png_bytes: bytes = field(repr=False)
    native_window: dict[str, Any] = field(default_factory=dict)
    native_worker_instance: str = ""

    def to_public_metadata(self) -> dict[str, Any]:
        return sanitize_image_metadata({
            "promotion_id": self.promotion_id,
            "id": self.screenshot_id,
            "artifact_id": self.artifact_id,
            "provenance": self.provenance,
            "provenance_kind": self.provenance_kind,
            "observation_id": self.observation_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "native_worker_instance": self.native_worker_instance,
            "emitting_execution_id": self.emitting_execution_id,
            "issued_execution_id": self.issued_execution_id,
            "issued_callback_id": self.issued_callback_id,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "byte_length": self.byte_length,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "zIndex": self.z_index,
            "stateToken": self.state_token,
            "window": copy.deepcopy(self.window),
            "native_window": copy.deepcopy(self.native_window),
        })


@dataclass
class ImagePromotionResult:
    """Explicit outcome of draining/promoting a queued image emission at the parent boundary."""

    promotion_id: str
    status: str  # "promoted" | "unavailable"
    disposition: str  # "promoted" | "unavailable"
    stage: str
    artifact_id: str
    screenshot_id: str
    observation_id: str
    operation_id: str
    receipt_id: str
    emitting_execution_id: str
    issued_execution_id: str
    issued_callback_id: str
    provenance: str
    provenance_kind: str
    sha256: str
    mime_type: str
    byte_length: int
    width: int
    height: int
    origin_x: int
    origin_y: int
    z_index: int
    window: dict[str, Any]
    state_token: str
    emission_ordinal: int
    native_window: dict[str, Any] = field(default_factory=dict)
    native_worker_instance: str = ""
    error_code: str | None = None
    reason: str | None = None
    carrier_receipt: dict[str, Any] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def advance_stage(self, target_stage: str, *, evidence: dict[str, Any] | None = None) -> None:
        if self.status != "promoted":
            raise JsProtocolError(
                "ryn_js_invalid_promotion_stage",
                f"Cannot advance promotion stage on unavailable image result ({self.promotion_id!r})",
            )
        validate_promotion_stage_transition(self.stage, target_stage)
        self.stage = target_stage
        if isinstance(evidence, dict):
            self.evidence.update(sanitize_snapshot_value(evidence))

    def to_public_dict(self) -> dict[str, Any]:
        return sanitize_image_metadata({
            "promotion_id": self.promotion_id,
            "status": self.status,
            "disposition": self.disposition,
            "promotion_stage": self.stage,
            "id": self.screenshot_id,
            "artifact_id": self.artifact_id,
            "provenance": self.provenance,
            "provenance_kind": self.provenance_kind,
            "observation_id": self.observation_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "native_worker_instance": self.native_worker_instance,
            "emitting_execution_id": self.emitting_execution_id,
            "issued_execution_id": self.issued_execution_id,
            "issued_callback_id": self.issued_callback_id,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "byte_length": self.byte_length,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "zIndex": self.z_index,
            "stateToken": self.state_token,
            "window": copy.deepcopy(self.window),
            "native_window": copy.deepcopy(self.native_window),
            **({"error_code": self.error_code} if self.error_code else {}),
            **({"reason": self.reason} if self.reason else {}),
            **({"evidence": copy.deepcopy(self.evidence)} if self.evidence else {}),
        })


def prepare_durable_window_state_payload(
    window_state: dict[str, Any],
    *,
    artifacts: list[VerifiedHostImageArtifact] | None = None,
    native_window: dict[str, Any] | None = None,
    native_worker_instance: str | None = None,
) -> dict[str, Any]:
    """Build a durable outbox-safe `get_window_state` payload containing artifact metadata/refs and NO tokens/URLs."""
    if not isinstance(window_state, dict):
        raise JsProtocolError("ryn_js_invalid_host_result", "get_window_state payload must be a dictionary")
    norm_win, norm_win_key = normalize_window_identity(window_state.get("window"))
    out: dict[str, Any] = {
        "window": norm_win,
        "accessibility": sanitize_snapshot_value(window_state.get("accessibility")),
    }
    raw_native = native_window if native_window is not None else window_state.get("native_window")
    worker_inst_raw = (
        native_worker_instance
        if native_worker_instance is not None
        else window_state.get("native_worker_instance")
    )
    worker_inst = str(worker_inst_raw or "").strip() if isinstance(worker_inst_raw, str) else ""
    if worker_inst:
        worker_inst = validate_identifier(worker_inst, "native_worker_instance")
        out["native_worker_instance"] = worker_inst

    has_combined_native = (
        isinstance(window_state.get("window"), dict)
        and _NATIVE_WINDOW_REQUIRED_KEYS.issubset(window_state["window"].keys())
    )
    if raw_native is not None or has_combined_native:
        if not worker_inst:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                "prepare_durable_window_state_payload requires explicit native_worker_instance when native_window is present",
            )
        out["native_window"] = normalize_native_window_ref(
            raw_native,
            fallback_window=window_state.get("window"),
            expected_worker_instance=worker_inst,
        )

    if artifacts is not None:
        safe_shots: list[dict[str, Any]] = []
        for art in artifacts:
            if not isinstance(art, VerifiedHostImageArtifact):
                raise JsProtocolError(
                    "ryn_js_image_untrusted",
                    "prepare_durable_window_state_payload requires VerifiedHostImageArtifact instances",
                )
            _, art_win_key = normalize_window_identity(art.window)
            if art_win_key != norm_win_key:
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    f"Artifact window {art_win_key!r} does not match payload window {norm_win_key!r}",
                )
            if "native_worker_instance" in out and art.native_worker_instance != out["native_worker_instance"]:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Artifact native_worker_instance {art.native_worker_instance!r} does not match payload native_worker_instance {out['native_worker_instance']!r}",
                )
            if "native_window" in out and not native_window_refs_equal(art.native_window, out["native_window"]):
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    "Artifact native_window does not match payload native_window",
                )
            safe_shots.append(art.to_outbox_metadata())
        out["screenshots"] = safe_shots
        return out

    raw_shots = window_state.get("screenshots")
    if raw_shots is None:
        out["screenshots"] = []
        return out
    if not isinstance(raw_shots, list):
        raise JsProtocolError("ryn_js_invalid_host_result", "get_window_state screenshots must be a list")

    cleaned_shots: list[dict[str, Any]] = []
    for item in raw_shots:
        if isinstance(item, VerifiedHostImageArtifact):
            _, art_win_key = normalize_window_identity(item.window)
            if art_win_key != norm_win_key:
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    f"Artifact window {art_win_key!r} does not match payload window {norm_win_key!r}",
                )
            if "native_worker_instance" in out and item.native_worker_instance != out["native_worker_instance"]:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Artifact native_worker_instance {item.native_worker_instance!r} does not match payload native_worker_instance {out['native_worker_instance']!r}",
                )
            if "native_window" in out and not native_window_refs_equal(item.native_window, out["native_window"]):
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    "Artifact native_window does not match payload native_window",
                )
            cleaned_shots.append(item.to_outbox_metadata())
        elif isinstance(item, dict):
            shot_copy = sanitize_image_metadata(item)
            for keep_key in ("task_id", "native_worker_instance"):
                if isinstance(item.get(keep_key), str) and item[keep_key].strip():
                    shot_copy[keep_key] = validate_identifier(item[keep_key].strip(), keep_key)
            if "window" in shot_copy and shot_copy["window"] is not None:
                _, shot_win_key = normalize_window_identity(shot_copy["window"])
                if shot_win_key != norm_win_key:
                    raise JsProtocolError(
                        "ryn_js_image_window_mismatch",
                        f"Screenshot window {shot_win_key!r} does not match payload window {norm_win_key!r}",
                    )
            if "native_window" in item and item.get("native_window") is not None:
                shot_worker = str(shot_copy.get("native_worker_instance") or worker_inst or "").strip()
                if not shot_worker:
                    raise JsProtocolError(
                        "ryn_js_image_scope_mismatch",
                        "Screenshot native_window requires explicit native_worker_instance",
                    )
                if worker_inst and shot_worker != worker_inst:
                    raise JsProtocolError(
                        "ryn_js_image_scope_mismatch",
                        f"Screenshot native_worker_instance {shot_worker!r} does not match payload native_worker_instance {worker_inst!r}",
                    )
                shot_copy["native_window"] = normalize_native_window_ref(
                    item["native_window"],
                    fallback_window=shot_copy.get("window") or norm_win,
                    expected_worker_instance=shot_worker,
                )
                if "native_window" in out and not native_window_refs_equal(
                    shot_copy["native_window"], out["native_window"]
                ):
                    raise JsProtocolError(
                        "ryn_js_image_window_mismatch",
                        "Screenshot native_window does not match payload native_window",
                    )
            cleaned_shots.append(shot_copy)
        else:
            raise JsProtocolError("ryn_js_invalid_host_result", "Invalid screenshot item in get_window_state payload")
    out["screenshots"] = cleaned_shots
    return out


class HostImageRegistry:
    """Per-owner trusted host image capability registry and bounded transient promotion queue."""

    def __init__(
        self,
        *,
        owner_id: str = "ryn_host_owner",
        artifact_verifier: HostArtifactVerifier | None = None,
        max_entries_per_session: int = MAX_SESSION_IMAGE_ENTRIES,
        max_active_pins: int = MAX_ACTIVE_PINNED_IMAGES,
        max_native_image_bytes: int = MAX_NATIVE_IMAGE_BYTES,
        max_session_original_bytes: int = MAX_SESSION_ORIGINAL_BYTES,
        max_total_promotion_bytes: int = MAX_TOTAL_PROMOTION_BYTES,
        max_image_pixels: int = MAX_IMAGE_PIXELS,
    ) -> None:
        self.owner_id = validate_identifier(owner_id, "owner_id")
        self._artifact_verifier = artifact_verifier
        if type(max_entries_per_session) is not int or not (1 <= max_entries_per_session <= 64):
            raise JsProtocolError("ryn_js_invalid_limits", "max_entries_per_session must be in 1..64")
        if type(max_active_pins) is not int or not (1 <= max_active_pins <= 16):
            raise JsProtocolError("ryn_js_invalid_limits", "max_active_pins must be in 1..16")
        if type(max_native_image_bytes) is not int or not (1024 <= max_native_image_bytes <= MAX_NATIVE_IMAGE_BYTES):
            raise JsProtocolError("ryn_js_invalid_limits", "max_native_image_bytes must be in 1KiB..8MiB")
        if type(max_session_original_bytes) is not int or max_session_original_bytes < max_native_image_bytes:
            raise JsProtocolError(
                "ryn_js_invalid_limits",
                "max_session_original_bytes must be >= max_native_image_bytes",
            )
        if type(max_total_promotion_bytes) is not int or max_total_promotion_bytes < 1024:
            raise JsProtocolError("ryn_js_invalid_limits", "max_total_promotion_bytes must be >= 1024")
        if type(max_image_pixels) is not int or not (1 <= max_image_pixels <= 64_000_000):
            raise JsProtocolError("ryn_js_invalid_limits", "max_image_pixels must be in 1..64000000")

        self.max_entries_per_session = max_entries_per_session
        self.max_active_pins = max_active_pins
        self.max_native_image_bytes = max_native_image_bytes
        self.max_session_original_bytes = max_session_original_bytes
        self.max_total_promotion_bytes = max_total_promotion_bytes
        self.max_image_pixels = max_image_pixels

        self._lock = threading.RLock()
        self._capabilities_by_token: dict[str, _RegistryCapabilityState] = {}
        self._session_tokens: dict[str, deque[str]] = {}
        self._promotion_queue: dict[str, ImagePromotionRequest] = {}
        self._session_promotions: dict[str, deque[str]] = {}
        self._session_promoted_bytes: dict[str, int] = {}
        self._emission_counter = 0

    def set_artifact_verifier(self, verifier: HostArtifactVerifier | None) -> None:
        with self._lock:
            self._artifact_verifier = verifier

    def _require_verifier_unlocked(self) -> HostArtifactVerifier:
        if self._artifact_verifier is None:
            raise JsProtocolError(
                "ryn_js_image_verifier_unavailable",
                "HostImageRegistry requires an explicit injected host artifact verifier",
            )
        return self._artifact_verifier

    def _active_session_entries_unlocked(self, session_id: str) -> list[_RegistryCapabilityState]:
        tokens = self._session_tokens.get(session_id, deque())
        return [
            self._capabilities_by_token[tok]
            for tok in tokens
            if tok in self._capabilities_by_token and not self._capabilities_by_token[tok].retired
        ]

    def _active_session_original_bytes_unlocked(self, session_id: str) -> int:
        return sum(st.binding.byte_length for st in self._active_session_entries_unlocked(session_id))

    def _active_pin_count_unlocked(self) -> int:
        return sum(
            1
            for req in self._promotion_queue.values()
            if req.disposition == "pinned" and not req.consumed
        )

    def _active_pinned_bytes_unlocked(self, session_id: str) -> int:
        return sum(
            req.byte_length
            for req in self._promotion_queue.values()
            if req.session_id == session_id and req.disposition == "pinned" and not req.consumed
        )

    def _remove_capability_token_unlocked(self, session_id: str, token: str) -> None:
        self._capabilities_by_token.pop(token, None)
        bucket = self._session_tokens.get(session_id)
        if bucket is not None:
            try:
                bucket.remove(token)
            except ValueError:
                pass
            if not bucket:
                self._session_tokens.pop(session_id, None)

    def _remove_promotion_request_unlocked(self, session_id: str, promotion_id: str) -> None:
        self._promotion_queue.pop(promotion_id, None)
        bucket = self._session_promotions.get(session_id)
        if bucket is not None:
            try:
                bucket.remove(promotion_id)
            except ValueError:
                pass
            if not bucket:
                self._session_promotions.pop(session_id, None)

    def _retire_capability_state_unlocked(
        self,
        cap_state: _RegistryCapabilityState,
        *,
        reason: str,
        cancel_pinned: bool,
    ) -> None:
        if not cap_state.retired:
            cap_state.retired = True
            cap_state.retired_reason = reason
        token = cap_state.binding.token
        session_id = cap_state.binding.session_id
        for req in list(self._promotion_queue.values()):
            if req.token != token or req.consumed:
                continue
            if req.disposition == "pinned":
                if cancel_pinned:
                    req.retired = True
                    req.retired_reason = reason
                    req.error_code = reason if reason.startswith("ryn_") else "ryn_js_image_retired"
            else:
                req.retired = True
                req.retired_reason = reason
                req.disposition = "unavailable"
                req.error_code = reason if reason.startswith("ryn_") else "ryn_js_image_retired"
        if cap_state.active_pins <= 0:
            self._remove_capability_token_unlocked(session_id, token)

    def _evict_oldest_unpinned_in_session_unlocked(self, session_id: str, *, reason: str) -> bool:
        tokens = self._session_tokens.get(session_id)
        if not tokens:
            return False
        for tok in list(tokens):
            cap_state = self._capabilities_by_token.get(tok)
            if cap_state is None:
                self._remove_capability_token_unlocked(session_id, tok)
                continue
            if cap_state.retired:
                if cap_state.active_pins <= 0:
                    self._remove_capability_token_unlocked(session_id, tok)
                continue
            if cap_state.active_pins == 0:
                self._retire_capability_state_unlocked(
                    cap_state,
                    reason=reason,
                    cancel_pinned=False,
                )
                return True
        return False

    def issue_screenshot_capability(
        self,
        candidate: Any,
        *,
        issuance_context: HostImageIssuanceContext,
    ) -> HostImageBinding:
        """Verify a host artifact via the injected verifier and mint a transient capability binding."""
        if not isinstance(issuance_context, HostImageIssuanceContext):
            raise JsProtocolError("ryn_js_image_invalid", "issuance_context must be a HostImageIssuanceContext")
        if issuance_context.owner_id != self.owner_id:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                f"Issuance owner_id {issuance_context.owner_id!r} does not match registry owner {self.owner_id!r}",
            )
        if not issuance_context.operation_id:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                "Issuance context requires an explicit committed operation_id",
            )
        if not issuance_context.native_worker_instance:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                "Issuance context requires an explicit native_worker_instance",
            )
        if not isinstance(issuance_context.native_window, dict):
            raise JsProtocolError(
                "ryn_js_image_window_mismatch",
                "Issuance context requires an explicit validated native_window WindowRef",
            )

        with self._lock:
            verifier = self._require_verifier_unlocked()

        ctx_win, ctx_win_key = normalize_window_identity(issuance_context.window)
        if isinstance(candidate, dict):
            if candidate.get("task_id") is not None and candidate.get("task_id") != issuance_context.task_id:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Candidate task_id {candidate.get('task_id')!r} does not match issuance task_id {issuance_context.task_id!r}",
                )
            if candidate.get("operation_id") is not None and candidate.get("operation_id") != issuance_context.operation_id:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Candidate operation_id {candidate.get('operation_id')!r} does not match callback operation_id {issuance_context.operation_id!r}",
                )
            if (
                candidate.get("native_worker_instance") is not None
                and candidate.get("native_worker_instance") != issuance_context.native_worker_instance
            ):
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    f"Candidate native_worker_instance {candidate.get('native_worker_instance')!r} does not match issuance native_worker_instance {issuance_context.native_worker_instance!r}",
                )
            if candidate.get("window") is not None:
                _, cand_win_key = normalize_window_identity(candidate["window"])
                if cand_win_key != ctx_win_key:
                    raise JsProtocolError(
                        "ryn_js_image_window_mismatch",
                        f"Candidate window {cand_win_key!r} does not match requested window {ctx_win_key!r}",
                    )
            if candidate.get("native_window") is not None:
                cand_nat_win = normalize_native_window_ref(
                    candidate["native_window"],
                    fallback_window=candidate.get("window"),
                    expected_worker_instance=issuance_context.native_worker_instance,
                )
                if not native_window_refs_equal(cand_nat_win, issuance_context.native_window):
                    raise JsProtocolError(
                        "ryn_js_image_window_mismatch",
                        "Candidate native_window WindowRef does not match issuance_context native_window",
                    )

        artifact = verifier(issuance_context, candidate)
        if not isinstance(artifact, VerifiedHostImageArtifact):
            raise JsProtocolError(
                "ryn_js_image_untrusted",
                "Injected artifact_verifier must return a sealed VerifiedHostImageArtifact",
            )

        if artifact.task_id != issuance_context.task_id:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                f"Artifact task_id {artifact.task_id!r} does not match issuance task_id {issuance_context.task_id!r}",
            )
        if artifact.operation_id != issuance_context.operation_id:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                f"Artifact operation_id {artifact.operation_id!r} does not match callback operation_id {issuance_context.operation_id!r}",
            )
        if artifact.native_worker_instance != issuance_context.native_worker_instance:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                f"Artifact native_worker_instance {artifact.native_worker_instance!r} does not match issuance native_worker_instance {issuance_context.native_worker_instance!r}",
            )
        art_win, art_win_key = normalize_window_identity(artifact.window)
        if art_win_key != ctx_win_key:
            raise JsProtocolError(
                "ryn_js_image_window_mismatch",
                f"Artifact window {art_win_key!r} does not match requested window {ctx_win_key!r}",
            )
        if not native_window_refs_equal(artifact.native_window, issuance_context.native_window):
            raise JsProtocolError(
                "ryn_js_image_window_mismatch",
                "Artifact native_window WindowRef does not match issuance_context native_window",
            )
        if artifact.byte_length > self.max_native_image_bytes:
            raise JsProtocolError(
                "ryn_js_image_too_large",
                f"Artifact size {artifact.byte_length} exceeds registry max_native_image_bytes {self.max_native_image_bytes}",
            )
        if artifact.width * artifact.height > self.max_image_pixels:
            raise JsProtocolError(
                "ryn_js_image_dimensions_exceeded",
                f"Artifact dimensions {artifact.width}x{artifact.height} exceed max_image_pixels {self.max_image_pixels}",
            )
        if artifact.byte_length > self.max_session_original_bytes:
            raise JsProtocolError(
                "ryn_js_image_quota_exceeded",
                f"Artifact size {artifact.byte_length} exceeds session quota {self.max_session_original_bytes}",
            )

        with self._lock:
            session_id = issuance_context.session_id
            session_bucket = self._session_tokens.setdefault(session_id, deque())

            # Retire any prior observation capabilities for the same (session_id, window_key)
            for tok in list(session_bucket):
                existing = self._capabilities_by_token.get(tok)
                if (
                    existing is not None
                    and not existing.retired
                    and (
                        existing.binding.window_key == art_win_key
                        or native_window_refs_equal(existing.binding.native_window, artifact.native_window)
                    )
                    and existing.binding.observation_id != artifact.observation_id
                ):
                    self._retire_capability_state_unlocked(
                        existing,
                        reason="superseded_by_new_observation",
                        cancel_pinned=False,
                    )

            # Enforce per-session metadata entry cap (initial 16/session) and session original bytes quota
            while len(self._active_session_entries_unlocked(session_id)) >= self.max_entries_per_session:
                if not self._evict_oldest_unpinned_in_session_unlocked(
                    session_id,
                    reason="session_entry_cap_evicted",
                ):
                    raise JsProtocolError(
                        "ryn_js_image_registry_full",
                        f"Session {session_id!r} reached max_entries_per_session ({self.max_entries_per_session})",
                    )

            while (
                self._active_session_original_bytes_unlocked(session_id) + artifact.byte_length
                > self.max_session_original_bytes
            ):
                if not self._evict_oldest_unpinned_in_session_unlocked(
                    session_id,
                    reason="session_byte_quota_evicted",
                ):
                    raise JsProtocolError(
                        "ryn_js_image_quota_exceeded",
                        f"Session {session_id!r} exceeded aggregate original image quota ({self.max_session_original_bytes} bytes)",
                    )

            token = secrets.token_hex(CAPABILITY_TOKEN_BYTES)
            while token in self._capabilities_by_token:
                token = secrets.token_hex(CAPABILITY_TOKEN_BYTES)
            cap_url = f"{CAPABILITY_URL_PREFIX}{token}"

            merged_win = dict(ctx_win)
            if "title" in art_win:
                merged_win["title"] = art_win["title"]

            binding = HostImageBinding(
                owner_id=self.owner_id,
                node_runtime_id=issuance_context.node_runtime_id,
                task_id=issuance_context.task_id,
                session_id=session_id,
                generation=issuance_context.generation,
                turn=issuance_context.turn,
                context_epoch=issuance_context.context_epoch,
                selection_scope=issuance_context.selection_scope,
                issued_execution_id=issuance_context.execution_id,
                callback_id=issuance_context.callback_id,
                request_id=issuance_context.request_id,
                operation_id=artifact.operation_id,
                receipt_id=artifact.receipt_id,
                native_worker_instance=artifact.native_worker_instance,
                observation_id=artifact.observation_id,
                window_key=art_win_key,
                window=merged_win,
                native_window=copy.deepcopy(artifact.native_window),
                state_token=artifact.state_token,
                screenshot_id=artifact.screenshot_id,
                token=token,
                capability_url=cap_url,
                artifact_id=artifact.artifact_id,
                sha256=artifact.sha256,
                mime_type=artifact.mime_type,
                byte_length=artifact.byte_length,
                width=artifact.width,
                height=artifact.height,
                origin_x=artifact.origin_x,
                origin_y=artifact.origin_y,
                z_index=artifact.z_index,
                artifact=artifact,
            )
            # Validate guest descriptor fits within 4KiB bound before registering
            binding.to_guest_descriptor()

            session_bucket = self._session_tokens.setdefault(session_id, deque())
            self._capabilities_by_token[token] = _RegistryCapabilityState(binding=binding)
            session_bucket.append(token)
            return binding

    def bind_post_commit_window_state_result(
        self,
        durable_payload: dict[str, Any] | None = None,
        *,
        durable_outbox_payload: dict[str, Any] | None = None,
        issuance_context: HostImageIssuanceContext,
        outbox_committed: bool = False,
        native_window: dict[str, Any] | None = None,
        native_worker_instance: str | None = None,
        native_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Construct a transient `get_window_state` result with capability URLs AFTER durable outbox commit."""
        if outbox_committed is not True:
            raise JsProtocolError(
                "ryn_js_image_precommit_forbidden",
                "Cannot mint or bind host screenshot capabilities before durable outbox commit",
            )
        payload = durable_outbox_payload if durable_outbox_payload is not None else durable_payload
        if not isinstance(payload, dict):
            raise JsProtocolError("ryn_js_invalid_host_result", "get_window_state result must be a dictionary")
        if not isinstance(issuance_context, HostImageIssuanceContext):
            raise JsProtocolError("ryn_js_image_invalid", "issuance_context must be a HostImageIssuanceContext")

        if (
            native_worker_instance is not None
            and native_worker_id is not None
            and str(native_worker_instance).strip() != str(native_worker_id).strip()
        ):
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                "Conflicting native_worker_instance and native_worker_id provided",
            )
        explicit_worker = str(
            native_worker_instance if native_worker_instance is not None else (native_worker_id or "")
        ).strip()
        payload_worker = (
            str(payload.get("native_worker_instance") or "").strip()
            if isinstance(payload.get("native_worker_instance"), str)
            else ""
        )
        if explicit_worker and issuance_context.native_worker_instance and explicit_worker != issuance_context.native_worker_instance:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                f"Explicit native_worker_instance {explicit_worker!r} does not match issuance_context {issuance_context.native_worker_instance!r}",
            )
        effective_worker = explicit_worker or issuance_context.native_worker_instance
        if not effective_worker:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                "bind_post_commit_window_state_result requires explicit host native_worker_instance",
            )
        if payload_worker and payload_worker != effective_worker:
            raise JsProtocolError(
                "ryn_js_image_scope_mismatch",
                f"Durable payload native_worker_instance {payload_worker!r} does not match issuance context {effective_worker!r}",
            )

        res_win = payload.get("window") if isinstance(payload.get("window"), dict) else issuance_context.window
        norm_win, win_key = normalize_window_identity(res_win)
        _, ctx_win_key = normalize_window_identity(issuance_context.window)
        if win_key != ctx_win_key:
            raise JsProtocolError(
                "ryn_js_image_window_mismatch",
                f"get_window_state result window {win_key!r} does not match requested window {ctx_win_key!r}",
            )

        payload_native_win: dict[str, Any] | None = None
        if isinstance(payload.get("native_window"), dict):
            payload_native_win = payload["native_window"]
        elif isinstance(payload.get("window"), dict) and any(
            k in payload["window"] for k in ("workerInstance", "windowKey", "hwnd", "pid", "processStarted")
        ):
            payload_native_win = payload["window"]

        host_expected_native = native_window if native_window is not None else issuance_context.native_window
        if host_expected_native is None:
            raise JsProtocolError(
                "ryn_js_image_window_mismatch",
                "bind_post_commit_window_state_result requires explicit host native_window WindowRef",
            )
        norm_effective_native = normalize_native_window_ref(
            host_expected_native,
            fallback_window=issuance_context.window,
            expected_worker_instance=effective_worker,
        )
        if native_window is not None and issuance_context.native_window is not None:
            if not native_window_refs_equal(norm_effective_native, issuance_context.native_window):
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    "Explicit native_window does not match issuance_context.native_window",
                )
        if payload_native_win is not None:
            norm_payload_native = normalize_native_window_ref(
                payload_native_win,
                fallback_window=payload.get("window"),
                expected_worker_instance=effective_worker,
            )
            if not native_window_refs_equal(norm_payload_native, norm_effective_native):
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    "Durable payload native_window does not match issuance context native_window",
                )
        effective_native_win = norm_effective_native

        effective_ctx = HostImageIssuanceContext(
            owner_id=issuance_context.owner_id,
            node_runtime_id=issuance_context.node_runtime_id,
            task_id=issuance_context.task_id,
            session_id=issuance_context.session_id,
            generation=issuance_context.generation,
            turn=issuance_context.turn,
            execution_id=issuance_context.execution_id,
            callback_id=issuance_context.callback_id,
            request_id=issuance_context.request_id,
            window=norm_win,
            operation_id=issuance_context.operation_id,
            context_epoch=issuance_context.context_epoch,
            selection_scope=issuance_context.selection_scope,
            native_worker_instance=effective_worker,
            native_window=effective_native_win,
        )

        raw_shots = payload.get("screenshots")
        if raw_shots is None:
            return {
                "window": norm_win,
                "screenshots": [],
                "accessibility": copy.deepcopy(payload.get("accessibility")),
            }
        if not isinstance(raw_shots, list):
            raise JsProtocolError("ryn_js_invalid_host_result", "get_window_state screenshots must be a list")

        descriptors: list[dict[str, Any]] = []
        for raw_shot in raw_shots:
            if isinstance(raw_shot, dict):
                raw_url = raw_shot.get("url")
                if isinstance(raw_url, str) and raw_url.strip():
                    raise JsProtocolError(
                        "ryn_js_image_untrusted",
                        "Durable outbox payload must not contain pre-populated screenshot URLs or capability tokens",
                    )
            binding = self.issue_screenshot_capability(raw_shot, issuance_context=effective_ctx)
            descriptors.append(binding.to_guest_descriptor())

        return {
            "window": norm_win,
            "screenshots": descriptors,
            "accessibility": copy.deepcopy(payload.get("accessibility")),
        }

    def ensure_window_state_result_issued(
        self,
        result: Any,
        *,
        issuance_context: HostImageIssuanceContext,
    ) -> dict[str, Any]:
        """Ensure a `get_window_state` result carries only registry-issued descriptors for `issuance_context`."""
        if not isinstance(result, dict):
            raise JsProtocolError("ryn_js_invalid_host_result", "get_window_state result must be a dictionary")
        raw_shots = result.get("screenshots")
        if not raw_shots:
            return copy.deepcopy(result)
        if not isinstance(raw_shots, list):
            raise JsProtocolError("ryn_js_invalid_host_result", "get_window_state screenshots must be a list")

        # Verify all screenshots were already minted by bind_post_commit_window_state_result for this exact callback
        with self._lock:
            for item in raw_shots:
                if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                    raise JsProtocolError(
                        "ryn_js_image_precommit_forbidden",
                        "get_window_state screenshots must be bound via bind_post_commit_window_state_result with outbox_committed=True before responding",
                    )
                match = CAPABILITY_URL_RE.fullmatch(item["url"])
                if not match:
                    raise JsProtocolError(
                        "ryn_js_image_untrusted",
                        "Untrusted screenshot URL in get_window_state result; must use HostImageRegistry issuance",
                    )
                tok = match.group(1)
                cap_state = self._capabilities_by_token.get(tok)
                if (
                    cap_state is None
                    or cap_state.retired
                    or cap_state.binding.session_id != issuance_context.session_id
                    or cap_state.binding.generation != issuance_context.generation
                    or cap_state.binding.turn != issuance_context.turn
                    or cap_state.binding.issued_execution_id != issuance_context.execution_id
                    or cap_state.binding.callback_id != issuance_context.callback_id
                    or cap_state.binding.screenshot_id != item.get("id")
                    or cap_state.binding.sha256 != item.get("sha256")
                ):
                    raise JsProtocolError(
                        "ryn_js_image_untrusted",
                        "Screenshot capability URL does not match active callback binding in HostImageRegistry",
                    )
        return copy.deepcopy(result)

    def validate_and_queue_request(
        self,
        image_payload: dict[str, Any],
        *,
        owner_id: str,
        node_runtime_id: str,
        task_id: str,
        session_id: str,
        generation: int,
        turn: int,
        emitting_execution_id: str,
        context_epoch: str = "epoch_1",
        selection_scope: str = "default",
    ) -> dict[str, Any]:
        """Independently validate a worker `image_request` against current registry state and queue promotion."""
        if not isinstance(image_payload, dict):
            raise JsProtocolError("ryn_js_image_invalid", "image_request payload must be a JSON object")

        provenance = str(image_payload.get("provenance") or "")
        raw_url = str(image_payload.get("url") or "").strip()

        if provenance == "guest_inline_data_url":
            if any(
                k in image_payload and image_payload[k] is not None
                for k in (
                    "window",
                    "native_window",
                    "native_worker_instance",
                    "stateToken",
                    "state_token",
                    "observation_id",
                    "artifact_id",
                    "receipt_id",
                    "operation_id",
                )
            ):
                raise JsProtocolError(
                    "ryn_js_image_untrusted",
                    "guest_inline_data_url cannot claim host window, observation, or artifact provenance",
                )
            if raw_url.startswith(CAPABILITY_URL_PREFIX) or not raw_url.startswith("data:image/"):
                raise JsProtocolError(
                    "ryn_js_image_untrusted",
                    "guest_inline_data_url must be a bounded data:image URL and cannot resolve capability tokens",
                )
            b64_len = image_payload.get("base64_length") or image_payload.get("base64Length") or 0
            if type(b64_len) is not int or b64_len <= 0 or b64_len > MAX_GUEST_INLINE_BASE64_BYTES:
                raise JsProtocolError(
                    "ryn_js_image_limit_exceeded",
                    "Inline data URL image exceeds 128KB guest limit",
                )
            return sanitize_image_metadata(image_payload)

        if provenance not in {"host_window_state", "host_issued_descriptor"}:
            raise JsProtocolError("ryn_js_image_untrusted", f"Untrusted image provenance: {provenance!r}")

        match = CAPABILITY_URL_RE.fullmatch(raw_url)
        if not match:
            raise JsProtocolError(
                "ryn_js_image_untrusted",
                "Host image_request requires a valid ryn-image:cap:<256bit-hex> capability URL",
            )
        token = match.group(1)

        with self._lock:
            cap_state = self._capabilities_by_token.get(token)
            if cap_state is None:
                raise JsProtocolError("ryn_js_image_untrusted", "Unknown or forged host image capability token")
            binding = cap_state.binding

            if cap_state.retired:
                reason = cap_state.retired_reason or "ryn_js_image_retired"
                raise JsProtocolError("ryn_js_image_untrusted", f"Host image capability was retired ({reason})")

            if (
                binding.owner_id != owner_id
                or binding.node_runtime_id != node_runtime_id
                or binding.task_id != task_id
                or binding.session_id != session_id
                or binding.generation != generation
                or binding.turn != turn
            ):
                raise JsProtocolError(
                    "ryn_js_image_untrusted",
                    "Host image capability does not match current owner/runtime/task/session/generation/turn scope",
                )
            if binding.context_epoch != context_epoch or binding.selection_scope != selection_scope:
                raise JsProtocolError(
                    "ryn_js_image_scope_mismatch",
                    "Host image capability does not match current context_epoch or selection_scope",
                )

            req_id = image_payload.get("id")
            if req_id != binding.screenshot_id:
                raise JsProtocolError("ryn_js_image_untrusted", "Screenshot id does not match host registry binding")
            req_sha = image_payload.get("sha256")
            if req_sha != binding.sha256:
                raise JsProtocolError("ryn_js_image_untrusted", "Screenshot sha256 does not match host registry binding")

            req_win = image_payload.get("window")
            if not isinstance(req_win, dict):
                raise JsProtocolError("ryn_js_image_window_mismatch", "Host image_request is missing window identity")
            norm_req_win, req_win_key = normalize_window_identity(req_win)
            if req_win_key != binding.window_key:
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    "Screenshot window identity does not match host registry binding",
                )
            if "title" in norm_req_win and "title" in binding.window and norm_req_win["title"] != binding.window["title"]:
                raise JsProtocolError(
                    "ryn_js_image_window_mismatch",
                    "Screenshot window title does not match host registry binding",
                )
            if "native_window" in image_payload and image_payload["native_window"] is not None:
                if not isinstance(image_payload["native_window"], dict) or not native_window_refs_equal(
                    image_payload["native_window"],
                    binding.native_window,
                ):
                    raise JsProtocolError(
                        "ryn_js_image_window_mismatch",
                        "Screenshot native_window does not match host registry binding",
                    )
            if "native_worker_instance" in image_payload and image_payload["native_worker_instance"] is not None:
                if image_payload["native_worker_instance"] != binding.native_worker_instance:
                    raise JsProtocolError(
                        "ryn_js_image_scope_mismatch",
                        "Screenshot native_worker_instance does not match host registry binding",
                    )

            for payload_key, expected_val in (
                ("stateToken", binding.state_token),
                ("width", binding.width),
                ("height", binding.height),
                ("originX", binding.origin_x),
                ("originY", binding.origin_y),
                ("zIndex", binding.z_index),
                ("mimeType", binding.mime_type),
                ("mime_type", binding.mime_type),
                ("byteLength", binding.byte_length),
                ("byte_length", binding.byte_length),
            ):
                if payload_key in image_payload and image_payload[payload_key] is not None:
                    if image_payload[payload_key] != expected_val:
                        raise JsProtocolError(
                            "ryn_js_image_untrusted",
                            f"Screenshot field {payload_key!r} does not match host registry binding",
                        )

            session_promos = self._session_promotions.setdefault(session_id, deque())
            for pid in list(session_promos):
                existing_req = self._promotion_queue.get(pid)
                if existing_req is None or existing_req.consumed:
                    self._remove_promotion_request_unlocked(session_id, pid)

            session_promos = self._session_promotions.setdefault(session_id, deque())
            if len(session_promos) >= MAX_PROMOTION_QUEUE_ENTRIES:
                for pid in list(session_promos):
                    existing_req = self._promotion_queue.get(pid)
                    if existing_req is not None and existing_req.retired and existing_req.disposition != "pinned":
                        self._remove_promotion_request_unlocked(session_id, pid)
                        if len(self._session_promotions.get(session_id, ())) < MAX_PROMOTION_QUEUE_ENTRIES:
                            break

            session_promos = self._session_promotions.setdefault(session_id, deque())
            active_unconsumed = [
                pid for pid in session_promos if pid in self._promotion_queue and not self._promotion_queue[pid].consumed
            ]
            if len(active_unconsumed) >= MAX_PROMOTION_QUEUE_ENTRIES:
                raise JsProtocolError("ryn_js_image_limit_exceeded", "Session transient image promotion queue is full")

            self._emission_counter += 1
            promo_id = f"promo_{generation}_{turn}_{self._emission_counter}_{uuid.uuid4().hex[:8]}"
            promo_req = ImagePromotionRequest(
                promotion_id=promo_id,
                owner_id=self.owner_id,
                task_id=task_id,
                session_id=session_id,
                generation=generation,
                turn=turn,
                context_epoch=context_epoch,
                selection_scope=selection_scope,
                emitting_execution_id=emitting_execution_id,
                issued_execution_id=binding.issued_execution_id,
                issued_callback_id=binding.callback_id,
                operation_id=binding.operation_id,
                receipt_id=binding.receipt_id,
                observation_id=binding.observation_id,
                artifact_id=binding.artifact_id,
                screenshot_id=binding.screenshot_id,
                provenance=provenance,
                provenance_kind=binding.artifact.provenance_kind,
                token=token,
                window=copy.deepcopy(binding.window),
                native_window=copy.deepcopy(binding.native_window),
                native_worker_instance=binding.native_worker_instance,
                state_token=binding.state_token,
                sha256=binding.sha256,
                mime_type=binding.mime_type,
                byte_length=binding.byte_length,
                width=binding.width,
                height=binding.height,
                origin_x=binding.origin_x,
                origin_y=binding.origin_y,
                z_index=binding.z_index,
                emission_ordinal=self._emission_counter,
                stage="requested",
                disposition="pending",
            )
            self._promotion_queue[promo_id] = promo_req
            session_promos.append(promo_id)
            return promo_req.to_public_dict()

    def pin_promotion_request(
        self,
        promotion_id: str,
        *,
        parent_request_id: str,
        task_id: str,
        session_id: str,
        generation: int,
        emitting_execution_id: str,
    ) -> ImagePromotionRequest:
        """Pin a queued promotion request for the exact current parent emission/result before draining."""
        parent_id = validate_identifier(parent_request_id, "parent_request_id")
        with self._lock:
            req = self._promotion_queue.get(promotion_id)
            if req is None:
                raise JsProtocolError("ryn_js_image_unknown_promotion", f"Unknown promotion_id {promotion_id!r}")
            if (
                req.task_id != task_id
                or req.session_id != session_id
                or req.generation != generation
                or req.emitting_execution_id != emitting_execution_id
            ):
                raise JsProtocolError(
                    "ryn_js_image_pin_mismatch",
                    "Promotion pin scope does not match exact task/session/generation/emitting_execution_id",
                )
            if req.consumed:
                raise JsProtocolError("ryn_js_image_already_consumed", f"Promotion {promotion_id!r} was already consumed")
            if req.disposition == "pinned":
                if req.pinned_by != parent_id:
                    raise JsProtocolError("ryn_js_image_pin_mismatch", f"Promotion {promotion_id!r} is already pinned")
                return req

            cap_state = self._capabilities_by_token.get(req.token)
            if req.retired or cap_state is None or cap_state.retired:
                reason = (
                    req.retired_reason
                    or (cap_state.retired_reason if cap_state is not None else None)
                    or "ryn_js_image_retired"
                )
                req.retired = True
                req.retired_reason = reason
                req.disposition = "unavailable"
                req.error_code = reason if reason.startswith("ryn_") else "ryn_js_image_retired"
                raise JsProtocolError(req.error_code, f"Cannot pin retired image capability ({reason})")

            if self._active_pin_count_unlocked() >= self.max_active_pins:
                raise JsProtocolError(
                    "ryn_js_image_pin_limit_exceeded",
                    f"Active pinned image limit ({self.max_active_pins}) exceeded",
                )

            promoted_so_far = self._session_promoted_bytes.get(session_id, 0)
            pinned_so_far = self._active_pinned_bytes_unlocked(session_id)
            if promoted_so_far + pinned_so_far + req.byte_length > self.max_total_promotion_bytes:
                req.disposition = "unavailable"
                req.error_code = "ryn_js_image_promotion_budget_exceeded"
                req.retired_reason = "Promotion byte budget exceeded"
                raise JsProtocolError(
                    "ryn_js_image_promotion_budget_exceeded",
                    f"Total promotion byte budget ({self.max_total_promotion_bytes}) exceeded",
                )

            validate_promotion_stage_transition(req.stage, "prepared")
            req.stage = "prepared"
            req.disposition = "pinned"
            req.pinned_by = parent_id
            cap_state.active_pins += 1
            return req

    def release_pinned_promotion(
        self,
        promotion_id: str,
        *,
        parent_request_id: str,
        reason: str = "ryn_js_image_pin_released",
    ) -> ImagePromotionResult:
        """Refuse/release an in-flight pinned promotion request and return an explicit unavailable result."""
        parent_id = validate_identifier(parent_request_id, "parent_request_id")
        with self._lock:
            req = self._promotion_queue.get(promotion_id)
            if req is None:
                raise JsProtocolError("ryn_js_image_unknown_promotion", f"Unknown promotion_id {promotion_id!r}")
            if req.disposition != "pinned" or req.pinned_by != parent_id:
                raise JsProtocolError(
                    "ryn_js_image_pin_mismatch",
                    f"Promotion {promotion_id!r} is not pinned by {parent_id!r}",
                )
            cap_state = self._capabilities_by_token.get(req.token)
            if cap_state is not None:
                if cap_state.active_pins > 0:
                    cap_state.active_pins -= 1
                if cap_state.retired and cap_state.active_pins <= 0:
                    self._remove_capability_token_unlocked(req.session_id, req.token)
            req.pinned_by = None
            req.consumed = True
            req.disposition = "unavailable"
            req.retired = True
            req.retired_reason = reason
            req.error_code = reason if reason.startswith("ryn_") else "ryn_js_image_pin_released"
            res = self._build_result_from_request_unlocked(
                req,
                status="unavailable",
                disposition="unavailable",
                error_code=req.error_code,
                reason=reason,
            )
            self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
            return res

    def complete_pinned_promotion(
        self,
        promotion_id: str,
        *,
        parent_request_id: str,
        task_id: str,
        session_id: str,
        generation: int,
        emitting_execution_id: str,
        carrier: Callable[[PromotedImageAttachment], Any] | None = None,
    ) -> ImagePromotionResult:
        """Complete an already-pinned promotion request by revalidating the PNG handle and invoking `carrier`."""
        parent_id = validate_identifier(parent_request_id, "parent_request_id")
        with self._lock:
            req = self._promotion_queue.get(promotion_id)
            if req is None:
                raise JsProtocolError("ryn_js_image_unknown_promotion", f"Unknown promotion_id {promotion_id!r}")
            if (
                req.task_id != task_id
                or req.session_id != session_id
                or req.generation != generation
                or req.emitting_execution_id != emitting_execution_id
            ):
                raise JsProtocolError(
                    "ryn_js_image_pin_mismatch",
                    "Promotion completion scope does not match exact task/session/generation/emitting_execution_id",
                )
            if req.consumed or req.disposition != "pinned" or req.pinned_by != parent_id:
                raise JsProtocolError(
                    "ryn_js_image_pin_mismatch",
                    f"Promotion {promotion_id!r} is not actively pinned by {parent_id!r}",
                )
            cap_state = self._capabilities_by_token.get(req.token)
            if cap_state is None or req.retired:
                reason = req.retired_reason or "ryn_js_image_retired"
                err_code = req.error_code or (reason if reason.startswith("ryn_") else "ryn_js_image_retired")
                if cap_state is not None:
                    if cap_state.active_pins > 0:
                        cap_state.active_pins -= 1
                    if cap_state.retired and cap_state.active_pins <= 0:
                        self._remove_capability_token_unlocked(req.session_id, req.token)
                req.pinned_by = None
                req.consumed = True
                req.disposition = "unavailable"
                res = self._build_result_from_request_unlocked(
                    req,
                    status="unavailable",
                    disposition="unavailable",
                    error_code=err_code,
                    reason=reason,
                )
                self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                return res
            binding = cap_state.binding

        try:
            if carrier is None:
                raise JsProtocolError(
                    "ryn_js_image_carrier_unavailable",
                    "Parent image carrier is absent; cannot promote image attachment",
                )

            raw_bytes, actual_sha, actual_w, actual_h = revalidate_artifact_png_bytes(
                artifact_path=binding.artifact.artifact_path,
                containment_root=binding.artifact.containment_root,
                expected_parent=binding.artifact.expected_parent,
                expected_sha256=binding.sha256,
                expected_bytes=binding.byte_length,
                expected_width=binding.width,
                expected_height=binding.height,
                max_bytes=self.max_native_image_bytes,
                max_pixels=self.max_image_pixels,
            )

            attachment = PromotedImageAttachment(
                promotion_id=req.promotion_id,
                artifact_id=binding.artifact_id,
                task_id=req.task_id,
                session_id=req.session_id,
                generation=req.generation,
                turn=req.turn,
                emitting_execution_id=req.emitting_execution_id,
                issued_execution_id=req.issued_execution_id,
                issued_callback_id=req.issued_callback_id,
                operation_id=binding.operation_id,
                receipt_id=binding.receipt_id,
                observation_id=binding.observation_id,
                screenshot_id=binding.screenshot_id,
                provenance=req.provenance,
                provenance_kind=binding.artifact.provenance_kind,
                window=copy.deepcopy(binding.window),
                native_window=copy.deepcopy(binding.native_window),
                native_worker_instance=binding.native_worker_instance,
                state_token=binding.state_token,
                sha256=actual_sha,
                mime_type=binding.mime_type,
                byte_length=len(raw_bytes),
                width=actual_w,
                height=actual_h,
                origin_x=binding.origin_x,
                origin_y=binding.origin_y,
                z_index=binding.z_index,
                emission_ordinal=req.emission_ordinal,
                png_bytes=raw_bytes,
            )

            carrier_out = carrier(attachment)
        except JsProtocolError as exc:
            with self._lock:
                if cap_state.active_pins > 0:
                    cap_state.active_pins -= 1
                if cap_state.retired and cap_state.active_pins <= 0:
                    self._remove_capability_token_unlocked(req.session_id, req.token)
                req.pinned_by = None
                req.consumed = True
                req.disposition = "unavailable"
                req.error_code = exc.code
                req.retired_reason = exc.message
                res = self._build_result_from_request_unlocked(
                    req,
                    status="unavailable",
                    disposition="unavailable",
                    error_code=exc.code,
                    reason=exc.message,
                )
                self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                return res
        except Exception as exc:
            with self._lock:
                if cap_state.active_pins > 0:
                    cap_state.active_pins -= 1
                if cap_state.retired and cap_state.active_pins <= 0:
                    self._remove_capability_token_unlocked(req.session_id, req.token)
                req.pinned_by = None
                req.consumed = True
                req.disposition = "unavailable"
                req.error_code = "ryn_js_image_carrier_rejected"
                req.retired_reason = str(exc)
                res = self._build_result_from_request_unlocked(
                    req,
                    status="unavailable",
                    disposition="unavailable",
                    error_code="ryn_js_image_carrier_rejected",
                    reason=str(exc),
                )
                self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                return res

        with self._lock:
            if cap_state.active_pins > 0:
                cap_state.active_pins -= 1
            if cap_state.retired and cap_state.active_pins <= 0:
                self._remove_capability_token_unlocked(req.session_id, req.token)
            req.pinned_by = None
            req.consumed = True

            if req.retired:
                reason = req.retired_reason or "ryn_js_image_retired"
                err_code = req.error_code or (reason if reason.startswith("ryn_") else "ryn_js_image_retired")
                req.disposition = "unavailable"
                res = self._build_result_from_request_unlocked(
                    req,
                    status="unavailable",
                    disposition="unavailable",
                    error_code=err_code,
                    reason=reason,
                )
                self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                return res

            carrier_ok = True
            target_stage = "prepared"
            carrier_error_code = "ryn_js_image_carrier_rejected"
            carrier_reason: str | None = None
            carrier_receipt: dict[str, Any] | None = None

            if carrier_out is False or carrier_out is None:
                carrier_ok = False
                carrier_reason = "Carrier rejected image attachment"
            elif isinstance(carrier_out, dict):
                carrier_receipt = sanitize_snapshot_value(carrier_out)
                if carrier_out.get("ok") is False or carrier_out.get("status") == "unavailable":
                    carrier_ok = False
                    carrier_error_code = str(carrier_out.get("error_code") or "ryn_js_image_carrier_rejected")
                    carrier_reason = str(carrier_out.get("reason") or "Carrier rejected image attachment")
                elif isinstance(carrier_out.get("stage"), str) and carrier_out["stage"] in PROMOTION_STAGES:
                    target_stage = carrier_out["stage"]

            if not carrier_ok:
                req.disposition = "unavailable"
                req.error_code = carrier_error_code
                req.retired_reason = carrier_reason
                res = self._build_result_from_request_unlocked(
                    req,
                    status="unavailable",
                    disposition="unavailable",
                    error_code=carrier_error_code,
                    reason=carrier_reason,
                    carrier_receipt=carrier_receipt,
                )
                self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                return res

            validate_promotion_stage_transition(req.stage, target_stage)
            req.stage = target_stage
            req.disposition = "promoted"
            self._session_promoted_bytes[session_id] = (
                self._session_promoted_bytes.get(session_id, 0) + req.byte_length
            )
            res = self._build_result_from_request_unlocked(
                req,
                status="promoted",
                disposition="promoted",
                carrier_receipt=carrier_receipt,
            )
            self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
            return res

    def drain_promotions(
        self,
        *,
        task_id: str,
        session_id: str,
        generation: int | None = None,
        execution_id: str | None = None,
        carrier: Callable[[PromotedImageAttachment], Any] | None = None,
        parent_request_id: str | None = None,
        max_promotions: int | None = None,
        max_total_bytes: int | None = None,
    ) -> list[ImagePromotionResult]:
        """Drain queued transient image promotions in original emission order for the outer parent assembler."""
        parent_id = validate_identifier(
            str(parent_request_id or f"parent_{uuid.uuid4().hex[:12]}").strip(),
            "parent_request_id",
        )
        if max_promotions is not None and (type(max_promotions) is not int or max_promotions < 1):
            raise JsProtocolError("ryn_js_invalid_limits", "max_promotions must be a positive integer")
        if max_total_bytes is not None and (type(max_total_bytes) is not int or max_total_bytes < 1):
            raise JsProtocolError("ryn_js_invalid_limits", "max_total_bytes must be a positive integer")

        with self._lock:
            promo_ids = list(self._session_promotions.get(session_id, ()))
            candidates: list[ImagePromotionRequest] = []
            for pid in promo_ids:
                req = self._promotion_queue.get(pid)
                if req is None or req.consumed:
                    continue
                if req.task_id != task_id:
                    continue
                if generation is not None and req.generation != generation:
                    continue
                if execution_id is not None and req.emitting_execution_id != execution_id:
                    continue
                candidates.append(req)

        results: list[ImagePromotionResult] = []
        promoted_count = 0
        batch_promoted_bytes = 0
        for req in candidates:
            if max_promotions is not None and promoted_count >= max_promotions:
                with self._lock:
                    req.consumed = True
                    req.disposition = "unavailable"
                    req.error_code = "ryn_js_image_limit_exceeded"
                    req.retired_reason = f"Drain max_promotions limit ({max_promotions}) exceeded"
                    results.append(
                        self._build_result_from_request_unlocked(
                            req,
                            status="unavailable",
                            disposition="unavailable",
                            error_code=req.error_code,
                            reason=req.retired_reason,
                        )
                    )
                    self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                continue

            if max_total_bytes is not None and batch_promoted_bytes + req.byte_length > max_total_bytes:
                with self._lock:
                    req.consumed = True
                    req.disposition = "unavailable"
                    req.error_code = "ryn_js_image_promotion_budget_exceeded"
                    req.retired_reason = f"Drain max_total_bytes limit ({max_total_bytes}) exceeded"
                    results.append(
                        self._build_result_from_request_unlocked(
                            req,
                            status="unavailable",
                            disposition="unavailable",
                            error_code=req.error_code,
                            reason=req.retired_reason,
                        )
                    )
                    self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                continue

            try:
                self.pin_promotion_request(
                    req.promotion_id,
                    parent_request_id=parent_id,
                    task_id=req.task_id,
                    session_id=req.session_id,
                    generation=req.generation,
                    emitting_execution_id=req.emitting_execution_id,
                )
            except JsProtocolError as exc:
                with self._lock:
                    req.consumed = True
                    req.disposition = "unavailable"
                    req.error_code = exc.code
                    req.retired_reason = exc.message
                    results.append(
                        self._build_result_from_request_unlocked(
                            req,
                            status="unavailable",
                            disposition="unavailable",
                            error_code=exc.code,
                            reason=exc.message,
                        )
                    )
                    self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                continue

            res = self.complete_pinned_promotion(
                req.promotion_id,
                parent_request_id=parent_id,
                task_id=req.task_id,
                session_id=req.session_id,
                generation=req.generation,
                emitting_execution_id=req.emitting_execution_id,
                carrier=carrier,
            )
            if res.status == "promoted":
                promoted_count += 1
                batch_promoted_bytes += res.byte_length
            results.append(res)

        return results

    def _build_result_from_request_unlocked(
        self,
        req: ImagePromotionRequest,
        *,
        status: str,
        disposition: str,
        error_code: str | None = None,
        reason: str | None = None,
        carrier_receipt: dict[str, Any] | None = None,
    ) -> ImagePromotionResult:
        return ImagePromotionResult(
            promotion_id=req.promotion_id,
            status=status,
            disposition=disposition,
            stage=req.stage,
            artifact_id=req.artifact_id,
            screenshot_id=req.screenshot_id,
            observation_id=req.observation_id,
            operation_id=req.operation_id,
            receipt_id=req.receipt_id,
            emitting_execution_id=req.emitting_execution_id,
            issued_execution_id=req.issued_execution_id,
            issued_callback_id=req.issued_callback_id,
            provenance=req.provenance,
            provenance_kind=req.provenance_kind,
            sha256=req.sha256,
            mime_type=req.mime_type,
            byte_length=req.byte_length,
            width=req.width,
            height=req.height,
            origin_x=req.origin_x,
            origin_y=req.origin_y,
            z_index=req.z_index,
            window=copy.deepcopy(req.window),
            native_window=copy.deepcopy(req.native_window),
            native_worker_instance=req.native_worker_instance,
            state_token=req.state_token,
            emission_ordinal=req.emission_ordinal,
            error_code=error_code,
            reason=reason,
            carrier_receipt=carrier_receipt,
        )

    def retire_window_observations(
        self,
        session_id: str,
        window: dict[str, Any],
        *,
        reason: str = "superseded_by_new_observation",
        cancel_pinned: bool = False,
    ) -> int:
        """Retire capabilities for a specific window in `session_id`."""
        _, win_key = normalize_window_identity(window)
        retired_count = 0
        with self._lock:
            for tok in list(self._session_tokens.get(session_id, ())):
                cap_state = self._capabilities_by_token.get(tok)
                if cap_state is not None and not cap_state.retired and cap_state.binding.window_key == win_key:
                    self._retire_capability_state_unlocked(
                        cap_state,
                        reason=reason,
                        cancel_pinned=cancel_pinned,
                    )
                    retired_count += 1
        return retired_count

    def retire_session(
        self,
        session_id: str,
        *,
        reason: str = "ryn_js_turn_ended",
        cancel_pinned: bool = True,
        reset_promotion_budget: bool = False,
    ) -> int:
        """Retire all capabilities and unfetched promotions for `session_id`."""
        retired_count = 0
        with self._lock:
            for tok in list(self._session_tokens.get(session_id, ())):
                cap_state = self._capabilities_by_token.get(tok)
                if cap_state is not None:
                    if not cap_state.retired:
                        retired_count += 1
                    self._retire_capability_state_unlocked(
                        cap_state,
                        reason=reason,
                        cancel_pinned=cancel_pinned,
                    )
            for pid in list(self._session_promotions.get(session_id, ())):
                req = self._promotion_queue.get(pid)
                if req is None or req.consumed:
                    self._remove_promotion_request_unlocked(session_id, pid)
                    continue
                if req.disposition != "pinned" or cancel_pinned:
                    req.retired = True
                    req.retired_reason = reason
                    req.error_code = reason if reason.startswith("ryn_") else "ryn_js_image_retired"
                    if req.disposition != "pinned":
                        req.disposition = "unavailable"
            if reset_promotion_budget:
                self._session_promoted_bytes[session_id] = 0
        return retired_count

    def retire_task(
        self,
        task_id: str,
        *,
        reason: str = "ryn_js_task_retired",
        cancel_pinned: bool = True,
    ) -> int:
        """Retire all capabilities and promotions across all sessions belonging to `task_id`."""
        retired_count = 0
        with self._lock:
            for cap_state in list(self._capabilities_by_token.values()):
                if cap_state.binding.task_id == task_id:
                    if not cap_state.retired:
                        retired_count += 1
                    self._retire_capability_state_unlocked(
                        cap_state,
                        reason=reason,
                        cancel_pinned=cancel_pinned,
                    )
            for req in list(self._promotion_queue.values()):
                if req.task_id == task_id:
                    if req.consumed:
                        self._remove_promotion_request_unlocked(req.session_id, req.promotion_id)
                        continue
                    if req.disposition != "pinned" or cancel_pinned:
                        req.retired = True
                        req.retired_reason = reason
                        req.error_code = reason if reason.startswith("ryn_") else "ryn_js_image_retired"
                        if req.disposition != "pinned":
                            req.disposition = "unavailable"
        return retired_count

    def get_session_pending_promotions(
        self,
        session_id: str,
        *,
        execution_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return sanitized public metadata for pending (unconsumed) promotions in `session_id`."""
        with self._lock:
            out: list[dict[str, Any]] = []
            for pid in self._session_promotions.get(session_id, ()):
                req = self._promotion_queue.get(pid)
                if req is None or req.consumed:
                    continue
                if execution_id is not None and req.emitting_execution_id != execution_id:
                    continue
                out.append(req.to_public_dict())
            return out
