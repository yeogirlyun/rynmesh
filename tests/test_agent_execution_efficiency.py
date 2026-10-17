"""Stream coalescing preserves authoritative output and cancellation boundaries."""
from types import SimpleNamespace

import pytest
from test_agent_runtime import assistant, make_runtime

from rynmesh.agent_runtime.store import AgentStore


def simulated_clock(monkeypatch, runtime):
    clock = [1000.0]
    timer = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr("rynmesh.agent_runtime.loop.time", timer)
    monkeypatch.setattr("rynmesh.agent_runtime.runtime.time", timer)
    transaction = runtime.store.transaction

    def persist(mutate):
        result = transaction(mutate)
        clock[0] += 0.05  # deterministic encrypted-commit latency
        return result

    monkeypatch.setattr(runtime.store, "transaction", persist)
    return clock


@pytest.mark.parametrize(("count", "interval", "max_commits"), [(100, 0.3, 35), (100, 0.001, 3)])
def test_small_stream_chunks_are_coalesced_without_losing_first_or_final_text(
        tmp_path, monkeypatch, count, interval, max_commits):
    runtime, model, _, key = make_runtime(tmp_path)
    created = runtime.create({"message": "Inspect a bounded set of facts."})
    clock = simulated_clock(monkeypatch, runtime)
    pieces = [f"片段{index} " for index in range(count)]
    text = "".join(pieces)

    def streaming(model_id, messages, specs, cancelled, on_event):
        for index, piece in enumerate(pieces):
            clock[0] += interval
            on_event({"type": "text_delta", "text": piece})
            if index == 0:
                # The first visible chunk does not wait for a full batch.
                row = runtime.store.read()["tasks"][created["id"]]
                assert [event["text"] for event in row["events"] if event["type"] == "text_delta"] == [piece]
        return assistant(text)

    model.complete = streaming
    runtime.run_once()
    done = AgentStore(tmp_path, key).read()["tasks"][created["id"]]
    deltas = [event for event in done["events"] if event["type"] == "text_delta"]
    assert done["status"] == "completed" and done["result"] == text
    assert done["messages"][-1]["content"] == text
    assert "".join(event["text"] for event in deltas) == text
    assert 1 < len(deltas) <= max_commits
    timing = next(event["timing"] for event in done["events"] if event["type"] == "assistant")
    assert timing["stream_persistence_count"] == len(deltas)
    assert timing["stream_persistence_seconds"] == pytest.approx(len(deltas) * 0.05)
    # The request phase includes its budget-reservation commit.
    assert timing["first_text_seconds"] == pytest.approx(interval + 0.05)
    assert timing["model_call_seconds"] >= count * interval
    assert timing["preparation_seconds"] > 0 and timing["response_processing_seconds"] >= 0


def test_large_stream_bursts_do_not_force_a_commit_per_callback(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "Report the observations."})
    clock = simulated_clock(monkeypatch, runtime)
    pieces = ["x" * 1000 for _ in range(16)]

    def streaming(model_id, messages, specs, cancelled, on_event):
        for piece in pieces:
            clock[0] += 0.02
            on_event({"type": "text_delta", "text": piece})
        return assistant("".join(pieces))

    model.complete = streaming
    runtime.run_once()
    done = runtime.get(created["id"])
    deltas = [event for event in done["events"] if event["type"] == "text_delta"]
    assert done["status"] == "completed" and done["result"] == "".join(pieces)
    assert "".join(event["text"] for event in deltas) == done["result"]
    assert len(deltas) == 2


def test_buffered_stream_tail_cannot_publish_a_late_answer_after_pause(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "Inspect the current facts."})
    simulated_clock(monkeypatch, runtime)

    def streaming(model_id, messages, specs, cancelled, on_event):
        on_event({"type": "text_delta", "text": "First observation."})
        on_event({"type": "text_delta", "text": " Buffered late tail."})
        runtime.action(created["id"], {"action": "pause"})
        return assistant("First observation. Buffered late tail.")

    model.complete = streaming
    runtime.run_once()
    paused = runtime.get(created["id"])
    assert paused["status"] == "paused" and not paused["result"]
    assert not any(event["type"] == "assistant" for event in paused["events"])
    assert [event["text"] for event in paused["events"] if event["type"] == "text_delta"] == ["First observation."]


def test_per_phase_timing_tracks_scheduler_wait_model_tool_and_store_persistence(tmp_path, monkeypatch):
    runtime, model, tools, _ = make_runtime(tmp_path)
    clock = simulated_clock(monkeypatch, runtime)
    created = runtime.create({"message": "Measure every phase across model and tool steps."})

    # Simulate 0.4s scheduler queue delay before step 1 is claimed.
    clock[0] += 0.40

    responses = [
        assistant("", name="inspect_service", args={"service_id": "svc_1"}, call_id="call_read_1"),
        assistant("Completed after tool observation."),
    ]

    def complete_with_latency(model_id, messages, specs, cancelled, on_event):
        clock[0] += 0.25
        return responses.pop(0)

    model.complete = complete_with_latency
    orig_execute = tools.execute

    def execute_with_latency(name, arguments, **kwargs):
        clock[0] += 0.35
        return orig_execute(name, arguments, **kwargs)

    monkeypatch.setattr(tools, "execute", execute_with_latency)

    orig_metadata = tools.metadata

    def metadata_with_prep_latency(name):
        clock[0] += 0.02
        return orig_metadata(name)

    monkeypatch.setattr(tools, "metadata", metadata_with_prep_latency)

    # Step 1: Model proposes tool call
    runtime.run_once()
    # Simulate 0.6s scheduler gap before tool execution tick
    clock[0] += 0.60
    # Step 2: Tool executes
    runtime.run_once()
    # Simulate 0.8s scheduler gap before final model tick
    clock[0] += 0.80
    # Step 3: Final model response
    runtime.run_once()

    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    assistant_events = [e for e in done["events"] if e["type"] == "assistant"]
    tool_events = [e for e in done["events"] if e["type"] == "tool_result"]
    assert len(assistant_events) == 2
    assert len(tool_events) == 1

    first_model_timing = assistant_events[0]["timing"]
    assert first_model_timing["scheduler_wait_seconds"] == pytest.approx(0.40)
    assert first_model_timing["model_call_seconds"] == pytest.approx(0.35)
    assert first_model_timing["preparation_persistence_seconds"] == pytest.approx(0.15)
    assert first_model_timing["store_persistence_seconds"] == pytest.approx(0.20)

    tool_timing = tool_events[0]["timing"]
    assert tool_timing["scheduler_wait_seconds"] == pytest.approx(0.60)
    assert tool_timing["preparation_seconds"] == pytest.approx(0.02)
    assert tool_timing["tool_execution_seconds"] == pytest.approx(0.35)
    assert tool_timing["store_persistence_seconds"] == pytest.approx(0.20)

    second_model_timing = assistant_events[1]["timing"]
    assert second_model_timing["scheduler_wait_seconds"] == pytest.approx(0.80)
    assert second_model_timing["model_call_seconds"] == pytest.approx(0.35)
    assert second_model_timing["store_persistence_seconds"] > 0

