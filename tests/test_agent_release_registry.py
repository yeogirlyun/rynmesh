"""Synthetic offline registry machinery; never formal trials or business passes."""
import copy
import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tests/fixtures/agent_acceptance/release_registry.py"
spec = importlib.util.spec_from_file_location("acceptance_release_registry", MODULE)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def save(path, value):
    path.write_bytes(registry.canonical(value))
    return path


@pytest.fixture
def prepared(tmp_path):
    """Fabricated reviewed release: scope is API/order/drift checks only."""
    implementation = tmp_path / "synthetic-implementation.py"
    implementation.write_text("# Synthetic pipeline placeholder, never a business grader.\n", encoding="utf-8")
    node = tmp_path / "synthetic-node.exe"
    node.write_bytes(b"not a real Node binary")
    observer_file = tmp_path / "synthetic_observer.py"
    observer_file.write_text(
        "import copy\nfrom datetime import datetime, timezone\n"
        "STATE = None\nCALLS = []\n"
        "def observe(challenge, request):\n"
        "    CALLS.append((copy.deepcopy(challenge), copy.deepcopy(request)))\n"
        "    value = copy.deepcopy(STATE)\n"
        "    value['binding'] = challenge\n"
        "    value['observed_at'] = datetime.now(timezone.utc).isoformat()\n"
        "    return value\n", encoding="utf-8")
    observer = registry._module(observer_file)
    manifest = registry.template()
    manifest.update(authors=["synthetic-author"], covered_cases=["Q01"], registry_root=str(tmp_path / "trials"))
    artifacts = manifest["artifacts"]
    for name in ("candidate_snapshot", "reference_snapshot", "dependency_snapshot"):
        artifacts[name] = registry.binding(save(tmp_path / (name + ".json"), {"files": [registry.binding(implementation)]}))
    policy = {"model": "glm-5.3-flash", "phases": {name: {"model": "glm-5.3-flash", "request_defaults": {},
              "max_context_bytes": 12000, "max_output_tokens": 512} for name in registry.PHASES},
              "case_limits": {"Q01": {"max_model_calls": 24, "max_tool_calls": 256, "max_tokens": 2000000,
                                      "max_seconds": 600, "max_input_chars": 20000,
                                      "round_limits": [{"max_steps": 24, "max_seconds": 600, "max_context_chars": 20000, "max_output_chars": 20000}],
                                      "lifetime_limits": [{"max_total_model_calls": 24, "max_total_tool_calls": 256, "max_total_tokens": 2000000, "max_total_seconds": 600}]}},
              "owner_wait_policy": "synthetic-common-policy"}
    artifacts["model_policy"] = registry.binding(save(tmp_path / "model-policy.json", policy))
    for name in ("grader", "normalizer", "run_schema", "blind_interface"):
        capabilities = ["fresh_registration_required", "original_evidence_required", "no_legacy_backfill"]
        if name == "blind_interface":
            capabilities += ["isolated_evaluator", "contents_not_exported", "version_bound_results"]
        interface = {"kind": name, "dataset_version": registry.VERSION,
                     "implementation": registry.binding(implementation), "capabilities": capabilities}
        artifacts[name] = registry.binding(save(tmp_path / (name + ".json"), interface))
    artifacts["reference_node"] = registry.binding(node)
    for cohort in ("ryn", "reference"):
        artifacts[f"controller:{cohort}:Q01"] = registry.binding(implementation)
        artifacts[f"observer:{cohort}:Q01"] = registry.binding(observer_file)
    manifest_path = save(tmp_path / "manifest.json", manifest)
    review = {"kind": "ryn.agent.release-review", "manifest_sha256": registry.sha(manifest_path),
              "reviewer": "synthetic-independent-reviewer", "independent_of": manifest["authors"],
              "areas": sorted(registry.REVIEW_AREAS), "verdict": "passed"}
    review_path = save(tmp_path / "review.json", review)
    nodes = {"node-" + letter: "actual-" + letter for letter in "abcd"}
    execution = {"owner_id": "owner-test", "runtime_consumer_node_id": "actual-a", "agent_store_consumer_node_id": "actual-a"}
    bound = registry.composer().load_effective_case("Q01", node_bindings=nodes, execution_identity=execution)["identity_binding"]
    deployment = {key: bound[key] for key in ("source_owner_ids", "new_source_owner_ids", "connection_host_ids",
                  "history_owner_ids", "model_provider_ids", "model_recipient_ids", "default_agent_owner_id")}
    observer.STATE = {"task_id": "synthetic-task", "node_bindings": nodes, "execution_identity": execution,
                      "deployment": deployment, "store_path_sha256": "1" * 64, "store_key_id_sha256": "2" * 64,
                      "actual_deployment": {"source_owners": {k: {"state": "not_instantiated"} for k in deployment["source_owner_ids"]},
                          "connections": {k: {"state": "not_instantiated"} for k in deployment["connection_host_ids"]},
                          "histories": {k: {"state": "not_instantiated"} for k in deployment["history_owner_ids"]}, "connection_accesses": []},
                      "decrypted_task_id": "synthetic-task", "source_snapshot_sha256": artifacts["candidate_snapshot"]["sha256"],
                      "model_policy_sha256": artifacts["model_policy"]["sha256"], "clean_world_sha256": "3" * 64,
                      "prior_dispatch_count": 0, "pending_request": None, "reference": None}
    return {"root": tmp_path, "manifest": manifest, "manifest_path": manifest_path, "review_path": review_path,
            "observer": observer, "implementation": implementation, "activation": tmp_path / "activation.json"}


