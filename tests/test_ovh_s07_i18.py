"""S07-I18: Uncapped default budgets, legacy default migration, ranges, and near_limit."""
from __future__ import annotations

import math
import uuid
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, assistant, make_runtime

import rynmesh.agent_runtime.loop as loop_module
import rynmesh.agent_runtime.runtime as runtime_module
import rynmesh.agent_runtime.store as store_module
from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.store import AgentError


def _new_default_row() -> dict:
    return {
        "limits": {"max_steps": 64, "max_seconds": 900},
        "limits_source": {"max_steps": "default", "max_seconds": "default"},
        "steps": 0,
        "elapsed_seconds": 0.0,
        "revision": 1,
        "event_cursor": 0,
        "events": [],
    }


def test_t1_default_budget_public_contract_null_ceilings_and_ranges():
    row = _new_default_row()
    pub = budget.public(row)
    assert pub["default_ceilings"] == {key: None for key in budget.DEFAULT_LIMITS}
    assert {k: tuple(v) for k, v in pub["ranges"].items()} == budget.RANGES
    assert pub["near_limit"] == []


def test_t2_default_ended_task_near_20m_tokens_extends_past_20m_on_add_message(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First answer."), assistant("Second answer."))
    created = runtime.create({"message": "Run first phase"})
    advance(runtime, 10)
    done = runtime.get(created["id"])
    assert done["status"] == "completed"

    def preload_near_20m(data):
        row = data["tasks"][created["id"]]
        state = budget.initialize(row)
        state["limits"]["max_total_tokens"] = 20_000_000
        state["limit_sources"]["max_total_tokens"] = "default"
        state["records"].append(
            {
                "id": "usage_near_20m",
                "kind": "model",
                "stage": "model",
                "round": state["round"],
                "status": "settled",
                "input_tokens": 19_800_000,
                "output_tokens": 100_000,
                "charged_tokens": 19_900_000,
                "charged_seconds": 10.0,
                "elapsed_seconds": 10.0,
                "input_reservation": 19_800_000,
                "output_reservation": 100_000,
            }
        )

    runtime.store.transaction(preload_near_20m)
    updated = runtime.add_message(
        created["id"],
        {"message": "Continue past 20M tokens", "expected_revision": done["revision"]},
    )
    assert updated["budget"]["limits"]["max_total_tokens"] == 22_000_000
    assert updated["budget"]["limits"]["max_total_tokens"] > 20_000_000
    ext_events = [ev for ev in updated["events"] if ev.get("type") == "budget_auto_extended"]
    assert ext_events
    assert ext_events[-1]["reason"] == "default_policy"
    assert ext_events[-1]["extended"]["max_total_tokens"] == 22_000_000


def test_t3_owner_ended_task_near_20m_tokens_not_extended_and_rejected(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First answer."))
    created = runtime.create(
        {"message": "Run owner-capped phase", "budget_limits": {"max_total_tokens": 20_000_000}}
    )
    advance(runtime, 10)
    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    assert done["budget"]["limit_sources"]["max_total_tokens"] == "owner"

    def spend_all_20m(data):
        row = data["tasks"][created["id"]]
        state = budget.initialize(row)
        state["records"].append(
            {
                "id": "usage_owner_20m",
                "kind": "model",
                "stage": "model",
                "round": state["round"],
                "status": "settled",
                "input_tokens": 19_900_000,
                "output_tokens": 100_000,
                "charged_tokens": 20_000_000,
                "charged_seconds": 10.0,
                "elapsed_seconds": 10.0,
                "input_reservation": 19_900_000,
                "output_reservation": 100_000,
            }
        )

    runtime.store.transaction(spend_all_20m)
    with pytest.raises(AgentError, match="agent_token_budget_exhausted"):
        runtime.add_message(
            created["id"],
            {"message": "Try continuing beyond owner cap", "expected_revision": done["revision"]},
        )
    after = runtime.get(created["id"])
    assert after["budget"]["limits"]["max_total_tokens"] == 20_000_000
    assert not any(ev.get("type") == "budget_auto_extended" for ev in after["events"])


def test_t4_default_model_calls_extend_past_1024_with_auto_extended_event():
    row = _new_default_row()
    state = budget.initialize(row)
    state["limits"]["max_total_model_calls"] = 1024
    state["legacy_model_calls"] = 1024

    rec = budget.reserve(row, "model", "main", input_bound=100, output_bound=50)
    assert rec["kind"] == "model"
    assert state["limits"]["max_total_model_calls"] == 1024 + budget.DEFAULT_LIMITS["max_total_model_calls"]
    ext_events = [ev for ev in row["events"] if ev.get("type") == "budget_auto_extended"]
    assert len(ext_events) == 1
    assert ext_events[0]["reason"] == "default_policy"
    assert ext_events[0]["extended"]["max_total_model_calls"] == 1152

    # Also verify ensure_default_room grows default tokens beyond old 20M ceiling without clamping.
    state["limits"]["max_total_tokens"] = 20_000_000
    budget.settle(row, rec["id"], usage={"input_tokens": 20_000_000, "output_tokens": 0}, elapsed_seconds=1.0)
    budget.ensure_default_room(row, "max_total_tokens", 500_000)
    assert state["limits"]["max_total_tokens"] >= 20_500_000


def test_t5_owner_max_total_model_calls_50_stops_at_50():
    row = _new_default_row()
    budget.set_owner_limits(row, {"max_total_model_calls": 50})
    row["budget"]["legacy_model_calls"] = 50
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        budget.reserve(row, "model", "main", input_bound=10, output_bound=10)
    assert row["budget"]["limits"]["max_total_model_calls"] == 50


def test_t6_time_remaining_inf_for_default_and_finite_for_owner(tmp_path):
    default_row = _new_default_row()
    budget.initialize(default_row)
    default_row["budget"]["legacy_seconds"] = 90_000.0
    rem_default = budget.time_remaining(default_row)
    assert math.isinf(rem_default) and rem_default > 0
    assert math.isinf(budget.run_seconds_left(default_row, rem_default))

    owner_row = _new_default_row()
    budget.set_owner_limits(owner_row, {"max_total_seconds": 100})
    owner_row["budget"]["legacy_seconds"] = 35.0
    assert budget.time_remaining(owner_row) == pytest.approx(65.0)
    owner_row["limits_source"]["max_seconds"] = "owner"
    owner_row["limits"]["max_seconds"] = 40
    owner_row["elapsed_seconds"] = 10.0
    assert budget.run_seconds_left(owner_row, budget.time_remaining(owner_row)) == pytest.approx(30.0)

    # Verify runtime claim + deadline calculation with supports_request_deadline handles inf cleanly.
    seen_deadlines: list[object] = []
    runtime, orig_model, _, _ = make_runtime(tmp_path, assistant("Done with inf lifetime."))

    class DeadlineModel:
        supports_request_deadline = True

        def complete(self, model_id, messages, tool_specs, cancelled, on_event=None, **kwargs):
            seen_deadlines.append(kwargs.get("deadline"))
            if callable(kwargs.get("on_request_policy")):
                kwargs["on_request_policy"](
                    {
                        "enforcement": "adapter_timeout_and_cancel_watch",
                        "provider_timeout_seconds": 120.0,
                        "effective_timeout_seconds": None,
                    }
                )
            return orig_model.complete(model_id, messages, tool_specs, cancelled, on_event)

    runtime.model = DeadlineModel()
    created = runtime.create({"message": "Check infinite lifetime claim"})
    advance(runtime, 5)
    finished = runtime.get(created["id"])
    assert finished["status"] == "completed"
    assert seen_deadlines == [None]


def test_t7_legacy_task_with_default_limits_and_no_budget_changed_migrates_to_default():
    legacy = {
        "limits": {"max_steps": 64, "max_seconds": 900},
        "steps": 12,
        "elapsed_seconds": 45.0,
        "revision": 2,
        "event_cursor": 2,
        "events": [{"seq": 1, "type": "created"}, {"seq": 2, "type": "model_started"}],
        "budget": {
            "limits": {
                "max_total_model_calls": 128,
                "max_total_tool_calls": 256,
                "max_total_tokens": 2_000_000,
                "max_total_seconds": 3600,
            },
            "round": 1,
            "records": [],
            "legacy_usage_unknown": True,
            "legacy_model_calls": 12,
            "legacy_tool_calls": 0,
            "legacy_seconds": 45.0,
            "limit_sources": {
                "max_total_model_calls": "legacy_unknown",
                "max_total_tool_calls": "legacy_unknown",
                "max_total_tokens": "legacy_unknown",
                "max_total_seconds": "legacy_unknown",
            },
        },
    }
    pub = budget.public(legacy)
    assert set(pub["limit_sources"].values()) == {"default"}
    assert budget.segment_source(legacy, "max_steps") == "default"
    assert budget.segment_source(legacy, "max_seconds") == "default"
    mig_events = [ev for ev in legacy["events"] if ev.get("type") == "budget_source_migrated"]
    assert len(mig_events) == 1

    # Calling initialize again does not emit duplicate budget_source_migrated events.
    budget.initialize(legacy)
    assert len([ev for ev in legacy["events"] if ev.get("type") == "budget_source_migrated"]) == 1


def test_t8_legacy_task_with_non_default_limit_300_stays_hard_legacy_unknown():
    legacy = {
        "limits": {"max_steps": 100, "max_seconds": 900},
        "steps": 5,
        "elapsed_seconds": 10.0,
        "revision": 1,
        "event_cursor": 1,
        "events": [{"seq": 1, "type": "created"}],
        "budget": {
            "limits": {
                "max_total_model_calls": 300,
                "max_total_tool_calls": 256,
                "max_total_tokens": 2_000_000,
                "max_total_seconds": 3600,
            },
            "round": 1,
            "records": [],
            "legacy_usage_unknown": True,
            "legacy_model_calls": 5,
            "legacy_tool_calls": 0,
            "legacy_seconds": 10.0,
            "limit_sources": {
                "max_total_model_calls": "legacy_unknown",
                "max_total_tool_calls": "legacy_unknown",
                "max_total_tokens": "legacy_unknown",
                "max_total_seconds": "legacy_unknown",
            },
        },
    }
    pub = budget.public(legacy)
    assert pub["limit_sources"]["max_total_model_calls"] == "legacy_unknown"
    assert budget.is_hard(legacy, "max_total_model_calls") is True
    assert budget.segment_source(legacy, "max_steps") == "legacy_unknown"
    assert pub["limit_sources"]["max_total_tool_calls"] == "default"


def test_t9_legacy_task_with_budget_changed_event_does_not_migrate():
    legacy = {
        "limits": {"max_steps": 64, "max_seconds": 900},
        "steps": 5,
        "elapsed_seconds": 10.0,
        "revision": 2,
        "event_cursor": 2,
        "events": [{"seq": 1, "type": "created"}, {"seq": 2, "type": "budget_changed"}],
        "budget": {
            "limits": dict(budget.DEFAULT_LIMITS),
            "round": 1,
            "records": [],
            "legacy_usage_unknown": True,
            "legacy_model_calls": 5,
            "legacy_tool_calls": 0,
            "legacy_seconds": 10.0,
            "limit_sources": dict.fromkeys(budget.DEFAULT_LIMITS, "legacy_unknown"),
        },
    }
    pub = budget.public(legacy)
    assert set(pub["limit_sources"].values()) == {"legacy_unknown"}
    assert budget.segment_source(legacy, "max_steps") == "legacy_unknown"
    assert not any(ev.get("type") == "budget_source_migrated" for ev in legacy["events"])


def test_t10_update_budget_release_legacy_marks_default_without_changing_numbers(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    created = runtime.create({"message": "Legacy task stopped at 300"})
    advance(runtime, 5)

    def make_stopped_legacy(data):
        row = data["tasks"][created["id"]]
        state = budget.initialize(row)
        state["limits"]["max_total_model_calls"] = 300
        state["limit_sources"]["max_total_model_calls"] = "legacy_unknown"
        state["limits"]["max_total_tokens"] = 5_000_000
        state["limit_sources"]["max_total_tokens"] = "owner"
        row["limits"]["max_steps"] = 40
        row["limits_source"] = {"max_steps": "legacy_unknown", "max_seconds": "default"}
        budget.stop_for_limit(row, "agent_model_call_budget_exhausted")

    runtime.store.transaction(make_stopped_legacy)
    stopped = runtime.get(created["id"])
    assert stopped["outcome"]["budget_stop"]["source"] == "legacy_unknown"

    released = runtime.update_budget(
        created["id"],
        {
            "limits": {},
            "release_legacy": True,
            "expected_control_revision": stopped["control_revision"],
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert released["budget"]["limits"]["max_total_model_calls"] == 300
    assert released["budget"]["limit_sources"]["max_total_model_calls"] == "default"
    # Owner hard limits stay owner and keep their value.
    assert released["budget"]["limits"]["max_total_tokens"] == 5_000_000
    assert released["budget"]["limit_sources"]["max_total_tokens"] == "owner"
    assert released["limits"]["max_steps"] == 40
    assert released["limits_source"]["max_steps"] == "default"


def test_t11_no_progress_task_still_stops_with_agent_no_progress_after_two_empty_segments(tmp_path, monkeypatch):
    monkeypatch.setitem(store_module.DEFAULT_SETTINGS, "max_seconds", 10)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(runtime_module, "time", fake_time)
    monkeypatch.setattr(loop_module, "time", fake_time)

    runtime, model, _, _ = make_runtime(tmp_path)
    responses = [
        assistant(name="inspect_service", args={"bad": 1}),
        assistant(name="inspect_service", args={"bad": 2}),
        assistant("Should never reach third segment"),
    ]

    def complete(model_id, messages, specs, cancelled, on_event):
        model.calls.append(messages)
        clock[0] += 11.0
        return responses.pop(0)

    model.complete = complete
    created = runtime.create({"message": "Keep failing without progress"})
    advance(runtime, 30)
    done = runtime.get(created["id"])
    assert done["status"] == "failed"
    assert done["error"] == "agent_no_progress"
    assert len(model.calls) == 2


def test_t12_owner_max_total_model_calls_accepts_50000_rejects_100001_and_public_ranges_match():
    validated = budget.validate_limits(
        {
            "max_total_model_calls": 50_000,
            "max_total_tool_calls": 400_000,
            "max_total_tokens": 2_000_000_000,
            "max_total_seconds": 2_592_000,
        }
    )
    assert validated["max_total_model_calls"] == 50_000
    assert validated["max_total_tool_calls"] == 400_000
    assert validated["max_total_tokens"] == 2_000_000_000
    assert validated["max_total_seconds"] == 2_592_000

    with pytest.raises(AgentError, match="agent_invalid_budget"):
        budget.validate_limits({"max_total_model_calls": 100_001})

    row = _new_default_row()
    pub = budget.public(row)
    assert {k: tuple(v) for k, v in pub["ranges"].items()} == budget.RANGES


def test_t13_owner_hard_limit_at_85_percent_appears_in_public_near_limit():
    row = _new_default_row()
    budget.set_owner_limits(row, {"max_total_model_calls": 100, "max_total_tokens": 10_000})
    row["budget"]["legacy_model_calls"] = 84
    assert budget.public(row)["near_limit"] == []

    row["budget"]["legacy_model_calls"] = 85
    assert budget.public(row)["near_limit"] == ["max_total_model_calls"]

    # Default limits at >=85% do NOT appear in near_limit because they auto-extend.
    row["budget"]["legacy_tool_calls"] = 250  # 250 / 256 > 85%, but source is "default"
    assert "max_total_tool_calls" not in budget.public(row)["near_limit"]


def test_t14_quick_extend_adds_one_default_step(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    created = runtime.create(
        {"message": "Owner limit near exhaustion", "budget_limits": {"max_total_model_calls": 950}}
    )
    advance(runtime, 5)
    stopped = runtime.get(created["id"])
    new_limit = stopped["budget"]["limits"]["max_total_model_calls"] + budget.DEFAULT_LIMITS["max_total_model_calls"]
    updated = runtime.update_budget(
        created["id"],
        {
            "limits": {"max_total_model_calls": new_limit},
            "expected_control_revision": stopped["control_revision"],
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert updated["budget"]["limits"]["max_total_model_calls"] == 1078
    assert updated["budget"]["limit_sources"]["max_total_model_calls"] == "owner"
