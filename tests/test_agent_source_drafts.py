import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source
from test_agent_integrations import node as integration_node
from test_agent_runtime import assistant

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.services import peer_box

node = integration_node


@pytest.fixture
def setup(node, monkeypatch):
    app, store = node
    _, tools, _ = build_agent_integrations(app, store, store.home)
    context = {"task_id": "source-task", "owner_id": "local_owner", "consumer_node_id": store.peer_id,
               "goal_revision": 1, "scope_revision": 1, "resource_grants": [], "egress_grants": [],
               "_model_probe": lambda invoke, **bounds: invoke(bounds["max_output_tokens"], 15, lambda: None)}
    sources = app.state.llm_agent_services["source_store"]
    requests = []
    class Adapter:
        def __init__(self, record, model):
            self.model, self.record = model, record
        def chat(self, body, *, task_id, timeout_s):
            requests.append({"model": self.model, "body": copy.deepcopy(body), "timeout_s": timeout_s})
            if self.model == "broken":
                raise AdapterError("secret-api-key-must-not-escape and private server response")
            if body.get("tool_choice") == "required":
                return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "probe", "type": "function", "function": {
                    "name": "ryn_capability_probe", "arguments": '{"value":"ryn-tool-test"}'}}]}}
            return {"text": "OK", "message": {"role": "assistant", "content": "OK"}}
        def cancel(self, task_id):
            return True
    monkeypatch.setattr(sources, "adapter", lambda record, model="": Adapter(record, model))
    config = {"name": "Draft API", "kind": "api", "base_url": "https://vendor.test/v1", "models": ["same-name", "model-b"]}
    return tools, context, sources, config, requests


def credential(tools, context, config, **extra):
    return tools.create_source_credential(context["task_id"], {"client_request_id": "credential-request", "kind": config["kind"],
        "base_url": config["base_url"], "api_key": "secret-api-key-must-not-escape", **extra},
        **{k: context[k] for k in ("owner_id", "consumer_node_id", "goal_revision")})


def run(tools, context, name, args, op):
    return tools.execute(name, args, operation_id=op, context=context, cancelled=lambda: False,
                         expected_preview=tools.preview(name, args, context=context))


def draft(tools, context, config, **extra):
    return run(tools, context, "ryn_prepare_source_draft", {"configuration": config, **extra}, "op-prepare")


def locator(row):
    return {"draft_id": row["draft_id"], "expected_draft_version": row["version"]}


def test_owner_credentials_new_multiple_models_test_save_retest_and_encrypted_state(setup):
    tools, context, sources, config, requests = setup
    value = credential(tools, context, config)
    assert value["has_credential"] and "api_key" not in value and "credential_ref" not in value
    row = draft(tools, context, config)
    assert sources.read() == {} and row["has_credential"]
    test = run(tools, context, "ryn_test_source_draft", {**locator(row), "test_tools": True}, "op-test")
    assert test["test"]["chat_passed"] and len(requests) == 6
    result = run(tools, context, "ryn_commit_source_draft", locator(row), "op-save")
    saved = sources.read()[result["source_id"]]
    assert saved["models"] == config["models"] and saved["shared_models"] == []
    assert all(value == "supported" for value in saved["tool_support"].values())
    checked = tools.verify({"tool": "ryn_commit_source_draft", "arguments": locator(row), "result": result},
                           [{"verifier": "saved_source_state"}], context=context, cancelled=lambda: False)
    assert checked["status"] == "passed" and len(requests) == 8
    assert all(request["timeout_s"] <= 15 for request in requests)
    assert "secret-api-key" not in json.dumps([value, row, test, result, checked, requests])
    assert b"secret-api-key" not in tools.source_drafts.path.read_bytes()
    assert "source-task" not in sources.path.read_bytes().decode()


def test_budget_output_admission_caps_every_chat_and_tool_probe(setup):
    tools, context, sources, config, requests = setup
    admitted = []
    def meter(invoke, **bounds):
        admitted.append(bounds)
        return invoke(64, 15, lambda: None)
    context = {**context, "_model_probe": meter}
    credential(tools, context, config)
    row = draft(tools, context, config)
    tested = run(tools, context, "ryn_test_source_draft", {**locator(row), "test_tools": True}, "op-test")
    assert tested["test"]["chat_passed"] and len(requests) == len(admitted) == 6
    for actual, bounds in zip(requests, admitted, strict=True):
        assert actual["body"]["max_tokens"] == 64
        assert bounds["model_id"] == actual["model"]
        assert bounds["input_bytes"] == len(encoded({**actual["body"], "max_tokens": bounds["max_output_tokens"]}))
    assert sources.read() == {}


