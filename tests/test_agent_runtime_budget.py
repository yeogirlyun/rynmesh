"""Dispatch admission and restart-safe unknown accounting."""
import pytest

from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.store import AgentError


def task(**limits):
    return {"limits": {"max_seconds": 30, **limits}, "steps": 0, "elapsed_seconds": 0}


def test_all_model_stages_share_one_lifetime_call_budget_and_round_does_not_reset_it():
    row = task(max_total_model_calls=3)
    for stage in ("main", "compaction", "verification"):
        item = budget.reserve(row, "model", stage, input_bound=100, output_bound=20)
        budget.settle(row, item["id"], usage={"input_tokens": 9, "output_tokens": 4}, elapsed_seconds=1)
    budget.new_round(row)
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        budget.reserve(row, "model", "main")
    result = budget.public(row)
    assert result["current_round"]["model_calls"] == 0 and result["lifetime"]["model_calls"] == 3
    assert result["lifetime"]["total_tokens"] == 39
    assert result["lifetime"]["cost"] is None


def test_unknown_usage_retains_conservative_charge_and_known_zero_is_distinct():
    row = task(max_total_tokens=1000)
    one = budget.reserve(row, "model", "main", input_bound=400, output_bound=200)
    budget.settle(row, one["id"], usage={"input_tokens": 300}, elapsed_seconds=2)
    assert budget.public(row)["lifetime"]["total_tokens"] is None
    assert budget.public(row)["lifetime"]["charged_tokens"] == 500
    with pytest.raises(AgentError, match="agent_token_budget_exhausted"):
        budget.reserve(row, "model", "main", input_bound=300, output_bound=300)
    two = budget.reserve(row, "model", "verification", input_bound=200, output_bound=200)
    budget.settle(row, two["id"], usage={"input_tokens": 0, "output_tokens": 0}, elapsed_seconds=0)
    assert row["budget"]["records"][-1]["charged_tokens"] == 0
    assert budget.public(row)["lifetime"]["charged_tokens"] == 500


def test_restart_never_refunds_unobserved_usage_and_duplicate_settlement_cannot_change_it():
    row = task(max_total_seconds=30)
    item = budget.reserve(row, "model", "main", input_bound=200, output_bound=100)
    budget.abandon(row)
    budget.settle(row, item["id"], usage={"input_tokens": 0, "output_tokens": 0}, elapsed_seconds=0)
    result = budget.public(row)["lifetime"]
    assert result["charged_tokens"] == 300 and result["charged_seconds"] == 30
    assert result["active_seconds"] is None and result["unsettled_operations"] == 0
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        budget.reserve(row, "tool", "verify")


def test_business_read_write_and_verification_admission_share_tool_budget():
    row = task(max_total_tool_calls=2)
    for stage in ("execute", "verify"):
        item = budget.reserve(row, "tool", stage)
        budget.settle(row, item["id"], elapsed_seconds=0.1)
    with pytest.raises(AgentError, match="agent_tool_budget_exhausted"):
        budget.reserve(row, "tool", "execute")


def test_legacy_usage_is_explicitly_incomplete_and_preserves_known_call_floor():
    row = task()
    row.update(steps=4, elapsed_seconds=12)
    result = budget.public(row)["lifetime"]
    assert result["model_calls"] is None and result["charged_model_calls"] == 4 and result["total_tokens"] is None
    assert result["active_seconds"] is None and result["charged_seconds"] == 12
    assert result["usage_complete"] is False


def test_legacy_tool_history_is_a_floor_not_false_zero_and_still_limits_dispatch():
    row = task(max_total_tool_calls=1)
    row["events"] = [{"type": "tool_started", "operation_id": "original-op"}]
    state = budget.public(row)
    assert state["lifetime"]["tool_calls"] is None
    assert state["lifetime"]["charged_tool_calls"] == 1
    assert state["current_round"]["total_tokens"] is None
    with pytest.raises(AgentError, match="agent_tool_budget_exhausted"):
        budget.reserve(row, "tool", "execute")
    budget.new_round(row)
    assert budget.public(row)["current_round"]["tool_calls"] == 0
    assert budget.public(row)["lifetime"]["tool_calls"] is None
