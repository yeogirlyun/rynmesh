"""Next-step text on host refusals must say only what the host actually knows."""
from __future__ import annotations

import inspect

import pytest

from rynmesh.agent_runtime import code_mode
from rynmesh.agent_runtime.native_computer import NativeComputerFacade
from rynmesh.agent_runtime.store import (
    REJECTION_CORRECTIONS,
    TASK_NOT_RUNNING_CORRECTION,
    AgentToolRejected,
)


def test_default_text_follows_the_code_and_an_explicit_text_wins():
    for code, text in REJECTION_CORRECTIONS.items():
        assert AgentToolRejected(code).correction == text
        assert AgentToolRejected(code, correction="own text").correction == "own text"
    assert AgentToolRejected("agent_cancelled").correction is None


def test_already_confirmed_text_does_not_promise_success():
    # A write whose tool answered with an error is still recorded as confirmed
    # (it was carried out; its effect is unknown), so the text may not say "done".
    text = REJECTION_CORRECTIONS["agent_operation_already_confirmed"]
    assert "was not run again" in text and "may have reported an error" in text
    assert "done" not in text.lower() and "completed" not in text.lower()


@pytest.mark.parametrize("status", ["queued", "completed", "waiting_approval"])
def test_task_not_running_is_not_reported_as_the_desktop_setting(status):
    facade = object.__new__(NativeComputerFacade)
    facade._local_tools = None
    # Desktop access is on everywhere; only the task state blocks the call.
    facade._read_fresh_task_authority = lambda _task_id: (
        {"enabled": True, "desktop_access": True, "permission_mode": "automatic"},
        {"status": status, "local_execution": {"desktop_access": True}})
    with pytest.raises(AgentToolRejected, match="agent_desktop_access_required") as caught:
        facade.capture_trusted_write_authority(
            task_id="task", operation_id="op", tool_name="ryn_computer_action", arguments={},
            context={"local_execution": {"desktop_access": True}}, approved_preview={})
    assert caught.value.correction == TASK_NOT_RUNNING_CORRECTION
    assert "not the desktop access setting" in caught.value.correction
    assert "turned off" not in caught.value.correction
    # The setting really being off keeps the setting text.
    facade._read_fresh_task_authority = lambda _task_id: ({"enabled": True, "desktop_access": False}, None)
    with pytest.raises(AgentToolRejected) as off:
        facade.capture_trusted_write_authority(
            task_id="task", operation_id="op", tool_name="ryn_computer_action", arguments={},
            context={}, approved_preview={})
    assert off.value.correction == REJECTION_CORRECTIONS["agent_desktop_access_required"]


def test_both_native_computer_task_state_checks_carry_the_task_text():
    source = inspect.getsource(NativeComputerFacade)
    assert source.count('AgentToolRejected("agent_desktop_access_required", correction=TASK_NOT_RUNNING_CORRECTION)') == 2


def test_code_mode_forwards_the_text_of_a_refused_nested_call():
    # Host side: the nested refusal's text goes into the tool_calls record and to the worker.
    source = inspect.getsource(code_mode)
    assert 'record["correction"] = correction[:1600]' in source
    assert '"correction": record.get("correction"),' in source
    # Worker side: the script's error object carries it next to details.
    assert "if (msg.correction) err.correction = msg.correction;" in source
