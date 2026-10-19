"""Current installed CLI metadata binds admission; no real inference or owner state."""
import copy
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_cli_models import cli_metadata_catalog as cli_metadata_catalog

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.cli_binding import CLIModelDispatchAdapter
from rynmesh.llm_package.sources import SourceAdapter, SourceStore


def test_capacity_change_rotates_only_affected_model_and_default_and_never_resurrects(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.models.append({"id": "model-b", "name": "B", "context_window": 128000})
    adapter = fixture.manager.adapter
    before = {model: adapter.binding_for_model(model) for model in ("", "model-a", "model-b")}
    fixture.models[0]["context_window"] = 16000
    fixture.expire()
    after = {model: adapter.binding_for_model(model) for model in before}
    assert after[""] != before[""]
    assert after["model-a"] != before["model-a"]
    assert after["model-b"] == before["model-b"]
    restarted = CLIModelDispatchAdapter(CLIAgentAdapter("codex_cli"), fixture.manager.manifest, adapter.path.parent)
    assert restarted.binding_for_model("model-a") == after["model-a"]
    fixture.models[0]["context_window"] = 400000
    fixture.expire()
    assert adapter.binding_for_model("model-a") not in (before["model-a"], after["model-a"])
    assert adapter.binding_for_model("model-b") == before["model-b"]


def test_default_model_selection_is_bound_without_rotating_unchanged_children(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    fixture.models.append({"id": "model-b", "name": "B", "context_window": 16000})
    adapter = fixture.manager.adapter
    before = {model: adapter.binding_for_model(model) for model in ("", "model-a", "model-b")}
    fixture.models[0]["default"], fixture.models[1]["default"] = False, True
    fixture.expire()
    assert adapter.binding_for_model() != before[""]
    assert adapter.context_metadata_for_model()["context_window"] == 16000
    for model in ("model-a", "model-b"):
        assert adapter.binding_for_model(model) == before[model]


def test_expired_discovery_failure_revokes_selected_and_default_authority(cli_metadata_catalog, monkeypatch):
    from rynmesh.llm_package import codex_session
    fixture = cli_metadata_catalog
    model = "local/codex-cli/model/model-a"
    assert fixture.catalog.get_model(model)["context_window"] == 400000
    fixture.expire()
    def unavailable(*args):
        raise AdapterError("fixture metadata unavailable")
    monkeypatch.setattr(codex_session, "list_models", unavailable)
    assert fixture.catalog.get_model(model) is None
    default = fixture.catalog.get_model("local/codex-cli")
    assert default["available"] is False
    assert default["destination_binding_supported"] is False
    with pytest.raises(AdapterError):
        fixture.manager.adapter.binding_for_model("model-a")


def test_passive_binding_never_discovers_or_reuses_expired_capacity(cli_metadata_catalog, monkeypatch):
    from rynmesh.llm_package import codex_session
    fixture = cli_metadata_catalog
    adapter = fixture.manager.adapter
    assert adapter.cached_binding_for_model("model-a") is None
    binding = adapter.binding_for_model("model-a")
    assert adapter.cached_binding_for_model("model-a") == binding
    fixture.expire()
    monkeypatch.setattr(codex_session, "list_models", lambda *_: pytest.fail("passive catalog must never discover"))
    assert adapter.cached_binding_for_model() is None
    assert adapter.cached_binding_for_model("model-a") is None


def test_empty_refreshed_listing_closes_default_dispatch_without_reusing_old_binding(cli_metadata_catalog, monkeypatch):
    fixture = cli_metadata_catalog
    adapter = fixture.manager.adapter
    before = adapter.binding_for_model()
    fixture.models.clear()
    fixture.expire()
    calls = []
    monkeypatch.setattr(adapter.inner, "chat", lambda *a, **kw: calls.append(True))
    with pytest.raises(AdapterError, match="cli_model_unavailable"):
        adapter.call_bound("chat", before, lambda: None, {"messages": [{"role": "user", "content": "hello"}]})
    row = fixture.catalog.get_model("local/codex-cli")
    assert row["available"] is False and row["destination_binding_supported"] is False
    assert adapter.cached_binding_for_model() is None
    assert calls == []


def test_old_binding_and_new_small_window_both_stop_local_dispatch(cli_metadata_catalog, monkeypatch):
    fixture = cli_metadata_catalog
    identifier = "local/codex-cli/model/model-a"
    old = fixture.catalog.get_model(identifier)
    fixture.models[0]["context_window"] = 16000
    fixture.expire()
    calls = []
    monkeypatch.setattr(fixture.manager.adapter.inner, "chat", lambda *a, **kw: calls.append(True))
    fixture.manager._slots = threading.BoundedSemaphore(1)
    fixture.manager._lock, fixture.manager._running = threading.Lock(), 0
    bridge = AgentModelBridge({"provider_for": lambda _: fixture.manager}, fixture.catalog)
    with pytest.raises(AgentError):
        bridge.complete(identifier, [{"role": "user", "content": "x" * 60000}], [], lambda: False,
                        expected_destination=fixture.catalog.destination(old))
    current = fixture.catalog.get_model(identifier)
    with pytest.raises(AgentError):
        bridge.complete(identifier, [{"role": "user", "content": "x" * 60000}], [], lambda: False,
                        expected_destination=fixture.catalog.destination(current))
    assert calls == []


@pytest.mark.parametrize("window", [None, True, "2000000", 10**20])
def test_invalid_capacity_replaces_prior_known_window_with_unknown(cli_metadata_catalog, window):
    fixture = cli_metadata_catalog
    old = fixture.manager.adapter.binding_for_model("model-a")
    fixture.models[0]["context_window"] = window
    fixture.expire()
    row = fixture.catalog.get_model("local/codex-cli/model/model-a")
    assert row["context_window"] == 32768
    assert row["context_window_known"] is False
    assert row["local_model_binding"] != old


def _source_body(**updates):
    return {"name": "Fixture", "kind": "local", "provider": "custom",
            "base_url": "http://127.0.0.1:11434/v1", "models": ["large"], **updates}


@pytest.mark.parametrize("field", ["context_window", "max_output_tokens"])
@pytest.mark.parametrize("value", [True, 10000.5, float("nan"), float("inf")])
def test_source_limits_reject_nonintegers_without_truncation(tmp_path, field, value):
    store = SourceStore(tmp_path, b"fixture")
    with pytest.raises(ValueError):
        store.prepare(_source_body(**{field: value}))


def test_auto_output_validates_every_effective_model_and_unknown_fallback(tmp_path, monkeypatch):
    store = SourceStore(tmp_path, b"fixture")
    monkeypatch.setattr(SourceAdapter, "models", lambda _: [{"id": "large", "context_window": 1048576}])
    record = store.prepare(_source_body())
    store.probe(record, models_only=True)
    store.save(record)
    assert store.prepare({"max_output_tokens": 128000}, record["id"])["max_output_tokens"] == 128000
    with pytest.raises(ValueError, match="output_exceeds_context"):
        store.prepare({"models": ["large", "unknown"], "max_output_tokens": 128000}, record["id"])
    with pytest.raises(ValueError):
        store.prepare({"context_window": 32768, "max_output_tokens": 128000}, record["id"])
    legacy = store.prepare(_source_body(context_window="65536", max_output_tokens="8192"))
    assert legacy["context_window"] == 65536 and legacy["max_output_tokens"] == 8192


def test_public_api_rechecks_cli_capacity_and_does_not_dispatch_old_large_window(cli_metadata_catalog, monkeypatch, tmp_path):
    fixture = cli_metadata_catalog
    manager = fixture.manager
    manager._slots, manager._lock, manager._running = threading.BoundedSemaphore(1), threading.Lock(), 0
    manager.context_window_for_model = lambda model="": manager.adapter.context_metadata_for_model(model)["context_window"]
    monkeypatch.setattr(manager.adapter.inner, "health", lambda: {"ok": True})
    calls = []
    def answer(body, **kwargs):
        calls.append(copy.deepcopy(body))
        return {"text": "ok", "message": {"role": "assistant", "content": "ok"},
                "input_tokens": 2, "output_tokens": 1, "duration_ms": 1}
    monkeypatch.setattr(manager.adapter.inner, "chat", answer)
    app = FastAPI()
    install_inference_api(app, home=tmp_path / "api", store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: manager, discover=lambda _: [], execute_order=None, cancel_order=None)
    with TestClient(app) as client:
        key = client.post("/api/local/llm/api-keys", json={"name": "fixture"}).json()["key"]
        headers = {"Authorization": "Bearer " + key}
        request = {"model": "local/codex-cli", "messages": [{"role": "user", "content": "x" * 150000}], "max_tokens": 8}
        assert client.post("/v1/chat/completions", headers=headers, json=request).status_code == 200
        fixture.models[0]["context_window"] = 16000
        fixture.expire()
        rejected = client.post("/v1/chat/completions", headers=headers, json=request)
        assert rejected.status_code == 400 and "context window" in rejected.text
        assert len(calls) == 1
