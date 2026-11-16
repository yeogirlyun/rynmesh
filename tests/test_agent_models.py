"""Tests for S01-I17 peer context-estimate:v2 capability advertisement and agent model admission density (T6, T7)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI

from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.llm_package import lifecycle
from rynmesh.llm_package.native_model import NativeModel
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_t6_peer_with_context_estimate_v2_uses_3bytes_per_token(tmp_path, monkeypatch) -> None:
    """T6 (covers Step 7): Providers advertise context-estimate:v2; peers with context-estimate:v2 omit context_admission_bytes_per_token and budget at 3 bytes/token."""
    nm = NativeModel(tmp_path / "native_home")
    assert "context-estimate:v2" in nm.manifest.capabilities

    store = RynmeshStore(home=tmp_path / "node_home", network_dir=tmp_path / "net")
    mkey = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=store.home,
        messaging_key=mkey,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    try:
        src_store = app.state.llm_agent_services["source_store"]
        rec = src_store.prepare(
            {
                "name": "Local Src",
                "kind": "local",
                "base_url": "http://127.0.0.1:18999/v1",
                "api_key": "sk-local",
                "models": ["m1"],
                "sharing": True,
                "shared_models": ["m1"],
            }
        )
        rec.update(status="ready", tool_support={"m1": "supported"})
        src_store.save(rec)
        mgrs = app.state.llm_agent_services["active_source_managers"]()
        assert mgrs and all("context-estimate:v2" in m.manifest.capabilities for m in mgrs.values())

        monkeypatch.setattr(
            lifecycle,
            "adapter_from_manifest",
            lambda _m: SimpleNamespace(
                health=lambda: {"ok": True, "model": "demo"},
                capabilities=lambda: {"streaming": True},
                models=lambda: [{"id": "demo", "context_window": 16384}],
            ),
        )
        monkeypatch.setattr(lifecycle, "self_test", lambda _m: {"ok": True})
        local_api_out = lifecycle.connect_local_api(
            base_url="http://127.0.0.1:18999/v1",
            package_id="local-demo",
            alias="Local Demo",
            root=tmp_path / "local_pkg",
        )
        saved_local_manifest = json.loads(open(local_api_out["manifest"], encoding="utf-8").read())
        assert "context-estimate:v2" in saved_local_manifest["capabilities"]

        cli_out = lifecycle.connect_cli(
            kind="codex_cli",
            root=tmp_path / "cli_pkg",
            self_test_fn=lambda _m: {"ok": True},
        )
        saved_cli_manifest = json.loads(open(cli_out["manifest"], encoding="utf-8").read())
        assert "context-estimate:v2" in saved_cli_manifest["capabilities"]

        peer_rows = [
            {
                "peer_id": "peer-v2",
                "node_name": "V2 Node",
                "online": True,
                "ready": True,
                "chat_protocol": "rynmesh.chat.v1",
                "service": {
                    "package_id": "svc-v2",
                    "model_alias": "V2 Model",
                    "adapter": "openai_compatible",
                    "capabilities": ["text-generation", "client-tools", "context-estimate:v2"],
                    "context_window": 32768,
                    "max_output_tokens": 4096,
                },
            }
        ]
        services = {
            **app.state.llm_agent_services,
            "discover": lambda _net: peer_rows,
            "discover_agent": lambda _net: peer_rows,
            "discover_display": lambda _net: peer_rows,
        }
        catalog = ModelCatalog(services, store)
        v2_model = next(m for m in catalog.list_models() if m["service_id"] == "svc-v2")
        assert "context_admission_bytes_per_token" not in v2_model
        assert output_policy.admission_bytes_per_token(v2_model) == 3
        v2_budget = output_policy.context_budget(v2_model)
        assert v2_budget["admission_bytes_per_token"] == 3
        assert v2_budget["effective_max_context_bytes"] == (32768 - 4096 - output_policy.FRAMING_RESERVE_TOKENS) * 3
    finally:
        app.state.llm_shutdown()


def test_t7_peer_without_context_estimate_v2_keeps_1byte_per_token(tmp_path) -> None:
    """T7 (covers Step 7): Legacy peer without context-estimate:v2 keeps context_admission_bytes_per_token == 1 (1 byte/token)."""
    store = RynmeshStore(home=tmp_path / "node_home", network_dir=tmp_path / "net")
    mkey = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=store.home,
        messaging_key=mkey,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    try:
        peer_rows = [
            {
                "peer_id": "peer-legacy",
                "node_name": "Legacy Node",
                "online": True,
                "ready": True,
                "chat_protocol": "rynmesh.chat.v1",
                "service": {
                    "package_id": "svc-legacy",
                    "model_alias": "Legacy Model",
                    "adapter": "openai_compatible",
                    "capabilities": ["text-generation", "client-tools"],
                    "context_window": 32768,
                    "max_output_tokens": 4096,
                },
            }
        ]
        services = {
            **app.state.llm_agent_services,
            "discover": lambda _net: peer_rows,
            "discover_agent": lambda _net: peer_rows,
            "discover_display": lambda _net: peer_rows,
        }
        catalog = ModelCatalog(services, store)
        legacy_model = next(m for m in catalog.list_models() if m["service_id"] == "svc-legacy")
        assert legacy_model.get("context_admission_bytes_per_token") == 1
        assert output_policy.admission_bytes_per_token(legacy_model) == 1
        legacy_budget = output_policy.context_budget(legacy_model)
        assert legacy_budget["admission_bytes_per_token"] == 1
        assert legacy_budget["effective_max_context_bytes"] == (32768 - 4096 - output_policy.FRAMING_RESERVE_TOKENS) * 1

        v2_peer_rows = [
            {
                **peer_rows[0],
                "service": {
                    **peer_rows[0]["service"],
                    "package_id": "svc-v2-cmp",
                    "capabilities": ["text-generation", "client-tools", "context-estimate:v2"],
                },
            }
        ]
        v2_catalog = ModelCatalog(
            {
                **services,
                "discover_agent": lambda _net: v2_peer_rows,
                "discover_display": lambda _net: v2_peer_rows,
                "discover": lambda _net: v2_peer_rows,
            },
            store,
        )
        v2_cmp = next(m for m in v2_catalog.list_models() if m["service_id"] == "svc-v2-cmp")
        assert v2_cmp.get("context_admission_bytes_per_token") is None
        assert output_policy.context_budget(v2_cmp)["effective_max_context_bytes"] == 3 * legacy_budget["effective_max_context_bytes"]
    finally:
        app.state.llm_shutdown()
