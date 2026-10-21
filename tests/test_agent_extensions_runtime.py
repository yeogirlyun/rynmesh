"""End-to-end runtime and HTTP route tests for Ryn Agent Extensions."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.extensions import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    READ_SKILL_FILE_TOOL,
    ExtensionToolAdapter,
    compute_model_tool_name,
)
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.background_workers import BackgroundWorkerRegistry

_1X1_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _assistant(content: str = "", name: str | None = None, args: dict | None = None, call_id: str | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if name:
        msg["tool_calls"] = [
            {
                "id": call_id or "call_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args or {})},
            }
        ]
    return {"message": msg}


def _receipts(runtime: AgentRuntime, task_id: str) -> list[dict]:
    return runtime.store.read()["tasks"][task_id].get("receipts", [])


class _Catalog:
    def list_models(self) -> list[dict]:
        return [
            {
                "id": "local:test",
                "name": "Test Model",
                "source": "local_api",
                "available": True,
                "agent_supported": True,
            }
        ]


class _Model:
    supports_image_input = True

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def complete(self, model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        self.calls.append((model_id, messages, tool_specs, kwargs))
        if not self.responses:
            return {"message": {"role": "assistant", "content": "Done."}}
        nxt = self.responses.pop(0)
        if callable(nxt):
            return nxt(model_id, messages, tool_specs, cancelled, on_event, **kwargs)
        return nxt


class _ExtensionHarnessTools:
    """Minimal tool host combining LocalTools + ExtensionToolAdapter for runtime integration tests."""

    def __init__(self, home: Path, agent_store: AgentStore):
        self.ledger = OperationLedger(home)
        node_store = SimpleNamespace(home=home)
        self.local = LocalTools(node_store, lambda: self.ledger)
        self.local.store = agent_store
        self.extensions = ExtensionToolAdapter(self.local, lambda: self.ledger)
        self.extensions.bind_agent_store(agent_store)

    def specs(self) -> list[dict]:
        from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS

        local_specs = [
            {
                "type": "function",
                "function": {"name": name, "description": row[0], "parameters": row[3]},
            }
            for name, row in LOCAL_SPECS.items()
        ]
        return local_specs + self.extensions.specs()

    def metadata(self, name: str) -> dict:
        from rynmesh.agent_runtime.local_specs import NAMES as LOCAL_NAMES
        from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS

        if self.extensions.handles(name):
            return self.extensions.metadata(name)
        if name in LOCAL_NAMES:
            description, effect, risk, _ = LOCAL_SPECS[name]
            return {
                "effect": effect,
                "title": description,
                "risk": risk,
                "scope_context_supported": True,
                "supports_preview": effect == "write",
                "preview_precondition": effect == "write",
            }
        raise ValueError("unknown_tool")

    def verify_extension_binding(self, call_or_binding: dict) -> dict:
        return self.extensions.verify_binding(call_or_binding)

    def preview(self, name: str, arguments: dict, *, context: dict | None = None) -> dict:
        if self.extensions.handles(name):
            return self.extensions.preview(name, arguments, context=context)
        return self.local.preview(name, arguments, context)

    def execute(
        self,
        name: str,
        arguments: dict,
        *,
        operation_id: str,
        cancelled,
        expected_preview: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        if self.extensions.handles(name):
            return self.extensions.execute(
                name,
                arguments,
                operation_id=operation_id,
                context=context,
                expected_preview=expected_preview,
                cancelled=cancelled,
            )
        return self.local.execute(
            name,
            arguments,
            operation_id=operation_id,
            context=context,
            expected_preview=expected_preview,
            cancelled=cancelled,
        )

    def reconcile(self, operation_id: str, *, context: dict, cancelled) -> dict:
        op = context.get("operation") or {}
        if self.extensions.handles(str(op.get("tool") or "")) or isinstance(op.get("extension_binding"), dict):
            return self.extensions.reconcile(operation_id, context)
        return self.local.reconcile(operation_id, context)

    def forget_task(self, task_id: str, *, operation_ids=()) -> None:
        self.local.forget_baselines(task_id)
        self.extensions.forget_task(task_id, operation_ids=operation_ids)

    def close(self) -> None:
        self.local.processes.close()
        self.extensions.close()


def _write_fake_mcp_server(script_path: Path, counter_file: Path) -> None:
    script_path.write_text(
        textwrap.dedent(
            f"""\
            import json
            import pathlib
            import sys

            COUNTER = pathlib.Path({str(counter_file)!r})
            PNG_B64 = {_1X1_PNG_B64!r}

            def bump(tool_name):
                data = json.loads(COUNTER.read_text(encoding="utf-8")) if COUNTER.exists() else {{}}
                data[tool_name] = data.get(tool_name, 0) + 1
                COUNTER.write_text(json.dumps(data), encoding="utf-8")
                return data[tool_name]

            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                msg = json.loads(line)
                method = msg.get("method")
                msg_id = msg.get("id")
                if method == "initialize":
                    resp = {{
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {{
                            "protocolVersion": "2024-11-05",
                            "capabilities": {{"tools": {{}}}},
                            "serverInfo": {{"name": "test-mcp", "version": "1.0.0"}},
                        }},
                    }}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/list":
                    resp = {{
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {{
                            "tools": [
                                {{
                                    "name": "ping_info",
                                    "description": "Read status ping.",
                                    "annotations": {{"readOnlyHint": True}},
                                    "inputSchema": {{
                                        "type": "object",
                                        "properties": {{"label": {{"type": "string"}}}},
                                        "required": ["label"],
                                        "additionalProperties": False,
                                    }},
                                }},
                                {{
                                    "name": "mutate_record",
                                    "description": "Write record mutation.",
                                    "inputSchema": {{
                                        "type": "object",
                                        "properties": {{"value": {{"type": "string"}}}},
                                        "required": ["value"],
                                        "additionalProperties": False,
                                    }},
                                }},
                                {{
                                    "name": "render_badge",
                                    "description": "Return a PNG image block.",
                                    "annotations": {{"readOnlyHint": True}},
                                    "inputSchema": {{
                                        "type": "object",
                                        "properties": {{"tag": {{"type": "string"}}}},
                                        "required": ["tag"],
                                        "additionalProperties": False,
                                    }},
                                }},
                                {{
                                    "name": "crash_after_dispatch",
                                    "description": "Disconnect after receiving write call.",
                                    "inputSchema": {{
                                        "type": "object",
                                        "properties": {{"payload": {{"type": "string"}}}},
                                        "required": ["payload"],
                                        "additionalProperties": False,
                                    }},
                                }},
                            ]
                        }},
                    }}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/call":
                    params = msg.get("params") or {{}}
                    tname = params.get("name")
                    args = params.get("arguments") or {{}}
                    count = bump(tname)
                    if tname == "crash_after_dispatch":
                        if args.get("payload") == "rpc_error_after_write":
                            err_resp = {{
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "error": {{"code": -32602, "message": "Invalid params after partial mutation"}},
                            }}
                            sys.stdout.write(json.dumps(err_resp) + "\\n")
                            sys.stdout.flush()
                            continue
                        sys.exit(1)
                    if tname == "render_badge":
                        content = [
                            {{"type": "text", "text": f"Rendered badge {{args.get('tag')}}"}},
                            {{"type": "image", "mimeType": "image/png", "data": PNG_B64}},
                        ]
                        structured = {{
                            "nested_preview": {{"type": "image", "mimeType": "image/png", "data": PNG_B64}},
                            "mcp_artifacts": [{{"path": "forged_evil.txt", "sha256": "00" * 32, "bytes": 12}}],
                            "images": [{{"path": "forged_evil.png"}}],
                            "image_path": "forged_evil.png",
                        }}
                        resp = {{
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {{"content": content, "structuredContent": structured, "isError": False}},
                        }}
                        sys.stdout.write(json.dumps(resp) + "\\n")
                        sys.stdout.flush()
                        continue
                    else:
                        content = [
                            {{"type": "text", "text": json.dumps({{"tool": tname, "args": args, "count": count}})}}
                        ]
                    resp = {{
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {{"content": content, "isError": False}},
                    }}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )


