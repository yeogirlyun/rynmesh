"""Bounded, opaque context snapshots for the local Assistant workspace."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_CONTEXT_BYTES = 4 * 1024 * 1024
MAX_RUN_CONTEXT_BYTES = 48 * 1024
DEFAULT_INPUT_BUDGET_BYTES = 12 * 1024
CONTEXT_TTL_SECONDS = 7 * 24 * 60 * 60


class AssistantContextError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise AssistantContextError(
            "assistant_pdf_support_unavailable",
            "PDF text extraction is not installed in this build.",
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        raise AssistantContextError(
            "assistant_context_invalid_pdf", "The selected PDF could not be read safely."
        ) from exc
    text = "\n\n".join(page for page in pages if page)
    if not text:
        raise AssistantContextError(
            "assistant_context_empty", "The selected PDF does not contain extractable text."
        )
    return text


def _decode_body(kind: str, body: Mapping[str, Any]) -> str:
    if kind in {"text", "markdown", "content-item", "reader-article", "library-item"}:
        text = str(body.get("text", ""))
    elif kind == "pdf":
        encoded = str(body.get("data_base64", ""))
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise AssistantContextError(
                "assistant_context_invalid_pdf", "The selected PDF is not valid base64 data."
            ) from exc
        if len(raw) > MAX_CONTEXT_BYTES:
            raise AssistantContextError(
                "assistant_context_too_large", "The selected PDF is larger than the local limit."
            )
        text = _extract_pdf(raw)
    else:
        raise AssistantContextError(
            "assistant_context_unsupported", "This context type is not supported."
        )
    text = text.replace("\x00", "").strip()
    if not text:
        raise AssistantContextError("assistant_context_empty", "The selected context is empty.")
    if len(text.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise AssistantContextError(
            "assistant_context_too_large", "The selected context is larger than the local limit."
        )
    return text


def _truncate_utf8(text: str, budget: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, False
    candidate = encoded[: max(0, budget)].decode("utf-8", errors="ignore")
    # Prefer a paragraph/sentence boundary without discarding most of the budget.
    floor = max(0, len(candidate) // 2)
    cut = max(candidate.rfind("\n\n", floor), candidate.rfind("。", floor), candidate.rfind(". ", floor))
    if cut > floor:
        candidate = candidate[: cut + 1]
    return candidate.rstrip(), True


class AssistantContextStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.purge_expired()

    def purge_expired(self) -> int:
        removed = 0
        now = time.time()
        for meta_path in self.root.glob("ctx_*.json"):
            try:
                record = dict(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if float(record.get("expires_at", 0)) >= now:
                continue
            body_path = self.root / f"{meta_path.stem}.txt"
            meta_path.unlink(missing_ok=True)
            body_path.unlink(missing_ok=True)
            removed += 1
        return removed

    def prepare(self, body: Mapping[str, Any]) -> dict[str, Any]:
        self.purge_expired()
        kind = str(body.get("kind", "")).strip()
        owner = str(body.get("conversation_id", "")).strip()
        if not owner or len(owner) > 160:
            raise AssistantContextError(
                "assistant_context_owner_required", "A conversation is required for this context."
            )
        text = _decode_body(kind, body)
        context_id = f"ctx_{uuid.uuid4().hex}"
        now = time.time()
        body_path = self.root / f"{context_id}.txt"
        body_path.write_text(text, encoding="utf-8")
        os.chmod(body_path, 0o600)
        content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        record = {
            "version": "ryn.assistant-context.v1",
            "id": context_id,
            "local_ref": context_id,
            "kind": kind,
            "title": str(body.get("title", "Context"))[:200],
            "conversation_id": owner,
            "source_url": str(body.get("source_url", ""))[:2048] or None,
            "source_ref": str(body.get("source_ref", ""))[:256] or None,
            "content_hash": content_hash,
            "size_bytes": len(text.encode("utf-8")),
            "created_at": now,
            "expires_at": now + CONTEXT_TTL_SECONDS,
        }
        _atomic_json(self.root / f"{context_id}.json", record)
        return {key: value for key, value in record.items() if key != "conversation_id"}

    def read(self, local_ref: str, *, conversation_id: str) -> tuple[dict[str, Any], str]:
        if not local_ref.startswith("ctx_") or not local_ref[4:].isalnum():
            raise AssistantContextError(
                "assistant_context_missing", "The selected context is no longer available."
            )
        meta_path = (self.root / f"{local_ref}.json").resolve()
        body_path = (self.root / f"{local_ref}.txt").resolve()
        if meta_path.parent != self.root or body_path.parent != self.root:
            raise AssistantContextError(
                "assistant_context_missing", "The selected context is no longer available."
            )
        try:
            record = dict(json.loads(meta_path.read_text(encoding="utf-8")))
            text = body_path.read_text(encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            raise AssistantContextError(
                "assistant_context_missing", "The selected context is no longer available."
            ) from exc
        if record.get("conversation_id") != conversation_id:
            raise AssistantContextError(
                "assistant_context_missing", "The selected context belongs to another conversation."
            )
        if float(record.get("expires_at", 0)) < time.time():
            raise AssistantContextError(
                "assistant_context_missing", "The selected context has expired. Add it again."
            )
        actual = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual != record.get("content_hash"):
            raise AssistantContextError(
                "assistant_context_missing", "The selected context failed its integrity check."
            )
        return record, text

    def budget(
        self,
        refs: Iterable[str],
        *,
        conversation_id: str,
        budget_bytes: int = DEFAULT_INPUT_BUDGET_BYTES,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        remaining = min(max(0, int(budget_bytes)), MAX_RUN_CONTEXT_BYTES)
        included: list[dict[str, Any]] = []
        original_bytes = 0
        included_bytes = 0
        truncated = False
        for local_ref in refs:
            record, text = self.read(str(local_ref), conversation_id=conversation_id)
            size = len(text.encode("utf-8"))
            original_bytes += size
            if remaining <= 0:
                truncated = True
                continue
            excerpt, clipped = _truncate_utf8(text, remaining)
            used = len(excerpt.encode("utf-8"))
            if not excerpt:
                truncated = True
                continue
            included.append({
                **{key: value for key, value in record.items() if key != "conversation_id"},
                "text": excerpt,
                "included_bytes": used,
                "original_bytes": size,
                "truncated": clipped,
            })
            included_bytes += used
            remaining -= used
            truncated = truncated or clipped
        return included, {
            "original_bytes": original_bytes,
            "included_bytes": included_bytes,
            "estimated_tokens_upper_bound": included_bytes,
            "truncated": truncated,
        }
