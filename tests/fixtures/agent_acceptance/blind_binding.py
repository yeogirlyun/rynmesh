"""Public version binding for an independently operated blind evaluator.

This module never loads the private suite and is not a business grader. The
private evaluator must use the original assertions and actual role collectors.
Its reviewed implementation is a required release artifact before begin().
Filesystem hashes detect drift, not a malicious operator on the same host.
"""
from __future__ import annotations

import copy
import uuid
from pathlib import Path

import release_registry as registry

CAPABILITIES = ["fresh_registration_required", "original_evidence_required",
                "no_legacy_backfill", "isolated_evaluator", "contents_not_exported",
                "version_bound_results"]
REQUIRED_PRIVATE_ARTIFACTS = {"blind_evaluator", "blind_suite_commitment"}


def descriptor():
    return {"kind": "blind_interface", "dataset_version": registry.VERSION,
            "implementation": registry.binding(__file__), "capabilities": CAPABILITIES.copy()}


def begin(activation_path, private_root, evaluator_id):
    """Called by the independent evaluator before creating tasks or dispatching.

    The opaque commitment is supplied by that evaluator, never by opening the
    suite in the product author's process. A fresh directory is mandatory.
    """
    _activation, manifest = registry._active(activation_path)
    if not registry._text(evaluator_id) or evaluator_id in manifest["authors"]:
        registry.reject("blind_independent_evaluator_required")
    if not REQUIRED_PRIVATE_ARTIFACTS <= set(manifest["artifacts"]):
        registry.reject("blind_private_executor_not_ready")
    interface = registry.read(manifest["artifacts"]["blind_interface"]["path"])
    if interface != descriptor():
        registry.reject("blind_interface_not_activated")
    commitment = registry.read(manifest["artifacts"]["blind_suite_commitment"]["path"])
    registry._exact(commitment, {"suite_sha256", "cases", "repetitions"}, "blind_commitment_invalid")
    if (not registry._hash(commitment["suite_sha256"])
            or type(commitment["cases"]) is not int or commitment["cases"] != 6
            or type(commitment["repetitions"]) is not int or commitment["repetitions"] != 3):
        registry.reject("blind_commitment_invalid")
    root = Path(private_root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        registry.reject("blind_fresh_workspace_required")
    row = {"kind": "ryn.agent.blind-registration", "dataset_version": registry.VERSION,
           "registered_at": registry._now(), "nonce": uuid.uuid4().hex,
           "evaluator_id": evaluator_id, "private_root": str(root),
           "activation": registry.binding(activation_path), "interface": registry.binding(__file__),
           "original_grader": registry.binding(Path(__file__).with_name("validate.py")),
           "original_schema": registry.binding(Path(__file__).with_name("run.schema.json")),
           "private_artifacts": {name: copy.deepcopy(manifest["artifacts"][name])
                                 for name in sorted(REQUIRED_PRIVATE_ARTIFACTS)},
           "suite_commitment": commitment}
    path = root / "blind-registration.json"
    registry._new(path, row)
    return registry.binding(path)


def check(registration_binding):
    """Revalidate immediately before every model/tool dispatch and publication."""
    registry._check_file(registration_binding)
    row = registry.read(registration_binding["path"])
    for name in ("activation", "interface", "original_grader", "original_schema"):
        registry._check_file(row[name])
    _activation, manifest = registry._active(row["activation"]["path"])
    if (row["kind"] != "ryn.agent.blind-registration" or row["dataset_version"] != registry.VERSION
            or Path(registration_binding["path"]).resolve() != Path(row["private_root"]) / "blind-registration.json"
            or row["evaluator_id"] in manifest["authors"]
            or row["interface"] != registry.binding(__file__)
            or any(row["private_artifacts"][name] != manifest["artifacts"].get(name)
                   for name in REQUIRED_PRIVATE_ARTIFACTS)):
        registry.reject("blind_registration_changed")
    for binding in row["private_artifacts"].values():
        registry._check_file(binding)
    return row


def public_result(registration_binding, *, aggregate, private_evidence_binding, role_status):
    """Whitelist export, called only inside the reviewed independent evaluator.

    aggregate must come from unchanged validate.aggregate(..., 'held_out').
    role_status comes from the same release's actual role/dispatch audits. This
    function binds and redacts their result; it does not authenticate assertions
    supplied by an arbitrary caller or substitute for that evaluator's review.
    """
    row = check(registration_binding)
    registry._check_file(private_evidence_binding)
    evidence_path = Path(private_evidence_binding["path"]).resolve()
    if not evidence_path.is_relative_to(Path(row["private_root"])):
        registry.reject("blind_original_evidence_outside_fresh_workspace")
    counts = aggregate["business"]
    permissions = aggregate["permission"]
    total = {key: counts[key] + permissions[key] for key in ("total", "passed", "failed", "pending")}
    if (any(type(value) is not int or value < 0 for value in total.values())
            or total["total"] != 18 or sum(total[k] for k in ("passed", "failed", "pending")) != 18
            or aggregate.get("partition") != "held_out"
            or role_status not in {"passed", "failed", "pending"}):
        registry.reject("blind_aggregate_invalid")
    status = aggregate["status"]
    if status not in {"passed", "not_passed"}:
        registry.reject("blind_aggregate_invalid")
    if role_status == "failed" or total["failed"] or aggregate.get("duplicate_trials"):
        status = "failed"
    elif status != "passed" or role_status != "passed" or total["passed"] != 18:
        status = "pending"
    return {"kind": "ryn.agent.blind-summary", "dataset_version": registry.VERSION,
            "registration_sha256": registration_binding["sha256"],
            "activation_sha256": row["activation"]["sha256"],
            "suite_sha256": row["suite_commitment"]["suite_sha256"],
            "private_evidence_sha256": private_evidence_binding["sha256"],
            "status": status, "candidate_trials": total,
            "limitation": "Version binding only; requires the reviewed isolated evaluator and original evidence."}
