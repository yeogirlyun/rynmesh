"""S03-I29 default accounting allowances never manufacture context pressure."""
import copy
import json

import pytest
from test_agent_context_compaction_policy import WindowCatalog, exchange, fill_until_near_limit, pressure, seed
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget, output_policy


def spy_compaction(runtime, monkeypatch):
    calls = []
    real_compact = runtime._compact

    def compact(selected, cancelled, started):
        calls.append(selected)
        return real_compact(selected, cancelled, started)

    monkeypatch.setattr(runtime, "_compact", compact)
    return calls


def prime_one_turn_remaining(runtime, task_id, messages):
    row = seed(runtime, task_id, messages)
    specs = runtime._specs(row)
    policy = output_policy.select(runtime._model_metadata(row["model_id"]), runtime._context(row), specs)
    one_turn = policy["estimated_input_tokens"] + policy["effective_cap"]
    remaining = one_turn + 1000

    def spend(data):
        live = data["tasks"][task_id]
        live["steps"] = 1
        state = budget.initialize(live)
        charged = state["limits"]["max_total_tokens"] - remaining
        state["records"].append({"id": "usage_prior_work", "kind": "model", "stage": "model",
            "round": state["round"], "status": "settled", "input_tokens": charged, "output_tokens": 0,
            "charged_tokens": charged, "charged_seconds": 0.0, "elapsed_seconds": 0.0,
            "input_reservation": charged, "output_reservation": 0})
    runtime.store.transaction(spend)
    return runtime.store.read()["tasks"][task_id]


@pytest.mark.parametrize("compactable", [True, False])
def test_t1_default_low_allowance_at_twenty_percent_extends_without_compaction(tmp_path, monkeypatch, compactable):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Inspection complete."), assistant("Inspection complete."))
    runtime.catalog = WindowCatalog(64_000, 4096)
    task_id = runtime.create({"message": "Inspect services without changes."})["id"]
    history = [*exchange("a", "a" * 7000), *exchange("b", "b" * 7000)] if compactable else [
        {"role": "assistant", "content": "Working notes: " + "x" * 14000, "context_epoch": 0}]
    row = prime_one_turn_remaining(runtime, task_id, history)
    snapshot = copy.deepcopy(row["messages"])
    limit = runtime._context_limit(row)
    decision = pressure(runtime, row)
    assert 0.15 < decision["request_bytes"] / decision["capacity_bytes"] < 0.25
    assert decision["compact"] is False
    assert runtime._has_compactable_history(row, min_non_user=2) is compactable
    assert budget.is_hard(row, "max_total_tokens") is False
    calls = spy_compaction(runtime, monkeypatch)
    advance(runtime)
    done = runtime.store.read()["tasks"][task_id]
    assert calls == []
    assert done["status"] == "completed" and done["result"] == "Inspection complete."
    assert len(model.calls) == 1 and model.calls[0][2]
    assert done["messages"][:-1] == snapshot and "context_checkpoint" not in done
    assert done["budget"]["limits"]["max_total_tokens"] == 4_000_000
    extended = [e for e in done["events"] if e["type"] == "budget_auto_extended"]
    assert len(extended) == 1
    assert extended[0]["previous_max_total_tokens"] == 2_000_000
    assert extended[0]["new_max_total_tokens"] == 4_000_000
    assert not any(e["type"] in {"budget_compaction_continued", "context_compaction_started", "context_compacted"} for e in done["events"])
    assert decision["request_bytes"] < limit


def test_t2_context_pressure_still_runs_real_compaction(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant("Goal: inspect services. Findings: observations collected. Next: report."),
        assistant("Inspection complete."))
    runtime.catalog = WindowCatalog(32_000, 4096)
    task_id = runtime.create({"message": "Inspect every service; never change anything."})["id"]
    row = runtime.store.read()["tasks"][task_id]
    row, _ = fill_until_near_limit(runtime, task_id, "x" * 5000, limit=runtime._context_limit(row))
    decision = pressure(runtime, row)
    assert decision["compact"] and decision["request_bytes"] <= decision["capacity_bytes"]
    calls = spy_compaction(runtime, monkeypatch)
    advance(runtime)
    done = runtime.store.read()["tasks"][task_id]
    assert len(calls) == 1 and calls[0]["_compaction_pressure"]["compact"] is True
    assert done["context_checkpoint"]["content"]
    assert any(e["type"] == "context_compacted" for e in done["events"])
    assert done["status"] == "completed" and len(model.calls) == 2


def test_t3_owner_hard_limit_keeps_wrap_up_without_compacting_or_extending(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Progress summary; work remains."))
    runtime.catalog = WindowCatalog(64_000, 4096)
    task_id = runtime.create({"message": "Inspect services.", "budget_limits": {"max_total_tokens": 2_000_000}})["id"]
    row = prime_one_turn_remaining(runtime, task_id, [*exchange("a", "a" * 7000), *exchange("b", "b" * 7000)])
    assert budget.is_hard(row, "max_total_tokens") is True and pressure(runtime, row)["compact"] is False
    calls = spy_compaction(runtime, monkeypatch)
    advance(runtime)
    done = runtime.store.read()["tasks"][task_id]
    assert calls == [] and len(model.calls) == 1
    assert "Server budget wrap-up notice (token_budget_low)" in json.dumps(model.calls[0][1])
    assert done["status"] == "failed" and done["error"] == "agent_token_budget_exhausted"
    assert done["outcome"]["budget_wrap_up_reason"] == "token_budget_low"
    assert done["budget"]["limits"]["max_total_tokens"] == 2_000_000
    assert not any(e["type"] in {"budget_auto_extended", "context_compaction_started", "context_compacted"} for e in done["events"])
