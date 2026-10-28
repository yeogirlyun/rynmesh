"""Internal model events: complete tool items are distinct from text deltas.

Only an explicit item-done or a chat choice's terminal marker closes a streamed
tool call. Balanced JSON is not a completion signal. These events are for the
Ryn execution loop; only ``text_delta`` is a public text projection.
"""
from __future__ import annotations

import copy
import json
import re

from . import output_policy

_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SUCCESSFUL_FINISH = frozenset({"stop", "tool_calls"})


class ModelEventError(ValueError):
    """A malformed event envelope, never a tool argument/schema rejection."""


def response_calls(message):
    """Validate bounded pairing identities, leaving tool semantics to the router.

    An unknown name, malformed arguments, or a missing function remains a paired
    tool error when its call id is usable. Replacing those with a model retry
    could repeat other calls in the same response.
    """
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) > output_policy.MAX_TOOL_CALLS:
        raise ModelEventError("invalid tool call batch")
    if len(json.dumps(calls).encode()) > output_policy.MAX_RAW_RESPONSE_BYTES:
        raise ModelEventError("tool call batch limit exceeded")
    ids = set()
    for call in calls:
        if not isinstance(call, dict):
            raise ModelEventError("invalid tool call envelope")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id) or call_id in ids:
            raise ModelEventError("invalid or duplicate tool call id")
        ids.add(call_id)
        if len(json.dumps(call).encode()) > output_policy.MAX_RAW_RESPONSE_BYTES:
            raise ModelEventError("tool call envelope limit exceeded")
    return calls


def project_leading_developer(messages, *, supports_developer=False):
    """Preserve prefix order for legacy providers without elevating user data."""
    projected = copy.deepcopy(messages)
    if supports_developer:
        return projected
    for message in projected:
        if message.get("role") not in {"system", "developer"}:
            break
        if message["role"] == "developer":
            message["role"] = "system"
    return projected


