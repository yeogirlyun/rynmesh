"""Tokenizer-independent context safety bounds shared by LLM admission paths.

Pricing may continue to use a chars/4 estimate. Context admission must not:
that heuristic under-counts CJK, combining marks, and emoji. UTF-8 byte length
is intentionally conservative until a trusted provider tokenizer is available.
"""

from __future__ import annotations

import re

CONTEXT_SAFETY_MARGIN_TOKENS = 128
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
    return max(1, len(stripped.encode("utf-8")) + images * IMAGE_CONTEXT_TOKENS)


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
