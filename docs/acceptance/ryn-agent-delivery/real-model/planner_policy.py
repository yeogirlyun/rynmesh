"""Explicit planner generation mapping; never alters the owner source or gates."""
from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path

from rynmesh.agent_runtime.output_policy import VERSION
from rynmesh.agent_runtime.store import encoded
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.sources import SourceStore

SOURCE_ID = "src-7de9fb1d36c44bc8"
MODEL_ID = "glm-5.3-flash"
FIELDS = ("context_window", "max_output_tokens", "request_defaults")


def read_planner(home, source_id):
    if source_id != SOURCE_ID:
        raise ValueError("configured_glm_source_required")
    home = Path(home)
    key = base64.b64decode((home / "identity.ed25519").read_text().strip())
    source = SourceStore(home, key, migrate=False).read()[source_id]
    if MODEL_ID not in source["models"] or not source.get("enabled") or source.get("status") != "ready":
        raise ValueError("configured_glm_model_required")
    effective = model_settings.effective(source, MODEL_ID)
    generation = model_settings.validate({key: effective[key] for key in FIELDS})
    # Explicit whitelist: never hash credentials, endpoint, or a source record.
    public = {"version": VERSION, "source_id": source_id, "model_id": MODEL_ID, "generation": generation}
    policy = {**public, "policy_sha256": hashlib.sha256(encoded(public)).hexdigest()}
    return source, SourceStore.adapter(source, MODEL_ID), policy


def map_fixture(record, model, policy):
    before = {key: model_settings.effective(record, model)[key] for key in FIELDS}
    record["model_settings"][model]["overrides"].update(copy.deepcopy(policy["generation"]))
    return {"fixture_source_id": record["id"], "fixture_model_id": model, "before": before,
            "after": copy.deepcopy(policy["generation"]), "planner_policy_sha256": policy["policy_sha256"],
            "scope": "Generation metadata mapped before first business snapshot; identities, sharing and grants retain frozen facts."}


def assert_mapping(sources, selected, policy):
    for record in sources.read().values():
        for model in record["models"]:
            if selected == "local/" + sources.model_service_id(record, model):
                actual = {key: model_settings.effective(record, model)[key] for key in FIELDS}
                if actual == policy["generation"]:
                    return
                raise RuntimeError("planner_fixture_policy_changed")
    raise RuntimeError("planner_fixture_model_missing")
