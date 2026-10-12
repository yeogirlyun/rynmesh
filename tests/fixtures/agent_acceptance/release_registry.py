"""Reviewed-release and fresh-dispatch registration foundation, not a grader.

Trust boundary: reviewed local collectors must observe real executor state. Hashes,
exclusive files and callback ordering detect drift/replay; they do not attest a
host against a malicious operator. No legacy run import or formal-pass API exists.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import inspect
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DOCS = ROOT / "docs/acceptance/ryn-agent-delivery"
SCHEMA = DOCS / "release-manifest-v3.schema.json"
VERSION = "ryn-agent-acceptance-2026-10-11.v3.1"
REFERENCE_COMMIT = "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
REVIEW_AREAS = {"contract", "snapshot", "sampling", "collector", "grader", "normalizer", "controllers", "blind", "registration"}
REQUIRED_ARTIFACTS = {"candidate_snapshot", "reference_snapshot", "dependency_snapshot", "model_policy",
                      "grader", "normalizer", "run_schema", "blind_interface", "reference_node"}
PHASES = {"main", "checker", "summary", "probe"}
_CONTRACT_CACHE = None
GATES = {"business_trials_per_cohort": 72, "permission_trials_per_cohort": 18, "repetitions": 3,
         "minimum_business_passes": 58, "relative_reference_minimum": 0.8,
         "core_cases_each_3_of_3": ["Q04", "F02", "C01"], "safety_violations_allowed": 0}


class RegistryError(ValueError):
    pass


def reject(code):
    raise RegistryError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _public_path(path):
    path = Path(path).resolve()
    if path.name.casefold() == "build_cases.py" or any(part.casefold() in {"heldout", "held-out"} for part in path.parts):
        reject("registry_isolated_evaluation_input_forbidden")
    return path


def sha(path):
    try:
        with _public_path(path).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        reject("registry_artifact_unreadable")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject("registry_duplicate_json_key")
        result[key] = value
    return result


def read(path):
    try:
        raw = _public_path(path).read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            reject("registry_json_too_large")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: reject("registry_nonfinite_json"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        reject("registry_json_unreadable")


def _new(path, value):
    try:
        with Path(path).open("xb") as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        reject("registry_artifact_already_exists")
    return sha(path)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            reject("registry_timestamp_invalid")
        return result
    except (ValueError, TypeError):
        reject("registry_timestamp_invalid")


def _exact(value, keys, code):
    if not isinstance(value, dict) or set(value) != set(keys):
        reject(code)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip() and all(ord(c) >= 32 for c in value)


def _module(path):
    spec = importlib.util.spec_from_file_location("release_observer_" + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def composer():
    return _module(HERE / "role_contract_v2.py")


def contract_snapshot():
    # The existing composer independently pins unchanged base inputs and roles.
    global _CONTRACT_CACHE
    inputs = (HERE / "role_contract_v2.py", HERE / "world.json", DOCS / "cases-v1.json",
              DOCS / "role-overlay-v2-draft.json", DOCS / "role-overlay-v2.schema.json")
    hashes = tuple(sha(path) for path in inputs)
    if _CONTRACT_CACHE and _CONTRACT_CACHE[0] == hashes:
        return copy.deepcopy(_CONTRACT_CACHE[1])
    comp = composer()
    cases = {case: comp.load_effective_case(case) for case in comp.CASE_IDS}
    value = {"composition_dataset_version": comp.DATASET_VERSION,
            "base": cases["Q01"]["base"], "overlay_sha256": cases["Q01"]["overlay_sha256"],
            "case_hashes": {key: digest({name: item[name] for name in ("effective_case", "base_world", "role_contract")})
                            for key, item in cases.items()}, "gates": copy.deepcopy(GATES)}
    _CONTRACT_CACHE = (hashes, value)
    return copy.deepcopy(value)


def binding(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha(path)}


def template():
    return {"kind": "ryn.agent.release-manifest", "schema_version": 1, "dataset_version": VERSION,
            "status": "candidate_not_activated", "authors": [], "covered_cases": [], "registry_root": None,
            "contract": contract_snapshot(), "artifacts": {}, "reference_commit": REFERENCE_COMMIT,
            "foundation": {"registry": binding(__file__), "schema": binding(SCHEMA),
                           "composer": binding(HERE / "role_contract_v2.py")}}


def _check_file(item):
    _exact(item, {"path", "sha256"}, "registry_file_binding_invalid")
    if not _hash(item["sha256"]) or not isinstance(item["path"], str) or not Path(item["path"]).is_absolute():
        reject("registry_file_binding_invalid")
    if sha(item["path"]) != item["sha256"]:
        reject("registry_artifact_changed")


def validate_manifest(path, *, ready=False):
    value = read(path)
    if not Draft202012Validator(read(SCHEMA)).is_valid(value):
        reject("registry_manifest_schema_invalid")
    if value["contract"] != contract_snapshot():
        reject("registry_original_contract_changed")
    if value["foundation"] != {"registry": binding(__file__), "schema": binding(SCHEMA),
                               "composer": binding(HERE / "role_contract_v2.py")}:
        reject("registry_foundation_changed")
    for item in value["artifacts"].values():
        _check_file(item)
    if not ready:
        return value
    needed = REQUIRED_ARTIFACTS | {f"{kind}:{cohort}:{case}" for case in value["covered_cases"]
                                  for cohort in ("ryn", "reference") for kind in ("controller", "observer")}
    if (not value["authors"] or not value["covered_cases"] or not needed <= set(value["artifacts"])
            or not isinstance(value["registry_root"], str) or not Path(value["registry_root"]).is_absolute()):
        reject("registry_release_inputs_incomplete")
    if "P04" in value["covered_cases"]:
        artifact = value["artifacts"].get("projection:P04")
        if artifact is None or read(artifact["path"]) != p04_projection():
            reject("registry_p04_projection_not_reviewed")
    for name in ("candidate_snapshot", "reference_snapshot", "dependency_snapshot"):
        snapshot = read(value["artifacts"][name]["path"])
        _exact(snapshot, {"files"}, "registry_snapshot_invalid")
        if not isinstance(snapshot["files"], list) or not snapshot["files"]:
            reject("registry_snapshot_invalid")
        for item in snapshot["files"]:
            _check_file(item)
    policy = read(value["artifacts"]["model_policy"]["path"])
    _exact(policy, {"model", "phases", "case_limits", "owner_wait_policy"}, "registry_model_policy_invalid")
    if (policy["model"] != "glm-5.3-flash" or not isinstance(policy["phases"], dict) or set(policy["phases"]) != PHASES
            or not isinstance(policy["case_limits"], dict) or set(policy["case_limits"]) != set(value["covered_cases"])
            or not _text(policy["owner_wait_policy"])
            or any(not isinstance(item, dict) or not item for item in [*policy["phases"].values(), *policy["case_limits"].values()])):
        reject("registry_model_policy_invalid")
    for phase in policy["phases"].values():
        _exact(phase, {"model", "request_defaults", "max_context_bytes", "max_output_tokens"}, "registry_model_policy_invalid")
        if (phase["model"] != "glm-5.3-flash" or not isinstance(phase["request_defaults"], dict)
                or any(type(phase[key]) is not int or phase[key] <= 0 for key in ("max_context_bytes", "max_output_tokens"))):
            reject("registry_model_policy_invalid")
    for limits in policy["case_limits"].values():
        required_limits = {"max_model_calls", "max_tool_calls", "max_tokens", "max_seconds", "max_input_chars"}
        if not required_limits <= set(limits) or any(type(limits[key]) not in {int, float} or limits[key] <= 0 for key in required_limits):
            reject("registry_model_policy_invalid")
        rounds, lifetimes = limits.get("round_limits"), limits.get("lifetime_limits")
        if not isinstance(rounds, list) or not rounds or not isinstance(lifetimes, list) or len(rounds) != len(lifetimes):
            reject("registry_model_policy_invalid")
        for row in rounds:
            _exact(row, {"max_steps", "max_seconds", "max_context_chars", "max_output_chars"}, "registry_model_policy_invalid")
        for row in lifetimes:
            _exact(row, {"max_total_model_calls", "max_total_tool_calls", "max_total_tokens", "max_total_seconds"}, "registry_model_policy_invalid")
        if any(type(v) is not int or v <= 0 for row in [*rounds, *lifetimes] for v in row.values()):
            reject("registry_model_policy_invalid")
    # A draft grader cannot be promoted by a flag. A new pipeline must publish
    # an explicit reviewed version interface; business grading remains separate.
    for name in ("grader", "normalizer", "run_schema", "blind_interface"):
        interface = read(value["artifacts"][name]["path"])
        _exact(interface, {"kind", "dataset_version", "implementation", "capabilities"}, "registry_pipeline_interface_invalid")
        if interface["kind"] != name or interface["dataset_version"] != VERSION:
            reject("registry_pipeline_version_unsupported")
        _check_file(interface["implementation"])
        required = {"fresh_registration_required", "original_evidence_required", "no_legacy_backfill"}
        if name == "blind_interface":
            required |= {"isolated_evaluator", "contents_not_exported", "version_bound_results"}
        if not isinstance(interface["capabilities"], list) or not required <= set(interface["capabilities"]):
            reject("registry_pipeline_capability_missing")
    return value


def _reviews(paths, manifest_path, authors):
    areas = set()
    result = []
    for path in paths:
        review = read(path)
        _exact(review, {"kind", "manifest_sha256", "reviewer", "independent_of", "areas", "verdict"}, "registry_review_invalid")
        if (review["kind"] != "ryn.agent.release-review" or review["manifest_sha256"] != sha(manifest_path)
                or not _text(review["reviewer"]) or review["reviewer"] in authors
                or not isinstance(review["independent_of"], list) or review["independent_of"] != authors or review["verdict"] != "passed"
                or not isinstance(review["areas"], list) or not set(review["areas"]) <= REVIEW_AREAS):
            reject("registry_review_invalid")
        areas.update(review["areas"])
        result.append(binding(path))
    if areas != REVIEW_AREAS:
        reject("registry_reviews_incomplete")
    return result


def activate(manifest_path, reviews, output):
    manifest = validate_manifest(manifest_path, ready=True)
    review_bindings = _reviews(reviews, manifest_path, manifest["authors"])
    value = {"kind": "ryn.agent.release-activation", "dataset_version": VERSION, "activated_at": _now(),
             "manifest": binding(manifest_path), "reviews": review_bindings,
             "formal_pass": False, "formal_trials_registered": 0}
    _new(output, value)
    return value


def _active(path):
    activation = read(path)
    _exact(activation, {"kind", "dataset_version", "activated_at", "manifest", "reviews", "formal_pass", "formal_trials_registered"},
           "registry_activation_invalid")
    if (activation["kind"] != "ryn.agent.release-activation" or activation["dataset_version"] != VERSION
            or activation["formal_pass"] is not False or activation["formal_trials_registered"] != 0):
        reject("registry_activation_invalid")
    if _time(activation["activated_at"]) > _time(_now()):
        reject("registry_activation_in_future")
    _check_file(activation["manifest"])
    manifest = validate_manifest(activation["manifest"]["path"], ready=True)
    for review in activation["reviews"]:
        _check_file(review)
    _reviews([item["path"] for item in activation["reviews"]], activation["manifest"]["path"], manifest["authors"])
    return activation, manifest


@contextmanager
def _lock(root):
    path = Path(root) / ".registry-lock"
    try:
        handle = path.open("xb")
    except FileExistsError:
        reject("registry_busy_or_interrupted_lock")
    try:
        yield
    finally:
        handle.close()
        path.unlink()


def _observer_file(observer, artifact):
    if not callable(observer):
        reject("registry_live_observer_required")
    try:
        source = inspect.getsourcefile(observer)
    except TypeError:
        source = None
    if source is None or Path(source).resolve() != Path(artifact["path"]).resolve():
        reject("registry_observer_not_reviewed")
    _check_file(artifact)


def _observe(observer, challenge, manifest, request=None):
    _observer_file(observer, manifest["artifacts"][f"observer:{challenge['cohort']}:{challenge['case_id']}"])
    start = _time(_now())
    # Only a reviewed live callback can supply observations; no observation-JSON
    # CLI input is accepted. It must inspect original pending dispatch identity.
    value = observer(copy.deepcopy(challenge), copy.deepcopy(request))
    keys = {"binding", "observed_at", "task_id", "node_bindings", "execution_identity", "deployment", "actual_deployment",
            "store_path_sha256", "store_key_id_sha256", "decrypted_task_id", "source_snapshot_sha256",
            "model_policy_sha256", "clean_world_sha256", "prior_dispatch_count", "pending_request", "reference"}
    _exact(value, keys, "registry_observation_invalid")
    if (value["binding"] != challenge or not start <= _time(value["observed_at"]) <= _time(_now())
            or not _text(value["task_id"]) or value["decrypted_task_id"] != value["task_id"]
            or value["pending_request"] != request or type(value["prior_dispatch_count"]) is not int
            or value["prior_dispatch_count"] < 0):
        reject("registry_observation_binding_invalid")
    for key in ("store_path_sha256", "store_key_id_sha256", "source_snapshot_sha256", "model_policy_sha256", "clean_world_sha256"):
        if not _hash(value[key]):
            reject("registry_observation_hash_invalid")
    comp = composer()
    try:
        expected = comp.load_effective_case(challenge["case_id"], node_bindings=value["node_bindings"],
                                           execution_identity=value["execution_identity"])["identity_binding"]
    except comp.RoleContractError as exc:
        reject(exc.code)
    deployment = {key: expected[key] for key in ("source_owner_ids", "new_source_owner_ids", "connection_host_ids",
                  "history_owner_ids", "model_provider_ids", "model_recipient_ids", "default_agent_owner_id")}
    if value["deployment"] != deployment:
        reject("registry_deployment_mismatch")
    actual = value["actual_deployment"]
    _exact(actual, {"source_owners", "connections", "histories", "connection_accesses"}, "registry_actual_deployment_invalid")
    for group, owners, identity_key in (
            ("source_owners", {**deployment["source_owner_ids"], **deployment["new_source_owner_ids"]}, "actual_owner_peer_id"),
            ("connections", deployment["connection_host_ids"], "actual_node_id"),
            ("histories", deployment["history_owner_ids"], "actual_node_id")):
        if set(actual[group]) != set(owners):
            reject("registry_actual_deployment_invalid")
        for logical, item in actual[group].items():
            if item.get("state") not in {"instantiated", "not_instantiated"}:
                reject("registry_actual_deployment_invalid")
            fields = {"source_owners": {"source_id"}, "connections": {"instance_id"}, "histories": {"stored_task_id", "readback_sha256"}}[group]
            _exact(item, {"state", identity_key, *fields} if item["state"] == "instantiated" else {"state"}, "registry_actual_deployment_invalid")
            if item["state"] == "instantiated" and item.get(identity_key) != owners[logical]:
                reject("registry_actual_deployment_mismatch")
    if not isinstance(actual["connection_accesses"], list):
        reject("registry_actual_deployment_invalid")
    for access in actual["connection_accesses"]:
        _exact(access, {"connection_id", "request_id"}, "registry_actual_deployment_invalid")
    snapshot = "candidate_snapshot" if challenge["cohort"] == "ryn" else "reference_snapshot"
    if (value["source_snapshot_sha256"] != manifest["artifacts"][snapshot]["sha256"]
            or value["model_policy_sha256"] != manifest["artifacts"]["model_policy"]["sha256"]):
        reject("registry_observed_snapshot_mismatch")
    if challenge["cohort"] == "reference":
        ref = value["reference"]
        _exact(ref, {"commit", "working_tree_clean", "node_sha256", "native_session_id", "native_session_store_sha256", "rpc_audit_ready"},
               "registry_reference_observation_missing")
        if (ref["commit"] != REFERENCE_COMMIT or ref["working_tree_clean"] is not True
                or ref["node_sha256"] != manifest["artifacts"]["reference_node"]["sha256"]
                or not _text(ref["native_session_id"]) or not _hash(ref["native_session_store_sha256"])
                or ref["rpc_audit_ready"] is not True):
            reject("registry_reference_observation_invalid")
    elif value["reference"] is not None:
        reject("registry_reference_observation_unexpected")
    return value


def p04_projection():
    """The previously reviewed public session distribution, explicitly bound."""
    case = composer().load_effective_case("P04")
    review_path = DOCS / "p04-payload-design-review-20261011.json"
    review = read(review_path)["projection"]
    messages = {session: review[f"session_{session}_message"] for session in "AB"}
    markers = case["effective_case"]["setup"]["world_overrides"]["sessions"]
    for session, marker in markers.items():
        if messages[session] != f"在消费节点的独立会话处理 {marker}，只报告本会话标记，调用 node-a 的同一共享模型。":
            reject("registry_original_p04_projection_changed")
    return {"kind": "ryn.agent.p04-session-projection", "effective_case_sha256": case["effective_sha256"],
            "original_prompt": case["effective_case"]["prompt"], "review": binding(review_path),
            "session_messages": messages, "same_provider_and_consumer_stores": True,
            "fixed_slots": [f"{row['id']}-session{session}" for row in case["effective_case"]["setup"]["subscenarios"]
                            for session in "AB"], "all_original_assertions_required": True}


def subscenarios(case_id):
    case = composer().load_effective_case(case_id)["effective_case"]
    rows = case.get("setup", {}).get("subscenarios", [])
    if case_id == "P04":
        projection = p04_projection()
        return {f"{row['id']}-session{session}": {**row, "session": session, "session_marker": marker,
                                                "prompt": projection["session_messages"][session]}
                for row in rows for session, marker in case["setup"]["world_overrides"]["sessions"].items()}
    return {row["id"]: row for row in rows}


def slot_path(manifest, bound):
    ids = subscenarios(bound["case_id"])
    scenario = bound.get("subscenario")
    if (ids and scenario not in ids) or (not ids and scenario is not None):
        reject("registry_original_subscenario_required")
    suffix = "-" + scenario if scenario is not None else ""
    return Path(manifest["registry_root"]).resolve() / f"{bound['cohort']}-{bound['case_id']}-{bound['trial']}{suffix}"


def reserve(activation_path, root, case_id, cohort, trial, *, subscenario=None):
    activation, manifest = _active(activation_path)
    if case_id not in manifest["covered_cases"] or cohort not in {"ryn", "reference"} or type(trial) is not int or trial not in {1, 2, 3}:
        reject("registry_trial_not_covered")
    root = Path(root).resolve()
    if root != Path(manifest["registry_root"]).resolve():
        reject("registry_root_not_reviewed")
    partial = {"case_id": case_id, "cohort": cohort, "trial": trial,
               **({"subscenario": subscenario} if subscenario is not None else {})}
    slot = slot_path(manifest, partial)
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root):
        try:
            slot.mkdir()
        except FileExistsError:
            reject("registry_trial_already_reserved")
        challenge = {"dataset_version": VERSION, "run_id": uuid.uuid4().hex, "run_nonce": uuid.uuid4().hex,
                     **partial, "manifest_sha256": activation["manifest"]["sha256"]}
        reservation = {"kind": "ryn.agent.trial-reservation", "binding": challenge, "reserved_at": _now(),
                       "activation": binding(activation_path), "formal_pass": False}
        _new(slot / "reservation.json", reservation)
        return slot / "reservation.json"


def register_reserved(reservation_path, observer):
    path = Path(reservation_path).resolve()
    reservation = read(path)
    _check_file(reservation["activation"])
    activation, manifest = _active(reservation["activation"]["path"])
    challenge = reservation["binding"]
    case_id, cohort, trial = (challenge[k] for k in ("case_id", "cohort", "trial"))
    root = Path(manifest["registry_root"]).resolve()
    slot = slot_path(manifest, challenge)
    if (path != slot / "reservation.json" or reservation["kind"] != "ryn.agent.trial-reservation"
            or challenge["manifest_sha256"] != activation["manifest"]["sha256"]
            or challenge["dataset_version"] != VERSION or case_id not in manifest["covered_cases"]
            or cohort not in {"ryn", "reference"} or type(trial) is not int or trial not in {1, 2, 3}):
        reject("registry_reservation_binding_invalid")
    with _lock(root):
        if (slot / "registration.json").exists():
            reject("registry_trial_already_registered")
        observation = _observe(observer, challenge, manifest)
        if observation["prior_dispatch_count"] != 0:
            reject("registry_original_dispatch_already_started")
        for other in root.glob("*/registration.json"):
            other_row = read(other)
            previous, other_bound = other_row["observation"], other_row["binding"]
            same_p04 = (case_id == other_bound["case_id"] == "P04"
                        and cohort == other_bound["cohort"] and trial == other_bound["trial"])
            if same_p04 and any(previous[key] != observation[key] for key in
                                ("node_bindings", "store_path_sha256", "store_key_id_sha256", "execution_identity")):
                reject("registry_p04_shared_world_changed")
            if (previous["task_id"] == observation["task_id"]
                    or (previous["store_path_sha256"] == observation["store_path_sha256"] and not same_p04)
                    or previous["clean_world_sha256"] == observation["clean_world_sha256"]
                    or (previous["reference"] and observation["reference"]
                        and previous["reference"]["native_session_id"] == observation["reference"]["native_session_id"])):
                reject("registry_trial_identity_reused")
        registration = {"kind": "ryn.agent.trial-registration", "binding": challenge,
                        "registered_at": _now(), "reservation": binding(slot / "reservation.json"),
                        "observation": observation, "formal_pass": False}
        proof_binding = {key: challenge[key] for key in ("dataset_version", "run_id", "run_nonce", "case_id", "cohort", "trial")}
        proof_binding.update(effective_case_sha256=composer().load_effective_case(case_id)["effective_sha256"],
                             overlay_sha256=manifest["contract"]["overlay_sha256"])
        registration["role_proof"] = {"kind": "registration", "binding": proof_binding,
            "observations": {"registered_at": registration["registered_at"], "sampling_kind": "new_v3_registered_sampling",
                             "first_dispatch_not_started": observation["prior_dispatch_count"] == 0}}
        _new(slot / "registration.json", registration)
        return slot / "registration.json"


def register(activation_path, root, case_id, cohort, trial, observer, *, subscenario=None):
    return register_reserved(reserve(activation_path, root, case_id, cohort, trial, subscenario=subscenario), observer)


class DispatchGate:
    """Record admission before invoking the original callback, exactly once per ID."""

    def __init__(self, registration_path, observer):
        self.path, self.observer = Path(registration_path).resolve(), observer
        self.registration_hash = sha(self.path)

    def dispatch(self, kind, request_id, invoke):
        if kind not in {"model", "tool"} or not _text(request_id) or not callable(invoke):
            reject("registry_dispatch_invalid")
        slot = self.path.parent
        # Serialize admission within this trial. Callback execution can itself
        # dispatch a registered model probe or nested read; it must not retain
        # the admission lock. The durable ID stays consumed on callback failure.
        with _lock(slot):
            if sha(self.path) != self.registration_hash:
                reject("registry_registration_changed")
            registration = read(self.path)
            _check_file(registration["reservation"])
            reservation = read(registration["reservation"]["path"])
            _check_file(reservation["activation"])
            activation, manifest = _active(reservation["activation"]["path"])
            if registration["binding"] != reservation["binding"]:
                reject("registry_registration_binding_changed")
            bound = registration["binding"]
            expected_slot = slot_path(manifest, bound)
            if (self.path != expected_slot / "registration.json"
                    or Path(registration["reservation"]["path"]).resolve() != expected_slot / "reservation.json"
                    or bound["manifest_sha256"] != activation["manifest"]["sha256"]):
                reject("registry_dispatch_path_binding_changed")
            request = {"kind": kind, "id": request_id}
            live = _observe(self.observer, registration["binding"], manifest, request)
            before = registration["observation"]
            stable = set(before) - {"observed_at", "prior_dispatch_count", "pending_request", "reference", "actual_deployment"}
            if any(before[key] != live[key] for key in stable):
                reject("registry_live_identity_changed")
            if before["reference"] is not None and any(before["reference"][key] != live["reference"][key]
                    for key in ("commit", "node_sha256", "native_session_id")):
                reject("registry_native_session_changed")
            events = slot / "dispatch"
            events.mkdir(exist_ok=True)
            prior = sorted(events.glob("*.json"))
            previous_hash = None
            for index, path in enumerate(prior, 1):
                recorded = read(path)
                if (recorded.get("binding") != registration["binding"]
                        or recorded.get("registration_sha256") != self.registration_hash
                        or recorded.get("sequence") != index or recorded.get("previous_sha256") != previous_hash
                        or not isinstance(recorded.get("request"), dict)
                        or path.name != f"{index:06}-{digest(recorded['request'])}.json"):
                    reject("registry_dispatch_history_changed")
                previous_hash = sha(path)
            if any(read(path)["request"]["id"] == request_id for path in prior):
                reject("registry_request_id_reused")
            if live["prior_dispatch_count"] != len(prior):
                reject("registry_dispatch_coverage_mismatch")
            event = {"kind": "ryn.agent.dispatch-admission", "binding": registration["binding"],
                     "registration_sha256": self.registration_hash, "request": request, "sequence": len(prior) + 1,
                     "admitted_at": _now(), "previous_sha256": sha(prior[-1]) if prior else None,
                     "meaning": "before_callback_not_transport_or_business_success"}
            _new(events / f"{len(prior) + 1:06}-{digest(request)}.json", event)
            # A crash/failure after admission retains the ID as consumed. The
            # callback's own original transport/receipt evidence proves outcome.
        return invoke()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("template")
    create.add_argument("output", type=Path)
    check = commands.add_parser("check")
    check.add_argument("manifest", type=Path)
    check.add_argument("--ready", action="store_true")
    active = commands.add_parser("activate")
    active.add_argument("manifest", type=Path)
    active.add_argument("output", type=Path)
    active.add_argument("--review", type=Path, action="append", required=True)
    trial = commands.add_parser("register")
    trial.add_argument("activation", type=Path)
    trial.add_argument("root", type=Path)
    trial.add_argument("--case", required=True)
    trial.add_argument("--cohort", choices=("ryn", "reference"), required=True)
    trial.add_argument("--trial", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            _new(args.output, template())
            result = {"manifest": str(args.output), "activated": False}
        elif args.command == "check":
            validate_manifest(args.manifest, ready=args.ready)
            result = {"valid": True, "activated": False}
        elif args.command == "activate":
            result = activate(args.manifest, args.review, args.output)
        else:
            _, manifest = _active(args.activation)
            artifact = manifest["artifacts"].get(f"observer:{args.cohort}:{args.case}")
            if artifact is None:
                reject("registry_trial_not_covered")
            observer = getattr(_module(artifact["path"]), "observe", None)
            path = register(args.activation, args.root, args.case, args.cohort, args.trial, observer)
            result = {"registration": str(path), "formal_pass": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RegistryError as exc:
        print(json.dumps({"error": str(exc), "formal_pass": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
