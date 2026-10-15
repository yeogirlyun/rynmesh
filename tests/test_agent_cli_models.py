"""CLI model identities and optional diagnostics do not gate transport admission."""
import threading
import time
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.cli_binding import CLIModelDispatchAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.tool_capabilities import ToolCapabilities


def test_cli_catalog_has_separate_identity_capability_and_dispatch(tmp_path, monkeypatch):
    from rynmesh.llm_package import cli_models
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "fixture-cli")
    monkeypatch.setattr(cli_models, "list_models", lambda *args: [
        {"id": "model-a", "name": "Model A", "default": True,
         "supported_reasoning_efforts": ["low", "high", "ultra"], "default_reasoning_effort": "low"},
        {"id": "model-b", "name": "Model B", "default": False}])
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                                  runtime="external", public_model_alias="Codex CLI")
    capabilities = ToolCapabilities(tmp_path)
    capabilities.save(manifest, "supported")
    capabilities.save(manifest, "supported", "model-b")
    calls = []
    class Adapter:
        def chat(self, body, **kwargs):
            calls.append(kwargs)
            return {"text": "ok", "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop", "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}
    manager = SimpleNamespace(manifest=manifest, adapter=Adapter(), _slots=threading.BoundedSemaphore(1),
                              _lock=threading.Lock(), _running=0)
    record = {"peer_id": "self", "online": True, "tool_support": "supported", "chat_protocol": "rynmesh.chat.v1",
              "service": {"package_id": "codex-cli", "adapter": "codex_cli", "runtime": "external",
                          "model_alias": "Codex CLI", "capabilities": ["client-tools"],
                          "context_window": 32768, "max_output_tokens": 4096}}
    store = SimpleNamespace(peer_id="self", llm_tool_capabilities=capabilities)
    services = {"source_store": SimpleNamespace(read=lambda: {}), "discover": lambda _: [record],
                "provider_for": lambda _: manager}
    catalog = ModelCatalog(services, store)
    rows = {row["id"]: row for row in catalog.list_models()}
    assert rows["local/codex-cli"]["agent_supported"] is True
    assert rows["local/codex-cli/model/model-a"]["agent_supported"] is True
    assert rows["local/codex-cli/model/model-a"]["tool_support"] == "unknown"
    assert rows["local/codex-cli/model/model-b"]["agent_supported"] is True
    assert rows["local/codex-cli"]["default_reasoning_effort"] == "low"
    assert rows["local/codex-cli/model/model-a"]["supported_reasoning_efforts"] == ["low", "high", "ultra"]
    assert "supported_reasoning_efforts" not in rows["local/codex-cli/model/model-b"]
    bridge = AgentModelBridge(services, catalog)
    bridge.complete("local/codex-cli/model/model-a", [{"role": "user", "content": "hello"}], [], lambda: False)
    assert calls[-1]["model"] == "model-a"
    bridge.complete("local/codex-cli/model/model-b", [{"role": "user", "content": "hello"}], [], lambda: False)
    assert calls[-1]["model"] == "model-b"
    assert catalog.get_model("local/codex-cli/model/model-b")["agent_supported"] is True
    capabilities.save(manifest, "unsupported", "model-b")
    assert catalog.get_model("local/codex-cli/model/model-b")["agent_supported"] is True
    assert catalog.get_model("local/codex-cli/model/model-b")["tool_support"] == "unsupported"


def test_destination_refresh_only_enumerates_the_selected_model():
    catalog = ModelCatalog({"discover": lambda _: []}, SimpleNamespace(peer_id="self"))
    selected = []
    expected = {"id": "peer/target/service"}
    def list_selected(*, selected_model_id=None):
        selected.append(selected_model_id)
        return [expected]
    catalog.list_models = list_selected
    assert catalog.refresh_destination(expected["id"]) == expected
    assert selected == [expected["id"]]


@pytest.fixture
def cli_metadata_catalog(tmp_path, monkeypatch):
    from rynmesh.llm_package import cli_models, codex_session
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"fixture-v1")
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: str(binary))
    enumerations = []
    models = [{"id": "model-a", "name": "Model A", "default": True, "context_window": 400000,
               "supported_reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"}]
    def discover(_executable):
        enumerations.append(True)
        return [dict(model) for model in models]
    monkeypatch.setattr(codex_session, "list_models", discover)
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                                  runtime="external", public_model_alias="Codex CLI")
    manager = SimpleNamespace(manifest=manifest,
        adapter=CLIModelDispatchAdapter(CLIAgentAdapter("codex_cli"), manifest, tmp_path))
    record = {"peer_id": "self", "online": True, "chat_protocol": "rynmesh.chat.v1",
              "service": {"package_id": "codex-cli", "adapter": "codex_cli", "runtime": "external",
                          "model_alias": "Codex CLI", "capabilities": ["client-tools"]}}
    store = SimpleNamespace(peer_id="self", llm_tool_capabilities=ToolCapabilities(tmp_path))
    services = {"source_store": SimpleNamespace(read=lambda: {}), "discover": lambda _: [record],
                "provider_for": lambda _: manager}
    catalog = ModelCatalog(services, store)
    def expire():
        with cli_models._cache_lock:
            key = ("codex_cli", str(binary))
            cached = cli_models._cache[key]
            cli_models._cache[key] = (time.monotonic() - 301, cached[1])
    return SimpleNamespace(catalog=catalog, binary=binary, manager=manager, record=record,
                           enumerations=enumerations, models=models, expire=expire)


