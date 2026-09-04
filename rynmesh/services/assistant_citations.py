"""Build citations from material actually included in an Assistant run."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

MAX_QUOTE_CHARS = 240


def citations_for_contexts(contexts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for index, context in enumerate(contexts, start=1):
        text = str(context.get("text", ""))
        if not text:
            continue
        citations.append({
            "citation_id": f"cite_{index}",
            "context_id": str(context.get("id", "")),
            "title": str(context.get("title", "Context")),
            "source_url": context.get("source_url"),
            "source_ref": context.get("source_ref"),
            "locator": {"kind": "character", "start": 0, "end": len(text)},
            "quote": text[:MAX_QUOTE_CHARS],
            "content_hash": str(context.get("content_hash", "")),
        })
    return citations


def validate_citations(
    citations: Sequence[Mapping[str, Any]], contexts: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(context.get("id", "")): context for context in contexts}
    valid: list[dict[str, Any]] = []
    for citation in citations:
        context = by_id.get(str(citation.get("context_id", "")))
        locator = citation.get("locator")
        if context is None or not isinstance(locator, Mapping):
            continue
        end = int(locator.get("end", -1))
        if int(locator.get("start", -1)) < 0 or end > len(str(context.get("text", ""))):
            continue
        if len(str(citation.get("quote", ""))) > MAX_QUOTE_CHARS:
            continue
        valid.append(dict(citation))
    return valid

