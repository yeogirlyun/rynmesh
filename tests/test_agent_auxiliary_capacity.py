"""Auxiliary model requests obey the same calibrated input capacity as the main loop.

Provider usage is synthetic (it sets the observed bytes/token density) and the
models are deterministic stand-ins. This is runtime admission evidence, not
proof of a real upstream overflow.
"""
from __future__ import annotations

import base64
import copy
import io
import json

import pytest
from PIL import Image
from test_agent_context_compaction_policy import WindowCatalog
from test_agent_evidence_semantics import remote_catalog
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget, context_compaction, output_policy, verification
from rynmesh.agent_runtime.store import AgentError, encoded

WINDOW, OUTPUT = 32_000, 4096
TOKEN_CAPACITY = WINDOW - OUTPUT - output_policy.FRAMING_RESERVE_TOKENS  # input tokens left in the window
HARD_LIMIT = TOKEN_CAPACITY * output_policy.ESTIMATED_BYTES_PER_TOKEN
OWNER = "检查服务目录并说明远端授权状态，保留原始证据。" + "要求原文保留。" * 700
ANSWER = "远端授权状态未知，目录只证明本机观察。" + "补充说明。" * 900


def observe_density(runtime, task_id, capacity, *, model_id="local:test"):
    """Record synthetic provider usage whose bytes/token ratio yields ``capacity`` request bytes."""
    def record(data):
        state = budget.initialize(data["tasks"][task_id])
        policy = {"projected_context_utf8_bytes": capacity, "estimated_image_tokens": 0}
        if model_id is not None:
            policy["model_id"] = model_id
        state["records"].append({
            "id": "usage_synthetic", "kind": "model", "stage": "model", "round": state["round"],
            "status": "settled", "input_tokens": TOKEN_CAPACITY, "output_tokens": 10, "charged_tokens": 10,
            "charged_seconds": 0.0, "elapsed_seconds": 0.0, "input_reservation": 0, "output_reservation": 0,
            "output_policy": policy})
    runtime.store.transaction(record)
    return runtime._effective_capacity(runtime.store.read()["tasks"][task_id])[0]


def request_size(messages, specs=()):
    return len(encoded(messages)) + len(encoded(list(specs)))


