"""S01-I18 regression tests: /props n_ctx & unknown_fallback discovery, conservative CLI windows & min CLI default, repeated length-truncated tool chunking guidance, and reasoning-exhausted output limit retry."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_runtime import Tools, advance, make_runtime

from rynmesh.agent_runtime import diagnostics as agent_diagnostics
from rynmesh.agent_runtime.bridge import OUTPUT_LIMIT_REACHED_FEEDBACK, AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import cli_models, lifecycle, model_settings
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, load_manifest, save_manifest
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    NativeStreamError,
    StreamDecoder,
    parse_response,
)
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.sources import SourceStore
from rynmesh.llm_package.tool_capabilities import ToolCapabilities
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@contextmanager
def _local_http_server(routes_map: dict[str, tuple[int, dict]]):
    seen_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen_paths.append(self.path)
            status, payload = routes_map.get(self.path, (404, {"error": "not_found"}))
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            seen_paths.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body_bytes = self.rfile.read(length) if length else b"{}"
            req_json = json.loads(body_bytes.decode("utf-8") or "{}")
            entry = routes_map.get(self.path)
            if callable(entry):
                status, ctype, payload = entry(req_json)
            elif entry is not None:
                status, payload = entry
                ctype = "application/json"
            else:
                status, ctype, payload = 404, "application/json", {"error": "not_found"}
            raw = (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen_paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_t1_source_probe_reads_llamacpp_props_n_ctx_and_vllm_max_model_len(tmp_path):
    """T1 (Change 1): SourceStore.probe reads llama.cpp /props n_ctx (even when base_url ends with /v1) as well as vLLM max_model_len."""
    routes = {
        "/v1/models": (200, {"data": [{"id": "local-gguf-q4"}]}),
        "/props": (200, {"default_generation_settings": {"n_ctx": 24576}}),
        "/v1/chat/completions": (
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        ),
    }
    with _local_http_server(routes) as (origin, _seen):
        store = SourceStore(tmp_path / "home1", b"key-1")
        record = store.prepare(
            {
                "name": "Local llama.cpp",
                "kind": "local",
                "provider": "custom",
                "base_url": f"{origin}/v1",
                "models": ["local-gguf-q4"],
            }
        )
        discovered = store.probe(record, models_only=True)
        assert discovered["model_metadata"]["local-gguf-q4"]["context_window"] == 24576
        store.save(record)
        assert model_settings.effective(record, "local-gguf-q4")["context_window"] == 24576
        assert record["context_window_basis"] == "provider_model_metadata"


def test_t2_connect_cli_writes_conservative_windows_32k_output_and_cli_default_basis(tmp_path, monkeypatch):
    """T2 (Change 4): connect_cli writes conservative windows (codex 128000, claude 200000, antigravity 65536), max_output_tokens=32000, and context_window_basis='cli_default'; claude_cli is marked context_window_known=False in catalog, and legacy CLI manifests refresh at startup."""
    monkeypatch.setattr(
        lifecycle,
        "adapter_from_manifest",
        lambda _m: SimpleNamespace(
            health=lambda: {"ok": True},
            capabilities=lambda: {"chat_completions": True, "streaming": False, "cancel": "best_effort"},
        ),
    )
    expected = {
        "codex_cli": 128000,
        "claude_cli": 200000,
        "antigravity_cli": 65536,
    }
    manifest_paths: dict[str, str] = {}
    for kind, exp_win in expected.items():
        res = lifecycle.connect_cli(
            kind=kind,
            root=tmp_path / "pkgs",
            self_test_fn=lambda _m: {"ok": True},
        )
        manifest_paths[kind] = res["manifest"]
        loaded = load_manifest(res["manifest"])
        assert loaded.context_window == exp_win
        assert loaded.max_output_tokens == 32000
        assert loaded.context_window_basis == "cli_default"
        pub = loaded.public_dict()
        assert pub["context_window"] == exp_win
        assert pub["max_output_tokens"] == 32000
        assert pub["context_window_basis"] == "cli_default"

    # ModelCatalog marks claude_cli context_window_known=False with basis='cli_default' unless declared
    claude_manifest = load_manifest(manifest_paths["claude_cli"])
    service_rec = {
        "peer_id": "self",
        "online": True,
        "chat_protocol": "rynmesh.chat.v1",
        "service": claude_manifest.public_dict(),
    }
    catalog = ModelCatalog(
        {"source_store": SimpleNamespace(read=lambda: {}), "discover": lambda _: [service_rec]},
        SimpleNamespace(peer_id="self", llm_tool_capabilities=ToolCapabilities(tmp_path / "caps")),
    )
    rows = {r["id"]: r for r in catalog.list_models()}
    claude_row = rows["local/claude-cli"]
    assert claude_row["context_window"] == 200000
    assert claude_row["max_output_tokens"] == 4096
    assert claude_row["max_output_tokens_known"] is False
    assert claude_row["max_output_basis"] == "unknown_fallback"
    assert claude_row["context_window_known"] is False
    assert claude_row["context_window_basis"] == "cli_default"

    # Startup active_cli_managers refreshes legacy hardcoded 872000/128000 manifest on disk
    from fastapi import FastAPI

    node_home = tmp_path / "node_legacy"
    ryn_store = RynmeshStore(home=node_home, network_dir=tmp_path / "net")
    legacy_path = node_home / "llm" / "packages" / "codex-cli" / "manifest.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    save_manifest(
        LLMPackageManifest(
            package_id="codex-cli",
            mode="codex_cli",
            adapter="codex_cli",
            runtime="external",
            public_model_alias="Codex CLI",
            context_window=872000,
            max_output_tokens=128000,
        ),
        legacy_path,
    )
    cli_cfg = node_home / "llm" / "cli-services.json"
    cli_cfg.parent.mkdir(parents=True, exist_ok=True)
    cli_cfg.write_text(json.dumps({"codex_cli": {"manifest": str(legacy_path), "publication_enabled": False}}), encoding="utf-8")
    app = FastAPI()
    install_llm_routes(
        app,
        store=ryn_store,
        home=node_home,
        messaging_key=peer_box.load_or_create_messaging_key(node_home / "msg.key"),
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    mgr = app.state.llm_agent_services["active_cli_managers"]()["codex_cli"]
    assert mgr.manifest.context_window == 128000
    assert mgr.manifest.max_output_tokens == 32000
    assert mgr.manifest.context_window_basis == "cli_default"
    reloaded = load_manifest(legacy_path)
    assert reloaded.context_window == 128000 and reloaded.max_output_tokens == 32000


@pytest.mark.parametrize("protocol", [RESPONSES, MESSAGES])
def test_t3_native_codecs_reasoning_only_with_length_raises_upstream_output_limit_reached(protocol):
    """T3 (Change 5): parse_response and StreamDecoder raise NativeStreamError('upstream_output_limit_reached') when finish==length and only reasoning/thinking exists without text or tools."""
    scope = "b" * 64
    if protocol == RESPONSES:
        raw = {
            "id": "resp_r",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "id": "rs_only", "summary": [], "encrypted_content": "ENC"}],
            "usage": {"input_tokens": 10, "output_tokens": 512},
        }
        with pytest.raises(NativeStreamError) as err_parse:
            parse_response(raw, RESPONSES, "gpt-5", scope)
        assert err_parse.value.code == "upstream_output_limit_reached"

        dec = StreamDecoder(RESPONSES, "gpt-5", scope)
        dec.feed({"type": "response.created", "response": {"id": "resp_r", "status": "in_progress"}})
        with pytest.raises(NativeStreamError) as err_stream:
            dec.feed({"type": "response.incomplete", "response": raw})
        assert err_stream.value.code == "upstream_output_limit_reached"
    else:
        raw = {
            "id": "msg_r",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "all tokens spent thinking", "signature": "SIG"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "output_tokens": 512},
        }
        with pytest.raises(NativeStreamError) as err_parse:
            parse_response(raw, MESSAGES, "claude-sonnet", scope)
        assert err_parse.value.code == "upstream_output_limit_reached"

        dec = StreamDecoder(MESSAGES, "claude-sonnet", scope)
        dec.feed({"type": "message_start", "message": {"id": "msg_r", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 10, "output_tokens": 1}}})
        dec.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "deep", "signature": "S"}})
        dec.feed({"type": "content_block_stop", "index": 0})
        dec.feed({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 512}})
        with pytest.raises(NativeStreamError) as err_stream:
            dec.feed({"type": "message_stop"})
        assert err_stream.value.code == "upstream_output_limit_reached"


def test_t4_native_and_chat_transport_reasoning_only_length_raises_upstream_output_limit_reached_with_max_tokens_diag():
    """T4 (Change 5): NativeProtocolAdapter and OpenAICompatibleAdapter raise AdapterError(code='upstream_output_limit_reached') with diagnostics.max_tokens when reasoning eats the entire max_tokens budget."""
    routes = {
        "/v1/responses": lambda req: (
            200,
            "application/json",
            {
                "id": "r1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "reasoning", "id": "rs1", "summary": []}],
                "usage": {"input_tokens": 8, "output_tokens": req["max_output_tokens"]},
            },
        ),
        "/v1/messages": lambda req: (
            200,
            "application/json",
            {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "long thought", "signature": "sig"}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 8, "output_tokens": req["max_tokens"]},
            },
        ),
        "/v1/chat/completions": lambda req: (
            200,
            "application/json",
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": None, "reasoning_content": "spent all tokens"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": req["max_tokens"]},
            },
        ),
    }
    with _local_http_server(routes) as (origin, _seen):
        for proto in ("openai_responses", "anthropic_messages"):
            native = NativeProtocolAdapter(protocol=proto, base_url=f"{origin}/v1", api_prefix="", api_key="k", model="m")
            with pytest.raises(AdapterError) as exc_info:
                native.chat({"messages": [{"role": "user", "content": "solve"}], "max_tokens": 320}, task_id="t4-n", timeout_s=3)
            assert exc_info.value.code == "upstream_output_limit_reached"
            assert exc_info.value.diagnostics["max_tokens"] == 320

        chat_adapter = OpenAICompatibleAdapter(base_url=origin, api_prefix="/v1", api_key="k", model="deepseek-reasoner")
        with pytest.raises(AdapterError) as chat_exc:
            chat_adapter.chat({"messages": [{"role": "user", "content": "solve"}], "max_tokens": 256}, task_id="t4-c", timeout_s=3)
        assert chat_exc.value.code == "upstream_output_limit_reached"
        assert chat_exc.value.diagnostics["max_tokens"] == 256


def test_t5_bridge_retries_once_doubling_max_tokens_capped_at_model_limit_and_fails_with_guidance_when_still_empty(tmp_path):
    """T5 (Change 5): AgentModelBridge maps upstream_output_limit_reached to agent_model_output_limit_reached, walks the 1-shot feedback retry path with min(2*orig, model max_output_tokens) keeping reasoning_effort unchanged, and fails with Chinese guidance if retry also exhausts output."""
    calls: list[dict] = []
    fail_second = [False]

    class FakeAdapter:
        supports_developer_role = True
        timeout_s = 30.0
        model_binding = {"kind": "cli", "id": "o3"}

        def binding_for_model(self, _mid):
            return self.model_binding

        def call_bound(self, _method, _binding, _before, chat_body, **opts):
            return self.chat(chat_body, **opts)

        def chat(self, chat_body, **_opts):
            calls.append(json.loads(json.dumps(chat_body)))
            if len(calls) == 1 or fail_second[0]:
                raise AdapterError(
                    "upstream_output_limit_reached",
                    code="upstream_output_limit_reached",
                    reported_usage={"input_tokens": 12, "output_tokens": chat_body["max_tokens"]},
                    finish_reason="length",
                    diagnostics={"max_tokens": chat_body["max_tokens"]},
                )
            return {
                "text": "Final concise answer",
                "message": {"role": "assistant", "content": "Final concise answer"},
                "finish_reason": "stop",
                "input_tokens": 20,
                "output_tokens": 15,
                "reported_usage": {"input_tokens": 20, "output_tokens": 15},
                "duration_ms": 5,
            }

        def cancel(self, _tid):
            return False

    model_max = [4096]

    def target_row() -> dict:
        return {
            "id": "local/codex-cli/model/o3",
            "name": "Reasoner",
            "source": "local_service",
            "source_id": "",
            "service_id": "codex-cli",
            "peer_id": "self",
            "model_id": "o3",
            "cli_model": "o3",
            "adapter": "codex_cli",
            "available": True,
            "agent_supported": True,
            "context_window": 128000,
            "context_window_known": True,
            "context_window_basis": "installed_cli_model_cache",
            "max_output_tokens": model_max[0],
            "local_model_binding": FakeAdapter.model_binding,
            "destination_binding_supported": True,
            "destination_revision": "rev-1",
            "supported_reasoning_efforts": ["low", "medium", "high"],
            "default_reasoning_effort": "medium",
        }

    manager = SimpleNamespace(
        adapter=FakeAdapter(),
        manifest=SimpleNamespace(timeout_seconds=60.0),
        _slots=threading.BoundedSemaphore(2),
        _lock=threading.Lock(),
        _running=0,
    )
    catalog = SimpleNamespace(
        network="default",
        store=SimpleNamespace(peer_id="self"),
        list_models=lambda **_kw: [target_row()],
        resolve=lambda _mid, **_kw: target_row(),
        destination=lambda r: {
            "catalog_model_id": r["id"],
            "provider_peer_id": r["peer_id"],
            "source_id": r["source_id"],
            "destination_revision": r["destination_revision"],
        },
    )
    bridge = AgentModelBridge({"provider_for": lambda _sid: manager}, catalog)
    runtime = AgentRuntime(AgentStore(tmp_path / "t5-rt", X25519PrivateKey.generate()), bridge, Tools(), catalog)
    runtime.update_settings({"enabled": True, "model_id": "local/codex-cli/model/o3"})

    orig_complete = runtime._complete

    def capped_first_attempt(row, messages, specs, cancelled, on_event, **kwargs):
        if kwargs.get("output_cap") is None:
            kwargs["output_cap"] = 1024
        return orig_complete(row, messages, specs, cancelled, on_event, **kwargs)

    runtime._complete = capped_first_attempt  # type: ignore[method-assign]

    # 1) Doubling: 1024 -> 2048 (<= 4096), reasoning_effort="high" preserved, feedback injected, succeeds on retry
    task1 = runtime.create({"message": "Solve this", "reasoning_effort": "high"})
    runtime.run_once()
    row1_mid = runtime.store.read()["tasks"][task1["id"]]
    assert row1_mid["status"] == "queued"
    assert row1_mid["model_retry_count"] == 1
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1024
    assert calls[0]["reasoning_effort"] == "high"

    runtime.run_once()
    done1 = runtime.get(task1["id"])
    assert done1["status"] == "completed"
    assert done1["result"] == "Final concise answer"
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 2048
    assert calls[1]["reasoning_effort"] == "high"
    assert OUTPUT_LIMIT_REACHED_FEEDBACK in json.dumps(calls[1]["messages"], ensure_ascii=False)

    # 2) Capped at model max_output_tokens (min(2 * 1024, 1500) == 1500) and fails with Chinese guidance on second empty
    calls.clear()
    model_max[0] = 1500
    fail_second[0] = True
    task2 = runtime.create({"message": "Solve harder", "reasoning_effort": "high"})
    runtime.run_once()
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1024

    runtime.run_once()
    done2 = runtime.get(task2["id"])
    raw2 = runtime.store.read()["tasks"][task2["id"]]
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 1500
    assert calls[1]["reasoning_effort"] == "high"
    assert done2["status"] == "failed"
    assert done2["error"] == "agent_model_output_limit_reached"
    expected_msg = "模型把输出额度都用在思考上了，请调高该模型的输出上限或降低推理强度"
    final_rej = [e for e in raw2["events"] if e.get("type") == "model_response_rejected" and e.get("error") == "agent_model_output_limit_reached"]
    assert final_rej and final_rej[-1]["detail"] == expected_msg
    assert agent_diagnostics._ERROR_GUIDANCE["agent_model_output_limit_reached"][1] == expected_msg


def test_t6_source_probe_sets_unknown_fallback_basis_when_neither_models_nor_props_declares_window(tmp_path):
    """T6 (Change 1): When neither /models nor /props provides a context window and model is unknown, probe writes context_window_basis='unknown_fallback' on the source record."""
    routes = {
        "/v1/models": (200, {"data": [{"id": "custom-unlisted-model-v1"}]}),
        "/v1/chat/completions": (
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        ),
    }
    with _local_http_server(routes) as (origin, _seen):
        store = SourceStore(tmp_path / "home6", b"key-6")
        record = store.prepare(
            {
                "name": "Unknown Box",
                "kind": "local",
                "provider": "custom",
                "base_url": f"{origin}/v1",
                "models": ["custom-unlisted-model-v1"],
            }
        )
        store.probe(record)
        assert record["context_window_basis"] == "unknown_fallback"
        pub = store.save(record)
        assert pub["context_window_basis"] == "unknown_fallback"


def test_t7_repeated_length_truncated_same_tool_gives_chunking_advice_on_second_and_emits_output_limit_hit_on_third(tmp_path):
    """T7 (Change 2): When the same tool is truncated by finish_reason='length' twice in a row, the 2nd message gives N & N/2 token chunking advice, and the 3rd emits output_limit_hit once."""
    def truncated_call(call_id: str):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "share_service", "arguments": '{"service_id":"big"}'},
                    }
                ],
            },
            "finish_reason": "length",
        }

    runtime, _, _, _ = make_runtime(
        tmp_path / "store7",
        truncated_call("c1"),
        truncated_call("c2"),
        truncated_call("c3"),
        {"message": {"role": "assistant", "content": "Completed in smaller chunks."}, "finish_reason": "stop"},
    )
    created = runtime.create({"message": "Write large output."})
    task_id = created["id"]

    # Step 1 (model #1 -> length tool rejection #1)
    advance(runtime, 2)
    mid1 = runtime.store.read()["tasks"][task_id]
    tool_msgs_1 = [json.loads(m["content"]) for m in mid1["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs_1) == 1
    assert "Issue a complete tool call" in tool_msgs_1[0]["message"]
    assert not any(ev["type"] == "output_limit_hit" for ev in mid1["events"])

    # Step 2 (model #2 -> length tool rejection #2: chunking advice with effective_cap N and N/2)
    advance(runtime, 2)
    mid2 = runtime.store.read()["tasks"][task_id]
    eff_cap = mid2["budget"]["records"][-1]["output_policy"]["effective_cap"]
    tool_msgs_2 = [json.loads(m["content"]) for m in mid2["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs_2) == 2
    second_msg = tool_msgs_2[1]["message"]
    assert f"输出上限约 {eff_cap} token" in second_msg
    assert f"每次不超过约 {eff_cap // 2} token" in second_msg
    assert "先建文件再追加或用补丁" in second_msg
    assert not any(ev["type"] == "output_limit_hit" for ev in mid2["events"])

    # Step 3 (model #3 -> 3rd consecutive length truncation emits output_limit_hit) + finish
    advance(runtime, 3)
    done = runtime.get(task_id)
    assert done["status"] == "completed"
    hit_events = [ev for ev in done["events"] if ev["type"] == "output_limit_hit"]
    assert len(hit_events) == 1
    assert hit_events[0]["tool"] == "share_service"
    assert hit_events[0]["max_output_tokens"] == eff_cap


def test_t8_antigravity_cli_default_row_uses_minimum_window_and_output_and_cli_fallbacks_match_kind():
    """T8 (Change 3): parse_antigravity_models sets CLI default row to the minimum context_window and minimum max_output_tokens across discovered models, and context_metadata uses per-CLI fallbacks."""
    sample_output = (
        "gemini-2.5-pro\tGemini 2.5 Pro\n"
        "claude-sonnet-4-5\tClaude Sonnet 4.5\n"
    )
    rows = cli_models.parse_antigravity_models(sample_output)
    default_row = rows[0]
    model_rows = rows[1:]
    assert default_row["id"] == "" and default_row["default"] is True
    declared_windows = [r["context_window"] for r in model_rows if "context_window" in r]
    declared_outputs = [r["max_output_tokens"] for r in model_rows if "max_output_tokens" in r]
    assert len(declared_windows) == 2 and min(declared_windows) < max(declared_windows)
    assert default_row["context_window"] == min(declared_windows)
    assert default_row["max_output_tokens"] == min(declared_outputs)

    assert cli_models.context_metadata([], kind="codex_cli")["context_window"] == 128000
    assert cli_models.context_metadata([], kind="claude_cli")["context_window"] == 200000
    assert cli_models.context_metadata([], kind="antigravity_cli")["context_window"] == 65536
