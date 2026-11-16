"""Native provider wire formats for Ryn's text, image and client-tool contract.

Private continuation blocks are bound to an immutable transport/model scope.
They are replayed only when their public projection still matches the history.
No tool is completed by a decoder before the native response terminates.
"""
from __future__ import annotations

import base64
import copy
import functools
import json
import math
import re
from typing import Any

from .chat import new_call_id_nonce, normalize_call_id
from .image_input_profile import (
    current_internal_image_profile,
    project_protocol_request_without_admitted_images,
    verify_messages_against_profile,
)
from .safety import MAX_REQUEST
from .transport_failure import classify_stream_error

RESPONSES = "openai_responses"
MESSAGES = "anthropic_messages"


class NativeStreamError(ValueError):
    """Closed native stream/response error classification carrying body-free diagnostics."""

    def __init__(self, code: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = dict(diagnostics or {})
_LIMIT = 4 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SCOPE = re.compile(r"[a-f0-9]{64}\Z")
_CACHE_KEY = re.compile(r"[A-Za-z0-9:_-]{1,64}\Z")
_IMAGE = re.compile(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})\Z")
_KNOWN_RESPONSE_ITEMS = frozenset({"message", "function_call", "custom_tool_call", "reasoning"})
_KNOWN_RESPONSE_PARTS = frozenset({"output_text", "refusal"})
_KNOWN_MESSAGE_BLOCKS = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})
_KNOWN_MESSAGE_DELTAS = frozenset({"text_delta", "input_json_delta", "thinking_delta", "signature_delta", "citations_delta"})


def _check(protocol, model, scope):
    if not isinstance(protocol, str) or protocol not in {RESPONSES, MESSAGES} or not isinstance(model, str) or not model:
        raise ValueError("unsupported native protocol or model")
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
        raise ValueError("invalid native continuation scope")


def _copy(value):
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > _LIMIT:
            raise ValueError("native protocol payload limit exceeded")
        return copy.deepcopy(value)
    except (TypeError, OverflowError, RecursionError):
        raise ValueError("invalid native protocol data") from None


def _safe(function):
    @functools.wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, IndexError, AttributeError, OverflowError, RecursionError):
            raise ValueError("invalid native protocol data") from None
    return checked


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _usage(raw, protocol):
    raw = raw if isinstance(raw, dict) else {}
    prompt = _count(raw.get("input_tokens"))
    if protocol == MESSAGES and prompt is not None:
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if key in raw:
                count = _count(raw[key])
                if count is None:
                    prompt = None
                    break
                prompt += count
    return {"prompt_tokens": prompt, "completion_tokens": _count(raw.get("output_tokens"))}


def _parts(content, *, images=False):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("invalid native message content")
    active_profile = current_internal_image_profile() if images else None
    max_image_bytes = active_profile.max_single_image_bytes if active_profile is not None else _LIMIT
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("invalid native content part")
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            result.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url" and images:
            image = part.get("image_url")
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise ValueError("invalid native image content")
            match = _IMAGE.fullmatch(image["url"])
            if not match:
                raise ValueError("native images require inline data URLs")
            try:
                decoded = base64.b64decode(match[2], validate=True)
            except ValueError:
                raise ValueError("invalid native image data") from None
            if not decoded or len(decoded) > max_image_bytes:
                raise ValueError("invalid native image data")
            detail = image.get("detail", "auto")
            if detail not in {"auto", "low", "high"}:
                raise ValueError("invalid native image detail")
            result.append({"type": "image", "url": image["url"], "media_type": match[1], "data": match[2], "detail": detail})
        else:
            raise ValueError("unsupported native content part")
    return result


def _text(content):
    return "".join(part["text"] for part in _parts(content))


def _call(call, index=0, nonce="", *, canonical=False):
    if not isinstance(call, dict) or call.get("type", "function") != "function" or "namespace" in call:
        raise ValueError("unsupported native tool call")
    wire_kind = call.get("wire_kind")
    if wire_kind is not None and wire_kind != "custom":
        raise ValueError("unsupported native tool wire_kind")
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not _NAME.fullmatch(function["name"]):
        raise ValueError("invalid native tool name")
    raw_id = call.get("id")
    if canonical:
        if not isinstance(raw_id, str) or not _ID.fullmatch(raw_id):
            raise ValueError("invalid native tool identity")
        call_id = raw_id
    else:
        if raw_id is not None and not isinstance(raw_id, str):
            raise ValueError("invalid native tool identity")
        call_id = normalize_call_id(raw_id, index, nonce or new_call_id_nonce())
    if not isinstance(function.get("arguments"), str):
        raise ValueError("invalid native tool arguments")
    out = {"id": call_id, "type": "function", "function": {"name": function["name"], "arguments": function["arguments"]}}
    if wire_kind == "custom":
        out["wire_kind"] = "custom"
    return out


def _calls(message, *, canonical=False):
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) > 128:
        raise ValueError("invalid native tool call batch")
    nonce = "" if canonical else new_call_id_nonce()
    result = [_call(call, index=idx, nonce=nonce, canonical=canonical) for idx, call in enumerate(calls)]
    if len({call["id"] for call in result}) != len(result):
        raise ValueError("duplicate native tool identity")
    return result


def _object_arguments(value):
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError
        _copy(result)
        return result
    except (ValueError, TypeError, RecursionError):
        raise ValueError("native tool arguments require a JSON object") from None


