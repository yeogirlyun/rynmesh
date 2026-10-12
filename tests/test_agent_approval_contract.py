"""Provider contract and actual permission gates for the atomic sharing tool."""
from types import SimpleNamespace

import pytest
from test_agent_atomic_sharing import seed
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime.prompt import PROMPT_VERSION
from rynmesh.agent_runtime.sharing import NAME
from rynmesh.agent_runtime.store import AgentError
from rynmesh.agent_runtime.tools import RynTools


def sharing_runtime(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path / "agent")
    sources, access, sharing, _, args = seed(tmp_path / "node")
    tools = RynTools(sharing.app, SimpleNamespace(home=tmp_path / "node"), sharing.services, runtime.catalog)
    runtime.tools = tools
    writes = []
    original = sources._write

    def observed_write(records):
        writes.append(records)
        original(records)

    monkeypatch.setattr(sources, "_write", observed_write)
    return runtime, model, sources, access, args, writes


def assert_no_completion(row):
    assert row["outcome"]["status"] == "unverified"
    assert not row.get("verification_evidence")
    assert all(criterion["status"] != "passed" for criterion in row["criteria"])


def test_write_proposal_exposes_contract_stages_exact_approval_and_cannot_run_early(tmp_path, monkeypatch):
    runtime, model, sources, access, args, writes = sharing_runtime(tmp_path, monkeypatch)
    model.responses.append(assistant(name=NAME, args=args))
    task = runtime.create({"message": "Share the selected model with the selected friend after I approve the exact change."})
    original = sources.path.read_bytes()
    advance(runtime, 2)
    waiting = runtime.get(task["id"])
    assert waiting["status"] == "waiting_approval" and waiting["pending_input"] is None
    assert waiting["pending_approval"]["tool"] == NAME
    assert waiting["pending_approval"]["arguments"] == args
    assert waiting["pending_approval"]["preview"]["after"]["recipients"][0]["peer_id"] == "node-b"
    assert sources.path.read_bytes() == original and access.list() == [] and writes == []
    assert_no_completion(waiting)
    # Assert the contract actually reaches the provider, not merely a constant
    # that could be omitted from the transmitted system/tool descriptions.
    system = model.calls[0][1][0]["content"]
    assert PROMPT_VERSION in system and "waiting_approval" in system
    assert "no write executes before owner approval" in system
    input_tool = next(spec["function"] for spec in model.calls[0][2] if spec["function"]["name"] == "ryn_request_input")
    assert "answers never authorize writes" in input_tool["description"]
    advance(runtime, 2)
    assert writes == [] and len(model.calls) == 1
    approve(runtime, task["id"])
    advance(runtime)
    assert len(writes) == 1
    assert [row["peer_id"] for row in access.list() if row["allowed"]] == ["node-b"]
    current = runtime.get(task["id"])
    assert_no_completion(current)  # A committed write still needs verification.


def test_input_answer_cannot_replace_new_tools_exact_approval_or_verify_work(tmp_path, monkeypatch):
    runtime, model, sources, access, args, writes = sharing_runtime(tmp_path, monkeypatch)
    # Even if a model ignores the clarified contract, an input answer does not
    # become a capability to dispatch this or any subsequent write.
    model.responses.extend([
        assistant(name="ryn_request_input", args={"goal_revision": 1, "kind": "choice", "prompt": "Approve this change?",
                  "fields": [{"id": "permission", "label": "Permission", "type": "choice", "required": True,
                              "options": [{"value": "approve", "label": "Approve"}, {"value": "reject", "label": "Reject"}]}]}),
        assistant(name=NAME, args=args),
    ])
    task = runtime.create({"message": "Prepare a model sharing change for my exact approval."})
    original = sources.path.read_bytes()
    advance(runtime, 2)
    waiting = runtime.get(task["id"])
    assert waiting["status"] == "waiting_input" and waiting["pending_approval"] is None
    assert_no_completion(waiting)
    with pytest.raises(AgentError, match="approval_mismatch"):
        runtime.action(task["id"], {"action": "approve", "approval_id": waiting["pending_input"]["id"],
                                  "expected_control_revision": waiting["control_revision"]})
    runtime.action(task["id"], {"action": "answer", "pending_input_id": waiting["pending_input"]["id"],
                   "expected_control_revision": waiting["control_revision"], "expected_goal_revision": 1,
                   "answers": {"permission": "approve"}})
    advance(runtime, 2)
    proposed = runtime.get(task["id"])
    assert proposed["status"] == "waiting_approval" and proposed["pending_input"] is None
    assert proposed["pending_approval"]["tool"] == NAME
    assert writes == [] and sources.path.read_bytes() == original and access.list() == []
    assert_no_completion(proposed)
    runtime.action(task["id"], {"action": "deny", "approval_id": proposed["pending_approval"]["id"],
                              "expected_control_revision": proposed["control_revision"]})
    assert sources.path.read_bytes() == original and writes == []