def activate(prepared):
    p = prepared
    return registry.activate(p["manifest_path"], [p["review_path"]], p["activation"])


def register(prepared, cohort="ryn", trial=1):
    p = prepared
    return registry.register(p["activation"], p["root"] / "trials", "Q01", cohort, trial, p["observer"].observe)


def pending(p, kind="model", request_id="native-rpc-1"):
    p["observer"].STATE["pending_request"] = {"kind": kind, "id": request_id}


def test_template_and_cli_never_activate_or_register_by_default(tmp_path, capsys):
    path = tmp_path / "candidate.json"
    assert registry.main(["template", str(path)]) == 0
    manifest = registry.read(path)
    assert manifest["status"] == "candidate_not_activated" and not manifest["covered_cases"]
    assert manifest["contract"]["gates"] == registry.GATES
    assert len(manifest["contract"]["case_hashes"]) == 30
    assert registry.main(["check", str(path)]) == 0
    assert registry.main(["check", str(path), "--ready"]) == 2
    assert "registry_release_inputs_incomplete" in capsys.readouterr().out
    assert not list(tmp_path.glob("**/registration.json"))


@pytest.mark.parametrize("change,code", [
    ("version", "registry_manifest_schema_invalid"), ("threshold", "registry_manifest_schema_invalid"),
    ("case", "registry_original_contract_changed"), ("missing_reference", "registry_release_inputs_incomplete"),
    ("draft_grader", "registry_pipeline_version_unsupported"), ("missing_blind", "registry_release_inputs_incomplete"),
])
def test_activation_rejects_contract_changes_and_incomplete_pipeline(prepared, change, code):
    p, value = prepared, copy.deepcopy(prepared["manifest"])
    if change == "version":
        value["dataset_version"] = "ryn-agent-acceptance-2026-10-10.v2-draft.1"
    elif change == "threshold":
        value["contract"]["gates"]["minimum_business_passes"] = 57
    elif change == "case":
        value["contract"]["case_hashes"]["Q04"] = "f" * 64
    elif change == "missing_reference":
        del value["artifacts"]["controller:reference:Q01"]
    elif change == "missing_blind":
        del value["artifacts"]["blind_interface"]
    else:
        path = Path(value["artifacts"]["grader"]["path"])
        interface = registry.read(path)
        interface["dataset_version"] = "ryn-agent-acceptance-2026-10-10.v2-draft.1"
        value["artifacts"]["grader"] = registry.binding(save(path, interface))
    save(p["manifest_path"], value)
    with pytest.raises(registry.RegistryError, match=code):
        activate(p)
    assert not p["activation"].exists()


