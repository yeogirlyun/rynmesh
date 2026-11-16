"""S02-I21: Continue oversized model answers instead of failing with agent_output_limit."""
from __future__ import annotations

import threading

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.turn_state import TurnDecision, decide_turn


def test_t1_decide_turn_length_continuations_limit_6_completes_and_5_continues():
    d5 = decide_turn(finish_reason="length", length_continuations=5)
    assert d5 == TurnDecision("continue", "output_truncated")
    assert d5.needs_follow_up and not d5.end_turn

    d6 = decide_turn(finish_reason="length", length_continuations=6)
    assert d6 == TurnDecision("complete", "output_truncated_limit")
    assert d6.end_turn and not d6.needs_follow_up


def test_t2_seven_empty_length_responses_stop_with_agent_output_truncated(tmp_path):
    responses = [
        {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
        for _ in range(7)
    ]
    runtime, model, _, _ = make_runtime(tmp_path, *responses)
    created = runtime.create({"message": "Write report"})
    advance(runtime, 20)
    done = runtime.get(created["id"])
    assert len(model.calls) == 7
    assert done["status"] == "failed"
    assert done["error"] == "agent_output_truncated"
    cont_events = [ev for ev in done["events"] if ev.get("type") == "response_continuation_requested"]
    assert len(cont_events) == 6


def test_t3_seven_non_empty_length_responses_complete_with_continuation_exhausted_event(tmp_path):
    responses = [
        {"message": {"role": "assistant", "content": f"Part-{idx};"}, "finish_reason": "length"}
        for idx in range(1, 8)
    ]
    runtime, model, _, _ = make_runtime(tmp_path, *responses)
    created = runtime.create({"message": "Write multi-part summary"})
    advance(runtime, 20)
    done = runtime.get(created["id"])
    assert len(model.calls) == 7
    assert done["status"] == "completed"
    assert done["result"] == "".join(f"Part-{idx};" for idx in range(1, 8))
    exhausted = [ev for ev in done["events"] if ev.get("type") == "response_continuation_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["count"] == 6


def test_t4_streamed_30kb_chinese_and_stream_overflow_continue_to_complete_intact(tmp_path, monkeypatch):
    part1 = "中" * 11_000  # 33,000 UTF-8 bytes (> default max_output_chars 20,000)
    part2 = "文" * 4_000   # 12,000 UTF-8 bytes
    runtime, model, _, _ = make_runtime(tmp_path)
    turn_idx = [0]

    def complete_30kb(model_id, messages, tool_specs, cancelled, on_event):
        model.calls.append((model_id, messages, tool_specs))
        turn_idx[0] += 1
        if turn_idx[0] == 1:
            for offset in range(0, len(part1), 2000):
                on_event({"type": "text_delta", "text": part1[offset : offset + 2000]})
            return {"message": {"role": "assistant", "content": part1}, "finish_reason": "length"}
        for offset in range(0, len(part2), 2000):
            on_event({"type": "text_delta", "text": part2[offset : offset + 2000]})
        return {"message": {"role": "assistant", "content": part2}, "finish_reason": "stop"}

    model.complete = complete_30kb
    created = runtime.create({"message": "生成长篇中文报告"})
    advance(runtime, 10)
    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    assert done["result"] == part1 + part2

    # Also verify _StreamOverflow mid-stream when streamed bytes exceed answer_limit(row)
    monkeypatch.setattr(output_policy, "answer_limit", lambda _row: 3000)
    runtime2, model2, _, _ = make_runtime(tmp_path / "overflow")
    step_idx = [0]

    def complete_overflow(model_id, messages, tool_specs, cancelled, on_event):
        model2.calls.append((model_id, messages, tool_specs))
        step_idx[0] += 1
        if step_idx[0] == 1:
            # 1200 Chinese chars = 3600 UTF-8 bytes > 3000 answer_limit -> raises _StreamOverflow
            on_event({"type": "text_delta", "text": "汉" * 1200})
            raise AssertionError("stream overflow should terminate reading via _StreamOverflow")
        return {"message": {"role": "assistant", "content": "收尾完成"}, "finish_reason": "stop"}

    model2.complete = complete_overflow
    created2 = runtime2.create({"message": "流式超限续写"})
    advance(runtime2, 10)
    done2 = runtime2.get(created2["id"])
    assert done2["status"] == "completed"
    assert done2["result"] == ("汉" * 1000) + "收尾完成"


def test_t5_overflow_with_tool_calls_discards_calls_without_executing(tmp_path, monkeypatch):
    monkeypatch.setattr(output_policy, "answer_limit", lambda _row: 1500)
    runtime, model, tools, _ = make_runtime(tmp_path)
    step_idx = [0]

    def complete_stream_with_tool(model_id, messages, tool_specs, cancelled, on_event):
        model.calls.append((model_id, messages, tool_specs))
        step_idx[0] += 1
        if step_idx[0] == 1:
            on_event(
                {
                    "type": "tool_call_complete",
                    "index": 0,
                    "call": {
                        "id": "call_discard_stream",
                        "type": "function",
                        "function": {"name": "share_service", "arguments": '{"service_id":"svc1"}'},
                    },
                }
            )
            on_event({"type": "text_delta", "text": "超" * 600})  # 1800 bytes > 1500
            return {
                "message": {
                    "role": "assistant",
                    "content": "超" * 600,
                    "tool_calls": [
                        {
                            "id": "call_discard_stream",
                            "type": "function",
                            "function": {"name": "share_service", "arguments": '{"service_id":"svc1"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        return {"message": {"role": "assistant", "content": "后续纯文本完成"}, "finish_reason": "stop"}

    model.complete = complete_stream_with_tool
    created = runtime.create({"message": "Stream overflow with tool call"})
    advance(runtime, 10)
    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    assert tools.calls == [] and not tools.shared
    assert done["result"] == ("超" * 500) + "后续纯文本完成"

    # Non-streaming overflow with tool_calls also discards tool_calls and continues via length
    runtime_ns, _, tools_ns, _ = make_runtime(
        tmp_path / "nonstream",
        assistant("非" * 600, name="inspect_service", args={"service_id": "svc1"}),
        assistant("非流式续写完成"),
    )
    created_ns = runtime_ns.create({"message": "Non-stream overflow with tool call"})
    advance(runtime_ns, 10)
    done_ns = runtime_ns.get(created_ns["id"])
    assert done_ns["status"] == "completed"
    assert tools_ns.calls == []
    assert done_ns["result"] == ("非" * 500) + "非流式续写完成"


def test_t6_continuation_over_1_mib_writes_full_file_artifact_and_64k_preview(tmp_path):
    chunk = "甲" * 80_000  # 240,000 UTF-8 bytes per chunk (<= 256 KiB per response)
    # 5 chunks = 1,200,000 UTF-8 bytes (> 1 MiB ANSWER_TOTAL_LIMIT = 1,048,576 bytes)
    responses = [
        *[{"message": {"role": "assistant", "content": chunk}, "finish_reason": "length"} for _ in range(4)],
        {"message": {"role": "assistant", "content": chunk}, "finish_reason": "stop"},
    ]
    expected_full = chunk * 5
    assert len(expected_full.encode("utf-8")) > output_policy.ANSWER_TOTAL_LIMIT

    runtime, _, _, _ = make_runtime(tmp_path, *responses)
    runtime.catalog.list_models = lambda: [
        {
            "id": "local:test",
            "name": "Test",
            "source": "local_api",
            "available": True,
            "agent_supported": True,
            "context_window": 1_048_576,
        }
    ]
    created = runtime.create({"message": "Produce >1 MiB full compilation"})
    advance(runtime, 20)
    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    assert "完整内容见附件" in done["result"]
    preview_part = done["result"].split("\n\n完整内容见附件")[0]
    assert 64 * 1024 - 4 <= len(preview_part.encode("utf-8")) <= 64 * 1024
    assert expected_full.startswith(preview_part)

    listing = runtime.artifacts(created["id"])
    assert len(listing["files"]) == 1
    file_item = listing["files"][0]
    assert file_item["bytes"] == len(expected_full.encode("utf-8"))
    read_back = runtime.read_file_artifact(created["id"], file_item["id"])
    assert read_back["bytes"].decode("utf-8") == expected_full


def test_t7_storage_reservation_uses_answer_limit_for_model_steps(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow_model(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return assistant("Done.")

    model.complete = slow_model
    created = runtime.create({"message": "Check model storage reservation"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(5)
    try:
        data = runtime.store.read()
        row = data["tasks"][created["id"]]
        reservation = data["storage_reservations"][created["id"]]
        expected_bytes = output_policy.storage_growth(output_policy.answer_limit(row))
        assert expected_bytes > output_policy.storage_growth(row["limits"]["max_output_chars"])
        assert reservation["bytes"] == expected_bytes
    finally:
        release.set()
        worker.join(5)
