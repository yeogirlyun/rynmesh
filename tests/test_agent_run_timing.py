"""Display the actual run deadline without changing accounting or admission."""
import copy
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_runtime import Catalog, Model, Tools, advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import timing
from rynmesh.agent_runtime.protocol import event
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime, public_task
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.background_workers import BackgroundWorkerRegistry


@pytest.fixture
def clock(monkeypatch):
    import rynmesh.agent_runtime.loop as loop_module
    import rynmesh.agent_runtime.runtime as runtime_module
    value = [100.0]
    # The state controller and the extracted step loop share one deadline clock.
    frozen = SimpleNamespace(monotonic=lambda: value[0])
    monkeypatch.setattr(runtime_module, "time", frozen)
    monkeypatch.setattr(loop_module, "time", frozen)
    return value


def test_live_deadline_includes_preparation_and_still_rejects_late_success(tmp_path, clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": 10})
    row = runtime.create({"message": "Inspect"})
    original = runtime._reserve_output

    def prepare(selected):
        original(selected)
        clock[0] += 3
    runtime._reserve_output = prepare

    def complete(*args):
        clock[0] += 2
        live = runtime.get(row["id"])["run_timing"]
        assert live == {"recorded_seconds": 0, "elapsed_seconds": 5, "max_seconds": 10, "measurement": "live"}
        assert runtime.list_tasks()[0]["run_timing"] == live
        assert runtime.store.read()["tasks"][row["id"]]["elapsed_seconds"] == 0
        clock[0] += 6
        assert args[3]()  # Original cancellation callback still enforces 10 seconds.
        return assistant("Late answer")
    model.complete = complete
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["error"] == "agent_time_limit" and done["status"] == "failed"
    assert done["outcome"]["status"] == "unverified"
    assert done["run_timing"] == {"recorded_seconds": 11, "elapsed_seconds": 11, "max_seconds": 10, "measurement": "recorded"}
    assert done["budget"]["current_round"]["active_seconds"] == 8
    assert runtime._active_run is None
    assert "run_timing" not in runtime.store.read()["tasks"][row["id"]]


def test_approval_pause_restart_and_new_execution_preserve_clock_semantics(tmp_path, clock):
    runtime, model, tools, key = make_runtime(tmp_path)
    def complete(*args):
        clock[0] += 2
        return assistant(name="share_service", args={"service_id": "A"})
    model.complete = complete
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_approval"
    assert waiting["run_timing"]["elapsed_seconds"] == 2
    clock[0] += 1000
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    assert restarted.get(row["id"])["run_timing"] == waiting["run_timing"]
    approve(restarted, row["id"])
    advance(restarted)
    paused = restarted.action(row["id"], {"action": "pause"})
    clock[0] += 1000
    assert restarted.get(row["id"])["run_timing"]["elapsed_seconds"] == 2
    resumed = restarted.action(row["id"], {"action": "resume", "expected_control_revision": paused["control_revision"]})
    assert resumed["run_timing"]["elapsed_seconds"] == 2
    model.complete = lambda *args: assistant("Done")
    advance(restarted)
    ended = restarted.get(row["id"])
    assert ended["status"] == "completed"
    new = restarted.add_message(row["id"], {"message": "Another question", "expected_control_revision": ended["control_revision"]})
    assert new["run_timing"]["elapsed_seconds"] == 0
    assert new["budget"]["lifetime"]["active_seconds"] == 2


def test_input_answer_changes_budget_round_but_not_run_limit_clock(tmp_path, clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    def complete(*args):
        clock[0] += 4
        return assistant(name="ryn_request_input", args={"goal_revision": 1, "prompt": "Which item?", "kind": "information",
            "fields": [{"id": "item", "label": "Item", "type": "text", "required": True}]})
    model.complete = complete
    row = runtime.create({"message": "Inspect an item"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_input"
    clock[0] += 1000
    answered = runtime.action(row["id"], {"action": "answer", "expected_control_revision": waiting["control_revision"], "expected_goal_revision": waiting["goal"]["revision"],
        "pending_input_id": waiting["pending_input"]["id"], "answers": {"item": "A"}})
    assert answered["budget"]["round"] == waiting["budget"]["round"] + 1
    assert answered["run_timing"]["elapsed_seconds"] == 4


def test_new_turn_events_capture_their_own_elapsed_baseline(tmp_path, clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "First question"})

    def complete(*args):
        clock[0] += 4
        return assistant("First answer")

    model.complete = complete
    advance(runtime)
    done = runtime.get(created["id"])
    completed = next(item for item in done["events"] if item["type"] == "completed")
    assert completed["run_elapsed_seconds"] == 4
    continued = runtime.add_message(created["id"], {"message": "Next question",
        "expected_control_revision": done["control_revision"]})
    added = next(item for item in reversed(continued["events"]) if item["type"] == "message_added")
    assert added["run_elapsed_seconds"] == 0
    assert completed in continued["events"]


@pytest.mark.parametrize("elapsed", [None, "5", True, -1, float("nan"), float("inf")])
def test_event_omits_unknown_or_invalid_elapsed_time(elapsed):
    row = {"event_cursor": 0, "events": [], "revision": 0, "elapsed_seconds": elapsed}
    event(row, "assistant", content="Observation")
    assert "run_elapsed_seconds" not in row["events"][0]


@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize("stage", ["model", "tool"])
def test_stopping_inflight_operation_keeps_counting_until_settled(tmp_path, clock, action, stage):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "A"}))
    row = runtime.create({"message": "Inspect"})
    if stage == "tool":
        advance(runtime)
    execute = tools.execute
    observations = []
    def complete(*args, **kwargs):
        clock[0] += 2
        changed = runtime.action(row["id"], {"action": action})
        clock[0] += 3
        observations.append((changed, runtime.get(row["id"])))
        return assistant("Late answer") if stage == "model" else execute(*args, **kwargs)
    if stage == "model":
        model.complete = complete
    else:
        tools.execute = complete
    advance(runtime)
    assert len(observations) == 1
    changed, stopping = observations[0]
    assert changed["run_timing"]["measurement"] == "pending"
    if action == "pause":
        assert changed["status"] == "paused" and changed["run_state"] == "pausing"
    assert stopping["run_timing"]["measurement"] == "live"
    assert stopping["run_timing"]["elapsed_seconds"] == 5
    stopped = runtime.get(row["id"])
    assert stopped["status"] == ("paused" if action == "pause" else "cancelled")
    assert stopped["run_timing"]["measurement"] == "recorded"
    assert stopped["run_timing"]["elapsed_seconds"] == 5
    settlement = [item for item in stopped["events"] if item["type"] == "run_settled"]
    assert len(settlement) == 1 and settlement[0]["run_elapsed_seconds"] == 5
    assert stopped["control_revision"] == changed["control_revision"]
    assert stopped["completion_event_seq"] == 0
    clock[0] += 1000
    assert runtime.get(row["id"])["run_timing"] == stopped["run_timing"]
    if action == "pause":
        next_turn = runtime.add_message(row["id"], {"message": "Next question",
            "expected_control_revision": stopped["control_revision"]})
        assert settlement[0] in next_turn["events"]
        assert next_turn["run_timing"]["elapsed_seconds"] == 0


