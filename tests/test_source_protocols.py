"""Source transport selection is part of the authorized model destination."""
import copy
import sys
from types import SimpleNamespace

import pytest

from rynmesh.llm_package import model_settings
from rynmesh.llm_package.sources import SourceAdapter, SourceStore


def configuration(**updates):
    return {"name": "Protocol source", "kind": "api", "provider": "custom",
            "base_url": "https://example.test/v1", "api_key": "test-key", "models": ["test-model"],
            **updates}


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
def test_protocol_round_trips_without_exposing_credentials(tmp_path, protocol):
    store = SourceStore(tmp_path, b"key")
    record = store.prepare(configuration(protocol=protocol))
    public = store.save(record)
    assert public["protocol"] == protocol
    assert public["has_key"] and "api_key" not in public
    assert SourceStore(tmp_path, b"key").read()[record["id"]]["protocol"] == protocol


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "", None, [], 1])
def test_unknown_protocol_is_rejected(tmp_path, protocol):
    with pytest.raises(ValueError, match="API"):
        SourceStore(tmp_path, b"key").prepare(configuration(protocol=protocol))


def test_legacy_chat_source_keeps_authorized_model_identity(tmp_path):
    store = SourceStore(tmp_path, b"key")
    legacy = store.prepare(configuration())
    store.save(legacy)
    legacy.pop("protocol")
    legacy["model_settings"]["test-model"]["evidence"] = {"tools": {"status": "supported"}}
    legacy["tool_support"] = {"test-model": "supported"}
    store._write({legacy["id"]: legacy})

    assert store.public(legacy)["protocol"] == "openai_chat"
    updated = store.prepare({}, legacy["id"])
    assert updated["protocol"] == "openai_chat"
    assert updated["model_settings"] == legacy["model_settings"]
    assert updated["tool_support"] == legacy["tool_support"]
    assert model_settings.fingerprint(updated, "test-model") == model_settings.fingerprint(legacy, "test-model")
    assert store._metadata_key(updated) == store._metadata_key(legacy)
    assert store.save(updated)["destination_revision"] == legacy["destination_revision"]
    assert isinstance(store.adapter(legacy), SourceAdapter)


def test_switching_only_protocol_invalidates_probe_and_destination_identity(tmp_path):
    store = SourceStore(tmp_path, b"key")
    old = store.prepare(configuration())
    store.save(old)
    old["model_metadata"] = {"test-model": {"context_window": 200000}}
    old["tool_support"] = {"test-model": "supported"}
    old["model_settings"]["test-model"]["evidence"] = {"tools": {"status": "supported"}}
    store._write({old["id"]: old})
    updated = store.prepare({"protocol": "anthropic_messages"}, old["id"])

    old_model = old["model_settings"]["test-model"]
    new_model = updated["model_settings"]["test-model"]
    assert new_model["destination_revision"] != old_model["destination_revision"]
    assert new_model["rule_revision"] != old_model["rule_revision"]
    assert new_model["evidence"] == {}
    assert updated["tool_support"] == {}
    assert updated["model_metadata"] == {}
    assert updated["api_key"] == old["api_key"]
    assert store._metadata_key(updated) != store._metadata_key(old)
    assert model_settings.fingerprint(updated, "test-model") != model_settings.fingerprint(old, "test-model")
    new_public = store.save(updated)
    assert new_public["destination_revision"] != old["destination_revision"]
    assert new_public["source_revision"] != old["source_revision"]


@pytest.mark.parametrize("kind", ["api", "local"])
@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages"])
def test_native_adapter_receives_explicit_protocol_and_exact_root(tmp_path, monkeypatch, kind, protocol):
    class Native:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, "rynmesh.llm_package.protocol_adapter", SimpleNamespace(NativeProtocolAdapter=Native))
    base = "https://example.test/v1" if kind == "api" else "http://127.0.0.1:1234/v1"
    record = SourceStore(tmp_path, b"key").prepare(configuration(kind=kind, protocol=protocol, base_url=base))
    adapter = SourceStore.adapter(copy.deepcopy(record))
    assert adapter.kwargs["protocol"] == protocol
    assert adapter.kwargs["base_url"] == base
    assert adapter.kwargs["api_prefix"] == ""
    assert adapter.kwargs["model"] == "test-model"