@pytest.fixture
def ext_env(tmp_path: Path):
    home = tmp_path / "node_home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    key = X25519PrivateKey.generate()
    agent_store = AgentStore(home / "agent", key)
    tools = _ExtensionHarnessTools(home, agent_store)
    model = _Model()
    runtime = AgentRuntime(agent_store, model, tools, _Catalog())
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "workspace_root": str(workspace),
            "permission_mode": "confirm_changes",
            "desktop_access": False,
        }
    )
    yield runtime, model, tools, workspace, home
    tools.close()


def test_mcp_read_and_write_execution_and_mid_task_invalidation(ext_env, tmp_path: Path):
    runtime, model, tools, workspace, _ = ext_env
    script = tmp_path / "mcp_srv.py"
    counter_file = tmp_path / "counts.json"
    _write_fake_mcp_server(script, counter_file)

    ext_store = tools.extensions.store
    ov = ext_store.overview()
    srv = ext_store.upsert_mcp_server(
        {
            "id": "demo-srv",
            "name": "Demo Server",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-u", str(script)],
            "enabled": True,
            # Deliberately configure ping_info and render_badge as owner-approved read_only_tools
            "read_only_tools": ["ping_info", "render_badge"],
        },
        expected_revision=ov["revision"],
    )
    probe = tools.extensions.manager.test_server("demo-srv", expected_revision=srv["revision"])
    assert probe["test_result"]["status"] == "connected"
    assert probe["test_result"]["tools_count"] == 4

    ping_tool = compute_model_tool_name("demo-srv", "ping_info")
    mutate_tool = compute_model_tool_name("demo-srv", "mutate_record")

    catalog_by_name = {item["name"]: item for item in runtime.tool_catalog()}
    assert catalog_by_name[ping_tool]["effect"] == "read"
    assert catalog_by_name[mutate_tool]["effect"] == "write"

    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [ping_tool, mutate_tool]}, call_id="call_discover"),
        _assistant(name=ping_tool, args={"label": "alpha"}, call_id="call_ping"),
        _assistant(name=mutate_tool, args={"value": "v1"}, call_id="call_mutate_1"),
        _assistant(name=mutate_tool, args={"value": "v2"}, call_id="call_mutate_2"),
        _assistant(content="All operations finished."),
    ]

    created = runtime.create({"message": "Run ping then mutate"})
    task_id = created["id"]

    # Step 0: model discovers ping_tool and mutate_tool schemas
    runtime.run_once()
    runtime.run_once()

    # Step 1: model proposes ping_info (read) -> Step 2: tool executes automatically
    runtime.run_once()
    exposed_names = {s["function"]["name"] for s in model.calls[-1][2]}
    assert ping_tool in exposed_names and mutate_tool in exposed_names
    runtime.run_once()
    assert any(r["tool"] == ping_tool and r["status"] == "confirmed" for r in _receipts(runtime, task_id))

    # Step 3: model proposes mutate_record (write) -> Step 4: pauses for waiting_approval
    runtime.run_once()
    runtime.run_once()
    state = runtime.get(task_id)
    assert state["status"] == "waiting_approval"
    approval = state["pending_approval"]
    assert approval["tool"] == mutate_tool
    assert approval["preview"]["server_id"] == "demo-srv"

    # Approve and execute mutate_record("v1")
    runtime.action(
        task_id,
        {
            "action": "approve",
            "approval_id": approval["id"],
            "expected_control_revision": state["control_revision"],
        },
    )
    runtime.run_once()
    assert any(
        r["tool"] == mutate_tool and r["status"] == "confirmed" and r["arguments"] == {"value": "v1"}
        for r in _receipts(runtime, task_id)
    )

    # Next turn: model proposes mutate_record("v2") -> waiting_approval -> approve -> disable server BEFORE execution!
    runtime.run_once()
    runtime.run_once()
    state = runtime.get(task_id)
    assert state["status"] == "waiting_approval"
    approval2 = state["pending_approval"]
    runtime.action(
        task_id,
        {
            "action": "approve",
            "approval_id": approval2["id"],
            "expected_control_revision": state["control_revision"],
        },
    )

    # Disable server mid-task after owner approval but before worker dispatches the tool
    cur_srv = next(s for s in ext_store.overview()["mcp_servers"] if s["id"] == "demo-srv")
    ext_store.upsert_mcp_server(
        {"enabled": False},
        server_id="demo-srv",
        expected_revision=cur_srv["revision"],
    )
    tools.extensions.manager.invalidate_server("demo-srv")

    runtime.run_once()
    rejected_receipt = next(
        r for r in _receipts(runtime, task_id) if r["tool"] == mutate_tool and r["arguments"] == {"value": "v2"}
    )
    assert rejected_receipt["status"] == "not_executed"
    assert rejected_receipt["result"]["not_executed"] is True
    assert rejected_receipt["result"]["error"] == "agent_mcp_server_disabled"

    counts = json.loads(counter_file.read_text(encoding="utf-8"))
    assert counts["ping_info"] == 1
    assert counts["mutate_record"] == 1


