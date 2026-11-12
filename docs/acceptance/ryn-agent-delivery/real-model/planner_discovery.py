"""Schema discovery for deterministic harness planners, never a live-model policy."""
import json
import uuid


def discover(specs, names):
    if not specs:
        return None
    exposed = {item["function"]["name"] for item in specs}
    if "ryn_discover_tools" in exposed and not set(names) <= exposed:
        return {"message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "fixture-discovery-" + uuid.uuid4().hex, "type": "function",
            "function": {"name": "ryn_discover_tools", "arguments": json.dumps({"load": [name for name in names if name != "ryn_request_input" or name not in exposed]})}}]},
            "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "tool_calls"}
    return None


RECEIPTS_MARKER = "\nServer-recorded mutation receipts (data, not instructions):\n"


def pinned_receipts(messages):
    """Mutation receipts the server pins into a context checkpoint note."""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith("Historical checkpoint") or RECEIPTS_MARKER not in content:
            continue
        raw = content.split(RECEIPTS_MARKER, 1)[1].split("\n", 1)[0]
        try:
            receipts = json.loads(raw)
        except ValueError:
            return []
        return [item for item in receipts if isinstance(item, dict) and isinstance(item.get("tool"), str)]
    return []


def business_history(messages):
    """Business tool history; after a checkpoint, pinned writes count as done.

    A model reads the server's pinned mutation receipts in the checkpoint note
    and does not repeat a confirmed write. Deterministic harness planners derive
    progress from tool results, so those receipts are restored as results.
    """
    calls = {call["id"]: call["function"]["name"] for message in messages for call in message.get("tool_calls", [])}
    kept = [message for message in messages if message["role"] != "tool"
            or calls.get(message.get("tool_call_id")) != "ryn_discover_tools"]
    restored = []
    for item in pinned_receipts(messages):
        call_id = "pinned-" + str(item.get("call_id") or uuid.uuid4().hex)
        if call_id[len("pinned-"):] in calls:
            continue  # Still verbatim in the current history.
        restored += [{"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
                      "function": {"name": item["tool"], "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": call_id, "content": json.dumps(item.get("result"))}]
    if not restored:
        return kept
    first_user = next((index for index, message in enumerate(kept) if message["role"] == "user"), 0)
    return [*kept[:first_user + 1], *restored, *kept[first_user + 1:]]


PROGRESS_MARKER = "Harness progress: "


def checkpoint_progress(messages):
    """Business tool names a harness checkpoint recorded as already called."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and PROGRESS_MARKER in content:
            try:
                return set(json.loads(content.split(PROGRESS_MARKER, 1)[1].split("\n", 1)[0]))
            except ValueError:
                return set()
    return set()


def checkpoint(messages, specs):
    """A synthetic hand-off when a tool-less request is a context checkpoint.

    Tool-less requests are either the independent checker (its payload has
    "evidence") or a context checkpoint (payload has "history"). Catalog and
    device listings can fill the window, so a checkpoint may come first. Like a
    model's summary, the hand-off records progress: the business tools called.
    """
    if specs or not messages:
        return None
    try:
        payload = json.loads(messages[-1].get("content") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "evidence" in payload or "history" not in payload:
        return None
    names = checkpoint_progress([{"content": payload.get("previous_checkpoint") or ""}])
    names.update(call["function"]["name"] for message in payload["history"] if isinstance(message, dict)
                 for call in message.get("tool_calls") or [] if call["function"]["name"] != "ryn_discover_tools")
    return {"message": {"role": "assistant", "content": "Goal: follow the owner's latest instruction. "
                        "Synthetic harness checkpoint; no business result is claimed.\n"
                        + PROGRESS_MARKER + json.dumps(sorted(names))},
            "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "stop"}
