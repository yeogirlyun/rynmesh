from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from .data_policy import memory_blocked, merge_state
from .protocol import now as now_iso

AUTOMATION_UPDATE_TOOL = "ryn_automation_update"
AUTOMATION_TOOL_NAMES = {AUTOMATION_UPDATE_TOOL}

# Keep schema compact so ALL_INTERNAL_SPECS stays well under the 15000-byte pin floor in tests.
AUTOMATION_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": AUTOMATION_UPDATE_TOOL,
            "description": (
                "Manage scheduled tasks/reminders (automations). "
                "Default kind='heartbeat' to wake this chat; use 'cron' for a new chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "pause", "resume", "delete", "list"],
                    },
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["heartbeat", "cron"],
                    },
                    "repeat": {
                        "type": "string",
                        "enum": ["daily", "weekdays", "weekly", "hourly", "minutely", "once"],
                    },
                    "time": {"type": "string", "description": "24h HH:MM local time"},
                    "delay_minutes": {"type": "integer"},
                    "interval_minutes": {"type": "integer"},
                    "weekday": {
                        "type": "string",
                        "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                    },
                    "interval_hours": {"type": "integer"},
                    "rrule": {"type": "string"},
                    "desktop_access": {"type": "boolean"},
                    "notification_policy": {
                        "type": "string",
                        "enum": ["default", "failed_runs_only"],
                    },
                },
                "required": ["action"],
            },
        },
    }
]

WEEKDAY_ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
WEEKDAY_TO_INDEX = {code: idx for idx, code in enumerate(WEEKDAY_ORDER)}
INDEX_TO_WEEKDAY = dict(enumerate(WEEKDAY_ORDER))

WEEKDAY_ZH = {
    "MO": "周一",
    "TU": "周二",
    "WE": "周三",
    "TH": "周四",
    "FR": "周五",
    "SA": "周六",
    "SU": "周日",
}
WEEKDAY_EN = {
    "MO": "Mon",
    "TU": "Tue",
    "WE": "Wed",
    "TH": "Thu",
    "FR": "Fri",
    "SA": "Sat",
    "SU": "Sun",
}

INBOX_ITEM_RE = re.compile(
    r"::inbox-item\{([^}]*)\}",
    re.IGNORECASE | re.DOTALL,
)
INBOX_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
DECISION_BLOCK_RE = re.compile(
    r"<decision>\s*(\{.*?\})\s*</decision>",
    re.IGNORECASE | re.DOTALL,
)
HEARTBEAT_FENCED_RE = re.compile(
    r"```(?:xml)?\s*<heartbeat>[\s\S]*?</heartbeat>\s*```",
    re.IGNORECASE,
)
HEARTBEAT_BLOCK_RE = re.compile(
    r"<heartbeat(?:\s+[^>]*)?>[\s\S]*?</heartbeat>",
    re.IGNORECASE,
)
HEARTBEAT_DECISION_RE = re.compile(
    r"<decision>\s*(NOTIFY|DONT_NOTIFY)\s*</decision>",
    re.IGNORECASE,
)
HEARTBEAT_MESSAGE_RE = re.compile(
    r"<message>\s*([\s\S]*?)\s*</message>",
    re.IGNORECASE,
)


def format_heartbeat_user_message(
    automation_id: str,
    prompt_text: str = "",
    *,
    instructions: str | None = None,
    current_time_iso: str | None = None,
) -> str:
    ts = current_time_iso or now_iso()
    body = instructions if instructions is not None else prompt_text
    return (
        "<heartbeat>\n"
        f"  <automation_id>{automation_id}</automation_id>\n"
        f"  <current_time_iso>{ts}</current_time_iso>\n"
        "  <instructions>\n"
        f"{body}\n"
        "  </instructions>\n"
        "</heartbeat>"
    )


def parse_heartbeat_response(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {
            "has_heartbeat_tag": False,
            "decision": "",
            "message": "",
            "visible_text": "",
            "notification_message": None,
            "cleaned_text": "",
        }
    cdata_match = re.match(r"^<!\[CDATA\[\s*([\s\S]*?)\s*\]\]>$", raw)
    unwrapped = (cdata_match.group(1) if cdata_match else raw).strip()
    if unwrapped.upper() == "DONT_NOTIFY":
        return {
            "has_heartbeat_tag": True,
            "decision": "DONT_NOTIFY",
            "message": "",
            "visible_text": "",
            "notification_message": None,
            "cleaned_text": "",
        }
    blocks = list(HEARTBEAT_BLOCK_RE.finditer(raw))
    if not blocks:
        return {
            "has_heartbeat_tag": False,
            "decision": "",
            "message": "",
            "visible_text": raw,
            "notification_message": None,
            "cleaned_text": raw,
        }
    last_block = blocks[-1].group(0)
    decision_match = HEARTBEAT_DECISION_RE.search(last_block)
    decision_val = (
        ("NOTIFY" if decision_match.group(1).upper() == "NOTIFY" else "DONT_NOTIFY")
        if decision_match
        else "NOTIFY"
    )
    msg_match = HEARTBEAT_MESSAGE_RE.search(last_block)
    notif_msg = msg_match.group(1).strip() if msg_match else ""
    if notif_msg:
        inner_cdata = re.match(r"^<!\[CDATA\[\s*([\s\S]*?)\s*\]\]>$", notif_msg)
        if inner_cdata:
            notif_msg = inner_cdata.group(1).strip()
    visible = HEARTBEAT_FENCED_RE.sub("", raw)
    visible = HEARTBEAT_BLOCK_RE.sub("", visible).strip()
    return {
        "has_heartbeat_tag": True,
        "decision": decision_val,
        "message": notif_msg,
        "visible_text": visible,
        "notification_message": notif_msg if notif_msg else None,
        "cleaned_text": visible,
    }


MAX_RUNS_PER_AUTOMATION = 50
ATTENTION_TIMEOUT_MS = 30 * 60 * 1000
LOST_TASK_TIMEOUT_MS = 10 * 60 * 1000
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
ATTENTION_TASK_STATUSES = frozenset({"waiting_input", "waiting_approval", "interrupted", "paused"})
AUTOMATION_RUN_STATUSES = frozenset(
    {
        "IN_PROGRESS",
        "ACCEPTED",
        "ARCHIVED",
        "FAILED",
        "CANCELLED",
        "NEEDS_ATTENTION",
        "SKIPPED_OVERLAP",
        "SKIPPED",
        "LOST",
    }
)


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_iso_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed_dt = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=dt.timezone.utc)
    return int(parsed_dt.timestamp() * 1000)


