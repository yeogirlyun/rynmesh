"""Ordinary peer tasks retain their initial recipient binding without resource grants."""
import copy
import json

import pytest
from test_agent_peer_scope import consumer, paired, resources  # noqa: F401
from test_agent_runtime import Tools, compaction_history
from test_agent_runtime_model_switch import body
from test_agent_space_binding import nodes, space_pair  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package.task_protocol import open_task


def reopen(runtime):
    return AgentRuntime(AgentStore(runtime.store.root, runtime.store.key), runtime.model,
                        runtime.tools, runtime.catalog, consumer_node_id=runtime.consumer_node_id)


def assert_empty_scope(runtime, task_id):
    row = runtime.store.read()["tasks"][task_id]
    assert row["scope"]["resource_grants"] == row["scope"]["egress_grants"] == []
    return row


def semantic_transport(p):
    original = p.providers["a"].adapter.inner.chat
    def chat(request, **options):
        result = original(request, **options)
        if not request.get("tools"):
            result["message"]["content"] = json.dumps({"status": "passed", "summary": "Synthetic answer assessment",
                "uncovered_requirements": [], "evidence_refs": []})
        return result
    p.providers["a"].adapter.inner.chat = chat


@pytest.mark.parametrize("explicit", [False, True])
def test_plain_peer_task_main_and_checker_carry_exact_permission_without_resource_grants(consumer, explicit):  # noqa: F811
    p, runtime, model, _, envelopes, _ = consumer
    semantic_transport(p)
    runtime.update_settings({"verify_text_responses": True})
    created = runtime.create({"message": "Give a short synthetic answer.", **({"model_id": model["id"]} if explicit else {})})
    row = assert_empty_scope(runtime, created["id"])
    assert row["model_destination_required"] is True
    assert not row.get("model_destination_confirmed")
    for _ in range(3):
        runtime.run_once()
    assert runtime.get(created["id"])["outcome"]["semantic_assessment"]["status"] == "passed"
    assert len(envelopes) == len(p.traces) == 2
    assert p.traces[0]["tools"] and not p.traces[1].get("tools")
    for wire in envelopes:
        assert wire["expected_model_binding"] == model["expected_model_binding"]
        assert wire["ai_permission"] == model["expected_model_binding"]["ai_permission"]
    assert assert_empty_scope(runtime, created["id"])["model_destination"] == row["model_destination"]


@pytest.mark.parametrize("legacy", [False, True])
def test_reopened_plain_peer_task_uses_saved_binding_and_rejects_regrant(consumer, legacy):  # noqa: F811
    p, runtime, model, _, envelopes, _ = consumer
    created = runtime.create({"message": "Keep the original destination."})
    saved = assert_empty_scope(runtime, created["id"])["model_destination"]
    if legacy:
        runtime.store.transaction(lambda data: data["tasks"][created["id"]].pop("model_destination_required"))
    runtime = reopen(runtime)
    runtime.reconcile()
    runtime.run_once()
    assert len(envelopes) == len(p.traces) == 1
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=False, expected_revision=1)
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=True, expected_revision=2)
    p.consumer.refresh(p.alice.peer_id)
    runtime = reopen(runtime)
    runtime.reconcile()
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime._validate_context(runtime.store.read()["tasks"][created["id"]])
    runtime.run_once()  # The pending checker must not send old history to the new grant.
    assert len(envelopes) == len(p.traces) == 1
    assert assert_empty_scope(runtime, created["id"])["model_destination"] == saved


@pytest.mark.parametrize("tamper", ["binding_flag", "recipient", "missing_snapshot", "unsupported_bridge"])
def test_plain_peer_task_fails_closed_when_binding_is_unverifiable(consumer, tamper):  # noqa: F811
    _, runtime, model, _, envelopes, _ = consumer
    if tamper == "missing_snapshot":
        created = runtime.create({"message": "Old unbound task"})
        def old(data):
            row = data["tasks"][created["id"]]
            row.pop("model_destination_required")
            row.pop("model_destination")
        runtime.store.transaction(old)
        runtime = reopen(runtime)
        runtime.run_once()
        assert runtime.get(created["id"])["error"] == "agent_model_destination_unverifiable"
    else:
        altered = copy.deepcopy(model)
        if tamper == "binding_flag":
            altered["destination_binding_supported"] = False
        elif tamper == "recipient":
            altered["expected_model_binding"]["consumer_peer_id"] = "other-recipient"
        else:
            runtime.model.supports_destination_binding = False
        runtime.catalog.list_models = lambda **_kwargs: [altered]
        with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
            runtime.create({"message": "No documents, still needs a bound peer"})
    assert not envelopes


