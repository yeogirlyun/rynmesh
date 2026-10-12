"""Actual pinned reference worker with synthetic responses, no provider access."""
import importlib.util
from pathlib import Path

import pytest
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / ".codex-tmp/research/zcode-runtime-reference"
NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"


@pytest.mark.parametrize("maximum", [512, 128000])
def test_pinned_worker_requests_configured_output_cap(maximum):
    if not NODE.exists() or not (UPSTREAM / "apps/zcode-cli/packages/adapters/dist/storage/index.js").exists():
        pytest.skip("Pinned built reference and matching Node runtime required")
    spec = importlib.util.spec_from_file_location("reference_output_worker", ROOT / "docs/acceptance/ryn-agent-delivery/reference/worker_host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    models = []
    def dispatch(method, params):
        if method == "tool":
            assert params["name"] == "ryn_fixture_read"
            return {"observed": True}
        assert method == "model"
        models.append(params)
        if len(models) == 1:
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function",
                "function": {"name": "ryn_fixture_read", "arguments": "{}"}}]}}
        return {"message": {"role": "assistant", "content": "Synthetic observation reported."}}
    config = {"session_id": "output-policy-" + str(maximum), "provider_id": "synthetic", "model_id": "synthetic",
        "context_window": 1000000, "max_output_tokens": maximum, "max_model_calls": 4, "max_seconds": 30,
        "real_model": False, "prompt": "Read the synthetic fixture and report.",
        "tools": [{"type": "function", "function": {"name": "ryn_fixture_read", "description": "Synthetic read",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
        "tool_metadata": {"ryn_fixture_read": {"effect": "read"}}}
    result = module.run_worker(config, dispatch, node=NODE, upstream=UPSTREAM)
    assert result["upstream_commit"] == module.PIN
    assert len(models) == 2 and [m.get("max_output_tokens") for m in models] == [maximum, maximum]


@pytest.mark.parametrize("seconds,expected", [(600, 180), (30, 30)])
def test_pinned_worker_host_propagates_budget_bound_to_real_gate(node, tmp_path, monkeypatch, seconds, expected):  # noqa: F811
    if not NODE.exists() or not (UPSTREAM / "apps/zcode-cli/packages/adapters/dist/storage/index.js").exists():
        pytest.skip("Pinned built reference and matching Node runtime required")
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/reference"))
    documents = importlib.import_module("documents")
    app, store = node
    _, selected = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    manager = app.state.llm_agent_services["provider_for"](catalog.resolve(selected)["service_id"])
    seen = []
    def synthetic(body, *, timeout_s, **kwargs):
        seen.append(timeout_s)
        return {"message": {"role": "assistant", "content": "Synthetic response."}, "input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(manager.adapter.inner, "chat", synthetic)
    runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": selected, "max_seconds": seconds})
    task = runtime.create({"message": "Respond briefly."})
    record = {"prompt": "Respond briefly.", "approvals": []}
    documents.execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=bridge,
                      record=record, home=tmp_path, node=NODE, upstream=UPSTREAM, real_model=False)
    assert seen and all(expected - 3 < value <= expected for value in seen)
    flight = runtime.store.read()["tasks"][task["id"]]["budget"]["records"][0]
    assert flight["request_policy"]["provider_timeout_seconds"] == 180
    assert expected - 3 < flight["request_policy"]["effective_timeout_seconds"] <= expected
    assert record["reference"]["upstream_commit"] == "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