def ms_to_iso(ts_ms: int | None) -> str:
    if not ts_ms or ts_ms <= 0:
        return ""
    return (
        dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _local_tz() -> dt.tzinfo:
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def ensure_automation_state(data: dict[str, Any]) -> None:
    automations = data.get("automations")
    if not isinstance(automations, dict):
        data["automations"] = {}
    runs = data.get("automation_runs")
    if not isinstance(runs, dict):
        data["automation_runs"] = {}


def automations_root(store: Any) -> Path:
    from .store import AgentError

    path = Path(store.root).resolve() / "automations"
    if path.resolve() != path:
        raise AgentError("agent_invalid_automation_path")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validated_automation_id(automation_id: str) -> str:
    from .store import AgentError

    value = str(automation_id or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}", value)
        or value.endswith(".")
        or value.split(".", 1)[0].upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        raise AgentError("agent_invalid_automation_id")
    return value


def automation_dir(store: Any, automation_id: str) -> Path:
    from .store import AgentError

    safe_id = _validated_automation_id(automation_id)
    root = automations_root(store)
    target = root / safe_id
    resolved = target.resolve()
    if resolved != target or resolved.parent != root:
        raise AgentError("agent_invalid_automation_path")
    return target


def automation_toml_path(store: Any, automation_id: str) -> Path:
    return automation_dir(store, automation_id) / "automation.toml"


def automation_memory_path(store: Any, automation_id: str) -> Path:
    return automation_dir(store, automation_id) / "memory.md"


def ensure_jitter_salt(store: Any) -> str:
    salt_file = automations_root(store) / ".run-jitter-salt"
    try:
        if salt_file.exists():
            val = salt_file.read_text(encoding="utf-8").strip()
            if val:
                return val
    except Exception:
        pass
    val = secrets.token_hex(16)
    try:
        salt_file.write_text(val + "\n", encoding="utf-8")
    except Exception:
        pass
    return val


def parse_hhmm(value: str | None, default_hour: int = 9, default_minute: int = 0) -> tuple[int, int]:
    raw = str(value or "").strip()
    m = re.match(r"^(\d{1,2})\s*:\s*(\d{1,2})$", raw)
    if m:
        hour = max(0, min(23, int(m.group(1))))
        minute = max(0, min(59, int(m.group(2))))
        return hour, minute
    return default_hour, default_minute


def format_hhmm(hour: int, minute: int) -> str:
    return f"{max(0, min(23, int(hour))):02d}:{max(0, min(59, int(minute))):02d}"


def build_rrule(
    *,
    repeat: str | None = None,
    time_str: str | None = None,
    weekday: str | None = None,
    weekdays: list[str] | None = None,
    interval_hours: int | None = None,
    interval_minutes: int | None = None,
    delay_minutes: int | None = None,
    raw_rrule: str | None = None,
) -> str:
    if raw_rrule and str(raw_rrule).strip():
        cleaned = str(raw_rrule).strip()
        if cleaned.upper().startswith("RRULE:"):
            cleaned = cleaned[6:].strip()
        if "FREQ=" in cleaned.upper():
            return cleaned.upper()

    if delay_minutes is not None and int(delay_minutes or 0) > 0 and not repeat:
        mode = "once"
    else:
        mode = str(repeat or "daily").strip().lower()

    if delay_minutes is not None and int(delay_minutes or 0) > 0 and mode == "once":
        target_dt = dt.datetime.now(_local_tz()) + dt.timedelta(minutes=max(1, int(delay_minutes)))
        return (
            f"FREQ=DAILY;BYHOUR={target_dt.hour};BYMINUTE={target_dt.minute};"
            f"BYSECOND={target_dt.second};COUNT=1"
        )

    hour, minute = parse_hhmm(time_str, default_hour=9, default_minute=0)

    if mode == "minutely":
        interval = max(1, min(1440, int(interval_minutes or 5)))
        return f"FREQ=MINUTELY;INTERVAL={interval};BYSECOND=0"

    if mode == "hourly":
        interval = max(1, min(24, int(interval_hours or 1)))
        return f"FREQ=HOURLY;INTERVAL={interval};BYMINUTE={minute};BYSECOND=0"

    if mode == "weekdays":
        return f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"

    if mode == "weekly":
        days: list[str] = []
        if isinstance(weekdays, list):
            for d in weekdays:
                code = str(d or "").strip().upper()
                if code in WEEKDAY_TO_INDEX and code not in days:
                    days.append(code)
        single = str(weekday or "").strip().upper()
        if single in WEEKDAY_TO_INDEX and single not in days:
            days.append(single)
        if not days:
            local_now = dt.datetime.now(_local_tz())
            days = [INDEX_TO_WEEKDAY.get(local_now.weekday(), "MO")]
        byday = ",".join(days)
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"

    if mode == "once":
        return f"FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0;COUNT=1"

    return f"FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"


def parse_rrule_parts(rrule_str: str | None) -> dict[str, Any]:
    raw = str(rrule_str or "").strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:].strip()
    parts: dict[str, str] = {}
    for token in raw.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        parts[k.strip().upper()] = v.strip().upper()

    freq = parts.get("FREQ", "DAILY")
    if freq not in {"DAILY", "WEEKLY", "HOURLY", "MINUTELY"}:
        freq = "DAILY"

    def _int_part(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(parts.get(key, default))))
        except Exception:
            return default

    byhour = _int_part("BYHOUR", 9, 0, 23)
    byminute = _int_part("BYMINUTE", 0, 0, 59)
    bysecond = _int_part("BYSECOND", 0, 0, 59)
    interval = _int_part("INTERVAL", 1, 1, 1440)
    count = _int_part("COUNT", 0, 0, 10000)

    byday_raw = parts.get("BYDAY", "")
    byday: list[str] = []
    if byday_raw:
        for item in byday_raw.split(","):
            code = item.strip().upper()
            if code in WEEKDAY_TO_INDEX and code not in byday:
                byday.append(code)

    if freq == "MINUTELY":
        repeat = "minutely"
    elif freq == "HOURLY":
        repeat = "hourly"
    elif count == 1:
        repeat = "once"
    elif freq == "WEEKLY" or byday:
        if set(byday) == {"MO", "TU", "WE", "TH", "FR"} and len(byday) == 5:
            repeat = "weekdays"
        elif set(byday) == set(WEEKDAY_ORDER):
            repeat = "daily"
        else:
            repeat = "weekly"
    else:
        repeat = "daily"

    primary_weekday = byday[0] if byday else "MO"

    return {
        "freq": freq,
        "byhour": byhour,
        "byminute": byminute,
        "bysecond": bysecond,
        "interval": interval,
        "count": count,
        "byday": byday,
        "repeat": repeat,
        "time": format_hhmm(byhour, byminute),
        "weekday": primary_weekday,
        "interval_hours": interval if freq == "HOURLY" else 1,
        "interval_minutes": interval if freq == "MINUTELY" else 15,
    }


def format_schedule_labels(rrule_str: str | None) -> dict[str, Any]:
    parsed = parse_rrule_parts(rrule_str)
    repeat = parsed["repeat"]
    time_str = parsed["time"]
    byday: list[str] = parsed["byday"]
    interval = parsed["interval"]

    if repeat == "minutely":
        if interval <= 1:
            zh = "每分钟"
            en = "Every minute"
        else:
            zh = f"每 {interval} 分钟"
            en = f"Every {interval} minutes"
    elif repeat == "hourly":
        if interval <= 1:
            zh = "每小时"
            en = "Every hour"
        else:
            zh = f"每 {interval} 小时"
            en = f"Every {interval} hours"
    elif repeat == "once":
        zh = f"仅一次 {time_str}"
        en = f"Once at {time_str}"
    elif repeat == "weekdays":
        zh = f"工作日 {time_str}"
        en = f"Weekdays at {time_str}"
    elif repeat == "weekly":
        days_zh = "、".join(WEEKDAY_ZH.get(d, d) for d in (byday or ["MO"]))
        days_en = ", ".join(WEEKDAY_EN.get(d, d) for d in (byday or ["MO"]))
        if len(byday) == 1:
            zh = f"每{days_zh} {time_str}"
            en = f"Every {days_en} at {time_str}"
        else:
            zh = f"{days_zh} {time_str}"
            en = f"{days_en} at {time_str}"
    else:
        zh = f"每天 {time_str}"
        en = f"Daily at {time_str}"

    return {
        "repeat": repeat,
        "time": time_str,
        "weekday": parsed["weekday"],
        "weekdays": byday or ([parsed["weekday"]] if repeat == "weekly" else []),
        "interval_hours": parsed["interval_hours"],
        "interval_minutes": parsed["interval_minutes"],
        "schedule_label_zh": zh,
        "schedule_label_en": en,
        "schedule_label": zh,
    }


