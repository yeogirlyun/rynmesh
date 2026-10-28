"""Authoritative release manifest validation, PE64 inspection, handle-pinned binary leases, and pre-resume process identity verification."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import sys
import threading
import uuid
import weakref
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .protocol import (
    EXPECTED_WORKER_NAME,
    EXPECTED_WORKER_VERSION,
    PROTOCOL_SEMVER,
    WIRE_VERSION,
    NativeBridgeError,
    NativeBridgeRejected,
    parse_strict_json,
)

MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
MIN_BINARY_SIZE_BYTES = 512
MAX_BINARY_SIZE_BYTES = 128 * 1024 * 1024

SUPPORTED_TARGET_TRIPLE = "x86_64-pc-windows-msvc"
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_NT_OPTIONAL_HDR64_MAGIC = 0x020B
IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
IMAGE_FILE_DLL = 0x2000

PACKAGE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "bin" / "computer-use-manifest.json"
)
PACKAGED_EXECUTABLE_BASENAME = "ryn_computer_use.exe"

_COMMITMENT_ISSUER_TOKEN = object()
_LEASE_PROOF_ISSUER_TOKEN = object()

_HEX_64_LOWER_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_VERSION_STR_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_SAFE_WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}\.[Ee][Xx][Ee]$")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:\\")

_RESERVED_DOS_NAMES = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    "CLOCK$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


# ============================================================================
# Owner & Pure-Memory Configuration Identity
# ============================================================================

_OWNER_IDS_LOCK = threading.Lock()
_OWNER_IDS: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_OWNER_FALLBACK_IDS: dict[int, str] = {}


def resolve_owner_id(owner: Any) -> str:
    """Return a stable, unique owner identifier without using mutable path strings or I/O."""
    if owner is None:
        return f"own_standalone_{uuid.uuid4().hex}"
    existing_attr = getattr(owner, "_ryn_native_bridge_owner_id", None)
    if isinstance(existing_attr, str) and existing_attr.startswith("own_"):
        return existing_attr
    with _OWNER_IDS_LOCK:
        existing_attr = getattr(owner, "_ryn_native_bridge_owner_id", None)
        if isinstance(existing_attr, str) and existing_attr.startswith("own_"):
            return existing_attr
        try:
            cached = _OWNER_IDS.get(owner)
        except TypeError:
            cached = _OWNER_FALLBACK_IDS.get(id(owner))
        if cached is not None:
            return cached
        new_id = f"own_{uuid.uuid4().hex}"
        try:
            owner._ryn_native_bridge_owner_id = new_id
        except Exception:
            try:
                _OWNER_IDS[owner] = new_id
            except TypeError:
                _OWNER_FALLBACK_IDS[id(owner)] = new_id
        return new_id


def _lexical_norm_path(value: str | Path | None) -> str | None:
    """Purely lexical path normalization that never calls stat(), resolve(), or touches disk."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return os.path.normcase(os.path.normpath(raw))


@dataclass(frozen=True)
class BridgeConfigIdentity:
    """Immutable configuration identity frozen at NativeBridge / NativeWorkerClient construction."""

    mode: str  # "production" | "development" | "test"
    bundle_root: str | None
    manifest_path: str | None
    bundled_executable: str | None
    dev_command: tuple[str, ...] | None
    resolver_tag: str | None
    transport_tag: str | None
    identity_provider_tag: str | None
    expected_worker_name: str
    expected_worker_version: str | None
    expected_protocol_version: str
    config_fingerprint: str


def build_bridge_config_identity(
    *,
    bundled_executable: str | Path | None = None,
    dev_executable: str | Path | Sequence[str | Path] | None = None,
    transport_factory: Callable[[], Any] | None = None,
    artifact_resolver: Any = None,
    manifest_path: str | Path | None = None,
    bundle_root: str | Path | None = None,
    process_identity_provider: Callable[..., Any] | None = None,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
) -> BridgeConfigIdentity:
    """Construct a frozen `BridgeConfigIdentity` purely in memory (zero filesystem I/O)."""
    dev_tuple: tuple[str, ...] | None = None
    if dev_executable is not None:
        if isinstance(dev_executable, (str, Path)):
            s = str(dev_executable).strip()
            dev_tuple = (_lexical_norm_path(s) or s,)
        elif isinstance(dev_executable, (list, tuple)):
            parts = [str(p).strip() for p in dev_executable]
            if parts:
                first_norm = _lexical_norm_path(parts[0]) or parts[0]
                dev_tuple = (first_norm, *parts[1:])
            else:
                dev_tuple = ()
        else:
            dev_tuple = (str(dev_executable),)

    resolver_tag: str | None = None
    resolver_mode: str | None = None
    if artifact_resolver is not None:
        resolver_mode = getattr(artifact_resolver, "mode", None)
        resolver_tag = getattr(
            artifact_resolver,
            "config_tag",
            f"{type(artifact_resolver).__qualname__}:{id(artifact_resolver)}",
        )

    transport_tag = (
        f"{ getattr(transport_factory, '__qualname__', type(transport_factory).__qualname__) }:{id(transport_factory)}"
        if transport_factory is not None
        else None
    )
    identity_provider_tag = (
        f"{ getattr(process_identity_provider, '__qualname__', type(process_identity_provider).__qualname__) }:{id(process_identity_provider)}"
        if process_identity_provider is not None
        else None
    )

    if transport_factory is not None:
        mode = "test"
    elif dev_tuple is not None:
        mode = "development"
    elif resolver_mode in {"production", "development", "test"}:
        mode = str(resolver_mode)
    else:
        mode = "production"

    norm_bundle_root = _lexical_norm_path(bundle_root)
    norm_manifest = _lexical_norm_path(manifest_path)
    norm_bundled_exe = _lexical_norm_path(bundled_executable)

    payload = {
        "mode": mode,
        "bundleRoot": norm_bundle_root,
        "manifestPath": norm_manifest,
        "bundledExecutable": norm_bundled_exe,
        "devCommand": list(dev_tuple) if dev_tuple is not None else None,
        "resolverTag": resolver_tag,
        "transportTag": transport_tag,
        "identityProviderTag": identity_provider_tag,
        "expectedWorkerName": expected_worker_name,
        "expectedWorkerVersion": expected_worker_version,
        "expectedProtocolVersion": expected_protocol_version,
    }
    raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(raw_json).hexdigest()

    return BridgeConfigIdentity(
        mode=mode,
        bundle_root=norm_bundle_root,
        manifest_path=norm_manifest,
        bundled_executable=norm_bundled_exe,
        dev_command=dev_tuple,
        resolver_tag=resolver_tag,
        transport_tag=transport_tag,
        identity_provider_tag=identity_provider_tag,
        expected_worker_name=expected_worker_name,
        expected_worker_version=expected_worker_version,
        expected_protocol_version=expected_protocol_version,
        config_fingerprint=fingerprint,
    )


# ============================================================================
# Manifest, PE, and Artifact Data Models
# ============================================================================


@dataclass(frozen=True)
class _PackagedReleaseCommitment:
    """Private immutable commitment issued exclusively by `_load_packaged_release_spec`."""

    _issuer_token: object
    package_source: str
    manifest_final_path: str
    manifest_volume_serial: int
    manifest_file_id_high: int
    manifest_file_id_low: int
    manifest_size: int
    manifest_sha256: str
    schema_version: int
    release_id: str
    wire_version: int
    protocol_version: str
    worker_name: str
    worker_version: str
    target_triple: str
    installed_filename: str
    expected_size: int
    expected_sha256: str
    expected_machine: int
    expected_optional_header_magic: int
    fixed_bundle_root: str
    fixed_executable_path: str


@dataclass(frozen=True)
class _ReleaseLeaseProof:
    """Private immutable proof minted only by `open_and_verify_artifact_lease` from a valid `_PackagedReleaseCommitment` and live pinned Win32 handles."""

    _issuer_token: object
    commitment: _PackagedReleaseCommitment
    executable_final_path: str
    bundle_root_final_path: str
    size: int
    sha256: str
    volume_serial: int
    file_id_high: int
    file_id_low: int
    last_write_ft: int
    pe_machine: int
    pe_magic: int


def _validate_spec_matches_commitment(
    spec: ArtifactSpec,
    commitment: Any,
) -> bool:
    """Verify that `commitment` is a genuine `_PackagedReleaseCommitment` and every field of `spec` matches it identically."""
    if not isinstance(spec, ArtifactSpec) or not isinstance(
        commitment, _PackagedReleaseCommitment
    ):
        return False
    if commitment._issuer_token is not _COMMITMENT_ISSUER_TOKEN:
        return False
    if (
        commitment.package_source
        != "frozen_packaged_module:rynmesh/bin/computer-use-manifest.json"
    ):
        return False
    return (
        spec.schema_version == commitment.schema_version == MANIFEST_SCHEMA_VERSION
        and spec.release_id == commitment.release_id
        and spec.wire_version == commitment.wire_version == WIRE_VERSION
        and spec.protocol_version == commitment.protocol_version == PROTOCOL_SEMVER
        and spec.worker_name == commitment.worker_name == EXPECTED_WORKER_NAME
        and spec.worker_version == commitment.worker_version == EXPECTED_WORKER_VERSION
        and spec.target_triple == commitment.target_triple == SUPPORTED_TARGET_TRIPLE
        and spec.installed_filename
        == commitment.installed_filename
        == PACKAGED_EXECUTABLE_BASENAME
        and spec.expected_size == commitment.expected_size
        and spec.expected_sha256 == commitment.expected_sha256
        and spec.expected_machine
        == commitment.expected_machine
        == IMAGE_FILE_MACHINE_AMD64
        and spec.expected_optional_header_magic
        == commitment.expected_optional_header_magic
        == IMAGE_NT_OPTIONAL_HDR64_MAGIC
        and spec.manifest_sha256 == commitment.manifest_sha256
    )


