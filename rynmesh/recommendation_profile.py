"""Persistent, local-first recommendation preferences and starter choices."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "PLATFORM_CHOICES",
    "TOPIC_CHOICES",
    "RecommendationProfileStore",
    "starter_items",
]

TOPIC_CHOICES: tuple[dict[str, Any], ...] = (
    {"id": "ai-agents", "label": "AI agents", "tags": ["ai", "agents", "automation"]},
    {"id": "open-source", "label": "Open source", "tags": ["open-source", "software", "code"]},
    {"id": "research", "label": "Research", "tags": ["research", "science", "papers"]},
    {"id": "technology", "label": "Technology", "tags": ["technology", "engineering", "startups"]},
    {"id": "creative", "label": "Creative work", "tags": ["art", "design", "music", "video"]},
    {"id": "business", "label": "Business", "tags": ["business", "markets", "strategy"]},
    {"id": "learning", "label": "Learning", "tags": ["education", "tutorial", "explainer"]},
    {"id": "culture", "label": "Culture", "tags": ["culture", "society", "history"]},
)

PLATFORM_CHOICES: tuple[dict[str, str], ...] = (
    {"id": "rynmesh", "label": "RynMesh"},
    {"id": "github", "label": "GitHub"},
    {"id": "arxiv", "label": "arXiv"},
    {"id": "youtube", "label": "YouTube"},
    {"id": "rss", "label": "RSS & blogs"},
    {"id": "podcasts", "label": "Podcasts"},
    {"id": "news", "label": "News"},
)

_TOPICS = {item["id"]: item for item in TOPIC_CHOICES}
_PLATFORMS = {item["id"]: item for item in PLATFORM_CHOICES}
_ACTIONS = {"more", "less", "hide", "neutral"}
_WORD = re.compile(r"[a-z0-9][a-z0-9+.-]{1,31}")
_STOP_WORDS = {
    "about",
    "and",
    "are",
    "for",
    "from",
    "into",
    "more",
    "that",
    "the",
    "this",
    "with",
    "would",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _defaults() -> dict[str, Any]:
    return {
        "version": 1,
        "direction": "",
        "topics": [],
        "platforms": [],
        "feedback": {},
        "updated_at": "",
    }


def _clean_ids(values: Any, allowed: Mapping[str, Any]) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value) for value in values if str(value) in allowed))


class RecommendationProfileStore:
    """Atomic JSON store owned entirely by the local node."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self) -> dict[str, Any]:
        data = _defaults()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            data["direction"] = str(loaded.get("direction", "") or "")[:2000]
            data["topics"] = _clean_ids(loaded.get("topics"), _TOPICS)
            data["platforms"] = _clean_ids(loaded.get("platforms"), _PLATFORMS)
            feedback = loaded.get("feedback", {})
            if isinstance(feedback, dict):
                data["feedback"] = {
                    str(key)[:256]: dict(value)
                    for key, value in feedback.items()
                    if isinstance(value, dict) and value.get("action") in _ACTIONS
                }
            data["updated_at"] = str(loaded.get("updated_at", "") or "")
        return data

    def public(self) -> dict[str, Any]:
        data = self.get()
        signals = self.signals(data)
        return {
            **data,
            "topic_choices": list(TOPIC_CHOICES),
            "platform_choices": list(PLATFORM_CHOICES),
            "learned_signals": len(signals["tag_weights"]),
            "feedback_count": len(data["feedback"]),
        }

    def patch(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        data = self.get()
        if "direction" in patch:
            data["direction"] = str(patch.get("direction", "") or "").strip()[:2000]
        if "topics" in patch:
            data["topics"] = _clean_ids(patch.get("topics"), _TOPICS)
        if "platforms" in patch:
            data["platforms"] = _clean_ids(patch.get("platforms"), _PLATFORMS)
        data["updated_at"] = _now()
        self._write(data)
        return self.public()

    def feedback(self, item: Mapping[str, Any], action: str) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in _ACTIONS:
            raise ValueError("recommendation_feedback_action_invalid")
        content_id = str(item.get("content_id", "") or "")
        if not content_id:
            raise ValueError("recommendation_content_id_required")
        data = self.get()
        if action == "neutral":
            data["feedback"].pop(content_id, None)
        else:
            tags = item.get("tags", [])
            data["feedback"][content_id] = {
                "action": action,
                "tags": [str(tag)[:64] for tag in tags if str(tag).strip()][:32]
                if isinstance(tags, list)
                else [],
                "publisher": str(item.get("publisher_peer_id", "") or "")[:256],
                "platform": str(item.get("source_platform", "") or "")[:64],
                "updated_at": _now(),
            }
        data["updated_at"] = _now()
        self._write(data)
        return self.public()

    def signals(self, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        profile = dict(data or self.get())
        tag_weights: dict[str, float] = {}
        publisher_weights: dict[str, float] = {}
        hidden_content_ids: set[str] = set()

        def add_tag(tag: str, weight: float) -> None:
            clean = str(tag or "").strip().lower()
            if clean:
                tag_weights[clean] = tag_weights.get(clean, 0.0) + weight

        for topic_id in profile.get("topics", []):
            for tag in _TOPICS.get(str(topic_id), {}).get("tags", []):
                add_tag(str(tag), 1.5)
        for platform_id in profile.get("platforms", []):
            add_tag(f"platform:{platform_id}", 1.25)
        for word in _WORD.findall(str(profile.get("direction", "")).lower()):
            if word not in _STOP_WORDS:
                add_tag(f"term:{word}", 1.0)

        feedback = profile.get("feedback", {})
        if isinstance(feedback, dict):
            for content_id, record in feedback.items():
                if not isinstance(record, dict):
                    continue
                action = str(record.get("action", ""))
                weight = 2.0 if action == "more" else -1.5 if action == "less" else 0.0
                if action == "hide":
                    hidden_content_ids.add(str(content_id))
                    weight = -2.0
                for tag in record.get("tags", []):
                    add_tag(str(tag), weight)
                platform = str(record.get("platform", "") or "")
                if platform:
                    add_tag(f"platform:{platform}", weight)
                publisher = str(record.get("publisher", "") or "")
                if publisher:
                    publisher_weights[publisher] = publisher_weights.get(publisher, 0.0) + weight
        return {
            "tag_weights": tag_weights,
            "publisher_weights": publisher_weights,
            "hidden_content_ids": sorted(hidden_content_ids),
        }

    def _write(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


_STARTERS: tuple[dict[str, Any], ...] = (
    {"id": "agents", "title": "AI agents and practical automation", "description": "Open-source agent workflows, local assistants, and tools that do useful work.", "tags": ["ai", "agents", "automation", "open-source"], "platform": "rynmesh", "kind": "code"},
    {"id": "oss", "title": "Interesting open-source projects", "description": "New repositories and engineering work worth inspecting.", "tags": ["open-source", "software", "code", "technology"], "platform": "github", "kind": "code"},
    {"id": "papers", "title": "New research explained clearly", "description": "Recent papers, technical results, and accessible research summaries.", "tags": ["research", "science", "papers", "learning"], "platform": "arxiv", "kind": "document"},
    {"id": "video", "title": "Thoughtful long-form video", "description": "Technical talks, documentaries, and deep explanations instead of short-lived trends.", "tags": ["video", "learning", "culture", "technology"], "platform": "youtube", "kind": "video"},
    {"id": "independent", "title": "Independent writing and analysis", "description": "Essays and specialist blogs selected for depth and original thinking.", "tags": ["writing", "analysis", "culture", "business"], "platform": "rss", "kind": "document"},
    {"id": "podcasts", "title": "Podcasts matched to your interests", "description": "Conversations and interviews that reward sustained attention.", "tags": ["podcast", "learning", "business", "culture"], "platform": "podcasts", "kind": "audio"},
    {"id": "tech-news", "title": "A calmer technology briefing", "description": "Important technology developments without repetitive headlines.", "tags": ["technology", "engineering", "startups", "news"], "platform": "news", "kind": "report"},
    {"id": "creative", "title": "Creative work outside your usual feed", "description": "Design, music, visual experiments, and unfamiliar creators for healthy exploration.", "tags": ["art", "design", "music", "creative"], "platform": "rynmesh", "kind": "image"},
)


def starter_items(
    profile: Mapping[str, Any],
    *,
    seed_key: str,
    now_unix: float,
) -> list[dict[str, Any]]:
    """Return a broad, daily-rotating local starter slate.

    These are preference choices, not fabricated remote content. The UI marks
    them as starter suggestions and only offers feedback actions.
    """
    day = datetime.fromtimestamp(now_unix, UTC).date().isoformat()
    seed = int(hashlib.sha256(f"{seed_key}:{day}".encode()).hexdigest()[:16], 16)
    entries = list(_STARTERS)
    random.Random(seed).shuffle(entries)
    out: list[dict[str, Any]] = []
    for entry in entries:
        content_id = f"starter:{entry['id']}"
        out.append(
            {
                "content_id": content_id,
                "manifest_hash": "",
                "title": entry["title"],
                "description": entry["description"],
                "tags": [*entry["tags"], f"platform:{entry['platform']}"],
                "content_kind": entry["kind"],
                "content_type": "application/x-rynmesh-starter",
                "publisher_peer_id": "rynmesh-starter",
                "provider_peer_id": "rynmesh-starter",
                "source_peer_name": "Ryn starter guide",
                "source_platform": entry["platform"],
                "identity_tier": "attested",
                "credit_score": 0.0,
                "distribution_weight": 0.0,
                "safety_outcome": "passed",
                "provenance_status": "signed",
                "provenance_head_hash": None,
                "fetch_status": "discovered",
                "review_basis": "metadata",
                "size": "",
                "published": datetime.fromtimestamp(now_unix, UTC).isoformat(),
                "starter": True,
            }
        )
    return out
