"""Independent branch boundary: retained prefix != discarded source suffix.

No real models or network. A genuine AgentRuntime creates every owner message
and applies the public edit API. Memory seeding invokes the real explicit-owner
helper on that exact stored row, never fabricated source IDs/hashes. The first
test can exercise the real data fence explicitly while integration is pending;
the second requires the public Runtime to pass the root transaction through.
Root should run after the writer's ready marker, not during an intermediate edit.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

sys.dont_write_bytecode = True
REPO = Path(os.environ.get("RYN_MEMORY_REPO", str(Path(__file__).resolve().parents[2]))).resolve()
sys.path.insert(0, str(REPO))
memory = importlib.import_module("rynmesh.agent_runtime.memory")
host = importlib.import_module("rynmesh.agent_runtime.memory_runtime")
runtime_module = importlib.import_module("rynmesh.agent_runtime.runtime")
store_module = importlib.import_module("rynmesh.agent_runtime.store")
models_module = importlib.import_module("rynmesh.agent_runtime.models")
for module in (memory, host, runtime_module, store_module, models_module):
    assert Path(module.__file__).resolve().is_relative_to(REPO), "Mixed checkout imports"


class RecordingFakeModel:
    supports_destination_binding = True
    supports_conversation_key = True

    def __init__(self):
        self.calls = []

    def complete(self, model_id, messages, tool_specs, cancelled, on_event=None, **options):
        assert not cancelled()
        self.calls.append({"model_id": model_id, "messages": copy.deepcopy(messages),
                           "tool_specs": copy.deepcopy(tool_specs), "options": copy.deepcopy(options)})
        return {"message": {"role": "assistant", "content": "离线验收模型已回复。"}}


class NoBusinessTools:
    def specs(self):
        return []

    def metadata(self, _name):
        raise AssertionError("No business tool should be proposed in this branch case")


class Catalog:
    def __init__(self, peer_id):
        self.row = {"id": "local:branch-fixture", "name": "Offline branch fixture",
            "source": "local_api", "peer_id": peer_id, "source_id": "synthetic-branch-source",
            "destination_revision": "synthetic-branch-rev1", "available": True,
            "agent_supported": True, "context_window": 32768, "max_output_tokens": 2048}

    def list_models(self, **_kwargs):
        return [copy.deepcopy(self.row)]

    def get_model(self, model_id):
        return copy.deepcopy(self.row) if model_id == self.row["id"] else None

    destination = staticmethod(models_module.ModelCatalog.destination)


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External network is forbidden: branch fixture uses RecordingFakeModel")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def finish(runtime, task_id):
    for _ in range(16):
        row = runtime.get(task_id)
        if row["status"] == "completed":
            return row
        assert row["status"] not in {"failed", "interrupted", "waiting_approval", "waiting_input"}, row["status"]
        runtime.run_once()
    pytest.fail("Actual Runtime did not finish the offline fixture turn")


def apply_real_owner_helper(runtime, task_id):
    result = runtime.store.transaction(lambda data: host.execute_explicit_owner_intent_in_tx(data, data["tasks"][task_id]))
    assert result.get("applied") is True and not result.get("errors"), result
    return result


def source_pairs(entries):
    return {(source["message_id"], source["content_sha256"], source["conversation_revision"])
            for entry in entries for source in entry.get("sources", []) if source.get("source_type") == "task_message"}


@pytest.fixture
def edited_branch(tmp_path):
    key = X25519PrivateKey.generate()
    store = store_module.AgentStore(tmp_path / "synthetic-node" / "agent", key)
    catalog, model = Catalog(store.pub), RecordingFakeModel()
    runtime = runtime_module.AgentRuntime(store, model, NoBusinessTools(), catalog)
    runtime.update_settings({"enabled": True, "model_id": catalog.row["id"], "permission_mode": "read_only"})
    created = runtime.create({"message": "请记住我喜欢摄影，我的机身是富士 X-T5。"})
    task_id = created["id"]
    finish(runtime, task_id)
    apply_real_owner_helper(runtime, task_id)
    initial = runtime.store.read()["tasks"][task_id]
    prefix_owner = copy.deepcopy(initial["messages"][0])
    assert prefix_owner["role"] == "user" and prefix_owner.get("message_id")
    assert initial.get("conversation_revision", 0) == 0
    prefix_entries = runtime.store.inspect(memory.list_memories_snapshot)["entries"]
    assert prefix_entries and any("X-T5" in entry["text"] for entry in prefix_entries)
    original_prefix_proofs = source_pairs(prefix_entries)
    expected_prefix_hash = hashlib.sha256(prefix_owner["content"].encode("utf-8")).hexdigest()
    assert original_prefix_proofs == {(prefix_owner["message_id"], expected_prefix_hash, 0)}

    current = runtime.get(task_id)
    runtime.add_message(task_id, {"message": "请回答：1 + 1 等于几？", "expected_revision": current["revision"]})
    finish(runtime, task_id)
    row = runtime.store.read()["tasks"][task_id]
    unrelated_index = next(index for index, message in enumerate(row["messages"])
                           if message.get("role") == "user" and "1 + 1" in message.get("content", ""))

    current = runtime.get(task_id)
    runtime.add_message(task_id, {"message": "请记住我的镜头是 XF16-55mm F2.8。", "expected_revision": current["revision"]})
    finish(runtime, task_id)
    apply_real_owner_helper(runtime, task_id)
    before = runtime.store.read()
    source_row = before["tasks"][task_id]
    suffix_owner = next(message for message in source_row["messages"]
                        if message.get("role") == "user" and "XF16-55" in message.get("content", ""))
    suffix_entries = [entry for entry in memory.list_memories_snapshot(before)["entries"] if "XF16-55" in entry["text"]]
    assert len(suffix_entries) == 1
    suffix_source = copy.deepcopy(suffix_entries[0]["sources"][0])
    assert suffix_source["message_id"] == suffix_owner["message_id"]
    assert suffix_source["content_sha256"] == hashlib.sha256(suffix_owner["content"].encode("utf-8")).hexdigest()
    assert suffix_source["conversation_revision"] == 0

    destination = catalog.destination(catalog.row)
    runtime.store.transaction(lambda data: memory.update_memory_settings(data,
        {"capture_enabled": True, "extraction_model_id": catalog.row["id"], "extraction_destination": destination}))
    queued = runtime.store.transaction(lambda data: memory.enqueue_extraction_job(
        data, task_id=task_id, message_ids=[suffix_owner["message_id"]]))
    assert queued.get("queued") is True
    leased = runtime.store.transaction(lambda data: memory.claim_extraction_job(
        data, job_id=queued["job"]["id"], worker_id="offline-branch-worker", lease_seconds=120))
    assert leased and leased["state"] == "leased"
    assert leased["source_manifest"][0]["message_id"] == suffix_owner["message_id"]

    current = runtime.get(task_id)
    # This is the actual public Runtime edit method. It authors the new owner
    # message and preserved encrypted suffix; the test never rebuilds its prefix.
    runtime.add_message(task_id, {"message": "请改成回答：2 + 2 等于几？", "edit_message_index": unrelated_index,
        "expected_revision": current["revision"], "client_request_id": str(uuid.uuid4())})
    rewritten = runtime.store.read()["tasks"][task_id]
    assert rewritten["conversation_revision"] == 1
    assert rewritten["messages"][0]["message_id"] == prefix_owner["message_id"]
    assert rewritten["messages"][0]["content"] == prefix_owner["content"]
    assert hashlib.sha256(rewritten["messages"][0]["content"].encode("utf-8")).hexdigest() == expected_prefix_hash
    branch = rewritten["conversation_branches"][-1]
    assert branch["revision"] == 0
    assert any(message.get("message_id") == suffix_owner["message_id"] for message in branch["messages"])
    return SimpleNamespace(runtime=runtime, task_id=task_id, key=key, prefix_owner=prefix_owner,
        prefix_ids={entry["id"] for entry in prefix_entries}, prefix_source_pairs=original_prefix_proofs,
        suffix_entry_id=suffix_entries[0]["id"], suffix_source=suffix_source, branch=branch, leased=leased)


def has_actual_pipeline_fence(env):
    data = env.runtime.store.read()
    return any(branch.get("task_id") == env.task_id and branch.get("conversation_revision") == 0
               and branch.get("new_conversation_revision") == 1
               and env.suffix_source["message_id"] in branch.get("retired_message_ids", [])
               for branch in data["memory"]["source_fences"].get("retired_branches", []))


def assert_branch_boundary(env):
    data = env.runtime.store.read()
    entries = data["memory"]["entries"]
    active_raw = {entry_id: entry for entry_id, entry in entries.items() if entry.get("status") == "active"}
    visible = memory.list_memories_snapshot(data)["entries"]
    visible_ids = {entry["id"] for entry in visible}
    prefix_after = [entry for entry_id, entry in active_raw.items() if entry_id in env.prefix_ids]
    prefix_proofs = source_pairs(prefix_after)
    retired_valid, retired_reason = memory.verify_source_ref_against_state(
        data, env.suffix_source, for_active_entry=True)
    late = env.runtime.store.transaction(lambda live: memory.commit_extraction_job(live,
        job_id=env.leased["id"], worker_id="offline-branch-worker", lease_token=env.leased["lease_token"],
        proposals=[{"category": "equipment", "fact_key": "personal.gear.lens", "text": "镜头是 XF16-55mm F2.8",
                    "source_message_ids": [env.suffix_source["message_id"]]}]))
    after_late = env.runtime.store.read()
    pending = [candidate for candidate in after_late["memory"]["candidates"].values()
               if candidate.get("status") == "pending" and "XF16-55" in candidate.get("text", "")]
    violations = []
    if not env.prefix_ids <= set(active_raw):
        violations.append("Untouched original prefix entries were physically invalidated")
    if not env.prefix_ids <= visible_ids:
        violations.append("Untouched prefix facts disappeared from active owner read")
    if prefix_proofs != env.prefix_source_pairs:
        violations.append("Retained prefix source IDs/hash/convrev0 were changed or reauthored")
    if env.suffix_entry_id in active_raw:
        violations.append("A fact whose only real source is the discarded suffix remains active")
    if retired_valid:
        violations.append("Discarded suffix source is still valid for canonical active memory")
    if late.get("state") != "stale" or late.get("committed_candidates") or pending:
        violations.append("Old real leased job recovered a discarded-source fact")
    evidence = {"violations": violations, "retained_prefix_ids": sorted(env.prefix_ids),
        "raw_active_ids": sorted(active_raw), "visible_ids": sorted(visible_ids),
        "original_prefix_sources": sorted(env.prefix_source_pairs), "current_prefix_sources": sorted(prefix_proofs),
        "retired_suffix_id": env.suffix_entry_id, "retired_source_check": [retired_valid, retired_reason],
        "late_job_state": late.get("state"), "late_job_candidate_count": len(late.get("committed_candidates", [])),
        "pending_discarded_fact_count": len(pending), "model_kind": "RecordingFakeModel; zero real model requests"}
    assert not violations, json.dumps(evidence, ensure_ascii=False, indent=2)


def test_real_store_branch_data_fence_keeps_untouched_prefix_and_retires_suffix(edited_branch, record_property):
    env = edited_branch
    integrated = has_actual_pipeline_fence(env)
    record_property("validation_layer", "public Runtime pipeline" if integrated else
                    "data fence explicitly called after genuine public edit; caller integration still pending")
    if not integrated:
        # Explicit DATA-LAYER boundary only: consume actual Runtime's recorded
        # discarded suffix. No fabricated owner/source or pretend integration.
        env.runtime.store.transaction(lambda data: memory.record_branch_rewrite_fence(data,
            task_id=env.task_id, previous_conversation_revision=0, new_conversation_revision=1,
            retired_messages=copy.deepcopy(env.branch["messages"])))
    assert_branch_boundary(env)


def test_public_runtime_edit_transaction_invokes_memory_branch_fence(edited_branch, record_property):
    env = edited_branch
    record_property("validation_layer", "public Runtime edit transaction; no direct fence helper")
    assert has_actual_pipeline_fence(env), (
        "INTEGRATION PENDING: public Runtime edit rewrote the actual task, but root data did not reach "
        "record_branch_rewrite_fence (for example settle() still called rewrite without data). "
        "The data-layer test is separate and must not be reported as this integration passing.")
    assert_branch_boundary(env)
