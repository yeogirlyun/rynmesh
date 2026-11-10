"""Tokenizer-independent context safety bounds shared by LLM admission paths.

Pricing may continue to use a chars/4 estimate. Context admission must not:
that heuristic under-counts CJK, combining marks, and emoji. UTF-8 byte length
is intentionally conservative until a trusted provider tokenizer is available.
"""

from __future__ import annotations

CONTEXT_SAFETY_MARGIN_TOKENS = 128


def estimate_context_safety_tokens(text: str) -> int:
    """Return a conservative upper estimate for multilingual prompt tokens."""

    return max(1, len(text.encode("utf-8")))


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