def test_plain_peer_switch_rebinds_only_after_explicit_confirmation(consumer):  # noqa: F811
    p, runtime, model, _, envelopes, _ = consumer
    task_id = runtime.create({"message": "Use the selected peer"})["id"]
    runtime.action(task_id, {"action": "pause"})
    before = assert_empty_scope(runtime, task_id)["model_destination"]
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=False, expected_revision=1)
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=True, expected_revision=2)
    p.consumer.refresh(p.alice.peer_id)
    runtime.switch_model(task_id, body(runtime, task_id, model["id"]))
    row = assert_empty_scope(runtime, task_id)
    assert row["model_destination"] != before and row["model_destination_confirmed"]
    assert not envelopes
    runtime.action(task_id, {"action": "resume", "expected_control_revision": row["control_revision"]})
    runtime.run_once()
    assert len(envelopes) == 1 and envelopes[0]["ai_permission"]["revision"] == 3


def test_plain_peer_summary_and_following_main_keep_binding_and_deadline(consumer):  # noqa: F811
    p, runtime, model, _, envelopes, _ = consumer
    deadlines = []
    complete = runtime.model.complete
    def capture(*args, **options):
        deadlines.append(options["deadline"])
        return complete(*args, **options)
    runtime.model.complete = capture
    runtime.tools = Tools()  # Small synthetic local tool menu, actual signed peer transport.
    runtime.update_settings({"max_context_chars": 8000})
    task_id = runtime.create({"message": "Inspect only"})["id"]
    compaction_history(runtime, task_id)
    runtime.run_once()
    current = runtime.get(task_id)
    assert current["status"] == "completed", current.get("error")
    assert any(event["type"] == "context_compacted" for event in current["events"])
    assert len(envelopes) == len(p.traces) == 2
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert not p.traces[0].get("tools") and p.traces[1]["tools"]
    assert all(wire["expected_model_binding"] == model["expected_model_binding"] for wire in envelopes)
    row = assert_empty_scope(runtime, task_id)
    assert not any(key.startswith("_run_") for key in row)


def test_plain_space_peer_task_binds_membership_and_rejects_policy_aba(space_pair, monkeypatch, tmp_path):  # noqa: F811
    p = space_pair
    p.consumer.refresh(p.locator)
    envelopes = []
    async def exchange(*, signed_request, **kwargs):
        _, value = open_task(signed_request, recipient_peer_id=p.root.store.peer_id,
                             recipient_messaging_key=p.keys[0], expected_kind="llm_request")
        envelopes.append(copy.deepcopy(value))
        return p.provider.handle(signed_request, on_event=lambda _: None), {"transport": "local_signed_fixture", "relay_used": False}
    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", exchange)
    bridge, tools, catalog = build_agent_integrations(p.apps[1], p.child.store, p.child.store.home)
    runtime = AgentRuntime(AgentStore(tmp_path / "plain-space-task", p.keys[1]), bridge, tools, catalog,
                           consumer_node_id=p.child.store.peer_id)
    model = next(row for row in catalog.list_models() if row["source"] == "peer")
    runtime.update_settings({"enabled": True, "model_id": model["id"]})
    task_id = runtime.create({"message": "Ordinary task with no document scope"})["id"]
    runtime.run_once()
    assert len(envelopes) == len(p.traces) == 1
    assert envelopes[0]["expected_model_binding"] == model["expected_model_binding"]
    assert "space_permission" in envelopes[0]["expected_model_binding"]
    assert_empty_scope(runtime, task_id)
    p.child.set_policy("local")
    p.child.set_policy("space")
    p.consumer.refresh(p.locator)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime._validate_context(runtime.store.read()["tasks"][task_id])
    runtime.run_once()
    assert len(envelopes) == len(p.traces) == 1