@pytest.mark.parametrize("change", ["author", "unreviewed_manifest", "missing_area", "failed"])
def test_activation_requires_complete_independent_exact_review(prepared, change):
    review = registry.read(prepared["review_path"])
    if change == "author":
        review["reviewer"] = "synthetic-author"
    elif change == "unreviewed_manifest":
        review["manifest_sha256"] = "0" * 64
    elif change == "missing_area":
        review["areas"].remove("registration")
    else:
        review["verdict"] = "failed"
    save(prepared["review_path"], review)
    with pytest.raises(registry.RegistryError, match="registry_review"):
        activate(prepared)


def test_fresh_nonce_and_admission_precede_original_callback_and_allow_later_calls(prepared):
    p = prepared
    activate(p)
    path = register(p)
    row = registry.read(path)
    assert len(row["binding"]["run_nonce"]) == 32 and row["formal_pass"] is False
    assert row["binding"]["run_id"] != row["binding"]["run_nonce"]
    assert p["observer"].CALLS[0] == (row["binding"], None)
    gate = registry.DispatchGate(path, p["observer"].observe)
    for sequence, kind in enumerate(("model", "tool", "model"), 1):
        request_id = f"original-{sequence}"
        pending(p, kind, request_id)
        def invoke(sequence=sequence, kind=kind, request_id=request_id):
            event = registry.read(next((path.parent / "dispatch").glob(f"{sequence:06}-*.json")))
            assert event["request"] == {"kind": kind, "id": request_id}
            assert event["registration_sha256"] == registry.sha(path)
            assert event["meaning"] == "before_callback_not_transport_or_business_success"
            p["observer"].STATE["prior_dispatch_count"] += 1
            return {"original-result": sequence}
        assert gate.dispatch(kind, request_id, invoke) == {"original-result": sequence}
    pending(p, "model", "original-1")
    with pytest.raises(registry.RegistryError, match="registry_request_id_reused"):
        registry.DispatchGate(path, p["observer"].observe).dispatch("model", "original-1", lambda: pytest.fail("replayed"))


def test_callback_failure_consumes_request_id_and_never_claims_execution_success(prepared):
    activate(prepared)
    path = register(prepared)
    gate = registry.DispatchGate(path, prepared["observer"].observe)
    pending(prepared)
    with pytest.raises(RuntimeError, match="synthetic transport failure"):
        gate.dispatch("model", "native-rpc-1", lambda: (_ for _ in ()).throw(RuntimeError("synthetic transport failure")))
    event = registry.read(next((path.parent / "dispatch").glob("000001-*.json")))
    assert "success" not in event and "result" not in event
    with pytest.raises(registry.RegistryError, match="registry_request_id_reused"):
        gate.dispatch("model", "native-rpc-1", lambda: pytest.fail("replayed"))


@pytest.mark.parametrize("change,code", [
    ("already_started", "registry_original_dispatch_already_started"),
    ("wrong_store", "role_execution_identity_mismatch"),
    ("wrong_owner", "registry_deployment_mismatch"),
    ("unknown_field", "registry_observation_invalid"),
])
def test_registration_rejects_retrospective_or_wrong_identity_observations(prepared, change, code):
    activate(prepared)
    state = prepared["observer"].STATE
    if change == "already_started":
        state["prior_dispatch_count"] = 1
    elif change == "wrong_store":
        state["execution_identity"]["agent_store_consumer_node_id"] = "actual-b"
    elif change == "wrong_owner":
        state["deployment"]["source_owner_ids"]["source-alpha"] = "actual-b"
    else:
        state["raw_credentials"] = "never persist"
    with pytest.raises(ValueError, match=code):
        register(prepared)
    assert not list((prepared["root"] / "trials").glob("*/registration.json"))
    assert "never persist" not in next((prepared["root"] / "trials").glob("*/reservation.json")).read_text()


