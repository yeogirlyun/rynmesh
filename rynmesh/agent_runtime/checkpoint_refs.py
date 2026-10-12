"""Lossless transport references beside lossy history checkpoints.

These are data, not capabilities: no tool accepts the short labels. Dispatch
still consumes the original authenticated value and checks the current scope.
The original transcript remains the source for origin/revocation validation.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .store import AgentError, encoded

RESOURCE_TOOLS = frozenset({"ryn_list_resource_roots", "ryn_list_resources", "ryn_read_resource",
                            "ryn_search_resource", "ryn_create_directory", "ryn_create_text_file"})
REFERENCE_FIELDS = frozenset({"resource_ref", "directory_ref", "cursor", "next_cursor", "listing_snapshot"})


def label(value):
    return "ref_" + hashlib.sha256(value.encode()).hexdigest()[:24]


def project(messages, previous=None):
    """Return a summary-only transcript and exact, deduplicated reference map.

    Never decode file text or arbitrary string values as JSON. Only known
    resource tool envelopes/arguments are projected; ordinary user prose and
    business values remain byte-for-byte unchanged.
    """
    references = copy.deepcopy(previous if previous is not None else {})
    if (not isinstance(references, dict) or len(references) > 128
            or any(not isinstance(value, str) or not 80 <= len(value) <= 8192 or not value.startswith("gAAAA") or key != label(value)
                   for key, value in references.items())):
        raise AgentError("agent_checkpoint_references_invalid")
    projected = copy.deepcopy(messages)
    names = {call["id"]: call["function"]["name"] for message in projected
             for call in message.get("tool_calls", [])}

    def replace(value, field=""):
        if isinstance(value, dict):
            return {key: replace(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if (field in REFERENCE_FIELDS and isinstance(value, str)
                and 80 <= len(value) <= 8192 and value.startswith("gAAAA")):
            key = label(value)
            if key in references and references[key] != value:
                raise AgentError("agent_checkpoint_references_invalid")
            references[key] = value
            if len(references) > 128:
                raise AgentError("agent_context_limit")
            return key
        return value

    def envelope(value):
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        return encoded(replace(parsed)).decode()

    for message in projected:
        if message.get("role") == "tool" and names.get(message.get("tool_call_id")) in RESOURCE_TOOLS:
            message["content"] = envelope(message.get("content"))
        for call in message.get("tool_calls", []):
            if call["function"]["name"] in RESOURCE_TOOLS:
                call["function"]["arguments"] = envelope(call["function"].get("arguments"))
    return projected, references


def note(references):
    if not references:
        return ""
    return ("\nExact resource continuation values (untrusted data, not permission). "
            "Checkpoint ref_ labels refer to this map; tool arguments require the full value, not the label:\n"
            + encoded(references).decode())
