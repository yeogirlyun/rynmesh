"""Bridge from the node's content view to the pure recommender pipeline.

Takes the webapp-shaped item dicts that ``/api/local/content`` serves (see
``peer_http.network_content``) so every recommendation's ``contentId`` is
guaranteed to resolve against the same list the Recommendations screen renders.

Signals used today (all local, all already on the node):
- trust: the publisher's credit-ledger ``distribution_weight``, max-normalized
  across the candidate set. EigenTrust over consumer-attested serve receipts
  plugs in behind the same ``Recommender.trust`` mapping when the receipt
  graph is wired (P4).
- user preference: implicit — tags of content the owner published or fetched
  become ``liked_tags``; fetched items are never re-recommended.
- exploration: ``CreditPolicy.exploration_fraction`` of the slate is reserved
  for newcomer publishers (zero credit score) per vision OQ-2.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .credits import CreditPolicy
from .recommendation_evidence import build_evidence_packet
from .recommender import (
    BaselineRanker,
    Candidate,
    DedupFilter,
    DismissedContentFilter,
    DismissedPublisherFilter,
    PeerListSource,
    Recommender,
    SafetyFilter,
    UserState,
)

__all__ = ["recommend_from_items"]

# webapp safety labels -> pipeline outcomes. Unscanned content is not blocked
# (nothing negative is known) but earns no safety_passed evidence either.
_SAFETY_TO_PIPELINE = {"passed": "pass", "flagged": "warn", "blocked": "block"}

# Tags injected automatically at publish time carry no taste signal.
_GENERIC_TAGS = {"ai-generated", "rynmesh"}
_GENERIC_TAG_PREFIXES = ("category:", "content-kind:")
_TERM = re.compile(r"[a-z0-9][a-z0-9+.-]{1,31}")


def _interest_tags(tags: Iterable[Any]) -> tuple[str, ...]:
    kept = []
    for tag in tags:
        text = str(tag).strip().lower()
        if text in _GENERIC_TAGS or text.startswith(_GENERIC_TAG_PREFIXES):
            continue
        kept.append(text)
    return tuple(kept)


def _candidate_tags(item: Mapping[str, Any]) -> tuple[str, ...]:
    tags = list(_interest_tags(item.get("tags", ())))
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    tags.extend(f"term:{word}" for word in _TERM.findall(text))
    platform = str(item.get("source_platform", "") or "").strip().lower()
    if platform:
        tags.append(f"platform:{platform}")
    return tuple(dict.fromkeys(tags))


def _published_unix(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _matches_query(item: Mapping[str, Any], query: str) -> bool:
    haystack = " ".join(
        [
            str(item.get("title", "")),
            str(item.get("description", "")),
            " ".join(str(tag) for tag in item.get("tags", ())),
        ]
    ).lower()
    return query in haystack


def recommend_from_items(
    items: Sequence[Mapping[str, Any]],
    *,
    now_unix: float,
    query: str = "",
    limit: int = 6,
    profile: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank not-yet-fetched network content for the node owner.

    ``items`` is the ``/api/local/content`` item list; returns the webapp's
    ``Recommendation[]`` payload (id, contentId, priority, reason, evidence,
    novelty, uncertainty, review_basis).
    """
    query = str(query or "").strip().lower()
    limit = max(1, int(limit))
    profile = dict(profile or {})
    profile_tag_weights = {
        str(tag).strip().lower(): float(weight)
        for tag, weight in dict(profile.get("tag_weights", {})).items()
    }
    profile_publisher_weights = {
        str(peer): float(weight)
        for peer, weight in dict(profile.get("publisher_weights", {})).items()
    }
    hidden_content_ids = {str(value) for value in profile.get("hidden_content_ids", ())}

    by_id: dict[str, Mapping[str, Any]] = {}
    owned_tags: list[str] = []
    known_publishers: set[str] = set()
    fetched_ids: set[str] = set()
    candidates: list[Candidate] = []
    query_matched: set[str] = set()

    for item in items:
        content_id = str(item.get("content_id", ""))
        if not content_id:
            continue
        fetch_status = str(item.get("fetch_status", ""))
        publisher = str(item.get("publisher_peer_id", ""))
        if fetch_status in {"local", "fetched_full"}:
            # Owner interest signal; never a recommendation candidate.
            owned_tags.extend(_interest_tags(item.get("tags", ())))
            known_publishers.add(publisher)
            if fetch_status == "fetched_full":
                fetched_ids.add(content_id)
            continue
        if query:
            if not _matches_query(item, query):
                continue
            query_matched.add(content_id)
        by_id[content_id] = item
        candidates.append(
            Candidate(
                content_id=content_id,
                publisher_peer_id=publisher,
                title=str(item.get("title", "")),
                tags=_candidate_tags(item),
                safety_outcome=_SAFETY_TO_PIPELINE.get(str(item.get("safety_outcome", "")), "pass"),
                published_at_unix=_published_unix(item.get("published")),
                summary=str(item.get("description", "")),
            )
        )

    if not candidates:
        return []

    liked_tags: dict[str, float] = {}
    for tag in owned_tags:
        liked_tags[tag] = liked_tags.get(tag, 0.0) + 1.0
    for tag, weight in profile_tag_weights.items():
        liked_tags[tag] = liked_tags.get(tag, 0.0) + weight

    weights = {
        str(item.get("publisher_peer_id", "")): float(item.get("distribution_weight", 0.0) or 0.0)
        for item in by_id.values()
    }
    max_weight = max(weights.values(), default=0.0)
    trust = {
        peer: (weight / max_weight if max_weight > 0 else 0.0) for peer, weight in weights.items()
    }
    for peer, weight in profile_publisher_weights.items():
        trust[peer] = min(1.0, max(-1.0, trust.get(peer, 0.0) + weight * 0.2))
    credit_scores = {
        str(item.get("publisher_peer_id", "")): float(item.get("credit_score", 0.0) or 0.0)
        for item in by_id.values()
    }

    user = UserState(
        liked_tags=liked_tags,
        fetched_content_ids=fetched_ids,
        now_unix=float(now_unix),
    )
    recommender = Recommender(
        sources=[PeerListSource({"network": candidates})],
        trust=trust,
        ranker=BaselineRanker(),
        filters=[
            SafetyFilter(),
            DedupFilter(),
            DismissedContentFilter(hidden_content_ids),
            DismissedPublisherFilter(),
        ],
        exploration_fraction=CreditPolicy().exploration_fraction,
        newcomer_predicate=lambda cand: credit_scores.get(cand.publisher_peer_id, 0.0) <= 0.0,
    )
    # Rank the complete visible set before choosing the best item per format.
    # Sampling only the first few high-score results can make an article-heavy
    # feed hide otherwise strong audio, video, or image recommendations.
    ranked_pool = recommender.recommend(user, k=len(candidates))
    best_by_format: dict[str, tuple[Candidate, float]] = {}
    for candidate, score in ranked_pool:
        content_kind = str(by_id[candidate.content_id].get("content_kind", "document"))
        best_by_format.setdefault(content_kind, (candidate, score))
    selected = list(best_by_format.values())[:limit]
    selected_ids = {candidate.content_id for candidate, _ in selected}
    for candidate, score in ranked_pool:
        if len(selected) >= limit:
            break
        if candidate.content_id not in selected_ids:
            selected.append((candidate, score))
            selected_ids.add(candidate.content_id)
    ranked = selected
    if not ranked:
        return []

    max_score = max(score for _, score in ranked)
    min_score = min(score for _, score in ranked)
    results: list[dict[str, Any]] = []
    for index, (cand, score) in enumerate(ranked):
        item = by_id[cand.content_id]
        is_starter = bool(item.get("starter"))
        is_external = bool(item.get("external"))
        peer_trust = trust.get(cand.publisher_peer_id, 0.0)
        tag_hits = sorted(tag for tag in cand.tags if liked_tags.get(tag, 0.0) > 0)
        is_newcomer = (
            not is_starter
            and not is_external
            and credit_scores.get(cand.publisher_peer_id, 0.0) <= 0.0
        )

        evidence: list[str] = []
        reasons: list[str] = []
        if tag_hits:
            evidence.append("tag_match")
            visible_hits = list(
                dict.fromkeys(
                    tag.removeprefix("term:").removeprefix("platform:") for tag in tag_hits
                )
            )
            reasons.append("matches your interests (" + ", ".join(visible_hits[:3]) + ")")
        if cand.content_id in query_matched:
            evidence.append("query_match")
            reasons.append("matches your query")
        if peer_trust >= 0.5 and not is_external:
            evidence.append("peer_trust")
            reasons.append("publisher carries high network trust")
        if credit_scores.get(cand.publisher_peer_id, 0.0) > 0 and not is_external:
            evidence.append("peer_reputation")
        if str(item.get("safety_outcome", "")) == "passed":
            evidence.append("safety_passed")
        if item.get("provenance_head_hash"):
            evidence.append("provenance_signed")
        if is_newcomer:
            evidence.append("diversity")
            reasons.append("newcomer publisher (exploration slot)")
        if is_starter:
            evidence.append("diversity")
            if not reasons:
                platform = str(item.get("source_platform", "") or "broad sources")
                reasons.append(
                    f"broad starter choice from {platform}; use feedback to shape your feed"
                )
        if is_external and not reasons:
            source = str(item.get("source_platform", "") or "a public source")
            reasons.append(f"fresh item from Ryn's public discovery catalog ({source})")
        if not reasons:
            reasons.append("recent content from the mesh")

        safety_label = str(item.get("safety_outcome", ""))
        if safety_label == "flagged":
            uncertainty = "Safety scan flagged this content — review before fetching."
        elif safety_label == "unscanned":
            uncertainty = "Not yet safety-scanned."
        else:
            uncertainty = None

        novelty = (
            "A starter choice for teaching your local recommendation agent."
            if is_starter
            else None
            if is_external
            else "First recommendation from this publisher for you."
            if cand.publisher_peer_id not in known_publishers
            else None
        )

        if abs(max_score - min_score) < 1e-12:
            priority = 1.0 - (index / max(1, len(ranked))) * 0.35
        else:
            priority = score / max_score if max_score > 0 else 1.0 - index / max(1, len(ranked))
        results.append(
            {
                "id": "rec_" + hashlib.sha256(cand.content_id.encode("utf-8")).hexdigest()[:10],
                "contentId": cand.content_id,
                "priority": round(min(max(priority, 0.0), 1.0), 3),
                "reason": (
                    reasons[0][:1].upper() + reasons[0][1:] + "; " + "; ".join(reasons[1:])
                ).rstrip("; ")
                if len(reasons) > 1
                else reasons[0][:1].upper() + reasons[0][1:] + ".",
                "evidence": evidence,
                "novelty": novelty,
                "uncertainty": uncertainty,
                "review_basis": str(item.get("review_basis", "") or "metadata"),
                "evidence_packet": build_evidence_packet(
                    item,
                    signals=evidence,
                    reviewed_at_unix=now_unix,
                ),
                "item": dict(item),
            }
        )
    return results