def test_failed_pre_save_probe_never_changes_existing_source(setup, node):
    tools, context, sources, config, requests = setup
    app, _ = node
    existing, _ = add_source(app)
    before = copy.deepcopy(sources.read())
    config = {**config, "models": ["broken"]}
    credential(tools, context, config, source_id=existing["id"])
    row = draft(tools, context, config, source_id=existing["id"])
    tested = run(tools, context, "ryn_test_source_draft", locator(row), "op-test")
    assert not tested["test"]["chat_passed"] and "private server" not in str(tested)
    with pytest.raises(AgentToolRejected, match="agent_source_test_required"):
        run(tools, context, "ryn_commit_source_draft", locator(row), "op-save")
    assert sources.read() == before and len(requests) == 1


def test_model_cannot_supply_secrets_or_move_credential_to_other_destination(setup):
    tools, context, _sources, config, _requests = setup
    credential(tools, context, config)
    with pytest.raises(AgentToolRejected, match="agent_tool_arguments_invalid"):
        draft(tools, context, {**config, "api_key": "model-supplied"})
    with pytest.raises(AgentToolRejected, match="agent_source_owner_credential_required"):
        draft(tools, context, {**config, "base_url": "https://different.test/v1"})
    for replacement in ({"task_id": "other-task"}, {"goal_revision": 2}, {"owner_id": "other"}):
        with pytest.raises(AgentToolRejected, match="agent_source_owner_credential_required"):
            draft(tools, {**context, **replacement}, config)


def test_edit_after_test_invalidates_evidence_and_changed_credential_invalidates_draft(setup):
    tools, context, _sources, config, _requests = setup
    credential(tools, context, config)
    row = draft(tools, context, config)
    run(tools, context, "ryn_test_source_draft", locator(row), "op-test")
    edited = run(tools, context, "ryn_prepare_source_draft", {**locator(row), "configuration": {**config, "models": ["model-c"]}}, "op-edit")
    assert edited["version"] == 2 and edited["test"] is None
    with pytest.raises(AgentToolRejected, match="agent_source_test_required"):
        run(tools, context, "ryn_commit_source_draft", locator(edited), "op-save")
    credential(tools, context, config, client_request_id="new-credential-request", api_key="replacement-secret")
    with pytest.raises(AgentToolRejected, match="agent_source_credential_changed"):
        tools.preview("ryn_test_source_draft", locator(edited), context=context)


def test_concurrent_source_edit_blocks_commit_without_overwrite(setup, node):
    tools, context, sources, config, _requests = setup
    app, _ = node
    existing, _ = add_source(app)
    credential(tools, context, config, source_id=existing["id"])
    row = draft(tools, context, config, source_id=existing["id"])
    run(tools, context, "ryn_test_source_draft", locator(row), "op-test")
    current = sources.read()[existing["id"]]
    sources.save({**current, "name": "Concurrent owner edit"})
    before = sources.read()
    with pytest.raises(AgentToolRejected, match="agent_source_revision_conflict"):
        run(tools, context, "ryn_commit_source_draft", locator(row), "op-save")
    assert sources.read() == before


def test_post_save_failed_retest_retains_exact_saved_config_and_never_rolls_back(setup, monkeypatch):
    tools, context, sources, config, _requests = setup
    credential(tools, context, config)
    row = draft(tools, context, config)
    run(tools, context, "ryn_test_source_draft", locator(row), "op-test")
    result = run(tools, context, "ryn_commit_source_draft", locator(row), "op-save")
    before = sources.read()
    class Offline:
        def chat(self, *a, **kw):
            raise AdapterError("private failure secret")
        def cancel(self, task_id):
            return True
    monkeypatch.setattr(sources, "adapter", lambda *a: Offline())
    checked = tools.verify({"tool": "ryn_commit_source_draft", "arguments": locator(row), "result": result},
        [{"verifier": "saved_source_state"}], context=context, cancelled=lambda: False)
    assert checked["status"] == "failed" and checked["evidence"]["saved_configuration_retained"]
    assert sources.read() == before and "private failure" not in str(checked)