class CatalogTools:
    """One registered read whose receipt yields an optional checker observation contract."""

    def __init__(self):
        self.calls = []

    def specs(self):
        return [{"type": "function", "function": {"name": "ryn_inspect_services", "description": "Inspect services",
                 "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]

    def metadata(self, name):
        if name != "ryn_inspect_services":
            raise ValueError("unavailable")
        return {"effect": "read", "title": name}

    def execute(self, name, arguments, *, operation_id, cancelled):
        self.calls.append(name)
        return remote_catalog()

    # No mechanical verifier: the answer goes straight to the semantic checker.
    def verification_specs(self):
        return []

    def verify(self, receipt, criteria, *, context, cancelled):
        raise AssertionError("no mechanical check is registered")


def answered(tmp_path):
    """Run the real loop until the candidate answer awaits its semantic checker."""
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.tools = CatalogTools()
    runtime.catalog = WindowCatalog(WINDOW, OUTPUT)
    runtime.update_settings({"verify_text_responses": True})
    calls = {"main": [], "checker": []}

    def complete(model_id, messages, specs, cancelled, on_event):
        if specs:
            calls["main"].append(request_size(messages, specs))
            if not any(m["role"] == "tool" for m in messages):
                return assistant(name="ryn_inspect_services", args={}, call_id="inspect")
            return assistant(ANSWER)
        calls["checker"].append((copy.deepcopy(messages), request_size(messages)))
        payload = json.loads(messages[-1]["content"])
        return assistant(json.dumps({"status": "passed", "summary": "Synthetic checker", "uncovered_requirements": [],
                                     "evidence_refs": [item["id"] for item in payload["evidence"]]}))

    model.complete = complete
    task = runtime.create({"message": OWNER})
    for _ in range(8):
        advance(runtime)
        row = runtime.store.read()["tasks"][task["id"]]
        if row.get("verification") and row["status"] == "queued":
            break
    assert row["result"] == ANSWER and len(calls["main"]) == 2 and not calls["checker"]
    assert all(size <= HARD_LIMIT for size in calls["main"])
    return runtime, task["id"], calls


def checker_sizes(runtime, task_id):
    """(request bytes without optional contracts, with them) as the runtime would build them."""
    row = copy.deepcopy(runtime.store.read()["tasks"][task_id])
    core = verification.checker_messages(copy.deepcopy(row), context_limit=1)
    full = verification.checker_messages(copy.deepcopy(row), context_limit=10 ** 7)
    assert json.loads(full[-1]["content"])["observation_contracts"]
    return request_size(core), request_size(full)


def model_calls(runtime, task_id):
    return budget.public(runtime.store.read()["tasks"][task_id])["lifetime"]["charged_model_calls"]


def checker_ran(row):
    """The checker's assessment was recorded. Semantic-only checks stay 'unverified' by design."""
    outcome = row["outcome"]
    return (row["status"] == "completed" and (outcome.get("semantic_assessment") or {}).get("status") == "passed"
            and "agent_verification_context_limit" not in outcome.get("uncertainties", []))


# --- Real checker call chain (loop -> _verify -> _complete -> budget meter -> model) ------------


def test_checker_within_calibrated_capacity_runs_with_complete_evidence(tmp_path):
    runtime, task_id, calls = answered(tmp_path)
    core, full = checker_sizes(runtime, task_id)
    capacity = observe_density(runtime, task_id, full + 2000)
    assert full < capacity < HARD_LIMIT
    before = model_calls(runtime, task_id)
    advance(runtime)
    assert len(calls["checker"]) == 1
    messages, size = calls["checker"][0]
    payload = json.loads(messages[-1]["content"])
    assert size <= capacity and payload["observation_contracts_omitted"] == 0
    assert payload["candidate_answer"] == ANSWER
    done = runtime.store.read()["tasks"][task_id]
    assert model_calls(runtime, task_id) == before + 1
    assert done["budget"]["records"][-1]["stage"] == "verification_model"
    assert checker_ran(done)
    # Real main requests are tagged with their model for later calibration.
    main_records = [r for r in done["budget"]["records"] if r["stage"] == "model" and r["id"] != "usage_synthetic"]
    assert main_records and all(r["output_policy"]["model_id"] == "local:test" for r in main_records)


def test_checker_drops_optional_metadata_to_fit_calibrated_capacity(tmp_path):
    runtime, task_id, calls = answered(tmp_path)
    core, full = checker_sizes(runtime, task_id)
    capacity = observe_density(runtime, task_id, core + 64)
    assert core <= capacity < full < HARD_LIMIT  # the byte limit alone would keep the metadata
    advance(runtime)
    assert len(calls["checker"]) == 1
    messages, size = calls["checker"][0]
    payload = json.loads(messages[-1]["content"])
    assert size <= capacity
    assert payload["observation_contracts"] == [] and payload["observation_contracts_omitted"] == 1
    # Required facts are never trimmed.
    assert payload["candidate_answer"] == ANSWER
    assert [m["content"] for m in payload["owner_information_in_order"]] == [OWNER]
    assert [c["text"] for c in payload["owner_requirements_in_order"]] == [OWNER]
    assert payload["receipts"] and payload["receipts"][0]["result"] == remote_catalog()
    assert checker_ran(runtime.get(task_id))


def test_checker_core_over_calibrated_capacity_is_an_explicit_error_without_dispatch(tmp_path):
    runtime, task_id, calls = answered(tmp_path)
    core, _ = checker_sizes(runtime, task_id)
    capacity = observe_density(runtime, task_id, core - 500)
    assert capacity < core <= HARD_LIMIT  # admitted by the byte limit alone
    before = model_calls(runtime, task_id)
    for _ in range(3):  # no retry loop
        advance(runtime)
    done = runtime.get(task_id)
    assert not calls["checker"] and model_calls(runtime, task_id) == before
    assert not any(e["type"] == "verification_model_started" for e in done["events"])
    assert done["status"] == "completed" and done["outcome"]["status"] == "unverified"
    assert "agent_verification_context_limit" in done["outcome"]["uncertainties"]
    assert done["result"] == ANSWER  # the candidate answer is kept, only unverified


def test_another_models_usage_does_not_tighten_the_checker(tmp_path):
    runtime, task_id, calls = answered(tmp_path)
    core, _ = checker_sizes(runtime, task_id)
    capacity = observe_density(runtime, task_id, core - 500, model_id="local:other")
    assert capacity == HARD_LIMIT  # a different tokenizer's ratio is not applied
    advance(runtime)
    assert len(calls["checker"]) == 1
    assert checker_ran(runtime.get(task_id))


# --- Real image-inspection call chain (tool context -> _complete -> budget meter -> model) -----


def png_data_url(size=32):
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (0, 64, 255)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def image_request(text):
    return [{"role": "user", "content": [{"type": "text", "text": text},
                                         {"type": "image_url", "image_url": {"url": png_data_url(), "detail": "high"}}]}]


def running_image_task(tmp_path, capacity):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("蓝色方块。"))
    model.supports_image_input = True  # local API route with an image carrier
    runtime.catalog = WindowCatalog(WINDOW, OUTPUT)
    task = runtime.create({"message": "检查图片"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(
        status="running", in_flight={"kind": "tool", "dispatched": True}))
    effective = observe_density(runtime, task["id"], capacity)
    row = runtime.store.read()["tasks"][task["id"]]
    inspect = runtime._tool_context({**row, "_run_control_revision": row["control_revision"]})["_vision_inference"]
    return runtime, model, task["id"], inspect, effective


def test_image_inspection_within_capacity_is_metered_and_dispatched(tmp_path):
    runtime, model, task_id, inspect, capacity = running_image_task(tmp_path, 50_000)
    messages = image_request("描述这张图片。")
    assert inspect(messages, lambda: False) == "蓝色方块。"
    assert len(model.calls) == 1
    records = runtime.store.read()["tasks"][task_id]["budget"]["records"]
    assert records[-1]["stage"] == "image_inspection" and records[-1]["output_policy"]["model_id"] == "local:test"


def test_image_inspection_over_capacity_is_rejected_before_metering(tmp_path):
    runtime, model, task_id, inspect, capacity = running_image_task(tmp_path, 50_000)
    density = capacity / TOKEN_CAPACITY
    image_tokens = output_policy.analyze_request(image_request(""), [])["estimated_image_tokens"]
    assert image_tokens > 0
    # Text fills the space left after the image at the observed density.
    text = "测" * ((capacity - int(image_tokens * density)) // 3 + 300)
    analysis = output_policy.analyze_request(image_request(text), [])
    assert context_compaction.request_bytes_at_density(analysis, density) > capacity
    assert analysis["projected_context_utf8_bytes"] <= HARD_LIMIT  # the byte limit alone admits it
    records = len(runtime.store.read()["tasks"][task_id]["budget"]["records"])
    with pytest.raises(AgentError, match="agent_context_limit"):
        inspect(image_request(text), lambda: False)
    assert not model.calls  # not retried, not dispatched
    assert len(runtime.store.read()["tasks"][task_id]["budget"]["records"]) == records  # nothing reserved


# --- Shared admission for requests that fit ------------------------------------------------------


@pytest.mark.parametrize("nested,stage", [(True, "image_inspection"), (False, "verification_model")])
def test_auxiliary_request_within_calibrated_capacity_still_dispatches(tmp_path, monkeypatch, nested, stage):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("OK"))
    runtime.catalog = WindowCatalog(WINDOW, OUTPUT)
    task = runtime.create({"message": "Chinese capacity boundary"})
    capacity = observe_density(runtime, task["id"], 49_000)
    row = runtime.store.read()["tasks"][task["id"]]
    messages = [{"role": "user", "content": "测" * (capacity // 3 - 100)}]
    assert request_size(messages) <= capacity
    monkeypatch.setattr(runtime, "_meter_model", lambda row, invoke, **kw: invoke(4096))
    runtime._complete(row, messages, [], lambda: False, lambda _: None, nested=nested, stage=stage)
    assert len(model.calls) == 1


# --- Which observations may calibrate a request ---------------------------------------------------


def test_density_applies_only_to_the_current_models_text_usage():
    def record(policy):
        return {"kind": "model", "stage": "model", "status": "settled", "input_tokens": 10_000,
                "output_policy": {"projected_context_utf8_bytes": 18_000, **policy}}
    same = {"model_id": "m1", "budget": {"records": [record({"model_id": "m1"})]}}
    assert context_compaction.observed_bytes_per_token(same) == 1.8
    other = {"model_id": "m2", "budget": {"records": [record({"model_id": "m1"})]}}
    assert context_compaction.observed_bytes_per_token(other) is None
    untagged = {"model_id": "m1", "budget": {"records": [record({})]}}
    assert context_compaction.observed_bytes_per_token(untagged) == 1.8
    switched = {**untagged, "model_history": [{"model_id": "m0"}]}
    assert context_compaction.observed_bytes_per_token(switched) is None
    image = {"model_id": "m1", "budget": {"records": [record({"model_id": "m1", "estimated_image_tokens": 1036})]}}
    assert context_compaction.observed_bytes_per_token(image) is None


def test_image_tokens_use_the_density_instead_of_the_default_bytes():
    analysis = output_policy.analyze_request(image_request("看图"), [])
    tokens, text = analysis["estimated_image_tokens"], analysis["text_projection_utf8_bytes"]
    assert context_compaction.request_bytes_at_density(analysis, None) == analysis["projected_context_utf8_bytes"]
    assert context_compaction.request_bytes_at_density(analysis, 1.8) == text + -(-tokens * 18 // 10)
    policy = output_policy.context_budget({"context_window": WINDOW, "max_output_tokens": OUTPUT})
    assert context_compaction.applied_density(policy, 1.8) == 1.8
    assert context_compaction.applied_density(policy, 3.6) is None  # sparser text never loosens the limit
    assert context_compaction.applied_density(output_policy.context_budget({}), 1.8) is None  # unknown window
