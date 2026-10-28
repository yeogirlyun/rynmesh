"""Body-free Agent failure diagnostics; never log prompts, paths or exception text."""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
_CODE = re.compile(r"agent_[a-z0-9_]{1,100}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9_:-]{1,80}\Z")
_SAFE_ENUM = re.compile(r"[a-z0-9_]{1,40}\Z")
_LOCAL = threading.local()


def _safe_context() -> dict:
    current = getattr(_LOCAL, "context", None)
    return dict(current) if isinstance(current, dict) else {}


@contextmanager
def context_scope(**fields):
    previous = _safe_context()
    updated = dict(previous)
    for key in ("task_id", "operation_id", "request_id"):
        value = fields.get(key)
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            updated[key] = value
    for key in ("step", "attempt"):
        val = fields.get(key)
        if type(val) is int and val >= 0:
            updated[key] = val
    _LOCAL.context = updated
    try:
        yield
    finally:
        _LOCAL.context = previous


def safe_diagnostics(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("rpc_stage", "session_mode"):
        val = raw.get(key)
        if isinstance(val, str) and _SAFE_ENUM.fullmatch(val):
            out[key] = val
    for key in ("events_received", "deltas_received"):
        val = raw.get(key)
        if type(val) is int and val >= 0:
            out[key] = val
    age = raw.get("last_event_age_seconds")
    if type(age) in (int, float) and age >= 0:
        out["last_event_age_seconds"] = round(float(age), 3)
    alive = raw.get("process_alive")
    if isinstance(alive, bool):
        out["process_alive"] = alive
    return out


def failure(stage, exc, *, elapsed_seconds=None, diagnostics=None, **context_fields):
    chain, seen, current = [], set(), exc if isinstance(exc, BaseException) else None
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        name = re.sub(r"[^A-Za-z0-9_]", "", type(current).__name__)[:80]
        code = str(current) if type(current).__module__ == "rynmesh.agent_runtime.store" else ""
        diag = safe_diagnostics(getattr(current, "diagnostics", None))
        chain.append({"type": name, **({"code": code} if _CODE.fullmatch(code) else {}),
                      **{key: value for key in ("errno", "winerror")
                         if type(value := getattr(current, key, None)) is int},
                      **({"diagnostics": diag} if diag else {})})
        current = current.__cause__ or current.__context__
    if not chain and isinstance(exc, str) and _CODE.fullmatch(exc):
        diag = safe_diagnostics(diagnostics)
        chain.append({"type": "AgentError", "code": exc, **({"diagnostics": diag} if diag else {})})
    elif chain and diagnostics and "diagnostics" not in chain[0]:
        diag = safe_diagnostics(diagnostics)
        if diag:
            chain[0]["diagnostics"] = diag
    with context_scope(**context_fields):
        ctx = _safe_context()
    # stage is a host-authored route/tool phase, never a request argument.
    _log.warning("agent_failure at=%s pid=%s thread=%s stage=%s context=%s elapsed_seconds=%s causes=%s",
                 datetime.now(timezone.utc).isoformat(), os.getpid(), threading.current_thread().name[:48], stage,
                 ctx, round(elapsed_seconds, 3) if elapsed_seconds is not None else None, chain)


def performance(stage, **phases):
    if sum(phases.values()) >= 1.0:
        ctx = _safe_context()
        _log.warning("agent_slow at=%s pid=%s thread=%s stage=%s context=%s phases=%s",
                     datetime.now(timezone.utc).isoformat(), os.getpid(), threading.current_thread().name[:48], stage,
                     ctx, {key: round(value, 3) for key, value in phases.items()})