class ModelEventStream:
    """Adapt chat chunks or explicit item-done events, then reconcile the result.

    ``finish`` always consumes the authoritative final response, including for
    non-streaming models. It never invents token deltas for a buffered answer.
    """

    def __init__(self, emit, *, stopped=lambda: False, text_limit=20000):
        self.emit = emit
        self.stopped = stopped
        self.text_limit = text_limit
        self.text_chars = 0
        self.calls = {}
        self.completed = {}
        self.completed_indices = {}
        self.response_candidates = {}
        self.provider_indices = {}
        self.response_slots = {}
        self.response_next = self.response_tool_count = 0
        self.response_order_known = True
        self.finished = False

    def _send(self, event):
        if self.emit is not None and not self.stopped():
            self.emit(event)

    def _text(self, text):
        if isinstance(text, str) and text and self.text_chars < self.text_limit:
            text = text[:self.text_limit - self.text_chars]
            self.text_chars += len(text)
            self._send({"type": "text_delta", "text": text})

    def _complete_call(self, index, call, *, provider_index=None):
        if type(index) is not int or not 0 <= index < output_policy.MAX_TOOL_CALLS:
            raise ModelEventError("invalid completed tool index")
        response_calls({"tool_calls": [call]})
        call = copy.deepcopy(call)
        call_id = call["id"]
        previous = self.completed.get(call_id)
        if previous is not None:
            if previous != call:
                raise ModelEventError("tool call changed after completion")
            return
        if len(self.completed) >= output_policy.MAX_TOOL_CALLS:
            raise ModelEventError("tool call batch limit exceeded")
        self.completed[call_id] = call
        self.completed_indices[call_id] = index
        event = {"type": "tool_call_complete", "index": index, "call": copy.deepcopy(call)}
        if provider_index is not None:
            event["provider_index"] = provider_index
        self._send(event)

    def _response_item(self, chunk):
        item = chunk.get("item")
        if not isinstance(item, dict):
            raise ModelEventError("invalid response output item")
        provider_index = chunk.get("output_index")
        call = None
        if item.get("type") == "function_call":
            call = {"id": item.get("call_id"), "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments")}}
            if "namespace" in item:
                call["namespace"] = item["namespace"]
            response_calls({"tool_calls": [call]})
            previous = self.response_candidates.get(call["id"])
            if previous is not None and previous != call:
                raise ModelEventError("response item changed after completion")
            if call["id"] in self.provider_indices and self.provider_indices[call["id"]] != provider_index:
                raise ModelEventError("response item output index changed")
            self.response_candidates[call["id"]] = copy.deepcopy(call)
            response_calls({"tool_calls": list(self.response_candidates.values())})
            self.provider_indices[call["id"]] = provider_index
        # A provider index counts reasoning/text as well as function items. Only
        # a known contiguous output prefix proves a function's tool ordinal.
        if type(provider_index) is not int or provider_index < 0:
            self.response_order_known = False
            self.response_slots.clear()
            return
        if not self.response_order_known:
            return
        if provider_index < self.response_next:
            return  # Duplicate already-consumed item; function identity checked above.
        if len(self.response_slots) >= 256:
            self.response_order_known = False
            self.response_slots.clear()
            return  # Bounded buffering; final response still resolves call order.
        slot = {"type": item.get("type"), "call": copy.deepcopy(call)}
        if provider_index in self.response_slots and self.response_slots[provider_index] != slot:
            raise ModelEventError("response output index changed")
        self.response_slots[provider_index] = slot
        while self.response_next in self.response_slots:
            slot = self.response_slots.pop(self.response_next)
            if slot["call"] is not None:
                self._complete_call(self.response_tool_count, slot["call"], provider_index=self.response_next)
                self.response_tool_count += 1
            self.response_next += 1

    def feed(self, chunk):
        if self.stopped():
            return
        if not isinstance(chunk, dict) or self.finished:
            raise ModelEventError("invalid or late model event")
        kind = chunk.get("type")
        if kind == "text_delta":
            self._text(chunk.get("text"))
            return
        if kind == "tool_call_complete":
            self._complete_call(chunk.get("index", len(self.completed)), chunk.get("call"))
            return
        if kind in {"response.output_item.done", "output_item.done"}:
            self._response_item(chunk)
            return
        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            raise ModelEventError("invalid model choices")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ModelEventError("invalid model choice")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ModelEventError("invalid model delta")
            self._text(delta.get("content"))
            fragments = delta.get("tool_calls") or []
            if not isinstance(fragments, list):
                raise ModelEventError("invalid tool fragments")
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    raise ModelEventError("invalid tool fragment")
                index = fragment.get("index")
                if type(index) is not int or not 0 <= index < output_policy.MAX_TOOL_CALLS:
                    raise ModelEventError("invalid tool fragment index")
                call = self.calls.setdefault(index, {"id": "", "type": "function",
                                                      "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    call["id"] = fragment["id"]
                for field in ("type", "namespace"):
                    if field in fragment:
                        call[field] = fragment[field]
                function = fragment.get("function") or {}
                if not isinstance(function, dict):
                    raise ModelEventError("invalid tool fragment function")
                for field in ("name", "arguments"):
                    value = function.get(field)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ModelEventError("invalid tool fragment value")
                        call["function"][field] += value
                if len(json.dumps(call).encode()) > output_policy.MAX_RAW_RESPONSE_BYTES:
                    raise ModelEventError("tool fragment limit exceeded")
            if choice.get("finish_reason") in _SUCCESSFUL_FINISH:
                calls = [self.calls[index] for index in sorted(self.calls)]
                response_calls({"tool_calls": calls})
                for index, call in sorted(self.calls.items()):
                    self._complete_call(index, call)

    def finish(self, response):
        if self.finished:
            raise ModelEventError("duplicate model response completion")
        calls = response_calls(response["message"])
        final = {call["id"]: call for call in calls}
        candidates = {**self.completed, **self.response_candidates}
        if any(final.get(call_id) != call for call_id, call in candidates.items()):
            raise ModelEventError("completed tool call missing or changed in final response")
        if any(index >= len(calls) or calls[index]["id"] != call_id
               for call_id, index in self.completed_indices.items()):
            raise ModelEventError("completed tool call order changed in final response")
        if response.get("finish_reason") in _SUCCESSFUL_FINISH:
            for index, call in enumerate(calls):
                self._complete_call(index, call, provider_index=self.provider_indices.get(call["id"]))
        self.finished = True
        event_response = copy.deepcopy(response)
        # Private continuation is retained in the authoritative return value,
        # never copied into observer/display events.
        event_response["message"].pop("reasoning_content", None)
        event_response["message"].pop("provider_state", None)
        self._send({"type": "response_complete", **event_response})
