"""Fork reference regressions through installed HTTP routes and real encrypted files."""
import copy
import json

import pytest
from test_agent_resources import file_ref
from test_agent_resources import resources as resources
from test_agent_task_forks import append, assistant, fork, parent, request, stored
from test_agent_task_forks import env as env

from rynmesh.agent_runtime import results

EXAMPLE = "result_" + "a" * 32


@pytest.mark.parametrize("slot", ["user", "assistant", "tool_text", "tool_json_text", "arguments_text", "checkpoint_text"])
def test_example_identifiers_in_text_do_not_read_storage(env, slot):
    tid = parent(env)
    # Even a JSON example in ordinary conversation is still prose.
    text = 'Parser example: ' + json.dumps({"result_ref": EXAMPLE})
    if slot in {"user", "assistant"}:
        append(env, tid, {"role": slot, "content": text})
    elif slot == "checkpoint_text":
        append(env, tid, context_checkpoint={"through": 1, "content": text, "references": {}})
    else:
        call = assistant(name="inspect_service", args={"text": text}, call_id="example")["message"]
        content = json.dumps({"text": text}) if slot == "tool_json_text" else text
        if slot == "arguments_text":
            content = '{"ok":true}'
        append(env, tid, call, {"role": "tool", "tool_call_id": "example", "content": content})
    before = stored(env, tid)
    child = fork(env, tid)
    assert not child["result_refs"]
    assert stored(env, tid) == before
    assert not env.model.calls and not env.runtime.tools.calls


@pytest.mark.parametrize("key", ["resource_ref", "directory_ref", "listing_snapshot", "cursor", "next_cursor"])
def test_parent_capability_in_nested_tool_arguments_rejects_without_child(env, resources, key):
    resource_tools, _, context, state = resources
    env.runtime.tools.resources = resource_tools
    tid = parent(env)
    ref = file_ref(resource_tools, {**context, "task_id": tid})
    assert resource_tools._open(ref)["task_id"] == tid
    append(env, tid,
           assistant(name="ryn_read_resource", args={"nested": [{key: ref}]}, call_id="bound-ref")["message"],
           {"role": "tool", "tool_call_id": "bound-ref", "content": '{"error":"read_failed"}'})
    before = copy.deepcopy(env.runtime.store.read())
    reads = list(state["reads"])
    body = request(env, tid)
    for _ in range(2):
        response = env.client.post(f"/api/local/agent/tasks/{tid}/fork", json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "agent_fork_reference_requires_source_task"
        assert env.runtime.store.read() == before
    assert state["reads"] == reads
    assert not env.model.calls and not env.runtime.tools.calls


@pytest.mark.parametrize("slot", ["tool_result", "tool_result_metadata", "arguments"])
def test_structured_result_references_copy_nested_blobs_and_remain_readable(env, slot):
    tid = parent(env)
    source = stored(env, tid)
    leaf = results.save(env.runtime.store, source, {"name": "inspect_service", "operation_id": "leaf"},
                        {"text": "Real output, with example " + EXAMPLE})
    outer = results.save(env.runtime.store, source, {"name": "inspect_service", "operation_id": "outer"},
                         {"nested": [{"result_ref": leaf["id"]}], "text": EXAMPLE})
    arguments = {"result_id": outer["id"]} if slot == "arguments" else {}
    content = {"result_ref": outer["id"]} if slot == "tool_result" else {"ok": True}
    if slot == "tool_result_metadata":
        content = {"result_ref": outer}
    append(env, tid, assistant(name="ryn_read_result", args=arguments, call_id="read")["message"],
           {"role": "tool", "tool_call_id": "read", "content": json.dumps(content)}, result_refs=[leaf, outer])
    before = stored(env, tid)
    child = fork(env, tid)
    assert {r["id"] for r in child["result_refs"]} == {leaf["id"], outer["id"]}
    assert stored(env, tid) == before
    for ref in (leaf, outer):
        results.path(env.runtime.store, tid, ref["id"]).unlink()
        response = env.client.get(f"/api/local/agent/tasks/{child['id']}/results/{ref['id']}")
        assert response.status_code == 200, response.text
        assert EXAMPLE in response.json()["content"]
    assert not env.model.calls and not env.runtime.tools.calls


@pytest.mark.parametrize("slot", ["tool_result", "arguments"])
def test_missing_explicit_result_reference_still_fails_closed(env, slot):
    tid = parent(env)
    append(env, tid,
           assistant(name="ryn_read_result", args={"result_id": EXAMPLE} if slot == "arguments" else {}, call_id="missing")["message"],
           {"role": "tool", "tool_call_id": "missing", "content": json.dumps({"result_ref": EXAMPLE} if slot == "tool_result" else {})})
    before = copy.deepcopy(env.runtime.store.read())
    response = env.client.post(f"/api/local/agent/tasks/{tid}/fork", json=request(env, tid))
    assert response.status_code == 409
    assert response.json()["detail"] == "agent_result_not_found"
    assert env.runtime.store.read() == before


def test_undecodable_tool_arguments_cannot_bypass_validation(env):
    tid = parent(env)
    call = assistant(name="ryn_read_resource", call_id="broken")["message"]
    call["tool_calls"][0]["function"]["arguments"] = '{"resource_ref":'
    append(env, tid, call, {"role": "tool", "tool_call_id": "broken", "content": '{"error":"bad_arguments"}'})
    before = copy.deepcopy(env.runtime.store.read())
    response = env.client.post(f"/api/local/agent/tasks/{tid}/fork", json=request(env, tid))
    assert response.status_code == 409
    assert response.json()["detail"] == "agent_fork_invalid_tool_arguments"
    assert env.runtime.store.read() == before
