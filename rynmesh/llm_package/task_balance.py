"""Development-only Task Balance with idempotent holds and settlements."""

from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_VERSION = "rynmesh-dev-task-balance-v1"
TERMINAL_HOLD_STATES = {"settled", "released"}


class TaskBalanceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _amount(value: float) -> float:
    number = round(float(value), 8)
    if not math.isfinite(number) or number < 0:
        raise TaskBalanceError("amount must be a finite non-negative number")
    return number


class TaskBalanceLedger:
    """A local simulated balance. It is intentionally unrelated to Credits."""

    def __init__(self, path: str | Path, *, initial_dev_balance: float = 100.0) -> None:
        self.path = Path(path)
        self.initial_dev_balance = _amount(initial_dev_balance)
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write({
                "version": LEDGER_VERSION, "development_only": True,
                "available": self.initial_dev_balance, "held": 0.0, "earned": 0.0,
                "holds": {}, "events": [],
            })

    def summary(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            return {k: state[k] for k in ("version", "development_only", "available", "held", "earned")}

    def hold(self, *, task_id: str, amount: float, service_id: str, provider_peer_id: str) -> dict[str, Any]:
        value = _amount(amount)
        if not task_id:
            raise TaskBalanceError("task_id is required")
        with self._lock:
            state = self._read()
            if task_id in state["holds"]:
                previous = state["holds"][task_id]
                if previous["state"] != "released":
                    return dict(previous)
                if _amount(previous["amount"]) != value:
                    raise TaskBalanceError("retry hold amount differs from the original hold")
                if value > float(state["available"]):
                    raise TaskBalanceError("insufficient development Task Balance")
                state["available"] = _amount(float(state["available"]) - value)
                state["held"] = _amount(float(state["held"]) + value)
                previous.update({"state": "held", "retried_at": _now()})
                previous.pop("reason", None)
                previous.pop("released_at", None)
                self._event(state, "rehold", {"task_id": task_id, "amount": value})
                self._write(state)
                return dict(previous)
            if value > float(state["available"]):
                raise TaskBalanceError("insufficient development Task Balance")
            record = {"task_id": task_id, "amount": value, "state": "held",
                      "service_id": service_id, "provider_peer_id": provider_peer_id,
                      "created_at": _now()}
            state["available"] = _amount(float(state["available"]) - value)
            state["held"] = _amount(float(state["held"]) + value)
            state["holds"][task_id] = record
            self._event(state, "hold", record)
            self._write(state)
            return dict(record)

    def settle(self, *, task_id: str, amount: float, input_tokens: int, output_tokens: int,
               duration_ms: int, service_id: str, provider_peer_id: str) -> dict[str, Any]:
        actual = _amount(amount)
        with self._lock:
            state = self._read()
            hold = state["holds"].get(task_id)
            if not hold:
                raise TaskBalanceError("task hold not found")
            if hold["state"] == "settled":
                return dict(hold)
            if hold["state"] == "released":
                raise TaskBalanceError("released task cannot be settled")
            reserved = _amount(hold["amount"])
            if actual > reserved:
                raise TaskBalanceError("settlement exceeds frozen amount")
            refund = _amount(reserved - actual)
            state["held"] = _amount(float(state["held"]) - reserved)
            state["available"] = _amount(float(state["available"]) + refund)
            hold.update({"state": "settled", "settled_amount": actual, "refund": refund,
                         "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
                         "duration_ms": int(duration_ms), "settled_at": _now()})
            billing = {"task_id": task_id, "service_id": service_id,
                       "provider_peer_id": provider_peer_id, "input_tokens": int(input_tokens),
                       "output_tokens": int(output_tokens), "duration_ms": int(duration_ms),
                       "amount": actual, "currency": "DEV_TASK_BALANCE"}
            self._event(state, "settle", billing)
            self._write(state)
            return dict(hold)

    def release(self, *, task_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            hold = state["holds"].get(task_id)
            if not hold:
                raise TaskBalanceError("task hold not found")
            if hold["state"] in TERMINAL_HOLD_STATES:
                return dict(hold)
            reserved = _amount(hold["amount"])
            state["held"] = _amount(float(state["held"]) - reserved)
            state["available"] = _amount(float(state["available"]) + reserved)
            hold.update({"state": "released", "reason": str(reason)[:120], "released_at": _now()})
            self._event(state, "release", {"task_id": task_id, "amount": reserved,
                                            "reason": str(reason)[:120]})
            self._write(state)
            return dict(hold)

    def earn(self, *, task_id: str, amount: float, input_tokens: int, output_tokens: int,
             duration_ms: int, service_id: str, consumer_peer_id: str) -> dict[str, Any]:
        value = _amount(amount)
        key = "earning:" + task_id
        with self._lock:
            state = self._read()
            previous = next((event for event in state["events"] if event.get("event_id") == key), None)
            if previous:
                return dict(previous)
            state["earned"] = _amount(float(state["earned"]) + value)
            event = {"event_id": key, "kind": "earning", "task_id": task_id,
                     "service_id": service_id, "consumer_peer_id": consumer_peer_id,
                     "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
                     "duration_ms": int(duration_ms), "amount": value,
                     "currency": "DEV_TASK_BALANCE", "created_at": _now()}
            state["events"].append(event)
            self._write(state)
            return dict(event)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read()["events"]]

    def _event(self, state: dict[str, Any], kind: str, fields: dict[str, Any]) -> None:
        state["events"].append({"event_id": uuid.uuid4().hex, "kind": kind,
                                "created_at": _now(), **fields})

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskBalanceError(f"cannot read Task Balance ledger: {exc}") from exc
        if value.get("version") != LEDGER_VERSION or not value.get("development_only"):
            raise TaskBalanceError("not a development Task Balance ledger")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