def test_credential_idempotency_expiry_revoke_and_task_deletion(setup, monkeypatch):
    tools, context, _sources, config, _requests = setup
    first = credential(tools, context, config)
    assert credential(tools, context, config) == first
    with pytest.raises(AgentToolRejected, match="agent_source_credential_request_conflict"):
        credential(tools, context, config, api_key="different")
    row = draft(tools, context, config)
    tools.revoke_source_credential(context["task_id"], {"client_request_id": "revoke-request", "kind": "api", "base_url": config["base_url"]},
        **{k: context[k] for k in ("owner_id", "consumer_node_id", "goal_revision")})
    with pytest.raises(AgentToolRejected, match="agent_source_owner_credential_required"):
        tools.preview("ryn_test_source_draft", locator(row), context=context)
    tools.forget_task(context["task_id"], operation_ids=["op-prepare"])
    state = tools.source_drafts._read()
    assert not state["credentials"] and not state["drafts"] and not state["requests"]


def test_expired_credentials_and_old_preview_cannot_dispatch_tests(setup, monkeypatch):
    tools, context, _sources, config, requests = setup
    created = credential(tools, context, config)
    row = draft(tools, context, config)
    preview = tools.preview("ryn_test_source_draft", locator(row), context=context)
    monkeypatch.setattr("rynmesh.agent_runtime.source_drafts.time.time", lambda: created["expires_at"] + 1)
    with pytest.raises(AgentToolRejected, match="agent_source_owner_credential_required"):
        tools.execute("ryn_test_source_draft", locator(row), operation_id="expired-test", context=context,
                      expected_preview=preview, cancelled=lambda: False)
    assert requests == []


def test_reused_key_cannot_cross_concurrent_source_rotation(setup, node, monkeypatch):
    tools, context, sources, config, _requests = setup
    existing, _ = add_source(node[0])
    entered, release = threading.Event(), threading.Event()
    original_write = tools.source_drafts._write
    def pause_slot_persistence(value):
        entered.set()
        assert release.wait(5)
        return original_write(value)
    monkeypatch.setattr(tools.source_drafts, "_write", pause_slot_persistence)
    body = {"client_request_id": "reuse-credential", "kind": config["kind"], "base_url": config["base_url"],
            "source_id": existing["id"], "reuse_existing": True, "expected_source_revision": existing["source_revision"]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.create_source_credential, context["task_id"], body,
            **{k: context[k] for k in ("owner_id", "consumer_node_id", "goal_revision")})
        assert entered.wait(5)
        sources.save({**sources.read()[existing["id"]], "api_key": "owner-rotated-new-key"})
        release.set()
        future.result(timeout=5)
    with pytest.raises(AgentToolRejected, match="agent_source_reused_credential_stale"):
        draft(tools, context, config, source_id=existing["id"])
    assert sources.read()[existing["id"]]["api_key"] == "owner-rotated-new-key"


def test_approved_commit_advances_only_its_own_reused_key_binding(setup, node):
    tools, context, sources, config, _requests = setup
    existing, _ = add_source(node[0])
    tools.create_source_credential(context["task_id"], {"client_request_id": "reuse-key-request", "kind": "api", "base_url": config["base_url"],
        "source_id": existing["id"], "reuse_existing": True}, **{k: context[k] for k in ("owner_id", "consumer_node_id", "goal_revision")})
    row = draft(tools, context, config, source_id=existing["id"])
    run(tools, context, "ryn_test_source_draft", locator(row), "op-test")
    saved = run(tools, context, "ryn_commit_source_draft", locator(row), "op-save")
    receipt = {"tool": "ryn_commit_source_draft", "arguments": locator(row), "result": saved}
    assert tools.verify(receipt, [{"verifier": "saved_source_state"}], context=context, cancelled=lambda: False)["status"] == "passed"
    current = sources.read()[existing["id"]]
    sources.save({**current, "api_key": "independent-owner-rotation"})
    assert tools.verify(receipt, [{"verifier": "saved_source_state"}], context=context, cancelled=lambda: False)["status"] == "unknown"