@pytest.mark.parametrize("change,code", [
    ("code", "registry_artifact_changed"), ("identity", "registry_live_identity_changed"),
    ("request", "registry_observation_binding_invalid"), ("registration", "registry_registration_changed"),
    ("unobserved_dispatch", "registry_dispatch_coverage_mismatch"),
])
def test_dispatch_rechecks_live_observation_and_frozen_bytes(prepared, change, code):
    p = prepared
    activate(p)
    path = register(p)
    gate = registry.DispatchGate(path, p["observer"].observe)
    pending(p)
    if change == "code":
        p["implementation"].write_text("changed after registration", encoding="utf-8")
    elif change == "identity":
        p["observer"].STATE["store_path_sha256"] = "9" * 64
    elif change == "request":
        pending(p, request_id="other-original-id")
    elif change == "registration":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        p["observer"].STATE["prior_dispatch_count"] = 1
    with pytest.raises(registry.RegistryError, match=code):
        gate.dispatch("model", "native-rpc-1", lambda: pytest.fail("must not dispatch"))
    assert not list(path.parent.glob("dispatch/*.json"))


def test_slot_and_task_history_cannot_be_reused_as_a_new_trial(prepared):
    activate(prepared)
    register(prepared)
    with pytest.raises(registry.RegistryError, match="registry_trial_already_reserved"):
        register(prepared)
    with pytest.raises(registry.RegistryError, match="registry_trial_identity_reused"):
        register(prepared, trial=2)


def test_reference_needs_actual_native_session_node_and_rpc_observer(prepared):
    p = prepared
    activate(p)
    with pytest.raises(registry.RegistryError, match="registry_reference_observation_missing"):
        register(p, "reference")
    p["observer"].STATE["reference"] = {
        "commit": registry.REFERENCE_COMMIT, "working_tree_clean": True,
        "node_sha256": p["manifest"]["artifacts"]["reference_node"]["sha256"],
        "native_session_id": "synthetic-native-session", "native_session_store_sha256": "a" * 64, "rpc_audit_ready": True}
    path = register(p, "reference", trial=2)
    gate = registry.DispatchGate(path, p["observer"].observe)
    pending(p)
    p["observer"].STATE["reference"]["native_session_id"] = "switched-session"
    with pytest.raises(registry.RegistryError, match="registry_native_session_changed"):
        gate.dispatch("model", "native-rpc-1", lambda: pytest.fail("must not dispatch"))


def test_json_observation_or_unreviewed_callable_is_not_a_live_observer(prepared):
    activate(prepared)
    with pytest.raises(registry.RegistryError, match="registry_live_observer_required"):
        registry.register(prepared["activation"], prepared["root"] / "trials", "Q01", "ryn", 1, prepared["observer"].STATE)
    with pytest.raises(registry.RegistryError, match="registry_observer_not_reviewed"):
        registry.register(prepared["activation"], prepared["root"] / "trials", "Q01", "ryn", 2, lambda *_: {})


def test_new_root_cannot_silently_reregister_the_same_trial(prepared):
    activate(prepared)
    register(prepared)
    with pytest.raises(registry.RegistryError, match="registry_root_not_reviewed"):
        registry.register(prepared["activation"], prepared["root"] / "another-root", "Q01", "ryn", 1, prepared["observer"].observe)


