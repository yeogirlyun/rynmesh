"""Stateless text and client-executed tool subsets of three inference APIs."""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from .chat import reported_token_count, validate_chat, validate_prompt_cache_key

IGNORABLE_PARAMETERS = frozenset(
    {"metadata", "user", "service_tier", "top_k", "text", "truncation", "prompt_cache_key"}
)


def text_content(value: Any, *, path: str = "content") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{path}[{idx}] must be an object")
            if item.get("type") not in {"text", "input_text", "output_text"} or not isinstance(item.get("text"), str):
                raise ValueError("only text content is supported")
            pieces.append(item["text"])
        return "".join(pieces)
    raise ValueError("only text content is supported")


def user_content(value: Any, *, protocol: str, path: str = "content") -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError(f"{path} must be text or an array")
    parts: list[dict[str, Any]] = []
    has_image = False
    for idx, item in enumerate(value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_path} must be an object")
        kind = item.get("type")
        if kind in {"text", "input_text"}:
            if not isinstance(item.get("text"), str):
                raise ValueError(f"{item_path}.text is required")
            parts.append({"type": "text", "text": item["text"]})
        elif protocol == "chat" and kind == "image_url":
            if set(item) - {"type", "image_url"}:
                raise ValueError(f"{item_path}: invalid image part")
            raw_img = item.get("image_url")
            if isinstance(raw_img, str) and raw_img:
                img_obj: dict[str, Any] = {"url": raw_img}
            elif isinstance(raw_img, dict):
                if set(raw_img) - {"url", "detail"}:
                    raise ValueError(f"{item_path}.image_url: invalid image part")
                if not isinstance(raw_img.get("url"), str) or not raw_img["url"]:
                    raise ValueError(f"{item_path}.image_url.url is required")
                img_obj = {"url": raw_img["url"]}
                if "detail" in raw_img and raw_img["detail"] is not None:
                    img_obj["detail"] = raw_img["detail"]
            else:
                raise ValueError(f"{item_path}.image_url is required")
            parts.append({"type": "image_url", "image_url": img_obj})
            has_image = True
        elif protocol == "responses" and kind == "input_image":
            raw_url = item.get("image_url")
            if not isinstance(raw_url, str) or not raw_url:
                raise ValueError(f"{item_path}.image_url is required")
            img_obj = {"url": raw_url}
            if "detail" in item and item["detail"] is not None:
                img_obj["detail"] = item["detail"]
            parts.append({"type": "image_url", "image_url": img_obj})
            has_image = True
        elif protocol == "messages" and kind == "image":
            source = item.get("source")
            if not isinstance(source, dict):
                raise ValueError(f"{item_path}.source must be an object")
            if source.get("type") != "base64":
                raise ValueError(f"{item_path}.source.type must be base64")
            media_type = source.get("media_type")
            data = source.get("data")
            if not isinstance(media_type, str) or not media_type:
                raise ValueError(f"{item_path}.source.media_type is required")
            if not isinstance(data, str) or not data:
                raise ValueError(f"{item_path}.source.data is required")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})
            has_image = True
        else:
            raise ValueError(f"{item_path}: unsupported content block")
    return parts if has_image else "".join(part["text"] for part in parts)


def _parse_messages_tool_input(raw_arguments: Any) -> dict[str, Any]:
    raw_str = raw_arguments if isinstance(raw_arguments, str) else str(raw_arguments or "")
    try:
        parsed = json.loads(raw_str)
    except (ValueError, TypeError):
        return {"_raw_arguments": raw_str}
    return parsed if isinstance(parsed, dict) else {"_raw_arguments": raw_str}


