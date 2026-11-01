"""Local model metadata stays current without remote registry dependencies."""
import hashlib
from types import SimpleNamespace

import pytest
from test_agent_cli_models import cli_metadata_catalog  # noqa: F401
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.models import ModelCatalog


def test_local_source_lookup_never_enters_offline_registry_and_keeps_readiness_current(node, monkeypatch):  # noqa: F811
    app, store = node
    source, identifier = add_source(app)
    registry_calls = []

    def offline_registry(**kwargs):
        registry_calls.append(kwargs)
        raise OSError("registry is offline")

    monkeypatch.setattr(store, "list_job_capacities", offline_registry)
    _, _, catalog = build_agent_integrations(app, store, store.home)
    first = catalog.get_model(identifier)
    assert first["available"] is True and registry_calls == []
    sources = app.state.llm_agent_services["source_store"]
    changed = sources.prepare({"enabled": False}, source["id"])
    sources.save(changed)
    assert catalog.get_model(identifier)["available"] is False
    assert registry_calls == []
    catalog.list_models()  # A full inventory still consults registry discovery.
    assert len(registry_calls) == 1


@pytest.mark.parametrize("change", ["executable", "configuration"])
def test_local_cli_lookup_avoids_remote_discovery_without_caching_binding(cli_metadata_catalog, change):  # noqa: F811
    fixture = cli_metadata_catalog
    identifier = "local/codex-cli/model/model-a"
    fixture.catalog.services["discover_local"] = lambda _: [fixture.record]
    fixture.catalog.services["discover_agent"] = lambda _: pytest.fail("local lookup entered remote discovery")
    first = fixture.catalog.get_model(identifier)
    if change == "executable":
        fixture.binary.write_bytes(b"updated executable")
    else:
        fixture.manager.manifest.context_window += 1024
    fixture.models[0]["supported_reasoning_efforts"] = ["medium", "high"]
    changed = fixture.catalog.get_model(identifier)
    assert changed["destination_revision"] != first["destination_revision"]
    assert changed["supported_reasoning_efforts"] == ["medium", "high"]
    assert len(fixture.enumerations) == 2
    fixture.record["online"] = False
    assert fixture.catalog.get_model(identifier)["available"] is False


def test_peer_lookup_and_full_catalog_keep_remote_discovery():
    peer = "other-node"
    record = {"peer_id": peer, "online": True, "ready": True, "chat_protocol": "rynmesh.chat.v1",
              "service": {"package_id": "remote", "adapter": "openai_compatible", "model_alias": "Remote"}}
    remote_calls = []

    def remote(network):
        remote_calls.append(network)
        return [record]

    services = {"source_store": SimpleNamespace(read=lambda: {}), "discover": remote,
                "discover_agent": remote,
                "discover_local": lambda _: pytest.fail("remote lookup entered local-only discovery")}
    catalog = ModelCatalog(services, SimpleNamespace(peer_id="self"))
    identifier = "peer/" + hashlib.sha256(peer.encode()).hexdigest() + "/remote"
    assert catalog.get_model(identifier)["available"] is True
    assert catalog.list_models()[0]["id"] == identifier
    assert len(remote_calls) == 2


def test_legacy_integrations_without_local_discovery_keep_existing_fallback(cli_metadata_catalog):  # noqa: F811
    fixture = cli_metadata_catalog
    assert "discover_local" not in fixture.catalog.services
    assert fixture.catalog.get_model("local/codex-cli/model/model-a")["available"] is True


def test_cli_model_lookup_reuses_stat_fingerprint_cache_and_skips_unselected_siblings(cli_metadata_catalog, monkeypatch):  # noqa: F811
    fixture = cli_metadata_catalog
    dispatch = fixture.manager.adapter
    fixture.models.append({"id": "model-b", "name": "Model B", "supported_reasoning_efforts": ["low", "medium"]})
    fixture.catalog.services["discover_local"] = lambda _: [fixture.record]

    binding_calls = []
    orig_binding_for_model = dispatch.binding_for_model

    def spy_binding_for_model(model=None):
        binding_calls.append(model)
        return orig_binding_for_model(model)

    monkeypatch.setattr(dispatch, "binding_for_model", spy_binding_for_model)
    first = fixture.catalog.get_model("local/codex-cli/model/model-a")
    assert first is not None and first["available"] is True
    # Only container (None) and the requested model-a are bound; sibling model-b is skipped.
    assert "model-a" in binding_calls and "model-b" not in binding_calls


    # Repeated binding_for_model calls with unchanged file stat + binary fingerprint do not re-read disk.
    from rynmesh.llm_package import cli_binding
    read_calls = [0]
    orig_read_json = cli_binding.read_json

    def spy_read_json(*args, **kwargs):
        read_calls[0] += 1
        return orig_read_json(*args, **kwargs)

    monkeypatch.setattr(cli_binding, "read_json", spy_read_json)
    again = fixture.catalog.get_model("local/codex-cli/model/model-a")
    assert again["destination_revision"] == first["destination_revision"]
    assert read_calls[0] == 0

    # Updating the binary invalidates the fingerprint immediately.
    fixture.binary.write_bytes(b"updated-binary-v2")
    refreshed = fixture.catalog.get_model("local/codex-cli/model/model-a")
    assert refreshed["destination_revision"] != first["destination_revision"]
    assert read_calls[0] >= 1
