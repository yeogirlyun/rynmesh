"""S03-I8 provider-calibrated sparse-text capacity through real dispatch."""
import copy

import pytest
from test_agent_model_context_budget import configure, stored, window_model
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_model_switch import body as switch_body, setup as switch_setup

from rynmesh.agent_runtime import budget, context_compaction as cc, output_policy as op
from rynmesh.agent_runtime.store import AgentError


def observation(model_id="local:test", **policy_changes):
    return {"id": "density-fixture", "kind": "model", "stage": "model", "round": 0, "status": "settled",
            "input_tokens": 10000, "output_tokens": 20, "charged_tokens": 10020, "charged_seconds": 0.0,
            "elapsed_seconds": 0.0, "input_reservation": 0, "output_reservation": 0,
            "output_policy": {"model_id": model_id, "projected_context_utf8_bytes": 42000,
                              "estimated_image_tokens": 0, **policy_changes}}


def calibrate(runtime, tid, record=None):
    def seed(data):
        row = data["tasks"][tid]
        state = budget.initialize(row)
        item = copy.deepcopy(record or observation(row["model_id"]))
        item["round"] = state["round"]
        state["records"].append(item)
    runtime.store.transaction(seed)
    return stored(runtime, tid)


def available_tokens(policy):
    return policy["context_window_tokens"] - policy["reserved_output_tokens"] - policy["framing_reserve_tokens"]


def test_t1_observed_4_2_is_capped_at_4_and_relaxes_with_error_margin():
    row = {"model_id": "local:test", "budget": {"records": [observation()]}}
    policy = op.context_budget(window_model(32000, output=4096))
    ratio = cc.observed_bytes_per_token(row)
    assert ratio == 4.2 and cc.applied_density(policy, ratio) == 4.0
    capacity = cc.input_capacity(policy["effective_max_context_bytes"], policy, ratio)
    assert capacity == int(available_tokens(policy) * 4.0 * (1 - cc.ESTIMATE_ERROR))
    assert capacity > policy["effective_max_context_bytes"]
    assert cc.input_capacity(policy["effective_max_context_bytes"], policy, ratio, 0.8) == int(capacity * 0.8)


def test_t2_dense_2_1_keeps_existing_tightening():
    policy = op.context_budget(window_model(32000, output=4096))
    assert cc.applied_density(policy, 2.1) == 2.1
    assert cc.input_capacity(policy["effective_max_context_bytes"], policy, 2.1) == int(available_tokens(policy) * 2.1)


@pytest.mark.parametrize("case", ["none", "tiny", "image", "summary", "reserved", "other_model", "untagged_switch"])
def test_t3_ineligible_usage_never_relaxes(case):
    record = observation()
    row = {"model_id": "local:test", "budget": {"records": [record]}}
    if case == "none":
        row["budget"]["records"] = []
    elif case == "tiny":
        record["output_policy"]["projected_context_utf8_bytes"] = 8191
    elif case == "image":
        record["output_policy"]["estimated_image_tokens"] = 100
    elif case == "summary":
        record["stage"] = "compaction"
    elif case == "reserved":
        record["status"] = "reserved"
    elif case == "other_model":
        record["output_policy"]["model_id"] = "local:other"
    else:
        record["output_policy"].pop("model_id")
        row["model_history"] = [{"previous_model_id": "local:other"}]
    policy = op.context_budget(window_model(32000, output=4096))
    ratio = cc.observed_bytes_per_token(row)
    assert ratio is None
    assert cc.input_capacity(policy["effective_max_context_bytes"], policy, ratio) == policy["effective_max_context_bytes"]


def test_t3_actual_model_switch_discards_previous_tokenizer_calibration(tmp_path):
    runtime, _, _, _, tid, models = switch_setup(tmp_path)
    for metadata in models:
        metadata.update(context_window=32000, max_output_tokens=4096, context_window_known=True)
    row = calibrate(runtime, tid)
    assert runtime._effective_capacity(row)[0] > runtime._context_limit(row)
    runtime.switch_model(tid, switch_body(runtime, tid))
    row = stored(runtime, tid)
    assert runtime._effective_capacity(row) == (runtime._context_limit(row), None)


