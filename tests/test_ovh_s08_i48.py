"""Tests for S08-I48: per-tool Craft timeouts, non-destructive request timeout, and post-publish reconciliation."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import time as std_time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_integrations import node as node  # noqa: F401
from test_craft_agent import ACTION, PREFIX, READ, TOOLS, _act, _args, _context, _read, craft  # noqa: F401
from test_craft_plugins import manager as manager  # noqa: F401

from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.node_apps import craft_agent, craft_native
from rynmesh.node_apps.craft_agent_policy import OUTPUT_TOOLS

REAL_MCP_PROCESS = craft_native.McpProcess


REAL_SESSION_SERVER = r"""
import json, sys, threading, time

state = {"text": "", "slow_get_done": False, "cancelled_ids": []}
out_lock = threading.Lock()

def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

def delayed_stale_reply(req_id):
    time.sleep(0.35)
    send({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps({"text": "STALE_TIMED_OUT_PAYLOAD"})}],
            "isError": False,
        },
    })

SCHEMAS = {
    "new_document": ({"template": {"type": "string"}}, []),
    "open_document": ({"path": {"type": "string"}}, ["path"]),
    "save_document": ({"path": {"type": "string"}}, []),
    "type_text": ({"text": {"type": "string"}, "paragraphs": {"type": "boolean"}}, ["text"]),
    "get_text": ({}, []),
    "inspect_document": ({}, []),
}

for raw in sys.stdin.buffer:
    msg = json.loads(raw)
    with open("session_received.ndjson", "a", encoding="utf-8") as logf:
        logf.write(json.dumps(msg, ensure_ascii=False) + "\n")
    method = msg.get("method", "")
    if "id" not in msg:
        if method == "notifications/cancelled":
            state["cancelled_ids"].append((msg.get("params") or {}).get("requestId"))
        continue
    req_id = msg["id"]
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "s08-i48-fixture", "version": "1.0"},
            },
        })
    elif method == "tools/list":
        tools = [
            {
                "name": name,
                "description": "Fixture " + name,
                "inputSchema": {"type": "object", "properties": props, "required": req},
            }
            for name, (props, req) in SCHEMAS.items()
        ]
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        tname = params.get("name")
        targs = params.get("arguments") or {}
        if tname == "new_document":
            state["text"] = ""
            payload = {"created": True}
        elif tname == "type_text":
            state["text"] += targs["text"]
            payload = {"characters": len(state["text"])}
        elif tname == "get_text":
            if not state["slow_get_done"]:
                state["slow_get_done"] = True
                threading.Thread(target=delayed_stale_reply, args=(req_id,), daemon=True).start()
                continue
            payload = {"text": state["text"]}
        elif tname == "inspect_document":
            payload = {"characters": len(state["text"])}
        elif tname == "save_document":
            with open(targs["path"], "wb") as f:
                f.write(b"Ryn MCP boundary fixture; not a real DOCX\n" + state["text"].encode("utf-8"))
            payload = {"saved": True}
        elif tname == "open_document":
            with open(targs["path"], "rb") as f:
                data = f.read()
            state["text"] = data.split(b"\n", 1)[-1].decode("utf-8")
            payload = {"opened": True}
        else:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown"}})
            continue
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": False,
            },
        })
