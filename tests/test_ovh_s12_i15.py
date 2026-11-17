"""S12-I15: distinguish guessed output limits and learn a rejected cap."""
import copy
import io
import json
import sys
from types import SimpleNamespace
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest

from test_agent_integrations import add_source, fake_upstream, node  # noqa: F401
from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import model_settings


@pytest.mark.parametrize("window,known,expected", [(128000, True, 16384), (16000, True, 4000), (32768, False, 8192)])
def test_t1_t2_unknown_output_policy(window, known, expected):
    model = {"context_window": window, "context_window_known": known,
             "max_output_tokens": 4096, "max_output_tokens_known": False}
    assert output_policy.configured_output(model) == expected
    assert output_policy.context_budget(model)["reserved_output_tokens"] == expected
    assert output_policy.select(model, [], [])["effective_cap"] == expected


def test_t2_no_metadata_uses_unknown_fallback():
    assert output_policy.configured_output({}) == 8192
    assert output_policy.context_metadata({}) == {"context_window_tokens": 32768,
        "context_window_known": False, "context_window_basis": "unknown_fallback"}


@pytest.mark.parametrize("cap", [1024, 4096, 32768])
def test_t4_declared_output_is_unchanged(cap):
    assert output_policy.configured_output({"max_output_tokens": cap, "context_window": 128000}) == cap


@pytest.mark.parametrize("record,model,expected,basis", [
    ({"models": ["unlisted"]}, "unlisted", False, "unknown_fallback"),
    ({"models": ["gpt-4o"]}, "gpt-4o", True, "vendor_family_default"),
    ({"models": ["unlisted"], "max_output_tokens": 4096, "max_output_tokens_explicit": True}, "unlisted", True, "source_configuration"),
    ({"models": ["unlisted"], "model_metadata": {"unlisted": {"max_output_tokens": 6000}}}, "unlisted", True, "provider_model_metadata"),
    ({"models": ["unlisted"], "model_settings": {"unlisted": {"overrides": {"max_output_tokens": 7000}}}}, "unlisted", True, "model_override"),
])
def test_output_metadata_provenance(record, model, expected, basis):
    effective = model_settings.effective(record, model)
    assert effective["max_output_tokens_known"] is expected
    assert effective["max_output_basis"] == basis


def test_manual_window_unknown_output_keeps_revision_on_unchanged_save():
    record = {"models": ["unlisted"], "model_settings": {"unlisted": {"overrides": {"context_window": 128000}}}}
    model_settings.reconcile(record, {})
    before = copy.deepcopy(record)
    assert model_settings.effective(record, "unlisted")["max_output_tokens"] == 16384
    model_settings.reconcile(record, before)
    assert record["model_settings"]["unlisted"]["destination_revision"] == before["model_settings"]["unlisted"]["destination_revision"]
    assert record["model_settings"]["unlisted"]["rule_revision"] == before["model_settings"]["unlisted"]["rule_revision"]


def test_catalog_local_and_peer_manifest_provenance(node):  # noqa: F811
    app, store = node
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    def discovered(_network):
        return [{"peer_id": peer, "online": True, "ready": True, "chat_protocol": "rynmesh.chat.v1",
                 "service": {"package_id": "example", "adapter": "openai_compatible", **metadata}}
                for peer, metadata in [(store.peer_id, {}), ("test-peer", {"max_output_tokens": 6000, "context_window": 128000})]]
    catalog.services["discover_agent"] = discovered
    local, peer = sorted(catalog.list_models(), key=lambda r: r["source"] == "peer")
    assert local["max_output_tokens_known"] is False
    assert local["max_output_basis"] == "unknown_fallback"
    assert local["context_window_known"] is False
    assert peer["max_output_tokens_known"] is True
    assert peer["max_output_basis"] == "provider_manifest"
    assert output_policy.configured_output(local) == 8192
    assert output_policy.configured_output(peer) == 6000


