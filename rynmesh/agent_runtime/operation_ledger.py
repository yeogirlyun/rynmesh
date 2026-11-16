"""Encrypted business receipts, separate from Agent task transactions."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..atomic_io import AtomicIOError, _fsync_dir, atomic_write_json, read_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .store import AgentError, AgentToolRejected, encoded

CHANNEL = b"rynmesh-agent-business-operations-v1"
LIMIT = 16 * 1024 * 1024
MAX_ACTIVE_OPERATIONS = 5000
_RECORD_FILE_RE = re.compile(r"^[0-9a-f]{24}\.json\Z")
_KEY_SENTINEL_NAME = "_key_sentinel.json"
_SENTINEL_PAYLOAD = b'{"version":1}'
_META_CACHE: dict[tuple[str, str, int, int], tuple[str, str, str, str]] = {}


def _record_filename(operation_id: str) -> str:
    digest = hashlib.sha256(str(operation_id).encode("utf-8")).hexdigest()[:24]
    return f"{digest}.json"


def _parse_iso_utc(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class OperationLedger:
    def __init__(self, home):
        self.home = Path(home)
        self.dir = self.home / "agent" / "business-operations"
        self.path = self.dir
        self.legacy_path = self.home / "agent" / "business-operations.json"
        self.migrated_path = self.home / "agent" / "business-operations.json.migrated"
        self.lock = self.home / "agent" / "business-operations.lock"
        self.quarantine_root = self.home / "agent" / "quarantine"
        self._reload_key()
        if self.legacy_path.exists():
            with file_transaction(self.lock):
                try:
                    self._ensure_migrated_locked()
                except AgentError:
                    # Keep construction usable when messaging.x25519 changed so
                    # health() / reset() can inspect and reset the ledger.
                    self._migration_pending = True

    def _reload_key(self) -> None:
        self.key = peer_box.load_or_create_messaging_key(self.home / "messaging.x25519")
        self.pub = peer_box.public_key_b64(self.key)

    def _cache_meta(self, target: Path, operation_id: str, row: dict) -> None:
        try:
            st = target.stat()
        except OSError:
            return
        if len(_META_CACHE) > 20000:
            _META_CACHE.clear()
        binding = row.get("binding") if isinstance(row.get("binding"), dict) else {}
        _META_CACHE[(str(target), self.pub, st.st_size, st.st_mtime_ns)] = (
            str(operation_id),
            str(binding.get("task_id") or ""),
            str(row.get("status") or ""),
            str(row.get("retired_at") or ""),
        )

    def _quarantine_path_locked(self, target: Path) -> Path | None:
        if not target.exists():
            return None
        ts = int(time.time())
        seq = 0
        while True:
            bucket = self.quarantine_root / f"{ts}-{seq}"
            dest = bucket / target.name
            if not dest.exists():
                break
            seq += 1
        bucket.mkdir(parents=True, exist_ok=True)
        os.replace(target, dest)
        return dest

    def _ensure_key_sentinel_locked(self) -> None:
        sentinel = self.dir / _KEY_SENTINEL_NAME
        if sentinel.is_file():
            return
        nonce, ciphertext = peer_box.seal(self.key, self.pub, _SENTINEL_PAYLOAD, info=CHANNEL)
        atomic_write_json(sentinel, {"nonce": nonce, "ciphertext": ciphertext}, max_bytes=4096)

    def _can_decrypt_file_locked(self, path: Path) -> tuple[bool, bool]:
        """Return ``(decrypted_ok, failed_with_invalid_tag)`` without quarantining."""
        try:
            value = read_json(path, max_bytes=LIMIT * 2)
            if not isinstance(value, dict):
                return False, False
            peer_box.open_sealed(self.key, self.pub, value["nonce"], value["ciphertext"], info=CHANNEL)
            return True, False
        except InvalidTag:
            return False, True
        except Exception:
            return False, False

    def _record_files_locked(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return [
            Path(entry.path)
            for entry in os.scandir(self.dir)
            if entry.is_file(follow_symlinks=False) and _RECORD_FILE_RE.match(entry.name)
        ]

    def _check_directory_key_locked(self) -> None:
        if not self.dir.is_dir():
            return
        sentinel = self.dir / _KEY_SENTINEL_NAME
        if sentinel.exists():
            ok, bad_tag = self._can_decrypt_file_locked(sentinel)
            if ok:
                return
            for record_file in self._record_files_locked():
                rec_ok, _ = self._can_decrypt_file_locked(record_file)
                if rec_ok:
                    sentinel.unlink(missing_ok=True)
                    self._ensure_key_sentinel_locked()
                    return
            if bad_tag or self._record_files_locked():
                raise AgentError("agent_operation_ledger_unreadable")
            self._quarantine_path_locked(sentinel)
            return
        files = self._record_files_locked()
        if not files:
            return
        saw_invalid_tag = False
        for record_file in files:
            rec_ok, bad_tag = self._can_decrypt_file_locked(record_file)
            if rec_ok:
                self._ensure_key_sentinel_locked()
                return
            saw_invalid_tag = saw_invalid_tag or bad_tag
        if saw_invalid_tag:
            raise AgentError("agent_operation_ledger_unreadable")

    def _ensure_migrated_locked(self) -> None:
        if not self.legacy_path.exists():
            return
        try:
            value = read_json(self.legacy_path, max_bytes=LIMIT * 2)
            raw = peer_box.open_sealed(self.key, self.pub, value["nonce"], value["ciphertext"], info=CHANNEL)
            if len(raw) > LIMIT:
                raise ValueError()
            records = json.loads(raw)
            if not isinstance(records, dict):
                raise ValueError()
        except Exception:
            raise AgentError("agent_operation_ledger_unreadable") from None
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ensure_key_sentinel_locked()
        migrated_now = datetime.now(timezone.utc).isoformat()
        for op, row in records.items():
            if not isinstance(op, str) or not op or not isinstance(row, dict):
                continue
            item = copy.deepcopy(row)
            binding = item.get("binding")
            if not isinstance(binding, dict):
                binding = {"operation_id": op}
                item["binding"] = binding
            elif "operation_id" not in binding:
                binding["operation_id"] = op
            if item.get("status") == "retired" and not item.get("retired_at"):
                item["retired_at"] = migrated_now
            self._write_one(op, item, fsync_dir=False)
        _fsync_dir(self.dir)
        os.replace(self.legacy_path, self.migrated_path)
        _fsync_dir(self.legacy_path.parent)

    def _read_path_locked(self, target: Path, *, expected_op: str | None = None) -> dict | None:
        if not target.exists():
            return None
        try:
            value = read_json(target, max_bytes=LIMIT * 2)
        except AtomicIOError as exc:
            cause = exc.__cause__
            if isinstance(cause, FileNotFoundError):
                return None
            if isinstance(cause, OSError):
                raise AgentError("agent_operation_ledger_unreadable") from None
            self._quarantine_path_locked(target)
            return None
        try:
            if not isinstance(value, dict):
                raise ValueError()
            raw = peer_box.open_sealed(self.key, self.pub, value["nonce"], value["ciphertext"], info=CHANNEL)
            if len(raw) > LIMIT:
                raise ValueError()
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get("binding"), dict):
                raise ValueError()
            stored_op = row["binding"].get("operation_id")
            if not isinstance(stored_op, str) or not stored_op:
                raise ValueError()
        except Exception:
            self._quarantine_path_locked(target)
            return None
        if expected_op is not None and stored_op != expected_op:
            raise AgentToolRejected("agent_operation_id_conflict")
        self._cache_meta(target, stored_op, row)
        return row

    def _read_one(self, operation_id: str) -> dict | None:
        with file_transaction(self.lock):
            self._ensure_migrated_locked()
            self._check_directory_key_locked()
            target = self.dir / _record_filename(operation_id)
            return self._read_path_locked(target, expected_op=operation_id)

    def _write_one(self, operation_id: str, row: dict, *, fsync_dir: bool = True) -> Path:
        with file_transaction(self.lock):
            raw = encoded(row)
            reserved = row.get("receipt_reserve", 0) if isinstance(row, dict) else 0
            if len(raw) + (reserved if type(reserved) is int and reserved > 0 else 0) > LIMIT:
                raise AgentError("agent_operation_ledger_limit")
            self._ensure_key_sentinel_locked()
            target = self.dir / _record_filename(operation_id)
            nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=CHANNEL)
            atomic_write_json(
                target,
                {"nonce": nonce, "ciphertext": ciphertext},
                max_bytes=LIMIT * 2,
                fsync_dir=fsync_dir,
            )
            self._cache_meta(target, operation_id, row)
            return target

    def _write(self, records: dict) -> None:
        """Compatibility writer used when tests intercept ``ledger._write``."""
        with file_transaction(self.lock):
            for op, row in records.items():
                self._write_one(op, row)

    def _commit_one_locked(self, operation_id: str, row: dict) -> None:
        if "_write" in self.__dict__:
            self._write({operation_id: row})
        else:
            self._write_one(operation_id, row)

    def _iter_record_meta_locked(self):
        if not self.dir.is_dir():
            return
        for entry in list(os.scandir(self.dir)):
            if not entry.is_file(follow_symlinks=False) or not _RECORD_FILE_RE.match(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            key = (entry.path, self.pub, st.st_size, st.st_mtime_ns)
            cached = _META_CACHE.get(key)
            if cached is not None:
                yield Path(entry.path), cached
                continue
            row = self._read_path_locked(Path(entry.path))
            if row is None:
                continue
            binding = row.get("binding") or {}
            meta = (
                str(binding.get("operation_id") or ""),
                str(binding.get("task_id") or ""),
                str(row.get("status") or ""),
                str(row.get("retired_at") or ""),
            )
            yield Path(entry.path), meta

    def _count_active_locked(self) -> int:
        return sum(1 for _path, (_op, _tid, status, _ret) in self._iter_record_meta_locked() if status != "retired")

    def _begin(self, binding, preview, status, extra=None, receipt_reserve=4096):
        with file_transaction(self.lock):
            self._ensure_migrated_locked()
            self._check_directory_key_locked()
            op_id = binding["operation_id"]
            prior = self._read_one(op_id)
            if prior:
                if prior.get("status") == "retired":
                    raise AgentToolRejected("agent_operation_retired")
                if prior["binding"] != binding:
                    raise AgentToolRejected("agent_operation_id_conflict")
                return copy.deepcopy(prior), False
            if self._count_active_locked() >= MAX_ACTIVE_OPERATIONS:
                raise AgentToolRejected("agent_operation_ledger_limit")
            if type(receipt_reserve) is not int or not 4096 <= receipt_reserve <= 32768:
                raise AgentError("agent_operation_receipt_reserve_invalid")
            row = {
                "binding": copy.deepcopy(binding),
                "preview": copy.deepcopy(preview),
                "status": status,
                "receipt_reserve": receipt_reserve,
            }
            if extra:
                row.update(copy.deepcopy(extra))
            self._commit_one_locked(op_id, row)
            return copy.deepcopy(row), True

    def begin(self, binding, preview, *, receipt_reserve=4096):
        return self._begin(binding, preview, "intent", None, receipt_reserve)

    def begin_dispatched(self, binding, preview, *, operation_classification, host_pid, receipt_reserve=4096):
        extra = {"operation_classification": operation_classification, "host_pid": host_pid}
        return self._begin(binding, preview, "dispatched", extra, receipt_reserve)

    def update(self, operation_id, status, **values):
        with file_transaction(self.lock):
            self._ensure_migrated_locked()
            self._check_directory_key_locked()
            row = self._read_one(operation_id)
            if row is None:
                raise KeyError(operation_id)
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
            self._commit_one_locked(operation_id, row)

    def get(self, operation_id):
        with file_transaction(self.lock):
            row = self._read_one(operation_id)
            return copy.deepcopy(row) if row is not None else None

    def forget_task(self, task_id):
        with file_transaction(self.lock):
            self._ensure_migrated_locked()
            self._check_directory_key_locked()
            retired_now = datetime.now(timezone.utc).isoformat()
            for path, (_op, rec_task_id, status, _ret) in list(self._iter_record_meta_locked()):
                if rec_task_id != task_id or status == "retired":
                    continue
                row = self._read_path_locked(path)
                if row is None or row.get("status") == "retired" or row["binding"].get("task_id") != task_id:
                    continue
                op_id = row["binding"]["operation_id"]
                tombstone = {
                    "binding": row["binding"],
                    "status": "retired",
                    "prior_status": row["status"],
                    "retired_at": retired_now,
                    "result_sha256": hashlib.sha256(encoded(row.get("result"))).hexdigest(),
                }
                self._commit_one_locked(op_id, tombstone)
            if self.dir.is_dir():
                for orphan in self.dir.glob(".*.tmp"):
                    try:
                        orphan.unlink()
                    except OSError:
                        pass
            for orphan in self.legacy_path.parent.glob("." + self.legacy_path.name + ".*.tmp"):
                try:
                    orphan.unlink()
                except OSError:
                    pass

    def prune_retired(self, older_than_days: float = 30) -> int:
        with file_transaction(self.lock):
            self._ensure_migrated_locked()
            self._check_directory_key_locked()
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            removed = 0
            for path, (_op, _tid, status, retired_at) in list(self._iter_record_meta_locked()):
                if status != "retired":
                    continue
                retired_dt = _parse_iso_utc(retired_at)
                if retired_dt is None or retired_dt > cutoff:
                    continue
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
            return removed

    def health(self) -> dict:
        with file_transaction(self.lock):
            try:
                self._ensure_migrated_locked()
                self._check_directory_key_locked()
                return {"state": "ok", "error": None, "reset_supported": True}
            except AgentError as exc:
                return {"state": "unreadable", "error": str(exc), "reset_supported": True}

    def reset(self) -> dict:
        with file_transaction(self.lock):
            self._reload_key()
            ts = int(time.time())
            seq = 0
            while True:
                bucket = self.quarantine_root / f"{ts}-{seq}"
                if not bucket.exists():
                    break
                seq += 1
            moved = False
            if self.dir.exists():
                bucket.mkdir(parents=True, exist_ok=True)
                os.replace(self.dir, bucket / "business-operations")
                moved = True
            if self.legacy_path.exists():
                bucket.mkdir(parents=True, exist_ok=True)
                os.replace(self.legacy_path, bucket / self.legacy_path.name)
                moved = True
            self.dir.mkdir(parents=True, exist_ok=True)
            self._ensure_key_sentinel_locked()
            return {"ok": True, "quarantine_id": bucket.name if moved else None}
