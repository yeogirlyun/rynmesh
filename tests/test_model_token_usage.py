"""Unified per-model token usage & quota management tests across local API, CLI, native, and shared peer models."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from test_agent_integrations import add_source
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.model_usage import ModelUsageStore, usage_caller_scope

node = integration_node


def patch_model_config(client, record, model, **config):
    return client.put(
        f"/api/local/llm/sources/{record['id']}/models/config",
        json={
            "model_id": model,
            "expected_rule_revision": record["model_settings"][model]["rule_revision"],
            "configuration": config,
        },
    )


def test_unified_usage_mounted_on_all_model_types_and_legacy_migrated(node):
    app, store = node
    record, model_id = add_source(app, models=["deepseek-flash", "glm-5"])
    sources = app.state.llm_agent_services["source_store"]
    svc_id = sources.model_service_id(record, "deepseek-flash")
    usage_key = f"local/{svc_id}"
    today = ModelUsageStore.utc_day()
    # Pre-seed legacy model-usage.json entry (without "local/" prefix) to verify seamless migration
    sources.usage_store.path.parent.mkdir(parents=True, exist_ok=True)
    sources.usage_store.path.write_text(
        json.dumps({svc_id: {"day": today, "requests": 3}}),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        # 1. Check /api/local/llm/sources mounts unified usage
        sources_resp = client.get("/api/local/llm/sources").json()["sources"]
        src = next(item for item in sources_resp if item["id"] == record["id"])
        usage = src["model_settings"]["deepseek-flash"]["usage"]
        assert usage["usage_key"] == usage_key
        assert usage["model_key"] == usage_key
        assert usage["today"]["requests"] == 3
        assert usage["total"]["requests"] == 3
        assert "by_caller" in usage and "self" in usage["by_caller"] and "shared" in usage["by_caller"]
        assert "quota" in usage
        assert src["model_usage"]["deepseek-flash"]["usage_key"] == usage_key

        # 2. Check ModelCatalog.list_models() mounts unified usage
        agent_models = ModelCatalog(app.state.llm_agent_services, store).list_models()
        api_row = next(m for m in agent_models if m["id"] == model_id)
        assert api_row["usage"]["usage_key"] == usage_key
        assert api_row["usage"]["today"]["requests"] == 3

        # 3. Check /api/local/llm/services, /api/local/llm/api-access, and /v1/models mount unified usage
        services_resp = client.get("/api/local/llm/services").json()["services"]
        svc_row = next(s for s in services_resp if s["service"]["package_id"] == svc_id)
        assert svc_row["usage"]["usage_key"] == usage_key
        assert svc_row["usage"]["today"]["requests"] == 3

        api_access = client.get("/api/local/llm/api-access").json()
        access_row = next(m for m in api_access["models"] if m["rynmesh"].get("service_id") == svc_id)
        assert access_row["usage"]["usage_key"] == usage_key
        assert access_row["rynmesh"]["usage"]["usage_key"] == usage_key

        created_key = client.post("/api/local/llm/api-keys", json={"name": "test-key", "output_token_limit": 10000}).json()["key"]
        v1_models = client.get("/v1/models", headers={"Authorization": f"Bearer {created_key}"}).json()["data"]
        v1_row = next(m for m in v1_models if m["rynmesh"].get("service_id") == svc_id)
        assert v1_row["usage"]["usage_key"] == usage_key
        assert v1_row["rynmesh"]["usage"]["usage_key"] == usage_key


def test_token_accounting_cache_hit_rate_caller_attribution_and_shared_token_quota(node):
    app, _ = node
    record, _ = add_source(app, models=["model-a"])
    sources = app.state.llm_agent_services["source_store"]
    usage_key = f"local/{sources.model_service_id(record, 'model-a')}"

    with TestClient(app) as client:
        # Configure shared_tokens_per_day=500 and tokens_per_day=2000
        res = patch_model_config(
            client,
            record,
            "model-a",
            tokens_per_day=2000,
            shared_tokens_per_day=500,
        )
        assert res.status_code == 200, res.text
        record = res.json()
        assert record["model_settings"]["model-a"]["effective"]["tokens_per_day"] == 2000
        assert record["model_settings"]["model-a"]["effective"]["shared_tokens_per_day"] == 500

    class FakeInner:
        def chat(self, body, **kwargs):
            return {
                "text": "ok",
                "input_tokens": 400,
                "output_tokens": 200,
                "cached_tokens": 300,
            }

    adapter = ModelDispatchAdapter(sources, sources.read()[record["id"]], "model-a", FakeInner())

    # 1. Local self call consumes 600 tokens (400 in + 200 out, 300 cached)
    with usage_caller_scope("self"):
        adapter.chat({}, task_id="self-1", timeout_s=1)

    u1 = sources.usage_store.public_usage(
        usage_key,
        tokens_per_day=2000,
        shared_tokens_per_day=500,
    )
    assert u1["today"]["input_tokens"] == 400
    assert u1["today"]["output_tokens"] == 200
    assert u1["today"]["cached_tokens"] == 300
    assert u1["today"]["total_tokens"] == 600
    assert u1["today"]["cache_hit_rate"] == 0.75
    assert u1["by_caller"]["self"]["today"]["total_tokens"] == 600
    assert u1["by_caller"]["shared"]["today"]["total_tokens"] == 0
    assert u1["quota"]["tokens_remaining"] == 1400
    assert u1["quota"]["shared_tokens_remaining"] == 500

    # 2. Shared peer call consumes 600 tokens -> reaches shared_tokens_per_day=500 after completion
    with usage_caller_scope("shared"):
        adapter.chat({}, task_id="shared-1", timeout_s=1)

    u2 = sources.usage_store.public_usage(
        usage_key,
        tokens_per_day=2000,
        shared_tokens_per_day=500,
    )
    assert u2["today"]["total_tokens"] == 1200
    assert u2["by_caller"]["self"]["today"]["total_tokens"] == 600
    assert u2["by_caller"]["shared"]["today"]["total_tokens"] == 600
    assert u2["quota"]["shared_tokens_remaining"] == 0

    # 3. Second shared peer call is rejected by shared_tokens_per_day, while local self call still succeeds!
    with usage_caller_scope("shared"):
        with pytest.raises(AdapterError, match="model_daily_quota_exhausted"):
            adapter.chat({}, task_id="shared-blocked", timeout_s=1)

    with usage_caller_scope("self"):
        adapter.chat({}, task_id="self-2", timeout_s=1)

    u3 = sources.usage_store.public_usage(
        usage_key,
        tokens_per_day=2000,
        shared_tokens_per_day=500,
    )
    assert u3["today"]["total_tokens"] == 1800
    assert u3["by_caller"]["self"]["today"]["total_tokens"] == 1200
    assert u3["by_caller"]["shared"]["today"]["total_tokens"] == 600

    # 4. Reset today's usage via API and verify counters reset while total or all reset works
    with TestClient(app) as client:
        reset_today = client.post(
            "/api/local/llm/models/usage/reset",
            json={"usage_key": usage_key, "scope": "today"},
        )
        assert reset_today.status_code == 200, reset_today.text
        payload = reset_today.json()["usage"]
        assert payload["today"]["total_tokens"] == 0
        assert payload["today"]["requests"] == 0
        assert payload["total"]["total_tokens"] == 1800

        reset_all = client.post(
            "/api/local/llm/models/usage/reset",
            json={"usage_key": usage_key, "scope": "all"},
        )
        assert reset_all.status_code == 200, reset_all.text
        payload_all = reset_all.json()["usage"]
        assert payload_all["today"]["total_tokens"] == 0
        assert payload_all["total"]["total_tokens"] == 0


def test_peer_shared_model_and_cli_model_usage_and_quota(node, monkeypatch):
    app, store = node
    sources = app.state.llm_agent_services["source_store"]
    peer_id = "peer-remote-provider-123456"
    service_id = "shared-deepseek-v4"
    peer_usage_key = f"peer/{hashlib.sha256(peer_id.encode()).hexdigest()}/{service_id}"

    # Simulate a discovered peer LLM service in ModelCatalog
    fake_services = [
        {
            "peer_id": peer_id,
            "node_name": "Friend Node",
            "online": True,
            "ready": True,
            "binding_verified": True,
            "availability": {"state": "ready", "reason": "ok"},
            "service": {
                "package_id": service_id,
                "model_alias": "Friend DeepSeek",
                "capabilities": ["chat", "tools"],
                "context_window": 64000,
                "max_output_tokens": 4096,
            },
        }
    ]
    catalog = ModelCatalog(
        {
            **app.state.llm_agent_services,
            "discover": lambda _net: fake_services,
            "discover_agent": lambda _net: fake_services,
        },
        store,
    )
    models = catalog.list_models()
    peer_model = next(m for m in models if m["id"] == peer_usage_key)
    assert peer_model["usage"]["usage_key"] == peer_usage_key

    # Configure local daily token quota on the shared peer model via API
    with TestClient(app) as client:
        quota_res = client.put(
            "/api/local/llm/models/usage/quota",
            json={
                "usage_key": peer_usage_key,
                "tokens_per_day": 500,
                "requests_per_day": 2,
            },
        )
        assert quota_res.status_code == 200, quota_res.text
        assert quota_res.json()["usage"]["quota"]["tokens_per_day"] == 500
        assert quota_res.json()["usage"]["quota"]["requests_per_day"] == 2

    # Record usage against the peer model as consumer
    sources.usage_store.admit_and_record_dispatch(peer_usage_key, caller="self")
    sources.usage_store.record_completion(
        peer_usage_key,
        input_tokens=350,
        output_tokens=200,
        cached_tokens=280,
        caller="self",
    )

    updated_models = catalog.list_models()
    updated_peer = next(m for m in updated_models if m["id"] == peer_usage_key)
    assert updated_peer["usage"]["today"]["total_tokens"] == 550
    assert updated_peer["usage"]["today"]["cached_tokens"] == 280
    assert updated_peer["usage"]["quota"]["exhausted"] is True
    assert updated_peer["usage"]["quota"]["exhausted_reason"] == "tokens_per_day"

    # Also verify CLI adapter records usage on both parent service and child cli_model
    cli_adapter = CLIAgentAdapter(
        "codex_cli",
        usage_store=sources.usage_store,
        service_id="codex-cli",
    )
    monkeypatch.setattr(
        cli_adapter,
        "_chat_inner",
        lambda body, *, task_id, timeout_s, on_event=None, model="", conversation_scope=None: {
            "text": "cli answer",
            "tool_calls": [],
            "input_tokens": 120,
            "output_tokens": 80,
            "cached_tokens": 60,
        },
    )
    cli_adapter.chat(
        {"cli_model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        task_id="user-task-1",
        timeout_s=5,
    )
    parent_usage = sources.usage_store.public_usage("local/codex-cli")
    child_usage = sources.usage_store.public_usage("local/codex-cli/model/gpt-5.4")
    assert parent_usage["today"]["total_tokens"] == 200
    assert parent_usage["today"]["cached_tokens"] == 60
    assert child_usage["today"]["total_tokens"] == 200
    assert child_usage["today"]["cached_tokens"] == 60
    assert len(parent_usage["daily"]) == 1
    assert parent_usage["daily"][0]["tokens"] == 200
    assert parent_usage["daily"][0]["total_tokens"] == 200
