"""S12-I18: synchronous model catalog TTL and credential-free diagnostics."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rynmesh.agent_runtime import models
from rynmesh.llm_package.sources import SourceStore


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    sources = SourceStore(tmp_path, b"s12-i18-test-key", migrate=False)
    discovery = Mock(return_value=[])
    selected = Mock(return_value=[])
    services = dict(source_store=sources, discover=discovery,
                    discover_local_model=selected)
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1234.0), raising=False)
    result = models.ModelCatalog(services, SimpleNamespace(peer_id="self", home=tmp_path))
    return result, sources, discovery, selected, clock


def save_source(sources, name="First"):
    record = sources.prepare(dict(name=name, kind="local", base_url="http://127.0.0.1:1234/v1",
                                  api_key="test-secret", models=["demo"]))
    sources.save(record)
    return record


def test_t1_discovery_diagnostic_has_no_exception_text(catalog):
    cat, _, discover, _, _ = catalog
    discover.side_effect = RuntimeError("https://secret.example/api /private/path token=secret")
    assert cat.list_models() == []
    assert cat.catalog_diagnostics() == {"discovery": dict(stage="discover", error_type="RuntimeError", at=1234.0)}
    snapshot = cat.catalog_diagnostics()
    snapshot["discovery"]["error_type"] = "mutated"
    assert cat.catalog_diagnostics()["discovery"]["error_type"] == "RuntimeError"


def test_t2_selected_model_uses_only_one_local_discovery_and_expires(catalog):
    cat, sources, discover, selected, clock = catalog
    reads = Mock(wraps=sources.read)
    sources.read = reads
    cat.get_model("local/example/model/demo")
    clock[0] = 101.99
    cat.get_model("local/example/model/demo")
    assert selected.call_count == reads.call_count == 1
    discover.assert_not_called()
    selected.assert_called_with(cat.network, "example")
    clock[0] = 102.0
    cat.get_model("local/example/model/demo")
    assert selected.call_count == 2


def test_t3_source_save_invalidates_immediately(catalog):
    cat, sources, discover, _, _ = catalog
    assert cat.list_models() == []
    assert cat.list_models() == []
    assert discover.call_count == 1
    record = save_source(sources)
    assert cat.list_models()[0]["source_id"] == record["id"]
    assert discover.call_count == 2


def test_model_settings_save_invalidates_and_results_are_isolated(catalog):
    cat, sources, discover, _, _ = catalog
    record = save_source(sources)
    first = cat.list_models()
    first[0]["name"] = "caller mutation"
    assert cat.list_models()[0]["name"] != "caller mutation"
    sources.configure_model(record["id"], dict(model_id="demo",
        expected_rule_revision=record["model_settings"]["demo"]["rule_revision"],
        configuration={"context_window": 65536}))
    assert cat.list_models()[0]["context_window"] == 65536
    assert discover.call_count == 2


def test_usage_quota_revision_invalidates(catalog):
    cat, sources, discover, _, _ = catalog
    save_source(sources)
    row = cat.list_models()[0]
    sources.usage_store.configure_quota(row["id"], {"requests_per_day": 7})
    assert cat.list_models()[0]["usage"]["quota"]["requests_per_day"] == 7
    assert discover.call_count == 2


def test_failed_discovery_retries_synchronously_after_ttl(catalog):
    cat, _, discover, _, clock = catalog
    discover.side_effect = OSError("unavailable")
    assert cat.list_models() == []
    discover.side_effect = None
    discover.return_value = [dict(peer_id="other", online=True,
        service=dict(package_id="remote", adapter="openai_compatible"))]
    assert cat.list_models() == []
    clock[0] += 2
    assert cat.list_models()[0]["available"]
    assert discover.call_count == 2


def test_cache_keys_separate_selection_and_display(catalog):
    cat, _, discover, selected, _ = catalog
    cat.list_models()
    cat.display_models()
    cat.get_model("local/first")
    cat.get_model("local/second")
    cat.get_model("local/second")
    assert discover.call_count == 2
    assert selected.call_count == 2


def test_refresh_destination_bypasses_cached_result(catalog):
    cat, _, discover, _, _ = catalog
    cat.get_model("peer/unknown/example")
    cat.get_model("peer/unknown/example")
    assert discover.call_count == 1
    cat.refresh_destination("peer/unknown/example")
    assert discover.call_count == 3


def test_cli_enumeration_diagnostic_is_sanitized(catalog, monkeypatch):
    cat, _, discover, _, _ = catalog
    discover.return_value = [dict(peer_id="self", online=True, chat_protocol="rynmesh.chat.v1",
        service=dict(package_id="cli", adapter="codex_cli", capabilities=["client-tools"]))]
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", Mock(side_effect=OSError("secret path")))
    rows = cat.list_models()
    assert not rows[0]["available"]
    assert cat.catalog_diagnostics()["cli_models"]["local/cli"] == dict(
        stage="cli_models", source="local/cli", error_type="OSError", at=1234.0)
