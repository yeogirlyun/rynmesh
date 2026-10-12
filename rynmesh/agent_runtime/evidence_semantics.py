"""Bounded observation metadata, not a classifier of natural-language claims."""
from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime

from .store import encoded

GUIDANCE = (
    "Ground each factual or causal claim in evidence for the same subject, direction, scope and time. "
    "An observation_contract describes what a read can establish: passed means the compared observations match, "
    "not that the answer's interpretation or cause is proved. Contracts limit interpretation; raw evidence "
    "remains the factual basis, including rows omitted from the bounded metadata projection. A consumer-visible catalog is not a provider's "
    "complete inventory; local outgoing grants/friendships say nothing about another provider's recipients. "
    "A device directory entry alone proves neither friendship nor personal-space membership. "
    "Unknown/null, an omitted item, and a probe not performed are not negative facts or history. "
    "Use record checked_at/last_seen and evidence_source for temporal claims; re-reading an old heartbeat "
    "does not refresh it or establish current reachability. A catalog invocation_verified=false means this "
    "observation does not verify invocation, not that no invocation ever occurred. "
    "State supported observations and label unresolved causes as hypotheses; apply these limits to the "
    "opening, tables and conclusion, since a later caveat cannot repair a stronger unsupported assertion."
)

_KINDS = {
    "ryn_list_models": "observer_visible_catalog",
    "ryn_inspect_services": "observer_visible_catalog_and_local_outgoing_grants",
    "ryn_list_devices": "observer_device_directory_and_local_relationships",
    "ryn_list_source_configurations": "local_source_configuration",
    "ryn_get_agent_configuration": "local_agent_configuration",
    "ryn_get_system_diagnostics": "catalog_and_existing_device_evidence",
    "ryn_diagnose_current_task": "specified_task_metadata",
    "ryn_diagnose_task": "specified_task_metadata",
    "ryn_list_resource_roots": "task_authorized_roots",
    "ryn_list_resources": "observed_directory_page",
    "ryn_read_resource": "authorized_document_fragment",
    "ryn_search_resource": "bounded_literal_search",
}
_LIMIT = 8


def _identity(value):
    # Values remain data; omit free-form labels, arguments and arbitrary notes.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", value) else "unknown"


def _peer_identity(value):
    # Production identities are canonical base64 public keys, not free-form
    # text. Keep legacy safe identifiers without broadening model/label fields.
    if isinstance(value, str) and len(value) == 44:
        try:
            raw = base64.b64decode(value, validate=True)
            if len(raw) == 32 and base64.b64encode(raw).decode("ascii") == value:
                return value
        except (ValueError, binascii.Error):
            pass
    return _identity(value)


def _time(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None:
                return value
        except ValueError:
            pass
    return None


def _enum(value, choices):
    return value if isinstance(value, str) and value in choices else "unknown"


def observation_contract(name, result, *, read_at=None):
    """Project only registered read semantics; raw results remain the evidence.

    No answer text is examined and no status is inferred. Missing legacy fields
    remain unknown. Metadata limits do not imply completeness of the raw result.
    """
    if name not in _KINDS or not isinstance(result, dict):
        return None
    contract = {"version": 1, "scope": _KINDS[name],
                "verifies": "compared_observations_only_not_answer_claims",
                "read_at": _time(read_at), "record_times_refreshed_by_read": False,
                "history_coverage": "not_established"}
    if name in {"ryn_list_models", "ryn_inspect_services", "ryn_get_system_diagnostics"}:
        models = result.get("models")
        if isinstance(models, list):
            contract["catalog_completeness"] = "no_global_absence_proof_local_known_configuration_retained"
            contract["model_scopes"] = []
            for model in models[:_LIMIT]:
                if not isinstance(model, dict):
                    continue
                access = model.get("recipient_access")
                access = access if isinstance(access, dict) else {}
                contract["model_scopes"].append({
                    "model_ref": _identity(model.get("id")), "provider_peer_id": _peer_identity(model.get("peer_id")),
                    "consumer_peer_id": _peer_identity(access.get("consumer_peer_id", model.get("consumer_peer_id"))),
                    "sharing_configuration_known": (model.get("sharing_configuration_known")
                        if type(model.get("sharing_configuration_known")) is bool else None),
                    "share_scope": _enum(model.get("share_scope"), {"none", "friends", "personal_space"}),
                    "access_basis": _enum(access.get("basis"), {"authenticated_catalog_observation"}),
                    "current_access": "unknown", "invocation_proof": "not_established_by_catalog"})
            contract["model_scopes_omitted"] = max(0, len(models) - _LIMIT)
    if name == "ryn_inspect_services":
        scope = result.get("friend_grants_scope")
        scope = scope if isinstance(scope, dict) else {}
        contract["grants_scope"] = {"provider_peer_id": _peer_identity(scope.get("provider_peer_id")),
            "direction": _enum(scope.get("direction"), {"local_outgoing"}), "remote_provider_grants": "unknown"}
        contract["grants_coverage"] = "returned_local_provider_snapshot_only_not_grant_history"
    if name in {"ryn_list_devices", "ryn_get_system_diagnostics"}:
        container = result if name == "ryn_list_devices" else result.get("devices", {})
        container = container if isinstance(container, dict) else {}
        contract["relationship_observer_peer_id"] = _peer_identity(container.get("self_id"))
        devices = container.get("devices")
        if isinstance(devices, list):
            contract["device_observations"] = [{
                "peer_id": _peer_identity(device.get("peer_id")),
                "source_checked_at": _time(device.get("checked_at")), "last_seen": _time(device.get("last_seen")),
                "basis": _enum(device.get("evidence_source"), {"self", "http", "heartbeat", "heartbeat_expired"}),
                **{key: device.get(key) if type(device.get(key)) is bool else None
                   for key in ("online", "http_probe_performed", "lan_probe_performed")}}
                for device in devices[:_LIMIT] if isinstance(device, dict)]
            contract["device_observations_omitted"] = max(0, len(devices) - _LIMIT)
    # Never trim the evidence itself. Only these duplicate metadata rows have a
    # strict byte budget; counts direct the reader back to the original result.
    while len(encoded(contract)) > 2048:
        key = max((key for key in ("model_scopes", "device_observations") if contract.get(key)),
                  key=lambda key: len(encoded(contract[key])), default=None)
        if key is None:
            break
        contract[key].pop()
        contract[key + "_omitted"] += 1
    return contract
