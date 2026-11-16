"""Device-owned AI source configurations. Secrets never leave the local control API."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from ..atomic_io import atomic_write_bytes
from ..file_transactions import file_transaction
from . import model_settings
from .adapters import AdapterError, OpenAICompatibleAdapter, validate_local_url
from .model_usage import ModelUsageStore

# A 16x16 red square for the owner-triggered image test.
IMAGE_PROBE_DATA_URL = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGO8IyLCQApgIkk1w6gG4gATkergYFQDMYDkUAIATz0BJPq/6scAAAAASUVORK5CYII='

PROTOCOLS = {"openai_chat", "openai_responses", "anthropic_messages"}

# Documentation for local clients, including task-authored programs. Runtime
# validation remains in prepare(), shared by the UI and owner HTTP endpoints.
SOURCE_HTTP_SCHEMA = {
    "type": "object", "required": ["name", "base_url"], "additionalProperties": True,
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "kind": {"type": "string", "enum": ["api", "local"], "default": "api"},
        "provider": {"type": "string", "maxLength": 80},
        "protocol": {"type": "string", "enum": sorted(PROTOCOLS), "default": "openai_chat"},
        "base_url": {"type": "string", "format": "uri", "description": "Provider API root; cloud API sources require HTTPS."},
        "models": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 160}},
        "api_key": {"type": "string", "maxLength": 8192, "writeOnly": True,
                    "description": "Supply from local owner input, never model/chat text. Public responses expose has_key only."},
        "enabled": {"type": "boolean", "default": True},
        "sharing": {"type": "boolean", "default": False},
        "shared_models": {"type": "array", "items": {"type": "string"}, "default": []},
        "context_window_mode": {"type": "string", "enum": ["auto", "manual"]},
        "context_window": {"type": "integer", "minimum": 256, "maximum": 2000000},
        "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": 393216},
        "stream_idle_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 120},
        "max_request_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 1800},
        "prompt_cache": {"type": "string", "enum": ["auto", "off"]},
        "request_defaults": {"type": "object"},
    },
}


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
        self.usage_store = ModelUsageStore(home / "llm" / "model-usage.json")
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

    def public(self, record=None):
        if record is None and isinstance(self, dict):
            record, usage_store = self, None
        else:
            usage_store = getattr(self, "usage_store", None)
        snapshot = usage_store.read_cached() if usage_store is not None else None
        settings_map = {}
        usage_map = {}
        for m in record["models"]:
            pub = model_settings.public(record, m)
            if usage_store is not None:
                sid = SourceStore.model_service_id(record, m)
                usage = usage_store.public_usage("local/" + sid, policy=pub["effective"], snapshot=snapshot)
                pub["usage"] = usage
                usage_map[m] = usage
            settings_map[m] = pub
        return {**{k: v for k, v in record.items() if k != "api_key" and not k.startswith("_")},
                "protocol": source_protocol(record),
                "shared_models": SourceStore.shared_models(record),
                "model_settings": settings_map,
                "model_usage": usage_map,
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
        provider_name = str(body.get("provider", old.get("provider", "custom")))[:80]
        default_context = 1048576 if provider_name == "deepseek" else model_settings.FALLBACK_CONTEXT_WINDOW
        default_output = 393216 if provider_name == "deepseek" else 4096
        context = integer(body.get("context_window", old.get("context_window", default_context)))
        output = integer(body.get("max_output_tokens", old.get("max_output_tokens", default_output)))
        if not 256 <= context <= 2000000 or not 1 <= output <= 393216 or (mode == "manual" and output > context):
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
        max_output_explicit = True if "max_output_tokens" in body else bool(old["max_output_tokens_explicit"]) if "max_output_tokens_explicit" in old else False
        record = {"id": source_id or "src-" + uuid.uuid4().hex[:16], "kind": kind, "protocol": protocol,
                "name": name, "provider": str(body.get("provider", old.get("provider", "custom")))[:80],
                "base_url": base, "api_key": key.strip(), "models": models,
                "context_window": context, "context_window_mode": mode, "max_output_tokens": output,
                "max_output_tokens_explicit": max_output_explicit, "request_defaults": defaults,
                "sharing": bool(shared), "shared_models": shared,
                "enabled": bool(body.get("enabled", old.get("enabled", True))),
                "status": "needs_key" if kind == "api" and not key else "untested"}
        for timeout_key in ("stream_idle_seconds", "max_request_seconds"):
            if timeout_key in body:
                if body[timeout_key] is not None:
                    timeout_val = integer(body[timeout_key])
                    if not 1 <= timeout_val <= 86400:
                        raise ValueError("model_" + timeout_key + "_invalid")
                    record[timeout_key] = timeout_val
            elif timeout_key in old:
                record[timeout_key] = old[timeout_key]
        if "prompt_cache" in body:
            if body["prompt_cache"] is not None:
                if body["prompt_cache"] not in model_settings.PROMPT_CACHE_VALUES:
                    raise ValueError("model_prompt_cache_invalid")
                record["prompt_cache"] = body["prompt_cache"]
        elif "prompt_cache" in old:
            record["prompt_cache"] = old["prompt_cache"]
        record["_context_window_mode_explicit"] = "context_window_mode" in body
        same_endpoint = _same_configuration(record, old, ("kind", "provider", "base_url", "api_key", "protocol"))
        metadata = copy.deepcopy(old.get("model_metadata", {})) if same_endpoint else {}
        metadata.update(self._model_metadata_cache.get(self._metadata_key(record), {}))
        record["model_metadata"] = {model: metadata[model] for model in models if model in metadata}
        # A text test or a user-supplied flag cannot assert client-tool support.
        record["tool_support"] = {m: v for m, v in old.get("tool_support", {}).items() if m in models}
        record["model_settings"] = copy.deepcopy(old.get("model_settings", {}))
        model_settings.reconcile(record, old)
        if not models and output > (context if mode == "manual" else default_context):
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

    def _record_model_capability(self, source_id: str, model: str, key: str, status: str) -> None:
        if key not in {"prompt_cache_key", "cache_control"} or status not in {"supported", "unsupported"}:
            return
        with self.lock:
            records = self.read()
            rec = records.get(source_id)
            if not rec or model not in rec.get("models", []):
                return
            row = rec.setdefault("model_settings", {}).setdefault(model, {})
            ev = row.setdefault("evidence", {})
            ev[key] = {"status": status, "checked_at": int(time.time())}
            self._write(records)

    def adapter(self_or_record, record_or_model="", model=""):
        """Build an upstream HTTP adapter for a configured source/model.

        ``timeout_s`` on the returned adapter is the per-read stream idle
        timeout (default 120 s from ``stream_idle_seconds``), not a wall-clock
        cap on the whole response; total request duration is bounded by the
        caller's ``deadline`` budget.
        """
        if isinstance(self_or_record, SourceStore):
            store_self: SourceStore | None = self_or_record
            record = record_or_model
            chosen_arg = model
        else:
            store_self = None
            record = self_or_record
            chosen_arg = model or record_or_model
        if record["kind"] == "api" and not record.get("api_key"):
            raise ValueError("请先填写 API Key")
        protocol = source_protocol(record)
        if protocol not in PROTOCOLS:
            raise ValueError("不支持的 API 协议")
        selected_model = chosen_arg or next(iter(record["models"]), "")
        policy = model_settings.effective(record, selected_model)
        evidence = (record.get("model_settings", {}).get(selected_model, {}) or {}).get("evidence") or {}
        pck_status = (evidence.get("prompt_cache_key") or {}).get("status")
        prompt_cache_key_supported = True if pck_status == "supported" else False if pck_status == "unsupported" else None
        cc_status = (evidence.get("cache_control") or {}).get("status")
        cache_control_supported = False if cc_status == "unsupported" else True if cc_status == "supported" else None
        ct_status = (evidence.get("custom_tools") or {}).get("status")
        custom_tools_supported = False if ct_status == "unsupported" else True if ct_status == "supported" else None
        source_id = record.get("id", "")
        on_cap = (
            (lambda k, st, _sid=source_id, _m=selected_model: store_self._record_model_capability(_sid, _m, k, st))
            if store_self is not None and source_id and selected_model
            else None
        )
        adapter_cls, protocol_kwargs = SourceAdapter, {}
        if protocol != "openai_chat":
            from .protocol_adapter import NativeProtocolAdapter
            adapter_cls, protocol_kwargs = NativeProtocolAdapter, {
                "protocol": protocol,
                "cache_control_supported": cache_control_supported,
                "custom_tools": custom_tools_supported,
                "tool_support": record.get("tool_support", {}).get(selected_model, "unknown"),
            }
        return adapter_cls(
            base_url=record["base_url"],
            api_prefix="",
            api_key=record.get("api_key", ""),
            model=selected_model,
            allow_non_loopback=record["kind"] == "api",
            timeout_s=policy["stream_idle_seconds"],
            request_defaults=policy["request_defaults"],
            provider=record.get("provider", ""),
            prompt_cache=policy.get("prompt_cache"),
            prompt_cache_key_supported=prompt_cache_key_supported,
            on_capability_evidence=on_cap,
            **protocol_kwargs,
        )

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
                if value is None and key not in {"requests_per_day", "tokens_per_day", "shared_tokens_per_day"}:
                    overrides.pop(key, None)
                else:
                    overrides[key] = value
            model_settings.validate({**model_settings.defaults(record, model), **overrides})
            record.setdefault("model_settings", {})[model] = {**row, "overrides": overrides}
            return self.save(record)

    @staticmethod
    def model_service_id(record, model):
        return record["id"] + "-" + hashlib.sha256(model.encode()).hexdigest()[:12]

    @staticmethod
    def _probe_llamacpp_n_ctx(adapter) -> int | None:
        if not hasattr(adapter, "_json"):
            return None
        timeout_s = min(5.0, float(getattr(adapter, "timeout_s", 5.0) or 5.0))
        props = None
        try:
            props = adapter._json("/props", None, timeout_s)
        except Exception:
            base = str(getattr(adapter, "base_url", "") or "").rstrip("/")
            if base.endswith("/v1") and len(base) > 3:
                try:
                    root_adapter = SourceAdapter(
                        base_url=base[:-3],
                        api_prefix="",
                        api_key=getattr(adapter, "api_key", ""),
                        model=getattr(adapter, "model", ""),
                        allow_non_loopback=True,
                        timeout_s=timeout_s,
                    )
                    props = root_adapter._json("/props", None, timeout_s)
                except Exception:
                    props = None
        if not isinstance(props, dict):
            return None
        gen_settings = props.get("default_generation_settings")
        for val in (
            gen_settings.get("n_ctx") if isinstance(gen_settings, dict) else None,
            props.get("n_ctx"),
        ):
            if type(val) is int and 256 <= val <= 2000000:
                return val
        return None

    def _collect_probed_metadata(self, record, adapter) -> tuple[list[dict], dict]:
        listing = adapter.models()
        metadata = copy.deepcopy(record.get("model_metadata", {}))
        metadata.update(copy.deepcopy(self._model_metadata_cache.get(self._metadata_key(record), {})))
        listed_ids: list[str] = []
        any_listed_window = False
        for item in listing:
            identifier = item.get("id") or item.get("name")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 160:
                continue
            listed_ids.append(identifier)
            values = [item.get(key) for key in ("context_window", "context_length", "max_model_len")]
            window = next((value for value in values if type(value) is int and 256 <= value <= 2000000), None)
            if window is not None:
                metadata[identifier] = {"context_window": window}
                any_listed_window = True
            out_values = [item.get(key) for key in ("max_output_tokens", "max_completion_tokens")]
            max_out = next((value for value in out_values if type(value) is int and 1 <= value <= 393216), None)
            if max_out is not None:
                metadata.setdefault(identifier, {})["max_output_tokens"] = max_out
            modalities = item.get("input_modalities")
            if isinstance(modalities, list) and 0 < len(modalities) <= 8 and all(isinstance(m, str) and m.strip() for m in modalities):
                metadata.setdefault(identifier, {})["input_modalities"] = [m.strip().lower() for m in modalities]
        if not any_listed_window:
            n_ctx = self._probe_llamacpp_n_ctx(adapter)
            if n_ctx is not None:
                for identifier in dict.fromkeys([*listed_ids, *record.get("models", [])]):
                    if identifier and "context_window" not in metadata.get(identifier, {}):
                        metadata.setdefault(identifier, {})["context_window"] = n_ctx
        with self.lock:
            key = self._metadata_key(record)
            if len(self._model_metadata_cache) >= 64 and key not in self._model_metadata_cache:
                self._model_metadata_cache.pop(next(iter(self._model_metadata_cache)))
            self._model_metadata_cache[key] = copy.deepcopy(metadata)
        record["model_metadata"] = {model: metadata[model] for model in record["models"] if model in metadata}
        return listing, metadata

    @staticmethod
    def _update_record_window_basis(record, *, candidate_ids: list[str] | None = None) -> None:
        models = record.get("models") or []
        if models:
            metas = [model_settings.context_metadata(record, m) for m in models]
            if any(m["context_window_basis"] == "unknown_fallback" for m in metas):
                record["context_window_basis"] = "unknown_fallback"
            else:
                record["context_window_basis"] = metas[0]["context_window_basis"]
        elif record.get("context_window_mode") == "auto":
            cands = candidate_ids or []
            if not cands or any(model_settings._known_vendor_context_window(m) is None for m in cands):
                record["context_window_basis"] = "unknown_fallback"

    def probe(self, record, *, models_only=False):
        adapter = self.adapter(record)
        if models_only:
            listing, metadata = self._collect_probed_metadata(record, adapter)
            listed_models = [str(item.get("id") or item.get("name")) for item in listing if item.get("id") or item.get("name")]
            unmatched = [m for m in listed_models if "context_window" not in metadata.get(m, {})]
            if record.get("models"):
                self._update_record_window_basis(record)
            elif unmatched:
                self._update_record_window_basis(record, candidate_ids=unmatched)
            return {
                "models": listed_models,
                "model_metadata": metadata,
                **({"context_window_basis": record["context_window_basis"]} if "context_window_basis" in record else {}),
            }
        if not record["models"]:
            raise ValueError("请先添加至少一个模型 ID")
        if record.get("context_window_mode") == "auto" and any(
            "context_window" not in record.get("model_metadata", {}).get(m, {})
            for m in record["models"]
        ):
            try:
                self._collect_probed_metadata(record, adapter)
            except Exception:
                n_ctx = self._probe_llamacpp_n_ctx(adapter)
                if n_ctx is not None:
                    meta_map = copy.deepcopy(record.get("model_metadata", {}))
                    for m in record["models"]:
                        meta_map.setdefault(m, {})["context_window"] = n_ctx
                    record["model_metadata"] = meta_map
            model_settings.reconcile(record, copy.deepcopy(record))
        self._update_record_window_basis(record)
        primary_model = record["models"][0]
        eff = model_settings.effective(record, primary_model)
        max_probe_tokens = min(4096, eff["max_output_tokens"])
        probe_body = {"messages": [{"role": "user", "content": "Reply briefly: OK"}], "max_tokens": max_probe_tokens}
        should_probe_cache_key = (
            source_protocol(record) == "openai_chat"
            and eff.get("prompt_cache") == "auto"
            and str(record.get("provider") or "").strip().lower() not in {"openai", "azure"}
            and isinstance(adapter, OpenAICompatibleAdapter)
        )
        if should_probe_cache_key:
            checked_ts = int(time.time())
            try:
                result = adapter.chat(
                    {**probe_body, "prompt_cache_key": "ryn_probe_cache_key"},
                    task_id="source-test-" + uuid.uuid4().hex,
                    timeout_s=90,
                    _probe_prompt_cache_key=True,
                )
                for m in record["models"]:
                    record.setdefault("model_settings", {}).setdefault(m, {}).setdefault("evidence", {})["prompt_cache_key"] = {
                        "status": "supported",
                        "checked_at": checked_ts,
                    }
            except AdapterError as exc:
                if exc.code in {"upstream_request_rejected", "upstream_http_failed"} and (exc.diagnostics or {}).get("http_status") in {400, 422}:
                    for m in record["models"]:
                        record.setdefault("model_settings", {}).setdefault(m, {}).setdefault("evidence", {})["prompt_cache_key"] = {
                            "status": "unsupported",
                            "checked_at": checked_ts,
                        }
                    result = adapter.chat(probe_body, task_id="source-test-" + uuid.uuid4().hex, timeout_s=90)
                else:
                    raise
        else:
            result = adapter.chat(probe_body, task_id="source-test-" + uuid.uuid4().hex, timeout_s=90)
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

    def probe_model_image(self, record, model):
        """Explicit owner-triggered image test: one small red square, asked its colour."""
        if model not in record["models"]:
            raise ValueError("unknown configured model")
        return self.probe_adapter_image(self.adapter(record, model), model_settings.effective(record, model)["max_output_tokens"])

    @staticmethod
    def probe_adapter_image(adapter, max_output_tokens):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Reply with the code RYN-RED if the square is red, otherwise RYN-OTHER"},
            {"type": "image_url", "image_url": {"url": IMAGE_PROBE_DATA_URL, "detail": "low"}}]}]
        try:
            result = adapter.chat({"messages": messages, "max_tokens": min(1024, max_output_tokens)},
                                  task_id="source-image-" + uuid.uuid4().hex, timeout_s=60)
        except AdapterError as exc:
            if exc.code == "upstream_image_rejected":
                return {"image_support": "unsupported"}
            raise
        text = str(result.get("text") or (result.get("message") or {}).get("content") or "").lower()
        if "ryn-other" in text and "ryn-red" not in text:
            supported = False
        elif "ryn-red" in text:
            supported = True
        else:
            supported = bool(re.search(r"\b(?:red|crimson|scarlet|rojo|rouge|rot)\b", text) or "红" in text)
        return {"image_support": "supported" if supported else "unknown"}

    @staticmethod
    def probe_adapter_tools(adapter, max_output_tokens):
        from .chat import new_call_id_nonce, normalize_call_id, validate_tool_arguments

        schema = {"type": "object", "properties": {"value": {"const": "ryn-tool-test"}},
                  "required": ["value"], "additionalProperties": False}
        spec = {"type": "function", "function": {"name": "ryn_capability_probe", "description": "Return the requested test value.", "parameters": schema}}
        messages = [{"role": "user", "content": "Call ryn_capability_probe with value ryn-tool-test."}]
        first = adapter.chat({"messages": messages, "tools": [spec], "tool_choice": "required",
                              "max_tokens": min(4096, max_output_tokens)},
                             task_id="source-tools-" + uuid.uuid4().hex, timeout_s=60)
        try:
            message = copy.deepcopy(first.get("message") or {})
            calls = message.get("tool_calls") or []
            if len(calls) != 1 or not isinstance(calls[0], dict):
                raise ValueError("model did not return a structured client tool call")
            call = dict(calls[0])
            fn = call.get("function")
            if not isinstance(fn, dict) or fn.get("name") != "ryn_capability_probe":
                raise ValueError("model did not return a structured client tool call")
            raw_id = call.get("id")
            if raw_id is not None and not isinstance(raw_id, str):
                raise ValueError("model did not return a structured client tool call")
            call_id = normalize_call_id(raw_id, 0, new_call_id_nonce())
            call["id"] = call_id
            message["tool_calls"] = [call]
            validate_tool_arguments(schema, json.loads(fn["arguments"]))
        except Exception:
            raise ValueError("model did not return a structured client tool call") from None
        messages += [message, {"role": "tool", "tool_call_id": call_id, "content": "ryn-tool-test-ok"},
                     {"role": "user", "content": "Return the tool result as text; do not call another tool."}]
        final = adapter.chat({"messages": messages, "tools": [spec], "tool_choice": "none",
                              "max_tokens": min(4096, max_output_tokens)},
                             task_id="source-tools-" + uuid.uuid4().hex, timeout_s=60)
        if not final.get("text") or final.get("message", {}).get("tool_calls"):
            raise ValueError("model did not complete the tool-result continuation")
        return {"tool_support": "supported"}
