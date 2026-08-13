"""Rynmesh local safety scanning primitives.

Phase 1 uses a deterministic scanner interface so the propagation rule can be
tested without depending on any particular moderation vendor. Real deployments
can plug in hash-matching feeds, perceptual hashes, classifiers, or local
model checks behind the same receipt shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .crypto import SignedPayload, sign_payload
from .types import SafetyOutcome, SafetyReceipt


class SafetyScanner(Protocol):
    scanner_id: str
    scanner_version: str
    policy_version: str

    def scan(self, *, clip_id: str, transcript: str = "") -> SafetyReceipt: ...


@dataclass
class KeywordSafetyScanner:
    """Small deterministic scanner used for local tests and demos."""

    blocked_terms: tuple[str, ...] = ()
    warning_terms: tuple[str, ...] = ()
    scanner_id: str = "rynmesh.keyword"
    scanner_version: str = "0.1"
    policy_version: str = "rynmesh-phase1"

    def scan(self, *, clip_id: str, transcript: str = "") -> SafetyReceipt:
        lowered = transcript.lower()
        blocked = sorted(term for term in self.blocked_terms if term.lower() in lowered)
        warnings = sorted(term for term in self.warning_terms if term.lower() in lowered)
        if blocked:
            outcome = SafetyOutcome.BLOCK
        elif warnings:
            outcome = SafetyOutcome.WARN
        else:
            outcome = SafetyOutcome.PASS
        return SafetyReceipt(
            subject_clip_id=clip_id,
            scanner_id=self.scanner_id,
            scanner_version=self.scanner_version,
            policy_version=self.policy_version,
            outcome=outcome,
            checks={
                "blocked_terms": blocked,
                "warning_terms": warnings,
                "transcript_chars": len(transcript),
            },
        )


@dataclass
class SafetyPolicy:
    """Minimum receipt policy for honest Rynmesh peers."""

    min_pass_receipts: int = 1
    allow_warnings: bool = True
    trusted_scanners: set[str] = field(default_factory=set)

    def receipt_trusted(self, receipt: SafetyReceipt) -> bool:
        return not self.trusted_scanners or receipt.scanner_id in self.trusted_scanners


def sign_safety_receipt(
    receipt: SafetyReceipt,
    *,
    private_key_bytes: bytes,
) -> SignedPayload:
    return sign_payload(receipt.to_dict(), private_key_bytes=private_key_bytes)
