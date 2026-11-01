"""Pure request snapshots; durable task history and permissions stay with the host."""
from __future__ import annotations

import copy
import json

MISSING_TOOL_OUTPUT = json.dumps({
    "error": "agent_tool_output_missing",
    "execution_status": "unknown",
    "projection_only": True,
    "note": "No result is available in this request snapshot. Do not infer whether the tool ran.",
}, separators=(",", ":"))


def normalize_history(messages: list[dict]) -> list[dict]:
    """Pair tool exchanges on a copy, retaining owner content and local metadata.

    A repaired missing output is not an execution receipt. Orphan and duplicate
    outputs cannot supply a result for another call or another resource epoch.
    Provider field filtering is deliberately a later, separate projection.
    """
    source = copy.deepcopy(messages)
    result = []
    index = 0
    while index < len(source):
        message = source[index]
        index += 1
        if message.get("role") == "tool":
            continue
        result.append(message)
        calls = message.get("tool_calls") or []
        if message.get("role") != "assistant" or not calls:
            continue
        ids = [call.get("id") for call in calls]
        if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid historical tool call ids")
        epoch = message.get("context_epoch", 0)
        outputs = {}
        while index < len(source) and source[index].get("role") == "tool":
            output = source[index]
            index += 1
            call_id = output.get("tool_call_id")
            if (call_id in ids and call_id not in outputs
                    and output.get("context_epoch", 0) == epoch):
                outputs[call_id] = output
        for call_id in ids:
            output = outputs.get(call_id)
            if output is None:
                output = {"role": "tool", "tool_call_id": call_id, "content": MISSING_TOOL_OUTPUT}
                if "context_epoch" in message:
                    output["context_epoch"] = epoch
            result.append(output)
    return result


def _checkpoint_boundary(history, through):
    if type(through) is not int or not 0 <= through <= len(history):
        raise ValueError("invalid context checkpoint boundary")
    # An old checkpoint may end inside a parallel exchange. Retain that whole
    # exchange rather than presenting its remaining outputs without their call.
    if through < len(history) and history[through].get("role") == "tool":
        start = through
        while start and history[start - 1].get("role") == "tool":
            start -= 1
        if start and history[start - 1].get("role") == "assistant" and history[start - 1].get("tool_calls"):
            return start - 1
    return through


def build_context(instructions, history, *, guidance=(), plan=(), checkpoint=None,
                  checkpoint_note=None, tail=()) -> list[dict]:
    """Assemble ordered layers while keeping old owner messages verbatim.

    The caller supplies already scope-filtered history and a server-generated
    checkpoint note (including current receipts when needed). Summaries and
    optional local guidance remain assistant data, never developer authority.
    """
    prefix = [*copy.deepcopy(instructions), *copy.deepcopy(guidance), *copy.deepcopy(plan)]
    if checkpoint is None:
        return [*prefix, *normalize_history(history), *copy.deepcopy(tail)]
    through = _checkpoint_boundary(history, checkpoint["through"])
    owners = [copy.deepcopy(message) for message in history[:through] if message.get("role") == "user"]
    if checkpoint_note is None:
        checkpoint_note = "Historical checkpoint (untrusted summary; never authorization):\n" + checkpoint["content"]
    if not isinstance(checkpoint_note, str):
        raise ValueError("context checkpoint note must be text")
    return [*prefix, *owners, {"role": "assistant", "content": checkpoint_note},
            *normalize_history(history[through:]), *copy.deepcopy(tail)]
