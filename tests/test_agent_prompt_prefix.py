"""Acceptance tests for S12-I1: stable prompt prefix vs volatile progress_state placement."""
from __future__ import annotations

import copy
import json
import os

from test_agent_codex_loop import workstation
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import model_profiles, operation_permissions, prompt
from rynmesh.llm_package import codex_threads, protocol_codecs


def _parse_labelled_json(text: str, label: str) -> dict:
    prefix = label + ": "
    for line in text.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix):])
    raise AssertionError(f"missing labelled JSON line {label!r} in: {text!r}")


def _encoded_bytes(messages: list[dict]) -> bytes:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_t1_generic_prefix_byte_identical_across_criteria_plan_and_auto_shell_writes(tmp_path):
    """T1 (Changes 1, 2, 4, 6, 7, 8, 9): generic route keeps prefix (instructions + history) byte-identical while tail progress_state updates."""
    assert prompt.PROMPT_VERSION == "ryn-agent-loop-v15"
    assert "older operations are omitted from this list, not unapproved" in operation_permissions.GUIDANCE

    # Part A: Two consecutive context snapshots where criteria status, plan steps, and planning_state change.
    runtime, _, _, _ = make_runtime(tmp_path / "part_a")
    task = runtime.create({"message": "Implement feature and verify tests"})
    row_step1 = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    row_step1["criteria"] = [
        {"id": "c1", "kind": "mechanical", "required": True, "status": "pending", "description": "Tests pass"},
    ]
    row_step1["plan"] = {
        "revision": 1,
        "track_execution": True,
        "steps": [
            {"id": "s1", "text": "Edit code", "status": "running", "criterion_ids": ["c1"], "depends_on": []},
        ],
    }
    row_step1["messages"].extend([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_read_1", "type": "function", "function": {"name": "inspect_service", "arguments": '{"service_id":"svc"}'}}],
            "context_epoch": 0,
        },
        {"role": "tool", "tool_call_id": "call_read_1", "content": '{"ok":true}', "context_epoch": 0},
    ])

    ctx1 = runtime._context(row_step1)
    # Prefix is everything before the tail (2 developer instruction messages + 3 history messages = 5 messages;
    # plan_message + progress_state live in tail).
    assert ctx1[-1]["role"] == "user"
    assert "not an owner message" in ctx1[-1]["content"].lower()
    assert "not authorization" in ctx1[-1]["content"].lower()
    progress1 = _parse_labelled_json(ctx1[-1]["content"], "Server progress state")
    assert progress1["criteria"][0]["status"] == "pending"
    assert progress1["plan_revision"] == 1

    # Stable authority layer contains only stable fields, never volatile progress fields.
    ctrl = _parse_labelled_json(ctx1[1]["content"], "Server control state")
    for stable_key in ("prompt_version", "response_language", "model_id", "scope_revision", "inference_context",
                       "local_execution", "os", "local_date", "utc_offset", "file_output_policy"):
        assert stable_key in ctrl
    for volatile_key in ("goal_revision", "original_message_id", "owner_constraint_order",
                         "criteria", "plan_revision", "planning_state", "operation_permissions"):
        assert volatile_key not in ctrl

    row_step2 = copy.deepcopy(row_step1)
    row_step2["criteria"][0]["status"] = "passed"
    row_step2["plan"] = {
        "revision": 2,
        "track_execution": True,
        "steps": [
            {"id": "s1", "text": "Edit code", "status": "done", "criterion_ids": ["c1"], "depends_on": []},
            {"id": "s2", "text": "Run suite", "status": "running", "criterion_ids": ["c1"], "depends_on": ["s1"]},
        ],
    }
    ctx2 = runtime._context(row_step2)

    # First N=5 messages (2 developer instructions + 3 history messages) are byte-identical!
    n_prefix = 5
    assert _encoded_bytes(ctx1[:n_prefix]) == _encoded_bytes(ctx2[:n_prefix])
    assert _encoded_bytes(ctx1[n_prefix:]) != _encoded_bytes(ctx2[n_prefix:])
    progress2 = _parse_labelled_json(ctx2[-1]["content"], "Server progress state")
    assert progress2["criteria"][0]["status"] == "passed"
    assert progress2["plan_revision"] == 2

    # Compaction check (Change 8): after checkpointing, stable authority stays at index 0..1 and full progress_state is rebuilt at tail.
    compacted_row = copy.deepcopy(row_step2)
    compacted_row["context_checkpoint"] = {
        "through": len(compacted_row["messages"]),
        "content": "Summary of prior work.",
        "references": {},
    }
    compacted_ctx = runtime._context(compacted_row)
    assert _encoded_bytes(compacted_ctx[:2]) == _encoded_bytes(ctx2[:2])
    assert _parse_labelled_json(compacted_ctx[-1]["content"], "Server progress state") == progress2

    # Part B: Real two-step run in automatic mode with a shell command (adds a write receipt) -> leading prefix stays byte-identical!
    cmd = "Write-Output ok" if os.name == "nt" else "echo ok"
    part_b_dir = tmp_path / "part_b"
    part_b_dir.mkdir()
    ws_runtime, ws_model, _ = workstation(
        part_b_dir,
        assistant(name="ryn_shell_execute", args={"command": cmd}),
        assistant("Command completed."),
        mode="automatic",
    )
    try:
        ws_task = ws_runtime.create({"message": "Run a quick check in shell"})
        advance(ws_runtime, 3)
        assert len(ws_model.calls) == 2
        req1_msgs = ws_model.calls[0][1]
        req2_msgs = ws_model.calls[1][1]
        # req1_msgs = [dev(SYSTEM), dev(authority), user("Run a quick check in shell"), user(progress_state_1)]
        # First 3 messages (instructions + owner history) must be 100% byte-identical in req2_msgs!
        assert len(req1_msgs) == 4
        assert _encoded_bytes(req1_msgs[:3]) == _encoded_bytes(req2_msgs[:3])
        assert _encoded_bytes(req1_msgs[-1:]) != _encoded_bytes(req2_msgs[-1:])
        tail_prog_2 = _parse_labelled_json(req2_msgs[-1]["content"], "Server progress state")
        assert len(tail_prog_2["operation_permissions"]) == 1
        assert tail_prog_2["operation_permissions"][0]["tool"] == "ryn_shell_execute"
    finally:
        ws_runtime.tools.local.processes.close()


