"""A sharing receipt proves its committed model, not a later replacement."""
import copy

import pytest
from test_agent_atomic_sharing import CONTEXT, execute, open_tools, receipt, seed

from rynmesh.llm_package.peer_binding import model_binding


@pytest.mark.parametrize("change", ["disabled", "unready", "endpoint", "key", "policy", "endpoint_aba", "model_removed"])
def test_shared_target_change_fails_readback_but_keeps_executed_attribution(tmp_path, change):
    sources, _, tool, _, args = seed(tmp_path)
    result = execute(tool, args)
    source = next(iter(sources.read().values()))
    original_endpoint = source["base_url"]
    if change == "disabled":
        source["enabled"] = False
    elif change == "unready":
        source["status"] = "untested"
    elif change in {"endpoint", "endpoint_aba"}:
        source["base_url"] = "http://127.0.0.1:9998/v1"
    elif change == "key":
        source["api_key"] = "replacement-private-test-key"
    elif change == "policy":
        source["model_settings"]["model-b"]["overrides"]["max_concurrent"] = 2
    else:
        source["models"] = ["model-a"]
        source["shared_models"] = ["model-a"]
    sources.save(source)
    if change == "endpoint_aba":
        source["base_url"] = original_endpoint
        sources.save(source)
    # Read from a fresh store, preserving historical attribution independently
    # of the current target's usability and configuration.
    reopened, _, tool, _ = open_tools(tmp_path)
    before = reopened.path.read_bytes()
    outcome = tool.reconcile("operation-one", context={**CONTEXT, "operation": receipt(args)}, cancelled=lambda: False)
    assert outcome["outcome"] == "executed" and outcome["receipt"]["result"] == result
    verified = tool.verify(receipt(args, result), context=CONTEXT, cancelled=lambda: False)
    assert verified["status"] == "failed"
    assert verified["evidence"]["operation_attribution"] == "atomic_sharing_receipt"
    assert verified["evidence"]["current_state_matches"] is False
    assert reopened.path.read_bytes() == before


@pytest.mark.parametrize("change", ["source_label", "sibling_policy", "sibling_sharing", "sibling_removed"])
def test_unrelated_source_edit_does_not_invalidate_committed_model_binding(tmp_path, change):
    sources, _, tool, _, args = seed(tmp_path)
    result = execute(tool, args)
    source = next(iter(sources.read().values()))
    assert result["model_binding"] == model_binding(source, "model-b")
    if change == "source_label":
        source["name"] = "Owner renamed source"
    elif change == "sibling_policy":
        source["model_settings"]["model-a"]["overrides"]["max_concurrent"] = 2
    elif change == "sibling_sharing":
        source["shared_models"] = ["model-b"]
    else:
        source["models"] = ["model-b"]
        source["shared_models"] = ["model-b"]
    sources.save(source)
    assert source["source_revision"] != result["source_revision"]
    assert model_binding(source, "model-b") == result["model_binding"]
    _, _, reopened, _ = open_tools(tmp_path)
    verified = reopened.verify(receipt(args, result), context=CONTEXT, cancelled=lambda: False)
    assert verified["status"] == "passed" and verified["evidence"]["current_state_matches"]


@pytest.mark.parametrize("legacy_binding", [None, {}, {"version": "old"}])
def test_legacy_or_invalid_binding_is_unknown_without_inference_or_receipt_upgrade(tmp_path, legacy_binding):
    sources, _, tool, _, args = seed(tmp_path)
    result = execute(tool, args)
    legacy_result = copy.deepcopy(result)
    if legacy_binding is None:
        legacy_result.pop("model_binding", None)
    else:
        legacy_result["model_binding"] = legacy_binding
    records = sources.read()
    next(iter(records.values()))["_sharing_operations"]["operation-one"]["result"] = legacy_result
    sources._write(records)
    _, _, reopened, _ = open_tools(tmp_path)
    before = sources.path.read_bytes()
    outcome = reopened.reconcile("operation-one", context={**CONTEXT, "operation": receipt(args)}, cancelled=lambda: False)
    assert outcome["outcome"] == "executed" and outcome["receipt"]["result"] == legacy_result
    verified = reopened.verify(receipt(args, legacy_result), context=CONTEXT, cancelled=lambda: False)
    assert verified["status"] == "unknown"
    assert verified["evidence"]["operation_attribution"] == "atomic_sharing_receipt"
    assert not verified["evidence"]["current_state_matches"]
    assert sources.path.read_bytes() == before
