"""Development-only Task Balance with idempotent holds and settlements.

Two persistence backends behind one interface:

- **standalone** (default): one mutable JSON file, as before. Used by tests,
  the CLI, and any embedder without a node identity.
- **ledger-backed**: every transition is also a signed ``CreditEvent`` in the
  ``dev:task_balance`` category of the node's ``FileCreditLedger`` — one
  auditable, append-only history for every service's escrow. Reputation
  scoring never sees that category (see ``credits._category_matches``), so
  Credits stay non-monetary while sharing the ledger.

In ledger-backed mode the JSON file becomes a materialized *snapshot*: the
balances, the open-hold map (which doubles as the idempotency index), and a
bounded recent-events cache. Hold/settle/release stay O(1) — the ledger's own
``list_events`` re-reads and re-verifies every file per call, which is right
for reputation and wrong for a per-order path. A missing or inconsistent
snapshot is rebuilt by replaying the category.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rynmesh.atomic_io import atomic_write_json, migration_backup

_logger = logging.getLogger(__name__)

LEDGER_VERSION = "rynmesh-dev-task-balance-v1"
SNAPSHOT_VERSION = "rynmesh-dev-task-balance-view-v2"
TERMINAL_HOLD_STATES = {"settled", "released"}
CURRENCY = "DEV_TASK_BALANCE"
DEV_GRANT_THRESHOLD = 10.0
_SNAPSHOT_RETENTION_DAYS = 30
_RECENT_EVENTS = 200
_KIND_PRIORITY = {
    "opening": 0,
    "hold": 1,
    "rehold": 2,
    "settle": 3,
    "release": 4,
    "earning": 5,
}

# Local transition kind -> signed credit event kind.
_CREDIT_KINDS = {
    "opening": "task_balance_opening",
    "hold": "task_hold",
    "rehold": "task_hold",
    "settle": "task_settle",
    "release": "task_release",
    "earning": "task_earning",
}


class TaskBalanceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _amount(value: float) -> float:
    number = round(float(value), 8)
    if not math.isfinite(number) or number < 0:
        raise TaskBalanceError("amount must be a finite non-negative number")
    return number


def _token_count(value: Any) -> int | None:
    if value is None or type(value) is int and value >= 0:
        return value
    raise TaskBalanceError("token count must be a non-negative integer or unknown")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _prune_old_entries(value: dict[str, Any]) -> None:
    now_dt = _parse_iso(_now()) or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=_SNAPSHOT_RETENTION_DAYS)
    holds = value.get("holds")
    if isinstance(holds, dict):
        stale_holds = [
            task_id
            for task_id, hold in holds.items()
            if isinstance(hold, dict)
            and hold.get("state") in TERMINAL_HOLD_STATES
            and (ended := _parse_iso(hold.get("settled_at") or hold.get("released_at"))) is not None
            and ended < cutoff
        ]
        for task_id in stale_holds:
            del holds[task_id]
    earnings = value.get("earnings")
    if isinstance(earnings, dict):
        stale_earnings = [
            key
            for key, item in earnings.items()
            if isinstance(item, dict)
            and (created := _parse_iso(item.get("created_at"))) is not None
            and created < cutoff
        ]
        for key in stale_earnings:
            del earnings[key]


def _event_kind_priority(event: Any) -> int:
    phase = str(event.metadata.get("phase") or event.reason or "")
    if phase in _KIND_PRIORITY:
        return _KIND_PRIORITY[phase]
    return {
        "task_balance_opening": 0,
        "task_hold": 1,
        "task_settle": 3,
        "task_release": 4,
        "task_earning": 5,
    }.get(event.kind, 6)


def _event_sort_key(event: Any) -> tuple[Any, ...]:
    raw_seq = event.metadata.get("sequence")
    has_seq = type(raw_seq) is int
    return (
        event.created_at,
        (0, raw_seq) if has_seq else (1, 0),
        _event_kind_priority(event),
        event.dedupe_key,
    )


class TaskBalanceLedger:
    """A local simulated balance. Not money, a deposit, or a payment system."""

    def __init__(
        self,
        path: str | Path,
        *,
        initial_dev_balance: float = 100.0,
        credit_ledger: Any | None = None,
        peer_id: str = "",
        private_key_bytes: bytes | None = None,
    ) -> None:
        self.path = Path(path)
        self.initial_dev_balance = _amount(initial_dev_balance)
        self._lock = threading.RLock()
        self._ledger = credit_ledger
        self._peer_id = peer_id
        self._private_key = private_key_bytes
        self._folded = 0
        self._last_dedupe_key = ""
        self._sequence = 0
        if credit_ledger is not None and not (peer_id and private_key_bytes):
            raise TaskBalanceError("ledger-backed Task Balance needs peer_id and private_key_bytes")
        with self._lock:
            if self._ledger is None:
                if not self.path.exists():
                    self._write(self._fresh_state(LEDGER_VERSION))
                return
            self._initialize_ledger_backed()

    # ---- public API (identical in both modes) ---------------------------
    def summary(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            return {
                "version": state["version"],
                "development_only": True,
                "available": state["available"],
                "held": state["held"],
                "earned": state["earned"],
                "currency": CURRENCY,
                "ledger_backed": self._ledger is not None,
                "replay_skipped": int(state.get("replay_skipped") or 0),
            }

    def dev_grant(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if float(state["available"]) >= DEV_GRANT_THRESHOLD:
                raise TaskBalanceError("dev_grant_not_needed")
            grant_seq = int(state.get("dev_grant_count") or 0) + 1
            dedupe = f"opening:dev_grant:{grant_seq}"
            state["dev_grant_count"] = grant_seq
            state["available"] = _amount(float(state["available"]) + self.initial_dev_balance)
            self._append_credit(
                "opening",
                "",
                self.initial_dev_balance,
                dedupe,
                {"source": "dev_grant", "grant_seq": grant_seq},
            )
            self._write(state)
            return {
                **self.summary(),
                "granted": self.initial_dev_balance,
                "dedupe_key": dedupe,
            }

    def hold(self, *, task_id: str, amount: float, service_id: str, provider_peer_id: str,
             idempotency_key: str = "", request_fingerprint: str = "") -> dict[str, Any]:
        value = _amount(amount)
        if not task_id:
            raise TaskBalanceError("task_id is required")
        with self._lock:
            state = self._read()
            record = self._apply_hold(
                state, task_id=task_id, amount=value, service_id=service_id,
                provider_peer_id=provider_peer_id, idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint, replay=False,
            )
            self._write(state)
            return record

    def settle(self, *, task_id: str, amount: float, input_tokens: int | None, output_tokens: int | None,
               duration_ms: int, service_id: str, provider_peer_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            record = self._apply_settle(
                state, task_id=task_id, amount=_amount(amount), input_tokens=_token_count(input_tokens),
                output_tokens=_token_count(output_tokens), duration_ms=int(duration_ms),
                service_id=service_id, provider_peer_id=provider_peer_id, replay=False,
            )
            self._write(state)
            return record

    def release(self, *, task_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            record = self._apply_release(state, task_id=task_id, reason=reason, replay=False)
            self._write(state)
            return record

    def earn(self, *, task_id: str, amount: float, input_tokens: int | None, output_tokens: int | None,
             duration_ms: int, service_id: str, consumer_peer_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            event = self._apply_earning(
                state, task_id=task_id, amount=_amount(amount), input_tokens=_token_count(input_tokens),
                output_tokens=_token_count(output_tokens), duration_ms=int(duration_ms),
                service_id=service_id, consumer_peer_id=consumer_peer_id, replay=False,
            )
            self._write(state)
            return event

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read()["events"]]

    def rebuild(self) -> dict[str, Any]:
        """Replay the signed category into a fresh snapshot (ledger-backed only)."""
        if self._ledger is None:
            raise TaskBalanceError("rebuild requires a ledger-backed Task Balance")
        with self._lock:
            state = self._replay()
            self._write(state)
            return self.summary()

    # ---- transitions (shared by live calls and replay) ------------------
    def _apply_hold(self, state, *, task_id, amount, service_id, provider_peer_id,
                    idempotency_key, request_fingerprint, replay, created_at=None):
        if task_id in state["holds"]:
            previous = state["holds"][task_id]
            expected = {
                "service_id": str(service_id),
                "provider_peer_id": str(provider_peer_id),
                "idempotency_key": str(idempotency_key or task_id),
                "request_fingerprint": str(request_fingerprint),
            }
            actual = {key: str(previous.get(key) or "") for key in expected}
            if actual != expected or _amount(previous["amount"]) != amount:
                raise TaskBalanceError("task idempotency conflict")
            if previous["state"] != "released":
                return dict(previous)
            if amount > float(state["available"]):
                raise TaskBalanceError("insufficient development Task Balance")
            state["available"] = _amount(float(state["available"]) - amount)
            state["held"] = _amount(float(state["held"]) + amount)
            previous.update({"state": "held", "retried_at": created_at or _now()})
            previous.pop("reason", None)
            previous.pop("released_at", None)
            previous["rehold_count"] = int(previous.get("rehold_count") or 0) + 1
            self._event(state, "rehold", {"task_id": task_id, "amount": amount}, replay=replay,
                        dedupe=f"hold:{task_id}:{previous['rehold_count']}",
                        metadata=expected)
            return dict(previous)
        if amount > float(state["available"]):
            raise TaskBalanceError("insufficient development Task Balance")
        record = {"task_id": task_id, "amount": amount, "state": "held",
                  "service_id": service_id, "provider_peer_id": provider_peer_id,
                  "idempotency_key": str(idempotency_key or task_id),
                  "request_fingerprint": str(request_fingerprint),
                  "created_at": created_at or _now()}
        state["available"] = _amount(float(state["available"]) - amount)
        state["held"] = _amount(float(state["held"]) + amount)
        state["holds"][task_id] = record
        self._event(state, "hold", record, replay=replay, dedupe=f"hold:{task_id}",
                    metadata={k: record[k] for k in ("service_id", "provider_peer_id",
                                                     "idempotency_key", "request_fingerprint")})
        return dict(record)

    def _apply_settle(self, state, *, task_id, amount, input_tokens, output_tokens,
                      duration_ms, service_id, provider_peer_id, replay, created_at=None):
        hold = state["holds"].get(task_id)
        if not hold:
            raise TaskBalanceError("task hold not found")
        if hold["state"] == "settled":
            return dict(hold)
        if hold["state"] == "released":
            raise TaskBalanceError("released task cannot be settled")
        reserved = _amount(hold["amount"])
        if amount > reserved:
            raise TaskBalanceError("settlement exceeds frozen amount")
        refund = _amount(reserved - amount)
        state["held"] = _amount(float(state["held"]) - reserved)
        state["available"] = _amount(float(state["available"]) + refund)
        hold.update({"state": "settled", "settled_amount": amount, "refund": refund,
                     "input_tokens": input_tokens, "output_tokens": output_tokens,
                     "usage_complete": input_tokens is not None and output_tokens is not None,
                     "duration_ms": duration_ms, "settled_at": created_at or _now()})
        billing = {"task_id": task_id, "service_id": service_id,
                   "provider_peer_id": provider_peer_id, "input_tokens": input_tokens,
                   "output_tokens": output_tokens, "duration_ms": duration_ms,
                   "usage_complete": input_tokens is not None and output_tokens is not None,
                   "amount": amount, "currency": CURRENCY}
        self._event(state, "settle", billing, replay=replay, dedupe=f"settle:{task_id}",
                    metadata={k: billing[k] for k in ("service_id", "provider_peer_id",
                                                      "input_tokens", "output_tokens",
                                                      "duration_ms")})
        return dict(hold)

    def _apply_release(self, state, *, task_id, reason, replay, created_at=None):
        hold = state["holds"].get(task_id)
        if not hold:
            raise TaskBalanceError("task hold not found")
        if hold["state"] in TERMINAL_HOLD_STATES:
            return dict(hold)
        reserved = _amount(hold["amount"])
        state["held"] = _amount(float(state["held"]) - reserved)
        state["available"] = _amount(float(state["available"]) + reserved)
        hold.update({"state": "released", "reason": str(reason)[:120],
                     "released_at": created_at or _now()})
        self._event(state, "release", {"task_id": task_id, "amount": reserved,
                                        "reason": str(reason)[:120]}, replay=replay,
                    dedupe=f"release:{task_id}:{int(hold.get('rehold_count') or 0)}",
                    metadata={"reason": str(reason)[:120]})
        return dict(hold)

    def _apply_earning(self, state, *, task_id, amount, input_tokens, output_tokens,
                       duration_ms, service_id, consumer_peer_id, replay, created_at=None):
        key = "earning:" + task_id
        if key in state.setdefault("earnings", {}):
            return dict(state["earnings"][key])
        state["earned"] = _amount(float(state["earned"]) + amount)
        event = {"event_id": key, "kind": "earning", "task_id": task_id,
                 "service_id": service_id, "consumer_peer_id": consumer_peer_id,
                 "input_tokens": input_tokens, "output_tokens": output_tokens,
                 "usage_complete": input_tokens is not None and output_tokens is not None,
                 "duration_ms": duration_ms, "amount": amount,
                 "currency": CURRENCY, "created_at": created_at or _now()}
        state["earnings"][key] = event
        state["events"].append(dict(event))
        self._trim(state)
        if not replay:
            self._append_credit("earning", task_id, amount, key, {
                "service_id": service_id, "consumer_peer_id": consumer_peer_id,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "duration_ms": duration_ms,
            })
        return dict(event)

    # ---- event plumbing ------------------------------------------------
    def _event(self, state, kind, fields, *, replay, dedupe, metadata):
        state["events"].append({"event_id": uuid.uuid4().hex, "kind": kind,
                                "created_at": _now(), **fields})
        self._trim(state)
        if not replay:
            self._append_credit(kind, str(fields.get("task_id") or ""),
                                _amount(fields.get("amount", 0.0)), dedupe, metadata)

    def _trim(self, state) -> None:
        if len(state["events"]) > _RECENT_EVENTS:
            del state["events"][: len(state["events"]) - _RECENT_EVENTS]

    def _append_credit(self, kind, task_id, amount, dedupe, metadata, *, created_at: str = "") -> None:
        if self._ledger is None:
            return
        next_seq = self._sequence + 1
        result = self._ledger.record(
            subject_peer_id=self._peer_id, issuer_peer_id=self._peer_id,
            private_key_bytes=self._private_key, kind=_CREDIT_KINDS[kind], amount=amount,
            role="consumer" if kind in {"hold", "rehold", "settle", "release"} else "provider",
            category=_task_balance_category(), subject_id=task_id, dedupe_key=dedupe,
            reason=kind, metadata={"phase": kind, "sequence": next_seq, **dict(metadata)},
            created_at=created_at or _now(),
        )
        if result.get("status") == "recorded":
            self._folded += 1
            self._last_dedupe_key = dedupe
            self._sequence = next_seq

    # ---- ledger-backed initialization / replay ---------------------------
    def _initialize_ledger_backed(self) -> None:
        self._folded = 0
        self._last_dedupe_key = ""
        self._sequence = 0
        existing = None
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = None
        events = self._category_events()
        category_count = len(events)
        last_key = events[-1]["dedupe_key"] if events else ""
        if (
            existing
            and existing.get("version") == SNAPSHOT_VERSION
            and existing.get("development_only")
            and isinstance(existing.get("holds"), dict)
            and isinstance(existing.get("events"), list)
        ):
            if (
                int(existing.get("folded_events") or 0) == category_count
                and str(existing.get("last_dedupe_key") or "") == last_key
            ):
                self._folded = category_count
                self._last_dedupe_key = last_key
                self._sequence = int(existing.get("sequence") or 0)
                return
            self._write(self._replay(events))
            return
        if existing and existing.get("version") == LEDGER_VERSION and category_count == 0:
            self._migrate_legacy(existing)
            return
        if category_count:
            self._write(self._replay(events))
            return
        state = self._fresh_state(SNAPSHOT_VERSION)
        # The opening grant must land in the snapshot as well as the ledger;
        # replay applies it from the event, the live path applies it here.
        state["available"] = self.initial_dev_balance
        self._append_credit("opening", "", self.initial_dev_balance, "opening:initial",
                            {"earned": 0.0, "source": "initial_dev_grant"})
        self._write(state)

    def _migrate_legacy(self, legacy: dict[str, Any]) -> None:
        """Turn a v1 file into signed events + a v2 snapshot, once."""
        migration_backup(self.path, suffix=".migrated")
        state = self._fresh_state(SNAPSHOT_VERSION)
        opening = _amount(float(legacy.get("available") or 0) + float(legacy.get("held") or 0))
        state["available"] = opening
        state["earned"] = _amount(float(legacy.get("earned") or 0))
        self._append_credit("opening", "", opening, "opening:migrated",
                            {"earned": state["earned"], "source": "legacy_task_balance_v1"})
        for task_id, hold in sorted(dict(legacy.get("holds") or {}).items()):
            if hold.get("state") != "held":
                continue
            self._apply_hold(
                state, task_id=task_id, amount=_amount(hold["amount"]),
                service_id=str(hold.get("service_id") or ""),
                provider_peer_id=str(hold.get("provider_peer_id") or ""),
                idempotency_key=str(hold.get("idempotency_key") or task_id),
                request_fingerprint=str(hold.get("request_fingerprint") or ""),
                replay=False, created_at=str(hold.get("created_at") or _now()),
            )
        self._write(state)

    def _category_events(self) -> list[dict[str, Any]]:
        from rynmesh.credits import CreditEvent

        signed = self._ledger.list_events(
            subject_peer_id=self._peer_id, category=_task_balance_category(),
        )
        events = [CreditEvent.from_payload(item.payload) for item in signed]
        events.sort(key=_event_sort_key)
        return [
            {
                "kind": e.kind,
                "amount": float(e.amount),
                "task_id": e.subject_id,
                "created_at": e.created_at,
                "metadata": dict(e.metadata),
                "dedupe_key": e.dedupe_key,
                "sequence": e.metadata.get("sequence") if type(e.metadata.get("sequence")) is int else None,
            }
            for e in events
        ]

    def _category_event_count(self) -> int:
        return len(self._category_events())

    def _replay(self, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        state = self._fresh_state(SNAPSHOT_VERSION)
        if events is None:
            events = self._category_events()
        for item in events:
            meta = item["metadata"]
            kind, task_id, amount = item["kind"], item["task_id"], _amount(item["amount"])
            try:
                if kind == "task_balance_opening":
                    state["available"] = _amount(float(state["available"]) + amount)
                    if "earned" in meta:
                        state["earned"] = _amount(float(meta.get("earned") or 0))
                    if item["dedupe_key"].startswith("opening:dev_grant:"):
                        suffix = item["dedupe_key"].rsplit(":", 1)[-1]
                        if suffix.isdigit():
                            state["dev_grant_count"] = max(
                                int(state.get("dev_grant_count") or 0), int(suffix)
                            )
                elif kind == "task_hold":
                    self._apply_hold(
                        state, task_id=task_id, amount=amount,
                        service_id=str(meta.get("service_id") or ""),
                        provider_peer_id=str(meta.get("provider_peer_id") or ""),
                        idempotency_key=str(meta.get("idempotency_key") or task_id),
                        request_fingerprint=str(meta.get("request_fingerprint") or ""),
                        replay=True, created_at=item["created_at"],
                    )
                elif kind == "task_settle":
                    self._apply_settle(
                        state, task_id=task_id, amount=amount,
                        input_tokens=_token_count(meta.get("input_tokens")),
                        output_tokens=_token_count(meta.get("output_tokens")),
                        duration_ms=int(meta.get("duration_ms") or 0),
                        service_id=str(meta.get("service_id") or ""),
                        provider_peer_id=str(meta.get("provider_peer_id") or ""),
                        replay=True, created_at=item["created_at"],
                    )
                elif kind == "task_release":
                    self._apply_release(state, task_id=task_id,
                                        reason=str(meta.get("reason") or ""),
                                        replay=True, created_at=item["created_at"])
                elif kind == "task_earning":
                    self._apply_earning(
                        state, task_id=task_id, amount=amount,
                        input_tokens=_token_count(meta.get("input_tokens")),
                        output_tokens=_token_count(meta.get("output_tokens")),
                        duration_ms=int(meta.get("duration_ms") or 0),
                        service_id=str(meta.get("service_id") or ""),
                        consumer_peer_id=str(meta.get("consumer_peer_id") or ""),
                        replay=True, created_at=item["created_at"],
                    )
            except TaskBalanceError as exc:
                state["replay_skipped"] = int(state.get("replay_skipped") or 0) + 1
                _logger.warning(
                    "Task Balance replay skipped invalid transition kind=%s dedupe_key=%s: %s",
                    kind,
                    item["dedupe_key"],
                    exc,
                )
                continue
        self._folded = len(events)
        self._last_dedupe_key = events[-1]["dedupe_key"] if events else ""
        self._sequence = max(
            (item["sequence"] for item in events if item.get("sequence") is not None),
            default=0,
        )
        return state

    # ---- persistence -----------------------------------------------------
    def _fresh_state(self, version: str) -> dict[str, Any]:
        return {
            "version": version, "development_only": True,
            "available": self.initial_dev_balance if version == LEDGER_VERSION else 0.0,
            "held": 0.0, "earned": 0.0, "holds": {}, "earnings": {}, "events": [],
            "sequence": 0, "replay_skipped": 0,
        }

    def _load_raw(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskBalanceError("task_balance_corrupt") from exc
        expected = LEDGER_VERSION if self._ledger is None else SNAPSHOT_VERSION
        if (
            not isinstance(value, dict)
            or value.get("version") != expected
            or not value.get("development_only")
            or not isinstance(value.get("holds"), dict)
            or not isinstance(value.get("events"), list)
        ):
            raise TaskBalanceError("task_balance_corrupt")
        value.setdefault("earnings", {})
        value.setdefault("replay_skipped", 0)
        if self._ledger is not None:
            self._folded = int(value.get("folded_events") or 0)
            self._last_dedupe_key = str(value.get("last_dedupe_key") or "")
            self._sequence = int(value.get("sequence") or 0)
        return value

    def _quarantine_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        corrupt_path = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        os.replace(self.path, corrupt_path)

    def _read(self) -> dict[str, Any]:
        with self._lock:
            try:
                return self._load_raw()
            except TaskBalanceError:
                if self._ledger is not None:
                    self._write(self._replay())
                    return self._load_raw()
                self._quarantine_corrupt_file()
                raise TaskBalanceError("task_balance_corrupt") from None

    def _write(self, value: dict[str, Any]) -> None:
        _prune_old_entries(value)
        value["sequence"] = self._sequence
        value.setdefault("replay_skipped", 0)
        if self._ledger is not None:
            value["folded_events"] = self._folded
            value["last_dedupe_key"] = self._last_dedupe_key
        atomic_write_json(self.path, value, sort_keys=True)


def _task_balance_category() -> str:
    from rynmesh.credits import TASK_BALANCE_CATEGORY

    return TASK_BALANCE_CATEGORY
