"""Stateless text and client-executed tool subsets of three inference APIs."""
from __future__ import annotations

import json
import time
from typing import Any

from .chat import reported_token_count, validate_chat


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(x, dict) and x.get("type") in {"text", "input_text", "output_text"} and isinstance(x.get("text"), str) for x in value):
        return "".join(x["text"] for x in value)
    raise ValueError("only text content is supported")


def normalize(body: dict[str, Any], protocol: str) -> dict[str, Any]:
    common = {"model", "stream", "temperature", "top_p", "tools", "tool_choice"}
    allowed = common | ({"messages", "max_tokens", "max_completion_tokens", "stop", "stream_options", "n", "enable_thinking", "chat_template_kwargs", "reasoning_effort"} if protocol == "chat" else
                        {"input", "instructions", "max_output_tokens", "store", "previous_response_id"} if protocol == "responses" else
                        {"messages", "system", "max_tokens", "stop_sequences"})
    if set(body) - allowed:
        raise ValueError("unsupported parameters: " + ", ".join(sorted(set(body) - allowed)))
    result = {key: body[key] for key in ("stream", "temperature", "top_p", "tool_choice") if key in body}
    if protocol == "chat":
        if body.get("n", 1) != 1:
            raise ValueError("only n=1 is supported")
        if set(body.get("stream_options") or {}) - {"include_usage"}:
            raise ValueError("unsupported stream_options")
        if "max_tokens" in body and "max_completion_tokens" in body:
            raise ValueError("use only one maximum token parameter")
        result.update({key: body[key] for key in ("messages", "tools", "stop", "enable_thinking", "chat_template_kwargs", "reasoning_effort") if key in body})
        result["max_tokens"] = body.get("max_completion_tokens", body.get("max_tokens", 512))
    elif protocol == "responses":
        if body.get("store") or body.get("previous_response_id"):
            raise ValueError("Responses is stateless: use store=false and send conversation history in input")
        result["max_tokens"] = body.get("max_output_tokens", 512)
        messages = []
        if body.get("instructions"):
            messages.append({"role": "system", "content": text_content(body["instructions"])})
        items = body.get("input", [])
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        if not isinstance(items, list):
            raise ValueError("input must be text or an array")
        for item in items:
            kind = item.get("type", "message")
            if kind == "message":
                messages.append({"role": item["role"], "content": text_content(item["content"])})
            elif kind == "function_call":
                call = {"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}}
                if messages and messages[-1].get("tool_calls"):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif kind == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": text_content(item["output"])})
            else:
                raise ValueError("unsupported Responses input item: " + str(kind))
        result["messages"] = messages
        if "tools" in body:
            result["tools"] = []
            for tool in body["tools"]:
                if tool.get("type") != "function":
                    raise ValueError("only client-executed function tools are supported")
                result["tools"].append({"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}})
        if isinstance(result.get("tool_choice"), dict):
            result["tool_choice"] = {"type": "function", "function": {"name": result["tool_choice"].get("name")}}
    else:
        result["max_tokens"] = body["max_tokens"]
        messages = []
        if body.get("system"):
            messages.append({"role": "system", "content": text_content(body["system"])})
        for message in body.get("messages", []):
            if message.get("role") not in {"user", "assistant"}:
                raise ValueError("invalid Messages role")
            content = message.get("content")
            if isinstance(content, str):
                messages.append({"role": message["role"], "content": content})
                continue
            texts, calls, results = [], [], []
            for block in content:
                if block["type"] == "text":
                    texts.append(block["text"])
                elif block["type"] == "tool_use" and message["role"] == "assistant":
                    calls.append({"id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": json.dumps(block["input"])}})
                elif block["type"] == "tool_result" and message["role"] == "user":
                    tool_text = text_content(block.get("content", ""))
                    if "is_error" in block and type(block["is_error"]) is not bool:
                        raise ValueError("invalid tool result error flag")
                    results.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": ("Tool error: " if block.get("is_error") else "") + tool_text,
                                    **({"is_error": block["is_error"]} if "is_error" in block else {})})
                else:
                    raise ValueError("unsupported Messages content block")
            messages.extend(results)
            if texts or calls:
                entry = {"role": message["role"], "content": "".join(texts) or None}
                if calls:
                    entry["tool_calls"] = calls
                messages.append(entry)
        result["messages"] = messages
        if "stop_sequences" in body:
            result["stop"] = body["stop_sequences"]
        if "tools" in body:
            result["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}} for t in body["tools"]]
        if "tool_choice" in body:
            choice = body["tool_choice"]
            if choice.get("disable_parallel_tool_use"):
                raise ValueError("disable_parallel_tool_use is not supported")
            kind = choice.get("type")
            if kind not in {"auto", "none", "any", "tool"}:
                raise ValueError("invalid tool_choice")
            result["tool_choice"] = {"auto": "auto", "none": "none", "any": "required"}.get(kind) if kind != "tool" else {"type": "function", "function": {"name": choice["name"]}}
    return validate_chat(result)


def convert_response(chat: dict[str, Any], protocol: str) -> dict[str, Any]:
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
    if protocol == "messages":
        if cached is not None and usage["input_tokens"] is not None:
            usage["cache_read_input_tokens"] = cached
        content = []
        if message.get("content"):
            content.append({"type": "text", "text": message["content"]})
        for call in calls:
            content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
        return {"id": "msg_" + chat["id"], "type": "message", "role": "assistant", "model": chat["model"], "content": content,
                "stop_reason": "max_tokens" if reason == "length" else "refusal" if reason == "content_filter" else "tool_use" if calls else "end_turn", "stop_sequence": None, "usage": usage}
    output = []
    if message.get("content"):
        output.append({"id": "msg_" + chat["id"], "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for call in calls:
        output.append({"id": "fc_" + call["id"], "type": "function_call", "status": "completed", "call_id": call["id"], **call["function"]})
    return {"id": "resp_" + chat["id"], "object": "response", "created_at": chat["created"], "model": chat["model"],
            "status": "incomplete" if reason in {"length", "content_filter"} else "completed", "error": None,
            "incomplete_details": {"reason": "max_output_tokens" if reason == "length" else "content_filter"} if reason in {"length", "content_filter"} else None,
            "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {**usage, "total_tokens": sum(usage.values()) if all(value is not None for value in usage.values()) else None,
                      "input_tokens_details": {"cached_tokens": (cached if cached is not None else 0) if usage["input_tokens"] is not None else None},
                      "output_tokens_details": {"reasoning_tokens": 0 if usage["output_tokens"] is not None else None}}}


def sse(value: Any, event: str = "") -> str:
    return (f"event: {event}\n" if event else "") + "data: " + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + "\n\n"


class StreamFormat:
    def __init__(self, protocol: str, model: str, task_id: str) -> None:
        self.protocol, self.model, self.task_id = protocol, model, task_id
        self.created = int(time.time())
        self.sequence = 0
        self.blocks: dict[str, int] = {}
        self.calls: dict[int, dict[str, Any]] = {}
        self.text = ""
        self.text_emitted = 0
        self.text_started = False
        self.chat_streamed = False
        self.chat_id = "chatcmpl-" + task_id

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
            return [self.event("message_start", message={"id": "msg_" + self.chat_id, "type": "message", "role": "assistant", "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": None, "output_tokens": None}})]
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
                key = "tool:" + str(call["index"])
                function = call.get("function", {})
                if key not in self.blocks:
                    index = self.blocks[key] = len(self.blocks)
                    self.calls[index] = {"id": "", "name": "", "arguments": ""}
                index = self.blocks[key]
                if call.get("id"):
                    if self.calls[index]["id"] and self.calls[index]["id"] != call["id"]:
                        raise ValueError("tool identity changed")
                    self.calls[index]["id"] = call["id"]
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
        final = convert_response(chat, self.protocol)
        output = []
        message = chat["choices"][0]["message"]
        text = message.get("content") or ""
        if not text.startswith(self.text):
            raise ValueError("final text disagrees with stream")
        self.text = text
        if text and "text" not in self.blocks:
            self.blocks["text"] = len(self.blocks)
        calls = message.get("tool_calls") or []
        for ordinal, call in enumerate(calls):
            key = "tool:" + str(ordinal)
            if key not in self.blocks:
                self.blocks[key] = len(self.blocks)
            index = self.blocks[key]
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
        if sum(key.startswith("tool:") for key in self.blocks) != len(calls):
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
