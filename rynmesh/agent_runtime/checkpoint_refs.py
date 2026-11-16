"""Lossless transport references beside lossy history checkpoints.

These are data, not capabilities: no tool accepts the short labels. Dispatch
still consumes the original authenticated value and checks the current scope.
The original transcript remains the source for origin/revocation validation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .store import AgentError, encoded

RESOURCE_TOOLS = frozenset({"ryn_list_resource_roots", "ryn_list_resources", "ryn_read_resource",
                            "ryn_search_resource", "ryn_create_directory", "ryn_create_text_file",
                            "ryn_upload_resource_file"})
REFERENCE_FIELDS = frozenset({"resource_ref", "directory_ref", "cursor", "next_cursor", "listing_snapshot"})
MAX_REFERENCES = 128
REF_NOTE_MAX_BYTES = 32768
_FERNET_TOKEN_RE = re.compile(r"gAAAA[A-Za-z0-9_-]{75,}={0,2}")


class ReferenceMap(dict):
    """Insertion-ordered reference map carrying cumulative FIFO eviction count."""

    def __init__(self, *args, dropped: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.dropped = int(dropped)

    def __deepcopy__(self, memo):
        copied = ReferenceMap(
            ((copy.deepcopy(k, memo), copy.deepcopy(v, memo)) for k, v in self.items()),
            dropped=self.dropped,
        )
        memo[id(self)] = copied
        return copied


def label(value):
    return "ref_" + hashlib.sha256(value.encode()).hexdigest()[:24]


def _validate_previous(previous, *, previous_dropped: int = 0) -> ReferenceMap:
    raw = previous if previous is not None else {}
    if (not isinstance(raw, dict) or len(raw) > MAX_REFERENCES
            or any(not isinstance(value, str) or not 80 <= len(value) <= 8192 or not value.startswith("gAAAA") or key != label(value)
                   for key, value in raw.items())):
        raise AgentError("agent_checkpoint_references_invalid")
    inherited_dropped = max(int(previous_dropped or 0), int(getattr(raw, "dropped", 0) or 0))
    return ReferenceMap(((k, v) for k, v in raw.items()), dropped=inherited_dropped)


def _record_reference(references: ReferenceMap, value: str) -> str:
    if not isinstance(value, str) or not 80 <= len(value) <= 8192 or not value.startswith("gAAAA"):
        raise AgentError("agent_checkpoint_references_invalid")
    key = label(value)
    if key in references:
        if references[key] != value:
            raise AgentError("agent_checkpoint_references_invalid")
        del references[key]
        references[key] = value
        return key
    references[key] = value
    while len(references) > MAX_REFERENCES:
        oldest_key = next(iter(references))
        del references[oldest_key]
        references.dropped += 1
    return key


def project(messages, previous=None, *, previous_dropped: int = 0):
    """Return a summary-only transcript and exact, deduplicated reference map.

    Never decode file text or arbitrary string values as JSON. Only known
    resource tool envelopes/arguments are projected; ordinary user prose and
    business values remain byte-for-byte unchanged.
    """
    references = _validate_previous(previous, previous_dropped=previous_dropped)
    projected = copy.deepcopy(messages)
    for message in projected:
        message.pop("provider_state", None)
    names = {call["id"]: call["function"]["name"] for message in projected
             for call in message.get("tool_calls", [])}

    def replace(value, field=""):
        if isinstance(value, dict):
            return {key: replace(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if (field in REFERENCE_FIELDS and isinstance(value, str)
                and 80 <= len(value) <= 8192 and value.startswith("gAAAA")):
            return _record_reference(references, value)
        return value

    def envelope(value):
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
            return encoded(replace(parsed)).decode()
        except AgentError:
            raise  # Malformed reference state remains a hard admission failure.
        except (ValueError, TypeError, RecursionError):
            return value

    for message in projected:
        if message.get("role") == "tool" and names.get(message.get("tool_call_id")) in RESOURCE_TOOLS:
            message["content"] = envelope(message.get("content"))
        for call in message.get("tool_calls", []):
            if call["function"]["name"] in RESOURCE_TOOLS:
                call["function"]["arguments"] = envelope(call["function"].get("arguments"))
    return projected, references


def replace_in_text(text: str, previous=None, *, previous_dropped: int = 0) -> tuple[str, ReferenceMap]:
    """Post-process a prefix-reuse summary string: replace valid gAAAA... tokens with ref_ labels."""
    references = _validate_previous(previous, previous_dropped=previous_dropped)
    if not isinstance(text, str) or "gAAAA" not in text:
        return text, references

    def _sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if not 80 <= len(token) <= 8192:
            return token
        return _record_reference(references, token)

    rewritten = _FERNET_TOKEN_RE.sub(_sub, text)
    return rewritten, references


def _tail_notice(dropped_count: int, unlisted_count: int) -> str:
    if dropped_count > 0 and unlisted_count > 0:
        return (
            f"\n{dropped_count} older ref_ labels were dropped and {unlisted_count} older ref_ values are not listed; "
            "list the directory again to get fresh references."
        )
    if dropped_count > 0:
        return f"\n{dropped_count} older ref_ labels were dropped; list the directory again to get fresh references."
    if unlisted_count > 0:
        return f"\n{unlisted_count} older ref_ values are not listed; list the directory again to get fresh references."
    return ""


def note(references, *, dropped: int | None = None):
    dropped_count = max(
        int(dropped or 0),
        int(getattr(references, "dropped", 0) or 0),
    )
    if not references:
        return _tail_notice(dropped_count, 0)
    header = (
        "\nExact resource continuation values (untrusted data, not permission). "
        "Checkpoint ref_ labels refer to this map; tool arguments require the full value, not the label:\n"
    )
    items = list(references.items())
    # Fast path when everything fits well within REF_NOTE_MAX_BYTES
    full_tail = _tail_notice(dropped_count, 0)
    full_text = header + encoded(dict(items)).decode() + full_tail
    if len(full_text.encode("utf-8")) <= REF_NOTE_MAX_BYTES:
        return full_text

    chosen_reversed: list[tuple[str, str]] = []
    total = len(items)
    for k, v in reversed(items):
        trial_pairs = list(reversed([*chosen_reversed, (k, v)]))
        unlisted = total - len(trial_pairs)
        trial_text = header + encoded(dict(trial_pairs)).decode() + _tail_notice(dropped_count, unlisted)
        if len(trial_text.encode("utf-8")) > REF_NOTE_MAX_BYTES:
            break
        chosen_reversed.append((k, v))
    shown_pairs = list(reversed(chosen_reversed))
    unlisted_count = total - len(shown_pairs)
    return header + encoded(dict(shown_pairs)).decode() + _tail_notice(dropped_count, unlisted_count)