@pytest.mark.parametrize("manual", [60000, 120000])
def test_t4_manual_limit_remains_authoritative(manual):
    model = window_model(32000, output=4096)
    policy = op.context_budget(model, manual)
    assert cc.applied_density(policy, 4.2) is None
    assert cc.input_capacity(policy["effective_max_context_bytes"], policy, 4.2) == policy["effective_max_context_bytes"]
    with pytest.raises(AgentError, match="^agent_context_limit$"):
        op.admit_request_context(model, [{"role": "user", "content": "x" * 125000}], [],
                                 context_policy=policy, capacity_bytes=999999)


def test_t3_unknown_window_and_stricter_provider_admission_do_not_relax():
    for model in ({}, window_model(32000, output=4096, context_admission_bytes_per_token=1)):
        policy = op.context_budget(model)
        assert cc.input_capacity(policy["effective_max_context_bytes"], policy, 4.2) == policy["effective_max_context_bytes"]


def test_t4_explicit_manual_cap_is_not_exceeded_even_in_auto_policy():
    policy = op.context_budget(window_model(32000, output=4096))
    policy["manual_max_context_bytes"] = 90000
    assert cc.input_capacity(policy["effective_max_context_bytes"], policy, 4.2) == 90000


def test_t5_admission_uses_explicit_calibrated_capacity_but_keeps_transport_bounds():
    model = window_model(32000, output=4096)
    messages = [{"role": "user", "content": "x" * 97000}]
    policy = op.context_budget(model)
    with pytest.raises(AgentError, match="^agent_context_limit$"):
        op.admit_request_context(model, messages, [], context_policy=policy)
    capacity = cc.input_capacity(policy["effective_max_context_bytes"], policy, 4.2)
    analysis = op.admit_request_context(model, messages, [], context_policy=policy, capacity_bytes=capacity)
    assert policy["effective_max_context_bytes"] < analysis["projected_context_utf8_bytes"] < capacity
    with pytest.raises(AgentError, match="^agent_context_limit$"):
        op.admit_request_context(model, messages, [], context_policy=policy, capacity_bytes=capacity, max_raw_request_bytes=80000)


def test_t5_complete_admission_and_output_budget_allow_sparse_text_above_old_window(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Accepted full sparse request"))
    configure(runtime, window_model(32000, output=4096))
    tid = runtime.create({"message": "Inspect"})["id"]
    row = calibrate(runtime, tid)
    runtime.store.transaction(lambda data: data["tasks"][tid].update(status="running"))
    row = stored(runtime, tid)
    messages = [{"role": "user", "content": "x" * 97000}]
    response = runtime._complete(row, messages, runtime.tools.specs(), lambda: False, lambda *_: None)
    assert response["message"]["content"] == "Accepted full sparse request" and len(model.calls) == 1
    record = stored(runtime, tid)["budget"]["records"][-1]["output_policy"]
    assert record["effective_cap"] == 4096
    assert record["estimated_input_tokens"] < 32000 - 4096
    assert record["estimated_input_tokens"] == record["estimated_text_tokens"] + op.FRAMING_RESERVE_TOKENS


def test_t5_real_loop_does_not_compact_only_because_old_byte_limit_was_crossed(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Kept the complete evidence"))
    configure(runtime, window_model(32000, output=4096))
    tid = runtime.create({"message": "Inspect all recorded evidence"})["id"]
    row = calibrate(runtime, tid)
    def history(data):
        messages = data["tasks"][tid]["messages"]
        messages.append({"role": "assistant", "content": "SPARSE_HISTORY " + "x" * 75000})
        messages.extend({"role": "assistant", "content": f"Small completed step {i}"} for i in range(3))
    runtime.store.transaction(history)
    row = stored(runtime, tid)
    specs = runtime._specs(row)
    analysis = runtime._request_context_analysis(row, specs=specs)
    size = runtime._request_context_bytes(row, specs=specs, analysis=analysis)
    assert runtime._context_limit(row) < size < runtime._effective_capacity(row)[0]
    advance(runtime)
    final = stored(runtime, tid)
    assert runtime.get(tid)["status"] == "completed" and len(model.calls) == 1
    assert model.calls[0][2] and "SPARSE_HISTORY" in str(model.calls[0][1])
    assert not any(e["type"].startswith("context_compaction") for e in final["events"])
