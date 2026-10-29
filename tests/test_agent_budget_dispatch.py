"""Budget limits at actual durable runtime dispatch boundaries."""
import copy

from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore


def test_model_limit_stops_before_another_provider_request(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "svc"}), assistant("unused"))
    row = runtime.create({"message": "Inspect service", "budget_limits": {"max_total_model_calls": 1}})
    advance(runtime, 4)
    done = runtime.get(row["id"])
    assert done["status"] == "failed" and done["error"] == "agent_model_call_budget_exhausted"
    assert len(model.calls) == 1 and len(tools.calls) == 1
    assert done["budget"]["lifetime"]["model_calls"] == 1
    assert done["budget"]["lifetime"]["total_tokens"] is None


def test_tool_limit_rejects_approved_write_before_mutation(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "svc"}),
        assistant(name="share_service", args={"service_id": "svc"}))
    row = runtime.create({"message": "Inspect then share", "budget_limits": {"max_total_tool_calls": 1}})
    advance(runtime, 4)
    approve(runtime, row["id"])
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "failed" and done["error"] == "agent_tool_budget_exhausted"
    assert tools.shared is False and len(tools.calls) == 1
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["receipts"][-1]["result"]["not_executed"] is True


def test_small_token_budget_rejects_before_inference(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("unused"))
    row = runtime.create({"message": "Inspect", "budget_limits": {"max_total_tokens": 1000}})
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["error"] == "agent_token_budget_exhausted" and model.calls == []


def test_usage_accounting_survives_actual_store_reopen(tmp_path):
    response = assistant("No business proof")
    response.update(usage={"input_tokens": 90, "output_tokens": 12}, reported_usage={"input_tokens": None, "output_tokens": None})
    runtime, model, tools, key = make_runtime(tmp_path, response)
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    before = copy.deepcopy(runtime.get(row["id"])["budget"])
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restored.reconcile()
    after = restored.get(row["id"])["budget"]
    assert after == before
    assert after["lifetime"]["model_calls"] == 1 and after["lifetime"]["total_tokens"] is None
    assert after["lifetime"]["charged_tokens"] > 102  # Estimates are not measured usage.


def test_adapter_without_output_limit_cannot_underreserve_its_output(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("must not dispatch"))
    row = runtime.create({"message": "Inspect"})
    stored = runtime.store.read()["tasks"][row["id"]]
    input_bound = output_policy.estimate_input(runtime._context(stored), runtime._specs(stored))["estimated_input_tokens"]
    # Enough for input plus only 100 output tokens. This model cannot enforce
    # that reduced limit, so reserving 100 then requesting 4096 would overspend.
    def reduce_limit(data):
        data["tasks"][row["id"]]["budget"]["limits"]["max_total_tokens"] = input_bound + 100
    runtime.store.transaction(reduce_limit)
    advance(runtime)
    assert model.calls == []
    assert runtime.get(row["id"])["error"] == "agent_token_budget_exhausted"
