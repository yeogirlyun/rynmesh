"""S06-I23: retain CLI proposal siblings and pair host-owned rejections."""
import copy
import json

import pytest
from test_codex_thread_reuse import codex, conversation, propose, step, TOOLS as THREAD_TOOLS

from rynmesh.agent_runtime.tool_router import normalize_tool_calls
from rynmesh.llm_package import codex_session, codex_tools
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter

SPEC = {"type": "function", "function": {"name": "probe", "parameters": {
    "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}}}
TOOLS = {"probe": SPEC["function"]}
OVERFLOW = "ryn_proposal_overflow"
MESSAGE = "Too many tool proposals in one reply; this one was not executed. Send it again in the next step."


def decode(proposals, *, required=False, restore_omitted=False):
    return codex_tools._validated({"text": json.dumps({"content": "Proposed batch", "tool_calls": proposals})},
                                  TOOLS, required, restore_omitted=restore_omitted)


def paired(message, normalized):
    replies = [{"role": "tool", "tool_call_id": call["call_id"],
                "content": json.dumps(call["error_result"] or {"ok": True})} for call in normalized]
    validate_chat({"messages": [message, *replies]})
    assert [reply["tool_call_id"] for reply in replies] == [call["id"] for call in message["tool_calls"]]


def test_T1_twenty_proposals_keep_first_sixteen_and_pair_four_overflows():
    assert codex_tools.MAX_PROPOSALS == 16
    proposals = [{"name": "probe", "arguments": ' { "value" : ' + str(i) + ' } '} for i in range(20)]
    original = copy.deepcopy(proposals)
    message = decode(proposals)
    calls = message["tool_calls"]
    assert [call["function"] for call in calls[:16]] == proposals[:16]
    assert [call["function"] for call in calls[16:]] == [{"name": OVERFLOW, "arguments": "{}"}] * 4
    assert len({call["id"] for call in calls}) == 20
    looked_up = []
    routed = normalize_tool_calls(calls, [SPEC], metadata=lambda name: (looked_up.append(name), {"effect": "read"})[1])
    assert all(call["error_result"] is None for call in routed[:16])
    assert [call["error_result"]["error"] for call in routed[16:]] == ["agent_tool_proposal_overflow"] * 4
    assert looked_up == ["probe"] * 16
    paired(message, routed)
    assert proposals == original


@pytest.mark.parametrize("restore", [False, True])
def test_T2_unknown_string_name_preserves_raw_arguments_and_valid_siblings(restore):
    raw = ' { "secret-shape": null, "unparsed": '
    message = decode([{"name": "probe", "arguments": {"value": 1}},
                      {"name": "not_loaded", "arguments": raw},
                      {"name": "probe", "arguments": {"value": 2}}], restore_omitted=restore)
    assert message["tool_calls"][1]["function"] == {"name": "not_loaded", "arguments": raw}
    looked_up = []
    routed = normalize_tool_calls(message["tool_calls"], [SPEC], metadata=lambda name: (looked_up.append(name), {"effect": "read"})[1])
    assert [call["arguments"] for call in (routed[0], routed[2])] == [{"value": 1}, {"value": 2}]
    assert routed[0]["error_result"] is routed[2]["error_result"] is None
    assert routed[1]["error_result"]["error"] == "agent_unknown_tool"
    assert routed[1]["error_result"]["not_executed"] is True
    assert looked_up == ["probe", "probe"]
    paired(message, routed)


@pytest.mark.parametrize("proposals", [None, {}, "calls", 1])
def test_T3_non_array_tool_calls_remain_invalid(proposals):
    with pytest.raises(AdapterError) as error:
        decode(proposals)
    assert error.value.code == "upstream_response_invalid"
    assert error.value.proposal_feedback_code == "tool_calls"


def test_T4_overflow_has_exact_code_and_correction_without_metadata_or_execution():
    calls = [{"id": "overflow", "type": "function", "function": {"name": OVERFLOW, "arguments": "{}"}}]
    looked_up = []
    call = normalize_tool_calls(calls, [SPEC], metadata=lambda name: looked_up.append(name))[0]
    assert call["error_result"] == {"error": "agent_tool_proposal_overflow", "message": MESSAGE,
                                     "not_executed": True, "no_mutation": True}
    assert looked_up == []
    assert OVERFLOW not in json.dumps(codex_tools._response_schema(TOOLS))


@pytest.mark.parametrize("proposal", [None, {}, {"name": 1, "arguments": {}}, {"name": [], "arguments": {}},
                                     {"name": "probe", "arguments": {}, "extra": True}])
def test_bad_shape_or_non_string_name_still_invalidates_whole_response(proposal):
    with pytest.raises(AdapterError) as error:
        decode([{"name": "probe", "arguments": {"value": 1}}, proposal])
    assert error.value.code == "upstream_response_invalid"


def test_required_empty_response_stays_invalid():
    with pytest.raises(AdapterError) as error:
        decode([], required=True)
    assert error.value.proposal_feedback_code == "tool_calls"


@pytest.mark.parametrize("tail", [{"name": 1, "arguments": {}}, {"name": "probe", "arguments": {}, "extra": True}])
def test_overflow_does_not_hide_invalid_name_or_proposal_shape(tail):
    proposals = [{"name": "probe", "arguments": {"value": i}} for i in range(16)] + [tail]
    with pytest.raises(AdapterError) as error:
        decode(proposals)
    assert error.value.code == "upstream_response_invalid"


def test_real_cli_chat_calls_native_once_and_retains_unknown_and_overflow(monkeypatch):
    native_calls = []
    proposals = [{"name": "not_loaded" if i == 1 else "probe", "arguments": {"value": i}} for i in range(20)]

    def native(_adapter, body, **_kwargs):
        native_calls.append(body)
        return {"text": json.dumps({"content": "Batch", "tool_calls": proposals}), "input_tokens": 100, "output_tokens": 200}

    monkeypatch.setattr(codex_session, "_native_chat", native)
    result = CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "Propose tools"}],
                                              "tools": [SPEC], "tool_choice": "required", "max_tokens": 1024},
                                             task_id="cli-proposals", timeout_s=1)
    routed = normalize_tool_calls(result["message"]["tool_calls"], [SPEC])
    assert len(native_calls) == 1
    assert len(routed) == 20
    assert routed[0]["error_result"] is None
    assert routed[1]["error_result"]["error"] == "agent_unknown_tool"
    assert all(call["error_result"]["error"] == "agent_tool_proposal_overflow" for call in routed[16:])
    paired(result["message"], routed)


