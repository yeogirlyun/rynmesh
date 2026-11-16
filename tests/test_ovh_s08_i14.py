"""Tests for S08-I14: MCP result normalization, truncation markers, resilient blocks, and transport/result bounds."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from PIL import Image

from rynmesh.agent_runtime import results
from rynmesh.agent_runtime.extensions import ExtensionStore, adapter as adapter_mod, mcp_transport
from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter, _strip_raw_images
from rynmesh.agent_runtime.extensions.mcp_transport import (
    McpSessionManager,
    McpTransportError,
    StreamableHttpMcpSession,
)
from rynmesh.agent_runtime.extensions.store import compute_model_tool_name
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.services import peer_box
from test_agent_extensions_runtime import _Catalog, _ExtensionHarnessTools, _Model


BINDING = {
    "server_id": "demo",
    "original_tool_name": "fetch_data",
    "model_tool_name": "mcp__demo__fetch_data",
    "effective_effect": "read",
}


def _make_adapter_env(tmp_path: Path) -> tuple[ExtensionToolAdapter, ExtensionStore, AgentStore, Path, dict[str, Any]]:
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(
        lambda data: data["settings"].update(
            enabled=True,
            workspace_root=str(ws),
            permission_mode="automatic",
        )
    )
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store
    ext_adapter = ExtensionToolAdapter(local, lambda: ledger, node_store=node_store)
    ext_adapter.bind_agent_store(agent_store)
    ext_store = ext_adapter.store
    context = {"task_id": "task_s08_i14", "local_execution": {"workspace_root": str(ws)}}
    return ext_adapter, ext_store, agent_store, ws, context


def _assistant_tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
        },
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


def _assistant_done(text: str) -> dict[str, Any]:
    return {
        "message": {"role": "assistant", "content": text},
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


def test_t1_seventy_blocks_include_truncated_marker_and_server_max_output_chars(tmp_path: Path) -> None:
    ext_adapter, ext_store, _, _, context = _make_adapter_env(tmp_path)
    try:
        # MAX_TEXT_BLOCK_CHARS constant is replaced by per-server max_output_chars (default 32000)
        assert not hasattr(adapter_mod, "MAX_TEXT_BLOCK_CHARS")

        ov = ext_store.overview()
        srv = ext_store.upsert_mcp_server(
            {
                "id": "demo",
                "name": "Demo",
                "transport": "streamable_http",
                "url": "https://demo.example.test/mcp",
                "enabled": True,
            },
            expected_revision=ov["revision"],
        )
        assert srv["max_output_chars"] == 32000

        blocks = [{"type": "text", "text": f"block-{idx:03d}"} for idx in range(70)]
        expected_chars = sum(len(b["text"]) for b in blocks)
        res = ext_adapter._normalize_mcp_result(
            {"content": blocks},
            binding=BINDING,
            context=context,
            operation_id="op_t1_70",
        )
        assert len(res["content"]) == 64
        assert res["truncated"] == {"blocks_total": 70, "chars_original": expected_chars}

        # Configure server max_output_chars via _apply_mcp_policy_fields
        srv_updated = ext_store.upsert_mcp_server(
            {
                "id": "demo",
                "name": "Demo",
                "transport": "streamable_http",
                "url": "https://demo.example.test/mcp",
                "enabled": True,
                "max_output_chars": 1500,
            },
            server_id="demo",
            expected_revision=srv["revision"],
        )
        assert srv_updated["max_output_chars"] == 1500
        long_res = ext_adapter._normalize_mcp_result(
            {"content": [{"type": "text", "text": "Z" * 4000}]},
            binding=BINDING,
            context=context,
            operation_id="op_t1_chars",
        )
        assert long_res["truncated"] == {"blocks_total": 1, "chars_original": 4000}
        assert len(long_res["text"]) == 1500
    finally:
        ext_adapter.manager.close()


def test_t2_hundred_k_text_paginated_completely_via_ryn_read_result_and_oversize_save_truncates(
    tmp_path: Path,
) -> None:
    home = tmp_path / "rt_home"
    ws = tmp_path / "rt_ws"
    home.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)
    agent_key = X25519PrivateKey.generate()
    agent_store = AgentStore(home / "agent", agent_key)
    tools = _ExtensionHarnessTools(home, agent_store)

    ext_store = tools.extensions.store
    ov = ext_store.overview()
    srv = ext_store.upsert_mcp_server(
        {
            "id": "bigdoc",
            "name": "BigDoc",
            "transport": "streamable_http",
            "url": "https://bigdoc.example.test/mcp",
            "secret_headers": {"Authorization": "Bearer synthetic-token"},
            "enabled": True,
            "read_only_tools": ["read_huge"],
        },
        expected_revision=ov["revision"],
    )
    ext_store.record_mcp_discovery(
        "bigdoc",
        expected_revision=srv["revision"],
        expected_config_generation=srv["config_generation"],
        expected_instance_id=srv.get("instance_id"),
        status="ready",
        discovered_tools=[
            {
                "name": "read_huge",
                "description": "Return a 100,000-char document",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
    )
    tool_name = compute_model_tool_name("bigdoc", "read_huge")

    # Build a deterministic 100,000-character string with unique checkpoints across the full 100k span
    chunks = [f"[SEG-{i:04d}:" + ("A" * 89) + "]" for i in range(1000)]
    full_100k = "".join(chunks)
    assert len(full_100k) == 100_000

    tools.extensions.manager.call_tool = lambda *_a, **_kw: {
        "content": [{"type": "text", "text": full_100k}]
    }

    collected_pages: list[str] = []

    def _next_step(_model_id, messages, _specs, *_rest, **_kw):
        last_tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not last_tool_msgs:
            # Context compaction summary request during multi-page read
            return _assistant_done("Compacted progress while reading result pages.")
        latest = json.loads(last_tool_msgs[-1]["content"])
        if "result_ref" in latest and not collected_pages:
            ref_id = latest["result_ref"]
            _next_step.ref_id = ref_id  # type: ignore[attr-defined]
            return _assistant_tool_call("ryn_read_result", {"result_id": ref_id, "offset": 0}, "call_page_0")
        assert "content" in latest
        collected_pages.append(latest["content"])
        nxt_off = latest.get("next_offset")
        if nxt_off is not None:
            return _assistant_tool_call(
                "ryn_read_result",
                {"result_id": _next_step.ref_id, "offset": nxt_off},  # type: ignore[attr-defined]
                f"call_page_{nxt_off}",
            )
        return _assistant_done("All 100k chars read.")

    model = _Model(
        _assistant_tool_call("ryn_discover_tools", {"load": [tool_name]}, "call_disc"),
        _assistant_tool_call(tool_name, {}, "call_huge"),
        *([_next_step] * 120),
    )
    class _LargeWindowCatalog(_Catalog):
        def list_models(self) -> list[dict]:
            rows = super().list_models()
            rows[0]["context_window"] = 256000
            rows[0]["context_window_known"] = True
            return rows

    runtime = AgentRuntime(agent_store, model, tools, _LargeWindowCatalog())
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "workspace_root": str(ws),
            "permission_mode": "automatic",
            "max_steps": 128,
            "max_output_chars": 30000,
        }
    )
    try:
        created = runtime.create({"message": "Read the entire 100k MCP document"})
        tid = created["id"]
        for _ in range(200):
            cur = runtime.get(tid)
            if cur["status"] in {"completed", "failed"}:
                break
            runtime.run_once()
        final_task = runtime.get(tid)
        assert final_task["status"] == "completed"
        reconstructed = json.loads("".join(collected_pages))
        assert reconstructed["text"] == full_100k
        assert len(reconstructed["text"]) == 100_000

        # Also verify results.save > 2 MiB truncates and marks truncated without raising agent_result_storage_limit
        raw_row = runtime.store.read()["tasks"][tid]
        huge_payload = {"text": "M" * (2 * 1024 * 1024 + 400_000), "keep_meta": "ok"}
        ref_oversize = results.save(
            runtime.store,
            raw_row,
            {"name": tool_name, "operation_id": "op_oversize_2m"},
            huge_payload,
        )
        assert ref_oversize["truncated"] is True
        assert ref_oversize["bytes"] <= results.MAX_RESULT_BYTES
        runtime.store.transaction(
            lambda data: data["tasks"][tid].setdefault("result_refs", []).append(ref_oversize)
        )
        page0 = runtime.get_result(tid, ref_oversize["id"], 0, 4096)
        assert page0["bytes"] <= results.MAX_RESULT_BYTES
        assert "MMMM" in page0["content"]
    finally:
        tools.close()


def test_t3_audio_and_resource_link_blocks_converted_to_text_placeholders(tmp_path: Path) -> None:
    ext_adapter, _, _, _, context = _make_adapter_env(tmp_path)
    try:
        res = ext_adapter._normalize_mcp_result(
            {
                "content": [
                    {"type": "audio", "mimeType": "audio/wav", "data": "UklGRg=="},
                    {
                        "type": "resource_link",
                        "uri": "https://files.example.test/report-q4.pdf",
                        "name": "Q4 Financial Report",
                    },
                    {"type": "custom_widget", "payload": 123},
                ]
            },
            binding=BINDING,
            context=context,
            operation_id="op_t3",
        )
        assert res["content"][0] == {"type": "text", "text": "[audio 内容未展示]"}
        assert res["content"][1]["type"] == "text"
        assert "https://files.example.test/report-q4.pdf" in res["content"][1]["text"]
        assert "Q4 Financial Report" in res["content"][1]["text"]
        assert res["content"][2] == {"type": "text", "text": "[custom_widget 内容未展示]"}
    finally:
        ext_adapter.manager.close()


def test_t4_read_tool_bad_png_with_text_returns_text_while_strict_mode_rejects(tmp_path: Path) -> None:
    ext_adapter, ext_store, _, _, context = _make_adapter_env(tmp_path)
    try:
        ov = ext_store.overview()
        srv = ext_store.upsert_mcp_server(
            {
                "id": "imgsrv",
                "name": "ImgSrv",
                "transport": "stdio",
                "command": "python",
                "enabled": True,
                "read_only_tools": ["inspect_chart"],
            },
            expected_revision=ov["revision"],
        )
        ext_store.record_mcp_discovery(
            "imgsrv",
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv.get("instance_id"),
            status="ready",
            discovered_tools=[
                {
                    "name": "inspect_chart",
                    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            ],
        )
        model_tool = compute_model_tool_name("imgsrv", "inspect_chart")
        ext_adapter.manager.call_tool = lambda *_a, **_kw: {
            "content": [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"not-a-valid-png").decode("ascii")},
                {"type": "text", "text": "Chart caption: revenue up 18%"},
            ]
        }

        out = ext_adapter.execute(model_tool, {}, operation_id="op_t4", context=context)
        assert out["is_error"] is False
        assert "error" not in out
        assert out["text"] == "Chart caption: revenue up 18%"
        assert any(b.get("type") == "image" and b.get("error") == "agent_mcp_image_invalid" for b in out["content"])
        assert any(b == {"type": "text", "text": "Chart caption: revenue up 18%"} for b in out["content"])

        # Strict mode (used for strict write validation) still raises agent_mcp_image_invalid
        with pytest.raises(AgentError, match="agent_mcp_image_invalid"):
            ext_adapter._normalize_mcp_result(
                {"content": [{"type": "image", "mimeType": "image/png", "data": "bad!"}]},
                binding={**BINDING, "effective_effect": "write"},
                context=context,
                operation_id="op_t4_strict",
                allow_image_rejection=True,
            )
    finally:
        ext_adapter.manager.close()


def test_t5_unknown_mime_saved_as_bin_and_multiple_blobs_supported_up_to_eight(tmp_path: Path) -> None:
    ext_adapter, _, _, ws, context = _make_adapter_env(tmp_path)
    try:
        img_buf = io.BytesIO()
        Image.new("RGB", (4, 4), (20, 40, 60)).save(img_buf, format="PNG")
        jpg_buf = io.BytesIO()
        Image.new("RGB", (4, 4), (60, 40, 20)).save(jpg_buf, format="JPEG")

        blobs_spec = [
            ("application/x-proprietary-dump", b"RAW_PROPRIETARY_BINARY_01", ".bin"),
            ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04docx-bytes", ".docx"),
            ("application/vnd.openxmlformats-officedocument.presentationml.presentation", b"PK\x03\x04pptx-bytes", ".pptx"),
            ("application/zip", b"PK\x03\x04zip-bytes", ".zip"),
            ("image/png", img_buf.getvalue(), ".png"),
            ("image/jpeg", jpg_buf.getvalue(), ".jpeg"),
            ("application/pdf", b"%PDF-1.4\nsample", ".pdf"),
            ("application/octet-stream", b"\x00\x01\x02\x03", ".bin"),
            ("text/csv", b"ninth,should,skip\n", None),
        ]
        content_blocks = [
            {
                "type": "resource",
                "resource": {
                    "uri": f"ryn://export/{idx}",
                    "mimeType": mime,
                    "blob": base64.b64encode(raw).decode("ascii"),
                },
            }
            for idx, (mime, raw, _ext) in enumerate(blobs_spec)
        ]
        res = ext_adapter._normalize_mcp_result(
            {"content": content_blocks},
            binding=BINDING,
            context=context,
            operation_id="op_t5",
        )
        assert len(res["files"]) == 8
        for idx in range(8):
            expected_mime, expected_raw, expected_ext = blobs_spec[idx]
            f_entry = res["files"][idx]
            assert f_entry["path"].endswith(expected_ext)
            assert f_entry["mime_type"] == expected_mime
            assert f_entry["bytes"] == len(expected_raw)
            assert f_entry["sha256"] == hashlib.sha256(expected_raw).hexdigest()
            assert (ws / f_entry["path"]).read_bytes() == expected_raw
        # 9th blob did not fail the whole call and produced a placeholder block
        assert len(res["content"]) == 9
    finally:
        ext_adapter.manager.close()


def test_t6_twenty_mib_docx_blob_saved_end_to_end_over_http_transport(tmp_path: Path) -> None:
    assert mcp_transport.MAX_OUTPUT_BYTES == 8 * 1024 * 1024

    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    raw_20m = b"PK\x03\x04" + (b"D" * (20 * 1024 * 1024 - 4))
    b64_20m = base64.b64encode(raw_20m).decode("ascii")
    expected_sha = hashlib.sha256(raw_20m).hexdigest()

    tool_call_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "ryn://exports/report.docx",
                            "mimeType": docx_mime,
                            "blob": b64_20m,
                        },
                    }
                ]
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    # ~26.7 MiB > 8 MiB general limit, <= 40 MiB tools/call limit
    assert 8 * 1024 * 1024 < len(tool_call_body) < 40 * 1024 * 1024

    class BigDocxHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        list_pad_bytes = 5 * 1024 * 1024

        def log_message(self, *_args) -> None:
            return None

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            ident = req.get("id")
            method = req.get("method")
            if ident is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "docx-http", "version": "1.0"},
                        },
                    }
                ).encode("utf-8")
            elif method == "tools/list":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "tools": [
                                {
                                    "name": "export_docx",
                                    "description": "x" * self.list_pad_bytes,
                                    "inputSchema": {"type": "object", "properties": {}},
                                }
                            ]
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            else:
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": json.loads(tool_call_body)["result"],
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ext_adapter, _, _, ws, context = _make_adapter_env(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BigDocxHandler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    sess = StreamableHttpMcpSession(
        {
            "id": "docx-http",
            "url": f"http://127.0.0.1:{server.server_port}/mcp",
            "startup_timeout_ms": 15000,
            "tool_timeout_ms": 30000,
        }
    )
    try:
        sess.initialize()
        # 5 MiB tools/list succeeds under 8 MiB general response limit
        tools_5m = sess.list_all_tools()
        assert len(tools_5m) == 1

        # ~26.7 MiB tools/call succeeds under 40 MiB tools/call response limit
        raw_res = sess.call_tool("export_docx", {})
        norm = ext_adapter._normalize_mcp_result(
            raw_res,
            binding=BINDING,
            context=context,
            operation_id="op_t6_20m",
        )
        assert len(norm["files"]) == 1
        saved = norm["files"][0]
        assert saved["path"].endswith(".docx")
        assert saved["bytes"] == 20 * 1024 * 1024
        assert saved["sha256"] == expected_sha
        assert (ws / saved["path"]).stat().st_size == 20 * 1024 * 1024

        # 9 MiB tools/list exceeds 8 MiB non-tools/call limit
        BigDocxHandler.list_pad_bytes = 9 * 1024 * 1024
        with pytest.raises(McpTransportError, match="agent_mcp_output_limit"):
            sess.list_all_tools()
    finally:
        sess.close()
        ext_adapter.manager.close()
        server.shutdown()
        server.server_close()
        t.join(timeout=2)


def test_t7_thirty_mib_blob_skipped_with_placeholder_and_other_blocks_preserved(tmp_path: Path) -> None:
    ext_adapter, _, _, ws, context = _make_adapter_env(tmp_path)
    try:
        # 30 MiB decoded = 40 * 1024 * 1024 base64 chars ("QUFB" == b"AAA")
        b64_30m = "QUFB" * (10 * 1024 * 1024)
        small_csv = b"id,name\n1,Alice\n"
        res = ext_adapter._normalize_mcp_result(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "ryn://exports/too_big.zip",
                            "mimeType": "application/zip",
                            "blob": b64_30m,
                        },
                    },
                    {"type": "text", "text": "Remaining summary is intact."},
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "ryn://exports/small.csv",
                            "mimeType": "text/csv",
                            "blob": base64.b64encode(small_csv).decode("ascii"),
                        },
                    },
                ]
            },
            binding=BINDING,
            context=context,
            operation_id="op_t7_30m",
        )
        assert "error" not in res
        assert len(res["files"]) == 1
        assert res["files"][0]["path"].endswith(".csv")
        assert (ws / res["files"][0]["path"]).read_bytes() == small_csv
        assert "Remaining summary is intact." in res["text"]
        assert any(
            b.get("type") == "text" and ("跳过" in b.get("text", "") or "25 MiB" in b.get("text", ""))
            for b in res["content"]
        )
    finally:
        ext_adapter.manager.close()


def test_t8_structured_content_500_rows_intact_6000_rows_marked_and_depth_cut_string(tmp_path: Path) -> None:
    ext_adapter, _, _, _, context = _make_adapter_env(tmp_path)
    try:
        rows_500 = [{"row": i, "val": f"v{i}"} for i in range(500)]
        res_500 = ext_adapter._normalize_mcp_result(
            {
                "content": [{"type": "text", "text": "serialized 500"}],
                "structuredContent": {"rows": rows_500},
            },
            binding=BINDING,
            context=context,
            operation_id="op_t8_500",
        )
        assert len(res_500["structuredContent"]["rows"]) == 500
        assert res_500["structuredContent"]["rows"][499] == {"row": 499, "val": "v499"}
        assert "structured_truncated" not in res_500

        rows_6000 = [{"row": i} for i in range(6000)]
        res_6000 = ext_adapter._normalize_mcp_result(
            {
                "content": [{"type": "text", "text": "serialized 6000"}],
                "structuredContent": {"rows": rows_6000},
            },
            binding=BINDING,
            context=context,
            operation_id="op_t8_6000",
        )
        out_rows = res_6000["structuredContent"]["rows"]
        assert len(out_rows) == 5001
        assert out_rows[4999] == {"row": 4999}
        assert out_rows[5000] == {"_truncated": True, "omitted_items": 1000}
        assert res_6000["structured_truncated"] == {
            "omitted_items": 1000,
            "depth_cut": 0,
            "omitted_strings": 0,
        }

        # Verify depth > 12 becomes "[深度超限已省略]" (not None) and _strip_raw_images returns counts
        deep: Any = {"leaf": "bottom"}
        for level in range(14):
            deep = {f"d{level}": deep}
        cleaned_deep, counts = _strip_raw_images(deep)
        assert counts["depth_cut"] >= 1
        assert "[深度超限已省略]" in json.dumps(cleaned_deep, ensure_ascii=False)
    finally:
        ext_adapter.manager.close()


def test_t9_data_key_40000_char_csv_truncated_with_marker_while_valid_base64_omitted(tmp_path: Path) -> None:
    ext_adapter, _, _, _, context = _make_adapter_env(tmp_path)
    try:
        line = "2026-11-16,SKU-1001,199.50,completed\n"
        csv_40k = (line * ((40_000 // len(line)) + 1))[:40_000]
        assert len(csv_40k) == 40_000

        res = ext_adapter._normalize_mcp_result(
            {
                "content": [{"type": "text", "text": "CSV summary"}],
                "structuredContent": {"data": csv_40k},
            },
            binding=BINDING,
            context=context,
            operation_id="op_t9_csv",
        )
        sc_data = res["structuredContent"]["data"]
        assert isinstance(sc_data, str)
        assert sc_data.startswith(csv_40k[:32_000])
        assert sc_data == csv_40k[:32_000] + "...[已截断 8000 字符]"
        assert res["structured_truncated"] == {
            "omitted_items": 0,
            "depth_cut": 0,
            "omitted_strings": 1,
        }

        # Valid long base64 under 'data' key IS still cleared to None and counted in omitted_binary
        raw_bin = bytes(range(256)) * 120  # 30,720 bytes -> 40,960 base64 chars
        b64_long = base64.b64encode(raw_bin).decode("ascii")
        assert len(b64_long) > 32_000
        cleaned_bin, bin_counts = _strip_raw_images({"data": b64_long})
        assert cleaned_bin["data"] is None
        assert bin_counts["omitted_binary"] == 1
    finally:
        ext_adapter.manager.close()


def test_t10_structured_truncation_keeps_text_blocks_while_untruncated_keeps_single_copy(tmp_path: Path) -> None:
    ext_adapter, _, _, _, context = _make_adapter_env(tmp_path)
    try:
        # Truncated structuredContent -> text block MUST stay so model receives both
        res_trunc = ext_adapter._normalize_mcp_result(
            {
                "content": [{"type": "text", "text": "Important tail summary not in first 5000 items"}],
                "structuredContent": {"items": list(range(5200))},
            },
            binding=BINDING,
            context=context,
            operation_id="op_t10_trunc",
        )
        assert res_trunc["text"] == "Important tail summary not in first 5000 items"
        assert res_trunc["content"] == [
            {"type": "text", "text": "Important tail summary not in first 5000 items"}
        ]
        assert res_trunc["structured_truncated"] == {
            "omitted_items": 200,
            "depth_cut": 0,
            "omitted_strings": 0,
        }

        # Untruncated structuredContent -> text block is deduplicated (single copy)
        res_intact = ext_adapter._normalize_mcp_result(
            {
                "content": [{"type": "text", "text": '{"items":[1,2,3]}'}],
                "structuredContent": {"items": [1, 2, 3]},
            },
            binding=BINDING,
            context=context,
            operation_id="op_t10_intact",
        )
        assert res_intact["text"] == ""
        assert res_intact["content"] == []
        assert "structured_truncated" not in res_intact
    finally:
        ext_adapter.manager.close()
