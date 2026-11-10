"""Fresh registered v3 trials, using the unchanged common and role assertions."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import release_registry as registry
from jsonschema import Draft202012Validator
from role_grade_v2 import DOCS, DraftRoleGrader, file_digest

VERSION = "ryn-agent-role-grader.v3.1"
SCHEMA = DOCS / "role-run-v3.schema.json"


def registration(path, *, require_dispatch=False):
    """Read original canonical registry files; never import a historical run."""
    path = Path(path).resolve()
    row = registry.read(path)
    registry._check_file(row["reservation"])
    reserved = registry.read(row["reservation"]["path"])
    registry._check_file(reserved["activation"])
    active, manifest = registry._active(reserved["activation"]["path"])
    bound = row["binding"]
    slot = registry.slot_path(manifest, bound)
    if (path != slot / "registration.json" or Path(row["reservation"]["path"]).resolve() != slot / "reservation.json"
            or row["kind"] != "ryn.agent.trial-registration" or bound != reserved["binding"]
            or bound["dataset_version"] != registry.VERSION or bound["manifest_sha256"] != active["manifest"]["sha256"]
            or bound["case_id"] not in manifest["covered_cases"] or row["observation"]["binding"] != bound
            or row["observation"]["prior_dispatch_count"] != 0):
        raise ValueError("release_registration_binding_invalid")
    previous, ids, dispatches = None, set(), []
    for index, item in enumerate(sorted((slot / "dispatch").glob("*.json")), 1):
        event = registry.read(item)
        request = event["request"]
        if (event["binding"] != bound or event["registration_sha256"] != registry.sha(path)
                or event["sequence"] != index or event["previous_sha256"] != previous
                or request["kind"] not in {"model", "tool"} or request["id"] in ids
                or item.name != f"{index:06}-{registry.digest(request)}.json"
                or registry._time(event["admitted_at"]) < registry._time(row["registered_at"])):
            raise ValueError("release_dispatch_chain_invalid")
        previous = registry.sha(item)
        ids.add(request["id"])
        dispatches.append(event)
    if require_dispatch and not dispatches:
        raise ValueError("release_original_dispatch_missing")
    return row, manifest, dispatches


class ReleaseRoleGrader(DraftRoleGrader):
    def __init__(self):
        super().__init__()
        self.schema = registry.read(SCHEMA)
        Draft202012Validator.check_schema(self.schema)
        self.suite["dataset_version"] = registry.VERSION
        self.suite_hash = registry.digest({"dataset_version": registry.VERSION, "contract": registry.contract_snapshot()})
        self.runtime_hashes.update({Path(__file__): file_digest(__file__), SCHEMA: file_digest(SCHEMA),
                                   Path(registry.__file__): file_digest(registry.__file__)})

    def registration_sampling_kind(self):
        return "new_v3_registered_sampling"

    def template_registered(self, registration_path):
        registered, _manifest, _events = registration(registration_path)
        bound = registered["binding"]
        body = self._common_template(bound["case_id"])
        body.update(cohort=bound["cohort"], trial=bound["trial"])
        return {"schema_version": 3, "kind": "ryn.agent.role-evidence-run",
                "dataset_version": registry.VERSION, "status": "registered", "formal_trial_registered": True,
                "run_id": bound["run_id"], "run_nonce": bound["run_nonce"],
                "task_id": registered["observation"]["task_id"],
                "node_bindings": copy.deepcopy(registered["observation"]["node_bindings"]),
                "contract": self._binding(bound["case_id"]), "registration": registry.binding(registration_path),
                "run": body, "role_evidence": dict.fromkeys(self._role_kinds())}

    @staticmethod
    def _role_kinds():
        from role_grade_v2 import ROLE_KINDS
        return ROLE_KINDS

    def grade(self, envelope, run_root):
        if isinstance(envelope, dict) and "components" in envelope:
            return self._grade_bundle(envelope, Path(run_root))
        return self._grade(envelope, run_root)

    def _grade_bundle(self, envelope, root):
        parent = copy.deepcopy(envelope)
        components = parent.pop("components")
        result = self._grade(parent, root, mode="bundle")
        try:
            required = registry.subscenarios(parent["run"]["case_id"])
            if not required or not isinstance(components, dict) or set(components) - set(required):
                raise ValueError("release_bundle_slots_invalid")
            if set(components) != set(required):
                result["pending"].append("complete fixed subscenario bundle required")
            identities, anchor = set(), False
            for slot, entry in components.items():
                path = self.common.confined(root, entry["path"])
                if set(entry) != {"path", "sha256"} or registry.sha(path) != entry["sha256"]:
                    raise ValueError("release_bundle_component_changed")
                child = registry.read(path)
                if "components" in child:
                    raise ValueError("release_nested_bundle_invalid")
                registered, _manifest, _dispatch = registration(child["registration"]["path"], require_dispatch=True)
                bound = registered["binding"]
                if (bound.get("subscenario") != slot or bound["manifest_sha256"] != result.get("manifest_sha256")
                        or any(bound[key] != parent["run"][key] for key in ("case_id", "cohort", "trial"))
                        or any(bound[key] in identities for key in ("run_id", "run_nonce"))):
                    raise ValueError("release_bundle_component_binding_mismatch")
                identities.update(bound[key] for key in ("run_id", "run_nonce"))
                anchor |= child["registration"] == parent["registration"]
                checked = self._grade(child, path.parent, mode="component")
                result["failures"].extend(f"{slot}: {item}" for item in checked["failures"])
                result["pending"].extend(f"{slot}: {item}" for item in checked["pending"])
                for flag in ("contract_integrity_failed", "evidence_integrity_failed", "role_contract_failed"):
                    result[flag] = result.get(flag, False) or checked.get(flag, False)
            if not anchor:
                raise ValueError("release_bundle_original_anchor_missing")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            result["evidence_integrity_failed"] = True
            result["failures"].append("invalid_or_unreadable_release_bundle")
        result["evidence_complete"] = not result["pending"] and not result["contract_integrity_failed"] and not result["evidence_integrity_failed"]
        result["status"] = "failed" if result["failures"] else "pending" if result["pending"] else "passed"
        result["formal_pass"] = result["status"] == "passed"
        return result

    def _grade(self, envelope, run_root, *, mode="standalone"):
        failures, pending = [], []
        result = {"status": "failed", "formal_pass": False, "formal_trial_registered": False,
                  "grader_version": VERSION, "failures": failures, "pending": pending,
                  "contract_integrity_failed": True, "evidence_integrity_failed": False, "evidence_complete": False}
        try:
            self._check_pins()
            if any(file_digest(p) != sha for p, sha in self.runtime_hashes.items()):
                raise ValueError("release_grader_snapshot_changed")
            if not Draft202012Validator(self.schema).is_valid(envelope):
                raise ValueError("release_run_schema_invalid")
            registry._check_file(envelope["registration"])
            registered, manifest, dispatches = registration(envelope["registration"]["path"], require_dispatch=True)
            bound, run = registered["binding"], envelope["run"]
            if (any(envelope[key] != bound[key] for key in ("dataset_version", "run_id", "run_nonce"))
                    or any(run[key] != bound[key] for key in ("case_id", "cohort", "trial"))
                    or envelope["task_id"] != registered["observation"]["task_id"]
                    or envelope["node_bindings"] != registered["observation"]["node_bindings"]
                    or envelope["contract"] != self._binding(run["case_id"])
                    or run["dataset_version"] != registry.VERSION or run["suite_sha256"] != self.suite_hash
                    or run["partition"] != "development"):
                raise ValueError("release_run_registration_mismatch")
            pipeline = registry.read(manifest["artifacts"]["grader"]["path"])
            if pipeline["implementation"] != registry.binding(__file__):
                raise ValueError("release_grader_not_activated")
            if not Draft202012Validator(self.common_schema).is_valid(run):
                raise ValueError("release_common_run_schema_invalid")
            result.update(case_id=run["case_id"], cohort=run["cohort"], trial=run["trial"],
                          partition="development",
                          run_id=bound["run_id"], run_nonce=bound["run_nonce"], dataset_version=registry.VERSION,
                          manifest_sha256=bound["manifest_sha256"], formal_trial_registered=True,
                          contract_integrity_failed=False)
            if bound.get("subscenario") and mode == "standalone":
                pending.append("complete fixed subscenario bundle required; a component cannot count as a trial")
            common = self._common_grade(run, Path(run_root))
            if mode == "component":
                # The complete parent evaluates every original business assertion.
                # Each actual child must still satisfy the original task outcome;
                # evidence, security, dependencies and role checks stay intact.
                parent_checks = {a["id"] for a in self.cases[run["case_id"]]["effective_case"]["expected"]["assertions"]
                                 if a["evidence_kind"] != "task_state"}
                for field in ("failures", "pending"):
                    common[field] = [item for item in common[field] if item.split(":", 1)[0] not in parent_checks]
            failures.extend(common["failures"])
            pending.extend(common["pending"])
            result["evidence_integrity_failed"] = any(
                any(marker in failure for marker in ("evidence hash mismatch", "duplicate/invalid evidence identity",
                                                     "untrusted producer", "invalid evidence (", "invalid dependency evidence kind"))
                for failure in common["failures"])
            role_failures, role_pending = self._role_checks(envelope, Path(run_root))
            failures.extend(role_failures)
            pending.extend(role_pending)
            result["role_contract_failed"] = bool(role_failures)
            # The role audit must match the registry's complete original ID set.
            _entries, documents = self._documents(run, Path(run_root))
            audits = [event for doc in documents.values() if isinstance(doc, dict)
                      for event in doc.get("dispatch_audit", {}).get("events", [])]
            expected = {(e["request"]["kind"], e["request"]["id"]) for e in dispatches}
            if len(audits) != len(expected) or {(e.get("kind"), e.get("id")) for e in audits} != expected:
                pending.append("complete original registry dispatch coverage missing")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            result["evidence_integrity_failed"] = True
            failures.append("invalid_or_unreadable_release_binding")
        result["evidence_complete"] = not pending and not result["contract_integrity_failed"] and not result["evidence_integrity_failed"]
        result["status"] = "failed" if failures else "pending" if pending else "passed"
        result["formal_pass"] = result["status"] == "passed"
        return result

    def aggregate(self, paths):
        results, failures, identities, manifests = [], [], set(), set()
        for path in paths:
            try:
                path = Path(path)
                row = self.grade(registry.read(path), path.resolve().parent)
                identity = (row.get("run_id"), row.get("run_nonce"))
                if None in identity or any(part in identities for part in identity):
                    failures.append("invalid_or_reused_run_identity")
                identities.update(identity)
                manifests.add(row.get("manifest_sha256"))
                results.append(row)
            except (OSError, ValueError, TypeError):
                failures.append("unreadable_run")
        if len(manifests) != 1 or None in manifests:
            failures.append("mixed_or_missing_release_manifest")
        valid = [r for r in results if all(k in r for k in ("case_id", "cohort", "trial"))]
        common = self._common_aggregate([{**r, "status": r["status"] if r["evidence_complete"] else "pending"}
                                         for r in valid], partition="development")
        reference_permissions = [r for r in valid if r["cohort"] == "reference" and r["case_id"].startswith("P")]
        reference_complete = len(reference_permissions) == 18 and all(r["evidence_complete"] for r in reference_permissions)
        if common["duplicate_trials"]:
            failures.append("duplicate cohort/case/trial")
        if any(r.get("contract_integrity_failed") or r.get("evidence_integrity_failed") for r in results):
            failures.append("contract/evidence integrity failure cannot count as an ordinary business failure")
        mandatory = {"Q04", "F02", "C01", "R02", "R03", "R04", "R05", "R06"}
        hard_failure = any(
            row.get("role_contract_failed") or any("security violation:" in error for error in row["failures"])
            or (row["cohort"] == "ryn" and row["status"] == "failed"
                and (row["case_id"] in mandatory or row["case_id"].startswith("P")))
            for row in valid)
        impossible_absolute_gate = common["business"]["passed"] + common["business"]["pending"] < 58
        completed_business = common["business"]["pending"] == common["reference"]["pending"] == 0
        conclusive_ratio_failure = completed_business and not common["reference_ratio_gate"]
        failed = bool(failures or hard_failure or impossible_absolute_gate or conclusive_ratio_failure)
        passed = common["status"] == "passed" and reference_complete and not failed
        return {"status": "failed" if failed else "passed" if passed else "pending",
                "development_gates_passed": passed, "formal_pass": False, "grader_version": VERSION,
                "dataset_version": registry.VERSION, "results": common, "failures": failures,
                "reference_permission_trials_complete": reference_complete,
                "pending": ["External, desktop and isolated six-case blind gates remain independently required."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("template", "grade", "aggregate"))
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    grader = ReleaseRoleGrader()
    if args.command == "template":
        result = grader.template_registered(args.paths[0])
    elif args.command == "grade":
        path = Path(args.paths[0])
        result = grader.grade(registry.read(path), path.resolve().parent)
    else:
        result = grader.aggregate(args.paths)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"registered", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
