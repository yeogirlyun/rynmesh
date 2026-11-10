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


def business_history(messages):
    calls = {call["id"]: call["function"]["name"] for message in messages for call in message.get("tool_calls", [])}
    return [message for message in messages if message["role"] != "tool"
            or calls.get(message.get("tool_call_id")) != "ryn_discover_tools"]
