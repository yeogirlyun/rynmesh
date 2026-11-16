"""Tests for S01-I37 Step 1 (T4, T5): partial model success in SourceDrafts."""
from __future__ import annotations

import copy
import json

import pytest
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.llm_package.adapters import AdapterError

node = integration_node


def _prepare_env(node_pair, monkeypatch, models: list[str]):
    app, store = node_pair
    _, tools, _ = build_agent_integrations(app, store, store.home)
    context = {
        "task_id": "partial-draft-task",
        "owner_id": "local_owner",
        "consumer_node_id": store.peer_id,
        "goal_revision": 1,
        "scope_revision": 1,
        "resource_grants": [],
        "egress_grants": [],
        "_model_probe": lambda invoke, **bounds: invoke(bounds["max_output_tokens"], 15, lambda: None),
    }
    sources = app.state.llm_agent_services["source_store"]
    requests = []

    class _FakeAdapter:
        def __init__(self, record, model):
            self.record = record
            self.model = model

        def chat(self, body, *, task_id, timeout_s):
            requests.append({"model": self.model, "body": copy.deepcopy(body)})
            if "broken" in self.model:
                raise AdapterError("upstream model broken secret")
            return {"text": "OK", "message": {"role": "assistant", "content": "OK"}}

        def cancel(self, task_id):
            return True

    monkeypatch.setattr(sources, "adapter", lambda record, model="": _FakeAdapter(record, model))
    config = {
        "name": "Multi Model API",
        "kind": "api",
        "base_url": "https://vendor.example.test/v1",
        "models": models,
    }
    tools.create_source_credential(
        context["task_id"],
        {
            "client_request_id": "cred-req-1",
            "kind": config["kind"],
            "base_url": config["base_url"],
            "api_key": "test-secret-credential-xyz",
        },
        owner_id=context["owner_id"],
        consumer_node_id=context["consumer_node_id"],
        goal_revision=context["goal_revision"],
    )
    return tools, context, sources, config, requests


def _exec(tools, context, name, args, op):
    preview = tools.preview(name, args, context=context)
    return tools.execute(name, args, operation_id=op, context=context, expected_preview=preview, cancelled=lambda: False)


def test_t4_all_models_failed_still_rejects_commit(node, monkeypatch):
    """T4: 全部模型测试失败时 chat_passed=False，列出全部 failed_models，且提交被拒绝。"""
    tools, context, sources, config, _ = _prepare_env(node, monkeypatch, ["broken-1", "broken-2"])
    draft = _exec(tools, context, "ryn_prepare_source_draft", {"configuration": config}, "op-prep-all-fail")
    loc = {"draft_id": draft["draft_id"], "expected_draft_version": draft["version"]}
    tested = _exec(tools, context, "ryn_test_source_draft", loc, "op-test-all-fail")
    assert tested["test"]["chat_passed"] is False
    assert tested["test"]["failed_models"] == ["broken-1", "broken-2"]
    with pytest.raises(AgentToolRejected, match="agent_source_test_required"):
        _exec(tools, context, "ryn_commit_source_draft", loc, "op-commit-all-fail")
    assert sources.read() == {}


def test_t5_three_models_one_fails_saves_two_and_lists_failed_models(node, monkeypatch):
    """T5: 3 个模型中 1 个失败时 chat_passed=True，保存通过的 2 个模型并在结果 failed_models 列出失败的 1 个。"""
    tools, context, sources, config, _ = _prepare_env(node, monkeypatch, ["model-ok-1", "broken-mid", "model-ok-2"])
    draft = _exec(tools, context, "ryn_prepare_source_draft", {"configuration": config}, "op-prep-partial")
    loc = {"draft_id": draft["draft_id"], "expected_draft_version": draft["version"]}
    tested = _exec(tools, context, "ryn_test_source_draft", loc, "op-test-partial")
    assert tested["test"]["chat_passed"] is True
    assert tested["test"]["failed_models"] == ["broken-mid"]

    committed = _exec(tools, context, "ryn_commit_source_draft", loc, "op-commit-partial")
    assert committed["ok"] is True
    assert committed["models"] == ["model-ok-1", "model-ok-2"]
    assert committed["failed_models"] == ["broken-mid"]
    assert "以下模型未通过测试，未保存" in committed.get("warning", "")
    assert "test-secret-credential-xyz" not in json.dumps([tested, committed])

    saved = sources.read()[committed["source_id"]]
    assert saved["models"] == ["model-ok-1", "model-ok-2"]
    assert set(saved["tool_support"].keys()) == {"model-ok-1", "model-ok-2"}

    verified = tools.verify(
        {"tool": "ryn_commit_source_draft", "arguments": loc, "result": committed},
        [{"verifier": "saved_source_state"}],
        context=context,
        cancelled=lambda: False,
    )
    assert verified["status"] == "passed"
