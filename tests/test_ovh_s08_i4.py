"""Tests for S08-I4: stdio MCP session idle timeout, 24h max lifetime, and per-method line limits."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from rynmesh.agent_runtime.extensions import ExtensionStore, mcp_transport
from rynmesh.agent_runtime.extensions.mcp_transport import (
    McpSessionManager,
    McpTransportError,
    StdioMcpSession,
)
from rynmesh.llm_package import process_guard
from rynmesh.services import peer_box


def _write_stdio_fixture_server(script_path: Path) -> None:
    script_path.write_text(
        """import json, os, pathlib, sys, time

ctrl_dir = pathlib.Path(sys.argv[1])
ctrl_dir.mkdir(parents=True, exist_ok=True)
pid = os.getpid()

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    req = json.loads(line)
    ident = req.get("id")
    if ident is None:
        continue
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        res = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "s08-i4-fixture", "version": "1.0"},
        }
        payload = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}) + "\\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
    elif method == "tools/list":
        size_file = ctrl_dir / "list_bytes.txt"
        pad_bytes = int(size_file.read_text(encoding="utf-8").strip()) if size_file.exists() else 0
        desc = "d" * pad_bytes if pad_bytes > 0 else "echo tool"
        res = {
            "tools": [
                {
                    "name": "echo",
                    "description": desc,
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
        payload = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}, separators=(",", ":")) + "\\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
    elif method == "tools/call":
        args = params.get("arguments") or {}
        delay = float(args.get("sleep_s") or 0.0)
        if delay > 0:
            time.sleep(delay)
        pad_bytes = int(args.get("pad_bytes") or 0)
        if pad_bytes > 0:
            prefix = (
                '{"jsonrpc":"2.0","id":'
                + str(ident)
                + ',"result":{"content":[{"type":"text","text":"'
            ).encode("utf-8")
            suffix = (
                '"}],"structuredContent":{"pid":'
                + str(pid)
                + ',"size":'
                + str(pad_bytes)
                + "}}}\\n"
            ).encode("utf-8")
            sys.stdout.buffer.write(prefix + (b"x" * pad_bytes) + suffix)
            sys.stdout.buffer.flush()
        else:
            res = {
                "content": [{"type": "text", "text": json.dumps({"pid": pid, "tag": args.get("tag", "")})}],
                "structuredContent": {"pid": pid, "tag": args.get("tag", "")},
            }
            payload = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}, separators=(",", ":")) + "\\n"
            sys.stdout.buffer.write(payload.encode("utf-8"))
            sys.stdout.buffer.flush()
    else:
        payload = json.dumps({"jsonrpc": "2.0", "id": ident, "result": {}}) + "\\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )


def _setup_manager_and_server(
    tmp_path: Path,
    server_id: str,
    script_path: Path,
    ctrl_dir: Path,
) -> tuple[McpSessionManager, dict]:
    key = peer_box.load_or_create_messaging_key(tmp_path / f"{server_id}.key")
    ext_store = ExtensionStore(tmp_path / f"store-{server_id}", key)
    ov = ext_store.overview()
    ext_store.upsert_mcp_server(
        {
            "id": server_id,
            "name": server_id,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-u", str(script_path), str(ctrl_dir)],
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        },
        expected_revision=ov["revision"],
    )
    info = ext_store.get_mcp_server_internal(server_id, require_enabled=True)
    ext_store.record_mcp_discovery(
        server_id,
        expected_revision=info["revision"],
        expected_config_generation=info["config_generation"],
        expected_instance_id=info["instance_id"],
        status="ready",
        discovered_tools=[
            {
                "name": "echo",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
    )
    srv = ext_store.get_mcp_server_internal(server_id, require_enabled=True)
    return McpSessionManager(ext_store), srv


def test_t1_stdio_idle_deadline_renews_across_calls_and_expires_when_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(mcp_transport, "SESSION_IDLE_SECONDS")
    assert hasattr(mcp_transport, "SESSION_MAX_SECONDS")
    assert mcp_transport.SESSION_IDLE_SECONDS == 1800.0
    assert mcp_transport.SESSION_MAX_SECONDS == 86400.0

    monkeypatch.setattr(mcp_transport, "SESSION_IDLE_SECONDS", 0.5)

    spawn_timeouts: list[float] = []
    real_spawn = process_guard.spawn

    def spy_spawn(args: list[str], *, cwd: str, env: dict[str, str], timeout: float):
        spawn_timeouts.append(timeout)
        return real_spawn(args, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(process_guard, "spawn", spy_spawn)

    script = tmp_path / "srv_t1.py"
    ctrl = tmp_path / "ctrl_t1"
    _write_stdio_fixture_server(script)
    manager, srv = _setup_manager_and_server(tmp_path, "srv-t1", script, ctrl)

    try:
        pids: list[int] = []
        for idx in range(4):
            if idx > 0:
                time.sleep(0.3)
            res = manager.call_tool(
                "srv-t1",
                "echo",
                {"tag": f"call-{idx}"},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
                session_scope="task-t1",
            )
            pids.append(int(res["structuredContent"]["pid"]))
            sess = manager._sessions[("srv-t1", "task-t1")]
            assert sess.is_usable() is True

        # 3 intervals * 0.3s = 0.9s > 0.5s idle window, yet session never restarted
        assert len(set(pids)) == 1
        assert spawn_timeouts == [mcp_transport.SESSION_MAX_SECONDS]

        first_sess = manager._sessions[("srv-t1", "task-t1")]
        first_pid = pids[0]

        # Wait longer than idle (0.5s): session becomes unusable and next call spawns a fresh process
        time.sleep(0.65)
        assert first_sess.is_usable() is False

        res_after_idle = manager.call_tool(
            "srv-t1",
            "echo",
            {"tag": "after-idle"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-t1",
        )
        assert int(res_after_idle["structuredContent"]["pid"]) != first_pid
        assert len(spawn_timeouts) == 2
    finally:
        manager.close()


def test_t2_inflight_stdio_call_exceeding_idle_timeout_still_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_transport, "SESSION_IDLE_SECONDS", 0.4, raising=False)

    script = tmp_path / "srv_t2.py"
    ctrl = tmp_path / "ctrl_t2"
    _write_stdio_fixture_server(script)

    sess = StdioMcpSession(
        {
            "id": "srv-t2",
            "command": sys.executable,
            "args": ["-u", str(script), str(ctrl)],
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        },
        fallback_cwd=tmp_path,
    )
    try:
        sess.initialize()
        init_res = sess.call_tool("echo", {"tag": "warmup"})
        worker_pid_before = int(init_res["structuredContent"]["pid"])
        proc_pid_before = sess.process.pid
        sess.deadline = time.monotonic() + 0.4

        # In-flight call takes 0.9s (> 2x 0.4s idle window) and must still complete without termination
        res = sess.call_tool("echo", {"sleep_s": 0.9, "tag": "slow-ok"})
        assert res["structuredContent"]["tag"] == "slow-ok"
        assert int(res["structuredContent"]["pid"]) == worker_pid_before
        assert sess.process.pid == proc_pid_before
        assert sess.is_usable() is True

        # Follow-up call reuses the exact same process
        res2 = sess.call_tool("echo", {"tag": "followup"})
        assert int(res2["structuredContent"]["pid"]) == worker_pid_before
        assert sess.process.pid == proc_pid_before
    finally:
        sess.close()


def test_t3_per_line_limits_5mib_readable_9mib_rejected_and_method_scoped_isolation(
    tmp_path: Path,
) -> None:
    script = tmp_path / "srv_t3.py"
    ctrl = tmp_path / "ctrl_t3"
    _write_stdio_fixture_server(script)
    manager, srv = _setup_manager_and_server(tmp_path, "srv-t3", script, ctrl)

    mib = 1024 * 1024
    sess = StdioMcpSession(
        {
            "id": "srv-t3-direct",
            "command": sys.executable,
            "args": ["-u", str(script), str(ctrl)],
            "startup_timeout_ms": 8000,
            "tool_timeout_ms": 10000,
        },
        fallback_cwd=tmp_path,
    )
    try:
        sess.initialize()
        orig_pid = sess.process.pid

        # 1. Non-tools/call method (tools/list, 8 MiB limit): 5 MiB line is readable, 9 MiB line fails only that request
        (ctrl / "list_bytes.txt").write_text(str(5 * mib), encoding="utf-8")
        tools_5m = sess.list_all_tools()
        assert len(tools_5m) == 1
        assert len(tools_5m[0]["description"]) == 5 * mib

        (ctrl / "list_bytes.txt").write_text(str(9 * mib), encoding="utf-8")
        with pytest.raises(McpTransportError) as exc_9m:
            sess.list_all_tools()
        assert str(exc_9m.value) == "agent_mcp_output_limit"
        assert sess.is_usable() is True
        assert sess.process.pid == orig_pid

        # Session survives the 9 MiB rejection and serves the next tools/list cleanly
        (ctrl / "list_bytes.txt").write_text("0", encoding="utf-8")
        tools_ok = sess.list_all_tools()
        assert len(tools_ok) == 1
        assert tools_ok[0]["description"] == "echo tool"

        # 2. tools/call follows the 40 MiB limit and removes cumulative session output_bytes cap:
        #    two 5 MiB calls + one 9 MiB call (> 19 MiB total) all succeed on the same session;
        #    a 41 MiB call fails only that request and keeps the session alive for subsequent calls.
        res_a = manager.call_tool(
            "srv-t3",
            "echo",
            {"pad_bytes": 5 * mib},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-t3",
        )
        mgr_pid = int(res_a["structuredContent"]["pid"])
        assert len(res_a["content"][0]["text"]) == 5 * mib

        res_b = manager.call_tool(
            "srv-t3",
            "echo",
            {"pad_bytes": 9 * mib},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-t3",
        )
        assert int(res_b["structuredContent"]["pid"]) == mgr_pid
        assert len(res_b["content"][0]["text"]) == 9 * mib

        with pytest.raises(McpTransportError) as exc_41m:
            manager.call_tool(
                "srv-t3",
                "echo",
                {"pad_bytes": 41 * mib},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
                session_scope="task-t3",
            )
        assert str(exc_41m.value) == "agent_mcp_output_limit"

        res_after = manager.call_tool(
            "srv-t3",
            "echo",
            {"tag": "still-alive"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-t3",
        )
        assert int(res_after["structuredContent"]["pid"]) == mgr_pid
        assert res_after["structuredContent"]["tag"] == "still-alive"
    finally:
        sess.close()
        manager.close()