def test_revoke_between_probe_requests_invalidates_evidence_without_active_mutation(setup, monkeypatch):
    tools, context, sources, config, requests = setup
    credential(tools, context, config)
    row = draft(tools, context, config)
    original = sources.adapter
    class RevokeAfterFirst:
        def __init__(self, adapter):
            self.adapter = adapter
        def chat(self, *args, **kwargs):
            result = self.adapter.chat(*args, **kwargs)
            tools.revoke_source_credential(context["task_id"], {"client_request_id": "revoke-mid-probe", "kind": "api", "base_url": config["base_url"]},
                **{k: context[k] for k in ("owner_id", "consumer_node_id", "goal_revision")})
            return result
        def cancel(self, task_id):
            return self.adapter.cancel(task_id)
    monkeypatch.setattr(sources, "adapter", lambda record, model="": RevokeAfterFirst(original(record, model)))
    result = run(tools, context, "ryn_test_source_draft", locator(row), "op-test")
    assert result["draft_only"] and result["test"]["status"] == "interrupted"
    assert not result["test"]["evidence_valid"] and result["test"]["probe_requests"] == len(requests) == 1
    assert result["test"]["cost"] == "unknown" and sources.read() == {}
    assert tools.source_drafts._read()["drafts"][row["draft_id"]]["test"] is None


