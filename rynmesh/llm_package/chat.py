"""Validated, text/tool chat contract shared by local and encrypted inference."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry

from .adapters import actual_usage
from .image_input_profile import (
    current_internal_image_profile,
    verify_messages_against_profile,
)
from .reasoning import validate_effort
from .safety import MAX_REQUEST


def validate_provider_state(state):
    """Validate opaque native continuation without treating it as model prose.

    The adapter additionally checks the protocol and destination scope before
    replay. This shared boundary only admits bounded JSON with an explicit
    namespace, never arbitrary Python objects or non-finite JSON extensions.
    """
    if (not isinstance(state, dict) or set(state) != {"protocol", "scope", "data"}
            or not isinstance(state.get("protocol"), str)
            or state["protocol"] not in {"openai_responses", "anthropic_messages"}
            or not isinstance(state.get("scope"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", state["scope"])):
        raise ValueError("invalid private provider state")
    pending = [(state["data"], 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 65536:
            raise ValueError("private provider state complexity limit exceeded")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("private provider state must contain JSON")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif value is not None and type(value) not in (str, int, bool, float):
            raise ValueError("private provider state must contain JSON")
        elif type(value) is float and not math.isfinite(value):
            raise ValueError("private provider state must contain finite JSON")
    if len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode()) > MAX_REQUEST:
        raise ValueError("private provider state exceeds request byte limit")


def validate_tool_schema(schema):
    pending = [(schema, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise ValueError("tool schema complexity limit exceeded")
        if isinstance(value, dict):
            for name, item in value.items():
                if name in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValueError("external tool schema references are disabled")
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError) as exc:
        raise ValueError("invalid tool parameter schema") from exc


def validate_tool_arguments(schema, arguments):
    try:
        Draft202012Validator(schema, registry=Registry()).validate(arguments)
    except (ValidationError, RecursionError) as exc:
        raise ValueError("client tool arguments do not match its schema") from exc


def validate_chat(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {"messages", "max_tokens", "temperature", "top_p", "stop", "tools", "tool_choice", "stream", "enable_thinking", "chat_template_kwargs", "reasoning_effort"}
    if set(value) - allowed:
        raise ValueError("unsupported chat parameters: " + ", ".join(sorted(set(value) - allowed)))
    body = copy.deepcopy(value)
    if "reasoning_effort" in body:
        validate_effort(body["reasoning_effort"])
    if "chat_template_kwargs" in body:
        options = body.pop("chat_template_kwargs")
        if not isinstance(options, dict) or set(options) - {"enable_thinking"}:
            raise ValueError("only enable_thinking is supported in chat_template_kwargs")
        if "enable_thinking" in options:
            if "enable_thinking" in body and (type(body["enable_thinking"]) is not type(options["enable_thinking"]) or body["enable_thinking"] != options["enable_thinking"]):
                raise ValueError("conflicting enable_thinking values")
            body["enable_thinking"] = options["enable_thinking"]
    if "enable_thinking" in body and type(body["enable_thinking"]) is not bool:
        raise ValueError("enable_thinking must be boolean")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    active_profile = current_internal_image_profile()
    total_profile_images = 0
    pending: set[str] = set()
    for msg_idx, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("invalid message role")
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content", "provider_state", "is_error"}:
            raise ValueError("unsupported message fields")
        if "is_error" in message and (message["role"] != "tool" or type(message["is_error"]) is not bool):
            raise ValueError("is_error must be a tool result boolean")
        if "reasoning_content" in message and (message["role"] != "assistant" or (message["reasoning_content"] is not None and not isinstance(message["reasoning_content"], str))):
            raise ValueError("reasoning_content must be assistant text")
        if "provider_state" in message:
            if message["role"] != "assistant":
                raise ValueError("private provider state requires an assistant message")
            validate_provider_state(message["provider_state"])
        content = message.get("content")
        if isinstance(content, list):
            images = 0
            for part_idx, part in enumerate(content):
                if not isinstance(part, dict):
                    raise ValueError("invalid content part")
                if part.get("type") == "text" and isinstance(part.get("text"), str) and not set(part) - {"type", "text"}:
                    continue
                if part.get("type") != "image_url" or message["role"] != "user" or set(part) - {"type", "image_url"}:
                    raise ValueError("only user text and inline images are supported")
                image = part.get("image_url")
                if not isinstance(image, dict) or set(image) - {"url", "detail"} or not isinstance(image.get("detail", "auto"), str) or image.get("detail", "auto") not in {"auto", "low", "high"}:
                    raise ValueError("invalid image part")
                url = image.get("url")
                if active_profile is not None:
                    if total_profile_images >= len(active_profile.admitted_images):
                        raise ValueError("image count limit exceeded")
                    expected_ident = active_profile.admitted_images[total_profile_images]
                    if (
                        expected_ident.message_index != msg_idx
                        or expected_ident.part_index != part_idx
                        or image.get("detail") != expected_ident.explicit_detail
                        or not isinstance(url, str)
                    ):
                        raise ValueError("images must be bounded inline data URLs")
                    url_bytes = url.encode("utf-8")
                    if (
                        len(url_bytes) != expected_ident.raw_url_utf8_bytes
                        or hashlib.sha256(url_bytes).hexdigest() != expected_ident.url_sha256
                    ):
                        raise ValueError("images must be bounded inline data URLs")
                    images += 1
                    total_profile_images += 1
                else:
                    if not isinstance(url, str) or len(url) > 180000 or not re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}", url):
                        raise ValueError("images must be bounded inline data URLs")
                    try:
                        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
                    except ValueError:
                        raise ValueError("invalid image data") from None
                    if not raw or len(raw) > 128 * 1024:
                        raise ValueError("image size limit exceeded")
                    images += 1
            if images > 4:
                raise ValueError("image count limit exceeded")
            if not images:
                message["content"] = content = "".join(part["text"] for part in content)
        if content is not None and not isinstance(content, (str, list)):
            raise ValueError("message content must be text")
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("tool result must reference a pending tool call")
            pending.remove(call_id)
        elif pending:
            raise ValueError("all tool results must precede the next message")
        calls = message.get("tool_calls", [])
        if calls and (message["role"] != "assistant" or not isinstance(calls, list)):
            raise ValueError("tool_calls require an assistant message")
        for call in calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            if not isinstance(call, dict) or not isinstance(function, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or not function.get("name") or not isinstance(function.get("arguments"), str):
                raise ValueError("invalid tool call")
            if call["id"] in pending:
                raise ValueError("duplicate tool call id")
            pending.add(call["id"])
    if pending:
        raise ValueError("missing tool results")
    if active_profile is not None:
        verify_messages_against_profile(messages, active_profile)
    tokens = body.get("max_tokens", 512)
    if type(tokens) is not int or not 1 <= tokens <= 131072:
        raise ValueError("max_tokens must be a positive integer <= 131072")
    body["max_tokens"] = tokens
    if type(body.get("stream", False)) is not bool:
        raise ValueError("stream must be boolean")
    for key, limit in (("temperature", 2), ("top_p", 1)):
        if key in body and (type(body[key]) not in (float, int) or not math.isfinite(body[key]) or not 0 <= body[key] <= limit):
            raise ValueError(f"invalid {key}")
    if "stop" in body and body["stop"] is not None:
        stop = body["stop"]
        if not isinstance(stop, str) and not (isinstance(stop, list) and len(stop) <= 4 and all(isinstance(x, str) for x in stop)):
            raise ValueError("stop must be text or up to four strings")
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("invalid tools")
    names = set()
    for tool in tools:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("only named function tools are supported")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", function["name"]) or function["name"] in names:
            raise ValueError("invalid or duplicate tool name")
        if not isinstance(function.get("parameters", {}), dict):
            raise ValueError("tool parameters must be a JSON schema object")
        validate_tool_schema(function.get("parameters", {}))
        if "strict" in function and type(function["strict"]) is not bool:
            raise ValueError("strict must be boolean")
        names.add(function["name"])
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if choice.get("type") != "function" or choice.get("function", {}).get("name") not in names:
            raise ValueError("unknown tool_choice function")
    elif not isinstance(choice, str) or choice not in {"auto", "none", "required"}:
        raise ValueError("invalid tool_choice")
    if choice != "auto" and choice != "none" and not tools:
        raise ValueError("tool_choice requires tools")
    raw_limit = min(MAX_REQUEST, active_profile.max_raw_request_bytes) if active_profile is not None else MAX_REQUEST
    if len(json.dumps(body, ensure_ascii=False).encode()) > raw_limit:
        raise ValueError("chat exceeds request byte limit")
    return body


class ChatAccumulator:
    """Assemble actual upstream deltas without losing tool arguments or usage."""

    def __init__(self) -> None:
        self.text = ""
        self.reasoning = ""
        self.calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.finish_reason = "stop"
        self.finished = False

    def add(self, chunk: dict[str, Any]) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("only one completion is supported")
            delta = choice.get("delta", {})
            self.text += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or ""
            for item in delta.get("tool_calls") or []:
                call = self.calls.setdefault(item["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if item.get("id"):
                    call["id"] = item["id"]
                for field in ("name", "arguments"):
                    call["function"][field] += item.get("function", {}).get(field) or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
                self.finished = True

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return message


def reported_token_count(value: Any) -> int | None:
    """Only a nonnegative reported integer is measured usage; missing stays unknown."""
    return value if type(value) is int and value >= 0 else None


def completion(result: dict[str, Any], model: str, request_id: str) -> dict[str, Any]:
    counts = actual_usage(result)
    usage = {"prompt_tokens": counts["input_tokens"], "completion_tokens": counts["output_tokens"]}
    usage["total_tokens"] = (usage["prompt_tokens"] + usage["completion_tokens"]
                             if all(value is not None for value in usage.values()) else None)
    message = copy.deepcopy(result.get("message") or {"role": "assistant", "content": result.get("output", result.get("text", ""))})
    message.pop("provider_state", None)
    return {"id": "chatcmpl-" + request_id, "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message,
                                          "finish_reason": result.get("finish_reason", "stop")}], "usage": usage}
