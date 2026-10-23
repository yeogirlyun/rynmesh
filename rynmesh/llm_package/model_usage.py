"""Unified per-model token usage and daily quota ledger across local and peer models."""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from .adapters import AdapterError, actual_usage, cached_input_tokens

MAX_USAGE_BYTES = 4 * 1024 * 1024
MAX_MODEL_ENTRIES = 512
MAX_RECENT_TASKS = 64
MAX_DAILY_BUCKETS = 30
QUOTA_FIELDS = ("requests_per_day", "tokens_per_day", "shared_tokens_per_day")

_USAGE_CALLER: ContextVar[str] = ContextVar("ryn_model_usage_caller", default="self")
_READ_CACHE_LOCK = threading.Lock()
_READ_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def current_usage_caller() -> str:
    caller = _USAGE_CALLER.get()
    return "shared" if caller == "shared" else "self"


@contextmanager
def usage_caller_scope(caller: str):
    normalized = "shared" if caller == "shared" else "self"
    token = _USAGE_CALLER.set(normalized)
    try:
        yield normalized
    finally:
        _USAGE_CALLER.reset(token)


def normalize_model_key(model_key: str) -> str:
    if not isinstance(model_key, str) or not model_key.strip():
        raise ValueError("model_key_invalid")
    cleaned = model_key.strip()
    if len(cleaned) > 320:
        raise ValueError("model_key_invalid")
    if cleaned.startswith(("local/", "peer/")):
        return cleaned
    return "local/" + cleaned


def validate_quota_limits(values: dict[str, Any]) -> dict[str, int | None]:
    if not isinstance(values, dict) or (set(values) - set(QUOTA_FIELDS)):
        raise ValueError("model_quota_configuration_invalid")
    validated: dict[str, int | None] = {}
    for key, high in (
        ("requests_per_day", 1_000_000),
        ("tokens_per_day", 10_000_000_000),
        ("shared_tokens_per_day", 10_000_000_000),
    ):
        if key not in values:
            continue
        val = values[key]
        if val is not None and (type(val) is not int or not (1 <= val <= high)):
            raise ValueError(f"model_{key}_invalid")
        validated[key] = val
    return validated


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def utc_day() -> str:
    return _utc_today()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_today(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "requests": 0,
        "succeeded_requests": 0,
        "failed_requests": 0,
        "unknown_usage_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "shared_requests": 0,
        "shared_tokens": 0,
    }


def _empty_caller(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "today_requests": 0,
        "today_tokens": 0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }


def _empty_entry(day: str) -> dict[str, Any]:
    return {
        "today": _empty_today(day),
        "total": {
            "requests": 0,
            "succeeded_requests": 0,
            "failed_requests": 0,
            "unknown_usage_requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
            "first_used_at": None,
            "last_used_at": None,
        },
        "by_caller": {
            "self": _empty_caller(day),
            "shared": _empty_caller(day),
        },
        "limits": {
            "requests_per_day": None,
            "tokens_per_day": None,
            "shared_tokens_per_day": None,
        },
        "daily": [],
        "dispatched_tasks": [],
        "completed_tasks": [],
    }


def _sync_today_to_daily(entry: dict[str, Any]) -> None:
    today = entry.get("today")
    if not isinstance(today, dict):
        return
    day_str = str(today.get("day") or "")
    if not day_str:
        return
    daily_map: dict[str, dict[str, Any]] = {}
    for item in entry.get("daily") or []:
        if isinstance(item, dict) and isinstance(item.get("day"), str) and item["day"]:
            d = item["day"]
            tok = _nonneg_int(item.get("total_tokens", item.get("tokens")))
            daily_map[d] = {
                "day": d,
                "tokens": tok,
                "total_tokens": tok,
                "input_tokens": _nonneg_int(item.get("input_tokens")),
                "output_tokens": _nonneg_int(item.get("output_tokens")),
                "cached_tokens": _nonneg_int(item.get("cached_tokens")),
                "requests": _nonneg_int(item.get("requests")),
            }
    t_tok = _nonneg_int(today.get("total_tokens"))
    t_req = _nonneg_int(today.get("requests"))
    if t_tok > 0 or t_req > 0 or day_str in daily_map:
        existing = daily_map.get(day_str) or {}
        merged_tok = max(t_tok, _nonneg_int(existing.get("total_tokens")))
        daily_map[day_str] = {
            "day": day_str,
            "tokens": merged_tok,
            "total_tokens": merged_tok,
            "input_tokens": max(_nonneg_int(today.get("input_tokens")), _nonneg_int(existing.get("input_tokens"))),
            "output_tokens": max(_nonneg_int(today.get("output_tokens")), _nonneg_int(existing.get("output_tokens"))),
            "cached_tokens": max(_nonneg_int(today.get("cached_tokens")), _nonneg_int(existing.get("cached_tokens"))),
            "requests": max(t_req, _nonneg_int(existing.get("requests"))),
        }
    sorted_days = sorted(daily_map.keys())[-MAX_DAILY_BUCKETS:]
    entry["daily"] = [daily_map[d] for d in sorted_days]


