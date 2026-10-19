"""Protocol turn decisions do not guess completion from natural language."""
import pytest

from rynmesh.agent_runtime.turn_state import decide_turn


def test_host_input_and_cancellation_win_over_model_end_and_queued_tools():
    waiting = decide_turn(pending_input=True, pending_tool_calls=2, end_turn=True)
    assert waiting.action == "wait_input" and not waiting.end_turn
    assert decide_turn(pending_input=True, pending_tool_calls=2, end_turn=True, cancelled=True).action == "cancelled"


def test_queued_call_including_paired_error_must_be_drained_before_completion():
    decision = decide_turn(pending_tool_calls=1, end_turn=True, finish_reason="stop")
    assert decision.action == "execute_tools" and decision.needs_follow_up and not decision.end_turn


@pytest.mark.parametrize("controls,reason", [
    ({"end_turn": False}, "provider_turn_continues"),
    ({"needs_follow_up": True, "end_turn": True}, "host_follow_up"),
    ({"finish_reason": "length", "end_turn": True}, "output_truncated"),
])
def test_protocol_follow_up_is_independent_of_tool_or_answer_text(controls, reason):
    decision = decide_turn(**controls)
    assert decision.action == "continue" and decision.reason == reason
    assert decision.needs_follow_up and not decision.end_turn


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls", "unknown", "legacy-provider-label", None])
def test_legacy_no_work_answer_can_complete_without_new_provider_features(finish_reason):
    assert decide_turn(finish_reason=finish_reason).end_turn


def test_provider_filter_is_interruption_not_successful_completion():
    decision = decide_turn(finish_reason="content_filter")
    assert decision.action == "interrupted" and not decision.end_turn


@pytest.mark.parametrize("controls", [{"pending_tool_calls": True}, {"pending_tool_calls": -1},
                                       {"needs_follow_up": "false"}, {"end_turn": 0}])
def test_malformed_control_flags_cannot_change_decisions_by_python_truthiness(controls):
    with pytest.raises(ValueError):
        decide_turn(**controls)