@pytest.mark.parametrize("model_budget,expected_probes,expected_saved,test_tools", [
    (128, 4, True, False), (2, 0, False, False), (3, 1, False, False), (6, 2, True, False),
    (128, 8, True, True), (4, 2, False, True),
])
def test_real_runtime_source_workflow_approvals_and_registered_verifiers(setup, node, model_budget, expected_probes, expected_saved, test_tools):
    tools, initial_context, sources, config, probes = setup
    app, store = node
    class Model:
        count = 0
        def complete(self, model_id, messages, specs, cancelled, on_event):
            if not specs:
                payload = json.loads(messages[-1]["content"])
                return assistant(json.dumps({"status": "passed", "summary": "Configured two private models, tested before save and read back after save.",
                    "uncovered_requirements": [], "evidence_refs": [e["id"] for e in payload["evidence"]]}))
            self.count += 1
            if self.count == 1:
                return assistant(name="ryn_prepare_source_draft", args={"configuration": config})
            if self.count in {2, 3}:
                result = json.loads(messages[-1]["content"])
                return assistant(name="ryn_test_source_draft" if self.count == 2 else "ryn_commit_source_draft",
                                 args={**locator(result), **({"test_tools": test_tools} if self.count == 2 else {})})
            return assistant("Two models configured, both tested, both private; saved state will be independently checked.")
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "available": True, "agent_supported": True}])
    runtime = AgentRuntime(AgentStore(store.home / "source-agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                           Model(), tools, catalog, consumer_node_id=initial_context["consumer_node_id"])
    app.state.agent_runtime = runtime
    runtime.update_settings({"enabled": True, "model_id": "fixture"})
    task = runtime.create({"message": "Configure and test this source with both models; keep them private.",
                           "budget_limits": {"max_total_model_calls": model_budget}})
    context = {**initial_context, "task_id": task["id"]}
    credential(tools, context, config)
    approvals = []
    for _ in range(25):
        row = runtime.get(task["id"])
        if row["status"] == "waiting_approval":
            approval = row["pending_approval"]
            approvals.append(approval["tool"])
            assert "secret-api-key" not in json.dumps(approval)
            runtime.action(task["id"], {"action": "approve", "expected_revision": row["revision"], "approval_id": approval["id"]})
        elif row["status"] not in {"queued", "running"}:
            break
        runtime.run_once()
    row = runtime.get(task["id"])
    assert len(probes) == expected_probes and bool(sources.read()) == expected_saved
    if model_budget != 128:
        assert row["budget"]["lifetime"]["model_calls"] == model_budget
        assert row["outcome"]["status"] != "verified"
        if expected_saved:
            # Exhaustion during post-save verification cannot undo or replay a committed source.
            assert approvals == ["ryn_commit_source_draft"] and len(sources.read()) == 1
            ledger = tools.source_drafts.ledger_provider()
            receipt = next(r for r in runtime.store.read()["tasks"][task["id"]]["receipts"] if r["tool"] == "ryn_commit_source_draft")
            assert ledger.get(receipt["operation_id"])["status"] == "confirmed"
        else:
            assert row["status"] == "failed" and row["error"] == "agent_model_call_budget_exhausted"
            assert all(r["test"] is None for r in tools.source_drafts._read()["drafts"].values())
        return
    assert row["budget"]["lifetime"]["model_calls"] == 5 + expected_probes
    assert row["status"] == "completed" and row["outcome"]["status"] == "verified", {"status": row["status"], "error": row["error"]}
    assert approvals == ["ryn_commit_source_draft"]
    assert len(sources.read()) == 1
    assert next(iter(sources.read().values()))["shared_models"] == []
    assert "secret-api-key" not in json.dumps(row)


def test_missing_probe_meter_fails_closed_before_any_request(setup):
    tools, context, sources, config, requests = setup
    credential(tools, context, config)
    row = draft(tools, context, config)
    context.pop("_model_probe")
    with pytest.raises(AgentError, match="agent_probe_budget_unavailable"):
        run(tools, context, "ryn_test_source_draft", locator(row), "no-meter")
    assert requests == [] and sources.read() == {}


@pytest.mark.parametrize("stop", ["owner_cancel", "credential_revoke", "time_budget"])
def test_probe_inflight_cancellation_stops_adapter_and_invalidates_evidence(setup, monkeypatch, stop):
    tools, context, sources, config, _ = setup
    credential(tools, context, config)
    row = draft(tools, context, config)
    entered, cancelled_transport, owner_stop = threading.Event(), threading.Event(), threading.Event()
    observed = []
    class Blocking:
        def chat(self, body, *, task_id, timeout_s):
            observed.append(timeout_s)
            entered.set()
            assert cancelled_transport.wait(2)
            raise AdapterError("private transport details")
        def cancel(self, task_id):
            cancelled_transport.set()
            return True
    monkeypatch.setattr(sources, "adapter", lambda *args: Blocking())
    deadline = [float("inf")]
    def guard():
        if time.monotonic() >= deadline[0]:
            raise AgentError("agent_time_budget_exhausted")
    context["_model_probe"] = lambda invoke, **bounds: invoke(bounds["max_output_tokens"], 0.2, guard)
    def execute():
        return tools.execute("ryn_test_source_draft", locator(row), operation_id="inflight-test", context=context,
                             cancelled=owner_stop.is_set, expected_preview=tools.preview("ryn_test_source_draft", locator(row), context=context))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        assert entered.wait(1)
        if stop == "owner_cancel":
            owner_stop.set()
        elif stop == "credential_revoke":
            tools.revoke_source_credential(context["task_id"], {"client_request_id": "inflight-revoke", "kind": "api", "base_url": config["base_url"]},
                **{k: context[k] for k in ("owner_id", "consumer_node_id", "goal_revision")})
        else:
            deadline[0] = time.monotonic()
        if stop == "time_budget":
            with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
                future.result(timeout=3)
        else:
            result = future.result(timeout=3)
            assert result["test"]["status"] == "interrupted" and result["test"]["evidence_valid"] is False
    assert cancelled_transport.is_set() and len(observed) == 1 and 0 < observed[0] <= 0.2
    assert sources.read() == {} and tools.source_drafts._read()["drafts"][row["draft_id"]]["test"] is None


def test_runtime_probe_hook_caps_run_and_lifetime_remainder_without_double_counting(tmp_path):
    from test_agent_runtime import make_runtime
    runtime, _, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Test source"})["id"]
    def running(data):
        row = data["tasks"][task_id]
        row.update(status="running", elapsed_seconds=row["limits"]["max_seconds"] - 0.7,
                   in_flight={"kind": "tool", "dispatched": True})
        return row
    selected = runtime.store.transaction(running)
    selected.update(_run_started=time.monotonic(), _run_lifetime_remaining=0.4, _run_control_revision=selected["control_revision"])
    hook = runtime._tool_context(selected)["_model_probe"]
    observed = []
    def invoke(tokens, seconds, guard):
        guard()
        observed.append(seconds)
        return {"reported_usage": {"input_tokens": 4, "output_tokens": 2}}
    hook(invoke, max_output_tokens=32, input_bytes=16, model_id="draft-model", source_id="draft")
    usage = runtime.get(task_id)["budget"]["lifetime"]
    assert 0 < observed[0] <= 0.400001
    assert usage["model_calls"] == 1 and usage["total_tokens"] == 6 and usage["charged_seconds"] == 0
    selected["_run_started"] = time.monotonic() - 1
    expired = runtime._tool_context(selected)["_model_probe"]
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        expired(invoke, max_output_tokens=32, input_bytes=16, model_id="draft-model", source_id="draft")
    assert len(observed) == 1
