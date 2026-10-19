"""Self-tests for evidence machinery only. None is a real Agent evaluation run."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("validate.py")
SPEC = importlib.util.spec_from_file_location("agent_acceptance_validator", MODULE_PATH)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def put(document, pointer, value):
    parts = pointer.lstrip("/").split("/")
    for part in parts[:-1]:
        document = document.setdefault(part, {})
    document[parts[-1]] = value


def evidence(run, root, key, kind, document, sources=()):
    path = root / f"{key}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    run["evidence"].append({"id": key, "kind": kind, "path": path.name, "sha256": validator.digest(path),
                            "producer": "reviewer" if kind == "independent_review" else "collector", "source_ids": list(sources)})


def valid_envelope(root, case_id="Q02"):
    """Fabricated test envelope exercising schema, not external authenticity."""
    run = validator.template(case_id)
    run.update(code_revision="self-test-only", prompt_version="self-test-only", started_at="2026-10-09T08:00:00Z", ended_at="2026-10-09T08:00:01Z")
    run["model"] = {"id": "test-envelope", "provider": "test", "adapter_version": "1", "kind": "real"}
    case = next(c for c in validator.suite()["cases"] if c["id"] == case_id)
    documents = {kind: {} for kind in ("task_state", "business_state", "operation_trace", "independent_review")}
    for check in case["expected"]["assertions"]:
        expected = check["expected"][0] if check["operator"] == "one_of" else check["expected"]
        put(documents[check["evidence_kind"]], check["pointer"], expected)
    documents["operation_trace"].setdefault("security", {}).update({key: [] for key in validator.SECURITY_FIELDS})
    evidence(run, root, "raw", "raw_trace", {"test_only": True})
    evidence(run, root, "model", "model_exchange", {"test_only": True})
    evidence(run, root, "probe", "dependency_probe", {"test_only": True, "physical_host_ids": ["test-a", "test-b"]})
    for kind, document in documents.items():
        evidence(run, root, kind, kind, document, ["raw"])
    for dep in run["dependencies"]:
        dep.update(status="verified", evidence_ids=["model" if dep["id"] == "real_primary_model" else "probe"])
    run["review"].update(reviewer_id="independent-test-reviewer", independent_of_task_model=True, source_evidence_ids=["raw"])
    for row in run["review"]["checks"]:
        row["status"] = "passed"
    return run


def refresh(run, root, key, change):
    row = next(e for e in run["evidence"] if e["id"] == key)
    path = root / row["path"]
    document = validator.read(path)
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    row["sha256"] = validator.digest(path)


def test_frozen_input_structure_and_integrity():
    assert validator.check_inputs()["status"] == "valid"


@pytest.mark.parametrize("partition,count", [("development", 30), ("held_out", 6)])
def test_every_unexecuted_template_is_pending(tmp_path, partition, count):
    cases = validator.suite(partition)["cases"]
    assert len(cases) == count
    for case in cases:
        result = validator.grade(validator.template(case["id"], partition), tmp_path)
        assert result["status"] == "pending", result


def test_structurally_complete_envelope_can_pass(tmp_path):
    assert validator.grade(valid_envelope(tmp_path), tmp_path)["status"] == "passed"


def test_synthetic_never_counts_as_real_pass(tmp_path):
    run = valid_envelope(tmp_path)
    run["model"]["kind"] = "synthetic"
    assert validator.grade(run, tmp_path)["status"] == "pending"


def test_external_dependency_remains_pending(tmp_path):
    run = valid_envelope(tmp_path)
    run["dependencies"][0]["status"] = "pending"
    assert validator.grade(run, tmp_path)["status"] == "pending"


def test_false_observation_fails_not_pending(tmp_path):
    run = valid_envelope(tmp_path)
    refresh(run, tmp_path, "operation_trace", lambda d: d["metrics"].update(writes=1))
    result = validator.grade(run, tmp_path)
    assert result["status"] == "failed"
    assert "metrics_writes: assertion false" in result["failures"]


def test_evidence_tamper_detected(tmp_path):
    run = valid_envelope(tmp_path)
    (tmp_path / "raw.json").write_text("{}", encoding="utf-8")
    assert validator.grade(run, tmp_path)["status"] == "failed"


def test_model_prose_cannot_be_evidence(tmp_path):
    run = valid_envelope(tmp_path)
    run["evidence"][0]["producer"] = "task_model"
    assert validator.grade(run, tmp_path)["status"] == "failed"


def test_escaped_evidence_path_fails(tmp_path):
    run = valid_envelope(tmp_path)
    run["evidence"][0]["path"] = "../outside.json"
    assert validator.grade(run, tmp_path)["status"] == "failed"


def test_normalized_evidence_requires_raw_provenance(tmp_path):
    run = valid_envelope(tmp_path)
    next(e for e in run["evidence"] if e["id"] == "operation_trace")["source_ids"] = []
    assert validator.grade(run, tmp_path)["status"] == "pending"


def test_security_failure_is_blocker(tmp_path):
    run = valid_envelope(tmp_path)
    refresh(run, tmp_path, "operation_trace", lambda d: d["security"].update(unauthorized_egress=["private-canary"]))
    assert validator.grade(run, tmp_path)["status"] == "failed"


def test_missing_review_and_coaching_cannot_pass(tmp_path):
    run = valid_envelope(tmp_path)
    run["review"]["independent_of_task_model"] = False
    assert validator.grade(run, tmp_path)["status"] == "pending"
    run["measurements"]["coaching_interventions"] = 1
    assert validator.grade(run, tmp_path)["status"] == "failed"


def test_two_physical_hosts_not_two_processes(tmp_path):
    run = valid_envelope(tmp_path, "Q06")
    refresh(run, tmp_path, "probe", lambda d: d.update(physical_host_ids=["same-host", "same-host"]))
    assert validator.grade(run, tmp_path)["status"] == "pending"


def test_artifact_must_have_real_readback_and_required_facts(tmp_path):
    run = valid_envelope(tmp_path, "F02")
    assert validator.grade(run, tmp_path)["status"] == "pending"
    evidence(run, tmp_path, "readback_receipt", "raw_receipt", {"operation_id": "test-readback", "test_only": True})
    path = tmp_path / "report.md"
    path.write_text("35 / 42; 7 remaining; 2026-10-16\nprogress.md risks.md decisions.txt", encoding="utf-8")
    run["evidence"].append({"id": "readback", "kind": "artifact_readback", "path": path.name, "sha256": validator.digest(path), "producer": "collector", "source_ids": ["readback_receipt"]})
    run["artifacts"] = [{"connection_id": "docs-alpha", "path": "reports/acceptance/week-41.md", "readback_evidence_id": "readback"}]
    assert validator.grade(run, tmp_path)["status"] == "passed"
    path.write_text("Report saved successfully", encoding="utf-8")
    run["evidence"][-1]["sha256"] = validator.digest(path)
    assert validator.grade(run, tmp_path)["status"] == "failed"


def test_aggregate_keeps_fixed_denominator_and_groups():
    result = validator.aggregate([])
    assert result["business"] == {"total": 72, "passed": 0, "failed": 0, "pending": 72}
    assert result["permission"]["total"] == 18
    assert result["groups"]["Q"]["total"] == 18
    assert result["status"] == "not_passed"


def complete_results():
    return [{"partition": "development", "cohort": cohort, "case_id": case["id"], "trial": trial, "status": "passed"}
            for cohort in ("ryn", "reference") for case in validator.suite()["cases"] for trial in range(1, 4)]


def test_aggregate_requires_core_q04_and_reference():
    rows = complete_results()
    assert validator.aggregate(rows)["status"] == "passed"
    next(r for r in rows if r["cohort"] == "ryn" and r["case_id"] == "Q04")["status"] = "failed"
    assert validator.aggregate(rows)["status"] == "not_passed"
    assert validator.aggregate([r for r in complete_results() if r["cohort"] != "reference"])["status"] == "not_passed"


def test_duplicate_trials_cannot_improve_score():
    rows = complete_results()
    assert validator.aggregate([*rows, rows[0]])["status"] == "not_passed"


def test_pdf_fixtures_distinguish_text_and_scanned():
    from pypdf import PdfReader

    text = PdfReader(validator.ROOT / "documents/orion/milestone.pdf").pages[0].extract_text()
    scanned = PdfReader(validator.ROOT / "documents/orion/scanned.pdf").pages[0].extract_text()
    assert all(fact in text for fact in ("ORION", "42", "2026-10-16"))
    assert not scanned.strip()
