"""Tests for KV-cache prefix stability, post-compaction result retrieval, and cached_tokens accounting."""
from __future__ import annotations

import copy
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import memory, projects, results, tool_menu
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.adapters import (
    actual_usage,
    cached_input_tokens,
)
from rynmesh.llm_package.api_formats import convert_response
from rynmesh.llm_package.chat import completion
from rynmesh.llm_package.protocol_codecs import MESSAGES, RESPONSES, StreamDecoder


def test_memory_query_text_stays_stable_across_many_tool_exchanges(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)

    def seed_and_check(data):
        dest = {
            "catalog_model_id": "local:test",
            "provider_peer_id": "self",
            "source_id": "local",
            "destination_revision": "rev-1",
        }
        memory.update_memory_settings(
            data,
            {"capture_enabled": True, "use_enabled": True, "extraction_destination": dest},
        )
        src = memory.build_source_ref(
            source_type="manual_owner",
            content="Owner uses Fujifilm X-T5 camera body",
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "Camera body is Fujifilm X-T5",
                "sources": [src],
                "exposure_policy": {"mode": "all_selected", "allowed_destinations": []},
            },
            origin="manual",
            client_request_id="req-mem-1",
        )

    runtime.store.transaction(seed_and_check)
    task = runtime.create({"message": "Recommend camera settings for my equipment"})
    row = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    data = runtime.store.read()

    sel_initial = memory.select_memories_for_task(data, row)
    assert len(sel_initial["selected"]) == 1

    # Append 8 assistant/tool exchanges in the same turn (> 6 messages).
    for idx in range(8):
        cid = f"call_{idx}"
        row["messages"].append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": "inspect_service", "arguments": '{"service_id":"A"}'},
                    }
                ],
                "context_epoch": 0,
            }
        )
        row["messages"].append(
            {
                "role": "tool",
                "tool_call_id": cid,
                "content": '{"status":"ok"}',
                "context_epoch": 0,
            }
        )

    sel_after_tools = memory.select_memories_for_task(data, row)
    assert sel_after_tools["selected"] == sel_initial["selected"]


def test_plan_update_in_uncompacted_history_preserves_prefix_until_compacted(tmp_path):
    runtime, model, _, _ = make_runtime(
        tmp_path,
        assistant(
            name="ryn_update_plan",
            args={
                "goal_revision": 1,
                "steps": [{"id": "s1", "text": "Inspect service A", "criterion_ids": [], "depends_on": []}],
            },
        ),
        assistant("Completed inspection."),
    )
    task = runtime.create({"message": "Plan and inspect service A"})
    advance(runtime, 3)

    assert len(model.calls) == 2
    first_messages = model.calls[0][1]
    second_messages = model.calls[1][1]
    # While ryn_update_plan is in active uncompacted history, the leading developer
    # instructions and user prefix remain byte-identical across step 1 and step 2.
    assert second_messages[:3] == first_messages[:3]

    # Once compacted past the ryn_update_plan exchange, plan_message is materialized in prefix.
    saved = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])
    saved["context_checkpoint"] = {
        "through": len(saved["messages"]),
        "content": "Compacted summary.",
        "references": {},
    }
    compacted_ctx = runtime._context(saved)
    assert any(
        "Untrusted model plan proposal" in (m.get("content") or "") and "Inspect service A" in (m.get("content") or "")
        for m in compacted_ctx
    )