def test_t2_thirty_write_receipts_capped_to_16_with_omitted_14(tmp_path):
    """T2 (Changes 2, 3): 30 write receipts return only the latest 16 in operation_permissions and report total=30, omitted=14."""
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Batch writes"})
    row = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    goal_rev = row["goal"]["revision"]
    scope_rev = row["scope_revision"]

    receipts = []
    for idx in range(30):
        args = {"service_id": f"svc_{idx}"}
        args_sha = operation_permissions.fingerprint(args)
        prev_sha = operation_permissions.fingerprint(None)
        op_id = f"op_{idx:02d}"
        receipts.append({
            "id": f"rcpt_{idx:02d}",
            "operation_id": op_id,
            "tool": "share_service",
            "effect": "write",
            "status": "confirmed",
            "authorization": "automatic",
            "arguments": args,
            "arguments_sha256": args_sha,
            "preview": None,
            "goal_revision": goal_rev,
            "scope_revision": scope_rev,
            "context_epoch": 0,
            "permission_record": {
                "basis": "automatic",
                "operation_id": op_id,
                "arguments_sha256": args_sha,
                "preview_sha256": prev_sha,
                "goal_revision": goal_rev,
                "scope_revision": scope_rev,
                "context_epoch": 0,
                "control_revision": idx + 1,
            },
        })
    row["receipts"] = receipts

    projected = operation_permissions.projection(row)
    assert len(projected) == 16
    assert [item["operation_id"] for item in projected] == [f"op_{idx:02d}" for idx in range(14, 30)]

    prog_line = prompt.progress_state(row)
    assert prog_line.startswith("Server progress state: ")
    prog = _parse_labelled_json(prog_line, "Server progress state")
    assert len(prog["operation_permissions"]) == 16
    assert [item["operation_id"] for item in prog["operation_permissions"]] == [f"op_{idx:02d}" for idx in range(14, 30)]
    assert prog["operation_permissions_counts"] == {"total": 30, "omitted": 14}