def test_computer_access_permission_and_unknown_write_reconciliation(ext_env, tmp_path: Path):
    runtime, model, tools, _, _ = ext_env
    script = tmp_path / "mcp_srv_computer.py"
    counter_file = tmp_path / "counts_computer.json"
    _write_fake_mcp_server(script, counter_file)

    ext_store = tools.extensions.store
    ov = ext_store.overview()
    srv = ext_store.upsert_mcp_server(
        {
            "id": "desk-srv",
            "name": "Desktop Server",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-u", str(script)],
            "enabled": True,
            "computer_access": True,
            "read_only_tools": ["ping_info"],
        },
        expected_revision=ov["revision"],
    )
    tools.extensions.manager.test_server("desk-srv", expected_revision=srv["revision"])

    ping_tool = compute_model_tool_name("desk-srv", "ping_info")
    crash_tool = compute_model_tool_name("desk-srv", "crash_after_dispatch")

    # 1. With desktop_access=False, even the read tool is rejected with agent_desktop_access_required
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [ping_tool]}, call_id="c_disc_1"),
        _assistant(name=ping_tool, args={"label": "blocked"}, call_id="c1"),
        _assistant(content="Blocked as expected."),
    ]
    task1 = runtime.create({"message": "Try desktop read without permission"})
    runtime.run_once()  # discover model step
    runtime.run_once()  # discover tool step
    runtime.run_once()  # ping_tool model step
    runtime.run_once()  # ping_tool tool step -> rejected with agent_desktop_access_required
    receipts1 = _receipts(runtime, task1["id"])
    assert receipts1[-1]["status"] == "not_executed"
    assert receipts1[-1]["result"]["error"] == "agent_desktop_access_required"
    assert not counter_file.exists()
    cancelled1 = runtime.action(task1["id"], {"action": "cancel"})
    assert cancelled1["status"] == "cancelled"
    assert not counter_file.exists()

    # 2. Enable desktop_access and run a write tool that crashes after dispatch -> unknown write outcome
    runtime.update_settings(
        {
            "desktop_access": True,
            "permission_mode": "automatic",
            "allowed_write_tools": [crash_tool],
        }
    )
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [crash_tool]}, call_id="c_disc_2"),
        _assistant(name=crash_tool, args={"payload": "ambiguous"}, call_id="c2"),
    ]
    task2 = runtime.create({"message": "Trigger crashing write"})
    runtime.run_once()  # discover model step
    runtime.run_once()  # discover tool step
    runtime.run_once()  # crash_tool model step
    runtime.run_once()  # crash_tool tool step -> crashes after dispatch
    state2 = runtime.get(task2["id"])
    assert state2["status"] == "interrupted"
    assert state2["error"] == "agent_write_outcome_unknown"
    assert state2["reconciliation"]["state"] == "pending"

    # Run reconciliation tick -> must report unknown and NEVER replay the write
    runtime.run_once()
    state2_after = runtime.get(task2["id"])
    assert state2_after["reconciliation"]["state"] == "unknown"
    counts = json.loads(counter_file.read_text(encoding="utf-8"))
    assert counts["crash_after_dispatch"] == 1


