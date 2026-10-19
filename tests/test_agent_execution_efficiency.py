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


def test_parallel_read_batch_coalesces_observed_files_and_usage_settlement(tmp_path, monkeypatch):
    from test_agent_speedups import calls, workstation

    runtime, _, workspace = workstation(
        tmp_path,
        calls(
            ("ryn_file_read", {"path": "a.py"}),
            ("ryn_file_read", {"path": "b.py"}),
            ("ryn_file_read", {"path": "c.py"}),
        ),
        assistant("Done reading."),
    )
    for name in ("a.py", "b.py", "c.py"):
        (workspace / name).write_text(f"{name} = 1\n", encoding="utf-8")

    created = runtime.create({"message": "Read three files in parallel."})

    # Step 1: Model emits 3 parallel ryn_file_read tool calls.
    runtime.run_once()

    tx_count = [0]
    orig_tx = runtime.store.transaction

    def count_tx(mutate):
        tx_count[0] += 1
        return orig_tx(mutate)

    monkeypatch.setattr(runtime.store, "transaction", count_tx)

    # Step 2: _tool_batch executes all 3 reads concurrently.
    runtime.run_once()
    # Expected transactions: claim + _reserve_output + authorize + _settle_usage_batch + finish = 5 total
    # (instead of 5 + 3 observed_files + 2 extra _settle_usage = 10 transactions).
    assert tx_count[0] == 5

    stored = runtime.store.read()["tasks"][created["id"]]
    assert len(stored.get("observed_files", {})) == 3


def test_model_timeout_retry_records_request_id_monotonic_timing_and_safe_diagnostics(tmp_path, monkeypatch):
    from rynmesh.agent_runtime.store import AgentError

    runtime, model, _, _ = make_runtime(tmp_path)
    clock = simulated_clock(monkeypatch, runtime)
    created = runtime.create({"message": "Diagnose a timed-out model turn."})

    attempts = [0]

    def complete_timeout_then_succeed(model_id, messages, specs, cancelled, on_event):
        attempts[0] += 1
        if attempts[0] == 1:
            clock[0] += 12.5
            err = AgentError("agent_inference_timeout")
            err.diagnostics = {
                "rpc_stage": "turn_wait",
                "session_mode": "started",
                "events_received": 1,
                "deltas_received": 0,
                "last_event_age_seconds": 12.4,
                "process_alive": True,
                "secret_token": "MUST_NOT_LEAK",
            }
            raise err
        clock[0] += 0.3
        return assistant("Recovered on attempt 2.")

    model.complete = complete_timeout_then_succeed
    runtime.run_once()
    runtime.run_once()

    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    started_events = [e for e in done["events"] if e["type"] == "model_started"]
    retry_events = [e for e in done["events"] if e["type"] == "model_retry_scheduled"]
    assistant_events = [e for e in done["events"] if e["type"] == "assistant"]

    assert len(started_events) == 2
    assert len(retry_events) == 1
    assert len(assistant_events) == 1

    assert started_events[0]["request_id"] == retry_events[0]["request_id"]
    assert started_events[0]["attempt"] == retry_events[0]["attempt"] == 1
    assert started_events[1]["request_id"] == assistant_events[0]["request_id"]
    assert started_events[1]["attempt"] == assistant_events[0]["attempt"] == 2
    assert started_events[0]["request_id"] != started_events[1]["request_id"]

    retry_timing = retry_events[0]["timing"]
    # 12.5s call + 2 * 0.05s (_meter_model admit + _settle_usage commits)
    assert retry_timing["model_call_seconds"] == pytest.approx(12.6)
    assert retry_timing["preparation_persistence_seconds"] == pytest.approx(0.15)
    assert retry_timing["store_persistence_seconds"] == pytest.approx(0.20)

    diag = retry_events[0]["diagnostics"]
    assert diag == {
        "rpc_stage": "turn_wait",
        "session_mode": "started",
        "events_received": 1,
        "deltas_received": 0,
        "last_event_age_seconds": 12.4,
        "process_alive": True,
    }
    assert "secret_token" not in str(done["events"])