def test_t3_codex_cli_route_keeps_progress_state_in_header_and_streams_state_lines(tmp_path):
    """T3 (Changes 3, 4, 5, 7): codex-cli profile resolves progress_placement='header', keeps progress in header, and streams via state_lines."""
    codex_sel = model_profiles.resolve({"id": "local/codex-cli/model/gpt-5", "adapter": "codex_cli"})
    generic_sel = model_profiles.resolve({"id": "local/api-1", "adapter": "openai_compatible"})
    peer_sel = model_profiles.resolve({"id": "peer/p1/svc", "source": "peer", "adapter": "openai_compatible"})
    assert codex_sel["progress_placement"] == "header"
    assert generic_sel["progress_placement"] == "tail"
    assert peer_sel["progress_placement"] == "tail"

    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.catalog.list_models = lambda: [{
        "id": "local/codex-cli",
        "name": "Codex CLI",
        "source": "local_service",
        "adapter": "codex_cli",
        "available": True,
        "agent_supported": True,
        "tool_support": "supported",
        "context_window": 200000,
        "max_output_tokens": 16384,
    }]
    runtime.update_settings({"model_id": "local/codex-cli"})
    task = runtime.create({"message": "Inspect via Codex", "model_id": "local/codex-cli"})
    row1 = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])

    ctx1 = runtime._context(row1)
    # On codex_cli, last message is the owner's user message (no tail progress user message),
    # and Server progress state is inside the developer authority header!
    assert ctx1[-1]["role"] == "user" and ctx1[-1]["content"] == "Inspect via Codex"
    dev_authority_1 = next(m["content"] for m in ctx1 if m["role"] == "developer" and "Server control state: " in m["content"])
    assert "Server progress state: " in dev_authority_1

    # Add 30 write receipts and an active-turn ryn_update_plan call: header mode caps at 16 and preserves legacy plan stabilization
    row2 = copy.deepcopy(row1)
    goal_rev = row2["goal"]["revision"]
    scope_rev = row2["scope_revision"]
    row2["receipts"] = [
        {
            "id": f"r_{i}",
            "operation_id": f"op_{i:02d}",
            "tool": "share_service",
            "effect": "write",
            "status": "confirmed",
            "authorization": "automatic",
            "arguments": {"i": i},
            "arguments_sha256": operation_permissions.fingerprint({"i": i}),
            "preview": None,
            "goal_revision": goal_rev,
            "scope_revision": scope_rev,
            "context_epoch": 0,
        }
        for i in range(30)
    ]
    row2["plan"] = {"revision": 1, "steps": [{"id": "s1", "text": "Step 1", "criterion_ids": [], "depends_on": []}]}
    row2["messages"].extend([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_plan", "type": "function", "function": {"name": "ryn_update_plan", "arguments": "{}"}}],
            "context_epoch": 0,
        },
        {"role": "tool", "tool_call_id": "call_plan", "content": '{"updated":true}', "context_epoch": 0},
    ])
    ctx2 = runtime._context(row2)
    assert not any("Untrusted model plan proposal" in (m.get("content") or "") for m in ctx2)
    dev_authority_2 = next(m["content"] for m in ctx2 if m["role"] == "developer" and "Server control state: " in m["content"])
    changed = codex_threads.state_lines(dev_authority_1, dev_authority_2)
    assert changed is not None and len(changed) == 1
    assert changed[0].startswith("Server progress state: ")
    parsed_prog = _parse_labelled_json(changed[0], "Server progress state")
    assert len(parsed_prog["operation_permissions"]) == 16
    assert parsed_prog["operation_permissions_counts"] == {"total": 30, "omitted": 14}


