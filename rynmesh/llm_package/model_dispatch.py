"""Actual source inference admission, shared by local, Agent and peer callers."""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import nullcontext
from contextvars import ContextVar

from . import model_settings
from .adapters import AdapterError, deadline_options
from .model_usage import ModelUsageStore, current_usage_caller
from .peer_binding import model_binding

DISPATCH_WAIT_MAX_SECONDS = 120.0
_BOUND = ContextVar("ryn_source_model_dispatch_binding", default=None)
_SYNC_INIT_LOCK = threading.Lock()


def _should_refund_dispatch(exc: BaseException) -> bool:
    if not isinstance(exc, AdapterError):
        return False
    if exc.code not in {"runtime_connection_failed", "runtime_busy"}:
        return False
    diag = exc.diagnostics if isinstance(exc.diagnostics, dict) else {}
    return diag.get("network_phase") == "request"


class ModelDispatchAdapter:
    supports_request_deadline = True
    supports_cancel_callback = True

    def __init__(self, sources, record, model, inner):
        self.sources, self.model, self.inner = sources, model, inner
        self.source_id = record["id"]
        self.revision = record["model_settings"][model]["rule_revision"]
        self.fingerprint = model_settings.fingerprint(record, model)
        self.model_binding = model_binding(record, model)
        self.service_id = sources.model_service_id(record, model)
        self.model_key = "local/" + self.service_id
        self.path = sources.path.parent / "model-usage.json"
        self.usage_store = getattr(sources, "usage_store", None) or ModelUsageStore(self.path)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _sync_primitives(self) -> tuple[threading.Condition, set[str]]:
        cond = getattr(self.sources, "_dispatch_cond", None)
        cancelled_tasks = getattr(self.sources, "_cancelled_dispatch_tasks", None)
        if cond is not None and cancelled_tasks is not None:
            return cond, cancelled_tasks
        with _SYNC_INIT_LOCK:
            cond = getattr(self.sources, "_dispatch_cond", None)
            if cond is None:
                cond = threading.Condition()
                self.sources._dispatch_cond = cond
            cancelled_tasks = getattr(self.sources, "_cancelled_dispatch_tasks", None)
            if cancelled_tasks is None:
                cancelled_tasks = set()
                self.sources._cancelled_dispatch_tasks = cancelled_tasks
            return cond, cancelled_tasks

    def _discard_inner_cancelled(self, task_id: str | None) -> None:
        if task_id and hasattr(self.inner, "_cancelled"):
            self.inner._cancelled.discard(task_id)

    def cancel(self, task_id: str) -> bool:
        if task_id:
            cond, cancelled_tasks = self._sync_primitives()
            with cond:
                cancelled_tasks.add(task_id)
                cond.notify_all()
        inner_cancel = getattr(self.inner, "cancel", None)
        return bool(inner_cancel(task_id)) if callable(inner_cancel) else True

    def _start(self, *, task_id=None, caller=None, max_tokens=None, deadline=None, timeout_s=None, cancelled=None):
        bound = _BOUND.get()
        cond, cancelled_tasks = self._sync_primitives()
        started = time.monotonic()
        max_wait = float(DISPATCH_WAIT_MAX_SECONDS)
        if deadline is not None:
            wait_deadline = min(started + max_wait, float(deadline))
        elif isinstance(timeout_s, (int, float)) and timeout_s >= 0:
            wait_deadline = started + min(max_wait, float(timeout_s))
        else:
            wait_deadline = started + max_wait

        while True:
            # Match owner mutations' space -> source lock order. Release both before
            # waiting on Condition or performing network I/O.
            with (bound[2] if bound and bound[2] is not None else nullcontext()), self.sources.lock, cond:
                if (task_id and task_id in cancelled_tasks) or (callable(cancelled) and cancelled()):
                    cancelled_tasks.discard(task_id)
                    self._discard_inner_cancelled(task_id)
                    raise AdapterError("task_cancelled")
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
                output_ceiling = policy["max_output_tokens"]
                if policy.get("max_output_tokens_known") is False:
                    context = model_settings.context_metadata(record, self.model)
                    window = context["context_window"] if context["context_window_known"] else model_settings.FALLBACK_CONTEXT_WINDOW
                    output_ceiling = max(output_ceiling, window // 2)
                    vendor = model_settings._known_vendor_max_output_tokens(self.model)
                    if vendor is not None:
                        output_ceiling = min(output_ceiling, vendor)
                requested_max = (
                    min(max_tokens, output_ceiling)
                    if type(max_tokens) is int and max_tokens >= 1
                    else policy["max_output_tokens"]
                )
                active = self.sources._active_models.get(self.service_id, 0)
                if active < policy["max_concurrent"]:
                    admitted = self.usage_store.admit_and_record_dispatch(
                        self.model_key,
                        policy=policy,
                        caller=caller or current_usage_caller(),
                        task_id=task_id,
                        max_tokens=requested_max,
                    )
                    granted_max = admitted.get("granted_max_tokens") or requested_max
                    self.sources._active_models[self.service_id] = active + 1
                    return policy, granted_max
            with cond:
                while True:
                    if (task_id and task_id in cancelled_tasks) or (callable(cancelled) and cancelled()):
                        cancelled_tasks.discard(task_id)
                        self._discard_inner_cancelled(task_id)
                        raise AdapterError("task_cancelled")
                    if self.sources._active_models.get(self.service_id, 0) < policy["max_concurrent"]:
                        break
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        err = AdapterError("runtime_busy", code="runtime_busy")
                        err.diagnostics = {"reason": "service_slots"}
                        raise err
                    cond.wait(timeout=min(remaining, 0.2 if callable(cancelled) else remaining))

    def call_bound(self, method, expected, authorize, *args, authority_lock=None, **kwargs):
        token = _BOUND.set((expected, authorize, authority_lock))
        try:
            return getattr(self, method)(*args, **kwargs)
        finally:
            _BOUND.reset(token)

    def _finish(self, task_id=None, *, refund: bool = False):
        cond, cancelled_tasks = self._sync_primitives()
        with self.sources.lock, cond:
            self.sources._active_models[self.service_id] -= 1
            if task_id:
                cancelled_tasks.discard(task_id)
            cond.notify_all()
        if refund and task_id:
            self.usage_store.refund_dispatch(task_id)

    def chat(self, body, *, deadline=None, cancelled=None, **kwargs):
        kwargs = deadline_options(kwargs, deadline)
        caller = current_usage_caller()
        task_id = kwargs.get("task_id")
        dispatch_task_id = task_id if isinstance(task_id, str) and task_id else f"_dispatch_{uuid.uuid4().hex}"
        _policy, granted_max = self._start(
            task_id=dispatch_task_id,
            caller=caller,
            max_tokens=body.get("max_tokens"),
            deadline=deadline,
            timeout_s=kwargs.get("timeout_s"),
            cancelled=cancelled,
        )
        refund = False
        try:
            body = {**body, "max_tokens": granted_max}
            inner_kwargs = deadline_options(kwargs, deadline)
            if deadline is not None and getattr(self.inner, "supports_request_deadline", False) is True:
                inner_kwargs = {**inner_kwargs, "deadline": deadline}
            result = self.inner.chat(body, **inner_kwargs)
            self.usage_store.record_completion(
                self.model_key, result=result, caller=caller, state="succeeded", task_id=dispatch_task_id
            )
            return result
        except Exception as exc:
            if _should_refund_dispatch(exc):
                refund = True
            else:
                self.usage_store.record_completion(
                    self.model_key, result=None, caller=caller, state="failed", task_id=dispatch_task_id
                )
            raise
        finally:
            self._finish(dispatch_task_id, refund=refund)

    def infer(self, *, max_tokens, deadline=None, cancelled=None, **kwargs):
        caller = current_usage_caller()
        task_id = kwargs.get("task_id")
        dispatch_task_id = task_id if isinstance(task_id, str) and task_id else f"_dispatch_{uuid.uuid4().hex}"
        _policy, granted_max = self._start(
            task_id=dispatch_task_id,
            caller=caller,
            max_tokens=max_tokens,
            deadline=deadline,
            timeout_s=kwargs.get("timeout_s"),
            cancelled=cancelled,
        )
        refund = False
        try:
            result = self.inner.infer(max_tokens=granted_max, **kwargs)
            self.usage_store.record_completion(
                self.model_key, result=result, caller=caller, state="succeeded", task_id=dispatch_task_id
            )
            return result
        except Exception as exc:
            if _should_refund_dispatch(exc):
                refund = True
            else:
                self.usage_store.record_completion(
                    self.model_key, result=None, caller=caller, state="failed", task_id=dispatch_task_id
                )
            raise
        finally:
            self._finish(dispatch_task_id, refund=refund)

    def infer_stream(self, *, max_tokens, deadline=None, cancelled=None, **kwargs):
        caller = current_usage_caller()
        task_id = kwargs.get("task_id")
        dispatch_task_id = task_id if isinstance(task_id, str) and task_id else f"_dispatch_{uuid.uuid4().hex}"
        _policy, granted_max = self._start(
            task_id=dispatch_task_id,
            caller=caller,
            max_tokens=max_tokens,
            deadline=deadline,
            timeout_s=kwargs.get("timeout_s"),
            cancelled=cancelled,
        )
        refund = False
        try:
            result = self.inner.infer_stream(max_tokens=granted_max, **kwargs)
            self.usage_store.record_completion(
                self.model_key, result=result, caller=caller, state="succeeded", task_id=dispatch_task_id
            )
            return result
        except Exception as exc:
            if _should_refund_dispatch(exc):
                refund = True
            else:
                self.usage_store.record_completion(
                    self.model_key, result=None, caller=caller, state="failed", task_id=dispatch_task_id
                )
            raise
        finally:
            self._finish(dispatch_task_id, refund=refund)