def compute_deterministic_jitter_ms(
    *,
    salt: str,
    automation_id: str,
    slot_ms: int,
    rrule_str: str,
) -> int:
    parsed = parse_rrule_parts(rrule_str)
    # Exact clock-time reminders (daily/weekdays/weekly/once/minutely) fire strictly on time (0ms jitter).
    # Hourly background checks get up to 45s deterministic jitter like Codex.
    if parsed["freq"] != "HOURLY":
        return 0
    window_ms = min(45_000, max(0, parsed["interval"] * 15_000))
    if window_ms <= 0:
        return 0
    digest = hashlib.sha256(f"{salt}:{automation_id}:{slot_ms}".encode("utf-8")).digest()
    raw_int = int.from_bytes(digest[:4], "big")
    return raw_int % window_ms


def compute_next_run_at(
    rrule_str: str | None,
    *,
    after_ms: int | None = None,
    tz: dt.tzinfo | None = None,
    salt: str = "",
    automation_id: str = "",
    run_count: int = 0,
) -> int | None:
    parsed = parse_rrule_parts(rrule_str)
    if parsed["count"] == 1 and run_count >= 1:
        return None

    tzinfo = tz or _local_tz()
    base_ms = int(after_ms if after_ms is not None else now_ms())
    base_dt = dt.datetime.fromtimestamp(base_ms / 1000.0, tz=tzinfo)

    freq = parsed["freq"]
    byhour = parsed["byhour"]
    byminute = parsed["byminute"]
    bysecond = parsed["bysecond"]
    interval = parsed["interval"]
    byday = parsed["byday"]

    if freq == "MINUTELY":
        candidate = base_dt.replace(second=bysecond, microsecond=0)
        while int(candidate.timestamp() * 1000) <= base_ms:
            candidate = candidate + dt.timedelta(minutes=max(1, interval))
        slot_ms = int(candidate.timestamp() * 1000)
    elif freq == "HOURLY":
        candidate = base_dt.replace(minute=byminute, second=bysecond, microsecond=0)
        while int(candidate.timestamp() * 1000) <= base_ms:
            candidate = candidate + dt.timedelta(hours=max(1, interval))
        slot_ms = int(candidate.timestamp() * 1000)
    elif freq == "WEEKLY" or byday:
        allowed_weekdays = {WEEKDAY_TO_INDEX[d] for d in byday if d in WEEKDAY_TO_INDEX}
        if not allowed_weekdays:
            allowed_weekdays = {base_dt.weekday()}
        candidate_date = base_dt.date()
        slot_ms = 0
        for offset in range(0, 15):
            day = candidate_date + dt.timedelta(days=offset)
            if day.weekday() not in allowed_weekdays:
                continue
            candidate_dt = dt.datetime(
                day.year,
                day.month,
                day.day,
                byhour,
                byminute,
                bysecond,
                tzinfo=tzinfo,
            )
            cand_ms = int(candidate_dt.timestamp() * 1000)
            if cand_ms > base_ms:
                slot_ms = cand_ms
                break
        if slot_ms <= 0:
            fallback_dt = base_dt + dt.timedelta(days=7)
            slot_ms = int(fallback_dt.timestamp() * 1000)
    else:
        candidate = base_dt.replace(hour=byhour, minute=byminute, second=bysecond, microsecond=0)
        if int(candidate.timestamp() * 1000) <= base_ms:
            candidate = candidate + dt.timedelta(days=max(1, interval))
        slot_ms = int(candidate.timestamp() * 1000)

    jitter_ms = compute_deterministic_jitter_ms(
        salt=salt,
        automation_id=automation_id,
        slot_ms=slot_ms,
        rrule_str=str(rrule_str or ""),
    )
    return slot_ms + jitter_ms


