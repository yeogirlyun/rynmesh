"""Project validated chat images into native Codex turn inputs, never JSON text."""
from __future__ import annotations

import copy


def text_messages(messages):
    projected = copy.deepcopy(messages)
    number = 0
    for message in projected:
        if not isinstance(message.get("content"), list):
            continue
        parts = []
        for part in message["content"]:
            if part.get("type") == "text":
                parts.append(part["text"])
            elif part.get("type") == "image_url":
                number += 1
                parts.append(f"[Attached image {number} is supplied separately as actual image input.]")
        message["content"] = "\n".join(parts)
    return projected


def image_inputs(messages):
    return [{"type": "image", "url": part["image_url"]["url"]}
            for message in messages if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "image_url"]
