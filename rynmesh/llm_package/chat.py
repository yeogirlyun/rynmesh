"""Validated, text/tool chat contract shared by local and encrypted inference."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError, best_match
from referencing import Registry

from .adapters import actual_usage, cached_input_tokens
from .image_input_profile import (
    SHARED_V2_CARRIER,
    SHARED_V2_MAX_IMAGE_BYTES,
    SHARED_V2_MAX_IMAGES,
    SHARED_V2_MAX_TOTAL_IMAGE_BYTES,
    current_internal_image_profile,
    current_shared_image_carrier,
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


def _additional_properties_detail(err: Exception) -> str:
    instance = getattr(err, "instance", None)
    schema = getattr(err, "schema", None)
    if isinstance(instance, dict) and isinstance(schema, dict):
        props = schema.get("properties")
        allowed_keys = set(props.keys()) if isinstance(props, dict) else set()
        patterns = schema.get("patternProperties")
        extra_count = 0
        for key in instance:
            if key in allowed_keys:
                continue
            if isinstance(patterns, dict) and any(
                re.search(pat, str(key)) for pat in patterns if isinstance(pat, str)
            ):
                continue
            extra_count += 1
        if extra_count > 0:
            return f"unexpected properties are not allowed ({extra_count} unexpected)"
    return "unexpected properties are not allowed"


def validate_tool_arguments(schema, arguments):
    try:
        validator = Draft202012Validator(schema, registry=Registry())
        errors = list(validator.iter_errors(arguments))
    except (ValidationError, RecursionError) as exc:
        json_path = str(getattr(exc, "json_path", "$") or "$")
        if getattr(exc, "validator", None) == "additionalProperties":
            detail = _additional_properties_detail(exc)
        else:
            detail = str(getattr(exc, "message", str(exc)) or "")[:300]
        err = ValueError(f"client tool arguments do not match its schema at {json_path}: {detail}", json_path, detail)
        err.field_path = json_path
        err.detail = detail
        raise err from exc
    if errors:
        matched = best_match(errors)
        json_path = str(getattr(matched, "json_path", "$") or "$")
        if getattr(matched, "validator", None) == "additionalProperties":
            detail = _additional_properties_detail(matched)
        else:
            detail = str(getattr(matched, "message", "") or "")[:300]
        err = ValueError(f"client tool arguments do not match its schema at {json_path}: {detail}", json_path, detail)
        err.field_path = json_path
        err.detail = detail
        raise err from matched


PROMPT_CACHE_KEY_RE = re.compile(r"[A-Za-z0-9:_-]{1,64}\Z")
CALL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def new_call_id_nonce() -> str:
    return secrets.token_hex(6)


def normalize_call_id(raw: Any, index: int, nonce: str) -> str:
    if raw is None or (isinstance(raw, str) and raw == ""):
        safe_nonce = (
            nonce
            if isinstance(nonce, str) and nonce and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce)
            else hashlib.sha256(str(nonce).encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
        )
        return f"call_{safe_nonce}_{index}"
    text = raw if isinstance(raw, str) else str(raw)
    if CALL_ID_RE.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return f"call_{digest}"


def normalize_message_tool_calls(message: dict[str, Any], nonce: str | None = None) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise TypeError("message must be an object")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return copy.deepcopy(message)
    active_nonce = nonce or new_call_id_nonce()
    normalized_msg = copy.deepcopy(message)
    normalized_calls: list[Any] = []
    for idx, call in enumerate(normalized_msg["tool_calls"]):
        if not isinstance(call, dict):
            normalized_calls.append(call)
            continue
        raw_id = call.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise ValueError("invalid tool call id")
        updated = dict(call)
        updated["id"] = normalize_call_id(raw_id, idx, active_nonce)
        normalized_calls.append(updated)
    normalized_msg["tool_calls"] = normalized_calls
    return normalized_msg


def validate_prompt_cache_key(value: Any) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64 or not PROMPT_CACHE_KEY_RE.fullmatch(value):
        raise ValueError("invalid prompt_cache_key")
    return value


def validate_chat(value: dict[str, Any], *, default_max_tokens: int | None = 512) -> dict[str, Any]:
    allowed = {"messages", "max_tokens", "temperature", "top_p", "stop", "tools", "tool_choice", "stream", "enable_thinking", "chat_template_kwargs", "reasoning_effort", "prompt_cache_key", "parallel_tool_calls"}
    if set(value) - allowed:
        raise ValueError("unsupported chat parameters: " + ", ".join(sorted(set(value) - allowed)))
    body = copy.deepcopy(value)
    if "prompt_cache_key" in body:
        validate_prompt_cache_key(body["prompt_cache_key"])
    if "parallel_tool_calls" in body and type(body["parallel_tool_calls"]) is not bool:
        raise ValueError("parallel_tool_calls must be boolean")
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
    shared_v2 = active_profile is None and current_shared_image_carrier() == SHARED_V2_CARRIER
    shared_v2_images = shared_v2_bytes = 0
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
                elif shared_v2:
                    # Negotiated shared-model carrier: larger images, same inline form.
                    if (not isinstance(url, str) or len(url) > (SHARED_V2_MAX_IMAGE_BYTES + 2) // 3 * 4 + 32
                            or not re.fullmatch(r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}", url)):
                        raise ValueError("images must be bounded inline data URLs")
                    try:
                        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
                    except ValueError:
                        raise ValueError("invalid image data") from None
                    shared_v2_images += 1
                    shared_v2_bytes += len(raw)
                    if not raw or len(raw) > SHARED_V2_MAX_IMAGE_BYTES or shared_v2_bytes > SHARED_V2_MAX_TOTAL_IMAGE_BYTES:
                        raise ValueError("image size limit exceeded")
                    if shared_v2_images > SHARED_V2_MAX_IMAGES:
                        raise ValueError("image count limit exceeded")
                    images += 1
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
    if "max_tokens" in body or default_max_tokens is not None:
        tokens = body.get("max_tokens", default_max_tokens)
        if type(tokens) is not int or not 1 <= tokens <= 393216:
            raise ValueError("max_tokens must be a positive integer <= 393216")
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

    def __init__(self, *, nonce: str | None = None) -> None:
        self.nonce = nonce or new_call_id_nonce()
        self.text = ""
        self.reasoning = ""
        self.calls: dict[int, dict[str, Any]] = {}
        self._index_to_slot: dict[int, int] = {}
        self._id_to_slot: dict[str, int] = {}
        self.usage: dict[str, Any] = {}
        self.finish_reason = "stop"
        self.finished = False

    def _resolve_slot(self, raw_index: Any, raw_id: str | None) -> tuple[int, str | None]:
        has_id = isinstance(raw_id, str) and raw_id != ""
        provisional_norm_id = normalize_call_id(raw_id, 0, self.nonce) if has_id else None
        if raw_index is None:
            if provisional_norm_id is not None and provisional_norm_id in self._id_to_slot:
                return self._id_to_slot[provisional_norm_id], provisional_norm_id
            if not has_id and len(self.calls) == 1:
                only_slot = next(iter(self.calls))
                return only_slot, None
            effective_index = 0
        else:
            if type(raw_index) is not int or raw_index < 0:
                raise ValueError("invalid tool call index")
            effective_index = raw_index

        if effective_index not in self._index_to_slot:
            slot = len(self.calls)
            self._index_to_slot[effective_index] = slot
        else:
            current_slot = self._index_to_slot[effective_index]
            current_id = self.calls[current_slot]["id"]
            if provisional_norm_id is not None and current_id and current_id != provisional_norm_id:
                slot = len(self.calls)
                self._index_to_slot[effective_index] = slot
            else:
                slot = current_slot

        norm_id = normalize_call_id(raw_id, slot, self.nonce) if has_id else None
        return slot, norm_id

    def add(self, chunk: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(chunk, dict):
            raise TypeError("chunk must be an object")
        normalized_chunk = copy.deepcopy(chunk)
        if normalized_chunk.get("usage"):
            self.usage = normalized_chunk["usage"]
        choices = normalized_chunk.get("choices", [])
        if not isinstance(choices, list):
            raise TypeError("choices must be a list")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ValueError("only one completion is supported")
            delta = choice.get("delta")
            if delta is None:
                delta = {}
            elif not isinstance(delta, dict):
                raise TypeError("delta must be an object")
            self.text += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or ""
            raw_fragments = delta.get("tool_calls")
            if raw_fragments is not None and not isinstance(raw_fragments, list):
                raise TypeError("tool_calls must be a list")
            norm_fragments: list[dict[str, Any]] = []
            slots_in_choice: dict[int, dict[str, Any]] = {}
            for item in raw_fragments or []:
                if not isinstance(item, dict):
                    raise TypeError("invalid tool fragment")
                raw_id = item.get("id")
                if raw_id is not None and not isinstance(raw_id, str):
                    raise ValueError("invalid tool fragment id")
                slot, norm_id = self._resolve_slot(item.get("index"), raw_id)
                call = self.calls.setdefault(
                    slot,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if norm_id:
                    call["id"] = norm_id
                    self._id_to_slot[norm_id] = slot
                for extra_field in ("type", "namespace"):
                    if extra_field in item and item[extra_field] is not None:
                        call[extra_field] = item[extra_field]
                function = item.get("function")
                if function is not None and not isinstance(function, dict):
                    raise TypeError("invalid tool fragment function")
                function = function or {}
                for field in ("name", "arguments"):
                    val = function.get(field)
                    if val is not None:
                        if not isinstance(val, str):
                            raise TypeError("invalid tool fragment value")
                        call["function"][field] += val
                norm_item = dict(item)
                norm_item["index"] = slot
                if norm_id:
                    norm_item["id"] = norm_id
                else:
                    norm_item.pop("id", None)
                norm_fragments.append(norm_item)
                slots_in_choice[slot] = norm_item
            if raw_fragments is not None:
                delta["tool_calls"] = norm_fragments
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
                self.finished = True
                for slot in sorted(self.calls):
                    call = self.calls[slot]
                    if not call["id"]:
                        generated_id = normalize_call_id("", slot, self.nonce)
                        call["id"] = generated_id
                        self._id_to_slot[generated_id] = slot
                        if slot in slots_in_choice:
                            slots_in_choice[slot]["id"] = generated_id
                        else:
                            if "tool_calls" not in delta or delta["tool_calls"] is None:
                                delta["tool_calls"] = norm_fragments
                                choice["delta"] = delta
                            norm_fragments.append({"index": slot, "id": generated_id})
        return normalized_chunk

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.calls:
            ordered: list[dict[str, Any]] = []
            for i in sorted(self.calls):
                call = copy.deepcopy(self.calls[i])
                if not call["id"]:
                    call["id"] = normalize_call_id("", i, self.nonce)
                ordered.append(call)
            message["tool_calls"] = ordered
        return message


def reported_token_count(value: Any) -> int | None:
    """Only a nonnegative reported integer is measured usage; missing stays unknown."""
    return value if type(value) is int and value >= 0 else None


def _reasoning_tokens(result: dict[str, Any]) -> int | None:
    if not isinstance(result, dict):
        return None
    direct = reported_token_count(result.get("reasoning_tokens"))
    if direct is not None:
        return direct
    rep = result.get("reported_usage")
    if isinstance(rep, dict):
        from_rep = reported_token_count(rep.get("reasoning_tokens"))
        if from_rep is not None:
            return from_rep
    raw_usage = result.get("usage")
    if isinstance(raw_usage, dict):
        for detail_key in ("completion_tokens_details", "output_tokens_details"):
            details = raw_usage.get(detail_key)
            if isinstance(details, dict):
                val = reported_token_count(details.get("reasoning_tokens"))
                if val is not None:
                    return val
        from_usage = reported_token_count(raw_usage.get("reasoning_tokens"))
        if from_usage is not None:
            return from_usage
    return None


def completion(result: dict[str, Any], model: str, request_id: str) -> dict[str, Any]:
    counts = actual_usage(result)
    usage: dict[str, Any] = {"prompt_tokens": counts["input_tokens"], "completion_tokens": counts["output_tokens"]}
    usage["total_tokens"] = (usage["prompt_tokens"] + usage["completion_tokens"]
                             if all(value is not None for value in usage.values()) else None)
    cached = cached_input_tokens(result)
    if cached is not None and usage["prompt_tokens"] is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    reasoning = _reasoning_tokens(result)
    if reasoning is not None and usage["completion_tokens"] is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    message = copy.deepcopy(result.get("message") or {"role": "assistant", "content": result.get("output", result.get("text", ""))})
    message.pop("provider_state", None)
    return {"id": "chatcmpl-" + request_id, "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message,
                                          "finish_reason": result.get("finish_reason", "stop")}], "usage": usage}
