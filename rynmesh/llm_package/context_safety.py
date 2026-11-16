"""Tokenizer-independent context safety bounds shared by LLM admission paths.

Pricing may continue to use a chars/4 estimate. Context admission uses
ceil(ascii_bytes / 3) + non_ascii_chars (counting each CJK/emoji code point as
one token) plus a bounded token cost per inline image.
"""

from __future__ import annotations

import re

ESTIMATED_BYTES_PER_TOKEN = 3
CONTEXT_SAFETY_MARGIN_TOKENS = 128
CONTEXT_OVERFLOW_DETAIL = "estimated_context_window_exceeded"
CONTEXT_ESTIMATE_CAPABILITY = "context-estimate:v2"
# An inline image costs the model a bounded number of tokens, not one per byte
# of its base64 text. This is above common vision pricing for a large image.
IMAGE_CONTEXT_TOKENS = 3000
_INLINE_IMAGE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/]*={0,2}")


def estimate_context_safety_tokens(text: str) -> int:
    """Return a conservative upper estimate for multilingual prompt tokens."""

    images = 0

    def drop(_match):
        nonlocal images
        images += 1
        return ""

    stripped = _INLINE_IMAGE.sub(drop, text)
    ascii_bytes = len(stripped) if stripped.isascii() else sum(1 for ch in stripped if ord(ch) < 128)
    non_ascii_chars = len(stripped) - ascii_bytes
    text_tokens = (ascii_bytes + ESTIMATED_BYTES_PER_TOKEN - 1) // ESTIMATED_BYTES_PER_TOKEN + non_ascii_chars
    return max(1, text_tokens + images * IMAGE_CONTEXT_TOKENS)


def context_overflow_detail() -> str:
    """Return the closed error detail code for estimated context window overflow."""
    return CONTEXT_OVERFLOW_DETAIL


def context_request_fits(
    prompt: str,
    *,
    output_tokens: int,
    context_window: int,
    safety_margin: int = CONTEXT_SAFETY_MARGIN_TOKENS,
) -> bool:
    return (
        estimate_context_safety_tokens(prompt)
        + max(0, int(output_tokens))
        + max(0, int(safety_margin))
        <= max(0, int(context_window))
    )
