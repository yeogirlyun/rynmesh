"""Actual source inference admission, shared by local, Agent and peer callers."""
from __future__ import annotations

from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from . import model_settings
from .adapters import AdapterError, deadline_options
from .peer_binding import model_binding

_BOUND = ContextVar("ryn_source_model_dispatch_binding", default=None)


class ModelDispatchAdapter:
    supports_request_deadline = True

    def __init__(self, sources, record, model, inner):
        self.sources, self.model, self.inner = sources, model, inner
        self.source_id = record["id"]
        self.revision = record["model_settings"][model]["rule_revision"]
        self.fingerprint = model_settings.fingerprint(record, model)
        self.model_binding = model_binding(record, model)
        self.service_id = sources.model_service_id(record, model)
        self.path = sources.path.parent / "model-usage.json"

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _start(self):
        bound = _BOUND.get()
        # Match owner mutations' space -> source lock order. Release both at
        # admission, before network I/O; already admitted requests are in flight.
        with (bound[2] if bound and bound[2] is not None else nullcontext()), self.sources.lock:
            record = self.sources.read().get(self.source_id, {})
            row = record.get("model_settings", {}).get(self.model, {})
            if (not record.get("enabled") or row.get("rule_revision") != self.revision
                    or model_settings.fingerprint(record, self.model) != self.fingerprint):
                raise AdapterError("model_configuration_changed", code="model_configuration_changed")
            if bound:
                expected, authorize, _authority_lock = bound
                if expected != self.model_binding:
                    raise AdapterError("model_configuration_changed", code="model_configuration_changed")
                try:
                    authorize()
                except (ValueError, OSError):
                    raise AdapterError("ai_permission_denied", code="ai_permission_denied") from None
            policy = model_settings.effective(record, self.model)
            active = self.sources._active_models.get(self.service_id, 0)
            if active >= policy["max_concurrent"]:
                raise AdapterError("runtime_busy", code="runtime_busy")
            # Attempts consume quota when dispatched. Unknown/failed/cancelled
            # outcomes never refund it; a restart cannot reset today's usage.
            with file_transaction(Path(str(self.path) + ".lock")):
                data = read_json(self.path, max_bytes=2 * 1024 * 1024) if self.path.exists() else {}
                day = datetime.now(timezone.utc).date().isoformat()
                data = {k: v for k, v in data.items() if v.get("day") == day}
                count = data.get(self.service_id, {}).get("requests", 0)
                limit = policy["requests_per_day"]
                if limit is not None and count >= limit:
                    raise AdapterError("model_daily_quota_exhausted", code="model_daily_quota_exhausted")
                data[self.service_id] = {"day": day, "requests": count + 1}
                atomic_write_json(self.path, data, max_bytes=2 * 1024 * 1024)
            self.sources._active_models[self.service_id] = active + 1
            return policy

    def call_bound(self, method, expected, authorize, *args, authority_lock=None, **kwargs):
        token = _BOUND.set((expected, authorize, authority_lock))
        try:
            return getattr(self, method)(*args, **kwargs)
        finally:
            _BOUND.reset(token)

    def _finish(self):
        with self.sources.lock:
            self.sources._active_models[self.service_id] -= 1

    def chat(self, body, *, deadline=None, **kwargs):
        kwargs = deadline_options(kwargs, deadline)
        policy = self._start()
        try:
            body = {**body, "max_tokens": min(body.get("max_tokens", policy["max_output_tokens"]), policy["max_output_tokens"])}
            return self.inner.chat(body, **deadline_options(kwargs, deadline))
        finally:
            self._finish()

    def infer(self, *, max_tokens, **kwargs):
        policy = self._start()
        try:
            return self.inner.infer(max_tokens=min(max_tokens, policy["max_output_tokens"]), **kwargs)
        finally:
            self._finish()

    def infer_stream(self, *, max_tokens, **kwargs):
        policy = self._start()
        try:
            return self.inner.infer_stream(max_tokens=min(max_tokens, policy["max_output_tokens"]), **kwargs)
        finally:
            self._finish()
