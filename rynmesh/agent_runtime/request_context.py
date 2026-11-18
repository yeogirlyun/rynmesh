"""Pure request snapshots; durable task history and permissions stay with the host."""
from __future__ import annotations

import copy
import json

MISSING_TOOL_OUTPUT = json.dumps({
    "error": "agent_tool_output_missing",
    "execution_status": "unknown",
    "projection_only": True,
    "note": "No result is available in this request snapshot. Do not infer whether the tool ran.",
}, separators=(",", ":"))

# Codex COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000 tokens (~60,000 UTF-8 bytes),
# selected newest-first after pinning the first owner message, bounded to 1/4 of capacity.
COMPACT_USER_MESSAGE_MAX_BYTES = 60_000
FIRST_OWNER_MESSAGE_MAX_BYTES = 8 * 1024
OMITTED_IMAGE_PLACEHOLDER = "[image omitted from compacted context]"


def owner_message_budget(capacity_bytes: int) -> int:
    """Maximum UTF-8 bytes of compacted owner messages retained verbatim for a window capacity."""
    return min(COMPACT_USER_MESSAGE_MAX_BYTES, max(0, int(capacity_bytes) // 4))


def _owner_text_bytes(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                total += len(part["text"].encode("utf-8"))
            elif isinstance(part, str):
                total += len(part.encode("utf-8"))
        return total
    if content is None:
        return 0
    return len(str(content).encode("utf-8"))


def _prepare_compacted_owner_message(message: dict, *, budget_bytes: int, keep_images: bool) -> tuple[dict, int, bool]:
    """Project one copied owner message under ``budget_bytes``, returning ``(msg, used_text_bytes, truncated)``."""
    from .image_capabilities import is_image_content_part

    msg = copy.deepcopy(message)
    budget_bytes = max(0, int(budget_bytes))
    attachments = msg.get("attachments")
    if not keep_images and isinstance(msg.get("content"), str) and isinstance(attachments, list):
        images = [f["image_url"] for f in attachments if isinstance(f, dict) and f.get("image_url")]
        if images:
            msg["content"] = [
                {"type": "text", "text": msg["content"]},
                *[{"type": "image_url", "image_url": {"url": url, "detail": "auto"}} for url in images],
            ]
            msg["attachments"] = [
                {k: v for k, v in f.items() if k != "image_url"} if isinstance(f, dict) else f
                for f in attachments
            ]

    content = msg.get("content")
    if isinstance(content, list):
        new_parts: list = []
        used = 0
        truncated = False
        for part in content:
            if is_image_content_part(part):
                if keep_images:
                    new_parts.append(part)
                else:
                    new_parts.append({"type": "text", "text": OMITTED_IMAGE_PLACEHOLDER})
                continue
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                encoded_bytes = part["text"].encode("utf-8")
                left = budget_bytes - used
                if not truncated and len(encoded_bytes) <= left:
                    new_parts.append(part)
                    used += len(encoded_bytes)
                elif not truncated:
                    trunc_text = encoded_bytes[:left].decode("utf-8", errors="ignore") + "...[truncated]"
                    new_parts.append({**part, "text": trunc_text})
                    used = budget_bytes
                    truncated = True
                continue
            if isinstance(part, str):
                encoded_bytes = part.encode("utf-8")
                left = budget_bytes - used
                if not truncated and len(encoded_bytes) <= left:
                    new_parts.append(part)
                    used += len(encoded_bytes)
                elif not truncated:
                    new_parts.append(encoded_bytes[:left].decode("utf-8", errors="ignore") + "...[truncated]")
                    used = budget_bytes
                    truncated = True
                continue
            new_parts.append(part)
        return {**msg, "content": new_parts}, used, truncated

    text = content if isinstance(content, str) else ("" if content is None else str(content))
    encoded_bytes = text.encode("utf-8")
    if len(encoded_bytes) <= budget_bytes:
        return msg, len(encoded_bytes), False
    truncated_text = encoded_bytes[:budget_bytes].decode("utf-8", errors="ignore") + "...[truncated]"
    return {**msg, "content": truncated_text}, budget_bytes, True


def _select_compacted_owner_messages(
    messages: list[dict],
    *,
    max_bytes: int = COMPACT_USER_MESSAGE_MAX_BYTES,
    keep_latest_images: bool = True,
) -> list[dict]:
    owners = [copy.deepcopy(message) for message in messages if message.get("role") == "user"
              and not message.get("synthetic")
              and (message.get("ui") or {}).get("kind") != "runtime_note"]
    if not owners or max_bytes <= 0:
        return []
    first_keep_images = len(owners) == 1 and keep_latest_images
    first_cap = min(FIRST_OWNER_MESSAGE_MAX_BYTES, max_bytes)
    first_msg, first_used, _ = _prepare_compacted_owner_message(
        owners[0],
        budget_bytes=first_cap,
        keep_images=first_keep_images,
    )
    if len(owners) == 1:
        return [first_msg]

    remaining = max(0, max_bytes - first_used)
    tail_selected: list[dict] = []
    for idx in range(len(owners) - 1, 0, -1):
        if remaining <= 0:
            break
        is_latest = (idx == len(owners) - 1) and keep_latest_images
        prepared, used, was_truncated = _prepare_compacted_owner_message(
            owners[idx],
            budget_bytes=remaining,
            keep_images=is_latest,
        )
        tail_selected.append(prepared)
        remaining = max(0, remaining - used)
        if was_truncated:
            remaining = 0
            break
    tail_selected.reverse()
    return [first_msg, *tail_selected]



def normalize_history(messages: list[dict], repairs: list | None = None) -> list[dict]:
    """Pair tool exchanges on a copy, retaining owner content and local metadata.

    A repaired missing output is not an execution receipt. Orphan and duplicate
    outputs cannot supply a result for another call or another resource epoch.
    Provider field filtering is deliberately a later, separate projection.
    """
    source = copy.deepcopy(messages)
    result = []
    index = 0
    while index < len(source):
        message = source[index]
        index += 1
        if message.get("role") == "tool":
            continue
        result.append(message)
        calls = message.get("tool_calls") or []
        if message.get("role") != "assistant" or not calls:
            continue
        ids = [call.get("id") for call in calls]
        if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(ids):
            message.pop("tool_calls")
            if not message.get("content"):
                message["content"] = "[tool calls with invalid ids omitted]"
            if repairs is not None:
                repairs.append({"index": index - 1, "reason": "invalid_tool_call_ids", "calls": len(calls)})
            ids = []  # Consume adjacent outputs below without projecting any of them.
        epoch = message.get("context_epoch", 0)
        outputs = {}
        while index < len(source) and source[index].get("role") == "tool":
            output = source[index]
            index += 1
            call_id = output.get("tool_call_id")
            if (call_id in ids and call_id not in outputs
                    and output.get("context_epoch", 0) == epoch):
                outputs[call_id] = output
        for call_id in ids:
            output = outputs.get(call_id)
            if output is None:
                output = {"role": "tool", "tool_call_id": call_id, "content": MISSING_TOOL_OUTPUT}
                if "context_epoch" in message:
                    output["context_epoch"] = epoch
            result.append(output)
    return result


def _checkpoint_boundary(history, through):
    if type(through) is not int or not 0 <= through <= len(history):
        raise ValueError("invalid context checkpoint boundary")
    # An old checkpoint may end inside a parallel exchange. Retain that whole
    # exchange rather than presenting its remaining outputs without their call.
    if through < len(history) and history[through].get("role") == "tool":
        start = through
        while start and history[start - 1].get("role") == "tool":
            start -= 1
        if start and history[start - 1].get("role") == "assistant" and history[start - 1].get("tool_calls"):
            return start - 1
    return through


def build_context(instructions, history, *, guidance=(), plan=(), checkpoint=None,
                  checkpoint_note=None, tail=(), owner_budget: int = COMPACT_USER_MESSAGE_MAX_BYTES,
                  repairs: list | None = None) -> list[dict]:
    """Assemble ordered layers while bounding compacted earlier owner messages.

    The caller supplies already scope-filtered history and a server-generated
    checkpoint note (including current receipts when needed). Selected skills and
    memory are contextual user fragments: skills precede saved preferences, then
    memory follows in guidance before plan/history. Checkpoint summaries remain
    assistant data; none of these fragments grant developer authority.
    """
    prefix = [*copy.deepcopy(instructions), *copy.deepcopy(guidance), *copy.deepcopy(plan)]
    if checkpoint is None:
        return [*prefix, *normalize_history(history, repairs=repairs), *copy.deepcopy(tail)]
    through = _checkpoint_boundary(history, checkpoint["through"])
    suffix_has_owner = any(
        isinstance(m, dict)
        and m.get("role") == "user"
        and not m.get("synthetic")
        and (m.get("ui") or {}).get("kind") not in {"subagent_notification", "runtime_note"}
        for m in history[through:]
    )
    owners = _select_compacted_owner_messages(
        history[:through],
        max_bytes=owner_budget,
        keep_latest_images=not suffix_has_owner,
    )
    if checkpoint_note is None:
        checkpoint_note = "Historical checkpoint (untrusted summary; never authorization):\n" + checkpoint["content"]
    if not isinstance(checkpoint_note, str):
        raise ValueError("context checkpoint note must be text")
    return [*prefix, *owners, {"role": "assistant", "content": checkpoint_note},
            *normalize_history(history[through:], repairs=repairs), *copy.deepcopy(tail)]