@dataclass(frozen=True)
class ArtifactSpec:
    """Validated expected native worker artifact specification from a manifest."""

    schema_version: int
    release_id: str
    wire_version: int
    protocol_version: str
    worker_name: str
    worker_version: str
    target_triple: str
    installed_filename: str
    expected_size: int
    expected_sha256: str
    expected_machine: int
    expected_optional_header_magic: int
    manifest_sha256: str
    source_mode: str = "production"
    _release_commitment: _PackagedReleaseCommitment | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def release_authorized(self) -> bool:
        """Return True only when this spec carries an unaltered private packaged release commitment under a frozen runtime."""
        return (
            bool(getattr(sys, "frozen", False))
            and self._release_commitment is not None
            and _validate_spec_matches_commitment(self, self._release_commitment)
        )


@dataclass(frozen=True)
class PEHeaderInfo:
    """Bounded PE32+ header inspection result."""

    machine: int
    characteristics: int
    optional_header_magic: int
    number_of_sections: int
    size_of_headers: int
    is_executable: bool
    is_dll: bool


@dataclass(frozen=True)
class VerifiedArtifact:
    """Pinned and verified executable artifact metadata."""

    mode: str  # "production" | "development" | "test"
    verified: bool
    release_verified: bool
    executable_path: str
    bundle_root: str
    size: int
    sha256: str
    volume_serial: int
    file_id_high: int
    file_id_low: int
    pe_machine: int
    pe_magic: int
    manifest_sha256: str | None
    release_id: str | None
    worker_name: str
    worker_version: str
    protocol_version: str
    target_triple: str

    def to_cache_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "fileId": {
                "volumeSerial": self.volume_serial,
                "fileIdHigh": self.file_id_high,
                "fileIdLow": self.file_id_low,
            },
            "machine": self.pe_machine,
            "optionalHeaderMagic": self.pe_magic,
            "targetTriple": self.target_triple,
        }


@dataclass(frozen=True)
class VerifiedProcessIdentity:
    """Verified OS process identity captured before resume (or from an explicit test identity provider)."""

    pid: int
    creation_time_ft: int
    image_path: str | None
    volume_serial: int | None
    file_id_high: int | None
    file_id_low: int | None
    verified_by: str  # "win32_process_handle" | "test_identity_provider"

    def to_cache_dict(self) -> dict[str, Any]:
        file_id = (
            {
                "volumeSerial": self.volume_serial,
                "fileIdHigh": self.file_id_high,
                "fileIdLow": self.file_id_low,
            }
            if self.volume_serial is not None
            and self.file_id_high is not None
            and self.file_id_low is not None
            else None
        )
        return {
            "pid": self.pid,
            "creationTime": str(self.creation_time_ft),
            "fileId": file_id,
            "verifiedBy": self.verified_by,
        }


@dataclass(frozen=True)
class CachedBinding:
    """Atomically published binding combining owner, config, manifest/artifact, OS process, and handshake."""

    owner_id: str
    config_fingerprint: str
    mode: str
    verified: bool
    release_verified: bool
    manifest_digest: str | None
    release_id: str | None
    artifact: dict[str, Any] | None
    process: dict[str, Any]
    worker_generation: int
    worker_instance: str
    handshake_timestamp: str
    validated_handshake: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ownerId": self.owner_id,
            "configFingerprint": self.config_fingerprint,
            "mode": self.mode,
            "verified": self.verified,
            "releaseVerified": self.release_verified,
            "manifestDigest": self.manifest_digest,
            "releaseId": self.release_id,
            "artifact": self.artifact,
            "process": self.process,
            "workerGeneration": self.worker_generation,
            "workerInstance": self.worker_instance,
            "handshakeTimestamp": self.handshake_timestamp,
            "validatedHandshake": self.validated_handshake,
        }


# ============================================================================
# Path, Basename, Manifest, and PE Header Validation
# ============================================================================


def _is_reserved_dos_segment(segment: str) -> bool:
    base = segment.split(".", 1)[0].strip().upper()
    return base in _RESERVED_DOS_NAMES


def validate_safe_installed_filename(filename: Any, *, phase: str = "pre_dispatch") -> str:
    """Validate that `installedFilename` is a strict, safe `.exe` basename with no traversal or device name."""
    if not isinstance(filename, str) or not filename:
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    if (
        "/" in filename
        or "\\" in filename
        or ":" in filename
        or ".." in filename
        or filename.startswith((".", " "))
        or filename.endswith((".", " "))
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in filename)
    ):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    if not _SAFE_BASENAME_RE.match(filename):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    if _is_reserved_dos_segment(filename):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    return filename


def validate_lexical_windows_path(
    path_value: str | Path,
    *,
    phase: str = "pre_dispatch",
) -> str:
    """Validate a Windows path string against UNC, device, ADS, relative traversal, and reserved names.

    Allows Unicode directory names on local drive roots (`X:\\...`).
    """
    if not isinstance(path_value, (str, Path)):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    raw = str(path_value)
    if not raw or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    # Reject UNC / extended device prefixes (`\\server\share`, `\\.\`, `\\?\`)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    if "/" in raw:
        # Reject forward-slash traversal or ambiguous mixed separators if relative
        raw_slash = raw.replace("/", "\\")
    else:
        raw_slash = raw

    if not _DRIVE_PATH_RE.match(raw_slash):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    # Reject Alternate Data Streams (`:` anywhere after drive letter `X:`)
    after_drive = raw_slash[2:]
    if ":" in after_drive:
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    # Inspect every path component before normalization so `..` or `.` or trailing dots/spaces are caught
    parts = [p for p in after_drive.split("\\") if p != ""]
    if not parts:
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    for seg in parts:
        if seg in {".", ".."}:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if seg.endswith((".", " ")):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if any(ch in seg for ch in '<>"|?*'):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if _is_reserved_dos_segment(seg):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    normalized = os.path.normpath(raw_slash)
    if not _DRIVE_PATH_RE.match(normalized):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    return normalized


def _normalize_machine_field(val: Any, *, phase: str) -> int:
    if type(val) is int:
        if val == IMAGE_FILE_MACHINE_AMD64:
            return IMAGE_FILE_MACHINE_AMD64
        raise NativeBridgeRejected("agent_computer_arch_unsupported", phase=phase)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"x86_64", "amd64", "0x8664"}:
            return IMAGE_FILE_MACHINE_AMD64
        raise NativeBridgeRejected("agent_computer_arch_unsupported", phase=phase)
    raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)


def _normalize_pe_magic_field(val: Any, *, phase: str) -> int:
    if type(val) is int:
        if val == IMAGE_NT_OPTIONAL_HDR64_MAGIC:
            return IMAGE_NT_OPTIONAL_HDR64_MAGIC
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"pe32+", "0x20b", "0x020b"}:
            return IMAGE_NT_OPTIONAL_HDR64_MAGIC
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)
    raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)


