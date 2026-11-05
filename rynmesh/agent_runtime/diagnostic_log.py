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
# Modules whose exception text is always a controlled ``agent_*`` code.
_CODED_MODULES = frozenset({"rynmesh.agent_runtime.store", "rynmesh.agent_runtime.extensions.mcp_oauth"})
_OAUTH_FIELDS = {
    "server": _SAFE_ID, "flow": re.compile(r"[a-f0-9]{12}\Z"), "reason": _SAFE_ENUM,
    "code": _CODE, "flow_state": _SAFE_ENUM, "oauth_error": _SAFE_ENUM,
}


def oauth_event(stage, **fields):
    """Stage-level OAuth trace. Only bounded enums, server ids, ports, counts and a
    one-way flow reference are accepted; URLs, codes, verifiers and tokens cannot
    be expressed through this function."""
    if not isinstance(stage, str) or not _SAFE_ENUM.fullmatch(stage):
        return
    out = {}
    for key, value in fields.items():
        pattern = _OAUTH_FIELDS.get(key)
        if pattern is not None and isinstance(value, str) and pattern.fullmatch(value):
            out[key] = value
        elif key in {"http_status", "redirect_port", "tools"} and type(value) is int and 0 <= value <= 65535:
            out[key] = value
        elif key in {"ok", "had_tokens"} and isinstance(value, bool):
            out[key] = value
    _log.warning("agent_oauth at=%s pid=%s thread=%s stage=%s fields=%s",
                 datetime.now(timezone.utc).isoformat(), os.getpid(), threading.current_thread().name[:48], stage, out)


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
    from ..llm_package.transport_failure import REASONS
    if isinstance(raw.get("network_reason"), str) and raw["network_reason"] in REASONS:
        out["network_reason"] = raw["network_reason"]
    if isinstance(raw.get("network_phase"), str) and raw["network_phase"] in {"request", "read"}:
        out["network_phase"] = raw["network_phase"]
    status = raw.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        out["http_status"] = status
    retry_after = raw.get("retry_after_seconds")
    if type(retry_after) in (int, float) and 0 <= retry_after <= 86400:
        out["retry_after_seconds"] = float(retry_after)
    return out


def failure(stage, exc, *, elapsed_seconds=None, diagnostics=None, **context_fields):
    chain, seen, current = [], set(), exc if isinstance(exc, BaseException) else None
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        name = re.sub(r"[^A-Za-z0-9_]", "", type(current).__name__)[:80]
        code = str(current) if type(current).__module__ in _CODED_MODULES else ""
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


def performance(stage, *, operation=None, **phases):
    if sum(phases.values()) >= 1.0:
        ctx = _safe_context()
        _log.warning("agent_slow at=%s pid=%s thread=%s stage=%s operation=%s context=%s phases=%s",
                     datetime.now(timezone.utc).isoformat(), os.getpid(), threading.current_thread().name[:48], stage, operation,
                     ctx, {key: round(value, 3) for key, value in phases.items()})
