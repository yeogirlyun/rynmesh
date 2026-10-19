"""Metadata-only observations durably revoke changed CLI destinations."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_agent_cli_models import cli_metadata_catalog as cli_metadata_catalog
from test_agent_peer_cli import cli_pair as cli_pair
from test_agent_peer_cli import nodes as nodes
from test_agent_peer_cli import space_pair as space_pair

from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.cli_binding import CLIModelDispatchAdapter


def test_metadata_only_capacity_observation_is_durable_across_restart_without_binding_reads(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.models.append({"id": "model-b", "name": "B", "context_window": 128000})
    adapter = fixture.manager.adapter
    before = {model: adapter.binding_for_model(model) for model in ("", "model-a", "model-b")}
    fixture.models[0]["context_window"] = 16000
    fixture.expire()
    assert adapter.context_metadata_for_model("model-a")["context_window"] == 16000
    stored = json.loads(adapter.path.read_text())[fixture.manager.manifest.package_id]
    assert stored["models"]["model-a"]["signature"]["context_window"] == 16000
    restarted = CLIModelDispatchAdapter(CLIAgentAdapter("codex_cli"), fixture.manager.manifest, adapter.path.parent)
    fixture.models[0]["context_window"] = 400000
    fixture.expire()
    assert restarted.context_metadata_for_model("model-a")["context_window"] == 400000
    assert restarted.binding_for_model("model-a") != before["model-a"]
    assert restarted.binding_for_model() != before[""]
    assert restarted.binding_for_model("model-b") == before["model-b"]


def test_successful_listing_disappearance_persists_tombstone_before_reappearance(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.models.append({"id": "model-b", "name": "B", "context_window": 128000})
    adapter = fixture.manager.adapter
    before = {model: adapter.binding_for_model(model) for model in ("model-a", "model-b")}
    removed = fixture.models.pop(0)
    fixture.expire()
    assert [item["id"] for item in adapter.current_models()] == ["model-b"]
    stored = json.loads(adapter.path.read_text())[fixture.manager.manifest.package_id]
    assert stored["models"]["model-a"]["signature"] == {"model": "model-a", "available": False}
    with pytest.raises(AdapterError, match="cli_model_unavailable"):
        adapter.binding_for_model("model-a")
    restarted = CLIModelDispatchAdapter(CLIAgentAdapter("codex_cli"), fixture.manager.manifest, adapter.path.parent)
    fixture.models.insert(0, removed)
    fixture.expire()
    restarted.current_models()
    assert restarted.binding_for_model("model-a") != before["model-a"]
    assert restarted.binding_for_model("model-b") == before["model-b"]


def test_default_selection_observation_aba_changes_only_default_generation(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.models.append({"id": "model-b", "name": "B", "context_window": 400000})
    adapter = fixture.manager.adapter
    before = {model: adapter.binding_for_model(model) for model in ("", "model-a", "model-b")}
    for selected in (1, 0):
        for index, item in enumerate(fixture.models):
            item["default"] = index == selected
        fixture.expire()
        assert next(item for item in adapter.current_models() if item.get("default"))["id"] == fixture.models[selected]["id"]
    assert adapter.binding_for_model() != before[""]
    for model in ("model-a", "model-b"):
        assert adapter.binding_for_model(model) == before[model]


def test_one_atomic_observation_write_for_whole_snapshot_and_none_for_unchanged_reads(cli_metadata_catalog, monkeypatch):
    import rynmesh.llm_package.cli_binding as binding_module
    fixture = cli_metadata_catalog
    adapter = fixture.manager.adapter
    adapter.current_models()
    writes = []
    original = binding_module.atomic_write_json
    def record(*args, **kwargs):
        writes.append(copy.deepcopy(args[1]))
        return original(*args, **kwargs)
    monkeypatch.setattr(binding_module, "atomic_write_json", record)
    for index in range(3):
        fixture.models.append({"id": f"new-{index}", "name": "New", "context_window": 128000})
    fixture.models[0]["context_window"] = 16000
    fixture.expire()
    adapter.current_models()
    assert len(writes) == 1
    for model in ("", "model-a", "new-0", "new-1", "new-2"):
        adapter.context_metadata_for_model(model)
        adapter.binding_for_model(model)
        adapter.cached_binding_for_model(model)
    assert len(writes) == 1


def test_failed_observation_storage_does_not_publish_changed_metadata(cli_metadata_catalog, monkeypatch):
    import rynmesh.llm_package.cli_binding as binding_module
    fixture = cli_metadata_catalog
    adapter = fixture.manager.adapter
    adapter.current_models()
    before = adapter.path.read_bytes()
    fixture.models[0]["context_window"] = 16000
    fixture.expire()
    def unavailable(*args, **kwargs):
        raise OSError("fixture store unavailable")
    monkeypatch.setattr(binding_module, "atomic_write_json", unavailable)
    with pytest.raises(OSError):
        adapter.context_metadata_for_model("model-a")
    assert adapter.path.read_bytes() == before


def test_configured_cli_models_http_listing_records_observed_capacity_aba(cli_pair, monkeypatch):
    from rynmesh.llm_package import cli_models
    fixture = cli_pair
    selected = "gemini-3.8-flash-medium"
    models = [{"id": "", "name": "Default", "default": True, "context_window": 32768},
              {"id": selected, "name": "Selected", "context_window": 400000},
              {"id": "unverified", "name": "Sibling", "context_window": 128000}]
    monkeypatch.setattr(cli_models, "list_models", lambda *a, **kw: copy.deepcopy(models))
    adapter = fixture.provider.adapter
    before = adapter.binding_for_model(selected)
    sibling = adapter.binding_for_model("unverified")
    with TestClient(fixture.apps[0]) as client:
        for window in (16000, 400000):
            models[1]["context_window"] = window
            response = client.get("/api/local/llm/cli-services/antigravity_cli/models")
            assert response.status_code == 200, response.text
            assert next(item for item in response.json()["models"] if item["id"] == selected)["context_window"] == window
    assert adapter.binding_for_model(selected) != before
    assert adapter.binding_for_model("unverified") == sibling
