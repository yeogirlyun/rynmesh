"""Non-secret endpoint and exact-recipient binding carried by authenticated catalogs."""
from __future__ import annotations

import hashlib
import json
import re

VERSION = "ryn.model-binding.v1"


def valid(value, service_id):
    return (isinstance(value, dict) and set(value) == {"version", "service_id", "model_generation", "rule_revision"}
            and value["version"] == VERSION and value["service_id"] == service_id
            and all(isinstance(value[k], str) and re.fullmatch(r"[a-f0-9]{32}", value[k]) for k in ("model_generation", "rule_revision")))


def model_binding(record, model):
    from .sources import SourceStore
    row = record["model_settings"][model]
    return {"version": VERSION, "service_id": SourceStore.model_service_id(record, model),
            "model_generation": row["destination_revision"], "rule_revision": row["rule_revision"]}


def expected(record, consumer_peer_id):
    binding, permission = record.get("model_binding"), record.get("ai_permission")
    service_id = record.get("service", {}).get("package_id", record.get("service_id"))
    if record.get("binding_verified") is not True or record.get("consumer_peer_id") != consumer_peer_id or not valid(binding, service_id):
        return None
    space = record.get("space_permission")
    if space is not None:
        if (permission is not None or not isinstance(space, dict) or set(space) != {"space_id", "snapshot_revision", "access_generation"}
                or type(space["snapshot_revision"]) is not int or space["snapshot_revision"] < 1
                or any(not isinstance(space[k], str) or not re.fullmatch(r"[a-f0-9]{32}", space[k]) for k in ("space_id", "access_generation"))):
            return None
        local_epoch = record.get("consumer_access_generation")
        if not isinstance(local_epoch, str) or not re.fullmatch(r"[a-f0-9]{64}", local_epoch):
            return None
        return {"model_binding": binding, "space_permission": space, "consumer_peer_id": consumer_peer_id,
                "consumer_access_generation": local_epoch}
    if (not isinstance(permission, dict) or set(permission) != {"relationship_id", "revision"}
            or not isinstance(permission["relationship_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", permission["relationship_id"])
            or type(permission["revision"]) is not int or permission["revision"] < 1):
        return None
    return {"model_binding": binding, "ai_permission": permission, "consumer_peer_id": consumer_peer_id}


def revision(record, consumer_peer_id):
    binding = expected(record, consumer_peer_id)
    if not binding:
        return ""
    return hashlib.sha256(json.dumps({"provider_peer_id": record["peer_id"], **binding}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def local_revision(binding, peer_id):
    if not isinstance(binding, dict) or not valid(binding, binding.get("service_id")):
        return ""
    return hashlib.sha256(json.dumps({"provider_peer_id": peer_id, "model_binding": binding},
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