def test_edited_admission_cannot_erase_a_consumed_request_identity(prepared):
    activate(prepared)
    path = register(prepared)
    gate = registry.DispatchGate(path, prepared["observer"].observe)
    pending(prepared)
    gate.dispatch("model", "native-rpc-1", lambda: None)
    event_path = next((path.parent / "dispatch").glob("*.json"))
    event = registry.read(event_path)
    event["request"]["id"] = "invented replacement"
    save(event_path, event)
    with pytest.raises(registry.RegistryError, match="registry_dispatch_history_changed"):
        gate.dispatch("model", "native-rpc-1", lambda: pytest.fail("must not dispatch"))


@pytest.mark.parametrize("relative", ["heldout/private.json", "fixtures/build_cases.py"])
def test_isolated_evaluator_contents_are_never_opened_even_for_hashing(tmp_path, relative):
    # Paths deliberately do not exist; rejection must precede any filesystem read.
    for reader in (registry.sha, registry.read):
        with pytest.raises(registry.RegistryError, match="registry_isolated_evaluation_input_forbidden"):
            reader(tmp_path / relative)


@pytest.mark.parametrize("destination", ["unreviewed-root/ryn-Q01-1", "trials/ryn-Q01-2"])
def test_copied_registration_cannot_dispatch_in_another_root_or_slot(prepared, destination):
    activate(prepared)
    original = register(prepared)
    copied_slot = prepared["root"] / destination
    shutil.copytree(original.parent, copied_slot)
    copied = copied_slot / "registration.json"
    assert copied.read_bytes() == original.read_bytes()
    pending(prepared)
    gate = registry.DispatchGate(copied, prepared["observer"].observe)
    with pytest.raises(registry.RegistryError, match="registry_dispatch_path_binding_changed"):
        gate.dispatch("model", "native-rpc-1", lambda: pytest.fail("copied slot must not dispatch"))
    assert not list(copied_slot.glob("dispatch/*.json"))
    assert not list(original.parent.glob("dispatch/*.json"))


def test_original_subscenarios_get_distinct_fresh_slots_and_cannot_omit_required_slot(prepared):
    p = prepared
    manifest = copy.deepcopy(p["manifest"])
    manifest["covered_cases"].append("C02")
    for cohort in ("ryn", "reference"):
        for kind in ("observer", "controller"):
            manifest["artifacts"][f"{kind}:{cohort}:C02"] = manifest["artifacts"][f"{kind}:{cohort}:Q01"]
    model_path = Path(manifest["artifacts"]["model_policy"]["path"])
    policy = registry.read(model_path)
    policy["case_limits"]["C02"] = copy.deepcopy(policy["case_limits"]["Q01"])
    save(model_path, policy)
    manifest["artifacts"]["model_policy"] = registry.binding(model_path)
    save(p["manifest_path"], manifest)
    review = registry.read(p["review_path"])
    review["manifest_sha256"] = registry.sha(p["manifest_path"])
    save(p["review_path"], review)
    activate(p)
    with pytest.raises(ValueError, match="original_subscenario_required"):
        registry.reserve(p["activation"], p["root"] / "trials", "C02", "ryn", 1)
    paths = [registry.reserve(p["activation"], p["root"] / "trials", "C02", "ryn", 1, subscenario=name)
             for name in ("pre_save_failure", "post_save_conflict")]
    rows = [registry.read(path) for path in paths]
    assert len({row["binding"]["run_nonce"] for row in rows}) == 2
    assert len({row["binding"]["run_id"] for row in rows}) == 2
    assert {row["binding"]["trial"] for row in rows} == {1}
    assert all(not (path.parent / "registration.json").exists() for path in paths)
    with pytest.raises(ValueError, match="already_reserved"):
        registry.reserve(p["activation"], p["root"] / "trials", "C02", "ryn", 1, subscenario="pre_save_failure")
    with pytest.raises(ValueError, match="original_subscenario_required"):
        registry.reserve(p["activation"], p["root"] / "trials", "C02", "ryn", 1, subscenario="invented_success")
