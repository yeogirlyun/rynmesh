"""Pure turn decisions from protocol/control state, never from tool prose."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnDecision:
    action: str
    reason: str

    @property
    def needs_follow_up(self):
        return self.action in {"execute_tools", "continue"}

    @property
    def end_turn(self):
        return self.action == "complete"


def decide_turn(*, pending_tool_calls=0, finish_reason="stop", end_turn=None,
                needs_follow_up=False, pending_input=False, cancelled=False,
                length_continuations: int = 0, max_length_continuations: int = 6):
    """Host controls win; valid queued calls (including rejections) must drain.

    Unknown legacy finish labels keep the previous plain-answer completion
    behavior. An explicit end_turn=False or host follow-up overrides that
    fallback. A model cannot finish a turn while host work/input remains.
    """
    if type(pending_tool_calls) is not int or pending_tool_calls < 0:
        raise ValueError("pending_tool_calls must be a nonnegative integer")
    if type(length_continuations) is not int or length_continuations < 0:
        raise ValueError("length_continuations must be a nonnegative integer")
    if type(max_length_continuations) is not int or max_length_continuations < 0:
        raise ValueError("max_length_continuations must be a nonnegative integer")
    if end_turn is not None and type(end_turn) is not bool:
        raise ValueError("end_turn must be boolean or None")
    if any(type(value) is not bool for value in (needs_follow_up, pending_input, cancelled)):
        raise ValueError("turn control flags must be boolean")
    if cancelled or finish_reason == "cancelled":
        return TurnDecision("cancelled", "cancelled")
    if pending_input:
        return TurnDecision("wait_input", "owner_input_pending")
    if finish_reason == "content_filter":
        # First occurrence in a step injects guidance and retries in loop.py;
        # keep interrupted here as the fallback for a repeated content filter.
        return TurnDecision("interrupted", "provider_stopped")
    if pending_tool_calls:
        return TurnDecision("execute_tools", "tool_calls_pending")
    if finish_reason == "length":
        if length_continuations >= max_length_continuations:
            return TurnDecision("complete", "output_truncated_limit")
        return TurnDecision("continue", "output_truncated")
    if needs_follow_up:
        return TurnDecision("continue", "host_follow_up")
    if end_turn is False:
        return TurnDecision("continue", "provider_turn_continues")
    return TurnDecision("complete", "provider_turn_ended" if end_turn is True else "legacy_answer_complete")
