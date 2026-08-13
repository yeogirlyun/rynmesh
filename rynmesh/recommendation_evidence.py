"""Versioned, user-visible evidence packets for recommendation decisions."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

EVIDENCE_PACKET_VERSION = 1

_SIGNAL_LABELS = {
    "content_match": "Content match",
    "publisher_match": "Publisher match",
    "peer_trust": "Network trust",
    "peer_reputation": "Publisher reputation",
    "query_match": "Query match",
    "tag_match": "Interest match",
    "diversity": "Exploration",
    "safety_passed": "Safety scan passed",
    "provenance_signed": "Signed provenance",
    "fresh": "Recently published",
    "source_affinity": "Source preference",
    "new_source": "New source",
}


def build_evidence_packet(
    item: Mapping[str, Any],
    *,
    signals: Iterable[str] = (),
    reviewed_at_unix: float,
) -> dict[str, Any]:
    """Describe exactly which node-visible facts supported a recommendation.

    This packet contains no model inference. It can therefore be shown even
    when optional AI enrichment is disabled or unavailable.
    """

    content_id = str(item.get("content_id") or item.get("item_id") or "")
    basis = str(item.get("review_basis") or "metadata")
    source_name = str(item.get("source_peer_name") or item.get("source_title") or "Unknown")
    platform = str(item.get("source_platform") or item.get("source_kind") or "unknown")
    original_url = str(item.get("external_url") or item.get("link") or "")
    description = str(
        item.get("source_description") or item.get("summary") or item.get("description") or ""
    )
    tags = [str(tag) for tag in item.get("tags", ()) if str(tag).strip()]
    published = item.get("published") or item.get("published_unix")

    observations = [{"field": "title", "label": "Title", "value": str(item.get("title", ""))}]
    if description:
        observations.append(
            {"field": "feed_summary", "label": "Feed summary", "value": description[:500]}
        )
    if tags:
        observations.append({"field": "tags", "label": "Tags", "value": ", ".join(tags[:12])})
    if published:
        observations.append({"field": "published", "label": "Published", "value": str(published)})

    limitations: list[str] = []
    if basis == "metadata":
        limitations.append("Ryn reviewed metadata only, not the full content.")
    elif basis == "preview":
        limitations.append("Ryn reviewed a preview, not the complete content.")
    if str(item.get("safety_outcome", "")) in {"", "unscanned", "pending"}:
        limitations.append("This item has not completed a local safety scan.")
    if str(item.get("provenance_status", "")) in {"", "unsigned", "partial"}:
        limitations.append("The source provenance is not fully verified.")

    citations = []
    if original_url:
        citations.append(
            {"kind": "original", "label": f"Original at {source_name}", "url": original_url}
        )

    unique_signals = list(dict.fromkeys(str(signal) for signal in signals if str(signal)))
    return {
        "version": EVIDENCE_PACKET_VERSION,
        "content_id": content_id,
        "review_basis": basis,
        "reviewed_at_unix": float(reviewed_at_unix),
        "source": {"name": source_name, "platform": platform, "url": original_url},
        "signals": [
            {"kind": signal, "label": _SIGNAL_LABELS.get(signal, signal.replace("_", " ").title())}
            for signal in unique_signals
        ],
        "observations": observations,
        "citations": citations,
        "limitations": limitations,
    }
