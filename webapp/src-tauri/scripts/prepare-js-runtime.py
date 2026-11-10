"""Pure offline resource-profile preparer for the standalone Ryn QuickJS host runtime.

Status: UNMOUNTED standalone build preparer.
This script prepares a deterministic, self-contained Ryn JS runtime resource profile
from explicitly approved local inputs (Node.js binary + license/provenance and the
installed `native/ryn_js_runtime` package closure) and validates the resulting profile
with `rynmesh.agent_runtime.js_runtime.resources`.

Safety & Architecture Invariants:
- Pure offline local file preparation: no network, no downloads, no npm/node execution,
  no subprocesses, no lifecycle hooks, and no PATH/environment search.
- Never modifies or deletes an existing destination directory (`output_profile_dir` must
  be a brand-new directory under an existing, reparse-free parent directory).
- Never performs recursive cleanup (`shutil.rmtree`) of computed paths.
- Verifies all input files, sizes, SHA-256 digests, lockfile integrity strings, package
  export maps, and all 4 QuickJS 0.32.0 WASM variants before copying a single byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rynmesh.agent_runtime.js_runtime.resources import (  # noqa: E402
    ALLOWED_ARCHES,
    ALLOWED_PLATFORMS,
    APPROVED_WIN32_X64_NODE_BYTES,
    APPROVED_WIN32_X64_NODE_SHA256,
    APPROVED_WIN32_X64_NODE_VERSION,
    BUILD_RECEIPT_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_AGGREGATE_PROFILE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_PROFILE_FILE_COUNT,
    MAX_SINGLE_RESOURCE_FILE_BYTES,
    NODE_LICENSE_REL_PATH,
    NODE_README_REL_PATH,
    PACKAGE_JSON_REL_PATH,
    PACKAGE_LOCK_REL_PATH,
    PINNED_QUICKJS_VARIANT,
    PINNED_QUICKJS_VERSION,
    PINNED_RUNTIME_PACKAGE_NAME,
    PINNED_WIRE_PROTOCOL,
    QUICKJS_VARIANT_PACKAGES,
    RELEASE_SYNC_WASM_REL_PATH,
    REQUIRED_PRODUCTION_PACKAGES,
    SKY_SHIM_REL_PATH,
    WORKER_REL_PATH,
    ExpectedJsRuntimeProfileSpec,
    JsResourceError,
    JsRuntimeResourceManifest,
    JsRuntimeResourceProfile,
    expected_node_executable_relpath,
    is_symlink_or_reparse,
    parse_strict_bounded_json,
    read_and_verify_contained_file,
    resolve_production_js_runtime_profile,
    validate_manifest_dict,
    validate_relative_resource_path,
    validate_semver,
    validate_sha256_hex,
    verify_directory_lineage_no_reparse,
)

_FORBIDDEN_CLOSURE_FILENAMES: frozenset[str] = frozenset({
    ".npmrc",
    ".env",
    ".env.local",
    ".DS_Store",
    "Thumbs.db",
    "input-provenance.json",
})


@dataclass(frozen=True)
class _PlannedFileCopy:
    dest_rel_path: str
    source_root: Path
    source_rel_path: str
    size: int
    sha256: str
    raw_bytes: bytes


@dataclass(frozen=True)
class PreparedJsRuntimeResult:
    """Result returned by `prepare_js_runtime_profile` after offline preparation and verification."""

    output_profile_dir: Path
    manifest_path: Path
    build_receipt_path: Path
    manifest: JsRuntimeResourceManifest
    expected_spec: ExpectedJsRuntimeProfileSpec
    verified_profile: JsRuntimeResourceProfile
    build_receipt: dict[str, Any]


def _assert_prospective_leaf_absent_after_verified_parent(
    parent_resolved: Path,
    leaf_name: str,
    *,
    field_name: str = "output_profile_dir",
) -> Path:
    """Verify `leaf_name` is a single safe segment and that `parent_resolved / leaf_name` is absent via `os.lstat`.

    Must only be called AFTER `parent_resolved` has been verified as an existing, reparse-free directory.
    - Validates `leaf_name` via `validate_relative_resource_path` (rejects `/`, `\\`, `.`, `..`, `:`, ADS,
      reserved Windows device names, trailing dots/spaces, control chars).
    - If `os.lstat(target_path)` succeeds: raises `ryn_js_prepare_output_exists` (covers existing directories,
      regular files, junctions, valid symlinks, and dangling symlinks).
    - If `os.lstat(target_path)` raises `FileNotFoundError`: confirms the prospective leaf is absent.
    - If `os.lstat(target_path)` raises `PermissionError` or any other `OSError`: fails closed with
      `ryn_js_prepare_output_unverifiable` rather than treating failed `lstat` as general nonexistence.
    """
    clean_leaf = validate_relative_resource_path(leaf_name, field_name=field_name)
    if "/" in clean_leaf:
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            f"{field_name} leaf must be a single path segment: {leaf_name!r}",
        )
    target_path = parent_resolved / clean_leaf
    try:
        os.lstat(target_path)
    except FileNotFoundError:
        return target_path
    except PermissionError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_unverifiable",
            f"Permission denied while checking prospective output path {target_path}: {exc}",
        ) from exc
    except OSError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_unverifiable",
            f"Unable to verify prospective output path {target_path}: {exc}",
        ) from exc

    raise JsResourceError(
        "ryn_js_prepare_output_exists",
        f"Refusing to overwrite or modify existing output path (file, directory, or link): {target_path}",
    )


def _verify_new_output_directory_target(output_profile_dir: str | Path) -> tuple[Path, Path]:
    """Ensure `output_profile_dir` is a brand-new leaf under an existing, reparse-free parent directory."""
    if not isinstance(output_profile_dir, (str, Path)):
        raise JsResourceError("ryn_js_prepare_invalid_output", "output_profile_dir must be a string or Path")
    raw = str(output_profile_dir)
    if not raw or not raw.strip() or "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            "output_profile_dir must be a non-empty path without control characters",
        )
    norm_raw = raw.replace("/", "\\")
    if norm_raw.startswith("\\\\"):
        raise JsResourceError(
            "ryn_js_resource_unc_or_device_forbidden",
            f"output_profile_dir must not use UNC or device namespace paths: {raw!r}",
        )
    if norm_raw.endswith("\\") or raw.endswith("/"):
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            f"output_profile_dir must name a new leaf directory without a trailing slash: {raw!r}",
        )
    for idx, ch in enumerate(raw):
        if ch == ":" and not (idx == 1 and len(raw) >= 2 and raw[0].isalpha()):
            raise JsResourceError(
                "ryn_js_resource_ads_or_drive_forbidden",
                f"output_profile_dir contains forbidden colon / Alternate Data Stream syntax: {raw!r}",
            )

    # Validate EVERY raw segment (excluding the drive anchor 'X:') BEFORE os.path.abspath()
    # so Windows GetFullPathNameW cannot silently strip trailing dots/spaces or collapse '..'.
    split_parts = norm_raw.split("\\")
    start_idx = 0
    if split_parts and len(split_parts[0]) == 2 and split_parts[0][0].isalpha() and split_parts[0][1] == ":":
        start_idx = 1
        if len(split_parts) >= 2 and split_parts[1] == "":
            start_idx = 2
    elif split_parts and split_parts[0] == "":
        start_idx = 1

    raw_segments = split_parts[start_idx:]
    if not raw_segments:
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            f"output_profile_dir must include at least one directory segment: {raw!r}",
        )
    for seg_idx, segment in enumerate(raw_segments):
        if not segment:
            raise JsResourceError(
                "ryn_js_prepare_invalid_output",
                f"output_profile_dir contains an empty path segment: {raw!r}",
            )
        if segment in {".", ".."}:
            raise JsResourceError(
                "ryn_js_resource_path_traversal",
                f"output_profile_dir must not contain '.' or '..' traversal segments: {raw!r}",
            )
        validate_relative_resource_path(
            segment,
            field_name="output_profile_dir.name" if seg_idx == len(raw_segments) - 1 else "output_profile_dir.segment",
        )

    raw_leaf = raw_segments[-1]
    candidate = Path(os.path.abspath(raw))
    leaf_name = candidate.name
    if leaf_name != raw_leaf:
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            f"output_profile_dir leaf {raw_leaf!r} changed under OS path normalization to {leaf_name!r}",
        )
    validate_relative_resource_path(leaf_name, field_name="output_profile_dir.name")

    parent = candidate.parent
    try:
        parent_st = os.lstat(parent)
    except FileNotFoundError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_parent_missing",
            f"Output parent directory does not exist: {parent}",
        ) from exc
    except PermissionError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_unverifiable",
            f"Permission denied while checking output parent directory {parent}: {exc}",
        ) from exc
    except OSError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_unverifiable",
            f"Unable to stat output parent directory {parent}: {exc}",
        ) from exc

    if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            f"Output parent is not a regular directory: {parent}",
        )

    parent_resolved = verify_directory_lineage_no_reparse(parent)
    target_path = _assert_prospective_leaf_absent_after_verified_parent(
        parent_resolved,
        raw_leaf,
        field_name="output_profile_dir.name",
    )
    return parent_resolved, target_path


def _validate_node_binary_header(platform_name: str, node_bytes: bytes) -> None:
    """Perform a lightweight static header check on the Node binary without executing it."""
    if len(node_bytes) < 64:
        raise JsResourceError("ryn_js_prepare_invalid_node_binary", "Node executable is smaller than 64 bytes")
    if platform_name == "win32":
        if not node_bytes.startswith(b"MZ"):
            raise JsResourceError(
                "ryn_js_prepare_invalid_node_binary",
                "Windows Node executable does not start with a valid MZ PE header",
            )


def _collect_package_files_under_node_modules(
    *,
    runtime_pkg_root_resolved: Path,
    pkg_name: str,
) -> list[str]:
    """Walk `node_modules/<pkg_name>` without following symlinks and return sorted relative paths."""
    pkg_rel_root = f"node_modules/{pkg_name}"
    pkg_abs_root = runtime_pkg_root_resolved / Path(pkg_rel_root)
    if not pkg_abs_root.exists() or is_symlink_or_reparse(pkg_abs_root) or not pkg_abs_root.is_dir():
        raise JsResourceError(
            "ryn_js_prepare_missing_dependency",
            f"Required production dependency directory is missing or a symlink: {pkg_rel_root}",
        )

    rel_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(pkg_abs_root, topdown=True, followlinks=False):
        curr_dir = Path(dirpath)
        if is_symlink_or_reparse(curr_dir):
            raise JsResourceError(
                "ryn_js_prepare_reparse_forbidden",
                f"Dependency directory contains a symlink or reparse point: {curr_dir}",
            )
        dirnames.sort()
        for dname in dirnames:
            if dname.startswith(".") or dname == "node_modules":
                raise JsResourceError(
                    "ryn_js_prepare_unexpected_nested_dir",
                    f"Dependency {pkg_name} contains forbidden nested directory: {dname!r}",
                )
            dpath = curr_dir / dname
            if is_symlink_or_reparse(dpath):
                raise JsResourceError(
                    "ryn_js_prepare_reparse_forbidden",
                    f"Dependency subdirectory is a symlink or reparse point: {dpath}",
                )
        for fname in sorted(filenames):
            if fname in _FORBIDDEN_CLOSURE_FILENAMES or fname.endswith(".tsbuildinfo"):
                raise JsResourceError(
                    "ryn_js_prepare_forbidden_file",
                    f"Dependency {pkg_name} contains forbidden file: {fname!r}",
                )
            fpath = curr_dir / fname
            if is_symlink_or_reparse(fpath):
                raise JsResourceError(
                    "ryn_js_prepare_reparse_forbidden",
                    f"Dependency file is a symlink or reparse point: {fpath}",
                )
            rel_to_runtime = fpath.relative_to(runtime_pkg_root_resolved).as_posix()
            validate_relative_resource_path(f"package/{rel_to_runtime}", field_name=rel_to_runtime)
            rel_files.append(rel_to_runtime)

    if not rel_files:
        raise JsResourceError(
            "ryn_js_prepare_missing_dependency",
            f"Dependency directory {pkg_rel_root} contains no files",
        )
    return sorted(rel_files)


def _verify_no_extra_packages_in_source_node_modules(runtime_pkg_root_resolved: Path) -> None:
    """Ensure `native/ryn_js_runtime/node_modules` contains ONLY the exact pinned production packages."""
    nm_root = runtime_pkg_root_resolved / "node_modules"
    if not nm_root.exists() or is_symlink_or_reparse(nm_root) or not nm_root.is_dir():
        raise JsResourceError(
            "ryn_js_prepare_missing_node_modules",
            f"Source node_modules directory is missing or invalid: {nm_root}",
        )

    discovered_packages: set[str] = set()
    for entry in sorted(nm_root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name == ".package-lock.json":
            if is_symlink_or_reparse(entry):
                raise JsResourceError("ryn_js_prepare_reparse_forbidden", f"Symlink in node_modules: {entry}")
            continue
        if name.startswith("."):
            raise JsResourceError(
                "ryn_js_prepare_unexpected_package",
                f"Unexpected hidden entry in node_modules: {name!r}",
            )
        if is_symlink_or_reparse(entry) or not entry.is_dir():
            raise JsResourceError(
                "ryn_js_prepare_unexpected_package",
                f"Unexpected non-directory or symlink in node_modules: {name!r}",
            )
        if name.startswith("@"):
            for scoped_entry in sorted(entry.iterdir(), key=lambda p: p.name):
                if is_symlink_or_reparse(scoped_entry) or not scoped_entry.is_dir():
                    raise JsResourceError(
                        "ryn_js_prepare_unexpected_package",
                        f"Unexpected entry in scoped node_modules/{name}: {scoped_entry.name!r}",
                    )
                discovered_packages.add(f"{name}/{scoped_entry.name}")
        else:
            discovered_packages.add(name)

    expected_set = set(REQUIRED_PRODUCTION_PACKAGES.keys())
    extra = discovered_packages - expected_set
    missing = expected_set - discovered_packages
    if extra:
        raise JsResourceError(
            "ryn_js_prepare_unexpected_package",
            f"Source node_modules contains unpinned extra packages: {sorted(extra)}",
        )
    if missing:
        raise JsResourceError(
            "ryn_js_prepare_missing_dependency",
            f"Source node_modules is missing pinned production packages: {sorted(missing)}",
        )


def _write_exclusive_regular_file(target_path: Path, content: bytes) -> None:
    """Create a brand-new regular file with `O_CREAT | O_EXCL` and write all bytes deterministically."""
    _assert_prospective_leaf_absent_after_verified_parent(
        target_path.parent,
        target_path.name,
        field_name=target_path.name,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target_path, flags, 0o644)
    except FileExistsError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_exists",
            f"Refusing to overwrite concurrently created file during profile preparation: {target_path}",
        ) from exc
    except OSError as exc:
        raise JsResourceError(
            "ryn_js_prepare_write_failed",
            f"Failed to create output file {target_path}: {exc}",
        ) from exc
    try:
        mv = memoryview(content)
        written = 0
        while written < len(content):
            n = os.write(fd, mv[written:])
            if n <= 0:
                raise JsResourceError("ryn_js_prepare_write_failed", f"Short write to {target_path}")
            written += n
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def prepare_js_runtime_profile(
    *,
    node_executable_path: str | Path,
    expected_node_version: str = APPROVED_WIN32_X64_NODE_VERSION,
    expected_node_sha256: str = APPROVED_WIN32_X64_NODE_SHA256,
    expected_node_bytes: int | None = APPROVED_WIN32_X64_NODE_BYTES,
    runtime_package_root: str | Path,
    output_profile_dir: str | Path,
    node_license_path: str | Path | None = None,
    node_readme_path: str | Path | None = None,
    provenance_json_path: str | Path | None = None,
    platform_name: str = "win32",
    arch_name: str = "x64",
    profile_id: str | None = None,
) -> PreparedJsRuntimeResult:
    """Verify all local runtime inputs in memory, write a new resource profile, and validate it.

    Never modifies or deletes existing directories, never executes Node or npm, and never
    accesses the network.
    """
    if platform_name not in ALLOWED_PLATFORMS:
        raise JsResourceError("ryn_js_prepare_invalid_platform", f"Unsupported platform: {platform_name!r}")
    if arch_name not in ALLOWED_ARCHES:
        raise JsResourceError("ryn_js_prepare_invalid_arch", f"Unsupported arch: {arch_name!r}")
    validate_semver(expected_node_version, "expected_node_version")
    expected_node_sha256 = validate_sha256_hex(expected_node_sha256, "expected_node_sha256")
    if expected_node_bytes is not None and (
        type(expected_node_bytes) is not int
        or expected_node_bytes <= 0
        or expected_node_bytes > MAX_SINGLE_RESOURCE_FILE_BYTES
    ):
        raise JsResourceError("ryn_js_prepare_invalid_size", "expected_node_bytes must be a positive bounded int")

    resolved_profile_id = profile_id if profile_id is not None else f"{platform_name}-{arch_name}"
    _, output_target = _verify_new_output_directory_target(output_profile_dir)

    node_exec_candidate = Path(node_executable_path)
    if not node_exec_candidate.exists() or is_symlink_or_reparse(node_exec_candidate):
        raise JsResourceError(
            "ryn_js_prepare_node_missing",
            f"Approved Node executable does not exist or is a symlink: {node_exec_candidate}",
        )
    node_dir_resolved = verify_directory_lineage_no_reparse(node_exec_candidate.parent)
    node_filename = node_exec_candidate.name

    lic_candidate = Path(node_license_path) if node_license_path is not None else (node_dir_resolved / "LICENSE")
    if not lic_candidate.exists() or is_symlink_or_reparse(lic_candidate):
        raise JsResourceError(
            "ryn_js_prepare_license_missing",
            f"Node LICENSE file is missing or a symlink: {lic_candidate}",
        )
    lic_dir_resolved = verify_directory_lineage_no_reparse(lic_candidate.parent)

    readme_candidate: Path | None = None
    readme_dir_resolved: Path | None = None
    if node_readme_path is not None:
        readme_candidate = Path(node_readme_path)
        if not readme_candidate.exists() or is_symlink_or_reparse(readme_candidate):
            raise JsResourceError(
                "ryn_js_prepare_readme_missing",
                f"Specified Node README file is missing or a symlink: {readme_candidate}",
            )
        readme_dir_resolved = verify_directory_lineage_no_reparse(readme_candidate.parent)
    else:
        default_readme = node_dir_resolved / "README.md"
        if default_readme.exists() and not is_symlink_or_reparse(default_readme):
            readme_candidate = default_readme
            readme_dir_resolved = node_dir_resolved

    provenance_candidate: Path | None = None
    provenance_dir_resolved: Path | None = None
    if provenance_json_path is not None:
        provenance_candidate = Path(provenance_json_path)
        if not provenance_candidate.exists() or is_symlink_or_reparse(provenance_candidate):
            raise JsResourceError(
                "ryn_js_prepare_provenance_missing",
                f"Specified input-provenance.json is missing or a symlink: {provenance_candidate}",
            )
        provenance_dir_resolved = verify_directory_lineage_no_reparse(provenance_candidate.parent)
    else:
        default_prov = node_dir_resolved / "input-provenance.json"
        if default_prov.exists() and not is_symlink_or_reparse(default_prov):
            provenance_candidate = default_prov
            provenance_dir_resolved = node_dir_resolved

    runtime_pkg_root_resolved = verify_directory_lineage_no_reparse(runtime_package_root)
    if output_target == runtime_pkg_root_resolved or output_target.is_relative_to(runtime_pkg_root_resolved):
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            "output_profile_dir must not be inside runtime_package_root",
        )
    if output_target == node_dir_resolved or output_target.is_relative_to(node_dir_resolved):
        raise JsResourceError(
            "ryn_js_prepare_invalid_output",
            "output_profile_dir must not be inside the Node input directory",
        )

    # 1. Verify Node executable, license, optional README, and optional input-provenance.json
    node_sha, node_size, node_raw, _ = read_and_verify_contained_file(
        root_resolved=node_dir_resolved,
        rel_path=node_filename,
        expected_sha256=expected_node_sha256,
        expected_size=expected_node_bytes,
        max_bytes=MAX_SINGLE_RESOURCE_FILE_BYTES,
        retain_bytes=True,
    )
    assert node_raw is not None
    _validate_node_binary_header(platform_name, node_raw)

    node_lic_sha, node_lic_size, node_lic_raw, _ = read_and_verify_contained_file(
        root_resolved=lic_dir_resolved,
        rel_path=lic_candidate.name,
        max_bytes=MAX_SINGLE_RESOURCE_FILE_BYTES,
        retain_bytes=True,
    )
    assert node_lic_raw is not None
    if node_lic_size <= 0:
        raise JsResourceError("ryn_js_prepare_license_missing", "Node LICENSE file is empty")

    provenance_record: dict[str, Any] | None = None
    if provenance_candidate is not None and provenance_dir_resolved is not None:
        prov_sha, prov_size, prov_raw, _ = read_and_verify_contained_file(
            root_resolved=provenance_dir_resolved,
            rel_path=provenance_candidate.name,
            max_bytes=MAX_MANIFEST_BYTES,
            retain_bytes=True,
        )
        assert prov_raw is not None
        prov_obj = parse_strict_bounded_json(prov_raw, label="input-provenance.json")
        if prov_obj.get("node_sha256") != expected_node_sha256:
            raise JsResourceError(
                "ryn_js_prepare_provenance_mismatch",
                f"input-provenance.json node_sha256 {prov_obj.get('node_sha256')!r} != {expected_node_sha256!r}",
            )
        if prov_obj.get("file_version") != expected_node_version:
            raise JsResourceError(
                "ryn_js_prepare_provenance_mismatch",
                f"input-provenance.json file_version {prov_obj.get('file_version')!r} != {expected_node_version!r}",
            )
        if prov_obj.get("node_bytes") != node_size:
            raise JsResourceError(
                "ryn_js_prepare_provenance_mismatch",
                f"input-provenance.json node_bytes {prov_obj.get('node_bytes')!r} != {node_size}",
            )
        provenance_record = {
            "provenance_sha256": prov_sha,
            "provenance_bytes": prov_size,
            "archive_sha256": prov_obj.get("archive_sha256"),
            "archive_checksum_verified": prov_obj.get("archive_checksum_verified"),
            "file_version": prov_obj.get("file_version"),
            "node_sha256": prov_obj.get("node_sha256"),
            "node_bytes": prov_obj.get("node_bytes"),
            "signature_status": prov_obj.get("signature_status"),
            "signer_subject": prov_obj.get("signer_subject"),
        }

    planned_copies: dict[str, _PlannedFileCopy] = {}
    node_exec_rel = expected_node_executable_relpath(platform_name)
    planned_copies[node_exec_rel] = _PlannedFileCopy(
        dest_rel_path=node_exec_rel,
        source_root=node_dir_resolved,
        source_rel_path=node_filename,
        size=node_size,
        sha256=node_sha,
        raw_bytes=node_raw,
    )
    planned_copies[NODE_LICENSE_REL_PATH] = _PlannedFileCopy(
        dest_rel_path=NODE_LICENSE_REL_PATH,
        source_root=lic_dir_resolved,
        source_rel_path=lic_candidate.name,
        size=node_lic_size,
        sha256=node_lic_sha,
        raw_bytes=node_lic_raw,
    )

    if readme_candidate is not None and readme_dir_resolved is not None:
        rd_sha, rd_size, rd_raw, _ = read_and_verify_contained_file(
            root_resolved=readme_dir_resolved,
            rel_path=readme_candidate.name,
            max_bytes=MAX_SINGLE_RESOURCE_FILE_BYTES,
            retain_bytes=True,
        )
        assert rd_raw is not None
        if rd_size > 0:
            planned_copies[NODE_README_REL_PATH] = _PlannedFileCopy(
                dest_rel_path=NODE_README_REL_PATH,
                source_root=readme_dir_resolved,
                source_rel_path=readme_candidate.name,
                size=rd_size,
                sha256=rd_sha,
                raw_bytes=rd_raw,
            )

    # 2. Verify runtime package root files (package.json, package-lock.json, src/worker.mjs, src/sky-shim.mjs)
    for src_rel, dest_rel in (
        ("package.json", PACKAGE_JSON_REL_PATH),
        ("package-lock.json", PACKAGE_LOCK_REL_PATH),
        ("src/worker.mjs", WORKER_REL_PATH),
        ("src/sky-shim.mjs", SKY_SHIM_REL_PATH),
    ):
        f_sha, f_size, f_raw, _ = read_and_verify_contained_file(
            root_resolved=runtime_pkg_root_resolved,
            rel_path=src_rel,
            max_bytes=MAX_SINGLE_RESOURCE_FILE_BYTES,
            retain_bytes=True,
        )
        assert f_raw is not None
        if f_size <= 0:
            raise JsResourceError("ryn_js_prepare_empty_file", f"Runtime source file {src_rel} is empty")
        planned_copies[dest_rel] = _PlannedFileCopy(
            dest_rel_path=dest_rel,
            source_root=runtime_pkg_root_resolved,
            source_rel_path=src_rel,
            size=f_size,
            sha256=f_sha,
            raw_bytes=f_raw,
        )

    # Ensure no extra files exist in runtime_package_root/src
    src_dir = runtime_pkg_root_resolved / "src"
    if not src_dir.is_dir() or is_symlink_or_reparse(src_dir):
        raise JsResourceError("ryn_js_prepare_invalid_source", "runtime_package_root/src is missing or invalid")
    src_entries = {p.name for p in src_dir.iterdir()}
    if src_entries != {"worker.mjs", "sky-shim.mjs"}:
        raise JsResourceError(
            "ryn_js_prepare_unexpected_source_file",
            f"runtime_package_root/src must contain only worker.mjs and sky-shim.mjs, found {sorted(src_entries)}",
        )

    # Verify root package.json and package-lock.json
    root_pkg_obj = parse_strict_bounded_json(
        planned_copies[PACKAGE_JSON_REL_PATH].raw_bytes,
        label=PACKAGE_JSON_REL_PATH,
    )
    if root_pkg_obj.get("name") != PINNED_RUNTIME_PACKAGE_NAME or root_pkg_obj.get("type") != "module":
        raise JsResourceError(
            "ryn_js_prepare_package_mismatch",
            f"Runtime package.json must be ESM package {PINNED_RUNTIME_PACKAGE_NAME!r}",
        )
    if "scripts" in root_pkg_obj and root_pkg_obj["scripts"]:
        raise JsResourceError(
            "ryn_js_prepare_lifecycle_hooks_forbidden",
            "Runtime package.json must not define lifecycle scripts",
        )
    if root_pkg_obj.get("dependencies") != {"quickjs-emscripten": PINNED_QUICKJS_VERSION}:
        raise JsResourceError(
            "ryn_js_prepare_package_mismatch",
            f"Runtime package.json must depend strictly on quickjs-emscripten@{PINNED_QUICKJS_VERSION}",
        )

    lock_obj = parse_strict_bounded_json(
        planned_copies[PACKAGE_LOCK_REL_PATH].raw_bytes,
        label=PACKAGE_LOCK_REL_PATH,
    )
    if lock_obj.get("lockfileVersion") != 3 or not isinstance(lock_obj.get("packages"), dict):
        raise JsResourceError("ryn_js_prepare_lockfile_invalid", "package-lock.json must be lockfileVersion 3")
    lock_packages: dict[str, Any] = lock_obj["packages"]

    # 3. Verify node_modules has no extra packages and collect all files in the 7 production packages
    _verify_no_extra_packages_in_source_node_modules(runtime_pkg_root_resolved)

    dependency_entries_raw: list[dict[str, Any]] = []
    license_entries_raw: list[dict[str, Any]] = [
        {
            "component": "node",
            "version": expected_node_version,
            "license": "Node.js",
            "path": NODE_LICENSE_REL_PATH,
            "sha256": node_lic_sha,
            "size": node_lic_size,
        }
    ]

    for pkg_name in sorted(REQUIRED_PRODUCTION_PACKAGES.keys()):
        expected_ver = REQUIRED_PRODUCTION_PACKAGES[pkg_name]
        lock_key = f"node_modules/{pkg_name}"
        lock_entry = lock_packages.get(lock_key)
        if not isinstance(lock_entry, dict):
            raise JsResourceError(
                "ryn_js_prepare_lockfile_invalid",
                f"package-lock.json is missing entry for {lock_key!r}",
            )
        if lock_entry.get("version") != expected_ver:
            raise JsResourceError(
                "ryn_js_prepare_version_mismatch",
                f"Lockfile version for {pkg_name} is {lock_entry.get('version')!r}, expected {expected_ver!r}",
            )
        integrity = lock_entry.get("integrity")
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            raise JsResourceError(
                "ryn_js_prepare_lockfile_invalid",
                f"Lockfile integrity missing or invalid for {pkg_name}",
            )
        lock_license = lock_entry.get("license")
        if not isinstance(lock_license, str) or not lock_license.strip():
            raise JsResourceError(
                "ryn_js_prepare_license_missing",
                f"Lockfile license missing for {pkg_name}",
            )

        pkg_rel_files = _collect_package_files_under_node_modules(
            runtime_pkg_root_resolved=runtime_pkg_root_resolved,
            pkg_name=pkg_name,
        )
        for src_rel in pkg_rel_files:
            dest_rel = f"package/{src_rel}"
            f_sha, f_size, f_raw, _ = read_and_verify_contained_file(
                root_resolved=runtime_pkg_root_resolved,
                rel_path=src_rel,
                max_bytes=MAX_SINGLE_RESOURCE_FILE_BYTES,
                retain_bytes=True,
            )
            assert f_raw is not None
            if f_size <= 0:
                raise JsResourceError("ryn_js_prepare_empty_file", f"Dependency file {src_rel} is empty")
            if dest_rel.endswith(".wasm"):
                if len(f_raw) < 8 or f_raw[:8] != b"\x00asm\x01\x00\x00\x00":
                    raise JsResourceError(
                        "ryn_js_prepare_invalid_wasm",
                        f"WASM file {src_rel} does not start with valid WebAssembly magic bytes",
                    )
            planned_copies[dest_rel] = _PlannedFileCopy(
                dest_rel_path=dest_rel,
                source_root=runtime_pkg_root_resolved,
                source_rel_path=src_rel,
                size=f_size,
                sha256=f_sha,
                raw_bytes=f_raw,
            )

        pkg_json_dest_rel = f"package/node_modules/{pkg_name}/package.json"
        lic_dest_rel = f"package/node_modules/{pkg_name}/LICENSE"
        if pkg_json_dest_rel not in planned_copies:
            raise JsResourceError("ryn_js_prepare_missing_dependency", f"Missing {pkg_json_dest_rel}")
        if lic_dest_rel not in planned_copies:
            raise JsResourceError("ryn_js_prepare_license_missing", f"Missing {lic_dest_rel}")

        lic_copy = planned_copies[lic_dest_rel]
        dependency_entries_raw.append({
            "name": pkg_name,
            "version": expected_ver,
            "integrity": integrity,
            "license": lock_license.strip(),
            "package_json_path": pkg_json_dest_rel,
            "license_path": lic_dest_rel,
        })
        license_entries_raw.append({
            "component": pkg_name,
            "version": expected_ver,
            "license": lock_license.strip(),
            "path": lic_dest_rel,
            "sha256": lic_copy.sha256,
            "size": lic_copy.size,
        })

    # 4. Enforce total file count and aggregate byte bounds BEFORE creating any directory or copying bytes
    sorted_rel_paths = sorted(planned_copies.keys())
    total_files = len(sorted_rel_paths)
    if total_files <= 0 or total_files > MAX_PROFILE_FILE_COUNT:
        raise JsResourceError(
            "ryn_js_prepare_file_count_exceeded",
            f"Planned file count {total_files} exceeds limit {MAX_PROFILE_FILE_COUNT}",
        )
    total_bytes = sum(planned_copies[p].size for p in sorted_rel_paths)
    if total_bytes <= 0 or total_bytes > MAX_AGGREGATE_PROFILE_BYTES:
        raise JsResourceError(
            "ryn_js_prepare_aggregate_bytes_exceeded",
            f"Planned aggregate size {total_bytes} bytes exceeds limit {MAX_AGGREGATE_PROFILE_BYTES}",
        )

    if RELEASE_SYNC_WASM_REL_PATH not in planned_copies:
        raise JsResourceError(
            "ryn_js_prepare_wasm_missing",
            f"Required RELEASE_SYNC WASM file {RELEASE_SYNC_WASM_REL_PATH!r} is missing",
        )

    manifest_dict: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "profile_id": resolved_profile_id,
        "platform": platform_name,
        "arch": arch_name,
        "node_version": expected_node_version,
        "node_executable_relpath": node_exec_rel,
        "node_sha256": planned_copies[node_exec_rel].sha256,
        "node_bytes": planned_copies[node_exec_rel].size,
        "quickjs_version": PINNED_QUICKJS_VERSION,
        "quickjs_variant": PINNED_QUICKJS_VARIANT,
        "wire_protocol": PINNED_WIRE_PROTOCOL,
        "worker_relpath": WORKER_REL_PATH,
        "worker_sha256": planned_copies[WORKER_REL_PATH].sha256,
        "sky_shim_relpath": SKY_SHIM_REL_PATH,
        "sky_shim_sha256": planned_copies[SKY_SHIM_REL_PATH].sha256,
        "wasm_relpath": RELEASE_SYNC_WASM_REL_PATH,
        "wasm_sha256": planned_copies[RELEASE_SYNC_WASM_REL_PATH].sha256,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "files": [
            {
                "path": rel_p,
                "size": planned_copies[rel_p].size,
                "sha256": planned_copies[rel_p].sha256,
            }
            for rel_p in sorted_rel_paths
        ],
        "licenses": license_entries_raw,
        "dependency_packages": dependency_entries_raw,
    }

    # Pre-validate the manifest dictionary in memory before creating output_target
    validated_manifest = validate_manifest_dict(manifest_dict)
    manifest_json_bytes = (
        json.dumps(
            validated_manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(manifest_json_bytes) > MAX_MANIFEST_BYTES:
        raise JsResourceError("ryn_js_prepare_manifest_too_large", "Encoded manifest.json exceeds size cap")

    expected_spec = ExpectedJsRuntimeProfileSpec(
        expected_manifest_sha256=validated_manifest.manifest_sha256,
        platform=platform_name,
        arch=arch_name,
        node_version=expected_node_version,
        node_sha256=validated_manifest.node_sha256,
        node_bytes=validated_manifest.node_bytes,
        quickjs_version=PINNED_QUICKJS_VERSION,
        quickjs_variant=PINNED_QUICKJS_VARIANT,
        wire_protocol=PINNED_WIRE_PROTOCOL,
        worker_sha256=validated_manifest.worker_sha256,
        sky_shim_sha256=validated_manifest.sky_shim_sha256,
        wasm_sha256=validated_manifest.wasm_sha256,
    )

    build_receipt_dict: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "profile_id": validated_manifest.profile_id,
        "platform": validated_manifest.platform,
        "arch": validated_manifest.arch,
        "manifest_sha256": validated_manifest.manifest_sha256,
        "manifest_file_sha256": hashlib.sha256(manifest_json_bytes).hexdigest(),
        "expected_profile_commitment": {
            "expected_manifest_sha256": expected_spec.expected_manifest_sha256,
            "platform": expected_spec.platform,
            "arch": expected_spec.arch,
            "node_version": expected_spec.node_version,
            "node_sha256": expected_spec.node_sha256,
            "node_bytes": expected_spec.node_bytes,
            "quickjs_version": expected_spec.quickjs_version,
            "quickjs_variant": expected_spec.quickjs_variant,
            "wire_protocol": expected_spec.wire_protocol,
            "worker_sha256": expected_spec.worker_sha256,
            "sky_shim_sha256": expected_spec.sky_shim_sha256,
            "wasm_sha256": expected_spec.wasm_sha256,
        },
        "input_provenance": provenance_record,
        "quickjs_variants_included": list(QUICKJS_VARIANT_PACKAGES),
        "total_files": validated_manifest.total_files,
        "total_bytes": validated_manifest.total_bytes,
    }
    build_receipt_bytes = (
        json.dumps(
            build_receipt_dict,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

    # 5. Re-verify prospective leaf absence after all input checks and create brand-new output directory exclusively
    _assert_prospective_leaf_absent_after_verified_parent(
        output_target.parent,
        output_target.name,
        field_name="output_profile_dir.name",
    )
    try:
        output_target.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise JsResourceError(
            "ryn_js_prepare_output_exists",
            f"Refusing to overwrite concurrently created output directory: {output_target}",
        ) from exc
    except OSError as exc:
        raise JsResourceError(
            "ryn_js_prepare_write_failed",
            f"Failed to create new output directory {output_target}: {exc}",
        ) from exc

    created_dirs: set[Path] = {output_target}
    for rel_p in sorted_rel_paths:
        item = planned_copies[rel_p]
        dest_file = output_target / Path(rel_p)
        # Create intermediate directories step-by-step without exist_ok on symlinks
        rel_dir_parts = Path(rel_p).parts[:-1]
        curr = output_target
        for part in rel_dir_parts:
            next_dir = curr / part
            if next_dir not in created_dirs:
                _assert_prospective_leaf_absent_after_verified_parent(
                    curr,
                    part,
                    field_name=rel_p,
                )
                try:
                    next_dir.mkdir(parents=False, exist_ok=False)
                except FileExistsError as exc:
                    raise JsResourceError(
                        "ryn_js_prepare_output_exists",
                        f"Unexpected concurrently created directory inside new output tree: {next_dir}",
                    ) from exc
                except OSError as exc:
                    raise JsResourceError(
                        "ryn_js_prepare_write_failed",
                        f"Failed to create intermediate directory {next_dir}: {exc}",
                    ) from exc
                created_dirs.add(next_dir)
            curr = next_dir
        _write_exclusive_regular_file(dest_file, item.raw_bytes)
        if rel_p == node_exec_rel and os.name != "nt":
            try:
                os.chmod(dest_file, 0o755)
            except OSError:
                pass

    manifest_file_path = output_target / MANIFEST_FILENAME
    build_receipt_file_path = output_target / BUILD_RECEIPT_FILENAME
    _write_exclusive_regular_file(manifest_file_path, manifest_json_bytes)
    _write_exclusive_regular_file(build_receipt_file_path, build_receipt_bytes)

    # 6. Independently re-verify the newly written profile on disk via production resolver
    verified_profile = resolve_production_js_runtime_profile(
        trusted_profile_root=output_target,
        expected_spec=expected_spec,
    )

    return PreparedJsRuntimeResult(
        output_profile_dir=output_target,
        manifest_path=manifest_file_path,
        build_receipt_path=build_receipt_file_path,
        manifest=validated_manifest,
        expected_spec=expected_spec,
        verified_profile=verified_profile,
        build_receipt=build_receipt_dict,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prepare-js-runtime",
        description=(
            "Offline local preparer and validator for the standalone Ryn QuickJS resource profile. "
            "Never downloads, executes Node/npm, or overwrites existing output directories."
        ),
    )
    parser.add_argument(
        "--node-executable",
        required=True,
        help="Explicit path to the approved local Node.js executable (e.g. node.exe).",
    )
    parser.add_argument(
        "--expected-node-version",
        default=APPROVED_WIN32_X64_NODE_VERSION,
        help=f"Expected semantic version of the Node.js binary (default: {APPROVED_WIN32_X64_NODE_VERSION}).",
    )
    parser.add_argument(
        "--expected-node-sha256",
        default=APPROVED_WIN32_X64_NODE_SHA256,
        help="Expected lowercase hex SHA-256 digest of the Node.js binary.",
    )
    parser.add_argument(
        "--expected-node-bytes",
        type=int,
        default=APPROVED_WIN32_X64_NODE_BYTES,
        help=f"Expected byte length of the Node.js binary (default: {APPROVED_WIN32_X64_NODE_BYTES}).",
    )
    parser.add_argument(
        "--runtime-package-root",
        required=True,
        help="Path to the source native/ryn_js_runtime package directory with installed production node_modules.",
    )
    parser.add_argument(
        "--output-profile-dir",
        required=True,
        help="Brand-new destination directory for the prepared resource profile (must not already exist).",
    )
    parser.add_argument(
        "--node-license",
        default=None,
        help="Optional explicit path to the Node.js LICENSE file (defaults to <node-dir>/LICENSE).",
    )
    parser.add_argument(
        "--node-readme",
        default=None,
        help="Optional explicit path to the Node.js README.md file (defaults to <node-dir>/README.md if present).",
    )
    parser.add_argument(
        "--provenance-json",
        default=None,
        help="Optional explicit path to input-provenance.json (defaults to <node-dir>/input-provenance.json if present).",
    )
    parser.add_argument(
        "--platform",
        default="win32",
        choices=sorted(ALLOWED_PLATFORMS),
        help="Target platform identifier (default: win32).",
    )
    parser.add_argument(
        "--arch",
        default="x64",
        choices=sorted(ALLOWED_ARCHES),
        help="Target architecture identifier (default: x64).",
    )
    parser.add_argument(
        "--profile-id",
        default=None,
        help="Optional profile identifier (defaults to '<platform>-<arch>').",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        result = prepare_js_runtime_profile(
            node_executable_path=args.node_executable,
            expected_node_version=args.expected_node_version,
            expected_node_sha256=args.expected_node_sha256,
            expected_node_bytes=args.expected_node_bytes,
            runtime_package_root=args.runtime_package_root,
            output_profile_dir=args.output_profile_dir,
            node_license_path=args.node_license,
            node_readme_path=args.node_readme,
            provenance_json_path=args.provenance_json,
            platform_name=args.platform,
            arch_name=args.arch,
            profile_id=args.profile_id,
        )
    except JsResourceError as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "error_code": exc.code,
                    "message": exc.message,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 1

    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "profile_id": result.manifest.profile_id,
                "profile_digest": result.manifest.manifest_sha256,
                "total_files": result.manifest.total_files,
                "total_bytes": result.manifest.total_bytes,
                "node_version": result.manifest.node_version,
                "node_sha256": result.manifest.node_sha256,
                "quickjs_version": result.manifest.quickjs_version,
                "quickjs_variant": result.manifest.quickjs_variant,
                "worker_sha256": result.manifest.worker_sha256,
                "wasm_sha256": result.manifest.wasm_sha256,
            },
            ensure_ascii=False,
             sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