"""


def test_s08_i48_t1_export_tool_completes_within_120s_without_timeout(craft, tmp_path, monkeypatch):
    assert craft_agent.TOOL_TIMEOUTS["read"] == 60.0
    assert craft_agent.TOOL_TIMEOUTS["output"] == 300.0
    for app_slug, tool_name in OUTPUT_TOOLS:
        assert craft_agent.tool_timeout(app_slug, tool_name) == 300.0
    assert craft_agent.tool_timeout("wordcraft", "get_text") == 60.0
    assert craft_agent.tool_timeout("wordcraft", "inspect_document") == 60.0
    assert craft_agent.tool_timeout("wordcraft", "type_text") == 60.0

    # Verify real McpProcess accepts 300s per-call timeout and 4h lifetime and
    # completes a 120s simulated export without hitting a 30s or 60s timeout.
    script = tmp_path / "export_server.py"
    script.write_text(REAL_SESSION_SERVER, encoding="utf-8")
    real_spawn = craft_native.process_guard.spawn
    clock = {"offset": 0.0}

    def fake_monotonic():
        return std_time.monotonic() + clock["offset"]

    monkeypatch.setattr(craft_native, "time", SimpleNamespace(monotonic=fake_monotonic, sleep=std_time.sleep))

    def spawn_fixture(args, **kwargs):
        return real_spawn([sys.executable, "-u", str(script)], **kwargs)

    observed_timeouts = []
    advancing_send = {"active": False}

    class RecordingMcpProcess(REAL_MCP_PROCESS):
        def _send(self, value, deadline):
            super()._send(value, deadline)
            if advancing_send["active"]:
                # Advance monotonic clock by 120s AFTER request deadline (now + timeout) was computed:
                # a 30s or 60s timeout would immediately fail self._check(deadline), whereas 300s succeeds.
                advancing_send["active"] = False
                clock["offset"] += 120.0

        def tools_call(self, name, arguments, *, timeout=None, cancelled=None):
            observed_timeouts.append((name, timeout))
            if name == "save_document":
                advancing_send["active"] = True
            return super().tools_call(name, arguments, timeout=timeout, cancelled=cancelled)

    monkeypatch.setattr(craft_native.process_guard, "spawn", spawn_fixture)
    monkeypatch.setattr(craft_native, "McpProcess", RecordingMcpProcess)

    runtime, _ = craft.runtime()
    task = runtime.create({"message": "导出 120 秒大文档"})
    context = _context(task)

    _act(craft, context, "new_document", operation="t1-new")
    _act(craft, context, "type_text", {"text": "large spreadsheet/export payload"}, operation="t1-edit")
    saved = _act(craft, context, "save_document", {"path": "export-120s.docx"}, operation="t1-save")

    assert saved["ok"] is True
    assert (craft.workspace / "export-120s.docx").is_file()
    save_calls = [timeout for name, timeout in observed_timeouts if name == "save_document"]
    assert save_calls == [300.0]


def test_s08_i48_t2_read_timeout_keeps_session_alive_and_next_call_succeeds(craft, tmp_path, monkeypatch):
    script = tmp_path / "session_server.py"
    script.write_text(REAL_SESSION_SERVER, encoding="utf-8")
    real_spawn = craft_native.process_guard.spawn

    def spawn_fixture(args, **kwargs):
        return real_spawn([sys.executable, "-u", str(script)], **kwargs)

    monkeypatch.setattr(craft_native.process_guard, "spawn", spawn_fixture)
    monkeypatch.setattr(craft_native, "McpProcess", REAL_MCP_PROCESS)
    monkeypatch.setitem(craft_agent.TOOL_TIMEOUTS, "read", 0.2)

    runtime, _ = craft.runtime()
    task = runtime.create({"message": "读取超时后继续使用同一会话"})
    context = _context(task)

    _act(craft, context, "new_document", operation="t2-new")
    _act(craft, context, "type_text", {"text": "in-memory document survives read timeout"}, operation="t2-edit")

    session_before = craft.tools.craft.sessions[(task["id"], "wordcraft")]
    session_id_before = session_before["id"]
    pid_before = session_before["process"].process.pid

    # First read times out at 0.2s; server later emits a stale response for that cancelled id.
    with pytest.raises(AgentToolRejected, match="craft_mcp_timeout"):
        _read(craft, context, "get_text", operation="t2-read-timeout")

    assert not session_before["process"].closed.is_set()
    assert session_before["process"].process.poll() is None

    # Wait briefly so the delayed stale reply for the timed-out id arrives on stdout,
    # then verify the next read on the SAME session discards the stale id and succeeds.
    std_time.sleep(0.25)

    monkeypatch.setitem(craft_agent.TOOL_TIMEOUTS, "read", 60.0)
    second = _read(craft, context, "get_text", operation="t2-read-ok")
    assert second["ok"] is True
    assert second["session_id"] == session_id_before
    assert second["data"]["text"] == "in-memory document survives read timeout"
    assert session_before["process"].process.pid == pid_before

    received = [
        json.loads(line)
        for line in (craft.workspace / "session_received.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cancelled_msgs = [row for row in received if row.get("method") == "notifications/cancelled"]
    assert len(cancelled_msgs) == 1
    assert isinstance(cancelled_msgs[0]["params"]["requestId"], int)


def test_s08_i48_t3_reopen_oserror_after_publish_returns_ok_with_validation_failed(craft, monkeypatch):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "发布后重开验证抛 OSError"})
    context = _context(task)
    _act(craft, context, "new_document", operation="t3-new")
    _act(craft, context, "type_text", {"text": "published document bytes"}, operation="t3-edit")

    # Case A: OSError raised inside _reopen (e.g. McpProcess spawn fails with OSError).
    original_mcp = craft_native.McpProcess

    def broken_reopen_mcp(*args, **kwargs):
        raise OSError("simulated reopen subprocess OSError")

    monkeypatch.setattr(craft_native, "McpProcess", broken_reopen_mcp)
    saved_inner = _act(craft, context, "save_document", {"path": "reopen-inner-oserror.docx"}, operation="t3-save-inner")
    assert saved_inner["ok"] is True
    assert len(saved_inner["files"]) == 1
    assert saved_inner["files"][0]["validation"]["status"] == "failed"
    assert (craft.workspace / "reopen-inner-oserror.docx").read_bytes() == PREFIX + b"published document bytes"
    assert craft.tools.resource_writes.ledger.get("t3-save-inner")["status"] == "confirmed"

    # Case B: _reopen itself raises OSError directly.
    monkeypatch.setattr(craft_native, "McpProcess", original_mcp)

    def broken_reopen(*_args, **_kwargs):
        raise OSError("simulated direct _reopen OSError")

    monkeypatch.setattr(craft.tools.craft, "_reopen", broken_reopen)
    saved_direct = _act(craft, context, "save_document", {"path": "reopen-direct-oserror.docx"}, operation="t3-save-direct")
    assert saved_direct["ok"] is True
    assert saved_direct["files"][0]["validation"]["status"] == "failed"
    assert (craft.workspace / "reopen-direct-oserror.docx").read_bytes() == PREFIX + b"published document bytes"
    assert craft.tools.resource_writes.ledger.get("t3-save-direct")["status"] == "confirmed"


def test_s08_i48_t4_reconcile_published_ledger_and_timeout_staged_output(craft, monkeypatch):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "published 台账对账与超时导出核对"})
    context = _context(task)
    _act(craft, context, "new_document", operation="t4-new")
    _act(craft, context, "type_text", {"text": "reconcile verified content"}, operation="t4-edit")

    ledger = craft.tools.resource_writes.ledger
    real_publish = craft.tools.craft._publish
    seen_before_publish = {}

    def publish_then_crash(source, destination):
        row = ledger.get("t4-published-crash")
        seen_before_publish["status"] = row["status"]
        seen_before_publish["staged_outputs"] = copy.deepcopy(row.get("staged_outputs"))
        real_publish(source, destination)
        raise RuntimeError("simulated crash after atomic publish before confirmed update")

    save_args = _args("save_document", {"path": "published-crash.docx"})
    save_preview = craft.tools.preview(ACTION, save_args, context=context)

    # First verify that if we crash BEFORE ledger reaches confirmed, ledger recorded published + staged_outputs,
    # AND execute's post-dispatch reconciliation recovers the published file; also test standalone reconcile()
    # directly when ledger status is "published".
    monkeypatch.setattr(craft.tools.craft, "_publish", publish_then_crash)
    recovered = craft.tools.execute(
        ACTION,
        save_args,
        context=context,
        operation_id="t4-published-crash",
        expected_preview=save_preview,
        cancelled=lambda: False,
    )
    assert seen_before_publish["status"] == "published"
    expected_bytes = PREFIX + b"reconcile verified content"
    expected_sha = hashlib.sha256(expected_bytes).hexdigest()
    assert seen_before_publish["staged_outputs"] == [
        {"path": str(craft.workspace / "published-crash.docx"), "sha256": expected_sha}
    ]
    assert recovered["ok"] is True
    assert recovered["files"][0]["sha256"] == expected_sha

    # Now set the ledger row explicitly to "published" (as after a hard process kill right after _publish)
    # and verify CraftTools.reconcile / AgentTools.reconcile directly.
    ledger.update("t4-published-crash", "published", staged_outputs=seen_before_publish["staged_outputs"])
    op_meta = {
        "operation_id": "t4-published-crash",
        "tool": ACTION,
        "arguments": save_args,
        "arguments_sha256": craft_agent.fingerprint(save_args),
    }
    reconciled = craft.tools.reconcile(
        "t4-published-crash",
        context={**context, "operation": op_meta},
        cancelled=lambda: False,
    )
    assert reconciled["outcome"] == "executed"
    assert reconciled["files"][0]["path"] == str(craft.workspace / "published-crash.docx")
    assert reconciled["files"][0]["sha256"] == expected_sha
    assert reconciled["receipt"]["result"]["files"][0]["sha256"] == expected_sha

    # Tampering with target file makes reconcile return unknown.
    (craft.workspace / "published-crash.docx").write_bytes(b"corrupted bytes")
    assert (
        craft.tools.reconcile("t4-published-crash", context={**context, "operation": op_meta}, cancelled=lambda: False)["outcome"]
        == "unknown"
    )

    # Legacy published/dispatched record without staged_outputs returns unknown.
    ledger.update("t4-published-crash", "published", staged_outputs=None)
    (craft.workspace / "published-crash.docx").write_bytes(expected_bytes)
    assert (
        craft.tools.reconcile("t4-published-crash", context={**context, "operation": op_meta}, cancelled=lambda: False)["outcome"]
        == "unknown"
    )

    # Change 5: Timeout after staged output was completely written reconciles and succeeds.
    monkeypatch.setattr(craft.tools.craft, "_publish", real_publish)
    timeout_args = _args("save_document", {"path": "timeout-complete.docx"})
    timeout_preview = craft.tools.preview(ACTION, timeout_args, context=context)
    craft.backend.fail_next = "save_document"
    timeout_saved = craft.tools.execute(
        ACTION,
        timeout_args,
        context=context,
        operation_id="t4-timeout-complete",
        expected_preview=timeout_preview,
        cancelled=lambda: False,
    )
    assert timeout_saved["ok"] is True
    assert timeout_saved["files"][0]["sha256"] == expected_sha
    assert (craft.workspace / "timeout-complete.docx").read_bytes() == expected_bytes
    assert ledger.get("t4-timeout-complete")["status"] == "confirmed"

    # Change 5 negative check: Timeout when NO staged output was written still raises agent_write_outcome_unknown.
    empty_timeout_args = _args("save_document", {"path": "timeout-missing.docx"})
    empty_timeout_preview = craft.tools.preview(ACTION, empty_timeout_args, context=context)
    session = craft.tools.craft.sessions[(task["id"], "wordcraft")]
    orig_tools_call = session["process"].tools_call

    def timeout_before_write(name, args_dict, *, timeout=None, cancelled=None):
        if name == "save_document":
            raise craft_native.CraftError("craft_mcp_timeout")
        return orig_tools_call(name, args_dict, timeout=timeout, cancelled=cancelled)

    monkeypatch.setattr(session["process"], "tools_call", timeout_before_write)
    with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
        craft.tools.execute(
            ACTION,
            empty_timeout_args,
            context=context,
            operation_id="t4-timeout-missing",
            expected_preview=empty_timeout_preview,
            cancelled=lambda: False,
        )


def test_s08_i48_t5_invalid_write_arguments_record_not_executed_and_keep_session(craft):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "写操作参数无效不杀会话且记 not_executed"})
    context = _context(task)

    _act(craft, context, "new_document", operation="t5-new")
    _act(craft, context, "type_text", {"text": "unsaved session text stays intact"}, operation="t5-edit")
    session_before = craft.tools.craft.sessions[(task["id"], "wordcraft")]
    session_id_before = session_before["id"]
    ledger = craft.tools.resource_writes.ledger

    # Case A: Direct write execute with invalid tool arguments (missing required 'text').
    bad_args = _args("type_text", {"unknown_param": 123})
    with pytest.raises(AgentToolRejected, match="agent_tool_arguments_invalid"):
        craft.tools.execute(
            ACTION,
            bad_args,
            context=context,
            operation_id="t5-invalid-schema",
            expected_preview=None,
            cancelled=lambda: False,
        )
    row_a = ledger.get("t5-invalid-schema")
    assert row_a is not None
    assert row_a["status"] == "not_executed"
    assert row_a["error"] == "agent_tool_arguments_invalid"
    assert not session_before["process"].closed.is_set()

    # Case B: Pre-_invoke_action rejection after ledger.begin (e.g. target output path created right before _arguments
    # or baseline rejects before engine_dispatched becomes True).
    save_args = _args("save_document", {"path": "conflict-before-dispatch.docx"})
    save_preview = craft.tools.preview(ACTION, save_args, context=context)
    # Bypass preview comparison by keeping preview intact while making baseline reject pre-dispatch,
    # and also test _arguments rejecting existing path after ledger.begin.
    original_baseline = craft.tools.local.baseline

    def reject_baseline(*_a, **_kw):
        raise AgentToolRejected("agent_craft_new_output_path_required")

    craft.tools.local.baseline = reject_baseline
    try:
        with pytest.raises(AgentToolRejected, match="agent_craft_new_output_path_required"):
            craft.tools.execute(
                ACTION,
                save_args,
                context=context,
                operation_id="t5-pre-dispatch-rejected",
                expected_preview=save_preview,
                cancelled=lambda: False,
            )
    finally:
        craft.tools.local.baseline = original_baseline

    row_b = ledger.get("t5-pre-dispatch-rejected")
    assert row_b is not None
    assert row_b["status"] == "not_executed"
    assert row_b["error"] == "agent_craft_new_output_path_required"

    # Session and unsaved edits are preserved!
    session_after = craft.tools.craft.sessions[(task["id"], "wordcraft")]
    assert session_after["id"] == session_id_before
    assert not session_after["process"].closed.is_set()
    assert _read(craft, context, "get_text", operation="t5-verify-read")["data"]["text"] == "unsaved session text stays intact"


def test_s08_i48_t6_same_operation_id_reexecutes_after_fixing_arguments(craft):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "同一 operation_id 修正参数后可重新执行"})
    other_task = runtime.create({"message": "另一任务不能复用该 operation_id"})
    context = _context(task)
    other_context = _context(other_task)

    _act(craft, context, "new_document", operation="t6-new")
    _act(craft, context, "type_text", {"text": "initial "}, operation="t6-edit")
    ledger = craft.tools.resource_writes.ledger

    # Step 1: Call write with invalid arguments under operation_id="t6-same-op".
    invalid_args = _args("type_text", {"wrong_key": "value"})
    with pytest.raises(AgentToolRejected, match="agent_tool_arguments_invalid"):
        craft.tools.execute(
            ACTION,
            invalid_args,
            context=context,
            operation_id="t6-same-op",
            expected_preview=None,
            cancelled=lambda: False,
        )
    assert ledger.get("t6-same-op")["status"] == "not_executed"

    # Another task still cannot hijack a not_executed operation_id.
    _act(craft, other_context, "new_document", operation="t6-other-new")
    other_valid_args = _args("type_text", {"text": "hijack"})
    other_preview = craft.tools.preview(ACTION, other_valid_args, context=other_context)
    with pytest.raises(AgentToolRejected, match="agent_operation_id_conflict"):
        craft.tools.execute(
            ACTION,
            other_valid_args,
            context=other_context,
            operation_id="t6-same-op",
            expected_preview=other_preview,
            cancelled=lambda: False,
        )

    # Step 2: Fix the arguments for the SAME task and re-execute with the SAME operation_id="t6-same-op".
    fixed_args = _args("type_text", {"text": "fixed text"})
    fixed_preview = craft.tools.preview(ACTION, fixed_args, context=context)
    result = craft.tools.execute(
        ACTION,
        fixed_args,
        context=context,
        operation_id="t6-same-op",
        expected_preview=fixed_preview,
        cancelled=lambda: False,
    )
    assert result["ok"] is True
    assert ledger.get("t6-same-op")["status"] == "confirmed"
    assert _read(craft, context, "get_text", operation="t6-readback")["data"]["text"] == "initial fixed text"

    # Also verify save_document with existing path -> not_executed -> fixed new path with same operation_id -> confirmed!
    existing = craft.workspace / "already-exists.docx"
    existing.write_bytes(PREFIX + b"old")
    with pytest.raises(AgentToolRejected, match="agent_craft_new_output_path_required"):
        craft.tools.execute(
            ACTION,
            _args("save_document", {"path": "already-exists.docx"}),
            context=context,
            operation_id="t6-save-same-op",
            expected_preview=None,
            cancelled=lambda: False,
        )
    assert ledger.get("t6-save-same-op")["status"] == "not_executed"

    fixed_save_args = _args("save_document", {"path": "fixed-output.docx"})
    fixed_save_preview = craft.tools.preview(ACTION, fixed_save_args, context=context)
    saved = craft.tools.execute(
        ACTION,
        fixed_save_args,
        context=context,
        operation_id="t6-save-same-op",
        expected_preview=fixed_save_preview,
        cancelled=lambda: False,
    )
    assert saved["ok"] is True
    assert (craft.workspace / "fixed-output.docx").read_bytes() == PREFIX + b"initial fixed text"
    assert ledger.get("t6-save-same-op")["status"] == "confirmed"
