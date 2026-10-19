"""Device-owned AI source configurations. Secrets never leave the local control API."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from ..atomic_io import atomic_write_bytes
from ..file_transactions import file_transaction
from . import model_settings
from .adapters import AdapterError, OpenAICompatibleAdapter, validate_local_url

PROTOCOLS = {"openai_chat", "openai_responses", "anthropic_messages"}


def source_protocol(record):
    """Missing protocol always means the original Chat Completions transport."""
    return record.get("protocol", "openai_chat")


def _same_configuration(record, old, fields):
    return all((source_protocol(record) == source_protocol(old)) if key == "protocol"
               else record.get(key) == old.get(key) for key in fields)


class SourceAdapter(OpenAICompatibleAdapter):
    """Explicit endpoint roots, including vendor roots that do not end in /v1."""
    def health(self):
        # Catalog rendering must not make paid requests or repeatedly probe a vendor.
        return {"ok": bool(self.model), "model": self.model}

    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        result = self.chat({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
                           task_id=task_id, timeout_s=timeout_s)
        if not result.get("text"):
            raise AdapterError("模型未返回正文，请提高输出上限后重试")
        return result


class SourceStore:
    def __init__(self, home: Path, private_key: bytes, *, migrate: bool = True):
        self.path = home / "llm" / "sources.enc"
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"ryn-ai-sources-v1" + private_key).digest()))
        self.lock_path = home / "llm" / ".sharing.lock"
        self._active_models = {}
        self._model_metadata_cache = {}
        with self.lock:
            records = self.read()
            changed = False
            for record in records.values() if migrate else ():
                if set(record.get("model_settings", {})) != set(record["models"]):
                    model_settings.reconcile(record, copy.deepcopy(record))
                    changed = True
            if changed:
                self._write(records)

    @property
    def lock(self):
        # Shared with AIAccessStore: source configuration, grant imports and
        # subsequent owner grant edits have one cross-process lock order.
        return file_transaction(self.lock_path)

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {}
            return json.loads(self.cipher.decrypt(self.path.read_bytes()))

    def _write(self, records):
        atomic_write_bytes(self.path, self.cipher.encrypt(json.dumps(records).encode()))

    @staticmethod
    def public(record):
        return {**{k: v for k, v in record.items() if k != "api_key" and not k.startswith("_")},
                "protocol": source_protocol(record),
                "shared_models": SourceStore.shared_models(record),
                "model_settings": {m: model_settings.public(record, m) for m in record["models"]},
                "tool_support": record.get("tool_support", {}), "has_key": bool(record.get("api_key"))}

    @staticmethod
    def shared_models(record):
        """Legacy whole-source grants apply only to the models already present."""
        return [m for m in record.get("models", []) if m in record.get(
            "shared_models", record.get("models", []) if record.get("sharing") else [])]

    def prepare(self, body, source_id=""):
        old = self.read().get(source_id, {})
        if not isinstance(body, dict):
            raise ValueError("配置格式不正确")
        kind = body.get("kind", old.get("kind", "api"))
        if kind not in {"api", "local"}:
            raise ValueError("不支持的接入方式")
        protocol = body.get("protocol", source_protocol(old))
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            raise ValueError("不支持的 API 协议")
        name = str(body.get("name", old.get("name", ""))).strip()
        if not name or len(name) > 80:
            raise ValueError("请填写来源名称（最多 80 字）")
        base = validate_local_url(str(body.get("base_url", old.get("base_url", ""))), allow_non_loopback=kind == "api")
        if kind == "api" and urlparse(base).scheme != "https":
            raise ValueError("厂商 API 地址必须使用 HTTPS；本机服务请选择本地模型")
        models = body.get("models", old.get("models", []))
        if not isinstance(models, list) or len(models) > 100 or any(not isinstance(m, str) or not m.strip() or len(m) > 160 for m in models):
            raise ValueError("模型 ID 格式不正确，最多 100 个")
        models = list(dict.fromkeys(m.strip() for m in models))
        key = body.get("api_key")
        if key is None or key == "":
            key = old.get("api_key", "") if base == old.get("base_url") else ""
        if body.get("clear_key") is True:
            key = ""
        if not isinstance(key, str) or len(key) > 8192 or any(c in key for c in "\r\n"):
            raise ValueError("API Key 格式不正确")
        mode = body.get("context_window_mode", "manual" if "context_window" in body else
                        old.get("context_window_mode", "manual" if "context_window" in old else "auto"))
        if mode not in {"auto", "manual"}:
            raise ValueError("model_context_window_mode_invalid")
        def integer(value):
            # Preserve legacy integer strings, but never truncate a float or bool.
            if type(value) is int:
                return value
            if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
                return int(value)
            raise ValueError("上下文或输出上限不正确")
        context = integer(body.get("context_window", old.get("context_window", model_settings.FALLBACK_CONTEXT_WINDOW)))
        output = integer(body.get("max_output_tokens", old.get("max_output_tokens", 4096)))
        if not 256 <= context <= 2000000 or not 1 <= output <= 256000 or (mode == "manual" and output > context):
            raise ValueError("上下文或输出上限不正确")
        defaults = body.get("request_defaults", old.get("request_defaults", {}))
        if not isinstance(defaults, dict) or set(defaults) - {"thinking", "reasoning_effort", "temperature", "top_p"}:
            raise ValueError("请求参数仅支持 thinking、reasoning_effort、temperature、top_p")
        if len(json.dumps(defaults)) > 4096:
            raise ValueError("请求参数过长")
        shared = body.get("shared_models", [m for m in self.shared_models(old) if m in models])
        if "shared_models" not in body and "sharing" in body:
            if type(body["sharing"]) is not bool:
                raise ValueError("sharing must be boolean")
            shared = models if body["sharing"] else []
        if not isinstance(shared, list) or any(not isinstance(m, str) or m not in models for m in shared):
            raise ValueError("shared_models must contain configured model IDs")
        shared = list(dict.fromkeys(shared))
        record = {"id": source_id or "src-" + uuid.uuid4().hex[:16], "kind": kind, "protocol": protocol,
                "name": name, "provider": str(body.get("provider", old.get("provider", "custom")))[:80],
                "base_url": base, "api_key": key.strip(), "models": models,
                "context_window": context, "context_window_mode": mode, "max_output_tokens": output, "request_defaults": defaults,
                "sharing": bool(shared), "shared_models": shared,
                "enabled": bool(body.get("enabled", old.get("enabled", True))),
                "status": "needs_key" if kind == "api" and not key else "untested"}
        record["_context_window_mode_explicit"] = "context_window_mode" in body
        same_endpoint = _same_configuration(record, old, ("kind", "provider", "base_url", "api_key", "protocol"))
        metadata = copy.deepcopy(old.get("model_metadata", {})) if same_endpoint else {}
        metadata.update(self._model_metadata_cache.get(self._metadata_key(record), {}))
        record["model_metadata"] = {model: metadata[model] for model in models if model in metadata}
        # A text test or a user-supplied flag cannot assert client-tool support.
        fields = ("kind", "provider", "base_url", "api_key", "protocol", "models", "context_window", "max_output_tokens", "request_defaults")
        record["tool_support"] = {m: v for m, v in old.get("tool_support", {}).items() if m in models}
        record["model_settings"] = copy.deepcopy(old.get("model_settings", {}))
        # Preserve unaffected sibling evidence when merely adding/removing a model.
        if _same_configuration(record, old, (k for k in fields if k != "models")):
            record["tool_support"] = {m: v for m, v in old.get("tool_support", {}).items() if m in models}
        model_settings.reconcile(record, old)
        if not models and output > (context if mode == "manual" else model_settings.FALLBACK_CONTEXT_WINDOW):
            raise ValueError("model_output_exceeds_context")
        return record

    def save(self, record):
        caller_record = record
        with self.lock:
            records = self.read()
            old = records.get(record["id"], {})
            # Private grants and receipts are server-owned, never accepted from
            # request bodies or overwritten by a previously prepared UI draft.
            explicit_mode = record.get("_context_window_mode_explicit", False)
            record = {k: copy.deepcopy(v) for k, v in record.items() if not k.startswith("_")}
            record.update({k: copy.deepcopy(v) for k, v in old.items() if k.startswith("_")})
            if (not explicit_mode and old.get("context_window_mode") == record.get("context_window_mode") == "auto"
                    and record.get("context_window") != old.get("context_window")):
                # Trusted internal callers historically edited the source's
                # numeric field directly. A changed value is explicit intent;
                # metadata refreshes only update model_metadata, never this field.
                record["context_window_mode"] = "manual"
            model_settings.reconcile(record, old)
            destination_fields = ("kind", "provider", "base_url", "api_key", "protocol", "models", "context_window", "context_window_mode", "model_metadata",
                                  "max_output_tokens", "request_defaults", "enabled")
            unchanged = _same_configuration(record, old, destination_fields)
            # This opaque generation is authoritative, never accepted from the
            # request body. Restoring an older endpoint/key does not resurrect an
            # earlier task's egress grant (including an A -> B -> A edit).
            record["destination_revision"] = old.get("destination_revision", uuid.uuid4().hex) if unchanged else uuid.uuid4().hex
            # Whole-source edits/probe results have a separate CAS generation;
            # destination_revision intentionally ignores sharing and labels.
            record["source_revision"] = uuid.uuid4().hex
            records[record["id"]] = record
            self._write(records)
            # Existing internal callers retain this record for later CAS/probe
            # updates; keep their authoritative generations current as before.
            caller_record.clear()
            caller_record.update(copy.deepcopy(record))
        return self.public(record)

    def delete(self, source_id):
        with self.lock:
            records = self.read()
            records.pop(source_id, None)
            self._write(records)

    @staticmethod
    def adapter(record, model=""):
        if record["kind"] == "api" and not record.get("api_key"):
            raise ValueError("请先填写 API Key")
        protocol = source_protocol(record)
        if protocol not in PROTOCOLS:
            raise ValueError("不支持的 API 协议")
        adapter, protocol_kwargs = SourceAdapter, {}
        if protocol != "openai_chat":
            from .protocol_adapter import NativeProtocolAdapter
            adapter, protocol_kwargs = NativeProtocolAdapter, {"protocol": protocol}
        return adapter(base_url=record["base_url"], api_prefix="", api_key=record.get("api_key", ""),
                             model=model or next(iter(record["models"]), ""),
                             allow_non_loopback=record["kind"] == "api", timeout_s=180,
                             request_defaults=model_settings.effective(record, model or next(iter(record["models"]), ""))["request_defaults"],
                             **protocol_kwargs)

    def configure_model(self, source_id, body):
        if not isinstance(body, dict) or set(body) != {"model_id", "expected_rule_revision", "configuration"}:
            raise ValueError("model_configuration_request_invalid")
        with self.lock:
            record = self.read().get(source_id)
            if not record or body["model_id"] not in record["models"]:
                raise ValueError("configured_model_required")
            model = body["model_id"]
            row = record.get("model_settings", {}).get(model, {})
            if row.get("rule_revision", "") != body["expected_rule_revision"]:
                raise ValueError("model_rule_revision_conflict")
            patch = body["configuration"]
            if not isinstance(patch, dict) or set(patch) - model_settings.FIELDS:
                raise ValueError("model_configuration_invalid")
            overrides = copy.deepcopy(row.get("overrides", {}))
            for key, value in patch.items():
                if value is None and key != "requests_per_day":
                    overrides.pop(key, None)
                else:
                    overrides[key] = value
            model_settings.validate({**model_settings.defaults(record, model), **overrides})
            record.setdefault("model_settings", {})[model] = {**row, "overrides": overrides}
            return self.save(record)

    @staticmethod
    def model_service_id(record, model):
        return record["id"] + "-" + hashlib.sha256(model.encode()).hexdigest()[:12]

    def probe(self, record, *, models_only=False):
        adapter = self.adapter(record)
        if models_only:
            listing = adapter.models()
            # Some listings expose only IDs. Omission is not a declaration that
            # a previously authenticated capacity changed; retain that cache.
            metadata = copy.deepcopy(record.get("model_metadata", {}))
            metadata.update(copy.deepcopy(self._model_metadata_cache.get(self._metadata_key(record), {})))
            for item in listing:
                identifier = item.get("id") or item.get("name")
                if not isinstance(identifier, str) or not identifier or len(identifier) > 160:
                    continue
                values = [item.get(key) for key in ("context_window", "context_length", "max_model_len")]
                window = next((value for value in values if type(value) is int and 256 <= value <= 2000000), None)
                if window is not None:
                    metadata[identifier] = {"context_window": window}
            with self.lock:
                key = self._metadata_key(record)
                if len(self._model_metadata_cache) >= 64 and key not in self._model_metadata_cache:
                    self._model_metadata_cache.pop(next(iter(self._model_metadata_cache)))
                self._model_metadata_cache[key] = copy.deepcopy(metadata)
            # Drafts may discover IDs before they are selected. Only server-read
            # metadata is cached; a browser-supplied model_metadata is ignored.
            record["model_metadata"] = {model: metadata[model] for model in record["models"] if model in metadata}
            return {"models": [str(item.get("id") or item.get("name")) for item in listing if item.get("id") or item.get("name")],
                    "model_metadata": metadata}
        if not record["models"]:
            raise ValueError("请先添加至少一个模型 ID")
        result = adapter.chat({"messages": [{"role": "user", "content": "Reply briefly: OK"}],
                               "max_tokens": min(4096, model_settings.effective(record, record["models"][0])["max_output_tokens"])}, task_id="source-test-" + uuid.uuid4().hex, timeout_s=90)
        if not result.get("text"):
            raise AdapterError("模型未返回正文，请检查输出上限或推理参数")
        return {"ok": True, "model": adapter.model}

    @staticmethod
    def _metadata_key(record):
        # Internal cache identity includes the authenticated endpoint snapshot.
        # Never return or log this secret-bearing configuration fingerprint.
        identity = {key: record.get(key) for key in ("kind", "provider", "base_url", "api_key")}
        identity["protocol"] = source_protocol(record)
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def probe_model_tools(self, record, model):
        """Explicit owner-triggered two-turn test, never called by discovery."""
        if model not in record["models"]:
            raise ValueError("unknown configured model")
        self.probe_adapter_tools(self.adapter(record, model), model_settings.effective(record, model)["max_output_tokens"])
        return {"model_id": model, "tool_support": "supported"}

    @staticmethod
    def probe_adapter_tools(adapter, max_output_tokens):
        from .chat import validate_tool_arguments

        schema = {"type": "object", "properties": {"value": {"const": "ryn-tool-test"}},
                  "required": ["value"], "additionalProperties": False}
        spec = {"type": "function", "function": {"name": "ryn_capability_probe", "description": "Return the requested test value.", "parameters": schema}}
        messages = [{"role": "user", "content": "Call ryn_capability_probe with value ryn-tool-test."}]
        first = adapter.chat({"messages": messages, "tools": [spec], "tool_choice": "required",
                              "max_tokens": min(4096, max_output_tokens)},
                             task_id="source-tools-" + uuid.uuid4().hex, timeout_s=60)
        message = first.get("message") or {}
        calls = message.get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "ryn_capability_probe":
            raise ValueError("model did not return a structured client tool call")
        validate_tool_arguments(schema, json.loads(calls[0]["function"]["arguments"]))
        messages += [message, {"role": "tool", "tool_call_id": calls[0]["id"], "content": "ryn-tool-test-ok"},
                     {"role": "user", "content": "Return the tool result as text; do not call another tool."}]
        final = adapter.chat({"messages": messages, "tools": [spec], "tool_choice": "none",
                              "max_tokens": min(4096, max_output_tokens)},
                             task_id="source-tools-" + uuid.uuid4().hex, timeout_s=60)
        if not final.get("text") or final.get("message", {}).get("tool_calls"):
            raise ValueError("model did not complete the tool-result continuation")
        return {"tool_support": "supported"}
