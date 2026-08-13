"""Rynmesh v0 types.

Rynmesh is a receipt-backed content mesh for AI agents. These dataclasses
intentionally stay transport-neutral: a local file sync, libp2p network, or
test loopback can all move the same objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

try:
    from importlib.metadata import version as _pkg_version
    RYNMESH_VERSION = _pkg_version("rynmesh")
except Exception:  # not installed as a dist / metadata missing
    RYNMESH_VERSION = "0.0.0+src"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SafetyOutcome(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class MediaAsset:
    clip_id: str
    media_hash: str
    size_bytes: int
    media_type: str = "video/mp4"
    content_kind: str = ""
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    preview_hash: str = ""
    transcript_hash: str = ""
    frame_hashes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["frame_hashes"] = list(self.frame_hashes)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaAsset":
        return cls(
            clip_id=str(data["clip_id"]),
            media_hash=str(data["media_hash"]),
            size_bytes=int(data["size_bytes"]),
            media_type=str(data.get("media_type", "video/mp4")),
            content_kind=str(data.get("content_kind", "")),
            duration_sec=float(data.get("duration_sec", 0.0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            preview_hash=str(data.get("preview_hash", "")),
            transcript_hash=str(data.get("transcript_hash", "")),
            frame_hashes=tuple(data.get("frame_hashes", ())),
        )


@dataclass(frozen=True)
class RunReceipt:
    """Publisher-signed record that an AI/automation run produced this asset.

    Minted locally by the publishing node. External attestation providers
    (e.g. Avaryn) can additionally vouch for content via ``ClipManifest.attestations``.
    """

    run_id: str
    work_id: str
    envelope_hash: str
    workflow: str = ""
    replay_hash: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunReceipt":
        return cls(
            run_id=str(data["run_id"]),
            work_id=str(data["work_id"]),
            envelope_hash=str(data["envelope_hash"]),
            workflow=str(data.get("workflow", "")),
            replay_hash=str(data.get("replay_hash", "")),
            created_at=str(data.get("created_at", now_iso())),
        )


@dataclass(frozen=True)
class SafetyReceipt:
    """A signed verifier result for a content manifest subject."""

    subject_clip_id: str
    scanner_id: str
    scanner_version: str
    policy_version: str
    outcome: SafetyOutcome
    checks: dict[str, Any] = field(default_factory=dict)
    evidence_hashes: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        payload["evidence_hashes"] = list(self.evidence_hashes)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SafetyReceipt":
        return cls(
            subject_clip_id=str(data["subject_clip_id"]),
            scanner_id=str(data["scanner_id"]),
            scanner_version=str(data.get("scanner_version", "")),
            policy_version=str(data.get("policy_version", "")),
            outcome=SafetyOutcome(str(data["outcome"])),
            checks=dict(data.get("checks", {})),
            evidence_hashes=tuple(data.get("evidence_hashes", ())),
            created_at=str(data.get("created_at", now_iso())),
        )


@dataclass(frozen=True)
class ClipManifest:
    """Signed metadata required before a content asset can propagate."""

    asset: MediaAsset
    run_receipt: RunReceipt
    safety_receipts: tuple[dict[str, Any], ...]
    publisher: str
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    manifest_version: str = RYNMESH_VERSION
    created_at: str = field(default_factory=now_iso)
    provenance_chain: tuple[dict[str, Any], ...] = ()
    provenance_head_hash: str = ""
    # Optional signed statements about this content from third-party issuers
    # (safety vendors, verifiable-AI attesters, etc.). Each entry is a
    # SignedPayload dict; receivers decide which issuers to trust and weight.
    attestations: tuple[dict[str, Any], ...] = ()

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "created_at": self.created_at,
            "publisher": self.publisher,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "asset": self.asset.to_dict(),
            "run_receipt": self.run_receipt.to_dict(),
            "safety_receipts": list(self.safety_receipts),
            "provenance_chain": [dict(item) for item in self.provenance_chain],
            "provenance_head_hash": self.provenance_head_hash,
            "attestations": [dict(item) for item in self.attestations],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ClipManifest":
        return cls(
            manifest_version=str(payload.get("manifest_version", RYNMESH_VERSION)),
            created_at=str(payload.get("created_at", now_iso())),
            publisher=str(payload["publisher"]),
            title=str(payload.get("title", "")),
            description=str(payload.get("description", "")),
            tags=tuple(payload.get("tags", ())),
            asset=MediaAsset.from_dict(dict(payload["asset"])),
            run_receipt=RunReceipt.from_dict(dict(payload["run_receipt"])),
            safety_receipts=tuple(dict(item) for item in payload.get("safety_receipts", ())),
            provenance_chain=tuple(dict(item) for item in payload.get("provenance_chain", ())),
            provenance_head_hash=str(payload.get("provenance_head_hash", "")),
            attestations=tuple(dict(item) for item in payload.get("attestations", ())),
        )