def _argument_identity(value):
    # Object member order/whitespace may change locally; semantic values may not.
    # Malformed proposals still need an error result, so identical raw strings
    # remain replayable for Responses, whose native arguments are strings.
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result
    try:
        parsed = json.loads(value, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return ("json", json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        return ("raw", value)


def _projection(message):
    return (_text(message.get("content")), [(call["id"], call["function"]["name"], _argument_identity(call["function"]["arguments"])) for call in _calls(message)])


def _from_responses(output, *, nonce="", allow_incomplete=False):
    if not isinstance(output, list) or len(output) > 512:
        raise ValueError("invalid native Responses output")
    active_nonce = nonce or new_call_id_nonce()
    allowed_statuses = {None, "completed", "incomplete", "in_progress"} if allow_incomplete else {None, "completed"}
    texts, calls, refused = [], [], False
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or not item.get("type"):
            raise ValueError("invalid native Responses item")
        kind = item["type"]
        if kind == "message":
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ValueError("invalid native Responses message")
            if item.get("status") not in allowed_statuses:
                raise ValueError("unfinished native response message")
            for part in item["content"]:
                if not isinstance(part, dict) or not isinstance(part.get("type"), str) or not part.get("type"):
                    raise ValueError("invalid native Responses content")
                ptype = part["type"]
                if ptype == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise ValueError("invalid native Responses content")
                    texts.append(part["text"])
                elif ptype == "refusal":
                    if not isinstance(part.get("refusal"), str):
                        raise ValueError("invalid native Responses content")
                    texts.append(part["refusal"])
                    refused = True
        elif kind == "function_call":
            if allow_incomplete:
                continue
            if item.get("status") not in {None, "completed"}:
                raise ValueError("unfinished native tool call")
            calls.append(_call({"id": item.get("call_id"), "function": {"name": item.get("name"), "arguments": item.get("arguments")}}, index=len(calls), nonce=active_nonce))
        elif kind == "custom_tool_call":
            if allow_incomplete:
                continue
            if item.get("status") not in {None, "completed"}:
                raise ValueError("unfinished native tool call")
            raw_input = item.get("input")
            if not isinstance(raw_input, str):
                raise ValueError("invalid native custom tool input")
            arguments = json.dumps({"patch": raw_input}, ensure_ascii=False, allow_nan=False)
            calls.append(_call({"id": item.get("call_id"), "wire_kind": "custom", "function": {"name": item.get("name"), "arguments": arguments}}, index=len(calls), nonce=active_nonce))
        elif kind == "reasoning":
            # This opaque native item, including encrypted_content, is private.
            if "encrypted_content" in item and item["encrypted_content"] is not None and not isinstance(item["encrypted_content"], str):
                raise ValueError("invalid native reasoning continuation")
    message = {"role": "assistant", "content": "".join(texts) or None}
    if calls:
        message["tool_calls"] = calls
    _calls(message, canonical=True)
    return message, refused


def _from_messages(content, *, nonce="", drop_tools=False):
    if not isinstance(content, list) or len(content) > 512:
        raise ValueError("invalid native Messages content")
    active_nonce = nonce or new_call_id_nonce()
    texts, calls = [], []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str) or not block.get("type"):
            raise ValueError("invalid native Messages block")
        kind = block["type"]
        if kind == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("invalid native Messages block")
            texts.append(block["text"])
        elif kind == "tool_use":
            if drop_tools:
                continue
            if not isinstance(block.get("input"), dict):
                raise ValueError("invalid native Messages block")
            arguments = json.dumps(block["input"], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            calls.append(_call({"id": block.get("id"), "function": {"name": block.get("name"), "arguments": arguments}}, index=len(calls), nonce=active_nonce))
        elif kind == "thinking":
            if not isinstance(block.get("thinking"), str):
                raise ValueError("invalid native Messages block")
            if "signature" in block and not isinstance(block["signature"], str):
                raise ValueError("invalid native thinking signature")
        elif kind == "redacted_thinking":
            if not isinstance(block.get("data"), str):
                raise ValueError("invalid native Messages block")
    message = {"role": "assistant", "content": "".join(texts) or None}
    if calls:
        message["tool_calls"] = calls
    _calls(message, canonical=True)
    return message


def _filter_continuation_items(native: list[dict[str, Any]], protocol: str) -> list[dict[str, Any]]:
    if protocol == RESPONSES:
        filtered = []
        for item in native:
            kind = item.get("type")
            if kind not in _KNOWN_RESPONSE_ITEMS:
                continue
            if kind == "message":
                item = {**item, "content": [p for p in item["content"] if p.get("type") in _KNOWN_RESPONSE_PARTS]}
            filtered.append(item)
        return filtered
    return [block for block in native if block.get("type") in _KNOWN_MESSAGE_BLOCKS]


def _continuation(message, protocol, scope, diagnostics=None):
    state = message.get("provider_state")
    if not isinstance(state, dict) or state.get("protocol") != protocol or state.get("scope") != scope:
        return None  # Never transmit private state to another provider/model.
    data = state.get("data")
    key = "output" if protocol == RESPONSES else "content"
    if not isinstance(data, dict) or set(data) != {key}:
        if isinstance(diagnostics, dict):
            diagnostics["provider_state_discarded"] = diagnostics.get("provider_state_discarded", 0) + 1
        return None
    expected = _projection(message)
    try:
        native = _copy(data[key])
        projected = _from_responses(native)[0] if protocol == RESPONSES else _from_messages(native)
        matched = _projection(projected) == expected
    except ValueError:
        matched = False
    if not matched:
        if isinstance(diagnostics, dict):
            diagnostics["provider_state_discarded"] = diagnostics.get("provider_state_discarded", 0) + 1
        return None
    return _filter_continuation_items(native, protocol)


def _custom_call_input(arguments_json: str) -> str:
    parsed = _object_arguments(arguments_json)
    if set(parsed) != {"patch"} or not isinstance(parsed.get("patch"), str):
        raise ValueError("custom tool call arguments require a string patch field")
    return parsed["patch"]


def _tool_definitions(tools, protocol, *, custom_tools: bool = False):
    from ..agent_runtime.local_specs import FREEFORM_GRAMMARS

    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("invalid native tool definitions")
    result, names, custom_names = [], set(), set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if (
            not isinstance(function, dict)
            or tool.get("type") != "function"
            or set(tool) - {"type", "function", "freeform_grammar"}
            or set(function) - {"name", "description", "parameters", "strict", "freeform_grammar"}
        ):
            raise ValueError("unsupported native tool definition")
        name = function.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise ValueError("invalid native tool definition name")
        names.add(name)
        parameters = function.get("parameters", {})
        if not isinstance(parameters, dict) or ("strict" in function and type(function["strict"]) is not bool):
            raise ValueError("invalid native tool definition schema")
        if "description" in function and not isinstance(function["description"], str):
            raise ValueError("invalid native tool description")
        has_explicit_grammar = "freeform_grammar" in function or "freeform_grammar" in tool
        grammar = (
            function.get("freeform_grammar")
            if "freeform_grammar" in function
            else tool.get("freeform_grammar")
            if "freeform_grammar" in tool
            else FREEFORM_GRAMMARS.get(name)
        )
        if has_explicit_grammar and grammar is not None and (not isinstance(grammar, str) or not grammar):
            raise ValueError("invalid native tool freeform grammar")
        if protocol == RESPONSES and custom_tools and isinstance(grammar, str) and grammar:
            entry: dict[str, Any] = {
                "type": "custom",
                "name": name,
                "format": {"type": "grammar", "syntax": "lark", "definition": grammar},
            }
            if "description" in function:
                entry["description"] = function["description"]
            custom_names.add(name)
            result.append(entry)
            continue
        entry = {"name": name, "parameters" if protocol == RESPONSES else "input_schema": _copy(parameters)}
        if "description" in function:
            entry["description"] = function["description"]
        if protocol == RESPONSES:
            entry.update(type="function", strict=function.get("strict", False))
        elif function.get("strict"):
            raise ValueError("strict native Messages tools are not supported")
        result.append(entry)
    return result, names, custom_names


def _tool_choice(choice, protocol, names, custom_names=()):
    if isinstance(choice, str):
        if choice not in {"auto", "none", "required"} or (choice == "required" and not names):
            raise ValueError("invalid native tool choice")
        return choice if protocol == RESPONSES else {"type": {"required": "any"}.get(choice, choice)}
    if not isinstance(choice, dict) or choice.get("type") != "function" or not isinstance(choice.get("function"), dict):
        raise ValueError("invalid native named tool choice")
    name = choice["function"].get("name")
    if name not in names:
        raise ValueError("unknown native named tool choice")
    if protocol == RESPONSES:
        return {"type": "custom" if name in custom_names else "function", "name": name}
    return {"type": "tool", "name": name}


def _messages(body, protocol, scope, diagnostics=None):
    history = body.get("messages")
    if not isinstance(history, list) or not history:
        raise ValueError("native messages must be a nonempty array")
    output, system = [], []
    pending: dict[str, tuple[str, str]] = {}
    seen_conversation = False
    for message in history:
        if not isinstance(message, dict):
            raise ValueError("invalid native message")
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("invalid native role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("native tool result must match a pending call")
            wire_call_id, wire_kind = pending.pop(call_id)
            text = _text(message.get("content"))
            if protocol == RESPONSES:
                out_type = "custom_tool_call_output" if wire_kind == "custom" else "function_call_output"
                output.append({"type": out_type, "call_id": wire_call_id, "output": text})
            else:
                block = {"type": "tool_result", "tool_use_id": wire_call_id, "content": text}
                if "is_error" in message:
                    if type(message["is_error"]) is not bool:
                        raise ValueError("invalid native tool error flag")
                    block["is_error"] = message["is_error"]
                if output and output[-1]["role"] == "user":
                    output[-1]["content"].append(block)
                else:
                    output.append({"role": "user", "content": [block]})
            continue
        if pending:
            raise ValueError("native tool results must precede the next message")
        if protocol == MESSAGES and role in {"system", "developer"}:
            if seen_conversation:
                raise ValueError("native Messages requires leading system instructions")
            system.extend(_parts(message.get("content")))
            continue
        seen_conversation = True
        calls = _calls(message, canonical=True)
        if calls and role != "assistant":
            raise ValueError("native tool calls require assistant role")
        native = _continuation(message, protocol, scope, diagnostics=diagnostics) if role == "assistant" else None
        if native is not None:
            if protocol == RESPONSES:
                raw_items = [
                    (item.get("call_id"), "custom" if item.get("type") == "custom_tool_call" else "function")
                    for item in native
                    if isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}
                ]
            else:
                raw_items = [
                    (block.get("id"), "function")
                    for block in native
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                ]
            for call, (raw_id, raw_kind) in zip(calls, raw_items, strict=False):
                pending[call["id"]] = (
                    raw_id if isinstance(raw_id, str) and raw_id else call["id"],
                    raw_kind,
                )
            if protocol == RESPONSES:
                output.extend(native)
            elif output and output[-1]["role"] == "assistant":
                thinking_blocks = [b for b in native if b.get("type") in {"thinking", "redacted_thinking"}]
                other_blocks = [b for b in native if b.get("type") not in {"thinking", "redacted_thinking"}]
                output[-1]["content"] = [*thinking_blocks, *output[-1]["content"], *other_blocks]
            else:
                output.append({"role": "assistant", "content": native})
            continue
        for call in calls:
            pending[call["id"]] = (
                call["id"],
                "custom" if protocol == RESPONSES and call.get("wire_kind") == "custom" else "function",
            )
        content = _parts(message.get("content"), images=role == "user")
        if protocol == RESPONSES:
            parts = [{"type": "input_image", "image_url": part["url"], "detail": part["detail"]} if part["type"] == "image" else
                     {"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]} for part in content]
            if parts:
                output.append({"role": role, "content": parts})
            for call in calls:
                if call.get("wire_kind") == "custom":
                    output.append({
                        "type": "custom_tool_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "input": _custom_call_input(call["function"]["arguments"]),
                    })
                else:
                    output.append({
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    })
        else:
            blocks = [{"type": "image", "source": {"type": "base64", "media_type": part["media_type"], "data": part["data"]}} if part["type"] == "image" else part for part in content]
            blocks.extend({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": _object_arguments(call["function"]["arguments"])} for call in calls)
            if output and output[-1]["role"] == role:
                output[-1]["content"].extend(blocks)
            else:
                output.append({"role": role, "content": blocks})
    if pending:
        raise ValueError("missing native tool results")
    if not output:
        raise ValueError("native request has no conversation")
    return output, system


def _apply_messages_cache_breakpoints(payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    # Anthropic cache creation charges 1.25x; only place ephemeral breakpoints
    # after step 2 (from step 3 onward, once at least 2 assistant turns exist).
    assistant_turns = sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "assistant")
    if assistant_turns < 2:
        return
    marker = {"type": "ephemeral"}
    system = payload.get("system")
    if isinstance(system, list) and system and isinstance(system[-1], dict):
        system[-1]["cache_control"] = dict(marker)
    tools = payload.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = dict(marker)
    user_msgs = [
        msg for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "user"
        and isinstance(msg.get("content"), list) and msg["content"]
    ]
    if len(user_msgs) >= 2 and isinstance(user_msgs[-2]["content"][-1], dict):
        user_msgs[-2]["content"][-1]["cache_control"] = dict(marker)


@_safe
def make_request(
    body: dict[str, Any],
    protocol: str,
    model: str,
    scope: str,
    *,
    prompt_cache: str | None = None,
    custom_tools: bool | None = None,
    profile: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert canonical history/tools; this function never performs inference."""
    _check(protocol, model, scope)
    if not isinstance(body, dict) or set(body) - {"messages", "max_tokens", "temperature", "top_p", "stop", "tools", "tool_choice", "stream", "reasoning_effort", "thinking", "enable_thinking", "chat_template_kwargs", "prompt_cache_key", "prompt_cache", "custom_tools"}:
        raise ValueError("unsupported native request parameter")
    if "prompt_cache_key" in body:
        cache_key = body["prompt_cache_key"]
        if not isinstance(cache_key, str) or len(cache_key.encode("utf-8")) > 64 or not _CACHE_KEY.fullmatch(cache_key):
            raise ValueError("invalid native prompt_cache_key")
    eff_prompt_cache = prompt_cache if prompt_cache is not None else body.get("prompt_cache", "auto")
    if eff_prompt_cache not in {"auto", "off"}:
        raise ValueError("invalid native prompt_cache")
    if profile is not None and not isinstance(profile, dict):
        raise ValueError("invalid native route profile")
    eff_custom_tools = (
        custom_tools
        if custom_tools is not None
        else profile.get("custom_tools", False)
        if profile is not None
        else body.get("custom_tools", False)
    )
    if type(eff_custom_tools) is not bool:
        raise ValueError("invalid native custom_tools")
    maximum = body.get("max_tokens", 512)
    if type(maximum) is not int or maximum < 1 or type(body.get("stream", False)) is not bool:
        raise ValueError("invalid native request limits")
    thinking = body.get("thinking")
    enabled = body.get("enable_thinking")
    template = body.get("chat_template_kwargs")
    if template is not None:
        if not isinstance(template, dict) or set(template) - {"enable_thinking"}:
            raise ValueError("unsupported native thinking parameter")
        if "enable_thinking" in template:
            if enabled is not None and enabled != template["enable_thinking"]:
                raise ValueError("conflicting native thinking parameters")
            enabled = template["enable_thinking"]
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("invalid native thinking parameter")
    if thinking is not None and thinking is not False and thinking != {"type": "disabled"}:
        raise ValueError("native thinking configuration is not supported")
    if enabled is True:
        raise ValueError("native thinking configuration is not supported")
    active_profile = current_internal_image_profile()
    if active_profile is not None:
        verify_messages_against_profile(body.get("messages"), active_profile)
    items, system = _messages(body, protocol, scope, diagnostics=diagnostics)
    result = {"model": model, "stream": body.get("stream", False), "max_output_tokens" if protocol == RESPONSES else "max_tokens": maximum}
    for key in ("temperature", "top_p"):
        if key in body:
            if key == "temperature":
                temperature = body[key]
                upper = 1 if protocol == MESSAGES else 2
                if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= upper:
                    raise ValueError("unsupported native temperature")
            result[key] = body[key]
    if "tools" in body:
        result["tools"], names, custom_names = _tool_definitions(body["tools"], protocol, custom_tools=eff_custom_tools)
    else:
        names, custom_names = set(), set()
    if "tool_choice" in body:
        result["tool_choice"] = _tool_choice(body["tool_choice"], protocol, names, custom_names)
    if protocol == RESPONSES:
        if body.get("stop") not in (None, "", []):
            raise ValueError("native Responses stop sequences are not supported")
        result.update(input=items, store=False, include=["reasoning.encrypted_content"])
        if "reasoning_effort" in body:
            if not isinstance(body["reasoning_effort"], str) or not body["reasoning_effort"]:
                raise ValueError("invalid native reasoning effort")
            result["reasoning"] = {"effort": body["reasoning_effort"]}
        if eff_prompt_cache != "off" and "prompt_cache_key" in body:
            result["prompt_cache_key"] = body["prompt_cache_key"]
    else:
        if "reasoning_effort" in body:
            raise ValueError("native Messages reasoning effort is not supported")
        result["messages"] = items
        if system:
            result["system"] = system
        if thinking is not None or enabled is False:
            result["thinking"] = {"type": "disabled"}
        if body.get("stop") is not None:
            stop = body["stop"]
            if not isinstance(stop, str) and not (isinstance(stop, list) and all(isinstance(item, str) for item in stop)):
                raise ValueError("invalid native stop sequences")
            result["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if active_profile is not None:
        non_image_projection = project_protocol_request_without_admitted_images(result, protocol, active_profile)
        if protocol == MESSAGES and eff_prompt_cache != "off":
            _apply_messages_cache_breakpoints(non_image_projection)
            _apply_messages_cache_breakpoints(result)
        _copy(non_image_projection)
        raw_limit = min(MAX_REQUEST, active_profile.max_raw_request_bytes)
        try:
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > raw_limit:
                raise ValueError("native protocol payload limit exceeded")
            return copy.deepcopy(result)
        except (TypeError, OverflowError, RecursionError):
            raise ValueError("invalid native protocol data") from None
    if protocol == MESSAGES and eff_prompt_cache != "off":
        _apply_messages_cache_breakpoints(result)
    return _copy(result)


def _has_responses_text(raw: Any) -> bool:
    output = raw.get("output") if isinstance(raw, dict) else None
    if not isinstance(output, list):
        return False
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message" and isinstance(item.get("content"), list):
            for part in item["content"]:
                if isinstance(part, dict):
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str) and part["text"]:
                        return True
                    if part.get("type") == "refusal" and isinstance(part.get("refusal"), str) and part["refusal"]:
                        return True
    return False


def _finish(raw, protocol):
    if protocol == MESSAGES:
        reason = raw.get("stop_reason")
        mapping = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter"}
        if reason not in mapping:
            raise ValueError("unsupported native Messages stop reason")
        return mapping[reason]
    if raw.get("error") or raw.get("status") == "failed":
        code, diag = classify_stream_error(raw.get("error") if raw.get("error") is not None else raw)
        mapped = code if code != "inference_failed" else "source_protocol_response_invalid"
        raise NativeStreamError(mapped, diag)
    status = raw.get("status")
    if status == "completed":
        return "stop"
    if status == "incomplete":
        details = raw.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        if _has_responses_text(raw):
            return "length"
    raise ValueError("unsupported native Responses completion status")


@_safe
def parse_response(raw: dict[str, Any], protocol: str, model: str, scope: str) -> dict[str, Any]:
    """Return canonical assistant message, safe finish reason and reported usage."""
    _check(protocol, model, scope)
    if not isinstance(raw, dict):
        raise ValueError("invalid native response")
    raw = _copy(raw)
    finish = _finish(raw, protocol)
    is_length = finish == "length"
    nonce = new_call_id_nonce()
    if protocol == RESPONSES:
        message, refused = _from_responses(raw.get("output"), nonce=nonce, allow_incomplete=is_length)
        if refused:
            finish = "content_filter"
            is_length = False
        if is_length:
            kept_output = []
            for item in raw["output"]:
                if not isinstance(item, dict) or item.get("type") in {"function_call", "custom_tool_call"}:
                    continue
                if item.get("type") == "message" and item.get("status") not in {None, "completed"}:
                    item = {**item, "status": "completed"}
                kept_output.append(item)
            data = {"output": kept_output}
        else:
            fn_items = [item for item in raw["output"] if isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}]
            for item, norm_call in zip(fn_items, message.get("tool_calls", []), strict=False):
                if not item.get("call_id"):
                    item["call_id"] = norm_call["id"]
            data = {"output": raw["output"]}
    else:
        if raw.get("role") != "assistant" or raw.get("type", "message") != "message":
            raise ValueError("invalid native Messages response")
        message = _from_messages(raw.get("content"), nonce=nonce, drop_tools=is_length)
        if is_length:
            data = {"content": [b for b in raw["content"] if isinstance(b, dict) and b.get("type") != "tool_use"]}
        else:
            tool_blocks = [b for b in raw["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
            for block, norm_call in zip(tool_blocks, message.get("tool_calls", []), strict=False):
                if not block.get("id"):
                    block["id"] = norm_call["id"]
            data = {"content": raw["content"]}
    calls = message.get("tool_calls", [])
    if calls:
        if finish in {"length", "content_filter"}:
            raise ValueError("native response ended before safe tool completion")
        if protocol == MESSAGES and finish != "tool_calls":
            raise ValueError("native tool calls have an inconsistent stop reason")
        finish = "tool_calls"
    elif finish == "tool_calls":
        raise ValueError("native tool stop has no tool calls")
    if finish != "content_filter" and not message.get("content") and not calls:
        if finish == "length":
            raise NativeStreamError("upstream_output_limit_reached")
        raise ValueError("native response has no assistant output")
    message["provider_state"] = {"protocol": protocol, "scope": scope, "data": data}
    return {"message": message, "finish_reason": finish, "usage": _usage(raw.get("usage"), protocol)}


class StreamDecoder:
    """Consume native SSE JSON events and yield canonical public chat chunks.

    Text streams immediately. Tool calls wait for the response's terminal event;
    neither complete-looking JSON nor a content/item stop authorizes execution.
    """

    def __init__(self, protocol: str, model: str, scope: str):
        _check(protocol, model, scope)
        self.protocol, self.model, self.scope = protocol, model, scope
        self.usage = {"prompt_tokens": None, "completion_tokens": None}
        self.cached_tokens = None
        self.cache_creation_input_tokens = None
        self.finish_reason = None
        self.ignored_events = 0
        self._native_usage = {}
        self._final = None
        self._failed = False
        self._text = ""
        self._started = False
        self._identity = None
        self._blocks = {}
        self._ignored_blocks = set()
        self._invalid_tool_json = set()
        self._stopped = set()
        self._arguments = {}
        self._items = {}
        self._ignored_items = set()
        self._item_done = set()
        self._reason = None
        self._stop_sequence = None

    def _update_usage(self, raw):
        if isinstance(raw, dict):
            self._native_usage.update(raw)
            mapped = _usage(self._native_usage, self.protocol)
            self.usage = {key: value if value is not None else self.usage[key] for key, value in mapped.items()}
            if self.protocol == MESSAGES:
                cached = _count(self._native_usage.get("cache_read_input_tokens"))
                created = _count(self._native_usage.get("cache_creation_input_tokens"))
                if created is not None:
                    self.cache_creation_input_tokens = created
            else:
                details = self._native_usage.get("input_tokens_details")
                cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
            if cached is not None:
                self.cached_tokens = cached

    def _delta(self, text):
        if not isinstance(text, str) or len((self._text + text).encode()) > _LIMIT:
            raise ValueError("invalid native text delta")
        self._text += text
        return [{"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}] if text else []

    def _complete(self, raw):
        self._update_usage(raw.get("usage"))
        self.finish_reason = _finish(raw, self.protocol)
        final = parse_response(raw, self.protocol, self.model, self.scope)
        text = final["message"].get("content") or ""
        if not text.startswith(self._text):
            raise ValueError("native final text differs from streamed text")
        output = self._delta(text[len(self._text):])
        calls = final["message"].get("tool_calls", [])
        if calls:
            output.append({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, **call} for index, call in enumerate(calls)]}, "finish_reason": None}]})
        self.finish_reason = final["finish_reason"]
        self.usage = final["usage"]
        output.append({"choices": [{"index": 0, "delta": {}, "finish_reason": self.finish_reason}]})
        output.append({"choices": [], "usage": _copy(self.usage)})
        self._final = final
        return output

    def _response_event(self, event):
        kind = event.get("type")
        if kind == "ping":
            return []
        if kind in {"response.created", "response.in_progress"}:
            response = event.get("response")
            if not isinstance(response, dict):
                raise ValueError("invalid native response start")
            identity = response.get("id")
            if self._identity is not None and identity != self._identity:
                raise ValueError("native response identity changed")
            self._identity, self._started = identity, True
            self._update_usage(response.get("usage"))
            return []
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if not isinstance(response, dict) or response.get("status") != kind.removeprefix("response."):
                raise ValueError("invalid native response termination")
            if self._identity is not None and response.get("id") != self._identity:
                raise ValueError("native response identity changed")
            if (
                kind == "response.incomplete"
                and self._text
                and isinstance(response.get("output"), list)
                and not _has_responses_text(response)
                and (not isinstance(response.get("incomplete_details"), dict)
                     or response["incomplete_details"].get("reason") != "content_filter")
            ):
                response = {
                    **response,
                    "output": [
                        *response["output"],
                        {
                            "id": "msg_partial",
                            "type": "message",
                            "role": "assistant",
                            "status": "incomplete",
                            "content": [{"type": "output_text", "text": self._text}],
                        },
                    ],
                }
            self._update_usage(response.get("usage"))
            self.finish_reason = _finish(response, self.protocol)
            final_items = response.get("output")
            if not isinstance(final_items, list):
                raise ValueError("invalid native final output")
            for index, item in self._items.items():
                if self.finish_reason == "length" and item.get("type") in {"function_call", "custom_tool_call"} and index >= len(final_items):
                    continue
                if index >= len(final_items):
                    raise ValueError("native output item missing from final response")
                final_item = final_items[index]
                if not isinstance(final_item, dict) or item.get("type") != final_item.get("type") or item.get("id") != final_item.get("id"):
                    raise ValueError("native output identity changed")
                if item.get("type") in {"function_call", "custom_tool_call"}:
                    field_key = "input" if item["type"] == "custom_tool_call" else "arguments"
                    for key in ("call_id", "name"):
                        if item.get(key) != final_item.get(key):
                            raise ValueError("native tool identity changed")
                    if self.finish_reason != "length":
                        observed = self._arguments.get(index, "")
                        if observed and observed != final_item.get(field_key):
                            raise ValueError("native tool arguments changed after streaming")
            return self._complete(response)
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            return self._delta(event.get("delta"))
        if kind in {"response.output_item.added", "response.output_item.done"}:
            index, item = event.get("output_index"), event.get("item")
            if (
                type(index) is not int
                or not 0 <= index < 512
                or not isinstance(item, dict)
                or not isinstance(item.get("type"), str)
                or not item.get("type")
            ):
                raise ValueError("unsupported native output item event")
            if item["type"] not in _KNOWN_RESPONSE_ITEMS:
                if index in self._items:
                    raise ValueError("native output item identity changed")
                self._ignored_items.add(index)
                self.ignored_events += 1
                return []
            if index in self._ignored_items:
                raise ValueError("native output item identity changed")
            if index in self._item_done:
                raise ValueError("native output item changed after completion")
            previous = self._items.get(index)
            if previous is not None and (previous.get("id") != item.get("id") or previous.get("type") != item.get("type")):
                raise ValueError("native output item identity changed")
            self._items[index] = _copy(item)
            if item["type"] in {"function_call", "custom_tool_call"}:
                if previous is not None and any(previous.get(key) != item.get(key) for key in ("call_id", "name")):
                    raise ValueError("native tool identity changed")
                field_key = "input" if item["type"] == "custom_tool_call" else "arguments"
                arguments = item.get(field_key, "")
                if not isinstance(arguments, str):
                    raise ValueError("invalid native tool argument event")
                if kind.endswith(".done"):
                    observed = self._arguments.get(index, "")
                    if observed and observed != arguments:
                        raise ValueError("native completed tool arguments changed")
                    self._arguments[index] = arguments
                else:
                    self._arguments[index] = arguments
            if kind.endswith(".done"):
                self._item_done.add(index)
            return []
        if kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            index = event.get("output_index")
            item = self._items.get(index)
            if not isinstance(item, dict) or item.get("type") != "function_call" or index in self._item_done or event.get("item_id") != item.get("id"):
                raise ValueError("native tool delta has no matching item")
            value = event.get("delta" if kind.endswith(".delta") else "arguments")
            if not isinstance(value, str):
                raise ValueError("invalid native tool argument delta")
            observed = self._arguments.get(index, "")
            if kind.endswith(".done"):
                if observed and observed != value:
                    raise ValueError("native tool argument completion changed")
                self._arguments[index] = value
            else:
                self._arguments[index] = observed + value
            if len(self._arguments[index].encode()) > _LIMIT:
                raise ValueError("native tool argument limit exceeded")
            return []
        if kind in {"response.custom_tool_call_input.delta", "response.custom_tool_call_input.done"}:
            index = event.get("output_index")
            item = self._items.get(index)
            if not isinstance(item, dict) or item.get("type") != "custom_tool_call" or index in self._item_done or (event.get("item_id") is not None and event.get("item_id") != item.get("id")):
                raise ValueError("native tool delta has no matching item")
            value = event.get("delta" if kind.endswith(".delta") else "input")
            if not isinstance(value, str):
                raise ValueError("invalid native tool argument delta")
            observed = self._arguments.get(index, "")
            if kind.endswith(".done"):
                if observed and observed != value:
                    raise ValueError("native tool argument completion changed")
                self._arguments[index] = value
            else:
                self._arguments[index] = observed + value
            if len(self._arguments[index].encode()) > _LIMIT:
                raise ValueError("native tool argument limit exceeded")
            return []
        if kind in {"response.content_part.added", "response.content_part.done"}:
            if event.get("output_index") in self._ignored_items:
                self.ignored_events += 1
                return []
            part = event.get("part")
            if not isinstance(part, dict) or not isinstance(part.get("type"), str) or not part.get("type"):
                raise ValueError("unsupported native response content event")
            if part["type"] not in _KNOWN_RESPONSE_PARTS:
                self.ignored_events += 1
                return []
            return []
        if kind in {"response.output_text.done", "response.refusal.done", "response.output_text.annotation.added",
                    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                    "response.reasoning_text.delta", "response.reasoning_text.done"}:
            return []
        if isinstance(kind, str) and kind:
            self.ignored_events += 1
            return []
        raise ValueError("unsupported native Responses stream event")

    def _message_event(self, event):
        kind = event.get("type")
        if kind == "ping":
            return []
        if kind == "message_start":
            message = event.get("message")
            if self._started or not isinstance(message, dict) or message.get("role") != "assistant" or message.get("content") != []:
                raise ValueError("invalid native message start")
            self._identity, self._started = message.get("id"), True
            self._update_usage(message.get("usage"))
            return []
        if not self._started:
            raise ValueError("native Messages stream has no start")
        if kind == "content_block_start":
            index, block = event.get("index"), event.get("content_block")
            if (
                type(index) is not int
                or not 0 <= index < 512
                or index in self._blocks
                or index in self._ignored_blocks
                or not isinstance(block, dict)
                or not isinstance(block.get("type"), str)
                or not block.get("type")
            ):
                raise ValueError("invalid native content block start")
            if block["type"] not in _KNOWN_MESSAGE_BLOCKS:
                self._ignored_blocks.add(index)
                self.ignored_events += 1
                return []
            self._blocks[index] = _copy(block)
            if block["type"] == "text":
                return self._delta(block.get("text"))
            return []
        if kind == "content_block_delta":
            index, delta = event.get("index"), event.get("delta")
            if index in self._ignored_blocks:
                self.ignored_events += 1
                return []
            block = self._blocks.get(index)
            if index in self._stopped or not isinstance(block, dict) or not isinstance(delta, dict):
                raise ValueError("invalid native content delta")
            dtype = delta.get("type")
            if dtype == "text_delta" and block["type"] == "text":
                text = delta.get("text")
                output = self._delta(text)
                block["text"] += text
                return output
            if dtype == "input_json_delta" and block["type"] == "tool_use":
                value = delta.get("partial_json")
                if not isinstance(value, str) or (block.get("input") and index not in self._arguments):
                    raise ValueError("invalid native tool input delta")
                self._arguments[index] = self._arguments.get(index, "") + value
                if len(self._arguments[index].encode()) > _LIMIT:
                    raise ValueError("native tool argument limit exceeded")
                return []
            if dtype in {"thinking_delta", "signature_delta"} and block["type"] == "thinking":
                key = "thinking" if dtype == "thinking_delta" else "signature"
                value = delta.get(key)
                if not isinstance(value, str):
                    raise ValueError("invalid native thinking delta")
                block[key] = block.get(key, "") + value
                if len(block[key].encode()) > _LIMIT:
                    raise ValueError("native thinking limit exceeded")
                return []
            if dtype == "citations_delta" and block["type"] == "text" and isinstance(delta.get("citation"), dict):
                block.setdefault("citations", []).append(_copy(delta["citation"]))
                return []
            if isinstance(dtype, str) and dtype and dtype not in _KNOWN_MESSAGE_DELTAS:
                self.ignored_events += 1
                return []
            raise ValueError("unsupported native Messages stream delta")
        if kind == "content_block_stop":
            index = event.get("index")
            if index in self._ignored_blocks:
                self.ignored_events += 1
                return []
            if index not in self._blocks or index in self._stopped:
                raise ValueError("invalid native block termination")
            if index in self._arguments:
                try:
                    self._blocks[index]["input"] = _object_arguments(self._arguments[index])
                except ValueError:
                    self._invalid_tool_json.add(index)
            self._stopped.add(index)
            return []
        if kind == "message_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("invalid native message delta")
            self._update_usage(event.get("usage"))
            if delta.get("stop_reason") is not None:
                self._reason = delta["stop_reason"]
                self._stop_sequence = delta.get("stop_sequence")
                self.finish_reason = _finish({"stop_reason": self._reason}, self.protocol)
            return []
        if kind == "message_stop":
            all_indices = set(self._blocks) | self._ignored_blocks
            if set(self._blocks) != self._stopped or sorted(all_indices) != list(range(len(all_indices))):
                raise ValueError("native message has unfinished content blocks")
            if self._invalid_tool_json and self.finish_reason != "length":
                raise ValueError("native tool arguments require a JSON object")
            return self._complete({"type": "message", "role": "assistant", "id": self._identity,
                                   "content": [self._blocks[index] for index in sorted(self._blocks)],
                                   "stop_reason": self._reason, "stop_sequence": self._stop_sequence,
                                   "usage": self._native_usage})
        if isinstance(kind, str) and kind:
            self.ignored_events += 1
            return []
        raise ValueError("unsupported native Messages stream event")

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if self._failed:
            raise ValueError("native stream is invalid")
        if self._final is not None:
            if isinstance(event, dict) and (not event or event.get("type") == "ping"):
                return []
            raise ValueError("native stream continued after termination")
        if not isinstance(event, dict):
            self._failed = True
            raise ValueError("native stream returned an error")
        if event.get("type") == "error":
            self._failed = True
            code, diag = classify_stream_error(event)
            mapped = code if code != "inference_failed" else "source_protocol_response_invalid"
            raise NativeStreamError(mapped, diag)
        try:
            return self._response_event(event) if self.protocol == RESPONSES else self._message_event(event)
        except ValueError:
            self._failed = True
            raise
        except (KeyError, TypeError, IndexError, AttributeError, OverflowError, RecursionError):
            self._failed = True
            raise ValueError("invalid native stream event") from None

    def result(self) -> dict[str, Any]:
        if self._failed or self._final is None:
            raise ValueError("native stream terminated before completion")
        out = _copy(self._final)
        if self.ignored_events > 0:
            out["diagnostics"] = {**(out.get("diagnostics") or {}), "ignored_stream_events": self.ignored_events}
        return out