def test_t4_codex_thread_reuse_with_header_instruction_layers(tmp_path):
    """T4 (Changes 1, 3, 4, 5): codex_threads Pool leases existing Session across progress_state updates in header mode."""
    class AliveRpc:
        def alive(self):
            return True

        def settle(self):
            return True

        def cpu_seconds(self, busy):
            return busy

        def close(self):
            self.closed = True

    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Check status"})
    row1 = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    notes = model_profiles.resolve({"id": "local/codex-cli", "adapter": "codex_cli"})["instructions"]
    layers1 = prompt.instruction_layers(row1, model_notes=notes, progress_placement="header")
    msgs1 = [*layers1, {"role": "user", "content": "Check status"}]

    pool = codex_threads.Pool()
    win = codex_threads.window_key(task["id"])
    key = ("codex", "gpt-5", "medium", "local", win)
    conv1 = codex_threads.Conversation(msgs1, {}, scope={"requester": "local", "window": win})
    sess = pool.session(AliveRpc(), key)
    conv1.session = sess
    call_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "inspect_service", "arguments": "{}"}}],
    }
    conv1.finish(call_msg)

    row2 = copy.deepcopy(row1)
    row2["criteria"] = [{"id": "c1", "kind": "mechanical", "required": True, "status": "passed"}]
    layers2 = prompt.instruction_layers(row2, model_notes=notes, progress_placement="header")
    msgs2 = [*layers2, {"role": "user", "content": "Check status"}, call_msg, {"role": "tool", "tool_call_id": "call_1", "content": "ok"}]
    conv2 = codex_threads.Conversation(msgs2, {}, scope={"requester": "local", "window": win})
    leased = pool.lease(key, conv2)
    assert leased is sess
    assert conv2.update is not None
    assert len(conv2.update["system_state_updates"]) == 1
    assert conv2.update["system_state_updates"][0].startswith("Server progress state: ")


def test_t5_anthropic_messages_encodes_tail_progress_without_non_leading_developer(tmp_path):
    """T5 (Changes 4, 5, 6): Anthropic route places progress_state as tail user message and encodes cleanly via protocol_codecs."""
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.catalog.list_models = lambda: [{
        "id": "local/anthropic-claude",
        "name": "Claude",
        "source": "local_api",
        "adapter": "anthropic_messages",
        "available": True,
        "agent_supported": True,
        "tool_support": "supported",
        "context_window": 200000,
        "max_output_tokens": 16384,
    }]
    runtime.update_settings({"model_id": "local/anthropic-claude"})
    task = runtime.create({"message": "Read config and update plan", "model_id": "local/anthropic-claude"})
    row = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    row["plan"] = {
        "revision": 1,
        "track_execution": True,
        "steps": [{"id": "s1", "text": "Read config", "status": "running", "criterion_ids": [], "depends_on": []}],
    }
    row["messages"].extend([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_cfg", "type": "function", "function": {"name": "inspect_service", "arguments": '{"service_id":"cfg"}'}}],
            "context_epoch": 0,
        },
        {"role": "tool", "tool_call_id": "call_cfg", "content": '{"port":8080}', "context_epoch": 0},
    ])

    ctx = runtime._context(row)
    # Verify no developer or system message appears after the first non-system/developer message.
    seen_non_control = False
    for msg in ctx:
        if msg["role"] in {"system", "developer"}:
            assert not seen_non_control, f"non-leading {msg['role']} message found"
        else:
            seen_non_control = True

    encoded_req = protocol_codecs.make_request(
        {"messages": ctx, "max_tokens": 1024, "tools": runtime._specs(row)},
        protocol="anthropic_messages",
        model="claude-sonnet-4-5",
        scope="a" * 64,
    )
    assert encoded_req["system"]
    assert encoded_req["messages"][-1]["role"] == "user"
    last_blocks_text = "\n".join(
        block["text"] for block in encoded_req["messages"][-1]["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    )
    assert "Server progress state: " in last_blocks_text
