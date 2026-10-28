"""Capacity comes from exact source metadata or owner configuration, not names."""
import copy

import pytest
from fastapi.testclient import TestClient
from test_agent_cli_models import cli_metadata_catalog as cli_metadata_catalog
from test_agent_integrations import node as node
from test_agent_peer_cli import cli_pair as cli_pair
from test_agent_peer_cli import nodes as nodes
from test_agent_peer_cli import space_pair as space_pair

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.cli_models import context_metadata
from rynmesh.llm_package.routes import _expires
from rynmesh.llm_package.sources import SourceAdapter, SourceStore
from rynmesh.llm_package.task_protocol import TaskProtocolError, open_task, seal_task
from rynmesh.services import peer_box


def body(**updates):
    return {"name": "Capacity fixture", "kind": "local", "provider": "custom",
            "base_url": "http://127.0.0.1:11434/v1", "models": ["large", "unknown"], **updates}


def test_auto_discovery_is_server_owned_exact_model_and_authenticated_endpoint_bound(tmp_path, monkeypatch):
    store = SourceStore(tmp_path, b"authored-key")
    listing = [{"id": "large", "context_length": 1048576}, {"id": "unknown"},
               {"id": "bool", "context_window": True}, {"id": "huge", "context_window": 10**20}]
    monkeypatch.setattr(SourceAdapter, "models", lambda _: copy.deepcopy(listing))
    monkeypatch.setattr(SourceAdapter, "chat", lambda *a, **kw: pytest.fail("metadata discovery must not infer"))
    draft = store.prepare(body(model_metadata={"unknown": {"context_window": 2000000}}))
    assert draft["context_window_mode"] == "auto"
    assert model_settings.context_metadata(draft, "unknown")["context_window_known"] is False
    discovered = store.probe(draft, models_only=True)
    assert discovered["model_metadata"] == {"large": {"context_window": 1048576}}
    record = store.prepare(body())
    store.save(record)
    assert model_settings.effective(record, "large")["context_window"] == 1048576
    assert model_settings.public(record, "large")["context_window_basis"] == "provider_model_metadata"
    assert model_settings.context_metadata(record, "unknown") == {"context_window": 32768,
        "context_window_basis": "unknown_fallback", "context_window_known": False}
    changed = store.prepare(body(base_url="http://127.0.0.1:12345/v1"), record["id"])
    assert model_settings.context_metadata(changed, "large")["context_window_known"] is False
    rotated_key = store.prepare({"api_key": "authored-different-credential"}, record["id"])
    assert model_settings.context_metadata(rotated_key, "large")["context_window_known"] is False
    rebuilt = SourceStore(tmp_path, b"authored-key")
    assert model_settings.effective(rebuilt.read()[record["id"]], "large")["context_window"] == 1048576
    monkeypatch.setattr(SourceAdapter, "models", lambda _: [{"id": "large"}, {"id": "unknown"}])
    observed = rebuilt.read()[record["id"]]
    rebuilt.probe(observed, models_only=True)
    assert model_settings.effective(observed, "large")["context_window"] == 1048576
    manual = rebuilt.prepare({"context_window": 131072}, record["id"])
    assert manual["context_window_mode"] == "manual"
    assert model_settings.effective(manual, "large")["context_window"] == 131072
    explicit_auto = rebuilt.prepare({"context_window_mode": "auto", "context_window": 40960}, record["id"])
    rebuilt.save(explicit_auto)
    assert explicit_auto["context_window_mode"] == "auto"
    assert model_settings.effective(explicit_auto, "large")["context_window"] == 1048576
    internal_edit = rebuilt.read()[record["id"]]
    internal_edit["context_window"] = 262144
    rebuilt.save(internal_edit)
    assert internal_edit["context_window_mode"] == "manual"
    assert model_settings.effective(internal_edit, "large")["context_window"] == 262144


def test_manual_legacy_32k_and_per_model_overrides_are_never_silently_replaced(tmp_path, monkeypatch):
    store = SourceStore(tmp_path, b"key")
    record = store.prepare(body(context_window=32768))
    store.save(record)
    # An old source with no provenance must remain an owner limit, even at 32k.
    old = store.read()
    old[record["id"]].pop("context_window_mode")
    store._write(old)
    record = store.read()[record["id"]]
    monkeypatch.setattr(SourceAdapter, "models", lambda _: [{"id": "large", "context_window": 1050000}])
    store.probe(record, models_only=True)
    store.save(record)
    assert model_settings.effective(record, "large")["context_window"] == 32768
    public = store.configure_model(record["id"], {"model_id": "large",
        "expected_rule_revision": record["model_settings"]["large"]["rule_revision"],
        "configuration": {"context_window": 65536}})
    assert public["model_settings"]["large"]["effective"]["context_window"] == 65536
    assert public["model_settings"]["large"]["context_window_basis"] == "model_override"


