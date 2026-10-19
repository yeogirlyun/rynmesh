"""Small, current inference facts from the server's saved destination and scope.

This is a projection, not a new grant or an egress audit. In particular, a local
catalog entry identifies the dispatch route, not the API endpoint's locality.
"""
from __future__ import annotations

GUIDANCE = (
    "Request messages/tool results reach the model; no send tool needed. "
    "Local tools/read_only imply neither local inference nor no transfer. "
    "inference_context records route/grants, not proof of resource transfers. "
    "API/service and peer upstream locality are unknown."
)


def projection(row: dict) -> dict:
    destination = row.get("model_destination") or {}
    model_id = destination.get("catalog_model_id", "")
    # These are the catalog's server-generated route conventions, not model
    # labels, tool output or owner prose. Unrecognized/legacy IDs stay unknown.
    kind = "unknown"
    if isinstance(model_id, str) and model_id == row.get("model_id"):
        if model_id.startswith("peer/") and destination.get("provider_peer_id"):
            kind = "peer"
        elif model_id.startswith("local/"):
            kind = "local_api" if destination.get("source_id") else "local_service"
    scope = row.get("scope") or {}
    grants = scope.get("resource_grants", [])
    bound = bool(destination.get("destination_revision"))
    matched = {
        grant["grant_id"] for grant in grants if grant.get("grant_id") and bound
        and any(egress.get("destination") == destination
                and grant["grant_id"] in egress.get("origin_grant_ids", [])
                for egress in scope.get("egress_grants", []))
    }
    # Do not copy URLs, credentials, paths, resource bodies, encrypted refs or
    # arbitrary nested destination fields into an always-present system prompt.
    # Selection.allow_model_data is deliberately ignored: a retained selection
    # after a switch is not an active grant. Connection validation still applies.
    return {"destination_kind": kind, "destination_binding_recorded": bound,
            "resource_grants_recorded": len(grants),
            "resources_with_matching_egress": len(matched),
            "historical_egress": "not_established"}