def parse_and_validate_manifest_bytes(
    raw_bytes: bytes,
    *,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
    source_mode: str = "production",
    phase: str = "pre_dispatch",
) -> ArtifactSpec:
    """Strictly parse and validate a `computer-use-manifest.json` byte payload."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    if len(raw_bytes) == 0 or len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    try:
        text = bytes(raw_bytes).decode("utf-8")
    except UnicodeDecodeError:
        raise NativeBridgeRejected(
            "agent_computer_manifest_invalid",
            phase=phase,
            reason="malformed_json",
        ) from None

    try:
        parsed = parse_strict_json(text, phase=phase)
    except NativeBridgeError as exc:
        code = (
            exc.code
            if exc.code in {"agent_computer_duplicate_json_key", "agent_computer_nonfinite_number"}
            else "agent_computer_manifest_invalid"
        )
        raise NativeBridgeRejected(
            code,
            phase=phase,
            reason=exc.reason,
        ) from None

    if not isinstance(parsed, dict):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    required_root = {
        "schemaVersion",
        "releaseId",
        "wireVersion",
        "protocolVersion",
        "workerName",
        "workerVersion",
        "targetTriple",
        "artifact",
    }
    if set(parsed.keys()) != required_root:
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    schema_ver = parsed["schemaVersion"]
    if type(schema_ver) is not int or schema_ver != MANIFEST_SCHEMA_VERSION:
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    release_id = parsed["releaseId"]
    if not isinstance(release_id, str) or not _SAFE_RELEASE_ID_RE.match(release_id):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    wire_ver = parsed["wireVersion"]
    if type(wire_ver) is not int or wire_ver != WIRE_VERSION:
        raise NativeBridgeRejected(
            "agent_computer_version_mismatch",
            phase=phase,
            reason="version_mismatch",
        )

    proto_ver = parsed["protocolVersion"]
    if not isinstance(proto_ver, str) or not _SAFE_VERSION_STR_RE.match(proto_ver):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    if proto_ver != expected_protocol_version:
        raise NativeBridgeRejected(
            "agent_computer_version_mismatch",
            phase=phase,
            reason="version_mismatch",
        )

    worker_name = parsed["workerName"]
    if not isinstance(worker_name, str) or not _SAFE_WORKER_NAME_RE.match(worker_name):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    if worker_name != expected_worker_name:
        raise NativeBridgeRejected(
            "agent_computer_worker_identity_mismatch",
            phase=phase,
        )

    worker_ver = parsed["workerVersion"]
    if not isinstance(worker_ver, str) or not _SAFE_VERSION_STR_RE.match(worker_ver):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    if expected_worker_version is not None and worker_ver != expected_worker_version:
        raise NativeBridgeRejected(
            "agent_computer_worker_identity_mismatch",
            phase=phase,
        )

    target_triple = parsed["targetTriple"]
    if not isinstance(target_triple, str) or not target_triple.strip():
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    if target_triple != SUPPORTED_TARGET_TRIPLE:
        raise NativeBridgeRejected("agent_computer_arch_unsupported", phase=phase)

    artifact = parsed["artifact"]
    if not isinstance(artifact, dict):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    required_art = {
        "installedFilename",
        "size",
        "sha256",
        "machine",
        "optionalHeaderMagic",
    }
    if set(artifact.keys()) != required_art:
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    installed_filename = validate_safe_installed_filename(
        artifact["installedFilename"], phase=phase
    )

    size_val = artifact["size"]
    if (
        type(size_val) is not int
        or size_val < MIN_BINARY_SIZE_BYTES
        or size_val > MAX_BINARY_SIZE_BYTES
    ):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    sha_val = artifact["sha256"]
    if not isinstance(sha_val, str) or not _HEX_64_LOWER_RE.match(sha_val):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    machine_val = _normalize_machine_field(artifact["machine"], phase=phase)
    magic_val = _normalize_pe_magic_field(artifact["optionalHeaderMagic"], phase=phase)

    manifest_sha256 = hashlib.sha256(bytes(raw_bytes)).hexdigest()

    return ArtifactSpec(
        schema_version=schema_ver,
        release_id=release_id,
        wire_version=wire_ver,
        protocol_version=proto_ver,
        worker_name=worker_name,
        worker_version=worker_ver,
        target_triple=target_triple,
        installed_filename=installed_filename,
        expected_size=size_val,
        expected_sha256=sha_val,
        expected_machine=machine_val,
        expected_optional_header_magic=magic_val,
        manifest_sha256=manifest_sha256,
        source_mode=source_mode,
    )


def parse_and_validate_pe_headers(
    header_bytes: bytes,
    *,
    file_size: int | None = None,
    expected_machine: int = IMAGE_FILE_MACHINE_AMD64,
    expected_magic: int = IMAGE_NT_OPTIONAL_HDR64_MAGIC,
    phase: str = "pre_dispatch",
) -> PEHeaderInfo:
    """Bounded PE32+ header validator. Never executes the image."""
    if not isinstance(header_bytes, (bytes, bytearray)):
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)
    eff_file_size = len(header_bytes) if file_size is None else file_size
    if (
        type(eff_file_size) is not int
        or len(header_bytes) < 64
        or eff_file_size < MIN_BINARY_SIZE_BYTES
        or eff_file_size > MAX_BINARY_SIZE_BYTES
    ):
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    if header_bytes[0:2] != b"MZ":
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    (e_lfanew,) = struct.unpack_from("<I", header_bytes, 0x3C)
    # Require bounded e_lfanew within the first 64 KiB and inside header_bytes
    if e_lfanew < 0x40 or e_lfanew > 64 * 1024 or e_lfanew + 24 > len(header_bytes):
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    if header_bytes[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    (
        machine,
        num_sections,
        _time_date_stamp,
        _ptr_sym_table,
        _num_symbols,
        size_of_opt_hdr,
        characteristics,
    ) = struct.unpack_from("<HHIIIHH", header_bytes, e_lfanew + 4)

    if machine != expected_machine:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    if num_sections < 1 or num_sections > 96:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    # PE32+ Optional Header is at least 112 bytes (up to NumberOfRvaAndSizes)
    if size_of_opt_hdr < 112 or size_of_opt_hdr > 4096:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    opt_offset = e_lfanew + 24
    section_table_end = opt_offset + size_of_opt_hdr + (num_sections * 40)
    if opt_offset + size_of_opt_hdr > len(header_bytes) or section_table_end > eff_file_size:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    is_exec = bool(characteristics & IMAGE_FILE_EXECUTABLE_IMAGE)
    is_dll = bool(characteristics & IMAGE_FILE_DLL)
    if not is_exec or is_dll:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    (opt_magic,) = struct.unpack_from("<H", header_bytes, opt_offset)
    if opt_magic != expected_magic:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    # SizeOfHeaders is at offset 60 inside PE32+ Optional Header
    (size_of_headers,) = struct.unpack_from("<I", header_bytes, opt_offset + 60)
    if size_of_headers < section_table_end or size_of_headers > eff_file_size:
        raise NativeBridgeRejected("agent_computer_pe_invalid", phase=phase)

    return PEHeaderInfo(
        machine=machine,
        characteristics=characteristics,
        optional_header_magic=opt_magic,
        number_of_sections=num_sections,
        size_of_headers=size_of_headers,
        is_executable=is_exec,
        is_dll=is_dll,
    )


# ============================================================================
# Win32 Handle Pinning, File Identity, and Process Verification
# ============================================================================

_INVALID_HANDLE_VALUES = frozenset({
    0,
    -1,
    0xFFFFFFFF,
    0xFFFFFFFFFFFFFFFF,
    ctypes.c_void_p(-1).value,
})

_GENERIC_READ = 0x80000000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_INVALID = 0xFFFFFFFF
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_VOLUME_NAME_DOS = 0x0
_STILL_ACTIVE = 259


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):  # noqa: N801 - matches the Win32 structure name.
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _FILE_ID_INFO(ctypes.Structure):  # noqa: N801 - matches the Win32 structure name.
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", ctypes.c_ubyte * 16),
    ]


def _get_kernel32() -> Any:
    if os.name != "nt":
        raise NativeBridgeRejected(
            "agent_computer_windows_required",
            phase="pre_dispatch",
        )
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
         ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetFileInformationByHandle.restype = wintypes.BOOL
    k32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    k32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    k32.GetFileAttributesW.restype = wintypes.DWORD
    k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    k32.SetFilePointerEx.restype = wintypes.BOOL
    k32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int64,
        ctypes.POINTER(ctypes.c_int64),
        wintypes.DWORD,
    ]
    k32.ReadFile.restype = wintypes.BOOL
    k32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    k32.GetProcessId.restype = wintypes.DWORD
    k32.GetProcessId.argtypes = [wintypes.HANDLE]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = []
    return k32


def verify_windows_x64_host(*, phase: str = "pre_dispatch") -> int:
    """Verify the host OS is native Windows x86_64 (`0x8664`) via `IsWow64Process2`."""
    if os.name != "nt":
        raise NativeBridgeRejected("agent_computer_windows_required", phase=phase)
    k32 = _get_kernel32()
    fn = getattr(k32, "IsWow64Process2", None)
    if fn is None:
        raise NativeBridgeRejected("agent_computer_arch_unsupported", phase=phase)
    fn.restype = wintypes.BOOL
    fn.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    process_machine = wintypes.USHORT(0)
    native_machine = wintypes.USHORT(0)
    if not fn(
        k32.GetCurrentProcess(),
        ctypes.byref(process_machine),
        ctypes.byref(native_machine),
    ):
        raise NativeBridgeRejected("agent_computer_arch_unsupported", phase=phase)
    if int(native_machine.value) != IMAGE_FILE_MACHINE_AMD64:
        raise NativeBridgeRejected("agent_computer_arch_unsupported", phase=phase)
    return int(native_machine.value)


def _close_win32_handle(handle: int | None) -> None:
    if handle is None or handle in _INVALID_HANDLE_VALUES:
        return
    try:
        k32 = _get_kernel32()
        k32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        pass


def _check_no_ancestor_reparse_points(
    lexical_path: str,
    *,
    missing_code: str = "agent_computer_executable_not_found",
    phase: str = "pre_dispatch",
) -> None:
    """Walk every ancestor directory from drive root down to `lexical_path` and reject reparse points."""
    k32 = _get_kernel32()
    drive = lexical_path[:3]
    rest = lexical_path[3:]
    segments = [s for s in rest.split("\\") if s]
    cur = drive.rstrip("\\")
    for idx, seg in enumerate(segments):
        cur = f"{cur}\\{seg}"
        attrs = k32.GetFileAttributesW(cur)
        if attrs == _FILE_ATTRIBUTE_INVALID:
            if idx == len(segments) - 1:
                raise NativeBridgeRejected(missing_code, phase=phase)
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)


@dataclass(frozen=True)
class _HandleIdentity:
    final_path: str
    is_directory: bool
    is_reparse: bool
    size: int
    volume_serial: int
    file_id_high: int
    file_id_low: int
    last_write_ft: int


def _query_handle_identity(
    handle: int,
    *,
    phase: str = "pre_dispatch",
) -> _HandleIdentity:
    k32 = _get_kernel32()
    info = _BY_HANDLE_FILE_INFORMATION()
    if not k32.GetFileInformationByHandle(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    attrs = int(info.dwFileAttributes)
    is_dir = bool(attrs & _FILE_ATTRIBUTE_DIRECTORY)
    is_reparse = bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    last_write = (int(info.ftLastWriteTime.dwHighDateTime) << 32) | int(
        info.ftLastWriteTime.dwLowDateTime
    )

    vol_serial = int(info.dwVolumeSerialNumber)
    fid_high = int(info.nFileIndexHigh)
    fid_low = int(info.nFileIndexLow)

    file_id_info = _FILE_ID_INFO()
    # FileIdInfo == 18
    if k32.GetFileInformationByHandleEx(
        wintypes.HANDLE(handle),
        18,
        ctypes.byref(file_id_info),
        ctypes.sizeof(file_id_info),
    ):
        vol_serial = int(file_id_info.VolumeSerialNumber)
        raw_fid = bytes(file_id_info.FileId)
        fid_low, fid_high = struct.unpack("<QQ", raw_fid)

    buf_len = 1024
    buf = ctypes.create_unicode_buffer(buf_len)
    chars = k32.GetFinalPathNameByHandleW(
        wintypes.HANDLE(handle), buf, buf_len, _VOLUME_NAME_DOS
    )
    if chars == 0:
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    if chars >= buf_len:
        buf_len = int(chars) + 2
        buf = ctypes.create_unicode_buffer(buf_len)
        chars = k32.GetFinalPathNameByHandleW(
            wintypes.HANDLE(handle), buf, buf_len, _VOLUME_NAME_DOS
        )
        if chars == 0 or chars >= buf_len:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    final_raw = buf.value
    if final_raw.startswith("\\\\?\\UNC\\"):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    if final_raw.startswith("\\\\?\\"):
        final_raw = final_raw[4:]

    final_clean = validate_lexical_windows_path(final_raw, phase=phase)
    return _HandleIdentity(
        final_path=final_clean,
        is_directory=is_dir,
        is_reparse=is_reparse,
        size=size,
        volume_serial=vol_serial,
        file_id_high=fid_high,
        file_id_low=fid_low,
        last_write_ft=last_write,
    )


def _read_bounded_bytes_from_handle(
    handle: int,
    *,
    max_bytes: int,
    error_code: str = "agent_computer_binary_invalid",
    phase: str = "pre_dispatch",
) -> tuple[bytes, str]:
    """Read all bytes from an opened Win32 file handle from offset 0 up to `max_bytes`, returning `(bytes, sha256_hex)`."""
    k32 = _get_kernel32()
    new_pos = ctypes.c_int64(0)
    if not k32.SetFilePointerEx(
        wintypes.HANDLE(handle),
        ctypes.c_int64(0),
        ctypes.byref(new_pos),
        0,  # FILE_BEGIN
    ):
        raise NativeBridgeRejected(error_code, phase=phase)

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    chunk_size = 65536
    buf = ctypes.create_string_buffer(chunk_size)
    bytes_read = wintypes.DWORD(0)

    while True:
        ok = k32.ReadFile(
            wintypes.HANDLE(handle),
            buf,
            chunk_size,
            ctypes.byref(bytes_read),
            None,
        )
        if not ok:
            raise NativeBridgeRejected(error_code, phase=phase)
        n = int(bytes_read.value)
        if n == 0:
            break
        total += n
        if total > max_bytes:
            raise NativeBridgeRejected(error_code, phase=phase)
        piece = buf.raw[:n]
        hasher.update(piece)
        chunks.append(piece)

    return b"".join(chunks), hasher.hexdigest()


def load_manifest(
    manifest_path: str | Path | None = None,
    *,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
    source_mode: str = "development",
    phase: str = "pre_dispatch",
) -> ArtifactSpec:
    """Load and validate a manifest file via a pinned Win32 handle with no reparse or ADS escape.

    This public helper validates manifest structure and identity only; it NEVER grants
    packaged release authority (`_release_commitment` is always `None`).
    """
    target = Path(manifest_path) if manifest_path is not None else PACKAGE_MANIFEST_PATH
    lexical = validate_lexical_windows_path(target, phase=phase)
    _check_no_ancestor_reparse_points(
        lexical,
        missing_code="agent_computer_manifest_missing",
        phase=phase,
    )

    k32 = _get_kernel32()
    handle = k32.CreateFileW(
        lexical,
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in _INVALID_HANDLE_VALUES or not handle:
        err = ctypes.get_last_error()
        if err in {2, 3}:  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
            raise NativeBridgeRejected("agent_computer_manifest_missing", phase=phase)
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    try:
        ident = _query_handle_identity(int(handle), phase=phase)
        if ident.is_directory or ident.is_reparse:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if os.path.normcase(ident.final_path) != os.path.normcase(lexical):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if ident.size <= 0 or ident.size > MAX_MANIFEST_BYTES:
            raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
        raw_bytes, _ = _read_bounded_bytes_from_handle(
            int(handle),
            max_bytes=MAX_MANIFEST_BYTES,
            error_code="agent_computer_manifest_invalid",
            phase=phase,
        )
        if len(raw_bytes) != ident.size:
            raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    finally:
        _close_win32_handle(int(handle))

    return parse_and_validate_manifest_bytes(
        raw_bytes,
        expected_worker_name=expected_worker_name,
        expected_worker_version=expected_worker_version,
        expected_protocol_version=expected_protocol_version,
        source_mode=source_mode,
        phase=phase,
    )


def load_authoritative_manifest(
    manifest_path: str | Path | None = None,
    *,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
    source_mode: str = "production",
    phase: str = "pre_dispatch",
) -> ArtifactSpec:
    """Backward-compatible alias for `load_manifest`. Never mints packaged release authority."""
    return load_manifest(
        manifest_path,
        expected_worker_name=expected_worker_name,
        expected_worker_version=expected_worker_version,
        expected_protocol_version=expected_protocol_version,
        source_mode=source_mode,
        phase=phase,
    )


def _load_packaged_release_spec(*, phase: str = "pre_dispatch") -> ArtifactSpec:
    """Private packaged release loader.

    Accepts NO caller `manifest_path`, `bundle_root`, `bundled_executable`, or `mode` arguments.
    Requires `sys.frozen` to be `True`, reads only the fixed packaged `PACKAGE_MANIFEST_PATH`,
    pins the fixed bundle root `Path(sys.executable).parent` and executable basename
    `ryn_computer_use.exe`, and issues a private `_PackagedReleaseCommitment`.
    """
    if not bool(getattr(sys, "frozen", False)):
        raise NativeBridgeRejected(
            "agent_computer_executable_not_found",
            phase=phase,
        )

    verify_windows_x64_host(phase=phase)

    sys_exe_lex = validate_lexical_windows_path(sys.executable, phase=phase)
    fixed_root_lex = validate_lexical_windows_path(Path(sys_exe_lex).parent, phase=phase)
    _check_no_ancestor_reparse_points(
        fixed_root_lex,
        missing_code="agent_computer_path_invalid",
        phase=phase,
    )

    k32 = _get_kernel32()
    root_handle_raw = k32.CreateFileW(
        fixed_root_lex,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if root_handle_raw in _INVALID_HANDLE_VALUES or not root_handle_raw:
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    try:
        root_ident = _query_handle_identity(int(root_handle_raw), phase=phase)
    finally:
        _close_win32_handle(int(root_handle_raw))

    if (
        not root_ident.is_directory
        or root_ident.is_reparse
        or os.path.normcase(root_ident.final_path) != os.path.normcase(fixed_root_lex)
    ):
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    manifest_lex = validate_lexical_windows_path(PACKAGE_MANIFEST_PATH, phase=phase)
    _check_no_ancestor_reparse_points(
        manifest_lex,
        missing_code="agent_computer_manifest_missing",
        phase=phase,
    )

    m_handle_raw = k32.CreateFileW(
        manifest_lex,
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if m_handle_raw in _INVALID_HANDLE_VALUES or not m_handle_raw:
        err = ctypes.get_last_error()
        if err in {2, 3}:
            raise NativeBridgeRejected("agent_computer_manifest_missing", phase=phase)
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    try:
        ident1 = _query_handle_identity(int(m_handle_raw), phase=phase)
        if ident1.is_directory or ident1.is_reparse:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if os.path.normcase(ident1.final_path) != os.path.normcase(manifest_lex):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if ident1.size <= 0 or ident1.size > MAX_MANIFEST_BYTES:
            raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
        raw_bytes, computed_manifest_sha = _read_bounded_bytes_from_handle(
            int(m_handle_raw),
            max_bytes=MAX_MANIFEST_BYTES,
            error_code="agent_computer_manifest_invalid",
            phase=phase,
        )
        if len(raw_bytes) != ident1.size:
            raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
        ident2 = _query_handle_identity(int(m_handle_raw), phase=phase)
        if (
            ident2.size != ident1.size
            or ident2.volume_serial != ident1.volume_serial
            or ident2.file_id_high != ident1.file_id_high
            or ident2.file_id_low != ident1.file_id_low
            or ident2.last_write_ft != ident1.last_write_ft
            or os.path.normcase(ident2.final_path)
            != os.path.normcase(ident1.final_path)
        ):
            raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
    finally:
        _close_win32_handle(int(m_handle_raw))

    base_spec = parse_and_validate_manifest_bytes(
        raw_bytes,
        expected_worker_name=EXPECTED_WORKER_NAME,
        expected_worker_version=EXPECTED_WORKER_VERSION,
        expected_protocol_version=PROTOCOL_SEMVER,
        source_mode="production",
        phase=phase,
    )
    if (
        base_spec.installed_filename != PACKAGED_EXECUTABLE_BASENAME
        or base_spec.manifest_sha256 != computed_manifest_sha
    ):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    fixed_exe_final = os.path.join(root_ident.final_path, PACKAGED_EXECUTABLE_BASENAME)
    commitment = _PackagedReleaseCommitment(
        _issuer_token=_COMMITMENT_ISSUER_TOKEN,
        package_source="frozen_packaged_module:rynmesh/bin/computer-use-manifest.json",
        manifest_final_path=ident2.final_path,
        manifest_volume_serial=ident2.volume_serial,
        manifest_file_id_high=ident2.file_id_high,
        manifest_file_id_low=ident2.file_id_low,
        manifest_size=ident2.size,
        manifest_sha256=computed_manifest_sha,
        schema_version=base_spec.schema_version,
        release_id=base_spec.release_id,
        wire_version=base_spec.wire_version,
        protocol_version=base_spec.protocol_version,
        worker_name=base_spec.worker_name,
        worker_version=base_spec.worker_version,
        target_triple=base_spec.target_triple,
        installed_filename=base_spec.installed_filename,
        expected_size=base_spec.expected_size,
        expected_sha256=base_spec.expected_sha256,
        expected_machine=base_spec.expected_machine,
        expected_optional_header_magic=base_spec.expected_optional_header_magic,
        fixed_bundle_root=root_ident.final_path,
        fixed_executable_path=fixed_exe_final,
    )
    return ArtifactSpec(
        schema_version=base_spec.schema_version,
        release_id=base_spec.release_id,
        wire_version=base_spec.wire_version,
        protocol_version=base_spec.protocol_version,
        worker_name=base_spec.worker_name,
        worker_version=base_spec.worker_version,
        target_triple=base_spec.target_triple,
        installed_filename=base_spec.installed_filename,
        expected_size=base_spec.expected_size,
        expected_sha256=base_spec.expected_sha256,
        expected_machine=base_spec.expected_machine,
        expected_optional_header_magic=base_spec.expected_optional_header_magic,
        manifest_sha256=base_spec.manifest_sha256,
        source_mode="production",
        _release_commitment=commitment,
    )


class ArtifactLease:
    """Holds pinned Win32 handles for the verified executable and its bundle directory until worker exit."""

    def __init__(
        self,
        *,
        verified_artifact: VerifiedArtifact,
        command: list[str],
        file_handle: int | None = None,
        dir_handle: int | None = None,
        _release_proof: _ReleaseLeaseProof | None = None,
    ) -> None:
        self.verified_artifact = verified_artifact
        self.command = list(command)
        self._file_handle = file_handle
        self._dir_handle = dir_handle
        self._release_proof = _release_proof
        self._lock = threading.Lock()
        self._closed = False
        self.process_identity: VerifiedProcessIdentity | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def has_valid_release_proof(
        self,
        *,
        process_identity: VerifiedProcessIdentity | None = None,
        handshake: dict[str, Any] | None = None,
    ) -> bool:
        """Return True only if this lease holds a live, unaltered packaged release proof and pinned handles."""
        return verify_lease_release_authority(
            self,
            process_identity=process_identity,
            handshake=handshake,
        )

    def has_pinned_verified_artifact(
        self,
        *,
        process_identity: VerifiedProcessIdentity | None = None,
    ) -> bool:
        """Return True only if this lease holds live pinned Win32 file and directory handles matching `verified_artifact`."""
        with self._lock:
            if self._closed:
                return False
            fh = self._file_handle
            dh = self._dir_handle
            proc_ident = (
                process_identity
                if process_identity is not None
                else self.process_identity
            )
        if fh is None or dh is None:
            return False
        art = self.verified_artifact
        if not isinstance(art, VerifiedArtifact) or not art.verified:
            return False
        if not isinstance(art.sha256, str) or not _HEX_64_LOWER_RE.match(art.sha256):
            return False
        try:
            dir_ident = _query_handle_identity(dh, phase="pre_dispatch")
            file_ident = _query_handle_identity(fh, phase="pre_dispatch")
        except Exception:
            return False
        if (
            not dir_ident.is_directory
            or dir_ident.is_reparse
            or os.path.normcase(dir_ident.final_path)
            != os.path.normcase(art.bundle_root)
        ):
            return False
        if (
            file_ident.is_directory
            or file_ident.is_reparse
            or file_ident.size != art.size
            or file_ident.volume_serial != art.volume_serial
            or file_ident.file_id_high != art.file_id_high
            or file_ident.file_id_low != art.file_id_low
            or os.path.normcase(file_ident.final_path)
            != os.path.normcase(art.executable_path)
            or os.path.normcase(str(Path(file_ident.final_path).parent))
            != os.path.normcase(art.bundle_root)
        ):
            return False
        if proc_ident is not None:
            if (
                not isinstance(proc_ident, VerifiedProcessIdentity)
                or proc_ident.verified_by != "win32_process_handle"
                or proc_ident.pid <= 0
                or proc_ident.creation_time_ft <= 0
                or proc_ident.volume_serial != art.volume_serial
                or proc_ident.file_id_high != art.file_id_high
                or proc_ident.file_id_low != art.file_id_low
                or proc_ident.image_path is None
                or os.path.normcase(proc_ident.image_path)
                != os.path.normcase(art.executable_path)
            ):
                return False
        return True

    def verify_still_pinned(self, *, phase: str = "pre_dispatch") -> None:
        """Re-verify that the held file and directory handles remain open, un-relocated, and unmodified."""
        with self._lock:
            if self._closed:
                raise NativeBridgeRejected(
                    "agent_computer_process_identity_mismatch",
                    phase=phase,
                )
            fh = self._file_handle
            dh = self._dir_handle

        if dh is not None:
            dir_ident = _query_handle_identity(dh, phase=phase)
            if (
                not dir_ident.is_directory
                or dir_ident.is_reparse
                or os.path.normcase(dir_ident.final_path)
                != os.path.normcase(self.verified_artifact.bundle_root)
            ):
                raise NativeBridgeRejected(
                    "agent_computer_process_identity_mismatch",
                    phase=phase,
                )

        if fh is not None:
            file_ident = _query_handle_identity(fh, phase=phase)
            if (
                file_ident.is_directory
                or file_ident.is_reparse
                or file_ident.size != self.verified_artifact.size
                or file_ident.volume_serial != self.verified_artifact.volume_serial
                or file_ident.file_id_high != self.verified_artifact.file_id_high
                or file_ident.file_id_low != self.verified_artifact.file_id_low
                or os.path.normcase(file_ident.final_path)
                != os.path.normcase(self.verified_artifact.executable_path)
                or os.path.normcase(str(Path(file_ident.final_path).parent))
                != os.path.normcase(self.verified_artifact.bundle_root)
            ):
                raise NativeBridgeRejected(
                    "agent_computer_process_identity_mismatch",
                    phase=phase,
                )

    def verify_suspended_process(
        self,
        process: Any,
        job_handle: Any = None,
    ) -> VerifiedProcessIdentity:
        """`before_resume` hook invoked inside `_windows_job` while the child is still suspended."""
        _ = job_handle
        self.verify_still_pinned(phase="pre_dispatch")
        ident = verify_suspended_process_handle(
            process,
            expected_artifact=self.verified_artifact,
            phase="pre_dispatch",
        )
        with self._lock:
            self.process_identity = ident
        try:
            process._ryn_verified_identity = ident
        except Exception:
            pass
        return ident

    def close(self) -> None:
        """Idempotently close all pinned file and directory handles."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fh = self._file_handle
            dh = self._dir_handle
            self._file_handle = None
            self._dir_handle = None
            self._release_proof = None
        _close_win32_handle(fh)
        _close_win32_handle(dh)


