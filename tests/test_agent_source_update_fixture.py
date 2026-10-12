"""C02 controlled faults exercise real HTTP and production encrypted sources."""
import copy
import importlib.util
from pathlib import Path

import pytest
from test_agent_integrations import node as integration_node
from test_agent_source_drafts import credential, draft, locator, run

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.verification import finish, record
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter

node = integration_node
_spec = importlib.util.spec_from_file_location("source_update_world", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/real-model/source_update_case.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.mark.parametrize("scenario", ["pre_save_failure", "post_save_conflict"])
def test_actual_candidate_failure_and_concurrent_owner_preservation(node, scenario):
    app, store = node
    _, tools, _ = build_agent_integrations(app, store, store.home)
    sources = app.state.llm_agent_services["source_store"]
    context = {"task_id": "source-update-task", "owner_id": "local_owner", "consumer_node_id": store.peer_id,
        "goal_revision": 1, "scope_revision": 1, "resource_grants": [], "egress_grants": [],
        "_model_probe": lambda invoke, **bounds: invoke(bounds["max_output_tokens"], 15, lambda: None)}
    with _module.SourceUpdateWorld(sources, scenario) as world:
        world.attach(tools)
        config = {"name": "source-alpha", "kind": "local", "base_url": world.candidate.url, "models": ["alpha", "beta", "gamma"]}
        credential(tools, context, config, source_id=world.source_id, api_key=world.candidate.secret)
        value = draft(tools, context, config, source_id=world.source_id)
        tested = run(tools, context, "ryn_test_source_draft", locator(value), "update-test")
        approval = {"tool": "ryn_commit_source_draft", "preview": tools.preview("ryn_commit_source_draft", locator(value), context=context)}
        assert world.approve(approval) is (scenario == "post_save_conflict")
        assert not world.approve({**approval, "preview": {**approval["preview"], "expected_source_revision": "forged"}})
        bad = copy.deepcopy(approval)
        bad["preview"]["configuration"]["enabled"] = False
        assert not world.approve(bad)
        classification = {"goal": {"revision": 1}, "scope_revision": 1, "context_epoch": 0, "criteria": [
            {"id": "goal-coverage", "required": True, "kind": "semantic", "status": "unknown", "evidence_refs": []},
            {"id": "prepare", "required": True, "kind": "mechanical", "status": "passed", "description": "Draft exists",
             "source": "tool", "source_ref": "receipt_prepare", "goal_revision": 1, "evidence_refs": ["prepare-proof"]},
        ]}
        record(classification, {"operation_id": "prepare", "name": "ryn_prepare_source_draft",
            "arguments": {"configuration": config}, "effect": "read"}, "confirmed", "fixed", result=value)
        if scenario == "pre_save_failure":
            assert tested["test"]["chat_passed"] is False
            with pytest.raises(AgentToolRejected, match="agent_source_test_required"):
                run(tools, context, "ryn_commit_source_draft", locator(value), "update-save")
            actual = world.evidence()
            assert actual["original_unchanged"] and actual["source_version"] == 7 and actual["config_saves"] == 0
            assert [r["status"] for r in actual["candidate_audit"]] == [503] * 3
            classification["criteria"].append({"id": "probe", "required": True, "kind": "mechanical", "status": "failed",
                "description": "Candidate probe failed", "evidence_refs": ["probe-proof"]})
            finish(classification)
            assert classification["outcome"]["status"] == "not_achieved"
            assert all(receipt["effect"] == "read" for receipt in classification["receipts"])
            return
        assert tested["test"]["chat_passed"]
        result = run(tools, context, "ryn_commit_source_draft", locator(value), "update-save")
        assert world.evidence()["source_version"] == 8
        saved = sources.read()[world.source_id]
        old_adapter = ModelDispatchAdapter(sources, saved, "alpha", sources.adapter(saved, "alpha"))
        checked = tools.verify({"tool": "ryn_commit_source_draft", "arguments": locator(value), "result": result},
            [{"verifier": "saved_source_state"}], context=context, cancelled=lambda: False)
        assert checked["status"] == "failed" and not checked["evidence"]["post_save_retest"]["chat_passed"]
        actual = world.evidence()
        assert actual["source_version"] == 9 and actual["endpoint"] == "endpoint-owner-newer"
        assert actual["config_saves"] == 1 and len(actual["owner_interventions"]) == 1
        assert [r["status"] for r in actual["candidate_audit"]] == [200] * 3 + [503] * 3
        assert [r["saved"] for r in actual["candidate_audit"]] == [False] * 3 + [True] * 3
        record(classification, {"operation_id": "update-save", "name": "ryn_commit_source_draft",
            "arguments": locator(value), "effect": "write"}, "confirmed", "fixed", result=result)
        classification["criteria"].append({"id": "saved-source", "source": "tool", "source_ref": "receipt_update-save",
            "goal_revision": 1, "required": True, "kind": "mechanical", "status": checked["status"],
            "description": "Saved source and post-save probe", "evidence_refs": ["save-proof"]})
        latest_source = sources.read()[world.source_id]
        finish(classification)
        assert classification["outcome"]["status"] == "not_achieved"
        saved_receipt = next(receipt for receipt in classification["receipts"] if receipt["effect"] == "write")
        assert saved_receipt["status"] == "confirmed" and saved_receipt["result"] == result
        assert sources.read()[world.source_id] == latest_source
        # Actual admission guard denies the stale capability before any HTTP.
        with pytest.raises(AdapterError, match="model_configuration_changed"):
            old_adapter.chat({"messages": [{"role": "user", "content": "OK"}]}, task_id="stale-capability", timeout_s=1)
        assert len(world.candidate.audit) == 6 and not world.newer.audit
        with pytest.raises(AgentToolRejected, match="agent_source_draft_immutable"):
            run(tools, context, "ryn_commit_source_draft", locator(value), "repeat-save")
        assert world.evidence()["source_version"] == 9 and world.evidence()["config_saves"] == 1
        listed = tools.source_drafts.list_sources()["sources"][0]
        assert listed["source_revision"] == actual["source_revision"]
        assert listed["fixture_identity"]["source_version"] == 9
