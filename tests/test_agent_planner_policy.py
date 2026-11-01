"""Synthetic planner mapping and reference parity, never owner or live access."""
import base64
import copy
import importlib
import json
from pathlib import Path

import pytest
from test_agent_integrations import fake_upstream, node  # noqa: F401
from test_agent_query_cases import module as query_module

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def planner(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    helper = importlib.import_module("planner_policy")
    home = tmp_path / "synthetic-owner"
    home.mkdir()
    key = b"synthetic-owner-key-for-test-only"
    (home / "identity.ed25519").write_text(base64.b64encode(key).decode())
    sources = SourceStore(home, key, migrate=False)
    record = sources.prepare({"name": "Synthetic GLM", "kind": "local", "base_url": "http://127.0.0.1:9999/v1",
        "models": [helper.MODEL_ID], "api_key": "PRIVATE_OWNER_KEY"})
    record.update(id=helper.SOURCE_ID, status="ready")
    record["model_settings"][helper.MODEL_ID]["overrides"] = {"context_window": 1000000, "max_output_tokens": 128000,
        "request_defaults": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}}
    sources.save(record)
    before = sources.path.read_bytes()
    source, adapter, policy = helper.read_planner(home, helper.SOURCE_ID)
    assert sources.path.read_bytes() == before
    assert "PRIVATE_OWNER_KEY" not in json.dumps(policy) and "127.0.0.1" not in json.dumps(policy)
    assert policy["generation"]["max_output_tokens"] == 128000
    return helper, policy, source, adapter


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_mapping_precedes_snapshot_and_real_outer_gate_enforces_it(node, tmp_path, monkeypatch, planner, cohort):  # noqa: F811
    app, store = node
    helper, policy, _, adapter = planner
    with query_module.QueryWorld(tmp_path / cohort, "Q01") as world:
        world.planner_policy = policy
        selected = world.configure(app, store)
        helper.assert_mapping(world.sources, selected, policy)
        before = world.sources.path.read_bytes()
        mapping = world.planner_policy_mapping[0]
        assert mapping["before"]["max_output_tokens"] == 4096 and mapping["after"] == policy["generation"]
        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        row = catalog.resolve(selected)
        assert row["max_output_tokens"] == 128000 and row["context_window"] == 1000000
        for logical, expected in world.world["models"].items():
            actual = next(r for r in catalog.list_models() if r["id"] == world.bindings[logical])
            assert tuple(actual[k] for k in ("available", "agent_supported", "sharing")) == tuple(expected[k] for k in ("available", "agent_supported", "sharing"))
        manager = app.state.llm_agent_services["provider_for"](selected[6:])
        assert isinstance(manager.adapter, ModelDispatchAdapter)
        gate = manager.adapter
        gate.inner = adapter
        requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "ok"}], json_only=True)
        result = bridge.complete(selected, [{"role": "user", "content": "test"}], [], lambda: False)
        assert requests[0]["max_tokens"] == 128000
        assert requests[0]["thinking"] == {"type": "enabled"} and requests[0]["reasoning_effort"] == "max"
        assert result["output_policy"]["effective_cap"] == 128000 and world.sources.path.read_bytes() == before
        # Authorization runs before the transport, even with mapped generation.
        def denied():
            raise ValueError("revoked")
        with pytest.raises(AdapterError, match="ai_permission_denied"):
            gate.call_bound("chat", gate.model_binding, denied, {"messages": [{"role": "user", "content": "x"}], "max_tokens": 128000}, task_id="deny", timeout_s=1)
        assert len(requests) == 1
        world.sources._active_models[gate.service_id] = 1
        try:
            with pytest.raises(AdapterError, match="runtime_busy"):
                gate.chat({"messages": [{"role": "user", "content": "x"}], "max_tokens": 128000}, task_id="busy", timeout_s=1)
        finally:
            world.sources._active_models[gate.service_id] = 0
        limited = world.sources.read()[row["source_id"]]
        limited["model_settings"]["alpha"]["overrides"]["requests_per_day"] = 1
        world.sources.save(limited)
        quota_gate = ModelDispatchAdapter(world.sources, limited, "alpha", adapter)
        with pytest.raises(AdapterError, match="model_daily_quota_exhausted"):
            quota_gate.chat({"messages": [{"role": "user", "content": "x"}], "max_tokens": 128000}, task_id="quota", timeout_s=1)
        assert len(requests) == 1
        saved = world.sources.read()[row["source_id"]]
        saved["model_settings"]["alpha"]["overrides"]["max_output_tokens"] = 9000
        world.sources.save(saved)
        with pytest.raises(RuntimeError, match="planner_fixture_policy_changed"):
            helper.assert_mapping(world.sources, selected, policy)
        with pytest.raises(AdapterError, match="model_configuration_changed"):
            gate.chat({"messages": [{"role": "user", "content": "x"}], "max_tokens": 128000}, task_id="stale", timeout_s=1)
        assert len(requests) == 1


@pytest.mark.parametrize("requested", [None, 6000, 256000])
def test_reference_documents_dispatch_honors_same_policy_and_lower_request(node, tmp_path, monkeypatch, planner, requested):  # noqa: F811
    app, store = node
    _, policy, _, adapter = planner
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/reference"))
    documents = importlib.import_module("documents")
    with query_module.QueryWorld(tmp_path / "reference", "Q01") as world:
        world.planner_policy = policy
        selected = world.configure(app, store)
        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        manager = app.state.llm_agent_services["provider_for"](selected[6:])
        manager.adapter.inner = adapter
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")), bridge, tools, catalog)
        runtime.update_settings({"enabled": True, "model_id": selected, "max_context_chars": 100000})
        task = runtime.create({"message": "Inspect"})
        requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "ok"}], json_only=True)
        configs = []
        def worker(config, dispatch, **kwargs):
            configs.append(copy.deepcopy(config))
            response = dispatch("model", {"messages": [{"role": "user", "content": "Inspect"}], "tools": [], "max_output_tokens": requested})
            return {"output_policy": response["output_policy"]}
        monkeypatch.setattr(documents, "run_worker", worker)
        record = {"prompt": "Inspect", "approvals": []}
        documents.execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=bridge,
            record=record, home=tmp_path, node=tmp_path, upstream=tmp_path)
        assert configs[0]["max_output_tokens"] == 128000
        assert requests[0]["max_tokens"] == min(requested or 128000, 128000)
        assert record["reference"]["output_policy"]["effective_cap"] == requests[0]["max_tokens"]
        state = runtime.store.read()["tasks"][task["id"]]["budget"]["records"][0]
        assert state["output_policy"]["effective_cap"] == requests[0]["max_tokens"]


def test_policy_uses_effective_overrides_and_hash_excludes_credentials(planner, tmp_path):
    helper, policy, source, _ = planner
    assert model_settings.effective(source, helper.MODEL_ID)["max_output_tokens"] == policy["generation"]["max_output_tokens"]
    assert source["max_output_tokens"] == 4096
    with pytest.raises(ValueError, match="configured_glm_source_required"):
        helper.read_planner(tmp_path, "some-other-source")