def test_real_source_api_listing_then_save_and_model_listing_refresh_reach_agent_catalog(node, monkeypatch):
    app, owner = node
    window = [400000]
    monkeypatch.setattr(SourceAdapter, "models", lambda _: [{"id": "large", "context_window": window[0]}, {"id": "unknown"}])
    monkeypatch.setattr(SourceAdapter, "chat", lambda *a, **kw: pytest.fail("no paid inference"))
    with TestClient(app) as client:
        discovered = client.post("/api/local/llm/sources/models", json=body())
        assert discovered.status_code == 200 and discovered.json()["model_metadata"]["large"]["context_window"] == 400000
        saved = client.put("/api/local/llm/sources/new", json=body()).json()
        source_id = saved["id"]
        old_revision = saved["model_settings"]["large"]["destination_revision"]
        _, _, catalog = build_agent_integrations(app, owner, owner.home)
        identifier = "local/" + SourceStore.model_service_id(saved, "large")
        row = catalog.get_model(identifier)
        assert row["context_window"] == 400000 and row["context_window_known"]
        window[0] = 1050000
        refreshed = client.post(f"/api/local/llm/sources/{source_id}/models/test", json={"model_id": "large", "probe": "model_exists"})
        assert refreshed.status_code == 200, refreshed.text
        actual = refreshed.json()["source"]
        assert actual["model_settings"]["large"]["effective"]["context_window"] == 1050000
        assert actual["model_settings"]["large"]["destination_revision"] != old_revision
        assert catalog.get_model(identifier)["context_window"] == 1050000


def test_cli_default_and_child_both_use_selected_installed_metadata(cli_metadata_catalog):
    fixture = cli_metadata_catalog
    rows = {row["id"]: row for row in fixture.catalog.list_models()}
    for identifier in ("local/codex-cli", "local/codex-cli/model/model-a"):
        assert rows[identifier]["context_window"] == 400000
        assert rows[identifier]["context_window_known"] is True
        assert rows[identifier]["context_window_basis"] == "installed_cli_model_cache"


@pytest.mark.parametrize("value", [None, True, "1050000", 10**20, -1])
def test_invalid_cli_capacity_cannot_inflate_an_unrelated_model(value):
    rows = [{"id": "large", "default": True, "context_window": 1050000},
            {"id": "unknown", "context_window": value}]
    assert context_metadata(rows, "unknown") == {"context_window": 32768,
        "context_window_known": False, "context_window_basis": "unknown_fallback"}
    assert context_metadata(rows, "missing")["context_window_known"] is False


def test_signed_cli_provider_uses_selected_window_not_32k_or_caller_capacity(cli_pair, monkeypatch):
    p = cli_pair
    models = [{"id": "gemini-3.8-flash-medium", "name": "Large", "default": True, "context_window": 262144},
              {"id": "unverified", "name": "Unknown", "context_window": 10**20}]
    from rynmesh.llm_package import cli_models
    # Both the provider's capacity gate and binding derive from installed listing.
    monkeypatch.setattr(cli_models, "list_models", lambda *a, **kw: copy.deepcopy(models))
    p.provider.manifest.context_window = 32768
    def wire(model, task_id):
        return seal_task(body={"task_id": task_id, "service_id": p.provider.manifest.package_id,
            "chat": {"messages": [{"role": "user", "content": "x" * 200000}]}, "max_tokens": 32, "max_amount": 1,
            "cli_model": model, "context_window": 2000000,
            "reply_messaging_pub": peer_box.public_key_b64(p.keys[1])}, task_id=task_id,
            kind="llm_request", sender_peer_id=p.child.store.peer_id, recipient_peer_id=p.root.store.peer_id,
            sender_signing_key=p.child.store.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(p.keys[0]), expires_at=_expires(300)).to_dict()
    response = open_task(p.provider.handle(wire("gemini-3.8-flash-medium", "large-context")),
        recipient_peer_id=p.child.store.peer_id, recipient_messaging_key=p.keys[1], expected_kind="llm_response")[1]
    assert response["state"] == "succeeded" and len(p.calls) == 1
    with pytest.raises(TaskProtocolError, match="context window"):
        p.provider.handle(wire("unverified", "small-context"))
    assert len(p.calls) == 1