def verify_lease_release_authority(
    lease: Any,
    *,
    process_identity: VerifiedProcessIdentity | None = None,
    handshake: dict[str, Any] | None = None,
) -> bool:
    """Independently verify that `lease` holds an unforgeable packaged release proof, live pinned handles, and matching process/handshake identity."""
    if not isinstance(lease, ArtifactLease):
        return False
    if not bool(getattr(sys, "frozen", False)):
        return False

    with lease._lock:
        if lease._closed:
            return False
        fh = lease._file_handle
        dh = lease._dir_handle
        proof = lease._release_proof
        proc_ident = (
            process_identity
            if process_identity is not None
            else lease.process_identity
        )

    if fh is None or dh is None or not isinstance(proof, _ReleaseLeaseProof):
        return False
    if proof._issuer_token is not _LEASE_PROOF_ISSUER_TOKEN:
        return False

    commitment = proof.commitment
    if not isinstance(commitment, _PackagedReleaseCommitment):
        return False
    if commitment._issuer_token is not _COMMITMENT_ISSUER_TOKEN:
        return False
    if (
        commitment.package_source
        != "frozen_packaged_module:rynmesh/bin/computer-use-manifest.json"
    ):
        return False

    art = lease.verified_artifact
    if not isinstance(art, VerifiedArtifact):
        return False
    if not (
        art.mode == "production"
        and art.verified is True
        and art.release_verified is True
    ):
        return False

    if (
        art.sha256 != proof.sha256
        or proof.sha256 != commitment.expected_sha256
        or art.size != proof.size
        or proof.size != commitment.expected_size
        or art.volume_serial != proof.volume_serial
        or art.file_id_high != proof.file_id_high
        or art.file_id_low != proof.file_id_low
        or art.pe_machine != proof.pe_machine
        or proof.pe_machine != commitment.expected_machine
        or proof.pe_machine != IMAGE_FILE_MACHINE_AMD64
        or art.pe_magic != proof.pe_magic
        or proof.pe_magic != commitment.expected_optional_header_magic
        or proof.pe_magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC
        or art.manifest_sha256 != commitment.manifest_sha256
        or art.release_id != commitment.release_id
        or art.worker_name != commitment.worker_name
        or commitment.worker_name != EXPECTED_WORKER_NAME
        or art.worker_version != commitment.worker_version
        or commitment.worker_version != EXPECTED_WORKER_VERSION
        or art.protocol_version != commitment.protocol_version
        or commitment.protocol_version != PROTOCOL_SEMVER
        or art.target_triple != commitment.target_triple
        or commitment.target_triple != SUPPORTED_TARGET_TRIPLE
        or commitment.installed_filename != PACKAGED_EXECUTABLE_BASENAME
        or os.path.normcase(art.executable_path)
        != os.path.normcase(proof.executable_final_path)
        or os.path.normcase(proof.executable_final_path)
        != os.path.normcase(commitment.fixed_executable_path)
        or os.path.normcase(art.bundle_root)
        != os.path.normcase(proof.bundle_root_final_path)
        or os.path.normcase(proof.bundle_root_final_path)
        != os.path.normcase(commitment.fixed_bundle_root)
    ):
        return False

    try:
        sys_exe_lex = validate_lexical_windows_path(sys.executable, phase="pre_dispatch")
        expected_root_lex = validate_lexical_windows_path(
            Path(sys_exe_lex).parent, phase="pre_dispatch"
        )
        if os.path.normcase(expected_root_lex) != os.path.normcase(
            commitment.fixed_bundle_root
        ):
            return False

        dir_ident = _query_handle_identity(dh, phase="pre_dispatch")
        file_ident = _query_handle_identity(fh, phase="pre_dispatch")
    except Exception:
        return False

    if (
        not dir_ident.is_directory
        or dir_ident.is_reparse
        or os.path.normcase(dir_ident.final_path)
        != os.path.normcase(proof.bundle_root_final_path)
    ):
        return False

    if (
        file_ident.is_directory
        or file_ident.is_reparse
        or file_ident.size != proof.size
        or file_ident.volume_serial != proof.volume_serial
        or file_ident.file_id_high != proof.file_id_high
        or file_ident.file_id_low != proof.file_id_low
        or file_ident.last_write_ft != proof.last_write_ft
        or os.path.normcase(file_ident.final_path)
        != os.path.normcase(proof.executable_final_path)
    ):
        return False

    if proc_ident is not None:
        if (
            not isinstance(proc_ident, VerifiedProcessIdentity)
            or proc_ident.verified_by != "win32_process_handle"
            or proc_ident.pid <= 0
            or proc_ident.creation_time_ft <= 0
            or proc_ident.volume_serial != proof.volume_serial
            or proc_ident.file_id_high != proof.file_id_high
            or proc_ident.file_id_low != proof.file_id_low
            or proc_ident.image_path is None
            or os.path.normcase(proc_ident.image_path)
            != os.path.normcase(proof.executable_final_path)
        ):
            return False

    if handshake is not None:
        if not isinstance(handshake, dict):
            return False
        if handshake.get("protocolVersion") != commitment.protocol_version:
            return False
        worker_obj = handshake.get("worker")
        if (
            not isinstance(worker_obj, dict)
            or worker_obj.get("name") != commitment.worker_name
            or worker_obj.get("version") != commitment.worker_version
        ):
            return False

    return True


