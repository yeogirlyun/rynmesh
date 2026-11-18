"""Tests for S01-I17: closed context overflow detail, local API CJK token admission & loopback Host allowlist, and bridge overflow mapping (T3-T5)."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_peer_binding import paired, request as make_sealed_peer_request  # noqa: F401
from test_agent_resources import resources  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.llm_package.peer_binding import expected as expected_peer_binding
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.task_protocol import TaskProtocolError
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_t3_routes_all_three_admission_rejections_use_closed_code_and_cli_per_model_window(
    paired, tmp_path, monkeypatch  # noqa: F811
) -> None:
    """T3 (covers Steps 2, 3, 6): ProviderService._handle_opened, execute_local_cli, and execute_order all reject overflow with 'estimated_context_window_exceeded', and CLI models still use per-model context_window."""
    p = paired
    p.providers["a"].manifest.context_window = 512
    p.providers["a"].manifest.max_output_tokens = 128
    p.consumer.refresh(p.alice.peer_id)

    # 1) ProviderService._handle_opened rejection -> TaskProtocolError("estimated_context_window_exceeded")
    catalog_row = p.consumer.refresh(p.alice.peer_id)["services"][0]
    bound = expected_peer_binding(catalog_row, p.bob.peer_id)
    signed_req = make_sealed_peer_request(p, "t3_provider_reject", bound, "中" * 600)
    with pytest.raises(TaskProtocolError) as prov_exc:
        p.providers["a"].handle(signed_req)
    assert str(prov_exc.value) == "estimated_context_window_exceeded"

    # 2) execute_local_cli rejection + Step 6 per-model CLI window check
    from rynmesh.llm_package import cli_models

    home = tmp_path / "cli_node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "cli_net")
    mkey = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=mkey,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    cli_manifest = LLMPackageManifest(
        package_id="codex-cli",
        mode="codex_cli",
        adapter="codex_cli",
        runtime="external",
        public_model_alias="Codex CLI",
        capabilities=["text-generation", "client-tools", "context-estimate:v2"],
        context_window=128000,
        max_output_tokens=1024,
    )
    mpath = home / "llm" / "codex-cli" / "manifest.json"
    save_manifest(cli_manifest, mpath)
    monkeypatch.setattr("rynmesh.llm_package.routes.connect_cli", lambda **_: {"manifest": str(mpath)})
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "C:/bin/codex.exe")
    monkeypatch.setattr(
        cli_models,
        "list_models",
        lambda *_a, **_kw: [
            {"id": "small-cli", "name": "Small CLI", "default": True, "context_window": 600},
            {"id": "large-cli", "name": "Large CLI", "default": False, "context_window": 16000},
        ],
    )
    monkeypatch.setattr(
        CLIAgentAdapter,
        "infer",
        lambda *_a, **_kw: {"text": "ok", "input_tokens": 10, "output_tokens": 2, "duration_ms": 1},
    )
    try:
        with TestClient(app) as client:
            assert client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200
            # S01-I42: admission reads the inventory snapshot; the background worker fills it.
            app.state.llm_agent_services["provider_for"]("codex-cli").adapter.refresh_inventory()
            # Small CLI model (window=600) rejects 700 CJK chars with closed code
            small_resp = client.post(
                "/api/local/llm/orders",
                json={
                    "provider_peer_id": store.peer_id,
                    "service_id": "codex-cli",
                    "cli_model": "small-cli",
                    "prompt": "汉" * 700,
                    "max_tokens": 64,
                },
            )
            assert small_resp.status_code == 400
            assert small_resp.json()["detail"] == "estimated_context_window_exceeded"

            # Large CLI model (window=16000) admits the exact same 700 CJK prompt (Step 6)
            large_resp = client.post(
                "/api/local/llm/orders",
                json={
                    "provider_peer_id": store.peer_id,
                    "service_id": "codex-cli",
                    "cli_model": "large-cli",
                    "prompt": "汉" * 700,
                    "max_tokens": 64,
                },
            )
            assert large_resp.status_code == 200, large_resp.text

        # 3) Consumer execute_order pre-dispatch rejection -> HTTPException(400, "estimated_context_window_exceeded")
        bob_store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "bob_net")
        bob_app = FastAPI()
        install_ai_access(bob_app, home=bob_store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
        bob_app.state.ai_access.catalog = p.consumer
        install_llm_routes(
            bob_app,
            store=bob_store,
            home=bob_store.home,
            messaging_key=p.bob.messaging_private,
            resolve_endpoint=lambda _: "",
            resolve_pubkey=lambda _: "",
        )
        try:
            with pytest.raises(HTTPException) as consumer_exc:
                asyncio.run(
                    bob_app.state.llm_agent_services["execute_order"](
                        {
                            "network_id": "rynmesh-main",
                            "provider_peer_id": p.alice.peer_id,
                            "service_id": p.providers["a"].manifest.package_id,
                            "prompt": "超" * 600,
                            "max_tokens": 64,
                            "expected_model_binding": bound,
                        }
                    )
                )
            assert consumer_exc.value.status_code == 400
            assert consumer_exc.value.detail == "estimated_context_window_exceeded"
        finally:
            bob_app.state.llm_shutdown()
    finally:
        app.state.llm_shutdown()


def test_t4_local_inference_api_chinese_token_estimate_and_loopback_host_whitelist(tmp_path) -> None:
    """T4 (covers Steps 4, 5): Local inference API uses estimate_context_safety_tokens for CJK, rejects overflow with closed code, and rejects Host=testserver with 403 while allowing 127.0.0.1/localhost."""
    seen_bodies: list[dict] = []

    class DummyAdapter:
        def health(self):
            return {"ok": True}

        def chat(self, body, **_kwargs):
            seen_bodies.append(dict(body))
            return {
                "text": "ok",
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
                "input_tokens": 100,
                "output_tokens": 2,
                "reported_usage": {"input_tokens": 100, "output_tokens": 2},
                "duration_ms": 1,
            }

    manager = SimpleNamespace(
        adapter=DummyAdapter(),
        manifest=LLMPackageManifest(
            package_id="local-cjk",
            mode="managed",
            public_model_alias="Local CJK",
            context_window=4096,
            max_output_tokens=512,
        ),
        _slots=threading.BoundedSemaphore(2),
        _lock=threading.Lock(),
        _running=0,
    )
    app = FastAPI()
    install_inference_api(
        app,
        home=tmp_path,
        store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: manager,
        discover=lambda _net: [
            {
                "peer_id": "self",
                "online": True,
                "ready": True,
                "service": {
                    "package_id": "local-cjk",
                    "model_alias": "Local CJK",
                    "adapter": "openai_compatible",
                    "capabilities": ["text-generation", "context-estimate:v2"],
                    "context_window": 4096,
                    "max_output_tokens": 512,
                },
            }
        ],
        execute_order=None,
        cancel_order=None,
    )

    # Default TestClient uses base_url="http://testserver" -> Host: testserver -> MUST be 403!
    with TestClient(app) as default_client:
        key = default_client.post(
            "/api/local/llm/api-keys", json={"name": "k1", "output_token_limit": 100000}
        ).json()["key"]
        forbidden = default_client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        assert forbidden.status_code == 403
        assert "inference_api_is_loopback_only" in forbidden.text

    # Loopback base_url="http://127.0.0.1" and Host=localhost succeed!
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"Authorization": f"Bearer {key}"}
        assert client.get("/v1/models", headers=headers).status_code == 200
        assert client.get("/v1/models", headers={**headers, "Host": "localhost"}).status_code == 200

        # 2500 Chinese chars (~2520 tokens < 4096) -> admitted (200)
        ok_resp = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "local/local-cjk", "messages": [{"role": "user", "content": "文" * 2500}]},
        )
        assert ok_resp.status_code == 200, ok_resp.text

        # 4200 Chinese chars: old chars/4 heuristic counted ~1060 (< 4096, falsely admitted!),
        # whereas new estimate_context_safety_tokens counts ~4215 (> 4096) -> 400 estimated_context_window_exceeded!
        over_resp = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "local/local-cjk", "messages": [{"role": "user", "content": "文" * 4200}]},
        )
        assert over_resp.status_code == 400
        err_payload = over_resp.json()
        assert err_payload["error"]["message"] == "estimated_context_window_exceeded"
        assert err_payload["error"]["code"] == "estimated_context_window_exceeded"
        assert len(seen_bodies) == 1


def test_t5_bridge_maps_estimated_context_window_exceeded_to_context_overflow(
    paired, resources, tmp_path  # noqa: F811
) -> None:
    """T5 (covers Step 3): AgentModelBridge maps estimated_context_window_exceeded (both HTTPException and peer result error_code) to agent_model_context_overflow."""
    p = paired
    p.providers["a"].manifest.context_window = 2000
    p.providers["a"].manifest.max_output_tokens = 64
    p.consumer.refresh(p.alice.peer_id)

    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    app.state.nas_plugin = resources[1]
    install_llm_routes(
        app,
        store=store,
        home=store.home,
        messaging_key=p.bob.messaging_private,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    bridge, _tools, catalog = build_agent_integrations(app, store, store.home)
    peer_model = next(row for row in catalog.list_models() if row["source"] == "peer")
    dest = catalog.destination(peer_model)
    orig_exec = app.state.llm_agent_services["execute_order"]
    try:
        # 1) Real execute_order raising HTTPException(400, "estimated_context_window_exceeded") -> agent_model_context_overflow
        async def shrink_then_exec(req, *args):
            p.providers["a"].manifest.context_window = 512
            p.consumer.refresh(p.alice.peer_id)
            return await orig_exec(req, *args)

        app.state.llm_agent_services["execute_order"] = shrink_then_exec
        with pytest.raises(AgentError) as exc_http:
            bridge.complete(
                peer_model["id"],
                [{"role": "user", "content": "超" * 400}],
                [],
                lambda: False,
                expected_destination=dest,
            )
        assert str(exc_http.value) == "agent_model_context_overflow"

        p.providers["a"].manifest.context_window = 2000
        p.consumer.refresh(p.alice.peer_id)

        # 2) Peer failed order returning state="failed", error_code="estimated_context_window_exceeded" -> agent_model_context_overflow
        async def failed_order_exec(_req, *_args):
            return {"state": "failed", "error_code": "estimated_context_window_exceeded"}

        app.state.llm_agent_services["execute_order"] = failed_order_exec
        try:
            with pytest.raises(AgentError) as exc_res:
                bridge.complete(
                    peer_model["id"],
                    [{"role": "user", "content": "short"}],
                    [],
                    lambda: False,
                    expected_destination=dest,
                )
            assert str(exc_res.value) == "agent_model_context_overflow"
        finally:
            app.state.llm_agent_services["execute_order"] = orig_exec
    finally:
        app.state.llm_shutdown()
