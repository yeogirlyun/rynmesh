"""Tests for Codex-aligned Ryn improvements (Steps 1-4):
1. 1-step BM25 `ryn_discover_tools(query=...)` search + auto-load + schema return,
   direct `ryn_node_app_action` without mandatory `reviewed_context`, and `operations` in `ryn_list_plugins`.
2. Sandboxed `ryn_code_exec` (`CodeModeRunner`) with `ALL_TOOLS`, `tools.*`, `Promise.all`,
   `as_settled`, `stream_settled`, `store`/`load` persistence, `// @exec:` pragma, and read-only enforcement.
3. Direct multimodal vision note alignment on image-capable routes and `ryn_code_exec` `image()` artifact materialization.
4. `codex_tools` 2048 default token budget and strict Structured Outputs schema compatibility for new/updated tools.
"""
from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from rynmesh.agent_runtime import code_mode, tool_menu
from rynmesh.agent_runtime.code_mode import SPEC as CODE_MODE_SPEC
from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.llm_package import codex_tools
from rynmesh.node_apps.agent import AppTools
from rynmesh.node_apps.plugin_agent import PluginTools


def _sample_specs() -> list[dict]:
    out = []
    for name, (desc, _eff, _risk, params) in LOCAL_SPECS.items():
        out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": copy.deepcopy(params)}})
    out.append(copy.deepcopy(CODE_MODE_SPEC))
    out.append({
        "type": "function",
        "function": {
            "name": "ryn_share_model_with_friend",
            "description": "Share one owned configured model with an active friend relationship.",
            "parameters": {
                "type": "object",
                "properties": {"model_id": {"type": "string"}, "relationship_id": {"type": "string"}},
                "required": ["model_id", "relationship_id"],
                "additionalProperties": False,
            },
        },
    })
    out.append({
        "type": "function",
        "function": {
            "name": "mcp__weather__get_forecast",
            "description": "查询城市天气预报与温度湿度信息 (Query city weather forecast and temperature)",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名称"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    })
    return out


def test_step1_discover_tools_bm25_query_auto_loads_and_returns_schemas():
    specs = _sample_specs()
    internal = [copy.deepcopy(tool_menu.SPEC)]
    _, state = tool_menu.project(
        specs,
        internal,
        None,
        max_context_bytes=64000,
        authority_bytes=4000,
        epoch=0,
        goal_revision=1,
        pinned_names=tool_menu.CORE_TOOLS,
    )
    assert state["mode"] == "directory"

    # English query matches and auto-loads `ryn_share_model_with_friend` in one step
    result, next_state = tool_menu.discover(
        specs,
        internal,
        lambda name: {"effect": "read", "title": name},
        state,
        {"query": "share model friend relationship", "limit": 3},
        capacity_bytes=60000,
    )
    assert result["query"] == "share model friend relationship"
    assert "ryn_share_model_with_friend" in result["matches"]
    assert "ryn_share_model_with_friend" in next_state["loaded_names"]
    matched_entry = next(item for item in result["tools"] if item["name"] == "ryn_share_model_with_friend")
    assert "parameters" in matched_entry
    assert "relationship_id" in matched_entry["parameters"]["properties"]

    # CJK query matches and auto-loads `mcp__weather__get_forecast` in one step
    result_cjk, next_state_cjk = tool_menu.discover(
        specs,
        internal,
        lambda name: {"effect": "read", "title": name},
        next_state,
        {"query": "天气预报 城市", "limit": 2},
        capacity_bytes=60000,
    )
    assert "mcp__weather__get_forecast" in result_cjk["matches"]
    assert "mcp__weather__get_forecast" in next_state_cjk["loaded_names"]
    cjk_entry = next(item for item in result_cjk["tools"] if item["name"] == "mcp__weather__get_forecast")
    assert "parameters" in cjk_entry


def test_step1_node_app_action_optional_reviewed_context_and_plugin_list_operations(tmp_path: Path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from rynmesh.nas import NasPlugin
    from rynmesh.node_apps.plugins import PluginManager
    from rynmesh.node_apps.service import NodeApps
    from rynmesh.node_apps.store import AppStore

    key = X25519PrivateKey.generate()
    peer_id = "peer_codex_align_1"
    nas = NasPlugin(tmp_path / "nas")
    manager = PluginManager(tmp_path / "home", peer_id, cache=tmp_path / "plugins", nas=nas)
    nas.plugin_manager = manager
    service = NodeApps(AppStore(tmp_path / "business", key), peer_id, lambda: None, plugins=manager)
    fake_app = SimpleNamespace(state=SimpleNamespace(node_apps=service, craft_plugins=manager, nas_plugin=nas))
    try:
        manager.install("resume")
        app_tools = AppTools(fake_app)
        plugin_tools = PluginTools(fake_app)
        context = {
            "consumer_node_id": peer_id,
            "model_destination": {"catalog_model_id": "local:test", "provider_peer_id": peer_id},
        }

        # 1. PluginTools.execute("ryn_list_plugins") includes `operations` directly on business plugins
        listed = plugin_tools.execute("ryn_list_plugins", {}, lambda: False)
        resume_row = next(p for p in listed["plugins"] if p["id"] == "resume")
        assert "operations" in resume_row and "create_resume" in resume_row["operations"]

        # 2. Omit `reviewed_context` -> preview & execute automatically resolve fresh workspace context
        args_without_ctx = {
            "app": "resume",
            "action": "create_resume",
            "arguments": {"name": "Ada Lovelace"},
        }
        preview = app_tools.preview(args_without_ctx, context)
        assert preview["app_preview"]["app"] == "resume"
        executed = app_tools.execute(
            "ryn_node_app_action",
            args_without_ctx,
            "op_app_1",
            lambda: False,
            preview,
            context,
        )
        assert executed["app"] == "resume"
        assert executed["action"] == "create_resume"
    finally:
        manager.close()


def test_step2_code_mode_parallel_reads_store_load_pragma_and_write_rejection(tmp_path: Path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "alpha.txt").write_text("alpha-line-1\nalpha-line-2\n", encoding="utf-8", newline="\n")
    (ws / "beta.txt").write_text("beta-line-1\nbeta-line-2\n", encoding="utf-8", newline="\n")

    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store

    class _HostTools:
        def __init__(self):
            self.local = local
            self.runner = code_mode.CodeModeRunner(self)

        def specs(self):
            return [
                {"type": "function", "function": {"name": k, "description": v[0], "parameters": copy.deepcopy(v[3])}}
                for k, v in LOCAL_SPECS.items()
            ] + [copy.deepcopy(CODE_MODE_SPEC)]

        def metadata(self, name: str):
            if name == code_mode.NAME:
                return {"effect": "read", "title": "Execute CodeMode JS", "scope_context_supported": True}
            desc, eff, risk, _ = LOCAL_SPECS[name]
            return {"effect": eff, "title": desc, "risk": risk, "scope_context_supported": True}

        def execute(self, name: str, arguments: dict, *, operation_id: str, cancelled, context=None, expected_preview=None):
            if name == code_mode.NAME:
                return self.runner.execute(arguments, operation_id=operation_id, cancelled=cancelled, context=context)
            return self.local.execute(
                name,
                arguments,
                operation_id=operation_id,
                context=context,
                expected_preview=expected_preview,
                cancelled=cancelled,
            )

    host = _HostTools()
    deferred_observed: dict[str, str | None] = {}
    ctx = {
        "task_id": "task_code_1",
        "context_epoch": 0,
        "scope_revision": 1,
        "local_execution": {"workspace_root": str(ws), "desktop_access": False},
        "_deferred_observed_files": deferred_observed,
        "_observed_files_snapshot": {},
    }

    # 1. Parallel read tools via Promise.all + store() + // @exec pragma
    script_1 = """// @exec: {"yield_time_ms": 8000, "max_output_tokens": 1000}
const [a, b] = await Promise.all([
  tools.ryn_file_read({ path: "alpha.txt" }),
  tools.ryn_file_read({ path: "beta.txt" }),
]);
store("combined", { alpha: a.text.trim(), beta: b.text.trim() });
text("Read both files");
return { count: 2, has_read_tool: ALL_TOOLS.some(t => t.name === "ryn_file_read") };
"""
    res1 = host.execute(
        "ryn_code_exec",
        {"code": script_1},
        operation_id="op_code_1",
        cancelled=lambda: False,
        context=ctx,
    )
    assert res1["status"] == "completed", res1
    assert res1["output"] == "Read both files"
    assert res1["return_value"] == {"count": 2, "has_read_tool": True}
    assert res1["stored_keys"] == ["combined"]
    assert len(res1["tool_calls"]) == 2
    # Both files read inside `ryn_code_exec` must be recorded in `_deferred_observed_files`
    assert len(deferred_observed) == 2

    # 2. Load persisted state in next call + test `as_settled` helper
    script_2 = """
const prev = load("combined");
const settled = [];
for await (const item of as_settled([
  tools.ryn_file_list({ path: "." }),
])) {
  settled.push(item.status);
}
return { prev, settled };
"""
    res2 = host.execute(
        "ryn_code_exec",
        {"code": script_2},
        operation_id="op_code_2",
        cancelled=lambda: False,
        context=ctx,
    )
    assert res2["status"] == "completed"
    assert res2["return_value"]["prev"] == {"alpha": "alpha-line-1\nalpha-line-2", "beta": "beta-line-1\nbeta-line-2"}
    assert res2["return_value"]["settled"] == ["fulfilled"]

    # 3. Attempting to call a write tool (`ryn_file_write`) inside `ryn_code_exec` is strictly forbidden
    script_write = """
await tools.ryn_file_write({ path: "hacked.txt", content: "bad" });
"""
    res_write = host.execute(
        "ryn_code_exec",
        {"code": script_write},
        operation_id="op_code_3",
        cancelled=lambda: False,
        context=ctx,
    )
    assert res_write["status"] == "failed"
    assert "agent_code_exec_write_forbidden" in res_write.get("error_detail", "") or not (ws / "hacked.txt").exists()
    assert not (ws / "hacked.txt").exists()


def test_step3_multimodal_image_notes_and_code_mode_image_artifact(tmp_path: Path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store
    adapter = ExtensionToolAdapter(local, lambda: ledger, node_store=node_store)

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 200, 40)).save(buf, format="PNG")
    png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    binding = {"server_id": "demo", "original_tool_name": "shot", "model_tool_name": "mcp__demo__shot"}
    raw_mcp = {"isError": False, "content": [{"type": "image", "mimeType": "image/png", "data": png_b64}]}

    # When route supports image input, note tells model pixels are attached to active turn
    norm_supported = adapter._normalize_mcp_result(
        raw_mcp,
        binding=binding,
        operation_id="op_img_1",
        context={
            "task_id": "task_img",
            "local_execution": {"workspace_root": str(ws)},
            "_image_capability": {"supports_image_input": True},
        },
    )
    assert "attached to the active turn" in norm_supported["image_note"]
    assert "attached to the active turn" in norm_supported["content"][0]["note"]

    # Also verify `image(dataUrl)` in `ryn_code_exec` saves verified artifact without raw base64 in content_items
    runner = code_mode.CodeModeRunner(SimpleNamespace(local=local, specs=lambda: [], metadata=lambda _n: {}))
    img_res = runner.execute(
        {"code": f'image("data:image/png;base64,{png_b64}"); text("captured");'},
        operation_id="op_code_img_1",
        cancelled=lambda: False,
        context={
            "task_id": "task_img",
            "context_epoch": 0,
            "scope_revision": 1,
            "local_execution": {"workspace_root": str(ws)},
        },
    )
    assert img_res["status"] == "completed"
    assert len(img_res.get("mcp_artifacts", [])) == 1
    assert img_res["mcp_artifacts"][0]["width"] == 16
    assert "base64," not in json.dumps(img_res)


def test_step4_codex_tools_default_budget_and_strict_schema_for_new_tools(monkeypatch):
    from test_codex_object_protocol import assert_strict_subset

    tools = {
        tool_menu.SPEC["function"]["name"]: copy.deepcopy(tool_menu.SPEC["function"]),
        CODE_MODE_SPEC["function"]["name"]: copy.deepcopy(CODE_MODE_SPEC["function"]),
    }
    strict_schema = codex_tools._response_schema(tools, strict=True)
    assert_strict_subset(strict_schema)

    captured = {}

    def fake_native_chat(adapter, body, **kwargs):
        captured["body"] = body
        return {"text": json.dumps({"content": "ok", "tool_calls": []})}

    monkeypatch.setattr("rynmesh.llm_package.codex_session._native_chat", fake_native_chat)
    codex_tools.chat(
        SimpleNamespace(),
        {"messages": [{"role": "user", "content": "hi"}], "tools": [tool_menu.SPEC, CODE_MODE_SPEC]},
        task_id="t1",
        timeout_s=5,
    )
    assert captured["body"]["max_tokens"] == 2048
    assert "Reply within 2048 tokens." in captured["body"]["messages"][0]["content"]


def test_phase2_upgrade1_workstation_inline_head_tail_preview_with_result_ref():
    from rynmesh.agent_runtime import results
    from rynmesh.agent_runtime.store import encoded

    reference = {
        "id": "result_" + "a" * 32,
        "sha256": "b" * 64,
        "bytes": 45000,
    }
    big_file_result = {
        "path": "src/main.py",
        "sha256": "c" * 64,
        "bytes": 45000,
        "start_line": 1,
        "end_line": 400,
        "total_lines": 1000,
        "next_start_line": 401,
        "next_offset": None,
        "text": "HEAD_MARKER_LINE\n" + ("middle_line = 1\n" * 2000) + "TAIL_MARKER_LINE\n",
    }
    formatted = results.format_reference_result("ryn_file_read", big_file_result, reference, max_bytes=4000)
    assert len(encoded(formatted)) <= 4000
    assert formatted["result_ref"] == reference["id"]
    assert formatted["truncated_inline"] is True
    assert formatted["path"] == "src/main.py"
    assert formatted["start_line"] == 1 and formatted["end_line"] == 400
    assert "HEAD_MARKER_LINE" in formatted["text"]
    assert "TAIL_MARKER_LINE" in formatted["text"]
    assert "chars truncated" in formatted["text"]

    # Non-workstation tools still return pure result_ref without leaking text inline
    non_ws = results.format_reference_result(
        "inspect_service",
        {"text": "PRIVATE_SECRET " * 500},
        reference,
        max_bytes=4000,
    )
    assert "text" not in non_ws
    assert non_ws["result_ref"] == reference["id"]


def test_phase2_upgrade2_seek_sequence_unicode_normalization_and_unread_file_edit(tmp_path: Path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from rynmesh.agent_runtime import local_patch

    # 1. local_patch normalizes curly quotes, em-dashes, and non-breaking spaces while preserving leading indentation
    original_src = 'def greet():\n    msg = \u201chello \u2014 world\u201d\u00a0  \n    return msg\n'
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: greet.py\n"
        "@@ def greet():\n"
        '     msg = "hello - world"\n'
        "-    return msg\n"
        "+    return msg.upper()\n"
        "*** End Patch"
    )
    ops = local_patch.parse(patch_text)
    patched = local_patch.apply_hunks(original_src, ops[0]["hunks"], path="greet.py")
    assert "return msg.upper()" in patched

    # 2. Unread file can be edited directly via ryn_file_edit without a mandatory prior read,
    #    while external changes after a read are still rejected
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "unread.py"
    target.write_text('def run():\n    title = \u201cSmart \u2013 Title\u201d   \n    return 1\n', encoding="utf-8")

    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = agent_store
    ctx = {
        "task_id": "task_edit_unread",
        "local_execution": {"workspace_root": str(ws), "desktop_access": False},
        "_observed_files_snapshot": {},
        "_deferred_observed_files": {},
    }

    # Edit unread file using ASCII quotes & no trailing spaces
    edit_args = {
        "path": "unread.py",
        "old_text": '    title = "Smart - Title"\n    return 1\n',
        "new_text": '    title = "Smart - Title"\n    return 2\n',
    }
    preview = local.preview("ryn_file_edit", edit_args, ctx)
    res = local.execute(
        "ryn_file_edit",
        edit_args,
        operation_id="op_unread_edit_1",
        context=ctx,
        expected_preview=preview,
        cancelled=lambda: False,
    )
    assert res["written"] is True
    assert "return 2" in target.read_text(encoding="utf-8")

    # After the edit recorded the file's observed sha256, an external modification on disk is caught!
    key = local._observed_key(target)
    ctx["_observed_files_snapshot"] = {key: {"sha256": res["sha256"]}}
    target.write_text("externally modified\n", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_local_file_changed_read_again"):
        local.preview(
            "ryn_file_edit",
            {"path": "unread.py", "old_text": "externally modified\n", "new_text": "x\n"},
            ctx,
        )


def test_phase2_upgrade3_read_file_indentation_block_mode(tmp_path: Path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    sample_code = (
        "import os\n"
        "\n"
        "class Service:\n"
        "    # Compute important value\n"
        "    @staticmethod\n"
        "    def compute(x: int) -> int:\n"
        "        step = x + 1\n"
        "        if step > 10:\n"
        "            return step * 2\n"
        "        return step\n"
        "\n"
        "    def other(self) -> int:\n"
        "        return 0\n"
    )
    (ws / "service.py").write_text(sample_code, encoding="utf-8", newline="\n")

    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = agent_store
    ctx = {"task_id": "task_indent", "local_execution": {"workspace_root": str(ws), "desktop_access": False}}

    # Reading around line 9 (`return step * 2`) with max_levels=2 extracts the entire `compute` method + header comments/decorators
    block = local.perform(
        "ryn_file_read",
        {"path": "service.py", "mode": "indentation", "anchor_line": 9, "max_levels": 2},
        ctx,
        lambda: False,
    )
    assert block["mode"] == "indentation"
    assert block["start_line"] == 4
    assert block["end_line"] == 10
    assert "# Compute important value" in block["text"]
    assert "@staticmethod" in block["text"]
    assert "def compute(x: int) -> int:" in block["text"]
    assert "def other" not in block["text"]


def test_phase2_upgrade4_shell_yield_seconds_git_safe_read_and_non_ambiguous_rejection(tmp_path: Path):
    from test_agent_runtime import make_runtime

    from rynmesh.agent_runtime.execution_policy import shell_read

    # 1. Safe git inspection commands are recognized as read-only
    assert shell_read({"command": "git status"}) is True
    assert shell_read({"command": "git diff HEAD"}) is True
    assert shell_read({"command": "git log -n 5"}) is True
    assert shell_read({"command": "git push origin main"}) is False
    assert shell_read({"command": "git checkout -b feat"}) is False
    assert shell_read({"command": "git diff --output=out.patch"}) is False

    # 2. yield_seconds returns running background process when command exceeds yield window
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)
    runtime, _, _, _ = make_runtime(home / "agent")
    agent_store = runtime.store
    cfg = {"workspace_root": str(ws), "desktop_access": False}
    runtime.update_settings(cfg)
    task = runtime.create({"message": "Test shell yield_seconds"})
    task_id = task["id"]
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = agent_store
    ctx = {"task_id": task_id, "local_execution": cfg}
    try:
        import os
        cmd = "Write-Output 'ready'; Start-Sleep 10" if os.name == "nt" else "echo ready; sleep 10"
        args = {"command": cmd, "yield_seconds": 0.3}
        preview = local.preview("ryn_shell_execute", args, ctx)
        res = local.execute(
            "ryn_shell_execute",
            args,
            operation_id="op_yield_1",
            context=ctx,
            expected_preview=preview,
            cancelled=lambda: False,
        )
        assert res["running"] is True
        assert res["process_id"].startswith("process_")
        stop_args = {"process_id": res["process_id"]}
        local.execute(
            "ryn_process_stop",
            stop_args,
            operation_id="op_stop_1",
            context=ctx,
            expected_preview=local.preview("ryn_process_stop", stop_args, ctx),
            cancelled=lambda: False,
        )
        # Writing to an already stopped process raises AgentToolRejected and records not_executed (not ambiguous write!)
        write_args = {"process_id": res["process_id"], "text": "hello\n"}
        write_preview = local.preview("ryn_process_write", write_args, ctx)
        with pytest.raises(AgentToolRejected, match="agent_process_not_running"):
            local.execute(
                "ryn_process_write",
                write_args,
                operation_id="op_write_stopped_1",
                context=ctx,
                expected_preview=write_preview,
                cancelled=lambda: False,
            )
        assert ledger.get("op_write_stopped_1")["status"] == "not_executed"
    finally:
        local.processes.close()


def test_phase2_upgrade5_codex_tools_markdown_fence_tolerance():
    tools = {
        "ryn_file_list": {
            "name": "ryn_file_list",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }
    }
    fenced = '```json\n{"content": "Listing directory", "tool_calls": [{"name": "ryn_file_list", "arguments": {"path": "."}}]}\n```'
    result = {"text": fenced}
    msg = codex_tools._validated(result, tools, required=True)
    assert msg["content"] == "Listing directory"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "ryn_file_list"


def test_phase3_universal_mcp_and_computer_use_speed_optimizations(tmp_path: Path):
    from rynmesh.agent_runtime import results
    from rynmesh.agent_runtime.extensions import (
        COMPUTER_USE_PLUGIN_ID,
        COMPUTER_USE_SERVER_ID,
        ExtensionStore,
    )
    from rynmesh.services import peer_box

    # 1. MCP / computer-use oversized results get inline structured_preview + text instead of bare blind-box
    ref = {"id": "result_" + "b" * 32, "sha256": "c" * 64, "bytes": 16000}
    mcp_payload = {
        "server_id": "computer-use",
        "tool": "computer_observe_windows",
        "model_tool_name": "mcp__computer_use__computer_observe_windows__10ed2a80",
        "is_error": False,
        "text": "Observed 31 windows.\n- hwnd=2689908 title='微信' proc=Weixin.exe rect=[586, 93, 1445, 863]",
        "structuredContent": {
            "windows": [{"hwnd": 2689908, "title": "微信", "padding": "X" * 9000}],
            "count": 31,
        },
    }
    inline_mcp = results.format_reference_result(
        "mcp__computer_use__computer_observe_windows__10ed2a80",
        mcp_payload,
        ref,
        max_bytes=4000,
    )
    assert inline_mcp["result_ref"] == ref["id"]
    assert inline_mcp.get("truncated_inline") is True
    assert "2689908" in inline_mcp.get("text", "")
    assert "structured_preview" in inline_mcp
    assert "2689908" in inline_mcp["structured_preview"]

    # 2. ExtensionStore caches decrypted state in memory by (mtime_ns, size)
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    ext_store = ExtensionStore(tmp_path / "agent", key)
    ext_store.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, True, desktop_access=True)
    open_calls = 0
    orig_open = peer_box.open_sealed

    def counting_open(*a, **kw):
        nonlocal open_calls
        open_calls += 1
        return orig_open(*a, **kw)

    peer_box.open_sealed = counting_open
    try:
        for _ in range(5):
            srvs = ext_store.list_enabled_mcp_servers_internal()
            assert any(s["id"] == COMPUTER_USE_SERVER_ID for s in srvs)
        assert open_calls == 0
    finally:
        peer_box.open_sealed = orig_open


def test_phase4_vendor_context_window_and_compact_computer_observation():
    from rynmesh.agent_runtime import results
    from rynmesh.agent_runtime.native_computer import _compact_public_element
    from rynmesh.llm_package import model_settings

    auto_record = {
        "context_window_mode": "auto",
        "models": ["deepseek-flash", "deepseek-v4-pro", "gpt-5.4", "claude-sonnet-4-5", "unknown"],
        "model_metadata": {},
    }
    assert model_settings.context_metadata(auto_record, "deepseek-flash") == {
        "context_window": 1048576,
        "context_window_basis": "vendor_family_default",
        "context_window_known": True,
    }
    assert model_settings.context_metadata(auto_record, "deepseek-v4-pro")["context_window"] == 1048576
    assert model_settings.defaults(auto_record, "deepseek-flash")["max_output_tokens"] == 393216
    assert model_settings.defaults(auto_record, "deepseek-flash")["max_concurrent"] == 2500
    assert model_settings.defaults(auto_record, "deepseek-v4-pro")["max_output_tokens"] == 393216
    assert model_settings.defaults(auto_record, "deepseek-v4-pro")["max_concurrent"] == 500
    assert model_settings.context_metadata(auto_record, "gpt-5.4")["context_window"] == 272000
    assert model_settings.context_metadata(auto_record, "claude-sonnet-4-5")["context_window"] == 200000
    assert model_settings.context_metadata(auto_record, "unknown") == {
        "context_window": 32768,
        "context_window_basis": "unknown_fallback",
        "context_window_known": False,
    }

    compact = _compact_public_element({
        "id": "el-0",
        "index": 0,
        "name": "搜索",
        "type": "Edit",
        "value": "test",
        "password": False,
        "enabled": True,
        "offscreen": False,
        "focused": False,
        "x": 100,
        "y": 200,
        "depth": 6,
        "patterns": ["ValuePattern"],
        "bounds": {"left": 80, "top": 190, "width": 40, "height": 20},
    })
    assert set(compact.keys()) == {"id", "name", "type", "value", "x", "y", "bounds"}

    ref = {"id": "result_" + "d" * 32, "sha256": "e" * 64, "bytes": 22000}
    act_payload = {
        "server_id": "computer-use",
        "tool": "computer_act_window",
        "model_tool_name": "mcp__computer_use__computer_act_window__10ed2a80",
        "is_error": False,
        "text": "click committed.",
        "structuredContent": {
            "status": "committed",
            "hwnd": 100100,
            "observation": {
                "hwnd": 100100,
                "title": "Demo",
                "state_token": "st_after_act",
                "observation_id": "obs_after_act",
                "rect": [0, 0, 800, 600],
                "elements": [{"id": f"el-{i}", "name": "X" * 200} for i in range(80)],
            },
        },
    }
    inline_act = results.format_reference_result(
        "mcp__computer_use__computer_act_window__10ed2a80",
        act_payload,
        ref,
        max_bytes=4000,
    )
    assert inline_act["structuredContent"]["observation"]["state_token"] == "st_after_act"
    assert "elements" not in inline_act["structuredContent"]["observation"]
