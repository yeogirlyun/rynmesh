import base64
import json
import math

from rynmesh.agent_runtime import output_policy
from rynmesh.llm_package import context_safety
from rynmesh.llm_package.context_safety import (
    CONTEXT_SAFETY_MARGIN_TOKENS,
    IMAGE_CONTEXT_TOKENS,
    context_request_fits,
    estimate_context_safety_tokens,
)


def test_t1_context_safety_estimate_english_json_chinese_emoji_and_image_vs_old_utf8_bytes() -> None:
    """T1: English/JSON use ceil(ascii_bytes/3), non-ASCII chars count 1 token each, images count 3000, sharing ESTIMATED_BYTES_PER_TOKEN with output_policy."""
    assert getattr(context_safety, "ESTIMATED_BYTES_PER_TOKEN", None) == 3
    assert output_policy.ESTIMATED_BYTES_PER_TOKEN == context_safety.ESTIMATED_BYTES_PER_TOKEN
    assert context_safety.context_overflow_detail() == "estimated_context_window_exceeded"

    # English: 4 ASCII bytes -> ceil(4/3) == 2 (old UTF-8 byte count was 4)
    eng = "abcd"
    assert len(eng.encode("utf-8")) == 4
    assert estimate_context_safety_tokens(eng) == 2

    # JSON: pure ASCII structural text -> ~1/3 of old UTF-8 byte count
    json_blob = json.dumps({"tool": "read_file", "arguments": {"path": "src/main.py", "offset": 0, "limit": 200}})
    old_json_bytes = len(json_blob.encode("utf-8"))
    assert estimate_context_safety_tokens(json_blob) == math.ceil(old_json_bytes / 3)
    assert estimate_context_safety_tokens(json_blob) < old_json_bytes // 2

    # Chinese: 2 CJK chars -> 2 tokens (old UTF-8 byte count was 6)
    zh = "中文"
    assert len(zh.encode("utf-8")) == 6
    assert estimate_context_safety_tokens(zh) == 2

    # Combining mark + ASCII: 'e' (1 ASCII byte -> ceil(1/3)=1) + '\u0301' (1 non-ASCII char -> 1) = 2 (old UTF-8 bytes = 3)
    comb = "e\u0301"
    assert len(comb.encode("utf-8")) == 3
    assert estimate_context_safety_tokens(comb) == 2

    # Emoji: 1 Unicode scalar -> 1 token (old UTF-8 bytes = 4)
    emoji = "😀"
    assert len(emoji.encode("utf-8")) == 4
    assert estimate_context_safety_tokens(emoji) == 1

    # Mixed ASCII + CJK + inline image: image still counts as 3000 tokens instead of base64 bytes
    fake_img = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 18000).decode("ascii")
    mixed = f'{{"text":"hello世界","img":"{fake_img}"}}'
    stripped_ascii = len('{"text":"hello","img":""}')
    assert estimate_context_safety_tokens(mixed) == math.ceil(stripped_ascii / 3) + 2 + IMAGE_CONTEXT_TOKENS


def test_t2_tool_heavy_agent_request_fits_32k_window_without_false_rejection() -> None:
    """T2: A typical agent chat payload with many tool schemas (~54 KiB JSON) fits in a 32K context window instead of being falsely rejected."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"workspace_tool_{idx}",
                "description": (
                    "Inspect or update workspace resources with deterministic schema validation, "
                    "bounded pagination, and explicit path scoping for safe agent execution."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "mode"],
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative POSIX file path."},
                        "mode": {"type": "string", "enum": ["read", "stat", "search", "verify"]},
                        "start_line": {"type": "integer", "minimum": 1, "maximum": 200000},
                        "end_line": {"type": "integer", "minimum": 1, "maximum": 200000},
                        "pattern": {"type": "string", "description": "Optional literal or regex filter."},
                    },
                },
            },
        }
        for idx in range(60)
    ]
    chat_payload = {
        "messages": [
            {"role": "system", "content": "You are a careful coding assistant. Follow workspace rules." * 20},
            {"role": "user", "content": "请检查 src/ 下的路由定义并给出修改建议。"},
        ],
        "tools": tools,
        "max_tokens": 4096,
    }
    serialized = json.dumps(chat_payload, ensure_ascii=False)
    # Old 1-byte=1-token heuristic exceeded 32768 (> 45 KiB + 4096 > 32768), whereas v2 estimate fits comfortably!
    assert len(serialized.encode("utf-8")) + 4096 + CONTEXT_SAFETY_MARGIN_TOKENS > 32768
    assert context_request_fits(serialized, output_tokens=4096, context_window=32768)


def test_context_safety_estimate_is_utf8_byte_upper_bound() -> None:
    assert estimate_context_safety_tokens("abcd") == 2
    assert estimate_context_safety_tokens("中文") == 2
    assert estimate_context_safety_tokens("e\u0301") == 2
    assert estimate_context_safety_tokens("😀") == 1


def test_context_admission_reserves_output_and_fixed_margin() -> None:
    prompt = "中文😀"
    required = (
        estimate_context_safety_tokens(prompt)
        + 64
        + CONTEXT_SAFETY_MARGIN_TOKENS
    )
    assert context_request_fits(prompt, output_tokens=64, context_window=required)
    assert not context_request_fits(prompt, output_tokens=64, context_window=required - 1)
