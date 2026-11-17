"""S03-I20 shares vendor-window inference with actual local/peer catalog projection."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_agent_cli_models import cli_metadata_catalog  # noqa: F401

from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import model_settings


MISSING = object()


def catalog_row(tmp_path, *, own=False, alias="deepseek-v4", service_id="service", window=MISSING,
                adapter="openai_compatible", cli_models=None):
    manifest = dict(package_id=service_id, adapter=adapter,
                    capabilities=["client-tools", "context-estimate:v2"], max_output_tokens=4096)
    if alias is not MISSING:
        manifest["model_alias"] = alias
    if window is not MISSING:
        manifest["context_window"] = window
    record = dict(peer_id="self" if own else "other", online=True, ready=True,
                  chat_protocol="rynmesh.chat.v1", service=manifest)
    if cli_models:
        record["cli_models"] = cli_models
    services = dict(source_store=SimpleNamespace(read=lambda: {}, path=tmp_path / "sources.enc"),
                    discover=lambda _: [record])
    catalog = ModelCatalog(services, SimpleNamespace(peer_id="self", home=tmp_path))
    rows = catalog.list_models()
    return catalog, next(row for row in rows if "/model/" not in row["id"])


@pytest.mark.parametrize("own", [False, True], ids=["peer", "local_service"])
def test_t1_missing_manifest_window_uses_deepseek_family_and_actual_admission(tmp_path, own):
    catalog, row = catalog_row(tmp_path, own=own)
    assert row["context_window"] == 1048576
    assert row["context_window_known"] is True
    assert row["context_window_basis"] == "vendor_family_default"
    assert catalog.get_model(row["id"])["context_window"] == 1048576
    policy = output_policy.context_budget(row)
    assert policy["effective_max_context_bytes"] > 120000
    output_policy.admit_request_context(row, [{"role": "user", "content": "x" * 150000}], [])


@pytest.mark.parametrize("own", [False, True])
def test_t2_unknown_alias_keeps_conservative_window_and_admission_cap(tmp_path, own):
    _, row = catalog_row(tmp_path, own=own, alias="unknown-private-model")
    assert row["context_window"] == 32768
    assert row["context_window_known"] is False
    assert row["context_window_basis"] == "unknown_fallback"
    policy = output_policy.context_budget(row)
    assert policy["context_window_tokens"] == 32768 and policy["effective_max_context_bytes"] <= 120000
    with pytest.raises(AgentError, match="^agent_context_limit$"):
        output_policy.admit_request_context(row, [{"role": "user", "content": "x" * 150000}], [])


@pytest.mark.parametrize("own", [False, True])
@pytest.mark.parametrize("window", [65536, 131072])
def test_t3_integer_manifest_has_priority_over_vendor_table(tmp_path, own, window):
    _, row = catalog_row(tmp_path, own=own, window=window)
    assert row["context_window"] == window
    assert row["context_window_known"] is True
    assert row["context_window_basis"] == "provider_manifest"


@pytest.mark.parametrize("own", [False, True])
def test_missing_alias_uses_service_id(tmp_path, own):
    _, row = catalog_row(tmp_path, own=own, alias=MISSING, service_id="deepseek-v4-local")
    assert row["context_window"] == 1048576 and row["context_window_known"]


@pytest.mark.parametrize("window", [None, "65536", True])
def test_noninteger_manifest_window_uses_vendor_inference(tmp_path, window):
    _, row = catalog_row(tmp_path, window=window)
    assert row["context_window"] == 1048576 and row["context_window_known"]


def test_public_lookup_preserves_existing_private_callers_and_table():
    assert model_settings.known_vendor_context_window("Vendor/DeepSeek-v4") == 1048576
    assert model_settings._known_vendor_context_window is model_settings.known_vendor_context_window
    assert model_settings.known_vendor_context_window("unknown-private-model") is None


def test_local_codex_keeps_current_cli_inventory_window(cli_metadata_catalog, monkeypatch):  # noqa: F811
    f = cli_metadata_catalog
    f.record["service"]["model_alias"] = "deepseek-v4"
    lookup = Mock(return_value=1048576)
    monkeypatch.setattr(model_settings, "known_vendor_context_window", lookup, raising=False)
    row = f.catalog.get_model("local/codex-cli/model/model-a")
    assert row["context_window"] == 400000 and row["context_window_known"]
    assert row["context_window_basis"] == "installed_cli_model_cache"
    lookup.assert_not_called()


@pytest.mark.parametrize("adapter", ["codex_cli", "antigravity_cli"])
def test_peer_cli_keeps_upstream_inventory_window(tmp_path, monkeypatch, adapter):
    lookup = Mock(return_value=1048576)
    monkeypatch.setattr(model_settings, "known_vendor_context_window", lookup, raising=False)
    _, row = catalog_row(tmp_path, adapter=adapter,
        cli_models=[dict(id="actual-model", name="Actual", default=True, context_window=400000)])
    assert row["context_window"] == 400000 and row["context_window_known"]
    assert row["context_window_basis"] == "provider_cli_model_metadata"
    lookup.assert_not_called()