def test_explicit_named_choice_cannot_turn_another_offered_tool_into_an_executable_call(monkeypatch):
    other = copy.deepcopy(SPEC)
    other["function"]["name"] = "other"
    monkeypatch.setattr(codex_session, "_native_chat", lambda *_args, **_kwargs: {
        "text": json.dumps({"content": "", "tool_calls": [{"name": "other", "arguments": {"value": 1}}]})})
    with pytest.raises(AdapterError) as error:
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "Only probe"}],
                                          "tools": [SPEC, other],
                                          "tool_choice": {"type": "function", "function": {"name": "probe"}}, "max_tokens": 1024},
                                         task_id="named-proposal", timeout_s=1)
    assert error.value.code == "upstream_response_invalid"


def test_unknown_proposal_error_returns_to_same_cli_thread_without_retry(codex):
    codex.replies.extend([propose(path="a.txt"), propose("exec_command", path="x"), propose(path="b.txt")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    second = adapter.chat({"messages": copy.deepcopy(messages), "tools": THREAD_TOOLS, "max_tokens": 256},
                          task_id="s2", timeout_s=5)
    calls = normalize_tool_calls(second["message"]["tool_calls"], THREAD_TOOLS)
    assert calls[0]["error_result"]["error"] == "agent_unknown_tool"
    messages.append(copy.deepcopy(second["message"]))
    messages.append({"role": "tool", "tool_call_id": calls[0]["call_id"], "content": json.dumps(calls[0]["error_result"])})
    step(adapter, messages, "s3")
    assert len(codex.instances) == 1 and not codex.instances[0].closed
    turns = codex.turns(codex.instances[0])
    assert len(turns) == 3
    assert "agent_unknown_tool" in json.dumps(codex.payload(turns[-1]))


def test_named_choice_still_pairs_a_truly_unknown_string_with_its_valid_sibling(monkeypatch):
    monkeypatch.setattr(codex_session, "_native_chat", lambda *_args, **_kwargs: {
        "text": json.dumps({"content": "", "tool_calls": [{"name": "probe", "arguments": {"value": 1}},
                                                          {"name": "not_loaded", "arguments": "{}"}]})})
    result = CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "Probe"}], "tools": [SPEC],
        "tool_choice": {"type": "function", "function": {"name": "probe"}}, "max_tokens": 1024}, task_id="named-unknown", timeout_s=1)
    calls = normalize_tool_calls(result["message"]["tool_calls"], [SPEC])
    assert calls[0]["error_result"] is None
    assert calls[1]["name"] == "not_loaded" and calls[1]["error_result"]["error"] == "agent_unknown_tool"
    paired(result["message"], calls)


def test_named_choice_overflow_tail_is_replaced_before_unselected_tool_rejection(monkeypatch):
    other = copy.deepcopy(SPEC)
    other["function"]["name"] = "other"
    proposals = [{"name": "probe" if i < 16 else "other", "arguments": {"value": i}} for i in range(20)]
    monkeypatch.setattr(codex_session, "_native_chat", lambda *_args, **_kwargs: {
        "text": json.dumps({"content": "", "tool_calls": proposals})})
    result = CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "Probe"}], "tools": [SPEC, other],
        "tool_choice": {"type": "function", "function": {"name": "probe"}}, "max_tokens": 1024}, task_id="named-overflow", timeout_s=1)
    calls = normalize_tool_calls(result["message"]["tool_calls"], [SPEC, other])
    assert len(calls) == 20
    assert all(call["name"] == "probe" and call["error_result"] is None for call in calls[:16])
    assert all(call["name"] == OVERFLOW and call["error_result"]["error"] == "agent_tool_proposal_overflow" for call in calls[16:])
    paired(result["message"], calls)
