"""Focused workstation acceptance: actual files/processes and durable approvals."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source, call, fake_upstream, passed_checker
from test_agent_integrations import node as node
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.services import peer_box


@pytest.fixture
def local(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "node"
    home.mkdir()
    runtime, _, _, _ = make_runtime(home / "agent")
    state = runtime.store
    configuration = {"workspace_root": str(workspace), "desktop_access": False}
    runtime.update_settings(configuration)
    task = runtime.create({"message": "Local tool acceptance"})
    def rename(data):
        row = data["tasks"].pop(task["id"])
        row["id"] = "task"
        data["tasks"]["task"] = row
    state.transaction(rename)
    ledger = OperationLedger(home)
    tools = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    tools.store = state
    context = {"task_id": "task", "local_execution": configuration}
    yield tools, context, workspace, state
    tools.processes.close()


def execute(tools, context, name, args, operation_id="op"):
    return tools.execute(name, args, operation_id=operation_id, context=context,
                         expected_preview=tools.preview(name, args, context), cancelled=lambda: False)


def test_file_create_edit_search_and_readback(local):
    tools, context, workspace, _ = local
    created = execute(tools, context, "ryn_file_write", {"path": "hello.txt", "content": "hello 世界\n"})
    observed = tools.perform("ryn_file_read", {"path": "hello.txt"}, context, lambda: False)
    assert observed["sha256"] == created["sha256"]
    execute(tools, context, "ryn_file_edit", {"path": "hello.txt", "old_text": "hello", "new_text": "你好", "expected_sha256": observed["sha256"]}, "edit")
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "你好 世界\n"
    assert tools.perform("ryn_file_search", {"path": ".", "query": "世界"}, context, lambda: False)["matches"][0]["line"] == 1


def test_search_prunes_generated_directories_and_reads_store_once(local):
    tools, context, workspace, state = local
    (workspace / ".gitignore").write_text("generated-cache/\n*.log\n", encoding="utf-8")
    generated = workspace / "generated-cache"
    generated.mkdir()
    for index in range(30):
        (generated / f"g{index}.txt").write_text("needle in cache\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dep.js").write_text("needle in dependency\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "real.py").write_text("def needle():\n    pass\n", encoding="utf-8")
    reads = []
    original = state.read
    state.read = lambda: (reads.append(1), original())[1]
    try:
        result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    finally:
        state.read = original
    assert [Path(match["path"]).resolve() for match in result["matches"]] == [(workspace / "src" / "real.py").resolve()]
    assert {"generated-cache", "node_modules"} <= set(result["pruned_directories"])
    assert result["incomplete"] is False
    # One policy read for the start path and one for the root; never one per scanned file.
    assert len(reads) <= 4
    explicit = tools.perform("ryn_file_search", {"path": "generated-cache", "query": "needle"}, context, lambda: False)
    assert explicit["scanned_files"] == 30 and len(explicit["matches"]) == 15 and explicit["next_offset"] == 15


def test_search_reaches_shallow_source_before_deep_generated_trees(local, monkeypatch):
    from rynmesh.agent_runtime import local_tools
    tools, context, workspace, _ = local
    deep = workspace / "aaa" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    for index in range(10):
        (deep / f"noise{index}.txt").write_text("needle deep\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "real.py").write_text("def needle():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(local_tools, "MAX_SEARCH_FILES", 3)
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert result["incomplete"] is True and result["scanned_files"] == 3
    assert any(Path(match["path"]).name == "real.py" for match in result["matches"])


def test_gitignore_pruning_is_partial_and_conservative(local):
    tools, context, workspace, _ = local
    (workspace / "build").mkdir()
    (workspace / "build" / "a.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "src" / "build").mkdir(parents=True)
    (workspace / "src" / "build" / "b.txt").write_text("needle\n", encoding="utf-8")
    (workspace / ".gitignore").write_text("/build/\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    found = {Path(match["path"]).resolve() for match in result["matches"]}
    # An anchored pattern prunes only the workspace-root directory, never src/build.
    assert found == {(workspace / "src" / "build" / "b.txt").resolve()}
    assert result["pruned_directories"] == ["build"] and result["pruned_count"] == 1
    # Any negation makes the file untrusted: nothing is pruned from .gitignore.
    (workspace / ".gitignore").write_text("/build/\n!build/keep.txt\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert len(result["matches"]) == 2 and result["pruned_directories"] == []
    # A negation naming something else must not disable unrelated pruning.
    (workspace / ".gitignore").write_text("/build/\n!deploy/env/*.example\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert len(result["matches"]) == 1 and result["pruned_directories"] == ["build"]
    # Wildcards and nested patterns are not interpreted, so they never hide anything.
    (workspace / ".gitignore").write_text("*/build/\nsrc/build\n**/build\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert len(result["matches"]) == 2 and result["pruned_directories"] == []


def test_read_reports_lines_pages_on_line_boundaries_and_addresses_by_line(local, monkeypatch):
    from rynmesh.agent_runtime import local_tools
    tools, context, workspace, _ = local
    body = "".join(f"line {number:03d} {'x' * 40}\r\n" for number in range(1, 201))  # CRLF, as in this repo
    (workspace / "big.txt").write_text(body, encoding="utf-8", newline="")
    monkeypatch.setattr(local_tools, "MAX_READ_CHARS", 1000)
    first = tools.perform("ryn_file_read", {"path": "big.txt"}, context, lambda: False)
    assert first["start_line"] == 1 and first["total_lines"] == 200
    assert first["text"].endswith("\r\n") and first["end_line"] == first["text"].count("\n")
    assert first["next_start_line"] == first["end_line"] + 1 and first["next_offset"] == len(first["text"])
    # Continuing by offset and by line must agree.
    by_offset = tools.perform("ryn_file_read", {"path": "big.txt", "offset": first["next_offset"]}, context, lambda: False)
    by_line = tools.perform("ryn_file_read", {"path": "big.txt", "start_line": first["next_start_line"]}, context, lambda: False)
    assert by_offset["text"] == by_line["text"] and by_offset["start_line"] == by_line["start_line"] == first["end_line"] + 1
    # Arbitrary line addressing, bounded by max_lines, never splitting a line.
    window = tools.perform("ryn_file_read", {"path": "big.txt", "start_line": 150, "max_lines": 3}, context, lambda: False)
    assert window["text"].splitlines() == [f"line {n:03d} {'x' * 40}" for n in (150, 151, 152)]
    assert (window["start_line"], window["end_line"], window["next_start_line"]) == (150, 152, 153)
    tail = tools.perform("ryn_file_read", {"path": "big.txt", "start_line": 200}, context, lambda: False)
    assert tail["end_line"] == 200 and tail["next_start_line"] is None and tail["next_offset"] is None
    with pytest.raises(AgentToolRejected, match="agent_local_line_invalid"):
        tools.perform("ryn_file_read", {"path": "big.txt", "start_line": 201}, context, lambda: False)
    # Paging through the whole file reproduces it exactly.
    pages, offset = [], 0
    while offset is not None:
        page = tools.perform("ryn_file_read", {"path": "big.txt", "offset": offset}, context, lambda: False)
        pages.append(page["text"])
        offset = page["next_offset"]
    assert "".join(pages) == body and len(pages) > 1


@pytest.mark.parametrize(("bad_call", "expected_error", "expected_message"), [
    (call("ryn_file_read", json.dumps({"start_line": 2}), "bad"), "agent_tool_arguments_invalid", "Tool arguments do not match the request-bound parameter schema."),
    (call("ryn_file_read", "{not json", "bad"), "agent_tool_arguments_invalid", "Tool arguments must be a valid JSON object."),
    (call("ryn_does_not_exist", "{}", "bad"), "agent_unknown_tool", "Choose a tool from the tools offered in this request."),
])
def test_invalid_tool_call_is_explained_on_the_retry_instead_of_repeated_blind(node, monkeypatch, tmp_path, bad_call, expected_error, expected_message):
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "invalid-agent", peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path)})
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8", newline="")
    requests = fake_upstream(monkeypatch, [
        call("ryn_discover_tools", json.dumps({"load": ["ryn_file_read"]}), "discover"),
        bad_call,
        call("ryn_file_read", json.dumps({"path": "a.txt"}), "good"),
        {"role": "assistant", "content": "It says alpha."}, passed_checker])
    task = runtime.create({"message": "What does a.txt say?"})
    for _ in range(20):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed", (current.get("error"), [(e["type"], e.get("detail"), e.get("error")) for e in current["events"][-10:]])
    expected_payload = {"error": expected_error, "message": expected_message, "not_executed": True, "no_mutation": True}
    rejection = next(e for e in current["events"] if e["type"] == "tool_rejected" and e.get("call_id") == "bad")
    assert rejection["result"] == expected_payload
    assert not [e for e in current["events"] if e["type"] in {"tool_started", "tool_execution_completed", "tool_result"} and e.get("call_id") == "bad"]
    good_started = [e for e in current["events"] if e["type"] == "tool_started" and not e.get("internal")]
    good_results = [e for e in current["events"] if e["type"] == "tool_result" and not e.get("internal")]
    assert [e["call_id"] for e in good_started] == ["good"]
    assert [e["call_id"] for e in good_results] == ["good"]
    good_op_id = good_results[0]["operation_id"]
    assert good_started[0]["operation_id"] == good_op_id
    receipts = runtime.store.read()["tasks"][task["id"]].get("receipts", [])
    assert [r["operation_id"] for r in receipts] == [good_op_id]
    assert receipts[0]["tool"] == "ryn_file_read" and receipts[0]["status"] == "confirmed" and receipts[0]["arguments"] == {"path": "a.txt"}
    raw_args = bad_call["tool_calls"][0]["function"]["arguments"]
    assistant_bad = next(
        tc for m in requests[2]["messages"] if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or []) if tc.get("id") == "bad"
    )
    assert assistant_bad["function"]["arguments"] == raw_args
    paired = requests[2]["messages"][-1]
    assert paired["role"] == "tool" and paired["tool_call_id"] == "bad"
    assert json.loads(paired["content"]) == expected_payload
    good_tool = next(m for m in requests[3]["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "good")
    assert json.loads(good_tool["content"])["text"] == "alpha\n"


def test_mixed_valid_and_invalid_tool_calls_execute_valid_sibling_once_and_pair_rejection(node, monkeypatch, tmp_path):
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "mixed-agent", peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path)})
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8", newline="")
    mixed = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "good", "type": "function", "function": {"name": "ryn_file_read", "arguments": json.dumps({"path": "a.txt"})}},
            {"id": "bad", "type": "function", "function": {"name": "ryn_file_read", "arguments": "{not json"}},
        ],
    }
    requests = fake_upstream(monkeypatch, [
        call("ryn_discover_tools", json.dumps({"load": ["ryn_file_read"]}), "discover"),
        mixed,
        {"role": "assistant", "content": "It says alpha."},
        passed_checker,
    ])
    task = runtime.create({"message": "Read a.txt even if one sibling call is malformed."})
    for _ in range(20):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed", current.get("error")
    stored = runtime.store.read()["tasks"][task["id"]]
    assert not [e for e in current["events"] if e["type"] in {"tool_started", "tool_execution_completed", "tool_result"} and e.get("call_id") == "bad"]
    good_started = [e for e in current["events"] if e["type"] == "tool_started" and not e.get("internal")]
    good_results = [e for e in current["events"] if e["type"] == "tool_result" and not e.get("internal")]
    assert [e["call_id"] for e in good_started] == ["good"]
    assert [e["call_id"] for e in good_results] == ["good"]
    good_op_id = good_results[0]["operation_id"]
    assert good_started[0]["operation_id"] == good_op_id
    receipts = stored.get("receipts", [])
    assert [r["operation_id"] for r in receipts] == [good_op_id]
    assert receipts[0]["tool"] == "ryn_file_read" and receipts[0]["status"] == "confirmed" and receipts[0]["arguments"] == {"path": "a.txt"}
    rejections = [e for e in current["events"] if e["type"] == "tool_rejected"]
    assert len(rejections) == 1 and rejections[0]["call_id"] == "bad"
    assert rejections[0]["result"] == {
        "error": "agent_tool_arguments_invalid",
        "message": "Tool arguments must be a valid JSON object.",
        "not_executed": True,
        "no_mutation": True,
    }
    followup = requests[2]["messages"]
    tool_msgs = {m["tool_call_id"]: json.loads(m["content"]) for m in followup if m.get("role") == "tool" and m.get("tool_call_id") in {"good", "bad"}}
    assert tool_msgs["good"]["text"] == "alpha\n"
    assert tool_msgs["bad"] == rejections[0]["result"]


def test_search_ranks_matching_files_so_a_broad_term_can_be_narrowed(local):
    tools, context, workspace, _ = local
    (workspace / "one.txt").write_text("needle\nother\n", encoding="utf-8")
    (workspace / "many.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert result["total_matches"] == 4 and result["files_matched_total"] == 2
    assert [(Path(item["path"]).name, item["matches"], item["first_line"]) for item in result["files_matched"]] == [
        ("many.txt", 3, 1), ("one.txt", 1, 1)]


def test_budget_notice_reaches_the_model_once_and_is_not_written_to_the_receipt(node, monkeypatch, tmp_path):
    from rynmesh.agent_runtime.streamed_reads import _publish, _stamp

    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "budget-agent", peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path), "max_steps": 7})
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    reads = [call("ryn_file_read", json.dumps({"path": "a.txt", "start_line": 1, "max_lines": number}), f"read{number}") for number in (1, 2, 3, 4)]
    requests = fake_upstream(monkeypatch, [call("ryn_discover_tools", json.dumps({"load": ["ryn_file_read"]}), "discover"), *reads, {"role": "assistant", "content": "Read it."}, passed_checker])
    task = runtime.create({"message": "Read a.txt three ways."})
    for _ in range(20):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed", current.get("error")
    final_request = requests[-1]["messages"]  # Normal completion has no extra checker request.
    notices = [m for m in final_request if m.get("role") == "tool" and "Server budget notice" in m.get("content", "")]
    assert len(notices) == 1 and "5 of 7" in notices[0]["content"] and "Conclude from the evidence" in notices[0]["content"]
    assert "make the change now" not in notices[0]["content"]
    assert all("server_note" not in json.dumps(e.get("result", {})) for e in current["events"] if e["type"] == "tool_result")
    stored_row = runtime.store.read()["tasks"][task["id"]]
    assert all("server_note" not in json.dumps(r.get("result", {})) for r in stored_row.get("receipts", []))

    # Verify multi-read streamed publication attaches server_note only to the last newly
    # published tool message and never advances budget_notice_level on rejected/idempotent calls.
    row = {
        "id": "task-batch",
        "status": "running",
        "model_id": model_id,
        "steps": 5,
        "limits": {"max_steps": 7},
        "goal": {"revision": 1},
        "scope_revision": 1,
        "control_revision": 1,
        "context_epoch": 0,
        "event_cursor": 0,
        "revision": 1,
        "model_destination": None,
        "local_execution": {"workspace_root": str(tmp_path)},
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "r1", "type": "function", "function": {"name": "ryn_file_read", "arguments": "{}"}},
                    {"id": "r2", "type": "function", "function": {"name": "ryn_file_read", "arguments": "{}"}},
                ],
            }
        ],
        "events": [],
        "receipts": [],
    }
    stamp = _stamp(row)
    records = [
        {"index": 0, "stamp": {**stamp, "scope_revision": 99}, "call": {"call_id": "r1", "name": "ryn_file_read", "operation_id": "op1"}, "result": {"text": "one"}},
        {"index": 1, "stamp": stamp, "call": {"call_id": "r2", "name": "ryn_file_read", "operation_id": "op2"}, "result": {"text": "two"}},
    ]
    assert _publish(row, records) is False
    assert row.get("budget_notice_level", 0) == 0
    records[0]["stamp"] = stamp
    assert _publish(row, records) is True
    assert row["budget_notice_level"] == 1
    published_tools = [json.loads(m["content"]) for m in row["messages"] if m.get("role") == "tool"]
    assert "server_note" not in published_tools[0] and "5 of 7" in published_tools[1]["server_note"]
    assert all("server_note" not in r["result"] for r in records)
    assert all("server_note" not in e["result"] for e in row["events"] if e["type"] == "tool_result")
    row["steps"] = 6  # Crosses 85% threshold, but idempotent re-publish has no unanswered calls.
    assert _publish(row, records) is True
    assert row["budget_notice_level"] == 1


def test_unchanged_range_read_twice_is_flagged_but_still_returned(local):
    tools, context, workspace, _ = local
    (workspace / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    first = tools.perform("ryn_file_read", {"path": "a.txt"}, context, lambda: False)
    assert "repeat_read" not in first
    again = tools.perform("ryn_file_read", {"path": "a.txt"}, context, lambda: False)
    assert again["text"] == first["text"] and again["repeat_read"] == 1 and "already received" in again["note"]
    # A different range, or the same range after the file changed, is not a repeat.
    assert "repeat_read" not in tools.perform("ryn_file_read", {"path": "a.txt", "start_line": 2}, context, lambda: False)
    (workspace / "a.txt").write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    assert "repeat_read" not in tools.perform("ryn_file_read", {"path": "a.txt"}, context, lambda: False)


def test_multiline_edit_matches_across_line_endings_and_keeps_the_file_convention(local):
    tools, context, workspace, _ = local
    crlf = workspace / "crlf.py"
    crlf.write_bytes(b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n")
    digest = hashlib.sha256(crlf.read_bytes()).hexdigest()
    # Models send "\n"; the file is CRLF. The edit must still apply and must not mix endings.
    execute(tools, context, "ryn_file_edit", {"path": "crlf.py", "old_text": "def b():\n    return 2\n",
            "new_text": "def b():\n    return 3\n", "expected_sha256": digest}, "crlf-edit")
    assert crlf.read_bytes() == b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 3\r\n"
    lf = workspace / "lf.py"
    lf.write_bytes(b"x = 1\ny = 2\n")
    digest = hashlib.sha256(lf.read_bytes()).hexdigest()
    execute(tools, context, "ryn_file_edit", {"path": "lf.py", "old_text": "x = 1\r\ny = 2\r\n",
            "new_text": "x = 10\r\ny = 20\r\n", "expected_sha256": digest}, "lf-edit")
    assert lf.read_bytes() == b"x = 10\ny = 20\n"
    # An exact match is never rewritten, and a genuinely absent text is still rejected.
    exact = workspace / "exact.txt"
    exact.write_bytes(b"one\r\ntwo\nthree\r\n")
    digest = hashlib.sha256(exact.read_bytes()).hexdigest()
    execute(tools, context, "ryn_file_edit", {"path": "exact.txt", "old_text": "two\n", "new_text": "2\n",
            "expected_sha256": digest}, "exact-edit")
    assert exact.read_bytes() == b"one\r\n2\nthree\r\n"
    with pytest.raises(AgentToolRejected, match="agent_local_edit_match_count"):
        tools.preview("ryn_file_edit", {"path": "exact.txt", "old_text": "missing\nlines\n", "new_text": "x",
                      "expected_sha256": hashlib.sha256(exact.read_bytes()).hexdigest()}, context)


def test_edit_preview_and_receipt_show_exactly_which_lines_were_removed(local):
    tools, context, workspace, _ = local
    target = workspace / "test_x.py"
    target.write_text("def test_a():\n    assert one\n    assert two\n", encoding="utf-8", newline="")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    args = {"path": "test_x.py", "old_text": "    assert one\n    assert two\n", "new_text": "    assert one\n", "expected_sha256": digest}
    preview = tools.preview("ryn_file_edit", args, context)
    assert preview["removed_lines"] == 1 and preview["added_lines"] == 0
    assert "-    assert two" in preview["diff"] and "assert one" not in "".join(
        line for line in preview["diff"].splitlines() if line.startswith(("-", "+")))
    result = execute(tools, context, "ryn_file_edit", args, "weaken")
    assert result["removed_lines"] == 1 and "-    assert two" in result["diff"]
    assert target.read_text(encoding="utf-8") == "def test_a():\n    assert one\n"


def test_shell_environment_does_not_inherit_node_configuration_or_secrets(local, monkeypatch):
    tools, context, _, _ = local
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "node-control-secret-value")
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "network-secret-value")
    monkeypatch.setenv("UNRELATED_SETTING", "kept-visible")
    if os.name == "nt":
        command = "Write-Output ('token=[' + $env:RYNMESH_LOCAL_TOKEN + '] key=[' + $env:RYNMESH_NETWORK_KEY + '] other=[' + $env:UNRELATED_SETTING + ']')"
    else:
        command = 'echo "token=[$RYNMESH_LOCAL_TOKEN] key=[$RYNMESH_NETWORK_KEY] other=[$UNRELATED_SETTING]"'
    result = execute(tools, context, "ryn_shell_execute", {"command": command}, "env")
    assert "node-control-secret-value" not in result["output"] and "network-secret-value" not in result["output"]
    assert "token=[]" in result["output"] and "key=[]" in result["output"] and "other=[kept-visible]" in result["output"]


def test_search_has_a_wall_clock_bound_and_reports_why_it_stopped(local, monkeypatch):
    from rynmesh.agent_runtime import local_tools
    tools, context, workspace, _ = local
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(local_tools, "MAX_SEARCH_SECONDS", 0.0)
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert result["incomplete"] is True and result["stopped_reason"] == "time_limit"
    monkeypatch.setattr(local_tools, "MAX_SEARCH_SECONDS", 30.0)
    monkeypatch.setattr(local_tools, "MAX_SEARCH_FILES", 1)
    (workspace / "b.txt").write_text("needle\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert result["incomplete"] is True and result["stopped_reason"] == "file_limit"


def test_search_skips_binary_suffixes_without_reading(local):
    tools, context, workspace, _ = local
    (workspace / "image.png").write_bytes(b"needle\x00\x01")
    (workspace / "notes.txt").write_text("needle\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert [Path(match["path"]).name for match in result["matches"]] == ["notes.txt"]
    assert result["binary_files_skipped"] == 1 and result["scanned_files"] == 1


def test_search_stops_when_agent_is_disabled_or_cancelled_mid_scan(local):
    tools, context, workspace, state = local
    for index in range(250):
        (workspace / f"f{index:03d}.txt").write_text("nothing\n", encoding="utf-8")
    original = tools.text
    seen = []

    def disabling(path):
        seen.append(path)
        if len(seen) == 40:
            state.transaction(lambda data: data["settings"].update(enabled=False))
        return original(path)

    tools.text = disabling
    try:
        with pytest.raises(AgentToolRejected, match="agent_disabled"):
            tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    finally:
        tools.text = original
    # Revocation is noticed at the next periodic check, not after the whole tree.
    assert len(seen) < 250
    state.transaction(lambda data: data["settings"].update(enabled=True))
    calls = []
    def cancel_after_twenty():
        calls.append(1)
        return len(calls) > 20
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, cancel_after_twenty)


def test_search_never_follows_symlinks_out_of_the_workspace(local, tmp_path):
    tools, context, workspace, _ = local
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle outside\n", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "link", target_is_directory=True)
        os.symlink(outside / "secret.txt", workspace / "secret-link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted in this environment")
    (workspace / "inside.txt").write_text("needle inside\n", encoding="utf-8")
    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert [Path(match["path"]).name for match in result["matches"]] == ["inside.txt"]


def test_file_version_and_approval_change_do_not_overwrite(local):
    tools, context, workspace, _ = local
    path = workspace / "a.txt"
    path.write_text("before", encoding="utf-8")
    args = {"path": "a.txt", "content": "approved", "expected_sha256": hashlib.sha256(b"before").hexdigest()}
    preview = tools.preview("ryn_file_write", args, context)
    path.write_text("owner edit", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="changed_read_again"):
        tools.execute("ryn_file_write", args, operation_id="op", context=context, expected_preview=preview, cancelled=lambda: False)
    assert path.read_text() == "owner edit"


def test_workspace_escape_and_settings_revocation(local):
    tools, context, _, state = local
    with pytest.raises(AgentToolRejected, match="outside_workspace"):
        tools.path("../outside.txt", context)
    state.transaction(lambda data: data["settings"].update(workspace_root=""))
    with pytest.raises(AgentToolRejected, match="workspace_changed"):
        tools.path("a.txt", context)


def test_copy_move_delete_and_no_recursive_delete(local):
    tools, context, workspace, _ = local
    initial = execute(tools, context, "ryn_file_write", {"path": "a.txt", "content": "a"})
    execute(tools, context, "ryn_file_manage", {"action": "copy", "path": "a.txt", "destination": "b.txt", "expected_sha256": initial["sha256"]}, "copy")
    execute(tools, context, "ryn_file_manage", {"action": "move", "path": "b.txt", "destination": "c.txt", "expected_sha256": initial["sha256"]}, "move")
    execute(tools, context, "ryn_file_manage", {"action": "delete", "path": "c.txt", "expected_sha256": initial["sha256"]}, "delete")
    assert not (workspace / "b.txt").exists() and not (workspace / "c.txt").exists()
    with pytest.raises(AgentToolRejected):
        tools.preview("ryn_file_manage", {"action": "delete", "path": "."}, context)


def test_shell_real_output_exit_code_and_durable_no_replay(local):
    tools, context, workspace, _ = local
    command = "Add-Content counter.txt x; Write-Output 'hello'; exit 7" if os.name == "nt" else "echo x >>counter.txt; echo hello; exit 7"
    args = {"command": command}
    preview = tools.preview("ryn_shell_execute", args, context)
    first = tools.execute("ryn_shell_execute", args, operation_id="command", context=context, expected_preview=preview, cancelled=lambda: False)
    assert first["exit_code"] == 7 and "hello" in first["output"]
    assert first["workspace_files"][0]["path"] == str(workspace / "counter.txt")
    assert first["workspace_files"][0]["created"] is True
    assert tools.execute("ryn_shell_execute", args, operation_id="command", context=context, expected_preview=preview, cancelled=lambda: False) == first
    assert (workspace / "counter.txt").read_text().count("x") == 1


def test_background_process_stop_and_task_isolation(local):
    tools, context, _, _ = local
    command = "Write-Output 'started'; Start-Sleep 30" if os.name == "nt" else "echo started; sleep 30"
    result = execute(tools, context, "ryn_shell_execute", {"command": command, "background": True})
    ident = result["process_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "started" not in tools.processes.read(ident, "task")["output"]:
        time.sleep(0.05)
    assert "started" in tools.processes.read(ident, "task")["output"]
    with pytest.raises(AgentToolRejected, match="not_found"):
        tools.processes.read(ident, "other-task")
    stopped = execute(tools, context, "ryn_process_stop", {"process_id": ident}, "stop")
    assert stopped["running"] is False


def test_background_file_delivery_repeats_without_model_polling_and_survives_restart(local):
    from rynmesh.agent_runtime import workspace_artifacts
    tools, context, workspace, state = local
    stable_id = None
    for number in range(20):
        operation = f"delivery-{number}"
        command = (f"Start-Sleep -Milliseconds 30; Set-Content -LiteralPath 'delivered.md' -Value 'round-{number}' -Encoding utf8"
                   if os.name == "nt" else f"sleep 0.03; printf 'round-{number}' >delivered.md")
        result = execute(tools, context, "ryn_shell_execute", {"command": command, "background": True}, operation)
        tools.processes.get(result["process_id"], "task")["process"].wait(timeout=10)
        deadline = time.monotonic() + 5
        while operation not in state.read()["tasks"]["task"].get("workspace_file_receipts", {}):
            assert time.monotonic() < deadline, f"background delivery missing at round {number}"
            time.sleep(0.02)
        item = workspace_artifacts.listing(state.read()["tasks"]["task"])["files"][0]
        stable_id = stable_id or item["id"]
        assert item["id"] == stable_id and item["status"] == "current"
        assert (workspace / "delivered.md").read_text(encoding="utf-8-sig").strip() == f"round-{number}"
    # A new process registry has no live handles; confirmed output records are encrypted in task history.
    restarted = LocalTools(state, tools.ledger_factory)
    try:
        runtime = AgentRuntime(state, object(), SimpleNamespace(collect_command_artifacts=restarted.collect_command_artifacts), object())
        assert runtime.artifacts("task")["files"][0]["id"] == stable_id
        assert runtime.read_file_artifact("task", stable_id)["bytes"] == (workspace / "delivered.md").read_bytes()
        assert len(state.read()["tasks"]["task"]["workspace_file_receipts"]) == 20
    finally:
        restarted.processes.close()


def test_delivery_publication_failure_does_not_replay_a_confirmed_command(local, monkeypatch):
    tools, context, workspace, state = local
    original = state.transaction
    calls = 0
    def unavailable_once(update):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AgentError("agent_storage_limit")
        return original(update)
    monkeypatch.setattr(state, "transaction", unavailable_once)
    command = "Add-Content counter.txt x" if os.name == "nt" else "echo x >>counter.txt"
    result = execute(tools, context, "ryn_shell_execute", {"command": command}, "publication-retry")
    assert result["exit_code"] == 0
    assert tools.ledger_factory().get("publication-retry")["status"] == "confirmed"
    assert tools.collect_command_artifacts("task") is False
    assert "publication-retry" in state.read()["tasks"]["task"]["workspace_file_receipts"]
    execute(tools, context, "ryn_shell_execute", {"command": command}, "publication-retry")
    assert (workspace / "counter.txt").read_text().count("x") == 1


def test_process_timeout_and_revocation(local):
    tools, context, _, state = local
    command = "Start-Sleep 30" if os.name == "nt" else "sleep 30"
    result = execute(tools, context, "ryn_shell_execute", {"command": command, "timeout_seconds": 1})
    assert result["running"] is False and result["stop_reason"] == "timeout"
    background = execute(tools, context, "ryn_shell_execute", {"command": command, "background": True}, "background")
    state.transaction(lambda data: data["tasks"]["task"].update(status="cancelled"))
    process = tools.processes.get(background["process_id"], "task")["process"]
    process.wait(timeout=3)
    assert process.poll() is not None


def test_desktop_is_explicitly_enabled(local):
    tools, context, _, _ = local
    with pytest.raises(AgentToolRejected, match="desktop_access_required"):
        tools.desktop(context)
    with pytest.raises(AgentToolRejected, match="desktop_access_required"):
        tools.browser(context)


def test_real_browser_dom_fill_click_upload_and_screenshot(local):
    from rynmesh.agent_runtime.local_browser import Browser, find_browser
    if not find_browser():
        pytest.skip("Chromium is not installed")
    tools, context, workspace, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)
    (workspace / "form.html").write_text('<input id="name"><input id="upload" type="file"><button id="submit" onclick="document.querySelector(\'#answer\').textContent=document.querySelector(\'#name\').value">OK</button><p id="answer"></p>', encoding="utf-8")
    (workspace / "upload.txt").write_text("upload", encoding="utf-8")
    opened = execute(tools, context, "ryn_browser_open", {"url": (workspace / "form.html").as_uri()})
    page_id = opened["page_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        if result["elements"]:
            break
        time.sleep(0.05)
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "fill", "selector": "#name", "text": "Ryn 测试"}, "fill")
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#submit"}, "click")
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "upload", "selector": "#upload", "files": ["upload.txt"]}, "upload")
    observed = tools.perform("ryn_browser_read", {"page_id": page_id, "screenshot_path": "browser.png"}, context, lambda: False)
    assert "Ryn 测试" in observed["text"] and (workspace / "browser.png").stat().st_size > 100
    assert tools._browser.evaluate(page_id, "task", "document.querySelector('#upload').files[0].name") == "upload.txt"


def test_runtime_discovers_local_tools_requires_approval_then_verifies(node, monkeypatch, tmp_path):
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "local-agent", peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path),
                             "permission_mode": "confirm_changes", "verify_text_responses": True})
    # Explicit strict permissions and independent verification are still supported.
    fake_upstream(monkeypatch, [call("ryn_discover_tools", json.dumps({"load": ["ryn_file_write", "ryn_file_read"]}), "discover"),
                              call("ryn_file_write", json.dumps({"path": "result.txt", "content": "实际文件"}), "write"),
                              call("ryn_file_read", json.dumps({"path": "result.txt"}), "read"),
                              {"role": "assistant", "content": "已保存并回读 result.txt。"},
                              passed_checker])
    task = runtime.create({"message": "Create result.txt containing 实际文件 and read it back."})
    for _ in range(12):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] == "waiting_approval":
            break
    assert current["status"] == "waiting_approval", current.get("error")
    assert not (tmp_path / "result.txt").exists()
    runtime.action(task["id"], {"action": "approve", "expected_revision": current["revision"], "approval_id": current["pending_approval"]["id"]})
    for _ in range(15):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed" and current["outcome"]["status"] == "verified", current.get("error")
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "实际文件"


@pytest.mark.parametrize("rule_enabled", [True, False])
def test_attempts_rejected_before_execution_do_not_downgrade_a_correct_outcome(node, monkeypatch, tmp_path, rule_enabled):
    from rynmesh.agent_runtime import verification
    if not rule_enabled:
        monkeypatch.setattr(verification, "_rejected_before_execution", lambda receipt: False)
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "rejected-agent", peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path),
                             "permission_mode": "automatic", "allowed_write_tools": ["ryn_file_edit"], "verify_text_responses": True})
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8", newline="")
    before = hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    after = hashlib.sha256(b"alpha\ngamma\n").hexdigest()
    fake_upstream(monkeypatch, [
        call("ryn_discover_tools", json.dumps({"load": ["ryn_file_edit"]}), "discover"),
        call("ryn_file_edit", json.dumps({"path": "notes.txt", "old_text": "beta\n", "new_text": "gamma\n", "expected_sha256": before}), "right"),
        # A later, redundant attempt is rejected before it does anything; it is the latest receipt for this target.
        call("ryn_file_edit", json.dumps({"path": "notes.txt", "old_text": "no such text\n", "new_text": "x\n", "expected_sha256": after}), "wrong"),
        {"role": "assistant", "content": "Changed beta to gamma."},
        passed_checker,
        # Only used when the rule is disabled: the downgraded outcome triggers the single correction continuation.
        {"role": "assistant", "content": "Nothing further to correct."}, passed_checker])
    task = runtime.create({"message": "Change beta to gamma in notes.txt."})
    for _ in range(30):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed", current.get("error")
    rejected = [e for e in current["events"] if e["type"] == "tool_result" and e["result"].get("error") == "agent_local_edit_match_count"]
    assert len(rejected) == 1 and rejected[0]["result"]["not_executed"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"
    if rule_enabled:
        assert current["outcome"]["status"] == "verified", current["outcome"]
    else:
        assert current["outcome"]["status"] != "verified"  # the old behavior this rule corrects


def test_change_review_reports_net_changes_against_the_originals(local):
    tools, context, workspace, _ = local
    original = "def test_a():\n    assert one\n    assert two\n"
    (workspace / "test_keep.py").write_text(original, encoding="utf-8", newline="")
    (workspace / "untouched.py").write_text("x = 1\n", encoding="utf-8", newline="")
    digest = hashlib.sha256((workspace / "test_keep.py").read_bytes()).hexdigest()
    execute(tools, context, "ryn_file_edit", {"path": "test_keep.py", "old_text": "    assert one\n    assert two\n",
            "new_text": "    assert one\n", "expected_sha256": digest}, "drop")
    execute(tools, context, "ryn_file_write", {"path": "new_module.py", "content": "y = 2\n"}, "create")
    review = tools.change_review("task", str(workspace))
    by_path = {item["path"]: item for item in review["files"]}
    assert set(by_path) == {"test_keep.py", "new_module.py"}
    kept = by_path["test_keep.py"]
    assert kept["status"] == "modified" and kept["existed_before"] and kept["test_file"]
    assert kept["removed_lines"] == 1 and kept["removed_check_like_lines"] == ["assert two"]
    assert Path(kept["original_copy"]).read_text(encoding="utf-8") == original
    assert by_path["new_module.py"]["status"] == "created" and "original_copy" not in by_path["new_module.py"]
    # Restoring the original content removes the file from the net change; the digest tracks the state.
    first_digest = review["digest"]
    current = hashlib.sha256((workspace / "test_keep.py").read_bytes()).hexdigest()
    execute(tools, context, "ryn_file_write", {"path": "test_keep.py", "content": original, "expected_sha256": current}, "restore")
    after = tools.change_review("task", str(workspace))
    assert {item["path"] for item in after["files"]} == {"new_module.py"} and after["digest"] != first_digest
    assert tools.change_review("another-task", str(workspace))["files"] == []
    tools.forget_baselines("task")
    assert tools.change_review("task", str(workspace))["files"] == []


def test_model_reviews_and_repairs_its_changes_in_the_same_conversation(node, monkeypatch, tmp_path):
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "review-agent", peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    # Explicit owner authorization for file edits only; shell and every other write tool still need approval.
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path),
                             "permission_mode": "automatic", "allowed_write_tools": ["ryn_file_edit"]})
    original = "def test_a():\n    assert one\n    assert two\n"
    target = tmp_path / "test_keep.py"
    target.write_text(original, encoding="utf-8", newline="")
    weakened = "def test_a():\n    assert one\n"
    seen = {}

    def restore(request):
        observation = json.loads(request["messages"][-1]["content"])
        seen["observed"] = observation["text"]
        assert observation["text"] == weakened
        return call("ryn_file_edit", json.dumps({"path": "test_keep.py", "old_text": "    assert one\n", "new_text": "    assert one\n    assert two\n",
                    "expected_sha256": hashlib.sha256(weakened.encode()).hexdigest()}), "restore")

    fake_upstream(monkeypatch, [
        call("ryn_discover_tools", json.dumps({"load": ["ryn_file_edit", "ryn_file_read"]}), "discover"),
        call("ryn_file_edit", json.dumps({"path": "test_keep.py", "old_text": "    assert one\n    assert two\n", "new_text": "    assert one\n",
                "expected_sha256": hashlib.sha256(original.encode()).hexdigest()}), "weaken"),
        call("ryn_file_read", json.dumps({"path": "test_keep.py"}), "observe"),
        restore,
        {"role": "assistant", "content": "Restored the assertion; no net change."}])
    task = runtime.create({"message": "Fix the bug but do not delete or weaken existing tests."})
    for _ in range(30):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed", current.get("error")
    assert "assert two" not in seen["observed"]
    assert target.read_text(encoding="utf-8") == original
    kinds = [event["type"] for event in current["events"]]
    assert not {"change_review_requested", "verification_started", "approval_requested"} & set(kinds)
    assert current["result"] == "Restored the assertion; no net change."


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop capability")
def test_windows_accessibility_write_and_readback(local):
    import sys

    from rynmesh.agent_runtime.computer.binary import PACKAGE_MANIFEST_PATH
    from rynmesh.agent_runtime.local_processes import shell_command

    if (
        not getattr(sys, "frozen", False)
        and not PACKAGE_MANIFEST_PATH.exists()
        and not PACKAGE_MANIFEST_PATH.is_symlink()
    ):
        pytest.skip("Requires the packaged native worker and computer-use manifest, absent in this source checkout")
    tools, context, workspace, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    title = "Ryn acceptance " + uuid.uuid4().hex[:8]
    command = "Add-Type -AssemblyName System.Windows.Forms; $form=[Windows.Forms.Form]::new(); $form.Text='" + title + "'; $edit=[Windows.Forms.TextBox]::new(); $edit.Name='ryn_test_input'; $form.Controls.Add($edit); [void]$form.ShowDialog()"
    ident = tools.processes.start(shell_command("powershell", command), workspace, "task", 20, tools.allowed(context), lambda: False)
    try:
        deadline = time.monotonic() + 6
        window = None
        while time.monotonic() < deadline:
            observed = tools.perform("ryn_computer_read", {}, context, lambda: False)
            window = next((w for w in observed["windows"] if w["title"] == title), None)
            if window:
                break
            time.sleep(0.1)
        assert window
        observed = tools.perform("ryn_computer_read", {"window_id": window["window_id"]}, context, lambda: False)
        element = next(e for e in observed["elements"] if e["automation_id"] == "ryn_test_input")
        execute(tools, context, "ryn_computer_action", {"window_id": window["window_id"], "action": "set_value", "element_id": element["element_id"], "text": "Ryn 中文输入"}, "desktop")
        readback = tools.perform("ryn_computer_read", {"window_id": window["window_id"]}, context, lambda: False)
        assert any(e["value"] == "Ryn 中文输入" for e in readback["elements"])
        execute(tools, context, "ryn_computer_action", {"window_id": window["window_id"], "action": "key", "keys": ["CTRL", "A"]}, "keys")
        execute(tools, context, "ryn_computer_action", {"window_id": window["window_id"], "action": "type", "text": "Ryn 键盘输入"}, "typing")
        readback = tools.perform("ryn_computer_read", {"window_id": window["window_id"]}, context, lambda: False)
        assert any(e["value"] == "Ryn 键盘输入" for e in readback["elements"])
    finally:
        tools.processes.stop(ident, "task")
