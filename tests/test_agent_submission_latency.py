"""Submitting a task must not discover unrelated installed CLI models."""
import json
import uuid

import pytest
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import cli_models
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.services import peer_box


@pytest.mark.parametrize("support", ["unknown", "unsupported", "supported"])
def test_api_model_actions_skip_probes_and_cold_unrelated_cli_discovery(node, monkeypatch, support):  # noqa: F811
    app, owner = node
    record, model_id = add_source(app, support=support, models=["model-a", "model-b"])
    other_id = "local/" + app.state.llm_agent_services["source_store"].model_service_id(record, "model-b")
    manifest = LLMPackageManifest(package_id="slow-cli", mode="antigravity_cli", adapter="antigravity_cli",
                                  runtime="external", public_model_alias="Unrelated CLI")
    manifest_path = owner.home / "slow-cli.json"
    save_manifest(manifest, manifest_path)
    settings = owner.home / "llm" / "cli-services.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"antigravity_cli": {"manifest": str(manifest_path),
                                                       "publication_enabled": False}}))
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "slow-cli-fixture")
    monkeypatch.setattr(cli_models, "cached_models", lambda *_: [])
    discoveries = []
    def discover(*_):
        discoveries.append(True)
        return [{"id": "", "name": "CLI default", "default": True},
                {"id": "other-model", "name": "Other model", "default": False}]
    monkeypatch.setattr(cli_models, "list_models", discover)
    bridge, tools, catalog = build_agent_integrations(app, owner, owner.home)
    runtime = AgentRuntime(AgentStore(owner.home / "submission-agent",
        peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id})
    # No worker/model inference runs as part of either submission operation.
    def infer(*_args, **_kwargs):
        raise AssertionError("submission ran inference")
    monkeypatch.setattr(bridge, "complete", infer)
    body = {"message": "Inspect this", "model_id": model_id, "client_request_id": str(uuid.uuid4())}
    created = runtime.create(body)
    assert created["status"] == "queued"
    assert runtime.create(body)["id"] == created["id"]
    runtime.action(created["id"], {"action": "pause", "expected_control_revision": created["control_revision"]})
    preview = runtime.preview_model_switch(created["id"], other_id)
    switched = runtime.switch_model(created["id"], {"model_id": other_id,
        "expected_control_revision": preview["control_revision"], "expected_scope_revision": preview["scope_revision"],
        "expected_destination": preview["destination"], "confirm_data_destination": True,
        "transfer_resource_scope": False, "client_request_id": str(uuid.uuid4())})
    assert switched["model_id"] == other_id and switched["status"] == "paused"
    assert runtime.settings()["model_id"] == model_id
    assert runtime.create({"message": "Use the saved default"})["model_id"] == model_id
    continued = runtime.add_message(created["id"], {"message": "One more detail",
        "expected_control_revision": switched["control_revision"], "client_request_id": str(uuid.uuid4())})
    assert continued["status"] == "queued"
    assert discoveries == []
    # Explicitly opening the full model picker still discovers that CLI.
    assert any(row.get("cli_model") == "other-model" for row in catalog.list_models())
    assert discoveries
