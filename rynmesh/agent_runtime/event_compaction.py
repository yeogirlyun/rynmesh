"""Keep a long conversation's event log bounded instead of refusing new messages.

Events are the owner-facing process log. The model history (messages), receipts
and the operation ledger are never touched here: only display events of older
turns are condensed, so no evidence of what ran is lost.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from . import budget
from .output_policy import encoded

_CONTINUED_SUFFIX = re.compile(r"\s*（续(?: (\d+))?）$")


def _continued_base_and_depth(title: str) -> tuple[str, int]:
    base, hops = str(title or "").strip(), 0
    while (match := _CONTINUED_SUFFIX.search(base)):
        hops = max(hops + 1, int(match.group(1) or 1))
        base = base[:match.start()].rstrip()
    return base, hops


def continuation_depth(title: str) -> int:
    return _continued_base_and_depth(title)[1]


def continued_title(title: str) -> str:
    """"Plan" -> "Plan（续）" -> "Plan（续 2）" -> "Plan（续 3）": one suffix, never stacked."""
    base, hops = _continued_base_and_depth(title)
    hops += 1
    return base[:72] + ("（续）" if hops == 1 else f"（续 {hops}）")

# Older turns keep their owner message, final replies, decisions and errors.
# Their step-by-step process events are condensed into one turn_compacted event.
CONDENSED_TYPES = frozenset({
    "text_delta", "model_started", "tool_started", "tool_result", "turn_decision", "provider_selected",
    "model_retry", "read_retry", "budget_notice", "memory_recalled", "skill_loaded",
})
OWNER_TURN_TYPES = frozenset({"created", "message_added", "information_added", "goal_changed",
                              "input_answered", "unknown_outcome_acknowledged", "message_edited"})
KEEP_RECENT_TURNS = 10
EVENT_SOFT_LIMIT = 700
TASK_BYTES_SOFT_LIMIT = int(1.5 * 1024 * 1024)
EVENT_HARD_LIMIT = 1000
TASK_BYTES_HARD_LIMIT = 2 * 1024 * 1024


def fold_stream_deltas(row: dict[str, Any]) -> int:
    """Drop the finished request's streamed text: its final assistant event holds the text.

    Call right after recording that assistant event; deltas after the latest
    model_started belong to the request that just finished.
    """
    started = max((item["seq"] for item in row["events"] if item.get("type") == "model_started"), default=None)
    if started is None:
        return 0
    before = len(row["events"])
    row["events"] = [item for item in row["events"]
                     if not (item.get("type") == "text_delta" and item["seq"] > started)]
    return before - len(row["events"])


def _compact_before(row: dict[str, Any], cutoff: int) -> int:
    """Shared folding for an eligible prefix, retaining receipt/coverage sources."""
    events = row["events"]
    writes = {(item.get("context_epoch", 0), item["call_id"]) for item in events
              if item.get("type") == "tool_started" and item.get("effect") == "write" and item.get("call_id")}
    kept: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        tools = Counter(str(item.get("tool") or "") for item in pending if item.get("type") == "tool_started")
        tools.pop("", None)
        last = pending[-1]
        summary = {"seq": last["seq"], "type": "turn_compacted", "at": last.get("at"),
                   "context_epoch": last.get("context_epoch", 0), "events": len(pending),
                   "operations": sum(tools.values()), "tools": dict(tools.most_common(20))}
        successes = [item["seq"] for item in pending if budget._successful_tool_result(item)]
        if successes:
            summary["successful_tool_result_seqs"] = successes
        if "run_elapsed_seconds" in last:
            summary["run_elapsed_seconds"] = last["run_elapsed_seconds"]
        kept.append(summary)
        pending.clear()

    for item in events[:cutoff]:
        kind = item.get("type")
        evidence = (kind in {"tool_started", "tool_result"} and
                    (item.get("effect") == "write" or (item.get("context_epoch", 0), item.get("call_id")) in writes))
        evidence = evidence or (kind == "tool_result" and item.get("tool") == "ryn_file_read")
        if evidence or kind in OWNER_TURN_TYPES:
            flush()
            kept.append(item)
        elif item.get("type") in CONDENSED_TYPES:
            pending.append(item)
        else:
            flush()
            kept.append(item)
    flush()
    removed = cutoff - len(kept)
    if removed > 0:
        row["events"] = kept + events[cutoff:]
    return max(0, removed)


def compact_old_turns(row: dict[str, Any], *, keep_turns: int = KEEP_RECENT_TURNS) -> int:
    """Condense process events of all but the latest turns. Returns events removed."""
    starts = [index for index, item in enumerate(row["events"]) if item.get("type") in OWNER_TURN_TYPES]
    return _compact_before(row, starts[-keep_turns]) if len(starts) > keep_turns else 0


def compact_in_turn(row: dict[str, Any], *, keep_events: int = 150) -> int:
    """Fold only checkpoint-covered events outside the recent display suffix."""
    events = row["events"]
    checkpoint = next((index for index in range(len(events) - 1, -1, -1)
                       if events[index].get("type") == "context_compacted"), None)
    if checkpoint is None:
        return 0
    return _compact_before(row, min(checkpoint, max(0, len(events) - keep_events)))


def note_near_limit(row: dict[str, Any], size_bytes: int | None = None, *, reserve_events: int = 0) -> None:
    """Warn once; a live task must still accept owner steering and finish work."""
    if (over_hard_limit(row, size_bytes or 0, reserve_events=reserve_events)
            and not any(item.get("type") == "event_log_near_limit" for item in row["events"])):
        from .protocol import event
        event(row, "event_log_near_limit", event_count=len(row["events"]),
              **({"size_bytes": size_bytes} if size_bytes is not None else {}))


def check_in_turn_limits(row: dict[str, Any]) -> None:
    """Count every model step; measure full row bytes only every twenty steps."""
    checked = len(row["events"]) >= EVENT_SOFT_LIMIT
    if checked:
        compact_in_turn(row)
    size = None
    if row["steps"] % 20 == 0:
        size = len(encoded(row))
        if size >= TASK_BYTES_SOFT_LIMIT and not checked and compact_in_turn(row):
            size = len(encoded(row))
    note_near_limit(row, size, reserve_events=1)


def compact_until_small(row: dict[str, Any]) -> int:
    """Condense older turns, keeping fewer whole turns while the log stays large."""
    removed = 0
    for keep in (KEEP_RECENT_TURNS, 5, 2, 1):
        removed += compact_old_turns(row, keep_turns=keep)
        if len(row["events"]) < EVENT_SOFT_LIMIT:
            break
    return removed


def needs_compaction(row: dict[str, Any], size_bytes: int) -> bool:
    return len(row["events"]) >= EVENT_SOFT_LIMIT or size_bytes >= TASK_BYTES_SOFT_LIMIT


def over_hard_limit(row: dict[str, Any], size_bytes: int, *, reserve_events: int = 0) -> bool:
    return len(row["events"]) + reserve_events >= EVENT_HARD_LIMIT or size_bytes > TASK_BYTES_HARD_LIMIT


def continuation_summary(row: dict[str, Any], *, limit: int = 12000) -> str:
    """A bounded, deterministic summary for continuing in a new conversation (no model call)."""
    parts: list[str] = []
    goal = (row.get("goal") or {}).get("text") if isinstance(row.get("goal"), dict) else None
    first_owner = next((m.get("content") for m in row.get("messages", [])
                        if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    original = (goal if isinstance(goal, str) and goal.strip() else first_owner or "").strip()
    if original:
        parts.append("Original request:\n" + original[:1500])
    checkpoint = row.get("context_checkpoint")
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("content"), str) and checkpoint["content"].strip():
        parts.append("Earlier summary:\n" + checkpoint["content"].strip()[:limit // 2])
    exchanges: list[str] = []
    for message in row.get("messages", []):
        if message.get("role") != "user" and message.get("context_epoch", 0) != row.get("context_epoch", 0):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if message.get("role") == "user":
            exchanges.append("Owner: " + content.strip()[:800])
        elif message.get("role") == "assistant" and not message.get("tool_calls"):
            exchanges.append("Ryn: " + content.strip()[:1200])
    recent: list[str] = []
    budget = limit - sum(len(part) for part in parts)
    for line in reversed(exchanges):
        if budget - len(line) < 0:
            break
        recent.append(line)
        budget -= len(line) + 1
    if recent:
        parts.append("Most recent exchanges:\n" + "\n".join(reversed(recent)))
    return "\n\n".join(parts)[:limit]
