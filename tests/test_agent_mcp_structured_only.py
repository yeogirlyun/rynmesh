"""A successful MCP result with structured content reaches the model once (Codex parity).

Codex sends only ``structured_content`` for such results. Ryn used to keep the
serialized text copy twice more (``content`` and ``text``), so a 12-24 KB page
grew past ``max_output_chars`` and lost its middle to head/tail truncation.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from PIL import Image

from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import DEFAULT_SETTINGS, AgentStore, encoded

BINDING = {"server_id": "demo", "original_tool_name": "search", "model_tool_name": "mcp__demo__search"}


def _adapter(tmp_path: Path):
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store
    context = {"task_id": "task_sc", "local_execution": {"workspace_root": str(ws)}}
    return ExtensionToolAdapter(local, lambda: ledger, node_store=node_store), context


def _page(item_bytes: int) -> dict:
    """A synthetic chat page shaped like the company plugin's search result."""
    items, size, n = [], 0, 0
    while size < item_bytes:
        item = {"message_id": f"m{n:05d}", "time": "2026-11-10T09:00:00+08:00",
                "sender": f"成员{n % 7}", "text": f"第{n}条合成消息，用于结果体积回归测试。"}
        size += len(encoded(item))
        items.append(item)
        n += 1
    data = {"items": items, "returned": len(items), "has_more": True, "next_cursor": "c" * 64,
            "coverage": {"state": "complete", "verified": True}}
    return {"status": "succeeded", "data": data, "trace_id": "0" * 32, "contract_version": "2.0"}


def _raw(structured: dict, *, is_error: bool = False, text: str | None = None) -> dict:
    body = json.dumps(structured["data"] if "data" in structured else structured, ensure_ascii=False)
    return {"isError": is_error, "content": [{"type": "text", "text": body if text is None else text}],
            "structuredContent": structured}


def test_success_with_structured_content_sends_one_copy(tmp_path):
    adapter, context = _adapter(tmp_path)
    structured = _page(12_000)
    result = adapter._normalize_mcp_result(_raw(structured), binding=BINDING, context=context, operation_id="op1")

    assert result["structuredContent"] == structured
    assert result["content"] == [] and result["text"] == ""
    message = encoded(result).decode()
    assert message.count("m00000") == 1  # was 3: content, text and structuredContent
    # A 12 KB plugin page now fits inline, so pagination fields are never cut out.
    assert len(encoded(result)) < DEFAULT_SETTINGS["max_output_chars"]


def test_old_triple_copy_would_have_exceeded_the_inline_limit(tmp_path):
    adapter, context = _adapter(tmp_path)
    structured = _page(12_000)
    raw = _raw(structured)
    result = adapter._normalize_mcp_result(raw, binding=BINDING, context=context, operation_id="op1")
    triple = {**result, "content": raw["content"], "text": raw["content"][0]["text"]}
    assert len(encoded(triple)) > DEFAULT_SETTINGS["max_output_chars"] > len(encoded(result))


def test_error_result_keeps_its_text(tmp_path):
    adapter, context = _adapter(tmp_path)
    structured = {"status": "error", "data": None, "error": {"code": "INVALID_ARGUMENT"}}
    result = adapter._normalize_mcp_result(_raw(structured, is_error=True, text="INVALID_ARGUMENT"),
                                           binding=BINDING, context=context, operation_id="op2")
    assert result["is_error"] is True
    assert result["text"] == "INVALID_ARGUMENT"
    assert result["content"] == [{"type": "text", "text": "INVALID_ARGUMENT"}]


def test_text_only_and_empty_structured_results_are_unchanged(tmp_path):
    adapter, context = _adapter(tmp_path)
    text_only = adapter._normalize_mcp_result({"content": [{"type": "text", "text": "plain answer"}]},
                                              binding=BINDING, context=context, operation_id="op3")
    assert text_only["text"] == "plain answer" and "structuredContent" not in text_only
    empty = adapter._normalize_mcp_result(
        {"content": [{"type": "text", "text": "plain answer"}], "structuredContent": {}},
        binding=BINDING, context=context, operation_id="op4")
    assert empty["text"] == "plain answer" and empty["content"][0]["text"] == "plain answer"


def test_images_and_embedded_resources_stay_with_structured_content(tmp_path):
    adapter, context = _adapter(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 40)).save(buf, format="PNG")
    png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    raw = {"content": [{"type": "text", "text": '{"ok": true}'},
                       {"type": "image", "mimeType": "image/png", "data": png_b64},
                       {"type": "resource", "resource": {"uri": "note://1", "text": "embedded note"}}],
           "structuredContent": {"ok": True}}
    result = adapter._normalize_mcp_result(raw, binding=BINDING, context=context, operation_id="op5")
    kinds = [block["type"] for block in result["content"]]
    assert kinds == ["image", "text"]
    assert result["content"][1]["text"] == "embedded note"
    assert result["text"] == "embedded note"
    assert result["images"] and png_b64 not in encoded(result).decode()