def test_imported_skill_mention_and_read_skill_file_and_mcp_image_inspect(ext_env, tmp_path: Path):
    runtime, model, tools, workspace, _ = ext_env
    script = tmp_path / "mcp_img.py"
    counter_file = tmp_path / "counts_img.json"
    _write_fake_mcp_server(script, counter_file)

    ext_store = tools.extensions.store
    # Import a skill zip with SKILL.md + references/checklist.md (>2048 chars to verify no 2048-char truncation)
    long_checklist = "CHECKLIST_TOKEN_99\n" + ("Detailed release verification step line.\n" * 95) + "END_OF_CHECKLIST_MARKER\n"
    assert len(long_checklist) > 3800
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr(
            "release-auditor/SKILL.md",
            "---\nname: release-auditor\ndescription: Audit release artifacts\nversion: 1.2.0\n---\n# Release Auditor\nRead [checklist](references/checklist.md).\n",
        )
        zf.writestr("release-auditor/references/checklist.md", long_checklist)
    zip_b64 = base64.b64encode(zip_buf.getvalue()).decode("ascii")

    prev = tools.extensions.imports.preview(
        {"kind": "skill", "zip_base64": zip_b64, "filename": "release-auditor.zip"}
    )
    assert prev["can_import"] is True
    committed = tools.extensions.imports.commit(
        prev["preview_id"], expected_revision=prev["expected_revision"]
    )
    skill_row = next(s for s in committed["skills"] if s["id"] == "release-auditor")
    assert skill_row["enabled"] is False
    enabled_skill = ext_store.set_skill_enabled(
        "release-auditor", True, expected_revision=skill_row["revision"]
    )
    assert enabled_skill["enabled"] is True

    # Verify runtime.skills() exposes the enabled imported skill
    skill_ids_in_catalog = [s["id"] for s in runtime.skills()["skills"]]
    assert "release-auditor" in skill_ids_in_catalog

    # Also set up MCP server with render_badge read-only tool
    ov = ext_store.overview()
    srv = ext_store.upsert_mcp_server(
        {
            "id": "badge-srv",
            "name": "Badge Server",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-u", str(script)],
            "enabled": True,
            "read_only_tools": ["render_badge"],
        },
        expected_revision=ov["revision"],
    )
    tools.extensions.manager.test_server("badge-srv", expected_revision=srv["revision"])
    badge_tool = compute_model_tool_name("badge-srv", "render_badge")

    captured_badge_path: list[str] = []

    def inspect_step(model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        # Find the tool output from render_badge
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                data = json.loads(msg["content"])
                if data.get("model_tool_name") != badge_tool:
                    continue
                # Verify structuredContent small base64 image was stripped and forged keys did not overwrite top-level
                nested_img = data.get("structuredContent", {}).get("nested_preview", {})
                assert nested_img.get("data") is None
                assert nested_img.get("path")
                assert data.get("image_path") != "forged_evil.png"
                for block in data.get("content", []):
                    if isinstance(block, dict) and block.get("type") in ("image", "image_file") and block.get("path"):
                        captured_badge_path.append(block["path"])
                        return _assistant(
                            name="ryn_image_inspect",
                            args={"path": block["path"], "question": "What is in this badge?"},
                            call_id="call_inspect",
                        )
        raise AssertionError("Expected normalized image block in render_badge output")

    model.responses = [
        _assistant(
            name=READ_SKILL_FILE_TOOL,
            args={"skill_id": "release-auditor", "path": "references/checklist.md"},
            call_id="call_skill_file",
        ),
        _assistant(
            name="ryn_discover_tools",
            args={"load": [badge_tool, "ryn_image_inspect"]},
            call_id="call_discover_badge",
        ),
        _assistant(name=badge_tool, args={"tag": "v1.2"}, call_id="call_badge"),
        inspect_step,
        {"message": {"role": "assistant", "content": "1x1 pixel badge verified."}},
        _assistant(content="Completed skill file read and badge inspection."),
    ]

    # Create task using $release-auditor mention without explicit skill_ids
    created = runtime.create({"message": "Please follow $release-auditor and inspect the badge"})
    task_id = created["id"]
    assert any(s["id"] == "release-auditor" for s in created["skills"])

    # Run 1: model calls ryn_read_skill_file (pinned when skill selected) -> Run 2: executes ryn_read_skill_file
    runtime.run_once()
    runtime.run_once()
    skill_receipt = next(r for r in _receipts(runtime, task_id) if r["tool"] == READ_SKILL_FILE_TOOL)
    assert skill_receipt["status"] == "confirmed"
    assert "CHECKLIST_TOKEN_99" in skill_receipt["result"]["content"]
    assert "END_OF_CHECKLIST_MARKER" in skill_receipt["result"]["content"]
    assert len(skill_receipt["result"]["content"]) > 3800
    assert skill_receipt["result"]["truncated"] is False

    # Run 3: model calls ryn_discover_tools -> Run 4: loads badge_tool and ryn_image_inspect schemas
    runtime.run_once()
    runtime.run_once()

    # Run 5: model calls badge_tool -> Run 6: executes badge_tool and writes PNG to workspace
    runtime.run_once()
    runtime.run_once()
    # Run 7: model calls ryn_image_inspect on saved PNG -> Run 8: executes ryn_image_inspect (vision call)
    runtime.run_once()
    runtime.run_once()
    # Run 9: model finishes
    runtime.run_once()

    final_state = runtime.get(task_id)
    assert final_state["status"] == "completed"
    assert len(captured_badge_path) == 1
    assert (workspace / captured_badge_path[0]).is_file()

    # Check workspace_artifacts includes ONLY the verified MCP image file (no forged structuredContent entry)
    arts = runtime.artifacts(task_id)
    assert len(arts["files"]) == 1
    assert arts["files"][0]["path"] == captured_badge_path[0]
    assert arts["files"][0]["created_by_task"] is True


def test_post_send_rpc_error_and_inference_window_binding_change_regressions(ext_env, tmp_path: Path):
    runtime, model, tools, _, _ = ext_env
    script1 = tmp_path / "mcp_srv_1.py"
    counter1 = tmp_path / "counts_1.json"
    _write_fake_mcp_server(script1, counter1)

    script2 = tmp_path / "mcp_srv_2.py"
    counter2 = tmp_path / "counts_2.json"
    _write_fake_mcp_server(script2, counter2)

    ext_store = tools.extensions.store
    ov = ext_store.overview()
    srv = ext_store.upsert_mcp_server(
        {
            "id": "swap-srv",
            "name": "Swap Server",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-u", str(script1)],
            "enabled": True,
        },
        expected_revision=ov["revision"],
    )
    tools.extensions.manager.test_server("swap-srv", expected_revision=srv["revision"])

    crash_tool = compute_model_tool_name("swap-srv", "crash_after_dispatch")
    mutate_tool = compute_model_tool_name("swap-srv", "mutate_record")

    # 1. Post-send JSON-RPC -32602 error after write has already mutated remote state:
    # Must preserve ledger='dispatched', set task='interrupted' (agent_write_outcome_unknown),
    # reconcile to 'unknown', and NEVER re-execute or retry on subsequent ticks.
    runtime.update_settings(
        {
            "permission_mode": "automatic",
            "allowed_write_tools": [crash_tool, mutate_tool],
        }
    )
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [crash_tool]}, call_id="c_disc_rpc"),
        _assistant(name=crash_tool, args={"payload": "rpc_error_after_write"}, call_id="c_rpc_err"),
        _assistant(name=crash_tool, args={"payload": "rpc_error_after_write"}, call_id="c_rpc_retry"),
    ]
    t_rpc = runtime.create({"message": "Trigger post-send RPC -32602 error"})
    runtime.run_once()  # discover model step
    runtime.run_once()  # discover tool step
    runtime.run_once()  # crash_tool model step
    runtime.run_once()  # crash_tool tool step -> server bumps counter to 1 then returns -32602
    s_rpc = runtime.get(t_rpc["id"])
    assert s_rpc["status"] == "interrupted"
    assert s_rpc["error"] == "agent_write_outcome_unknown"
    assert s_rpc["reconciliation"]["state"] == "pending"

    # Run reconciliation + extra ticks -> state becomes unknown, count stays 1
    runtime.run_once()
    runtime.run_once()
    s_rpc_after = runtime.get(t_rpc["id"])
    assert s_rpc_after["reconciliation"]["state"] == "unknown"
    counts_after_rpc = json.loads(counter1.read_text(encoding="utf-8"))
    assert counts_after_rpc["crash_after_dispatch"] == 1

    # 2. Binding change DURING model.complete inference window (same server_id, tool name, and schema,
    # but server args updated to point at script2 while model.complete is running):
    # Pre-inference snapshot must bind the queued call to the old revision/config_generation,
    # and dispatch must reject with agent_tool_definition_changed and 0 writes on both servers.
    def mutate_config_during_inference(model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        cur = next(s for s in ext_store.overview()["mcp_servers"] if s["id"] == "swap-srv")
        updated = ext_store.upsert_mcp_server(
            {"args": ["-u", str(script2)]},
            server_id="swap-srv",
            expected_revision=cur["revision"],
        )
        tools.extensions.manager.invalidate_server("swap-srv")
        tools.extensions.manager.test_server("swap-srv", expected_revision=updated["revision"])
        return _assistant(name=mutate_tool, args={"value": "hijacked"}, call_id="c_hijack")

    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [mutate_tool]}, call_id="c_disc_swap"),
        mutate_config_during_inference,
        _assistant(content="Aborted after binding mismatch."),
    ]
    t_swap = runtime.create({"message": "Mutate while server config changes mid-inference"})
    runtime.run_once()  # discover model step
    runtime.run_once()  # discover tool step
    runtime.run_once()  # model step (mutates server config during inference)
    runtime.run_once()  # tool step -> must fail closed before dispatch
    swap_receipt = next(r for r in _receipts(runtime, t_swap["id"]) if r["tool"] == mutate_tool)
    assert swap_receipt["status"] == "not_executed"
    assert swap_receipt["result"]["not_executed"] is True
    assert swap_receipt["result"]["error"] == "agent_tool_definition_changed"
    assert json.loads(counter1.read_text(encoding="utf-8")).get("mutate_record", 0) == 0
    assert not counter2.exists()