def compute_missed_runs(
    rrule_str: str | None,
    next_run_at_ms: int | float | None,
    current_ms: int,
    *,
    tz: dt.tzinfo | None = None,
) -> int:
    if not isinstance(next_run_at_ms, (int, float)):
        return 0
    due_ms = int(next_run_at_ms)
    if due_ms <= 0 or current_ms <= due_ms:
        return 0
    parsed = parse_rrule_parts(rrule_str)
    if parsed["count"] == 1 or parsed["repeat"] == "once":
        return 0
    elapsed = current_ms - due_ms
    freq = parsed["freq"]
    interval = max(1, int(parsed["interval"]))
    byday = parsed["byday"]
    if freq == "MINUTELY":
        period_ms = interval * 60_000
        return int(elapsed // period_ms) if elapsed >= period_ms else 0
    if freq == "HOURLY":
        period_ms = interval * 3_600_000
        return int(elapsed // period_ms) if elapsed >= period_ms else 0
    if freq == "DAILY" and not byday:
        period_ms = interval * 86_400_000
        return int(elapsed // period_ms) if elapsed >= period_ms else 0
    missed = 0
    cursor = due_ms
    for _ in range(1000):
        nxt = compute_next_run_at(
            rrule_str,
            after_ms=cursor,
            tz=tz,
            salt="",
            automation_id="",
            run_count=0,
        )
        if nxt is None or nxt <= cursor or nxt > current_ms:
            break
        missed += 1
        cursor = nxt
    return missed


def prune_automation_runs_in_tx(
    data: dict[str, Any],
    automation_id: str,
    *,
    max_runs: int = MAX_RUNS_PER_AUTOMATION,
) -> None:
    target_id = str(automation_id or "").strip()
    runs = data.get("automation_runs")
    if not target_id or not isinstance(runs, dict):
        return
    matching: list[tuple[int, int, str]] = []
    for idx, (rid, rec) in enumerate(runs.items()):
        if isinstance(rec, dict) and str(rec.get("automation_id") or "") == target_id:
            ts = parse_iso_ms(rec.get("created_at")) or 0
            matching.append((ts, idx, str(rid)))
    if len(matching) <= max_runs:
        return
    matching.sort(key=lambda item: (item[0], item[1]))
    for _ts, _idx, rid in matching[: len(matching) - max_runs]:
        runs.pop(rid, None)


def _toml_quote(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def serialize_automation_toml(item: dict[str, Any]) -> str:
    lines = [
        "version = 1",
        f"id = {_toml_quote(str(item.get('id') or ''))}",
        f"name = {_toml_quote(str(item.get('name') or ''))}",
        f"prompt = {_toml_quote(str(item.get('prompt') or ''))}",
        f"status = {_toml_quote(str(item.get('status') or 'ACTIVE'))}",
        f"rrule = {_toml_quote(str(item.get('rrule') or ''))}",
        f"kind = {_toml_quote(str(item.get('kind') or 'cron'))}",
        f"desktop_access = {'true' if bool(item.get('desktop_access', True)) else 'false'}",
        f"workspace_root = {_toml_quote(str(item.get('workspace_root') or ''))}",
        f"target_task_id = {_toml_quote(str(item.get('target_task_id') or ''))}",
        f"dedicated_chat = {'true' if bool(item.get('dedicated_chat', False)) else 'false'}",
        f"execution_environment = {_toml_quote(str(item.get('execution_environment') or 'local'))}",
        f"notification_policy = {_toml_quote(str(item.get('notification_policy') or 'default'))}",
        f"auto_archive = {'true' if bool(item.get('auto_archive', False)) else 'false'}",
        f"created_at = {_toml_quote(str(item.get('created_at') or ''))}",
        f"updated_at = {_toml_quote(str(item.get('updated_at') or ''))}",
        f"data_policy_state_json = {_toml_quote(json.dumps(item.get('data_policy_state'), sort_keys=True))}",
        f"source_task_ids_json = {_toml_quote(json.dumps(item.get('source_task_ids') or []))}",
    ]
    return "\n".join(lines) + "\n"


def parse_automation_toml(text: str) -> dict[str, Any]:
    if tomllib is not None:
        try:
            loaded = tomllib.loads(text)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    # Fallback line parser if needed
    out: dict[str, Any] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip()
        if val in {"true", "false"}:
            out[key] = val == "true"
        elif val.isdigit():
            out[key] = int(val)
        elif val.startswith('"') and val.endswith('"'):
            try:
                out[key] = json.loads(val)
            except Exception:
                out[key] = val[1:-1]
        else:
            out[key] = val
    return out


def write_automation_files(store: Any, item: dict[str, Any]) -> None:
    auto_id = str(item.get("id") or "").strip()
    if not auto_id:
        return
    folder = automation_dir(store, auto_id)
    folder.mkdir(parents=True, exist_ok=True)
    toml_file = folder / "automation.toml"
    tmp_file = folder / f"automation.toml.{os.getpid()}.tmp"
    content = serialize_automation_toml(item)
    try:
        tmp_file.write_text(content, encoding="utf-8")
        tmp_file.replace(toml_file)
    except Exception:
        try:
            toml_file.write_text(content, encoding="utf-8")
        except Exception:
            pass

    mem_file = folder / "memory.md"
    if not memory_blocked(item) and not mem_file.exists():
        header = (
            f"# Automation Memory: {item.get('name') or auto_id}\n\n"
            f"- ID: `{auto_id}`\n"
            f"- Created: {item.get('created_at') or now_iso()}\n\n"
            "## Run Notes\n"
        )
        try:
            mem_file.write_text(header, encoding="utf-8")
        except Exception:
            pass


def delete_automation_files(store: Any, automation_id: str) -> None:
    auto_id = str(automation_id or "").strip()
    if not auto_id:
        return
    folder = automation_dir(store, auto_id)
    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def read_automation_memory(store: Any, automation_id: str, max_chars: int = 4000) -> str:
    mem_file = automation_memory_path(store, automation_id)
    try:
        if mem_file.exists():
            text = mem_file.read_text(encoding="utf-8").strip()
            if len(text) > max_chars:
                return text[-max_chars:]
            return text
    except Exception:
        pass
    return ""


def append_automation_memory_note(store: Any, automation_id: str, note: str) -> None:
    clean = str(note or "").strip()
    if not clean:
        return
    mem_file = automation_memory_path(store, automation_id)
    try:
        mem_file.parent.mkdir(parents=True, exist_ok=True)
        existing = mem_file.read_text(encoding="utf-8") if mem_file.exists() else ""
        entry = f"\n- [{now_iso()}] {clean}\n"
        mem_file.write_text(existing.rstrip() + entry, encoding="utf-8")
    except Exception:
        pass


def normalize_automation_record(
    store: Any,
    raw: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = dict(existing or {})
    incoming_policy = raw.get("data_policy_state")
    if incoming_policy is None and isinstance(raw.get("data_policy_state_json"), str):
        incoming_policy = json.loads(raw["data_policy_state_json"])
    policy_state = merge_state(base.get("data_policy_state"), incoming_policy)
    source_tasks = raw.get("source_task_ids") or []
    if isinstance(raw.get("source_task_ids_json"), str):
        source_tasks = json.loads(raw["source_task_ids_json"])
    source_tasks = sorted(set(base.get("source_task_ids") or []) | {item for item in source_tasks if isinstance(item, str) and item})
    now_str = now_iso()
    current_ms = now_ms()
    auto_id = str(raw.get("id") or base.get("id") or "").strip()
    if not auto_id:
        auto_id = f"auto-{current_ms}-{secrets.token_hex(3)}"
    auto_id = _validated_automation_id(auto_id)

    name = str(raw.get("name") if "name" in raw else base.get("name") or "").strip()
    prompt = str(raw.get("prompt") if "prompt" in raw else base.get("prompt") or "").strip()
    if not name and prompt:
        name = prompt.splitlines()[0][:28].strip()
    if not name:
        name = "定时任务"

    status = str(raw.get("status") if "status" in raw else base.get("status") or "ACTIVE").strip().upper()
    if status not in {"ACTIVE", "PAUSED"}:
        status = "ACTIVE"

    target_task_id = str(
        raw.get("target_task_id")
        if "target_task_id" in raw
        else base.get("target_task_id") or ""
    ).strip()

    default_kind = "heartbeat" if target_task_id else "cron"
    kind = str(raw.get("kind") if "kind" in raw else base.get("kind") or default_kind).strip().lower()
    if kind not in {"cron", "heartbeat"}:
        kind = default_kind

    if "dedicated_chat" in raw and raw.get("dedicated_chat") is not None:
        dedicated_chat = bool(raw.get("dedicated_chat"))
    else:
        dedicated_chat = bool(base.get("dedicated_chat", False))
        if kind != "heartbeat":
            dedicated_chat = False
        elif "target_task_id" in raw and target_task_id != str(base.get("target_task_id") or "").strip():
            dedicated_chat = False

    has_schedule_change = any(
        k in raw
        for k in (
            "rrule",
            "repeat",
            "time",
            "weekday",
            "weekdays",
            "interval_hours",
            "interval_minutes",
            "delay_minutes",
        )
    )
    if has_schedule_change or not base.get("rrule"):
        existing_parts = parse_rrule_parts(str(base.get("rrule") or ""))
        rrule_str = build_rrule(
            repeat=str(raw.get("repeat") or existing_parts["repeat"]),
            time_str=str(raw.get("time") or existing_parts["time"]),
            weekday=str(raw.get("weekday") or existing_parts["weekday"]),
            weekdays=(
                raw.get("weekdays")
                if isinstance(raw.get("weekdays"), list)
                else (
                    existing_parts["byday"]
                    if "weekday" not in raw and existing_parts.get("byday")
                    else None
                )
            ),
            interval_hours=int(raw.get("interval_hours") or existing_parts["interval_hours"]),
            interval_minutes=int(raw.get("interval_minutes") or existing_parts.get("interval_minutes", 5)),
            delay_minutes=int(raw.get("delay_minutes") or 0) if raw.get("delay_minutes") is not None else None,
            raw_rrule=str(raw.get("rrule") or "") if "rrule" in raw else None,
        )
    else:
        rrule_str = str(base.get("rrule") or build_rrule())

    desktop_access = bool(
        raw.get("desktop_access")
        if "desktop_access" in raw
        else base.get("desktop_access", True)
    )
    workspace_root = str(
        raw.get("workspace_root")
        if "workspace_root" in raw
        else base.get("workspace_root") or ""
    ).strip()
    execution_environment = str(
        raw.get("execution_environment")
        if "execution_environment" in raw
        else base.get("execution_environment") or "local"
    ).strip() or "local"

    notification_policy = str(
        raw.get("notification_policy")
        if "notification_policy" in raw
        else base.get("notification_policy") or "all_runs"
    ).strip()
    if notification_policy not in {"all_runs", "failed_runs_only", "never"}:
        notification_policy = "all_runs"

    labels = format_schedule_labels(rrule_str)
    if "auto_archive" in raw and raw.get("auto_archive") is not None:
        auto_archive = bool(raw.get("auto_archive"))
    elif "auto_archive" in base:
        auto_archive = bool(base.get("auto_archive"))
    else:
        auto_archive = labels["repeat"] == "once"

    run_count = int(raw.get("run_count", base.get("run_count", 0)) or 0)
    missed_runs = int(raw.get("missed_runs", base.get("missed_runs", 0)) or 0)
    last_run_at = raw.get("last_run_at", base.get("last_run_at"))
    last_run_status = str(raw.get("last_run_status", base.get("last_run_status") or "")).strip()
    last_run_summary = str(raw.get("last_run_summary", base.get("last_run_summary") or "")).strip()
    last_task_id = str(raw.get("last_task_id", base.get("last_task_id") or "")).strip()

    salt = ensure_jitter_salt(store)
    if status == "PAUSED":
        next_run_at = None
    elif "next_run_at" in raw and not has_schedule_change:
        next_run_at = raw.get("next_run_at")
    else:
        next_run_at = compute_next_run_at(
            rrule_str,
            after_ms=current_ms,
            salt=salt,
            automation_id=auto_id,
            run_count=run_count,
        )

    created_at = str(base.get("created_at") or raw.get("created_at") or now_str)
    updated_at = now_str

    record: dict[str, Any] = {
        "id": auto_id,
        "name": name,
        "prompt": prompt,
        "status": status,
        "rrule": rrule_str,
        "kind": kind,
        "repeat": labels["repeat"],
        "time": labels["time"],
        "weekday": labels["weekday"],
        "weekdays": labels["weekdays"],
        "interval_hours": labels["interval_hours"],
        "interval_minutes": labels.get("interval_minutes", 5),
        "schedule_label": labels["schedule_label"],
        "schedule_label_zh": labels["schedule_label_zh"],
        "schedule_label_en": labels["schedule_label_en"],
        "desktop_access": desktop_access,
        "workspace_root": workspace_root,
        "target_task_id": target_task_id,
        "dedicated_chat": dedicated_chat,
        "execution_environment": execution_environment,
        "notification_policy": notification_policy,
        "auto_archive": auto_archive,
        "next_run_at": next_run_at,
        "next_run_at_iso": ms_to_iso(next_run_at),
        "last_run_at": last_run_at,
        "last_run_at_iso": ms_to_iso(last_run_at if isinstance(last_run_at, int) else None),
        "last_run_status": last_run_status,
        "last_run_summary": last_run_summary,
        "last_task_id": last_task_id,
        "run_count": run_count,
        "missed_runs": missed_runs,
        "memory_path": str(automation_memory_path(store, auto_id)),
        "toml_path": str(automation_toml_path(store, auto_id)),
        "created_at": created_at,
        "updated_at": updated_at,
        "data_policy_state": policy_state,
        "source_task_ids": source_tasks,
    }
    return record


def public_automation(store: Any, item: dict[str, Any]) -> dict[str, Any]:
    labels = format_schedule_labels(str(item.get("rrule") or ""))
    next_ms = item.get("next_run_at")
    last_ms = item.get("last_run_at")
    auto_id = str(item.get("id") or "")
    return {
        "id": auto_id,
        "name": str(item.get("name") or "定时任务"),
        "prompt": str(item.get("prompt") or ""),
        "status": str(item.get("status") or "ACTIVE"),
        "rrule": str(item.get("rrule") or ""),
        "kind": str(item.get("kind") or "cron"),
        "repeat": labels["repeat"],
        "time": labels["time"],
        "weekday": labels["weekday"],
        "weekdays": labels["weekdays"],
        "interval_hours": labels["interval_hours"],
        "interval_minutes": labels.get("interval_minutes", 5),
        "schedule_label": labels["schedule_label"],
        "schedule_label_zh": labels["schedule_label_zh"],
        "schedule_label_en": labels["schedule_label_en"],
        "desktop_access": bool(item.get("desktop_access", True)),
        "workspace_root": str(item.get("workspace_root") or ""),
        "target_task_id": str(item.get("target_task_id") or ""),
        "dedicated_chat": bool(item.get("dedicated_chat", False)),
        "execution_environment": str(item.get("execution_environment") or "local"),
        "notification_policy": str(item.get("notification_policy") or "all_runs"),
        "auto_archive": bool(item.get("auto_archive", False)),
        "next_run_at": next_ms if isinstance(next_ms, int) else None,
        "next_run_at_iso": ms_to_iso(next_ms if isinstance(next_ms, int) else None),
        "last_run_at": last_ms if isinstance(last_ms, int) else None,
        "last_run_at_iso": ms_to_iso(last_ms if isinstance(last_ms, int) else None),
        "last_run_status": str(item.get("last_run_status") or ""),
        "last_run_summary": str(item.get("last_run_summary") or ""),
        "last_task_id": str(item.get("last_task_id") or ""),
        "run_count": int(item.get("run_count") or 0),
        "missed_runs": int(item.get("missed_runs") or 0),
        "memory_path": str(automation_memory_path(store, auto_id)),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
    }


def sync_automations_from_disk_in_tx(store: Any, data: dict[str, Any]) -> bool:
    ensure_automation_state(data)
    return sync_automation_files(store, data["automations"], check_only=False)


def sync_automation_files(store: Any, automations: dict[str, Any], *, check_only: bool) -> bool:
    root = automations_root(store)
    changed = False

    # 1. Load any TOML files created or edited on disk
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        toml_file = child / "automation.toml"
        if not toml_file.exists():
            continue
        try:
            parsed = parse_automation_toml(toml_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        auto_id = str(parsed.get("id") or child.name).strip()
        if not auto_id:
            continue
        existing = automations.get(auto_id)
        if not isinstance(existing, dict):
            if check_only:
                return True
            record = normalize_automation_record(store, {**parsed, "id": auto_id})
            automations[auto_id] = record
            write_automation_files(store, record)
            changed = True
        else:
            # Check if key fields in TOML differ from store
            for key in (
                "name",
                "prompt",
                "status",
                "rrule",
                "kind",
                "desktop_access",
                "target_task_id",
                "dedicated_chat",
                "notification_policy",
                "auto_archive",
            ):
                existing_val = bool(existing.get(key, False)) if key == "dedicated_chat" else existing.get(key)
                if key in parsed and parsed[key] != existing_val:
                    if check_only:
                        return True
                    merged = normalize_automation_record(store, parsed, existing=existing)
                    automations[auto_id] = merged
                    changed = True
                    break

    # 2. Ensure all store records have their TOML + memory.md on disk
    for auto_id, item in list(automations.items()):
        if not isinstance(item, dict):
            continue
        toml_file = automation_toml_path(store, auto_id)
        if not toml_file.exists():
            if check_only:
                return True
            write_automation_files(store, item)

    return changed


def create_or_update_automation_in_tx(
    store: Any,
    data: dict[str, Any],
    payload: dict[str, Any],
    *,
    automation_id: str | None = None,
    source_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_automation_state(data)
    automations: dict[str, Any] = data["automations"]
    target_id = str(automation_id or payload.get("id") or "").strip()
    existing = automations.get(target_id) if target_id else None
    record = normalize_automation_record(
        store,
        {**payload, **({"id": target_id} if target_id else {})},
        existing=existing if isinstance(existing, dict) else None,
    )
    if isinstance(source_row, dict):
        record["data_policy_state"] = merge_state(record.get("data_policy_state"), source_row.get("data_policy_state"))
        if source_row.get("id"):
            record["source_task_ids"] = sorted(set(record.get("source_task_ids") or []) | {source_row["id"]})
        _absorb_automation_policy_in_tx(data, source_row, record)
    automations[record["id"]] = record
    if isinstance(source_row, dict):
        inherit_task_policy_in_tx(data, source_row)
    write_automation_files(store, record)
    return public_automation(store, record)


def inherit_task_policy_in_tx(data: dict[str, Any], row: dict[str, Any]) -> None:
    """Tighten scheduled derivatives before a source task can receive new data."""
    from .memory import invalidate_task_policy_in_tx

    pending = [row]
    while pending:
        source = pending.pop()
        source_state = source.get("data_policy_state")
        if not source_state:
            continue
        for item in (data.get("automations") or {}).values():
            if not isinstance(item, dict) or not (
                source.get("id") in (item.get("source_task_ids") or []) or source.get("automation_id") == item.get("id")
            ):
                continue
            item["data_policy_state"] = merge_state(item.get("data_policy_state"), source_state)
            for target in (data.get("tasks") or {}).values():
                if not isinstance(target, dict) or target.get("automation_id") != item.get("id"):
                    continue
                inherited = merge_state(target.get("data_policy_state"), item["data_policy_state"])
                if inherited != target.get("data_policy_state"):
                    target["data_policy_state"] = inherited
                    invalidate_task_policy_in_tx(data, target)
                    pending.append(target)


def _absorb_automation_policy_in_tx(data: dict[str, Any], row: dict[str, Any], item: dict[str, Any]) -> None:
    from .memory import invalidate_task_policy_in_tx

    row["data_policy_state"] = merge_state(row.get("data_policy_state"), item.get("data_policy_state"))
    invalidate_task_policy_in_tx(data, row)


def delete_automation_in_tx(store: Any, data: dict[str, Any], automation_id: str) -> bool:
    ensure_automation_state(data)
    target_id = str(automation_id or "").strip()
    if not target_id:
        return False
    _validated_automation_id(target_id)
    if target_id not in data["automations"]:
        return False
    delete_automation_files(store, target_id)
    removed = data["automations"].pop(target_id, None)
    runs = data.get("automation_runs")
    if isinstance(runs, dict):
        for rid in [
            rk
            for rk, rv in runs.items()
            if isinstance(rv, dict) and str(rv.get("automation_id") or "") == target_id
        ]:
            runs.pop(rid, None)
    return removed is not None


def list_automations_in_tx(store: Any, data: dict[str, Any]) -> dict[str, Any]:
    ensure_automation_state(data)
    sync_automations_from_disk_in_tx(store, data)
    items = [
        public_automation(store, item)
        for item in data["automations"].values()
        if isinstance(item, dict)
    ]
    items.sort(
        key=lambda x: (
            0 if x.get("status") == "ACTIVE" else 1,
            x.get("next_run_at") or 9999999999999,
            str(x.get("created_at") or ""),
        )
    )
    runs = [
        dict(run)
        for run in data["automation_runs"].values()
        if isinstance(run, dict)
    ]
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return {
        "items": items,
        "runs": runs[:30],
    }


def execute_automation_tool_call_in_tx(
    store: Any,
    data: dict[str, Any],
    row: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    ensure_automation_state(data)
    raw_action = str(args.get("action") or args.get("mode") or "create").strip().lower()
    action_map = {
        "suggested_create": "create",
        "suggested_update": "update",
        "view": "list",
    }
    action = action_map.get(raw_action, raw_action)

    automations: dict[str, Any] = data["automations"]
    target_id = str(args.get("id") or "").strip()
    target_name = str(args.get("name") or "").strip()

    # Allow matching by name or current turn's automation_id if id wasn't provided
    if not target_id and target_name and action in {"update", "pause", "resume", "delete"}:
        for aid, item in automations.items():
            if isinstance(item, dict) and str(item.get("name") or "").strip() == target_name:
                target_id = aid
                break
    if not target_id and action in {"update", "pause", "resume", "delete"}:
        active_auto_id = str(row.get("automation_id") or "").strip()
        if active_auto_id and active_auto_id in automations:
            target_id = active_auto_id

    if action == "list":
        listed = list_automations_in_tx(store, data)
        for item in data["automations"].values():
            if isinstance(item, dict):
                _absorb_automation_policy_in_tx(data, row, item)
        inherit_task_policy_in_tx(data, row)
        return {
            "ok": True,
            "action": "list",
            "items": listed["items"],
            "count": len(listed["items"]),
        }

    if action == "delete":
        if not target_id:
            return {"ok": False, "error": "Missing automation id to delete."}
        existing = automations.get(target_id)
        if isinstance(existing, dict):
            _absorb_automation_policy_in_tx(data, row, existing)
            inherit_task_policy_in_tx(data, row)
        deleted = delete_automation_in_tx(store, data, target_id)
        return {
            "ok": deleted,
            "action": "delete",
            "id": target_id,
            "name": str((existing or {}).get("name") or target_name or target_id),
        }

    if action in {"pause", "resume"}:
        if not target_id or target_id not in automations:
            return {"ok": False, "error": f"Automation '{target_id or target_name}' not found."}
        new_status = "PAUSED" if action == "pause" else "ACTIVE"
        updated = create_or_update_automation_in_tx(
            store,
            data,
            {"status": new_status},
            automation_id=target_id,
            source_row=row,
        )
        return {
            "ok": True,
            "action": action,
            "automation": updated,
            **updated,
        }

    # create or update
    local_exec = row.get("local_execution") if isinstance(row.get("local_execution"), dict) else {}
    default_workspace = str(local_exec.get("workspace_root") or "").strip()
    payload: dict[str, Any] = {}
    for key in (
        "name",
        "prompt",
        "repeat",
        "time",
        "weekday",
        "weekdays",
        "interval_hours",
        "interval_minutes",
        "delay_minutes",
        "rrule",
        "status",
        "kind",
        "desktop_access",
        "workspace_root",
        "target_task_id",
        "notification_policy",
        "auto_archive",
    ):
        if key in args and args[key] is not None:
            payload[key] = args[key]

    if "workspace_root" not in payload and default_workspace:
        payload["workspace_root"] = default_workspace
    if "desktop_access" not in payload:
        payload["desktop_access"] = True

    is_update = action == "update" and bool(target_id)
    if not is_update:
        row_task_id = str(row.get("id") or "").strip()
        if "kind" not in payload:
            payload["kind"] = "heartbeat" if row_task_id else "cron"
        if payload.get("kind") == "heartbeat" and "target_task_id" not in payload and row_task_id:
            payload["target_task_id"] = row_task_id

    saved = create_or_update_automation_in_tx(
        store,
        data,
        payload,
        automation_id=target_id if is_update else None,
        source_row=row,
    )
    return {
        "ok": True,
        "action": "update" if is_update else "create",
        "automation": saved,
        **saved,
    }


def automation_developer_instructions(store: Any, row: dict[str, Any]) -> str:
    if str(row.get("source") or "") != "automation":
        return ""
    if not str(row.get("automation_run_id") or "").strip():
        return ""
    auto_id = str(row.get("automation_id") or "").strip()
    if not auto_id:
        return ""
    kind = str(row.get("automation_kind") or "").strip().lower()
    mem_path = automation_memory_path(store, auto_id)
    blocked = memory_blocked(row)
    mem_preview = "" if blocked else read_automation_memory(store, auto_id, max_chars=2000)

    if kind == "heartbeat":
        lines = [
            "# Heartbeat wakeups",
            "User messages wrapped in `<heartbeat>` tags are scheduled wakeups for this thread.",
            "Each `<heartbeat>` message includes `<automation_id>`, `<current_time_iso>`, and `<instructions>`.",
            "The user is not interactively watching the screen right now. Execute the scheduled instructions or tools directly without asking for confirmation.",
            "Long-term memory is disabled for this task. Do not read or write automation memory files." if blocked else f"Persistent memory file across runs: {mem_path}",
            "",
            "# Stopping completed reminders or monitors",
            "If this wakeup is a one-off reminder (e.g. 'remind me in 10 minutes') or a monitor whose condition has now been satisfied ('notify me once X finishes'), call `ryn_automation_update` with `action=\"delete\"` (or `action=\"pause\"`) and `id=\"<automation_id>\"` before finishing so it does not keep firing.",
            "",
            "# Heartbeat response format",
            "Write normal visible text for the user first, then end your final response with a `<heartbeat>` block:",
            "<heartbeat>",
            f"<automation_id>{auto_id}</automation_id>",
            "<decision>NOTIFY or DONT_NOTIFY</decision>",
            "<message>concise summary or notification banner text</message>",
            "</heartbeat>",
            "- Use `NOTIFY` for reminders, completed scheduled actions, or monitors that found something the user should see.",
            "- Use `DONT_NOTIFY` only when polling/monitoring a condition and nothing noteworthy happened (keeps the thread quiet and unread-free).",
            '- You may also include `::inbox-item{title="..." summary="..."}` if you want to set a custom run title.',
        ]
        if mem_preview:
            lines.append("\nCurrent memory.md contents:\n```markdown\n" + mem_preview + "\n```")
        return "\n".join(lines)

    lines = [
        "<automation_context>",
        f"mode: Automation (automation_id={auto_id})",
        "The user is not interactively watching the screen right now. Execute the scheduled instructions directly without asking for confirmation.",
        "Long-term memory is disabled for this task. Do not read or write automation memory files." if blocked else f"memory_file: {mem_path}",
        "" if blocked else "Before starting work, review the memory notes below to understand what happened on previous runs.",
        "" if blocked else "If you need to record state for the next scheduled run, update that memory.md file or include a concise summary.",
        'At the end of your final response, include `::inbox-item{title="..." summary="..."}` to summarize the outcome.',
    ]
    if mem_preview:
        lines.append("\nCurrent memory.md contents:\n```markdown\n" + mem_preview + "\n```")
    lines.append("</automation_context>")
    return "\n".join(lines)


def parse_automation_completion_output(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    inbox_title = ""
    inbox_summary = ""
    match = INBOX_ITEM_RE.search(raw)
    if match:
        attrs = dict(INBOX_ATTR_RE.findall(match.group(1)))
        inbox_title = str(attrs.get("title") or "").strip()
        inbox_summary = str(attrs.get("summary") or "").strip()

    hb = parse_heartbeat_response(raw)

    decision: dict[str, Any] = {}
    d_match = DECISION_BLOCK_RE.search(raw)
    if d_match:
        try:
            loaded = json.loads(d_match.group(1))
            if isinstance(loaded, dict):
                decision = loaded
        except Exception:
            pass
    if hb["decision"] and "heartbeat_decision" not in decision:
        decision["heartbeat_decision"] = hb["decision"]

    cleaned = INBOX_ITEM_RE.sub("", hb["cleaned_text"]).strip()
    summary = (
        inbox_summary
        or hb["message"]
        or (cleaned.splitlines()[0][:140].strip() if cleaned else "")
    )
    return {
        "inbox_title": inbox_title,
        "inbox_summary": summary,
        "decision": decision,
        "heartbeat": hb,
        "cleaned_text": cleaned,
    }


def _extract_row_error(row: dict[str, Any], fallback: str) -> str:
    err = row.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or fallback)
    if isinstance(err, str) and err.strip():
        return err.strip()
    return fallback


def _latest_turn_assistant_message(
    messages: list[Any],
    run_id: str,
) -> tuple[dict[str, Any] | None, str]:
    last_user_idx = -1
    if run_id:
        hb_msg_id = f"msg-hb-{run_id}"
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if isinstance(msg, dict) and msg.get("message_id") == hb_msg_id:
                last_user_idx = idx
                break
    if last_user_idx < 0:
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            ui = msg.get("ui")
            if isinstance(ui, dict) and ui.get("kind") == "subagent_notification":
                continue
            last_user_idx = idx
            break
    tail = messages[last_user_idx + 1 :] if last_user_idx >= 0 else []
    for msg in reversed(tail):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            cand = str(msg.get("content") or "").strip()
            if cand:
                return msg, cand
    return None, ""


def settle_lost_automation_run_in_tx(
    store: Any,
    data: dict[str, Any],
    run_rec: dict[str, Any],
    *,
    now_str: str | None = None,
) -> None:
    ensure_automation_state(data)
    now_s = now_str or now_iso()
    auto_id = str(run_rec.get("automation_id") or "").strip()
    run_rec["status"] = "LOST"
    run_rec["error"] = "task_removed"
    run_rec["inbox_summary"] = "对应任务已被删除"
    run_rec["decision"] = ""
    run_rec["completed_at"] = now_s
    run_rec["updated_at"] = now_s
    auto_item = data["automations"].get(auto_id) if auto_id else None
    if isinstance(auto_item, dict):
        auto_item["last_run_status"] = "LOST"
        auto_item["last_run_summary"] = "对应任务已被删除"
        auto_item["updated_at"] = now_s
        write_automation_files(store, auto_item)
    if auto_id:
        prune_automation_runs_in_tx(data, auto_id)


def settle_automation_turn_in_tx(store: Any, data: dict[str, Any], row: dict[str, Any]) -> None:
    auto_id = str(row.get("automation_id") or "").strip()
    run_id = str(row.get("automation_run_id") or "").strip()
    if not run_id:
        return

    ensure_automation_state(data)
    inherit_task_policy_in_tx(data, row)
    status = str(row.get("status") or "").strip()
    if status not in (TERMINAL_TASK_STATUSES | ATTENTION_TASK_STATUSES):
        return

    existing_run = data["automation_runs"].get(run_id)
    if isinstance(existing_run, dict):
        r_status = str(existing_run.get("status") or "").strip()
        if status in ATTENTION_TASK_STATUSES:
            if r_status != "IN_PROGRESS":
                return
        elif r_status not in {"IN_PROGRESS", "NEEDS_ATTENTION"}:
            return

    now_str = now_iso()
    auto_item = data["automations"].get(auto_id) if auto_id else None
    policy = str((auto_item or {}).get("notification_policy") or "all_runs").strip()
    auto_name = str((auto_item or {}).get("name") or row.get("title") or "定时提醒")

    if status in ATTENTION_TASK_STATUSES:
        pending_input = row.get("pending_input") if isinstance(row.get("pending_input"), dict) else {}
        pending_approval = row.get("pending_approval") if isinstance(row.get("pending_approval"), dict) else {}
        detail = str(
            pending_input.get("question")
            or pending_approval.get("title")
            or _extract_row_error(row, f"当前状态：{status}")
        ).strip()
        attn_summary = (
            detail
            if "定时任务需要处理" in detail
            else f"定时任务需要处理：{detail}"
        )[:240]
        row["latest_turn_is_silent_heartbeat"] = policy == "never"
        if policy != "never":
            row["automation_notification"] = {
                "id": run_id or f"notif-{now_ms()}",
                "automation_id": auto_id,
                "automation_name": auto_name,
                "kind": str(row.get("automation_kind") or "heartbeat"),
                "decision": "NOTIFY",
                "title": "定时任务需要处理",
                "message": attn_summary,
                "task_id": str(row.get("id") or ""),
                "status": "NEEDS_ATTENTION",
                "created_at": now_str,
            }
        if run_id and isinstance(data["automation_runs"].get(run_id), dict):
            run_rec = data["automation_runs"][run_id]
            run_rec["status"] = "NEEDS_ATTENTION"
            run_rec["decision"] = "NOTIFY" if policy != "never" else ""
            run_rec["inbox_title"] = "定时任务需要处理"
            run_rec["inbox_summary"] = attn_summary
            run_rec["updated_at"] = now_str
        if auto_id and isinstance(auto_item, dict):
            auto_item["last_run_status"] = "NEEDS_ATTENTION"
            auto_item["last_run_summary"] = attn_summary
            auto_item["last_task_id"] = str(row.get("id") or "")
            auto_item["updated_at"] = now_str
            write_automation_files(store, auto_item)
        if auto_id:
            prune_automation_runs_in_tx(data, auto_id)
        return

    assistant_msg, assistant_text = _latest_turn_assistant_message(
        list(row.get("messages") or []),
        run_id,
    )
    parsed = parse_automation_completion_output(assistant_text)
    hb = parsed["heartbeat"]
    error_text = _extract_row_error(row, status)
    summary = parsed["inbox_summary"] or error_text

    # Strip raw <heartbeat> and ::inbox-item tags from the visible assistant message & result
    if assistant_msg is not None and (hb["has_heartbeat_tag"] or INBOX_ITEM_RE.search(assistant_text)):
        display_text = parsed["cleaned_text"] or hb["message"] or summary
        assistant_msg["content"] = display_text
        if isinstance(row.get("result"), str) and row.get("result"):
            row["result"] = display_text

    hb_decision = str(hb.get("decision") or "").upper() if assistant_msg is not None else ""
    unattended_blocked = (
        list(row.get("unattended_blocked") or [])
        if isinstance(row.get("unattended_blocked"), list)
        else []
    )
    if status in {"failed", "cancelled"} or unattended_blocked:
        is_silent = policy == "never"
    else:
        is_silent = (
            policy == "never"
            or policy == "failed_runs_only"
            or hb_decision == "DONT_NOTIFY"
        )
    row["latest_turn_is_silent_heartbeat"] = bool(is_silent)

    if run_id and is_silent and str(row.get("automation_kind") or "") == "heartbeat":
        # Mark the latest heartbeat user & assistant messages as silent so UI hides them like Codex
        if assistant_msg is not None:
            ui_meta = dict(assistant_msg.get("ui") or {}) if isinstance(assistant_msg.get("ui"), dict) else {}
            ui_meta["kind"] = "silent_heartbeat"
            ui_meta["hidden"] = True
            assistant_msg["ui"] = ui_meta
        hb_msg_id = f"msg-hb-{run_id}"
        for msg in reversed(row.get("messages") or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                ui_meta = dict(msg.get("ui") or {}) if isinstance(msg.get("ui"), dict) else {}
                if (
                    msg.get("message_id") == hb_msg_id
                    or ui_meta.get("kind") == "heartbeat"
                    or "<heartbeat>" in str(msg.get("content") or "")
                ):
                    ui_meta["kind"] = "silent_heartbeat"
                    ui_meta["hidden"] = True
                    msg["ui"] = ui_meta
                break
    elif not is_silent:
        if unattended_blocked:
            notif_title = "定时任务需要处理"
            blocked_names = ", ".join(
                str(b.get("tool") or b) if isinstance(b, dict) else str(b)
                for b in unattended_blocked[:5]
            )
            notif_body = f"以下操作需要批准而被跳过：{blocked_names}。{summary}"
        elif status in {"failed", "cancelled"}:
            notif_title = parsed["inbox_title"] or auto_name
            notif_body = error_text if assistant_msg is None or hb_decision == "DONT_NOTIFY" else (
                hb["message"] or parsed["cleaned_text"] or error_text
            )
        else:
            notif_title = parsed["inbox_title"] or auto_name
            notif_body = hb["message"] or parsed["cleaned_text"] or summary
        row["automation_notification"] = {
            "id": run_id or f"notif-{now_ms()}",
            "automation_id": auto_id,
            "automation_name": auto_name,
            "kind": str(row.get("automation_kind") or "heartbeat"),
            "decision": "NOTIFY",
            "title": notif_title,
            "message": str(notif_body or "")[:240],
            "task_id": str(row.get("id") or ""),
            "status": status,
            "created_at": now_str,
        }

    if unattended_blocked:
        row.pop("unattended_blocked", None)

    if run_id and isinstance(data["automation_runs"].get(run_id), dict):
        run_rec = data["automation_runs"][run_id]
        if status == "completed":
            run_rec["status"] = "ARCHIVED" if is_silent else "ACCEPTED"
        else:
            run_rec["status"] = status.upper()
        run_rec["decision"] = hb_decision if assistant_msg is not None else ""
        run_rec["inbox_title"] = parsed["inbox_title"] or str(row.get("title") or "")
        run_rec["inbox_summary"] = summary
        if unattended_blocked:
            run_rec["blocked_operations"] = unattended_blocked
        run_rec["completed_at"] = now_str
        run_rec["updated_at"] = now_str

    if auto_id and isinstance(auto_item, dict):
        is_once = str(auto_item.get("repeat") or "") == "once"
        if status == "completed" and is_once and bool(auto_item.get("auto_archive", False)):
            delete_automation_files(store, auto_id)
            data["automations"].pop(auto_id, None)
        else:
            auto_item["last_run_status"] = "completed" if status == "completed" else status
            auto_item["last_run_summary"] = summary
            auto_item["last_task_id"] = str(row.get("id") or "")
            auto_item["updated_at"] = now_str
            if auto_item.get("next_run_at") is None and is_once:
                auto_item["status"] = "PAUSED"
            write_automation_files(store, auto_item)
            if summary and not memory_blocked(row) and not memory_blocked(auto_item):
                append_automation_memory_note(store, auto_id, f"({status}) {summary}")

    if auto_id:
        prune_automation_runs_in_tx(data, auto_id)
