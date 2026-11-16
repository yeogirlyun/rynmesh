"""Overhaul S08-I42: server_hint_read MCP tools — parallel in automatic mode, non-interrupting unknown outcome on transport error, approval preserved in confirm_changes."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from rynmesh.agent_runtime.extensions import compute_model_tool_name
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.tool_dispatch import supports_parallel
from test_agent_extensions_runtime import _assistant, _Catalog, _ExtensionHarnessTools, _Model
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


class _McpHttpFixture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tool_calls: list[tuple[str, dict]] = []
        self.parallel_barrier = threading.Barrier(2, timeout=3.0)
        self.active_calls = 0
        self.peak_concurrent_calls = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                msg = json.loads(raw.decode("utf-8"))
                method = msg.get("method")
                msg_id = msg.get("id")

                if method == "notifications/initialized" or msg_id is None:
                    self.send_response(202)
                    self.end_headers()
                    return

                if method == "initialize":
                    body = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "s08-i42-http", "version": "1.0.0"},
                        },
                    }
                    self._send_json(body)
                    return

                if method == "tools/list":
                    body = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "tools": [
                                {
                                    "name": "query_orders",
                                    "title": "Query orders",
                                    "description": "Read-only order query hinted by server.",
                                    "annotations": {"readOnlyHint": True, "destructiveHint": False},
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "mode": {"type": "string"},
                                            "slot": {"type": "string"},
                                        },
                                        "required": ["mode"],
                                        "additionalProperties": False,
                                    },
                                },
                                {
                                    "name": "purge_cache",
                                    "title": "Purge cache",
                                    "description": "Destructive tool even if readOnlyHint is true.",
                                    "annotations": {"readOnlyHint": True, "destructiveHint": True},
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"mode": {"type": "string"}},
                                        "required": ["mode"],
                                        "additionalProperties": False,
                                    },
                                },
                                {
                                    "name": "owner_forced_write",
                                    "title": "Owner forced write",
                                    "description": "Hinted read-only by server, but marked write by owner.",
                                    "annotations": {"readOnlyHint": True, "destructiveHint": False},
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"mode": {"type": "string"}},
                                        "required": ["mode"],
                                        "additionalProperties": False,
                                    },
                                },
                            ]
                        },
                    }
                    self._send_json(body)
                    return

                if method == "tools/call":
                    params = msg.get("params") or {}
                    t_name = str(params.get("name") or "")
                    t_args = dict(params.get("arguments") or {})
                    with fixture.lock:
                        fixture.tool_calls.append((t_name, t_args))
                        fixture.active_calls += 1
                        fixture.peak_concurrent_calls = max(
                            fixture.peak_concurrent_calls, fixture.active_calls
                        )
                    try:
                        if t_args.get("mode") == "timeout":
                            time.sleep(0.7)
                            return
                        if t_args.get("mode") == "parallel":
                            fixture.parallel_barrier.wait()
                        body = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {"tool": t_name, "slot": t_args.get("slot", "ok")},
                                            ensure_ascii=False,
                                        ),
                                    }
                                ],
                                "isError": False,
                            },
                        }
                        self._send_json(body)
                    except BrokenPipeError:
                        return
                    except ConnectionAbortedError:
                        return
                    finally:
                        with fixture.lock:
                            fixture.active_calls -= 1
                    return

                self._send_json({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "not found"}})

            def _send_json(self, obj: dict) -> None:
                payload = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def mcp_http_env(tmp_path: Path):
    fixture = _McpHttpFixture()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    key = X25519PrivateKey.generate()
    store = AgentStore(home / "agent", key)
    tools = _ExtensionHarnessTools(home, store)
    model = _Model()
    runtime = AgentRuntime(store, model, tools, _Catalog())
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "workspace_root": str(workspace),
            "permission_mode": "automatic",
        }
    )

    ov = tools.extensions.store.overview()
    srv = tools.extensions.store.upsert_mcp_server(
        {
            "id": "hint-srv",
            "name": "Hint Server",
            "transport": "streamable_http",
            "url": fixture.url,
            "enabled": True,
            "tool_timeout_ms": 250,
            "startup_timeout_ms": 3000,
        },
        expected_revision=ov["revision"],
    )
    tested = tools.extensions.manager.test_server("hint-srv", expected_revision=srv["revision"])
    # Mark owner_forced_write in owner_write_tools to verify Step 1 owner_write_tools exclusion:
    ext_state = tools.extensions.store._read()
    ext_state["mcp_servers"]["hint-srv"]["owner_write_tools"] = ["owner_forced_write"]
    tools.extensions.store._write(ext_state)
    tools.extensions._active_mcp_tools_cache = None

    assert tested["server"]["status"] in {"ready", "Ready", "connected"}
    try:
        yield runtime, model, tools, fixture
    finally:
        tools.close()
        fixture.close()


def _run_until(runtime: AgentRuntime, task_id: str, targets: set[str], max_ticks: int = 20) -> dict:
    for _ in range(max_ticks):
        state = runtime.get(task_id)
        if state["status"] in targets:
            return state
        runtime.run_once()
    return runtime.get(task_id)


def test_t1_readonly_hint_unmarked_tool_timeout_returns_unknown_without_interrupting_and_keeps_dispatched_ledger(mcp_http_env):
    """T1: readOnlyHint=true 的未标记工具，HTTP 超时后结果 outcome=unknown、任务不 interrupted、台账为 dispatched（覆盖改动 1、2、4、5）。"""
    runtime, model, tools, fixture = mcp_http_env
    query_tool = compute_model_tool_name("hint-srv", "query_orders")
    owner_write_tool = compute_model_tool_name("hint-srv", "owner_forced_write")

    # Step 1 & Step 2: check server_hint_read in binding and metadata, and owner_write_tools exclusion:
    q_meta = tools.metadata(query_tool)
    assert q_meta["effect"] == "write"
    assert q_meta["server_hint_read"] is True
    assert q_meta["extension_binding"]["server_hint_read"] is True

    ow_meta = tools.metadata(owner_write_tool)
    assert ow_meta["effect"] == "write"
    assert ow_meta["server_hint_read"] is False
    assert ow_meta["extension_binding"]["server_hint_read"] is False

    # Test under confirm_changes (approved) as well as automatic mode (Step 5 applies to all modes!):
    for mode in ("automatic", "confirm_changes"):
        runtime.update_settings({"permission_mode": mode})
        model.responses = [
            _assistant(name="ryn_discover_tools", args={"load": [query_tool]}, call_id=f"disc_{mode}"),
            _assistant(name=query_tool, args={"mode": "timeout"}, call_id=f"call_to_{mode}"),
            _assistant(content=f"Handled unknown in {mode}."),
        ]
        created = runtime.create({"message": f"Query orders ({mode})"})
        tid = created["id"]
        if mode == "confirm_changes":
            waiting = _run_until(runtime, tid, {"waiting_approval", "interrupted", "failed"})
            assert waiting["status"] == "waiting_approval"
            runtime.action(
                tid,
                {
                    "action": "approve",
                    "approval_id": waiting["pending_approval"]["id"],
                    "expected_control_revision": waiting["control_revision"],
                },
            )
        final_state = _run_until(runtime, tid, {"completed", "interrupted", "failed"})
        assert final_state["status"] == "completed", f"task unexpectedly stopped in {mode}: {final_state}"

        stored = runtime.store.read()["tasks"][tid]
        tool_msgs = [
            json.loads(m["content"])
            for m in stored["messages"]
            if m.get("role") == "tool" and m.get("tool_call_id") == f"call_to_{mode}"
        ]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["outcome"] == "unknown"
        assert tool_msgs[0].get("hint") == "先用只读工具核对再决定是否重试"

        # Verify OperationLedger stayed 'dispatched' (never rewritten to 'not_executed'):
        unknown_receipt = next(r for r in stored["receipts"] if r["tool"] == query_tool)
        ledger_entry = tools.ledger.get(unknown_receipt["operation_id"])
        assert ledger_entry is not None
        assert ledger_entry["status"] == "dispatched"


def test_t2_destructive_hint_tool_retains_original_interrupted_flow(mcp_http_env):
    """T2: destructiveHint=true 的工具仍走原中断流程（覆盖改动 1、2、4、5）。"""
    runtime, model, tools, _ = mcp_http_env
    purge_tool = compute_model_tool_name("hint-srv", "purge_cache")

    meta = tools.metadata(purge_tool)
    assert meta["effect"] == "write"
    assert meta["server_hint_read"] is False
    assert meta["extension_binding"]["server_hint_read"] is False
    assert supports_parallel(purge_tool, meta, permission_mode="automatic") is False

    runtime.update_settings({"permission_mode": "confirm_changes"})
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [purge_tool]}, call_id="disc_purge"),
        _assistant(name=purge_tool, args={"mode": "timeout"}, call_id="call_purge"),
    ]
    created = runtime.create({"message": "Purge cache"})
    tid = created["id"]
    waiting = _run_until(runtime, tid, {"waiting_approval", "interrupted", "failed"})
    assert waiting["status"] == "waiting_approval"
    runtime.action(
        tid,
        {
            "action": "approve",
            "approval_id": waiting["pending_approval"]["id"],
            "expected_control_revision": waiting["control_revision"],
        },
    )
    runtime.run_once()
    interrupted = runtime.get(tid)
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == "agent_write_outcome_unknown"
    assert interrupted["reconciliation"]["state"] == "pending"

    # Also verify direct adapter call raises agent_write_outcome_unknown (not agent_mcp_outcome_unknown_reported):
    ctx = runtime._execution_context(runtime.store.read()["tasks"][tid], "op_direct_purge")
    ctx["extension_binding"] = meta["extension_binding"]
    prev = tools.preview(purge_tool, {"mode": "timeout"}, context=ctx)
    with pytest.raises(AgentError) as exc_info:
        tools.execute(
            purge_tool,
            {"mode": "timeout"},
            operation_id="op_direct_purge",
            context=ctx,
            expected_preview=prev,
            cancelled=lambda: False,
        )
    assert str(exc_info.value) == "agent_write_outcome_unknown"


def test_t3_automatic_mode_runs_two_server_hint_read_calls_in_parallel(mcp_http_env):
    """T3: automatic 档两个 hint 只读调用并行（覆盖改动 2、3）。"""
    runtime, model, tools, fixture = mcp_http_env
    ov = tools.extensions.store.overview()
    srv_b = tools.extensions.store.upsert_mcp_server(
        {
            "id": "hint-srv-b",
            "name": "Hint Server B",
            "transport": "streamable_http",
            "url": fixture.url,
            "enabled": True,
            "tool_timeout_ms": 2000,
            "startup_timeout_ms": 3000,
        },
        expected_revision=ov["revision"],
    )
    tools.extensions.manager.test_server("hint-srv-b", expected_revision=srv_b["revision"])
    ext_state = tools.extensions.store._read()
    ext_state["mcp_servers"]["hint-srv"]["tool_timeout_ms"] = 2000
    tools.extensions.store._write(ext_state)
    tools.extensions._active_mcp_tools_cache = None

    query_tool_a = compute_model_tool_name("hint-srv", "query_orders")
    query_tool_b = compute_model_tool_name("hint-srv-b", "query_orders")
    meta_a = tools.metadata(query_tool_a)
    meta_b = tools.metadata(query_tool_b)

    assert supports_parallel(query_tool_a, meta_a) is False
    assert supports_parallel(query_tool_a, meta_a, permission_mode="confirm_changes") is False
    assert supports_parallel(query_tool_a, meta_a, permission_mode="workspace") is False
    assert supports_parallel(query_tool_a, meta_a, permission_mode="automatic") is True
    assert supports_parallel(query_tool_b, meta_b, permission_mode="automatic") is True

    runtime.update_settings({"permission_mode": "automatic"})
    two_calls_msg = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_par_1",
                    "type": "function",
                    "function": {
                        "name": query_tool_a,
                        "arguments": json.dumps({"mode": "parallel", "slot": "alpha"}),
                    },
                },
                {
                    "id": "call_par_2",
                    "type": "function",
                    "function": {
                        "name": query_tool_b,
                        "arguments": json.dumps({"mode": "parallel", "slot": "beta"}),
                    },
                },
            ],
        }
    }
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [query_tool_a, query_tool_b]}, call_id="disc_par"),
        two_calls_msg,
        _assistant(content="Both parallel queries completed."),
    ]
    created = runtime.create({"message": "Run two hinted queries concurrently"})
    done = _run_until(runtime, created["id"], {"completed", "interrupted", "failed"})
    assert done["status"] == "completed", done
    assert fixture.peak_concurrent_calls >= 2

    stored = runtime.store.read()["tasks"][created["id"]]
    par_msgs = [
        m for m in stored["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") in {"call_par_1", "call_par_2"}
    ]
    assert [m["tool_call_id"] for m in par_msgs] == ["call_par_1", "call_par_2"]
    assert "alpha" in par_msgs[0]["content"]
    assert "beta" in par_msgs[1]["content"]
    confirmed = [
        r for r in stored["receipts"]
        if r["tool"] in {query_tool_a, query_tool_b} and r["status"] == "confirmed"
    ]
    assert len(confirmed) == 2


def test_t4_confirm_changes_mode_still_requires_owner_approval_for_server_hint_read(mcp_http_env):
    """T4: confirm_changes 档仍需审批（覆盖改动 1、2）。"""
    runtime, model, tools, fixture = mcp_http_env
    query_tool = compute_model_tool_name("hint-srv", "query_orders")
    fixture.tool_calls.clear()

    runtime.update_settings({"permission_mode": "confirm_changes"})
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [query_tool]}, call_id="disc_confirm"),
        _assistant(name=query_tool, args={"mode": "fast", "slot": "approved"}, call_id="call_confirm"),
        _assistant(content="Approved query done."),
    ]
    created = runtime.create({"message": "Query under confirm_changes"})
    waiting = _run_until(runtime, created["id"], {"waiting_approval", "completed", "failed"})
    assert waiting["status"] == "waiting_approval"
    assert waiting["pending_approval"]["tool"] == query_tool
    # No tools/call sent to server before owner approves:
    assert fixture.tool_calls == []

    runtime.action(
        created["id"],
        {
            "action": "approve",
            "approval_id": waiting["pending_approval"]["id"],
            "expected_control_revision": waiting["control_revision"],
        },
    )
    done = _run_until(runtime, created["id"], {"completed", "interrupted", "failed"})
    assert done["status"] == "completed"
    assert len(fixture.tool_calls) == 1