@pytest.mark.parametrize("peer", [False, True])
def test_cli_models_keep_independent_output_provenance(node, monkeypatch, peer):  # noqa: F811
    from rynmesh.agent_runtime.models import ModelCatalog
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
    from rynmesh.llm_package.manifest import LLMPackageManifest
    app, store = node
    upstream = [{"id": "unlisted", "name": "Unknown", "default": True},
                {"id": "declared", "name": "Declared", "max_output_tokens": 6000, "context_window": 128000},
                {"id": "gpt-4o", "name": "Vendor", "context_window": 128000}]
    manifest = LLMPackageManifest(package_id="test-cli", public_model_alias="Test CLI", adapter="codex_cli", mode="codex_cli", capabilities=["client-tools"])
    manager = SimpleNamespace(manifest=manifest, adapter=SimpleNamespace(current_models=lambda: upstream, binding_for_model=lambda _: None))
    services = dict(app.state.llm_agent_services)
    services["provider_for"] = lambda _: manager
    services["discover_agent"] = lambda _: [{"peer_id": "test-peer" if peer else store.peer_id,
        "online": True, "chat_protocol": "rynmesh.chat.v1", "service": manifest.to_dict(), "cli_models": upstream}]
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: sys.executable)
    rows = ModelCatalog(services, store).list_models()
    children = {row["cli_model"]: row for row in rows if "cli_model" in row}
    assert children["unlisted"]["max_output_tokens_known"] is False
    assert children["unlisted"]["max_output_basis"] == "unknown_fallback"
    assert children["declared"]["max_output_tokens"] == 6000
    assert children["declared"]["max_output_basis"] == "provider_cli_model_metadata"
    assert children["gpt-4o"]["max_output_tokens"] == 16384
    assert children["gpt-4o"]["max_output_basis"] == "vendor_family_default"


@pytest.mark.parametrize("reject_retry", [False, True])
def test_t3_real_http_retries_once_and_only_learns_success(node, monkeypatch, reject_retry):  # noqa: F811
    app, store = node
    _, model_id = add_source(app)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    real_requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "ok"}] * 2, json_only=True)
    import urllib.request
    success = urllib.request.urlopen
    attempts = []
    @contextmanager
    def upstream(request, **kwargs):
        body = json.loads(request.data)
        attempts.append(copy.deepcopy(body))
        if len(attempts) == 1 or reject_retry:
            raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps({"error": {
                "type": "invalid_request_error", "param": "max_tokens", "message": "max_tokens must be at most 4096"}}).encode()))
        with success(request, **kwargs) as response:
            yield response
    monkeypatch.setattr(urllib.request, "urlopen", upstream)
    messages = [{"role": "user", "content": "same request"}]
    if reject_retry:
        with pytest.raises(AgentError):
            bridge.complete(model_id, messages, [], lambda: False)
        assert output_policy.configured_output(catalog.resolve(model_id)) == 8192
    else:
        result = bridge.complete(model_id, messages, [], lambda: False)
        assert result["message"]["content"] == "ok"
        assert result["output_policy"]["effective_cap"] == 4096
        assert catalog.resolve(model_id)["max_output_basis"] == "upstream_learned"
        assert output_policy.configured_output(catalog.resolve(model_id)) == 4096
        bridge.complete(model_id, messages, [], lambda: False)
        assert attempts[-1]["max_tokens"] == 4096
        from rynmesh.agent_runtime.models import ModelCatalog
        assert output_policy.configured_output(ModelCatalog(catalog.services, store).resolve(model_id)) == 8192
        sources = catalog.services["source_store"]
        record = sources.read()[catalog.resolve(model_id)["source_id"]]
        record.update(max_output_tokens=6000, max_output_tokens_explicit=True)
        sources.save(record)
        assert catalog.get_model(model_id)["max_output_tokens"] == 6000
        assert catalog.get_model(model_id)["max_output_basis"] == "source_configuration"
    assert [b["max_tokens"] for b in attempts[:2]] == [8192, 4096]
    assert attempts[0]["messages"] == attempts[1]["messages"]


def test_unrelated_400_never_retries(node, monkeypatch):  # noqa: F811
    app, store = node
    _, model_id = add_source(app)
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    attempts = []
    @contextmanager
    def upstream(request, **kwargs):
        attempts.append(request)
        raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":{"type":"invalid_request_error","param":"temperature","message":"invalid temperature"}}'))
        yield
    monkeypatch.setattr("urllib.request.urlopen", upstream)
    with pytest.raises(AgentError):
        bridge.complete(model_id, [{"role": "user", "content": "test"}], [], lambda: False)
    assert len(attempts) == 1