def test_selected_known_cli_model_reuses_expired_metadata_without_changing_authority(cli_metadata_catalog, monkeypatch):
    from rynmesh.llm_package import codex_session
    fixture = cli_metadata_catalog
    identifier = "local/codex-cli/model/model-a"
    before = fixture.catalog.get_model(identifier)
    binding_bytes = fixture.manager.adapter.path.read_bytes()
    fixture.expire()
    monkeypatch.setattr(codex_session, "list_models", lambda *_: pytest.fail("known bound model must not spawn discovery"))
    fixture.record["online"] = False
    after = fixture.catalog.get_model(identifier)
    assert after["available"] is False
    for key in ("id", "context_window", "supported_reasoning_efforts", "default_reasoning_effort",
                "destination_revision", "local_model_binding"):
        assert after[key] == before[key]
    assert fixture.manager.adapter.path.read_bytes() == binding_bytes
    assert fixture.enumerations == [True]


@pytest.mark.parametrize("change", ["executable", "configuration"])
def test_changed_cli_binding_refreshes_even_fresh_model_cache(cli_metadata_catalog, change):
    fixture = cli_metadata_catalog
    identifier = "local/codex-cli/model/model-a"
    before = fixture.catalog.get_model(identifier)
    if change == "executable":
        fixture.binary.write_bytes(b"fixture-updated-executable")
    else:
        fixture.manager.manifest.context_window += 1024
    fixture.models[0]["supported_reasoning_efforts"] = ["medium", "high"]
    after = fixture.catalog.get_model(identifier)
    assert after["destination_revision"] != before["destination_revision"]
    assert after["supported_reasoning_efforts"] == ["medium", "high"]
    assert len(fixture.enumerations) == 2


def test_unknown_cli_model_uses_existing_discovery_without_inventing_a_row(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.catalog.get_model("local/codex-cli/model/model-a")
    fixture.expire()
    assert fixture.catalog.get_model("local/codex-cli/model/missing") is None
    assert len(fixture.enumerations) == 2
    fixture.expire()
    fixture.models.append({"id": "new-model", "name": "New model"})
    assert fixture.catalog.get_model("local/codex-cli/model/new-model")["cli_model"] == "new-model"
    assert len(fixture.enumerations) == 3


def test_full_cli_model_list_refreshes_expired_cache_despite_selected_snapshot(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.catalog.get_model("local/codex-cli/model/model-a")
    fixture.expire()
    fixture.models.append({"id": "new-model", "name": "New model"})
    rows = fixture.catalog.list_models()
    assert any(row.get("cli_model") == "new-model" for row in rows)
    assert len(fixture.enumerations) == 2