def test_tool_menu_keeps_loaded_schemas_across_goal_revision_on_additive_window():
    business = [
        {
            "type": "function",
            "function": {
                "name": f"custom_tool_{i}",
                "description": f"Tool {i} description " + ("x" * 160),
                "parameters": {
                    "type": "object",
                    "properties": {"arg": {"type": "string"}},
                    "required": ["arg"],
                    "additionalProperties": False,
                },
            },
        }
        for i in range(40)
    ]
    _, state1 = tool_menu.project(
        business,
        [],
        None,
        max_context_bytes=64000,
        authority_bytes=2000,
        epoch=0,
        goal_revision=1,
    )
    _, state_loaded = tool_menu.discover(
        business,
        [],
        lambda n: {"effect": "read", "title": n},
        state1,
        {"load": ["custom_tool_5"]},
        capacity_bytes=50000,
    )
    assert "custom_tool_5" in state_loaded["loaded_names"]

    # Follow-up user turn increments goal_revision on an additive window (>= 48000):
    # loaded_names stays intact so the tools prefix is not busted.
    _, state_g2 = tool_menu.project(
        business,
        [],
        state_loaded,
        max_context_bytes=64000,
        authority_bytes=2000,
        epoch=0,
        goal_revision=2,
    )
    assert "custom_tool_5" in state_g2["loaded_names"]

    # On a small non-additive window (< 48000), goal_revision change still clears loaded_names.
    _, state_small_g2 = tool_menu.project(
        business,
        [],
        state_loaded,
        max_context_bytes=12000,
        authority_bytes=2000,
        epoch=0,
        goal_revision=2,
    )
    assert state_small_g2["loaded_names"] == []


def test_project_read_progress_excludes_uncompacted_call_ids(tmp_path):
    row = {
        "project": {"id": "proj1", "name": "Demo", "path": str(tmp_path)},
        "context_checkpoint": {"through": 2, "content": "Compacted checkpoint."},
        "context_epoch": 0,
        "events": [
            {
                "type": "tool_result",
                "tool": "ryn_file_read",
                "call_id": "call_old",
                "context_epoch": 0,
                "result": {
                    "path": str(tmp_path / "a.py"),
                    "start_line": 1,
                    "end_line": 20,
                    "total_lines": 50,
                    "sha256": "a" * 64,
                },
            },
            {
                "type": "tool_result",
                "tool": "ryn_file_read",
                "call_id": "call_recent",
                "context_epoch": 0,
                "result": {
                    "path": str(tmp_path / "b.py"),
                    "start_line": 1,
                    "end_line": 50,
                    "total_lines": 100,
                    "sha256": "b" * 64,
                },
            },
        ],
    }
    filtered = projects.read_progress(row, exclude_call_ids={"call_recent"})
    assert "a.py" in filtered
    assert "b.py" not in filtered

    full = projects.read_progress(row)
    assert "a.py" in full and "b.py" in full


def test_post_compaction_result_retrieval_for_compacted_calls_and_result_refs(tmp_path):
    runtime, model, tools, _ = make_runtime(
        tmp_path,
        assistant(name="inspect_service", args={"service_id": "svc-alpha"}, call_id="call_hist_1"),
        assistant("Done."),
    )
    task = runtime.create({"message": "Inspect svc-alpha"})
    advance(runtime, 2)

    # Simulate a large result reference + a checkpoint that compacted call_hist_1
    def install_checkpoint(data):
        row = data["tasks"][task["id"]]
        ref = results.save(
            runtime.store,
            row,
            {"call_id": "call_big", "name": "inspect_service", "operation_id": "op_big"},
            {"service": "svc-big", "payload": "LARGE_DATA_" * 200},
        )
        row.setdefault("result_refs", []).append(ref)
        row["context_checkpoint"] = {
            "through": len(row["messages"]),
            "content": "Summary of earlier inspection.",
            "references": {},
        }
        return ref

    ref = runtime.store.transaction(install_checkpoint)
    selected = runtime.store.read()["tasks"][task["id"]]

    # 1. checkpoint_note pins the active result_<hex> reference
    ctx = runtime._context(selected)
    checkpoint_msg = next(
        m["content"] for m in ctx if (m.get("content") or "").startswith("Historical checkpoint")
    )
    assert ref["id"] in checkpoint_msg

    # 2. ryn_read_result can list compacted tool results via result_id="checkpoint_index"
    index_page = results.page(runtime.store, selected, "checkpoint_index", 0, 2048, provider=True)
    index_data = json.loads(index_page["content"])
    assert any(item["call_id"] == "call_hist_1" and item["tool"] == "inspect_service" for item in index_data["compacted_tool_results"])
    assert any(item["result_id"] == ref["id"] for item in index_data["result_refs"])

    # 3. ryn_read_result can page the exact compacted tool result by call_id
    call_page = results.page(runtime.store, selected, "call_hist_1", 0, 2048, provider=True)
    assert "svc-alpha" in call_page["content"]

    # 4. Epoch isolation still rejects reading compacted calls across a new context_epoch
    selected_new_epoch = {**selected, "context_epoch": 1}
    selected_new_epoch["messages"] = [
        {**m, "context_epoch": 0} for m in selected["messages"]
    ]
    with pytest.raises(AgentError, match="agent_result_(scope_changed|not_found)"):
        results.page(runtime.store, selected_new_epoch, "call_hist_1", 0, 2048, provider=True)


