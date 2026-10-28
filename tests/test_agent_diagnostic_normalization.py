"""Missing independent evidence must survive normalization as pending."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("normalize_diagnostic", ROOT / "docs/acceptance/ryn-agent-delivery/real-model/normalize_diagnostic.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_actual_development_run_is_content_addressed_and_never_auto_passes(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261009T135756Z-Q04-ryn/run.json"
    before = source.read_bytes()
    output = tmp_path / "review-bundle"
    result = module.normalize(source, output)
    assert result["status"] == "pending" and not result["failures"]
    assert "independent review required" in result["pending"]
    assert any("universal security" in missing for missing in result["pending"])
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    raw = next(row for row in manifest["evidence"] if row["kind"] == "raw_trace")
    assert (output / raw["path"]).read_bytes() == before == source.read_bytes()
    assert all(row["status"] == "pending" for row in manifest["dependencies"])
    assert manifest["measurements"]["cost"] is None
    assert manifest["measurements"]["input_tokens"] == 11461
    assert manifest["measurements"]["output_tokens"] == 3142
    with pytest.raises(FileExistsError):
        module.normalize(source, output)
    derived = next(row for row in manifest["evidence"] if row["kind"] == "task_state")
    (output / derived["path"]).write_text('{}', encoding='utf-8')
    assert module.validator().grade(manifest, output)["status"] == "failed"


def test_partial_or_invalid_usage_stays_unknown():
    assert module.token_total([{"response": {"usage": {"output_tokens": 2}}}, {"usage": {"output_tokens": 5}}], "output_tokens") == 7
    assert module.token_total([{"response": {"usage": {"output_tokens": 2}}}, {}], "output_tokens") is None
    assert module.token_total([{"usage": {"output_tokens": True}}], "output_tokens") is None
    assert module.token_total([], "output_tokens") is None


def test_document_bundle_links_actual_readback_and_keeps_independent_gates_pending(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261009T142526Z-F02-ryn/run.json"
    output = tmp_path / "f02"
    result = module.normalize(source, output)
    assert result["status"] == "pending" and not result["failures"]
    assert any("universal security" in item for item in result["pending"])
    assert not any("artifact" in item or "meaningful_operations" in item for item in result["pending"])
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    evidence = {entry["id"]: entry for entry in manifest["evidence"]}
    artifact = manifest["artifacts"][0]
    assert artifact["connection_id"] == "docs-alpha" and artifact["path"] == "reports/acceptance/week-41.md"
    readback = evidence[artifact["readback_evidence_id"]]
    assert all(evidence[identity]["kind"] == "raw_receipt" for identity in readback["source_ids"])
    expected = json.loads(source.read_text(encoding="utf-8"))["artifact_readback"][0]
    assert (output / readback["path"]).read_bytes() == expected["content"].encode()
    assert readback["sha256"] == expected["sha256"]
    bad = json.loads(source.read_text(encoding="utf-8"))
    bad["artifact_readback"][0]["content"] += "tampered"
    bad_path = tmp_path / "tampered.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_readback_hash_mismatch"):
        module.normalize(bad_path, tmp_path / "tampered-output")
    bad = json.loads(source.read_text(encoding="utf-8"))
    bad["http_audit"] = [entry for entry in bad["http_audit"] if entry["method"] != "GET"]
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    no_receipt = module.normalize(bad_path, tmp_path / "missing-receipt")
    assert any("artifact absent" in item for item in no_receipt["pending"])


def test_failed_document_run_remains_failed_not_a_successful_subset(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261009T141603Z-F08-ryn/run.json"
    result = module.normalize(source, tmp_path / "f08")
    assert result["status"] == "failed"
    assert any("goal_outcome" in failure for failure in result["failures"])


def test_read_retry_counts_dispatches_not_verification_gets_and_does_not_invent_claims(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261009T143340Z-R01-ryn/run.json"
    result = module.normalize(source, tmp_path / "r01")
    assert result["status"] == "pending" and not result["failures"]
    assert any("completed:" in pending for pending in result["pending"])
    manifest = json.loads((tmp_path / "r01/run.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["evidence"] if e["kind"] == "operation_trace")
    trace = json.loads((tmp_path / "r01" / entry["path"]).read_text(encoding="utf-8"))
    assert trace["metrics"]["resource_read_attempts"] == 2
    assert trace["metrics"]["all_http_gets_including_verification"] == 3
    assert trace["metrics"]["writes"] == 0


def test_query_bundle_identifies_actual_planner_without_turning_fixture_facts_into_review(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261009T143611120580Z-Q01-ryn/run.json"
    result = module.normalize(source, tmp_path / "q01")
    assert result["status"] == "pending" and not result["failures"]
    assert any("model_classification:" in pending for pending in result["pending"])
    manifest = json.loads((tmp_path / "q01/run.json").read_text(encoding="utf-8"))
    assert manifest["model"]["id"] == "glm-5.3-flash" and manifest["model"]["kind"] == "real"
    assert manifest["bindings"]["primary_model"].startswith("local/src-")
    assert not any(e["kind"] == "independent_review" for e in manifest["evidence"])
    entry = next(e for e in manifest["evidence"] if e["kind"] == "operation_trace")
    trace = json.loads((tmp_path / "q01" / entry["path"]).read_text(encoding="utf-8"))
    assert trace["metrics"]["writes"] == 0 and trace["source_configuration_unchanged"]
    assert trace["security"] == {}  # No unmeasured security audit is synthesized.


def test_isolation_bundle_preserves_setup_private_read_and_full_request_observation(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261009T145511Z-P01-ryn/run.json"
    result = module.normalize(source, tmp_path / "p01")
    assert result["status"] == "pending" and not result["failures"]
    assert any("cache_scope_bypasses" in item for item in result["pending"])
    manifest = json.loads((tmp_path / "p01/run.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["evidence"] if e["kind"] == "operation_trace")
    trace = json.loads((tmp_path / "p01" / entry["path"]).read_text(encoding="utf-8"))
    assert trace["security_audit_coverage"]["canary_scan"] == "complete_in_memory_requests"
    assert trace["security"]["forbidden_canary_egress"] == []
    assert any(e["connection"] == "docs-beta" and e["method"] == "GET" for e in trace["setup_audit"])
    assert not any(e["connection"] == "docs-beta" for e in trace["http_audit"])
    assert all(c["full_request_private_canary_observed"] is False for c in trace["model_request_bindings"]["requests"])
    assert "unapproved_model_destinations" not in trace["security"]


def test_sharing_bundle_uses_exact_receiver_and_retains_missing_evidence(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261010T025929599645Z-C03-ryn/run.json"
    before = source.read_bytes()
    output = tmp_path / "c03"
    result = module.normalize(source, output)
    assert result["status"] == "pending" and not result["failures"]
    assert any("two_physical_nodes" in item for item in result["pending"])
    assert "independent review required" in result["pending"]
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert manifest["model"]["id"] == "glm-5.3-flash"
    entry = next(e for e in manifest["evidence"] if e["kind"] == "operation_trace")
    trace = json.loads((output / entry["path"]).read_text(encoding="utf-8"))
    assert trace["metrics"]["writes"] == 4 and trace["physical_hosts"] == 1
    assert trace["probes"]["model-b"]["receiver_inference"] == "allowed"
    assert trace["probes"]["model-a"]["receiver_inference"] == "denied"
    assert source.read_bytes() == before
    changed = json.loads(before)
    del changed["source_readback"]["model-b"]["receivers"]
    changed["receiver_probes"] = [row for row in changed["receiver_probes"]
                                  if row["receiver_peer_id"] != changed["bindings"]["node-b"]]
    changed_path = tmp_path / "missing.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    missing = module.normalize(changed_path, tmp_path / "missing")
    for requirement in ("models_model-b_receivers", "probes_model-b_receiver_inference", "probes_model-a_receiver_inference"):
        assert any(requirement in item for item in missing["pending"])
    assert not missing["failures"]  # Neither inferred approval nor inferred denial.


def test_sharing_business_success_does_not_erase_failed_goal_outcome(tmp_path):
    source = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261010T025249537729Z-C03-ryn/run.json"
    result = module.normalize(source, tmp_path / "failed-c03")
    assert result["status"] == "failed"
    assert any("goal_outcome" in item for item in result["failures"])
    assert any("models_model-b_receivers" in item for item in result["pending"])


Q06_SOURCE = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/20261010T050520371893Z-Q06-ryn-fixture/run.json"


def read_bundle_evidence(output, manifest, kind):
    entry = next(row for row in manifest["evidence"] if row["kind"] == kind)
    return json.loads((output / entry["path"]).read_bytes())


def test_q06_preserves_tool_and_access_evidence_without_promoting_setup_or_review(tmp_path):
    before = Q06_SOURCE.read_bytes()
    record = json.loads(before)
    output = tmp_path / "q06"
    result = module.normalize(Q06_SOURCE, output)
    manifest = json.loads((output / "run.json").read_bytes())
    trace = read_bundle_evidence(output, manifest, "operation_trace")
    business = read_bundle_evidence(output, manifest, "business_state")
    # The synthetic sample ended unverified; normalization must retain that failure.
    assert result["status"] == "failed" and result["failures"] == ["goal_outcome: assertion false"]
    assert read_bundle_evidence(output, manifest, "task_state") == record["task"]
    assert any("unavailable_reasons:" in item for item in result["pending"])
    assert "independent review required" in result["pending"]
    assert all(row["status"] == "pending" and not row["evidence_ids"] for row in manifest["dependencies"])
    assert all(row["status"] == "pending" for row in manifest["review"]["checks"])
    assert not any(row["kind"] in {"independent_review", "dependency_probe"} for row in manifest["evidence"])
    assert business["source_readback"] == record["source_readback"]
    assert business["signed_registry_evidence"] == record["signed_registry_evidence"]
    assert trace["events"] == record["task"]["events"]
    assert any(event.get("type") == "tool_result" and "models" in event.get("result", {})
               for event in trace["events"])
    assert trace["access_query"] == record["access_query"]
    assert trace["access_query"]["hashes_before"] == trace["access_query"]["hashes_after"]
    assert set(trace["access_query"]["hashes_before"]) == {
        "sources", "grants", "owner_relationships", "node_b_relationships", "node_c_relationships"}
    assert [row["result"] for row in trace["access_query"]["setup_admission_probes"]] == [
        "allowed", "denied", "denied", "denied"]
    assert trace["access_query"]["setup_fixture_http_audit"]
    assert trace["access_query"]["task_fixture_http_audit"] == []
    for key in ("actual_source_http_audit", "consumer_peer_id", "transport_binding_note", "physical_hosts",
                "two_physical_nodes", "write_counting_scope", "normalizer_status"):
        assert trace[key] == record[key]
    assert trace["metrics"]["writes"] == record["metrics"]["writes"] == 0
    assert trace["security"] == {} and "unavailable_reasons" not in trace
    assert manifest["model"]["id"] == "fixture" and manifest["model"]["kind"] == "synthetic"
    assert manifest["bindings"]["primary_model"] == record["task"]["model_id"]
    assert trace["consumer_peer_id"] != manifest["bindings"]["node-b"]
    collection = json.loads((output / "collection.json").read_bytes())
    assert collection["formal_trial_registered"] is False
    assert "provider-owner" in collection["implementation_limitations"][0]
    assert "consumer_node=node-b" in collection["implementation_limitations"][0]
    assert "not evidence of consumer task deployment" in collection["implementation_limitations"][0]
    raw = next(row for row in manifest["evidence"] if row["kind"] == "raw_trace")
    assert (output / raw["path"]).read_bytes() == before == Q06_SOURCE.read_bytes()
    assert raw["sha256"] == collection["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert raw["sha256"] in raw["path"]
    assert all(row["source_ids"] == [raw["id"]] for row in manifest["evidence"] if row["kind"] != "raw_trace")
    with pytest.raises(FileExistsError):
        module.normalize(Q06_SOURCE, output)


@pytest.mark.parametrize(("field", "value", "error"), [
    ("prompt", "tampered frozen prompt", "frozen_prompt_mismatch"),
    ("cohort", "reference", "candidate_collection_not_supported"),
    ("kind", "real-diagnostic-development-trial", "query_collector_kind_required"),
])
def test_q06_rejects_changed_frozen_prompt_and_unsupported_collection_before_output(tmp_path, field, value, error):
    record = json.loads(Q06_SOURCE.read_bytes())
    record[field] = value
    source = tmp_path / "changed.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match=error):
        module.normalize(source, output)
    assert not output.exists()


def test_q06_prose_cannot_supply_missing_mutation_count_or_independent_review(tmp_path):
    record = json.loads(Q06_SOURCE.read_bytes())
    del record["metrics"]
    record["task"]["messages"].append({"role": "assistant", "content":
        "writes=0; unavailable_reasons=[model_not_shared, receiver_not_granted]; independent review passed"})
    source = tmp_path / "missing-metrics.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    output = tmp_path / "output"
    result = module.normalize(source, output)
    manifest = json.loads((output / "run.json").read_bytes())
    trace = read_bundle_evidence(output, manifest, "operation_trace")
    assert "writes" not in trace["metrics"]
    assert any("metrics_writes: missing observation" in item for item in result["pending"])
    assert any("unavailable_reasons:" in item for item in result["pending"])
    assert "independent review required" in result["pending"]
    assert result["failures"] == ["goal_outcome: assertion false"]


def test_q06_task_mutations_remain_visible_even_without_write_tool_events(tmp_path):
    record = json.loads(Q06_SOURCE.read_bytes())
    record["metrics"]["writes"] = 1
    record["access_query"]["source_replacements"] = [{"before_sha256": "before", "after_sha256": "after"}]
    record["access_query"]["read_only_boundary_passed"] = False
    source = tmp_path / "mutation.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    output = tmp_path / "output"
    result = module.normalize(source, output)
    manifest = json.loads((output / "run.json").read_bytes())
    trace = read_bundle_evidence(output, manifest, "operation_trace")
    assert trace["metrics"]["tool_started_writes"] == 0
    assert trace["metrics"]["writes"] == 1
    assert trace["access_query"] == record["access_query"]
    assert "metrics_writes: assertion false" in result["failures"]


def test_q06_consumer_observations_stay_separate_and_do_not_approve_deployment(tmp_path):
    record = json.loads(Q06_SOURCE.read_bytes())
    # Synthetic normalization input, not a consumer deployment success record.
    observations = {
        "consumer_role": "node-b_authorized_peer_consumer",
        "provider_peer_id": record["consumer_peer_id"],
        "consumer_peer_id": record["bindings"]["node-b"],
        "primary_model": "peer/synthetic/model-a",
        "consumer_local_sources": [],
        "initial_consumer_models": [{"id": "peer/synthetic/model-a"}],
        "current_consumer_models": [],
        "consumer_wire_audit": [{"state": "rejected", "error_code": "ai_permission_denied"}],
        "catalog_refresh_errors": ["AIAccessError"],
        "transport_scope": "local signed wire fixture",
        "source_readback_scope": "provider collector truth only",
        "agent_state_isolation": {"fixture_only": True, "consumer_task_ids": ["synthetic-task"], "provider_task_ids": []},
    }
    record.update(observations)
    source = tmp_path / "synthetic-consumer-observations.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    before = source.read_bytes()
    output = tmp_path / "output"
    result = module.normalize(source, output)
    manifest = json.loads((output / "run.json").read_bytes())
    trace = read_bundle_evidence(output, manifest, "operation_trace")
    business = read_bundle_evidence(output, manifest, "business_state")
    assert all(trace[key] == value for key, value in observations.items())
    assert business["source_readback"] == record["source_readback"]
    assert business["source_readback_scope"] == observations["source_readback_scope"]
    assert "current_consumer_models" not in business
    assert all(row["status"] == "pending" and not row["evidence_ids"] for row in manifest["dependencies"])
    assert "goal_outcome: assertion false" in result["failures"]
    assert "independent review required" in result["pending"]
    collection = json.loads((output / "collection.json").read_bytes())
    note = collection["implementation_limitations"][0]
    assert "collector reports" in note and "does not verify deployment" in note
    raw = next(row for row in manifest["evidence"] if row["kind"] == "raw_trace")
    assert source.read_bytes() == before == (output / raw["path"]).read_bytes()