def test_missing_legacy_time_and_unowned_flight_are_not_false_zero_or_live(tmp_path, clock):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "Inspect"})
    stored = runtime.store.read()["tasks"][row["id"]]
    stored.pop("elapsed_seconds")
    assert public_task(stored)["run_timing"] == {"recorded_seconds": None, "elapsed_seconds": None, "max_seconds": stored["limits"]["max_seconds"], "measurement": "unknown"}
    stored.update(elapsed_seconds=7, status="running", in_flight={"kind": "model"})
    assert public_task(stored)["run_timing"]["measurement"] == "pending"
    before = copy.deepcopy(stored)
    # Stale local clock from an already settled tick cannot double-count it.
    projected = timing.snapshot(stored, active_run={"task_id": row["id"], "recorded_seconds": 3, "started": 0, "revision": 0}, monotonic_now=500)
    assert projected["elapsed_seconds"] == 7 and projected["measurement"] == "pending"
    assert stored == before


@pytest.mark.parametrize("failed_query", [False, True])
def test_reconciliation_uses_original_tick_and_clears_live_marker(tmp_path, clock, failed_query):
    from test_agent_runtime_verification import unknown_write, verified_runtime
    runtime, _, tools, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    row = unknown_write(runtime, tools)
    original = tools.reconcile
    observations = []
    def reconcile(*args, **kwargs):
        clock[0] += 5
        observations.append(runtime.get(row["id"])["run_timing"])
        if failed_query:
            raise RuntimeError("query unavailable")
        return original(*args, **kwargs)
    tools.reconcile = reconcile
    advance(runtime)
    assert len(observations) == 1
    assert observations[0]["measurement"] == "live" and observations[0]["elapsed_seconds"] == 5
    result = runtime.get(row["id"])
    assert result["run_timing"]["measurement"] == "recorded"
    assert result["run_timing"]["elapsed_seconds"] == 5
    assert result["reconciliation"]["state"] == ("unknown" if failed_query else "executed")
    assert runtime._active_run is None and len(tools.calls) == 1


def test_restart_does_not_estimate_unsettled_tick_from_wall_clock(tmp_path, clock):
    runtime, model, tools, key = make_runtime(tmp_path)
    row = runtime.create({"message": "Inspect"})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(
        elapsed_seconds=7, status="running", in_flight={"kind": "model", "dispatched": True}))
    clock[0] += 10000
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    assert restarted.get(row["id"])["run_timing"]["measurement"] == "pending"
    restarted.reconcile()
    recovered = restarted.get(row["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["run_timing"] == {"recorded_seconds": 7, "elapsed_seconds": 7, "max_seconds": recovered["limits"]["max_seconds"], "measurement": "recorded"}


def test_owner_api_exposes_live_snapshot_but_rejects_injected_timing(tmp_path, clock):
    app = FastAPI()
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    model = Model()
    runtime = install_agent(app, store=object(), home=tmp_path, workers=BackgroundWorkerRegistry(), local_control=guard,
        messaging_key=X25519PrivateKey.generate(), integrations=(model, Tools(), Catalog()))
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    with TestClient(app) as client:
        headers = {"x-owner": "yes"}
        assert client.post("/api/local/agent/tasks", headers=headers, json={"message": "Inspect", "run_timing": {"elapsed_seconds": 0}}).status_code == 400
        row = client.post("/api/local/agent/tasks", headers=headers, json={"message": "Inspect"}).json()
        url = f"/api/local/agent/tasks/{row['id']}"
        assert client.get(url).status_code == 401
        def complete(*args):
            clock[0] += 3
            response = client.get(url, headers=headers)
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["run_timing"]["elapsed_seconds"] == 3
            assert response.json()["run_timing"]["measurement"] == "live"
            return assistant("Done")
        model.complete = complete
        advance(runtime)
        completed = client.get(url, headers=headers).json()
        assert completed["status"] == "completed" and completed["error"] is None
        assert completed["run_timing"]["measurement"] == "recorded"