def verify_suspended_process_handle(
    process: Any,
    *,
    expected_artifact: VerifiedArtifact | None = None,
    expected_executable_path: str | Path | None = None,
    phase: str = "pre_dispatch",
) -> VerifiedProcessIdentity:
    """Inspect a suspended Windows process via its OS handle before `NtResumeProcess`."""
    proc_handle_raw = getattr(process, "_handle", None)
    proc_pid_raw = getattr(process, "pid", None)
    if (
        proc_handle_raw is None
        or int(proc_handle_raw) in _INVALID_HANDLE_VALUES
        or type(proc_pid_raw) is not int
        or proc_pid_raw <= 0
    ):
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )

    k32 = _get_kernel32()
    hproc = wintypes.HANDLE(int(proc_handle_raw))

    actual_os_pid = int(k32.GetProcessId(hproc))
    if actual_os_pid <= 0 or actual_os_pid != proc_pid_raw:
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
            reason="pid_mismatch",
        )

    exit_code = wintypes.DWORD(0)
    if (
        not k32.GetExitCodeProcess(hproc, ctypes.byref(exit_code))
        or int(exit_code.value) != _STILL_ACTIVE
    ):
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )

    ft_create = _FILETIME()
    ft_exit = _FILETIME()
    ft_kernel = _FILETIME()
    ft_user = _FILETIME()
    if not k32.GetProcessTimes(
        hproc,
        ctypes.byref(ft_create),
        ctypes.byref(ft_exit),
        ctypes.byref(ft_kernel),
        ctypes.byref(ft_user),
    ):
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )
    creation_time_ft = (int(ft_create.dwHighDateTime) << 32) | int(
        ft_create.dwLowDateTime
    )
    if creation_time_ft <= 0:
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )

    buf_len = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(buf_len.value)
    if not k32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(buf_len)):
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )
    image_path = validate_lexical_windows_path(buf.value, phase=phase)

    # Open the process's actual image file to verify its OS FileId and final path
    img_handle = k32.CreateFileW(
        image_path,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if img_handle in _INVALID_HANDLE_VALUES or not img_handle:
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )
    try:
        img_ident = _query_handle_identity(int(img_handle), phase=phase)
    finally:
        _close_win32_handle(int(img_handle))

    if img_ident.is_directory or img_ident.is_reparse:
        raise NativeBridgeRejected(
            "agent_computer_process_identity_mismatch",
            phase=phase,
        )

    if expected_artifact is not None:
        if (
            img_ident.volume_serial != expected_artifact.volume_serial
            or img_ident.file_id_high != expected_artifact.file_id_high
            or img_ident.file_id_low != expected_artifact.file_id_low
            or img_ident.size != expected_artifact.size
            or os.path.normcase(img_ident.final_path)
            != os.path.normcase(expected_artifact.executable_path)
        ):
            raise NativeBridgeRejected(
                "agent_computer_process_identity_mismatch",
                phase=phase,
            )

    if expected_executable_path is not None:
        exp_norm = os.path.normcase(
            validate_lexical_windows_path(expected_executable_path, phase=phase)
        )
        if os.path.normcase(img_ident.final_path) != exp_norm:
            raise NativeBridgeRejected(
                "agent_computer_process_identity_mismatch",
                phase=phase,
            )

    return VerifiedProcessIdentity(
        pid=actual_os_pid,
        creation_time_ft=creation_time_ft,
        image_path=img_ident.final_path,
        volume_serial=img_ident.volume_serial,
        file_id_high=img_ident.file_id_high,
        file_id_low=img_ident.file_id_low,
        verified_by="win32_process_handle",
    )


