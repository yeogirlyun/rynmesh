"""Single durable commit, real killed processes, exact grants and later edits."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_sharing_receiver import dispatch, sharing  # noqa: F401

from rynmesh.agent_runtime.sharing import NAME, FriendSharingTool, digest
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.ai_access.store import AIAccessError, AIAccessStore
from rynmesh.llm_package.model_settings import effective
from rynmesh.llm_package.sources import SourceStore

CONTEXT = {"task_id": "task-approved-share", "owner_id": "owner", "consumer_node_id": "consumer",
           "goal_revision": 1, "scope_revision": 1}
RELATIONS = {name * 32: {"relationship_id": name * 32, "peer_id": "node-" + name,
                       "node_name": "Node " + name, "status": "active"} for name in ("a", "b", "c")}


def open_tools(home):
    sources = SourceStore(Path(home), b"synthetic-sharing-test-key", migrate=False)
    relations = copy.deepcopy(RELATIONS)
    access = AIAccessStore(home, relationship=relations.get, sources=sources)
    app = SimpleNamespace(state=SimpleNamespace(ai_access=SimpleNamespace(store=access, recheck=None)))
    tool = FriendSharingTool(app, {"source_store": sources})
    return sources, access, tool, relations


def seed(home):
    sources, access, tool, relations = open_tools(home)
    record = sources.prepare({"name": "Synthetic", "kind": "local", "base_url": "http://127.0.0.1:9999/v1",
                              "models": ["model-a", "model-b"], "api_key": "private-test-key"})
    record.update(status="ready", shared_models=["model-a"], sharing=True)
    sources.save(record)
    sid = sources.model_service_id(record, "model-b")
    args = {"model_id": "local/" + sid, "relationship_id": "b" * 32}
    return sources, access, tool, relations, args


def execute(tool, args, *, preview=None, operation_id="operation-one"):
    return tool.execute(args, operation_id=operation_id, context=CONTEXT, cancelled=lambda: False,
                        expected_preview=preview if preview is not None else tool.preview(args))


def receipt(args, result=None, operation_id="operation-one"):
    return {"operation_id": operation_id, "tool": NAME, "arguments": args, "arguments_sha256": digest(args), "result": result}


def test_one_commit_exact_receiver_and_private_metadata_survives_owner_edit(tmp_path, monkeypatch):
    sources, access, tool, relations, args = seed(tmp_path)
    sid = args["model_id"][6:]
    # Legacy grants, including an inactive recipient, are fully previewed and
    # revoked in the same commit. They cannot reactivate upon renewed friendship.
    access.set(sid, "a" * 32, allowed=True, expected_revision=0)
    access.set(sid, "c" * 32, allowed=True, expected_revision=0)
    relations["c" * 32]["status"] = "inactive"
    original = copy.deepcopy(sources.read())
    permission_bytes = access.path.read_bytes()
    source_bytes = sources.path.read_bytes()
    preview = tool.preview(args)
    assert len(preview["before"]["friend_grants"]) == 2
    assert sources.path.read_bytes() == source_bytes and access.path.read_bytes() == permission_bytes
    writes = []
    original_write = sources._write
    monkeypatch.setattr(sources, "_write", lambda value: (writes.append(copy.deepcopy(value)), original_write(value))[1])
    result = execute(tool, args, preview=preview)
    assert len(writes) == 1 and access.path.read_bytes() == permission_bytes
    assert [row["peer_id"] for row in access.list() if row["allowed"]] == ["node-b"]
    current = next(iter(sources.read().values()))
    old = next(iter(original.values()))
    assert current["shared_models"] == ["model-a", "model-b"]
    assert current["model_settings"]["model-a"] == old["model_settings"]["model-a"]
    assert effective(current, "model-b")["share_scope"] == "friends"
    assert current["api_key"] == old["api_key"]
    assert all(not key.startswith("_") for key in sources.public(current))
    assert "task-approved-share" not in json.dumps(sources.public(current))
    # Duplicate dispatch finds attribution and does not reapply the business write.
    assert execute(tool, args, preview=preview) == result and len(writes) == 1
    prepared = sources.prepare({"name": "Owner renamed source"}, current["id"])
    sources.save(prepared)
    assert next(iter(sources.read().values()))["_sharing_operations"]["operation-one"]["result"] == result
    assert [row["peer_id"] for row in access.list() if row["allowed"]] == ["node-b"]
    grant = access.set(sid, "b" * 32, allowed=False, expected_revision=1)
    assert grant["revision"] == 2
    access.set(sid, "b" * 32, allowed=True, expected_revision=2)
    with pytest.raises(AIAccessError, match="revoked"):
        access.authorize("node-b", sid, {"relationship_id": "b" * 32, "revision": 1})
    assert tool.verify(receipt(args, result), context=CONTEXT, cancelled=lambda: False)["status"] == "failed"


@pytest.mark.parametrize("edit", ["source", "grant", "relationship"])
def test_pending_approval_detects_concurrent_owner_edits(tmp_path, edit):
    sources, access, tool, relations, args = seed(tmp_path)
    preview = tool.preview(args)
    if edit == "source":
        other_sources, _, _, _ = open_tools(tmp_path)
        row = next(iter(other_sources.read().values()))
        row["name"] = "Changed while waiting"
        other_sources.save(row)
    elif edit == "grant":
        _, other_access, _, _ = open_tools(tmp_path)
        other_access.set(args["model_id"][6:], "a" * 32, allowed=True, expected_revision=0)
    else:
        relations["b" * 32]["peer_id"] = "different-node"
    before = sources.path.read_bytes()
    with pytest.raises(AgentToolRejected, match="approval_preview_changed"):
        execute(tool, args, preview=preview)
    assert before == sources.path.read_bytes()


@pytest.mark.parametrize("stage", ["before", "after"])
def test_actual_process_death_reconciles_only_atomic_receipt_without_replay(tmp_path, stage):
    sources, access, tool, _, args = seed(tmp_path)
    approved = tool.preview(args)
    request = tmp_path / "approved.json"
    request.write_text(json.dumps({"args": args, "preview": approved}), encoding="utf-8")
    before = sources.path.read_bytes()
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path), stage], timeout=30,
                           capture_output=True, text=True)
    assert child.returncode == (90 if stage == "before" else 91), child.stderr
    # Reopen every store in this independent process; no in-memory operation
    # cache or current-state equality is accepted as proof of attribution.
    reopened, reopened_access, recovered, _ = open_tools(tmp_path)
    after = reopened.path.read_bytes()
    outcome = recovered.reconcile("operation-one", context={**CONTEXT, "operation": receipt(args)}, cancelled=lambda: False)
    if stage == "before":
        assert after == before and outcome["outcome"] == "unknown"
        assert reopened_access.list() == []
    else:
        assert after != before and outcome["outcome"] == "executed"
        result = outcome["receipt"]["result"]
        verified = recovered.verify(receipt(args, result), context=CONTEXT, cancelled=lambda: False)
        assert verified["status"] == "passed" and verified["evidence"]["operation_attribution"] == "atomic_sharing_receipt"
        assert execute(recovered, args, preview=approved) == result
        forged = {**receipt(args), "arguments": {**args, "relationship_id": "a" * 32}}
        assert recovered.reconcile("operation-one", context={**CONTEXT, "operation": forged}, cancelled=lambda: False)["outcome"] == "unknown"
        assert recovered.reconcile("operation-one", context={**CONTEXT, "owner_id": "other", "operation": receipt(args)}, cancelled=lambda: False)["outcome"] == "unknown"
    assert reopened.path.read_bytes() == after  # Reconcile did not write anything.


def test_matching_state_from_other_operation_does_not_prove_receipt(tmp_path):
    _, _, tool, _, args = seed(tmp_path)
    execute(tool, args, operation_id="unrelated-owner-operation")
    result = tool.reconcile("operation-one", context={**CONTEXT, "operation": receipt(args)}, cancelled=lambda: False)
    assert result["outcome"] == "unknown"


def test_other_process_owner_edit_serializes_after_atomic_commit(tmp_path):
    sources, _, tool, _, args = seed(tmp_path)
    with sources.lock:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(tmp_path), "owner-edit"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "owner-started"
            result = execute(tool, args)
        except BaseException:
            child.kill()
            child.wait(timeout=10)
            raise
    _, error = child.communicate(timeout=30)
    assert child.returncode == 0, error
    source = next(iter(sources.read().values()))
    assert source["name"] == "Concurrent owner label"
    assert source["_sharing_operations"]["operation-one"]["result"] == result
    assert source["shared_models"] == ["model-a", "model-b"]


def test_source_remove_readd_keeps_denials_and_new_source_never_inherits_grants(tmp_path):
    sources, access, tool, _, args = seed(tmp_path)
    sid = args["model_id"][6:]
    access.set(sid, "a" * 32, allowed=True, expected_revision=0)
    execute(tool, args)
    source = next(iter(sources.read().values()))
    sources.save(sources.prepare({"models": ["model-a"]}, source["id"]))
    sources.save(sources.prepare({"models": ["model-a", "model-b"]}, source["id"]))
    assert access.grant(sid, "a" * 32)["allowed"] is False
    assert access.grant(sid, "a" * 32)["revision"] == 2
    assert sources.read()[source["id"]]["shared_models"] == ["model-a"]
    sources.delete(source["id"])
    fresh = sources.prepare({"name": "Replacement", "kind": "local", "base_url": source["base_url"], "models": ["model-b"]})
    sources.save(fresh)
    assert fresh["id"] != source["id"] and access.grant(sources.model_service_id(fresh, "model-b"), "a" * 32) is None
    # Source deletion also removes attribution. Even a matching new model is
    # never substituted for that lost receipt and no old operation is replayed.
    assert tool.reconcile("operation-one", context={**CONTEXT, "operation": receipt(args)}, cancelled=lambda: False)["outcome"] == "unknown"


def test_atomic_tool_signed_receiver_and_catalog_use_authoritative_grants(sharing):  # noqa: F811
    p = sharing
    args = {"model_id": p.model_id, "relationship_id": p.rid}
    preview = p.tools.preview(NAME, args, context=CONTEXT)
    before = p.sources.path.read_bytes()
    assert not p.consumer.refresh(p.owner.peer_id)["services"] and p.upstream.audit == []
    assert p.sources.path.read_bytes() == before
    result = p.tools.execute(NAME, args, operation_id="atomic-signed-receiver", context=CONTEXT,
                             expected_preview=preview, cancelled=lambda: False)
    rows = p.consumer.refresh(p.owner.peer_id)["services"]
    assert len(rows) == 1 and rows[0]["service"]["package_id"] == p.sid
    assert not p.other_catalog.refresh(p.owner.peer_id)["services"]
    permission = {"relationship_id": p.rid, "revision": result["grant_revisions"][p.rid]}
    assert dispatch(p, p.recipient, permission)["state"] == "succeeded"
    assert dispatch(p, p.other, permission)["state"] == "rejected"
    assert len(p.upstream.audit) == 1
    p.grants.set(p.sid, p.rid, allowed=False, expected_revision=permission["revision"])
    assert dispatch(p, p.recipient, permission)["state"] == "rejected"
    assert not p.consumer.refresh(p.owner.peer_id)["services"] and len(p.upstream.audit) == 1


if __name__ == "__main__":
    home, stage = Path(sys.argv[1]), sys.argv[2]
    if stage == "owner-edit":
        print("owner-started", flush=True)
    sources, _, tool, _ = open_tools(home)
    if stage == "owner-edit":
        with sources.lock:
            record = next(iter(sources.read().values()))
            record["name"] = "Concurrent owner label"
            sources.save(record)
        sys.exit(0)
    approved = json.loads((home / "approved.json").read_text(encoding="utf-8"))
    write = sources._write

    def killed_write(records):
        if stage == "before":
            os._exit(90)
        write(records)
        os._exit(91)

    sources._write = killed_write
    execute(tool, approved["args"], preview=approved["preview"])