def normalize_with_ignored(body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(body, dict):
        raise ValueError("request must be a JSON object")
    common = {"model", "stream", "temperature", "top_p", "tools", "tool_choice", "parallel_tool_calls"} | IGNORABLE_PARAMETERS
    allowed = common | (
        {"messages", "max_tokens", "max_completion_tokens", "stop", "stream_options", "n", "enable_thinking", "chat_template_kwargs", "reasoning_effort"}
        if protocol == "chat"
        else {"input", "instructions", "max_output_tokens", "store", "previous_response_id", "reasoning"}
        if protocol == "responses"
        else {"messages", "system", "max_tokens", "stop_sequences", "thinking"}
    )
    if set(body) - allowed:
        raise ValueError("unsupported parameters: " + ", ".join(sorted(set(body) - allowed)))
    ignored = set(body) & (IGNORABLE_PARAMETERS - {"prompt_cache_key"})
    result: dict[str, Any] = {
        key: body[key]
        for key in ("stream", "temperature", "top_p", "tool_choice", "parallel_tool_calls")
        if key in body
    }
    if "prompt_cache_key" in body:
        if protocol in {"chat", "responses"}:
            validate_prompt_cache_key(body["prompt_cache_key"])
            result["prompt_cache_key"] = body["prompt_cache_key"]
        else:
            ignored.add("prompt_cache_key")

    if protocol == "chat":
        if body.get("n", 1) != 1:
            raise ValueError("only n=1 is supported")
        stream_opts = body.get("stream_options")
        if stream_opts is not None:
            if not isinstance(stream_opts, dict) or set(stream_opts) - {"include_usage"}:
                raise ValueError("unsupported stream_options")
        if "max_tokens" in body and "max_completion_tokens" in body:
            raise ValueError("use only one maximum token parameter")
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("messages must be a non-empty array")
        chat_messages: list[dict[str, Any]] = []
        for idx, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                raise ValueError(f"messages[{idx}] must be an object")
            role = msg.get("role")
            if not isinstance(role, str) or not role:
                raise ValueError(f"messages[{idx}].role is required")
            if role == "tool" and (not isinstance(msg.get("tool_call_id"), str) or not msg.get("tool_call_id")):
                raise ValueError(f"messages[{idx}].tool_call_id is required")
            copied = copy.deepcopy(msg)
            if isinstance(copied.get("content"), list):
                if role == "user":
                    copied["content"] = user_content(copied["content"], protocol="chat", path=f"messages[{idx}].content")
                else:
                    copied["content"] = text_content(copied["content"], path=f"messages[{idx}].content")
            chat_messages.append(copied)
        result["messages"] = chat_messages
        result.update(
            {
                key: body[key]
                for key in ("tools", "stop", "enable_thinking", "chat_template_kwargs", "reasoning_effort")
                if key in body
            }
        )
        max_tok = body.get("max_completion_tokens") if "max_completion_tokens" in body else body.get("max_tokens")
        if max_tok is not None:
            result["max_tokens"] = max_tok
    elif protocol == "responses":
        if body.get("store") or body.get("previous_response_id"):
            raise ValueError("Responses is stateless: use store=false and send conversation history in input")
        if body.get("max_output_tokens") is not None:
            result["max_tokens"] = body["max_output_tokens"]
        if "reasoning" in body and body["reasoning"] is not None:
            reasoning_cfg = body["reasoning"]
            if not isinstance(reasoning_cfg, dict):
                raise ValueError("reasoning must be an object")
            if set(reasoning_cfg) - {"effort", "summary", "generate_summary"}:
                raise ValueError("unsupported reasoning fields")
            if reasoning_cfg.get("effort") is not None:
                result["reasoning_effort"] = reasoning_cfg["effort"]
        messages: list[dict[str, Any]] = []
        if body.get("instructions") is not None and body.get("instructions") != "":
            messages.append({"role": "system", "content": text_content(body["instructions"], path="instructions")})
        if "input" not in body or body["input"] is None:
            raise ValueError("input is required")
        items = body["input"]
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        if not isinstance(items, list):
            raise ValueError("input must be text or an array")
        for idx, item in enumerate(items):
            item_path = f"input[{idx}]"
            if not isinstance(item, dict):
                raise ValueError(f"{item_path} must be an object")
            kind = item.get("type", "message")
            if not isinstance(kind, str):
                raise ValueError(f"{item_path}.type must be a string")
            if kind == "message":
                role = item.get("role")
                if not isinstance(role, str) or not role:
                    raise ValueError(f"{item_path}.role is required")
                if "content" not in item or item["content"] is None:
                    raise ValueError(f"{item_path}.content is required")
                content_val = (
                    user_content(item["content"], protocol="responses", path=f"{item_path}.content")
                    if role == "user"
                    else text_content(item["content"], path=f"{item_path}.content")
                )
                messages.append({"role": role, "content": content_val})
            elif kind == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError(f"{item_path}.call_id is required")
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{item_path}.name is required")
                if not isinstance(arguments, str):
                    raise ValueError(f"{item_path}.arguments is required")
                call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                if messages and messages[-1].get("tool_calls"):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif kind == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError(f"{item_path}.call_id is required")
                if "output" not in item or item["output"] is None:
                    raise ValueError(f"{item_path}.output is required")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": text_content(item["output"], path=f"{item_path}.output"),
                    }
                )
            else:
                raise ValueError("unsupported Responses input item: " + str(kind))
        result["messages"] = messages
        if "tools" in body:
            raw_tools = body["tools"]
            if not isinstance(raw_tools, list):
                raise ValueError("tools must be an array")
            result["tools"] = []
            for idx, tool in enumerate(raw_tools):
                if not isinstance(tool, dict):
                    raise ValueError(f"tools[{idx}] must be an object")
                if tool.get("type") != "function":
                    raise ValueError("only client-executed function tools are supported")
                if not isinstance(tool.get("name"), str) or not tool["name"]:
                    raise ValueError(f"tools[{idx}].name is required")
                result["tools"].append({"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}})
        if isinstance(result.get("tool_choice"), dict):
            tc = result["tool_choice"]
            if tc.get("type") != "function":
                raise ValueError("invalid tool_choice")
            tc_name = tc.get("name")
            if not isinstance(tc_name, str) or not tc_name:
                raise ValueError("tool_choice.name is required")
            result["tool_choice"] = {"type": "function", "function": {"name": tc_name}}
    else:
        if body.get("max_tokens") is None:
            raise ValueError("max_tokens is required")
        result["max_tokens"] = body["max_tokens"]
        if "thinking" in body and body["thinking"] is not None:
            thinking_cfg = body["thinking"]
            if not isinstance(thinking_cfg, dict):
                raise ValueError("thinking must be an object")
            thinking_type = thinking_cfg.get("type")
            if thinking_type == "enabled":
                if "budget_tokens" in thinking_cfg and (
                    type(thinking_cfg["budget_tokens"]) is not int or thinking_cfg["budget_tokens"] < 1
                ):
                    raise ValueError("thinking.budget_tokens must be a positive integer")
                result["enable_thinking"] = True
            elif thinking_type == "disabled":
                result["enable_thinking"] = False
            else:
                raise ValueError("thinking.type must be enabled or disabled")
        messages = []
        if body.get("system") is not None and body.get("system") != "":
            messages.append({"role": "system", "content": text_content(body["system"], path="system")})
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("messages must be a non-empty array")
        for idx, message in enumerate(raw_messages):
            msg_path = f"messages[{idx}]"
            if not isinstance(message, dict):
                raise ValueError(f"{msg_path} must be an object")
            role = message.get("role")
            if not isinstance(role, str) or not role:
                raise ValueError(f"{msg_path}.role is required")
            if role not in {"user", "assistant"}:
                raise ValueError(f"{msg_path}.role: invalid Messages role")
            if "content" not in message or message["content"] is None:
                raise ValueError(f"{msg_path}.content is required")
            content = message["content"]
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                raise ValueError(f"{msg_path}.content must be text or an array")
            user_parts: list[dict[str, Any]] = []
            has_user_image = False
            texts: list[str] = []
            calls: list[dict[str, Any]] = []
            results: list[dict[str, Any]] = []
            for b_idx, block in enumerate(content):
                bpath = f"{msg_path}.content[{b_idx}]"
                if not isinstance(block, dict):
                    raise ValueError(f"{bpath} must be an object")
                btype = block.get("type")
                if not isinstance(btype, str) or not btype:
                    raise ValueError(f"{bpath}.type is required")
                if btype == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError(f"{bpath}.text is required")
                    if role == "user":
                        user_parts.append({"type": "text", "text": block["text"]})
                    else:
                        texts.append(block["text"])
                elif btype == "image" and role == "user":
                    converted = user_content([block], protocol="messages", path=f"{msg_path}.content")
                    assert isinstance(converted, list)
                    user_parts.extend(converted)
                    has_user_image = True
                elif btype == "tool_use" and role == "assistant":
                    if not isinstance(block.get("id"), str) or not block["id"]:
                        raise ValueError(f"{bpath}.id is required")
                    if not isinstance(block.get("name"), str) or not block["name"]:
                        raise ValueError(f"{bpath}.name is required")
                    if "input" not in block or not isinstance(block["input"], dict):
                        raise ValueError(f"{bpath}.input must be an object")
                    calls.append(
                        {
                            "id": block["id"],
                            "type": "function",
                            "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                        }
                    )
                elif btype == "tool_result" and role == "user":
                    if not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"]:
                        raise ValueError(f"{bpath}.tool_use_id is required")
                    tool_text = text_content(block.get("content", ""), path=f"{bpath}.content")
                    if "is_error" in block and type(block["is_error"]) is not bool:
                        raise ValueError("invalid tool result error flag")
                    results.append(
                        {
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": ("Tool error: " if block.get("is_error") else "") + tool_text,
                            **({"is_error": block["is_error"]} if "is_error" in block else {}),
                        }
                    )
                else:
                    raise ValueError("unsupported Messages content block")
            messages.extend(results)
            if role == "user":
                if has_user_image:
                    messages.append({"role": "user", "content": user_parts})
                elif user_parts:
                    messages.append({"role": "user", "content": "".join(p["text"] for p in user_parts)})
            elif texts or calls:
                entry: dict[str, Any] = {"role": role, "content": "".join(texts) or None}
                if calls:
                    entry["tool_calls"] = calls
                messages.append(entry)
        result["messages"] = messages
        if "stop_sequences" in body:
            result["stop"] = body["stop_sequences"]
        if "tools" in body:
            raw_tools = body["tools"]
            if not isinstance(raw_tools, list):
                raise ValueError("tools must be an array")
            norm_tools: list[dict[str, Any]] = []
            for idx, t in enumerate(raw_tools):
                if not isinstance(t, dict):
                    raise ValueError(f"tools[{idx}] must be an object")
                if not isinstance(t.get("name"), str) or not t["name"]:
                    raise ValueError(f"tools[{idx}].name is required")
                if "input_schema" not in t or not isinstance(t["input_schema"], dict):
                    raise ValueError(f"tools[{idx}].input_schema is required")
                norm_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t["input_schema"],
                        },
                    }
                )
            result["tools"] = norm_tools
        if "tool_choice" in body:
            choice = body["tool_choice"]
            if not isinstance(choice, dict):
                raise ValueError("tool_choice must be an object")
            if "disable_parallel_tool_use" in choice:
                if type(choice["disable_parallel_tool_use"]) is not bool:
                    raise ValueError("disable_parallel_tool_use must be boolean")
                if choice["disable_parallel_tool_use"]:
                    result["parallel_tool_calls"] = False
            kind = choice.get("type")
            if kind not in {"auto", "none", "any", "tool"}:
                raise ValueError("invalid tool_choice")
            if kind == "tool" and (not isinstance(choice.get("name"), str) or not choice["name"]):
                raise ValueError("tool_choice.name is required")
            result["tool_choice"] = (
                {"auto": "auto", "none": "none", "any": "required"}[kind]
                if kind != "tool"
                else {"type": "function", "function": {"name": choice["name"]}}
            )
    return validate_chat(result, default_max_tokens=None), sorted(ignored)