def test_extensions_http_routes_end_to_end(tmp_path: Path):
    home = tmp_path / "http_node"
    workspace = tmp_path / "http_ws"
    home.mkdir()
    workspace.mkdir()
    script = tmp_path / "route_mcp.py"
    counter = tmp_path / "route_counts.json"
    _write_fake_mcp_server(script, counter)

    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    key = X25519PrivateKey.generate()
    agent_store = AgentStore(home / "agent", key)
    tools = _ExtensionHarnessTools(home, agent_store)
    model = _Model()
    runtime = install_agent(
        app,
        store=SimpleNamespace(home=home, peer_id="node_local"),
        home=home,
        workers=workers,
        local_control=lambda _req: None,
        messaging_key=key,
        integrations=(model, tools, _Catalog()),
    )
    runtime.update_settings({"enabled": True, "model_id": "local:test", "workspace_root": str(workspace)})

    with TestClient(app) as client:
        # 1. GET overview
        r = client.get("/api/local/agent/extensions")
        assert r.status_code == 200
        ov = r.json()
        assert any(p["id"] == COMPUTER_USE_PLUGIN_ID for p in ov["plugins"])
        assert any(s["id"] == COMPUTER_USE_SERVER_ID for s in ov["mcp_servers"])

        # 2. Stale revision on POST /mcp/servers -> 409
        r_conflict = client.post(
            "/api/local/agent/extensions/mcp/servers",
            json={
                "id": "route-srv",
                "name": "Route Server",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-u", str(script)],
                "expected_revision": ov["revision"] + 99,
            },
        )
        assert r_conflict.status_code == 409
        assert r_conflict.json()["detail"] == "agent_extensions_revision_conflict"

        # 3. Create MCP server
        r_create = client.post(
            "/api/local/agent/extensions/mcp/servers",
            json={
                "id": "route-srv",
                "name": "Route Server",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-u", str(script)],
                "env": {"SECRET_TOKEN": "canary-value-123"},
                "expected_revision": ov["revision"],
            },
        )
        assert r_create.status_code == 200
        srv = r_create.json()
        assert srv["id"] == "route-srv"
        assert "canary-value-123" not in json.dumps(srv)
        assert srv["masked_env"]["SECRET_TOKEN"]["has_value"] is True

        # 4. Test MCP server while disabled (allowed, remains disabled)
        r_test = client.post(
            "/api/local/agent/extensions/mcp/servers/route-srv/test",
            json={"expected_revision": srv["revision"]},
        )
        assert r_test.status_code == 200
        tested = r_test.json()
        assert tested["test_result"]["status"] == "connected"
        assert tested["server"]["enabled"] is False
        assert tested["server"]["tools_count"] == 4

        # 5. Patch MCP server to enable and set read_only_tools
        r_patch = client.patch(
            "/api/local/agent/extensions/mcp/servers/route-srv",
            json={
                "enabled": True,
                "read_only_tools": ["ping_info"],
                "expected_revision": tested["server"]["revision"],
            },
        )
        assert r_patch.status_code == 200
        patched = r_patch.json()
        assert patched["enabled"] is True
        assert patched["read_only_tools"] == ["ping_info"]

        # 6. Import skill via preview + commit
        r_prev = client.post(
            "/api/local/agent/extensions/import/preview",
            json={
                "kind": "skill",
                "filename": "SKILL.md",
                "skill_text": "---\nname: route-skill\ndescription: Route skill\n---\n# Route Skill\nGuidance.\n",
            },
        )
        assert r_prev.status_code == 200
        prev_data = r_prev.json()
        assert prev_data["can_import"] is True

        r_commit = client.post(
            "/api/local/agent/extensions/import/commit",
            json={
                "preview_id": prev_data["preview_id"],
                "expected_revision": prev_data["expected_revision"],
            },
        )
        assert r_commit.status_code == 200
        after_commit = r_commit.json()
        imported_skill = next(s for s in after_commit["skills"] if s["id"] == "route-skill")
        assert imported_skill["enabled"] is False

        # Enable skill via PATCH /skills/{id}
        r_skill = client.patch(
            "/api/local/agent/extensions/skills/route-skill",
            json={"enabled": True, "expected_revision": imported_skill["revision"]},
        )
        assert r_skill.status_code == 200
        assert r_skill.json()["enabled"] is True

        # Verify GET /api/local/agent/skills includes route-skill
        r_skills_list = client.get("/api/local/agent/skills")
        assert r_skills_list.status_code == 200
        assert any(s["id"] == "route-skill" for s in r_skills_list.json()["skills"])

        # Delete skill and MCP server
        r_del_skill = client.delete(
            f"/api/local/agent/extensions/skills/route-skill?expected_revision={r_skill.json()['revision']}"
        )
        assert r_del_skill.status_code == 200
        assert r_del_skill.json()["deleted"] is True

        cur_srv_rev = next(
            s["revision"]
            for s in client.get("/api/local/agent/extensions").json()["mcp_servers"]
            if s["id"] == "route-srv"
        )
        r_del_srv = client.delete(
            f"/api/local/agent/extensions/mcp/servers/route-srv?expected_revision={cur_srv_rev}"
        )
        assert r_del_srv.status_code == 200
        assert r_del_srv.json()["deleted"] is True

    tools.close()


def test_early_specs_before_install_agent_and_normal_order_key_binding_and_reconstruction(tmp_path: Path):
    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    def _make_node(root: Path):
        node = RynmeshStore(home=root / "node", network_dir=root / "network")
        app = FastAPI()
        key = peer_box.load_or_create_messaging_key(node.home / "messaging.x25519")
        install_llm_routes(
            app,
            store=node,
            home=node.home,
            messaging_key=key,
            resolve_endpoint=lambda _: "",
            resolve_pubkey=lambda _: "",
        )
        return app, node, key

    def _install(app, node, key, integrations=None):
        return install_agent(
            app,
            store=node,
            home=node.home,
            workers=app.state.background_workers,
            local_control=lambda _req: None,
            messaging_key=key,
            integrations=integrations,
        )

    def _close(app, runtime=None, tools=None):
        target_tools = runtime.tools if runtime is not None else tools
        if target_tools is not None:
            target_tools.extensions.close()
            target_tools.craft.close()
        app.state.llm_shutdown()

    # 1. Normal control: install_agent before specs, then fresh reconstruction
    app1, node1, key1 = _make_node(tmp_path / "normal")
    rt1 = None
    try:
        rt1 = _install(app1, node1, key1)
        assert rt1.tools.extensions.store.pub == rt1.store.pub
        assert not (rt1.tools.extensions.store.root / "messaging.key").exists()
        assert len(rt1.tools.specs()) > 0
        assert rt1.tools.extensions.store.overview()["revision"] >= 1
    finally:
        _close(app1, runtime=rt1)

    app1_r, node1_r, key1_r = _make_node(tmp_path / "normal")
    rt1_r = None
    try:
        rt1_r = _install(app1_r, node1_r, key1_r)
        assert rt1_r.tools.extensions.store.pub == rt1_r.store.pub
        assert app1_r.state.agent_startup_error is None
        client = TestClient(app1_r)
        try:
            resp = client.get("/api/local/agent/extensions")
            assert resp.status_code == 200
        finally:
            client.close()
    finally:
        _close(app1_r, runtime=rt1_r)

    # 2. Early RynTools.specs() BEFORE install_agent, then install_agent, then fresh reconstruction
    app2, node2, key2 = _make_node(tmp_path / "early_specs")
    integrations2 = build_agent_integrations(app2, node2, node2.home)
    tools2 = integrations2[1]
    rt2 = None
    try:
        assert not (node2.home / "agent" / "extensions.json").exists()
        specs = tools2.specs()
        assert len(specs) > 0
        before_store = tools2.extensions.store
        assert before_store.pub == peer_box.public_key_b64(key2)
        assert not (node2.home / "agent" / "messaging.key").exists()

        rt2 = _install(app2, node2, key2, integrations=integrations2)
        assert rt2.tools.extensions.store is before_store
        assert rt2.tools.extensions.store.pub == rt2.store.pub
    finally:
        _close(app2, runtime=rt2, tools=tools2)

    app2_r, node2_r, key2_r = _make_node(tmp_path / "early_specs")
    rt2_r = None
    try:
        rt2_r = _install(app2_r, node2_r, key2_r)
        assert rt2_r.tools.extensions.store.pub == rt2_r.store.pub
        assert app2_r.state.agent_startup_error is None
        assert len(rt2_r.tools.specs()) > 0
        assert rt2_r.skills()["skills"] is not None
        assert rt2_r.tools.extensions.store.overview()["revision"] >= 1
        client = TestClient(app2_r)
        try:
            resp = client.get("/api/local/agent/extensions")
            assert resp.status_code == 200
        finally:
            client.close()
        # Same-root bind_agent_store with a different authoritative key must rebind rather than early-returning on path only
        alt_key = X25519PrivateKey.generate()
        alt_store = SimpleNamespace(
            root=rt2_r.store.root,
            key=alt_key,
            pub=peer_box.public_key_b64(alt_key),
        )
        rt2_r.tools.extensions.bind_agent_store(alt_store)
        assert rt2_r.tools.extensions._store.pub == alt_store.pub
        # Accessing .store while local.store is still the original AgentStore automatically restores original authority
        assert rt2_r.tools.extensions.store.pub == rt2_r.store.pub
        # Wiring local.store to alt_store binds .store to alt_store.pub, and restoring local.store restores rt2_r.store.pub
        rt2_r.tools.local.store = alt_store
        assert rt2_r.tools.extensions.store.pub == alt_store.pub
        rt2_r.tools.local.store = rt2_r.store
        assert rt2_r.tools.extensions.store.pub == rt2_r.store.pub
    finally:
        _close(app2_r, runtime=rt2_r)


