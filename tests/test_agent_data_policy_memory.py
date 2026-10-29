"""Restricted task data never reaches memory dispatch, late commits or derivatives."""
import socket
from types import SimpleNamespace

import pytest
from test_agent_memory_store import _make_store, _seed_task

from rynmesh.agent_runtime import automation_worker, automations, memory
from rynmesh.agent_runtime.data_policy import memory_blocked, merge_state, snapshot_source
from rynmesh.agent_runtime.memory_worker import MemoryExtractionWorker
from rynmesh.agent_runtime.state import initialize
from rynmesh.agent_runtime.store import AgentError, AgentStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in data policy memory tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


POLICIES = [{"memory": "deny"}, {"models": {"mode": "allowlist", "allow": [{"on_device": True}]}, "memory": "allow"}]
DEST = {"catalog_model_id": "local/test", "provider_peer_id": "self", "source_id": "test", "destination_revision": "r1"}
PROPOSALS = [{"category": "equipment", "fact_key": "personal.gear.camera_body", "text": "Synthetic camera", "source_message_ids": ["owner"]}]


def restrict(row, policy=POLICIES[0]):
    row["data_policy_state"] = merge_state(row.get("data_policy_state"), snapshot_source({"id": "remote", "instance_id": "installation", "data_policy": policy}))


def seed(data):
    data.setdefault("settings", {})["enabled"] = True
    memory.update_memory_settings(data, {"capture_enabled": True, "use_enabled": True, "extraction_destination": DEST})
    row = _seed_task(data, task_id="source", message_id="owner", text="My camera is synthetic.", destination=DEST)
    row["limits"] = {}
    initialize(row)
    return row


def runtime(store):
    def forbidden(*args, **kwargs):
        raise AssertionError("Restricted content reached the memory model")
    model = SimpleNamespace(complete=forbidden, supports_destination_binding=True)
    metadata = {"id": DEST["catalog_model_id"], "source": "local_api", "available": True, "agent_supported": True}
    return SimpleNamespace(store=store, model=model, _model_metadata=lambda _: metadata,
                           _bound_destination=lambda _: DEST, _destination=lambda _: DEST)


@pytest.mark.parametrize("policy", POLICIES)
def test_blocked_task_cannot_enqueue_or_relearn_and_worker_never_discloses(tmp_path, policy):
    store, _ = _make_store(tmp_path)
    def setup(data):
        row = seed(data); restrict(row, policy)
        row["messages"].extend([{"role": "tool", "content": "restricted tool output"},
                                {"role": "assistant", "content": "restricted paraphrase", "summary": True}])
        assert memory.enqueue_extraction_job(data, task_id=row["id"], force_relearn=True)["queued"] is False
    store.transaction(setup)
    worker = MemoryExtractionWorker(runtime(store))
    worker.run_once()
    state = store.read()["memory"]
    assert not state["jobs"] and not state["candidates"] and not state["entries"]


@pytest.mark.parametrize("policy", POLICIES)
def test_policy_arriving_between_claim_and_dispatch_cancels_real_worker(tmp_path, policy):
    store, _ = _make_store(tmp_path)
    store.transaction(seed)
    worker = MemoryExtractionWorker(runtime(store))
    original = worker._collect_and_claim
    captured = []
    def collect(destination):
        outcome = original(destination)
        assert outcome and outcome[0] == "dispatch"
        captured.append(outcome)
        def tighten(data):
            row = data["tasks"]["source"]; restrict(row, policy)
            memory.invalidate_task_policy_in_tx(data, row)
        store.transaction(tighten)
        return outcome
    worker._collect_and_claim = collect
    worker.run_once()
    assert captured
    state = store.read()["memory"]
    assert all(item["state"] == "stale" for item in state["jobs"].values())
    assert not state["candidates"] and not state["entries"]


@pytest.mark.parametrize("policy", POLICIES)
def test_late_extractor_commit_rechecks_policy_without_relying_on_invalidation_hook(tmp_path, policy):
    store, _ = _make_store(tmp_path)
    def setup(data):
        row = seed(data)
        memory.enqueue_extraction_job(data, task_id=row["id"])
        return memory.claim_extraction_job(data, worker_id="worker")
    leased = store.transaction(setup)
    store.transaction(lambda data: restrict(data["tasks"]["source"], policy))
    result = store.transaction(lambda data: memory.commit_extraction_job(data, job_id=leased["id"], worker_id="worker",
                               lease_token=leased["lease_token"], proposals=PROPOSALS))
    assert result["state"] == "stale" and not result["committed_candidates"]
    assert not store.read()["memory"]["entries"]


@pytest.mark.parametrize("use_hook", [True, False])
def test_pending_candidate_cannot_be_approved_after_policy_tightens(tmp_path, use_hook):
    store, _ = _make_store(tmp_path)
    def setup(data):
        row = seed(data)
        memory.enqueue_extraction_job(data, task_id=row["id"])
        leased = memory.claim_extraction_job(data, worker_id="worker")
        result = memory.commit_extraction_job(data, job_id=leased["id"], worker_id="worker", lease_token=leased["lease_token"], proposals=PROPOSALS)
        return result["committed_candidates"][0]["id"]
    candidate_id = store.transaction(setup)
    def tighten(data):
        row = data["tasks"]["source"]; restrict(row)
        if use_hook:
            assert memory.invalidate_task_policy_in_tx(data, row)
    store.transaction(tighten)
    with pytest.raises(AgentError):
        store.transaction(lambda data: memory.decide_candidate(data, candidate_id, decision="accept"))
    assert not store.read()["memory"]["entries"]


