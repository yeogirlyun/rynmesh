"""Overhaul S01-I20: CLI tool proposal limit (16), default max_tokens from model max_output_tokens, experimental Antigravity support marker, missing named tool_choice error, and Antigravity output_tokens > max_tokens truncation handling."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.llm_package import antigravity_session, cli_models, codex_session, codex_tools
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.tool_capabilities import ToolCapabilities


def _sample_tool_spec(name: str = "inspect_service") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Inspect {name}",
            "parameters": {
                "type": "object",
                "properties": {"service_id": {"type": "string"}},
                "required": ["service_id"],
                "additionalProperties": False,
            },
        },
    }


def test_t1_codex_and_antigravity_accept_12_proposals_and_pair_overflow(monkeypatch):
    """T1: Codex object protocol and Antigravity accept 12 (up to 16) proposals and reject 17 (covers Step 1)."""
    tool_spec = _sample_tool_spec("inspect_service")
    tools_map = {"inspect_service": tool_spec["function"]}

    twelve_proposals = [
        {"name": "inspect_service", "arguments": {"service_id": f"svc_{idx}"}}
        for idx in range(12)
    ]
    codex_msg = codex_tools._validated(
        {"text": json.dumps({"content": "Batch check", "tool_calls": twelve_proposals})},
        tools_map,
        required=True,
    )
    assert len(codex_msg["tool_calls"]) == 12
    assert [
        json.loads(tc["function"]["arguments"])["service_id"]
        for tc in codex_msg["tool_calls"]
    ] == [f"svc_{idx}" for idx in range(12)]

    # Antigravity also accepts 12 proposals:
    monkeypatch.setattr(
        antigravity_session,
        "infer",
        lambda *_a, **_kw: {
            "text": json.dumps({"content": "AG batch", "tool_calls": twelve_proposals}),
            "structured_output": {"content": "AG batch", "tool_calls": twelve_proposals},
            "input_tokens": 20,
            "output_tokens": 40,
            "finish_reason": "stop",
        },
    )
    ag_adapter = CLIAgentAdapter("antigravity_cli", executable="fixture-agy")
    ag_res = ag_adapter.chat(
        {"messages": [{"role": "user", "content": "Inspect 12 services"}], "tools": [tool_spec]},
        task_id="t1-ag-12",
        timeout_s=5,
    )
    assert len(ag_res["message"]["tool_calls"]) == 12

    # S06-I23 preserves the valid siblings and pairs the excess proposal as a rejection.
    limit = codex_tools.MAX_PROPOSALS
    proposals = [
        {"name": "inspect_service", "arguments": {"service_id": f"svc_{idx}"}}
        for idx in range(limit + 1)
    ]
    result = codex_tools._validated(
        {"text": json.dumps({"content": "", "tool_calls": proposals})},
        tools_map,
        required=False,
    )["tool_calls"]
    assert len({call["id"] for call in result}) == len(result) == limit + 1
    assert [c["function"]["name"] for c in result] == ["inspect_service"] * limit + ["ryn_proposal_overflow"]
    assert json.loads(result[-1]["function"]["arguments"]) == {}


def test_t2_antigravity_and_codex_default_max_tokens_use_model_max_output_tokens_and_experimental_marker(
    tmp_path, monkeypatch
):
    """T2: Antigravity & Codex omitted max_tokens default to model max_output_tokens; cli_models marks Antigravity agent_support='experimental' and fallback window 65536 (covers Steps 2, 3, 6)."""
    # Verify Step 3 & Step 6 metadata:
    parsed_rows = cli_models.parse_antigravity_models(
        "gemini-2.5-pro\tGemini 2.5 Pro\nclaude-sonnet-4-5\tClaude Sonnet 4.5\n"
    )
    assert all(r.get("agent_support") == "experimental" for r in parsed_rows)
    assert cli_models.fallback_window("antigravity_cli") == 65536

    ag_manifest = LLMPackageManifest(
        package_id="antigravity-cli",
        mode="antigravity_cli",
        adapter="antigravity_cli",
        runtime="external",
        public_model_alias="Antigravity",
        capabilities=["text-generation", "client-tools", "buffered-streaming"],
        context_window=65536,
        max_output_tokens=32000,
    )
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda self: "fixture-agy")
    monkeypatch.setattr(cli_models, "cached_models", lambda kind, exe: list(parsed_rows))
    catalog = ModelCatalog(
        {
            "source_store": SimpleNamespace(read=lambda: {}),
            "discover": lambda _: [
                {
                    "peer_id": "self",
                    "online": True,
                    "chat_protocol": "rynmesh.chat.v1",
                    "service": ag_manifest.public_dict(),
                }
            ],
            "provider_for": lambda _sid: SimpleNamespace(
                adapter=CLIAgentAdapter("antigravity_cli", executable="fixture-agy"),
                manifest=ag_manifest,
            ),
        },
        SimpleNamespace(peer_id="self", llm_tool_capabilities=ToolCapabilities(tmp_path / "caps")),
    )
    models_by_id = {r["id"]: r for r in catalog.list_models()}
    assert models_by_id["local/antigravity-cli"]["agent_support"] == "experimental"
    assert models_by_id["local/antigravity-cli/model/gemini-2.5-pro"]["agent_support"] == "experimental"

    # Verify Step 2: omitted max_tokens uses model's max_output_tokens (e.g. 65536 for gemini-2.5-pro, 32000 fallback)
    captured_ag_budgets: list[int] = []

    def fake_ag_infer(_adapter, _prompt, **kwargs):
        captured_ag_budgets.append(kwargs["max_tokens"])
        return {
            "text": json.dumps({"content": "ok", "tool_calls": []}),
            "structured_output": {"content": "ok", "tool_calls": []},
            "message": {"role": "assistant", "content": "ok"},
            "input_tokens": 10,
            "output_tokens": 5,
            "finish_reason": "stop",
        }

    monkeypatch.setattr(antigravity_session, "infer", fake_ag_infer)
    ag_adapter = CLIAgentAdapter("antigravity_cli", executable="fixture-agy")
    # Plain conversation without max_tokens on gemini-2.5-pro -> uses model max_output_tokens (65536, not 512)
    ag_adapter.chat(
        {"messages": [{"role": "user", "content": "Hello"}]},
        task_id="t2-plain",
        timeout_s=5,
        model="gemini-2.5-pro",
    )
    # Tool conversation without max_tokens on unknown model -> uses CLI fallback max_output_tokens (32000, not 512)
    monkeypatch.setattr(cli_models, "cached_models", lambda kind, exe: [])
    ag_adapter.chat(
        {"messages": [{"role": "user", "content": "Inspect"}], "tools": [_sample_tool_spec()]},
        task_id="t2-tools",
        timeout_s=5,
        model="custom-experimental-model",
    )
    assert captured_ag_budgets == [65536, 32000]

    # Codex tools without max_tokens also uses model max_output_tokens (not 2048):
    captured_codex_budgets: list[int] = []
    monkeypatch.setattr(
        codex_session,
        "_native_chat",
        lambda _adapter, req_body, **_kw: (
            captured_codex_budgets.append(req_body["max_tokens"])
            or {"text": json.dumps({"content": "done", "tool_calls": []}), "input_tokens": 8, "output_tokens": 4}
        ),
    )
    codex_tools.chat(
        CLIAgentAdapter("codex_cli", executable="fixture-codex"),
        {"messages": [{"role": "user", "content": "Check"}], "tools": [_sample_tool_spec()]},
        task_id="t2-codex",
        timeout_s=5,
        model="gpt-5-codex",
    )
    assert captured_codex_budgets and captured_codex_budgets[0] > 2048


def test_t3_antigravity_usage_exceeding_max_tokens_sets_finish_length_and_skips_tool_execution(
    tmp_path, monkeypatch
):
    """T3: When Antigravity returns usage.output_tokens > max_tokens, finish_reason is 'length', diagnostics has output_limit_not_enforced=True, and loop does not execute proposed tools (covers Step 5)."""
    adapter = CLIAgentAdapter("antigravity_cli", executable="fixture-agy")
    monkeypatch.setattr(adapter, "_command_path", lambda: "fixture-agy")
    monkeypatch.setattr(antigravity_session, "spawn", lambda _args, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(antigravity_session, "terminate", lambda _p: None)

    over_budget_payload = {
        "status": "SUCCESS",
        "response": "Attempting tool call over budget",
        "structured_output": {
            "content": "Over budget proposal",
            "tool_calls": [{"name": "inspect_service", "arguments": {"service_id": "svc_should_not_run"}}],
        },
        "usage": {"input_tokens": 50, "output_tokens": 300},
    }
    monkeypatch.setattr(antigravity_session, "communicate_bounded", lambda *_a, **_kw: json.dumps(over_budget_payload))

    tool_spec = _sample_tool_spec("inspect_service")
    direct = adapter.chat(
        {"messages": [{"role": "user", "content": "Inspect"}], "tools": [tool_spec], "max_tokens": 128},
        task_id="t3-direct",
        timeout_s=5,
    )
    assert direct["finish_reason"] == "length"
    assert direct["diagnostics"]["output_limit_not_enforced"] is True

    # Verify end-to-end through AgentModelBridge + AgentRuntime loop: tool must NOT execute when finish_reason == 'length'!
    runtime, _, tools, _ = make_runtime(tmp_path)
    target = {
        "source": "local_service",
        "service_id": "ag-len-fixture",
        "adapter": "antigravity_cli",
        "streaming": False,
        "max_output_tokens": 128,
        "context_window": 65536,
    }
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=SimpleNamespace(timeout_seconds=10),
        _slots=threading.BoundedSemaphore(1),
        _lock=threading.Lock(),
        _running=0,
    )
    bridge = AgentModelBridge({"provider_for": lambda _sid: manager}, SimpleNamespace(resolve=lambda _mid: target))
    runtime.model = bridge

    calls_count = 0

    def fake_comm(*_a, **_kw):
        nonlocal calls_count
        calls_count += 1
        if calls_count == 1:
            return json.dumps(over_budget_payload)
        return json.dumps(
            {
                "status": "SUCCESS",
                "response": "Recovered concise answer",
                "structured_output": {"content": "Recovered concise answer", "tool_calls": []},
                "usage": {"input_tokens": 60, "output_tokens": 20},
            }
        )

    monkeypatch.setattr(antigravity_session, "communicate_bounded", fake_comm)
    created = runtime.create({"message": "Inspect svc_should_not_run"})
    for _ in range(5):
        if runtime.get(created["id"])["status"] not in {"queued", "running"}:
            break
        runtime.run_once()

    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    # The over-budget tool call was marked incomplete and NEVER executed on host tools:
    assert tools.calls == []


def test_t4_named_tool_choice_not_in_tools_raises_source_protocol_parameters_unsupported():
    """T4: Named tool_choice pointing to a missing function raises AdapterError(code='source_protocol_parameters_unsupported') rather than KeyError/ValueError in both Codex and Antigravity (covers Step 4)."""
    tool_spec = _sample_tool_spec("inspect_service")
    bad_choice = {"type": "function", "function": {"name": "nonexistent_tool"}}

    for fn in (
        lambda: codex_tools.chat(
            CLIAgentAdapter("codex_cli", executable="fixture-codex"),
            {"messages": [{"role": "user", "content": "hi"}], "tools": [tool_spec], "tool_choice": bad_choice},
            task_id="t4-codex-direct",
            timeout_s=5,
        ),
        lambda: antigravity_session.chat(
            CLIAgentAdapter("antigravity_cli", executable="fixture-agy"),
            {"messages": [{"role": "user", "content": "hi"}], "tools": [tool_spec], "tool_choice": bad_choice},
            task_id="t4-ag-direct",
            timeout_s=5,
        ),
        lambda: CLIAgentAdapter("codex_cli", executable="fixture-codex").chat(
            {"messages": [{"role": "user", "content": "hi"}], "tools": [tool_spec], "tool_choice": bad_choice},
            task_id="t4-codex-adapter",
            timeout_s=5,
        ),
        lambda: CLIAgentAdapter("antigravity_cli", executable="fixture-agy").chat(
            {"messages": [{"role": "user", "content": "hi"}], "tools": [tool_spec], "tool_choice": bad_choice},
            task_id="t4-ag-adapter",
            timeout_s=5,
        ),
    ):
        with pytest.raises(AdapterError) as exc_info:
            fn()
        assert exc_info.value.code == "source_protocol_parameters_unsupported"
        assert "named tool choice is not in tools" in str(exc_info.value)
