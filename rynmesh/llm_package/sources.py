"""Device-owned AI source configurations. Secrets never leave the local control API."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from .adapters import AdapterError, OpenAICompatibleAdapter, validate_local_url


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
    def __init__(self, home: Path, private_key: bytes):
        self.path = home / "llm" / "sources.enc"
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"ryn-ai-sources-v1" + private_key).digest()))
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {}
            return json.loads(self.cipher.decrypt(self.path.read_bytes()))

    def _write(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(self.cipher.encrypt(json.dumps(records).encode()))
        temp.chmod(0o600)
        temp.replace(self.path)

    @staticmethod
    def public(record):
        return {**{k: v for k, v in record.items() if k != "api_key"}, "has_key": bool(record.get("api_key"))}

    def prepare(self, body, source_id=""):
        old = self.read().get(source_id, {})
        if not isinstance(body, dict):
            raise ValueError("配置格式不正确")
        kind = body.get("kind", old.get("kind", "api"))
        if kind not in {"api", "local"}:
            raise ValueError("不支持的接入方式")
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
        context = int(body.get("context_window", old.get("context_window", 32768)))
        output = int(body.get("max_output_tokens", old.get("max_output_tokens", 4096)))
        if not 256 <= context <= 2000000 or not 1 <= output <= min(context, 256000):
            raise ValueError("上下文或输出上限不正确")
        defaults = body.get("request_defaults", old.get("request_defaults", {}))
        if not isinstance(defaults, dict) or set(defaults) - {"thinking", "reasoning_effort", "temperature", "top_p"}:
            raise ValueError("请求参数仅支持 thinking、reasoning_effort、temperature、top_p")
        if len(json.dumps(defaults)) > 4096:
            raise ValueError("请求参数过长")
        return {"id": source_id or "src-" + uuid.uuid4().hex[:16], "kind": kind,
                "name": name, "provider": str(body.get("provider", old.get("provider", "custom")))[:80],
                "base_url": base, "api_key": key.strip(), "models": models,
                "context_window": context, "max_output_tokens": output, "request_defaults": defaults,
                "sharing": bool(body.get("sharing", old.get("sharing", False))),
                "enabled": bool(body.get("enabled", old.get("enabled", True))),
                "status": "needs_key" if kind == "api" and not key else "untested"}

    def save(self, record):
        with self.lock:
            records = self.read()
            records[record["id"]] = record
            self._write(records)
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
        return SourceAdapter(base_url=record["base_url"], api_prefix="", api_key=record.get("api_key", ""),
                             model=model or next(iter(record["models"]), ""),
                             allow_non_loopback=record["kind"] == "api", timeout_s=180,
                             request_defaults=record.get("request_defaults", {}))

    @staticmethod
    def model_service_id(record, model):
        return record["id"] + "-" + hashlib.sha256(model.encode()).hexdigest()[:12]

    def probe(self, record, *, models_only=False):
        adapter = self.adapter(record)
        if models_only:
            return {"models": [str(item.get("id") or item.get("name")) for item in adapter.models() if item.get("id") or item.get("name")]}
        if not record["models"]:
            raise ValueError("请先添加至少一个模型 ID")
        result = adapter.chat({"messages": [{"role": "user", "content": "Reply briefly: OK"}],
                               "max_tokens": min(4096, record["max_output_tokens"])}, task_id="source-test-" + uuid.uuid4().hex, timeout_s=90)
        if not result.get("text"):
            raise AdapterError("模型未返回正文，请检查输出上限或推理参数")
        return {"ok": True, "model": adapter.model}
