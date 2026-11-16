"""Tests for S13-I36: ryn_share_model_with_friend keeps other friends by default unless revoke_other_friends=true."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.sharing import NAME, VERIFIER, FriendSharingTool, digest
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.llm_package.sources import SourceStore

node = integration_node

CONTEXT = {
    "task_id": "task-s13-i36",
    "owner_id": "owner-local",
    "consumer_node_id": "consumer-local",
    "goal_revision": 1,
    "scope_revision": 1,
}

RELATIONS = {
    ch * 32: {
        "relationship_id": ch * 32,
        "peer_id": f"node-{ch}",
        "node_name": f"Node {ch}",
        "status": "active",
    }
    for ch in ("a", "b", "c")
}


def _setup_sharing(tmp_path: Path):
    sources = SourceStore(tmp_path, b"synthetic-s13-i36-test-key", migrate=False)
    relations = copy.deepcopy(RELATIONS)
    access = AIAccessStore(tmp_path, relationship=relations.get, sources=sources)
    app = SimpleNamespace(state=SimpleNamespace(ai_access=SimpleNamespace(store=access, recheck=None)))
    tool = FriendSharingTool(app, {"source_store": sources})
    record = sources.prepare(
        {
            "name": "Local Provider",
            "kind": "local",
            "base_url": "http://127.0.0.1:11434/v1",
            "models": ["qwen-coder"],
            "api_key": "synthetic-key",
        }
    )
    record.update(status="ready")
    sources.save(record)
    sid = sources.model_service_id(record, "qwen-coder")
    return sources, access, tool, relations, "local/" + sid, sid


def _run_share(tool: FriendSharingTool, args: dict, op_id: str, context: dict = CONTEXT):
    prev = tool.preview(args, context)
    res = tool.execute(args, operation_id=op_id, context=context, expected_preview=prev, cancelled=lambda: False)
    rec = {
        "operation_id": op_id,
        "tool": NAME,
        "arguments": copy.deepcopy(args),
        "arguments_sha256": digest(args),
        "result": copy.deepcopy(res),
    }
    return prev, res, rec


def test_s13_i36_t1_default_share_keeps_first_friend_access_and_schema_defaults_false(tmp_path, node):
    app, store = node
    _, ryn_tools, _ = build_agent_integrations(app, store, store.home)
    share_spec = next(spec["function"] for spec in ryn_tools.specs() if spec["function"]["name"] == NAME)
    props = share_spec["parameters"]["properties"]
    assert "revoke_other_friends" in props
    assert props["revoke_other_friends"]["type"] == "boolean"
    assert props["revoke_other_friends"].get("default") is False

    _, access, tool, _, model_id, _ = _setup_sharing(tmp_path)
    args_a = {"model_id": model_id, "relationship_id": "a" * 32}
    prev_a, res_a, _ = _run_share(tool, args_a, "op-share-a")
    assert prev_a["other_friend_access"] == "keep"
    assert prev_a["will_revoke"] == []
    assert res_a["revoked_recipients"] == []

    args_b = {"model_id": model_id, "relationship_id": "b" * 32}
    prev_b, res_b, _ = _run_share(tool, args_b, "op-share-b")
    assert prev_b["other_friend_access"] == "keep"
    assert prev_b["will_revoke"] == []
    assert res_b["revoked_recipients"] == []

    allowed_peers = sorted(row["peer_id"] for row in access.list() if row["allowed"])
    assert allowed_peers == ["node-a", "node-b"]


def test_s13_i36_t2_revoke_other_friends_true_lists_will_revoke_and_revokes_others(tmp_path):
    _, access, tool, _, model_id, _ = _setup_sharing(tmp_path)
    _run_share(tool, {"model_id": model_id, "relationship_id": "a" * 32}, "op-a")
    _run_share(tool, {"model_id": model_id, "relationship_id": "c" * 32}, "op-c")
    assert sorted(row["peer_id"] for row in access.list() if row["allowed"]) == ["node-a", "node-c"]

    args_only_b = {"model_id": model_id, "relationship_id": "b" * 32, "revoke_other_friends": True}
    prev_b, res_b, _ = _run_share(tool, args_only_b, "op-only-b")
    expected_revoked = [
        {"relationship_id": "a" * 32, "name": "Node a"},
        {"relationship_id": "c" * 32, "name": "Node c"},
    ]
    assert prev_b["other_friend_access"] == "revoke"
    assert prev_b["will_revoke"] == expected_revoked
    assert res_b["revoked_recipients"] == expected_revoked
    assert [row["peer_id"] for row in access.list() if row["allowed"]] == ["node-b"]
    assert access.grant(model_id[6:], "a" * 32)["allowed"] is False
    assert access.grant(model_id[6:], "c" * 32)["allowed"] is False


def test_s13_i36_t3_verify_passes_for_both_keep_and_revoke_modes_and_detects_tamper(tmp_path, node):
    app, store = node
    relations = copy.deepcopy(RELATIONS)
    sources = app.state.llm_agent_services["source_store"]
    access = AIAccessStore(store.home, relationship=relations.get, sources=sources)
    app.state.ai_access = SimpleNamespace(store=access, recheck=None)
    record = sources.prepare(
        {
            "name": "Verify Source",
            "kind": "local",
            "base_url": "http://127.0.0.1:11434/v1",
            "models": ["verify-model"],
            "api_key": "synthetic-key",
        }
    )
    record.update(status="ready")
    sources.save(record)
    _, ryn_tools, _ = build_agent_integrations(app, store, store.home)
    sid = sources.model_service_id(record, "verify-model")
    model_id = "local/" + sid
    ctx = {
        "task_id": "task-verify",
        "owner_id": "local_owner",
        "consumer_node_id": store.peer_id,
        "goal_revision": 1,
        "scope_revision": 1,
    }
    criteria = [{"verifier": VERIFIER}]
    v_spec = next(s for s in ryn_tools.verification_specs() if s["tool_name"] == NAME)
    assert "revoke_other_friends" in v_spec["target_fields"]

    # 1) Keep mode: share with A then B; both receipts pass verification.
    _, _, rec_a = _run_share(ryn_tools.friend_sharing, {"model_id": model_id, "relationship_id": "a" * 32}, "op-va", context=ctx)
    _, _, rec_b = _run_share(ryn_tools.friend_sharing, {"model_id": model_id, "relationship_id": "b" * 32, "revoke_other_friends": False}, "op-vb", context=ctx)
    assert ryn_tools.verify(rec_a, criteria, context=ctx, cancelled=lambda: False)["status"] == "passed"
    assert ryn_tools.verify(rec_b, criteria, context=ctx, cancelled=lambda: False)["status"] == "passed"

    # 2) Revoke mode: share exclusively with C; C's receipt passes, and granting A afterward fails C's revoke check.
    _, _, rec_c = _run_share(ryn_tools.friend_sharing, {"model_id": model_id, "relationship_id": "c" * 32, "revoke_other_friends": True}, "op-vc", context=ctx)
    assert ryn_tools.verify(rec_c, criteria, context=ctx, cancelled=lambda: False)["status"] == "passed"

    access.set(sid, "a" * 32, allowed=True, expected_revision=access.grant(sid, "a" * 32)["revision"])
    assert ryn_tools.verify(rec_c, criteria, context=ctx, cancelled=lambda: False)["status"] == "failed"
