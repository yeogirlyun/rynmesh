"""C01 fixture: owner secure HTTP form -> real upstream probes -> encrypted save.

Deterministic planner; no claim of real-model quality or formal frozen score.
"""
import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_agent_integrations import node as integration_node
from test_agent_runtime import assistant

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box

node = integration_node
_spec = importlib.util.spec_from_file_location("agent_source_fixture", Path(__file__).parent / "fixtures/agent_acceptance/source_fixture.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
FixtureSource = _module.FixtureSource

_runner_spec = importlib.util.spec_from_file_location("source_development_runner", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/real-model/run_sources.py")
_runner = importlib.util.module_from_spec(_runner_spec)
_runner_spec.loader.exec_module(_runner)


@pytest.mark.parametrize("polluted", [{"content": "PRIVATE-KEY"}, {"reasoning_content": "hidden"}])
def test_export_failure_retains_only_safe_failure_receipt(tmp_path, polluted):
    record = {"model_calls": [polluted], "approvals": [], "task": {"result": "UNSAFE-CONTENT"}}
    assert not _runner.export_record(tmp_path, record, secrets=["PRIVATE-KEY"])
    text = (tmp_path / "run.json").read_text(encoding="utf-8")
    saved = json.loads(text)
    assert saved["evidence_export_blocked"] and saved["model_call_count"] == 1
    assert "PRIVATE-KEY" not in text and "UNSAFE-CONTENT" not in text and "reasoning_content" not in text


def test_source_approval_is_exact_but_does_not_invent_tool_capability_requirement():
    approval = {"tool": "ryn_commit_source_draft", "preview": {"source_id": None,
        "configuration": {"name": "source-new", "models": ["alpha", "beta"], "kind": "local", "base_url": "http://127.0.0.1:123/v1"},
        "test": {"chat_passed": True}}}
    def allowed(value, targets=()):
        return _runner.approve_new_source(value, endpoint="http://127.0.0.1:123/v1", existing_targets=targets)
    assert allowed(approval)  # Chat-only models are valid source configurations.
    assert not allowed(approval, [{"id": "already-saved"}])
    for key, value in (("source_id", "existing-source"), ("test", {"chat_passed": False})):
        assert not allowed({**approval, "preview": {**approval["preview"], key: value}})
    for key, value in (("base_url", "http://other/v1"), ("models", ["alpha"]), ("name", "other")):
        assert not allowed({**approval, "preview": {**approval["preview"], "configuration": {**approval["preview"]["configuration"], key: value}}})


def test_secure_owner_form_actual_http_probes_and_saved_private_models(node):
    app, store = node
    _, tools, _ = build_agent_integrations(app, store, store.home)
    sources = app.state.llm_agent_services["source_store"]
    requests = []
    with FixtureSource(saved=lambda: bool(sources.read())) as upstream:
        configuration = {"name": "source-new", "kind": "local", "base_url": upstream.url, "models": ["alpha", "beta"]}
        class Model:
            count = 0
            def complete(self, model_id, messages, specs, cancelled, on_event):
                requests.append({"messages": messages, "tools": specs})
                if not specs:
                    data = json.loads(messages[-1]["content"])
                    return assistant(json.dumps({"status": "passed", "summary": "Both tested and saved private.",
                        "uncovered_requirements": [], "evidence_refs": [e["id"] for e in data["evidence"]]}))
                self.count += 1
                if self.count == 1:
                    return assistant(name="ryn_request_input", args={"goal_revision": 1, "kind": "credential_ref", "prompt": "Use the secure local credential form.",
                        "fields": [{"id": "source", "label": "Source credential", "type": "credential_ref", "required": True}]})
                if self.count == 2:
                    return assistant(name="ryn_prepare_source_draft", args={"configuration": configuration})
                if self.count in {3, 4}:
                    draft = json.loads(messages[-1]["content"])
                    return assistant(name="ryn_test_source_draft" if self.count == 3 else "ryn_commit_source_draft",
                        args={"draft_id": draft["draft_id"], "expected_draft_version": draft["version"], **({"test_tools": True} if self.count == 3 else {})})
                return assistant("Both alpha and beta were tested and saved private.")
        def owner(request):
            if request.headers.get("x-fixture-owner") != "yes":
                raise HTTPException(401)
        catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "available": True, "agent_supported": True}])
        runtime = install_agent(app, store=store, home=store.home, workers=BackgroundWorkerRegistry(), local_control=owner,
            messaging_key=peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"), integrations=(Model(), tools, catalog))
        runtime.update_settings({"enabled": True, "model_id": "fixture", "max_steps": 24})
        case = next(c for c in json.loads((Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "C01")
        task = runtime.create({"message": case["prompt"]})
        form_results, approvals = [], []
        with TestClient(app) as client:
            for _ in range(30):
                row = runtime.get(task["id"])
                if row["status"] == "waiting_input":
                    assert row["pending_input"]["kind"] == "credential_ref" and not form_results
                    payload = {"expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"],
                        "pending_input_id": row["pending_input"]["id"], "client_request_id": str(uuid.uuid4()), "kind": "local",
                        "base_url": upstream.url, "api_key": upstream.secret}
                    url = f'/api/local/agent/tasks/{row["id"]}/source-credential'
                    assert client.post(url, json=payload).status_code == 401
                    response = client.post(url, json=payload, headers={"x-fixture-owner": "yes"})
                    assert response.status_code == 200
                    form_results.append(response.json())
                    assert sources.read() == {} and not upstream.audit
                elif row["status"] == "waiting_approval":
                    approval = row["pending_approval"]
                    assert approval["tool"] == "ryn_commit_source_draft"
                    assert len(upstream.audit) == 6 and not sources.read()
                    approvals.append(approval)
                    runtime.action(row["id"], {"action": "approve", "approval_id": approval["id"], "expected_control_revision": row["control_revision"]})
                elif row["status"] not in {"queued", "running"}:
                    break
                runtime.run_once()
        final = runtime.get(task["id"])
        assert final["status"] == "completed" and final["outcome"]["status"] == "verified", final.get("error")
        assert len(form_results) == len(approvals) == 1
        assert len(upstream.audit) == 8 and [a["saved"] for a in upstream.audit] == [False] * 6 + [True] * 2
        saved = next(iter(sources.read().values()))
        assert len(sources.read()) == 1 and saved["name"] == "source-new"
        assert saved["models"] == ["alpha", "beta"] and saved["shared_models"] == []
        assert saved["api_key"] == upstream.secret
        assert final["budget"]["lifetime"]["model_calls"] == 14  # six planner/checker calls + eight real probes
        assert upstream.secret not in json.dumps([requests, final, form_results, approvals, upstream.audit])
        assert upstream.secret.encode() not in sources.path.read_bytes()
        assert upstream.secret.encode() not in tools.source_drafts.path.read_bytes()
