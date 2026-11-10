"""Long tasks continue across internal execution segments; owner budgets stay hard.

Internal segment sizes come from the system defaults (store.DEFAULT_SETTINGS).
Tests lower them by patching those defaults before the store is created, never
through update_settings, which records an explicit owner limit.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from test_agent_codex_loop import WorkstationTools
from test_agent_runtime import Tools, advance, assistant, make_runtime

import rynmesh.agent_runtime.loop as loop_module
import rynmesh.agent_runtime.runtime as runtime_module
import rynmesh.agent_runtime.store as store_module
from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected


def inspect(index):
    return assistant(name="inspect_service", args={"service_id": f"svc{index}"})


def segment_events(row):
    return [item for item in row["events"] if item["type"] == "execution_segment_continued"]


def stored(runtime, task_id):
    return runtime.store.read()["tasks"][task_id]


def small_segments(monkeypatch, *, steps=None, seconds=None):
    if steps is not None:
        monkeypatch.setitem(store_module.DEFAULT_SETTINGS, "max_steps", steps)
    if seconds is not None:
        monkeypatch.setitem(store_module.DEFAULT_SETTINGS, "max_seconds", seconds)


# 1. Normal progress crosses at least three internal segments without "继续".

def test_default_step_segments_continue_to_completion_without_owner_message(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=3)
    runtime, model, tools, _ = make_runtime(tmp_path, *[inspect(i) for i in range(1, 8)],
                                            assistant("All seven services inspected."))
    row = runtime.create({"message": "Inspect svc1 through svc7"})
    advance(runtime, 40)
    done = runtime.get(row["id"])
    assert done["status"] == "completed", (done["status"], done.get("error"))
    assert done["result"] == "All seven services inspected."
    assert len(model.calls) == 8 and len(tools.calls) == 7
    segments = segment_events(done)
    assert len(segments) >= 2 and {item["reason"] for item in segments} == {"max_steps"}
    assert [item["segment"] for item in segments] == list(range(2, 2 + len(segments)))
    assert sum(message["role"] == "user" for message in stored(runtime, row["id"])["messages"]) == 1
    assert not any(item["type"] in {"message_added", "budget_wrap_up", "failed", "budget_limit_reached"} for item in done["events"])
    # Cumulative accounting is not reset by a segment.
    assert done["budget"]["lifetime"]["model_calls"] == 8
    assert done["budget"]["lifetime"]["tool_calls"] == 7


def test_segment_boundary_hands_control_back_to_the_scheduler(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    order = []

    class Model:
        calls = []

        def complete(self, model_id, messages, specs, cancelled, on_event):
            owner = next(m["content"] for m in messages if m["role"] == "user" and m["content"].startswith("task-"))
            name = owner.split()[0]
            done = sum(1 for m in messages if m["role"] == "tool")
            order.append(name)
            self.calls.append(name)
            return inspect(done) if done < 4 else assistant(f"{name} finished")

    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.model = Model()
    first = runtime.create({"message": "task-a inspect four services"})
    second = runtime.create({"message": "task-b inspect four services"})
    advance(runtime, 60)
    assert runtime.get(first["id"])["status"] == "completed"
    assert runtime.get(second["id"])["status"] == "completed"
    assert segment_events(runtime.get(first["id"])) and segment_events(runtime.get(second["id"]))
    # Neither task monopolizes a worker across its segment boundary.
    first_b = order.index("task-b")
    assert first_b < order.count("task-a") and order[first_b:].count("task-a") >= 2


def test_default_time_segment_keeps_completed_work_and_continues(tmp_path, monkeypatch):
    small_segments(monkeypatch, seconds=10)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(runtime_module, "time", fake_time)
    monkeypatch.setattr(loop_module, "time", fake_time)
    runtime, model, tools, _ = make_runtime(tmp_path)
    pending = [inspect(1), assistant("Done after a slow first request.")]

    def complete(model_id, messages, specs, cancelled, on_event):
        model.calls.append(messages)
        if len(model.calls) == 1:
            clock[0] += 11  # Crosses the 10 s internal segment while in flight.
            assert not cancelled()
        return pending.pop(0)
    model.complete = complete
    row = runtime.create({"message": "Inspect svc1"})
    advance(runtime, 10)
    done = runtime.get(row["id"])
    assert done["status"] == "completed", (done["status"], done.get("error"))
    assert len(model.calls) == 2 and [call[1]["service_id"] for call in tools.calls] == ["svc1"]
    assert [item["reason"] for item in segment_events(done)] == ["max_seconds"]


# 2/3. Owner budgets are hard in every segment and after restart; defaults extend visibly.

def test_owner_budget_is_hard_across_segments_restart_and_owner_turns(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    runtime, model, tools, key = make_runtime(tmp_path, *[inspect(i) for i in range(1, 20)])
    row = runtime.create({"message": "Inspect many services", "budget_limits": {"max_total_model_calls": 5}})
    advance(runtime, 40)
    stopped = runtime.get(row["id"])
    assert len(model.calls) == 5
    # "failed" is the existing budget-stop status; the outcome says it was a budget stop, not completion.
    assert stopped["status"] == "failed" and stopped["error"] == "agent_model_call_budget_exhausted"
    assert stopped["outcome"]["status"] == "budget_limited"
    assert stopped["outcome"]["budget_stop"] == {"code": "agent_model_call_budget_exhausted",
                                                  "limit": "max_total_model_calls", "source": "owner"}
    assert stopped["outcome"]["summary"]  # Host-written progress; no extra model call.
    assert stopped["budget"]["limits"]["max_total_model_calls"] == 5
    assert stopped["budget"]["limit_sources"]["max_total_model_calls"] == "owner"
    assert segment_events(stopped)
    assert not any(item["type"] == "budget_auto_extended" for item in stopped["events"])

    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restored.reconcile()
    advance(restored, 5)
    after = restored.get(row["id"])
    assert len(model.calls) == 5
    assert after["budget"]["limits"]["max_total_model_calls"] == 5
    assert after["budget"]["lifetime"]["charged_model_calls"] == 5
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        restored.add_message(row["id"], {"message": "继续", "expected_revision": after["revision"]})
    unchanged = restored.get(row["id"])
    assert unchanged["budget"]["limits"]["max_total_model_calls"] == 5
    assert not any(item["type"] == "budget_auto_extended" for item in unchanged["events"])

    raised = restored.update_budget(row["id"], {"limits": {"max_total_model_calls": 7},
                                                "expected_control_revision": after["control_revision"]})
    advance(restored, 3)
    assert len(model.calls) == 5  # Saving a higher limit does not resume the task by itself.
    restored.add_message(row["id"], {"message": "继续", "expected_revision": raised["revision"]})
    advance(restored, 20)
    final = restored.get(row["id"])
    assert len(model.calls) == 7
    assert final["status"] == "failed" and final["error"] == "agent_model_call_budget_exhausted"
    assert final["budget"]["limits"]["max_total_model_calls"] == 7


def test_default_budget_extends_visibly_and_stops_at_its_ceiling(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    monkeypatch.setitem(budget.DEFAULT_LIMITS, "max_total_model_calls", 3)
    monkeypatch.setitem(budget.RANGES, "max_total_model_calls", (1, 7))
    runtime, model, _, _ = make_runtime(tmp_path, *[inspect(i) for i in range(1, 20)])
    row = runtime.create({"message": "Inspect many services"})
    advance(runtime, 40)
    done = runtime.get(row["id"])
    assert len(model.calls) == 7
    assert done["status"] == "failed" and done["error"] == "agent_model_call_budget_exhausted"
    assert done["outcome"]["status"] == "budget_limited"
    assert done["budget"]["limit_sources"]["max_total_model_calls"] == "default"
    assert done["outcome"]["budget_stop"]["source"] == "default"
    extended = [item for item in done["events"] if item["type"] == "budget_auto_extended"]
    assert [item["extended"]["max_total_model_calls"] for item in extended] == [6, 7]
    assert all(item["reason"] == "default_policy" for item in extended)


def test_legacy_task_without_limit_sources_is_not_relaxed_by_default_values(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    monkeypatch.setitem(budget.DEFAULT_LIMITS, "max_total_model_calls", 3)
    runtime, model, _, _ = make_runtime(tmp_path, *[inspect(i) for i in range(1, 20)])
    row = runtime.create({"message": "Inspect many services"})

    def make_legacy(data):
        legacy = data["tasks"][row["id"]]
        legacy.pop("limits_source")
        legacy["budget"].pop("limit_sources")
        # Values equal the defaults; that alone must not prove default intent.
        assert legacy["limits"]["max_steps"] == 2
        assert legacy["budget"]["limits"]["max_total_model_calls"] == budget.DEFAULT_LIMITS["max_total_model_calls"]
    runtime.store.transaction(make_legacy)
    assert runtime.get(row["id"])["budget"]["limit_sources"]["max_total_model_calls"] == "legacy_unknown"

    advance(runtime, 10)
    first = runtime.get(row["id"])
    assert len(model.calls) == 2
    assert first["status"] == "failed" and first["error"] == "agent_step_limit"
    assert first["outcome"]["budget_stop"]["source"] == "legacy_unknown"
    assert not segment_events(first)
    runtime.add_message(row["id"], {"message": "继续", "expected_revision": first["revision"]})
    advance(runtime, 10)
    second = runtime.get(row["id"])
    assert len(model.calls) == 3
    assert second["status"] == "failed" and second["error"] == "agent_model_call_budget_exhausted"
    assert second["budget"]["limits"]["max_total_model_calls"] == 3
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        runtime.add_message(row["id"], {"message": "继续", "expected_revision": second["revision"]})
    assert not any(item["type"] == "budget_auto_extended" for item in runtime.get(row["id"])["events"])


def test_limit_sources_for_default_owner_and_legacy_rows():
    fresh = {"limits": {"max_seconds": 30}, "steps": 0, "elapsed_seconds": 0}
    assert set(budget.public(fresh)["limit_sources"].values()) == {"default"}
    owner = {"limits": {"max_seconds": 30}, "steps": 0, "elapsed_seconds": 0}
    budget.set_owner_limits(owner, {"max_total_tokens": 5000})
    assert budget.public(owner)["limit_sources"]["max_total_tokens"] == "owner"
    assert budget.public(owner)["limits"]["max_total_tokens"] == 5000
    # Persisted rows from before limit sources existed.
    legacy = {"limits": {"max_seconds": 30}, "steps": 0, "elapsed_seconds": 0,
              "budget": {"limits": dict(budget.DEFAULT_LIMITS), "round": 1, "records": [], "legacy_usage_unknown": False,
                         "legacy_model_calls": 0, "legacy_tool_calls": 0, "legacy_seconds": 0.0}}
    assert set(budget.public(legacy)["limit_sources"].values()) == {"legacy_unknown"}
    for key in budget.DEFAULT_LIMITS:
        assert budget.is_hard(legacy, key)
    assert budget.segment_source(legacy, "max_steps") == "legacy_unknown"


# 4. Owner control at the boundary between segments.

def at_boundary(tmp_path, monkeypatch, *responses, **create):
    small_segments(monkeypatch, steps=2)
    runtime, model, tools, key = make_runtime(tmp_path, inspect(1), inspect(2), *responses)
    row = runtime.create({"message": "Inspect services", **create})
    advance(runtime, 4)  # model, tool, model, tool: the next claim starts a new segment
    current = stored(runtime, row["id"])
    assert current["status"] == "queued" and current["steps"] == 2 and not current["queue"]
    assert not segment_events(runtime.get(row["id"]))
    return runtime, model, tools, row


def test_pause_and_resume_around_an_automatic_segment(tmp_path, monkeypatch):
    runtime, model, tools, row = at_boundary(tmp_path, monkeypatch, inspect(3), assistant("Finished after resume."))
    runtime.action(row["id"], {"action": "pause"})
    advance(runtime, 5)
    paused = runtime.get(row["id"])
    assert paused["status"] == "paused" and len(model.calls) == 2 and not segment_events(paused)
    runtime.action(row["id"], {"action": "resume", "expected_control_revision": paused["control_revision"]})
    advance(runtime, 10)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["result"] == "Finished after resume."
    assert len(model.calls) == 4


def test_cancel_at_the_segment_boundary_stops_dispatch(tmp_path, monkeypatch):
    runtime, model, _, row = at_boundary(tmp_path, monkeypatch, inspect(3))
    runtime.action(row["id"], {"action": "cancel"})
    advance(runtime, 5)
    done = runtime.get(row["id"])
    assert done["status"] == "cancelled" and len(model.calls) == 2 and not segment_events(done)


def test_new_owner_instruction_at_the_boundary_reaches_the_next_segment(tmp_path, monkeypatch):
    runtime, model, _, row = at_boundary(tmp_path, monkeypatch, assistant("Switched to svc9 as instructed."))
    current = runtime.get(row["id"])
    runtime.add_message(row["id"], {"message": "Stop inspecting; only report on svc9.", "intent": "steer",
                                    "expected_control_revision": current["control_revision"],
                                    "expected_goal_revision": current["goal"]["revision"]})
    advance(runtime, 5)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["result"] == "Switched to svc9 as instructed."
    assert len(model.calls) == 3
    assert any(m["role"] == "user" and "only report on svc9" in m["content"] for m in model.calls[-1][1])


def test_permission_revocation_at_the_boundary_blocks_the_queued_write(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    runtime, model, tools, _ = make_runtime(tmp_path, inspect(1),
                                            assistant(name="share_service", args={"service_id": "svc"}),
                                            assistant("Sharing was not permitted."))
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    row = runtime.create({"message": "Inspect then share"})
    advance(runtime, 3)  # model, tool, model -> the write is queued at the segment boundary
    queued = stored(runtime, row["id"])
    assert queued["steps"] == 2 and queued["queue"] and queued["queue"][0]["name"] == "share_service"
    runtime.update_settings({"permission_mode": "read_only"})
    advance(runtime, 10)
    done = runtime.get(row["id"])
    assert tools.shared is False and [call[0] for call in tools.calls] == ["inspect_service"]
    assert done["status"] == "completed" and len(model.calls) == 3


# 5/7. Real loop, real file/process tools, restart and no repeated side effects.

def append_command(text):
    return ({"command": f"Add-Content -Path log.txt -Value {text}", "shell": "powershell"} if os.name == "nt"
            else {"command": f"printf '{text}\\n' >> log.txt"})


class ReconcilingWorkstation(WorkstationTools):
    names = ("ryn_shell_execute", "ryn_file_write", "ryn_file_read")

    def reconcile(self, operation_id, *, context, cancelled=None):
        return self.local.reconcile(operation_id, context)


def workstation(tmp_path, monkeypatch, responses):
    small_segments(monkeypatch, steps=2)
    workspace = tmp_path / "work"
    workspace.mkdir()
    runtime, model, _, key = make_runtime(tmp_path / "private", *responses)
    runtime.tools = ReconcilingWorkstation(runtime, tmp_path / "private")
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": "workspace"})
    return runtime, model, workspace, key


def restart(runtime, key, tmp_path, model):
    restored = AgentRuntime(AgentStore(tmp_path / "private", key), model, None, runtime.catalog)
    restored.tools = ReconcilingWorkstation(restored, tmp_path / "private")
    restored.reconcile()
    return restored


def test_real_files_cross_segments_and_restart_without_repeating_writes(tmp_path, monkeypatch):
    responses = [
        assistant(name="ryn_shell_execute", args=append_command("one")),
        assistant(name="ryn_file_write", args={"path": "a.txt", "content": "alpha"}),
        assistant(name="ryn_shell_execute", args=append_command("two")),
        assistant(name="ryn_file_write", args={"path": "b.txt", "content": "beta"}),
        assistant(name="ryn_shell_execute", args=append_command("three")),
        assistant(name="ryn_file_read", args={"path": "log.txt"}),
        assistant("log.txt has one, two and three; a.txt and b.txt are written."),
    ]
    runtime, model, workspace, key = workstation(tmp_path, monkeypatch, responses)
    row = runtime.create({"message": "Append one/two/three to log.txt and write a.txt and b.txt"})
    advance(runtime, 8)  # four model steps and four writes: two segments' worth
    middle = stored(runtime, row["id"])
    assert middle["status"] == "queued" and not middle["in_flight"]
    assert (workspace / "log.txt").read_text(encoding="utf-8").split() == ["one", "two"]

    restored = restart(runtime, key, tmp_path, model)
    advance(restored, 20)
    done = restored.get(row["id"])
    assert done["status"] == "completed", (done["status"], done.get("error"))
    assert len(model.calls) == 7 and len(segment_events(done)) >= 2
    assert (workspace / "log.txt").read_text(encoding="utf-8").split() == ["one", "two", "three"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "beta"
    writes = [r for r in stored(restored, row["id"])["receipts"] if r["tool"] in {"ryn_shell_execute", "ryn_file_write"}]
    assert len(writes) == 5 and len({r["operation_id"] for r in writes}) == 5
    assert all(r["status"] == "confirmed" for r in writes)


def test_write_with_unknown_outcome_after_crash_is_reconciled_not_retried(tmp_path, monkeypatch):
    responses = [
        assistant(name="ryn_file_write", args={"path": "a.txt", "content": "alpha"}),
        assistant(name="ryn_shell_execute", args=append_command("once")),
        assistant(name="ryn_file_read", args={"path": "log.txt"}),
        assistant("Appended once."),
    ]
    runtime, model, workspace, key = workstation(tmp_path, monkeypatch, responses)
    original = runtime.tools.execute

    def crash_after_side_effect(name, args, **kwargs):
        result = original(name, args, **kwargs)
        if name == "ryn_shell_execute":
            raise KeyboardInterrupt  # The process dies before the receipt commit.
        return result
    runtime.tools.execute = crash_after_side_effect
    row = runtime.create({"message": "Write a.txt then append once to log.txt"})
    with pytest.raises(KeyboardInterrupt):
        advance(runtime, 6)
    crashed = stored(runtime, row["id"])
    assert crashed["in_flight"]["effect"] == "write" and crashed["in_flight"]["dispatched"]
    assert crashed["steps"] == 2  # The crash happened at the segment boundary.

    restored = restart(runtime, key, tmp_path, model)
    interrupted = restored.get(row["id"])
    assert interrupted["status"] == "interrupted" and interrupted["error"] == "agent_write_outcome_unknown"
    advance(restored, 12)
    done = restored.get(row["id"])
    assert (workspace / "log.txt").read_text(encoding="utf-8").split() == ["once"]
    receipts = [r for r in stored(restored, row["id"])["receipts"] if r["tool"] == "ryn_shell_execute"]
    assert len(receipts) == 1
    events = [item["type"] for item in done["events"]]
    assert "reconciliation_finished" in events
    assert done["status"] == "completed", (done["status"], done.get("error"))
    assert len(model.calls) == 4


# 6. Persistent failure / no progress stops; mixed progress does not.

class FlakyTools(Tools):
    def __init__(self, fail):
        super().__init__()
        self.fail = fail

    def execute(self, name, arguments, *, operation_id, cancelled):
        self.calls.append((name, arguments, operation_id))
        if self.fail(len(self.calls)):
            raise AgentToolRejected("agent_tool_arguments_invalid")
        return {"service_id": arguments["service_id"], "ok": True}


def test_a_full_segment_without_successful_progress_stops_with_a_reason(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=3)
    runtime, model, _, _ = make_runtime(tmp_path, *[inspect(i) for i in range(1, 20)])
    runtime.tools = FlakyTools(lambda _count: True)
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 40)
    done = runtime.get(row["id"])
    assert len(model.calls) == 3
    assert done["status"] == "failed" and done["error"] == "agent_no_progress"
    assert done["outcome"]["status"] != "budget_limited"
    assert any(item["type"] == "no_progress_stopped" for item in done["events"])
    # A new owner turn starts a fresh progress audit instead of stopping immediately.
    runtime.add_message(row["id"], {"message": "Try a different service id.", "expected_revision": done["revision"]})
    advance(runtime, 2)
    assert len(model.calls) == 4


def test_mixed_failures_with_progress_are_not_misjudged(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=3)
    runtime, model, _, _ = make_runtime(tmp_path, *[inspect(i) for i in range(1, 13)], assistant("Done despite retries."))
    runtime.tools = FlakyTools(lambda count: count % 3 != 0)  # Two failures, then a success.
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 60)
    done = runtime.get(row["id"])
    assert done["status"] == "completed", (done["status"], done.get("error"))
    assert len(model.calls) == 13 and len(segment_events(done)) >= 3


def test_time_segment_rollover_never_resets_an_owner_step_limit(tmp_path, monkeypatch):
    small_segments(monkeypatch, seconds=10)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(runtime_module, "time", fake_time)
    monkeypatch.setattr(loop_module, "time", fake_time)
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_steps": 3})  # Explicit owner run limit; time stays a default segment.
    responses = [inspect(i) for i in range(1, 10)]

    def complete(model_id, messages, specs, cancelled, on_event):
        model.calls.append(messages)
        clock[0] += 11  # Every request crosses the default 10 s segment.
        return responses.pop(0)
    model.complete = complete
    row = runtime.create({"message": "Inspect"})
    assert runtime.get(row["id"])["limits_source"] == {"max_steps": "owner", "max_seconds": "default"}
    advance(runtime, 30)
    done = runtime.get(row["id"])
    assert len(model.calls) == 3
    assert done["status"] == "failed" and done["error"] == "agent_step_limit"
    assert done["outcome"]["budget_stop"]["source"] == "owner"
    assert {item["reason"] for item in segment_events(done)} == {"max_seconds"}


def test_slow_segments_without_any_success_stop_after_two(tmp_path, monkeypatch):
    small_segments(monkeypatch, seconds=10)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(runtime_module, "time", fake_time)
    monkeypatch.setattr(loop_module, "time", fake_time)
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.tools = FlakyTools(lambda _count: True)
    responses = [inspect(i) for i in range(1, 20)]

    def complete(model_id, messages, specs, cancelled, on_event):
        model.calls.append(messages)
        clock[0] += 11
        return responses.pop(0)
    model.complete = complete
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 30)
    done = runtime.get(row["id"])
    assert done["status"] == "failed" and done["error"] == "agent_no_progress"
    assert len(model.calls) == 2 and len(segment_events(done)) == 1


def test_default_token_allowance_grows_at_model_admission(tmp_path, monkeypatch):
    monkeypatch.setitem(budget.DEFAULT_LIMITS, "max_total_tokens", 1000)
    # The first request needs more than 1000 tokens of reservation by itself.
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Done."))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["status"] == "completed", (done["status"], done.get("error"))
    grown = [item for item in done["events"] if item["type"] == "budget_auto_extended"
             and "max_total_tokens" in item["extended"]]
    assert grown and all(item["reason"] == "default_policy" for item in grown)
    # The same request under an owner budget of 1000 tokens is refused before inference.
    owner_runtime, owner_model, _, _ = make_runtime(tmp_path / "owner", assistant("unused"))
    owner = owner_runtime.create({"message": "Inspect", "budget_limits": {"max_total_tokens": 1000}})
    advance(owner_runtime, 2)
    refused = owner_runtime.get(owner["id"])
    assert refused["error"] == "agent_token_budget_exhausted" and owner_model.calls == []