def test_mcp_write_result_is_error_preserves_unknown_and_blocks_model_retry(tmp_path: Path):
    from rynmesh.agent_runtime.store import AgentError, encoded
    from rynmesh.agent_runtime.tools import RynTools
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    worker_script = tmp_path / "iserror_worker.py"
    worker_script.write_text(
        textwrap.dedent(
            f"""\
            import json
            import os
            import pathlib
            import sys

            COUNTER = pathlib.Path(sys.argv[1])
            PNG_B64 = {_1X1_PNG_B64!r}

            def read_count():
                return json.loads(COUNTER.read_text(encoding="utf-8"))["count"] if COUNTER.exists() else 0

            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                req = json.loads(line)
                ident = req.get("id")
                if ident is None:
                    continue
                method = req.get("method")
                if method == "initialize":
                    val = {{
                        "protocolVersion": "2025-03-26",
                        "capabilities": {{"tools": {{}}}},
                        "serverInfo": {{"name": "durable-write-iserror-fixture", "version": "1"}},
                    }}
                elif method == "tools/list":
                    val = {{
                        "tools": [
                            {{
                                "name": "synthetic_write",
                                "description": "Write a synthetic counter and return isError=true",
                                "inputSchema": {{"type": "object", "properties": {{}}, "additionalProperties": False}},
                                "annotations": {{"readOnlyHint": False, "idempotentHint": False}},
                            }},
                            {{
                                "name": "synthetic_read_err",
                                "description": "Read-only tool returning isError=true",
                                "inputSchema": {{"type": "object", "properties": {{}}, "additionalProperties": False}},
                                "annotations": {{"readOnlyHint": True}},
                            }},
                        ]
                    }}
                elif method == "tools/call":
                    tname = req["params"]["name"]
                    if tname == "synthetic_read_err":
                        val = {{"isError": True, "content": [{{"type": "text", "text": "Read query failed."}}]}}
                    else:
                        count = read_count() + 1
                        COUNTER.parent.mkdir(parents=True, exist_ok=True)
                        with COUNTER.open("w", encoding="utf-8") as fh:
                            fh.write(json.dumps({{"count": count}}))
                            fh.flush()
                            os.fsync(fh.fileno())
                        val = {{
                            "isError": True,
                            "content": [
                                {{"type": "text", "text": "Synthetic application processing failed."}},
                                {{"type": "image", "mimeType": "image/png", "data": PNG_B64}},
                            ],
                        }}
                elif method == "ping":
                    val = {{}}
                else:
                    raise AssertionError("Unexpected MCP method: " + str(method))
                print(json.dumps({{"jsonrpc": "2.0", "id": ident, "result": val}}), flush=True)
            """
        ),
        encoding="utf-8",
    )

    def _read_count(p: Path) -> int:
        return json.loads(p.read_text(encoding="utf-8"))["count"] if p.exists() else 0

    class _FixtureModel:
        def __init__(self):
            self.name = None
            self.calls = []
            self.write_calls = 0

        def complete(self, model_id, messages, specs, cancelled, on_event=None, **kwargs):
            exposed = {row["function"]["name"] for row in specs}
            if self.name not in exposed:
                name, args = "ryn_discover_tools", {"load": [self.name]}
            else:
                self.write_calls += 1
                name, args = self.name, {}
            self.calls.append({"name": name, "write_call_index": self.write_calls})
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"fixture_new_call_{len(self.calls)}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }

    def _setup(sub_root: Path):
        node = RynmeshStore(home=sub_root / "node", network_dir=sub_root / "network")
        app = FastAPI()
        key = peer_box.load_or_create_messaging_key(node.home / "messaging.x25519")
        install_llm_routes(
            app,
            store=node,
            home=node.home,
            messaging_key=key,
            resolve_endpoint=lambda _: "",
            resolve_pubkey=lambda _: "",
        )
        catalog, model = _Catalog(), _FixtureModel()
        tools = RynTools(app, node, app.state.llm_agent_services, catalog)
        runtime = install_agent(
            app,
            store=node,
            home=node.home,
            workers=app.state.background_workers,
            local_control=lambda _req: None,
            messaging_key=key,
            integrations=(model, tools, catalog),
        )
        counter = sub_root / "synthetic_counter.json"
        ext = tools.extensions.store
        ext.upsert_mcp_server(
            {
                "id": "iserror-fixture",
                "enabled": True,
                "command": sys.executable,
                "args": ["-u", str(worker_script), str(counter)],
                "computer_access": False,
                "read_only_tools": ["synthetic_read_err"],
            },
            expected_revision=ext.overview()["revision"],
        )
        srv = ext.get_mcp_server_internal("iserror-fixture", require_enabled=True)
        discovered = tools.extensions.manager.test_server("iserror-fixture", expected_revision=srv["revision"])
        assert discovered["test_result"]["tools_count"] == 2 and _read_count(counter) == 0
        write_name = compute_model_tool_name("iserror-fixture", "synthetic_write")
        read_name = compute_model_tool_name("iserror-fixture", "synthetic_read_err")
        model.name = write_name
        ws = sub_root / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        runtime.update_settings(
            {
                "enabled": True,
                "model_id": "local:test",
                "workspace_root": str(ws),
                "permission_mode": "automatic",
                "allowed_write_tools": [write_name],
                "desktop_access": False,
            }
        )
        task = runtime.create({"message": "Record one synthetic fixture item."})
        return app, runtime, model, tools, task["id"], write_name, read_name, counter

    def _close(app, runtime, tools):
        runtime.update_settings({"enabled": False})
        tools.extensions.close()
        tools.craft.close()
        app.state.llm_shutdown()

    # 1. Direct adapter + encrypted OperationLedger: read isError returns normal error, write isError raises agent_write_outcome_unknown
    app1, rt1, _, tools1, tid1, write_name1, read_name1, counter1 = _setup(tmp_path / "direct")
    try:
        adapter = tools1.extensions
        # Read-only isError returns ordinary error payload without raising AgentError
        read_ctx = rt1._tool_context(rt1.store.read()["tasks"][tid1], "op-read-err")
        read_ctx["extension_binding"] = adapter.metadata(read_name1)["extension_binding"]
        read_res = adapter.execute(read_name1, {}, operation_id="op-read-err", context=read_ctx)
        assert read_res["is_error"] is True
        assert read_res["error"] == "agent_mcp_tool_returned_error"
        assert _read_count(counter1) == 0

        # Write isError: durable count=1, raises AgentError('agent_write_outcome_unknown'), ledger stays 'dispatched'
        op_id = "direct-iserror-fixture-operation"
        w_ctx = rt1._tool_context(rt1.store.read()["tasks"][tid1], op_id)
        w_ctx["extension_binding"] = adapter.metadata(write_name1)["extension_binding"]
        preview = adapter.preview(write_name1, {}, context=w_ctx)
        with pytest.raises(AgentError) as exc_info:
            adapter.execute(write_name1, {}, operation_id=op_id, context=w_ctx, expected_preview=preview)
        assert str(exc_info.value) == "agent_write_outcome_unknown"
        assert _read_count(counter1) == 1

        ledger_row = tools1.resource_writes.ledger.get(op_id)
        assert ledger_row["status"] == "dispatched"
        assert ledger_row["error"] == "agent_mcp_tool_returned_error"
        assert _1X1_PNG_B64 not in json.dumps(ledger_row)

        rec_ctx = rt1._tool_context(rt1.store.read()["tasks"][tid1], op_id)
        rec_ctx["operation"] = {
            "operation_id": op_id,
            "tool": write_name1,
            "arguments": {},
            "arguments_sha256": hashlib.sha256(encoded({})).hexdigest(),
            "extension_binding": adapter.metadata(write_name1)["extension_binding"],
        }
        rec = tools1.reconcile(op_id, context=rec_ctx, cancelled=lambda: False)
        assert rec["outcome"] == "unknown"
        assert rec["error"] == "agent_mcp_tool_returned_error"

        # Same operation_id retry must NOT re-execute the remote write (count stays 1)
        with pytest.raises(AgentError) as exc_retry:
            adapter.execute(write_name1, {}, operation_id=op_id, context=w_ctx, expected_preview=preview)
        assert str(exc_retry.value) == "agent_write_outcome_unknown"
        assert _read_count(counter1) == 1
    finally:
        _close(app1, rt1, tools1)

    # 2. Full runtime + model new-call retry across ticks, reconciliation, and fresh reconstruction
    app2, rt2, model2, tools2, tid2, write_name2, _, counter2 = _setup(tmp_path / "runtime")
    try:
        for _ in range(10):
            rt2.run_once()
            row = rt2.get(tid2)
            if row["status"] in {"failed", "interrupted", "cancelled", "completed", "waiting_approval"}:
                break
        assert _read_count(counter2) == 1
        assert model2.write_calls == 1
        state2 = rt2.get(tid2)
        assert state2["status"] == "interrupted"
        assert state2["error"] == "agent_write_outcome_unknown"

        # Run reconciliation tick -> state becomes unknown, count remains 1, model never called a second time
        rt2.run_once()
        rt2.run_once()
        state2_after = rt2.get(tid2)
        assert state2_after["reconciliation"]["state"] == "unknown"
        assert [r["status"] for r in _receipts(rt2, tid2) if r["tool"] == write_name2] == ["unknown"]
        assert _read_count(counter2) == 1
        assert model2.write_calls == 1
    finally:
        _close(app2, rt2, tools2)

    # 3. Fresh node/app/tools/runtime reconstruction on the same home preserves count=1 and unknown reconciliation
    sub_root = tmp_path / "runtime"
    node2_r = RynmeshStore(home=sub_root / "node", network_dir=sub_root / "network")
    app2_r = FastAPI()
    key2_r = peer_box.load_or_create_messaging_key(node2_r.home / "messaging.x25519")
    install_llm_routes(
        app2_r,
        store=node2_r,
        home=node2_r.home,
        messaging_key=key2_r,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    catalog2_r, model2_r = _Catalog(), _FixtureModel()
    model2_r.name = write_name2
    tools2_r = RynTools(app2_r, node2_r, app2_r.state.llm_agent_services, catalog2_r)
    rt2_r = install_agent(
        app2_r,
        store=node2_r,
        home=node2_r.home,
        workers=app2_r.state.background_workers,
        local_control=lambda _req: None,
        messaging_key=key2_r,
        integrations=(model2_r, tools2_r, catalog2_r),
    )
    try:
        rt2_r.run_once()
        reconstructed_state = rt2_r.get(tid2)
        assert reconstructed_state["status"] == "interrupted"
        assert reconstructed_state["error"] == "agent_write_outcome_unknown"
        assert reconstructed_state["reconciliation"]["state"] == "unknown"
        assert [r["status"] for r in _receipts(rt2_r, tid2) if r["tool"] == write_name2] == ["unknown"]
        assert _read_count(counter2) == 1
        assert model2_r.write_calls == 0
    finally:
        _close(app2_r, rt2_r, tools2_r)


def test_post_dispatch_write_diagnostics_distinct_and_sanitized_without_replay(tmp_path: Path):
    from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
    from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager
    from rynmesh.agent_runtime.extensions.store import ExtensionStore
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    from rynmesh.agent_runtime.store import AgentError, encoded
    from rynmesh.services import peer_box

    canary = "SYNTHETIC_SECRET_CANARY_9f7e1c"
    fixture_script = tmp_path / "diag_provider.py"
    fixture_script.write_text(
        """\
import json
import os
import sys
import time
from pathlib import Path

log_dir = Path(sys.argv[1])
mode = sys.argv[2]
canary = sys.argv[3]
log_dir.mkdir(parents=True, exist_ok=True)
pid = os.getpid()

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    req = json.loads(line)
    ident = req.get("id")
    method = req.get("method")
    if ident is None:
        continue
    if method == "initialize":
        res = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "Diag", "version": "1"}}
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}), flush=True)
    elif method == "tools/call":
        name = req.get("params", {}).get("name")
        with (log_dir / "calls.jsonl").open("a", encoding="utf-8") as fh:
            print(json.dumps({"pid": pid, "id": ident, "name": name}), file=fh, flush=True)
        if name == "computer_act_window":
            print("stderr leak " + canary, file=sys.stderr, flush=True)
            if mode == "exit":
                os._exit(19)
            if mode == "cancel":
                (log_dir / "dispatched.flag").write_text("1", encoding="utf-8")
                time.sleep(5.0)
            err_map = {
                "blocked": "agent_computer_focus_blocked",
                "changed": "agent_computer_window_changed_read_again",
                "poisoned": "agent_computer_focus_blocked: Authorization: Bearer " + canary,
                "unlisted_agent": "agent_secret_" + canary.lower(),
            }
            if mode in err_map:
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32602, "message": err_map[mode], "data": {"secret": canary}},
                }), flush=True)
                continue
        res = {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"ok": True, "pid": pid}, "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}), flush=True)
""",
        encoding="utf-8",
    )

    expected_codes = {
        "blocked": "agent_computer_focus_blocked",
        "changed": "agent_computer_window_changed_read_again",
        "poisoned": "agent_mcp_rpc_rejected",
        "unlisted_agent": "agent_mcp_rpc_rejected",
        "exit": "agent_mcp_process_disconnected",
        "cancel": "agent_cancelled",
    }

    for mode, expected_code in expected_codes.items():
        case_root = tmp_path / f"case_{mode}"
        case_root.mkdir(parents=True, exist_ok=True)
        key = peer_box.load_or_create_messaging_key(case_root / "messaging.x25519")
        ext = ExtensionStore(case_root / "agent", key)
        workspace = case_root / "workspace"
        workspace.mkdir()
        log_dir = case_root / "logs"
        srv_created = ext.upsert_mcp_server(
            {
                "id": "diag-computer",
                "name": "Diag Computer Server",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-u", str(fixture_script), str(log_dir), mode, canary],
                "enabled": True,
                "computer_access": True,
            }
        )
        ext.record_mcp_discovery(
            "diag-computer",
            expected_revision=srv_created["revision"],
            expected_config_generation=srv_created["config_generation"],
            expected_instance_id=srv_created["instance_id"],
            status="connected",
            discovered_tools=[
                {
                    "name": "computer_act_window",
                    "title": "Act Window",
                    "description": "Perform action on window",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "hwnd": {"type": "integer"},
                            "action": {"type": "string"},
                        },
                        "required": ["hwnd", "action"],
                        "additionalProperties": False,
                    },
                    "annotations": {"readOnlyHint": False},
                }
            ],
        )

        class _Ctrl:
            def read_control(self, task_id):
                return {
                    "settings": {"enabled": True, "desktop_access": True},
                    "tasks": {
                        task_id: {
                            "local_execution": {"desktop_access": True},
                            "control_revision": 1,
                        }
                    },
                }

        class _Local:
            store = _Ctrl()

            def policy(self, context):
                return {"desktop_access": True, "workspace_root": str(workspace)}  # noqa: B023 - this callback finishes within the current case.

        local = _Local()
        ledger = OperationLedger(case_root)
        spawned: list = []

        def _make_adapter(local=local) -> tuple[ExtensionToolAdapter, McpSessionManager]:
            mgr = McpSessionManager(ext)  # noqa: B023 - this callback finishes within the current case.
            orig_create = mgr._create_session

            def _create(server, **kwargs):
                srv_copy = dict(server)
                srv_copy["command"] = sys.executable
                srv_copy["args"] = ["-u", str(fixture_script), str(log_dir), mode, canary]  # noqa: B023 - this callback finishes within the current case.
                sess = orig_create(srv_copy, **kwargs)
                spawned.append(sess)  # noqa: B023 - this callback finishes within the current case.
                return sess

            mgr._create_session = _create
            return (
                ExtensionToolAdapter(local, lambda: ledger, extension_store=ext, session_manager=mgr),  # noqa: B023 - this callback finishes within the current case.
                mgr,
            )

        name_act = compute_model_tool_name("diag-computer", "computer_act_window")
        adapter, mgr = _make_adapter()
        try:
            ctx = {
                "task_id": "task-diag-1",
                "local_execution": {"desktop_access": True, "workspace_root": str(workspace)},
                "extension_binding": adapter.metadata(name_act)["extension_binding"],
            }
            args = {"hwnd": 65537, "action": "focus"}
            preview = adapter.preview(name_act, args, context=ctx)
            op_id = f"op-diag-{mode}"
            cancel_fn = (
                (lambda d=log_dir: (d / "dispatched.flag").exists())
                if mode == "cancel"
                else (lambda: False)
            )

            with pytest.raises(AgentError) as exc_info:
                adapter.execute(
                    name_act,
                    args,
                    operation_id=op_id,
                    context=ctx,
                    expected_preview=preview,
                    cancelled=cancel_fn,
                )
            assert str(exc_info.value) == "agent_write_outcome_unknown"

            row = ledger.get(op_id)
            assert row is not None
            assert row["status"] == "dispatched"
            assert row["error"] == expected_code
            diag = row["error_result"]
            assert isinstance(diag, dict)
            assert diag["error"] == expected_code
            assert diag["exception_type"] == "McpTransportError"
            assert diag["phase"] == "post_dispatch"
            assert diag["stage"] == ("await_response" if mode in {"exit", "cancel"} else "rpc_error")
            assert diag["operation_id"] == op_id
            assert diag["session_scope"] == "task-diag-1"
            assert isinstance(diag.get("request_id"), int)
            assert isinstance(diag.get("session_pid"), int)
            raw_row = json.dumps(row, ensure_ascii=False)
            assert canary not in raw_row
            assert canary.lower() not in raw_row
            assert "Authorization:" not in raw_row

            rec = adapter.reconcile(
                op_id,
                {
                    **ctx,
                    "operation": {
                        "tool": name_act,
                        "arguments_sha256": hashlib.sha256(encoded(args)).hexdigest(),
                        "extension_binding": ctx["extension_binding"],
                    },
                },
            )
            assert rec["outcome"] == "unknown"
            assert rec["error"] == expected_code
            assert rec["diagnostic"]["error"] == expected_code

            # Duplicate call and rebuilt adapter call must not replay tools/call
            with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
                adapter.execute(
                    name_act,
                    args,
                    operation_id=op_id,
                    context=ctx,
                    expected_preview=preview,
                    cancelled=lambda: False,
                )
            adapter.close()
            mgr.close()

            adapter2, mgr2 = _make_adapter()
            try:
                with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
                    adapter2.execute(
                        name_act,
                        args,
                        operation_id=op_id,
                        context=ctx,
                        expected_preview=preview,
                        cancelled=lambda: False,
                    )
            finally:
                adapter2.close()
                mgr2.close()

            calls_lines = (log_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
            act_calls = [json.loads(line) for line in calls_lines if json.loads(line)["name"] == "computer_act_window"]
            assert len(act_calls) == 1
            assert len(spawned) == 1
        finally:
            adapter.close()
            mgr.close()
            for s in spawned:
                s.close()
