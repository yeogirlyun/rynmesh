"""Draft contract composition only: no controllers, model calls or held-out IO."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/agent_acceptance"
DOCS = ROOT / "docs/acceptance/ryn-agent-delivery"


def import_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = import_path("draft_role_contract_v2", FIXTURES / "role_contract_v2.py")
ADMIN = {"Q01", "Q06", "C02", "C03", "C04", "R02", "R04", "P03"}
CASES_PATH = DOCS / "cases-v1.json"
WORLD_PATH = FIXTURES / "world.json"
OVERLAY_PATH = DOCS / "role-overlay-v2-draft.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_overlay(tmp_path, value):
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def base_copy(tmp_path):
    for relative in (contract.BASE_INPUTS["cases"]["path"], contract.BASE_INPUTS["world"]["path"]):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    return tmp_path


def test_all_cases_only_eight_authorization_deltas_and_no_input_mutation():
    before = {path: path.read_bytes() for path in (CASES_PATH, WORLD_PATH, OVERLAY_PATH)}
    suite, world = read(CASES_PATH), read(WORLD_PATH)
    seen, changes = set(), set()
    for original in suite["cases"]:
        case_id = original["id"]
        result = contract.load_effective_case(case_id)
        seen.add(case_id)
        effective = result["effective_case"]
        expected = copy.deepcopy(original)
        if case_id in ADMIN:
            expected["authorization"]["consumer_node"] = "node-a"
            changes.add(case_id)
        assert effective == expected  # Includes prompts, grants, faults and every assertion.
        assert result["base_world"] == world
        assert result["base_suite_metadata"] == {k: v for k, v in suite.items() if k != "cases"}
        assert result["base_suite_metadata"]["minimum_business_passes"] == 58
        assert result["base_suite_metadata"]["business_trials"] == 72
        assert effective["repetitions"] == 3
        assert result["role_contract"]["agent_execution_node"] == effective["authorization"]["consumer_node"]
        assert result["identity_binding"] == {"status": "unbound", "independent_identity_proof": False}
        assert result["enabled"] is False and result["formal_ready"] is False
        assert result["status"] == "draft/not_formal_ready"
    assert seen == set(contract.CASE_IDS) and len(seen) == 30
    assert changes == ADMIN
    assert all(path.read_bytes() == data for path, data in before.items())


def test_deterministic_hash_and_defensive_copy():
    first = contract.load_effective_case("Q06")
    second = contract.load_effective_case("Q06")
    assert first == second
    payload = {key: value for key, value in first.items() if key != "effective_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert first["effective_sha256"] == hashlib.sha256(canonical).hexdigest()
    first["effective_case"]["prompt"] = "injected"
    first["role_contract"]["model_recipient"]["model-a"].clear()
    first["base_world"]["models"]["model-b"]["sharing"] = True
    assert contract.load_effective_case("Q06") == second
    assert contract.load_effective_case("Q01")["effective_sha256"] != second["effective_sha256"]


def test_q02_is_only_source_ownership_exception():
    for case_id in contract.CASE_IDS:
        role = contract.load_effective_case(case_id)["role_contract"]
        assert role["source_owners"]["source-alpha"] == "node-a"
        assert role["source_owners"]["source-other"] == "node-a"
        expected = "node-b" if case_id == "Q02" else "node-a"
        assert role["source_owners"]["source-broken"] == expected
        assert role["model_provider"]["model-d"] == expected
        assert role["source_owner_override"] == ({"source-broken": "node-b"} if case_id == "Q02" else {})
        assert role["new_source_owners"] == ({"source-new": "node-b"} if case_id == "C01" else {})


def test_c03_default_is_local_a_and_private_override_does_not_clear_grants():
    result = contract.load_effective_case("C03")
    role = result["role_contract"]
    assert role["agent_execution_node"] == role["default_agent_owner"] == "node-a"
    assert set(role["initial_model_sharing"].values()) == {False}
    assert role["model_recipient"]["model-a"] == ["node-b"]
    assert role["model_recipient"]["model-b"] == []
    assert result["base_world"]["models"]["model-a"]["sharing"] is True
    assert result["effective_case"]["setup"]["world_overrides"] == {"all_models_private": True, "agent_enabled": True}


def test_c04_preexisting_grant_and_all_approval_barriers_preserved():
    original = next(c for c in read(CASES_PATH)["cases"] if c["id"] == "C04")
    result = contract.load_effective_case("C04")
    role = result["role_contract"]
    assert role["initial_model_sharing"]["model-a"] is True
    assert role["model_recipient"]["model-a"] == ["node-b"]
    assert role["model_recipient"]["model-b"] == []
    assert result["effective_case"]["interventions"] == original["interventions"]
    assert original["interventions"][0]["point"] == "pending model-a/node-b grant"
    assert result["effective_case"]["setup"]["world_overrides"] == {}


@pytest.mark.parametrize("case_id,expected", [("Q04", "node-b"), ("R02", "node-a"), ("R03", "node-b"), ("R05", "node-b"), ("R06", "node-b")])
def test_task_and_connector_ownership(case_id, expected):
    role = contract.load_effective_case(case_id)["role_contract"]
    assert role["agent_execution_node"] == expected
    assert role["history_owners"] == {"task-failed-read": "node-b"}
    assert set(role["connection_hosts"].values()) == {"node-b"}
    assert role["primary_model"]["provider_resolution"] == "pending_actual_dispatch_binding"


@pytest.mark.parametrize("key", ["cases", "world"])
def test_changed_base_bytes_rejected_even_if_overlay_digest_is_updated(base_copy, tmp_path, key):
    binding = contract.BASE_INPUTS[key]
    path = base_copy / binding["path"]
    data = read(path)
    if key == "cases":
        data["cases"][0]["prompt"] = "unapproved prompt change"
    else:
        data["models"]["model-b"]["receivers"] = ["node-c"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(contract.RoleContractError, match="role_base_hash_mismatch"):
        contract.load_effective_case("Q01", base_root=base_copy)
    overlay = read(OVERLAY_PATH)
    overlay["base"][key]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    forged = save_overlay(tmp_path, overlay)
    with pytest.raises(contract.RoleContractError, match="role_base_binding_changed"):
        contract.load_effective_case("Q01", base_root=base_copy, overlay_path=forged)


@pytest.mark.parametrize("key", ["cases", "world"])
def test_wrong_declared_base_hash_rejected(tmp_path, key):
    overlay = read(OVERLAY_PATH)
    overlay["base"][key]["sha256"] = "0" * 64
    with pytest.raises(contract.RoleContractError, match="role_base_binding_changed"):
        contract.load_effective_case("Q01", overlay_path=save_overlay(tmp_path, overlay))


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(extra=True),
    lambda x: x["base"]["cases"].update(extra=True),
    lambda x: x["cases"]["Q01"].update(config_actions=["arbitrary"]),
    lambda x: x["cases"].update(H01={"execution_role": "consumer", "agent_execution_node": "node-b"}),
    lambda x: x["cases"].pop("F08"),
    lambda x: x["authorization_changes"].update(Q02={"from": "node-b", "to": "node-a"}),
    lambda x: x["authorization_changes"]["Q01"].update(to="node-c"),
    lambda x: x["deployment"]["source_owner_overrides"].update(Q03={"source-broken": "node-b"}),
    lambda x: x["deployment"]["source_owner_overrides"]["Q02"].update({"source-other": "node-b"}),
    lambda x: x["deployment"]["source_owners"].update({"source-alpha": "node-b"}),
    lambda x: x["deployment"]["history_owners"].update({"task-failed-read": "node-a"}),
    lambda x: x["deployment"]["connection_hosts"].update({"docs-alpha": "node-a"}),
    lambda x: x["deployment"]["new_source_owners"]["C01"].update({"source-new": "node-a"}),
    lambda x: x["deployment"].update(admin_credentials="SYNTHETIC_PRIVATE_VALUE"),
    lambda x: x.update(enabled=True),
    lambda x: x.update(formal_ready=True),
    lambda x: x.update(status="active"),
    lambda x: x.update(partition="held_out"),
    lambda x: x.update(dataset_version=contract.BASE_VERSION),
    lambda x: x["base"]["world"].update(path="../../outside.json"),
])
def test_closed_overlay_rejects_unknown_or_unauthorized_changes(tmp_path, mutation):
    overlay = read(OVERLAY_PATH)
    mutation(overlay)
    with pytest.raises(contract.RoleContractError) as error:
        contract.load_effective_case("Q01", overlay_path=save_overlay(tmp_path, overlay))
    assert error.value.code == "role_overlay_schema_invalid"
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(error.value)


@pytest.mark.parametrize("case_id,node,role", [("Q02", "node-a", "owner-admin"), ("Q01", "node-b", "consumer"), ("C01", "node-b", "owner-admin")])
def test_schema_valid_but_extra_role_changes_rejected(tmp_path, case_id, node, role):
    overlay = read(OVERLAY_PATH)
    overlay["cases"][case_id] = {"execution_role": role, "agent_execution_node": node}
    with pytest.raises(contract.RoleContractError, match="role_execution_change_not_allowed"):
        contract.load_effective_case("Q01", overlay_path=save_overlay(tmp_path, overlay))


@pytest.mark.parametrize("bad", ["H01", "Q07", "Q1", "", None, 1, []])
def test_only_exact_development_ids_accepted(bad):
    with pytest.raises(contract.RoleContractError, match="role_case_not_development"):
        contract.load_effective_case(bad)


@pytest.mark.parametrize("raw,code", [
    ('{"schema_version":1,"schema_version":1}', "role_json_duplicate_key"),
    ('{"value":NaN}', "role_json_nonfinite"),
    ('{"value":Infinity}', "role_json_nonfinite"),
    ('{', "role_json_invalid"),
    (' ' * (64 * 1024 + 1), "role_input_too_large"),
], ids=["duplicate", "nan", "infinity", "malformed", "oversize"])
def test_ambiguous_or_unbounded_overlay_rejected(tmp_path, raw, code):
    path = tmp_path / "overlay.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(contract.RoleContractError, match=code):
        contract.load_effective_case("Q01", overlay_path=path)


def identities(node="node-a"):
    mapping = {logical: "ed25519-public-identity-" + logical[-1] for logical in contract.LOGICAL_NODES}
    observation = {"runtime_consumer_node_id": mapping[node], "agent_store_consumer_node_id": mapping[node],
                   "owner_id": "owner-test"}
    return mapping, observation


@pytest.mark.parametrize("case_id,node", [("Q06", "node-a"), ("Q02", "node-b"), ("F02", "node-b")])
def test_logical_nodes_are_explicitly_mapped_not_compared_to_actual_strings(case_id, node):
    mapping, observation = identities(node)
    result = contract.load_effective_case(case_id, node_bindings=mapping, execution_identity=observation)
    bound = result["identity_binding"]
    assert bound["agent_execution_node_id"] == mapping[node] != node
    assert bound["status"] == "declared_bindings_consistent"
    assert bound["independent_identity_proof"] is False and bound["physical_host_proof"] is False
    assert bound["model_provider_ids"]["model-a"] == mapping["node-a"]
    assert bound["model_recipient_ids"]["model-a"] == [mapping["node-b"]]
    assert result["enabled"] is False and result["formal_ready"] is False
    assert result["effective_sha256"] != contract.load_effective_case(case_id)["effective_sha256"]
    mapping["node-a"] = "changed-after-load"
    assert bound["node_bindings"]["node-a"] != "changed-after-load"


@pytest.mark.parametrize("mutation,code", [
    (lambda m, o: m.update({"node-b": m["node-a"]}), "role_node_bindings_invalid"),
    (lambda m, o: m.pop("node-d"), "role_node_bindings_invalid"),
    (lambda m, o: m.update({"node-e": "extra"}), "role_node_bindings_invalid"),
    (lambda m, o: m.update({"node-c": ""}), "role_node_bindings_invalid"),
    (lambda m, o: o.update(runtime_consumer_node_id="node-a"), "role_execution_identity_mismatch"),
    (lambda m, o: o.update(runtime_consumer_node_id=m["node-b"]), "role_execution_identity_mismatch"),
    (lambda m, o: o.update(agent_store_consumer_node_id=m["node-b"]), "role_execution_identity_mismatch"),
    (lambda m, o: o.update(owner_id="other-owner"), "role_execution_identity_mismatch"),
    (lambda m, o: o.update(physical_proof=True), "role_execution_identity_invalid"),
])
def test_mismatched_or_ambiguous_identity_binding_rejected(mutation, code):
    mapping, observation = identities()
    mutation(mapping, observation)
    with pytest.raises(contract.RoleContractError, match=code):
        contract.load_effective_case("Q06", node_bindings=mapping, execution_identity=observation)


def test_half_binding_is_not_silently_accepted():
    mapping, observation = identities()
    with pytest.raises(contract.RoleContractError, match="role_execution_identity_invalid"):
        contract.load_effective_case("Q01", node_bindings=mapping)
    with pytest.raises(contract.RoleContractError, match="role_node_bindings_invalid"):
        contract.load_effective_case("Q01", execution_identity=observation)


def test_loader_reads_only_three_inputs_plus_its_schema(monkeypatch):
    allowed = {CASES_PATH.resolve(), WORLD_PATH.resolve(), OVERLAY_PATH.resolve(), contract.SCHEMA_PATH.resolve()}
    actual_open, observed = Path.open, set()

    def guarded(path, *args, **kwargs):
        resolved = path.resolve()
        assert resolved in allowed, f"unexpected input path: {resolved}"
        observed.add(resolved)
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    contract.load_effective_case("Q06")
    assert observed == allowed


def test_current_v1_grader_cannot_accept_draft_envelope_or_version(tmp_path, monkeypatch):
    grader = import_path("current_v1_grader_role_test", FIXTURES / "validate.py")
    suite_original = grader.suite

    def development_only(partition="development"):
        assert partition == "development"
        return suite_original(partition)

    monkeypatch.setattr(grader, "suite", development_only)
    envelope = contract.load_effective_case("Q06")
    rejected = grader.grade(envelope, tmp_path)
    assert rejected["status"] == "failed"
    assert any("run schema" in item for item in rejected["failures"])
    # Even a structurally valid v1-shaped run cannot bind the draft version.
    run = grader.template("Q06", partition="development")
    run["dataset_version"] = envelope["dataset_version"]
    run["suite_sha256"] = envelope["effective_sha256"]
    rejected = grader.grade(run, tmp_path)
    assert rejected["status"] == "failed"
    assert "run does not bind frozen suite" in rejected["failures"]
