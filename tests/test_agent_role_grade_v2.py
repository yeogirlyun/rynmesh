"""Fabricated envelopes exercise grading only; no evidence here is a real run."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "tests/fixtures/agent_acceptance"
SPEC = importlib.util.spec_from_file_location("draft_role_grader_tests", HERE / "role_grade_v2.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.fixture(scope="module")
def grader():
    return module.DraftRoleGrader()


def put(document, pointer, value):
    parts = pointer.lstrip("/").split("/")
    for key in parts[:-1]:
        document = document.setdefault(key, {})
    document[parts[-1]] = value


def evidence(envelope, root, key, kind, document, sources=()):
    path = root / (key + ".json")
    path.write_text(json.dumps(document), encoding="utf-8")
    envelope["run"]["evidence"].append({
        "id": key, "kind": kind, "path": path.name, "sha256": module.file_digest(path),
        "producer": "reviewer" if kind == "independent_review" else "collector", "source_ids": list(sources),
    })


def refresh(envelope, root, key, change):
    row = next(e for e in envelope["run"]["evidence"] if e["id"] == key)
    path = root / row["path"]
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    row["sha256"] = module.file_digest(path)


def fixture_run(grader, root, case_id="Q06", cohort="ryn", trial=1, nonce=None):
    root.mkdir(parents=True, exist_ok=True)
    envelope = grader.template(case_id, cohort, trial)
    if nonce is not None:
        envelope["run_nonce"] = nonce
    case = grader.cases[case_id]["effective_case"]
    role = grader.cases[case_id]["role_contract"]
    run = envelope["run"]
    run.update(code_revision="fabricated-unit-test-only", prompt_version="fabricated-unit-test-only",
               started_at="2026-10-10T08:00:00Z", ended_at="2026-10-10T08:00:10Z")
    run["model"] = {"id": "unit-test-envelope", "provider": "not-a-real-call", "kind": "real", "adapter_version": "test"}
    run["bindings"].update(primary_model="test-planner", isolated_run_root=str(root))
    nodes = {key: "test-public-identity-" + key[-1] for key in grader.composer.LOGICAL_NODES}
    envelope.update(node_bindings=nodes, task_id="test-task-" + envelope["run_id"])
    current_node = nodes[role["agent_execution_node"]]
    evidence(envelope, root, "raw", "raw_trace", {"fabricated_test_evidence": True})
    evidence(envelope, root, "model", "model_exchange", {"fabricated_test_evidence": True})
    evidence(envelope, root, "probe", "dependency_probe", {"fabricated_test_evidence": True, "physical_host_ids": ["test-host-a", "test-host-b"]})
    binding = grader.proof_binding(envelope)
    observations = {
        "registration": {"registered_at": "2026-10-10T08:00:01Z", "sampling_kind": "new_v2_draft_sampling", "first_dispatch_not_started": True},
        "runtime": {
            "runtime_role": "ryn_agent_executor" if cohort == "ryn" else "reference_accounting_and_tool_permission_host",
            "runtime_consumer_node_id": current_node, "runtime_owner_id": "owner-test",
            "task_context_consumer_node_id": current_node, "task_context_owner_id": "owner-test",
            "tools_store_peer_id": current_node, "agent_store_path": "isolated-test/agent/sessions.json",
            "agent_store_encryption_public_key": "test-public-encryption-key",
            "task_context_binding_basis": "Runtime._execution_context; identity is runtime-derived, not persisted in row",
            "authentication_source_ids": ["auth"], "dispatch_audit_source_ids": ["dispatch"],
            "first_dispatch_at": "2026-10-10T08:00:03Z",
        },
        "storage": {
            "agent_store_path": "isolated-test/agent/sessions.json",
            "agent_store_encryption_public_key": "test-public-encryption-key",
            "task_readback": {"id": envelope["task_id"], "status": "queued" if cohort == "reference" else "completed"},
            "decrypted_from_store": True,
        },
        "deployment": {
            "source_owners": {key: ({"state": "instantiated", "actual_owner_peer_id": nodes[node], "source_id": "test-" + key}
                                      if key in module.SOURCE_REQUIREMENTS.get(case_id, ()) else {"state": "not_instantiated"})
                              for key, node in {**role["source_owners"], **role["new_source_owners"]}.items()},
            "connections": {key: ({"state": "instantiated", "actual_node_id": nodes[node], "instance_id": "test-" + key}
                                    if any(g["connection_id"] == key for g in case["authorization"]["resource_grants"])
                                    else {"state": "not_instantiated"}) for key, node in role["connection_hosts"].items()},
            "histories": {"task-failed-read": ({"state": "instantiated", "actual_node_id": nodes["node-b"],
                                                "stored_task_id": "real-local-history-id", "readback_sha256": "1" * 64}
                                               if case_id == "Q04" else {"state": "not_instantiated"})},
            "connection_accesses": [],
        },
    }
    observations["storage"]["task_readback_sha256"] = grader.composer.digest(observations["storage"]["task_readback"])
    evidence(envelope, root, "auth", "raw_trace", {"authenticated_runtime": {
        "run_nonce": envelope["run_nonce"], "peer_id": current_node, "owner_id": "owner-test",
        "method": "in_process_owner_key_and_runtime", "identity_key_sha256": "2" * 64,
    }})
    if cohort == "reference":
        path = root / "native.sqlite"
        path.write_bytes(b"fabricated-native-session-file")
        run["evidence"].append({"id": "native-file", "kind": "artifact_readback", "path": path.name,
                                "sha256": module.file_digest(path), "producer": "collector", "source_ids": ["native"]})
        evidence(envelope, root, "native", "raw_receipt", {"reference_session_readback": {
            "artifact_evidence_id": "native-file", "sha256": module.file_digest(path), "session_id": "test-native-session",
            "observed_request_ids": ["native-request-1"], "proof_scope": "native_session_and_request_readback",
            "run_nonce": envelope["run_nonce"], "task_id": envelope["task_id"],
        }})
        observations["runtime"]["reference_execution_evidence_id"] = "native"
    if any(d["id"] == "two_physical_nodes" for d in case["dependencies"]):
        observations["physical_hosts"] = {"node_host_bindings": {nodes["node-a"]: "test-host-a", nodes["node-b"]: "test-host-b"},
                                          "physical_host_ids": ["test-host-a", "test-host-b"], "host_probe_source_ids": ["host-a", "host-b"]}
        for suffix in ("a", "b"):
            evidence(envelope, root, "host-" + suffix, "dependency_probe", {"physical_host_probe": {
                "peer_id": nodes["node-" + suffix], "physical_host_id": "test-host-" + suffix,
                "run_nonce": envelope["run_nonce"], "identity_source": "os_machine_identity",
            }})
    for name, observation in observations.items():
        evidence(envelope, root, "role-" + name, module.ROLE_KINDS[name],
                 {"role_proof": {"kind": name, "binding": binding, "observations": observation}})
        envelope["role_evidence"][name] = "role-" + name
    registration_hash = next(e["sha256"] for e in run["evidence"] if e["id"] == "role-registration")
    dispatch = {"sampling_binding": binding, "task_id": envelope["task_id"], "registration_sha256": registration_hash,
                "dispatched_at": "2026-10-10T08:00:03Z", "id": "native-request-1", "kind": "model"}
    refresh(envelope, root, "model", lambda doc: doc.update(model_requests=[dispatch]))
    evidence(envelope, root, "dispatch", "raw_trace", {"dispatch_audit": {
        "coverage": "all_model_and_tool_dispatches", "events": [dispatch],
    }})
    evidence(envelope, root, "role-task_binding", "raw_trace", {"role_proof": {
        "kind": "task_binding", "binding": binding, "observations": {
            "task_id": envelope["task_id"], "registration_sha256": registration_hash, "bound_at": "2026-10-10T08:00:02Z",
        },
    }})
    envelope["role_evidence"]["task_binding"] = "role-task_binding"
    documents = {kind: {} for kind in ("task_state", "business_state", "operation_trace", "independent_review")}
    for assertion in case["expected"]["assertions"]:
        value = assertion["expected"][0] if assertion["operator"] == "one_of" else assertion["expected"]
        put(documents[assertion["evidence_kind"]], assertion["pointer"], value)
    documents["operation_trace"].setdefault("security", {}).update({key: [] for key in grader.common.SECURITY_FIELDS})
    checks = []
    for name, ref in envelope["role_evidence"].items():
        if ref is None:
            continue
        sources = [ref]
        if name == "runtime":
            sources += ["auth", "dispatch", "model"] + (["native"] if cohort == "reference" else [])
        if name == "physical_hosts":
            sources += ["host-a", "host-b"]
        checks.append({"id": name, "status": "passed", "source_ids": sources})
    documents["independent_review"]["role_review"] = {
        "binding": binding, "reviewer_id": "independent-fixture-reviewer", "independent_of_task_model": True,
        "independent_of_collector": True, "checks": checks,
    }
    for kind, document in documents.items():
        evidence(envelope, root, kind, kind, document, ["raw"])
    envelope["role_evidence"]["review"] = "independent_review"
    for dep in run["dependencies"]:
        dep.update(status="verified", evidence_ids=["model" if dep["id"] == "real_primary_model" else "probe"])
    run["review"].update(reviewer_id="independent-fixture-reviewer", independent_of_task_model=True, source_evidence_ids=["raw"])
    for check in run["review"]["checks"]:
        check["status"] = "passed"
    for index, artifact in enumerate(case["expected"]["artifacts"]):
        path = root / f"artifact-{index}.txt"
        path.write_text("\n".join(artifact["must_contain"] + artifact["must_cite"]), encoding="utf-8")
        key = f"artifact-{index}"
        run["evidence"].append({"id": key, "kind": "artifact_readback", "path": path.name,
                               "sha256": module.file_digest(path), "producer": "collector", "source_ids": ["role-storage"]})
        run["artifacts"].append({"connection_id": artifact["connection_id"], "path": artifact["path"], "readback_evidence_id": key})
    return envelope


def write_run(root, envelope):
    path = root / "run.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def test_unmodified_common_code_and_non_filesystem_suite_token(grader, tmp_path):
    before_doc_root = grader.common.DOCS
    original = grader.common.template("Q06")
    result = grader.template("Q06")
    assert original["dataset_version"] == grader.composer.BASE_VERSION
    assert result["run"]["dataset_version"] == grader.composer.DATASET_VERSION
    assert grader._common_grade.__code__ is grader.common.grade.__code__
    assert grader._common_aggregate.__code__ is grader.common.aggregate.__code__
    assert grader._common_grade.__globals__ is not grader.common.grade.__globals__
    assert grader.common.DOCS == before_doc_root
    real_suite_path = grader.common.DOCS / "cases-v1.json"
    assert grader._digest(real_suite_path) == module.file_digest(real_suite_path) != grader.suite_hash
    assert grader._digest(grader.token) == grader.suite_hash
    with pytest.raises(TypeError):
        Path(grader.token)
    assert grader.common.grade(original, tmp_path)["status"] == "pending"


@pytest.mark.parametrize("case_id,cohort", [("Q01", "ryn"), ("Q06", "ryn"), ("Q06", "reference"), ("C03", "ryn"), ("C04", "ryn"), ("Q04", "ryn"), ("F02", "ryn"), ("P04", "ryn")])
def test_complete_fabricated_checks_stay_draft_never_formal_pass(grader, tmp_path, case_id, cohort):
    envelope = fixture_run(grader, tmp_path, case_id, cohort)
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "passed", result
    assert result["diagnostic_checks_satisfied"] is True
    assert result["status"] == "pending" and result["formal_pass"] is False
    assert result["formal_trial_registered"] is False


def test_template_and_metadata_consistency_are_not_identity_proof(grader, tmp_path):
    envelope = grader.template("Q06")
    envelope["node_bindings"] = {node: "test-peer-" + node[-1] for node in grader.composer.LOGICAL_NODES}
    result = grader.grade(envelope, tmp_path)
    assert result["status"] == result["evidence_status"] == "pending"
    assert result["diagnostic_checks_satisfied"] is False
    assert any("role proof missing: runtime" == reason for reason in result["pending"])


@pytest.mark.parametrize("role", ["registration", "task_binding", "runtime", "storage", "deployment", "review"])
def test_missing_role_proof_stays_pending(grader, tmp_path, role):
    envelope = fixture_run(grader, tmp_path)
    envelope["role_evidence"][role] = None
    result = grader.grade(envelope, tmp_path)
    assert result["status"] == "pending", result
    assert result["evidence_status"] == "pending"


@pytest.mark.parametrize("field", ["effective_suite_sha256", "effective_case_sha256", "effective_world_sha256", "overlay_sha256", "base_world_sha256", "loader_sha256"])
def test_contract_hash_tampering_fails(grader, tmp_path, field):
    envelope = grader.template("Q06")
    envelope["contract"][field] = "0" * 64
    assert grader.grade(envelope, tmp_path)["status"] == "failed"


@pytest.mark.parametrize("part", ["top", "body", "partition"])
def test_version_or_partition_cannot_be_rewritten_to_v1(grader, tmp_path, part):
    envelope = grader.template("Q06")
    if part == "top":
        envelope["dataset_version"] = grader.composer.BASE_VERSION
    elif part == "body":
        envelope["run"]["dataset_version"] = grader.composer.BASE_VERSION
    else:
        envelope["run"]["partition"] = "held_out"
    assert grader.grade(envelope, tmp_path)["status"] == "failed"


@pytest.mark.parametrize("proof,change", [
    ("runtime", lambda o: o.update(task_context_consumer_node_id="wrong-node")),
    ("runtime", lambda o: o.update(runtime_owner_id="wrong-owner")),
    ("runtime", lambda o: o.update(tools_store_peer_id="wrong-node")),
    ("storage", lambda o: o.update(agent_store_encryption_public_key="wrong-key")),
    ("storage", lambda o: o.update(task_readback_sha256="0" * 64)),
    ("storage", lambda o: o["task_readback"].update(owner_id="invented-persisted-identity")),
    ("task_binding", lambda o: o.update(task_id="other-task")),
    ("task_binding", lambda o: o.update(bound_at="2026-10-10T08:00:04Z")),
])
def test_wrong_actual_observations_fail(grader, tmp_path, proof, change):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "role-" + proof, lambda d: change(d["role_proof"]["observations"]))
    assert grader.grade(envelope, tmp_path)["status"] == "failed"


def test_late_registration_cannot_be_backfilled_from_old_run(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "role-registration",
            lambda d: d["role_proof"]["observations"].update(registered_at="2026-10-10T08:00:04Z"))
    new_hash = next(e["sha256"] for e in envelope["run"]["evidence"] if e["id"] == "role-registration")
    refresh(envelope, tmp_path, "role-task_binding",
            lambda d: d["role_proof"]["observations"].update(registration_sha256=new_hash))
    result = grader.grade(envelope, tmp_path)
    assert "registration/task binding was not before first dispatch" in result["failures"]


def test_authenticated_flag_without_raw_source_is_pending(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "auth", lambda d: d.update(authenticated_runtime={"authenticated": True}))
    result = grader.grade(envelope, tmp_path)
    assert result["diagnostic_checks_satisfied"] is False
    assert result["status"] == "pending"


def test_declared_not_instantiated_connections_are_valid_only_without_grants(grader, tmp_path):
    no_grants = fixture_run(grader, tmp_path / "no-grants", "Q01")
    assert grader.grade(no_grants, tmp_path / "no-grants")["evidence_status"] == "passed"
    granted = fixture_run(grader, tmp_path / "granted", "F02")
    refresh(granted, tmp_path / "granted", "role-deployment",
            lambda d: d["role_proof"]["observations"]["connections"].update({"docs-alpha": {"state": "not_instantiated"}}))
    assert grader.grade(granted, tmp_path / "granted")["evidence_status"] == "pending"


def test_reference_queued_accounting_task_does_not_prove_native_execution(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path, cohort="reference")
    refresh(envelope, tmp_path, "native", lambda d: d["reference_session_readback"].update(
        proof_scope="file_exists_and_hash_only", observed_request_ids=[]))
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "pending"
    assert "reference session file existence is not execution proof" in result["pending"]


def test_physical_hosts_require_raw_bound_host_probes(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path, "P04")
    (tmp_path / "host-b.json").unlink()
    result = grader.grade(envelope, tmp_path)
    assert result["diagnostic_checks_satisfied"] is False
    assert result["status"] == "pending"
    assert result["formal_pass"] is False


def test_false_business_result_and_security_audit_remain_failures(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "operation_trace", lambda d: d["metrics"].update(writes=1))
    result = grader.grade(envelope, tmp_path)
    assert "metrics_writes: assertion false" in result["failures"]
    refresh(envelope, tmp_path, "operation_trace", lambda d: d["security"].update(unauthorized_reads=["bad"]))
    assert any("security violation: unauthorized_reads" in error for error in grader.grade(envelope, tmp_path)["failures"])


def test_same_real_suite_as_evidence_uses_real_file_digest(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    source = grader.common.DOCS / "cases-v1.json"
    (tmp_path / "copied-suite.json").write_bytes(source.read_bytes())
    envelope["run"]["evidence"].append({"id": "suite-as-evidence", "kind": "raw_trace", "path": "copied-suite.json",
        "sha256": module.file_digest(source), "producer": "collector", "source_ids": []})
    assert grader.grade(envelope, tmp_path)["evidence_status"] == "passed"


def test_common_engine_pin_drift_rejected(grader, monkeypatch):
    real_digest = module.file_digest
    monkeypatch.setattr(module, "file_digest", lambda path: "0" * 64 if Path(path) == HERE / "validate.py" else real_digest(path))
    with pytest.raises(module.RoleGradeError):
        module.DraftRoleGrader()


def test_aggregate_regrades_paths_keeps_denominators_and_rejects_mixes(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path / "valid")
    path = write_run(tmp_path / "valid", envelope)
    result = grader.aggregate([path])
    counts = result["diagnostic_not_formal_acceptance"]
    assert counts["business"] == {"total": 72, "passed": 1, "failed": 0, "pending": 71}
    assert counts["permission"]["total"] == 18 and counts["reference"]["total"] == 72
    assert result["status"] == "pending" and result["formal_pass"] is False
    assert grader.aggregate([path, path])["status"] == "failed"
    assert grader.aggregate([{"status": "passed"}])["status"] == "failed"
    bad = copy.deepcopy(envelope)
    bad["dataset_version"] = grader.composer.BASE_VERSION
    other = tmp_path / "old.json"
    other.write_text(json.dumps(bad), encoding="utf-8")
    assert "mixed v1/v2 or overlay inputs rejected" in grader.aggregate([path, other])["failures"]


def test_all_frozen_assertions_and_all_180_trials_use_common_engine(grader, tmp_path):
    paths = []
    for cohort in ("ryn", "reference"):
        for case_id in grader.composer.CASE_IDS:
            for trial in (1, 2, 3):
                root = tmp_path / f"{cohort}-{case_id}-{trial}"
                paths.append(write_run(root, fixture_run(grader, root, case_id, cohort, trial)))
    result = grader.aggregate(paths)
    assert result["diagnostic_gates_satisfied"] is True, result
    assert result["diagnostic_not_formal_acceptance"]["business"]["passed"] == 72
    assert result["diagnostic_not_formal_acceptance"]["permission"]["passed"] == 18
    assert result["diagnostic_not_formal_acceptance"]["reference"]["passed"] == 72
    assert result["status"] == "pending" and result["formal_trials_registered"] == 0
    # A normal business failure can remain within the unchanged 58/72 threshold.
    first = paths[0]
    envelope = json.loads(first.read_text(encoding="utf-8"))
    refresh(envelope, first.parent, "operation_trace", lambda d: d["metrics"].update(writes=1))
    write_run(first.parent, envelope)
    result = grader.aggregate(paths)
    assert result["diagnostic_gates_satisfied"] is True
    assert result["diagnostic_not_formal_acceptance"]["business"]["passed"] == 71
    assert result["status"] == "pending"
    # Any universal security breach remains blocking even if arithmetic passes.
    refresh(envelope, first.parent, "operation_trace", lambda d: d["security"].update(unauthorized_reads=["bad"]))
    write_run(first.parent, envelope)
    result = grader.aggregate(paths)
    assert result["diagnostic_gates_satisfied"] is False
    assert result["status"] == "failed"


def complete_count_rows(grader):
    return [{"partition": "development", "cohort": cohort, "case_id": case_id, "trial": trial, "status": "passed"}
            for cohort in ("ryn", "reference") for case_id in grader.composer.CASE_IDS for trial in (1, 2, 3)]


@pytest.mark.parametrize("case_id", ["Q04", "F02", "C01", "R02", "R03", "R04", "R05", "R06"])
def test_every_original_mandatory_reproducibility_gate_preserved(grader, case_id):
    rows = complete_count_rows(grader)
    next(r for r in rows if r["cohort"] == "ryn" and r["case_id"] == case_id)["status"] = "failed"
    result = grader._common_aggregate(rows)
    assert result["status"] == "not_passed"
    assert result["mandatory_reproducibility_gate"] is False


def test_absolute_relative_and_permission_gates_remain_unchanged(grader):
    rows = complete_count_rows(grader)
    optional = [r for r in rows if r["cohort"] == "ryn" and r["case_id"] in {"Q01", "Q02", "Q03", "Q05", "Q06"}]
    for row in optional[:14]:
        row["status"] = "failed"
    result = grader._common_aggregate(rows)
    assert result["business"]["passed"] == 58 and result["status"] == "passed"
    optional[14]["status"] = "failed"
    assert grader._common_aggregate(rows)["status"] == "not_passed"
    optional[14]["status"] = "passed"
    reference = next(r for r in rows if r["cohort"] == "reference")
    reference["status"] = "pending"
    assert grader._common_aggregate(rows)["reference_ratio_gate"] is False
    reference["status"] = "passed"
    next(r for r in rows if r["cohort"] == "ryn" and r["case_id"] == "P06")["status"] = "failed"
    assert grader._common_aggregate(rows)["status"] == "not_passed"


def test_nonce_reuse_across_distinct_cases_cannot_supply_new_samples(grader, tmp_path):
    first = fixture_run(grader, tmp_path / "first", "Q01")
    second = fixture_run(grader, tmp_path / "second", "Q06", nonce=first["run_nonce"])
    paths = [write_run(tmp_path / "first", first), write_run(tmp_path / "second", second)]
    result = grader.aggregate(paths)
    assert "run identity/nonce reused" in result["failures"]
    assert result["status"] == "failed"


def test_other_overlay_and_formal_registration_flags_are_rejected(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    envelope["formal_trial_registered"] = True
    assert grader.grade(envelope, tmp_path)["status"] == "failed"
    envelope["formal_trial_registered"] = False
    envelope["contract"]["overlay_sha256"] = "0" * 64
    path = write_run(tmp_path, envelope)
    assert "mixed v1/v2 or overlay inputs rejected" in grader.aggregate([path])["failures"]


def test_cli_template_remains_draft(grader, capsys):
    assert module.main(["template", "Q06"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "draft/not_formal_ready"
    assert output["formal_trial_registered"] is False
    assert output["run"]["dataset_version"] == grader.composer.DATASET_VERSION


@pytest.mark.parametrize("kind", ["contract", "evidence"])
def test_integrity_errors_cannot_hide_in_allowed_business_failures(grader, tmp_path, kind):
    envelope = fixture_run(grader, tmp_path, "Q01")
    if kind == "contract":
        envelope["contract"]["effective_case_sha256"] = "0" * 64
    else:
        (tmp_path / "raw.json").write_text('{"tampered":true}', encoding="utf-8")
    result = grader.aggregate([write_run(tmp_path, envelope)])
    assert result["status"] == "failed"
    assert "contract/evidence integrity failure cannot count as an ordinary business failure" in result["failures"]


def test_wrong_dependency_evidence_kind_is_global_integrity_failure(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path, "Q01")
    next(dep for dep in envelope["run"]["dependencies"] if dep["id"] == "real_primary_model")["evidence_ids"] = ["raw"]
    graded = grader.grade(envelope, tmp_path)
    assert graded["evidence_integrity_failed"] is True
    result = grader.aggregate([write_run(tmp_path, envelope)])
    assert "contract/evidence integrity failure cannot count as an ordinary business failure" in result["failures"]


def test_known_business_failure_does_not_complete_missing_sampling_evidence(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path, "Q01")
    refresh(envelope, tmp_path, "operation_trace", lambda doc: doc["metrics"].update(writes=1))
    refresh(envelope, tmp_path, "model", lambda doc: doc.pop("model_requests"))
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "failed" and result["evidence_complete"] is False
    counts = grader.aggregate([write_run(tmp_path, envelope)])["diagnostic_not_formal_acceptance"]["business"]
    assert counts == {"total": 72, "passed": 0, "failed": 0, "pending": 72}


def test_unknown_connection_identity_fields_do_not_become_flags(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path, "F02")
    refresh(envelope, tmp_path, "role-deployment", lambda d: d["role_proof"]["observations"]["connections"].update(
        {"docs-alpha": {"state": "instantiated", "instance_id": True}}))
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "pending"
    assert "actual connection identity readback incomplete" in result["pending"]


def test_old_model_exchange_cannot_gain_registration_from_final_header(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "model", lambda doc: doc.pop("model_requests"))
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "pending", result
    assert "original model exchange sampling provenance absent" in result["pending"]


@pytest.mark.parametrize("field,value", [("task_id", "old-task"), ("registration_sha256", "0" * 64),
                                         ("sampling_binding", {}), ("dispatched_at", "2026-10-10T07:59:59Z")])
def test_original_dispatch_must_belong_to_registered_task_interval(grader, tmp_path, field, value):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "model", lambda doc: doc["model_requests"][0].update({field: value}))
    assert grader.grade(envelope, tmp_path)["evidence_status"] == "failed"


def test_dispatch_audit_must_cover_original_model_requests(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "dispatch", lambda doc: doc["dispatch_audit"].update(events=[]))
    result = grader.grade(envelope, tmp_path)
    assert "dispatch audit and original model request coverage differ" in result["failures"]


def test_missing_raw_dispatch_audit_is_pending_not_assumed_denied(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    refresh(envelope, tmp_path, "role-runtime", lambda doc: doc["role_proof"]["observations"].update(dispatch_audit_source_ids=["missing"]))
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "pending", result


def test_tool_dispatch_cannot_borrow_another_sampling_registration(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    def change(doc):
        event = copy.deepcopy(doc["dispatch_audit"]["events"][0])
        event.update(kind="tool", id="tool-1", task_id="different-task")
        doc["dispatch_audit"]["events"].append(event)
    refresh(envelope, tmp_path, "dispatch", change)
    assert grader.grade(envelope, tmp_path)["evidence_status"] == "failed"


@pytest.mark.parametrize("request_ids,status", [(True, "pending"), ("native-request-1", "pending"),
                                               (["another-request"], "failed")])
def test_reference_native_receipt_requires_correlated_request_readback(grader, tmp_path, request_ids, status):
    envelope = fixture_run(grader, tmp_path, cohort="reference")
    refresh(envelope, tmp_path, "native", lambda doc: doc["reference_session_readback"].update(observed_request_ids=request_ids))
    assert grader.grade(envelope, tmp_path)["evidence_status"] == status


def test_independent_runtime_review_must_inspect_dispatch_sources(grader, tmp_path):
    envelope = fixture_run(grader, tmp_path)
    def change(doc):
        next(check for check in doc["role_review"]["checks"] if check["id"] == "runtime")["source_ids"] = ["role-runtime", "auth"]
    refresh(envelope, tmp_path, "independent_review", change)
    result = grader.grade(envelope, tmp_path)
    assert result["evidence_status"] == "pending"
    assert "role review lacks inspected raw sources: runtime" in result["pending"]
