from rynmesh.llm_package.context_safety import (
    CONTEXT_SAFETY_MARGIN_TOKENS,
    context_request_fits,
    estimate_context_safety_tokens,
)


def test_context_safety_estimate_is_utf8_byte_upper_bound() -> None:
    assert estimate_context_safety_tokens("abcd") == 4
    assert estimate_context_safety_tokens("中文") == 6
    assert estimate_context_safety_tokens("e\u0301") == 3
    assert estimate_context_safety_tokens("😀") == 4


def test_context_admission_reserves_output_and_fixed_margin() -> None:
    prompt = "中文😀"
    required = (
        estimate_context_safety_tokens(prompt)
        + 64
        + CONTEXT_SAFETY_MARGIN_TOKENS
    )
    assert context_request_fits(prompt, output_tokens=64, context_window=required)
    assert not context_request_fits(prompt, output_tokens=64, context_window=required - 1)