def test_automation_created_before_restriction_inherits_before_next_run_and_updates_existing_children(tmp_path):
    store, key = _make_store(tmp_path); rt = runtime(store)
    def setup(data):
        row = seed(data)
        item = automations.execute_automation_tool_call_in_tx(store, data, row,
            {"action": "create", "kind": "cron", "name": "Derived", "prompt": "Synthetic derived task", "repeat": "daily"})
        automation_worker.trigger_automation_in_tx(rt, data, item["id"], force=True)
        return item["id"]
    auto_id = store.transaction(setup)
    def tighten(data):
        row = data["tasks"]["source"]; restrict(row)
        automations.inherit_task_policy_in_tx(data, row)
        assert memory_blocked(data["automations"][auto_id])
        child = next(item for item in data["tasks"].values() if item.get("automation_id") == auto_id)
        assert memory_blocked(child)
    store.transaction(tighten)
    restored = AgentStore(tmp_path, key)
    def trigger(data):
        automation_worker.trigger_automation_in_tx(runtime(restored), data, auto_id, force=True)
        assert all(memory_blocked(row) for row in data["tasks"].values() if row.get("automation_id") == auto_id)
        item = data["automations"][auto_id]
        parsed = automations.parse_automation_toml(automations.serialize_automation_toml(item))
        assert memory_blocked(automations.normalize_automation_record(restored, parsed))
        clean = {**parsed, "data_policy_state_json": "null", "prompt": "Edited on disk"}
        assert memory_blocked(automations.normalize_automation_record(restored, clean, existing=item))
    restored.transaction(trigger)


@pytest.mark.parametrize("kind", ["heartbeat", "cron"])
@pytest.mark.parametrize("policy", POLICIES)
def test_automation_memory_files_never_read_or_appended_for_restricted_runs(tmp_path, kind, policy, monkeypatch):
    store, _ = _make_store(tmp_path)
    def setup(data):
        row = seed(data); restrict(row, policy)
        saved = automations.execute_automation_tool_call_in_tx(store, data, row,
            {"action": "create", "kind": kind, "name": "Restricted", "prompt": "Synthetic", "repeat": "daily"})
        assert not automations.automation_memory_path(store, saved["id"]).exists()
        automation_worker.trigger_automation_in_tx(runtime(store), data, saved["id"], force=True)
        spawned = next(item for item in data["tasks"].values() if item.get("automation_id") == saved["id"])
        assert memory_blocked(spawned)
        return spawned["id"]
    task_id = store.transaction(setup)
    def forbidden(*args, **kwargs):
        raise AssertionError("Restricted automation accessed memory.md")
    monkeypatch.setattr(automations, "read_automation_memory", forbidden)
    monkeypatch.setattr(automations, "append_automation_memory_note", forbidden)
    row = store.read()["tasks"][task_id]
    instructions = automations.automation_developer_instructions(store, row)
    assert "Long-term memory is disabled" in instructions and "memory_file:" not in instructions
    def finish(data):
        row = data["tasks"][task_id]
        row.update(status="completed", messages=[{"role": "assistant", "content": 'Derived summary. ::inbox-item{title="Done" summary="Derived summary"}'}])
        automations.settle_automation_turn_in_tx(store, data, row)
    store.transaction(finish)


def test_listing_restricted_automation_tightens_the_receiving_task_before_return(tmp_path):
    store, _ = _make_store(tmp_path)
    def check(data):
        source = seed(data); restrict(source)
        automations.execute_automation_tool_call_in_tx(store, data, source,
            {"action": "create", "kind": "cron", "prompt": "Restricted derived instruction", "repeat": "daily"})
        receiver = _seed_task(data, task_id="receiver")
        assert not memory_blocked(receiver)
        result = automations.execute_automation_tool_call_in_tx(store, data, receiver, {"action": "list"})
        assert result["items"] and memory_blocked(receiver)
    store.transaction(check)


def test_automation_policy_cycles_converge_without_discarding_other_sources():
    first = {"id": "first", "automation_id": "a"}
    second = {"id": "second", "automation_id": "b"}
    restrict(first)
    second["data_policy_state"] = merge_state(None, snapshot_source({"id": "second-source", "instance_id": "second-installation", "data_policy": POLICIES[1]}))
    data = {"tasks": {"first": first, "second": second}, "automations": {
        "a": {"id": "a", "source_task_ids": ["second"]},
        "b": {"id": "b", "source_task_ids": ["first"]}}}
    automations.inherit_task_policy_in_tx(data, first)
    automations.inherit_task_policy_in_tx(data, second)
    assert all(len(row["data_policy_state"]["sources"]) == 2 for row in data["tasks"].values())
    revisions = [row["data_policy_state"]["revision"] for row in data["tasks"].values()]
    automations.inherit_task_policy_in_tx(data, first)
    assert revisions == [row["data_policy_state"]["revision"] for row in data["tasks"].values()]
