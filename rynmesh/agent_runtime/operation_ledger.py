"""Encrypted business receipts, separate from Agent task transactions."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .store import AgentError, AgentToolRejected, encoded

CHANNEL = b"rynmesh-agent-business-operations-v1"
LIMIT = 16 * 1024 * 1024


class OperationLedger:
    def __init__(self, home):
        self.path = Path(home) / "agent" / "business-operations.json"
        self.lock = self.path.with_suffix(".lock")
        self.key = peer_box.load_or_create_messaging_key(Path(home) / "messaging.x25519")
        self.pub = peer_box.public_key_b64(self.key)

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            value = read_json(self.path, max_bytes=LIMIT * 2)
            raw = peer_box.open_sealed(self.key, self.pub, value["nonce"], value["ciphertext"], info=CHANNEL)
            if len(raw) > LIMIT:
                raise ValueError()
            records = json.loads(raw)
            if not isinstance(records, dict):
                raise ValueError()
            return records
        except Exception:
            raise AgentError("agent_operation_ledger_unreadable") from None

    def _write(self, records):
        raw = encoded(records)
        # Reserve bounded receipt space before dispatch; known capacity cannot
        # become a predictable post-commit failure. I/O failures remain unknown.
        reserved = sum(row.get("receipt_reserve", 0) for row in records.values())
        if len(raw) + reserved > LIMIT:
            raise AgentError("agent_operation_ledger_limit")
        nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=CHANNEL)
        atomic_write_json(self.path, {"nonce": nonce, "ciphertext": ciphertext}, max_bytes=LIMIT * 2)

    def begin(self, binding, preview, *, receipt_reserve=4096):
        with file_transaction(self.lock):
            records = self._read()
            prior = records.get(binding["operation_id"])
            if prior:
                if prior.get("status") == "retired":
                    raise AgentToolRejected("agent_operation_retired")
                if prior["binding"] != binding:
                    raise AgentToolRejected("agent_operation_id_conflict")
                return copy.deepcopy(prior), False
            if len(records) >= 5000:
                raise AgentToolRejected("agent_operation_ledger_limit")
            if type(receipt_reserve) is not int or not 4096 <= receipt_reserve <= 32768:
                raise AgentError("agent_operation_receipt_reserve_invalid")
            row = {"binding": copy.deepcopy(binding), "preview": copy.deepcopy(preview), "status": "intent", "receipt_reserve": receipt_reserve}
            records[binding["operation_id"]] = row
            self._write(records)
            return copy.deepcopy(row), True

    def begin_dispatched(self, binding, preview, *, operation_classification, host_pid, receipt_reserve=4096):
        with file_transaction(self.lock):
            records = self._read()
            prior = records.get(binding["operation_id"])
            if prior:
                if prior.get("status") == "retired":
                    raise AgentToolRejected("agent_operation_retired")
                if prior["binding"] != binding:
                    raise AgentToolRejected("agent_operation_id_conflict")
                return copy.deepcopy(prior), False
            if len(records) >= 5000:
                raise AgentToolRejected("agent_operation_ledger_limit")
            if type(receipt_reserve) is not int or not 4096 <= receipt_reserve <= 32768:
                raise AgentError("agent_operation_receipt_reserve_invalid")
            row = {
                "binding": copy.deepcopy(binding),
                "preview": copy.deepcopy(preview),
                "status": "dispatched",
                "receipt_reserve": receipt_reserve,
                "operation_classification": copy.deepcopy(operation_classification),
                "host_pid": host_pid,
            }
            records[binding["operation_id"]] = row
            self._write(records)
            return copy.deepcopy(row), True

    def update(self, operation_id, status, **values):
        with file_transaction(self.lock):
            records = self._read()
            row = records[operation_id]
            row.update(status=status, **copy.deepcopy(values))
            if status in {"confirmed", "not_executed"}:
                row.pop("receipt_reserve", None)
            elif (
                status == "failed"
                and row.get("no_mutation") is True
                and row.get("cleanup_confirmed") is True
            ):
                from .execution_policy import verified_read_classification
                binding = row.get("binding") if isinstance(row.get("binding"), dict) else {}
                if verified_read_classification(
                    row,
                    task_id=binding.get("task_id"),
                    operation_id=operation_id,
                    tool=binding.get("tool"),
                ):
                    row.pop("receipt_reserve", None)
            self._write(records)

    def get(self, operation_id):
        with file_transaction(self.lock):
            return copy.deepcopy(self._read().get(operation_id))

    def forget_task(self, task_id):
        with file_transaction(self.lock):
            records = self._read()
            changed = False
            for op, row in list(records.items()):
                if row["binding"].get("task_id") != task_id or row.get("status") == "retired":
                    continue
                records[op] = {"binding": row["binding"], "status": "retired", "prior_status": row["status"],
                               "result_sha256": hashlib.sha256(encoded(row.get("result"))).hexdigest()}
                changed = True
            if changed:
                self._write(records)
            # atomic_io may leave encrypted pre-delete snapshots on hard kill.
            # This store lock excludes a live writer using the same temp prefix.
            for orphan in self.path.parent.glob("." + self.path.name + ".*.tmp"):
                orphan.unlink()