def _nonneg_int(val: Any) -> int:
    return val if type(val) is int and val >= 0 else 0


def _normalize_entry(raw: Any, day: str) -> dict[str, Any]:
    base = _empty_entry(day)
    if not isinstance(raw, dict):
        return base
    # Seamless migration from legacy {"day": "YYYY-MM-DD", "requests": N} format
    if "today" not in raw and "total" not in raw and ("day" in raw or "requests" in raw):
        legacy_day = str(raw.get("day") or "")
        legacy_requests = _nonneg_int(raw.get("requests"))
        if legacy_day == day:
            base["today"]["requests"] = legacy_requests
            base["by_caller"]["self"]["today_requests"] = legacy_requests
        base["total"]["requests"] = legacy_requests
        base["by_caller"]["self"]["requests"] = legacy_requests
        return base

    raw_today = raw.get("today") if isinstance(raw.get("today"), dict) else {}
    if raw_today.get("day") == day:
        for k in (
            "requests",
            "succeeded_requests",
            "failed_requests",
            "unknown_usage_requests",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
            "shared_requests",
            "shared_tokens",
        ):
            base["today"][k] = _nonneg_int(raw_today.get(k))

    daily_map: dict[str, dict[str, Any]] = {}
    raw_daily = raw.get("daily")
    if isinstance(raw_daily, list):
        for item in raw_daily:
            if isinstance(item, dict) and isinstance(item.get("day"), str) and item["day"]:
                d = item["day"]
                tok = _nonneg_int(item.get("total_tokens", item.get("tokens")))
                daily_map[d] = {
                    "day": d,
                    "tokens": tok,
                    "total_tokens": tok,
                    "input_tokens": _nonneg_int(item.get("input_tokens")),
                    "output_tokens": _nonneg_int(item.get("output_tokens")),
                    "cached_tokens": _nonneg_int(item.get("cached_tokens")),
                    "requests": _nonneg_int(item.get("requests")),
                }
    prev_day = str(raw_today.get("day") or "")
    if prev_day and (_nonneg_int(raw_today.get("total_tokens")) > 0 or _nonneg_int(raw_today.get("requests")) > 0):
        existing = daily_map.get(prev_day) or {}
        merged_tok = max(_nonneg_int(raw_today.get("total_tokens")), _nonneg_int(existing.get("total_tokens")))
        daily_map[prev_day] = {
            "day": prev_day,
            "tokens": merged_tok,
            "total_tokens": merged_tok,
            "input_tokens": max(_nonneg_int(raw_today.get("input_tokens")), _nonneg_int(existing.get("input_tokens"))),
            "output_tokens": max(_nonneg_int(raw_today.get("output_tokens")), _nonneg_int(existing.get("output_tokens"))),
            "cached_tokens": max(_nonneg_int(raw_today.get("cached_tokens")), _nonneg_int(existing.get("cached_tokens"))),
            "requests": max(_nonneg_int(raw_today.get("requests")), _nonneg_int(existing.get("requests"))),
        }
    base["daily"] = [daily_map[d] for d in sorted(daily_map.keys())[-MAX_DAILY_BUCKETS:]]
    _sync_today_to_daily(base)

    raw_total = raw.get("total") if isinstance(raw.get("total"), dict) else {}
    for k in (
        "requests",
        "succeeded_requests",
        "failed_requests",
        "unknown_usage_requests",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "total_tokens",
    ):
        base["total"][k] = _nonneg_int(raw_total.get(k))
    for ts_key in ("first_used_at", "last_used_at"):
        ts_val = raw_total.get(ts_key)
        base["total"][ts_key] = ts_val if isinstance(ts_val, str) and ts_val else None

    raw_callers = raw.get("by_caller") if isinstance(raw.get("by_caller"), dict) else {}
    for caller in ("self", "shared"):
        c_raw = raw_callers.get(caller) if isinstance(raw_callers.get(caller), dict) else {}
        for k in ("requests", "input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            base["by_caller"][caller][k] = _nonneg_int(c_raw.get(k))
        if c_raw.get("day") == day:
            base["by_caller"][caller]["today_requests"] = _nonneg_int(c_raw.get("today_requests"))
            base["by_caller"][caller]["today_tokens"] = _nonneg_int(c_raw.get("today_tokens"))

    raw_limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    for k in QUOTA_FIELDS:
        val = raw_limits.get(k)
        base["limits"][k] = val if type(val) is int and val >= 1 else None

    for list_key in ("dispatched_tasks", "completed_tasks"):
        items = raw.get(list_key)
        if isinstance(items, list):
            base[list_key] = [str(x) for x in items[-MAX_RECENT_TASKS:] if isinstance(x, str) and x]
    return base


def _merge_legacy_and_canonical(existing: dict[str, Any], legacy: dict[str, Any], day: str) -> dict[str, Any]:
    norm_existing = _normalize_entry(existing, day)
    norm_legacy = _normalize_entry(legacy, day)
    for k in (
        "requests",
        "succeeded_requests",
        "failed_requests",
        "unknown_usage_requests",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "total_tokens",
        "shared_requests",
        "shared_tokens",
    ):
        norm_existing["today"][k] = max(norm_existing["today"][k], norm_legacy["today"][k])
    for k in (
        "requests",
        "succeeded_requests",
        "failed_requests",
        "unknown_usage_requests",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "total_tokens",
    ):
        norm_existing["total"][k] = max(norm_existing["total"][k], norm_legacy["total"][k])
    for caller in ("self", "shared"):
        for k in ("today_requests", "today_tokens", "requests", "input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            norm_existing["by_caller"][caller][k] = max(
                norm_existing["by_caller"][caller][k],
                norm_legacy["by_caller"][caller][k],
            )
    return norm_existing


def _normalize_store_data(raw_data: Any, day: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_data, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_val in raw_data.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        try:
            key = normalize_model_key(raw_key)
        except ValueError:
            continue
        if key in normalized:
            normalized[key] = _merge_legacy_and_canonical(normalized[key], raw_val, day)
        else:
            normalized[key] = _normalize_entry(raw_val, day)
    return normalized


def effective_limits(entry: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, int | None]:
    stored = entry.get("limits") if isinstance(entry.get("limits"), dict) else {}
    result: dict[str, int | None] = {}
    for key in QUOTA_FIELDS:
        policy_val = policy.get(key) if isinstance(policy, dict) and key in policy else None
        if type(policy_val) is int and policy_val >= 1:
            result[key] = policy_val
        elif isinstance(policy, dict) and key in policy and policy.get(key) is None and stored.get(key) is None:
            result[key] = None
        else:
            s_val = stored.get(key)
            result[key] = s_val if type(s_val) is int and s_val >= 1 else None
    return result


def format_public_usage(model_key: str, entry: dict[str, Any], *, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = normalize_model_key(model_key)
    day = _utc_today()
    norm = _normalize_entry(entry, day)
    today = norm["today"]
    total = norm["total"]
    by_caller = norm["by_caller"]
    limits = effective_limits(norm, policy)

    req_limit = limits["requests_per_day"]
    tok_limit = limits["tokens_per_day"]
    shared_tok_limit = limits["shared_tokens_per_day"]

    exhausted_reason: str | None = None
    if req_limit is not None and today["requests"] >= req_limit:
        exhausted_reason = "requests_per_day"
    elif tok_limit is not None and today["total_tokens"] >= tok_limit:
        exhausted_reason = "tokens_per_day"
    elif shared_tok_limit is not None and today["shared_tokens"] >= shared_tok_limit:
        exhausted_reason = "shared_tokens_per_day"

    today_hit_rate = (
        round(min(1.0, today["cached_tokens"] / today["input_tokens"]), 4)
        if today["input_tokens"] > 0
        else None
    )
    total_hit_rate = (
        round(min(1.0, total["cached_tokens"] / total["input_tokens"]), 4)
        if total["input_tokens"] > 0
        else None
    )

    caller_view = copy.deepcopy(by_caller)
    for caller in ("self", "shared"):
        bucket = caller_view[caller]
        bucket["today"] = {
            "day": bucket.get("day") or day,
            "requests": bucket["today_requests"],
            "completed_requests": bucket["today_requests"],
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": bucket["today_tokens"],
            "cache_hit_rate": 0.0,
        }
        bucket["total"] = {
            "requests": bucket["requests"],
            "completed_requests": bucket["requests"],
            "input_tokens": bucket["input_tokens"],
            "output_tokens": bucket["output_tokens"],
            "cached_tokens": bucket["cached_tokens"],
            "total_tokens": bucket["total_tokens"],
            "cache_hit_rate": (
                round(min(1.0, bucket["cached_tokens"] / bucket["input_tokens"]), 4)
                if bucket["input_tokens"] > 0
                else 0.0
            ),
        }

    rem_req = max(0, req_limit - today["requests"]) if req_limit is not None else None
    rem_tok = max(0, tok_limit - today["total_tokens"]) if tok_limit is not None else None
    rem_shared = max(0, shared_tok_limit - today["shared_tokens"]) if shared_tok_limit is not None else None

    return {
        "model_key": canonical,
        "usage_key": canonical,
        "today": {
            **copy.deepcopy(today),
            "completed_requests": today["succeeded_requests"] + today["failed_requests"],
            "cache_hit_rate": today_hit_rate if today_hit_rate is not None else 0.0,
        },
        "total": {
            **copy.deepcopy(total),
            "completed_requests": total["succeeded_requests"] + total["failed_requests"],
            "cache_hit_rate": total_hit_rate if total_hit_rate is not None else 0.0,
        },
        "daily": copy.deepcopy(norm.get("daily") or []),
        "by_caller": caller_view,
        "quota": {
            "requests_per_day": req_limit,
            "remaining_requests_today": rem_req,
            "requests_remaining": rem_req,
            "tokens_per_day": tok_limit,
            "remaining_tokens_today": rem_tok,
            "tokens_remaining": rem_tok,
            "shared_tokens_per_day": shared_tok_limit,
            "remaining_shared_tokens_today": rem_shared,
            "shared_tokens_remaining": rem_shared,
            "exhausted": exhausted_reason in {"requests_per_day", "tokens_per_day"},
            "shared_exhausted": exhausted_reason == "shared_tokens_per_day",
            "exhausted_reason": exhausted_reason,
        },
        "updated_at": total.get("last_used_at"),
    }


class ModelUsageStore:
    """Thread- and process-safe usage & daily quota ledger stored in model-usage.json."""

    def __init__(self, path: Path | str) -> None:
        p = Path(path)
        if p.name != "model-usage.json":
            p = (p / "llm" / "model-usage.json") if (p / "llm").is_dir() or p.name != "llm" else (p / "model-usage.json")
        self.path = p
        self.lock_path = Path(str(self.path) + ".lock")

    def _read_unlocked(self, day: str) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = read_json(self.path, max_bytes=MAX_USAGE_BYTES)
        except Exception:
            return {}
        return _normalize_store_data(raw, day)

    def _write_unlocked(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if len(data) > MAX_MODEL_ENTRIES:
            sorted_keys = sorted(
                data.keys(),
                key=lambda k: str(data[k].get("total", {}).get("last_used_at") or ""),
            )
            for k in sorted_keys[: len(data) - MAX_MODEL_ENTRIES]:
                data.pop(k, None)
        atomic_write_json(self.path, data, max_bytes=MAX_USAGE_BYTES)
        try:
            st = self.path.stat()
            with _READ_CACHE_LOCK:
                _READ_CACHE[str(self.path)] = ((st.st_size, st.st_mtime_ns), copy.deepcopy(data))
        except OSError:
            with _READ_CACHE_LOCK:
                _READ_CACHE.pop(str(self.path), None)

    def read_cached(self) -> dict[str, dict[str, Any]]:
        day = _utc_today()
        key = str(self.path)
        try:
            st = self.path.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError:
            with _READ_CACHE_LOCK:
                _READ_CACHE.pop(key, None)
            return {}
        with _READ_CACHE_LOCK:
            cached = _READ_CACHE.get(key)
            if cached is not None and cached[0] == stamp:
                return _normalize_store_data(cached[1], day)
        with file_transaction(self.lock_path):
            data = self._read_unlocked(day)
        with _READ_CACHE_LOCK:
            _READ_CACHE[key] = (stamp, copy.deepcopy(data))
        return data

    @staticmethod
    def utc_day() -> str:
        return _utc_today()

    def public_usage(self, model_key: str, *, policy: dict[str, Any] | None = None,
                     snapshot: dict[str, dict[str, Any]] | None = None,
                     **quota_Overrides: Any) -> dict[str, Any]:
        canonical = normalize_model_key(model_key)
        data = snapshot if snapshot is not None else self.read_cached()
        entry = data.get(canonical) or _empty_entry(_utc_today())
        merged_policy = dict(policy or {})
        for k in QUOTA_FIELDS:
            if k in quota_Overrides:
                merged_policy[k] = quota_Overrides[k]
        return format_public_usage(canonical, entry, policy=merged_policy or None)

    def check_quota(
        self,
        model_key: str,
        *,
        policy: dict[str, Any] | None = None,
        caller: str | None = None,
    ) -> dict[str, int | None]:
        canonical = normalize_model_key(model_key)
        effective_caller = ("shared" if caller == "shared" else "self") if caller else current_usage_caller()
        day = _utc_today()
        with file_transaction(self.lock_path):
            data = self._read_unlocked(day)
            entry = data.get(canonical) or _empty_entry(day)
            limits = effective_limits(entry, policy)
            self._enforce_limits(entry, limits, effective_caller)
            return limits

    @staticmethod
    def _enforce_limits(entry: dict[str, Any], limits: dict[str, int | None], caller: str) -> None:
        today = entry["today"]
        req_limit = limits.get("requests_per_day")
        if req_limit is not None and today["requests"] >= req_limit:
            raise AdapterError("model_daily_quota_exhausted", code="model_daily_quota_exhausted")
        tok_limit = limits.get("tokens_per_day")
        if tok_limit is not None and today["total_tokens"] >= tok_limit:
            raise AdapterError("model_daily_quota_exhausted", code="model_daily_quota_exhausted")
        if caller == "shared":
            shared_limit = limits.get("shared_tokens_per_day")
            if shared_limit is not None and today["shared_tokens"] >= shared_limit:
                raise AdapterError("model_daily_quota_exhausted", code="model_daily_quota_exhausted")

    def admit_and_record_dispatch(
        self,
        model_key: str,
        *,
        policy: dict[str, Any] | None = None,
        caller: str | None = None,
        task_id: str | None = None,
        enforce_quota: bool = True,
    ) -> dict[str, Any]:
        canonical = normalize_model_key(model_key)
        effective_caller = ("shared" if caller == "shared" else "self") if caller else current_usage_caller()
        day = _utc_today()
        now_iso = _utc_now_iso()
        with file_transaction(self.lock_path):
            data = self._read_unlocked(day)
            entry = data.get(canonical) or _empty_entry(day)
            limits = effective_limits(entry, policy)
            if enforce_quota:
                self._enforce_limits(entry, limits, effective_caller)
            if task_id and isinstance(task_id, str) and task_id in entry["dispatched_tasks"]:
                return format_public_usage(canonical, entry, policy=policy)
            if task_id and isinstance(task_id, str):
                entry["dispatched_tasks"] = [*entry["dispatched_tasks"][-(MAX_RECENT_TASKS - 1):], task_id]
            entry["today"]["requests"] += 1
            if effective_caller == "shared":
                entry["today"]["shared_requests"] += 1
            entry["total"]["requests"] += 1
            if not entry["total"]["first_used_at"]:
                entry["total"]["first_used_at"] = now_iso
            entry["total"]["last_used_at"] = now_iso
            caller_bucket = entry["by_caller"][effective_caller]
            caller_bucket["requests"] += 1
            caller_bucket["today_requests"] += 1
            _sync_today_to_daily(entry)
            data[canonical] = entry
            self._write_unlocked(data)
            return format_public_usage(canonical, entry, policy=policy)

    def record_completion(
        self,
        model_key: str,
        *,
        result: dict[str, Any] | None = None,
        caller: str | None = None,
        state: str = "succeeded",
        task_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_tokens: int | None = None,
        ensure_dispatched: bool = False,
    ) -> dict[str, Any]:
        canonical = normalize_model_key(model_key)
        effective_caller = ("shared" if caller == "shared" else "self") if caller else current_usage_caller()
        day = _utc_today()
        now_iso = _utc_now_iso()

        if isinstance(result, dict):
            counts = actual_usage(result)
            if input_tokens is None:
                input_tokens = counts.get("input_tokens")
            if output_tokens is None:
                output_tokens = counts.get("output_tokens")
            if cached_tokens is None:
                cached_tokens = cached_input_tokens(result)

        in_tok = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
        out_tok = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
        cache_tok = cached_tokens if type(cached_tokens) is int and cached_tokens >= 0 else 0
        if in_tok is not None and cache_tok > in_tok:
            cache_tok = in_tok

        with file_transaction(self.lock_path):
            data = self._read_unlocked(day)
            entry = data.get(canonical) or _empty_entry(day)
            if task_id and isinstance(task_id, str) and task_id in entry["completed_tasks"]:
                return format_public_usage(canonical, entry)
            if task_id and isinstance(task_id, str):
                entry["completed_tasks"] = [*entry["completed_tasks"][-(MAX_RECENT_TASKS - 1):], task_id]

            if ensure_dispatched and (not task_id or task_id not in entry["dispatched_tasks"]):
                if task_id and isinstance(task_id, str):
                    entry["dispatched_tasks"] = [*entry["dispatched_tasks"][-(MAX_RECENT_TASKS - 1):], task_id]
                entry["today"]["requests"] += 1
                if effective_caller == "shared":
                    entry["today"]["shared_requests"] += 1
                entry["total"]["requests"] += 1
                entry["by_caller"][effective_caller]["requests"] += 1
                entry["by_caller"][effective_caller]["today_requests"] += 1

            if not entry["total"]["first_used_at"]:
                entry["total"]["first_used_at"] = now_iso
            entry["total"]["last_used_at"] = now_iso

            if state == "succeeded":
                entry["today"]["succeeded_requests"] += 1
                entry["total"]["succeeded_requests"] += 1
                if in_tok is None or out_tok is None:
                    entry["today"]["unknown_usage_requests"] += 1
                    entry["total"]["unknown_usage_requests"] += 1
            else:
                entry["today"]["failed_requests"] += 1
                entry["total"]["failed_requests"] += 1

            add_in = in_tok or 0
            add_out = out_tok or 0
            add_total = add_in + add_out
            add_cache = cache_tok if (in_tok is not None or cache_tok > 0) else 0

            entry["today"]["input_tokens"] += add_in
            entry["today"]["output_tokens"] += add_out
            entry["today"]["cached_tokens"] += add_cache
            entry["today"]["total_tokens"] += add_total
            if effective_caller == "shared":
                entry["today"]["shared_tokens"] += add_total

            entry["total"]["input_tokens"] += add_in
            entry["total"]["output_tokens"] += add_out
            entry["total"]["cached_tokens"] += add_cache
            entry["total"]["total_tokens"] += add_total

            caller_bucket = entry["by_caller"][effective_caller]
            caller_bucket["input_tokens"] += add_in
            caller_bucket["output_tokens"] += add_out
            caller_bucket["cached_tokens"] += add_cache
            caller_bucket["total_tokens"] += add_total
            caller_bucket["today_tokens"] += add_total

            _sync_today_to_daily(entry)
            data[canonical] = entry
            self._write_unlocked(data)
            return format_public_usage(canonical, entry)

    def reset_usage(self, model_key: str, *, scope: str = "all", policy: dict[str, Any] | None = None) -> dict[str, Any]:
        if scope not in {"all", "today"}:
            raise ValueError("usage_reset_scope_invalid")
        canonical = normalize_model_key(model_key)
        day = _utc_today()
        with file_transaction(self.lock_path):
            data = self._read_unlocked(day)
            entry = data.get(canonical) or _empty_entry(day)
            saved_limits = copy.deepcopy(entry.get("limits") or {})
            if scope == "all":
                entry = _empty_entry(day)
                entry["limits"].update(saved_limits)
            else:
                entry["today"] = _empty_today(day)
                entry["daily"] = [item for item in (entry.get("daily") or []) if item.get("day") != day]
                for caller in ("self", "shared"):
                    entry["by_caller"][caller]["day"] = day
                    entry["by_caller"][caller]["today_requests"] = 0
                    entry["by_caller"][caller]["today_tokens"] = 0
                entry["dispatched_tasks"] = []
                entry["completed_tasks"] = []
            data[canonical] = entry
            self._write_unlocked(data)
            return format_public_usage(canonical, entry, policy=policy)

    def configure_quota(
        self,
        model_key: str,
        limits: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        canonical = normalize_model_key(model_key)
        validated = validate_quota_limits(limits)
        day = _utc_today()
        with file_transaction(self.lock_path):
            data = self._read_unlocked(day)
            entry = data.get(canonical) or _empty_entry(day)
            entry.setdefault("limits", {})
            for key, val in validated.items():
                entry["limits"][key] = val
            data[canonical] = entry
            self._write_unlocked(data)
            return format_public_usage(canonical, entry, policy=policy)
