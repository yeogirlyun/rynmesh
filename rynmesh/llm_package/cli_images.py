"""Project validated chat images into native Codex turn inputs, never JSON text."""
from __future__ import annotations

import copy


class ProjectedMessages(list):
    """List of projected messages carrying omitted unknown attachment count."""

    def __init__(self, items, omitted_count: int = 0) -> None:
        super().__init__(items)
        self.omitted = omitted_count
        self.omitted_count = omitted_count


def _sanitize_part_type(raw_type: object) -> str:
    if not isinstance(raw_type, str):
        return "unknown"
    cleaned = "".join(ch for ch in raw_type if ("a" <= ch <= "z") or ch == "_")[:32]
    return cleaned or "unknown"


def format_omitted_attachment(raw_type: object) -> str:
    return f"[不支持的附件类型：{_sanitize_part_type(raw_type)}，已省略]"


def normalize_cli_messages(messages):
    if not isinstance(messages, list):
        return messages, 0
    projected = copy.deepcopy(messages)
    total_omitted = 0
    for message in projected:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        new_parts = []
        msg_omitted = 0
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") not in {"text", "image_url"}:
                new_parts.append({"type": "text", "text": format_omitted_attachment(part.get("type"))})
                msg_omitted += 1
                total_omitted += 1
            else:
                new_parts.append(part)
        if msg_omitted > 0 and all(
            isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str) and not (set(p) - {"type", "text"})
            for p in new_parts
        ):
            message["content"] = "\n".join(p["text"] for p in new_parts)
        else:
            message["content"] = new_parts
    return projected, total_omitted


def text_messages(messages, *, with_count: bool = False):
    projected = copy.deepcopy(messages)
    number = 0
    omitted = 0
    for message in projected:
        if not isinstance(message.get("content"), list):
            continue
        parts = []
        for part in message["content"]:
            part_type = part.get("type") if isinstance(part, dict) else None
            if part_type == "text":
                parts.append(part["text"])
            elif part_type == "image_url":
                number += 1
                parts.append(f"[Attached image {number} is supplied separately as actual image input.]")
            else:
                omitted += 1
                parts.append(format_omitted_attachment(part_type))
        message["content"] = "\n".join(parts)
    if with_count:
        return projected, omitted
    return ProjectedMessages(projected, omitted)


def image_inputs(messages):
    return [{"type": "image", "url": part["image_url"]["url"]}
            for message in messages if isinstance(message.get("content"), list)
            for part in message["content"] if isinstance(part, dict) and part.get("type") == "image_url"]
