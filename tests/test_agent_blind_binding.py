"""Synthetic interface tests; never load the actual held-out suite."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures/agent_acceptance"
sys.path.insert(0, str(FIXTURES))
import blind_binding as blind  # noqa: E402


def setup_binding(tmp_path, monkeypatch):
    def save(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return blind.registry.binding(path)

    activation = save("activation.json", {"synthetic": True})
    artifacts = {
        "blind_interface": save("interface.json", blind.descriptor()),
        "blind_evaluator": save("synthetic_evaluator.json", {"synthetic_only": True}),
        "blind_suite_commitment": save("commitment.json", {"suite_sha256": "a" * 64,
                                                           "cases": 6, "repetitions": 3}),
    }
    manifest = {"authors": ["product-author"], "artifacts": artifacts}
    monkeypatch.setattr(blind.registry, "_active", lambda _path: ({}, manifest))
    return activation, manifest


def aggregate(monkeypatch, statuses=None):
    # Use the original aggregate with six invented empty cases, never suite().
    spec = importlib.util.spec_from_file_location("synthetic_blind_common", FIXTURES / "validate.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    cases = [{"id": f"synthetic-{n}", "group": "P" if n == 6 else "F"} for n in range(1, 7)]
    monkeypatch.setattr(common, "suite", lambda _partition: {"cases": cases})
    rows = [{"partition": "held_out", "case_id": case["id"], "cohort": "ryn", "trial": trial,
             "status": (statuses or {}).get((case["id"], trial), "passed")}
            for case in cases for trial in range(1, 4)]
    return common.aggregate(rows, "held_out")


def test_fresh_independent_binding_and_drift(tmp_path, monkeypatch):
    activation, manifest = setup_binding(tmp_path, monkeypatch)
    root = tmp_path / "private-run"
    with pytest.raises(ValueError, match="independent_evaluator"):
        blind.begin(activation["path"], root, "product-author")
    binding = blind.begin(activation["path"], root, "independent-evaluator")
    assert blind.check(binding)["suite_commitment"]["cases"] == 6
    with pytest.raises(ValueError, match="fresh_workspace"):
        blind.begin(activation["path"], root, "independent-evaluator")
    Path(manifest["artifacts"]["blind_evaluator"]["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_changed"):
        blind.check(binding)


def test_export_whitelist_preserves_fixed_denominator_and_pending_roles(tmp_path, monkeypatch):
    activation, _manifest = setup_binding(tmp_path, monkeypatch)
    root = tmp_path / "private-run"
    binding = blind.begin(activation["path"], root, "independent-evaluator")
    evidence = root / "private-evidence.json"
    evidence.write_text('{"synthetic_private_text":"DO_NOT_EXPORT"}', encoding="utf-8")
    counts = aggregate(monkeypatch)
    counts["private_details"] = "DO_NOT_EXPORT"
    exported = blind.public_result(binding, aggregate=counts,
                                   private_evidence_binding=blind.registry.binding(evidence), role_status="pending")
    assert exported["status"] == "pending"
    assert exported["candidate_trials"] == {"total": 18, "passed": 18, "failed": 0, "pending": 0}
    assert "DO_NOT_EXPORT" not in json.dumps(exported)
    assert "private-run" not in json.dumps(exported)
    failed = aggregate(monkeypatch, {("synthetic-1", 1): "failed"})
    assert blind.public_result(binding, aggregate=failed,
                               private_evidence_binding=blind.registry.binding(evidence),
                               role_status="passed")["status"] == "failed"
    missing = aggregate(monkeypatch, {("synthetic-1", 1): "pending"})
    assert blind.public_result(binding, aggregate=missing,
                               private_evidence_binding=blind.registry.binding(evidence),
                               role_status="passed")["status"] == "pending"
    with pytest.raises(ValueError, match="outside_fresh_workspace"):
        blind.public_result(binding, aggregate=counts, private_evidence_binding=activation, role_status="passed")


def test_missing_private_executor_cannot_start(tmp_path, monkeypatch):
    activation, manifest = setup_binding(tmp_path, monkeypatch)
    del manifest["artifacts"]["blind_evaluator"]
    with pytest.raises(ValueError, match="private_executor_not_ready"):
        blind.begin(activation["path"], tmp_path / "private-run", "independent-evaluator")
    assert not (tmp_path / "private-run").exists()