def test_cached_tokens_extraction_across_deepseek_openai_anthropic_and_budget(tmp_path):
    # DeepSeek format: prompt_cache_hit_tokens
    assert cached_input_tokens({"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 896}) == 896
    # OpenAI Chat Completions format: prompt_tokens_details.cached_tokens
    assert cached_input_tokens({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 768}}) == 768
    # OpenAI Responses format: input_tokens_details.cached_tokens
    assert cached_input_tokens({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 512}}) == 512
    # Anthropic Messages format: cache_read_input_tokens
    assert cached_input_tokens({"input_tokens": 200, "cache_read_input_tokens": 640}) == 640
    # actual_usage contract remains strictly 2 keys
    res = {"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 896, "reported_usage": {"input_tokens": 1000, "output_tokens": 50}}
    assert actual_usage(res) == {"input_tokens": 1000, "output_tokens": 50}

    # StreamDecoder tracks cached_tokens for both RESPONSES and MESSAGES without altering decoder.usage keys
    scope = "a" * 64
    resp_dec = StreamDecoder(RESPONSES, "gpt-5", scope)
    resp_dec.feed({"type": "response.created", "response": {"id": "r1", "status": "in_progress"}})
    resp_dec.feed({"type": "response.output_item.added", "output_index": 0, "item": {"id": "m1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}})
    resp_dec.feed({"type": "response.output_text.delta", "delta": "Hello"})
    resp_dec.feed({"type": "response.output_item.done", "output_index": 0, "item": {"id": "m1", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Hello"}]}})
    resp_dec.feed({
        "type": "response.completed",
        "response": {
            "id": "r1",
            "status": "completed",
            "output": [{"id": "m1", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Hello"}]}],
            "usage": {"input_tokens": 500, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 384}},
        },
    })
    assert resp_dec.result()["usage"] == {"prompt_tokens": 500, "completion_tokens": 10}
    assert resp_dec.cached_tokens == 384

    msg_dec = StreamDecoder(MESSAGES, "claude-sonnet", scope)
    msg_dec.feed({
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 400},
        },
    })
    msg_dec.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Hi"}})
    msg_dec.feed({"type": "content_block_stop", "index": 0})
    msg_dec.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
    msg_dec.feed({"type": "message_stop"})
    assert msg_dec.result()["usage"] == {"prompt_tokens": 500, "completion_tokens": 5}
    assert msg_dec.cached_tokens == 400

    # chat.completion and api_formats.convert_response propagate cached_tokens
    comp = completion(res, "deepseek-chat", "req1")
    assert comp["usage"]["prompt_tokens_details"] == {"cached_tokens": 896}
    as_responses = convert_response(comp, "responses")
    assert as_responses["usage"]["input_tokens_details"]["cached_tokens"] == 896
    as_messages = convert_response(comp, "messages")
    assert as_messages["usage"]["cache_read_input_tokens"] == 896

    # AgentModelBridge + AgentRuntime + budget accounting
    normalized = AgentModelBridge._response(
        {
            "text": "Done",
            "message": {"role": "assistant", "content": "Done"},
            "finish_reason": "stop",
            "input_tokens": 1000,
            "output_tokens": 50,
            "reported_usage": {"input_tokens": 1000, "output_tokens": 50},
            "cached_tokens": 896,
        },
        {"source": "local_api"},
        [],
        {},
    )
    assert normalized["cached_tokens"] == 896
    runtime, _, _, _ = make_runtime(tmp_path, normalized)
    task = runtime.create({"message": "Test cached tokens"})
    advance(runtime)
    b = runtime.get(task["id"])["budget"]
    assert b["lifetime"]["cached_input_tokens"] == 896
    assert b["lifetime"]["known_cached_input_tokens"] == 896
    assert b["lifetime"]["charged_tokens"] == 1050