def normalize(body: dict[str, Any], protocol: str) -> dict[str, Any]:
    return normalize_with_ignored(body, protocol)[0]


def convert_response(
    chat: dict[str, Any],
    protocol: str,
    request_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if protocol == "chat":
        return chat
    message = chat["choices"][0]["message"]
    reason = chat["choices"][0]["finish_reason"]
    if reason not in {"stop", "tool_calls", "length", "content_filter"}:
        raise ValueError("completion has no supported terminal reason")
    calls = message.get("tool_calls", [])
    reported = chat.get("usage")
    reported = reported if isinstance(reported, dict) else {}
    usage = {"input_tokens": reported_token_count(reported.get("prompt_tokens")),
             "output_tokens": reported_token_count(reported.get("completion_tokens"))}
    details = reported.get("prompt_tokens_details") if isinstance(reported.get("prompt_tokens_details"), dict) else {}
    cached = reported_token_count(details.get("cached_tokens"))
    if cached is None:
        cached = reported_token_count(reported.get("cached_tokens"))
    out_details = (
        reported.get("completion_tokens_details")
        if isinstance(reported.get("completion_tokens_details"), dict)
        else reported.get("output_tokens_details")
        if isinstance(reported.get("output_tokens_details"), dict)
        else {}
    )
    reasoning = reported_token_count(out_details.get("reasoning_tokens"))
    if reasoning is None:
        reasoning = reported_token_count(reported.get("reasoning_tokens"))
    if protocol == "messages":
        if cached is not None and usage["input_tokens"] is not None:
            usage["cache_read_input_tokens"] = cached
        content = []
        if message.get("content"):
            content.append({"type": "text", "text": message["content"]})
        for call in calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": _parse_messages_tool_input(call["function"]["arguments"]),
                }
            )
        return {"id": "msg_" + chat["id"], "type": "message", "role": "assistant", "model": chat["model"], "content": content,
                "stop_reason": "max_tokens" if reason == "length" else "refusal" if reason == "content_filter" else "tool_use" if calls else "end_turn", "stop_sequence": None, "usage": usage}
    output = []
    if message.get("content"):
        output.append({"id": "msg_" + chat["id"], "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for call in calls:
        output.append({"id": "fc_" + call["id"], "type": "function_call", "status": "completed", "call_id": call["id"], **call["function"]})
    echoed_tools = copy.deepcopy(request_body["tools"]) if isinstance(request_body, dict) and "tools" in request_body else []
    echoed_tool_choice = copy.deepcopy(request_body["tool_choice"]) if isinstance(request_body, dict) and "tool_choice" in request_body else "auto"
    echoed_parallel = bool(request_body["parallel_tool_calls"]) if isinstance(request_body, dict) and "parallel_tool_calls" in request_body else True
    return {"id": "resp_" + chat["id"], "object": "response", "created_at": chat["created"], "model": chat["model"],
            "status": "incomplete" if reason in {"length", "content_filter"} else "completed", "error": None,
            "incomplete_details": {"reason": "max_output_tokens" if reason == "length" else "content_filter"} if reason in {"length", "content_filter"} else None,
            "output": output, "parallel_tool_calls": echoed_parallel, "tool_choice": echoed_tool_choice, "tools": echoed_tools,
            "usage": {**usage, "total_tokens": sum(usage.values()) if all(value is not None for value in usage.values()) else None,
                      "input_tokens_details": {"cached_tokens": (cached if cached is not None else 0) if usage["input_tokens"] is not None else None},
                      "output_tokens_details": {"reasoning_tokens": reasoning if usage["output_tokens"] is not None else None}}}


def sse(value: Any, event: str = "") -> str:
    return (f"event: {event}\n" if event else "") + "data: " + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + "\n\n"


class StreamFormat:
    def __init__(
        self,
        protocol: str,
        model: str,
        task_id: str,
        request_body: dict[str, Any] | None = None,
    ) -> None:
        self.protocol, self.model, self.task_id = protocol, model, task_id
        self.request_body = request_body
        self.created = int(time.time())
        self.sequence = 0
        self.blocks: dict[str, int] = {}
        self.calls: dict[int, dict[str, Any]] = {}
        self._tool_slots: list[int] = []
        self._id_to_block: dict[str, int] = {}
        self._upstream_index_to_block: dict[Any, int] = {}
        self.text = ""
        self.text_emitted = 0
        self.text_started = False
        self.chat_streamed = False
        self.chat_id = "chatcmpl-" + task_id

    def _open_tool_slot(self, call_id: str = "") -> int:
        slot_num = len(self._tool_slots)
        key = f"tool:{slot_num}"
        block_index = self.blocks[key] = len(self.blocks)
        self._tool_slots.append(block_index)
        self.calls[block_index] = {"id": call_id, "name": "", "arguments": ""}
        if call_id:
            self._id_to_block[call_id] = block_index
        return block_index

    def _resolve_delta_tool_block(self, call: dict[str, Any]) -> int:
        raw_id = call.get("id")
        call_id = raw_id if isinstance(raw_id, str) and raw_id != "" else ""
        has_index = "index" in call and call.get("index") is not None
        raw_index = call.get("index") if has_index else None

        if call_id:
            if call_id in self._id_to_block:
                block_index = self._id_to_block[call_id]
                if has_index:
                    self._upstream_index_to_block[raw_index] = block_index
                else:
                    self._upstream_index_to_block[None] = block_index
                return block_index
            candidate = (
                self._upstream_index_to_block.get(raw_index)
                if has_index
                else (self._tool_slots[0] if len(self._tool_slots) == 1 else self._upstream_index_to_block.get(0))
            )
            if candidate is not None and not self.calls[candidate]["id"]:
                self.calls[candidate]["id"] = call_id
                self._id_to_block[call_id] = candidate
                return candidate
            block_index = self._open_tool_slot(call_id)
            if has_index:
                self._upstream_index_to_block[raw_index] = block_index
            else:
                self._upstream_index_to_block[None] = block_index
                self._upstream_index_to_block[0] = block_index
            return block_index

        if has_index:
            if raw_index in self._upstream_index_to_block:
                return self._upstream_index_to_block[raw_index]
            block_index = self._open_tool_slot("")
            self._upstream_index_to_block[raw_index] = block_index
            return block_index

        if None in self._upstream_index_to_block:
            return self._upstream_index_to_block[None]
        if len(self._tool_slots) == 1:
            return self._tool_slots[0]
        if 0 in self._upstream_index_to_block:
            return self._upstream_index_to_block[0]
        block_index = self._open_tool_slot("")
        self._upstream_index_to_block[None] = block_index
        self._upstream_index_to_block[0] = block_index
        return block_index

    def event(self, kind: str, **values: Any) -> str:
        value = {"type": kind, **values}
        if self.protocol == "responses":
            value["sequence_number"] = self.sequence
            self.sequence += 1
        return sse(value, kind)

    def start(self) -> list[str]:
        if self.protocol == "chat":
            return []
        if self.protocol == "messages":
            return [self.event("message_start", message={"id": "msg_" + self.chat_id, "type": "message", "role": "assistant", "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}})]
        value = {"id": "resp_" + self.chat_id, "object": "response", "created_at": self.created, "model": self.model, "status": "in_progress", "output": [], "error": None}
        return [self.event("response.created", response=value), self.event("response.in_progress", response=value)]

    def delta(self, chunk: dict[str, Any]) -> list[str]:
        if self.protocol == "chat":
            self.chat_streamed = self.chat_streamed or bool(chunk.get("choices"))
            return [sse({**chunk, "id": self.chat_id, "object": "chat.completion.chunk", "created": self.created, "model": self.model})]
        output = []
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            if text:
                if "text" not in self.blocks:
                    index = self.blocks["text"] = len(self.blocks)
                    if self.protocol == "messages" and not self.calls:
                        output.append(self.event("content_block_start", index=index, content_block={"type": "text", "text": ""}))
                        self.text_started = True
                    elif self.protocol == "responses" and not self.calls:
                        output.append(self.event("response.output_item.added", output_index=index, item={"id": "msg_" + self.chat_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}))
                        output.append(self.event("response.content_part.added", item_id="msg_" + self.chat_id, output_index=index, content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
                        self.text_started = True
                index = self.blocks["text"]
                self.text += text
                if not self.calls:
                    self.text_emitted += len(text)
                    output.append(self.event("content_block_delta", index=index, delta={"type": "text_delta", "text": text}) if self.protocol == "messages" else
                                  self.event("response.output_text.delta", item_id="msg_" + self.chat_id, output_index=index, content_index=0, delta=text))
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                index = self._resolve_delta_tool_block(call)
                self.calls[index]["name"] += function.get("name") or ""
                args = function.get("arguments") or ""
                self.calls[index]["arguments"] += args
                # Neither target protocol can amend a tool name after its
                # start event. Wait for the authoritative completed item.
        return output

    def finish(self, chat: dict[str, Any]) -> list[str]:
        if self.protocol == "chat":
            output = []
            if not self.chat_streamed:
                message = {**chat["choices"][0]["message"]}
                if "tool_calls" in message:
                    message["tool_calls"] = [{"index": index, **call} for index, call in enumerate(message["tool_calls"])]
                output.extend(self.delta({"choices": [{"index": 0, "delta": message,
                    "finish_reason": chat["choices"][0]["finish_reason"]}]}))
            return [*output, sse("[DONE]")]
        final = convert_response(chat, self.protocol, request_body=self.request_body)
        output = []
        message = chat["choices"][0]["message"]
        text = message.get("content") or ""
        if not text.startswith(self.text):
            raise ValueError("final text disagrees with stream")
        self.text = text
        if text and "text" not in self.blocks:
            self.blocks["text"] = len(self.blocks)
        calls = message.get("tool_calls") or []
        unmatched_slots = list(self._tool_slots)
        for call in calls:
            cid = call["id"]
            if cid in self._id_to_block and self._id_to_block[cid] in unmatched_slots:
                index = self._id_to_block[cid]
                unmatched_slots.remove(index)
            else:
                no_id_slot = next((s for s in unmatched_slots if not self.calls[s]["id"]), None)
                if no_id_slot is not None:
                    index = no_id_slot
                    unmatched_slots.remove(no_id_slot)
                else:
                    slot_num = len(self._tool_slots)
                    index = self.blocks[f"tool:{slot_num}"] = len(self.blocks)
                    self._tool_slots.append(index)
            seen = self.calls.get(index)
            if seen:
                if ((seen["id"] and seen["id"] != call["id"])
                        or (seen["name"] and seen["name"] != call["function"]["name"])):
                    raise ValueError("final tool identity disagrees with stream")
                try:
                    arguments_match = json.loads(seen["arguments"]) == json.loads(call["function"]["arguments"])
                except ValueError:
                    arguments_match = seen["arguments"] == call["function"]["arguments"]
                if not arguments_match:
                    raise ValueError("final tool arguments disagree with stream")
            self.calls[index] = {"id": call["id"], **call["function"]}
        if unmatched_slots:
            raise ValueError("streamed tool missing from final response")
        for key, index in self.blocks.items():
            if key == "text":
                if not self.text_started:
                    if self.protocol == "messages":
                        output.append(self.event("content_block_start", index=index, content_block={"type": "text", "text": ""}))
                    else:
                        output.append(self.event("response.output_item.added", output_index=index, item={"id": "msg_" + self.chat_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}))
                        output.append(self.event("response.content_part.added", item_id="msg_" + self.chat_id, output_index=index, content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
                remaining = self.text[self.text_emitted:]
                if remaining:
                    output.append(self.event("content_block_delta", index=index, delta={"type": "text_delta", "text": remaining}) if self.protocol == "messages" else
                                  self.event("response.output_text.delta", item_id="msg_" + self.chat_id, output_index=index, content_index=0, delta=remaining))
            else:
                call = self.calls[index]
                if self.protocol == "messages":
                    output.append(self.event("content_block_start", index=index, content_block={"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}}))
                    output.append(self.event("content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": call["arguments"]}))
                else:
                    output.append(self.event("response.output_item.added", output_index=index, item={"id": "fc_" + call["id"], "type": "function_call", "status": "in_progress", "call_id": call["id"], "name": call["name"], "arguments": ""}))
                    output.append(self.event("response.function_call_arguments.delta", item_id="fc_" + call["id"], output_index=index, delta=call["arguments"]))
            if self.protocol == "messages":
                output.append(self.event("content_block_stop", index=index))
            elif key == "text":
                part = {"type": "output_text", "text": self.text, "annotations": []}
                output.append(self.event("response.output_text.done", item_id="msg_" + self.chat_id, output_index=index, content_index=0, text=self.text))
                output.append(self.event("response.content_part.done", item_id="msg_" + self.chat_id, output_index=index, content_index=0, part=part))
                output.append(self.event("response.output_item.done", output_index=index, item={"id": "msg_" + self.chat_id, "type": "message", "role": "assistant", "status": "completed", "content": [part]}))
            else:
                call = self.calls[index]
                output.append(self.event("response.function_call_arguments.done", item_id="fc_" + call["id"], output_index=index, arguments=call["arguments"], name=call["name"]))
                output.append(self.event("response.output_item.done", output_index=index, item={"id": "fc_" + call["id"], "type": "function_call", "status": "completed", "call_id": call["id"], "name": call["name"], "arguments": call["arguments"]}))
        if self.protocol == "messages":
            output.append(self.event("message_delta", delta={"stop_reason": final["stop_reason"], "stop_sequence": None}, usage=final["usage"]))
            output.append(self.event("message_stop"))
        else:
            # Preserve output indexes established by added/delta events, even
            # when an upstream emits a tool before its explanatory text.
            by_id = {item["id"]: item for item in final["output"]}
            final["output"] = [by_id["msg_" + self.chat_id if key == "text" else "fc_" + self.calls[index]["id"]]
                               for key, index in self.blocks.items()]
            output.append(self.event("response." + final["status"], response=final))
        return output