def open_and_verify_artifact_lease(
    *,
    spec: ArtifactSpec,
    bundle_root: str | Path,
    candidate_executable: str | Path | None = None,
    mode: str = "production",
    on_before_open_file: Callable[[], None] | None = None,
    on_after_hash_before_return: Callable[[], None] | None = None,
    phase: str = "pre_dispatch",
) -> ArtifactLease:
    """Pin the bundle directory and target executable handles, verifying containment, PE64, size, and SHA-256.

    Release authority (`release_verified=True` and `_release_proof`) is minted ONLY when `spec`
    carries an unaltered private `_PackagedReleaseCommitment` under a frozen runtime and
    `bundle_root`/`candidate_executable` match the fixed packaged installation paths.
    If `spec` carries a `_release_commitment` that was altered (e.g., via `dataclasses.replace`)
    or paired with any non-fixed path/non-frozen runtime, this function fails closed before spawn.
    """
    if not isinstance(spec, ArtifactSpec):
        raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)

    raw_commitment = spec._release_commitment
    if raw_commitment is not None:
        if not _validate_spec_matches_commitment(spec, raw_commitment):
            raise NativeBridgeRejected("agent_computer_manifest_invalid", phase=phase)
        if not bool(getattr(sys, "frozen", False)):
            raise NativeBridgeRejected(
                "agent_computer_executable_not_found",
                phase=phase,
            )

    verify_windows_x64_host(phase=phase)

    root_lexical = validate_lexical_windows_path(bundle_root, phase=phase)
    _check_no_ancestor_reparse_points(
        root_lexical,
        missing_code="agent_computer_path_invalid",
        phase=phase,
    )

    expected_exe_lexical = os.path.join(root_lexical, spec.installed_filename)
    if candidate_executable is not None:
        cand_lexical = validate_lexical_windows_path(
            candidate_executable, phase=phase
        )
        # Candidate executable must be inside bundle_root and have the exact installedFilename
        if os.path.normcase(cand_lexical) != os.path.normcase(expected_exe_lexical):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    if raw_commitment is not None:
        if (
            os.path.normcase(root_lexical)
            != os.path.normcase(raw_commitment.fixed_bundle_root)
            or os.path.normcase(expected_exe_lexical)
            != os.path.normcase(raw_commitment.fixed_executable_path)
        ):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    k32 = _get_kernel32()
    dir_handle_raw = k32.CreateFileW(
        root_lexical,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # No FILE_SHARE_DELETE: prevents directory rename/delete
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if dir_handle_raw in _INVALID_HANDLE_VALUES or not dir_handle_raw:
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
    dir_handle = int(dir_handle_raw)
    file_handle: int | None = None

    try:
        dir_ident = _query_handle_identity(dir_handle, phase=phase)
        if not dir_ident.is_directory or dir_ident.is_reparse:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
        if os.path.normcase(dir_ident.final_path) != os.path.normcase(root_lexical):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

        if on_before_open_file is not None:
            on_before_open_file()

        attrs = k32.GetFileAttributesW(expected_exe_lexical)
        if attrs == _FILE_ATTRIBUTE_INVALID:
            raise NativeBridgeRejected(
                "agent_computer_executable_not_found", phase=phase
            )
        if (attrs & _FILE_ATTRIBUTE_REPARSE_POINT) or (
            attrs & _FILE_ATTRIBUTE_DIRECTORY
        ):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

        # Open executable with GENERIC_READ and FILE_SHARE_READ only (no WRITE, no DELETE)
        file_handle_raw = k32.CreateFileW(
            expected_exe_lexical,
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if file_handle_raw in _INVALID_HANDLE_VALUES or not file_handle_raw:
            err = ctypes.get_last_error()
            if err in {2, 3}:
                raise NativeBridgeRejected(
                    "agent_computer_executable_not_found", phase=phase
                )
            raise NativeBridgeRejected("agent_computer_binary_invalid", phase=phase)
        file_handle = int(file_handle_raw)

        ident1 = _query_handle_identity(file_handle, phase=phase)
        if ident1.is_directory or ident1.is_reparse:
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

        # Strict containment: final path must equal dir_ident.final_path + "\" + spec.installed_filename
        expected_final_exe = os.path.join(
            dir_ident.final_path, spec.installed_filename
        )
        if (
            os.path.normcase(ident1.final_path)
            != os.path.normcase(expected_final_exe)
            or os.path.normcase(str(Path(ident1.final_path).parent))
            != os.path.normcase(dir_ident.final_path)
        ):
            raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

        if ident1.size < MIN_BINARY_SIZE_BYTES or ident1.size > MAX_BINARY_SIZE_BYTES:
            raise NativeBridgeRejected("agent_computer_binary_tampered", phase=phase)
        if ident1.size != spec.expected_size:
            raise NativeBridgeRejected("agent_computer_binary_tampered", phase=phase)

        raw_bytes, computed_sha = _read_bounded_bytes_from_handle(
            file_handle,
            max_bytes=spec.expected_size,
            error_code="agent_computer_binary_tampered",
            phase=phase,
        )
        if len(raw_bytes) != spec.expected_size:
            raise NativeBridgeRejected("agent_computer_binary_tampered", phase=phase)

        # Validate PE64 headers from the same read stream before checking or accepting digest
        pe_info = parse_and_validate_pe_headers(
            raw_bytes[:65536],
            file_size=len(raw_bytes),
            expected_machine=spec.expected_machine,
            expected_magic=spec.expected_optional_header_magic,
            phase=phase,
        )

        if computed_sha != spec.expected_sha256:
            raise NativeBridgeRejected("agent_computer_binary_tampered", phase=phase)

        # Re-read handle identity after hashing to confirm zero mutation or path change
        ident2 = _query_handle_identity(file_handle, phase=phase)
        dir_ident2 = _query_handle_identity(dir_handle, phase=phase)
        if (
            ident2.size != ident1.size
            or ident2.volume_serial != ident1.volume_serial
            or ident2.file_id_high != ident1.file_id_high
            or ident2.file_id_low != ident1.file_id_low
            or ident2.last_write_ft != ident1.last_write_ft
            or os.path.normcase(ident2.final_path)
            != os.path.normcase(ident1.final_path)
            or os.path.normcase(dir_ident2.final_path)
            != os.path.normcase(dir_ident.final_path)
        ):
            raise NativeBridgeRejected("agent_computer_binary_tampered", phase=phase)

        if on_after_hash_before_return is not None:
            on_after_hash_before_return()

        release_proof: _ReleaseLeaseProof | None = None
        if raw_commitment is not None:
            if (
                not bool(getattr(sys, "frozen", False))
                or not _validate_spec_matches_commitment(spec, raw_commitment)
            ):
                raise NativeBridgeRejected(
                    "agent_computer_manifest_invalid", phase=phase
                )
            if (
                os.path.normcase(dir_ident2.final_path)
                != os.path.normcase(raw_commitment.fixed_bundle_root)
                or os.path.normcase(ident2.final_path)
                != os.path.normcase(raw_commitment.fixed_executable_path)
            ):
                raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

            if mode == "production":
                release_proof = _ReleaseLeaseProof(
                    _issuer_token=_LEASE_PROOF_ISSUER_TOKEN,
                    commitment=raw_commitment,
                    executable_final_path=ident2.final_path,
                    bundle_root_final_path=dir_ident2.final_path,
                    size=ident2.size,
                    sha256=computed_sha,
                    volume_serial=ident2.volume_serial,
                    file_id_high=ident2.file_id_high,
                    file_id_low=ident2.file_id_low,
                    last_write_ft=ident2.last_write_ft,
                    pe_machine=pe_info.machine,
                    pe_magic=pe_info.optional_header_magic,
                )

        is_release_verified = release_proof is not None
        effective_mode = (
            "production"
            if is_release_verified
            else ("test" if mode == "test" else "development")
        )

        verified_art = VerifiedArtifact(
            mode=effective_mode,
            verified=True,
            release_verified=is_release_verified,
            executable_path=ident2.final_path,
            bundle_root=dir_ident2.final_path,
            size=ident2.size,
            sha256=computed_sha,
            volume_serial=ident2.volume_serial,
            file_id_high=ident2.file_id_high,
            file_id_low=ident2.file_id_low,
            pe_machine=pe_info.machine,
            pe_magic=pe_info.optional_header_magic,
            manifest_sha256=spec.manifest_sha256,
            release_id=spec.release_id,
            worker_name=spec.worker_name,
            worker_version=spec.worker_version,
            protocol_version=spec.protocol_version,
            target_triple=spec.target_triple,
        )

        lease = ArtifactLease(
            verified_artifact=verified_art,
            command=[ident2.final_path],
            file_handle=file_handle,
            dir_handle=dir_handle,
            _release_proof=release_proof,
        )
        file_handle = None
        dir_handle = None
        return lease
    except BaseException:
        _close_win32_handle(file_handle)
        _close_win32_handle(dir_handle)
        raise


def open_dev_executable_lease(
    dev_executable: str | Path | Sequence[str | Path],
    *,
    expected_worker_name: str = EXPECTED_WORKER_NAME,
    expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
    expected_protocol_version: str = PROTOCOL_SEMVER,
    phase: str = "pre_dispatch",
) -> ArtifactLease:
    """Resolve an explicit development/test executable command without claiming `verified=True`."""
    if os.name != "nt":
        raise NativeBridgeRejected("agent_computer_windows_required", phase=phase)

    if isinstance(dev_executable, (list, tuple)):
        if not dev_executable or not all(
            isinstance(part, (str, Path)) and str(part).strip()
            for part in dev_executable
        ):
            raise NativeBridgeRejected(
                "agent_computer_executable_not_found",
                phase=phase,
            )
        raw_exe = str(dev_executable[0])
        rest_args = [str(p) for p in dev_executable[1:]]
    elif isinstance(dev_executable, (str, Path)) and str(dev_executable).strip():
        raw_exe = str(dev_executable)
        rest_args = []
    else:
        raise NativeBridgeRejected(
            "agent_computer_executable_not_found",
            phase=phase,
        )

    lexical_exe = validate_lexical_windows_path(raw_exe, phase=phase)
    _check_no_ancestor_reparse_points(
        lexical_exe,
        missing_code="agent_computer_executable_not_found",
        phase=phase,
    )

    k32 = _get_kernel32()
    fh = k32.CreateFileW(
        lexical_exe,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if fh in _INVALID_HANDLE_VALUES or not fh:
        raise NativeBridgeRejected(
            "agent_computer_executable_not_found",
            phase=phase,
        )
    try:
        ident = _query_handle_identity(int(fh), phase=phase)
    finally:
        _close_win32_handle(int(fh))

    if ident.is_directory or ident.is_reparse:
        raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)

    parent_dir = str(Path(ident.final_path).parent)
    art = VerifiedArtifact(
        mode="development",
        verified=False,
        release_verified=False,
        executable_path=ident.final_path,
        bundle_root=parent_dir,
        size=ident.size,
        sha256="",
        volume_serial=ident.volume_serial,
        file_id_high=ident.file_id_high,
        file_id_low=ident.file_id_low,
        pe_machine=IMAGE_FILE_MACHINE_AMD64,
        pe_magic=IMAGE_NT_OPTIONAL_HDR64_MAGIC,
        manifest_sha256=None,
        release_id=None,
        worker_name=expected_worker_name,
        worker_version=expected_worker_version or EXPECTED_WORKER_VERSION,
        protocol_version=expected_protocol_version,
        target_triple=SUPPORTED_TARGET_TRIPLE,
    )
    return ArtifactLease(
        verified_artifact=art,
        command=[ident.final_path, *rest_args],
        file_handle=None,
        dir_handle=None,
    )


class ProductionBinaryResolver:
    """Authoritative production binary resolver backed by fixed packaged `rynmesh/bin/computer-use-manifest.json`.

    Under a frozen runtime (`sys.frozen is True`), any caller attempt to override
    `manifest_path`, `bundle_root`, `bundled_executable`, `require_frozen=False`, or
    worker/protocol identity is rejected before spawn, and only `_load_packaged_release_spec`
    is used.
    Under a non-frozen runtime (`sys.frozen is False`), default production resolution fails
    closed (`agent_computer_executable_not_found`), and any explicit staged override runs
    strictly in `mode='development'` with `release_verified=False` and `_release_proof=None`.
    """

    mode = "production"

    def __init__(
        self,
        *,
        manifest_path: str | Path | None = None,
        bundle_root: str | Path | None = None,
        bundled_executable: str | Path | None = None,
        expected_worker_name: str = EXPECTED_WORKER_NAME,
        expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
        expected_protocol_version: str = PROTOCOL_SEMVER,
        require_frozen: bool | None = None,
        on_before_open_file: Callable[[], None] | None = None,
        on_after_hash_before_return: Callable[[], None] | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.bundle_root = bundle_root
        self.bundled_executable = bundled_executable
        self.expected_worker_name = expected_worker_name
        self.expected_worker_version = expected_worker_version
        self.expected_protocol_version = expected_protocol_version
        self._explicit_require_frozen = require_frozen
        self.require_frozen = (
            (bundle_root is None and bundled_executable is None)
            if require_frozen is None
            else bool(require_frozen)
        )
        self.on_before_open_file = on_before_open_file
        self.on_after_hash_before_return = on_after_hash_before_return
        self.config_tag = (
            f"prod:{_lexical_norm_path(manifest_path)}:"
            f"{_lexical_norm_path(bundle_root)}:"
            f"{_lexical_norm_path(bundled_executable)}:"
            f"frozen={self.require_frozen}"
        )

    def acquire_lease(self, *, phase: str = "pre_dispatch") -> ArtifactLease:
        verify_windows_x64_host(phase=phase)

        if bool(getattr(sys, "frozen", False)):
            if self._explicit_require_frozen is False:
                raise NativeBridgeRejected("agent_computer_path_invalid", phase=phase)
            if self.expected_worker_name != EXPECTED_WORKER_NAME or (
                self.expected_worker_version is not None
                and self.expected_worker_version != EXPECTED_WORKER_VERSION
            ):
                raise NativeBridgeRejected(
                    "agent_computer_worker_identity_mismatch",
                    phase=phase,
                )
            if self.expected_protocol_version != PROTOCOL_SEMVER:
                raise NativeBridgeRejected(
                    "agent_computer_version_mismatch",
                    phase=phase,
                    reason="version_mismatch",
                )
            if self.manifest_path is not None:
                m_lex = validate_lexical_windows_path(self.manifest_path, phase=phase)
                pkg_lex = validate_lexical_windows_path(
                    PACKAGE_MANIFEST_PATH, phase=phase
                )
                if os.path.normcase(m_lex) != os.path.normcase(pkg_lex):
                    raise NativeBridgeRejected(
                        "agent_computer_path_invalid", phase=phase
                    )
            sys_exe_lex = validate_lexical_windows_path(sys.executable, phase=phase)
            fixed_root_lex = validate_lexical_windows_path(
                Path(sys_exe_lex).parent, phase=phase
            )
            if self.bundle_root is not None:
                b_lex = validate_lexical_windows_path(self.bundle_root, phase=phase)
                if os.path.normcase(b_lex) != os.path.normcase(fixed_root_lex):
                    raise NativeBridgeRejected(
                        "agent_computer_path_invalid", phase=phase
                    )
            if self.bundled_executable is not None:
                e_lex = validate_lexical_windows_path(
                    self.bundled_executable, phase=phase
                )
                fixed_exe_lex = os.path.join(
                    fixed_root_lex, PACKAGED_EXECUTABLE_BASENAME
                )
                if os.path.normcase(e_lex) != os.path.normcase(fixed_exe_lex):
                    raise NativeBridgeRejected(
                        "agent_computer_path_invalid", phase=phase
                    )

            spec = _load_packaged_release_spec(phase=phase)
            commitment = spec._release_commitment
            assert commitment is not None
            return open_and_verify_artifact_lease(
                spec=spec,
                bundle_root=commitment.fixed_bundle_root,
                candidate_executable=commitment.fixed_executable_path,
                mode="production",
                on_before_open_file=self.on_before_open_file,
                on_after_hash_before_return=self.on_after_hash_before_return,
                phase=phase,
            )

        spec = load_manifest(
            self.manifest_path,
            expected_worker_name=self.expected_worker_name,
            expected_worker_version=self.expected_worker_version,
            expected_protocol_version=self.expected_protocol_version,
            source_mode="development",
            phase=phase,
        )

        if self.require_frozen:
            raise NativeBridgeRejected(
                "agent_computer_executable_not_found",
                phase=phase,
            )

        if self.bundle_root is not None:
            target_root: str | Path = self.bundle_root
        elif self.bundled_executable is not None:
            cand_lex = validate_lexical_windows_path(
                self.bundled_executable, phase=phase
            )
            target_root = str(Path(cand_lex).parent)
        else:
            raise NativeBridgeRejected(
                "agent_computer_executable_not_found",
                phase=phase,
            )

        return open_and_verify_artifact_lease(
            spec=spec,
            bundle_root=target_root,
            candidate_executable=self.bundled_executable,
            mode="development",
            on_before_open_file=self.on_before_open_file,
            on_after_hash_before_return=self.on_after_hash_before_return,
            phase=phase,
        )


class DevelopmentBinaryResolver:
    """Explicit staged development binary resolver (full handle/PE/SHA verification, `mode='development'`)."""

    mode = "development"

    def __init__(
        self,
        *,
        staging_root: str | Path,
        manifest_path: str | Path,
        candidate_executable: str | Path | None = None,
        expected_worker_name: str = EXPECTED_WORKER_NAME,
        expected_worker_version: str | None = EXPECTED_WORKER_VERSION,
        expected_protocol_version: str = PROTOCOL_SEMVER,
        on_before_open_file: Callable[[], None] | None = None,
        on_after_hash_before_return: Callable[[], None] | None = None,
    ) -> None:
        self.staging_root = staging_root
        self.manifest_path = manifest_path
        self.candidate_executable = candidate_executable
        self.expected_worker_name = expected_worker_name
        self.expected_worker_version = expected_worker_version
        self.expected_protocol_version = expected_protocol_version
        self.on_before_open_file = on_before_open_file
        self.on_after_hash_before_return = on_after_hash_before_return
        self.config_tag = (
            f"dev_staged:{_lexical_norm_path(manifest_path)}:"
            f"{_lexical_norm_path(staging_root)}:"
            f"{_lexical_norm_path(candidate_executable)}"
        )

    def acquire_lease(self, *, phase: str = "pre_dispatch") -> ArtifactLease:
        verify_windows_x64_host(phase=phase)
        spec = load_manifest(
            self.manifest_path,
            expected_worker_name=self.expected_worker_name,
            expected_worker_version=self.expected_worker_version,
            expected_protocol_version=self.expected_protocol_version,
            source_mode="development",
            phase=phase,
        )
        return open_and_verify_artifact_lease(
            spec=spec,
            bundle_root=self.staging_root,
            candidate_executable=self.candidate_executable,
            mode="development",
            on_before_open_file=self.on_before_open_file,
            on_after_hash_before_return=self.on_after_hash_before_return,
            phase=phase,
        )
