"""Search & Ask — answer the owner's question over what the node knows.

Grounds the model in local evidence only: digest items (web sources the owner
chose) and mesh content metadata. No provider -> an honest "connect a model"
response instead of an error. Returns the webapp's SearchAskResponse shape.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

__all__ = ["answer"]

MAX_EVIDENCE = 12

NO_PROVIDER_TEXT = (
    "No AI model is connected yet. Run Ollama locally (ollama.com) or set "
    "ANTHROPIC_API_KEY, then ask again — answers stay grounded in your own "
    "sources and mesh content."
)


def _keywords(query: str) -> list[str]:
    words = re.findall(r"[\w']+", query.lower())
    return [word for word in words if len(word) > 2]


def _matches(text: str, keywords: Sequence[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in keywords if word in lowered)


def _gather_evidence(
    query: str,
    digest_items: Sequence[Mapping[str, Any]],
    content_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    keywords = _keywords(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in digest_items:
        text = " ".join(
            [str(item.get("title", "")), str(item.get("summary", "")),
             str(item.get("ai_summary", "")), str(item.get("source_title", ""))]
        )
        hits = _matches(text, keywords)
        if hits or not keywords:
            scored.append((hits, {
                "kind": "web",
                "ref": str(item.get("link", "")),
                "title": str(item.get("title", "")),
                "detail": str(item.get("ai_summary") or item.get("summary") or "")[:300],
                "origin": str(item.get("source_title", "")),
            }))
    for item in content_items:
        text = " ".join(
            [str(item.get("title", "")), str(item.get("description", "")),
             " ".join(str(tag) for tag in item.get("tags", ()))]
        )
        hits = _matches(text, keywords)
        if hits:
            scored.append((hits, {
                "kind": "mesh",
                "ref": str(item.get("content_id", "")),
                "title": str(item.get("title", "")),
                "detail": str(item.get("description", ""))[:300],
                "origin": str(item.get("source_peer_name", "") or "mesh"),
            }))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _, entry in scored[:MAX_EVIDENCE]]


def answer(
    query: str,
    *,
    provider: Any | None,
    digest_items: Sequence[Mapping[str, Any]] = (),
    content_items: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    query = str(query or "").strip()
    operations = [
        {"name": "searchDigestItems", "risk": "low", "status": "done"},
        {"name": "searchMeshContent", "risk": "low", "status": "done"},
    ]
    if provider is None:
        return {
            "routing": {
                "text": "The local Ryn node searched your digest and mesh metadata.",
                "operations": operations,
            },
            "assistant": {"text": NO_PROVIDER_TEXT, "cites": [], "suggests": [
                "Install Ollama for a free local model",
                "Or set ANTHROPIC_API_KEY in the node environment",
            ]},
        }

    evidence = _gather_evidence(query, digest_items, content_items)
    operations.append({
        "name": f"{provider.id}.generate ({provider.model})", "risk": "low", "status": "done",
    })
    if evidence:
        numbered = "\n".join(
            f"[{index + 1}] ({entry['kind']}: {entry['origin']}) {entry['title']} — {entry['detail']}"
            for index, entry in enumerate(evidence)
        )
        prompt = (
            f"Question from the node owner: {query}\n\n"
            f"Evidence from their own sources:\n{numbered}\n\n"
            "Answer the question using ONLY this evidence. Reference items as "
            "[1], [2]... where relevant. If the evidence doesn't answer it, say "
            "so plainly and suggest what source to add. Keep it under 150 words."
        )
    else:
        prompt = (
            f"Question from the node owner: {query}\n\n"
            "Their node found no matching items in its sources or mesh content. "
            "Say so in one sentence and suggest what kind of source (RSS feed, "
            "subreddit, YouTube channel) would cover this topic. Under 60 words."
        )
    try:
        text = provider.generate(
            prompt,
            system="You are the owner's private agent on their rynmesh node. "
                   "You only know what their node has gathered; never invent sources.",
            max_tokens=600,
        )
    except Exception as exc:
        text = f"The connected model ({provider.id}) failed: {exc}"
    return {
        "routing": {
            "text": "Answered locally from your node's own evidence — no platform in the middle.",
            "operations": operations,
        },
        "assistant": {
            "text": text,
            "cites": [entry["ref"] for entry in evidence if entry["kind"] == "mesh"],
            "suggests": (
                ["Open the cited items", "Add more sources to widen coverage"]
                if evidence else ["Add a source that covers this topic"]
            ),
        },
    }
