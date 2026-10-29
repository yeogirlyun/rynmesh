"""Tests for KV-cache prefix stability, post-compaction result retrieval, and cached_tokens accounting."""
from __future__ import annotations

import copy
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget, memory, projects, results, tool_menu
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentError, encoded
from rynmesh.llm_package.adapters import (
    OpenAICompatibleAdapter,
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
    assert b["lifetime"]["total_tokens"] == 1050
    # Direct API route discounts cached input tokens at 10%: (1000 - 896) + ceil(896 * 0.1) + 50 = 104 + 90 + 50 = 244
    assert b["lifetime"]["charged_tokens"] == 244

    # Local catalog model with provider_peer_id populated in model_destination still gets 0.1 weight
    local_dest_row = {
        "limits": {"max_seconds": 30},
        "steps": 0,
        "elapsed_seconds": 0,
        "model_id": "local/src-fc320104c4ec4113-7d963f65d1ed",
        "model_destination": {
            "catalog_model_id": "local/src-fc320104c4ec4113-7d963f65d1ed",
            "provider_peer_id": "local-self-peer-id",
            "source_id": "src-fc320104c4ec4113",
            "destination_revision": "rev1",
        },
    }
    local_res = budget.reserve(local_dest_row, "model", "main", input_bound=1200, output_bound=100)
    budget.settle(local_dest_row, local_res["id"], usage={"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 896}, elapsed_seconds=1.0)
    local_b = budget.public(local_dest_row)
    assert local_b["lifetime"]["total_tokens"] == 1050
    assert local_b["lifetime"]["charged_tokens"] == 244

    # Peer route does NOT discount cached input tokens (weight = 1.0)
    peer_row = {"limits": {"max_seconds": 30}, "steps": 0, "elapsed_seconds": 0, "model_id": "peer/node1:gpt-5"}
    peer_res = budget.reserve(peer_row, "model", "main", input_bound=1200, output_bound=100)
    budget.settle(peer_row, peer_res["id"], usage={"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 896}, elapsed_seconds=1.0)
    peer_b = budget.public(peer_row)
    assert peer_b["lifetime"]["total_tokens"] == 1050
    assert peer_b["lifetime"]["charged_tokens"] == 1050


def test_proactive_soft_landing_wrap_up_and_user_continuation_auto_renewal(tmp_path):
    # Step 1 calls a tool and uses enough tokens that only 1 turn's worth of token budget remains.
    # Step 2 should proactively enter soft-landing wrap-up, skip any tool_calls returned by the model,
    # pair them with not_executed tool results, and complete with outcome status 'budget_limited'.
    step1_resp = assistant(name="inspect_service", args={"service_id": "svc"})
    step1_resp.update(
        usage={"input_tokens": 7000, "output_tokens": 500},
        reported_usage={"input_tokens": 7000, "output_tokens": 500},
    )
    step2_resp = assistant("Here is the progress summary so far.", name="inspect_service", args={"service_id": "svc2"})
    step2_resp.update(
        usage={"input_tokens": 1000, "output_tokens": 100},
        reported_usage={"input_tokens": 1000, "output_tokens": 100},
    )
    step3_resp = assistant("Completed after user continuation.")
    step3_resp.update(
        usage={"input_tokens": 1000, "output_tokens": 100},
        reported_usage={"input_tokens": 1000, "output_tokens": 100},
    )

    runtime, model, tools, _ = make_runtime(tmp_path, step1_resp, step2_resp, step3_resp)
    row = runtime.create({"message": "Inspect services", "budget_limits": {"max_total_tokens": 25000}})
    advance(runtime, 4)
    wrapped = runtime.get(row["id"])
    assert wrapped["status"] == "completed"
    assert wrapped["outcome"]["status"] == "budget_limited"
    assert "Here is the progress summary so far." in wrapped["result"]
    # Only step 1's tool call was executed; step 2's tool call was skipped during wrap-up!
    assert len(tools.calls) == 1
    assert any(ev.get("type") == "budget_wrap_up" and ev.get("skipped_tool_calls") == 1 for ev in wrapped["events"])

    # Now user sends "继续" via add_message -> budget limit is auto-extended without resetting lifetime usage
    resumed = runtime.add_message(row["id"], {"message": "继续", "expected_revision": wrapped["revision"]})
    assert resumed["budget"]["limits"]["max_total_tokens"] > 25000
    assert resumed["budget"]["lifetime"]["charged_tokens"] == 8600
    assert any(ev.get("type") == "budget_auto_extended" for ev in resumed["events"])
    advance(runtime, 4)
    final = runtime.get(row["id"])
    assert final["status"] == "completed"
    assert final["result"] == "Completed after user continuation."
    assert final["budget"]["lifetime"]["charged_tokens"] == 9700


def test_code_mode_tab_ax_decorator_and_output_dedup(tmp_path):
    from types import SimpleNamespace
    from rynmesh.agent_runtime import code_mode

    runner = code_mode.CodeModeRunner(SimpleNamespace(local=None, specs=lambda: [], metadata=lambda _n: {}))
    script = """
    const clicked = [];
    const fakeTab = {
      ax: {
        get: async (what) => "10 button Login\\n20 text field Username",
        click: async (target, opts) => { clicked.push(target); return { ok: true, target }; },
        scroll: async (target, dir, pages) => ({ target, dir, pages }),
        drag: async (from, to) => ({ from, to }),
      }
    };
    const dec = buildTabDecorator();
    dec(fakeTab);
    const st = await fakeTab.ax.getState();
    await fakeTab.ax.click({ x: 688, y: 122 });
    await fakeTab.ax.action({ kind: "click", point: { x: 100, y: 200 } });
    await fakeTab.ax.action({ kind: "click", x: 300, y: 400 });
    const dragRes = await fakeTab.ax.action({ kind: "drag", from: { x: 1, y: 2 }, to: { x: 3, y: 4 } });
    text("Hello once");
    return {
      sliceOk: st.slice(0, 9) === "10 button",
      includesOk: st.includes("Login"),
      statePropOk: st.state.includes("Username"),
      clicked,
      dragRes,
    };
    """
    res = runner.execute(
        {"code": script},
        operation_id="op_ax_test",
        cancelled=lambda: False,
        context={"task_id": "t_ax", "context_epoch": 0, "scope_revision": 1},
    )
    assert res["status"] == "completed", res
    rv = res["return_value"]
    assert rv["sliceOk"] is True
    assert rv["includesOk"] is True
    assert rv["statePropOk"] is True
    assert rv["clicked"] == [[688, 122], [100, 200], [300, 400]]
    assert rv["dragRes"] == {"from": [1, 2], "to": [3, 4]}
    assert res["output"] == "Hello once"
    assert res["content_items"] == []


def test_retryable_transport_failure_does_not_burn_393k_output_reservation_and_heals_legacy(tmp_path):
    row = {
        "limits": {"max_seconds": 30},
        "steps": 0,
        "elapsed_seconds": 0,
        "model_id": "local/src-fc320104c4ec4113-7d963f65d1ed",
    }
    res1 = budget.reserve(row, "model", "main", input_bound=28000, output_bound=393216)
    budget.settle(row, res1["id"], usage=None, elapsed_seconds=10.0, retryable_failure=True)
    pub = budget.public(row)
    assert pub["lifetime"]["charged_tokens"] == 64

    # Also verify initialize() heals existing settled records that had output_reservation > 32768 with None usage
    legacy_row = {
        "limits": {"max_seconds": 30},
        "steps": 1,
        "elapsed_seconds": 10,
        "budget": {
            "limits": dict(budget.DEFAULT_LIMITS),
            "round": 2,
            "legacy_usage_unknown": False,
            "legacy_model_calls": 0,
            "legacy_tool_calls": 0,
            "legacy_seconds": 0.0,
            "records": [
                {
                    "id": "usage_1",
                    "kind": "model",
                    "stage": "main",
                    "round": 1,
                    "status": "settled",
                    "input_tokens": None,
                    "output_tokens": None,
                    "charged_tokens": 421216,
                    "charged_seconds": 10.0,
                    "elapsed_seconds": 10.0,
                    "input_reservation": 28000,
                    "output_reservation": 393216,
                }
            ],
        },
    }
    healed = budget.public(legacy_row)
    assert healed["lifetime"]["charged_tokens"] == 64
