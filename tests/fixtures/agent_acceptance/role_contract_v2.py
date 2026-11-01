"""Explicit, read-only composition of the development role-contract draft.

No controller imports this module, no run is produced, and no grader is supplied.
Only the pinned development cases/world are read; fixtures and other partitions
are never traversed. Identity observations are caller-provided consistency inputs,
not independent proof that a process or physical host actually has that identity.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[3]
DOCS = Path("docs/acceptance/ryn-agent-delivery")
DEFAULT_OVERLAY = REPO / DOCS / "role-overlay-v2-draft.json"
SCHEMA_PATH = REPO / DOCS / "role-overlay-v2.schema.json"
BASE_VERSION = "ryn-agent-acceptance-2026-10-09.v1"
DATASET_VERSION = "ryn-agent-acceptance-2026-10-10.v2-draft.1"
COMPOSITION_VERSION = "ryn.agent.role-composition.v1"
REQUIRED_GRADER = "ryn-agent-role-grader.v2"
ADMIN_CASES = frozenset(("Q01", "Q06", "C02", "C03", "C04", "R02", "R04", "P03"))
CASE_IDS = tuple(f"{group}{number:02}" for group, count in (("Q", 6), ("F", 8), ("C", 4), ("R", 6), ("P", 6))
                 for number in range(1, count + 1))
LOGICAL_NODES = ("node-a", "node-b", "node-c", "node-d")
BASE_INPUTS = {
    "cases": {"path": "docs/acceptance/ryn-agent-delivery/cases-v1.json",
              "sha256": "27a50f909a09a6972fc7b11df55f7798412ec415b8ef87ccadef8ddc85bd06b5"},
    "world": {"path": "tests/fixtures/agent_acceptance/world.json",
              "sha256": "fbeb1e3466870df1d65685ed11835a4cdf078817a3b922e912a83453be09ba82"},
}


class RoleContractError(ValueError):
    """Closed error codes; do not include raw inputs, prompts or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    """Canonical JSON digest, not a file-byte digest."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _read_bytes(path, limit):
    try:
        with Path(path).open("rb") as handle:
            value = handle.read(limit + 1)
    except OSError as exc:
        raise RoleContractError("role_input_unreadable") from exc
    if len(value) > limit:
        raise RoleContractError("role_input_too_large")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RoleContractError("role_json_duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise RoleContractError("role_json_nonfinite")


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RoleContractError("role_json_invalid") from exc


def _overlay(path):
    raw = _read_bytes(path, 64 * 1024)
    value = _parse(raw)
    schema_raw = _read_bytes(SCHEMA_PATH, 256 * 1024)
    schema = _parse(schema_raw)
    Draft202012Validator.check_schema(schema)
    if not Draft202012Validator(schema).is_valid(value):
        raise RoleContractError("role_overlay_schema_invalid")
    # The trusted loader pins inputs independently of the editable overlay.
    # Updating a digest alongside contaminated source bytes is not sufficient.
    if value["base"] != {"dataset_version": BASE_VERSION, **BASE_INPUTS}:
        raise RoleContractError("role_base_binding_changed")
    expected_roles = {
        case_id: {"execution_role": "owner-admin" if case_id in ADMIN_CASES else "consumer",
                  "agent_execution_node": "node-a" if case_id in ADMIN_CASES else "node-b"}
        for case_id in CASE_IDS
    }
    if value["cases"] != expected_roles:
        raise RoleContractError("role_execution_change_not_allowed")
    return value, hashlib.sha256(raw).hexdigest(), hashlib.sha256(schema_raw).hexdigest()


def _base_inputs(base_root):
    values = {}
    for name, binding in BASE_INPUTS.items():
        # Paths come from trusted constants, never from an overlay.
        raw = _read_bytes(Path(base_root) / binding["path"], 2 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
            raise RoleContractError("role_base_hash_mismatch")
        values[name] = _parse(raw)
    if (values["cases"]["dataset_version"] != BASE_VERSION
            or values["world"]["dataset_version"] != BASE_VERSION
            or values["cases"]["partition"] != "development"
            or tuple(case["id"] for case in values["cases"]["cases"]) != CASE_IDS):
        raise RoleContractError("role_base_contract_invalid")
    return values["cases"], values["world"]


def _identity(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _bind_identities(role, case, node_bindings, execution_identity):
    if node_bindings is None and execution_identity is None:
        return {"status": "unbound", "independent_identity_proof": False}
    if (not isinstance(node_bindings, dict) or set(node_bindings) != set(LOGICAL_NODES)
            or not all(_identity(value) for value in node_bindings.values())
            or len(set(node_bindings.values())) != len(LOGICAL_NODES)):
        raise RoleContractError("role_node_bindings_invalid")
    required = {"runtime_consumer_node_id", "agent_store_consumer_node_id", "owner_id"}
    if (not isinstance(execution_identity, dict) or set(execution_identity) != required
            or not all(_identity(value) for value in execution_identity.values())):
        raise RoleContractError("role_execution_identity_invalid")
    expected = node_bindings[role["agent_execution_node"]]
    if (execution_identity["runtime_consumer_node_id"] != expected
            or execution_identity["agent_store_consumer_node_id"] != expected
            or execution_identity["owner_id"] != case["authorization"]["owner_id"]):
        raise RoleContractError("role_execution_identity_mismatch")
    # This is consistency validation only. Future collectors must authenticate
    # these observations and independently establish physical-host requirements.
    return {
        "status": "declared_bindings_consistent",
        "independent_identity_proof": False,
        "physical_host_proof": False,
        "node_bindings": copy.deepcopy(node_bindings),
        "node_bindings_sha256": digest(node_bindings),
        "execution_identity": copy.deepcopy(execution_identity),
        "agent_execution_node_id": expected,
        "model_provider_ids": {key: node_bindings[node] for key, node in role["model_provider"].items()},
        "model_recipient_ids": {key: [node_bindings[node] for node in nodes]
                                for key, nodes in role["model_recipient"].items()},
        "source_owner_ids": {key: node_bindings[node] for key, node in role["source_owners"].items()},
        "new_source_owner_ids": {key: node_bindings[node] for key, node in role["new_source_owners"].items()},
        "connection_host_ids": {key: node_bindings[node] for key, node in role["connection_hosts"].items()},
        "history_owner_ids": {key: node_bindings[node] for key, node in role["history_owners"].items()},
        "default_agent_owner_id": node_bindings[role["default_agent_owner"]],
    }


def load_effective_case(case_id, *, overlay_path=DEFAULT_OVERLAY, base_root=REPO,
                        node_bindings=None, execution_identity=None):
    """Return a deterministic draft input envelope, never a runnable/graded run.

    Base business facts and every case field except the eight explicit
    authorization.consumer_node changes are preserved. World setup directives
    are retained in effective_case for a future fixture builder; this loader
    neither mounts resources nor executes those directives.
    """
    if not isinstance(case_id, str) or case_id not in CASE_IDS:
        raise RoleContractError("role_case_not_development")
    overlay, overlay_hash, schema_hash = _overlay(overlay_path)
    suite, world = _base_inputs(base_root)
    original = next(case for case in suite["cases"] if case["id"] == case_id)
    effective = copy.deepcopy(original)
    role_entry = overlay["cases"][case_id]
    change = overlay["authorization_changes"].get(case_id)
    if change:
        if original["authorization"]["consumer_node"] != change["from"]:
            raise RoleContractError("role_original_authorization_mismatch")
        effective["authorization"]["consumer_node"] = change["to"]
    if effective["authorization"]["consumer_node"] != role_entry["agent_execution_node"]:
        raise RoleContractError("role_effective_authorization_mismatch")
    deployment = overlay["deployment"]
    source_owners = copy.deepcopy(deployment["source_owners"])
    ownership_override = deployment["source_owner_overrides"].get(case_id, {})
    source_owners.update(ownership_override)
    # C03's all_models_private disables publication only. Receivers are not
    # cleared by this role loader; C04's existing grant remains present.
    sharing = {key: model["sharing"] for key, model in world["models"].items()}
    if effective["setup"]["world_overrides"].get("all_models_private") is True:
        sharing = dict.fromkeys(sharing, False)
    role = {
        **copy.deepcopy(role_entry),
        "authorization_change": copy.deepcopy(change),
        "source_owners": source_owners,
        "source_owner_override": copy.deepcopy(ownership_override),
        "new_source_owners": copy.deepcopy(deployment["new_source_owners"].get(case_id, {})),
        "model_provider": {key: source_owners[model["source_id"]] for key, model in world["models"].items()},
        "model_recipient": {key: copy.deepcopy(model["receivers"]) for key, model in world["models"].items()},
        "initial_model_sharing": sharing,
        "connection_hosts": copy.deepcopy(deployment["connection_hosts"]),
        "history_owners": copy.deepcopy(deployment["history_owners"]),
        "default_agent_owner": deployment["default_agent_owners"].get(case_id, role_entry["agent_execution_node"]),
        "primary_model": {"binding": effective["authorization"]["model_destination"],
                          "provider_resolution": "pending_actual_dispatch_binding"},
        "world_setup_status": "directives_preserved_not_executed",
    }
    payload = {
        "artifact_type": "ryn.agent.effective-role-case",
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "partition": "development",
        "status": "draft/not_formal_ready",
        "enabled": False,
        "formal_ready": False,
        "required_grader": REQUIRED_GRADER,
        "composition_version": COMPOSITION_VERSION,
        "base": copy.deepcopy(overlay["base"]),
        "overlay_sha256": overlay_hash,
        "overlay_schema_sha256": schema_hash,
        "base_suite_metadata": {key: copy.deepcopy(value) for key, value in suite.items() if key != "cases"},
        "effective_case": effective,
        "base_world": copy.deepcopy(world),
        "role_contract": role,
        "identity_binding": _bind_identities(role, effective, node_bindings, execution_identity),
        "pending_requirements": [
            "independent_contract_review", "explicit_version_activation", "v2_grader",
            "controller_migration", "authenticated_runtime_and_storage_identity",
            "case_external_dependencies", "independent_blind_role_binding",
        ],
    }
    # The payload is canonical-hashed before adding this field. Input byte
    # hashes bind serialization provenance; ordering in Python maps is irrelevant.
    return {**payload, "effective_sha256": digest(payload)}
